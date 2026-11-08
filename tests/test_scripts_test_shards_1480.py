"""``scripts/test.py`` slice switches: ``CA_HA_SHARDS`` (Issue #1480) and the ``CA_UNIT_SHARDS`` it shares code with.

Developer tooling only. The real ``uv run ... pytest`` is replaced by a tiny Python child that echoes its
arguments, so these tests check what each slice would run (the ``--ca-shard K/N`` split CI uses, the worker
count, the real-HA config) and how the runner reports and fails, without starting Home Assistant.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location("ca_scripts_test_runner", REPO / "scripts" / "test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(monkeypatch):
    """The runner with uv faked: each slice's child prints its pytest args and fails when told to."""
    module = _load_runner()
    calls: list[tuple[str, str, list[str]]] = []
    syncs: list[list[str]] = []

    def fake_local_cmd(group, venv, pytest_args):
        calls.append((group, venv, list(pytest_args)))
        code = (
            "import sys; args = sys.argv[1:]; print('ARGS', ' '.join(args)); "
            "sys.exit(3 if any(a.startswith('FAIL_SLICE=') and a.split('=')[1] in args for a in args) else 0)"
        )
        return [sys.executable, "-c", code, *pytest_args], {}

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "sync" in cmd:
            syncs.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(module, "_local_cmd", fake_local_cmd)
    monkeypatch.setattr(module, "_uv_cmd", lambda: ["uv"])
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.os, "name", "posix")
    for var in ("CA_HA_SHARD", "CA_HA_SHARDS", "CA_HA_WORKERS", "CA_UNIT_SHARD", "CA_UNIT_SHARDS", "CA_UNIT_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CA_COMPONENT_ROOT", raising=False)
    module.calls, module.syncs = calls, syncs
    return module


def _slice_calls(runner):
    return [c for c in runner.calls if "--version" not in c[2]]


def _shard_of(args: list[str]) -> str:
    return args[args.index("--ca-shard") + 1]


def test_ha_shards_runs_every_ci_slice_once_with_its_workers(runner, monkeypatch, capsys):
    monkeypatch.setenv("CA_HA_SHARDS", "4")
    monkeypatch.setenv("CA_HA_WORKERS", "6")

    assert runner.main(["ha"]) == 0

    slices = _slice_calls(runner)
    assert sorted(_shard_of(args) for _, _, args in slices) == ["1/4", "2/4", "3/4", "4/4"]
    for group, venv, args in slices:
        assert (group, venv) == ("test-ha", ".venv-test-ha")
        assert args[:3] == ["-c", "tests_ha/pytest.ini", "--rootdir=tests_ha"]
        assert args[args.index("-n") + 1] == "6"
        assert "tests_ha/" in args
        assert args.count("--ca-shard") == 1
    # The real-HA venv is synced once up front, not raced by four slices.
    assert len(runner.syncs) == 1 and runner.syncs[0][-1] == "test-ha"

    out = capsys.readouterr().out
    for k in range(1, 5):
        assert f"[{k}/4] ARGS " in out
        assert f"  [{k}/4] passed in " in out
    assert "ha tier in 4 slices:" in out


def test_ha_shards_exits_nonzero_when_one_slice_fails(runner, monkeypatch, capsys):
    monkeypatch.setenv("CA_HA_SHARDS", "3")

    assert runner.main(["ha", "FAIL_SLICE=2/3"]) == 3

    out = capsys.readouterr().out
    assert "  [2/3] FAILED (exit 3) in " in out
    assert "  [1/3] passed in " in out and "  [3/3] passed in " in out


def test_ha_shards_keeps_a_named_target(runner, monkeypatch):
    monkeypatch.setenv("CA_HA_SHARDS", "2")

    assert runner.main(["ha", "tests_ha/test_house_smoke.py"]) == 0

    for _, _, args in _slice_calls(runner):
        assert "tests_ha/test_house_smoke.py" in args and "tests_ha/" not in args


def test_ha_single_slice_and_plain_runs_unchanged(runner, monkeypatch):
    monkeypatch.setenv("CA_HA_SHARD", "2/4")
    monkeypatch.setenv("CA_HA_WORKERS", "4")
    assert runner.main(["ha"]) == 0
    monkeypatch.delenv("CA_HA_SHARD")
    assert runner.main(["ha"]) == 0

    one, plain = runner.calls
    assert _shard_of(one[2]) == "2/4" and one[2][one[2].index("-n") + 1] == "4"
    assert "--ca-shard" not in plain[2]
    assert runner.syncs == []


@pytest.mark.parametrize("tier", ["ha", "unit"])
def test_shards_and_single_shard_together_is_refused(runner, monkeypatch, capsys, tier):
    prefix = "CA_HA" if tier == "ha" else "CA_UNIT"
    monkeypatch.setenv(f"{prefix}_SHARDS", "4")
    monkeypatch.setenv(f"{prefix}_SHARD", "1/4")

    with pytest.raises(SystemExit) as exc:
        runner.main([tier])

    assert exc.value.code == 2
    assert "not both" in capsys.readouterr().err
    assert runner.calls == []


@pytest.mark.parametrize("value", ["four", "-1", "2/4"])
def test_ha_shards_rejects_a_non_number(runner, monkeypatch, value):
    monkeypatch.setenv("CA_HA_SHARDS", value)

    with pytest.raises(SystemExit) as exc:
        runner.main(["ha"])

    assert exc.value.code == 2


def test_ha_shards_zero_means_one_process(runner, monkeypatch):
    monkeypatch.setenv("CA_HA_SHARDS", "0")

    assert runner.main(["ha"]) == 0

    assert len(runner.calls) == 1 and "--ca-shard" not in runner.calls[0][2]


def test_ha_shards_refused_on_windows(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setenv("CA_HA_SHARDS", "4")

    assert runner.main(["ha"]) == 2

    assert "not supported on Windows" in capsys.readouterr().err
    assert runner.calls == []


def test_unit_shards_still_runs_every_slice(runner, monkeypatch, capsys):
    monkeypatch.setenv("CA_UNIT_SHARDS", "4")
    monkeypatch.setenv("CA_UNIT_WORKERS", "2")

    assert runner.main(["unit"]) == 0

    slices = _slice_calls(runner)
    assert sorted(_shard_of(args) for _, _, args in slices) == ["1/4", "2/4", "3/4", "4/4"]
    for group, venv, args in slices:
        assert (group, venv) == ("test", ".venv-test")
        assert args[args.index("-n") + 1] == "2"
        assert "tests/" in args
    assert len(runner.syncs) == 1 and runner.syncs[0][-1] == "test"
    assert "unit tier in 4 slices:" in capsys.readouterr().out
