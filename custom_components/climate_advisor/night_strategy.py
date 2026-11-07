"""Night strategy for the bedroom (Issue #1230 R4): ADVISE only.

Occupant view: each evening the person gets ONE clear recommendation for the bedroom tonight ("open the
bedroom windows until about 1 AM, then close them; CA keeps the heat for the bedroom at 66"), with the
trade-off against the alternatives (cold bedroom, extra heating or AC minutes, more things to do), and only
options Climate Advisor has evidence for. Nothing here runs a fan or opens anything: R4 advises, CA never starts
the whole-house fan for a strategy (DP-7).

Pure and headless (no Home Assistant, no clock, no I/O). The physics is the v4 model (``predictor_v4.rollout``,
one 15-minute step at a time, replaying the duty the controller below chose); the controller is the part v4's
``predict`` cannot express for a night: the thermostat reads the hallway, the sleep-band outer loop (#1194)
nudges its setpoint from the bedroom reading, and HVAC is paused while windows or the whole-house fan are open.

Parts:

* ``plan_night`` - inputs -> ``NightStrategy`` (ranked offered options, hidden options with the reason in a debug
  field, recommendation, bedroom range / HVAC minutes / score / action times / confidence per option).
* the confidence gate - an option is offered only when every rate it USED in its own simulation is at medium
  confidence or better (``grade_rates``); ``NightValidation`` carries the replay result
  (``tools/night_strategy_replay.py``) that makes an option class "validated".
* ``apply_night_strategy`` - pure ``Timeline -> Timeline`` adding ``NIGHT_STRATEGY`` + ``WINDOWS_*`` / ``FAN_*``
  steps per the frozen R3 timeline contract.

Scope facts (claude.md, Issue #1197): only ``whole_house_fan`` and ``hvac_fan`` are supported fan modes; ``both``
is never planned for. Spec: docs/night-strategy-spec.md.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from . import predictor_v4 as p4
from .const import (
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    REMOTE_OFFSET_CAP_F,
    REMOTE_OFFSET_STEP_F,
    THERMAL_SWING_DEFAULT_F,
)
from .temperature import FAHRENHEIT, format_temp

NIGHT_STRATEGY_SCHEMA_VERSION = 1

# --- Tunables (documented in docs/night-strategy-spec.md; every number is a decision, not a fit) --------------------
STEP_MINUTES = p4.V4_STEP_MINUTES
DT_H = STEP_MINUTES / 60.0
# DP-2 (decided 11/6): 1 F-hour outside the band beyond the swing = 1 h of HVAC. A constant.
HVAC_HOURS_PER_DISCOMFORT_FH = 1.0
TIE_EPS = 0.05  # scores closer than this tie; a tie goes to the option with fewer occupant actions
# an adaptive fan keeps running only while outdoor is at least this much cooler than the bedroom
FREE_COOL_MIN_DELTA_F = 2.0
FAN_UNTIL_ABOVE_FLOOR_F = 1.0  # "fan until the bedroom reaches floor + 1"
FAN_THEN_AC_BELOW_CEILING_F = 1.0  # "fan, then AC": fan until the bedroom is this far under the ceiling
DEFAULT_WINDOW_HOURS = 2.0  # "windows N h" and "fan N h"
YOU_STEP_LEAD_MIN = 30  # DP-4: a YOU step is shown this long ahead
NATVENT_COVER_TOLERANCE_MIN = 20  # a CA nat-vent session "covers" a fan span when it starts/stops within this of it

# Confidence gate. "medium" = the minimum to be offered. Thresholds read from the v4 fit (``fit["frozen"]``,
# ``confidence``) and from the history counts in ``Evidence``.
MIN_HEAT_HOURS = 4.0  # v4 itself calls a mode "seen" at 2 h; offering a plan on it needs 4 h
MIN_COOL_HOURS = 4.0
MIN_WINDOW_OPEN_HOURS = 24.0
MIN_FAN_HOURS = 12.0
MIN_TARGET_SENSOR_HOURS = 72.0  # a bedroom (non-primary) node needs 3 days of its own readings
MIN_FAN_EFFECT_PER_H = 0.10  # fitted a_f below this means the fan's effect is not resolved
WINDOW_EFFECT_MIN_RATIO = 0.5  # fitted a_w must be at least this share of the envelope leak a_o ...
WINDOW_EFFECT_MIN_PER_H = 0.01  # ... and at least this (1/h): otherwise "windows only" is indistinguishable from closed
HIGH_FACTOR = 3.0  # "high" needs this multiple of every history threshold
VALIDATION_MIN_NIGHTS = 3  # replay: nights of an option class needed to call the class validated
VALIDATION_MAX_MAE_F = 1.5  # ... with a mean absolute error at or under this (about the thermostat swing) ...
VALIDATION_HIGH_NIGHTS = 10
VALIDATION_HIGH_MAE_F = 1.0
# ... and beating "nothing changes" (``flat_mae_f``) when that was measured.

GRADE_ORDER = {"low": 0, "medium": 1, "high": 2}
OFFERED_GRADES = ("medium", "high")

# Option keys
OPT_CLOSED = "closed"
OPT_WINDOWS_2H = "windows_2h"
OPT_WINDOWS_ALL = "windows_all_night"
OPT_FAN_2H = "fan_2h"
OPT_FAN_UNTIL_TARGET = "fan_until_target"
OPT_FAN_THEN_AC = "fan_then_ac"
OPT_BLOWER_2H = "blower_2h"

# Validation classes: what the replay can measure from recorded nights.
CLS_CLOSED = "closed"
CLS_WINDOWS = "windows"  # windows open, no fan
CLS_FAN = "fan"  # whole-house fan (windows open)
CLS_BLOWER = "blower"  # HVAC blower, windows closed
VALIDATION_CLASSES = (CLS_CLOSED, CLS_WINDOWS, CLS_FAN, CLS_BLOWER)

# Step reasons / kinds (see the sentence table at the bottom)
REASON_PLAN = "night_strategy.plan"
REASON_WINDOWS_OPEN = "night_strategy.windows_open"
REASON_WINDOWS_CLOSE = "night_strategy.windows_close"
REASON_FAN_ON = "night_strategy.fan_on"
REASON_FAN_OFF = "night_strategy.fan_off"
STEP_SOURCE = "night_strategy.apply_night_strategy"


# ---------------------------------------------------------------------------------------------- data types
@dataclass(frozen=True)
class Evidence:
    """Hours of history behind each rate (15-minute steps x 0.25), from ``evidence_from_samples``."""

    window_open_hours: float = 0.0
    fan_hours: float = 0.0
    heat_hours: float = 0.0
    cool_hours: float = 0.0
    sensor_hours: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassValidation:
    """Replay of recorded nights of one option class: v4 fitted BEFORE each night vs what the sensor recorded."""

    n_nights: int
    mae_f: float
    flat_mae_f: float | None = None
    bias_f: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class NightValidation:
    """Replay results per option class (``tools/night_strategy_replay.py``); ``provisional`` caps grades at medium."""

    classes: Mapping[str, ClassValidation] = field(default_factory=dict)
    provisional: bool = False
    generated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NIGHT_STRATEGY_SCHEMA_VERSION,
            "provisional": self.provisional,
            "generated": self.generated,
            "classes": {k: v.to_dict() for k, v in self.classes.items()},
        }

    @staticmethod
    def from_dict(d: Any) -> NightValidation | None:
        if not isinstance(d, Mapping) or d.get("schema_version") != NIGHT_STRATEGY_SCHEMA_VERSION:
            return None
        classes: dict[str, ClassValidation] = {}
        for k, v in (d.get("classes") or {}).items():
            if not isinstance(v, Mapping) or k not in VALIDATION_CLASSES:
                continue
            try:
                n = int(v["n_nights"])
                mae = float(v["mae_f"])
            except KeyError, TypeError, ValueError:
                continue
            if not math.isfinite(mae):
                continue

            def _opt(x: Any) -> float | None:
                return float(x) if isinstance(x, (int, float)) and math.isfinite(x) else None

            classes[k] = ClassValidation(n, mae, _opt(v.get("flat_mae_f")), _opt(v.get("bias_f")))
        return NightValidation(classes, bool(d.get("provisional", False)), str(d.get("generated", "")))


@dataclass(frozen=True)
class NightInputs:
    """Everything one evening's strategy needs. All temperatures internal degrees F, times aware."""

    model: p4.V4Model
    start: datetime  # bedtime (or now, when later): the state in ``readings`` is the state at this moment
    end: datetime  # wake time
    tz: tzinfo
    fan_mode: str  # whole_house_fan | hvac_fan (anything else: no fan options)
    outdoor: Sequence[tuple[datetime, float]]  # tonight's CORRECTED outdoor forecast (any grid, interpolated)
    readings: Mapping[str, float]  # sensor id -> reading at ``start`` ("_ca" included when it is a model sensor)
    floor_f: float  # sleep band at the governing (bedroom) sensor
    ceiling_f: float
    target_sensor: str | None = None  # the bedroom sensor id; None -> the model's primary
    mass_f: float | None = None
    swing_heat_f: float = THERMAL_SWING_DEFAULT_F
    swing_cool_f: float = THERMAL_SWING_DEFAULT_F
    can_heat: bool = True
    can_cool: bool = True
    evidence: Evidence | None = None
    validation: NightValidation | None = None
    window_hours: float = DEFAULT_WINDOW_HOURS
    offset_loop: bool = True  # the #1194 outer loop is active when the target is not the thermostat's sensor
    unit: str = FAHRENHEIT


@dataclass(frozen=True)
class OptionAction:
    """One thing to do, at a time. ``kind`` is a step kind value; ``actor`` is "you" in R4."""

    ts: datetime
    kind: str  # windows_open | windows_close | fan_on | fan_off
    actor: str = "you"
    predicted: bool = False  # True when the time is the prediction of a condition ("until the bedroom reaches 67")
    through_night: bool = False  # a close that is not scheduled because the span runs to wake time


@dataclass(frozen=True)
class StrategyOption:
    key: str
    label: str
    cls: str  # validation class
    actions: tuple[OptionAction, ...]
    n_actions: int  # occupant actions (0 when the option would not run tonight)
    bed_min_f: float
    bed_max_f: float
    bed_wake_f: float
    discomfort_fh: float  # F-hours outside the sleep band beyond the swing
    heat_min: int
    cool_min: int
    score: float
    grade: str  # low | medium | high (the offered ones are medium or high)
    reasons: tuple[str, ...]  # why the grade is not higher (debug / hidden reasons)
    runs: bool  # False: the option would not do anything tonight (fan never needed)
    note: str = ""
    path: tuple[tuple[datetime, float], ...] = ()  # target-sensor path under the option
    hall_path: tuple[tuple[datetime, float], ...] = ()
    used: Mapping[str, float] = field(default_factory=dict)  # rate -> steps used (debug)

    @property
    def hvac_minutes(self) -> int:
        return self.heat_min + self.cool_min

    def to_dict(self, *, include_path: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "class": self.cls,
            "bedroom_low_f": round(self.bed_min_f, 1),
            "bedroom_high_f": round(self.bed_max_f, 1),
            "bedroom_wake_f": round(self.bed_wake_f, 1),
            "discomfort_fh": round(self.discomfort_fh, 2),
            "heat_min": self.heat_min,
            "cool_min": self.cool_min,
            "hvac_minutes": self.hvac_minutes,
            "score": round(self.score, 2),
            "confidence": self.grade,
            "n_actions": self.n_actions,
            "runs": self.runs,
            "note": self.note,
            "actions": [
                {
                    "ts": a.ts.isoformat(),
                    "kind": a.kind,
                    "actor": a.actor,
                    "predicted": a.predicted,
                    "through_night": a.through_night,
                }
                for a in self.actions
            ],
        }
        if include_path:
            d["path"] = [[t.isoformat(), round(v, 2)] for t, v in self.path]
        return d


@dataclass(frozen=True)
class NightStrategy:
    """The answer for one night. ``options`` are the offered ones, best first; ``hidden`` is debug only."""

    start: datetime
    end: datetime
    floor_f: float
    ceiling_f: float
    target_sensor: str
    fan_mode: str
    options: tuple[StrategyOption, ...]
    recommended: str | None
    show_numbers: bool  # False: the bedroom model is not trustworthy yet, show the plan without predicted figures
    hidden: tuple[Mapping[str, Any], ...] = ()
    debug: Mapping[str, Any] = field(default_factory=dict)

    @property
    def best(self) -> StrategyOption | None:
        for o in self.options:
            if o.key == self.recommended:
                return o
        return None

    def option(self, key: str) -> StrategyOption | None:
        for o in self.options:
            if o.key == key:
                return o
        return None

    def to_dict(self, *, include_debug: bool = False, include_path: bool = True) -> dict[str, Any]:
        """JSON-safe. The occupant-facing payload NEVER carries hidden options; ``include_debug`` adds them."""
        d: dict[str, Any] = {
            "schema_version": NIGHT_STRATEGY_SCHEMA_VERSION,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "floor_f": self.floor_f,
            "ceiling_f": self.ceiling_f,
            "target_sensor": self.target_sensor,
            "fan_mode": self.fan_mode,
            "recommended": self.recommended,
            "show_numbers": self.show_numbers,
            "options": [o.to_dict(include_path=include_path) for o in self.options],
        }
        if include_debug:
            d["debug"] = {"hidden": [dict(h) for h in self.hidden], **dict(self.debug)}
        return d


# ---------------------------------------------------------------------------------------------- option catalogue
@dataclass(frozen=True)
class _Spec:
    key: str
    cls: str
    windows: bool  # windows open while the option "runs"
    fan: bool  # fan running while the option "runs"
    mode: str  # none | fixed | all_night | until_floor | until_ceiling
    n_actions: int
    needs_cool: bool = False


_SPECS: dict[str, _Spec] = {
    OPT_CLOSED: _Spec(OPT_CLOSED, CLS_CLOSED, False, False, "none", 0),
    OPT_WINDOWS_2H: _Spec(OPT_WINDOWS_2H, CLS_WINDOWS, True, False, "fixed", 2),
    OPT_WINDOWS_ALL: _Spec(OPT_WINDOWS_ALL, CLS_WINDOWS, True, False, "all_night", 1),
    OPT_FAN_2H: _Spec(OPT_FAN_2H, CLS_FAN, True, True, "fixed", 2),
    OPT_FAN_UNTIL_TARGET: _Spec(OPT_FAN_UNTIL_TARGET, CLS_FAN, True, True, "until_floor", 2),
    OPT_FAN_THEN_AC: _Spec(OPT_FAN_THEN_AC, CLS_FAN, True, True, "until_ceiling", 2, needs_cool=True),
    OPT_BLOWER_2H: _Spec(OPT_BLOWER_2H, CLS_BLOWER, False, True, "fixed", 2),
}

#: DP-3 (decided 11/6): options per fan mode. "windows only" is offered only once its effect passes the gate.
OPTIONS_BY_FAN_MODE: dict[str, tuple[str, ...]] = {
    FAN_MODE_WHOLE_HOUSE: (
        OPT_CLOSED,
        OPT_WINDOWS_2H,
        OPT_WINDOWS_ALL,
        OPT_FAN_2H,
        OPT_FAN_UNTIL_TARGET,
        OPT_FAN_THEN_AC,
    ),
    FAN_MODE_HVAC: (OPT_CLOSED, OPT_WINDOWS_2H, OPT_WINDOWS_ALL, OPT_BLOWER_2H),
}
#: No fan configured: windows are the only lever. ``both`` is unsupported (Issue #1175) and gets only "closed".
_NO_FAN_OPTIONS = (OPT_CLOSED, OPT_WINDOWS_2H, OPT_WINDOWS_ALL)


def options_for_fan_mode(fan_mode: str) -> tuple[str, ...]:
    if fan_mode in OPTIONS_BY_FAN_MODE:
        return OPTIONS_BY_FAN_MODE[fan_mode]
    if fan_mode in ("disabled", "", "none"):
        return _NO_FAN_OPTIONS
    return (OPT_CLOSED,)  # "both" or anything unknown: unsupported, never planned for


# ---------------------------------------------------------------------------------------------- simulation helpers
def _interp(points: Sequence[tuple[float, float]], t: float) -> float:
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, v0 = points[lo]
    t1, v1 = points[hi]
    return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def _local_hour(ts: datetime, tz: tzinfo) -> float:
    loc = ts.astimezone(tz)
    return loc.hour + loc.minute / 60.0 + loc.second / 3600.0


def _grid(start: datetime, end: datetime) -> list[datetime]:
    """Step-END timestamps from start to end in absolute time (DST-safe), at least one step."""
    n = max(1, math.ceil((end.timestamp() - start.timestamp()) / (STEP_MINUTES * 60) - 1e-9))
    return p4.grid_steps(start, n)


def _frozen_set(model: p4.V4Model) -> set[str]:
    return {str(x) for x in (model.fit.get("frozen") or [])}


def _node_index(model: p4.V4Model, sensor: str) -> int | None:
    extra = [s for s in model.sensors if s != model.primary]
    return extra.index(sensor) if sensor in extra else None


@dataclass
class _Sim:
    ts: list[datetime]
    hall: list[float]
    target: list[float]
    uh: list[float]
    uc: list[float]
    win: list[float]
    fan: list[float]
    paused_steps: int


def _simulate(
    inp: NightInputs,
    spec: _Spec,
    target: str,
    *,
    flags: Sequence[tuple[float, float]] | None = None,
    setpoints: Sequence[tuple[float | None, float | None]] | None = None,
) -> _Sim | None:
    """Run one option through the night. Physics AND thermostat: ``predictor_v4.rollout`` one step at a time with the
    predictor's own R1b emulator (hysteretic stop, calibrated start offsets, learned minimum run), so a night figure
    and the v4 prediction of the same hours cannot disagree about when the heat or AC runs. What stays here is the
    sleep-band outer loop on the target (#1194) and the HVAC pause while windows or the whole-house fan are open.

    ``flags`` / ``setpoints`` (replay only) override the option schedule and the band-derived setpoints per step with
    what was RECORDED, so the replay tool exercises the same controller and physics."""
    model = inp.model
    prim = model.primary
    h = inp.readings.get(prim)
    if h is None or not math.isfinite(h):
        return None
    house = [model.house[k] for k in p4.HOUSE_KEYS]
    node_ids: list[str] = []
    node_params: list[list[float]] = []
    b: list[float] = []
    for sid in model.sensors:
        if sid == prim or sid not in model.nodes:
            continue
        r = inp.readings.get(sid)
        if r is None or not math.isfinite(r):
            continue
        node_ids.append(sid)
        node_params.append([model.nodes[sid][k] for k in p4.NODE_KEYS])
        b.append(float(r))
    tgt_j = node_ids.index(target) if target in node_ids else None
    m = inp.mass_f if (inp.mass_f is not None and math.isfinite(inp.mass_f)) else float(h)
    h = float(h)

    th = model.thermostat

    out_pts = sorted((t.timestamp(), float(v)) for t, v in inp.outdoor)
    if not out_pts:
        return None
    ts_list = _grid(inp.start, inp.end)
    sim = _Sim([], [], [], [], [], [], [], 0)

    use_loop = inp.offset_loop and tgt_j is not None
    off_h = 0.0
    off_c = 0.0
    run_mode = 0  # the emulated thermostat's run in progress: +1 heating, -1 cooling, 0 none
    run_min = 0.0  # minutes that run has lasted (the predictor's minimum-run rule counts from it)
    elapsed_h = 0.0
    adaptive_on = spec.mode in ("until_floor", "until_ceiling")
    done = False
    for ts in ts_list:
        o = _interp(out_pts, ts.timestamp())
        tgt_now = b[tgt_j] if tgt_j is not None else h
        # --- window / fan flags for this step
        run = False
        if spec.mode == "all_night":
            run = True
        elif spec.mode == "fixed":
            run = elapsed_h < inp.window_hours - 1e-9
        elif adaptive_on and not done:
            if spec.mode == "until_floor":
                run = tgt_now > inp.floor_f + FAN_UNTIL_ABOVE_FLOOR_F and o < tgt_now - FREE_COOL_MIN_DELTA_F
            else:
                run = tgt_now > inp.ceiling_f - FAN_THEN_AC_BELOW_CEILING_F and o < tgt_now - FREE_COOL_MIN_DELTA_F
            if not run:
                done = True
        win = 1.0 if (run and spec.windows) else 0.0
        fan = 1.0 if (run and spec.fan) else 0.0
        if flags is not None:
            win, fan = flags[len(sim.ts)] if len(sim.ts) < len(flags) else (0.0, 0.0)
            run = win > 0 or fan > 0
        # HVAC is paused while a window is open or the whole-house fan runs (owner rules 2-3); the blower is HVAC
        paused = win > 0 or (fan > 0 and spec.cls == CLS_FAN)
        # --- sleep-band outer loop (#1194): only between runs, at most one step per step
        if use_loop and run_mode == 0 and not paused and setpoints is None:
            gap_h = inp.floor_f - tgt_now
            gap_c = tgt_now - inp.ceiling_f
            if gap_h > inp.swing_heat_f:
                off_h = min(REMOTE_OFFSET_CAP_F, off_h + REMOTE_OFFSET_STEP_F)
            elif gap_h < -inp.swing_heat_f:
                off_h = max(0.0, off_h - REMOTE_OFFSET_STEP_F)
            if gap_c > inp.swing_cool_f:
                off_c = min(REMOTE_OFFSET_CAP_F, off_c + REMOTE_OFFSET_STEP_F)
            elif gap_c < -inp.swing_cool_f:
                off_c = max(0.0, off_c - REMOTE_OFFSET_STEP_F)
        sp_h = min(inp.floor_f + off_h, inp.ceiling_f - 1.0) if inp.can_heat else None
        sp_c = max(inp.ceiling_f - off_c, inp.floor_f + 1.0) if inp.can_cool else None
        if setpoints is not None:
            sp_h, sp_c = setpoints[len(sim.ts)] if len(sim.ts) < len(setpoints) else (None, None)
        # --- thermostat: the PREDICTOR's own emulator (R1b: hysteretic stop at setpoint + off offset, calibrated
        # start offsets, learned minimum run, reading quantisation), one step at a time so the outer loop and the HVAC
        # pause can act between steps. The run in progress is carried in and out through ``init_run``.
        if paused:
            hsp: list[float | None] = [None]  # no setpoint: a run in progress stops (owner rules 2-3)
            csp: list[float | None] = [None]
            init_run = None
            sim.paused_steps += 1
        else:
            hsp, csp = [sp_h], [sp_c]
            init_run = (run_mode, run_min) if run_mode else None
        roll = p4.rollout(
            house,
            node_params,
            h0=h,
            m0=m,
            b0=b,
            out=[o],
            win=[win],
            fan=[fan],
            hours=[_local_hour(ts, inp.tz)],
            thermostat=th,
            heat_sp=hsp,
            cool_sp=csp,
            init_run=init_run,
        )
        uh = roll.heat_duty[0]
        uc = roll.cool_duty[0]
        # a full-duty step leaves the run going; a partial step is the run landing on its stop point
        if uh >= 1.0 - 1e-9:
            run_min = (run_min if run_mode == 1 else 0.0) + STEP_MINUTES
            run_mode = 1
        elif uc >= 1.0 - 1e-9:
            run_min = (run_min if run_mode == -1 else 0.0) + STEP_MINUTES
            run_mode = -1
        else:
            run_mode, run_min = 0, 0.0
        h = roll.primary[0]
        m = roll.mass_end
        b = [roll.nodes[j][0] for j in range(len(node_ids))]
        sim.ts.append(ts)
        sim.hall.append(h)
        sim.target.append(b[tgt_j] if tgt_j is not None else h)
        sim.uh.append(uh)
        sim.uc.append(uc)
        sim.win.append(win)
        sim.fan.append(fan)
        if run:
            elapsed_h += DT_H
    return sim


def _spans(flags: Sequence[float]) -> list[tuple[int, int]]:
    """Index spans [i, j) of consecutive steps with a flag set."""
    res: list[tuple[int, int]] = []
    i = 0
    n = len(flags)
    while i < n:
        if flags[i] > 0:
            j = i
            while j < n and flags[j] > 0:
                j += 1
            res.append((i, j))
            i = j
        else:
            i += 1
    return res


def _actions_from_sim(sim: _Sim, start: datetime, spec: _Spec) -> tuple[OptionAction, ...]:
    acts: list[OptionAction] = []
    n = len(sim.ts)
    adaptive = spec.mode in ("until_floor", "until_ceiling")

    def begin(i: int) -> datetime:
        return start if i == 0 else sim.ts[i - 1]

    for kind_on, kind_off, flags in (
        ("windows_open", "windows_close", sim.win),
        ("fan_on", "fan_off", sim.fan),
    ):
        # a fan option's windows and fan share the same span: the windows steps carry it, the fan steps are added
        # separately so the occupant gets both instructions
        for i, j in _spans(flags):
            acts.append(OptionAction(begin(i), kind_on))
            if j >= n:
                acts.append(OptionAction(sim.ts[-1], kind_off, through_night=True))
            else:
                acts.append(OptionAction(sim.ts[j - 1], kind_off, predicted=adaptive))
    acts.sort(key=lambda a: (a.ts, 0 if a.kind.endswith("_open") or a.kind == "fan_on" else 1))
    # through-night closes are not instructions: drop them from the action list
    return tuple(a for a in acts if not a.through_night)


# ---------------------------------------------------------------------------------------------- evidence + gate
def evidence_from_samples(samples: Sequence[p4.V4Sample]) -> Evidence:
    """Hours of window-open, fan, heating, cooling and per-sensor readings in a v4 sample history."""
    w = f = hh = ch = 0.0
    sensor: dict[str, float] = {}
    for s in samples:
        if s.windows_open:
            w += DT_H
        if s.fan_running:
            f += DT_H
        uh = s.heat_duty if s.heat_duty is not None else (1.0 if s.hvac_action == "heating" else 0.0)
        uc = s.cool_duty if s.cool_duty is not None else (1.0 if s.hvac_action == "cooling" else 0.0)
        hh += uh * DT_H
        ch += uc * DT_H
        for sid, r in s.readings.items():
            if r is not None:
                sensor[sid] = sensor.get(sid, 0.0) + DT_H
    return Evidence(round(w, 2), round(f, 2), round(hh, 2), round(ch, 2), {k: round(v, 2) for k, v in sensor.items()})


def validation_status(validation: NightValidation | None, cls: str) -> tuple[str, str]:
    """(grade, reason) of an option class from the replay: "high" | "medium" | "low" | "unknown"."""
    if validation is None or cls not in validation.classes:
        return "unknown", f"no recorded {cls} nights replayed yet"
    v = validation.classes[cls]
    if v.n_nights < VALIDATION_MIN_NIGHTS:
        return "low", f"only {v.n_nights} recorded {cls} night(s) replayed (need {VALIDATION_MIN_NIGHTS})"
    if v.mae_f > VALIDATION_MAX_MAE_F:
        return "low", f"{cls} nights replay with {v.mae_f:.1f} F mean error (limit {VALIDATION_MAX_MAE_F:.1f})"
    if v.flat_mae_f is not None and v.mae_f >= v.flat_mae_f:
        return "low", f"{cls} nights replay no better than 'nothing changes' ({v.mae_f:.1f} vs {v.flat_mae_f:.1f} F)"
    if v.n_nights >= VALIDATION_HIGH_NIGHTS and v.mae_f <= VALIDATION_HIGH_MAE_F and not validation.provisional:
        return "high", ""
    return "medium", ""


def grade_rates(
    inp: NightInputs, spec: _Spec, sim: _Sim, target: str, *, target_fallback: bool
) -> tuple[str, list[str]]:
    """Confidence of one simulated option: the LOWEST grade over every rate the simulation actually used.

    An option that never ran the fan does not depend on ``a_f``; a night with no heating does not depend on ``q_h``.
    """
    model = inp.model
    ev = inp.evidence
    frozen = _frozen_set(model)
    reasons: list[str] = []
    grade = "high"

    def cap(g: str, why: str) -> None:
        nonlocal grade
        if GRADE_ORDER[g] < GRADE_ORDER[grade]:
            grade = g
        if g == "low":
            reasons.append(why)

    # 1. the model as a whole
    if model.confidence == "none":
        cap("low", f"v4 model unusable ({model.fit.get('reason') or 'confidence none'})")
    elif model.confidence != "ok":
        cap("low", f"v4 model confidence {model.confidence} ({model.fit.get('reason') or 'thin history'})")

    steps_heat = sum(1 for u in sim.uh if u > 0)
    steps_cool = sum(1 for u in sim.uc if u > 0)
    steps_fan = sum(1 for f in sim.fan if f > 0)
    steps_win_only = sum(1 for w, f in zip(sim.win, sim.fan, strict=True) if w > 0 and f <= 0)

    def hist(value: float | None, need: float, name: str) -> None:
        if ev is None:
            cap("low", f"no history counts for {name}")
        elif value is not None and value < need:
            cap("low", f"{name}: {value:.0f} h of history (need {need:.0f})")
        elif value is not None and value < need * HIGH_FACTOR:
            cap("medium", "")

    # 2. heating / cooling rates
    if steps_heat:
        if "h/q_h" in frozen:
            cap("low", "heating rate q_h not learned (heating never seen in the fit)")
        hist(ev.heat_hours if ev else None, MIN_HEAT_HOURS, "heating")
    if steps_cool:
        if "h/q_c" in frozen:
            cap("low", "AC rate q_c is assumed (cooling never seen in the fit)")
        hist(ev.cool_hours if ev else None, MIN_COOL_HOURS, "cooling")
    # 3. fan
    if steps_fan:
        if "h/a_f" in frozen:
            cap("low", "fan rate a_f not learned (fan never ran in the fit)")
        elif model.house.get("a_f", 0.0) < MIN_FAN_EFFECT_PER_H:
            cap("low", f"fan effect not resolved (a_f {model.house.get('a_f', 0.0):.3f}/h)")
        hist(ev.fan_hours if ev else None, MIN_FAN_HOURS, "fan running")
    # 4. windows (windows-only periods: the whole effect rests on a_w / b_w)
    if steps_win_only:
        a_w, a_o = model.house.get("a_w", 0.0), model.house.get("a_o", 0.0)
        if "h/a_w" in frozen:
            cap("low", "window rate a_w not learned (no window-open time in the fit)")
        elif a_w < max(WINDOW_EFFECT_MIN_PER_H, WINDOW_EFFECT_MIN_RATIO * a_o):
            cap("low", f"windows-only effect not resolved (a_w {a_w:.3f}/h vs envelope a_o {a_o:.3f}/h)")
        hist(ev.window_open_hours if ev else None, MIN_WINDOW_OPEN_HOURS, "windows open")
    # 5. the bedroom node
    if target != model.primary:
        idx = _node_index(model, target)
        if idx is None:
            cap("low", "bedroom sensor is not a node of the v4 model")
        else:
            hours = (ev.sensor_hours.get(target, 0.0)) if ev else 0.0
            if hours < MIN_TARGET_SENSOR_HOURS:
                cap("low", f"bedroom sensor: {hours:.0f} h of readings (need {MIN_TARGET_SENSOR_HOURS:.0f})")
            elif hours < MIN_TARGET_SENSOR_HOURS * HIGH_FACTOR:
                cap("medium", "")
            if f"n/{idx}/q_b" in frozen and (steps_heat or steps_cool):
                cap("low", "bedroom share of the HVAC gain not learned")
            if steps_win_only and f"n/{idx}/b_w" in frozen:
                cap("low", "bedroom window rate b_w not learned")
    elif target_fallback:
        cap("low", "bedroom sensor not available: figures are for the thermostat room")
    # 6. replayed recorded nights of this option class (closed is the baseline: only gated when a record exists)
    vg, vwhy = validation_status(inp.validation, spec.cls)
    if vg == "unknown":
        if spec.cls != CLS_CLOSED:
            cap("low", vwhy)
        else:
            cap("medium", "")
    elif vg == "low":
        cap("low", vwhy)
    else:
        cap(vg, "")
    if inp.validation is not None and inp.validation.provisional:
        cap("medium", "")
    return grade, reasons


# ---------------------------------------------------------------------------------------------- labels
def _t(value: float, unit: str) -> str:
    return format_temp(round(value * 2) / 2, unit)


def _label(key: str, inp: NightInputs, sim: _Sim | None) -> str:
    if key == OPT_CLOSED:
        return "Keep the windows closed; CA heats or cools the bedroom as needed"
    if key == OPT_WINDOWS_2H:
        return f"Open the bedroom windows for {inp.window_hours:g} h at bedtime, then close them"
    if key == OPT_WINDOWS_ALL:
        return "Leave the bedroom windows open all night"
    if key == OPT_FAN_2H:
        return f"Whole-house fan with the windows open for {inp.window_hours:g} h, then close up"
    if key == OPT_FAN_UNTIL_TARGET:
        reach = _t(inp.floor_f + FAN_UNTIL_ABOVE_FLOOR_F, inp.unit)
        return f"Whole-house fan until the bedroom reaches {reach}, then close up"
    if key == OPT_FAN_THEN_AC:
        return (
            f"Whole-house fan until the bedroom is under {_t(inp.ceiling_f - FAN_THEN_AC_BELOW_CEILING_F, inp.unit)}, "
            "then close up and let the AC finish"
        )
    if key == OPT_BLOWER_2H:
        return f"Run the HVAC fan for {inp.window_hours:g} h at bedtime"
    return key


# ---------------------------------------------------------------------------------------------- the planner
def _score(discomfort_fh: float, hvac_min: float) -> float:
    return discomfort_fh * HVAC_HOURS_PER_DISCOMFORT_FH + hvac_min / 60.0


def _evaluate(inp: NightInputs, key: str, target: str, target_fallback: bool) -> StrategyOption | None:
    spec = _SPECS[key]
    sim = _simulate(inp, spec, target)
    if sim is None:
        return None
    lo = inp.floor_f - inp.swing_heat_f
    hi = inp.ceiling_f + inp.swing_cool_f
    disc = sum(max(0.0, lo - v, v - hi) for v in sim.target) * DT_H
    heat_min = round(sum(sim.uh) * STEP_MINUTES)
    cool_min = round(sum(sim.uc) * STEP_MINUTES)
    runs = any(w > 0 or f > 0 for w, f in zip(sim.win, sim.fan, strict=True))
    acts = _actions_from_sim(sim, inp.start, spec) if runs else ()
    grade, reasons = grade_rates(inp, spec, sim, target, target_fallback=target_fallback)
    note = ""
    if spec.mode != "none" and not runs:
        note = "would not run tonight: the bedroom does not need it"
    elif spec.mode == "all_night" and acts:
        note = "windows stay open until you get up"
    used = {
        "heat_steps": sum(1 for u in sim.uh if u > 0),
        "cool_steps": sum(1 for u in sim.uc if u > 0),
        "fan_steps": sum(1 for f in sim.fan if f > 0),
        "window_steps": sum(1 for w in sim.win if w > 0),
        "hvac_paused_steps": sim.paused_steps,
    }
    return StrategyOption(
        key=key,
        label=_label(key, inp, sim),
        cls=spec.cls,
        actions=acts,
        n_actions=spec.n_actions if runs else 0,
        bed_min_f=min(sim.target),
        bed_max_f=max(sim.target),
        bed_wake_f=sim.target[-1],
        discomfort_fh=disc,
        heat_min=heat_min,
        cool_min=cool_min,
        score=_score(disc, heat_min + cool_min),
        grade=grade,
        reasons=tuple(reasons),
        runs=runs,
        note=note,
        path=tuple(zip(sim.ts, sim.target, strict=True)),
        hall_path=tuple(zip(sim.ts, sim.hall, strict=True)),
        used=used,
    )


def _order(key: str) -> int:
    """Catalogue position: on a full tie the gentler option (closed, then windows, then fan) comes first."""
    keys = list(_SPECS)
    return keys.index(key) if key in keys else len(keys)


def rank(options: Sequence[StrategyOption]) -> list[StrategyOption]:
    """Lowest score first; scores within ``TIE_EPS`` tie: fewer occupant actions win, then less HVAC."""
    if not options:
        return []
    best = min(o.score for o in options)
    tied = sorted(
        (o for o in options if o.score - best < TIE_EPS),
        key=lambda o: (o.n_actions, o.hvac_minutes, o.score, _order(o.key)),
    )
    rest = sorted(
        (o for o in options if o.score - best >= TIE_EPS), key=lambda o: (o.score, o.n_actions, _order(o.key))
    )
    return tied + rest


def plan_night(inp: NightInputs) -> NightStrategy:
    """Simulate every option for the fan mode, gate by evidence, rank, and recommend."""
    model = inp.model
    target = inp.target_sensor or model.primary
    target_fallback = False
    if target != model.primary and (inp.readings.get(target) is None or target not in model.nodes):
        target = model.primary
        target_fallback = True
    fan_supported = inp.fan_mode in OPTIONS_BY_FAN_MODE
    keys = options_for_fan_mode(inp.fan_mode)
    offered: list[StrategyOption] = []
    hidden: list[dict[str, Any]] = []
    for key in keys:
        spec = _SPECS[key]
        if spec.needs_cool and not inp.can_cool:
            hidden.append({"key": key, "label": _label(key, inp, None), "reasons": ["no cooling available to assist"]})
            continue
        opt = _evaluate(inp, key, target, target_fallback)
        if opt is None:
            hidden.append(
                {
                    "key": key,
                    "label": _label(key, inp, None),
                    "reasons": ["could not simulate: missing reading or forecast"],
                }
            )
            continue
        if key == OPT_CLOSED or opt.grade in OFFERED_GRADES:
            offered.append(opt)
        else:
            hidden.append(
                {
                    "key": key,
                    "label": opt.label,
                    "class": opt.cls,
                    "grade": opt.grade,
                    "reasons": list(opt.reasons),
                    "would_have_scored": round(opt.score, 2),
                    "bedroom_range_f": [round(opt.bed_min_f, 1), round(opt.bed_max_f, 1)],
                    "used": dict(opt.used),
                }
            )
    ranked = rank([o for o in offered])
    # an option that would not run tonight duplicates "closed"; it stays listed (with its note) but is never the pick
    pick_pool = [o for o in ranked if o.runs or o.key == OPT_CLOSED]
    recommended = pick_pool[0].key if pick_pool else (ranked[0].key if ranked else None)
    closed = next((o for o in ranked if o.key == OPT_CLOSED), None)
    show_numbers = bool(closed is not None and closed.grade in OFFERED_GRADES)
    debug = {
        "model_confidence": model.confidence,
        "frozen": sorted(_frozen_set(model)),
        "target_requested": inp.target_sensor,
        "target_used": target,
        "target_fallback": target_fallback,
        "fan_mode_supported": fan_supported,
        "evidence": dataclasses.asdict(inp.evidence) if inp.evidence else None,
        "validation": inp.validation.to_dict() if inp.validation else None,
        "steps": len(_grid(inp.start, inp.end)),
    }
    return NightStrategy(
        start=inp.start,
        end=inp.end,
        floor_f=inp.floor_f,
        ceiling_f=inp.ceiling_f,
        target_sensor=target,
        fan_mode=inp.fan_mode,
        options=tuple(ranked),
        recommended=recommended,
        show_numbers=show_numbers,
        hidden=tuple(hidden),
        debug=debug,
    )


# ---------------------------------------------------------------------------------------------- the payload
FIGURE_KEYS = (
    "bedroom_low_f",
    "bedroom_high_f",
    "bedroom_wake_f",
    "discomfort_fh",
    "heat_min",
    "cool_min",
    "hvac_minutes",
    "score",
)
#: Shown on the night card: the plan is built on v4's rates whichever model the Status chart shows (Settings).
SOURCE_MODEL = "v4"
SOURCE_NOTE = (
    "From v4. Tonight's bedroom plan is always built on v4's learned rates, "
    "whichever prediction model the Status chart shows."
)


def api_payload(
    strategy: NightStrategy,
    *,
    unit: str,
    convert: Any,
    include_debug: bool = False,
) -> dict[str, Any]:
    """The REST body of ``GET /api/climate_advisor/night_strategy``: ``NightStrategy.to_dict()`` with temperatures in
    the display unit (keys keep their ``_f`` names, like the timeline payload), ``unit`` added, the headline and the
    trade-off lines, and the source label (``source_model``/``source_note``: always v4).

    ``convert`` is the caller's ``display_temp(value_f, unit)``. When ``show_numbers`` is False (the bedroom model is
    below medium confidence; there is no accuracy gate) every predicted figure is ``null`` and the path is empty, so
    the guarantee does not depend on the page remembering to hide them; the plan itself (options, actions, labels) is
    unchanged."""
    gated = strategy
    d = gated.to_dict(include_debug=include_debug)
    scale = 5.0 / 9.0 if unit == "celsius" else 1.0
    d["unit"] = unit
    d["floor_f"] = convert(d["floor_f"], unit)
    d["ceiling_f"] = convert(d["ceiling_f"], unit)
    d["source_model"] = SOURCE_MODEL
    d["source_note"] = SOURCE_NOTE
    show = gated.show_numbers
    opts = []
    for o in d["options"]:
        o2 = dict(o)
        if show:
            for k in ("bedroom_low_f", "bedroom_high_f", "bedroom_wake_f"):
                o2[k] = convert(o2[k], unit)
            o2["discomfort_fh"] = round(o2["discomfort_fh"] * scale, 2)
            o2["path"] = [[ts, convert(v, unit)] for ts, v in o2.get("path", [])]
        else:
            for k in FIGURE_KEYS:
                o2[k] = None
            o2["path"] = []
        opts.append(o2)
    d["options"] = opts
    d["headline"] = headline(gated, unit)
    d["tradeoffs"] = tradeoffs(gated, unit) if show else []
    return d


# ---------------------------------------------------------------------------------------------- presentation
def tradeoffs(strategy: NightStrategy, unit: str = FAHRENHEIT) -> list[dict[str, Any]]:
    """One line per alternative against the recommendation: what you gain or lose. Facts plus a sentence."""
    best = strategy.best
    if best is None:
        return []
    rows: list[dict[str, Any]] = []
    for o in strategy.options:
        if o.key == best.key:
            continue
        d_score = o.score - best.score
        d_hvac = o.hvac_minutes - best.hvac_minutes
        d_disc = o.discomfort_fh - best.discomfort_fh
        parts: list[str] = []
        d_disc_u = d_disc * (5.0 / 9.0 if unit == "celsius" else 1.0)  # a degree-hours figure in the user's unit
        deg = "C" if unit == "celsius" else "F"
        if d_disc_u > 0.05:
            parts.append(f"{d_disc_u:.1f} more {deg}-hours outside your sleep band")
        elif d_disc_u < -0.05:
            parts.append(f"{-d_disc_u:.1f} fewer {deg}-hours outside your sleep band")
        if d_hvac >= 5:
            parts.append(f"{d_hvac} more minutes of heat or AC")
        elif d_hvac <= -5:
            parts.append(f"{-d_hvac} fewer minutes of heat or AC")
        if o.n_actions > best.n_actions:
            parts.append(f"{o.n_actions - best.n_actions} more thing(s) for you to do")
        elif o.n_actions < best.n_actions:
            parts.append(f"{best.n_actions - o.n_actions} fewer thing(s) for you to do")
        if not o.runs:
            parts = ["would not do anything tonight"]
        rows.append(
            {
                "key": o.key,
                "label": o.label,
                "score_delta": round(d_score, 2),
                "hvac_minutes_delta": d_hvac,
                "discomfort_fh_delta": round(d_disc, 2),
                "text": f"{o.label}: " + (", ".join(parts) if parts else "about the same"),
            }
        )
    return rows


def headline(strategy: NightStrategy, unit: str = FAHRENHEIT) -> str | None:
    """The one-sentence recommendation, in occupant words. None when there is nothing to recommend."""
    o = strategy.best
    if o is None:
        return None
    hold = _hold_clause(strategy, o, unit)
    if o.key == OPT_CLOSED or not o.runs:
        return f"Keep the windows closed tonight; {hold}." if hold else "Keep the windows closed tonight."
    close = next((a for a in o.actions if a.kind in ("windows_close", "fan_off")), None)
    fan = any(a.kind == "fan_on" for a in o.actions)
    what = {
        OPT_BLOWER_2H: "run the HVAC fan",
    }.get(o.key, "run the whole-house fan with the windows open" if fan else "open the bedroom windows")
    if o.key == OPT_WINDOWS_ALL:
        text = "Open the bedroom windows at bedtime and leave them open all night"
    elif close is not None:
        when = _fmt_time(close.ts)
        approx = "about " if close.predicted else ""
        verb = what[0].upper() + what[1:]
        close_what = "turn it off" if o.key == OPT_BLOWER_2H else "close up"
        text = f"{verb} until {approx}{when}, then {close_what}"
    else:
        text = what[0].upper() + what[1:]
    return f"{text}; {hold}." if hold else f"{text}."


def _hold_clause(strategy: NightStrategy, o: StrategyOption, unit: str) -> str:
    heat = o.heat_min > 0 or o.cool_min == 0
    cool = o.cool_min > 0
    parts = []
    if heat:
        parts.append(f"CA keeps the heat for the bedroom at {_t(strategy.floor_f, unit)}")
    if cool:
        parts.append(f"CA keeps the AC for the bedroom at {_t(strategy.ceiling_f, unit)}")
    return "; ".join(parts)


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------------------------- the timeline step
def _timeline_module() -> Any:
    """The R3 timeline module (a function so tests can stand a contract-shaped module in)."""
    from . import operation_timeline  # noqa: PLC0415

    return operation_timeline


def _sentence(key: str, unit: str, **f: Any) -> str:
    """The R4 sentences (plan section 9). Each says only what CA does or what the occupant is asked to do."""
    tmpl = NIGHT_SENTENCES[key]
    for name in tmpl.facts:
        if f.get(name) is None:
            raise KeyError(f"{key}: {name}")
    vals: dict[str, str] = {}
    for name, kind in tmpl.facts.items():
        v = f[name]
        vals[name] = _fmt_time(v) if kind == "time" else (_t(v, unit) if kind == "temp" else str(v))
    return tmpl.template.format(**vals)


@dataclass(frozen=True)
class NightSentence:
    key: str
    template: str
    facts: Mapping[str, str]  # name -> time | temp | text
    source: str
    guard: str


NIGHT_SENTENCES: dict[str, NightSentence] = {
    s.key: s
    for s in (
        NightSentence(
            "night_strategy.plan.closed",
            "{t}: CA holds the bedroom between {floor} and {ceiling} tonight; nothing for you to do.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "StrategyOption(closed) + sleep band",
            "recommended option is 'closed' or would not run",
        ),
        NightSentence(
            "night_strategy.plan.active",
            "{t}: tonight's bedroom plan is below; CA holds the bedroom between {floor} and {ceiling} around it.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "StrategyOption(recommended) + sleep band",
            "recommended option has occupant actions",
        ),
        NightSentence(
            "night_strategy.windows_open",
            "{t}: open the bedroom windows; the outdoor air is cooler than the bedroom.",
            {"t": "time"},
            "OptionAction(windows_open)",
            "chosen option opens windows",
        ),
        NightSentence(
            "night_strategy.windows_close",
            "{t}: close the bedroom windows; CA keeps the bedroom at {floor} or above.",
            {"t": "time", "floor": "temp"},
            "OptionAction(windows_close)",
            "chosen option closes windows",
        ),
        NightSentence(
            "night_strategy.windows_close_predicted",
            "{t}: close the bedroom windows (about when the bedroom reaches {target}); CA keeps it at {floor}+.",
            {"t": "time", "target": "temp", "floor": "temp"},
            "OptionAction(windows_close, predicted)",
            "chosen option closes on a condition",
        ),
        NightSentence(
            "night_strategy.fan_on",
            "{t}: turn the {fan} on with the windows open to cool the bedroom.",
            {"t": "time", "fan": "text"},
            "OptionAction(fan_on)",
            "chosen option runs a fan and CA's nat-vent will not run it",
        ),
        NightSentence(
            "night_strategy.fan_off",
            "{t}: turn the {fan} off; CA takes over holding the bedroom.",
            {"t": "time", "fan": "text"},
            "OptionAction(fan_off)",
            "chosen option runs a fan and CA's nat-vent will not stop it",
        ),
    )
}


def _with_natvent_flag(step: Any, kind_cls: Any) -> Any:
    if step.kind == kind_cls.NIGHT_STRATEGY:
        return dataclasses.replace(step, meta={**step.meta, "fan_by_ca_natvent": True})
    return step


def _covered_by_natvent(steps: Sequence[Any], kind_cls: Any, actor_cls: Any, on: datetime, off: datetime) -> bool:
    """True when a CA nat-vent session in the timeline already runs the fan across [on, off] (within a tolerance)."""
    tol = timedelta(minutes=NATVENT_COVER_TOLERANCE_MIN)
    starts = [s.ts for s in steps if s.kind == kind_cls.NAT_VENT_START and s.actor == actor_cls.CA]
    stops = [s.ts for s in steps if s.kind == kind_cls.NAT_VENT_STOP and s.actor == actor_cls.CA]
    if not starts:
        return False
    started = any(t <= on + tol for t in starts)
    if not started:
        return False
    # the session that started at or before `on` must not stop before `off`
    last_start = max(t for t in starts if t <= on + tol)
    stop_after = [t for t in stops if t >= last_start]
    return not stop_after or min(stop_after) >= off - tol


def apply_night_strategy(
    timeline: Any,
    strategy: NightStrategy | StrategyOption,
    *,
    unit: str = FAHRENHEIT,
    floor_f: float | None = None,
    ceiling_f: float | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    target_sensor: str | None = None,
    now: datetime | None = None,
) -> Any:
    """Return a NEW timeline with tonight's chosen strategy added (R3 contract: pure, never mutates, idempotent).

    Steps added:

    * one ``NIGHT_STRATEGY`` step at the start of the night (actor CA: it states what CA holds; the occupant
      instructions are the separate window / fan steps, so Next Automation never reads an occupant instruction);
    * ``WINDOWS_OPEN`` / ``WINDOWS_CLOSE`` (actor YOU, lead 30 min, always: CA has no window actuator);
    * ``FAN_ON`` / ``FAN_OFF``. ACTOR RULE: in R4 CA does NOT run a fan for a strategy (DP-7), so a fan step is
      advice with actor YOU, EXCEPT when the timeline already holds a CA nat-vent session (``NAT_VENT_START`` /
      ``NAT_VENT_STOP``, actor CA) that runs the fan across the same span: then the fan runs on CA's own rule and
      no fan step is added (the nat-vent steps already say it). A partly covered span keeps the YOU steps.

    ``now`` (optional): steps already in the past are marked ``status: done`` (the latch does the same for its own
    steps), so re-applying every cycle never turns a step that has happened back into a pending one.

    Existing YOU window/fan steps inside the night span are replaced by the chosen option's (for a "closed" pick only
    the OPEN steps are removed, so a generic "close at bedtime" survives). The target sensor's path points in the
    night span are replaced by the option's predicted path (model "v4").
    """
    ot = _timeline_module()
    Actor, StepKind, TimelineStep, PathPoint = ot.Actor, ot.StepKind, ot.TimelineStep, ot.PathPoint
    if isinstance(strategy, NightStrategy):
        opt = strategy.best
        floor = strategy.floor_f if floor_f is None else floor_f
        ceiling = strategy.ceiling_f if ceiling_f is None else ceiling_f
        t0 = strategy.start if start is None else start
        t1 = strategy.end if end is None else end
        sensor = strategy.target_sensor if target_sensor is None else target_sensor
        show = strategy.show_numbers
    else:
        opt = strategy
        floor, ceiling, t0, t1 = floor_f, ceiling_f, start, end
        sensor = target_sensor
        show = True
        if floor is None or ceiling is None or t0 is None or t1 is None:
            raise ValueError("a bare StrategyOption needs floor_f, ceiling_f, start and end")
    if opt is None:
        return timeline

    fan_word = "HVAC fan" if opt.cls == CLS_BLOWER else "whole-house fan"
    do_nothing = opt.key == OPT_CLOSED or not opt.runs
    conf = "ok" if opt.grade in OFFERED_GRADES and (show or opt.key != OPT_CLOSED) else "low"

    old = list(timeline.steps)
    kept: list[Any] = []
    replaced: list[str] = []
    window_fan_kinds = {StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE, StepKind.FAN_ON, StepKind.FAN_OFF}
    for s in old:
        if s.kind == StepKind.NIGHT_STRATEGY or s.meta.get("source_module") == "night_strategy":
            continue  # idempotent: drop our own earlier steps
        # a "closed" pick removes only the opens (a generic close at bedtime must survive); any other pick
        # replaces the plan's own window / fan steps for the night
        in_scope = t0 <= s.ts <= t1 and s.actor == Actor.YOU and s.kind in window_fan_kinds
        if in_scope and (not do_nothing or s.kind in (StepKind.WINDOWS_OPEN, StepKind.FAN_ON)):
            replaced.append(str(s.meta.get("id", s.reason)))
            continue
        kept.append(s)

    new: list[Any] = []
    meta_base: dict[str, Any] = {
        "source_module": "night_strategy",
        "option": opt.key,
        "grade": opt.grade,
        "replaced": replaced,
    }
    plan_key = "night_strategy.plan.closed" if do_nothing else "night_strategy.plan.active"
    new.append(
        TimelineStep(
            ts=t0,
            actor=Actor.CA,
            kind=StepKind.NIGHT_STRATEGY,
            text=_sentence(plan_key, unit, t=t0, floor=floor, ceiling=ceiling),
            reason=plan_key,
            target_f=floor,
            confidence=conf,
            lead_minutes=0,
            source=STEP_SOURCE,
            meta={
                **meta_base,
                "label": opt.label,
                "bedroom_low_f": round(opt.bed_min_f, 1),
                "bedroom_high_f": round(opt.bed_max_f, 1),
                "bedroom_wake_f": round(opt.bed_wake_f, 1),
                "hvac_minutes": opt.hvac_minutes,
                "score": round(opt.score, 2),
                "show_numbers": show,
            },
        )
    )
    fan_on = next((a for a in opt.actions if a.kind == "fan_on"), None)
    fan_off = next((a for a in opt.actions if a.kind == "fan_off"), None)
    natvent_covers = (
        fan_on is not None and fan_off is not None and _covered_by_natvent(old, StepKind, Actor, fan_on.ts, fan_off.ts)
    )
    if conf == "ok":
        for a in opt.actions:
            if a.kind.startswith("fan") and natvent_covers:
                continue
            kind = StepKind(a.kind)
            if a.kind == "windows_open":
                key, facts = REASON_WINDOWS_OPEN, {"t": a.ts}
            elif a.kind == "windows_close":
                tgt_t = floor + FAN_UNTIL_ABOVE_FLOOR_F
                if a.predicted:
                    key = "night_strategy.windows_close_predicted"
                    facts = {"t": a.ts, "target": tgt_t, "floor": floor}
                else:
                    key, facts = REASON_WINDOWS_CLOSE, {"t": a.ts, "floor": floor}
            elif a.kind == "fan_on":
                key, facts = REASON_FAN_ON, {"t": a.ts, "fan": fan_word}
            else:
                key, facts = REASON_FAN_OFF, {"t": a.ts, "fan": fan_word}
            new.append(
                TimelineStep(
                    ts=a.ts,
                    actor=Actor.YOU,
                    kind=kind,
                    text=_sentence(key, unit, **facts),
                    reason=key,
                    target_f=floor if a.kind.endswith("close") or a.kind == "fan_off" else None,
                    confidence="ok",
                    lead_minutes=YOU_STEP_LEAD_MIN,
                    source=STEP_SOURCE,
                    meta={**meta_base, "predicted_time": a.predicted, "fan_by_ca_natvent": False},
                )
            )
    if natvent_covers:
        new = [_with_natvent_flag(s, StepKind) for s in new]
    if now is not None:
        new = [dataclasses.replace(s, meta={**s.meta, "status": "done"}) if s.ts < now else s for s in new]

    order = {StepKind.NIGHT_STRATEGY: 0}
    steps = sorted([*kept, *new], key=lambda s: (s.ts, order.get(s.kind, 1)))

    path = list(timeline.path)
    if sensor is not None and opt.path:
        path = [p for p in path if not (p.sensor == sensor and t0 < p.ts <= t1)]
        path.extend(PathPoint(ts=t, temp_f=round(v, 2), sensor=sensor, model="v4") for t, v in opt.path)
        path.sort(key=lambda p: (p.sensor, p.ts))
    return dataclasses.replace(timeline, steps=tuple(steps), path=tuple(path))


def replay_recorded(
    inp: NightInputs,
    flags: Sequence[tuple[float, float]],
    setpoints: Sequence[tuple[float | None, float | None]] | None,
    *,
    fan_is_whole_house: bool = True,
    target: str | None = None,
) -> _Sim | None:
    """Closed-loop replay of a night with RECORDED window / fan flags (and recorded setpoints when given):
    the same controller and physics as ``plan_night``, used by ``tools/night_strategy_replay.py``."""
    spec = _SPECS[OPT_FAN_2H if fan_is_whole_house else OPT_BLOWER_2H]
    return _simulate(inp, spec, target or inp.model.primary, flags=flags, setpoints=setpoints)
