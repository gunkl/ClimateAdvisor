"""Learning engine for Climate Advisor.

Tracks human compliance with suggestions, HVAC runtime patterns, and
environmental outcomes to generate adaptive improvement suggestions.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from homeassistant.util import dt as dt_util

from .const import (
    COMPLIANCE_THRESHOLD_LOW,
    LEARNING_DB_FILE,
    MIN_DATA_POINTS_FOR_SUGGESTION,
    MIN_WEATHER_BIAS_OBSERVATIONS,
    OBS_TYPE_HVAC_HEAT,
    REJECT_ABANDONED,
    REJECT_HVAC_RUN_TOO_SHORT,
    REJECT_HVAC_SIGNAL_BELOW_RESOLUTION,
    REJECT_NO_K_PASSIVE,
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_BOUNDS,
    REJECT_OLS_WRONG_SIGN,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_BLOCKS,
    REJECT_TOO_FEW_SAMPLES,
    THERMAL_BLOCK_OLS_BLOCK_MINUTES,
    THERMAL_BLOCK_OLS_MIN_BLOCKS,
    THERMAL_HVAC_MIN_SIGNAL_F,
    THERMAL_HVAC_RUN_MIN_MINUTES,
    THERMAL_K_ACTIVE_COOL_MAX,
    THERMAL_K_ACTIVE_COOL_MIN,
    THERMAL_K_ACTIVE_HEAT_MAX,
    THERMAL_K_ACTIVE_HEAT_MIN,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
    THERMAL_K_SOLAR_MAX_F_PER_HR,
    THERMAL_LEGACY_OBS_WEIGHT,
    THERMAL_MIN_DECAY_SAMPLES,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_MIN_R_SQUARED,
    THERMAL_OBS_CAP,
    THERMAL_RATE_HALF_LIFE_DAYS,
    THERMAL_SOLAR_FACTOR_MIN_RANGE,
    THERMAL_SWING_CONF_HIGH,
    THERMAL_SWING_CONF_LOW,
    THERMAL_SWING_CONF_MEDIUM,
    THERMAL_SWING_DEFAULT_F,
    THERMAL_SWING_DEFINITION_VERSION,
    WEATHER_BIAS_MAX_OBS,
)
from .storage_paths import migrate_legacy_storage_file, resolve_entry_scoped_path
from .swing import (
    SWING_DEFINITION_TEXT,
    apply_coast_samples,
    apply_swing_samples,
    record_swing_audit,
    refresh_swing_cache,
    reset_swing_cache,
    swing_debug_summary,
)
from .temperature import format_temp_delta
from .thermal_confidence import (
    drop_stored_grades,
    explain_physics_eligibility,
    grade_hvac_confidence,
    grade_passive_confidence,
    grade_solar_confidence,
)

_LOGGER = logging.getLogger(__name__)

# Issue #1110: which shutdown path persisted the restart-cause diagnostics.
SHUTDOWN_KIND_UNLOAD = "unload"  # config-entry unload (zone reload / repair / removal)
SHUTDOWN_KIND_HA_STOP = "ha_stop"  # EVENT_HOMEASSISTANT_STOP (real HA restart/stop)
SHUTDOWN_KINDS = frozenset({SHUTDOWN_KIND_UNLOAD, SHUTDOWN_KIND_HA_STOP})


class ObsSample(TypedDict):
    timestamp: str
    indoor_temp_f: float
    outdoor_temp_f: float
    elapsed_minutes: float


class PendingObservation(TypedDict):
    obs_type: str
    obs_id: str
    start_time: str
    status: str
    samples: list
    flags_at_start: dict
    schema_version: int


class ThermalRejectionEvent(TypedDict):
    obs_type: str
    reason_code: str
    n_samples: int
    n_required: int
    r_squared: float | None
    r_squared_required: float | None
    delta_t_f: float | None
    delta_t_required: float | None
    elapsed_minutes: int | None
    timestamp: str


# Hard cap on daily records — 2 years of data is the absolute maximum.
# The 90-day rolling trim in record_day() normally keeps the list much shorter;
# this cap protects against edge cases (e.g., trim logic bypassed, time jumps).
MAX_DAILY_RECORDS = 730


@dataclass
class ThermalObservation:
    """A single observed HVAC session — v2 two-parameter physics model."""

    event_id: str
    timestamp: str  # ISO8601 — when committed
    date: str  # YYYY-MM-DD — for rolling trim
    hvac_mode: str  # "heat" / "cool" / "fan_only"
    session_minutes: float
    start_indoor_f: float
    end_indoor_f: float
    peak_indoor_f: float
    start_outdoor_f: float
    avg_outdoor_f: float
    delta_t_avg: float
    k_passive: float  # hr⁻¹, always negative
    k_active: float | None  # °F/hr; None for fan_only sessions
    passive_baseline_rate: float
    r_squared_passive: float
    r_squared_active: float | None  # None for fan_only
    sample_count_pre: int
    sample_count_active: int
    sample_count_post: int
    confidence_grade: str  # "low" | "medium" | "high"
    schema_version: int = 2


# ---------------------------------------------------------------------------
# Module-level physics functions (importable for tests)
# ---------------------------------------------------------------------------


def _grade_passive_confidence(cache: dict) -> str:
    """Compute confidence tier for k_passive based on combined observation count.

    Only observation types that write to k_passive are counted:
      - passive_decay, hvac_heat, hvac_cool

    Excluded types (they write to different cache fields):
      - observation_count_vent_window / observation_count_vent_fan → write to
        k_vent_window / k_vent_fan only (guarded by _envelope_modes check in
        learning.py commit path)
      - observation_count_solar → writes to k_solar only

    Issue #587: fan_only_decay's observation_count_fan_only was previously folded in
    here as an approximation — fan-only-no-windows genuinely resembles passive decay
    with a small forced-convection perturbation, so it was treated as a close proxy
    for envelope decay. That type is retired outright, and its count is DROPPED here
    rather than replaced with observation_count_vent_window/observation_count_vent_fan:
    window-open decay (and especially fan-driven ventilation) are NOT close proxies for
    envelope-only decay — they measure a materially larger, different rate (the same
    premise behind the over-prediction concern that removed the forecast gate bridge in Issue #1512).
    Folding either into *envelope* confidence would be scientifically wrong in a way
    the original fan_only inclusion was not. This is a real, intentional behavior
    change: houses previously getting confidence credit from fan_only_decay will show
    slightly lower confidence_k_passive after upgrade — called out explicitly in the
    CHANGELOG as deliberate, not a silent regression.
    """
    return grade_passive_confidence(cache)


def _ewma_update_field(cache: dict, field_name: str, value: float, alpha: float) -> None:
    """Blend value into cache[field_name] via EWMA, initializing on first observation.

    Factors out the `if cache.get(field) is None: cache[field] = value; else: cache[field]
    = (1-alpha)*cache[field] + alpha*value` pattern that would otherwise be repeated per
    field (Issue #587) — small, but the same class of duplication the vent-split trigger/
    abort logic avoids. Immediately reusable by k_passive/k_active_heat/k_active_cool's
    own EWMA blocks too if touched later (not required for this change).
    """
    if cache.get(field_name) is None:
        cache[field_name] = value
    else:
        cache[field_name] = (1.0 - alpha) * cache[field_name] + alpha * value


_CHART_LOG_FIT_SOURCES = ("endpoint", "block_ols", "duty_balance")


def _same_chart_log_fit(a: dict, b: dict) -> bool:
    """True when ``a`` and ``b`` are chart_log fits of the same window (Issue #1476).

    Fits carry the window's first and last chart_log stamps; older fits (before #1476) did not, so
    those match on the fit itself: same mode, estimator, k and R^2 (a refit of one window reproduces
    them exactly, while two different windows never share both to full float precision).
    """
    if a.get("source") not in _CHART_LOG_FIT_SOURCES or a.get("hvac_mode") != b.get("hvac_mode"):
        return False
    if a.get("window_start") and b.get("window_start"):
        return a["window_start"] == b["window_start"] and a.get("window_end") == b.get("window_end")
    return (
        a.get("k_passive") is not None
        and a.get("source") == b.get("source")
        and a.get("k_passive") == b.get("k_passive")
        and a.get("r_squared") == b.get("r_squared")
    )


def _replay_envelope_k_passive(obs_list: list[dict]) -> float | None:
    """EWMA ``k_passive`` as ``_update_thermal_model_cache`` builds it from these observations, from empty."""
    alpha_map = {"high": 0.3, "medium": 0.15, "low": 0.05}
    cache: dict = {}
    for o in obs_list:
        k = o.get("k_passive")
        if isinstance(k, (int, float)) and o.get("hvac_mode") not in ("vent_window", "vent_fan"):
            _ewma_update_field(cache, "k_passive", k, alpha_map.get(o.get("confidence_grade", "low"), 0.05))
    return cache.get("k_passive")


def _apply_two_param_solar_update(cache: dict, obs: dict, grade: str) -> None:
    """Blend a 2-param vent-split commit's separated k_solar term into the cache.

    Shared by both vent_window_decay and vent_fan_decay commits (Issue #587) — factored
    out of what was previously written once for the single "ventilated" branch, now
    shared across both split branches rather than copy-pasted.
    """
    k_sol = obs.get("k_solar")
    if k_sol is not None and obs.get("two_param"):
        alpha_sol = {"high": 0.3, "medium": 0.15, "low": 0.05}.get(grade, 0.05)
        _ewma_update_field(cache, "k_solar", k_sol, alpha_sol)


def _new_thermal_cache() -> dict:
    """A fresh, empty thermal_model_cache (the one place its key set is spelled out)."""
    return {
        "k_passive": None,
        "k_active_heat": None,
        "k_active_cool": None,
        "k_vent_window": None,
        "k_vent_fan": None,
        "k_solar": None,
        "solar_phase_offset_h": None,
        "solar_phase_offset_last_obs_date": None,
        "solar_phase_offset_ac_h": None,
        "solar_phase_offset_ac_obs_count": 0,
        "solar_phase_offset_ac_last_obs_date": None,
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "observation_count_passive": 0,
        "observation_count_vent_window": 0,
        "observation_count_vent_fan": 0,
        "observation_count_solar": 0,
        "swing_heat_f": None,
        "swing_cool_f": None,
        "observation_count_swing_heat": 0,
        "observation_count_swing_cool": 0,
        "swing_definition_version": THERMAL_SWING_DEFINITION_VERSION,
        "last_observation_date": None,
        "avg_r_squared_passive": None,
        "rate_prior": {},
    }


def _grade_swing_confidence(count: int) -> str:
    """Compute confidence tier for thermostat swing based on observation count."""
    if count < THERMAL_SWING_CONF_LOW:
        return "none"
    if count < THERMAL_SWING_CONF_MEDIUM:
        return "low"
    if count < THERMAL_SWING_CONF_HIGH:
        return "medium"
    return "high"


def _resolve_solar_phase_offset(cache: dict) -> float:
    """Return the best current solar phase offset.

    A parameter is stale if its last_obs_date is absent or older than THERMAL_PARAM_STALE_DAYS.
    Stale home-specific measurements are always preferred over a generic default prior —
    the default is only returned when nothing has ever been observed.

    Priority:
      1. Fresh primary (passive-decay fit, within THERMAL_PARAM_STALE_DAYS)
      2. Fresh secondary (AC duty cycle, within THERMAL_PARAM_STALE_DAYS, obs >= min)
      3. Stale primary (EWMA value preserved in cache — better than a generic prior)
      4. Stale secondary (obs >= min — better than a generic prior)
      5. Default prior (THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT) — only when nothing learned
    """
    from datetime import date as _date

    from .const import THERMAL_PARAM_STALE_DAYS, THERMAL_SOLAR_PHASE_AC_MIN_OBS, THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT

    def _is_stale(last_obs_date_str: str | None) -> bool:
        if last_obs_date_str is None:
            return True
        try:
            return (_date.today() - _date.fromisoformat(last_obs_date_str)).days > THERMAL_PARAM_STALE_DAYS
        except ValueError, TypeError:
            return True

    primary = cache.get("solar_phase_offset_h")
    secondary = cache.get("solar_phase_offset_ac_h")
    ac_obs = cache.get("solar_phase_offset_ac_obs_count", 0)
    secondary_qualified = secondary is not None and ac_obs >= THERMAL_SOLAR_PHASE_AC_MIN_OBS

    # Fresh data first
    if primary is not None and not _is_stale(cache.get("solar_phase_offset_last_obs_date")):
        return float(primary)
    if secondary_qualified and not _is_stale(cache.get("solar_phase_offset_ac_last_obs_date")):
        return float(secondary)

    # Stale home-specific data beats a generic prior
    if primary is not None:
        return float(primary)
    if secondary_qualified:
        return float(secondary)

    # Nothing has ever been learned — use configured default
    return float(THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT)


def _smooth_temps(temps: list[float]) -> list[float]:
    """Apply 3-sample centered moving average to smooth 1-degF integer staircase noise.

    Edge samples are unchanged (no padding artifacts).
    """
    if len(temps) < 3:
        return list(temps)
    smoothed = [temps[0]]
    for i in range(1, len(temps) - 1):
        smoothed.append((temps[i - 1] + temps[i] + temps[i + 1]) / 3.0)
    smoothed.append(temps[-1])
    return smoothed


def compute_k_passive(
    post_samples: list[dict],
    pre_samples: list[dict] | None = None,
    min_samples: int | None = None,
) -> tuple[float | None, float, str | None]:
    """Estimate envelope decay rate (k_passive, hr⁻¹) from HVAC-off sample windows.

    Uses OLS regression forced through origin:
        rate_i = k_passive * delta_i
    where:
        rate_i = (T_indoor[i+1] - T_indoor[i]) / dt_hours
        delta_i = midpoint(T_indoor - T_outdoor) over interval

    Args:
        post_samples: Post-heat/cool samples (list of dicts with
            timestamp, indoor_temp_f, outdoor_temp_f, elapsed_minutes).
        pre_samples: Optional pre-heat samples (also HVAC-off), used to
            increase regression data density.
        min_samples: Override OLS floor (default: THERMAL_MIN_POST_HEAT_SAMPLES).
            Set to THERMAL_MIN_DECAY_SAMPLES for short rolling-window decay obs.

    Returns:
        Tuple of (k_passive, r_squared, reason_code). k_passive is None if regression
        fails validation (too few points, wrong sign, or bad R²). reason_code is one of
        the REJECT_* constants when k_passive is None, else None on success.
    """
    # Process each window independently to avoid spurious rates at the pre→post boundary.
    # The boundary between pre-heat and post-heat spans the active heating phase where
    # indoor temp rises — computing a rate across that gap yields a large positive spike
    # that corrupts the OLS estimate.
    _min_s = min_samples if min_samples is not None else THERMAL_MIN_POST_HEAT_SAMPLES
    windows: list[list[dict]] = []
    if pre_samples:
        windows.append(list(pre_samples))
    windows.append(list(post_samples))

    total_samples = sum(len(w) for w in windows)
    if total_samples < _min_s + 1:
        return None, 0.0, REJECT_TOO_FEW_SAMPLES

    rates: list[float] = []
    deltas: list[float] = []
    for window in windows:
        if len(window) < 2:
            continue
        indoor_raw = [s["indoor_temp_f"] for s in window]
        outdoor = [s["outdoor_temp_f"] for s in window]
        elapsed = [s["elapsed_minutes"] for s in window]
        indoor = _smooth_temps(indoor_raw)
        for i in range(len(indoor) - 1):
            dt_hours = (elapsed[i + 1] - elapsed[i]) / 60.0
            if dt_hours <= 0:
                continue
            rate = (indoor[i + 1] - indoor[i]) / dt_hours
            delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor[i] + outdoor[i + 1]) / 2.0)
            rates.append(rate)
            deltas.append(delta)

    if len(rates) < _min_s:
        return None, 0.0, REJECT_TOO_FEW_SAMPLES

    sum_rd = sum(r * d for r, d in zip(rates, deltas, strict=False))
    sum_d2 = sum(d * d for d in deltas)
    if sum_d2 == 0:
        return None, 0.0, REJECT_SMALL_DELTA

    k_p = sum_rd / sum_d2

    # Validate sign and bounds
    if k_p > 0:
        _LOGGER.debug(
            "compute_k_passive: wrong sign k_p=%.5f (must be < 0) n=%d",
            k_p,
            len(rates),
        )
        return None, 0.0, REJECT_OLS_WRONG_SIGN
    if not (THERMAL_K_PASSIVE_MIN <= k_p <= THERMAL_K_PASSIVE_MAX):
        _LOGGER.debug(
            "compute_k_passive: out of bounds k_p=%.5f (must be in [%.3f, %.3f]) n=%d",
            k_p,
            THERMAL_K_PASSIVE_MIN,
            THERMAL_K_PASSIVE_MAX,
            len(rates),
        )
        return None, 0.0, REJECT_OLS_BOUNDS

    # R² (vs forced-through-origin model)
    ss_res = sum((r - k_p * d) ** 2 for r, d in zip(rates, deltas, strict=False))
    ss_tot = sum(r * r for r in rates)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    r_squared = max(0.0, r_squared)

    if r_squared < THERMAL_MIN_R_SQUARED:
        return None, r_squared, REJECT_OLS_BAD_FIT

    return k_p, r_squared, None


def compute_k_passive_blocks(
    window_entries: list[dict],
    block_minutes: int = THERMAL_BLOCK_OLS_BLOCK_MINUTES,
    min_blocks: int = THERMAL_BLOCK_OLS_MIN_BLOCKS,
) -> tuple[float | None, float, str | None]:
    """Block-averaged 1-param OLS estimator for k_passive using chart_log window data.

    Aggregates chart_log entries into block_minutes-wide blocks, averages indoor/outdoor
    temp within each block, then runs compute_k_passive on the block averages. This
    reduces 1°F thermostat quantization noise via CLT before OLS, giving a meaningful R².

    Args:
        window_entries: List of chart_log entry dicts with keys "ts" (ISO string),
            "indoor" (°F float), and "outdoor" (°F float).
        block_minutes: Width of each averaging block in minutes (default 60).
        min_blocks: Minimum number of blocks required (default 6, i.e. ≥6h window).

    Returns:
        Tuple of (k_passive, r_squared, reason_code) — same shape as compute_k_passive.
        reason_code is REJECT_TOO_FEW_BLOCKS when fewer than min_blocks remain after
        aggregation, or a downstream REJECT_* code if compute_k_passive itself rejects.
    """
    if not window_entries:
        return None, 0.0, REJECT_TOO_FEW_SAMPLES

    # Parse t0 from the first entry's ISO timestamp
    try:
        t0_str = window_entries[0]["ts"]
        t0 = datetime.fromisoformat(t0_str)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
        t0_epoch = t0.timestamp()
    except ValueError, KeyError, TypeError:
        return None, 0.0, REJECT_TOO_FEW_SAMPLES

    # Group entries into block_minutes-wide blocks
    blocks: dict[int, list[dict]] = {}
    for entry in window_entries:
        try:
            ts_str = entry["ts"]
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            elapsed_s = ts.timestamp() - t0_epoch
            block_idx = int((elapsed_s / 60.0) // block_minutes)
            blocks.setdefault(block_idx, []).append(entry)
        except ValueError, KeyError, TypeError:
            continue

    # Build synthetic samples from block averages
    synthetic_samples: list[dict] = []
    for block_idx in sorted(blocks.keys()):
        block_entries = blocks[block_idx]
        if len(block_entries) < 2:
            continue
        mean_indoor = sum(float(e["indoor"]) for e in block_entries) / len(block_entries)
        mean_outdoor = sum(float(e["outdoor"]) for e in block_entries) / len(block_entries)
        mean_elapsed = sum(
            (
                datetime.fromisoformat(e["ts"])
                .replace(
                    tzinfo=UTC
                    if datetime.fromisoformat(e["ts"]).tzinfo is None
                    else datetime.fromisoformat(e["ts"]).tzinfo
                )
                .timestamp()
                - t0_epoch
            )
            / 60.0
            for e in block_entries
        ) / len(block_entries)
        synthetic_samples.append(
            {
                "indoor_temp_f": mean_indoor,
                "outdoor_temp_f": mean_outdoor,
                "elapsed_minutes": mean_elapsed,
            }
        )

    if len(synthetic_samples) < min_blocks:
        return None, 0.0, REJECT_TOO_FEW_BLOCKS

    return compute_k_passive(synthetic_samples, min_samples=min_blocks - 1)


def _interp_outdoor(times_hours: list[float], outdoors: list[float], t: float) -> float:
    """Piecewise-linear interpolation of outdoor temp at time t (hours since window start).

    Clamps to the first/last sample when t falls outside the window's time range.
    Windows are short (at most a few dozen samples), so a linear scan is simple and
    fast enough — no need for binary search.
    """
    if t <= times_hours[0]:
        return outdoors[0]
    if t >= times_hours[-1]:
        return outdoors[-1]
    for i in range(len(times_hours) - 1):
        t0, t1 = times_hours[i], times_hours[i + 1]
        if t0 <= t <= t1:
            if t1 == t0:
                return outdoors[i]
            frac = (t - t0) / (t1 - t0)
            return outdoors[i] + frac * (outdoors[i + 1] - outdoors[i])
    return outdoors[-1]


def _integrate_indoor_rk4(
    t_start_indoor: float,
    times_hours: list[float],
    outdoors: list[float],
    k: float,
    dt_hours: float,
    n_steps: int,
) -> float:
    """RK4 forward-integrate dT/dt = k*(T - T_out(t)) from t=0 to t=dt_hours.

    T_out(t) is the piecewise-linear interpolation of the window's real per-sample
    outdoor trace (_interp_outdoor) rather than a constant window-average — this is
    the mechanism behind Defect A's time-varying-outdoor bias fix (Issue #587).
    """
    if n_steps < 1:
        n_steps = 1
    h = dt_hours / n_steps
    t = 0.0
    temp = t_start_indoor
    for _ in range(n_steps):

        def _deriv(tt: float, tt_indoor: float) -> float:
            return k * (tt_indoor - _interp_outdoor(times_hours, outdoors, tt))

        k1 = _deriv(t, temp)
        k2 = _deriv(t + h / 2.0, temp + h / 2.0 * k1)
        k3 = _deriv(t + h / 2.0, temp + h / 2.0 * k2)
        k4 = _deriv(t + h, temp + h * k3)
        temp += (h / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        t += h
    return temp


def compute_k_passive_endpoint(
    window: list[dict],
    k_min: float,
    k_max: float,
) -> float | None:
    """Bisection root-find k using the window's real per-sample outdoor trace (Issue #587).

    Fixes Defect A: the prior closed-form endpoint formula
    (``ratio = (T_end - T_out_avg) / (T_start - T_out_avg); k = ln(ratio) / dt``) collapsed
    the window's outdoor readings into a single scalar average, assuming outdoor
    temperature held constant across the whole window. It doesn't — outdoor swings
    through the day — and this produced k values 1.3x-1.9x higher in magnitude than the
    ODE-consistent true rate on real overnight windows (confirmed via independent RK4
    numerical integration against the same raw samples).

    Root-finds (bisection) the k value whose RK4 forward-integration of
    dT/dt = k*(T - T_out(t)) — using the window's real per-sample outdoor trace,
    piecewise-linearly interpolated between samples (_interp_outdoor) — reproduces the
    observed T_end starting from T_start.

    Stays bookend-only on the *indoor* side (#141's blip-immunity design constraint,
    reaffirmed for this fix): only T_start/T_end (window[0]["indoor"]/window[-1]["indoor"])
    ever feed the root-find target — no interior indoor samples are read, so a mid-window
    indoor sensor blip cannot skew the result. Only the *outdoor* series gains full
    per-sample resolution. On a 2-sample window, piecewise-linear interpolation over the
    2 outdoor points *is* the whole trace, so the result naturally matches the old
    closed-form answer in that degenerate case.

    Args:
        window: chart_log window entries — dicts with "ts" (ISO string), "indoor" (°F),
            "outdoor" (°F), ordered by time, at least 2 entries.
        k_min: Lower bracket bound for bisection (e.g. THERMAL_K_PASSIVE_MIN).
        k_max: Upper bracket bound for bisection (e.g. THERMAL_K_PASSIVE_MAX).

    Returns:
        The root-found k (hr⁻¹), or None if no root exists in [k_min, k_max] — the same
        regime-filter role the old ratio<=0/ratio>=1.0 reject played: an unreachable
        T_end given the real outdoor path means this window doesn't fit a single-k decay
        model within the physically valid range, so the caller should fall back to
        block-OLS (Estimator B) instead.
    """
    if len(window) < 2:
        return None

    try:
        ts_list = [datetime.fromisoformat(s["ts"]) for s in window]
    except ValueError, KeyError, TypeError:
        return None
    ts_list = [ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts for ts in ts_list]
    t0_epoch = ts_list[0].timestamp()
    times_hours = [(ts.timestamp() - t0_epoch) / 3600.0 for ts in ts_list]
    try:
        outdoors = [float(s["outdoor"]) for s in window]
        t_start = float(window[0]["indoor"])
        t_end = float(window[-1]["indoor"])
    except KeyError, TypeError, ValueError:
        return None

    dt_hours = times_hours[-1]
    if dt_hours <= 0:
        return None

    # At least 4 RK4 sub-steps per sample interval, floor of 40 total steps so even a
    # 2-sample window integrates accurately.
    n_steps = max(40, (len(window) - 1) * 4)

    def _residual(k: float) -> float:
        return _integrate_indoor_rk4(t_start, times_hours, outdoors, k, dt_hours, n_steps) - t_end

    f_lo = _residual(k_min)
    f_hi = _residual(k_max)

    if f_lo == 0.0:
        return k_min
    if f_hi == 0.0:
        return k_max
    if (f_lo > 0) == (f_hi > 0):
        # No sign change across the bracket — no single-k decay model in the valid range
        # reproduces this window's observed T_end given its real outdoor path.
        return None

    lo, hi = k_min, k_max
    flo = f_lo
    for _ in range(60):
        mid = (lo + hi) / 2.0
        fmid = _residual(mid)
        if fmid == 0.0:
            return mid
        if (flo > 0) == (fmid > 0):
            lo = mid
            flo = fmid
        else:
            hi = mid
    return (lo + hi) / 2.0


def compute_k_env_solar(
    samples: list[dict],
    min_samples: int = 4,
) -> tuple[float | None, float | None, float | None]:
    """Two-parameter OLS: dT/dt = k_env*(T_in-T_out) + k_solar*solar_factor.

    Separates ventilation-driven heat transfer (k_env) from solar gain (k_solar)
    in ventilated_decay observations.  Uses the same sign convention as compute_k_passive:
    delta = midpoint(T_in - T_out), rate = k_env * delta + k_solar * sf.
    k_env < 0 means the envelope/ventilation cools the home (indoor > outdoor → rate < 0).

    Returns (k_env, k_solar, r_squared) or (None, None, None) on failure.
    k_env expected range: [THERMAL_K_PASSIVE_MIN, 0.001] (matches k_passive convention)
    k_solar expected range: [0.0, 8.0] °F/hr (positive; solar warms, not cools)

    Falls back to (None, None, None) if:
    - fewer than min_samples pairs are available
    - solar_factor range across samples is < THERMAL_SOLAR_FACTOR_MIN_RANGE (collinear / no solar signal)
    - OLS matrix is near-singular (det ≈ 0)
    """
    pairs = []
    for i in range(len(samples) - 1):
        s0, s1 = samples[i], samples[i + 1]
        t0_in = s0.get("indoor_temp_f")
        t1_in = s1.get("indoor_temp_f")
        t0_out = s0.get("outdoor_temp_f")
        t1_out = s1.get("outdoor_temp_f")
        sf0 = s0.get("solar_factor", 0.0)
        sf1 = s1.get("solar_factor", 0.0)
        e0 = s0.get("elapsed_minutes")
        e1 = s1.get("elapsed_minutes")
        if any(v is None for v in (t0_in, t1_in, t0_out, t1_out, e0, e1)):
            continue
        dt_hours = (e1 - e0) / 60.0
        if dt_hours <= 0:
            continue
        rate = (t1_in - t0_in) / dt_hours
        # delta = midpoint(T_in - T_out): positive when indoor warmer than outdoor
        delta = ((t0_in - t0_out) + (t1_in - t1_out)) / 2.0
        sf = (sf0 + sf1) / 2.0
        pairs.append((rate, delta, sf))

    if len(pairs) < min_samples:
        return None, None, None

    rates = [p[0] for p in pairs]
    deltas = [p[1] for p in pairs]
    sfs = [p[2] for p in pairs]

    # Collinearity guard: need sufficient solar_factor variation for 2-param separation
    sf_range = max(sfs) - min(sfs)
    if sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE:
        return None, None, None  # caller falls back to 1-param

    # 2×2 normal equations (no intercept): [x1x1 x1x2; x1x2 x2x2] @ [k_env; k_solar] = [x1y; x2y]
    x1x1 = sum(d * d for d in deltas)
    x2x2 = sum(s * s for s in sfs)
    x1x2 = sum(d * s for d, s in zip(deltas, sfs, strict=False))
    x1y = sum(d * r for d, r in zip(deltas, rates, strict=False))
    x2y = sum(s * r for s, r in zip(sfs, rates, strict=False))
    det = x1x1 * x2x2 - x1x2 * x1x2
    if abs(det) < 1e-12:
        return None, None, None  # numerical collinearity

    k_env = (x2x2 * x1y - x1x2 * x2y) / det
    k_solar = (x1x1 * x2y - x1x2 * x1y) / det

    # R² (mean-centered, 2-param)
    mean_r = sum(rates) / len(rates)
    ss_res = sum((r - k_env * d - k_solar * s) ** 2 for r, d, s in zip(rates, deltas, sfs, strict=False))
    ss_tot = sum((r - mean_r) ** 2 for r in rates)
    r_squared = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    return k_env, k_solar, r_squared


def compute_k_active(
    active_samples: list[dict],
    k_passive: float,
    session_mode: str,
) -> tuple[float | None, float]:
    """Estimate HVAC contribution rate (k_active, °F/hr) from active-phase samples.

    For each active interval:
        k_active_i = rate_i - k_passive * delta_i

    Args:
        active_samples: Samples during active heating/cooling phase.
        k_passive: Validated k_passive from the same event.
        session_mode: "heat", "cool", or "fan_only".

    Returns:
        Tuple of (k_active, r_squared). k_active is None for fan_only or
        when fewer than 2 active samples are available.
    """
    if session_mode == "fan_only" or len(active_samples) < 2:
        return None, 0.0

    indoor_raw = [s["indoor_temp_f"] for s in active_samples]
    outdoor = [s["outdoor_temp_f"] for s in active_samples]
    elapsed = [s["elapsed_minutes"] for s in active_samples]

    indoor = _smooth_temps(indoor_raw)

    k_actives: list[float] = []
    rates: list[float] = []
    for i in range(len(indoor) - 1):
        dt_hours = (elapsed[i + 1] - elapsed[i]) / 60.0
        if dt_hours <= 0:
            continue
        rate = (indoor[i + 1] - indoor[i]) / dt_hours
        delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor[i] + outdoor[i + 1]) / 2.0)
        k_a_i = rate - k_passive * delta
        k_actives.append(k_a_i)
        rates.append(rate)

    if not k_actives:
        return None, 0.0

    k_active = sum(k_actives) / len(k_actives)

    # Sanity bounds
    if session_mode == "heat" and not (THERMAL_K_ACTIVE_HEAT_MIN <= k_active <= THERMAL_K_ACTIVE_HEAT_MAX):
        return None, 0.0
    if session_mode == "cool" and not (THERMAL_K_ACTIVE_COOL_MIN <= k_active <= THERMAL_K_ACTIVE_COOL_MAX):
        return None, 0.0

    var_residual = sum((k_a - k_active) ** 2 for k_a in k_actives)
    var_total = sum((r - sum(rates) / len(rates)) ** 2 for r in rates)
    r_squared = 1.0 - (var_residual / var_total) if var_total > 0 else 0.0
    r_squared = max(0.0, r_squared)

    return k_active, r_squared


def compute_k_solar(
    samples: list[dict],
    k_passive: float,
) -> tuple[float | None, float]:
    """Estimate solar-gain rate (k_solar, °F/hr) from solar_gain window samples.

    Mirrors compute_k_active's per-interval subtraction pattern: the raw observed
    warming rate over each interval already includes ordinary conduction toward
    (or away from) outdoor temperature — that contribution, `k_passive * delta`,
    must be subtracted before what remains can be credited to solar gain (Issue
    #587, Defect B). Without this subtraction, a purely-conductive warming window
    (e.g. a warm outdoor with zero solar physics) is misreported as solar gain.

    For each interval:
        k_solar_i = rate_i - k_passive * delta_i

    Args:
        samples: Solar-gain window samples (indoor_temp_f, outdoor_temp_f,
            elapsed_minutes), at least 2.
        k_passive: Cached k_passive (envelope decay rate, hr⁻¹, always negative)
            from the current best thermal model — the subtrahend, never itself
            committed as part of this observation.

    Returns:
        Tuple of (k_solar, r_squared). k_solar is None when fewer than 2 samples
        are available or all intervals have non-positive dt. Bounded to
        [0.0, THERMAL_K_SOLAR_MAX_F_PER_HR] — a negative result (i.e. subtraction
        would credit "negative solar gain") is clamped to 0.0.
    """
    if len(samples) < 2:
        return None, 0.0

    indoor = [s["indoor_temp_f"] for s in samples]
    outdoor = [s["outdoor_temp_f"] for s in samples]
    elapsed = [s["elapsed_minutes"] for s in samples]

    k_solars: list[float] = []
    rates: list[float] = []
    for i in range(len(indoor) - 1):
        dt_hours = (elapsed[i + 1] - elapsed[i]) / 60.0
        if dt_hours <= 0:
            continue
        rate = (indoor[i + 1] - indoor[i]) / dt_hours
        delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor[i] + outdoor[i + 1]) / 2.0)
        k_solar_i = rate - k_passive * delta
        k_solars.append(k_solar_i)
        rates.append(rate)

    if not k_solars:
        return None, 0.0

    k_solar = sum(k_solars) / len(k_solars)
    k_solar = max(0.0, min(THERMAL_K_SOLAR_MAX_F_PER_HR, k_solar))

    var_residual = sum((k_s - k_solar) ** 2 for k_s in k_solars)
    var_total = sum((r - sum(rates) / len(rates)) ** 2 for r in rates)
    r_squared = 1.0 - (var_residual / var_total) if var_total > 0 else 0.0
    r_squared = max(0.0, r_squared)

    return k_solar, r_squared


def compute_k_active_single_point(
    T_start: float,
    T_peak: float,
    elapsed_hours: float,
    k_passive: float,
    avg_delta_t: float,
    session_mode: str,
) -> float | None:
    """Single-point k_active estimate using actual cycle duration and ΔT.

    Used when n_active < 2 (cycle shorter than sampling interval).
    Physics: k_active = (T_peak - T_start) / elapsed - k_passive * avg(T_in - T_out)

    Args:
        T_start: Indoor temperature at HVAC start (°F).
        T_peak: Peak indoor temperature during/after cycle (°F).
        elapsed_hours: True HVAC-on duration derived from timestamps (hours).
        k_passive: Envelope or proxy decay rate (hr⁻¹, expected negative).
        avg_delta_t: Mean (T_indoor - T_outdoor) across available samples (°F).
        session_mode: "heat" or "cool".

    Returns:
        k_active (°F/hr) within validated bounds, or None if signal is too small,
        elapsed is zero, or result falls outside physical bounds.
    """
    if elapsed_hours <= 0:
        return None
    signal = T_peak - T_start
    if session_mode == "heat" and signal < THERMAL_HVAC_MIN_SIGNAL_F:
        return None
    if session_mode == "cool" and signal > -THERMAL_HVAC_MIN_SIGNAL_F:
        return None
    gross_rate = signal / elapsed_hours
    k_active = gross_rate - k_passive * avg_delta_t
    if session_mode == "heat" and not (THERMAL_K_ACTIVE_HEAT_MIN <= k_active <= THERMAL_K_ACTIVE_HEAT_MAX):
        return None
    if session_mode == "cool" and not (THERMAL_K_ACTIVE_COOL_MIN <= k_active <= THERMAL_K_ACTIVE_COOL_MAX):
        return None
    return k_active


def compute_hvac_run_rate(
    *,
    mode: str,
    start_f: float,
    off_f: float,
    minutes: float,
    avg_delta_t: float,
    k_passive: float | None,
    resolution_f: float | None,
) -> tuple[float | None, str | None]:
    """Steady heating/cooling rate (F/hr) from one HVAC run's own edges (Issue #1512, run-edge estimator).

    ``k_active = (T_off - T_on)/hours - k_passive * avg(T_in - T_out)`` where ``T_off`` is the indoor reading AT
    the off edge, never a post-run peak: whatever the room does after the equipment stops (furnace coast, an AC
    run followed by the room warming) is excluded by construction. Reuses ``compute_k_active_single_point``.

    Returns ``(k_active, None)`` or ``(None, reject_code)``: too-short run, no closed-house ``k_passive``,
    net change below ``max(THERMAL_HVAC_MIN_SIGNAL_F, 2 x resolution_f)`` (wrong direction included), or a rate
    outside the physical bounds.
    """
    if minutes < THERMAL_HVAC_RUN_MIN_MINUTES:
        return None, REJECT_HVAC_RUN_TOO_SHORT
    if k_passive is None or k_passive >= 0:
        return None, REJECT_NO_K_PASSIVE
    gate = max(THERMAL_HVAC_MIN_SIGNAL_F, 2.0 * resolution_f if resolution_f and resolution_f > 0 else 0.0)
    signal = off_f - start_f
    if (mode == "heat" and signal < gate) or (mode == "cool" and -signal < gate) or mode not in ("heat", "cool"):
        return None, REJECT_HVAC_SIGNAL_BELOW_RESOLUTION
    k_active = compute_k_active_single_point(
        T_start=start_f,
        T_peak=off_f,
        elapsed_hours=minutes / 60.0,
        k_passive=k_passive,
        avg_delta_t=avg_delta_t,
        session_mode=mode,
    )
    if k_active is None:
        return None, REJECT_OLS_BOUNDS
    return k_active, None


_GRADE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Median of ``(value, weight)`` pairs; the midpoint of the two values when the weight splits exactly."""
    pts = sorted((v, w) for v, w in pairs if w > 0)
    if not pts:
        return None
    half = sum(w for _, w in pts) / 2.0
    cum = 0.0
    for i, (v, w) in enumerate(pts):
        cum += w
        if cum > half + 1e-12:
            return v
        if abs(cum - half) <= 1e-12 and i + 1 < len(pts):
            return (v + pts[i + 1][0]) / 2.0
    return pts[-1][0]


def _age_days(stamp: Any, today: Any) -> float:
    """Whole days from an ISO date/datetime stamp to ``today`` (a date); 0 when unparseable or in the future."""
    try:
        d = datetime.fromisoformat(str(stamp)[:10]).date()
    except ValueError, TypeError:
        return 0.0
    return max(0.0, float((today - d).days))


def _obs_rate_weight(obs: dict, today: Any, *, legacy: bool) -> float:
    """Ageing weight of one observation: grade weight x method weight x 0.5 ** (age / half-life)."""
    grade_w = _GRADE_WEIGHT.get(obs.get("confidence_grade", "low"), _GRADE_WEIGHT["low"])
    method_w = THERMAL_LEGACY_OBS_WEIGHT if legacy else 1.0
    age = _age_days(obs.get("date") or obs.get("timestamp"), today)
    return grade_w * method_w * 0.5 ** (age / THERMAL_RATE_HALF_LIFE_DAYS)


_AGED_RATE_FIELDS = ("k_active_heat", "k_active_cool", "k_passive")


def aged_rates(observations: list[dict], today: Any, prior: Any = None) -> dict[str, float]:
    """The three aged rates from these observations as of ``today`` (Issue #1512): weighted medians.

    weight = grade_w x method_w x 0.5 ** (age_days / THERMAL_RATE_HALF_LIFE_DAYS); legacy heat/cool commits (no
    ``method`` key) weigh THERMAL_LEGACY_OBS_WEIGHT. ``prior`` (the cache's ``rate_prior``) adds one aged legacy-weight
    pseudo-observation per field. Only fields with at least one usable value are returned, so a caller never sets a
    rate to None through ageing. vent_window/vent_fan never feed ``k_passive``; run-edge HVAC runs carry no
    ``k_passive``. One definition for the live engine and the backtest's as-of rebuild (tools/predict_backtest.py).
    """
    pairs: dict[str, list[tuple[float, float]]] = {f: [] for f in _AGED_RATE_FIELDS}

    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for o in observations:
        mode = o.get("hvac_mode")
        has_method = "method" in o
        if mode in ("heat", "cool"):
            field_name = "k_active_heat" if mode == "heat" else "k_active_cool"
            if _num(o.get("k_active")):
                pairs[field_name].append((float(o["k_active"]), _obs_rate_weight(o, today, legacy=not has_method)))
            if _num(o.get("k_passive")):
                pairs["k_passive"].append((float(o["k_passive"]), _obs_rate_weight(o, today, legacy=not has_method)))
        elif mode == "passive" and _num(o.get("k_passive")):
            # Passive fits are the closed-house learner itself (live decay commits, chart_log endpoint/block_ols,
            # duty_balance): full method weight, not the legacy HVAC post-fit.
            pairs["k_passive"].append((float(o["k_passive"]), _obs_rate_weight(o, today, legacy=False)))
    if isinstance(prior, dict):
        for f, rec in prior.items():
            if f in pairs and isinstance(rec, dict) and _num(rec.get("value")):
                w = THERMAL_LEGACY_OBS_WEIGHT * 0.5 ** (_age_days(rec.get("ts"), today) / THERMAL_RATE_HALF_LIFE_DAYS)
                pairs[f].append((float(rec["value"]), w))
    out: dict[str, float] = {}
    for f, plist in pairs.items():
        med = weighted_median(plist)
        if med is not None:
            out[f] = med
    return out


def estimate_daily_cost(
    heat_minutes: float,
    cool_minutes: float,
    total_minutes: float,
    energy_rate: float | None,
    heat_power_kw: float | None,
    cool_power_kw: float | None,
) -> float | None:
    """Estimated electricity cost of one day's heating and cooling (#1230 R5c, DP-10).

    ``minutes / 60 * power_kw * energy_rate`` per mode, summed. Returns ``None`` (never a guess) when:
    no positive energy rate is set; a mode that ran has no power rating; or the per-mode minutes do not
    account for the day's total runtime (a record from before the per-mode split). Fan-only runtime is not
    costed (its draw is not modelled). A day where nothing ran costs 0.0.
    """
    if not energy_rate or energy_rate <= 0:
        return None
    if heat_minutes + cool_minutes < total_minutes - 1.0:
        return None
    cost = 0.0
    for minutes, power_kw in ((heat_minutes, heat_power_kw), (cool_minutes, cool_power_kw)):
        if minutes <= 0:
            continue
        if not power_kw or power_kw <= 0:
            return None
        cost += minutes / 60.0 * power_kw * energy_rate
    return round(cost, 2)


@dataclass
class DailyRecord:
    """One day's worth of tracked data."""

    date: str
    day_type: str
    trend_direction: str

    # What we recommended
    windows_recommended: bool = False
    window_open_time: str | None = None
    window_close_time: str | None = None
    hvac_mode_recommended: str = ""

    # Physical window state (independent of recommendations)
    windows_physically_opened: bool = False
    window_physical_open_time: str | None = None
    window_physical_close_time: str | None = None

    # What actually happened
    windows_opened: bool = False
    window_open_actual_time: str | None = None
    window_close_actual_time: str | None = None
    hvac_runtime_minutes: float = 0.0
    # #1230 R5c: the same runtime split by mode (heat + cool == hvac_runtime_minutes for new records), so the
    # optional cost estimate can price heating and cooling at their own power ratings.
    hvac_heat_runtime_minutes: float = 0.0
    hvac_cool_runtime_minutes: float = 0.0
    # Issue #912: fan_only (thermostat or WHF-driven) runtime tracked separately from
    # true compressor/burner runtime above — never blend the two into a number that
    # implies heat/cool actually ran. Deliberately prefixed "thermostat_" to avoid
    # confusion with automation.py's unrelated WHF fan_runtime_minutes counter.
    thermostat_fan_only_runtime_minutes: float = 0.0
    occupancy_away_minutes: float = 0.0
    occupancy_mode: str = "home"
    door_window_pause_events: int = 0
    door_pause_by_sensor: dict[str, int] = field(default_factory=dict)
    manual_overrides: int = 0
    override_details: list[dict] = field(default_factory=list)

    # Outcomes
    avg_indoor_temp: float | None = None
    comfort_violations_minutes: float = 0.0  # Time outside the target range in effect (#1107)
    estimated_cost: float | None = None

    # User responded to suggestion?
    suggestion_sent: list[str] = field(default_factory=list)
    suggestion_response: str | None = None  # "accepted", "dismissed", "ignored"

    # Thermal observation tracking
    thermal_session_count: int = 0
    thermal_plateau_count: int = 0  # events abandoned by plateau guard (future use)
    peak_hvac_rate_f_per_hour: float | None = None

    # Weather forecast accuracy tracking
    forecast_high_f: float | None = None  # what weather service predicted for today's high
    forecast_low_f: float | None = None  # what weather service predicted for today's low
    observed_high_f: float | None = None  # actual max from outdoor temp history
    observed_low_f: float | None = None  # actual min from outdoor temp history

    # Bedtime setback tracking
    setback_heat_applied_f: float | None = None
    setback_cool_applied_f: float | None = None
    setback_depth_f: float | None = None
    setback_was_adaptive: bool | None = None
    setback_skipped_reason: str | None = None


@dataclass
class LearningState:
    """Persistent learning state."""

    records: list[dict] = field(default_factory=list)
    # Note: active_suggestions is reserved for future multi-day suggestion state tracking.
    # Currently, suggestion generation is stateless (generate_suggestions() returns fresh pairs
    # on each call) and dismissed_suggestions tracks display history.
    active_suggestions: list[dict] = field(default_factory=list)
    dismissed_suggestions: list[str] = field(default_factory=list)
    settings_history: list[dict] = field(default_factory=list)
    thermal_observations: list[dict] = field(default_factory=list)  # cap: THERMAL_OBS_CAP
    thermal_model_cache: dict | None = None  # EWMA-accumulated k_passive, k_active_heat/cool
    rejection_log: dict = field(default_factory=dict)  # keyed by obs_type; each list capped at 100 entries
    # Restart-cause diagnostics (Issue #403)
    last_shutdown_version: str | None = None
    clean_shutdown: bool = False
    user_initiated_restart: bool = False
    # Issue #1110: which path wrote the clean-shutdown marker — "unload" (config-entry
    # unload/reload, async_shutdown) or "ha_stop" (EVENT_HOMEASSISTANT_STOP). None for
    # state persisted before #1110 (classifier falls back to hass.is_running).
    last_shutdown_kind: str | None = None


class LearningEngine:
    """Tracks patterns and generates adaptive suggestions."""

    def __init__(self, storage_path: Path, entry_id: str = "", temp_unit: str = "fahrenheit") -> None:
        """Initialize the learning engine.

        Args:
            storage_path: Path to the HA config directory for persistent storage.
            entry_id: Owning config entry's entry_id, for multi-zone storage
                scoping (Issue #796). Empty string keeps the legacy unscoped
                path — see `storage_paths.resolve_entry_scoped_path`.
            temp_unit: The user's configured display unit ("fahrenheit"/"celsius"), used only to
                word temperatures in suggestion text (Issue #1048). All stored values stay degF.

        Note: Call load_state() after construction to read persisted data
        from disk.  This is intentionally not done in __init__ because
        the file I/O is blocking and must be run via
        hass.async_add_executor_job from an async context.
        """
        self._storage_dir = storage_path
        self._entry_id = entry_id
        self._temp_unit = temp_unit
        self._db_path = resolve_entry_scoped_path(storage_path, LEARNING_DB_FILE, entry_id)
        self._state = LearningState()
        self._last_suggestion_keys: list[str] = []

    def _zone_log_id(self) -> str:
        """Zone identifier for log lines (the config entry id; ``-`` for the legacy unscoped engine)."""
        return getattr(self, "_entry_id", "") or "-"

    def load_state(self) -> None:
        """Load learning state from disk (blocking I/O — run via executor)."""
        migrate_legacy_storage_file(self._storage_dir, LEARNING_DB_FILE, self._entry_id)
        if self._db_path.exists():
            try:
                data = json.loads(self._db_path.read_text())
                if not isinstance(data, dict):
                    _LOGGER.warning("Learning state is not a JSON object, starting fresh")
                    self._state = LearningState()
                    return
                _LOGGER.debug(
                    "Loaded learning state — %d records",
                    len(data.get("records", [])),
                )
                data.pop("pending_thermal_event", None)  # v2 field removed in Issue #133
                # Issue #1437: retired (nothing wrote it since Issue #133; in-flight observations are not
                # recovered across a restart, by design). Dropped silently: not an unknown newer-release key.
                data.pop("pending_observations", None)
                # Forward compatibility: a file written by a newer release may hold keys this one does not know.
                # Ignore them (the known fields load intact) instead of raising and resetting the whole history.
                known = {f.name for f in fields(LearningState)}
                unknown = sorted(k for k in data if k not in known)
                if unknown:
                    # Visible at INFO (once per load): the next save drops these keys for good. A file with no known
                    # key at all is not a learning file (e.g. another file written over it): WARNING, it loads empty.
                    has_known = any(k in known for k in data)
                    _LOGGER.log(
                        logging.INFO if has_known else logging.WARNING,
                        "Learning state: ignoring unknown keys from a newer release: %s%s",
                        ", ".join(unknown),
                        "" if has_known else " (no known field present: loading an empty learning state)",
                    )
                    data = {k: v for k, v in data.items() if k in known}
                self._state = LearningState(**data)
                # Validate all list fields — corrupted JSON may have wrong types
                for field_name in (
                    "records",
                    "active_suggestions",
                    "dismissed_suggestions",
                    "settings_history",
                    "thermal_observations",
                ):
                    if not isinstance(getattr(self._state, field_name, None), list):
                        _LOGGER.warning(
                            "Learning state field %r is not a list (got %r), resetting to empty",
                            field_name,
                            type(getattr(self._state, field_name, None)).__name__,
                        )
                        setattr(self._state, field_name, [])
                # Validate thermal_observations entries are dicts
                self._state.thermal_observations = [
                    obs for obs in self._state.thermal_observations if isinstance(obs, dict)
                ]
                # Validate dict|None fields
                for field_name in ("thermal_model_cache",):
                    val = getattr(self._state, field_name, None)
                    if val is not None and not isinstance(val, dict):
                        _LOGGER.warning(
                            "Learning state field %r has unexpected type %r, resetting to None",
                            field_name,
                            type(val).__name__,
                        )
                        setattr(self._state, field_name, None)
                # Issue #1234: the swing DEFINITION changed (half the temperature rise per run ->
                # the thermostat's own hysteresis). Null values learned under the old one so they
                # re-learn; they fall back to THERMAL_SWING_DEFAULT_F meanwhile.
                _cache_mig = self._state.thermal_model_cache
                if isinstance(_cache_mig, dict) and reset_swing_cache(_cache_mig):
                    _LOGGER.info(
                        "Thermal model cache: swing values from the retired definition reset (Issue #1234); "
                        "they re-learn from real heat/cool start points"
                    )
                # Issue #1477: recompute the stored swing from the retained samples with the current estimator
                # (finest-resolution samples, rounded up to the next half degree F), so a value stored by an
                # older estimator is right from the first cycle after the update.
                if isinstance(_cache_mig, dict):
                    _before_swing = {m: _cache_mig.get(f"swing_{m}_f") for m in ("heat", "cool")}
                    for _mode in refresh_swing_cache(_cache_mig):
                        _LOGGER.info(
                            "Thermal model cache: %s swing recomputed %s -> %s F (raw median %s F; Issue #1477)",
                            _mode,
                            _before_swing[_mode],
                            _cache_mig.get(f"swing_{_mode}_f"),
                            _cache_mig.get(f"swing_{_mode}_raw_f"),
                        )
                # Issue #1277: confidence grades are computed live from the counts; the stored copies were
                # never refreshed and misled the diagnostic tools, so drop them.
                if isinstance(_cache_mig, dict) and drop_stored_grades(_cache_mig):
                    _LOGGER.debug("Thermal model cache: dropped stale stored confidence grades (Issue #1277)")
                # Issue #1476: undo repeated chart_log fits of one window.
                self._drop_duplicate_chart_log_fits()
                # Issue #1512: keep the cached rates as an ageing prior, then age them against retained observations.
                self._ensure_rate_prior()
                self._recompute_aged_rates()
                # Validate rejection_log is a dict; apply per-obs-type cap on load
                if not isinstance(self._state.rejection_log, dict):
                    self._state.rejection_log = {}
                else:
                    for _obs_type, _entries in list(self._state.rejection_log.items()):
                        if not isinstance(_entries, list):
                            self._state.rejection_log[_obs_type] = []
                        elif len(_entries) > 100:
                            self._state.rejection_log[_obs_type] = _entries[-100:]
                # Validate restart-cause diagnostics fields (Issue #403)
                if not isinstance(self._state.last_shutdown_version, str):
                    self._state.last_shutdown_version = None
                if not isinstance(self._state.clean_shutdown, bool):
                    self._state.clean_shutdown = False
                if not isinstance(self._state.user_initiated_restart, bool):
                    self._state.user_initiated_restart = False
                if self._state.last_shutdown_kind not in SHUTDOWN_KINDS:
                    self._state.last_shutdown_kind = None
                return
            except (json.JSONDecodeError, TypeError) as err:
                _LOGGER.warning("Failed to load learning state, starting fresh: %s", err)
        self._state = LearningState()

    def save_state(self) -> None:
        """Persist learning state to disk (blocking I/O — run via executor).

        Issue #493: uses tempfile.mkstemp() for a unique per-call temp filename,
        mirroring state.py's save() — a fixed, shared ".tmp" path let concurrent
        calls (9 call sites in coordinator.py, one of them fire-and-forget from
        _abandon_observation) race on os.replace(), raising ENOENT for whichever
        call lost the race.
        """
        try:
            serialized = json.dumps(asdict(self._state), indent=2)
        except (TypeError, ValueError) as err:
            _LOGGER.error("Failed to serialize learning state: %s", err)
            return

        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=self._db_path.parent,
            prefix=f"{self._db_path.stem}_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, self._db_path)
            if sys.platform != "win32":
                os.chmod(self._db_path, 0o600)
            _LOGGER.debug("Saved learning state — %d records", len(self._state.records))
        except OSError as err:
            _LOGGER.error("Failed to save learning state: %s", err)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path_str)

    def record_day(self, record: DailyRecord) -> None:
        """Record a day's data for learning.

        Args:
            record: The day's tracking data.
        """
        self._state.records.append(asdict(record))

        # Keep a rolling window (90 days)
        pre_trim_count = len(self._state.records)
        cutoff = (datetime.now().date() - timedelta(days=90)).isoformat()
        self._state.records = [r for r in self._state.records if r.get("date", "") >= cutoff]
        trimmed = pre_trim_count - len(self._state.records)
        if trimmed > 0:
            _LOGGER.debug("Trimmed %d records older than 90 days", trimmed)

        # Hard cap — daily records should not exceed 2 years even with dense data
        if len(self._state.records) > MAX_DAILY_RECORDS:
            self._state.records = self._state.records[-MAX_DAILY_RECORDS:]

        _LOGGER.debug(
            "Recorded day — date=%s, type=%s, records=%d",
            record.date,
            record.day_type,
            len(self._state.records),
        )

    def record_thermal_observation(self, obs: dict) -> bool:
        """Record a thermal observation and update the EWMA thermal model cache.

        obs must be a dict representation of a committed ThermalObservation (v2).
        Trims to 90-day rolling window and enforces THERMAL_OBS_CAP.
        Also updates thermal_model_cache via EWMA using obs.confidence_grade.

        Returns False (nothing recorded) for an invalid obs or a chart_log fit of a window that is
        already recorded (Issue #1476): the chart_log fits re-run on the newest window every time a
        passive stretch ends, so the same night used to be committed over and over.
        """
        if not isinstance(obs, dict) or "date" not in obs:
            return False
        if any(_same_chart_log_fit(obs, o) for o in self._state.thermal_observations):
            _LOGGER.debug(
                "chart_log %s fit of an already-recorded window skipped (Issue #1476): k=%s",
                obs.get("hvac_mode"),
                obs.get("k_passive"),
            )
            return False
        self._state.thermal_observations.append(obs)
        # 90-day rolling trim
        cutoff = (dt_util.now().date() - timedelta(days=90)).isoformat()
        self._state.thermal_observations = [o for o in self._state.thermal_observations if o.get("date", "") >= cutoff]
        # Hard cap
        if len(self._state.thermal_observations) > THERMAL_OBS_CAP:
            self._state.thermal_observations = self._state.thermal_observations[-THERMAL_OBS_CAP:]

        # Update EWMA cache (swing/solar/vent/counts), then re-derive the three aged rates (Issue #1512)
        self._update_thermal_model_cache(obs)
        self._recompute_aged_rates()
        return True

    # ------------------------------------------------------------------
    # Issue #1512: ageing of k_active_heat / k_active_cool / k_passive
    # ------------------------------------------------------------------
    _AGED_RATE_FIELDS = _AGED_RATE_FIELDS

    def _ensure_rate_prior(self) -> None:
        """Store the current cached rates once as the ageing prior (an upgrade never jumps or falls to defaults).

        Only when the cache has no ``rate_prior`` key yet; the prior is then one legacy-weight pseudo-observation
        per field, aged from the stamp stored here.
        """
        cache = self._state.thermal_model_cache
        if not isinstance(cache, dict) or "rate_prior" in cache:
            return
        now_iso = dt_util.now().isoformat()
        cache["rate_prior"] = {
            f: {"value": cache[f], "ts": now_iso}
            for f in self._AGED_RATE_FIELDS
            if isinstance(cache.get(f), (int, float)) and not isinstance(cache.get(f), bool)
        }

    def _recompute_aged_rates(self) -> None:
        """Recompute the three rates as weighted medians of the retained observations (Issue #1512).

        weight = grade_w x method_w x 0.5 ** (age_days / THERMAL_RATE_HALF_LIFE_DAYS); legacy heat/cool commits
        (no ``method`` key) weigh THERMAL_LEGACY_OBS_WEIGHT. The ``rate_prior`` pseudo-observations take part, so a
        field with no usable observation keeps its value (never falls to None through ageing). vent_window/vent_fan
        observations never feed ``k_passive``; run-edge HVAC runs carry no ``k_passive`` so they never do either.
        """
        cache = self._state.thermal_model_cache
        if not isinstance(cache, dict):
            if not self._state.thermal_observations:
                return
            cache = _new_thermal_cache()
            self._state.thermal_model_cache = cache
        self._ensure_rate_prior()
        rates = aged_rates(self._state.thermal_observations, dt_util.now().date(), cache.get("rate_prior"))
        cache.update(rates)

    def record_hvac_run(self, run: dict) -> tuple[dict | None, str | None]:
        """Learn one HVAC run's steady rate from its own edges and record it (Issue #1512).

        Returns ``(obs, None)`` when committed, else ``(None, reject_code)``. The observation never carries an
        envelope ``k_passive`` (``None``), so HVAC runs never feed the closed-house rate.
        """
        mode = run.get("mode")
        k_active, code = compute_hvac_run_rate(
            mode=mode,
            start_f=run["start_f"],
            off_f=run["off_f"],
            minutes=run["minutes"],
            avg_delta_t=run.get("avg_delta_t") or 0.0,
            k_passive=run.get("k_passive"),
            resolution_f=run.get("resolution_f"),
        )
        if k_active is None:
            return None, code or REJECT_OLS_BOUNDS
        minutes = float(run["minutes"])
        stamp = run.get("off_ts") or dt_util.now().isoformat()
        if not isinstance(stamp, str):
            stamp = stamp.isoformat()
        obs = {
            "event_id": run.get("obs_id") or str(uuid.uuid4()),
            "timestamp": stamp,
            "date": stamp[:10],
            "hvac_mode": mode,
            "method": "run_edge",
            "k_active": round(k_active, 3),
            "k_passive": None,
            "k_passive_used": run.get("k_passive"),
            "k_passive_source": run.get("k_passive_source"),
            "signal_f": round(abs(run["off_f"] - run["start_f"]), 2),
            "session_minutes": round(minutes, 1),
            "start_indoor_f": run["start_f"],
            "end_indoor_f": run["off_f"],
            "peak_indoor_f": run.get("peak_f", run["off_f"]),
            "delta_t_avg": round(run.get("avg_delta_t") or 0.0, 2),
            "avg_outdoor_f": run.get("avg_outdoor_f"),
            "confidence_grade": "medium" if minutes >= 15 else "low",
            "schema_version": 2,
        }
        _LOGGER.info(
            "Thermal HVAC run recorded: zone=%s mode=%s k_active=%.3f k_p=%.4f (%s) signal=%.2f min=%.1f",
            self._zone_log_id(),
            mode,
            k_active,
            run.get("k_passive"),
            run.get("k_passive_source"),
            obs["signal_f"],
            minutes,
        )
        self.record_thermal_observation(obs)
        return obs, None

    def record_duty_balance_fit(self, fit: dict) -> int:
        """Record a ``hvac_rate_fit.fit_duty_balance`` result as up to three observations (Issue #1512).

        ``heat`` (k_active), ``cool`` (k_active) and ``passive`` (k_passive), method/source ``duty_balance``. Fits
        re-run on a rolling window, so a fit of an already-recorded window is skipped (``_same_chart_log_fit``) and a
        fit of a newer window replaces the previous duty_balance observation of that mode rather than piling up.
        Returns how many observations were recorded.
        """
        if not isinstance(fit, dict):
            return 0
        r2 = fit.get("r_squared")
        grade = "high" if (r2 or 0.0) >= 0.7 else "medium" if (r2 or 0.0) >= 0.4 else "low"
        stamp = dt_util.now().isoformat()
        recorded = 0
        for mode, key, out_key in (
            ("heat", "k_active_heat", "k_active"),
            ("cool", "k_active_cool", "k_active"),
            ("passive", "k_passive", "k_passive"),
        ):
            val = fit.get(key)
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            obs = {
                "event_id": str(uuid.uuid4()),
                "timestamp": stamp,
                "date": stamp[:10],
                "hvac_mode": mode,
                "method": "duty_balance",
                "source": "duty_balance",
                "k_active": round(float(val), 3) if out_key == "k_active" else None,
                "k_passive": round(float(val), 5) if out_key == "k_passive" else None,
                "r_squared": r2,
                "r_squared_passive": r2 if mode == "passive" else None,
                "n_intervals": fit.get("n_intervals"),
                "heat_hours": fit.get("heat_hours"),
                "cool_hours": fit.get("cool_hours"),
                "window_start": fit.get("window_start"),
                "window_end": fit.get("window_end"),
                "confidence_grade": grade,
                "schema_version": 2,
            }
            if any(_same_chart_log_fit(obs, o) for o in self._state.thermal_observations):
                continue
            old = [
                o
                for o in self._state.thermal_observations
                if o.get("source") == "duty_balance" and o.get("hvac_mode") == mode
            ]
            if old:
                self._state.thermal_observations = [o for o in self._state.thermal_observations if o not in old]
            if self.record_thermal_observation(obs):
                recorded += 1
                cache = self._state.thermal_model_cache
                count_field = {
                    "heat": "observation_count_heat",
                    "cool": "observation_count_cool",
                    "passive": "observation_count_passive",
                }[mode]
                if old and isinstance(cache, dict):
                    # a refit of a rolling window replaces its predecessor: the evidence count does not grow
                    cache[count_field] = max(0, cache.get(count_field, 0) - len(old))
        return recorded

    def reset_vent_rates(self) -> int:
        """Forget the windows-only and fan rates and the fits behind them, for a one-time re-learn (Issue #1505).

        Returns how many vent fits were dropped. ``k_solar`` keeps any two-param update those fits made: it is
        learned from every regime and the re-learn's own fits update it again.
        """
        before = len(self._state.thermal_observations)
        self._state.thermal_observations = [
            o for o in self._state.thermal_observations if o.get("hvac_mode") not in ("vent_window", "vent_fan")
        ]
        cache = self._state.thermal_model_cache
        if isinstance(cache, dict):
            for key in ("k_vent_window", "k_vent_fan"):
                cache[key] = None
            for key in ("observation_count_vent_window", "observation_count_vent_fan"):
                cache[key] = 0
        return before - len(self._state.thermal_observations)

    def seed_vent_rates_from_fits(self) -> None:
        """After the Issue #1505 re-learn: each vent rate = the median of its fits, count = how many.

        The EWMA initialises on its FIRST fit and moves 5-15 % a fit after that, so a batch replayed from empty is
        mostly its first window (Zone 1: EWMA -0.147/h from a -0.167 first fit, median of the 30 fits -0.108/h). The
        median is the batch's own central value; later fits update it through the normal EWMA.
        """
        cache = self._state.thermal_model_cache
        if not isinstance(cache, dict):
            return
        for mode, field_name, count_field in (
            ("vent_window", "k_vent_window", "observation_count_vent_window"),
            ("vent_fan", "k_vent_fan", "observation_count_vent_fan"),
        ):
            ks = sorted(
                float(o["k_passive"])
                for o in self._state.thermal_observations
                if o.get("hvac_mode") == mode and isinstance(o.get("k_passive"), (int, float))
            )
            if ks:
                mid = len(ks) // 2
                cache[field_name] = ks[mid] if len(ks) % 2 else (ks[mid - 1] + ks[mid]) / 2.0
                cache[count_field] = len(ks)

    def _drop_duplicate_chart_log_fits(self) -> None:
        """Remove repeated chart_log fits of one window and undo their pull on the cache (Issue #1476).

        Before #1476 the newest passive window was re-committed every time a passive stretch ended
        (and at every restart), so one night could sit in the observation list 20+ times. That dragged
        the EWMA ``k_passive`` onto that night, inflated the observation counts, and pushed real
        observations out of the 200-entry cap. Idempotent: a list with no repeats is left untouched.

        ``k_passive`` is rebuilt by replaying the de-duplicated envelope observations only when replaying
        the list as stored reproduces the stored value (the list then explains the cache, so the
        de-duplicated replay is what the cache would hold without the repeats). Otherwise it is left
        alone. Vent rates keep their stored values (older history outside the list still weighs on them);
        only their counts are corrected.
        """
        obs_list = self._state.thermal_observations
        kept: list[dict] = []
        dropped: dict[str, int] = {}
        for o in obs_list:
            if any(_same_chart_log_fit(o, k) for k in kept):
                mode = str(o.get("hvac_mode"))
                dropped[mode] = dropped.get(mode, 0) + 1
                continue
            kept.append(o)
        if not dropped:
            return
        self._state.thermal_observations = kept
        cache = self._state.thermal_model_cache
        if not isinstance(cache, dict):
            return
        for mode, count_field in (
            ("passive", "observation_count_passive"),
            ("vent_window", "observation_count_vent_window"),
            ("vent_fan", "observation_count_vent_fan"),
        ):
            n = dropped.get(mode, 0)
            if n and isinstance(cache.get(count_field), int):
                cache[count_field] = max(0, cache[count_field] - n)
        old_k = cache.get("k_passive")
        rebuilt = None
        if any(m not in ("vent_window", "vent_fan") for m in dropped) and isinstance(old_k, (int, float)):
            as_stored = _replay_envelope_k_passive(obs_list)
            if as_stored is not None and abs(as_stored - old_k) <= 0.02 * abs(old_k):
                rebuilt = _replay_envelope_k_passive(kept)
                if rebuilt is not None:
                    cache["k_passive"] = rebuilt
        _LOGGER.info(
            "Thermal observations: removed %d repeated chart_log fits %s (Issue #1476); k_passive %s",
            sum(dropped.values()),
            dropped,
            f"{old_k:.4f} -> {rebuilt:.4f}" if rebuilt is not None else "kept",
        )

    def _update_thermal_model_cache(self, obs: dict) -> None:
        """Apply one observation to the EWMA thermal model cache."""
        grade = obs.get("confidence_grade", "low")
        alpha_map = {"high": 0.3, "medium": 0.15, "low": 0.05}
        alpha = alpha_map.get(grade, 0.05)

        cache = self._state.thermal_model_cache
        if cache is None:
            cache = _new_thermal_cache()
        elif ("k_vent" in cache or "observation_count_fan_only" in cache) and not getattr(
            self, "_logged_legacy_k_vent_present", False
        ):
            # Issue #587: old-format persisted cache (pre-retirement) — present but
            # ignored going forward (k_vent's sole ODE consumer was always called with
            # ventilation_active=False, so it never affected a live prediction). Logged
            # once per process so the upgrade is visible in logs rather than silently
            # invisible via tolerant .get() calls elsewhere.
            _LOGGER.info(
                "Thermal model cache: legacy k_vent/observation_count_fan_only key(s) "
                "found on state load — present but ignored (Issue #587)"
            )
            self._logged_legacy_k_vent_present = True

        k_p = obs.get("k_passive")
        mode = obs.get("hvac_mode")

        # k_passive tracks envelope-only decay — vent_window/vent_fan observations must
        # NOT write here. vent_window k_p reflects window-open heat transfer (→
        # k_vent_window); vent_fan k_p reflects window-open+fan-driven heat transfer (→
        # k_vent_fan). Mixing either into k_passive would bias the envelope rate used for
        # bedtime-setback predictions. (Issue #587: fan_only_decay/"fan_only" retired —
        # it used to write k_vent, a separate now-removed cache field.)
        _envelope_modes = mode not in ("vent_window", "vent_fan")
        # Issue #1512: k_passive / k_active_heat / k_active_cool are no longer EWMA-blended here; they are
        # recomputed as aged weighted medians of the retained observations (_recompute_aged_rates).

        # Update avg_r_squared_passive (simple EWMA) — same guard as k_passive.
        # Vent-split R² reflects fit quality of a different physical regime and must
        # not contaminate the envelope-fit confidence metric.
        r2_p = obs.get("r_squared_passive")
        if r2_p is not None and _envelope_modes:
            _ewma_update_field(cache, "avg_r_squared_passive", r2_p, alpha)
        k_a = obs.get("k_active")
        if mode == "heat" and k_a is not None:
            cache["observation_count_heat"] = cache.get("observation_count_heat", 0) + 1
        elif mode == "cool" and k_a is not None:
            cache["observation_count_cool"] = cache.get("observation_count_cool", 0) + 1
        elif mode == "passive":
            cache["observation_count_passive"] = cache.get("observation_count_passive", 0) + 1
        elif mode in ("vent_window", "vent_fan"):
            # Issue #587: one DRY'd branch resolving the target field/count names once,
            # rather than two near-duplicate branches (the same drift risk 2.2/2.3's
            # trigger/abort split avoids) — see _VENT_SPLIT_TYPES' rationale.
            _field = "k_vent_window" if mode == "vent_window" else "k_vent_fan"
            _count_field = "observation_count_vent_window" if mode == "vent_window" else "observation_count_vent_fan"
            _was_unset = cache.get(_field) is None
            if k_p is not None:
                _ewma_update_field(cache, _field, k_p, alpha)
                if _was_unset:
                    _LOGGER.info(
                        "%s: first observation committed (Issue #587) k=%.4f",
                        _field,
                        k_p,
                    )
            _apply_two_param_solar_update(cache, obs, grade)
            cache[_count_field] = cache.get(_count_field, 0) + 1
        elif mode == "solar":
            k_solar = obs.get("k_solar")
            if k_solar is not None:
                if cache.get("k_solar") is None:
                    cache["k_solar"] = k_solar
                else:
                    cache["k_solar"] = (1.0 - alpha) * cache["k_solar"] + alpha * k_solar
            cache["observation_count_solar"] = cache.get("observation_count_solar", 0) + 1

        # Issue #1234: swing is NOT derived from k-fit observations any more. It is the thermostat's
        # own hysteresis, sampled at each real on-transition (record_swing_sample), so a rejected
        # k fit no longer drops it and a legacy ``swing_f`` on an old observation is ignored.

        cache["last_observation_date"] = obs.get("date")
        self._state.thermal_model_cache = cache

    def record_swing_sample(self, mode: str, sample: dict) -> None:
        """Fold one accepted swing sample (``swing.evaluate_swing_cycle``) into the cache.

        The thermostat's own hysteresis is sampled at the real on-transition, independent of
        whether the cycle's k_active fit later commits (Issue #1234). In-memory only; the caller
        persists via ``save_state``.
        """
        if mode not in ("heat", "cool") or not isinstance(sample, dict) or "ts" not in sample:
            return
        cache = self._state.thermal_model_cache
        if cache is None:
            cache = _new_thermal_cache()
            self._state.thermal_model_cache = cache
        apply_swing_samples(cache, mode, [sample])

    def _swing_cache(self) -> dict:
        cache = self._state.thermal_model_cache
        if cache is None:
            cache = _new_thermal_cache()
            self._state.thermal_model_cache = cache
        return cache

    def record_swing_audit(self, mode: str, record: dict) -> None:
        """Keep the inputs and verdict of one judged cycle, accepted or rejected (Issue #1233).

        Bounded per mode (``THERMAL_SWING_AUDIT_CAP``); the per-reason reject counters are
        cumulative. In-memory only - the caller persists via ``save_state``.
        """
        if mode not in ("heat", "cool") or not isinstance(record, dict):
            return
        record_swing_audit(self._swing_cache(), mode, record)

    def record_coast_sample(self, mode: str, sample: dict) -> None:
        """Fold one closed coast window (peak after heat-off / trough after cool-off) into the cache.

        Stored apart from the swing (``coast_{mode}_*``): the swing is the on-point hysteresis and
        must never absorb what happens after the equipment stops (Issue #1233).
        """
        if mode not in ("heat", "cool") or not isinstance(sample, dict) or "ts" not in sample:
            return
        apply_coast_samples(self._swing_cache(), mode, [sample])

    def update_solar_phase_offset(self, observed_h: float, alpha: float) -> None:
        """EWMA update for solar_phase_offset_h from a phase observation.

        Args:
            observed_h: Observed solar peak offset in hours (raw, will be clamped).
            alpha: EWMA smoothing factor (use THERMAL_SOLAR_PHASE_ALPHA = 0.10).
        """
        from datetime import date as _date

        from .const import THERMAL_SOLAR_PHASE_OFFSET_MAX, THERMAL_SOLAR_PHASE_OFFSET_MIN

        obs_clamped = max(
            float(THERMAL_SOLAR_PHASE_OFFSET_MIN),
            min(float(THERMAL_SOLAR_PHASE_OFFSET_MAX), float(observed_h)),
        )

        cache = self._state.thermal_model_cache
        if cache is None:
            cache = _new_thermal_cache()

        current = cache.get("solar_phase_offset_h")
        new_val = obs_clamped if current is None else (1.0 - alpha) * float(current) + alpha * obs_clamped

        cache["solar_phase_offset_h"] = new_val
        cache["solar_phase_offset_last_obs_date"] = _date.today().isoformat()
        self._state.thermal_model_cache = cache

    def update_ac_duty_solar_phase_offset(self, observed_offset_h: float, today_str: str) -> None:
        """Update the secondary (AC duty cycle) solar phase EWMA.

        Never called by the primary passive-decay path. Called by the coordinator's
        AC duty cycle estimator (_run_ac_duty_solar_phase_fit) after Phase 2.

        Args:
            observed_offset_h: Estimated offset from AC duty peak (peak_hour - 13).
            today_str: ISO date string (YYYY-MM-DD) for the observation date.
        """
        from .const import THERMAL_SOLAR_PHASE_AC_ALPHA

        cache = self._state.thermal_model_cache or {}
        current = cache.get("solar_phase_offset_ac_h")
        if current is None:
            cache["solar_phase_offset_ac_h"] = observed_offset_h
        else:
            cache["solar_phase_offset_ac_h"] = (
                THERMAL_SOLAR_PHASE_AC_ALPHA * observed_offset_h + (1.0 - THERMAL_SOLAR_PHASE_AC_ALPHA) * current
            )
        cache["solar_phase_offset_ac_obs_count"] = cache.get("solar_phase_offset_ac_obs_count", 0) + 1
        cache["solar_phase_offset_ac_last_obs_date"] = today_str
        self._state.thermal_model_cache = cache

    def get_engine_status(self) -> dict:
        """Return structured engine status for each thermal parameter.

        Returns a dict with one entry per learnable parameter plus meta keys.
        Each parameter entry has at minimum: active (bool), confidence (str|None).
        """
        cache = self._state.thermal_model_cache or {}

        k_passive_val = cache.get("k_passive")
        k_solar_val = cache.get("k_solar")
        k_vent_window_val = cache.get("k_vent_window")
        k_vent_fan_val = cache.get("k_vent_fan")
        k_active_heat = cache.get("k_active_heat")
        k_active_cool = cache.get("k_active_cool")
        phase_offset_val = cache.get("solar_phase_offset_h")
        conf_k_passive = _grade_passive_confidence(cache)

        _solar_obs = cache.get("observation_count_solar", 0)
        _conf_solar = grade_solar_confidence(_solar_obs)

        _conf_hvac = grade_hvac_confidence(cache)

        status: dict = {
            "k_passive": {
                "active": k_passive_val is not None,
                "value": k_passive_val,
                "confidence": conf_k_passive,
            },
            "k_solar": {
                "active": k_solar_val is not None,
                "value": k_solar_val,
                "confidence": _conf_solar,
            },
            "solar_phase_offset_h": {
                "active": phase_offset_val is not None,
                "value": phase_offset_val,
            },
            "k_vent_window": {
                "active": k_vent_window_val is not None,
                "value": k_vent_window_val,
            },
            # Keep in sync with tools/engine_status.py::build_engine_status() — that CLI
            # tool independently reimplements this shape (it can't import this module, which
            # pulls in homeassistant.util) and previously missed this same key (Issue #929).
            "k_vent_fan": {
                "active": k_vent_fan_val is not None,
                "value": k_vent_fan_val,
            },
            "k_active_hvac": {
                "active": k_active_heat is not None or k_active_cool is not None,
                "value": {"heat": k_active_heat, "cool": k_active_cool},
                "confidence": _conf_hvac,
            },
        }

        has_solar = k_solar_val is not None or k_vent_window_val is not None or k_vent_fan_val is not None
        status["ode_version"] = "v3" if has_solar else "basic"

        # Issue #1277: the same rule _build_predicted_indoor_future applies (grade plus a closed-house k_passive).
        physics_eligible, eligible_reason = explain_physics_eligibility(
            _conf_hvac, conf_k_passive, k_passive_val, k_vent_window_val
        )
        status["physics_eligible"] = physics_eligible
        status["physics_eligible_reason"] = eligible_reason

        return status

    def get_thermal_model(
        self,
        learning_health: dict | None = None,
        outdoor_temp_f: float | None = None,
        solar_factor: float = 0.0,
    ) -> dict:
        """Return the current thermal model from the EWMA cache.

        Returns v2 model fields (k_active_heat, k_active_cool, k_passive) plus
        legacy compat fields (heating_rate_f_per_hour, cooling_rate_f_per_hour).
        Returns "none" confidence when insufficient data.
        Pure computation — no I/O.

        Args:
            learning_health: Optional pre-aggregated health dict (built by the coordinator
                from its _rejection_log). Included verbatim in the returned dict under the
                "learning_health" key. Pass None or omit to get an empty dict.
            outdoor_temp_f: Current outdoor temperature (°F). When provided, enables
                computation of thermal_equilibrium_f — the indoor temperature the house
                will naturally reach with HVAC off.
            solar_factor: Normalized solar intensity [0.0–1.0] for equilibrium computation.
                Defaults to 0.0 (night or no solar data).
        """
        cache = self._state.thermal_model_cache or {}
        count_heat = cache.get("observation_count_heat", 0)
        count_cool = cache.get("observation_count_cool", 0)
        total = count_heat + count_cool

        confidence = grade_hvac_confidence(cache)

        cache["confidence"] = confidence

        k_active_heat = cache.get("k_active_heat")
        k_active_cool = cache.get("k_active_cool")
        k_passive = cache.get("k_passive")
        k_solar = cache.get("k_solar")

        # Thermal equilibrium: the indoor temp the house converges to with HVAC off.
        # ODE steady-state: T_eq = T_outdoor + (k_solar / |k_passive|) * solar_factor
        # Requires outdoor_temp_f and k_passive; solar term added when k_solar is known.
        thermal_equilibrium_f: float | None = None
        if outdoor_temp_f is not None and k_passive is not None and k_passive < 0:
            thermal_equilibrium_f = outdoor_temp_f
            if k_solar is not None and solar_factor > 0:
                thermal_equilibrium_f += (k_solar / abs(k_passive)) * solar_factor

        _solar_obs = cache.get("observation_count_solar", 0)
        _conf_solar = grade_solar_confidence(_solar_obs)

        return {
            "k_active_heat": k_active_heat,
            "k_active_cool": k_active_cool,
            "k_passive": k_passive,
            "k_vent_window": cache.get("k_vent_window"),
            "k_vent_fan": cache.get("k_vent_fan"),
            "k_solar": k_solar,
            "solar_phase_offset_h": _resolve_solar_phase_offset(cache),
            "solar_phase_offset_ac_h": cache.get("solar_phase_offset_ac_h"),
            "solar_phase_offset_ac_obs_count": cache.get("solar_phase_offset_ac_obs_count", 0),
            "thermal_equilibrium_f": thermal_equilibrium_f,
            # Legacy compat
            "heating_rate_f_per_hour": round(k_active_heat, 2) if k_active_heat is not None else None,
            "cooling_rate_f_per_hour": round(abs(k_active_cool), 2) if k_active_cool is not None else None,
            "observation_count_heat": count_heat,
            "observation_count_cool": count_cool,
            "observation_count_total": total,
            "observation_count_passive": cache.get("observation_count_passive", 0),
            "observation_count_vent_window": cache.get("observation_count_vent_window", 0),
            "observation_count_vent_fan": cache.get("observation_count_vent_fan", 0),
            "observation_count_solar": _solar_obs,
            "confidence": confidence,
            "confidence_k_passive": _grade_passive_confidence(cache),
            "confidence_k_hvac": cache.get("confidence", "none"),
            "confidence_k_solar": _conf_solar,
            "avg_r_squared_passive": cache.get("avg_r_squared_passive"),
            "last_observation_date": cache.get("last_observation_date"),
            "swing_heat_f": cache.get("swing_heat_f"),
            "swing_cool_f": cache.get("swing_cool_f"),
            "swing_heat_f_display": (
                cache.get("swing_heat_f") if cache.get("swing_heat_f") is not None else THERMAL_SWING_DEFAULT_F
            ),
            "swing_cool_f_display": (
                cache.get("swing_cool_f") if cache.get("swing_cool_f") is not None else THERMAL_SWING_DEFAULT_F
            ),
            # Issue #1477: the median before the half-degree-up rounding (diagnostics only; never a decision input)
            "swing_heat_raw_f": cache.get("swing_heat_raw_f"),
            "swing_cool_raw_f": cache.get("swing_cool_raw_f"),
            "swing_heat_resolution_f": cache.get("swing_heat_resolution_f", 0.0),
            "swing_cool_resolution_f": cache.get("swing_cool_resolution_f", 0.0),
            "swing_heat_off_point_f": cache.get("swing_heat_off_point_f"),
            "swing_cool_off_point_f": cache.get("swing_cool_off_point_f"),
            "observation_count_swing_heat": cache.get("observation_count_swing_heat", 0),
            "observation_count_swing_cool": cache.get("observation_count_swing_cool", 0),
            "confidence_swing_heat": _grade_swing_confidence(cache.get("observation_count_swing_heat", 0)),
            "confidence_swing_cool": _grade_swing_confidence(cache.get("observation_count_swing_cool", 0)),
            # Issue #1233: how the swing was learned (recent cycles with inputs, reject counts) and
            # what it means; coast overshoot is reported SEPARATELY from the swing.
            "swing_definition": SWING_DEFINITION_TEXT,
            "swing_debug": swing_debug_summary(cache),
            "coast_heat_f": cache.get("coast_heat_f"),
            "coast_cool_f": cache.get("coast_cool_f"),
            "coast_heat_past_setpoint_f": cache.get("coast_heat_past_setpoint_f"),
            "coast_cool_past_setpoint_f": cache.get("coast_cool_past_setpoint_f"),
            "observation_count_coast_heat": cache.get("observation_count_coast_heat", 0),
            "observation_count_coast_cool": cache.get("observation_count_coast_cool", 0),
            "learning_health": learning_health or {},
        }

    def _commit_event_from_dict(
        self, event: dict, force_grade: str | None, obs_type: str = OBS_TYPE_HVAC_HEAT
    ) -> tuple[dict | None, str | None, float | None]:
        """Compute k_passive/k_active from event samples and record the observation.

        Returns a 3-tuple: (obs_dict_or_None, reject_code, r_squared).
        - obs_dict_or_None: the committed observation dict on success, else None.
        - reject_code: the REJECT_* constant string on failure, else None.
        - r_squared: the R² computed by OLS on failure (or 0.0 if OLS ran but returned 0);
          None when OLS never ran (pre-OLS rejection path).

        force_grade overrides the computed confidence grade when set.
        obs_type selects the commit path: passive_decay/vent_window_decay/vent_fan_decay
        use k_passive only; solar_gain subtracts cached k_passive's conductive
        contribution before attributing the remainder to k_solar (Issue #587,
        Defect B — see compute_k_solar); hvac_heat/hvac_cool use
        the existing two-parameter path. (Issue #587: fan_only_decay retired; the
        vent-split's shared solar-separation logic is regime-agnostic physics —
        separating conduction/ventilation from solar gain works identically whether
        the channel is window-only or window+fan — so it applies to both new types
        unchanged, just widening this guard.)
        """
        _decay_tag_map = {
            "passive_decay": "passive",
            "vent_window_decay": "vent_window",
            "vent_fan_decay": "vent_fan",
        }
        if obs_type in ("passive_decay", "vent_window_decay", "vent_fan_decay"):
            samples = event.get("samples", event.get("active_samples", []))
            hvac_mode_tag_2p = _decay_tag_map[obs_type]

            # For vent_window_decay/vent_fan_decay: attempt 2-param OLS as PRIMARY path
            # when solar_factor variation is sufficient.  This handles the warm-sunny-day
            # case where solar gain drives a positive net dT/dt (indoor rising) even
            # though outdoor is cooler than indoor.  1-param OLS sees a positive k and
            # raises ols_wrong_sign — so 2-param must be tried FIRST to separate k_env
            # (ventilation) from k_solar.
            if obs_type in ("vent_window_decay", "vent_fan_decay"):
                _sf_vals = [s.get("solar_factor", 0.0) for s in samples]
                _sf_range = max(_sf_vals) - min(_sf_vals) if len(_sf_vals) >= 2 else 0.0
                if _sf_range >= THERMAL_SOLAR_FACTOR_MIN_RANGE:
                    _k_env_2p, _k_solar_2p, _r2_2p = compute_k_env_solar(samples)
                    if (
                        _k_env_2p is not None
                        and _k_solar_2p is not None
                        and _r2_2p is not None
                        and THERMAL_K_PASSIVE_MIN <= _k_env_2p <= 0.001
                        and 0.0 <= _k_solar_2p <= THERMAL_K_SOLAR_MAX_F_PER_HR
                        and _r2_2p >= THERMAL_MIN_R_SQUARED
                    ):
                        # 2-param PRIMARY commit — bypass 1-param entirely.
                        now_str = dt_util.now().isoformat()
                        date_str = dt_util.now().date().isoformat()
                        obs = {
                            "event_id": event.get("obs_id", event.get("event_id", str(uuid.uuid4()))),
                            "timestamp": now_str,
                            "date": date_str,
                            "hvac_mode": hvac_mode_tag_2p,
                            "k_passive": round(_k_env_2p, 5),
                            "k_solar": round(_k_solar_2p, 3),
                            "k_active": None,
                            "r_squared_passive": round(_r2_2p, 3),
                            "r_squared_active": None,
                            "sample_count_post": len(samples),
                            "confidence_grade": force_grade or "low",
                            "schema_version": 2,
                            "two_param": True,
                        }
                        _LOGGER.info(
                            "Thermal obs 2-param PRIMARY commit: zone=%s mode=%s "
                            "k_env=%.4f k_solar=%.3f R²=%.3f sf_range=%.2f n=%d",
                            self._zone_log_id(),
                            hvac_mode_tag_2p,
                            _k_env_2p,
                            _k_solar_2p,
                            _r2_2p,
                            _sf_range,
                            len(samples),
                        )
                        self.record_thermal_observation(obs)
                        return obs, None, None
                    # 2-param attempted but failed bounds/R²; fall through to 1-param.

            k_p, r2_p, _reject_code = compute_k_passive(samples, min_samples=THERMAL_MIN_DECAY_SAMPLES)
            if k_p is None:
                _temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
                _indoor_dt = round(max(_temps) - min(_temps), 2) if len(_temps) >= 2 else 0.0
                _sf_vals = [s.get("solar_factor", 0.0) for s in samples if "solar_factor" in s]
                _sf_range_diag = round(max(_sf_vals) - min(_sf_vals), 2) if len(_sf_vals) >= 2 else 0.0
                _indoor_direction = (
                    "rising"
                    if len(_temps) >= 2 and _temps[-1] > _temps[0] + 0.1
                    else "falling"
                    if len(_temps) >= 2 and _temps[-1] < _temps[0] - 0.1
                    else "flat"
                )
                _LOGGER.info(
                    "Thermal event commit failed (%s): k_passive rejected (R²=%.3f, n=%d, "
                    "indoor_ΔT=%.2f°F) code=%s sf_range=%.2f indoor=%s",
                    obs_type,
                    r2_p,
                    len(samples),
                    _indoor_dt,
                    _reject_code or "unknown",
                    _sf_range_diag,
                    _indoor_direction,
                )
                return None, _reject_code, r2_p
            now_str = dt_util.now().isoformat()
            date_str = dt_util.now().date().isoformat()
            hvac_mode_tag = _decay_tag_map[obs_type]
            obs = {
                "event_id": event.get("obs_id", event.get("event_id", str(uuid.uuid4()))),
                "timestamp": now_str,
                "date": date_str,
                "hvac_mode": hvac_mode_tag,
                "k_passive": round(k_p, 5),
                "k_active": None,
                "r_squared_passive": round(r2_p, 3),
                "r_squared_active": None,
                "sample_count_post": len(samples),
                "confidence_grade": force_grade or "low",
                "schema_version": 2,
            }
            # For vent_window_decay/vent_fan_decay: attempt adaptive 2-param OLS upgrade
            # when sf_range is below the primary-path threshold (< 0.30) but 1-param
            # succeeded. Falls back silently to 1-param result if bounds checks fail.
            if obs_type in ("vent_window_decay", "vent_fan_decay"):
                _k_env_2p, _k_solar_2p, _r2_2p = compute_k_env_solar(samples)
                if _k_env_2p is not None and _k_solar_2p is not None and _r2_2p is not None:
                    _k_env_in_bounds = THERMAL_K_PASSIVE_MIN <= _k_env_2p <= 0.001
                    _k_solar_in_bounds = 0.0 <= _k_solar_2p <= THERMAL_K_SOLAR_MAX_F_PER_HR
                    if _k_env_in_bounds and _k_solar_in_bounds and _r2_2p >= THERMAL_MIN_R_SQUARED:
                        obs["k_passive"] = round(_k_env_2p, 5)
                        obs["k_solar"] = round(_k_solar_2p, 3)
                        obs["r_squared_passive"] = round(_r2_2p, 3)
                        obs["two_param"] = True
                        _LOGGER.info(
                            "Thermal observation 2-param upgrade: mode=%s k_env=%.4f k_solar=%.3f R²=%.3f",
                            hvac_mode_tag,
                            _k_env_2p,
                            _k_solar_2p,
                            _r2_2p,
                        )

            _LOGGER.info(
                "Thermal observation committed: zone=%s mode=%s grade=%s k_passive=%.4f R²_p=%.3f",
                self._zone_log_id(),
                hvac_mode_tag,
                obs["confidence_grade"],
                obs["k_passive"],
                obs["r_squared_passive"],
            )
            self.record_thermal_observation(obs)
            return obs, None, None

        if obs_type == "solar_gain":
            samples = event.get("samples", event.get("active_samples", []))
            if len(samples) < 2:
                return None, REJECT_TOO_FEW_SAMPLES, None
            indoor_temps = [s["indoor_temp_f"] for s in samples]
            elapsed = [s["elapsed_minutes"] for s in samples]
            total_dt_hours = (elapsed[-1] - elapsed[0]) / 60.0
            if total_dt_hours <= 0:
                return None, REJECT_SMALL_DELTA, None
            # Diagnostic/shadow-log only — the OLD (pre-#587-Defect-B) formula credited
            # this entire raw rate to solar gain with no subtraction of the ordinary
            # conductive contribution. Never committed; kept only to log how much the
            # fixed subtraction below diverges from it (Part 2.10 observability).
            mean_rate_old_style = (indoor_temps[-1] - indoor_temps[0]) / total_dt_hours

            cache = self._state.thermal_model_cache or {}
            k_passive = cache.get("k_passive")
            if k_passive is None:
                # Fresh install / no envelope model yet — nothing to subtract, and
                # committing the raw rate as k_solar would silently reintroduce the
                # conduction-attribution bug this reject exists to prevent.
                return None, REJECT_NO_K_PASSIVE, None

            k_solar, r2_solar = compute_k_solar(samples, k_passive)
            if k_solar is None:
                return None, REJECT_TOO_FEW_SAMPLES, None

            now_str = dt_util.now().isoformat()
            date_str = dt_util.now().date().isoformat()
            obs = {
                "event_id": event.get("obs_id", event.get("event_id", str(uuid.uuid4()))),
                "timestamp": now_str,
                "date": date_str,
                "hvac_mode": "solar",
                "k_passive": None,
                "k_active": None,
                "k_solar": round(k_solar, 3),
                "r_squared_passive": None,
                "r_squared_active": None,
                "sample_count_post": len(samples),
                "confidence_grade": force_grade or "low",
                "schema_version": 2,
            }
            _LOGGER.info(
                "Thermal observation committed: zone=%s mode=solar grade=%s k_solar=%.3f R²=%.3f",
                self._zone_log_id(),
                obs["confidence_grade"],
                k_solar,
                r2_solar,
            )
            _subtraction_pct = (
                100 * (mean_rate_old_style - k_solar) / mean_rate_old_style if mean_rate_old_style else 0.0
            )
            _LOGGER.info(
                "solar_gain commit [Issue #587]: k_solar=%.3f raw_mean_rate=%.3f k_passive_used=%.4f "
                "subtraction_pct=%.1f%% n=%d",
                k_solar,
                mean_rate_old_style,
                k_passive,
                _subtraction_pct,
                len(samples),
            )
            self.record_thermal_observation(obs)
            return obs, None, None

        # hvac_heat / hvac_cool are learned at the run's off edge (record_hvac_run, Issue #1512); the old post-run
        # fit (and its k_vent_window -> k_passive proxy) is gone, so no HVAC obs_type commits through this path.
        _LOGGER.warning("_commit_event_from_dict: obs_type %r is not committed here (use record_hvac_run)", obs_type)
        return None, REJECT_ABANDONED, None

    def get_weather_bias(self) -> dict:
        """Compute the weather forecast bias from recent daily records.

        Compares forecast_high_f/forecast_low_f against observed_high_f/observed_low_f
        in stored DailyRecord data. Returns "none" confidence when insufficient data.
        Pure computation — no I/O.
        """
        usable = [
            r
            for r in self._state.records
            if (
                r.get("forecast_high_f") is not None
                and r.get("observed_high_f") is not None
                and r.get("forecast_low_f") is not None
                and r.get("observed_low_f") is not None
            )
        ]
        # Use only most recent observations
        usable = usable[-WEATHER_BIAS_MAX_OBS:]

        if len(usable) < MIN_WEATHER_BIAS_OBSERVATIONS:
            return {
                "high_bias": 0.0,
                "low_bias": 0.0,
                "observation_count": len(usable),
                "confidence": "none",
            }

        high_errors = [r["observed_high_f"] - r["forecast_high_f"] for r in usable]
        low_errors = [r["observed_low_f"] - r["forecast_low_f"] for r in usable]
        high_bias = sum(high_errors) / len(high_errors)
        low_bias = sum(low_errors) / len(low_errors)

        count = len(usable)
        if count < 14:
            confidence = "low"
        elif count < 28:
            confidence = "medium"
        else:
            confidence = "high"

        return {
            "high_bias": round(high_bias, 2),
            "low_bias": round(low_bias, 2),
            "observation_count": count,
            "confidence": confidence,
        }

    def get_record_by_date(self, date_str: str) -> dict | None:
        """Return a record dict for the given date, or None."""
        for rec in reversed(self._state.records):
            if rec.get("date") == date_str:
                return rec
        return None

    def generate_suggestions(self) -> list[str]:
        """Analyze recent patterns and generate improvement suggestions.

        Returns:
            List of human-readable suggestion strings.
        """
        records = self._state.records
        if len(records) < MIN_DATA_POINTS_FOR_SUGGESTION:
            _LOGGER.debug(
                "Not enough data for suggestions — %d records, need %d",
                len(records),
                MIN_DATA_POINTS_FOR_SUGGESTION,
            )
            self._last_suggestion_keys = []
            return []

        pairs: list[tuple[str, str]] = []
        recently_dismissed = set(self._state.dismissed_suggestions)

        # Build suppression set from feedback history (last 30 days)
        cutoff_30 = datetime.now() - timedelta(days=30)
        recently_accepted_or_incorrect: set[str] = set()
        for entry in reversed(self._state.settings_history):
            entry_type = entry.get("type")
            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError, TypeError:
                continue
            if ts < cutoff_30:
                break  # history is chronological; once past cutoff we can stop
            suggestion = entry.get("suggestion", "")
            if entry_type == "feedback" and entry.get("verdict") == "incorrect":
                recently_accepted_or_incorrect.add(suggestion)
            elif entry_type is None and suggestion:
                # Legacy accept entries (no "type" field) — treat as accepted
                recently_accepted_or_incorrect.add(suggestion)

        # --- Pattern: Windows recommended but rarely opened ---
        window_days = [
            r for r in records if r.get("windows_recommended") and r.get("occupancy_mode", "home") != "vacation"
        ]
        if len(window_days) >= 7:
            compliance = sum(1 for r in window_days if r.get("windows_opened")) / len(window_days)
            suggestion_key = "low_window_compliance"
            if (
                compliance < COMPLIANCE_THRESHOLD_LOW
                and suggestion_key not in recently_dismissed
                and suggestion_key not in recently_accepted_or_incorrect
            ):
                pairs.append(
                    (
                        suggestion_key,
                        f"Over the past {len(window_days)} days where opening windows was recommended, "
                        f"they were opened only {compliance:.0%} of the time. "
                        f"Would you like Climate Advisor to stop suggesting window actions "
                        f"and instead rely on HVAC with optimized schedules? "
                        f"This uses slightly more energy but requires no manual action.",
                    )
                )

        # --- Pattern: Frequent manual overrides ---
        recent_14 = records[-14:] if len(records) >= 14 else records

        # Exclude vacation days from pattern analysis (abnormal runtime/compliance)
        non_vacation = [r for r in recent_14 if r.get("occupancy_mode", "home") != "vacation"]

        total_overrides = sum(r.get("manual_overrides", 0) for r in non_vacation)
        if total_overrides > 10:
            suggestion_key = "frequent_overrides"
            if suggestion_key not in recently_dismissed and suggestion_key not in recently_accepted_or_incorrect:
                # Analyze override direction and timing from granular data
                all_overrides: list[dict] = []
                for r in non_vacation:
                    all_overrides.extend(r.get("override_details", []))

                if all_overrides:
                    up_count = sum(1 for o in all_overrides if o.get("direction") == "up")
                    down_count = sum(1 for o in all_overrides if o.get("direction") == "down")
                    avg_mag = sum(o.get("magnitude", 0) for o in all_overrides) / len(all_overrides)

                    # Find peak hour by clustering override times
                    hour_counts: dict[int, int] = {}
                    for o in all_overrides:
                        try:
                            hour = int(o.get("time", "12:00").split(":")[0])
                            hour_counts[hour] = hour_counts.get(hour, 0) + 1
                        except ValueError, IndexError:
                            pass

                    direction_word = "up" if up_count >= down_count else "down"
                    detail = f"mostly {direction_word} by about {format_temp_delta(avg_mag, self._temp_unit)}"
                    if hour_counts:
                        peak_hour = max(hour_counts, key=hour_counts.get)  # type: ignore[arg-type]
                        peak_time = f"{peak_hour}:00" if peak_hour >= 10 else f" {peak_hour}:00"
                        detail += f", often around {peak_time.strip()}"

                    pairs.append(
                        (
                            suggestion_key,
                            f"You've manually adjusted the thermostat {total_overrides} times "
                            f"in the past two weeks ({detail}). "
                            f"Would you like Climate Advisor to adjust the comfort setpoints, "
                            f"or add a scheduled temperature bump?",
                        )
                    )
                else:
                    pairs.append(
                        (
                            suggestion_key,
                            f"You've manually adjusted the thermostat {total_overrides} times "
                            f"in the past two weeks. This may indicate the comfort setpoints "
                            f"don't match your preferences. Would you like Climate Advisor to "
                            f"analyze the override patterns and suggest new setpoints?",
                        )
                    )

        # --- Pattern: High runtime on mild/warm days ---
        mild_warm_days = [r for r in non_vacation if r.get("day_type") in ("mild", "warm")]
        if mild_warm_days:
            avg_runtime = sum(r.get("hvac_runtime_minutes", 0) for r in mild_warm_days) / len(mild_warm_days)
            if avg_runtime > 120:  # More than 2 hours on mild/warm days
                suggestion_key = "high_runtime_mild_days"
                if suggestion_key not in recently_dismissed and suggestion_key not in recently_accepted_or_incorrect:
                    pairs.append(
                        (
                            suggestion_key,
                            f"On mild and warm days, the HVAC has been running an average of "
                            f"{avg_runtime:.0f} minutes — more than expected. This could indicate "
                            f"doors/windows being left open, or the setpoint being too aggressive. "
                            f"Would you like to add more door/window sensors, or adjust the "
                            f"setpoints for mild days?",
                        )
                    )

        # --- Pattern: Leaving home frequently without setback taking effect ---
        away_days = [r for r in recent_14 if r.get("occupancy_away_minutes", 0) > 30]
        short_away = [r for r in away_days if r.get("occupancy_away_minutes", 0) < 45]
        if len(short_away) > 5:
            suggestion_key = "short_departures"
            if suggestion_key not in recently_dismissed and suggestion_key not in recently_accepted_or_incorrect:
                pairs.append(
                    (
                        suggestion_key,
                        "You frequently leave for 30\u201345 minute periods, which is barely "
                        "long enough for the setback to take effect before you return. "
                        "Would you like to shorten the setback delay from 15 minutes to "
                        "5 minutes for these quick trips, or skip setback for departures "
                        "under 1 hour?",
                    )
                )

        # --- Pattern: Comfort violations (too cold/hot despite automation) ---
        violation_days = [r for r in non_vacation if r.get("comfort_violations_minutes", 0) > 30]
        if len(violation_days) > 5:
            suggestion_key = "comfort_violations"
            if suggestion_key not in recently_dismissed and suggestion_key not in recently_accepted_or_incorrect:
                pairs.append(
                    (
                        suggestion_key,
                        f"The house has been outside your comfort range for more than "
                        f"30 minutes on {len(violation_days)} of the last 14 days. "
                        f"Would you like to reduce the setback aggressiveness, or "
                        f"start the morning warm-up earlier?",
                    )
                )

        # --- Pattern: Door/window pauses happening frequently ---
        pause_total = sum(r.get("door_window_pause_events", 0) for r in non_vacation)
        if pause_total > 20:
            suggestion_key = "frequent_door_pauses"
            if suggestion_key not in recently_dismissed and suggestion_key not in recently_accepted_or_incorrect:
                # Aggregate per-sensor pause data across 14-day window
                sensor_totals: dict[str, int] = {}
                for r in non_vacation:
                    for sensor, count in r.get("door_pause_by_sensor", {}).items():
                        sensor_totals[sensor] = sensor_totals.get(sensor, 0) + count

                if sensor_totals:
                    top_sensor = max(sensor_totals, key=sensor_totals.get)  # type: ignore[arg-type]
                    top_count = sensor_totals[top_sensor]
                    top_name = top_sensor.replace("_", " ").title()
                    pairs.append(
                        (
                            suggestion_key,
                            f"HVAC has been paused {pause_total} times due to open doors/windows "
                            f"in the past two weeks. {top_name} was the most frequent trigger "
                            f"({top_count} times). Would you like to extend the pause delay for "
                            f"that sensor, or exclude it from monitoring?",
                        )
                    )
                else:
                    pairs.append(
                        (
                            suggestion_key,
                            f"HVAC has been paused {pause_total} times due to open doors/windows "
                            f"in the past two weeks. If a specific door is the main culprit, "
                            f"would you like to extend the pause delay for that door, or "
                            f"exclude it from monitoring?",
                        )
                    )

        # Design note: thermal_model_ready and forecast_bias_significant are
        # informational one-time suggestions included in the daily briefing.
        # They use push_briefing / email_briefing toggles — no separate notification
        # toggles needed as they don't warrant urgent alerting.

        # --- Pattern: Thermal model first reached usable confidence ---
        thermal_model = self.get_thermal_model()
        if (
            thermal_model["confidence"] != "none"
            and "thermal_model_ready" not in recently_dismissed
            and "thermal_model_ready" not in recently_accepted_or_incorrect
            and not any(s.get("key") == "thermal_model_ready" for s in self._state.active_suggestions)
        ):
            pairs.append(
                (
                    "thermal_model_ready",
                    (
                        "I've now collected enough thermal observations to estimate your home's "
                        "envelope and HVAC performance. Pre-heat timing and bedtime setbacks will "
                        "adjust automatically based on how your home actually behaves — no action needed."
                    ),
                )
            )

        # --- Pattern: Weather forecast consistently biased ---
        weather_bias = self.get_weather_bias()
        if (
            weather_bias["confidence"] != "none"
            and abs(weather_bias["high_bias"]) >= 2.0
            and "forecast_bias_significant" not in recently_dismissed
            and "forecast_bias_significant" not in recently_accepted_or_incorrect
            and not any(s.get("key") == "forecast_bias_significant" for s in self._state.active_suggestions)
        ):
            direction = "higher" if weather_bias["high_bias"] < 0 else "lower"
            magnitude = abs(round(weather_bias["high_bias"], 1))
            pairs.append(
                (
                    "forecast_bias_significant",
                    (
                        f"I've noticed your weather service consistently forecasts highs about "
                        f"{format_temp_delta(magnitude, self._temp_unit, 1)} {direction} than what actually occurs "
                        "at your location. "
                        "I'm now applying a correction to improve forecasting accuracy — this helps "
                        "pre-heat and pre-cool decisions account for your local conditions."
                    ),
                )
            )

        self._last_suggestion_keys = [key for key, _ in pairs]
        suggestions = [text for _, text in pairs]
        _LOGGER.debug("Generated %d learning suggestions", len(suggestions))
        return suggestions

    def get_last_suggestion_keys(self) -> list[str]:
        """Return the keys of the most recently generated suggestions."""
        return list(self._last_suggestion_keys)

    def dismiss_suggestion(self, suggestion_key: str) -> None:
        """Mark a suggestion as dismissed so it won't reappear soon."""
        self._state.dismissed_suggestions.append(suggestion_key)
        # Cap to prevent unbounded growth
        if len(self._state.dismissed_suggestions) > 100:
            self._state.dismissed_suggestions = self._state.dismissed_suggestions[-100:]
        _LOGGER.info("Learning suggestion dismissed — key=%s", suggestion_key)

    def record_feedback(self, suggestion_key: str, verdict: str) -> None:
        """Record whether a suggestion diagnosis was correct or incorrect."""
        self._state.settings_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "type": "feedback",
                "suggestion": suggestion_key,
                "verdict": verdict,  # "correct" | "incorrect"
            }
        )
        if len(self._state.settings_history) > 200:
            self._state.settings_history = self._state.settings_history[-200:]
        _LOGGER.info("Suggestion feedback recorded — key=%s verdict=%s", suggestion_key, verdict)

    def accept_suggestion(self, suggestion_key: str) -> dict[str, Any]:
        """Accept a suggestion and return the config changes to apply.

        Returns:
            Dict of configuration changes the coordinator should apply.
        """
        # This is a placeholder — in a full implementation, each suggestion key
        # maps to a specific set of config changes.
        changes: dict[str, Any] = {}
        # Issue #874: tracked separately from `changes` so informational-only keys
        # (below) don't fall through to the "not recognized" WARNING — they ARE
        # recognized, they just have no config change to apply.
        recognized = True

        if suggestion_key == "low_window_compliance":
            changes["disable_window_recommendations"] = True
        elif suggestion_key == "frequent_overrides":
            changes["request_setpoint_analysis"] = True
        elif suggestion_key == "short_departures":
            changes["occupancy_setback_minutes"] = 5
        elif suggestion_key == "comfort_violations":
            changes["setback_modifier"] = 2  # Less aggressive setback
            changes["morning_preheat_offset_minutes"] = 15  # Start earlier
        elif suggestion_key == "frequent_door_pauses":
            changes["door_pause_seconds"] = 300  # Extend to 5 minutes
        elif suggestion_key in ("thermal_model_ready", "forecast_bias_significant", "high_runtime_mild_days"):
            pass  # informational-only / no single deterministic config change
        else:
            recognized = False

        if changes:
            _LOGGER.info(
                "Learning suggestion accepted — key=%s, changes=%s",
                suggestion_key,
                changes,
            )
        elif recognized:
            _LOGGER.info(
                "Learning suggestion accepted — key=%s, no automatic config change available",
                suggestion_key,
            )
        else:
            _LOGGER.warning(
                "Suggestion key %r not recognized — no changes applied",
                suggestion_key,
            )

        self._state.settings_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "suggestion": suggestion_key,
                "changes": changes,
            }
        )
        # Cap to prevent unbounded growth
        if len(self._state.settings_history) > 200:
            self._state.settings_history = self._state.settings_history[-200:]

        return changes

    def reset(self, scope: str = "all") -> None:
        """Reset learning data for the given scope.

        Args:
            scope: What to reset — "thermal_model", "weather_bias", "suggestions", or "all".
        """
        _LOGGER.info("Learning data reset requested: scope=%s", scope)
        if scope == "all":
            self._state = LearningState()
            self.save_state()
        elif scope == "thermal_model":
            self._state.thermal_observations = []
            self._state.thermal_model_cache = None
            self.save_state()
        elif scope == "weather_bias":
            count = 0
            for record in self._state.records:
                record["forecast_high_f"] = None
                record["forecast_low_f"] = None
                record["observed_high_f"] = None
                record["observed_low_f"] = None
                count += 1
            self.save_state()
            _LOGGER.info("Weather bias reset: cleared forecast/observed fields from %d daily records", count)
        elif scope == "suggestions":
            self._state.dismissed_suggestions = []
            self._state.active_suggestions = []
            self.save_state()
        else:
            _LOGGER.warning("reset() called with unknown scope %r — no action taken", scope)
            return
        _LOGGER.info("Learning data reset complete: scope=%s", scope)

    def get_compliance_summary(self) -> dict[str, Any]:
        """Get a summary of compliance and learning metrics.

        Returns:
            Dict with compliance scores and pattern summaries.
        """
        records = self._state.records
        if not records:
            return {"status": "collecting_data", "days_recorded": 0}

        recent = records[-14:] if len(records) >= 14 else records

        # Window compliance
        window_days = [r for r in recent if r.get("windows_recommended")]
        window_compliance = (
            sum(1 for r in window_days if r.get("windows_opened")) / len(window_days) if window_days else None
        )

        # Average runtime
        avg_runtime = sum(r.get("hvac_runtime_minutes", 0) for r in recent) / len(recent)
        avg_fan_only_runtime = sum(r.get("thermostat_fan_only_runtime_minutes", 0) for r in recent) / len(recent)

        # Comfort score (% of time in comfort range)
        total_day_minutes = len(recent) * 1440  # Minutes in a day
        total_violations = sum(r.get("comfort_violations_minutes", 0) for r in recent)
        comfort_score = max(0.0, 1 - (total_violations / total_day_minutes)) if total_day_minutes else 1.0

        return {
            "status": "active",
            "days_recorded": len(records),
            # the scores below are over the most recent ``window_days`` records (14, or fewer while young)
            "window_days": len(recent),
            "window_compliance": window_compliance,
            "window_compliance_denominator": len(window_days),
            "avg_daily_hvac_runtime_minutes": avg_runtime,
            "avg_daily_thermostat_fan_only_runtime_minutes": avg_fan_only_runtime,
            "comfort_score": comfort_score,
            "total_manual_overrides": sum(r.get("manual_overrides", 0) for r in recent),
            "pending_suggestions": len(self.generate_suggestions()),
        }

    def get_recent_records(self, n: int) -> list[Any]:
        """Return the last n daily records. Returns [] if unavailable."""
        try:
            records = getattr(self._state, "records", None)
            if isinstance(records, list):
                return records[-n:]
        except Exception:
            pass
        return []
