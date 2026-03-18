"""Learning engine for Climate Advisor.

Tracks human compliance with suggestions, HVAC runtime patterns, and
environmental outcomes to generate adaptive improvement suggestions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .const import (
    LEARNING_DB_FILE,
    MIN_DATA_POINTS_FOR_SUGGESTION,
    COMPLIANCE_THRESHOLD_LOW,
)

_LOGGER = logging.getLogger(__name__)


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

    # What actually happened
    windows_opened: bool = False
    window_open_actual_time: str | None = None
    window_close_actual_time: str | None = None
    hvac_runtime_minutes: float = 0.0
    occupancy_away_minutes: float = 0.0
    door_window_pause_events: int = 0
    manual_overrides: int = 0

    # Outcomes
    avg_indoor_temp: float | None = None
    comfort_violations_minutes: float = 0.0  # Time spent outside comfort range
    estimated_cost: float | None = None

    # User responded to suggestion?
    suggestion_sent: str | None = None
    suggestion_response: str | None = None  # "accepted", "dismissed", "ignored"


@dataclass
class LearningState:
    """Persistent learning state."""

    records: list[dict] = field(default_factory=list)
    active_suggestions: list[dict] = field(default_factory=list)
    dismissed_suggestions: list[str] = field(default_factory=list)
    settings_history: list[dict] = field(default_factory=list)


class LearningEngine:
    """Tracks patterns and generates adaptive suggestions."""

    def __init__(self, storage_path: Path) -> None:
        """Initialize the learning engine.

        Args:
            storage_path: Path to the HA config directory for persistent storage.
        """
        self._db_path = storage_path / LEARNING_DB_FILE
        self._state = self._load_state()

    def _load_state(self) -> LearningState:
        """Load learning state from disk."""
        if self._db_path.exists():
            try:
                data = json.loads(self._db_path.read_text())
                return LearningState(**data)
            except (json.JSONDecodeError, TypeError) as err:
                _LOGGER.warning("Failed to load learning state, starting fresh: %s", err)
        return LearningState()

    def _save_state(self) -> None:
        """Persist learning state to disk."""
        try:
            self._db_path.write_text(json.dumps(asdict(self._state), indent=2))
        except OSError as err:
            _LOGGER.error("Failed to save learning state: %s", err)

    def record_day(self, record: DailyRecord) -> None:
        """Record a day's data for learning.

        Args:
            record: The day's tracking data.
        """
        self._state.records.append(asdict(record))

        # Keep a rolling window (90 days)
        cutoff = (datetime.now() - timedelta(days=90)).isoformat()[:10]
        self._state.records = [
            r for r in self._state.records if r.get("date", "") >= cutoff
        ]

        self._save_state()

    def generate_suggestions(self) -> list[str]:
        """Analyze recent patterns and generate improvement suggestions.

        Returns:
            List of human-readable suggestion strings.
        """
        records = self._state.records
        if len(records) < MIN_DATA_POINTS_FOR_SUGGESTION:
            return []

        suggestions: list[str] = []
        recently_dismissed = set(self._state.dismissed_suggestions)

        # --- Pattern: Windows recommended but rarely opened ---
        window_days = [r for r in records if r.get("windows_recommended")]
        if len(window_days) >= 7:
            compliance = sum(1 for r in window_days if r.get("windows_opened")) / len(window_days)
            suggestion_key = "low_window_compliance"
            if compliance < COMPLIANCE_THRESHOLD_LOW and suggestion_key not in recently_dismissed:
                suggestions.append(
                    f"Over the past {len(window_days)} days where opening windows was recommended, "
                    f"they were opened only {compliance:.0%} of the time. "
                    f"Would you like Climate Advisor to stop suggesting window actions "
                    f"and instead rely on HVAC with optimized schedules? "
                    f"This uses slightly more energy but requires no manual action."
                )

        # --- Pattern: Frequent manual overrides ---
        recent_14 = records[-14:] if len(records) >= 14 else records
        total_overrides = sum(r.get("manual_overrides", 0) for r in recent_14)
        if total_overrides > 10:
            suggestion_key = "frequent_overrides"
            if suggestion_key not in recently_dismissed:
                # Try to detect direction — are they overriding up or down?
                suggestions.append(
                    f"You've manually adjusted the thermostat {total_overrides} times "
                    f"in the past two weeks. This may indicate the comfort setpoints "
                    f"don't match your preferences. Would you like Climate Advisor to "
                    f"analyze the override patterns and suggest new setpoints?"
                )

        # --- Pattern: High runtime on mild/warm days ---
        mild_warm_days = [
            r for r in recent_14
            if r.get("day_type") in ("mild", "warm")
        ]
        if mild_warm_days:
            avg_runtime = sum(r.get("hvac_runtime_minutes", 0) for r in mild_warm_days) / len(mild_warm_days)
            if avg_runtime > 120:  # More than 2 hours on mild/warm days
                suggestion_key = "high_runtime_mild_days"
                if suggestion_key not in recently_dismissed:
                    suggestions.append(
                        f"On mild and warm days, the HVAC has been running an average of "
                        f"{avg_runtime:.0f} minutes — more than expected. This could indicate "
                        f"doors/windows being left open, or the setpoint being too aggressive. "
                        f"Would you like to add more door/window sensors, or adjust the "
                        f"setpoints for mild days?"
                    )

        # --- Pattern: Leaving home frequently without setback taking effect ---
        away_days = [r for r in recent_14 if r.get("occupancy_away_minutes", 0) > 30]
        short_away = [r for r in away_days if r.get("occupancy_away_minutes", 0) < 45]
        if len(short_away) > 5:
            suggestion_key = "short_departures"
            if suggestion_key not in recently_dismissed:
                suggestions.append(
                    "You frequently leave for 30–45 minute periods, which is barely "
                    "long enough for the setback to take effect before you return. "
                    "Would you like to shorten the setback delay from 15 minutes to "
                    "5 minutes for these quick trips, or skip setback for departures "
                    "under 1 hour?"
                )

        # --- Pattern: Comfort violations (too cold/hot despite automation) ---
        violation_days = [
            r for r in recent_14 if r.get("comfort_violations_minutes", 0) > 30
        ]
        if len(violation_days) > 5:
            suggestion_key = "comfort_violations"
            if suggestion_key not in recently_dismissed:
                suggestions.append(
                    f"The house has been outside your comfort range for more than "
                    f"30 minutes on {len(violation_days)} of the last 14 days. "
                    f"Would you like to reduce the setback aggressiveness, or "
                    f"start the morning warm-up earlier?"
                )

        # --- Pattern: Door/window pauses happening frequently ---
        pause_total = sum(r.get("door_window_pause_events", 0) for r in recent_14)
        if pause_total > 20:
            suggestion_key = "frequent_door_pauses"
            if suggestion_key not in recently_dismissed:
                suggestions.append(
                    f"HVAC has been paused {pause_total} times due to open doors/windows "
                    f"in the past two weeks. If a specific door is the main culprit, "
                    f"would you like to extend the pause delay for that door, or "
                    f"exclude it from monitoring?"
                )

        return suggestions

    def dismiss_suggestion(self, suggestion_key: str) -> None:
        """Mark a suggestion as dismissed so it won't reappear soon."""
        self._state.dismissed_suggestions.append(suggestion_key)
        self._save_state()

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

        self._state.settings_history.append({
            "timestamp": datetime.now().isoformat(),
            "suggestion": suggestion_key,
            "changes": changes,
        })
        self._save_state()

        return changes

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
            sum(1 for r in window_days if r.get("windows_opened")) / len(window_days)
            if window_days
            else None
        )

        # Average runtime
        avg_runtime = sum(r.get("hvac_runtime_minutes", 0) for r in recent) / len(recent)

        # Comfort score (% of time in comfort range)
        total_day_minutes = len(recent) * 1440  # Minutes in a day
        total_violations = sum(r.get("comfort_violations_minutes", 0) for r in recent)
        comfort_score = 1 - (total_violations / total_day_minutes) if total_day_minutes else 1.0

        return {
            "status": "active",
            "days_recorded": len(records),
            "window_compliance": window_compliance,
            "avg_daily_hvac_runtime_minutes": avg_runtime,
            "comfort_score": comfort_score,
            "total_manual_overrides": sum(r.get("manual_overrides", 0) for r in recent),
            "pending_suggestions": len(self.generate_suggestions()),
        }
