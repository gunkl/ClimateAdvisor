"""Issue #1745: a CI job that ran out of time is named, and a job near its limit is warned about.

Occupant first: nothing here changes a house. This is the CI guard that was missing when #1730's loop-lag probe made
the real-HA test driver 9x slower (#1743): the real-HA slices drifted from ~2 minutes to the 8-minute cap and some were
cancelled, and the required check's summary said only ``ha-slice=cancelled``. ``scripts/ci_job_times.sh`` now reports a
cancelled job with how long it ran and its limit, and warns about any job past 75% of its ``timeout-minutes``.

The script runs here for real under bash with a fake ``gh`` (as ``test_nightly_shards_1740.py`` does for the nightly's
report), reading the same jobs-API JSON shape through the script's own jq filter. The limits each workflow passes to
the script are compared with the workflow's ``timeout-minutes``, so a changed limit cannot leave the warning stale.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "ci_job_times.sh"
_VALIDATE = yaml.safe_load((_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))
_NIGHTLY = yaml.safe_load((_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8"))
_SHELL = pytest.mark.skipif(sys.platform == "win32", reason="runs the workflow's bash script")

# the fake gh serves the canned jobs JSON through the --jq filter the script passes (so the real filter is exercised)
_FAKE_GH = r"""#!/usr/bin/env bash
[ -e "$FAKE_DIR/api_fails" ] && { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
[ "$1" = api ] || { echo "unexpected gh $*" >&2; exit 1; }
printf '%s\n' "$2" > "$FAKE_DIR/api_path.txt"
filter=""
while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && filter="$2"; shift; done
jq -r ".jobs[] | ${filter#.jobs[] | }" < "$FAKE_DIR/jobs.json"
"""

_VALIDATE_LIMITS = "lint=5\nunit (*/8)=10\nisolated=15\ncoverage=5\ntyping=10\nha-real (*/4)=8\n"


def _job(name: str, conclusion: str | None, seconds: int | None, jid: int = 1) -> dict:
    started = "2026-11-28T10:00:00Z"
    done = (
        None
        if seconds is None
        else f"2026-11-28T{10 + seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}Z"
    )
    return {"id": jid, "name": name, "conclusion": conclusion, "started_at": started, "completed_at": done}


def _run(tmp_path: Path, mode: str, jobs: list[dict], limits: str = _VALIDATE_LIMITS, *, api_fails: bool = False):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    fake = tmp_path / "fake"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    if api_fails:
        (fake / "api_fails").write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "GH_REPO": "o/r",
        "RUN_ID": "900",
        "LIMITS": limits,
    }
    return subprocess.run(["bash", str(_SCRIPT), mode], env=env, capture_output=True, text=True, check=False)


# -- the report: a cancelled job is named with its duration and limit --
@_SHELL
def test_a_cancelled_slice_is_reported_with_its_duration_and_limit(tmp_path):
    run = _run(
        tmp_path,
        "timeouts",
        [
            _job("ha-real (2/4)", "cancelled", 8 * 60 + 3),
            _job("ha-real (1/4)", "success", 130),
            _job("lint", "success", 40),
        ],
    )
    assert run.returncode == 0
    assert run.stdout.splitlines() == [
        "::error title=Job timed out::ha-real (2/4) timed out or was cancelled after 8 min (limit 8 min)"
    ]
    assert (tmp_path / "fake" / "api_path.txt").read_text().strip() == "repos/o/r/actions/runs/900/jobs"


@_SHELL
def test_every_cancelled_job_is_named_and_unknown_or_unfinished_jobs_are_ignored(tmp_path):
    jobs = [
        _job("unit (3/8)", "cancelled", 600),
        _job("typing", "cancelled", 601),
        _job("ha-real", "cancelled", 5),  # the summary job's own name: no slice suffix, no limit known
        _job("some-new-job", "cancelled", 100),  # not in LIMITS: ignored, never an error
        _job("isolated", None, None),  # still running: no conclusion yet
        _job("ha-real (4/4)", "failure", 100),  # failed, not cancelled
    ]
    run = _run(tmp_path, "timeouts", jobs)
    assert run.stdout.splitlines() == [
        "::error title=Job timed out::unit (3/8) timed out or was cancelled after 10 min (limit 10 min)",
        "::error title=Job timed out::typing timed out or was cancelled after 10 min (limit 10 min)",
    ]


@_SHELL
def test_nightly_shard_names_use_the_shard_wording(tmp_path):
    limits = "generated-days (shard */4)=120\nha-lifecycle=60\n"
    run = _run(tmp_path, "timeouts", [_job("generated-days (shard 2/4)", "cancelled", 7203)], limits)
    assert "generated-days (shard 2/4) timed out or was cancelled after 120 min (limit 120 min)" in run.stdout


# -- the warning: over 75% of the limit, never a failure --
@_SHELL
@pytest.mark.parametrize(
    ("seconds", "warned"),
    [
        (60 * 6, False),
        (60 * 6 + 1, True),
        (60 * 7, True),
        (60 * 8, True),
        (30, False),
    ],  # ha-real limit 8 min: 6 min = 75%
)
def test_warns_only_above_75_percent_of_the_limit(tmp_path, seconds, warned):
    run = _run(tmp_path, "warn", [_job("ha-real (1/4)", "success", seconds)])
    assert run.returncode == 0
    assert bool(run.stdout.strip()) is warned
    if warned:
        assert run.stdout.startswith("::warning title=Job near its time limit::ha-real (1/4) ran ")
        assert "of its 8 min limit" in run.stdout


@_SHELL
def test_warn_mode_skips_cancelled_jobs_and_a_failed_job_still_warns_but_never_fails(tmp_path):
    jobs = [
        _job("ha-real (1/4)", "cancelled", 8 * 60),
        _job("unit (1/8)", "failure", 9 * 60),
        _job("lint", "success", 10),
    ]
    run = _run(tmp_path, "warn", jobs)
    assert run.returncode == 0
    assert run.stdout.count("::warning") == 1 and "unit (1/8) ran 9 min of its 10 min limit (90%)" in run.stdout


@_SHELL
def test_a_job_with_no_timestamps_does_not_break_the_report(tmp_path):
    odd = {"id": 9, "name": "ha-real (1/4)", "conclusion": "cancelled", "started_at": None, "completed_at": None}
    run = _run(tmp_path, "timeouts", [odd, _job("typing", "cancelled", 600)])
    assert run.returncode == 0
    assert "ha-real (1/4) timed out or was cancelled after 0 min (limit 8 min)" in run.stdout
    assert "typing timed out" in run.stdout


@_SHELL
def test_an_unreadable_api_gives_a_notice_and_exit_zero(tmp_path):
    for mode in ("timeouts", "warn"):
        run = _run(tmp_path / mode, mode, [], api_fails=True)
        assert run.returncode == 0  # reporting must never change a gate's verdict
        assert run.stdout.startswith("::notice title=Job times::could not read")


@_SHELL
def test_the_script_rejects_an_unknown_mode(tmp_path):
    assert _run(tmp_path, "explode", []).returncode == 2


# -- the wiring: the limits each workflow passes equal its timeout-minutes --
def _display_key(job: dict) -> str:
    name = job["name"].replace("${{ matrix.shard }}", "*")
    return name


def _timeout_map(workflow: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for job_id, job in workflow["jobs"].items():
        if "timeout-minutes" in job:
            out[_display_key({"name": job.get("name", job_id)})] = job["timeout-minutes"]
    return out


def _steps_running_script(workflow: dict) -> list[tuple[str, str, dict]]:
    found = []
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            m = re.search(r"scripts/ci_job_times\.sh (\w+)", step.get("run", ""))
            if m:
                found.append((job_id, m.group(1), step))
    return found


def _limits(step: dict) -> dict[str, int]:
    return {k: int(v) for k, v in (line.split("=") for line in step["env"]["LIMITS"].split("\n") if line.strip())}


def test_the_script_is_wired_into_the_summary_jobs_and_a_warning_job():
    steps = {(job, mode) for job, mode, _ in _steps_running_script(_VALIDATE)}
    assert steps == {("validate-and-test", "timeouts"), ("ha-real", "timeouts"), ("ci-timing", "warn")}
    assert {(job, mode) for job, mode, _ in _steps_running_script(_NIGHTLY)} == {("ci-timing", "warn")}


@pytest.mark.parametrize("workflow", [_VALIDATE, _NIGHTLY], ids=["validate", "nightly"])
def test_each_steps_limits_equal_the_workflows_timeout_minutes(workflow):
    expected = _timeout_map(workflow)
    for job_id, _mode, step in _steps_running_script(workflow):
        limits = _limits(step)
        # every limit named is a real job's, with the real number ...
        for key, minutes in limits.items():
            assert expected.get(key) == minutes, f"{job_id}: LIMITS {key}={minutes}, workflow says {expected.get(key)}"
        # ... and no job with a timeout is left out (the warning job watches all of its needs)
        if job_id == "ci-timing":
            watched = set(workflow["jobs"][job_id]["needs"])
            for watched_id in watched:
                job = workflow["jobs"][watched_id]
                if "timeout-minutes" in job:
                    assert _display_key({"name": job.get("name", watched_id)}) in limits, watched_id


def test_the_gate_jobs_keep_their_pass_fail_semantics_and_get_the_least_permission_that_works():
    gate = _VALIDATE["jobs"]["validate-and-test"]
    ha = _VALIDATE["jobs"]["ha-real"]
    assert gate["if"] == ha["if"] == "always()"
    for job in (gate, ha):
        assert job["permissions"] == {"contents": "read", "actions": "read"}
    # the report steps run before the verdict step and never decide it
    for job, needed in ((gate, ["lint", "unit", "isolated", "coverage", "typing"]), (ha, ["ha-slice"])):
        report = next(s for s in job["steps"] if "ci_job_times.sh" in s.get("run", ""))
        verdict = job["steps"][-1]
        assert job["steps"].index(report) < job["steps"].index(verdict)
        assert report["if"] == "always()"
        for dep in needed:
            assert f"test '${{{{ needs.{dep}.result }}}}' = success" in verdict["run"]
    warn = _VALIDATE["jobs"]["ci-timing"]
    assert warn["if"] == "always()" and warn["permissions"] == {"contents": "read", "actions": "read"}
    assert set(warn["needs"]) == {"lint", "unit", "isolated", "coverage", "typing", "ha-slice"}
    assert set(_NIGHTLY["jobs"]["ci-timing"]["needs"]) == {
        "seeds",
        "generated-days",
        "waiver-ledger",
        "surfaces",
        "ha-lifecycle",
    }
