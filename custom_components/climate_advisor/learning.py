"""Learning engine for Climate Advisor.

Tracks human compliance with suggestions, HVAC runtime patterns, and
environmental outcomes to generate adaptive improvement suggestions.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    COMPLIANCE_THRESHOLD_LOW,
    LEARNING_DB_FILE,
    MIN_DATA_POINTS_FOR_SUGGESTION,
    MIN_THERMAL_OBSERVATIONS,
    MIN_WEATHER_BIAS_OBSERVATIONS,
    THERMAL_MODEL_MAX_OBS,
    THERMAL_OBS_CAP,
    WEATHER_BIAS_MAX_OBS,
)

_LOGGER = logging.getLogger(__name__)

# Hard cap on daily records — 2 years of data is the absolute maximum.
# The 90-day rolling trim in record_day() normally keeps the list much shorter;
# this cap protects against edge cases (e.g., trim logic bypassed, time jumps).
MAX_DAILY_RECORDS = 730


@dataclass
class ThermalObservation:
    """A single observed HVAC heating or cooling session."""

    timestamp: str  # ISO8601 — when session ended
    date: str  # YYYY-MM-DD — for rolling trim
    hvac_mode: str  # "heat" or "cool"
    session_minutes: float
    rate_f_per_hour: float  # abs(end_indoor - start_indoor) / (session_minutes / 60)
    outdoor_temp_f: float
    start_indoor_f: float
    end_indoor_f: float


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
        cutoff = (datetime.now() - timedelta(days=90)).isoformat()[:10]
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
        """Record a thermal observation from an HVAC session.

        Trims to 90-day rolling window and enforces THERMAL_OBS_CAP.
        obs must include a "date" key (YYYY-MM-DD) for trimming.
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

    def get_thermal_model(self) -> dict:
        """Compute the thermal model from recent observations.

        Returns a dict with heating/cooling rates and confidence level.
        Pure computation — no I/O. Returns "none" confidence when insufficient data.
        """
        heat_obs = [o for o in self._state.thermal_observations if o.get("hvac_mode") == "heat"]
        cool_obs = [o for o in self._state.thermal_observations if o.get("hvac_mode") == "cool"]
        # Use only most recent observations
        heat_obs = heat_obs[-THERMAL_MODEL_MAX_OBS:]
        cool_obs = cool_obs[-THERMAL_MODEL_MAX_OBS:]

        heating_rate = sum(o["rate_f_per_hour"] for o in heat_obs) / len(heat_obs) if heat_obs else None
        cooling_rate = sum(o["rate_f_per_hour"] for o in cool_obs) / len(cool_obs) if cool_obs else None

        total = len(heat_obs) + len(cool_obs)
        if total < MIN_THERMAL_OBSERVATIONS:
            confidence = "none"
        elif total < 10:
            confidence = "low"
        elif total < 20:
            confidence = "medium"
        else:
            confidence = "high"

        return {
            "heating_rate_f_per_hour": round(heating_rate, 2) if heating_rate is not None else None,
            "cooling_rate_f_per_hour": round(cooling_rate, 2) if cooling_rate is not None else None,
            "observation_count_heat": len(heat_obs),
            "observation_count_cool": len(cool_obs),
            "confidence": confidence,
        }

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
                        "I've now observed enough HVAC sessions to estimate your home's heating "
                        "and cooling performance. Pre-heat timing and bedtime setbacks will now "
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
