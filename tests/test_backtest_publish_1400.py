"""Issue #1400 item 4: the v4 backtest file gets onto the install as a deploy step, in two SSH connections.

Occupant view: the Prediction accuracy page's history, gate rows and ``gate_summary`` come from the per-zone file
``climate_advisor_v4_backtest_<entry>.json``, which only ``tools/predict_backtest.py --publish`` writes; on 0.9.4 no one
had run it, so the scorecard was empty. ``tools/deploy.py`` now runs it after every deploy (``--no-backtest`` skips it)
with ``--batch-ssh``: one connection reads every input, one writes every zone's file (the HA SSH add-on blocks a
burst of connections). A failed backtest never fails the deploy.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import deploy  # noqa: E402
import predict_backtest as pb  # noqa: E402


def _run_locally(config_dir: Path):
    """A fake ``subprocess.run`` that runs the remote command with ``sh`` against ``config_dir`` instead of /config."""
    calls: list[str] = []
    real_run = subprocess.run

    def run(cmd, **kw):
        remote = cmd[-1].replace("/config", str(config_dir))
        calls.append(remote)
        return real_run(["sh", "-c", remote], input=kw.get("input"), capture_output=True, timeout=60)

    return run, calls


def test_batch_fetch_reads_every_zone_and_the_time_zone_in_one_connection(tmp_path, monkeypatch):
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "core.config").write_text(json.dumps({"data": {"time_zone": "America/Los_Angeles"}}))
    for z in ("aaa", "bbb"):
        (tmp_path / f"climate_advisor_chart_log_{z}.json").write_text(json.dumps({"entries": [{"ts": z}]}))
    (tmp_path / "climate_advisor_learning_aaa.json").write_text(json.dumps({"db": 1}))
    run, calls = _run_locally(tmp_path)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", "host", remote])
    tz, zones = pb.batch_fetch({}, None)
    assert len(calls) == 1
    assert tz == "America/Los_Angeles"
    assert set(zones) == {"aaa", "bbb"}
    assert zones["aaa"] == {
        "chart": {"entries": [{"ts": "aaa"}]},
        "learning": {"db": 1},
        "archive": None,
        "cycles": None,
    }  # Issues #1512/#1516: the batch also reads the cycle file
    assert zones["bbb"]["learning"] is None
    _, one = pb.batch_fetch({}, "bbb")
    assert set(one) == {"bbb"}


def test_batch_fetch_falls_back_to_the_legacy_unscoped_files(tmp_path, monkeypatch):
    (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": []}))
    run, _ = _run_locally(tmp_path)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", "host", remote])
    tz, zones = pb.batch_fetch({}, None)
    assert tz is None and zones == {
        "": {"chart": {"entries": []}, "learning": None, "archive": None, "cycles": None}
    }  # the cycles kind is part of every zone


def test_parse_ignores_garbage_blobs():
    blob = base64.b64encode(b"not json").decode()
    tz, zones = pb.parse_batch_output(f"___CA_ZONE z\n___CA_FILE chart\n{blob}\n___CA_TZ\n\n")
    assert tz is None and zones == {
        "z": {"chart": None, "learning": None, "archive": None, "cycles": None}
    }  # the cycles kind is part of every zone


def test_batch_publish_writes_every_zone_private_in_one_connection(tmp_path, monkeypatch):
    run, calls = _run_locally(tmp_path)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", "host", remote])
    dests = pb.publish_remote_batch({}, {"aaa": {"zone": "aaa"}, "bbb": {"zone": "bbb"}})
    assert len(calls) == 1
    assert dests == [
        "/config/climate_advisor_v4_backtest_aaa.json",
        "/config/climate_advisor_v4_backtest_bbb.json",
    ]
    for z in ("aaa", "bbb"):
        f = tmp_path / f"climate_advisor_v4_backtest_{z}.json"
        assert json.loads(f.read_text()) == {"zone": z}
        assert f.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".ca_backtest_publish").exists()


def test_batch_publish_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"reset"))
    monkeypatch.setattr(pb, "ssh_cmd", lambda cfg, remote: ["ssh", remote])
    with pytest.raises(RuntimeError, match="reset"):
        pb.publish_remote_batch({}, {"a": {}})


# ------------------------------------------------------------------------------------------------- deploy step
def test_deploy_runs_the_batched_backtest_for_every_zone():
    cmd = deploy.backtest_command()
    assert cmd[1].endswith("predict_backtest.py")
    assert {"--all-zones", "--publish", "--batch-ssh"} <= set(cmd)


def test_a_published_backtest_reports_ok():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
        out = "table...\npublished /config/climate_advisor_v4_backtest_a.json\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert deploy.publish_backtest(run) is True
    assert seen["timeout"] == deploy.BACKTEST_TIMEOUT_S


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, "", "Connection reset by peer"),
        subprocess.CompletedProcess([], 0, "nothing published", ""),
        subprocess.TimeoutExpired("x", 1),
        OSError("no python"),
    ],
)
def test_a_failed_backtest_only_warns(outcome, capsys):
    def run(cmd, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert deploy.publish_backtest(run) is False  # returns, never raises or exits: the deploy is complete
    assert "deploy itself is complete" in capsys.readouterr().out


def test_no_backtest_flag_and_the_step_comes_after_the_deploy():
    src = (ROOT / "tools" / "deploy.py").read_text()
    main = src[src.index("def main()") :]
    assert '"--no-backtest"' in main
    assert main.index("deploy_files(config") < main.index("publish_backtest()")
    assert "if not args.no_backtest:" in main
