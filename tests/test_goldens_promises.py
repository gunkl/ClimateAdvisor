"""Every golden scenario, through the Session seam, judged by the production household promises.

Issue #1128 I2.  Occupant first: a golden is a documented real-world situation.  If replaying one
shows Climate Advisor breaking a promise a person relies on (heat right after the AC, a command
sent while automation is off, an override undone before its time, a blind fan), the golden's
outcome assertions would never have said so.  This test says so.

* The promises are PRODUCTION code (``invariant_watchdog.run_promise_checks``, scope "test",
  with ``promise_journal.PromiseJournal``); the probe only collects evidence from the FakeHass
  run.  Severities come from the production registry (hard fails, warning is ratcheted).
* Goldens are LOCKED.  A known hit is recorded in ``tools/sim_harness/promise_waivers.json``
  (and must still occur, or the entry is stale); warnings are ratcheted by
  ``tools/sim_harness/promise_baseline.json``.
* ``outcomes.py`` is untouched; the probe never changes ``passed``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.climate_advisor.invariant_watchdog import PROMISES, SEVERITY_HARD, SEVERITY_WARNING
from custom_components.climate_advisor.promise_journal import Command
from tools.sim_harness.promise_probe import (
    PromiseProbe,
    SourceBatch,
    check_waiver_counts,
    check_warning_baseline,
    load_baseline,
    load_waivers,
    waivers_for,
)
from tools.sim_harness.run_production import run_production_scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tools" / "simulations" / "golden"
_GOLDEN_FILES = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
_WAIVERS = load_waivers()
_BASELINE = load_baseline()


def _scenario(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(path: Path, *, probe: PromiseProbe | None):
    scenario = _scenario(path)
    return run_production_scenario(scenario, use_coordinator=bool(scenario.get("use_coordinator", False)), probe=probe)


def _actions(log: list[dict]) -> list[dict]:
    """An action's ``context`` is a fresh object per run (identity only); compare that it was set."""
    return [{**a, "context": a.get("context") is not None} for a in log]


def _describe(hits) -> str:
    return "\n".join(
        f"  - {h.promise} [{h.severity}] at {h.ts.isoformat()} ({h.step}): {h.detail} evidence={dict(h.evidence)} "
        f"replay={h.replay}"
        for h in hits
    )


def test_all_goldens_discovered() -> None:
    assert len(_GOLDEN_FILES) >= 90, f"expected the full golden set, found {len(_GOLDEN_FILES)}"


@pytest.mark.parametrize("path", _GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden_keeps_the_household_promises(path: Path) -> None:
    """No un-waived hard promise is broken; warnings never exceed the recorded baseline."""
    probe = PromiseProbe(scenario_name=path.stem, waivers=waivers_for(path.stem, _WAIVERS))
    result = _run(path, probe=probe)
    report = result.promise_report
    assert report is not None and report.steps > 0

    assert not report.hard, f"{path.stem}: a hard household promise was broken:\n{_describe(report.hard)}"

    # A waiver for a hit that no longer happens is stale; more hits than its expected_count hide a new one.
    waived_counts: dict[str, int] = {}
    for h in report.waived:
        waived_counts[h.promise] = waived_counts.get(h.promise, 0) + 1
    problems = check_waiver_counts(path.stem, waived_counts, _WAIVERS)
    assert not problems, f"{path.stem}: " + "; ".join(problems)

    seen: dict[str, int] = {}
    for h in report.warnings:
        seen[h.promise] = seen.get(h.promise, 0) + 1
    problems = check_warning_baseline(path.stem, seen, _BASELINE.get(path.stem, {}))
    assert not problems, f"{path.stem}: " + "; ".join(problems) + "\n" + _describe(report.warnings)


@pytest.mark.parametrize("path", _GOLDEN_FILES, ids=lambda p: p.stem)
def test_attaching_the_probe_does_not_change_the_run(path: Path) -> None:
    """Behavior-neutral: slicing time for the probe leaves event_log / action_log / state identical."""
    plain = _run(path, probe=None)
    probed = _run(path, probe=PromiseProbe(scenario_name=path.stem, waivers=waivers_for(path.stem, _WAIVERS)))
    assert probed.event_log == plain.event_log
    assert _actions(probed.action_log) == _actions(plain.action_log)
    assert probed.engine_state == plain.engine_state
    assert [(t, repr(e)) for t, e in probed.callback_errors] == [(t, repr(e)) for t, e in plain.callback_errors]


def test_waivers_and_baseline_only_name_real_goldens_and_promises() -> None:
    names = {p.stem for p in _GOLDEN_FILES}
    keys = {s.key for s in PROMISES}
    for w in _WAIVERS:
        assert w["scenario"] in names, f"waiver for unknown golden {w['scenario']}"
        assert w["promise"] in keys, f"waiver for unknown promise {w['promise']}"
    for scenario, per in _BASELINE.items():
        assert scenario in names, f"baseline for unknown golden {scenario}"
        assert set(per) <= keys


# ── effectiveness (negative controls): the probe must be able to FAIL ─────────────────────────
class _ScriptedSource:
    """A PromiseSource that replays a hand-built batch - exercises the probe, not a promise."""

    def __init__(self, cfg, batches) -> None:
        self._cfg = cfg
        self._batches = list(batches)
        self._now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def config(self):
        return self._cfg

    def now(self):
        return self._now

    def poll(self, now):
        return self._batches.pop(0) if self._batches else SourceBatch()


def _probe_with(batches, *, waivers=None) -> PromiseProbe:
    from custom_components.climate_advisor.invariant_watchdog import PromiseConfig

    probe = PromiseProbe(scenario_name="control", waivers=waivers)
    probe.attach_source(_ScriptedSource(PromiseConfig(), batches))
    return probe


_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_a_command_sent_while_automation_is_off_is_a_hard_failure() -> None:
    """P3 (observe_only_silence): the occupant switched automation off; CA still moved the thermostat."""
    cmd = Command(
        ts=_NOW, target="thermostat", service="set_hvac_mode", mode="heat", automation_enabled=False, indoor_f=70.0
    )
    probe = _probe_with([SourceBatch(commands=[cmd])])
    probe.step("event[0]:control")
    report = probe.report()
    assert [h.promise for h in report.hard] == ["observe_only_silence"]
    assert report.hard[0].severity == SEVERITY_HARD
    assert report.hard[0].replay["scenario"] == "control"


def test_a_waived_hit_is_reported_but_does_not_fail() -> None:
    cmd = Command(ts=_NOW, target="fan", service="turn_on", fan_on=True, automation_enabled=False, indoor_f=70.0)
    probe = _probe_with([SourceBatch(commands=[cmd])], waivers={"observe_only_silence": "reviewed"})
    probe.step("event[0]:control")
    report = probe.report()
    assert not report.hard and [h.promise for h in report.waived] == ["observe_only_silence"]


def test_warning_promises_route_to_warnings_not_failures() -> None:
    """P5b (blind_fan_continue) is a warning in the production registry."""
    from datetime import timedelta

    from custom_components.climate_advisor.promise_journal import Reading

    readings = [Reading(ts=_NOW + timedelta(minutes=m), fan_on=True, indoor_f=None) for m in (0, 20, 40)]
    probe = _probe_with([SourceBatch(readings=readings)])
    probe._source._now = _NOW + timedelta(minutes=41)  # noqa: SLF001 - drive the scripted clock
    probe.step("tick")
    report = probe.report()
    assert not report.hard
    assert [h.promise for h in report.warnings] == ["blind_fan_continue"]
    assert report.warnings[0].severity == SEVERITY_WARNING


# ── the ratchet helpers themselves ─────────────────────────────────────────────────────────────
def test_baseline_ratchet_fails_on_more_and_on_fewer() -> None:
    assert check_warning_baseline("s", {"comfort_kept": 1}, {"comfort_kept": 1}) == []
    assert "new 'comfort_kept'" in check_warning_baseline("s", {"comfort_kept": 2}, {"comfort_kept": 1})[0]
    stale = check_warning_baseline("s", {}, {"comfort_kept": 1})
    assert stale and "lower the baseline" in stale[0]
    assert "new 'x'" in check_warning_baseline("s", {"x": 1}, {})[0]


def test_waiver_needs_an_expected_count_and_enforces_it(tmp_path: Path) -> None:
    rows = [{"scenario": "s", "promise": "p", "reason": "r", "expected_count": 2}]
    assert check_waiver_counts("s", {"p": 2}, rows) == []
    assert "investigate" in check_waiver_counts("s", {"p": 3}, rows)[0]
    assert "lower expected_count" in check_waiver_counts("s", {"p": 1}, rows)[0]
    assert "stale" in check_waiver_counts("s", {}, rows)[0]
    assert check_waiver_counts("other", {}, rows) == []
    bad = tmp_path / "w.json"
    bad.write_text(json.dumps({"waivers": [{"scenario": "s", "promise": "p", "reason": "r"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_count"):
        load_waivers(bad)


def test_generate_cli_fails_when_a_day_crashes_or_logs_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nightly job must go red when days raise or log a CA exception: an unjudged day is not green."""
    from tools.sim_harness import replay
    from tools.sim_harness.generator import BatchResult

    def fake_batch(ms, days, profile, **_kw):
        return BatchResult(master_seed=ms, days=days, seconds=0.1, **fake_batch.kw)

    monkeypatch.setattr(replay, "run_batch", fake_batch)
    try:  # cli_generate silences logging for the whole process; undo it so later tests still capture records
        for kw, expected in (
            ({}, 0),
            ({"errors": [(7, "ValueError: boom")]}, 1),
            ({"callback_error_days": [7]}, 1),
        ):
            fake_batch.kw = kw
            assert replay.cli_generate([1], 1, "coordinator", save=False) == expected, kw
    finally:
        logging.disable(logging.NOTSET)
