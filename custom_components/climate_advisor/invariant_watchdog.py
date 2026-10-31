"""Hard system invariants that must never be violated (Issue #749).

Deliberately minimal — this is NOT a general validation framework. It holds a small,
explicit list of absolute system states, each checked by a pure function over
**ground-truth** inputs only (live sensor/thermostat reads), never internal automation
bookkeeping (override flags, grace flags, session flags). Ground truth is the point: the
2026-08-22 incident that motivated this module (Issue #739/#748 — the AC and the
whole-house fan running simultaneously) happened while CA's own internal bookkeeping
(``_fan_override_active``, ``_fan_remote_timer_hours``) was entirely self-consistent. Only a
check that ignores that bookkeeping and reads the physical world directly can catch the next
bug of this shape, whatever its internal cause turns out to be.

This module detects and alerts only. It never issues a corrective command — the automation
engine's own command-layer fixes (e.g. ``_deactivate_fan()``/``_suppress_hvac_for_whf()``)
are the sole enforcement path for the invariants checked here. A violation firing after such
a fix has landed is itself a signal that something new needs investigating, not something to
silently paper over with a second, independent enforcement path (this codebase has been bitten
before by two parallel copies of the same rule drifting out of sync).

Household promises (Issue #1128): the second half of this module (``PROMISES``,
``run_promise_checks``) holds the broader "household promises" — pure checks over an immutable
``PromiseInput`` built by ``promise_journal.PromiseJournal``. Same ground-truth rule, with ONE
amendment dated 2026-10-29 (user-approved): promise 4 (``override_respected``) may also read
CA's own override/grace *events* (``Episode``/``Mark``), because it is a promise about CA keeping
its own word. Every other promise reads only ground truth: thermostat/fan readings and the
service commands CA actually issued.

Changing the invariant set (adding, removing, or loosening a check) is treated the same as
this project's LOCKED golden-scenario policy (see CLAUDE.md's "Golden Simulation Test
Policy"): it requires explicit human review — show the proposed invariant in human-readable
form, confirm it represents a genuine hard constraint, only then land it. Do not add a new
entry to ``INVARIANTS`` without that sign-off.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    COMFORT_FAMILY_RECENCY_WINDOW_MIN_MAX,
    COMFORT_FAMILY_RECENCY_WINDOW_MIN_MIN,
    CONF_COMFORT_DEADBAND_COLD_F,
    CONF_COMFORT_DEADBAND_COOL_F,
    CONF_COMFORT_DEADBAND_HOT_F,
    CONF_COMFORT_DEADBAND_MILD_F,
    CONF_COMFORT_DEADBAND_WARM_F,
    CONF_COMFORT_FAMILY_RECENCY_WINDOW_MIN,
    CONF_FAN_MODE,
    CONF_SLEEP_COOL,
    CONF_SLEEP_HEAT,
    CONF_TEMP_UNIT,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_DEADBAND_COLD_F,
    DEFAULT_COMFORT_DEADBAND_COOL_F,
    DEFAULT_COMFORT_DEADBAND_HOT_F,
    DEFAULT_COMFORT_DEADBAND_MILD_F,
    DEFAULT_COMFORT_DEADBAND_WARM_F,
    DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_SLEEP_COOL,
    DEFAULT_SLEEP_HEAT,
    DEFAULT_THRESHOLD_MILD,
    DEFAULT_THRESHOLD_WARM,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_WHOLE_HOUSE,
)
from .hvac_action import is_hvac_compressor_active
from .promise_journal import NAT_VENT_END_KINDS, Command, Episode, Mark, PromiseInput, Reading, RunKey
from .temperature import format_temp_delta

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvariantViolation:
    """A single hard-invariant violation, ready to log/notify/surface."""

    name: str
    detail: str


def check_ac_whf_mutex(
    *,
    hvac_action: str | None,
    whf_physically_on: bool | None,
    fan_mode: str,
) -> InvariantViolation | None:
    """AC and the whole-house fan must never both be physically active at once.

    Scoped to ``FAN_MODE_WHOLE_HOUSE``/``FAN_MODE_BOTH`` archetypes only —
    ``FAN_MODE_HVAC`` coexists with the compressor by design (that fan IS the thermostat's
    own blower, not a whole-house fan fighting it for outdoor air exchange). See
    docs/08-COMPUTATION-REFERENCE.md's WHF-vs-HVAC-fan distinct-physics note.

    Args:
        hvac_action: The thermostat's live ``hvac_action`` attribute (ground truth —
            "heating"/"cooling"/"fan"/"idle"/"off"/etc.), not the commanded ``hvac_mode``.
        whf_physically_on: The WHF's live physical state (ground truth, e.g. from
            ``coordinator._get_fan_physical_state()``). ``None`` means unknown (fan state
            feedback not configured/available) — a violation cannot be confirmed without a
            physical read, so this returns no violation rather than guessing.
        fan_mode: The configured ``CONF_FAN_MODE`` value.
    """
    if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if whf_physically_on is not True:
        return None
    if hvac_action is None:
        return None
    if not is_hvac_compressor_active(hvac_action):
        return None
    return InvariantViolation(
        name="ac_whf_mutex",
        detail=(
            f"AC is actively {hvac_action} while the whole-house fan is physically running — "
            "these must never run at the same time."
        ),
    )


# Deliberately a flat list, not a registry/plugin system — add a new entry only with the
# explicit human sign-off described in this module's docstring.
INVARIANTS = [check_ac_whf_mutex]


def run_invariant_checks(
    *,
    hvac_action: str | None,
    whf_physically_on: bool | None,
    fan_mode: str,
) -> list[InvariantViolation]:
    """Run every registered hard invariant and return any violations found.

    Cheap, synchronous, pure dict/attribute lookups only — safe to call every coordinator
    update cycle with no executor offload.
    """
    violations = []
    for check in INVARIANTS:
        violation = check(hvac_action=hvac_action, whf_physically_on=whf_physically_on, fan_mode=fan_mode)
        if violation is not None:
            violations.append(violation)
    return violations


# ═══════════════════════════════════════════════════════════════════════════════
# Household promises (Issue #1128, Step 3) — pure checks over PromiseInput
# ═══════════════════════════════════════════════════════════════════════════════

SEVERITY_HARD = "hard"
SEVERITY_WARNING = "warning"
LEVEL_LOG = "log"
LEVEL_ALERT = "alert"
KIND_EVENT = "event"
KIND_STATE = "state"
SCOPE_HOME = "home"
SCOPE_TEST = "test"

Evidence = tuple[tuple[str, "str | float | bool | None"], ...]


@dataclass(frozen=True)
class PromiseNumbers:
    """Every tunable promise number, in one place. Each is looser than production (design 3.1).

    ``tests/test_promise_numbers.py`` walks the legal configuration grid and fails if a promise
    ever becomes stricter than production's own allowance.
    """

    P1_WINDOW_MIN: float = 120.0  # = default recency window; effective = min(this, configured) - slack
    P1_SHALLOW_F: float = 2.0  # = default tolerance; effective = min(this, smallest configured) - slack
    READING_SLACK_F: float = 0.5  # whole-degree thermostat readings
    TIME_SLACK_MIN: float = 5.0  # timer jitter
    P1_NATIVE_MARGIN_F: float = 3.0  # forecast-high gate margin around the day-type thresholds
    P1_OCCUPANT_EXCUSE_MIN: float = 30.0
    P4_REVERSAL_SETPOINT_F: float = 2.0  # production's setpoint-match tolerance is 1.0
    P4_MIN_EARLY_END_S: float = 60.0
    P4_START_SLACK_S: float = 10.0
    P4_END_SLACK_S: float = 5.0
    P5_BLIND_CONTINUE_MIN: float = 35.0  # production auto-stops at 30 min
    P6_MAX_FLIPS: int = 2
    P6_WINDOW_MIN: float = 60.0
    P6_OCCUPANT_EXCUSE_MIN: float = 5.0
    P7_MAX_MIN: float = 45.0
    P7_SLACK_F: float = 1.0
    P7_MAX_GAP_MIN: float = 35.0
    P7_AGGRESSIVE_WIDEN_F: float = 2.0
    P7_OVERSHOOT_EXPLAINED_F: float = 5.0
    P7_OVERSHOOT_WINDOW_MIN: float = 120.0
    P7_EXCLUSION_MIN: float = 60.0
    TRANSITION_MIN: float = 60.0  # sleep/wake boundary band where the extreme of both values is used
    OCCUPANT_ECHO_S: float = 45.0  # cloud thermostats echo a CA command ~25 s later (#1049)
    OCCUPANT_AFTER_S: float = 5.0
    OCCUPANT_SETPOINT_F: float = 0.5


NUMBERS = PromiseNumbers()


def _num(config: dict[str, Any], key: str, default: float) -> float:
    val = config.get(key, default)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return float(default)
    return float(val)


def _parse_minute(value: Any) -> int | None:
    """'HH:MM[:SS]' -> minute of day; malformed -> None."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError, IndexError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


@dataclass(frozen=True)
class PromiseConfig:
    """Raw-config view the promises need. Never imports the engine or its band helpers."""

    fan_mode: str = FAN_MODE_DISABLED
    comfort_heat: float = float(DEFAULT_COMFORT_HEAT)
    comfort_cool: float = float(DEFAULT_COMFORT_COOL)
    sleep_heat: float = float(DEFAULT_SLEEP_HEAT)
    sleep_cool: float = float(DEFAULT_SLEEP_COOL)
    sleep_minute: int | None = None
    wake_minute: int | None = None
    aggressive_savings: bool = False
    tolerances_f: tuple[float, ...] = (
        DEFAULT_COMFORT_DEADBAND_HOT_F,
        DEFAULT_COMFORT_DEADBAND_WARM_F,
        DEFAULT_COMFORT_DEADBAND_MILD_F,
        DEFAULT_COMFORT_DEADBAND_COOL_F,
        DEFAULT_COMFORT_DEADBAND_COLD_F,
    )
    recency_window_min: float = DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN
    threshold_mild: float = float(DEFAULT_THRESHOLD_MILD)
    threshold_warm: float = float(DEFAULT_THRESHOLD_WARM)
    temp_unit: str = "fahrenheit"
    p2_min_overlap_s: float = 0.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PromiseConfig:
        config = config if isinstance(config, dict) else {}
        tol_keys = (
            (CONF_COMFORT_DEADBAND_HOT_F, DEFAULT_COMFORT_DEADBAND_HOT_F),
            (CONF_COMFORT_DEADBAND_WARM_F, DEFAULT_COMFORT_DEADBAND_WARM_F),
            (CONF_COMFORT_DEADBAND_MILD_F, DEFAULT_COMFORT_DEADBAND_MILD_F),
            (CONF_COMFORT_DEADBAND_COOL_F, DEFAULT_COMFORT_DEADBAND_COOL_F),
            (CONF_COMFORT_DEADBAND_COLD_F, DEFAULT_COMFORT_DEADBAND_COLD_F),
        )
        window = _num(config, CONF_COMFORT_FAMILY_RECENCY_WINDOW_MIN, DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN)
        window = min(max(window, COMFORT_FAMILY_RECENCY_WINDOW_MIN_MIN), COMFORT_FAMILY_RECENCY_WINDOW_MIN_MAX)
        return cls(
            fan_mode=str(config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)),
            comfort_heat=_num(config, "comfort_heat", DEFAULT_COMFORT_HEAT),
            comfort_cool=_num(config, "comfort_cool", DEFAULT_COMFORT_COOL),
            sleep_heat=_num(config, CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT),
            sleep_cool=_num(config, CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL),
            sleep_minute=_parse_minute(config.get("sleep_time")),
            wake_minute=_parse_minute(config.get("wake_time")),
            aggressive_savings=bool(config.get("aggressive_savings", False)),
            tolerances_f=tuple(_num(config, k, d) for k, d in tol_keys),
            recency_window_min=window,
            threshold_mild=_num(config, "threshold_mild", DEFAULT_THRESHOLD_MILD),
            threshold_warm=_num(config, "threshold_warm", DEFAULT_THRESHOLD_WARM),
            temp_unit=str(config.get(CONF_TEMP_UNIT, "fahrenheit")),
        )

    # ── promise-owned sleep window (midnight wrap; malformed -> never asleep) ──
    def in_sleep(self, t: datetime) -> bool:
        if self.sleep_minute is None or self.wake_minute is None:
            return False
        m = t.hour * 60 + t.minute
        s, w = self.sleep_minute, self.wake_minute
        if s == w:
            return False
        return (s <= m < w) if s < w else (m >= s or m < w)

    def in_transition(self, t: datetime, minutes: float = NUMBERS.TRANSITION_MIN) -> bool:
        if self.sleep_minute is None or self.wake_minute is None:
            return False
        m = t.hour * 60 + t.minute
        for b in (self.sleep_minute, self.wake_minute):
            d = abs(m - b)
            if min(d, 1440 - d) <= minutes:
                return True
        return False

    def floor_hi(self, t: datetime) -> float:
        if self.in_transition(t):
            return max(self.comfort_heat, self.sleep_heat)
        return self.sleep_heat if self.in_sleep(t) else self.comfort_heat

    def ceiling_lo(self, t: datetime) -> float:
        if self.in_transition(t):
            return min(self.comfort_cool, self.sleep_cool)
        return self.sleep_cool if self.in_sleep(t) else self.comfort_cool

    def floor_lo(self, t: datetime) -> float:
        if self.in_transition(t):
            return min(self.comfort_heat, self.sleep_heat)
        return self.sleep_heat if self.in_sleep(t) else self.comfort_heat

    def ceiling_hi(self, t: datetime) -> float:
        if self.in_transition(t):
            return max(self.comfort_cool, self.sleep_cool)
        return self.sleep_cool if self.in_sleep(t) else self.comfort_cool

    # ── effective P1 numbers (looser than production by construction) ──────────
    def p1_dip_limit_f(self) -> float:
        """Flag only a dip shallower than this (production holds up to the smallest tolerance)."""
        smallest = min(self.tolerances_f) if self.tolerances_f else NUMBERS.P1_SHALLOW_F
        return min(NUMBERS.P1_SHALLOW_F, smallest) - NUMBERS.READING_SLACK_F

    def p1_window_min(self) -> float:
        """Look back this far for the opposite equipment (production's window is the configured one)."""
        return min(NUMBERS.P1_WINDOW_MIN, self.recency_window_min) - NUMBERS.TIME_SLACK_MIN


@dataclass(frozen=True, slots=True)
class Hit:
    """One broken promise, ready to log / event / assert."""

    promise: str
    ts: datetime
    key: str
    detail: str
    evidence: Evidence = ()
    kind: str = KIND_EVENT


@dataclass(frozen=True, slots=True)
class OccupantAction:
    ts: datetime
    target: str  # "thermostat" | "fan"
    what: str  # "mode" | "setpoint" | "fan"


def _mins(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _ev(**kw: Any) -> Evidence:
    return tuple((k, round(v, 2) if isinstance(v, float) else v) for k, v in kw.items())


def equipment(r: Reading) -> str | None:
    """'heating' | 'cooling' | 'idle' | 'fan' | None (unknown) from ground truth.

    Uses ``hvac_action`` when the thermostat reports it; otherwise infers from mode, setpoint and
    indoor with a 0.5 F swing.
    """
    a = r.hvac_action
    if a:
        a = a.lower()
        if a in ("heating", "cooling", "fan"):
            return a
        if a in ("idle", "off"):
            return "idle"
        return None
    mode = (r.hvac_mode or "").lower()
    if mode == "off":
        return "idle"
    if mode in ("heat", "cool") and r.indoor_f is not None and r.setpoint_f is not None:
        if mode == "heat":
            return "heating" if r.indoor_f < r.setpoint_f - 0.5 else "idle"
        return "cooling" if r.indoor_f > r.setpoint_f + 0.5 else "idle"
    return None


# One-entry memo: the window is an immutable snapshot, and four promises ask the same question of
# it, so run_promise_checks pays for it once per tick. Identity (``is``), never equality.
_OCC_MEMO: tuple[PromiseInput | None, list[OccupantAction]] = (None, [])


def occupant_actions(inp: PromiseInput) -> list[OccupantAction]:
    """Mode / setpoint / fan changes between consecutive readings with no matching CA command nearby.

    Needs only readings and commands, so production and tests derive the same occupant actions
    without trusting CA's own override detection. Treat the returned list as read-only (it is
    shared by every check of the same input).
    """
    global _OCC_MEMO
    if _OCC_MEMO[0] is inp:
        return _OCC_MEMO[1]
    out = _compute_occupant_actions(inp)
    _OCC_MEMO = (inp, out)
    return out


def _any_in_span(sorted_ts: list[datetime], ts: datetime, span: timedelta, *, inclusive: bool) -> bool:
    """True when some stamp ``s`` has ``0 <= ts - s <= span`` (``< span`` when not inclusive)."""
    hi = bisect_right(sorted_ts, ts)  # stamps <= ts
    if hi == 0:
        return False
    newest = sorted_ts[hi - 1]
    return (ts - newest) <= span if inclusive else (ts - newest) < span


def _compute_occupant_actions(inp: PromiseInput) -> list[OccupantAction]:
    ca_by_target: dict[str, list[datetime]] = {}
    for c in inp.commands:
        if c.origin == "ca":
            ca_by_target.setdefault(c.target, []).append(c.ts)
    for stamps in ca_by_target.values():
        stamps.sort()
    echo = timedelta(seconds=NUMBERS.OCCUPANT_ECHO_S)
    after = timedelta(seconds=NUMBERS.OCCUPANT_AFTER_S)
    out: list[OccupantAction] = []
    rs = inp.readings
    for i in range(1, len(rs)):
        prev, cur = rs[i - 1], rs[i]
        changes: list[tuple[str, str]] = []
        if prev.hvac_mode is not None and cur.hvac_mode is not None and prev.hvac_mode != cur.hvac_mode:
            changes.append(("thermostat", "mode"))
        if (
            prev.setpoint_f is not None
            and cur.setpoint_f is not None
            and abs(prev.setpoint_f - cur.setpoint_f) >= NUMBERS.OCCUPANT_SETPOINT_F
        ):
            changes.append(("thermostat", "setpoint"))
        if prev.fan_on is not None and cur.fan_on is not None and prev.fan_on != cur.fan_on:
            changes.append(("fan", "fan"))
        for target, what in changes:
            stamps = ca_by_target.get(target, [])
            i0 = bisect_left(stamps, cur.ts - echo)
            if i0 < len(stamps) and stamps[i0] <= cur.ts + after:
                continue
            out.append(OccupantAction(cur.ts, target, what))
    return out


# ── P1 settling_flip ───────────────────────────────────────────────────────────
# Issue #1150 part B. When True, the 30-min occupant excuse is ALSO measured to the first CA
# ``set_hvac_mode`` that armed the same-side family (heat / cool) within that window, not only to
# the equipment edge: the occupant spoke, CA armed in response, and the equipment lagged the
# command. This WIDENS the excuse (design Invariant 2: loosening needs owner sign-off), so it
# stays False (the original behaviour) until the owner answers.
P1_EXCUSE_ANCHOR_ON_ARMING_COMMAND = False


def _p1_arming_command_ts(inp: PromiseInput, side: str, cur_ts: datetime, excuse: timedelta) -> datetime | None:
    """Timestamp of the first CA ``set_hvac_mode`` into the ``side`` family within ``excuse`` before ``cur_ts``."""
    want = "heat" if side == "heat" else "cool"
    stamps = [
        c.ts
        for c in inp.commands
        if c.origin == "ca"
        and c.target == "thermostat"
        and c.service == "set_hvac_mode"
        and (c.mode or "").lower() == want
        and timedelta(0) <= cur_ts - c.ts <= excuse
    ]
    return min(stamps) if stamps else None


def _p1_start_judged(rs: tuple[Reading, ...] | list[Reading], k: int) -> bool:
    """True when the equipment start at ``rs[k]`` was evaluable by P1 (same input gates as the main loop)."""
    if k <= 0:
        return False
    prev, cur = rs[k - 1], rs[k]
    if equipment(prev) is None or prev.hvac_mode is None or cur.hvac_mode is None:
        return False
    return cur.indoor_f is not None and cur.setpoint_f is not None and cur.forecast_high_f is not None


def check_p1_settling_flip(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """After cooling, a shallow dip must not start the heat (and the mirror)."""
    hits: list[Hit] = []
    rs = inp.readings
    if len(rs) < 2:
        return hits
    dip_limit = cfg.p1_dip_limit_f()
    window = cfg.p1_window_min()
    if window <= 0 or dip_limit <= 0:
        return hits
    occ_ts = sorted(o.ts for o in occupant_actions(inp) if o.target == "thermostat")
    excuse = timedelta(minutes=NUMBERS.P1_OCCUPANT_EXCUSE_MIN)
    for i in range(1, len(rs)):
        prev, cur = rs[i - 1], rs[i]
        ea, eb = equipment(prev), equipment(cur)
        if eb == "heating" and ea not in (None, "heating"):
            side, opposite = "heat", "cooling"
        elif eb == "cooling" and ea not in (None, "cooling"):
            side, opposite = "cool", "heating"
        else:
            continue
        if cur.indoor_f is None or cur.setpoint_f is None or cur.forecast_high_f is None:
            continue
        if prev.hvac_mode is None or cur.hvac_mode is None:
            continue
        if side == "heat":
            edge = cfg.floor_hi(cur.ts)
            dip = edge - cur.indoor_f
            defending = cur.setpoint_f <= edge + NUMBERS.READING_SLACK_F
            applies = cur.forecast_high_f >= cfg.threshold_mild + NUMBERS.P1_NATIVE_MARGIN_F
        else:
            edge = cfg.ceiling_lo(cur.ts)
            dip = cur.indoor_f - edge
            defending = cur.setpoint_f >= edge - NUMBERS.READING_SLACK_F
            applies = cur.forecast_high_f < cfg.threshold_warm - NUMBERS.P1_NATIVE_MARGIN_F
        if not (applies and defending):
            continue
        if not (-NUMBERS.READING_SLACK_F <= dip < dip_limit):
            continue
        # A flip is opposite -> idle* -> same-side with NO same-side run in between (#1150 part A):
        # same-side equipment found first means this is a restart of the family, not a flip -- but
        # only when that earlier run's own start was judged by this check. If its start could not be
        # judged (unknown mode/equipment or missing inputs at that edge), keep looking for the opposite
        # equipment, or a real flip whose first start was unjudgeable would go unreported.
        same_side = "heating" if side == "heat" else "cooling"
        last_opposite: datetime | None = None
        j = i - 1
        while j >= 0:
            r = rs[j]
            if (cur.ts - r.ts) > timedelta(minutes=window):
                break
            eq = equipment(r)
            if eq == same_side:
                k = j
                while k > 0 and equipment(rs[k - 1]) == same_side:
                    k -= 1
                if _p1_start_judged(rs, k):
                    break
                j = k - 1
                continue
            if eq == opposite:
                last_opposite = r.ts
                break
            j -= 1
        if last_opposite is None:
            continue
        if _any_in_span(occ_ts, cur.ts, excuse, inclusive=True):
            continue
        if P1_EXCUSE_ANCHOR_ON_ARMING_COMMAND:
            armed = _p1_arming_command_ts(inp, side, cur.ts, excuse)
            if armed is not None and _any_in_span(occ_ts, armed, excuse, inclusive=True):
                continue
        if any(
            e.target == "thermostat"
            and e.kind == "override"
            and e.start_ts <= cur.ts
            and (e.end_ts is None or e.end_ts >= cur.ts)
            for e in inp.episodes
        ):
            continue
        since = _mins(last_opposite, cur.ts)
        where = (
            "right at"
            if dip <= 0.05
            else f"only {format_temp_delta(dip, cfg.temp_unit, 1)} {'under' if side == 'heat' else 'over'}"
        )
        if side == "heat":
            detail = (
                f"Your heat started {since:.0f} min after the AC cooled the house, while the house was "
                f"{where} your comfort minimum. It should have waited."
            )
        else:
            detail = (
                f"Your AC started {since:.0f} min after the heat ran, while the house was "
                f"{where} your comfort maximum. It should have waited."
            )
        hits.append(
            Hit(
                promise="settling_flip",
                ts=cur.ts,
                key=f"settling_flip:{side}@{cur.ts.strftime('%Y-%m-%dT%H:%M')}",
                detail=detail,
                evidence=_ev(
                    side=side,
                    minutes_since_opposite=since,
                    dip_f=dip,
                    indoor_f=cur.indoor_f,
                    edge_f=edge,
                    outdoor_f=cur.outdoor_f,
                    setpoint_f=cur.setpoint_f,
                ),
            )
        )
    return hits


# ── P2 ac_whf_mutex (adapter over the existing snapshot check) ──────────────────
def check_p2_ac_whf_mutex(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    latest = inp.latest
    if latest is None:
        return []
    v = check_ac_whf_mutex(hvac_action=latest.hvac_action, whf_physically_on=latest.fan_on, fan_mode=cfg.fan_mode)
    if v is None:
        return []
    if cfg.p2_min_overlap_s > 0:
        bound = latest.ts - timedelta(seconds=cfg.p2_min_overlap_s)
        older = [r for r in inp.readings if r.ts <= bound]
        if not older:
            return []
        span = [older[-1]] + [r for r in inp.readings if bound < r.ts <= latest.ts]
        if not all(
            check_ac_whf_mutex(hvac_action=r.hvac_action, whf_physically_on=r.fan_on, fan_mode=cfg.fan_mode)
            for r in span
        ):
            return []
    return [
        Hit(
            promise="ac_whf_mutex",
            ts=latest.ts,
            key="ac_whf_mutex",
            detail=v.detail,
            evidence=_ev(hvac_action=latest.hvac_action, fan_on=latest.fan_on),
            kind=KIND_STATE,
        )
    ]


# ── P3 observe_only_silence ────────────────────────────────────────────────────
def check_p3_observe_only_silence(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """With automation switched off, CA sends no thermostat or fan command (no tolerance)."""
    hits: list[Hit] = []
    for c in inp.commands:
        if c.origin == "ca" and c.target in ("thermostat", "fan") and c.automation_enabled is False:
            hits.append(
                Hit(
                    promise="observe_only_silence",
                    ts=c.ts,
                    key=f"observe_only_silence:{c.ts.isoformat()}:{c.service}",
                    detail=(
                        "Climate Advisor sent a command to your thermostat or fan while automation was switched off."
                    ),
                    evidence=_ev(target=c.target, service=c.service),
                )
            )
    return hits


# ── P4 override_respected ──────────────────────────────────────────────────────
_COMMON_LISTED = frozenset({"grace_expired", "superseded", "restart"})
# ``new_override_during_grace`` is the occupant replacing their own change (#201, "the most recent
# user action always wins"); the replacement's ``override_detected`` mark can be deduped away for
# 5 min, so supersede detection alone cannot be relied on to excuse it.
_THERMOSTAT_LISTED = _COMMON_LISTED | {
    "adopted",
    "adopted_matching_decision",
    "self_resolved",
    "ac_whf_mutex",
    "new_override_during_grace",
}
_FAN_OVERRIDE_LISTED = _THERMOSTAT_LISTED | {"bedtime", "morning_wakeup"}


def _p4_end_is_listed(ep: Episode) -> bool:
    r = ep.end_reason or ""
    if r.startswith("user_cancel"):
        return True
    if ep.target == "thermostat":
        return r in _THERMOSTAT_LISTED or r.startswith("occupancy_")
    if ep.kind == "override":  # fan override
        return r in _FAN_OVERRIDE_LISTED or r.startswith("occupancy_")
    return r in _COMMON_LISTED or "ceiling" in r  # fan_off grace (#134 ceiling exception)


# Services whose ``Command.mode`` is an HVAC mode. ``set_fan_mode`` also stores its fan mode in
# ``Command.mode`` (P5 reads it), so it must never be compared with the occupant's HVAC mode.
_HVAC_MODE_SERVICES = ("set_hvac_mode", "set_temperature")


def _p4_reverses(cmd: Command, ep: Episode) -> bool:
    if ep.target == "thermostat":
        if (
            cmd.service in _HVAC_MODE_SERVICES
            and cmd.mode is not None
            and ep.value is not None
            and cmd.mode != ep.value
        ):
            return True
        return (
            cmd.setpoint_f is not None
            and ep.setpoint_f is not None
            and abs(cmd.setpoint_f - ep.setpoint_f) >= NUMBERS.P4_REVERSAL_SETPOINT_F
        )
    if cmd.fan_on is None:
        return False
    occupant_wants_off = ep.kind == "fan_off" or str(ep.value).lower() in ("off", "false", "0")
    return cmd.fan_on is True if occupant_wants_off else cmd.fan_on is False


def _p4_when(remaining_min: float) -> str:
    """Wording for a reversal: minutes of grace left (round half up), or 'still in force' when none."""
    whole = int(remaining_min + 0.5)
    if whole <= 0:
        return "while your manual change was still in force"
    return f"{whole} min before your manual change was due to end"


def check_p4_override_respected(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """A manual override is never undone before its grace ends, except by a listed event.

    The only promise that reads CA's own override/grace events (brief Invariant 1 amendment,
    2026-10-29): it asks "after the occupant spoke and CA acknowledged it, did CA send the
    opposite command before a listed ending?".
    """
    hits: list[Hit] = []
    occ_ts = sorted(o.ts for o in occupant_actions(inp))
    start_slack = timedelta(seconds=NUMBERS.P4_START_SLACK_S)
    end_slack = timedelta(seconds=NUMBERS.P4_END_SLACK_S)
    for ep in inp.episodes:
        reported_ts: set[datetime] = set()  # one hit per reversal instant (#1151)
        listed = ep.end_ts is not None and _p4_end_is_listed(ep)
        if ep.end_ts is None:
            until = inp.now
        elif listed:
            until = ep.end_ts
        else:
            until = max(ep.end_ts, ep.grace_end_ts or ep.end_ts)
        label = "thermostat" if ep.target == "thermostat" else "fan"
        for cmd in inp.commands:
            if cmd.origin != "ca" or cmd.target != ep.target:
                continue
            if not (ep.start_ts < cmd.ts <= until):
                continue
            if listed and ep.end_ts is not None and cmd.ts >= ep.end_ts - end_slack:
                continue
            if not _p4_reverses(cmd, ep):
                continue
            if cmd.ts in reported_ts:
                continue
            first_after = bisect_right(occ_ts, ep.start_ts + start_slack)
            if first_after < len(occ_ts) and occ_ts[first_after] <= cmd.ts:
                continue
            reported_ts.add(cmd.ts)
            remaining = _mins(cmd.ts, ep.grace_end_ts) if ep.grace_end_ts and ep.grace_end_ts > cmd.ts else 0.0
            hits.append(
                Hit(
                    promise="override_respected",
                    ts=cmd.ts,
                    key=f"override_respected:{ep.id}:{cmd.ts.isoformat()}",
                    detail=(f"Climate Advisor changed your {label} setting {_p4_when(remaining)}."),
                    evidence=_ev(
                        target=ep.target,
                        remaining_min=remaining,
                        reversed_to=cmd.mode if cmd.mode is not None else cmd.fan_on,
                        end_reason=ep.end_reason,
                    ),
                )
            )
        # (b) silent early end: unlisted ending while grace remained, so nothing protects it any more.
        if (
            ep.end_ts is not None
            and not listed
            and ep.grace_end_ts is not None
            and (ep.grace_end_ts - ep.end_ts).total_seconds() >= NUMBERS.P4_MIN_EARLY_END_S
        ):
            remaining = _mins(ep.end_ts, ep.grace_end_ts)
            hits.append(
                Hit(
                    promise="override_respected",
                    ts=ep.end_ts,
                    key=f"override_respected:early_end:{ep.id}",
                    detail=(
                        f"Climate Advisor ended your manual {label} change {int(remaining + 0.5)} min early, "
                        "so nothing protects it any more."
                    ),
                    evidence=_ev(target=ep.target, remaining_min=remaining, end_reason=ep.end_reason),
                )
            )
    return hits


# ── P5 / P5b blind fan ─────────────────────────────────────────────────────────
def check_p5_no_blind_fan_start(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """No fan start while CA's indoor reading was unreadable at send time (``Command.indoor_f is None``).

    Adapters MUST stamp ``Command.indoor_f`` from the indoor value CA had when it sent.
    """
    hits: list[Hit] = []
    for c in inp.commands:
        starts = (c.target == "fan" and c.fan_on is True) or (
            c.target == "thermostat" and c.service == "set_fan_mode" and str(c.mode).lower() == "on"
        )
        if c.origin == "ca" and starts and c.indoor_f is None:
            hits.append(
                Hit(
                    promise="no_blind_fan_start",
                    ts=c.ts,
                    key=f"no_blind_fan_start:{c.ts.isoformat()}",
                    detail="The fan was started while the indoor temperature could not be read.",
                    evidence=_ev(target=c.target, service=c.service),
                )
            )
    return hits


def check_p5b_blind_fan_continue(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """A fan already running does not keep running blind for more than 35 minutes."""
    hits: list[Hit] = []
    rs = inp.readings

    def flush(run: list[Reading], ongoing: bool) -> None:
        if not run:
            return
        end = inp.now if ongoing else run[-1].ts
        minutes = _mins(run[0].ts, end)
        if minutes > NUMBERS.P5_BLIND_CONTINUE_MIN:
            hits.append(
                Hit(
                    promise="blind_fan_continue",
                    ts=run[0].ts,
                    # Stable while the run stays open, even after its first reading ages out of the window.
                    key=RunKey("blind_fan_continue", run[0].ts, end),
                    detail=f"The fan ran {minutes:.0f} minutes while the indoor temperature could not be read.",
                    evidence=_ev(minutes=minutes),
                )
            )

    run: list[Reading] = []
    for r in rs:
        if r.fan_on is True and r.indoor_f is None:
            run.append(r)
        else:
            flush(run, False)
            run = []
    flush(run, bool(rs) and rs[-1].fan_on is True and rs[-1].indoor_f is None)
    return hits


# ── P6 mode_flip_rate ──────────────────────────────────────────────────────────
def check_p6_mode_flip_rate(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """At most 2 heat/cool flips per hour, counted from the modes the thermostat reported."""
    hits: list[Hit] = []
    occ_ts = sorted(o.ts for o in occupant_actions(inp) if o.target == "thermostat")
    excuse = timedelta(minutes=NUMBERS.P6_OCCUPANT_EXCUSE_MIN)
    flips: list[datetime] = []
    family: str | None = None
    for r in inp.readings:
        m = (r.hvac_mode or "").lower()
        if m not in ("heat", "cool"):
            continue
        if family is not None and m != family and not _any_in_span(occ_ts, r.ts, excuse, inclusive=False):
            flips.append(r.ts)
        family = m
    n = NUMBERS.P6_MAX_FLIPS
    for i in range(n, len(flips)):
        if flips[i] - flips[i - n] <= timedelta(minutes=NUMBERS.P6_WINDOW_MIN):
            hits.append(
                Hit(
                    promise="mode_flip_rate",
                    ts=flips[i],
                    key=f"mode_flip_rate:{flips[i].isoformat()}",
                    detail=f"The thermostat flipped between heat and cool {n + 1} times in the past hour.",
                    evidence=_ev(flips=n + 1, window_min=NUMBERS.P6_WINDOW_MIN),
                )
            )
    return hits


# ── P7 comfort_kept ────────────────────────────────────────────────────────────
def _state_timeline(
    marks: tuple[Mark, ...], on: frozenset[str], off: frozenset[str]
) -> tuple[list[datetime], list[bool]]:
    """Stamps and the on/off state each mark leaves behind (marks are time-ordered)."""
    stamps: list[datetime] = []
    states: list[bool] = []
    state = False
    for m in marks:
        if m.kind in on:
            state = True
        elif m.kind in off:
            state = False
        else:
            continue
        stamps.append(m.ts)
        states.append(state)
    return stamps, states


def _state_at(timeline: tuple[list[datetime], list[bool]], ts: datetime) -> bool:
    stamps, states = timeline
    i = bisect_right(stamps, ts)
    return states[i - 1] if i else False


def check_p7_comfort_kept(inp: PromiseInput, cfg: PromiseConfig) -> list[Hit]:
    """While Climate Advisor is in control, the house is not outside the range for > 45 min."""
    hits: list[Hit] = []
    rs = inp.readings
    marks = tuple(sorted(inp.marks, key=lambda m: m.ts))
    occ_ts = sorted(o.ts for o in occupant_actions(inp))
    widen = NUMBERS.P7_AGGRESSIVE_WIDEN_F if cfg.aggressive_savings else 0.0
    excl = timedelta(minutes=NUMBERS.P7_EXCLUSION_MIN)
    away_tl = _state_timeline(marks, frozenset({"occupancy_setback"}), frozenset({"occupancy_comfort_restored"}))
    vent_tl = _state_timeline(marks, frozenset({"nat_vent_activated"}), NAT_VENT_END_KINDS)
    off_cmd_ts = sorted(c.ts for c in inp.commands if c.origin == "ca" and c.automation_enabled is False)
    restart_ts = sorted(m.ts for m in marks if m.kind in ("system_restarted", "startup_coalesced"))
    window = timedelta(minutes=NUMBERS.P7_OVERSHOOT_WINDOW_MIN)
    # Newest earlier reading that was heating / cooling, per reading index (the overshoot excuse).
    last_eq: dict[str, list[datetime | None]] = {"heating": [], "cooling": []}
    seen_eq: dict[str, datetime | None] = {"heating": None, "cooling": None}
    for r in rs:
        for k in last_eq:
            last_eq[k].append(seen_eq[k])
        e = equipment(r)
        if e in seen_eq:
            seen_eq[e] = r.ts

    def outside(r: Reading) -> tuple[str, float] | None:
        if r.indoor_f is None:
            return None
        lo = cfg.floor_lo(r.ts) - NUMBERS.P7_SLACK_F - widen
        hi = cfg.ceiling_hi(r.ts) + NUMBERS.P7_SLACK_F + widen
        if r.indoor_f < lo:
            return "below", lo - r.indoor_f
        if r.indoor_f > hi:
            return "above", r.indoor_f - hi
        return None

    def in_control(idx: int, r: Reading, side: str, dist: float) -> bool:
        if r.hvac_mode is None or r.contacts_open is True or r.fan_on is True:
            return False
        if _state_at(away_tl, r.ts) or _state_at(vent_tl, r.ts):
            return False
        if any(e.start_ts <= r.ts and (e.end_ts is None or e.end_ts >= r.ts) for e in inp.episodes):
            return False
        if (
            _any_in_span(occ_ts, r.ts, excl, inclusive=True)
            or _any_in_span(off_cmd_ts, r.ts, excl, inclusive=True)
            or _any_in_span(restart_ts, r.ts, excl, inclusive=True)
        ):
            return False
        working = "heating" if side == "below" else "cooling"
        if equipment(r) == working:
            return False
        opposite = "cooling" if side == "below" else "heating"
        recent = last_eq[opposite][idx]
        return not (
            dist <= NUMBERS.P7_OVERSHOOT_EXPLAINED_F and recent is not None and timedelta(0) <= r.ts - recent <= window
        )

    # Collect each open run, then emit one hit per run that outlasted the allowance.
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    gap = timedelta(minutes=NUMBERS.P7_MAX_GAP_MIN)
    for idx, r in enumerate(rs):
        o = outside(r)
        if o is None or not in_control(idx, r, o[0], o[1]):
            cur = None
            continue
        side, dist = o
        if cur is None or side != cur["side"] or r.ts - cur["last"] > gap:
            cur = {"start": r.ts, "side": side, "worst": 0.0, "cross": None, "last": r.ts}
            runs.append(cur)
        cur["last"] = r.ts
        cur["worst"] = max(cur["worst"], dist)
        if cur["cross"] is None and _mins(cur["start"], r.ts) > NUMBERS.P7_MAX_MIN:
            cur["cross"] = r.ts
            cur["worst_at_cross"] = cur["worst"]
    for run in runs:
        if run["cross"] is None:
            continue
        minutes = _mins(run["start"], run["cross"])
        worst = run["worst_at_cross"]
        hits.append(
            Hit(
                promise="comfort_kept",
                ts=run["cross"],
                # Stable while the run stays open, even after its first reading ages out of the window.
                key=RunKey(f"comfort_kept:{run['side']}", run["start"], run["last"]),
                detail=(
                    f"The house stayed {format_temp_delta(worst, cfg.temp_unit, 1)} outside your comfort range "
                    f"for {minutes:.0f} minutes while Climate Advisor was in control."
                ),
                evidence=_ev(minutes=minutes, worst_f=worst, side=run["side"]),
            )
        )
    return hits


@dataclass(frozen=True, slots=True)
class PromiseSpec:
    number: int
    key: str
    label: str  # "1".."7", "5b"
    check: Callable[[PromiseInput, PromiseConfig], list[Hit]]
    test_severity: str
    home_level: str | None  # LEVEL_LOG | LEVEL_ALERT | None (not checked at home)


# Flat tuple, not a plugin registry. Promise 8 (surfaces_agree) is test-only and lives with the
# test-side probe; it is intentionally not listed here. Changing this tuple needs human sign-off.
PROMISES: tuple[PromiseSpec, ...] = (
    PromiseSpec(1, "settling_flip", "1", check_p1_settling_flip, SEVERITY_HARD, LEVEL_LOG),
    PromiseSpec(2, "ac_whf_mutex", "2", check_p2_ac_whf_mutex, SEVERITY_HARD, LEVEL_ALERT),
    PromiseSpec(3, "observe_only_silence", "3", check_p3_observe_only_silence, SEVERITY_HARD, LEVEL_LOG),
    PromiseSpec(4, "override_respected", "4", check_p4_override_respected, SEVERITY_HARD, LEVEL_LOG),
    PromiseSpec(5, "no_blind_fan_start", "5", check_p5_no_blind_fan_start, SEVERITY_HARD, LEVEL_LOG),
    PromiseSpec(5, "blind_fan_continue", "5b", check_p5b_blind_fan_continue, SEVERITY_WARNING, LEVEL_LOG),
    PromiseSpec(6, "mode_flip_rate", "6", check_p6_mode_flip_rate, SEVERITY_WARNING, LEVEL_LOG),
    PromiseSpec(7, "comfort_kept", "7", check_p7_comfort_kept, SEVERITY_WARNING, LEVEL_LOG),
)


def run_promise_checks(inp: PromiseInput, cfg: PromiseConfig, *, scope: str) -> list[Hit]:
    """Run every promise that applies to ``scope`` and return the hits (time-ordered).

    ``scope='home'`` runs promises with a home level; ``'test'`` runs all. A check that raises is
    logged once at WARNING and skipped at home (graceful degradation) but re-raised in tests so a
    broken check can never hide a real failure. Pure and cheap: O(window), no I/O.
    """
    if scope not in (SCOPE_HOME, SCOPE_TEST):
        raise ValueError(f"unknown scope: {scope!r}")
    hits: list[Hit] = []
    for spec in PROMISES:
        if scope == SCOPE_HOME and spec.home_level is None:
            continue
        try:
            hits.extend(spec.check(inp, cfg))
        except Exception:
            if scope == SCOPE_TEST:
                raise
            _LOGGER.warning("Household promise check failed: promise=%s", spec.key, exc_info=True)
    hits.sort(key=lambda h: h.ts)
    return hits
