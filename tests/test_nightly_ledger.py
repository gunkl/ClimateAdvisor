"""Nightly promise-hit mechanism evidence and the strict, issue-linked waiver ledger (Issue #1153).

Occupant first: the nightly generated days are how a rare household-day failure is found before a
customer finds it.  Their seeds are fresh each run, so a known hit has to be recognised by the
MECHANISM that produced it - and a tolerated hit must never become a permanent exemption.  These tests
pin both halves: the mechanism record a hit carries (every field present, ``None`` where unknowable),
and the ledger that fails the gate for a closed issue, a passed expiry, a hit that no longer occurs,
a duplicate, or a lookup that cannot prove the issue is open.

Nothing here re-implements a promise or the ledger rule: hits come from the production registry on a
real generated day (``run_day``) and from hand-built journal inputs; expectations are written out
literally, not derived with the code under test.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.invariant_watchdog import PROMISES, Hit
from custom_components.climate_advisor.promise_journal import Command, Episode, Mark, PromiseInput, Reading
from tools.sim_harness import mechanism as mech
from tools.sim_harness import nightly_ledger as nl
from tools.sim_harness.generator import run_day
from tools.sim_harness.replay import judge_with_ledger

T0 = datetime(2026, 5, 12, 21, 15, tzinfo=UTC)
TODAY = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ── helpers ────────────────────────────────────────────────────────────────────────────────────
def _entry(**over) -> dict:
    base = {
        "id": "w-1133-pause",
        "issue": 1133,
        "promise": "override_respected",
        "match": {"cause": "pause", "configured_grace_seconds": 0},
        "reason": "door pause over a grace-less override",
        "added": "2026-10-31",
        "expires": "2026-11-30",
    }
    return {**base, **over}


def _write(tmp_path: Path, *entries: dict) -> Path:
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"entries": list(entries)}), encoding="utf-8")
    return p


def _hit(promise: str = "override_respected", **mechanism) -> SimpleNamespace:
    m = mech.blank_mechanism("abc123")
    m.update(mechanism)
    return SimpleNamespace(promise=promise, mechanism=m, detail="d", ts=T0)


class _States:
    """Stub issue lookup: no network."""

    def __init__(self, states: dict[int, str], problems: list[str] | None = None) -> None:
        self._s, self._p = states, problems or []

    def states(self, numbers):
        return {n: self._s[n] for n in numbers if n in self._s}, list(self._p)


def _ledger(tmp_path: Path, *entries: dict) -> list[dict]:
    return nl.load_ledger(_write(tmp_path, *entries))


# ── mechanism record ───────────────────────────────────────────────────────────────────────────
def _inp(commands=(), marks=(), episodes=(), readings=()) -> PromiseInput:
    return PromiseInput(
        now=T0,
        latest=readings[-1] if readings else None,
        readings=tuple(readings),
        commands=tuple(commands),
        marks=tuple(marks),
        episodes=tuple(episodes),
    )


def _p4_hit(ts=T0) -> Hit:
    return Hit(promise="override_respected", ts=ts, key="k", detail="d", evidence=(("target", "thermostat"),))


def test_a_hit_with_nothing_to_go_on_still_carries_every_field_as_null() -> None:
    out = mech.build_mechanism(_p4_hit(), _inp(), commit=None)
    assert set(out) == set(mech.MECHANISM_FIELDS)
    assert all(v is None for v in out.values())


def test_mechanism_is_read_from_the_command_the_episode_and_the_reading() -> None:
    cmd = Command(
        ts=T0, target="thermostat", service="set_hvac_mode", mode="off", origin="ca", cause="pause", indoor_f=70.0
    )
    start = T0 - timedelta(hours=3)
    ep = Episode("e", "thermostat", "override", start, "heat", None, None, None)
    marks = (
        Mark(ts=start, kind="override_detected", attrs=(("detected_mode", "heat"), ("source", "manual"))),
        Mark(ts=start, kind="grace_started", attrs=(("source", "manual"), ("duration_seconds", 900), ("trigger", ""))),
    )
    readings = (Reading(ts=T0, contacts_open=True),)
    events = [(T0, {"to_mode": "off", "reason": "door or window opened"})]
    ctx = {"fsm_paused_by_door": True, "fsm_grace_active": False, "configured_grace_seconds": 900}
    out = mech.build_mechanism(
        _p4_hit(),
        _inp([cmd], marks, [ep], readings),
        ctx=ctx,
        mode_set_events=events,
        commit="deadbeef",
    )
    assert out["cause"] == "pause"
    assert (out["command_service"], out["command_target"], out["command_mode"]) == (
        "set_hvac_mode",
        "thermostat",
        "off",
    )
    assert out["command_origin"] == "ca" and out["command_reason"] == "door or window opened"
    assert out["command_age_s"] == 0.0
    assert (out["episode_kind"], out["episode_source"], out["episode_grace_seconds"]) == ("override", "manual", 900)
    assert out["configured_grace_seconds"] == 900
    assert out["fsm_paused_by_door"] is True and out["fsm_grace_active"] is False
    assert out["fsm_manual_override_active"] is None  # not provided by the source: null, not False
    assert out["sensor_open"] is True and out["commit"] == "deadbeef"


def test_an_old_command_is_not_the_command_of_a_hit() -> None:
    old = Command(
        ts=T0 - timedelta(minutes=20), target="thermostat", service="set_hvac_mode", mode="off", cause="pause"
    )
    out = mech.build_mechanism(_p4_hit(), _inp([old]))
    assert out["command_service"] is None and out["cause"] is None


def test_a_command_to_the_other_device_is_not_the_hits_command() -> None:
    fan = Command(ts=T0, target="fan", service="turn_on", fan_on=True, cause="other")
    out = mech.build_mechanism(_p4_hit(), _inp([fan]))
    assert out["command_target"] is None


def test_an_episode_without_a_grace_mark_has_null_grace_seconds() -> None:
    ep = Episode("e", "thermostat", "override", T0 - timedelta(hours=1), "heat", None, None, None)
    out = mech.build_mechanism(_p4_hit(), _inp(episodes=[ep]), ctx={"configured_grace_seconds": 0})
    assert out["episode_kind"] == "override"
    assert out["episode_grace_seconds"] is None and out["configured_grace_seconds"] == 0


def test_a_real_generated_day_hit_carries_the_1133_mechanism() -> None:
    """Day 1000025 is one of the 12 days named in #1133 (grace 0, window opens after a confirmed override)."""
    rec = run_day(1000025)
    hits = [h for h in rec.report.hard if h.promise == "override_respected"]
    assert hits, "day 1000025 no longer breaks override_respected (fixed?) - pick a day that still does"
    m = hits[0].mechanism
    assert set(m) == set(mech.MECHANISM_FIELDS)
    assert m["cause"] == "pause" and m["command_service"] == "set_hvac_mode" and m["command_mode"] == "off"
    assert m["configured_grace_seconds"] == 0 and m["fsm_manual_override_active"] is True
    assert m["fsm_paused_by_door"] is True and m["sensor_open"] is True
    assert m["command_origin"] == "ca" and m["command_reason"] == "door or window opened"
    assert m["commit"] == mech.current_commit()


def test_matchable_fields_never_include_a_seed_or_run_detail() -> None:
    assert not {"commit", "command_age_s", "seed", "day_seed", "step", "ts"} & set(mech.MATCHABLE_FIELDS)


def test_commit_prefers_the_ci_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    mech.current_commit.cache_clear()
    monkeypatch.setenv("GITHUB_SHA", "feedface")
    try:
        assert mech.current_commit() == "feedface"
    finally:
        mech.current_commit.cache_clear()


# ── loading ────────────────────────────────────────────────────────────────────────────────────
def test_a_good_entry_loads_with_parsed_dates(tmp_path: Path) -> None:
    (e,) = _ledger(tmp_path, _entry())
    assert e["expires_on"] == date(2026, 11, 30) and e["issue"] == 1133


def test_a_missing_ledger_file_is_an_empty_ledger(tmp_path: Path) -> None:
    assert nl.load_ledger(tmp_path / "nope.json") == []


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"issue": None}, "missing"),
        ({"issue": "1133"}, "positive issue number"),
        ({"issue": True}, "positive issue number"),
        ({"expires": None}, "missing"),
        ({"expires": "next week"}, "not a YYYY-MM-DD date"),
        ({"expires": "2026-10-01"}, "before added"),
        ({"expires": "2027-06-30"}, "exceeds"),
        ({"match": {}}, "missing"),
        ({"match": {"day_seed": 1000025}}, "cannot match on"),
        ({"match": {"commit": "abc"}}, "cannot match on"),
        ({"promise": "no_such_promise"}, "unknown promise"),
    ],
)
def test_a_loose_or_malformed_entry_is_rejected_loudly(tmp_path: Path, override: dict, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        nl.load_ledger(_write(tmp_path, _entry(**override)), promise_keys=[p.key for p in PROMISES])


def test_the_same_mechanism_twice_is_a_duplicate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicates"):
        _ledger(tmp_path, _entry(), _entry(id="another-id"))


def test_the_same_id_twice_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice"):
        _ledger(tmp_path, _entry(), _entry(match={"cause": "restore"}))


def test_the_shipped_ledger_loads_against_the_production_registry() -> None:
    nl.load_ledger(nl.LEDGER_PATH, promise_keys=[p.key for p in PROMISES])


# ── strictness ─────────────────────────────────────────────────────────────────────────────────
def _hits(n: int = 3, **mechanism):
    return [(1000000 + i, _hit(**mechanism)) for i in range(n)]


def test_a_live_matching_waiver_passes_and_dedupes_many_hits_to_one_entry(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    ev = nl.evaluate(
        _hits(5, cause="pause", configured_grace_seconds=0), ledger, today=TODAY, issue_states=_States({1133: "open"})
    )
    assert ev.clean and list(ev.waived) == ["w-1133-pause"] and len(ev.waived["w-1133-pause"]) == 5
    assert ev.warnings == []


def test_a_different_seed_with_the_same_mechanism_is_still_waived(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    hits = [(7_000_047, _hit(cause="pause", configured_grace_seconds=0))]
    assert nl.evaluate(hits, ledger, today=TODAY, issue_states=_States({1133: "open"})).clean


def test_a_hit_with_another_mechanism_is_not_waived(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    hits = [
        (1, _hit(cause="restore", configured_grace_seconds=0)),
        (2, _hit(cause="pause", configured_grace_seconds=900)),
    ]
    ev = nl.evaluate(hits, ledger, today=TODAY, issue_states=_States({1133: "open"}))
    assert len(ev.unwaived) == 2 and not ev.clean
    assert any("stale waiver" in p for p in ev.problems)  # and nothing matched the entry


def test_a_null_in_the_match_matches_only_a_null_mechanism_value(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry(match={"cause": "pause", "episode_source": None}))
    ok = [(1, _hit(cause="pause"))]
    not_ok = [(2, _hit(cause="pause", episode_source="manual"))]
    states = _States({1133: "open"})
    assert nl.evaluate(ok, ledger, today=TODAY, issue_states=states).clean
    assert nl.evaluate(not_ok, ledger, today=TODAY, issue_states=states).unwaived


def test_a_closed_issue_fails_even_when_the_hit_still_occurs(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    ev = nl.evaluate(
        _hits(2, cause="pause", configured_grace_seconds=0), ledger, today=TODAY, issue_states=_States({1133: "closed"})
    )
    assert not ev.clean and any("#1133 is closed" in p for p in ev.problems)
    assert len(ev.unwaived) == 2  # a closed issue waives nothing


def test_an_expired_entry_fails(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry(expires="2026-10-31", added="2026-10-01"))
    ev = nl.evaluate(
        _hits(1, cause="pause", configured_grace_seconds=0), ledger, today=TODAY, issue_states=_States({1133: "open"})
    )
    assert not ev.clean and any("expired on 2026-10-31" in p for p in ev.problems)


def test_the_expiry_day_itself_is_still_valid(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry(expires="2026-11-01", added="2026-10-31"))
    hits = _hits(1, cause="pause", configured_grace_seconds=0)
    assert nl.evaluate(hits, ledger, today=date(2026, 11, 1), issue_states=_States({1133: "open"})).clean


def test_an_entry_whose_hit_no_longer_occurs_is_stale(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    ev = nl.evaluate([], ledger, today=TODAY, issue_states=_States({1133: "open"}))
    assert not ev.clean and any("stale waiver w-1133-pause" in p for p in ev.problems)


def test_two_entries_covering_one_hit_is_ambiguous(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry(), _entry(id="w-2", match={"cause": "pause"}))
    ev = nl.evaluate(
        _hits(1, cause="pause", configured_grace_seconds=0), ledger, today=TODAY, issue_states=_States({1133: "open"})
    )
    assert not ev.clean and any("exactly one waiver" in p for p in ev.problems)


def test_a_lookup_that_cannot_prove_the_issue_open_fails(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    states = _States({}, problems=["issue #1133: could not read its state from GitHub (URLError: boom)"])
    ev = nl.evaluate(_hits(1, cause="pause", configured_grace_seconds=0), ledger, today=TODAY, issue_states=states)
    assert not ev.clean and any("could not read" in p for p in ev.problems)
    assert ev.warnings == []  # a failure is red, not the offline warning


def test_offline_skips_the_closed_check_loudly_not_silently(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _entry())
    ev = nl.evaluate(
        _hits(1, cause="pause", configured_grace_seconds=0),
        ledger,
        today=TODAY,
        issue_states=nl.OfflineIssueStates(),
    )
    assert ev.clean and len(ev.warnings) == 1 and "SKIPPED" in ev.warnings[0] and "1133" in ev.warnings[0]


def test_offline_with_a_state_file_still_fails_a_closed_issue(tmp_path: Path) -> None:
    f = tmp_path / "states.json"
    f.write_text(json.dumps({"1133": "closed"}), encoding="utf-8")
    ledger = _ledger(tmp_path, _entry())
    ev = nl.evaluate(
        _hits(1, cause="pause", configured_grace_seconds=0),
        ledger,
        today=TODAY,
        issue_states=nl.OfflineIssueStates.from_file(f),
    )
    assert not ev.clean and ev.warnings == []


# ── GitHub lookup (stubbed transport) ──────────────────────────────────────────────────────────
def test_github_state_uses_the_issue_endpoint_and_token() -> None:
    seen: list[tuple[str, dict]] = []

    def fetch(url, headers):
        seen.append((url, dict(headers)))
        return {"state": "open"}

    states, problems = nl.GitHubIssueStates("gunkl/ClimateAdvisor", token="tkn", fetch=fetch).states([1133, 1133])
    assert states == {1133: "open"} and problems == []
    assert seen == [
        (
            "https://api.github.com/repos/gunkl/ClimateAdvisor/issues/1133",
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": "Bearer tkn",
            },
        )
    ]


def test_github_failure_pull_request_and_odd_state_are_problems() -> None:
    def fetch(url, headers):
        if url.endswith("/1"):
            raise urllib.error.URLError("down")
        if url.endswith("/2"):
            return {"state": "open", "pull_request": {}}
        return {"state": "weird"}

    states, problems = nl.GitHubIssueStates("o/r", fetch=fetch).states([1, 2, 3])
    assert states == {}
    assert len(problems) == 3
    assert any("pull request" in p for p in problems) and any("could not read" in p for p in problems)


def test_github_mode_needs_a_repo() -> None:
    with pytest.raises(ValueError, match="GITHUB_REPOSITORY"):
        nl.issue_states_from_args("github", state_file=None, repo=None, token=None)


# ── the CLI verdict ────────────────────────────────────────────────────────────────────────────
def test_the_run_verdict_is_red_for_an_unlisted_hit_and_green_for_a_waived_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = _write(tmp_path, _entry())
    state_file = tmp_path / "s.json"
    state_file.write_text(json.dumps({"1133": "open"}), encoding="utf-8")
    waived = _hits(2, cause="pause", configured_grace_seconds=0)
    assert judge_with_ledger(waived, str(ledger_path), "offline", str(state_file), 0, today=TODAY) == 0
    out = capsys.readouterr().out
    assert "waived w-1133-pause (issue #1133, expires 2026-11-30): 2 hit(s) on 2 day(s)" in out
    new = [*waived, (9, _hit(cause="restore"))]
    assert judge_with_ledger(new, str(ledger_path), "offline", str(state_file), 0, today=TODAY) == 1
    assert "UNWAIVED override_respected day_seed=9" in capsys.readouterr().out
    assert judge_with_ledger(waived, str(ledger_path), "offline", str(state_file), 1, today=TODAY) == 1  # a crashed day
