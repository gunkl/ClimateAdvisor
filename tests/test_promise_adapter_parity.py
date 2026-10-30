"""Adapter parity: the FakeHass probe collects the SAME evidence production's own hooks collect.

Issue #1128 I9.  Occupant first: the promise checks are only as honest as the evidence they are
fed.  If the test-side adapter saw a fan "off" where Climate Advisor itself sees "unknown", or
missed a command Climate Advisor really sent, every random-day verdict and every golden verdict
would silently describe a house that production never sees.

How: a coordinator-mode session runs the REAL coordinator, whose production hooks fill
``coordinator._promise_journal`` (``_record_promise_reading`` in the thermostat listener and the
5-minute tick, ``_promise_observe_command`` via the engine's ``_command_observer`` in
``_call_service_with_context``, ``_record_promise_event`` in ``_emit_event``).  The probe's
``FakeHassPromiseSource`` fills ``probe.journal`` from the very same run by polling states and
diffing ``action_log``.  This file records every item either journal receives (test-only
patch of the journal's ``record_*`` methods, so pruning cannot hide anything) and compares:

* commands: equal, item for item (ts, target, service, mode, setpoint, fan_on, automation_enabled, indoor);
* marks: equal, item for item, plus exactly one adapter-only ``system_restarted`` per
  ``simulate_restart`` event (the harness fakes a restart in place; production's real restart
  builds a fresh journal and emits that mark, which the adapter models in ``notify_restart``);
* readings: every production reading has an equal adapter reading at the same instant
  (``outdoor_f`` is compared only when production has one: the harness feeds outdoor temperature
  to the engine, not to ``coordinator._last_outdoor_temp``; production readings from before the
  probe attached carry no scenario data and are skipped).

Nothing here re-implements a promise or a hook: both sides are production code or the adapter
under test; the comparison only lines their outputs up.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from custom_components.climate_advisor.promise_journal import PromiseJournal
from tools.sim_harness.generator import build_plan, day_seed_of, patch_stub_gaps, run_plan
from tools.sim_harness.promise_probe import PromiseProbe
from tools.sim_harness.session import drive_scenario, make_session

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "tools" / "simulations" / "golden"
_GOLDENS = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
_KINDS = (("record_reading", "readings"), ("record_command", "commands"), ("record_mark", "marks"))


@pytest.fixture(autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture()
def capture_journals(monkeypatch):
    """Test-only: remember every reading/command/mark each PromiseJournal instance is handed."""
    originals = {name: getattr(PromiseJournal, name) for name, _ in _KINDS}

    def make(name, kind):
        def recording(self, item):
            self.__dict__.setdefault("_cap", {"readings": [], "commands": [], "marks": []})[kind].append(item)
            return originals[name](self, item)

        return recording

    for name, kind in _KINDS:
        monkeypatch.setattr(PromiseJournal, name, make(name, kind))


def _captured(journal: PromiseJournal) -> dict[str, list]:
    return journal.__dict__.setdefault("_cap", {"readings": [], "commands": [], "marks": []})


def _command_key(c) -> tuple:
    return (c.ts.isoformat(), c.target, c.service, c.mode, c.setpoint_f, c.fan_on, c.automation_enabled, c.indoor_f)


def _mark_key(m) -> tuple:
    return (m.ts.isoformat(), m.kind, m.attrs)


def _reading_key(r) -> tuple:
    return (
        r.ts.isoformat(),
        r.hvac_mode,
        r.hvac_action,
        r.indoor_f,
        r.forecast_high_f,
        r.setpoint_f,
        r.fan_on,
        r.contacts_open,
    )


def _run_both(scenario: dict) -> tuple[dict, dict]:
    """Run ``scenario`` through a real coordinator; return (production-hook evidence, adapter evidence)."""
    probe = PromiseProbe(scenario_name=str(scenario.get("name", "parity")))
    session = make_session(scenario, engine="fakehass", probe=probe, use_coordinator=True)
    try:
        patch_stub_gaps(session)
        drive_scenario(session, scenario)
    finally:
        session.close()
    return _captured(session.coordinator._promise_journal), _captured(probe.journal)


def compare(prod: dict, adap: dict, *, restarts: int = 0) -> list[str]:
    """Lines up the two evidence sets; returns human-readable differences (empty = equivalent)."""
    problems: list[str] = []

    pc, ac = [_command_key(c) for c in prod["commands"]], [_command_key(c) for c in adap["commands"]]
    if pc != ac:
        problems.append(
            f"commands differ: production-only={[c for c in pc if c not in ac][:3]} "
            f"adapter-only={[c for c in ac if c not in pc][:3]} (counts {len(pc)} vs {len(ac)})"
        )

    pm, am = [_mark_key(m) for m in prod["marks"]], [_mark_key(m) for m in adap["marks"]]
    missing = [m for m in pm if m not in am]
    extra = [m for m in am if m not in pm]
    if missing or [m for m in extra if m[1] != "system_restarted"] or len(extra) != restarts:
        problems.append(
            f"marks differ: production-only={missing[:3]} adapter-only={extra[:3]} "
            f"(expected exactly {restarts} adapter-only system_restarted)"
        )
    elif [m for m in am if m not in extra] != pm:
        problems.append("marks are the same set but in a different order")

    adapter_readings: dict[str, list[tuple]] = {}
    for r in adap["readings"]:
        adapter_readings.setdefault(r.ts.isoformat(), []).append(_reading_key(r))
    first_adapter_ts = min((r.ts for r in adap["readings"]), default=None)
    unmatched = []
    for r in prod["readings"]:
        if first_adapter_ts is not None and r.ts < first_adapter_ts:
            continue  # recorded while the session was still being built: before the probe attached
        if _reading_key(r) not in adapter_readings.get(r.ts.isoformat(), []):
            unmatched.append((_reading_key(r), adapter_readings.get(r.ts.isoformat())))
    if unmatched:
        problems.append(f"{len(unmatched)} production readings have no equal adapter reading, e.g. {unmatched[:2]}")
    return problems


# ── goldens (every one, forced through the real coordinator) ──────────────────────────────────────
@pytest.mark.parametrize("path", _GOLDENS, ids=lambda p: p.stem)
def test_golden_evidence_is_the_same_through_production_hooks_and_the_adapter(path, capture_journals) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    prod, adap = _run_both(scenario)
    restarts = sum(1 for e in scenario["events"] if e.get("type") == "simulate_restart")
    assert compare(prod, adap, restarts=restarts) == []


def test_the_golden_sample_is_not_vacuous(capture_journals) -> None:
    """Across the goldens the comparison really saw commands, marks and readings."""
    totals = {"commands": 0, "marks": 0, "readings": 0}
    for path in _GOLDENS:
        prod, _ = _run_both(json.loads(path.read_text(encoding="utf-8")))
        for k in totals:
            totals[k] += len(prod[k])
    assert totals["commands"] > 100 and totals["marks"] > 100 and totals["readings"] > 300, totals


# ── generated days (include the new automation_toggle / indoor_unreadable hazards) ────────────────
def _parity_days() -> list[int]:
    """A fixed sample: the first days, plus days that draw each hazard and a restart."""
    wanted = {"automation_toggle", "indoor_unreadable", "simulate_restart"}
    seen: dict[str, int] = {}
    out = list(range(4))
    for i in range(400):
        kinds = {k for _, k, _ in build_plan(day_seed_of(7, i), "coordinator").timed} & wanted
        for k in kinds:
            if seen.get(k, 0) < 3 and i not in out:
                seen[k] = seen.get(k, 0) + 1
                out.append(i)
        if all(seen.get(k, 0) >= 3 for k in wanted):
            break
    return out


@pytest.mark.parametrize("index", _parity_days())
def test_generated_day_evidence_is_the_same_through_production_hooks_and_the_adapter(index, capture_journals) -> None:
    rec = run_plan(build_plan(day_seed_of(7, index), "coordinator"))
    prod, adap = _run_both(rec.scenario)
    restarts = sum(1 for e in rec.scenario["events"] if e.get("type") == "simulate_restart")
    assert compare(prod, adap, restarts=restarts) == []


def test_the_generated_sample_covers_the_hazards() -> None:
    kinds: set[str] = set()
    for i in _parity_days():
        kinds |= {k for _, k, _ in build_plan(day_seed_of(7, i), "coordinator").timed}
    assert {"automation_toggle", "indoor_unreadable", "simulate_restart"} <= kinds


# ── the comparison itself must be able to fail ────────────────────────────────────────────────────
def test_the_comparison_detects_a_divergence(capture_journals) -> None:
    from dataclasses import replace

    path = GOLDEN_DIR / "override_second_override_during_grace.json"
    prod, adap = _run_both(json.loads(path.read_text(encoding="utf-8")))
    assert compare(prod, adap) == []

    wrong_fan = {**adap, "readings": [replace(r, fan_on=False) for r in adap["readings"]]}
    assert any("readings" in p for p in compare(prod, wrong_fan))

    dropped = {**adap, "commands": adap["commands"][1:]}
    assert any("commands differ" in p for p in compare(prod, dropped))

    reordered = {**adap, "commands": [replace(adap["commands"][0], automation_enabled=False), *adap["commands"][1:]]}
    assert any("commands differ" in p for p in compare(prod, reordered))

    assert any("marks differ" in p for p in compare(prod, {**adap, "marks": adap["marks"][:-1]}))
