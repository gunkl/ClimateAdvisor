"""promise_probe - feed the production promise registry from a scenario run (Issue #1128 I2).

The probe owns NO promise logic.  It collects evidence (readings, commands, CA events) from a
``PromiseSource``, records it in the production ``PromiseJournal`` (``promise_journal.py``) and
asks the production ``run_promise_checks(..., scope="test")`` (``invariant_watchdog.py``) what, if
anything, is broken.  Severity comes from the production registry (``PROMISES[].test_severity``).

One source adapter per world: ``FakeHassPromiseSource`` here; the real-Home-Assistant runner
supplies its own (``state_changed`` + ``EVENT_CALL_SERVICE`` + ``coordinator._event_log``) and
reuses ``PromiseProbe`` unchanged.  This module imports no Home Assistant code.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from tools.sim_harness.ha_stubs import install_ha_stubs
from tools.sim_harness.mechanism import FSM_FLAGS, build_mechanism, current_commit

# The production promise modules live inside the climate_advisor package, whose __init__ needs
# Home Assistant; under FakeHass that is the stub layer (idempotent, adapter-side only).
install_ha_stubs()

from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    PROMISES,
    SCOPE_TEST,
    SEVERITY_HARD,
    PromiseConfig,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    Command,
    Mark,
    PromiseJournal,
    Reading,
)

WAIVERS_PATH = Path(__file__).with_name("promise_waivers.json")
BASELINE_PATH = Path(__file__).with_name("promise_baseline.json")

_UNREADABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})
_COMMAND_DOMAINS = frozenset({"climate", "fan", "switch"})


# ── probe-side value types ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class SourceBatch:
    """New evidence since the last poll."""

    readings: list[Reading] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    # (ts, ca_event_type, payload) - mapped to Marks by the journal (EVENT_MARK_MAP)
    events: list[tuple[datetime, str, dict]] = field(default_factory=list)


class PromiseSource(Protocol):
    """One adapter per world."""

    def poll(self, now: datetime) -> SourceBatch: ...

    def config(self) -> PromiseConfig: ...

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class ProbeHit:
    """A broken promise with the pointer needed to replay it."""

    promise: str
    severity: str  # "hard" | "warning"
    ts: datetime
    step: str
    detail: str
    evidence: tuple
    key: str
    replay: dict
    occurrences: int = 1
    waiver: str | None = None  # reason, when waived
    # Issue #1153: the mechanism that produced the hit (tools/sim_harness/mechanism.py), every field
    # present and None where unknowable.  Waivers match on this, never on a seed.
    mechanism: dict | None = None


@dataclass
class PromiseReport:
    hard: list[ProbeHit] = field(default_factory=list)
    warnings: list[ProbeHit] = field(default_factory=list)
    waived: list[ProbeHit] = field(default_factory=list)
    steps: int = 0
    stats: dict[str, int] = field(default_factory=dict)  # promise key -> distinct hits (incl. waived)

    @property
    def clean(self) -> bool:
        return not self.hard


_SEVERITY_BY_KEY = {spec.key: spec.test_severity for spec in PROMISES}


def load_waivers(path: Path = WAIVERS_PATH) -> list[dict]:
    """The waiver ledger: ``[{scenario, promise, reason, issue, expected_count}]`` (goldens are LOCKED,
    so a known promise hit on a golden is recorded here, never by editing the golden).

    ``expected_count`` (int >= 1) is the number of distinct hits the waiver tolerates: more is a new
    failure hiding behind the waiver, fewer means the entry is stale.  A row without it is rejected
    loudly - a silently dropped waiver would turn a known hit into a surprise failure, and an
    uncounted one would let new hits ride along unnoticed."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = data.get("waivers", []) if isinstance(data, dict) else []
    out = []
    for r in rows:
        if not (isinstance(r, dict) and r.get("scenario") and r.get("promise") and r.get("reason")):
            continue
        n = r.get("expected_count")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError(f"waiver {r['scenario']}/{r['promise']} needs an integer expected_count >= 1")
        out.append(r)
    return out


def check_waiver_counts(scenario: str, waived: dict[str, int], rows: list[dict]) -> list[str]:
    """Problems for one scenario: ``waived`` = promise -> distinct waived hits seen this run."""
    problems: list[str] = []
    for r in rows:
        if r["scenario"] != scenario:
            continue
        seen = waived.get(r["promise"], 0)
        if seen == 0:
            problems.append(f"stale waiver '{r['promise']}' - the hit no longer occurs; remove the entry")
        elif seen != r["expected_count"]:
            problems.append(
                f"waiver '{r['promise']}' expects {r['expected_count']} hit(s) but {seen} occurred - "
                f"{'investigate the new hit(s)' if seen > r['expected_count'] else 'lower expected_count'}"
            )
    return problems


def check_warning_baseline(scenario: str, seen: dict[str, int], allowed: dict[str, int]) -> list[str]:
    """Warning ratchet for one scenario: more than the baseline is a new warning; fewer is stale."""
    problems: list[str] = []
    for promise in sorted(set(seen) | set(allowed)):
        have, cap = seen.get(promise, 0), allowed.get(promise, 0)
        if have > cap:
            problems.append(f"{have} new '{promise}' warning(s) over the baseline {cap}")
        elif have < cap:
            problems.append(
                f"'{promise}' now has {have} warning(s), baseline says {cap} - lower the baseline "
                "in tools/sim_harness/promise_baseline.json (the ratchet only goes down)"
            )
    return problems


def waivers_for(scenario: str, rows: list[dict] | None = None) -> dict[str, str]:
    """``promise key -> reason`` for one scenario."""
    return {r["promise"]: r["reason"] for r in (load_waivers() if rows is None else rows) if r["scenario"] == scenario}


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, int]]:
    """Warning ratchet: ``{scenario: {promise: distinct warning hits}}``.  May only go down."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = data.get("warnings", {}) if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


class PromiseProbe:
    """Run the production promises after every settled step of a session.

    ``waivers`` maps ``promise key -> reason`` for THIS scenario (a sidecar ledger, never an edit
    to a golden).  Waived hits are reported separately; they never fail a run.
    """

    def __init__(self, *, scenario_name: str = "", waivers: dict[str, str] | None = None) -> None:
        self._scenario = scenario_name
        self._waivers = dict(waivers or {})
        self._journal = PromiseJournal()
        self._source: PromiseSource | None = None
        self._cfg: PromiseConfig | None = None
        self._steps = 0
        self._label_index = -1
        self._hits: dict[str, ProbeHit] = {}  # dedup key -> first hit
        self._mode_set_events: list[tuple[datetime, dict]] = []  # recent thermostat_mode_set events (#1153)
        self._order: list[str] = []

    # ── wiring ────────────────────────────────────────────────────────────────
    def attach(self, session: Any) -> None:
        """Bind to a FakeHass session (the source adapter is chosen by the session kind)."""
        self.attach_source(FakeHassPromiseSource(session))
        if not self._scenario:
            self._scenario = str(getattr(session, "scenario", {}).get("name", ""))

    def attach_source(self, source: PromiseSource) -> None:
        self._source = source
        self._cfg = source.config()

    @property
    def journal(self) -> PromiseJournal:
        return self._journal

    # ── per-step evaluation ─────────────────────────────────────────────────────
    def notify_restart(self, ts: datetime) -> None:
        """Clean slate (the documented restart behavior): drop evidence, mark the restart."""
        self._journal.reset()
        self._journal.record_mark(Mark(ts=ts, kind="system_restarted"))

    def step(self, label: str) -> list[ProbeHit]:
        """Poll the source, run the promises, return the NEW hits.  Never raises on source faults."""
        if self._source is None or self._cfg is None:
            return []
        self._steps += 1
        if label.startswith("event["):
            with contextlib.suppress(ValueError):
                self._label_index = int(label[6 : label.index("]")])
        now = self._source.now()
        batch = self._source.poll(now)
        for r in batch.readings:
            self._journal.record_reading(r)
        for c in batch.commands:
            self._journal.record_command(c)
        for ts, etype, payload in batch.events:
            self._journal.on_event(ts, etype, payload)
            if etype == "thermostat_mode_set":
                self._mode_set_events = [*self._mode_set_events[-63:], (ts, payload)]
        inp = self._journal.window(now)
        new: list[ProbeHit] = []
        for hit in run_promise_checks(inp, self._cfg, scope=SCOPE_TEST):
            key = str(hit.key)
            known = self._hits.get(key)
            if known is not None:
                self._hits[key] = _bump(known)
                continue
            if not self._journal.is_new(hit.key):
                continue
            severity = _SEVERITY_BY_KEY.get(hit.promise, SEVERITY_HARD)
            ctx_fn = getattr(self._source, "mechanism_context", None)
            ph = ProbeHit(
                promise=hit.promise,
                severity=severity,
                ts=hit.ts,
                step=label,
                detail=hit.detail,
                evidence=hit.evidence,
                key=key,
                replay={
                    "scenario": self._scenario,
                    "event_index": self._label_index,
                    "virtual_ts": now.isoformat(),
                },
                waiver=self._waivers.get(hit.promise),
                mechanism=build_mechanism(
                    hit,
                    inp,
                    ctx=ctx_fn() if callable(ctx_fn) else None,
                    mode_set_events=self._mode_set_events,
                    commit=current_commit(),
                ),
            )
            self._hits[key] = ph
            self._order.append(key)
            new.append(ph)
        return new

    def report(self) -> PromiseReport:
        rep = PromiseReport(steps=self._steps)
        for key in self._order:
            h = self._hits[key]
            rep.stats[h.promise] = rep.stats.get(h.promise, 0) + 1
            if h.waiver is not None:
                rep.waived.append(h)
            elif h.severity == SEVERITY_HARD:
                rep.hard.append(h)
            else:
                rep.warnings.append(h)
        return rep


def _bump(h: ProbeHit) -> ProbeHit:
    return ProbeHit(**{**h.__dict__, "occurrences": h.occurrences + 1})


# ═══════════════════════════════════════════════════════════════════════════════
# FakeHass source adapter
# ═══════════════════════════════════════════════════════════════════════════════
class FakeHassPromiseSource:
    """Evidence from a ``FakeHassSession``.

    * Readings: one per write to the thermostat / fan / door-window entities (``states.add_observer``)
      plus one heartbeat per poll, so outdoor/forecast/classification changes are seen too.
    * Commands: the FakeHass ``action_log`` (every climate/fan/switch service call, in order).
    * Events: the engine/coordinator event log (CA's own words), read incrementally.
    """

    def __init__(self, session: Any) -> None:
        self._s = session
        self._cfg = PromiseConfig.from_config(session.config)
        # Issue #1144: a thermostat's equipment report trails the command that stopped it by seconds, so an
        # occupant turning the fan on while the AC runs always shows a moment of overlap before the HVAC-off
        # lands.  A scenario may state the overlap it allows (the 30 s the safety rules allow); default 0.
        _overlap = session.config.get("promise_p2_min_overlap_s")
        if isinstance(_overlap, (int, float)) and _overlap > 0:
            self._cfg = dataclasses.replace(self._cfg, p2_min_overlap_s=float(_overlap))
        self._unit = str(session.config.get("temp_unit", "fahrenheit"))
        self._watched = {session.climate_entity}
        fan_entity = session.config.get("fan_entity")
        if isinstance(fan_entity, str) and fan_entity:
            self._watched.add(fan_entity)
        fan_state_entity = session.config.get("fan_state_entity")
        if isinstance(fan_state_entity, str) and fan_state_entity:
            self._watched.add(fan_state_entity)
        self._contacts = [e for e in session.config.get("door_window_sensors", []) if isinstance(e, str)]
        self._watched.update(self._contacts)
        self._pending: list[Reading] = []
        self._actions_seen = 0
        self._events_seen = 0
        # Issue #1153: why each command went out, captured at send time (the engine's mode-change
        # reason is only set inside the sending call) from the same observer hook production uses.
        self._cmd_causes: list[tuple[str, str, str | None, str | None]] = []
        self._install_cause_capture()
        session.fake_hass.states.add_observer(self._on_state_write)

    # ── PromiseSource ──────────────────────────────────────────────────────────
    def config(self) -> PromiseConfig:
        return self._cfg

    def now(self) -> datetime:
        return self._s.scheduler.now()

    def poll(self, now: datetime) -> SourceBatch:
        readings = self._pending
        self._pending = []
        heartbeat = self._reading(now)
        readings.append(heartbeat)

        commands: list[Command] = []
        log = self._s.fake_hass.action_log
        indoor = heartbeat.indoor_f
        for entry in log[self._actions_seen :]:
            cmd = self._command(entry, now, indoor)
            if cmd is not None:
                commands.append(cmd)
        self._actions_seen = len(log)

        events: list[tuple[datetime, str, dict]] = []
        elog = self._s.event_log
        for etype, payload, ts in elog[self._events_seen :]:
            events.append((ts or now, etype, payload if isinstance(payload, dict) else {}))
        self._events_seen = len(elog)
        return SourceBatch(readings=readings, commands=commands, events=events)

    def mechanism_context(self) -> dict[str, Any]:
        """Engine override/grace flags and the configured grace, sampled NOW (Issue #1153).

        Called when a hit is first detected, so it is the state at detection, not at the instant the
        hit's own evidence was recorded.  A flag the engine does not have reads as ``None``."""
        from custom_components.climate_advisor.const import CONF_MANUAL_GRACE_PERIOD  # noqa: PLC0415

        engine = self._s.engine
        out: dict[str, Any] = {}
        for attr, suffix in FSM_FLAGS:
            value = getattr(engine, attr, None)
            out[f"fsm_{suffix}"] = value if isinstance(value, bool) else None
        grace = self._s.config.get(CONF_MANUAL_GRACE_PERIOD)
        out["configured_grace_seconds"] = (
            grace if isinstance(grace, (int, float)) and not isinstance(grace, bool) else None
        )
        return out

    # ── internals ──────────────────────────────────────────────────────────────
    def _install_cause_capture(self) -> None:
        """Record the engine's ``_mode_change_reason`` for every command, chaining any existing observer.

        Same value production journals as ``Command.cause`` (``coordinator._promise_observe_command``).
        Read-only: the original observer still runs first and nothing is sent or changed here."""
        engine = self._s.engine
        original = getattr(engine, "_command_observer", None)

        def capture(domain: str, service: str, data: dict, kind: str) -> None:
            if original is not None:
                original(domain, service, data, kind)
            entity = data.get("entity_id") if isinstance(data, dict) else None
            cause = getattr(engine, "_mode_change_reason", None)
            self._cmd_causes = [
                *self._cmd_causes[-255:],
                (domain, service, entity, cause if isinstance(cause, str) else None),
            ]

        engine._command_observer = capture

    def _take_cause(self, domain: str, service: str, entity: Any) -> str | None:
        for i, (d, sv, ent, cause) in enumerate(self._cmd_causes):
            if (d, sv, ent) == (domain, service, entity):
                del self._cmd_causes[i]
                return cause
        return None

    def _on_state_write(self, entity_id: str) -> None:
        if entity_id in self._watched:
            self._pending.append(self._reading(self.now()))

    def _indoor_f(self, climate_state: Any) -> float | None:
        # The engine's own resolver (indoor_temp.resolve_indoor_temp_f): the same answer CA acts on.
        try:
            return self._s.engine._get_indoor_temp_f()
        except Exception:  # noqa: BLE001 - a probe must never break a run
            from custom_components.climate_advisor.temperature import read_state_temp_f  # noqa: PLC0415

            return read_state_temp_f(climate_state, "current_temperature", self._unit)

    def _fan_physical_state(self, states: Any) -> bool | None:
        """The fan's physical state exactly as production reads it (adapter parity, Issue #1128 I9).

        ``ClimateAdvisorCoordinator._get_fan_physical_state()``: None ("unknown", never "off") in
        command-only mode (``fan_state_feedback`` false, the default) or for an ``assumed_state`` /
        unavailable entity; otherwise the optional state entity, then the command entity, read
        through the shared ``fan_status.fan_entity_on``.  The first version of this adapter read the
        command entity's raw state, so it reported a fan "off" that production reports "unknown".
        """
        from custom_components.climate_advisor.fan_status import fan_entity_on  # noqa: PLC0415

        cfg = self._s.config
        if not bool(cfg.get("fan_state_feedback", False)):
            return None
        state_entity = cfg.get("fan_state_entity")
        if isinstance(state_entity, str) and state_entity:
            on = fan_entity_on(states.get(state_entity))
            if on is not None:
                return on
        fan_entity = cfg.get("fan_entity")
        if isinstance(fan_entity, str) and fan_entity:
            return fan_entity_on(states.get(fan_entity))
        return None

    def _reading(self, ts: datetime) -> Reading:
        from custom_components.climate_advisor.temperature import read_state_temp_f  # noqa: PLC0415

        s = self._s
        states = s.fake_hass.states
        st = states.get(s.climate_entity)
        mode = None
        action = None
        setpoint = None
        if st is not None:
            mode = None if str(st.state).lower() in _UNREADABLE_STATES else str(st.state)
            action = st.attributes.get("hvac_action")
            setpoint = read_state_temp_f(st, "temperature", self._unit)
        fan_on = self._fan_physical_state(states)
        contacts_open: bool | None = None
        if self._contacts:
            inverted = bool(s.config.get("sensor_polarity_inverted", False))
            open_state = "off" if inverted else "on"
            contacts_open = any(
                (cs := states.get(e)) is not None and str(cs.state).lower() == open_state for e in self._contacts
            )
        classification = getattr(s.engine, "_current_classification", None)
        high = getattr(classification, "today_high", None)
        outdoor = getattr(s.engine, "_last_outdoor_temp", None)
        return Reading(
            ts=ts,
            hvac_mode=mode,
            hvac_action=str(action) if action else None,
            indoor_f=self._indoor_f(st),
            outdoor_f=float(outdoor) if isinstance(outdoor, (int, float)) else None,
            forecast_high_f=float(high) if isinstance(high, (int, float)) else None,
            setpoint_f=setpoint,
            fan_on=fan_on,
            contacts_open=contacts_open,
            automation_enabled=self._automation_enabled(),
        )

    def _automation_enabled(self) -> bool:
        coord = self._s.coordinator
        if coord is not None:
            return bool(coord.automation_enabled)
        return not bool(getattr(self._s.engine, "dry_run", False))

    def _command(self, entry: dict, now: datetime, indoor_f: float | None) -> Command | None:
        domain = entry.get("domain")
        if domain not in _COMMAND_DOMAINS:
            return None
        service = str(entry.get("service", ""))
        data = entry.get("data") or {}
        cause = self._take_cause(domain, service, data.get("entity_id"))
        ts = entry.get("ts") or now
        enabled = self._automation_enabled()
        if domain == "climate":
            setpoint = data.get("temperature")
            if setpoint is not None:
                from custom_components.climate_advisor.temperature import to_fahrenheit  # noqa: PLC0415

                try:
                    setpoint = to_fahrenheit(float(setpoint), self._unit)
                except TypeError, ValueError:
                    setpoint = None
            mode = data.get("fan_mode") if service == "set_fan_mode" else data.get("hvac_mode")
            return Command(
                ts=ts,
                target="thermostat",
                service=service,
                mode=str(mode) if mode is not None else None,
                setpoint_f=setpoint,
                origin="ca",
                automation_enabled=enabled,
                indoor_f=indoor_f,
                cause=cause,
            )
        # fan / switch
        return Command(
            ts=ts,
            target="fan",
            service=service,
            fan_on=True if service == "turn_on" else False if service == "turn_off" else None,
            origin="ca",
            automation_enabled=enabled,
            indoor_f=indoor_f,
            cause=cause,
        )
