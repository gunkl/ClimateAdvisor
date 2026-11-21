"""Coverage for the investigator's context builder and deterministic fallback (ai_skills_investigator.py).

Occupant view: when the AI provider is unavailable, the occupant still gets a report from the deterministic scan:
windows recommended but not opened, an implausible override count, nat-vent thrashing, the same setpoint
rejected over and over. A broken data source is skipped, never allowed to sink the whole report.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.ai_skills_context import ContextProvider
from custom_components.climate_advisor.ai_skills_investigator import (
    async_build_investigator_context,
    investigation_fallback,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _ago(minutes: float) -> str:
    return (_now() - datetime.timedelta(minutes=minutes)).isoformat()


def _learning(*, records=None, compliance=None, suggestions=None, state=True) -> MagicMock:
    learning = MagicMock()
    learning.get_compliance_summary.return_value = compliance or {}
    if isinstance(suggestions, list) or suggestions is None:
        learning.generate_suggestions.return_value = suggestions or []
    else:
        learning.generate_suggestions.side_effect = suggestions
    if state:
        learning._state = MagicMock(records=records if records is not None else [])
    else:
        learning._state = None
    return learning


def _coord(*, learning=None, events=None, config=None) -> MagicMock:
    coord = MagicMock()
    coord.learning = learning if learning is not None else _learning()
    coord._event_log = events or []
    coord.config = config if config is not None else {"temp_unit": "fahrenheit"}
    return coord


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def test_empty_sections_are_dropped_and_a_broken_footer_is_skipped():
    async def _empty(hass, coordinator, **kwargs):
        return ""

    async def _full(hass, coordinator, **kwargs):
        return "=== FULL ===\n  data"

    registry = MagicMock()
    registry.select.return_value = [
        ContextProvider(name="empty", tags=frozenset(), builder=_empty, priority=1),
        ContextProvider(name="full", tags=frozenset(), builder=_full, priority=2),
    ]

    async def _boom(hass, coordinator):
        raise RuntimeError("snapshot unavailable")

    with (
        patch("custom_components.climate_advisor.ai_skills_context.get_provider_registry", return_value=registry),
        patch("custom_components.climate_advisor.diagnostic_snapshot.async_build_diagnostic_snapshot", _boom),
        patch("custom_components.climate_advisor.i18n.hass_language_tag", return_value="en"),
    ):
        statuses: list[str] = []

        async def _status(msg):
            statuses.append(msg)

        context = asyncio.run(async_build_investigator_context(MagicMock(), _coord(), status_callback=_status))
    assert statuses == ["Loading empty…", "Loading full…"]
    assert context.splitlines() == ["=== Climate Advisor Investigator Context ===", "", "=== FULL ===", "  data"]


# ---------------------------------------------------------------------------
# Fallback: learning checks
# ---------------------------------------------------------------------------


class TestFallbackLearning:
    def test_recommended_but_unopened_windows_and_odd_records(self):
        records = [
            "not-a-dict",
            {"date": "2026-11-01", "windows_recommended": True, "windows_opened": False},
            {"date": "2026-11-02", "windows_recommended": True, "windows_opened": True},
        ]
        result = investigation_fallback(_coord(learning=_learning(records=records)))
        assert "Record 2026-11-01: windows_recommended=True but windows_opened=False" in result["incongruities"]
        assert "2026-11-02" not in result["incongruities"]

    def test_no_state_or_non_list_records_finds_nothing(self):
        assert investigation_fallback(_coord(learning=_learning(state=False)))["incongruities"] == "None detected."
        learning = _learning()
        learning._state.records = "corrupt"
        assert investigation_fallback(_coord(learning=learning))["incongruities"] == "None detected."

    def test_a_broken_record_is_skipped(self):
        class _BadRecord(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("bad record")

        result = investigation_fallback(_coord(learning=_learning(records=[_BadRecord()])))
        assert result["incongruities"] == "None detected."

    def test_zero_compliance_with_low_compliance_suggestion_is_flagged(self):
        learning = _learning(
            compliance={"window_compliance": 0.0},
            suggestions=[{"suggestion_type": "low_window_compliance"}],
        )
        result = investigation_fallback(_coord(learning=learning))
        assert "window_compliance is 0.0 but a 'low_window_compliance' suggestion exists" in result["incongruities"]

    def test_unparseable_compliance_and_override_counts_are_ignored(self):
        learning = _learning(
            compliance={"window_compliance": "n/a", "total_manual_overrides": "many"},
            suggestions=[{"suggestion_type": "low_window_compliance"}],
        )
        result = investigation_fallback(_coord(learning=learning))
        assert result["incongruities"] == "None detected." and result["data_quality"] == "None detected."

    def test_implausible_override_totals_are_flagged(self):
        learning = _learning(
            compliance={"total_manual_overrides": 51},
            suggestions=[
                "not-a-dict",
                {"suggestion_type": "frequent_overrides", "evidence": {"override_count": 77}},
                {"suggestion_type": "frequent_overrides", "evidence": {"override_count": "lots"}},
                {"suggestion_type": "frequent_overrides", "evidence": "not-a-dict"},
            ],
        )
        quality = investigation_fallback(_coord(learning=learning))["data_quality"]
        assert "total_manual_overrides=51 is unusually high (>50)." in quality
        assert "frequent_overrides suggestion cites override_count=77" in quality
        assert "lots" not in quality

    def test_failing_learning_calls_never_sink_the_report(self):
        learning = _learning()
        learning.get_compliance_summary.side_effect = RuntimeError("compliance broke")
        learning.generate_suggestions.side_effect = RuntimeError("suggestions broke")
        result = investigation_fallback(_coord(learning=learning))
        assert result["incongruities"] == "None detected." and result["data_quality"] == "None detected."

    def test_unreadable_learning_engine_is_skipped(self):
        coord = _coord()
        type(coord).learning = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
        try:
            result = investigation_fallback(coord)
        finally:
            del type(coord).learning
        assert result["incongruities"] == "None detected."


# ---------------------------------------------------------------------------
# Fallback: event-log scans
# ---------------------------------------------------------------------------


class TestFallbackEvents:
    def test_bad_hours_skips_every_scan_but_still_returns_a_report(self):
        result = investigation_fallback(_coord(events=[{"type": "error_x", "time": _ago(5)}]), hours="abc")
        assert result["errors_warnings"] == "No errors or warnings in the supplied window."
        assert result["activity_summary"] == "No activity recorded in the analyzed window."

    def test_nat_vent_cycling_counts_only_nat_vent_transitions(self):
        naive_now = _now().replace(tzinfo=None)
        events = [
            # Plain fan-only activity is not nat-vent thrash.
            {"type": "fan_activated", "reason": "manual fan", "time": _ago(30)},
            {"type": "fan_deactivated", "reason": "manual fan", "time": _ago(29)},
            # Missing or unparseable times are skipped.
            {"type": "nat_vent_comfort_floor_exit"},
            {"type": "nat_vent_comfort_floor_exit", "time": "not-a-time"},
            # Real nat-vent transitions: a naive datetime, a naive ISO string, and a nat-vent fan event.
            {"type": "nat_vent_outdoor_rise_exit", "time": naive_now - datetime.timedelta(minutes=20)},
            {"type": "nat_vent_away_ceiling_exit", "time": (naive_now - datetime.timedelta(minutes=10)).isoformat()},
            {"type": "fan_activated", "reason": "natural vent cooling", "time": _ago(5)},
        ]
        incongruities = investigation_fallback(_coord(events=events))["incongruities"]
        assert "Rapid nat-vent cycling detected: 3 transitions" in incongruities

    def test_repeated_setpoint_rejection_is_flagged_unless_nudged(self):
        events = [
            {"type": "setpoint_rejected", "time": _ago(30)},  # no commanded value
            {"type": "setpoint_rejected", "commanded": "abc", "time": _ago(29)},
            {"type": "setpoint_rejected", "commanded": 72, "time": _ago(28)},
            {"type": "setpoint_rejected", "commanded": 72, "time": _ago(27)},
            {"type": "setpoint_rejected", "commanded": 68, "time": _ago(26)},  # only once
            {"type": "setpoint_rejected", "commanded": 75, "time": _ago(25)},
            {"type": "setpoint_rejected", "commanded": 75, "time": _ago(24)},
            {"type": "setpoint_nudge", "commanded": 75, "time": _ago(23)},  # already being corrected
        ]
        quality = investigation_fallback(_coord(events=events))["data_quality"]
        assert (
            quality == "Setpoint 72°F rejected 2 times without resolving — possible device-level command deduplication"
        )
