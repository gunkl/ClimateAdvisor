"""Evidence journal for the household promises (Issue #1128, Step 3).

Pure leaf module: NO Home Assistant imports, no I/O, no clock reads. It defines the shape of
the evidence the promise checks in ``invariant_watchdog.py`` decide on (``Reading``,
``Command``, ``Mark``, ``Episode``, ``PromiseInput``) and a bounded in-memory
``PromiseJournal`` that collects it.

The same journal class is fed by production hooks and by the test adapters, so one pure check
runs in both worlds. Every store is capped (CLAUDE.md: lists that grow MUST have a cap).
Every temperature is internal degrees F. The journal is deliberately not persisted: after a
restart it is empty, every history-needing check sees "no data" and reports nothing (a blind
window of at most ``JOURNAL_MAX_AGE`` and never a false alarm).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

Scalar = str | float | int | bool | None
Attrs = tuple[tuple[str, Scalar], ...]

# ── Retention (design section 2.3) ─────────────────────────────────────────────
JOURNAL_MAX_AGE = timedelta(hours=3)
MAX_READINGS = 400
MAX_COMMANDS = 128
MAX_MARKS = 128
MAX_EPISODE_MARKS = 64
MAX_NOTICES = 16
MAX_OPEN_EPISODES = 8
MAX_SEEN_KEYS = 256
# A reading is stored when a discrete field changed, indoor moved this far, or this long passed.
READING_STORE_DELTA_F = 0.5
READING_STORE_INTERVAL = timedelta(minutes=5)
# CA event followed by a new episode of the same target within this many seconds => "superseded".
SUPERSEDE_WINDOW_S = 5.0


class RunKey(str):
    """Dedup key for a hit that describes a long-running episode (P5b, P7).

    The episode's first in-window reading slides forward once the run outlives the journal window,
    so a key built from that start would change every tick. A ``RunKey`` carries the run's
    ``series`` plus the (start, end) span the check saw; ``PromiseJournal.is_new`` treats a span
    that starts at or before the last reported span's end as the same, still-open episode.
    """

    series: str
    start: datetime
    end: datetime

    def __new__(cls, series: str, start: datetime, end: datetime) -> RunKey:
        obj = super().__new__(cls, f"{series}:{start.isoformat()}")
        obj.series = series
        obj.start = start
        obj.end = end
        return obj


@dataclass(frozen=True, slots=True)
class Reading:
    """One observation of reality (ground truth)."""

    ts: datetime
    hvac_mode: str | None = None
    hvac_action: str | None = None
    indoor_f: float | None = None
    outdoor_f: float | None = None
    forecast_high_f: float | None = None
    setpoint_f: float | None = None
    fan_on: bool | None = None
    contacts_open: bool | None = None
    # Issue #1144: False while automation is switched off (observe-only). Climate Advisor commands nothing then,
    # so the AC/whole-house-fan mutex (P2) cannot be CA's failure; see check_p2_ac_whf_mutex.
    automation_enabled: bool = True


@dataclass(frozen=True, slots=True)
class Command:
    """One climate/fan command CA actually sent. ``target`` is a generic label, never an entity id."""

    ts: datetime
    target: str  # "thermostat" | "fan"
    service: str  # set_hvac_mode | set_temperature | set_fan_mode | turn_on | turn_off
    mode: str | None = None
    setpoint_f: float | None = None
    fan_on: bool | None = None
    origin: str = "ca"  # "ca" | "external"
    automation_enabled: bool = True
    indoor_f: float | None = None  # indoor reading CA had when it sent; None = blind
    cause: str | None = None  # log text only, never used to excuse


@dataclass(frozen=True, slots=True)
class Mark:
    """CA's declared lifecycle word, mapped from its event stream."""

    ts: datetime
    kind: str
    attrs: Attrs = ()


@dataclass(frozen=True, slots=True)
class Episode:
    """An override / fan-off grace window derived from marks (design section 2.4)."""

    id: str
    target: str  # "thermostat" | "fan"
    kind: str  # "override" | "fan_off"
    start_ts: datetime
    value: Scalar
    grace_end_ts: datetime | None
    end_ts: datetime | None
    end_reason: str | None
    setpoint_f: float | None = None


@dataclass(frozen=True, slots=True)
class PromiseInput:
    now: datetime
    latest: Reading | None
    readings: tuple[Reading, ...]
    commands: tuple[Command, ...]
    marks: tuple[Mark, ...]
    episodes: tuple[Episode, ...]
    # Issue #1144: (opened, closed-or-None) spans of observe-only sealed-fan notices, kept past the window so a long
    # run whose start has aged out is still recognised as the noticed one (see PromiseJournal.on_event).
    observe_notices: tuple[tuple[datetime, datetime | None], ...] = ()


# event type -> payload keys read into Mark.attrs. A test asserts every mapped type exists in
# ai_skills_context's renderer table so a renamed event fails loudly instead of blinding P4.
# UNVERIFIED payloads: occupancy_* (only presence is used). A nat-vent session opens on
# nat_vent_activated and ends on any *_exit / nat_vent_exit_resume event (NAT_VENT_END_KINDS).
EVENT_MARK_MAP: dict[str, tuple[str, ...]] = {
    "override_detected": ("detected_mode", "source"),
    "override_confirmed": ("mode", "confirm_delay_seconds"),
    "override_self_resolved": ("detected_mode", "current_mode"),
    "override_cleared": ("was_mode", "old_setpoint_f", "reason"),
    "override_adopted": (),
    "grace_started": ("source", "duration_seconds", "trigger"),
    "grace_cancelled": ("reason", "trigger", "grace_source", "remaining_minutes", "protects_override"),
    "grace_expired": ("source", "re_paused"),
    "fan_manual_override": ("fan_before", "fan_after", "remote_timer_hours"),
    "fan_override_cleared": ("reason", "source", "remote_timer_hours"),
    "stuck_grace_recovered": ("reason",),
    "occupancy_setback": ("mode",),
    "occupancy_comfort_restored": (),
    "nat_vent_activated": (),
    "nat_vent_comfort_floor_exit": (),
    "nat_vent_outdoor_rise_exit": (),
    "nat_vent_away_ceiling_exit": (),
    "nat_vent_manual_override_exit": (),
    "nat_vent_reconcile_exit": (),
    "nat_vent_exit_resume": (),
    "system_restarted": (),
    "startup_coalesced": (),
    # Issue #1144: observe-only (automation off) and the occupant was notified that the fan runs on a sealed house.
    "whf_sealed_observe_notified": (),
}

NAT_VENT_END_KINDS = frozenset(
    {
        "nat_vent_comfort_floor_exit",
        "nat_vent_outdoor_rise_exit",
        "nat_vent_away_ceiling_exit",
        "nat_vent_manual_override_exit",
        "nat_vent_reconcile_exit",
        "nat_vent_exit_resume",
    }
)

# Marks that drive episode derivation (kept longer than the window while an episode is open).
_EPISODE_KINDS = frozenset(
    {
        "override_detected",
        "override_self_resolved",
        "override_cleared",
        "override_adopted",
        "grace_started",
        "grace_cancelled",
        "grace_expired",
        "fan_manual_override",
        "fan_override_cleared",
        "stuck_grace_recovered",
        "system_restarted",
    }
)


def _scalar(value: Any) -> Scalar:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def mark_from_event(ts: datetime, event_type: str, payload: dict[str, Any] | None) -> Mark | None:
    """Map one CA event to a ``Mark`` (None for event types the promises do not read)."""
    keys = EVENT_MARK_MAP.get(event_type)
    if keys is None:
        return None
    data = payload if isinstance(payload, dict) else {}
    return Mark(ts=ts, kind=event_type, attrs=tuple((k, _scalar(data.get(k))) for k in keys))


def mark_attr(mark: Mark, key: str, default: Scalar = None) -> Scalar:
    for k, v in mark.attrs:
        if k == key:
            return v
    return default


class _Ep:
    """Mutable episode builder."""

    __slots__ = ("id", "target", "kind", "start_ts", "value", "grace_end_ts", "end_ts", "end_reason", "setpoint_f")

    def __init__(self, idx: int, target: str, kind: str, ts: datetime, value: Scalar) -> None:
        # Identity is the start time, never the position in the (prunable) mark list: pruning an
        # older episode's marks must not rename a newer one (it would re-report its hits).
        self.id = f"{target}:{kind}:{ts.isoformat()}"
        self.target = target
        self.kind = kind
        self.start_ts = ts
        self.value = value
        self.grace_end_ts: datetime | None = None
        self.end_ts: datetime | None = None
        self.end_reason: str | None = None
        self.setpoint_f: float | None = None

    def freeze(self) -> Episode:
        return Episode(
            self.id,
            self.target,
            self.kind,
            self.start_ts,
            self.value,
            self.grace_end_ts,
            self.end_ts,
            self.end_reason,
            self.setpoint_f,
        )


def derive_episodes(
    marks: tuple[Mark, ...] | list[Mark], readings: tuple[Reading, ...] | list[Reading] = ()
) -> tuple[Episode, ...]:
    """Derive override / fan-off episodes from ordered marks (design section 2.4). Pure.

    ``readings`` is only used to read the occupant's setpoint at the start of a thermostat
    override (first reading at or after the start).
    """
    eps: list[_Ep] = []
    marks = sorted(marks, key=lambda m: m.ts)

    def open_eps(target: str | None = None, kind: str | None = None) -> list[_Ep]:
        return [
            e
            for e in eps
            if e.end_ts is None and (target is None or e.target == target) and (kind is None or e.kind == kind)
        ]

    def close(e: _Ep, ts: datetime, reason: str) -> None:
        e.end_ts = ts
        e.end_reason = reason

    def replaced_soon(i: int, target: str) -> bool:
        ts = marks[i].ts
        for later in marks[i + 1 :]:
            if (later.ts - ts).total_seconds() > SUPERSEDE_WINDOW_S:
                break
            if target == "thermostat" and later.kind == "override_detected":
                return True
            if target == "fan" and later.kind in ("fan_manual_override", "grace_started"):
                return True
        return False

    for i, m in enumerate(marks):
        k = m.kind
        if k == "override_detected":
            for e in open_eps("thermostat", "override"):
                close(e, m.ts, "superseded")
            e = _Ep(i, "thermostat", "override", m.ts, mark_attr(m, "detected_mode"))
            for r in readings:
                if r.ts >= m.ts and r.setpoint_f is not None:
                    e.setpoint_f = r.setpoint_f
                    break
            eps.append(e)
        elif k == "fan_manual_override":
            for e in open_eps("fan"):
                close(e, m.ts, "superseded")
            eps.append(_Ep(i, "fan", "override", m.ts, mark_attr(m, "fan_after")))
        elif k == "grace_started":
            trigger = str(mark_attr(m, "trigger") or "")
            duration = mark_attr(m, "duration_seconds")
            end = m.ts + timedelta(seconds=float(duration)) if isinstance(duration, (int, float)) else None
            if trigger == "fan_off":
                for e in open_eps("fan", "fan_off"):
                    close(e, m.ts, "superseded")
                e = _Ep(i, "fan", "fan_off", m.ts, "off")
                e.grace_end_ts = end
                eps.append(e)
            elif mark_attr(m, "source") == "manual":
                # Only the occupant-override grace sets an override's protected window; door/window
                # resume, nat-vent exit etc. graces say nothing about it.
                target = "fan" if "fan" in trigger else "thermostat"
                for e in open_eps(target, "override"):
                    e.grace_end_ts = end
        elif k == "override_self_resolved":
            for e in open_eps("thermostat", "override"):
                close(e, m.ts, "self_resolved")
        elif k == "override_adopted":
            for e in open_eps("thermostat", "override"):
                close(e, m.ts, "adopted")
        elif k == "override_cleared":
            reason = str(mark_attr(m, "reason") or "unknown")
            if mark_attr(m, "was_mode") is None:
                cleared = open_eps("fan", "override")
            else:
                cleared = open_eps("thermostat", "override") + open_eps("fan", "override")
            # A clear immediately followed by a new occupant override on the same device is the
            # occupant replacing their own change (e.g. a second manual change during grace) -- a
            # legitimate ending, not CA ending it early.
            for e in cleared:
                close(e, m.ts, "superseded" if replaced_soon(i, e.target) else reason)
        elif k == "fan_override_cleared":
            reason = str(mark_attr(m, "reason") or "unknown")
            for e in open_eps("fan", "override"):
                close(e, m.ts, "superseded" if replaced_soon(i, e.target) else reason)
        elif k == "grace_expired":
            for e in open_eps():
                close(e, m.ts, "grace_expired")
        elif k == "grace_cancelled":
            reason = str(mark_attr(m, "reason") or "grace_cancelled")
            protects = mark_attr(m, "protects_override") is True
            fan_off = str(mark_attr(m, "trigger") or "") == "fan_off"
            # A cancelled grace only ends an override episode when it was the one protecting it
            # (door/window resume, nat-vent exit ... graces are unrelated), and only ends a fan-off
            # episode when it was the fan-off grace.
            for e in open_eps():
                if not ((e.kind == "override" and protects) or (e.kind == "fan_off" and fan_off)):
                    continue
                r = reason
                if reason == "superseded" and not replaced_soon(i, e.target):
                    r = "superseded_unreplaced"
                close(e, m.ts, r)
        elif k == "stuck_grace_recovered":
            for e in open_eps():
                close(e, m.ts, "stuck_grace_recovered")
        elif k == "system_restarted":
            for e in open_eps():
                close(e, m.ts, "restart")
    return tuple(e.freeze() for e in eps[-(MAX_OPEN_EPISODES * 4) :])


class PromiseJournal:
    """Bounded in-memory evidence store. All ``record_*`` calls are O(1)."""

    def __init__(self, max_age: timedelta = JOURNAL_MAX_AGE) -> None:
        self._max_age = max_age
        self._readings: deque[Reading] = deque(maxlen=MAX_READINGS)
        self._commands: deque[Command] = deque(maxlen=MAX_COMMANDS)
        self._marks: deque[Mark] = deque(maxlen=MAX_MARKS)
        self._episode_marks: deque[Mark] = deque(maxlen=MAX_EPISODE_MARKS)
        self._latest: Reading | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._notices: deque[list] = deque(maxlen=MAX_NOTICES)  # [opened, closed|None]
        self._runs: dict[str, datetime] = {}  # run series -> end of the last reported span

    # ── recording ────────────────────────────────────────────────────────────
    def record_reading(self, reading: Reading) -> bool:
        """Record a reading; returns True when it was stored in the window (always updates latest)."""
        self._latest = reading
        # Issue #1144: an observe-only sealed-fan notice covers the episode until the fan stops or a window opens.
        if self._notices and self._notices[-1][1] is None and (reading.fan_on is False or reading.contacts_open):
            self._notices[-1][1] = reading.ts
        last = self._readings[-1] if self._readings else None
        if last is not None and not self._worth_storing(last, reading):
            return False
        self._readings.append(reading)
        return True

    @staticmethod
    def _worth_storing(last: Reading, new: Reading) -> bool:
        if (
            last.hvac_mode != new.hvac_mode
            or last.hvac_action != new.hvac_action
            or last.fan_on != new.fan_on
            or last.contacts_open != new.contacts_open
            or (last.indoor_f is None) != (new.indoor_f is None)
        ):
            return True
        if last.setpoint_f != new.setpoint_f and (
            last.setpoint_f is None or new.setpoint_f is None or abs(last.setpoint_f - new.setpoint_f) >= 0.5
        ):
            return True
        if (
            last.indoor_f is not None
            and new.indoor_f is not None
            and abs(new.indoor_f - last.indoor_f) >= READING_STORE_DELTA_F
        ):
            return True
        return (new.ts - last.ts) >= READING_STORE_INTERVAL

    def record_command(self, command: Command) -> None:
        self._commands.append(command)

    def record_mark(self, mark: Mark) -> None:
        self._marks.append(mark)
        if mark.kind in _EPISODE_KINDS:
            self._episode_marks.append(mark)

    def on_event(self, ts: datetime, event_type: str, payload: dict[str, Any] | None = None) -> Mark | None:
        """Map and record one CA event; returns the mark or None when not read by any promise."""
        mark = mark_from_event(ts, event_type, payload)
        if mark is not None:
            self.record_mark(mark)
        if event_type == "whf_sealed_observe_notified" and not (self._notices and self._notices[-1][1] is None):
            self._notices.append([ts, None])
        return mark

    def reset(self) -> None:
        """Clean slate (restart): evidence is dropped, dedup memory is kept."""
        self._readings.clear()
        self._commands.clear()
        self._marks.clear()
        self._episode_marks.clear()
        self._notices.clear()
        self._latest = None

    # ── window ───────────────────────────────────────────────────────────────
    def window(self, now: datetime) -> PromiseInput:
        """Prune by age and return an immutable snapshot."""
        cutoff = now - self._max_age
        self._prune(self._readings, cutoff)
        self._prune(self._commands, cutoff)
        self._prune(self._marks, cutoff)
        readings = tuple(self._readings)
        # Open episodes are never aged out: keep their marks until an end mark closes them.
        eps = derive_episodes(tuple(self._episode_marks), readings)
        open_starts = [e.start_ts for e in eps if e.end_ts is None]
        ep_cutoff = min([cutoff, *[s - timedelta(microseconds=1) for s in open_starts]])
        self._prune(self._episode_marks, ep_cutoff)
        eps = derive_episodes(tuple(self._episode_marks), readings)
        eps = tuple(e for e in eps if e.end_ts is None or e.end_ts >= cutoff)
        return PromiseInput(
            now=now,
            latest=self._latest,
            readings=readings,
            commands=tuple(self._commands),
            marks=tuple(self._marks),
            episodes=eps,
            observe_notices=tuple((o, c) for o, c in self._notices),
        )

    @staticmethod
    def _prune(store: deque, cutoff: datetime) -> None:
        while store and store[0].ts < cutoff:
            store.popleft()

    # ── dedup ────────────────────────────────────────────────────────────────
    def is_new(self, key: str) -> bool:
        """True the first time ``key`` is seen (and remembers it); LRU-capped.

        A ``RunKey`` is new only when its span starts after the last reported span of the same
        series ended, i.e. the earlier run genuinely closed and a new one opened.
        """
        if isinstance(key, RunKey):
            prev_end = self._runs.get(key.series)
            if prev_end is not None and key.start <= prev_end:
                self._runs[key.series] = max(prev_end, key.end)
                return False
            self._runs[key.series] = key.end
            return True
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN_KEYS:
            self._seen.popitem(last=False)
        return True

    # ── introspection (tests, diagnostics) ───────────────────────────────────
    def sizes(self) -> dict[str, int]:
        return {
            "readings": len(self._readings),
            "commands": len(self._commands),
            "marks": len(self._marks),
            "episode_marks": len(self._episode_marks),
            "seen": len(self._seen),
        }


# ── persisted per-promise counters ──────────────────────────────────────────────
def sanitize_promise_stats(raw: Any, allowed_keys: frozenset[str] | set[str]) -> dict[str, dict[str, Any]]:
    """Type-validate persisted ``promise_stats``: dict of allowed key -> {hits:int, first_ts, last_ts}."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        if key not in allowed_keys or not isinstance(val, dict) or len(out) >= len(allowed_keys):
            continue
        hits = val.get("hits")
        first_ts = val.get("first_ts")
        last_ts = val.get("last_ts")
        if not isinstance(hits, int) or isinstance(hits, bool) or hits < 0:
            continue
        out[key] = {
            "hits": hits,
            "first_ts": first_ts if isinstance(first_ts, str) else None,
            "last_ts": last_ts if isinstance(last_ts, str) else None,
        }
    return out


def bump_promise_stats(stats: dict[str, dict[str, Any]], key: str, ts: datetime) -> None:
    """Count one hit for ``key`` (in place)."""
    entry = stats.setdefault(key, {"hits": 0, "first_ts": None, "last_ts": None})
    entry["hits"] = int(entry.get("hits", 0)) + 1
    iso = ts.isoformat()
    if entry.get("first_ts") is None:
        entry["first_ts"] = iso
    entry["last_ts"] = iso
