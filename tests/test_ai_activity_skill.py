"""Tests for the Activity Report AI skill (ai_skills_activity.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills import AISkillRegistry
from custom_components.climate_advisor.ai_skills_activity import (
    activity_fallback,
    async_build_activity_context,
    parse_activity_response,
    register_activity_skill,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_coordinator(data_overrides: dict | None = None, options_overrides: dict | None = None) -> MagicMock:
    """Build a mock coordinator with realistic default data."""
    coord = MagicMock()
    coord.data = {
        "day_type": "mild",
        "trend_direction": "stable",
        "automation_status": "active",
        "occupancy_mode": "home",
        "fan_status": "disabled",
        "contact_status": "all_closed",
        "last_action_time": "2024-01-15T10:30:00",
        "last_action_reason": "daily classification — mild day",
        "next_automation_action": "bedtime setback",
        "next_automation_time": "22:30",
        "pending_suggestions": [],
    }
    if data_overrides:
        coord.data.update(data_overrides)

    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "climate_entity": "climate.thermostat",
    }
    if options_overrides:
        coord.config_entry.options.update(options_overrides)

    return coord


def _mock_hass() -> MagicMock:
    """Build a mock hass with a plausible climate entity state."""
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "heat"
    climate_state.attributes = {"current_temperature": 70, "temperature": 72}
    hass.states.get.return_value = climate_state
    return hass


# ---------------------------------------------------------------------------
# TestParseActivityResponse
# ---------------------------------------------------------------------------


class TestParseActivityResponse:
    """Tests for parse_activity_response()."""

    def test_parse_well_formed_response(self):
        """All 5 section headers produce correctly populated dict entries."""
        raw = (
            "## SUMMARY\n"
            "System running normally. Mild day detected.\n"
            "\n"
            "## TIMELINE\n"
            "06:30 — wake-up comfort restore\n"
            "22:30 — bedtime setback applied\n"
            "\n"
            "## DECISIONS\n"
            "Mild day: used heat mode. Setback applied at configured sleep time.\n"
            "\n"
            "## ANOMALIES\n"
            "No anomalies detected.\n"
            "\n"
            "## DIAGNOSTICS\n"
            "Automation engine active. All sensors connected.\n"
        )

        result = parse_activity_response(raw)

        assert "System running normally" in result["summary"]
        assert "Mild day detected" in result["summary"]
        assert "06:30" in result["timeline"]
        assert "22:30 — bedtime setback applied" in result["timeline"]
        assert "Mild day" in result["decisions"]
        assert "No anomalies detected" in result["anomalies"]
        assert "Automation engine active" in result["diagnostics"]

    def test_parse_missing_sections(self):
        """Only the present sections are populated; absent ones default to empty string."""
        raw = "## SUMMARY\nBrief overview.\n\n## DIAGNOSTICS\nAll systems nominal.\n"

        result = parse_activity_response(raw)

        assert "Brief overview" in result["summary"]
        assert "All systems nominal" in result["diagnostics"]
        assert result["timeline"] == ""
        assert result["decisions"] == ""
        assert result["anomalies"] == ""

    def test_parse_malformed_response(self):
        """Text with no section headers is handled without raising."""
        raw = "This is just some free-form text with no markdown headers at all."

        result = parse_activity_response(raw)

        # Parser should return a dict with all expected keys
        assert set(result.keys()) == {"summary", "timeline", "decisions", "anomalies", "diagnostics"}
        # All sections are empty (no headers were found to assign content to)
        for key in result:
            assert isinstance(result[key], str)

    def test_parse_empty_response(self):
        """Empty input string returns a dict with all sections as empty strings."""
        result = parse_activity_response("")

        assert set(result.keys()) == {"summary", "timeline", "decisions", "anomalies", "diagnostics"}
        for key in result:
            assert result[key] == ""

    def test_parse_unknown_headers_are_discarded(self):
        """Content under an unrecognised header is not placed into any section."""
        raw = (
            "## SUMMARY\n"
            "Known summary text.\n"
            "\n"
            "## UNKNOWN_SECTION\n"
            "This content should be discarded.\n"
            "\n"
            "## TIMELINE\n"
            "Timeline entry.\n"
        )

        result = parse_activity_response(raw)

        assert "Known summary text" in result["summary"]
        assert "Timeline entry" in result["timeline"]
        # The unknown section content must not appear anywhere
        for key in result:
            assert "This content should be discarded" not in result[key]

    def test_parse_strips_leading_trailing_whitespace(self):
        """Section content has surrounding whitespace stripped."""
        raw = "## SUMMARY\n\n   Leading and trailing spaces.   \n\n"

        result = parse_activity_response(raw)

        assert result["summary"] == "Leading and trailing spaces."

    def test_parse_multiline_sections(self):
        """Multi-line section bodies are preserved verbatim (internal newlines kept)."""
        raw = "## DECISIONS\nLine one.\nLine two.\nLine three.\n"

        result = parse_activity_response(raw)

        assert "Line one." in result["decisions"]
        assert "Line two." in result["decisions"]
        assert "Line three." in result["decisions"]


# ---------------------------------------------------------------------------
# TestActivityFallback
# ---------------------------------------------------------------------------


class TestActivityFallback:
    """Tests for activity_fallback()."""

    def test_fallback_output_has_all_expected_keys(self):
        """Fallback always returns a dict with the same keys as parse_activity_response."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert set(result.keys()) == {"summary", "timeline", "decisions", "anomalies", "diagnostics"}

    def test_fallback_summary_includes_automation_status_and_day_type(self):
        """Summary line reflects the coordinator's automation_status and day_type."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert "active" in result["summary"]
        assert "mild" in result["summary"]

    def test_fallback_timeline_includes_last_action(self):
        """Timeline includes the last action time and reason from coordinator data."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert "2024-01-15T10:30:00" in result["timeline"]
        assert "mild day" in result["timeline"]

    def test_fallback_timeline_includes_next_action(self):
        """Timeline notes the next scheduled automation action."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert "bedtime setback" in result["timeline"]
        assert "22:30" in result["timeline"]

    def test_fallback_decisions_includes_last_reason(self):
        """Decisions section reports the last action reason."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert "mild day" in result["decisions"].lower()

    def test_fallback_diagnostics_includes_status_fields(self):
        """Diagnostics includes automation status, fan status, and contact status."""
        coord = _mock_coordinator()

        result = activity_fallback(coord)

        assert "active" in result["diagnostics"]
        assert "disabled" in result["diagnostics"]
        assert "all_closed" in result["diagnostics"]

    def test_fallback_anomalies_flags_open_contact_sensor(self):
        """Open contact sensor state is reported as an anomaly."""
        coord = _mock_coordinator(data_overrides={"contact_status": "open — 2 sensors"})

        result = activity_fallback(coord)

        assert "open" in result["anomalies"].lower()

    def test_fallback_anomalies_empty_when_normal(self):
        """No anomalies are reported when contact_status is all_closed."""
        coord = _mock_coordinator(data_overrides={"contact_status": "all_closed"})

        result = activity_fallback(coord)

        assert "No anomalies detected" in result["anomalies"]

    def test_fallback_handles_missing_data_keys(self):
        """Fallback does not raise when coordinator.data is missing expected keys."""
        coord = MagicMock()
        coord.data = {}  # completely empty

        result = activity_fallback(coord)

        assert isinstance(result, dict)
        assert set(result.keys()) == {"summary", "timeline", "decisions", "anomalies", "diagnostics"}

    def test_fallback_handles_none_data(self):
        """Fallback does not raise when coordinator.data is None."""
        coord = MagicMock()
        coord.data = None

        result = activity_fallback(coord)

        assert isinstance(result, dict)

    def test_fallback_timeline_graceful_when_no_actions(self):
        """Timeline returns a sensible message when no actions have been recorded."""
        coord = _mock_coordinator(
            data_overrides={
                "last_action_time": "unknown",
                "last_action_reason": "unknown",
                "next_automation_action": "unknown",
                "next_automation_time": "unknown",
            }
        )

        result = activity_fallback(coord)

        assert "No recent events recorded" in result["timeline"]


# ---------------------------------------------------------------------------
# TestAsyncBuildActivityContext
# ---------------------------------------------------------------------------


class TestAsyncBuildActivityContext:
    """Tests for async_build_activity_context()."""

    def test_context_builder_output_contains_classification_section(self):
        """Output includes a CLASSIFICATION block with day type and trend."""
        coord = _mock_coordinator()
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "CLASSIFICATION" in context
        assert "mild" in context
        assert "stable" in context

    def test_context_builder_output_contains_automation_state_section(self):
        """Output includes an AUTOMATION STATE block."""
        coord = _mock_coordinator()
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "AUTOMATION STATE" in context
        assert "active" in context

    def test_context_builder_output_contains_configuration_section(self):
        """Output includes a CONFIGURATION block with comfort setpoints."""
        coord = _mock_coordinator()
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "CONFIGURATION" in context
        assert "70" in context  # comfort_heat
        assert "75" in context  # comfort_cool

    def test_context_builder_reads_hvac_mode_from_hass(self):
        """HVAC mode is read from the climate entity via hass.states.get()."""
        coord = _mock_coordinator()
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "heat" in context  # climate_state.state = "heat"

    def test_context_builder_handles_missing_climate_entity(self):
        """No raise when climate_entity option is absent or entity state is None."""
        coord = _mock_coordinator(options_overrides={"climate_entity": ""})
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert isinstance(context, str)
        assert len(context) > 0

    def test_context_builder_handles_none_climate_state(self):
        """No raise when hass.states.get() returns None for the climate entity."""
        coord = _mock_coordinator()
        hass = MagicMock()
        hass.states.get.return_value = None

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert isinstance(context, str)
        assert "unknown" in context  # hvac_mode and current_temp default to "unknown"

    def test_context_builder_handles_missing_coordinator_data(self):
        """No raise when coordinator.data is empty."""
        coord = MagicMock()
        coord.data = {}
        coord.config_entry = MagicMock()
        coord.config_entry.options = {}
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert isinstance(context, str)

    def test_context_builder_includes_learning_section(self):
        """Output includes a LEARNING block with suggestion count."""
        coord = _mock_coordinator(
            data_overrides={
                "pending_suggestions": [
                    {"suggestion_type": "setback_adjust"},
                    {"suggestion_type": "preheat_adjust"},
                ]
            }
        )
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "LEARNING" in context
        assert "2 pending" in context

    def test_context_builder_includes_active_features_section(self):
        """Output includes an ACTIVE FEATURES block."""
        coord = _mock_coordinator(
            options_overrides={
                "adaptive_preheat_enabled": True,
                "adaptive_setback_enabled": False,
                "weather_bias_enabled": True,
            }
        )
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert "ACTIVE FEATURES" in context

    def test_context_builder_returns_string(self):
        """Return value is always a non-empty string."""
        coord = _mock_coordinator()
        hass = _mock_hass()

        context = asyncio.run(async_build_activity_context(hass, coord))

        assert isinstance(context, str)
        assert len(context) > 0


# ---------------------------------------------------------------------------
# TestRegisterActivitySkill
# ---------------------------------------------------------------------------


class TestRegisterActivitySkill:
    """Tests for register_activity_skill()."""

    def test_register_activity_skill_adds_to_registry(self):
        """After calling register_activity_skill, the registry contains 'activity_report'."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert skill is not None

    def test_register_activity_skill_name(self):
        """The registered skill has name 'activity_report'."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert skill.name == "activity_report"

    def test_register_activity_skill_triggered_by_manual(self):
        """The activity skill is triggered_by='manual' (counts against manual limit)."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert skill.triggered_by == "manual"

    def test_register_activity_skill_has_fallback(self):
        """The activity skill has a fallback function (not None)."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert skill.fallback is not None

    def test_register_activity_skill_has_description(self):
        """The activity skill has a non-empty description string."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert isinstance(skill.description, str)
        assert len(skill.description) > 0

    def test_register_activity_skill_list_shows_it(self):
        """list_skills() includes the activity_report entry after registration."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        names = [entry["name"] for entry in registry.list_skills()]
        assert "activity_report" in names

    def test_register_activity_skill_parser_is_callable(self):
        """The response_parser attribute is a callable."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert callable(skill.response_parser)

    def test_register_activity_skill_context_builder_is_callable(self):
        """The context_builder attribute is a callable."""
        registry = AISkillRegistry()
        register_activity_skill(registry)

        skill = registry.get("activity_report")
        assert callable(skill.context_builder)
