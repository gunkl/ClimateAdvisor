"""Pure rules for learning the thermostat's own hysteresis ("swing") - Issue #1234.

The learned swing is how far the room is allowed to drift past the setpoint before the
occupant's thermostat starts the equipment:

* heat: ``swing_heat_f = setpoint - indoor`` at the real heat-on transition
* cool: ``swing_cool_f = indoor - setpoint`` at the real cool-on transition

It is one-sided - a thermostat that starts heat at setpoint-1.4 and stops at setpoint-0.9 has a
heat swing of 1.4 and an off-point of 0.9. The off-point (and the coast past it) are different
quantities, kept separately (``off_point_f``) and never mixed into the swing.

This module is the ONE implementation of "is this a valid swing sample, and what value":
``coordinator.py`` (live) and ``tools/thermal_replay.py`` (chart_log backfill) both call
``evaluate_swing_cycle`` and the cache helpers below, so the two cannot drift apart. It is
dependency-free (no Home Assistant import) and loadable as a standalone file by tools/.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

try:
    from .const import (
        COMFORT_DEVIATION_FLOOR_F,
        COMFORT_SWING_TOLERANCE_MAX_F,
        THERMAL_COAST_ESTIMATOR_WINDOW,
        THERMAL_COAST_MAX_F,
        THERMAL_COAST_MAX_S,
        THERMAL_SWING_AUDIT_CAP,
        THERMAL_SWING_DEFAULT_F,
        THERMAL_SWING_DEFINITION_VERSION,
        THERMAL_SWING_ESTIMATOR_WINDOW,
        THERMAL_SWING_MAX_F,
        THERMAL_SWING_MIN_F,
        THERMAL_SWING_REARM_WINDOW_S,
        THERMAL_SWING_ROUND_STEP_F,
        THERMAL_SWING_ROUND_TOLERANCE_F,
        THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S,
        THERMAL_SWING_WHOLE_DEGREE_RESOLUTION_F,
    )
except ImportError:  # loaded as a standalone file by tools/ (the package __init__ needs Home Assistant)
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location("_ca_const_standalone", _Path(__file__).with_name("const.py"))
    _const = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_const)  # type: ignore[union-attr]
    COMFORT_DEVIATION_FLOOR_F = _const.COMFORT_DEVIATION_FLOOR_F
    COMFORT_SWING_TOLERANCE_MAX_F = _const.COMFORT_SWING_TOLERANCE_MAX_F
    THERMAL_COAST_ESTIMATOR_WINDOW = _const.THERMAL_COAST_ESTIMATOR_WINDOW
    THERMAL_COAST_MAX_F = _const.THERMAL_COAST_MAX_F
    THERMAL_COAST_MAX_S = _const.THERMAL_COAST_MAX_S
    THERMAL_SWING_AUDIT_CAP = _const.THERMAL_SWING_AUDIT_CAP
    THERMAL_SWING_DEFAULT_F = _const.THERMAL_SWING_DEFAULT_F
    THERMAL_SWING_DEFINITION_VERSION = _const.THERMAL_SWING_DEFINITION_VERSION
    THERMAL_SWING_ESTIMATOR_WINDOW = _const.THERMAL_SWING_ESTIMATOR_WINDOW
    THERMAL_SWING_MAX_F = _const.THERMAL_SWING_MAX_F
    THERMAL_SWING_MIN_F = _const.THERMAL_SWING_MIN_F
    THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S = _const.THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S
    THERMAL_SWING_REARM_WINDOW_S = _const.THERMAL_SWING_REARM_WINDOW_S
    THERMAL_SWING_ROUND_STEP_F = _const.THERMAL_SWING_ROUND_STEP_F
    THERMAL_SWING_ROUND_TOLERANCE_F = _const.THERMAL_SWING_ROUND_TOLERANCE_F
    THERMAL_SWING_WHOLE_DEGREE_RESOLUTION_F = _const.THERMAL_SWING_WHOLE_DEGREE_RESOLUTION_F

SWING_MODES = ("heat", "cool")

# Reason codes (returned with every verdict so a rejected sample is explainable in logs/tests).
SWING_OK = "ok"
SWING_REJECT_MISSING_INPUT = "missing_input"
SWING_REJECT_SETPOINT_CHANGE = "setpoint_change"
SWING_REJECT_SHORT_RESTART = "short_restart"
SWING_REJECT_WRONG_DIRECTION = "wrong_direction"
SWING_REJECT_NON_POSITIVE = "non_positive"
SWING_REJECT_OUT_OF_BOUNDS = "out_of_bounds"
# Every reject code ``evaluate_swing_cycle`` can return, in the order it checks them (Issue #1233:
# the audit log, the ``learning_db --swing`` histogram and the AI context all list from here).
SWING_REJECT_REASONS = (
    SWING_REJECT_MISSING_INPUT,
    SWING_REJECT_SETPOINT_CHANGE,
    SWING_REJECT_SHORT_RESTART,
    SWING_REJECT_WRONG_DIRECTION,
    SWING_REJECT_NON_POSITIVE,
    SWING_REJECT_OUT_OF_BOUNDS,
)

# One line for the homeowner / the AI: what the number means (Issue #1233).
SWING_DEFINITION_TEXT = (
    "Swing = how far past the setpoint the thermostat lets the room drift before starting the equipment "
    "(heat: degrees below the setpoint at heat-on; cool: degrees above it at cool-on)."
)
SWING_TRIGGER_HVAC_ACTION = "hvac_action"

# What a "setpoint change" is (Issue #1234 rule, shared by the coordinator and tools/swing_audit.py since
# #1233): the HVAC mode (the climate state) or any of these setpoint attributes changing between two states.
SETPOINT_CHANGE_ATTRS = ("temperature", "target_temp_low", "target_temp_high")


def setpoint_attrs_for(mode: str) -> tuple[str, str]:
    """Where a thermostat's ``mode`` setpoint is read, in preference order: the single ``temperature``,
    else the heat_cool low (heat) / high (cool) bound (Issue #1048)."""
    return ("temperature", "target_temp_low" if mode == "heat" else "target_temp_high")


def pick_setpoint(get: Callable[[str], Any], mode: str) -> Any:
    """The first non-None setpoint attribute for ``mode`` (``get`` reads one attribute, already converted)."""
    for attr in setpoint_attrs_for(mode):
        value = get(attr)
        if value is not None:
            return value
    return None


def setpoint_or_mode_changed(
    old_mode: Any, old_get: Callable[[str], Any], new_mode: Any, new_get: Callable[[str], Any]
) -> bool:
    """True when the HVAC mode or any ``SETPOINT_CHANGE_ATTRS`` differs between two thermostat states."""
    return old_mode != new_mode or any(old_get(a) != new_get(a) for a in SETPOINT_CHANGE_ATTRS)


# Reading-granularity inference: readings (stored in F, 2 dp) that all sit on one grid of the unit
# the thermostat reports reveal that grid. Tolerance absorbs the 2-dp rounding of converted values.
_GRID_TOLERANCE = 0.011
_MIN_DISTINCT_READINGS_FOR_RESOLUTION = 3
# (unit, grid step in that unit, resolution in F) - checked in order within the reporting unit.
_RESOLUTION_GRIDS = {
    "fahrenheit": ((1.0, 1.0), (0.5, 0.5)),
    "celsius": ((1.0, 1.8), (0.5, 0.9)),
}


@dataclass(frozen=True)
class SwingVerdict:
    """Outcome of judging one heat/cool cycle: a sample dict, or ``None`` plus the reason."""

    sample: dict[str, Any] | None
    reason: str


def whole_degree_resolution_f(unit: str | None) -> float:
    """One whole degree of ``unit``, in F: the granularity of a thermostat's own ``current_temperature``."""
    return THERMAL_SWING_WHOLE_DEGREE_RESOLUTION_F * (1.8 if unit == "celsius" else 1.0)


def swing_value(mode: str, setpoint_f: float, indoor_f: float) -> float:
    """Signed distance past the setpoint at an on-transition (positive = on past the setpoint)."""
    return setpoint_f - indoor_f if mode == "heat" else indoor_f - setpoint_f


def evaluate_swing_cycle(
    *,
    mode: str,
    ts: str,
    setpoint_f: float | None,
    on_indoor_f: float | None,
    off_indoor_f: float | None,
    off_setpoint_f: float | None = None,
    seconds_since_setpoint_change: float | None = None,
    resolution_f: float = 0.0,
    seconds_since_previous_off: float | None = None,
    reading_unit: str | None = None,
) -> SwingVerdict:
    """Judge one completed heat/cool cycle and return the swing sample it supports, if any.

    Args:
        mode: ``"heat"`` or ``"cool"`` (a REAL ``hvac_action`` cycle; callers never pass a
            mode-fallback "phantom" session).
        ts: ISO timestamp of the on-transition (the sample's identity for de-duplication).
        setpoint_f: the setpoint in force at the on-transition.
        on_indoor_f: the finest available reading of the thermostat's own temperature at the
            on-transition.
        off_indoor_f: the same reading at the following off-transition (direction check and
            off-point only).
        off_setpoint_f: the setpoint in force at the off-transition; the off-point is recorded
            only when it equals ``setpoint_f`` (a setpoint moved mid-run makes it meaningless).
        seconds_since_setpoint_change: seconds between the last setpoint/mode change and the
            on-transition, or ``None`` when no recent change is known.
        resolution_f: reading granularity known from the SOURCE (0.0 = fine sensor; whole degree
            of the unit otherwise) - a fallback only: ``summarize_swing_samples`` infers the real
            granularity from the readings once there are enough of them.
        seconds_since_previous_off: seconds between the previous off-transition in the same
            direction and this on-transition, or ``None`` when unknown.
        reading_unit: the unit the thermostat reports (``"fahrenheit"``/``"celsius"``), for the
            resolution inference; ``None`` when unknown (chart_log replay).

    Exclusions, in order: missing input; on-transition within
    ``THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S`` of a setpoint change (a jump, not hysteresis -
    exactly the window length IS sampled); on-transition within ``THERMAL_SWING_REARM_WINDOW_S``
    of the previous off-transition (``hvac_action`` flapping, not a new drift); wrong-direction
    run (heat whose temperature fell / cool whose temperature rose); non-positive value (on
    at/inside the setpoint); value outside ``[THERMAL_SWING_MIN_F, THERMAL_SWING_MAX_F]``.
    """
    if mode not in SWING_MODES or setpoint_f is None or on_indoor_f is None or off_indoor_f is None:
        return SwingVerdict(None, SWING_REJECT_MISSING_INPUT)
    if (
        seconds_since_setpoint_change is not None
        and seconds_since_setpoint_change < THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S
    ):
        return SwingVerdict(None, SWING_REJECT_SETPOINT_CHANGE)
    if seconds_since_previous_off is not None and seconds_since_previous_off < THERMAL_SWING_REARM_WINDOW_S:
        return SwingVerdict(None, SWING_REJECT_SHORT_RESTART)
    fell = off_indoor_f < on_indoor_f
    rose = off_indoor_f > on_indoor_f
    if (mode == "heat" and fell) or (mode == "cool" and rose):
        return SwingVerdict(None, SWING_REJECT_WRONG_DIRECTION)
    value = swing_value(mode, setpoint_f, on_indoor_f)
    if value <= 0:
        return SwingVerdict(None, SWING_REJECT_NON_POSITIVE)
    if not (THERMAL_SWING_MIN_F <= value <= THERMAL_SWING_MAX_F):
        return SwingVerdict(None, SWING_REJECT_OUT_OF_BOUNDS)
    off_point: float | None = None
    if off_setpoint_f is not None and abs(off_setpoint_f - setpoint_f) < 1e-6:
        # Off-point = how far the equipment lets the room get to the setpoint before stopping,
        # in the SAME sign convention as the swing (positive = still short of the setpoint).
        off_point = round(swing_value(mode, setpoint_f, off_indoor_f), 2)
    return SwingVerdict(
        {
            "ts": ts,
            "value_f": round(value, 2),
            "resolution_f": float(resolution_f),
            "setpoint_f": round(float(setpoint_f), 2),
            "on_indoor_f": round(float(on_indoor_f), 2),
            "off_indoor_f": round(float(off_indoor_f), 2),
            "reading_unit": reading_unit,
            "off_point_f": off_point,
        },
        SWING_OK,
    )


def infer_resolution_f(readings: list[float | None], unit: str | None = None) -> float | None:
    """Reading granularity (in F) inferred from the readings themselves, or ``None`` if too few.

    Readings are in internal F. They are checked on the grids of the unit the thermostat reports:
    whole F -> 1.0, half F -> 0.5; whole C -> 1.8, half C -> 0.9; anything finer (e.g. an
    Ecobee's tenths) -> 0.0. ``unit=None`` (unknown, e.g. chart_log replay) tries Fahrenheit then
    Celsius grids. Needs ``_MIN_DISTINCT_READINGS_FOR_RESOLUTION`` distinct readings: a steady
    thermostat that always starts at the same reading proves nothing about its granularity, and
    the caller then falls back to what the SOURCE implies.
    """
    vals = sorted({round(float(r), 2) for r in readings if r is not None})
    if len(vals) < _MIN_DISTINCT_READINGS_FOR_RESOLUTION:
        return None
    return grid_resolution_f(vals, unit)


def grid_resolution_f(readings: list[float | None], unit: str | None = None) -> float:
    """The coarsest reading grid every one of ``readings`` sits on (degF; 0.0 = on none, i.e. fine).

    The grid test of ``infer_resolution_f`` without its distinct-readings bar - for a caller that already knows
    the readings are a sample of a source proven finer elsewhere (``thermal_replay``, Issue #1477). Empty -> 0.0.
    """
    vals = [float(r) for r in readings if r is not None]
    if not vals:
        return 0.0
    units = (unit,) if unit in _RESOLUTION_GRIDS else ("fahrenheit", "celsius")
    for u in units:
        native = vals if u == "fahrenheit" else [(v - 32.0) / 1.8 for v in vals]
        for step, res_f in _RESOLUTION_GRIDS[u]:
            if all(abs(x / step - round(x / step)) * step <= _GRID_TOLERANCE for x in native):
                return res_f
    return 0.0


def _sample_key(ts: str) -> str:
    """Identity of a sample: its start time to the minute (a live sample and the chart_log row for
    the same cycle are stamped by two clock reads a few ms apart; cycles are minutes apart)."""
    return str(ts)[:16]


def merge_swing_samples(
    existing: list[dict[str, Any]] | None, new: list[dict[str, Any]], cap: int = THERMAL_SWING_ESTIMATOR_WINDOW
) -> tuple[list[dict[str, Any]], int]:
    """Merge samples by start minute (new wins), keep the newest ``cap``. Returns (merged, n_added)."""
    by_key = {_sample_key(s["ts"]): s for s in (existing or []) if isinstance(s, dict) and "ts" in s}
    added = sum(1 for s in new if _sample_key(s["ts"]) not in by_key)
    for s in new:
        by_key[_sample_key(s["ts"])] = s
    merged = sorted(by_key.values(), key=lambda s: s["ts"])
    return merged[-cap:], added


def round_swing_up_f(value: float | None) -> float | None:
    """The stored and used swing: ``value`` rounded UP to the next half degree F (Issue #1477).

    ``ceil(x * 2) / 2``: 1.44 -> 1.5, 1.0 -> 1.0, 1.01 -> 1.5, 1.5 -> 1.5. ``THERMAL_SWING_ROUND_TOLERANCE_F``
    keeps float noise (1.0000001) from rounding a whole half degree up. Owner decision 2026-11-08: a swing that is
    too small makes CA expect the heat or AC sooner than the thermostat starts, so err upward. Always degF; a
    Celsius display converts the result as a delta (x 5/9, no offset). ``None`` stays ``None``.
    """
    if value is None:
        return None
    step = THERMAL_SWING_ROUND_STEP_F
    return round(math.ceil((float(value) - THERMAL_SWING_ROUND_TOLERANCE_F) / step) * step, 2)


def _sample_resolution(sample: dict[str, Any]) -> float | None:
    """The reading granularity recorded on one sample (degF; 0.0 = fine), or ``None`` when unknown."""
    r = sample.get("resolution_f")
    if isinstance(r, bool) or not isinstance(r, (int, float)) or r < 0:
        return None
    return float(r)


def _readings(samples: list[dict[str, Any]]) -> list[float | None]:
    return [s.get(k) for s in samples for k in ("on_indoor_f", "off_indoor_f")]


def finest_resolution_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The samples the estimator uses: the finest-resolution ones in ``samples`` (Issue #1477).

    A whole-degree sample only says the true swing lies within +/- 0.5 of its reading (``setpoint - k``), so
    when finer samples are in the window they decide and the coarse ones sit out: on Zone 1, five 1.0 samples
    from the thermostat's whole-degree display out-voted two 1.44 readings of its finer sensor. Nothing is
    dropped from the stored window, so the rule is reversible: the coarse samples are used again as soon as no
    finer one is left in the window. A window of one resolution is returned unchanged; so is a "finer" tier
    whose own readings turn out to be no finer than the rest (``infer_resolution_f``), e.g. a configured
    sensor that reports whole degrees. Samples with no recorded resolution count as the coarsest.
    """
    known = [r for r in (_sample_resolution(s) for s in samples) if r is not None]
    if not known:
        return samples
    finest = min(known)
    tier = [s for s in samples if (r := _sample_resolution(s)) is not None and r <= finest + 1e-6]
    if len(tier) == len(samples):
        return samples
    coarser = [r for r in known if r > finest + 1e-6]
    inferred = infer_resolution_f(_readings(tier), tier[-1].get("reading_unit"))
    if coarser and inferred is not None and inferred >= min(coarser) - 1e-6:
        return samples
    return tier


def summarize_swing_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimator: the half-degree-up median of the window's finest samples, plus resolution and off-point.

    The window is the last ``THERMAL_SWING_ESTIMATOR_WINDOW`` samples; of those, only the finest-resolution
    ones are used (``finest_resolution_samples``, Issue #1477). ``raw_value_f`` is their median; ``value_f``
    (what is stored and used) is that median rounded up to the next half degree (``round_swing_up_f``).
    ``resolution_f`` is inferred from the used samples' own on/off readings in the unit the thermostat reports
    (``infer_resolution_f``); only when there are too few distinct readings does it fall back to the worst
    SOURCE-implied resolution recorded on them. ``n_used`` / ``n_window`` say how many samples decided.
    """
    window = samples[-THERMAL_SWING_ESTIMATOR_WINDOW:]
    if not window:
        return {
            "value_f": None,
            "raw_value_f": None,
            "resolution_f": 0.0,
            "off_point_f": None,
            "n_used": 0,
            "n_window": 0,
        }
    used = finest_resolution_samples(window)
    values = [s["value_f"] for s in used]
    inferred = infer_resolution_f(_readings(used), used[-1].get("reading_unit"))
    resolution = inferred if inferred is not None else max(float(s.get("resolution_f", 0.0) or 0.0) for s in used)
    offs = [s["off_point_f"] for s in used if s.get("off_point_f") is not None]
    raw = round(median(values), 2)
    return {
        "value_f": round_swing_up_f(raw),
        "raw_value_f": raw,
        "resolution_f": resolution,
        "off_point_f": round(median(offs), 2) if offs else None,
        "n_used": len(used),
        "n_window": len(window),
    }


def _prefix(mode: str) -> str:
    return "swing_heat" if mode == "heat" else "swing_cool"


def _store_swing_summary(cache: dict[str, Any], mode: str, samples: list[dict[str, Any]]) -> None:
    p = _prefix(mode)
    summary = summarize_swing_samples(samples)
    cache[f"{p}_samples"] = samples
    cache[f"{p}_f"] = summary["value_f"]
    cache[f"{p}_raw_f"] = summary["raw_value_f"]
    cache[f"{p}_resolution_f"] = summary["resolution_f"]
    cache[f"{p}_off_point_f"] = summary["off_point_f"]


def apply_swing_samples(cache: dict[str, Any], mode: str, new_samples: list[dict[str, Any]]) -> int:
    """Fold accepted samples into a thermal_model_cache dict (live and replay share this).

    Updates ``swing_{mode}_f`` (the half-degree-up median of the finest samples, Issue #1477),
    ``swing_{mode}_raw_f`` (that median before rounding), ``swing_{mode}_samples`` (the retained window),
    ``swing_{mode}_resolution_f``, ``swing_{mode}_off_point_f`` and ``observation_count_swing_{mode}``.
    Returns the number of NEW samples added.
    """
    p = _prefix(mode)
    merged, added = merge_swing_samples(cache.get(f"{p}_samples"), new_samples)
    _store_swing_summary(cache, mode, merged)
    cache[f"observation_count_{p}"] = int(cache.get(f"observation_count_{p}", 0) or 0) + added
    cache["swing_definition_version"] = THERMAL_SWING_DEFINITION_VERSION
    return added


def refresh_swing_cache(cache: dict[str, Any]) -> list[str]:
    """Recompute each mode's stored swing from its retained samples with the current estimator (Issue #1477).

    Called on load so a value stored by an older estimator (e.g. Zone 1's 1.0 from a median that let the
    whole-degree samples out-vote the fine ones, never rounded) is corrected at once instead of waiting for
    the next accepted heat/cool cycle. A mode with a value but no samples keeps it, rounded up. Counts,
    audit and reject counters are untouched; idempotent. Returns the modes whose ``swing_{mode}_f`` changed.
    """
    changed: list[str] = []
    for mode in SWING_MODES:
        p = _prefix(mode)
        before = cache.get(f"{p}_f")
        samples = [s for s in (cache.get(f"{p}_samples") or []) if isinstance(s, dict) and "value_f" in s]
        if samples:
            _store_swing_summary(cache, mode, samples[-THERMAL_SWING_ESTIMATOR_WINDOW:])
        elif isinstance(before, (int, float)) and not isinstance(before, bool) and before > 0:
            cache[f"{p}_f"] = round_swing_up_f(before)
        if cache.get(f"{p}_f") != before:
            changed.append(mode)
    return changed


def _holds_swing_data(cache: dict[str, Any], mode: str) -> bool:
    p = _prefix(mode)
    return bool(cache.get(f"{p}_f") is not None or cache.get(f"observation_count_{p}") or cache.get(f"{p}_samples"))


def needs_swing_migration(cache: dict[str, Any]) -> bool:
    """True when ``cache`` holds swing data learned under the retired definition (no current marker)."""
    if cache.get("swing_definition_version") == THERMAL_SWING_DEFINITION_VERSION:
        return False
    # nothing learned under the retired definition -> nothing to reset; the first new sample stamps it
    return any(_holds_swing_data(cache, mode) for mode in SWING_MODES)


def reset_swing_cache(cache: dict[str, Any]) -> bool:
    """Migration: null the swing fields of a cache written under the retired definition.

    Returns True when anything was reset. A cache already at the current definition version is
    left untouched.
    """
    if not needs_swing_migration(cache):
        return False
    for mode in SWING_MODES:
        p = _prefix(mode)
        cache[f"{p}_f"] = None
        cache[f"observation_count_{p}"] = 0
        for extra in ("_samples", "_raw_f", "_resolution_f", "_off_point_f", "_audit", "_reject_counts"):
            cache.pop(f"{p}{extra}", None)
    cache["swing_definition_version"] = THERMAL_SWING_DEFINITION_VERSION
    return True


# ---------------------------------------------------------------------------
# Issue #1233 - the audit trail: every evaluated cycle, accepted or rejected
# ---------------------------------------------------------------------------


def build_swing_audit_record(
    mode: str,
    verdict: SwingVerdict,
    *,
    ts: str,
    setpoint_f: float | None,
    setpoint_age_s: float | None,
    on_indoor_f: float | None,
    off_indoor_f: float | None,
    indoor_entity: str | None,
    resolution_f: float,
    reading_unit: str | None,
    since_prev_off_s: float | None = None,
    trigger: str = SWING_TRIGGER_HVAC_ACTION,
) -> dict[str, Any]:
    """The persisted record of one judged cycle: the verdict AND every input that produced it.

    ``value_f`` is the accepted sample value, or - for a rejected cycle whose inputs allow it - the
    would-be value, so a homeowner sees e.g. ``out_of_bounds`` at 6.0 F rather than just a code.
    """
    if verdict.sample is not None:
        value: float | None = verdict.sample["value_f"]
    elif mode in SWING_MODES and setpoint_f is not None and on_indoor_f is not None:
        value = round(swing_value(mode, setpoint_f, on_indoor_f), 2)
    else:
        value = None

    def _r(x: float | None) -> float | None:
        return None if x is None else round(float(x), 2)

    return {
        "ts": ts,
        "mode": mode,
        "accepted": verdict.sample is not None,
        "reason": verdict.reason,
        "value_f": value,
        "setpoint_f": _r(setpoint_f),
        "setpoint_age_s": _r(setpoint_age_s),
        "on_indoor_f": _r(on_indoor_f),
        "off_indoor_f": _r(off_indoor_f),
        "indoor_entity": indoor_entity,
        "resolution_f": float(resolution_f),
        "reading_unit": reading_unit,
        "trigger": trigger,
        "since_prev_off_s": _r(since_prev_off_s),
    }


def record_swing_audit(cache: dict[str, Any], mode: str, record: dict[str, Any]) -> None:
    """Append ``record`` to the bounded per-mode audit list and bump the cumulative reject counter."""
    p = _prefix(mode)
    audit = list(cache.get(f"{p}_audit") or [])
    audit.append(record)
    cache[f"{p}_audit"] = audit[-THERMAL_SWING_AUDIT_CAP:]
    counts = dict(cache.get(f"{p}_reject_counts") or {})
    if not record.get("accepted"):
        reason = str(record.get("reason"))
        counts[reason] = int(counts.get(reason, 0)) + 1
    cache[f"{p}_reject_counts"] = counts


def swing_debug_summary(cache: dict[str, Any], recent: int = 5) -> dict[str, Any]:
    """What the dashboard / API / AI show about HOW the swing was learned (per mode)."""
    out: dict[str, Any] = {"definition": SWING_DEFINITION_TEXT}
    for mode in SWING_MODES:
        p = _prefix(mode)
        audit = [r for r in (cache.get(f"{p}_audit") or []) if isinstance(r, dict)]
        out[mode] = {
            "recent": audit[-recent:] if recent > 0 else [],
            "reject_counts": dict(cache.get(f"{p}_reject_counts") or {}),
            "window_accepted": sum(1 for r in audit if r.get("accepted")),
            "window_rejected": sum(1 for r in audit if not r.get("accepted")),
        }
    return out


# ---------------------------------------------------------------------------
# Issue #1233 - coast overshoot: how far the room keeps going after the equipment STOPS
# ---------------------------------------------------------------------------
# Kept apart from the swing on purpose: the swing is the ON-point hysteresis (what the thermostat
# tolerates before starting); the coast is what happens after the OFF-point (thermal momentum of the
# house and equipment). Heat: the peak above the off reading. Cool: the trough below it.

COAST_ENDED_NEXT_ON = "next_on"
COAST_ENDED_SETPOINT_CHANGE = "setpoint_change"  # a setpoint OR an HVAC mode change
COAST_ENDED_TIMEOUT = "timeout"
COAST_ENDED_REASONS = (COAST_ENDED_NEXT_ON, COAST_ENDED_SETPOINT_CHANGE, COAST_ENDED_TIMEOUT)

COAST_OK = "ok"
COAST_REJECT_MISSING_INPUT = "missing_input"
COAST_REJECT_NO_POST_READINGS = "no_post_readings"
COAST_REJECT_OUT_OF_BOUNDS = "out_of_bounds"


@dataclass(frozen=True)
class CoastVerdict:
    """Outcome of closing one coast window: a sample dict, or ``None`` plus the reason."""

    sample: dict[str, Any] | None
    reason: str


def coast_timed_out(age_s: float) -> bool:
    """True when a coast window opened ``age_s`` seconds ago has run past its bound (live and audit share it)."""
    return age_s > THERMAL_COAST_MAX_S


def coast_stop_was_commanded(seconds_since_setpoint_change: float | None) -> bool:
    """True when an equipment stop came within THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S of a
    setpoint/HVAC-mode change: CA's or the occupant's command, not the thermostat's own choice.

    The mirror of the on-edge rule in evaluate_swing_cycle: many thermostats report the command in
    one state change and hvac_action stopping in a later one, so "no change in the same event" alone
    would open a coast window at a commanded stop (against a setpoint that was not in force).
    """
    return (
        seconds_since_setpoint_change is not None
        and seconds_since_setpoint_change < THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S
    )


def coast_extreme(mode: str, current: float | None, reading: float | None) -> float | None:
    """Running peak (heat) / trough (cool) of the post-off readings."""
    if reading is None:
        return current
    if current is None:
        return reading
    return max(current, reading) if mode == "heat" else min(current, reading)


def build_coast_sample(mode: str, cand: dict[str, Any], ended_by: str, window_s: float) -> CoastVerdict:
    """Judge a closed coast window. ``cand`` carries ``ts`` (off time), ``off_indoor_f``,
    ``extreme_f``, ``setpoint_f`` and ``n_post`` (readings seen after the off edge).

    ``coast_f`` is never negative (a room that fell back after a heat stop coasted zero), and a
    window with no post-off reading proves nothing, so it yields no sample.
    """
    off = cand.get("off_indoor_f")
    extreme = cand.get("extreme_f")
    if mode not in SWING_MODES or off is None or extreme is None:
        return CoastVerdict(None, COAST_REJECT_MISSING_INPUT)
    if int(cand.get("n_post") or 0) < 1:
        return CoastVerdict(None, COAST_REJECT_NO_POST_READINGS)
    moved = (extreme - off) if mode == "heat" else (off - extreme)
    coast = max(0.0, moved)
    if coast > THERMAL_COAST_MAX_F:
        return CoastVerdict(None, COAST_REJECT_OUT_OF_BOUNDS)
    sp = cand.get("setpoint_f")
    past_setpoint = None if sp is None else round((extreme - sp) if mode == "heat" else (sp - extreme), 2)
    return CoastVerdict(
        {
            "ts": cand["ts"],
            "coast_f": round(coast, 2),
            "past_setpoint_f": past_setpoint,
            "off_indoor_f": round(float(off), 2),
            "extreme_f": round(float(extreme), 2),
            "setpoint_f": None if sp is None else round(float(sp), 2),
            "n_post": int(cand["n_post"]),
            "window_s": round(float(window_s), 1),
            "ended_by": ended_by,
        },
        COAST_OK,
    )


# --- The coast-window driver: ONE implementation for the live coordinator and tools/swing_audit.py ---
# ``windows`` maps "heat"/"cool" to an open window {ts, off_dt, off_indoor_f, extreme_f, setpoint_f, n_post}.
# Each function returns the windows it closed as (mode, ended_by, CoastVerdict); the caller records them.

CoastClosed = tuple[str, str, CoastVerdict]


def coast_close(windows: dict[str, dict[str, Any]], mode: str, ended_by: str, now: datetime) -> CoastClosed | None:
    """Close one window. An event that arrives after the bound did not end it: the bound did (``timeout``)."""
    cand = windows.pop(mode, None)
    if cand is None:
        return None
    age_s = (now - cand["off_dt"]).total_seconds()
    if coast_timed_out(age_s):
        ended_by = COAST_ENDED_TIMEOUT
    return mode, ended_by, build_coast_sample(mode, cand, ended_by, min(age_s, THERMAL_COAST_MAX_S))


def coast_close_all(windows: dict[str, dict[str, Any]], ended_by: str, now: datetime) -> list[CoastClosed]:
    return [c for m in list(windows) if (c := coast_close(windows, m, ended_by, now)) is not None]


def coast_feed(windows: dict[str, dict[str, Any]], now: datetime, reading: float | None) -> list[CoastClosed]:
    """Fold one indoor reading into the open windows. A window past ``THERMAL_COAST_MAX_S`` closes first,
    WITHOUT this reading (it is past the bound). Used for a sensor tick and for a plain thermostat update."""
    closed: list[CoastClosed] = []
    for mode in list(windows):
        cand = windows[mode]
        if coast_timed_out((now - cand["off_dt"]).total_seconds()):
            c = coast_close(windows, mode, COAST_ENDED_TIMEOUT, now)
            if c is not None:
                closed.append(c)
        elif reading is not None:
            cand["extreme_f"] = coast_extreme(mode, cand["extreme_f"], reading)
            cand["n_post"] += 1
    return closed


def coast_thermostat_event(
    windows: dict[str, dict[str, Any]],
    *,
    now: datetime,
    reading: float | None,
    changed: bool,
    old_dir: str | None,
    new_dir: str | None,
    seconds_since_setpoint_change: float | None,
    off_setpoint_f: float | None,
) -> list[CoastClosed]:
    """Apply one thermostat state change to the coast windows (Issue #1233).

    Ends: a setpoint/mode change (``setpoint_change``) or the equipment starting again (``next_on``) closes
    every window and the reading that comes with it is NOT coast; any other change is a post-off reading.
    Start: a real heating/cooling -> not-running edge that nobody commanded - no setpoint/mode change in
    this event nor within ``THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S`` before it (``coast_stop_was_commanded``)
    - opens a window for ``old_dir`` with ``reading`` as its baseline.
    """
    if changed:
        closed = coast_close_all(windows, COAST_ENDED_SETPOINT_CHANGE, now)
    elif new_dir is not None and new_dir != old_dir:
        closed = coast_close_all(windows, COAST_ENDED_NEXT_ON, now)
    else:
        closed = coast_feed(windows, now, reading)
    if (
        old_dir is not None
        and new_dir is None
        and not changed
        and not coast_stop_was_commanded(seconds_since_setpoint_change)
        and reading is not None
    ):
        windows[old_dir] = {
            "ts": now.isoformat(),
            "off_dt": now,
            "off_indoor_f": reading,
            "extreme_f": reading,
            "setpoint_f": off_setpoint_f,
            "n_post": 0,
        }
    return closed


def summarize_coast_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Median coast and median distance past the setpoint over the last K windows."""
    window = samples[-THERMAL_COAST_ESTIMATOR_WINDOW:]
    if not window:
        return {"coast_f": None, "past_setpoint_f": None}
    pasts = [s["past_setpoint_f"] for s in window if s.get("past_setpoint_f") is not None]
    return {
        "coast_f": round(median(s["coast_f"] for s in window), 2),
        "past_setpoint_f": round(median(pasts), 2) if pasts else None,
    }


def apply_coast_samples(cache: dict[str, Any], mode: str, new_samples: list[dict[str, Any]]) -> int:
    """Fold coast samples into a thermal_model_cache dict. Returns the number of NEW samples added."""
    merged, added = merge_swing_samples(cache.get(f"coast_{mode}_samples"), new_samples, THERMAL_COAST_ESTIMATOR_WINDOW)
    summary = summarize_coast_samples(merged)
    cache[f"coast_{mode}_samples"] = merged
    cache[f"coast_{mode}_f"] = summary["coast_f"]
    cache[f"coast_{mode}_past_setpoint_f"] = summary["past_setpoint_f"]
    cache[f"observation_count_coast_{mode}"] = int(cache.get(f"observation_count_coast_{mode}", 0) or 0) + added
    return added


# ---------------------------------------------------------------------------
# Comfort-deviation tolerance (Issue #1258)
# ---------------------------------------------------------------------------
# The room is allowed to sit up to the thermostat's own swing past the band edge while the thermostat is
# running in the matching direction: that is the configured hysteresis, not a failure to hold the band.

SWING_SOURCE_LEARNED = "learned"
SWING_SOURCE_DEFAULT = "default"
SWING_SOURCE_CAPPED_SUFFIX = " (capped)"

# hvac modes in which the thermostat holds each direction (owner rule, 2026-11-02): Off, the opposite mode
# or an unknown mode do NOT hold the room by hysteresis, so only the fixed floor applies there.
_HOLDING_MODES: dict[str, frozenset[str]] = {
    "heat": frozenset({"heat", "heat_cool", "auto"}),
    "cool": frozenset({"cool", "heat_cool", "auto"}),
}


def swing_tolerance(swing_f: Any) -> tuple[float, str]:
    """``(tolerance_f, source)`` for one direction.

    ``min(max(swing or 1.5 F default, 0.5 F floor), 2.5 F cap)``. ``source`` is ``"learned"`` / ``"default"``,
    with ``" (capped)"`` appended when the cap bit (owner decision 2026-11-02: a mis-learned large swing must not
    silence the comfort alarms). Only the comfort tolerance is capped, never the learned swing itself.
    """
    if isinstance(swing_f, (int, float)) and not isinstance(swing_f, bool) and swing_f > 0:
        raw, source = max(COMFORT_DEVIATION_FLOOR_F, float(swing_f)), SWING_SOURCE_LEARNED
    else:
        raw, source = max(COMFORT_DEVIATION_FLOOR_F, THERMAL_SWING_DEFAULT_F), SWING_SOURCE_DEFAULT
    if raw > COMFORT_SWING_TOLERANCE_MAX_F:
        return COMFORT_SWING_TOLERANCE_MAX_F, source + SWING_SOURCE_CAPPED_SUFFIX
    return raw, source


def thermostat_holds_direction(direction: str, thermostat_mode: Any) -> bool:
    """True when the thermostat's own mode holds the room in ``direction`` ("heat" or "cool")."""
    return isinstance(thermostat_mode, str) and thermostat_mode.lower() in _HOLDING_MODES.get(direction, frozenset())


def within_thermostat_hysteresis(
    indoor_f: float,
    floor_f: float | None,
    ceiling_f: float | None,
    swing_heat_f: Any,
    swing_cool_f: Any,
    thermostat_mode: Any,
) -> bool:
    """True when ``indoor_f`` is out of band only by the thermostat's own learned swing (Issue #1258).

    Under the floor by no more than the heat tolerance while the thermostat is in heat/heat_cool/auto, or over
    the ceiling by no more than the cool tolerance while it is in cool/heat_cool/auto. In band, beyond the
    swing, or with the thermostat Off / in the opposite mode / unknown: False (the caller's fixed 0.5 F rule
    stays). All values are internal degrees F; the swing is a delta. The boundary is inclusive (a room exactly
    one swing out is still the thermostat's own hysteresis).
    """
    eps = 1e-9
    if floor_f is not None and indoor_f < floor_f:
        tol, _ = swing_tolerance(swing_heat_f)
        return thermostat_holds_direction("heat", thermostat_mode) and (floor_f - indoor_f) <= tol + eps
    if ceiling_f is not None and indoor_f > ceiling_f:
        tol, _ = swing_tolerance(swing_cool_f)
        return thermostat_holds_direction("cool", thermostat_mode) and (indoor_f - ceiling_f) <= tol + eps
    return False
