"""Issue #1740: the nightly generated-days job in 4 shards, timeouts reported as timeouts, auto-diagnosis of a
cancelled run, and closing resolved nightly-failure issues on a green run.

Occupant first: nothing here changes a house. The nightly is how a rare household-day failure is found before a
customer finds it, and on 2026-11-26 its generated-days job hit the 120-minute cap and was cancelled with nothing
filed that said why. These tests pin (1) that the four shards run exactly the seeds of one unsharded run, (2) the
workflow wiring that depends on the job being a matrix, (3) the report-failure and close-resolved scripts, run for
real under bash with a fake ``gh``, and (4) the ledger merge that keeps the waiver ledger's stale verdict exact.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.sim_harness import nightly_ledger as nl
from tools.sim_harness import replay

_ROOT = Path(__file__).resolve().parent.parent
_NIGHTLY = yaml.safe_load((_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8"))
_AUTOFIX = yaml.safe_load((_ROOT / ".github/workflows/nightly-autofix.yml").read_text(encoding="utf-8"))
_JOBS = _NIGHTLY["jobs"]
_SHELL = pytest.mark.skipif(sys.platform == "win32", reason="runs the workflow's bash scripts")


def _script(job: str, step_name_part: str) -> str:
    step = next(s for s in _JOBS[job]["steps"] if step_name_part in s.get("name", ""))
    assert "${{" not in step["run"], "the script must take its inputs from env so it can run outside Actions"
    return step["run"]


# ── (1) the split: a partition of the seed list ──────────────────────────────────────────────────────────────────
def _run_seeds_step(tmp_path: Path, count: str, days: str = "250", run_number: str = "4321") -> dict[str, str]:
    out = tmp_path / "out.txt"
    out.write_text("")
    env = {
        **os.environ,
        "INPUT_SEEDS": count,
        "INPUT_DAYS": days,
        "GITHUB_RUN_NUMBER": run_number,
        "GITHUB_OUTPUT": str(out),
    }
    subprocess.run(
        ["bash", "-e", "-c", _script("seeds", "Pick fresh master seeds")], env=env, check=True, capture_output=True
    )
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


@_SHELL
@pytest.mark.parametrize("count", ["1", "3", "4", "5", "12", "13", "100"])
def test_shards_are_a_disjoint_cover_of_the_seed_list(tmp_path, count):
    out = _run_seeds_step(tmp_path, count)
    full = out["list"].split()
    assert len(full) == int(count)
    shards = [out[f"shard_{k}"].split() for k in (1, 2, 3, 4)]
    flat = [s for sh in shards for s in sh]
    assert sorted(flat) == sorted(full)  # every seed ran
    assert len(flat) == len(set(flat))  # and in exactly one shard
    # round-robin: shard k holds positions k-1, k+3, ...; sizes differ by at most one seed
    for k, sh in enumerate(shards):
        assert sh == full[k::4]
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


@_SHELL
def test_default_run_gives_each_shard_three_seeds(tmp_path):
    out = _run_seeds_step(tmp_path, "12")
    assert [len(out[f"shard_{k}"].split()) for k in (1, 2, 3, 4)] == [3, 3, 3, 3]
    assert out["days"] == "250"
    # fewer seeds than shards: the later shards are empty (their job runs nothing and passes)
    two = _run_seeds_step(tmp_path, "2")
    assert two["shard_3"] == "" and two["shard_4"] == "" and len(two["shard_1"].split()) == 1


def test_generated_days_is_a_four_shard_matrix_with_its_own_cap():
    job = _JOBS["generated-days"]
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert job["strategy"]["fail-fast"] is False  # one slow shard must not cancel the other three
    assert job["timeout-minutes"] == 120  # each shard keeps the cap: about 4x headroom
    assert job["name"] == "generated-days (shard ${{ matrix.shard }}/4)"
    run = next(s for s in job["steps"] if "household promises" in s["name"])
    assert "--ledger-defer-stale" in run["run"] and "--hits-out" in run["run"]
    assert "--perf-report" in run["run"] and "--waiver-ledger" in run["run"]
    assert run["env"]["SHARD_SEEDS"] == "${{ needs.seeds.outputs[format('shard_{0}', matrix.shard)] }}"
    outputs = _JOBS["seeds"]["outputs"]
    assert all(f"shard_{k}" in outputs for k in (1, 2, 3, 4))


def test_every_artifact_name_in_the_matrix_job_is_per_shard():
    names = [s["with"]["name"] for s in _JOBS["generated-days"]["steps"] if "upload-artifact" in s.get("uses", "")]
    assert len(names) == 3 and len(set(names)) == 3
    assert all(n.endswith("-${{ matrix.shard }}") for n in names)


def test_jobs_that_need_generated_days_are_wired_for_the_matrix():
    assert _JOBS["waiver-ledger"]["needs"] == ["seeds", "generated-days"]
    # no `if`: it runs only when every shard succeeded, and is skipped (not failed) otherwise
    assert "if" not in _JOBS["waiver-ledger"]
    needs = set(_JOBS["report-failure"]["needs"])
    assert {"generated-days", "waiver-ledger"} <= needs
    assert set(_JOBS["close-resolved"]["needs"]) == {
        "seeds",
        "generated-days",
        "waiver-ledger",
        "surfaces",
        "ha-lifecycle",
    }
    for name, job in _JOBS.items():
        for dep in [job.get("needs")] if isinstance(job.get("needs"), str) else job.get("needs", []):
            assert dep in _JOBS, f"{name} needs unknown job {dep}"


# ── (4) the ledger merge ─────────────────────────────────────────────────────────────────────────────────────────
def _entry(eid: str, cause: str) -> dict:
    return {
        "id": eid,
        "issue": 1133,
        "promise": "override_respected",
        "match": {"cause": cause},
        "reason": "r",
        "added": "2026-11-20",
        "expires": "2026-12-30",
        "expires_on": date(2026, 12, 30),
    }


def _hit(promise="override_respected", cause="pause"):
    return SimpleNamespace(promise=promise, mechanism={"cause": cause}, detail="d")


class _Open(nl.IssueStates):
    def states(self, numbers):
        return {n: "open" for n in numbers}, []


def test_a_shard_that_misses_a_waivers_hit_is_not_stale_but_the_union_decides():
    ledger = [_entry("w-a", "pause"), _entry("w-b", "door")]
    kw = {"today": date(2026, 11, 28), "issue_states": _Open()}
    shard1 = [(1, _hit(cause="pause"))]  # reaches w-a only
    shard2 = [(2, _hit(cause="door"))]  # reaches w-b only
    # alone, each shard would call the other's waiver stale; deferring leaves it to the merge
    assert (
        nl.evaluate(shard1, ledger, **kw).problems and not nl.evaluate(shard1, ledger, check_stale=False, **kw).problems
    )
    assert nl.evaluate(shard1, ledger, check_stale=False, **kw).clean
    assert nl.evaluate(shard1 + shard2, ledger, **kw).clean
    # a waiver no shard reached is stale in the union
    union = nl.evaluate(shard1, [*ledger], **kw)
    assert any("stale waiver w-b" in p for p in union.problems)
    # an unlisted hit still fails the shard even with the stale check deferred
    assert not nl.evaluate([(3, _hit(cause="x"))], ledger, check_stale=False, **kw).clean


def test_hits_round_trip_and_judge_hits_is_the_union_verdict(tmp_path, capsys):
    today = date.today()
    entries = [
        {
            "id": "w-a",
            "issue": 1133,
            "promise": "override_respected",
            "match": {"cause": "pause"},
            "reason": "r",
            "added": today.isoformat(),
            "expires": (today + timedelta(days=30)).isoformat(),
        }
    ]
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    state_file = tmp_path / "states.json"
    state_file.write_text(json.dumps({"1133": "open"}), encoding="utf-8")
    h = SimpleNamespace(promise="override_respected", mechanism={"cause": "pause"}, detail="d")
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    replay.write_hits([(7, h)], str(a))
    replay.write_hits([], str(b))
    args = (str(ledger_path), "offline", str(state_file))
    assert replay.cli_judge_hits([str(a), str(b)], *args) == 0
    assert replay.cli_judge_hits([str(b)], *args) == 1  # nobody reached the waiver: stale over the union
    assert "stale waiver w-a" in capsys.readouterr().out
    assert replay.cli_judge_hits([str(a), str(tmp_path / "missing.json")], *args) == 1  # a lost shard file is red


# ── (2) report-failure: timeouts are reported as timeouts ────────────────────────────────────────────────────────
_FAKE_GH = r"""#!/usr/bin/env bash
# fake gh for the nightly scripts: records writes, serves canned reads from $FAKE_DIR
d="$FAKE_DIR"
case "$1 $2" in
  "label create") exit 0 ;;
  "api repos/"*) cat "$d/jobs.tsv" ;;
  "run view") cat "$d/log_$3_$4.txt" 2>/dev/null || true ;;
  "issue list") cat "$d/issue_list.txt" 2>/dev/null || true ;;
  "issue view") cat "$d/body_$3.txt" ;;
  "issue create") printf '%s\n' "$*" >> "$d/created.txt" ;;
  "issue comment") printf '%s\n' "$*" >> "$d/commented.txt" ;;
  "issue close") printf '%s\n' "$*" >> "$d/closed.txt" ;;
  *) echo "unexpected gh $*" >&2; exit 1 ;;
esac
"""


def _fake_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    fake = tmp_path / "fake"
    fake.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "GH_REPO": "o/r",
        "RUN_ID": "900",
        "RUN_URL": "https://example/runs/900",
        "GITHUB_SHA": "abc123",
        "SEEDS": "success",
        "GENERATED": "cancelled",
        "LEDGER": "skipped",
        "SURFACES": "success",
        "HA": "success",
        **extra,
    }


def _report(tmp_path: Path, rows: list[tuple], logs: dict[str, str] | None = None, **env) -> str:
    """Run the report-failure script; return the text it passed to `gh issue create` / `comment`."""
    e = _fake_env(tmp_path, **env)
    fake = Path(e["FAKE_DIR"])
    (fake / "jobs.tsv").write_text("".join("\t".join(map(str, r)) + "\n" for r in rows), encoding="utf-8")
    for jid, text in (logs or {}).items():
        (fake / f"log_--job_{jid}.txt").write_text(text, encoding="utf-8")
    cwd = tmp_path / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", "-e", "-c", _script("report-failure", "File (or update)")],
        env=e,
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    for name in ("created.txt", "commented.txt"):
        if (fake / name).exists():
            return (fake / name).read_text(encoding="utf-8")
    raise AssertionError("neither an issue nor a comment was written")


def _sig(text: str) -> str:
    return re.search(r"nightly-signature: ([0-9a-f]{12})", text).group(1)


@_SHELL
def test_a_cancelled_shard_is_reported_as_a_timeout_with_its_duration(tmp_path):
    text = _report(tmp_path, [(11, "generated-days (shard 2/4)", "cancelled", 7203)])
    assert "`generated-days (shard 2/4)` timed out or was cancelled after 2h 0m 3s" in text
    assert "no failing test line found" not in text
    assert "(timed out)" in text and "Nightly failure" in text


@_SHELL
def test_timeouts_of_any_shard_share_a_signature_and_differ_from_a_test_failure(tmp_path):
    shard2 = _sig(_report(tmp_path / "a", [(11, "generated-days (shard 2/4)", "cancelled", 7200)]))
    shard3 = _sig(_report(tmp_path / "b", [(12, "generated-days (shard 3/4)", "cancelled", 7300)]))
    assert shard2 == shard3  # repeated timeouts dedupe, whichever shard is slow that night
    failure = _sig(
        _report(
            tmp_path / "c",
            [(13, "generated-days (shard 2/4)", "failure", 3000)],
            {13: "x\nUNWAIVED override_respected day_seed=5001000007: detail\n"},
        )
    )
    assert failure != shard2  # a promise hit is not a timeout
    other_job = _sig(_report(tmp_path / "d", [(14, "ha-lifecycle", "cancelled", 3600)]))
    assert other_job != shard2  # another job's timeout is its own issue


@_SHELL
def test_a_test_failure_keeps_listing_its_failing_test_and_a_plain_failure_keeps_the_old_text(tmp_path):
    text = _report(
        tmp_path / "a", [(21, "ha-lifecycle", "failure", 100)], {21: "FAILED tests_ha/test_x.py::test_y - boom\n"}
    )
    assert "- `tests_ha/test_x.py::test_y`" in text and "timed out" not in text
    text = _report(tmp_path / "b", [(22, "surfaces", "failure", 100)], {22: "no test line\n"})
    assert "no failing test line found; see the failed jobs: surfaces" in text


def test_the_jobs_query_yields_the_duration_the_script_reads():
    """The jq filter, run on a sample of the jobs API, gives `<id> TAB <name> TAB <conclusion> TAB <seconds>`."""
    script = _script("report-failure", "File (or update)")
    m = re.search(r"--jq '([^']+)'", script)
    assert m
    sample = {
        "jobs": [
            {
                "id": 5,
                "name": "generated-days (shard 1/4)",
                "conclusion": "cancelled",
                "started_at": "2026-11-26T06:20:00Z",
                "completed_at": "2026-11-26T08:20:03Z",
            },
            {
                "id": 6,
                "name": "seeds",
                "conclusion": "success",
                "started_at": "2026-11-26T06:18:00Z",
                "completed_at": "2026-11-26T06:18:05Z",
            },
            {
                "id": 7,
                "name": "report-failure",
                "conclusion": None,
                "started_at": "2026-11-26T08:21:00Z",
                "completed_at": None,
            },
            # a job with no timestamps must not make jq fail (set -o pipefail would then file no issue at all)
            {"id": 8, "name": "surfaces", "conclusion": "cancelled", "started_at": None, "completed_at": None},
        ]
    }
    out = subprocess.run(["jq", "-r", m.group(1)], input=json.dumps(sample), capture_output=True, text=True, check=True)
    assert out.stdout == "5\tgenerated-days (shard 1/4)\tcancelled\t7203\n8\tsurfaces\tcancelled\t0\n"


# ── (3) auto-diagnosis accepts a cancelled run ───────────────────────────────────────────────────────────────────
def test_autofix_runs_on_failure_or_cancelled_and_keeps_its_permissions():
    job = _AUTOFIX["jobs"]["diagnose"]
    cond = job["if"]
    assert "workflow_run.conclusion == 'failure'" in cond and "workflow_run.conclusion == 'cancelled'" in cond
    assert "workflow_dispatch" in cond
    assert "success" not in cond
    assert job["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
        "actions": "read",
        "id-token": "write",
    }
    step = next(s for s in job["steps"] if s.get("id") == "claude")
    prompt = step["with"]["prompt"]
    assert "timed out or was cancelled after" in prompt and "RUNTIME" in prompt
    assert "shard" in prompt and "earlier runs" in prompt
    assert "Raising a timeout or editing the workflow is not yours to do" in prompt


# ── close-resolved ───────────────────────────────────────────────────────────────────────────────────────────────
def _close(tmp_path: Path, issues: dict[int, str]) -> tuple[str, str]:
    e = _fake_env(tmp_path)
    fake = Path(e["FAKE_DIR"])
    (fake / "issue_list.txt").write_text("".join(f"{n}\n" for n in issues), encoding="utf-8")
    for n, body in issues.items():
        (fake / f"body_{n}.txt").write_text(body, encoding="utf-8")
    subprocess.run(
        ["bash", "-e", "-c", _script("close-resolved", "Close the nightly-failure")],
        env=e,
        check=True,
        capture_output=True,
    )
    closed = (fake / "closed.txt").read_text() if (fake / "closed.txt").exists() else ""
    commented = (fake / "commented.txt").read_text() if (fake / "commented.txt").exists() else ""
    return closed, commented


_FILED = (
    "Nightly run failed: https://x/runs/1 (commit a).\n\n**Jobs:** seeds=success, generated-days=failure, "
    "surfaces=success, ha-lifecycle=success\n\n<!-- nightly-signature: aaaaaaaaaaaa -->\n<!-- nightly-run: 1 -->\n"
)


@_SHELL
def test_a_green_run_closes_only_issues_the_workflow_filed_for_jobs_that_just_passed(tmp_path):
    closed, commented = _close(
        tmp_path,
        {
            10: _FILED,
            11: "hand-written issue mentioning nightly\n",  # label but no markers: not ours
            12: _FILED.replace("generated-days=failure", "retired-job=failure"),  # a job this run did not pass
            13: _FILED.replace("generated-days=failure", "generated-days=success"),  # cannot tell what failed
            14: _FILED.replace("generated-days=failure", "generated-days=cancelled"),  # a timeout counts
            # a "Jobs" line copied into a hand-written issue, without the hidden markers: not ours
            15: _FILED.split("<!--")[0],
        },
    )
    assert sorted(re.findall(r"issue close (\d+) --reason completed", closed)) == ["10", "14"]
    assert "https://example/runs/900" in commented and "nightly-closed-by: 900" in commented
    assert "10 " in commented and "14 " in commented and "11 " not in commented and "15 " not in commented


@_SHELL
@pytest.mark.parametrize(
    ("shards", "files", "ok"),
    [
        (["1 5", "2 6", "3", ""], [1, 2, 3], True),  # an empty shard leaves no file and is not expected to
        (["1 5", "2 6", "3", "4"], [1, 2, 3], False),  # a shard with seeds left no hits file: the union is incomplete
    ],
)
def test_the_waiver_ledger_job_refuses_an_incomplete_union(tmp_path, shards, files, ok):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$FAKE_DIR/uv_args.txt"\n', encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    work = tmp_path / "work"
    for k in files:
        (work / "shard-hits" / f"shard-hits-{k}").mkdir(parents=True)
        (work / "shard-hits" / f"shard-hits-{k}" / "shard_hits.json").write_text('{"hits": []}', encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        **{f"SHARD_{k}": v for k, v in enumerate(shards, 1)},
    }
    run = subprocess.run(
        ["bash", "-e", "-c", _script("waiver-ledger", "Waiver ledger judged")],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (run.returncode == 0) is ok, run.stderr
    if ok:
        args = (tmp_path / "uv_args.txt").read_text(encoding="utf-8")
        assert "--judge-hits" in args and all(f"shard-hits-{k}/shard_hits.json" in args for k in files)
    else:
        assert "expected 4 shard hits files, found 3" in run.stderr
        assert not (tmp_path / "uv_args.txt").exists()


def test_close_resolved_runs_only_after_a_fully_green_default_run_and_needs_only_issues_write():
    job = _JOBS["close-resolved"]
    assert job["permissions"] == {"issues": "write"}
    cond = job["if"]
    for name in ("seeds", "generated-days", "waiver-ledger", "surfaces", "ha-lifecycle"):
        assert f"needs.{name}.result == 'success'" in cond
    assert "github.event_name == 'schedule'" in cond
    # a manual run counts only on main with the default seeds and days
    assert (
        "github.ref == 'refs/heads/main'" in cond and "inputs.seeds == '12'" in cond and "inputs.days == '250'" in cond
    )
    assert "cancelled" not in cond and "always()" not in cond
