"""Learning engine for Climate Advisor.

Tracks human compliance with suggestions, HVAC runtime patterns, and
environmental outcomes to generate adaptive improvement suggestions.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from homeassistant.util import dt as dt_util

from .const import (
    COMPLIANCE_THRESHOLD_LOW,
    LEARNING_DB_FILE,
    MIN_DATA_POINTS_FOR_SUGGESTION,
    MIN_THERMAL_OBSERVATIONS,
    MIN_WEATHER_BIAS_OBSERVATIONS,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_BOUNDS,
    REJECT_OLS_WRONG_SIGN,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_SAMPLES,
    THERMAL_K_ACTIVE_COOL_MAX,
    THERMAL_K_ACTIVE_COOL_MIN,
    THERMAL_K_ACTIVE_HEAT_MAX,
    THERMAL_K_ACTIVE_HEAT_MIN,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
    THERMAL_MIN_DECAY_SAMPLES,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_MIN_R_SQUARED,
    THERMAL_OBS_CAP,
    THERMAL_PASSIVE_CONF_HIGH,
    THERMAL_PASSIVE_CONF_LOW,
    THERMAL_PASSIVE_CONF_MEDIUM,
    WEATHER_BIAS_MAX_OBS,
)

_LOGGER = logging.getLogger(__name__)


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
    """Compute confidence tier for k_passive based on combined observation count."""
    count = (
        cache.get("observation_count_passive", 0)
        + cache.get("observation_count_vent", 0)
        + cache.get("observation_count_fan_only", 0)
        + cache.get("observation_count_heat", 0)
        + cache.get("observation_count_cool", 0)
    )  # TODO: add confidence_k_env as a dedicated envelope-only confidence field (future follow-up)
    if count < THERMAL_PASSIVE_CONF_LOW:
        return "none"
    if count < THERMAL_PASSIVE_CONF_MEDIUM:
        return "low"
    if count < THERMAL_PASSIVE_CONF_HIGH:
        return "medium"
    return "high"


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
    occupancy_away_minutes: float = 0.0
    occupancy_mode: str = "home"
    door_window_pause_events: int = 0
    door_pause_by_sensor: dict[str, int] = field(default_factory=dict)
    manual_overrides: int = 0
    override_details: list[dict] = field(default_factory=list)

    # Outcomes
    avg_indoor_temp: float | None = None
    comfort_violations_minutes: float = 0.0  # Time spent outside comfort range
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
    pending_thermal_event: dict | None = None  # in-progress observation window
    pending_observations: dict = field(default_factory=dict)  # v3 multi-type obs windows
    thermal_model_cache: dict | None = None  # EWMA-accumulated k_passive, k_active_heat/cool
    rejection_log: dict = field(default_factory=dict)  # keyed by obs_type; each list capped at 100 entries


class LearningEngine:
    """Tracks patterns and generates adaptive suggestions."""

    def __init__(self, storage_path: Path) -> None:
        """Initialize the learning engine.

        Args:
            storage_path: Path to the HA config directory for persistent storage.

        Note: Call load_state() after construction to read persisted data
        from disk.  This is intentionally not done in __init__ because
        the file I/O is blocking and must be run via
        hass.async_add_executor_job from an async context.
        """
        self._db_path = storage_path / LEARNING_DB_FILE
        self._state = LearningState()
        self._last_suggestion_keys: list[str] = []

    def load_state(self) -> None:
        """Load learning state from disk (blocking I/O — run via executor)."""
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
                for field_name in ("pending_thermal_event", "thermal_model_cache"):
                    val = getattr(self._state, field_name, None)
                    if val is not None and not isinstance(val, dict):
                        _LOGGER.warning(
                            "Learning state field %r has unexpected type %r, resetting to None",
                            field_name,
                            type(val).__name__,
                        )
                        setattr(self._state, field_name, None)
                # Validate pending_observations is a dict
                if not isinstance(self._state.pending_observations, dict):
                    self._state.pending_observations = {}
                # Validate rejection_log is a dict; apply per-obs-type cap on load
                if not isinstance(self._state.rejection_log, dict):
                    self._state.rejection_log = {}
                else:
                    for _obs_type, _entries in list(self._state.rejection_log.items()):
                        if not isinstance(_entries, list):
                            self._state.rejection_log[_obs_type] = []
                        elif len(_entries) > 100:
                            self._state.rejection_log[_obs_type] = _entries[-100:]
                # v3 migration: convert pending_thermal_event → pending_observations
                old_event = self._state.pending_thermal_event
                if isinstance(old_event, dict) and old_event.get("status") in ("post_heat", "stabilized"):
                    session_mode = old_event.get("session_mode") or old_event.get("hvac_mode") or "heat"
                    obs_type = OBS_TYPE_HVAC_HEAT if session_mode == "heat" else OBS_TYPE_HVAC_COOL
                    if obs_type not in self._state.pending_observations:
                        migrated: PendingObservation = {
                            "obs_type": obs_type,
                            "obs_id": old_event.get("event_id", str(uuid.uuid4())),
                            "start_time": old_event.get("active_start") or old_event.get("created_at", ""),
                            "status": "monitoring",
                            "samples": old_event.get("active_samples", []),
                            "flags_at_start": {
                                "hvac_mode": old_event.get("hvac_mode", "heat"),
                                "hvac_action": "heating" if session_mode == "heat" else "cooling",
                                "fan_active": False,
                                "windows_open": False,
                                "occupancy_mode": "home",
                            },
                            "_legacy_event": old_event,
                            "schema_version": 1,
                        }
                        migrated.update(
                            {
                                "_phase": "active" if old_event.get("status") == "active" else "post_heat",
                                "active_samples": old_event.get("active_samples", []),
                                "post_heat_samples": old_event.get("post_heat_samples", []),
                                "peak_indoor_f": old_event.get("peak_indoor_f"),
                                "start_indoor_f": old_event.get("start_indoor_f"),
                                "active_start": old_event.get("active_start", ""),
                                "active_end": old_event.get("active_end"),
                                "session_mode": session_mode,
                                "hvac_mode": old_event.get("hvac_mode", session_mode),
                            }
                        )
                        self._state.pending_observations[obs_type] = migrated
                        _LOGGER.info(
                            "v3 migration: pending_thermal_event (mode=%s) → pending_observations[%s]",
                            session_mode,
                            obs_type,
                        )
                    self._state.pending_thermal_event = None
                elif isinstance(old_event, dict) and old_event.get("status") == "active":
                    # Active v2 events cannot be recovered without runtime HVAC state — discard
                    _LOGGER.info("v3 migration: discarding active-status v2 pending_thermal_event (unrecoverable)")
                    self._state.pending_thermal_event = None
                return
            except (json.JSONDecodeError, TypeError) as err:
                _LOGGER.warning("Failed to load learning state, starting fresh: %s", err)
        self._state = LearningState()

    def save_state(self) -> None:
        """Persist learning state to disk (blocking I/O — run via executor)."""
        try:
            serialized = json.dumps(asdict(self._state), indent=2)
            tmp_path = self._db_path.with_suffix(".tmp")
            tmp_path.write_text(serialized)
            if sys.platform != "win32":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._db_path)
            _LOGGER.debug("Saved learning state — %d records", len(self._state.records))
        except OSError as err:
            _LOGGER.error("Failed to save learning state: %s", err)

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

    def record_thermal_observation(self, obs: dict) -> None:
        """Record a thermal observation and update the EWMA thermal model cache.

        obs must be a dict representation of a committed ThermalObservation (v2).
        Trims to 90-day rolling window and enforces THERMAL_OBS_CAP.
        Also updates thermal_model_cache via EWMA using obs.confidence_grade.
        """
        if not isinstance(obs, dict) or "date" not in obs:
            return
        self._state.thermal_observations.append(obs)
        # 90-day rolling trim
        cutoff = (dt_util.now().date() - timedelta(days=90)).isoformat()
        self._state.thermal_observations = [o for o in self._state.thermal_observations if o.get("date", "") >= cutoff]
        # Hard cap
        if len(self._state.thermal_observations) > THERMAL_OBS_CAP:
            self._state.thermal_observations = self._state.thermal_observations[-THERMAL_OBS_CAP:]

        # Update EWMA cache
        self._update_thermal_model_cache(obs)

    def _update_thermal_model_cache(self, obs: dict) -> None:
        """Apply one observation to the EWMA thermal model cache."""
        grade = obs.get("confidence_grade", "low")
        alpha_map = {"high": 0.3, "medium": 0.15, "low": 0.05}
        alpha = alpha_map.get(grade, 0.05)

        cache = self._state.thermal_model_cache
        if cache is None:
            cache = {
                "k_passive": None,
                "k_active_heat": None,
                "k_active_cool": None,
                "k_vent": None,
                "k_vent_window": None,
                "k_solar": None,
                "observation_count_heat": 0,
                "observation_count_cool": 0,
                "observation_count_passive": 0,
                "observation_count_fan_only": 0,
                "observation_count_vent": 0,
                "observation_count_solar": 0,
                "last_observation_date": None,
                "avg_r_squared_passive": None,
                "confidence_k_passive": "none",
                "confidence_k_hvac": "none",
            }

        k_p = obs.get("k_passive")
        if k_p is not None:
            if cache["k_passive"] is None:
                cache["k_passive"] = k_p
            else:
                cache["k_passive"] = (1.0 - alpha) * cache["k_passive"] + alpha * k_p

        # Update avg_r_squared_passive (simple EWMA)
        r2_p = obs.get("r_squared_passive")
        if r2_p is not None:
            if cache["avg_r_squared_passive"] is None:
                cache["avg_r_squared_passive"] = r2_p
            else:
                cache["avg_r_squared_passive"] = (1.0 - alpha) * cache["avg_r_squared_passive"] + alpha * r2_p

        mode = obs.get("hvac_mode")
        k_a = obs.get("k_active")
        if mode == "heat" and k_a is not None:
            if cache["k_active_heat"] is None:
                cache["k_active_heat"] = k_a
            else:
                cache["k_active_heat"] = (1.0 - alpha) * cache["k_active_heat"] + alpha * k_a
            cache["observation_count_heat"] = cache.get("observation_count_heat", 0) + 1
        elif mode == "cool" and k_a is not None:
            if cache["k_active_cool"] is None:
                cache["k_active_cool"] = k_a
            else:
                cache["k_active_cool"] = (1.0 - alpha) * cache["k_active_cool"] + alpha * k_a
            cache["observation_count_cool"] = cache.get("observation_count_cool", 0) + 1
        elif mode == "passive":
            cache["observation_count_passive"] = cache.get("observation_count_passive", 0) + 1
        elif mode == "fan_only":
            if k_p is not None:
                if cache.get("k_vent") is None:
                    cache["k_vent"] = k_p
                else:
                    cache["k_vent"] = (1.0 - alpha) * cache["k_vent"] + alpha * k_p
            cache["observation_count_fan_only"] = cache.get("observation_count_fan_only", 0) + 1
        elif mode == "ventilated":
            if k_p is not None:
                if cache.get("k_vent_window") is None:
                    cache["k_vent_window"] = k_p
                else:
                    cache["k_vent_window"] = (1.0 - alpha) * cache["k_vent_window"] + alpha * k_p
            cache["observation_count_vent"] = cache.get("observation_count_vent", 0) + 1
        elif mode == "solar":
            k_solar = obs.get("k_solar")
            if k_solar is not None:
                if cache.get("k_solar") is None:
                    cache["k_solar"] = k_solar
                else:
                    cache["k_solar"] = (1.0 - alpha) * cache["k_solar"] + alpha * k_solar
            cache["observation_count_solar"] = cache.get("observation_count_solar", 0) + 1

        cache["last_observation_date"] = obs.get("date")
        self._state.thermal_model_cache = cache

    def get_thermal_model(self, learning_health: dict | None = None) -> dict:
        """Return the current thermal model from the EWMA cache.

        Returns v2 model fields (k_active_heat, k_active_cool, k_passive) plus
        legacy compat fields (heating_rate_f_per_hour, cooling_rate_f_per_hour).
        Returns "none" confidence when insufficient data.
        Pure computation — no I/O.

        Args:
            learning_health: Optional pre-aggregated health dict (built by the coordinator
                from its _rejection_log). Included verbatim in the returned dict under the
                "learning_health" key. Pass None or omit to get an empty dict.
        """
        cache = self._state.thermal_model_cache or {}
        count_heat = cache.get("observation_count_heat", 0)
        count_cool = cache.get("observation_count_cool", 0)
        total = count_heat + count_cool

        if total < MIN_THERMAL_OBSERVATIONS:
            confidence = "none"
        elif total < 10:
            confidence = "low"
        elif total < 20:
            confidence = "medium"
        else:
            confidence = "high"

        cache["confidence"] = confidence

        k_active_heat = cache.get("k_active_heat")
        k_active_cool = cache.get("k_active_cool")

        return {
            "k_active_heat": k_active_heat,
            "k_active_cool": k_active_cool,
            "k_passive": cache.get("k_passive"),
            "k_vent": cache.get("k_vent"),
            "k_vent_window": cache.get("k_vent_window"),
            "k_solar": cache.get("k_solar"),
            # Legacy compat
            "heating_rate_f_per_hour": round(k_active_heat, 2) if k_active_heat is not None else None,
            "cooling_rate_f_per_hour": round(abs(k_active_cool), 2) if k_active_cool is not None else None,
            "observation_count_heat": count_heat,
            "observation_count_cool": count_cool,
            "observation_count_total": total,
            "observation_count_passive": cache.get("observation_count_passive", 0),
            "observation_count_fan_only": cache.get("observation_count_fan_only", 0),
            "observation_count_vent": cache.get("observation_count_vent", 0),
            "observation_count_solar": cache.get("observation_count_solar", 0),
            "confidence": confidence,
            "confidence_k_passive": _grade_passive_confidence(cache),
            "confidence_k_hvac": cache.get("confidence", "none"),
            "avg_r_squared_passive": cache.get("avg_r_squared_passive"),
            "last_observation_date": cache.get("last_observation_date"),
            "learning_health": learning_health or {},
        }

    def get_pending_thermal_event(self) -> dict | None:
        """Return the in-progress thermal event dict, or None."""
        return self._state.pending_thermal_event

    def set_pending_thermal_event(self, event: dict | None) -> None:
        """Update the in-progress thermal event (does NOT call save_state)."""
        self._state.pending_thermal_event = event

    def recover_pending_event_on_startup(self) -> dict | None:
        """Process any pending thermal event that survived an HA restart.

        Called once at startup (via async_add_executor_job).
        Commits, partially commits, or discards the event per the recovery table:

        | Status                        | Action                                   |
        |-------------------------------|------------------------------------------|
        | active                        | Discard                                  |
        | post_heat + ≥10 post samples  | Partial commit at low confidence         |
        | post_heat + <10 post samples  | Discard                                  |
        | stabilized                    | Full commit                              |
        | complete / abandoned          | Clear                                    |

        Returns the committed observation dict if one was produced, else None.
        """
        event = self._state.pending_thermal_event
        if not isinstance(event, dict):
            return None

        status = event.get("status", "")
        _LOGGER.info("Startup recovery: pending thermal event status=%s", status)

        result = None
        if status == "stabilized":
            result, *_ = self._commit_event_from_dict(event, force_grade=None)
        elif status == "post_heat":
            post_samples = event.get("post_heat_samples", [])
            if len(post_samples) >= THERMAL_MIN_POST_HEAT_SAMPLES:
                result, *_ = self._commit_event_from_dict(event, force_grade="low")
            else:
                _LOGGER.info(
                    "Startup recovery: discarding post_heat event with only %d post samples",
                    len(post_samples),
                )
        elif status in ("complete", "abandoned"):
            pass  # just clear
        else:
            _LOGGER.info("Startup recovery: discarding event with status=%r", status)

        self._state.pending_thermal_event = None
        self.save_state()
        return result

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
        obs_type selects the commit path: passive_decay/fan_only_decay/ventilated_decay
        use k_passive only; solar_gain computes mean rate; hvac_heat/hvac_cool use
        the existing two-parameter path.
        """
        if obs_type in ("passive_decay", "fan_only_decay", "ventilated_decay"):
            samples = event.get("samples", event.get("active_samples", []))
            k_p, r2_p, _reject_code = compute_k_passive(samples, min_samples=THERMAL_MIN_DECAY_SAMPLES)
            if k_p is None:
                _temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
                _indoor_dt = round(max(_temps) - min(_temps), 2) if len(_temps) >= 2 else 0.0
                _LOGGER.info(
                    "Thermal event commit failed (%s): k_passive rejected (R²=%.3f, n=%d, indoor_ΔT=%.2f°F) code=%s",
                    obs_type,
                    r2_p,
                    len(samples),
                    _indoor_dt,
                    _reject_code or "unknown",
                )
                return None, _reject_code, r2_p
            now_str = dt_util.now().isoformat()
            date_str = dt_util.now().date().isoformat()
            _decay_tag_map = {
                "passive_decay": "passive",
                "fan_only_decay": "fan_only",
                "ventilated_decay": "ventilated",
            }
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
            _LOGGER.info(
                "Thermal observation committed: mode=%s grade=%s k_passive=%.4f R²_p=%.3f",
                hvac_mode_tag,
                obs["confidence_grade"],
                k_p,
                r2_p,
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
            mean_rate = (indoor_temps[-1] - indoor_temps[0]) / total_dt_hours
            if mean_rate < 0:
                return None, REJECT_OLS_WRONG_SIGN, None
            now_str = dt_util.now().isoformat()
            date_str = dt_util.now().date().isoformat()
            obs = {
                "event_id": event.get("obs_id", event.get("event_id", str(uuid.uuid4()))),
                "timestamp": now_str,
                "date": date_str,
                "hvac_mode": "solar",
                "k_passive": None,
                "k_active": None,
                "k_solar": round(mean_rate, 3),
                "r_squared_passive": None,
                "r_squared_active": None,
                "sample_count_post": len(samples),
                "confidence_grade": force_grade or "low",
                "schema_version": 2,
            }
            _LOGGER.info(
                "Thermal observation committed: mode=solar grade=%s k_solar=%.3f",
                obs["confidence_grade"],
                mean_rate,
            )
            self.record_thermal_observation(obs)
            return obs, None, None

        # Default: hvac_heat / hvac_cool — original two-parameter path
        post_samples = event.get("post_heat_samples", [])
        pre_samples = event.get("pre_heat_samples", [])
        active_samples = event.get("active_samples", [])
        session_mode = event.get("session_mode") or event.get("hvac_mode") or "heat"

        k_p, r2_p, _reject_code = compute_k_passive(post_samples, pre_samples)
        if k_p is None:
            _LOGGER.info(
                "Thermal event commit failed: k_passive rejected (R²=%.3f, post_n=%d)",
                r2_p,
                len(post_samples),
            )
            return None, _reject_code, r2_p

        k_a, r2_a = compute_k_active(active_samples, k_p, session_mode)
        # k_a may be None for fan_only — that is acceptable

        # Passive baseline rate (mean of pre-heat rates)
        passive_baseline = 0.0
        if pre_samples and len(pre_samples) >= 2:
            indoor_pre = [s["indoor_temp_f"] for s in pre_samples]
            elapsed_pre = [s["elapsed_minutes"] for s in pre_samples]
            rates_pre: list[float] = []
            for i in range(len(indoor_pre) - 1):
                dt_h = (elapsed_pre[i + 1] - elapsed_pre[i]) / 60.0
                if dt_h > 0:
                    rates_pre.append((indoor_pre[i + 1] - indoor_pre[i]) / dt_h)
            if rates_pre:
                passive_baseline = sum(rates_pre) / len(rates_pre)

        # Aggregate outdoor temps
        all_samples = pre_samples + active_samples + post_samples
        outdoor_temps = [s["outdoor_temp_f"] for s in all_samples if s.get("outdoor_temp_f") is not None]
        avg_outdoor = sum(outdoor_temps) / len(outdoor_temps) if outdoor_temps else 0.0

        # Indoor stats
        all_indoor = [s["indoor_temp_f"] for s in all_samples if s.get("indoor_temp_f") is not None]
        peak_indoor = max(all_indoor) if all_indoor else (event.get("start_indoor_f") or 0.0)

        start_indoor = event.get("start_indoor_f") or (active_samples[0]["indoor_temp_f"] if active_samples else 0.0)
        end_indoor_samples = post_samples if post_samples else active_samples
        end_indoor = end_indoor_samples[-1]["indoor_temp_f"] if end_indoor_samples else start_indoor
        start_outdoor = active_samples[0]["outdoor_temp_f"] if active_samples else avg_outdoor
        delta_t_avg = (
            sum(s["indoor_temp_f"] - s["outdoor_temp_f"] for s in all_samples) / len(all_samples)
            if all_samples
            else 0.0
        )

        session_minutes = event.get("session_minutes") or 0.0
        if not session_minutes and active_samples and len(active_samples) >= 2:
            session_minutes = active_samples[-1]["elapsed_minutes"] - active_samples[0]["elapsed_minutes"]

        # Confidence grade
        if force_grade is not None:
            grade = force_grade
        else:
            n_post = len(post_samples)
            r2_thresh_high = 0.7
            r2_thresh_med = 0.4
            r2_ok_active = (r2_a is not None and r2_a >= r2_thresh_high) or session_mode == "fan_only"
            r2_ok_active_med = (r2_a is not None and r2_a >= r2_thresh_med) or session_mode == "fan_only"
            if r2_p >= r2_thresh_high and r2_ok_active and n_post >= 10:
                grade = "high"
            elif r2_p >= r2_thresh_med and r2_ok_active_med and n_post >= 5:
                grade = "medium"
            else:
                grade = "low"

        now_str = dt_util.now().isoformat()
        date_str = dt_util.now().date().isoformat()
        obs = {
            "event_id": event.get("event_id", str(uuid.uuid4())),
            "timestamp": now_str,
            "date": date_str,
            "hvac_mode": session_mode,
            "session_minutes": round(session_minutes, 1),
            "start_indoor_f": start_indoor,
            "end_indoor_f": end_indoor,
            "peak_indoor_f": peak_indoor,
            "start_outdoor_f": start_outdoor,
            "avg_outdoor_f": round(avg_outdoor, 1),
            "delta_t_avg": round(delta_t_avg, 2),
            "k_passive": round(k_p, 5),
            "k_active": round(k_a, 3) if k_a is not None else None,
            "passive_baseline_rate": round(passive_baseline, 3),
            "r_squared_passive": round(r2_p, 3),
            "r_squared_active": round(r2_a, 3) if r2_a is not None else None,
            "sample_count_pre": len(pre_samples),
            "sample_count_active": len(active_samples),
            "sample_count_post": len(post_samples),
            "confidence_grade": grade,
            "schema_version": 2,
        }

        _LOGGER.info(
            "Thermal observation committed: mode=%s grade=%s k_passive=%.4f k_active=%s R²_p=%.3f",
            session_mode,
            grade,
            k_p,
            f"{k_a:.3f}" if k_a is not None else "None",
            r2_p,
        )
        self.record_thermal_observation(obs)
        return obs, None, None

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
            except (ValueError, TypeError):
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
                        except (ValueError, IndexError):
                            pass

                    direction_word = "up" if up_count >= down_count else "down"
                    detail = f"mostly {direction_word} by about {avg_mag:.0f}\u00b0F"
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
                        f"{magnitude}\u00b0F {direction} than what actually occurs at your location. "
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

        if changes:
            _LOGGER.info(
                "Learning suggestion accepted — key=%s, changes=%s",
                suggestion_key,
                changes,
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
            self._state.pending_thermal_event = None
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

        # Comfort score (% of time in comfort range)
        total_day_minutes = len(recent) * 1440  # Minutes in a day
        total_violations = sum(r.get("comfort_violations_minutes", 0) for r in recent)
        comfort_score = max(0.0, 1 - (total_violations / total_day_minutes)) if total_day_minutes else 1.0

        return {
            "status": "active",
            "days_recorded": len(records),
            "window_compliance": window_compliance,
            "avg_daily_hvac_runtime_minutes": avg_runtime,
            "comfort_score": comfort_score,
            "total_manual_overrides": sum(r.get("manual_overrides", 0) for r in recent),
            "pending_suggestions": len(self.generate_suggestions()),
        }
