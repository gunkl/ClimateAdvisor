"""Tests for the Investigative Agent AI skill (ai_skills_investigator.py) — Issue #82."""

from __future__ import annotations

import asyncio
import datetime
import sys
from unittest.mock import MagicMock, patch

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Inject a mock anthropic package so ClaudeAPIClient can be imported without a
# real anthropic installation (same pattern as test_claude_api.py).
if "anthropic" not in sys.modules:
    _mock_anthropic = MagicMock()
    _mock_anthropic.__name__ = "anthropic"
    _mock_anthropic.__path__ = []
    _mock_anthropic.__file__ = None
    _mock_anthropic.__spec__ = None
    _mock_anthropic.__loader__ = None
    _mock_anthropic.__package__ = "anthropic"
    _mock_anthropic.APIError = type("APIError", (Exception,), {})
    _mock_anthropic.APITimeoutError = type("APITimeoutError", (Exception,), {})
    _mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
    _mock_anthropic.AsyncAnthropic = MagicMock()
    sys.modules["anthropic"] = _mock_anthropic

from custom_components.climate_advisor.ai_skills import AISkillRegistry  # noqa: E402
from custom_components.climate_advisor.ai_skills_investigator import (  # noqa: E402
    async_build_investigator_context,
    investigation_fallback,
    parse_investigation_response,
    register_investigator_skill,
)
from custom_components.climate_advisor.claude_api import ClaudeAPIClient  # noqa: E402
from custom_components.climate_advisor.const import INVESTIGATION_REPORT_HISTORY_CAP  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EXPECTED_PARSE_KEYS = {
    "summary",
    "incongruities",
    "data_quality",
    "errors_warnings",
    "hypotheses",
    "recommended_actions",
    "assumptions",
    "full_text",
}

_EXPECTED_FALLBACK_KEYS = {
    "summary",
    "incongruities",
    "data_quality",
    "errors_warnings",
    "hypotheses",
    "recommended_actions",
    "assumptions",
    "full_text",
}

_TEST_KEY = "sk-ant-test-investigator-key"


def _make_learning_mock(
    suggestions: list | None = None,
    compliance: dict | None = None,
    records: list | None = None,
) -> MagicMock:
    """Build a minimal learning engine mock."""
    learning = MagicMock()
    learning.generate_suggestions.return_value = suggestions or []
    learning.get_compliance_summary.return_value = compliance or {
        "window_compliance": 0.0,
        "pending_suggestions": 0,
        "avg_daily_hvac_runtime_minutes": 120,
        "comfort_score": 0.9,
        "total_manual_overrides": 3,
    }
    learning.get_thermal_model.return_value = {
        "heating_rate_f_per_hour": 2.5,
        "cooling_rate_f_per_hour": 1.8,
        "confidence": "medium",
        "observation_count_heat": 10,
        "observation_count_cool": 8,
    }
    learning.get_weather_bias.return_value = {
        "high_bias": 1.2,
        "low_bias": -0.5,
        "confidence": "low",
        "observation_count": 5,
    }
    state_obj = MagicMock()
    state_obj.records = records or []
    learning._state = state_obj
    return learning


def _make_coordinator(
    data_overrides: dict | None = None,
    config_overrides: dict | None = None,
    learning=None,
    event_log: list | None = None,
) -> MagicMock:
    """Build a mock coordinator."""
    coord = MagicMock()
    coord.data = {
        "day_type": "mild",
        "trend": "stable",
        "hvac_action": "heating",
        "hvac_runtime_today": 45,
        "automation_status": "active",
        "last_action_time": "2026-04-06T08:00:00",
        "last_action_reason": "wake-up comfort restore",
        "next_automation_action": "bedtime setback",
        "next_automation_time": "22:30",
        "occupancy_mode": "home",
        "fan_status": "disabled",
        "contact_status": "all_closed",
    }
    if data_overrides:
        coord.data.update(data_overrides)

    coord.config = {
        "climate_entity": "climate.thermostat",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "ai_enabled": True,
        "ai_model": "claude-sonnet-4-6",
        "learning_enabled": True,
        "ai_api_key": _TEST_KEY,
    }
    if config_overrides:
        coord.config.update(config_overrides)

    coord.learning = learning if learning is not None else _make_learning_mock()
    coord._event_log = event_log or []
    coord.get_ai_report_history.return_value = [
        {"timestamp": "2026-04-05T10:00:00", "result": {"data": {"summary": "All fine yesterday."}}},
        {"timestamp": "2026-04-06T06:00:00", "result": {"data": {"summary": "Mild day, no issues."}}},
    ]
    return coord


def _make_hass() -> MagicMock:
    """Build a mock hass with a plausible climate entity."""
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "heat"
    climate_state.attributes = {"current_temperature": 70}
    hass.states.get.return_value = climate_state
    return hass


def _make_investigator_client(**overrides) -> ClaudeAPIClient:
    """Return a ClaudeAPIClient configured for the investigator."""
    config = {
        "ai_enabled": True,
        "ai_api_key": _TEST_KEY,
        "ai_model": "claude-sonnet-4-6",
        "ai_reasoning_effort": "medium",
        "ai_max_tokens": 4096,
        "ai_temperature": 0.3,
        "ai_monthly_budget": 0,
        "ai_auto_requests_per_day": 5,
        "ai_manual_requests_per_day": 20,
        "ai_investigator_enabled": True,
        "ai_investigator_requests_per_day": 3,
    }
    config.update(overrides)
    mock_inner = MagicMock()
    return ClaudeAPIClient(config=config, client=mock_inner)


# ---------------------------------------------------------------------------
# Group 1: parse_investigation_response
# ---------------------------------------------------------------------------


class TestParseInvestigationResponse:
    """Tests for the pure parse_investigation_response() function."""

    _FULL_RESPONSE = (
        "## INVESTIGATION SUMMARY\n"
        "The system appears healthy. One minor discrepancy found.\n"
        "\n"
        "## INCONGRUITIES FOUND\n"
        "- window_compliance=0.0 but windows_physically_opened=True on 2026-04-01\n"
        "\n"
        "## DATA QUALITY ISSUES\n"
        "- observation_count_heat is 0 despite 10 days of heating records\n"
        "\n"
        "## SYSTEM ERRORS / WARNINGS\n"
        "No errors or warnings in the supplied window.\n"
        "\n"
        "## HYPOTHESES\n"
        "1. Compliance counter was zeroed on coordinator restart. (High)\n"
        "2. Thermal model was not persisted across restarts. (Medium)\n"
        "\n"
        "## RECOMMENDED ACTIONS\n"
        "1. Check coordinator startup code for compliance counter reset.\n"
        "2. Verify thermal model persistence after restart.\n"
        "\n"
        "## ASSUMPTIONS & CONFIDENCE\n"
        "ASSUMPTION: Installation was completed in March 2026.\n"
        "Overall confidence: Medium.\n"
    )

    def test_all_seven_sections_parsed(self):
        """A response with all 7 headers populates every key."""
        result = parse_investigation_response(self._FULL_RESPONSE)

        assert "healthy" in result["summary"]
        assert "window_compliance=0.0" in result["incongruities"]
        assert "observation_count_heat" in result["data_quality"]
        assert "No errors or warnings" in result["errors_warnings"]
        assert "Compliance counter" in result["hypotheses"]
        assert "coordinator startup" in result["recommended_actions"]
        assert "ASSUMPTION" in result["assumptions"]

    def test_full_text_always_set_to_raw(self):
        """full_text is always the original raw string, unmodified."""
        result = parse_investigation_response(self._FULL_RESPONSE)

        assert result["full_text"] == self._FULL_RESPONSE

    def test_empty_string_returns_all_empty(self):
        """Empty input → all section values are empty string, full_text is ''."""
        result = parse_investigation_response("")

        assert result["full_text"] == ""
        for key in _EXPECTED_PARSE_KEYS - {"full_text"}:
            assert result[key] == "", f"Expected {key!r} to be empty, got {result[key]!r}"

    def test_unknown_headers_silently_skipped(self):
        """Content under unrecognised headers does not appear in any known section."""
        raw = (
            "## INVESTIGATION SUMMARY\n"
            "Known summary.\n"
            "\n"
            "## FUTURE_PLANS\n"
            "This content should be silently ignored.\n"
            "\n"
            "## HYPOTHESES\n"
            "1. A real hypothesis.\n"
        )
        result = parse_investigation_response(raw)

        assert "Known summary" in result["summary"]
        assert "real hypothesis" in result["hypotheses"]
        for key in _EXPECTED_PARSE_KEYS - {"full_text"}:
            assert "silently ignored" not in result[key]

    def test_only_investigation_summary_header(self):
        """Only summary is populated when only that header appears."""
        raw = "## INVESTIGATION SUMMARY\nJust a summary.\n"
        result = parse_investigation_response(raw)

        assert "Just a summary" in result["summary"]
        for key in _EXPECTED_PARSE_KEYS - {"summary", "full_text"}:
            assert result[key] == "", f"Expected {key!r} to be empty"

    def test_section_content_stripped_of_whitespace(self):
        """Leading and trailing whitespace around section content is stripped."""
        raw = "## INVESTIGATION SUMMARY\n\n   Trimmed content.   \n\n"
        result = parse_investigation_response(raw)

        assert result["summary"] == "Trimmed content."

    def test_returned_dict_has_all_expected_keys(self):
        """Result always contains exactly the 8 expected keys."""
        result = parse_investigation_response("some text with no headers")

        assert set(result.keys()) == _EXPECTED_PARSE_KEYS

    def test_multiline_section_body_preserved(self):
        """Internal newlines within a section body are preserved."""
        raw = "## HYPOTHESES\n1. First hypothesis.\n2. Second hypothesis.\n3. Third hypothesis.\n"
        result = parse_investigation_response(raw)

        assert "First hypothesis" in result["hypotheses"]
        assert "Second hypothesis" in result["hypotheses"]
        assert "Third hypothesis" in result["hypotheses"]

    def test_full_text_preserved_even_with_all_sections(self):
        """full_text is identical to input even after all sections are parsed."""
        result = parse_investigation_response(self._FULL_RESPONSE)

        # full_text must not be mutated by section flushing
        assert result["full_text"] is self._FULL_RESPONSE or result["full_text"] == self._FULL_RESPONSE


# ---------------------------------------------------------------------------
# Group 2: investigation_fallback
# ---------------------------------------------------------------------------


class TestInvestigationFallback:
    """Tests for the synchronous investigation_fallback() function."""

    def test_fallback_returns_all_expected_keys(self):
        """Fallback always returns a dict with all 8 expected keys."""
        coord = _make_coordinator()
        result = investigation_fallback(coord)

        assert set(result.keys()) == _EXPECTED_FALLBACK_KEYS

    def test_fallback_with_error_events_populates_errors_warnings(self):
        """Error events in the event log appear in errors_warnings."""
        now = datetime.datetime.now(datetime.UTC)
        error_event = {
            "type": "automation_error",
            "time": now.isoformat(),
            "detail": "setback failed",
        }
        coord = _make_coordinator(event_log=[error_event])

        result = investigation_fallback(coord)

        assert result["errors_warnings"] != "No errors or warnings in the supplied window."
        assert "automation_error" in result["errors_warnings"]

    def test_fallback_with_warning_events_populates_errors_warnings(self):
        """Warning events in the event log appear in errors_warnings."""
        now = datetime.datetime.now(datetime.UTC)
        warn_event = {
            "type": "sensor_warning",
            "time": now.isoformat(),
            "detail": "stale reading",
        }
        coord = _make_coordinator(event_log=[warn_event])

        result = investigation_fallback(coord)

        assert "sensor_warning" in result["errors_warnings"]

    def test_fallback_no_errors_says_no_errors(self):
        """With no error/warning events, errors_warnings says 'No errors'."""
        info_event = {
            "type": "setpoint_applied",
            "time": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        coord = _make_coordinator(event_log=[info_event])

        result = investigation_fallback(coord)

        assert "No errors" in result["errors_warnings"]

    def test_fallback_does_not_raise_with_none_learning(self):
        """Fallback is safe when coordinator.learning is None."""
        coord = _make_coordinator()
        coord.learning = None

        result = investigation_fallback(coord)

        assert isinstance(result, dict)
        assert set(result.keys()) == _EXPECTED_FALLBACK_KEYS

    def test_fallback_detects_recommended_not_opened(self):
        """Incongruity flagged when windows_recommended=True but windows_opened=False.

        The old code checked a non-existent window_compliance per-record field.
        The fix uses the actual DailyRecord fields: windows_recommended + windows_opened.
        """
        records = [
            {
                "date": "2026-04-01",
                "windows_recommended": True,
                "windows_opened": False,
                "windows_physically_opened": False,
                "hvac_runtime_minutes": 60,
                "manual_overrides": 1,
            }
        ]
        learning = _make_learning_mock(records=records)
        coord = _make_coordinator(learning=learning)

        result = investigation_fallback(coord)

        assert "windows_recommended=True" in result["incongruities"]
        assert "windows_opened=False" in result["incongruities"]

    def test_fallback_high_override_count_reported_as_data_quality(self):
        """total_manual_overrides > 50 is flagged as a data quality issue."""
        compliance = {
            "window_compliance": 0.8,
            "pending_suggestions": 0,
            "avg_daily_hvac_runtime_minutes": 90,
            "comfort_score": 0.9,
            "total_manual_overrides": 99,
        }
        learning = _make_learning_mock(compliance=compliance)
        coord = _make_coordinator(learning=learning)

        result = investigation_fallback(coord)

        assert "99" in result["data_quality"] or "unusually high" in result["data_quality"]

    def test_fallback_summary_reflects_issue_count(self):
        """Summary mentions the number of issues when any are found."""
        now = datetime.datetime.now(datetime.UTC)
        error_event = {"type": "automation_error", "time": now.isoformat()}
        coord = _make_coordinator(event_log=[error_event])

        result = investigation_fallback(coord)

        # Should mention issues were found
        assert "issue" in result["summary"].lower() or "error" in result["summary"].lower()

    def test_fallback_summary_says_no_issues_when_clean(self):
        """Summary says no obvious incongruities when none are found."""
        coord = _make_coordinator()

        result = investigation_fallback(coord)

        assert "no obvious" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Group 3: async_build_investigator_context
# ---------------------------------------------------------------------------


class TestAsyncBuildInvestigatorContext:
    """Tests for async_build_investigator_context()."""

    def test_context_is_non_empty_string(self):
        """Return value is always a non-empty string."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert isinstance(context, str)
        assert len(context) > 0

    def test_context_does_not_contain_api_key(self):
        """The ai_api_key is stripped from the config section of the context."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert _TEST_KEY not in context

    def test_context_contains_current_temperature(self):
        """Climate entity current_temperature appears in the context."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "70" in context  # current_temperature from mock climate state

    def test_context_contains_event_log_summary(self):
        """Event log section appears in the context."""
        now = datetime.datetime.now(datetime.UTC)
        events = [
            {"type": "setpoint_applied", "time": now.isoformat()},
            {"type": "setpoint_applied", "time": now.isoformat()},
            {"type": "override_detected", "time": now.isoformat()},
        ]
        coord = _make_coordinator(event_log=events)
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "EVENT LOG" in context

    def test_context_safe_when_learning_is_none(self):
        """No exception when coordinator.learning is None."""
        coord = _make_coordinator()
        coord.learning = None
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert isinstance(context, str)
        assert len(context) > 0

    def test_context_safe_when_hass_states_returns_none(self):
        """No exception when hass.states.get() returns None."""
        coord = _make_coordinator()
        hass = MagicMock()
        hass.states.get.return_value = None

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert isinstance(context, str)
        assert "unknown" in context  # hvac_mode falls back to unknown

    def test_context_includes_focus_kwarg(self):
        """When focus= is provided, it appears in the context output."""
        coord = _make_coordinator()
        hass = _make_hass()
        focus_text = "Why did the thermal model reset?"

        context = asyncio.run(async_build_investigator_context(hass, coord, focus=focus_text))

        assert focus_text in context

    def test_context_no_focus_section_when_empty(self):
        """When focus= is empty string, the INVESTIGATION FOCUS section is omitted."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord, focus=""))

        assert "INVESTIGATION FOCUS" not in context

    def test_context_contains_ai_report_history(self):
        """The recent AI activity reports section appears in the context."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "RECENT AI ACTIVITY REPORTS" in context

    def test_context_contains_configuration_section(self):
        """Configuration section appears and includes comfort temps."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "CONFIGURATION" in context
        assert "70" in context  # comfort_heat
        assert "75" in context  # comfort_cool

    def test_context_contains_learning_sections(self):
        """Learning engine sections (compliance, thermal, weather bias) appear."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "LEARNING" in context
        assert "COMPLIANCE" in context

    def test_context_safe_with_empty_coordinator_data(self):
        """No exception when coordinator.data is empty."""
        coord = _make_coordinator()
        coord.data = {}
        coord.config = {"ai_api_key": _TEST_KEY}
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord))

        assert isinstance(context, str)

    def test_context_hours_kwarg_affects_event_window_label(self):
        """Providing hours= kwarg changes the event window reported."""
        coord = _make_coordinator()
        hass = _make_hass()

        context = asyncio.run(async_build_investigator_context(hass, coord, hours=24))

        assert "24h" in context


# ---------------------------------------------------------------------------
# Group 4: register_investigator_skill
# ---------------------------------------------------------------------------


class TestRegisterInvestigatorSkill:
    """Tests for register_investigator_skill() and the resulting skill definition."""

    def test_skill_name_is_investigator(self):
        """The registered skill has the name 'investigator'."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert skill is not None
        assert skill.name == "investigator"

    def test_skill_accessible_via_registry(self):
        """After registration, registry.get('investigator') is not None."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        assert registry.get("investigator") is not None

    def test_skill_triggered_by_manual(self):
        """Investigator skill has triggered_by == 'manual'."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert skill.triggered_by == "manual"

    def test_skill_has_non_empty_description(self):
        """The skill has a non-empty string description."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert isinstance(skill.description, str)
        assert len(skill.description) > 0

    def test_skill_has_callable_context_builder(self):
        """The context_builder attribute is a callable."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert callable(skill.context_builder)

    def test_skill_has_callable_response_parser(self):
        """The response_parser attribute is a callable."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert callable(skill.response_parser)

    def test_skill_has_fallback(self):
        """The skill has a non-None fallback function."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        skill = registry.get("investigator")
        assert skill.fallback is not None
        assert callable(skill.fallback)

    def test_skill_appears_in_list_skills(self):
        """list_skills() includes the investigator after registration."""
        registry = AISkillRegistry()
        register_investigator_skill(registry)

        names = [entry["name"] for entry in registry.list_skills()]
        assert "investigator" in names


# ---------------------------------------------------------------------------
# Group 5: ClaudeAPIClient investigator rate limit methods
# ---------------------------------------------------------------------------


class TestInvestigatorRateLimit:
    """Tests for ClaudeAPIClient.check_investigator_rate_limit() and friends."""

    def test_returns_false_when_investigator_disabled(self):
        """check_investigator_rate_limit() returns (False, ...) when disabled."""
        client = _make_investigator_client(ai_investigator_enabled=False)

        allowed, reason = client.check_investigator_rate_limit()

        assert allowed is False
        assert reason  # non-empty reason string

    def test_returns_true_when_enabled_and_limit_not_reached(self):
        """Returns (True, '') when enabled and no requests have been made yet."""
        client = _make_investigator_client(
            ai_investigator_enabled=True,
            ai_investigator_requests_per_day=3,
        )

        allowed, reason = client.check_investigator_rate_limit()

        assert allowed is True
        assert reason == ""

    def test_returns_false_when_daily_limit_reached(self):
        """Returns (False, ...) after daily limit is consumed."""
        client = _make_investigator_client(
            ai_investigator_enabled=True,
            ai_investigator_requests_per_day=2,
        )
        client.increment_investigator_counter()
        client.increment_investigator_counter()

        allowed, reason = client.check_investigator_rate_limit()

        assert allowed is False
        assert "2" in reason  # mentions the count

    def test_increment_raises_counter(self):
        """increment_investigator_counter() increments _investigator_requests_today."""
        client = _make_investigator_client()

        assert client._investigator_requests_today == 0
        client.increment_investigator_counter()
        assert client._investigator_requests_today == 1
        client.increment_investigator_counter()
        assert client._investigator_requests_today == 2

    def test_counter_resets_when_date_changes(self):
        """Counter resets to 0 when _investigator_requests_date is yesterday."""
        client = _make_investigator_client(ai_investigator_requests_per_day=10)
        # Set counter as if it was used yesterday
        client._investigator_requests_today = 5
        yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        client._investigator_requests_date = yesterday

        # Calling check should trigger the reset
        allowed, _ = client.check_investigator_rate_limit()

        assert client._investigator_requests_today == 0
        assert allowed is True

    def test_limit_of_zero_means_unlimited(self):
        """A limit of 0 should not block requests (0 = unlimited)."""
        client = _make_investigator_client(
            ai_investigator_enabled=True,
            ai_investigator_requests_per_day=0,
        )
        # Make many increments
        for _ in range(100):
            client.increment_investigator_counter()

        allowed, _ = client.check_investigator_rate_limit()

        assert allowed is True


# ---------------------------------------------------------------------------
# Group 6: API endpoint logic (replicated as plain helpers)
# ---------------------------------------------------------------------------


def _simulate_investigate_post(
    coordinator: MagicMock,
    focus: str | None = None,
    hours: int = 48,
) -> tuple[int, str]:
    """Replicate the logic of ClimateAdvisorInvestigateView.post() as a plain function.

    Returns (status_code, body_text) for assertions.
    This avoids instantiating HomeAssistantView which has metaclass constraints.
    """
    from custom_components.climate_advisor.const import (
        CONF_AI_ENABLED,
        CONF_AI_INVESTIGATOR_ENABLED,
        DEFAULT_AI_ENABLED,
        DEFAULT_AI_INVESTIGATOR_ENABLED,
    )

    if not coordinator.config.get(CONF_AI_ENABLED, DEFAULT_AI_ENABLED):
        return 403, "AI features are not enabled"

    if not coordinator.config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED):
        return 403, "Investigative agent is not enabled"

    if coordinator.claude_client is None:
        return 503, "AI client not available"

    allowed, reason = coordinator.claude_client.check_investigator_rate_limit()
    if not allowed:
        return 429, reason

    return 200, "ok"


def _simulate_investigation_reports_get(coordinator: MagicMock) -> list[dict]:
    """Replicate the logic of ClimateAdvisorInvestigationReportsView.get()."""
    return coordinator.get_investigation_report_history()


class TestAPIEndpointLogic:
    """Tests for the investigate POST / investigation_reports GET endpoint logic."""

    def _make_api_coordinator(self, **config_overrides) -> MagicMock:
        config = {
            "ai_enabled": True,
            "ai_investigator_enabled": True,
        }
        config.update(config_overrides)
        coord = MagicMock()
        coord.config = config
        claude = MagicMock()
        claude.check_investigator_rate_limit.return_value = (True, "")
        coord.claude_client = claude
        coord.get_investigation_report_history.return_value = []
        return coord

    def test_post_ai_disabled_returns_403(self):
        """POST returns 403 when ai_enabled is False."""
        coord = self._make_api_coordinator(ai_enabled=False)

        status, body = _simulate_investigate_post(coord)

        assert status == 403
        assert "AI features are not enabled" in body

    def test_post_investigator_disabled_returns_403(self):
        """POST returns 403 when ai_investigator_enabled is False."""
        coord = self._make_api_coordinator(ai_investigator_enabled=False)

        status, body = _simulate_investigate_post(coord)

        assert status == 403
        assert "Investigative agent is not enabled" in body

    def test_post_no_client_returns_503(self):
        """POST returns 503 when claude_client is None."""
        coord = self._make_api_coordinator()
        coord.claude_client = None

        status, body = _simulate_investigate_post(coord)

        assert status == 503

    def test_post_rate_limit_reached_returns_429(self):
        """POST returns 429 when the investigator daily limit is reached."""
        coord = self._make_api_coordinator()
        coord.claude_client.check_investigator_rate_limit.return_value = (False, "limit reached")

        status, body = _simulate_investigate_post(coord)

        assert status == 429
        assert "limit reached" in body

    def test_post_all_checks_pass_returns_200(self):
        """POST returns 200 when all pre-conditions are satisfied."""
        coord = self._make_api_coordinator()

        status, _ = _simulate_investigate_post(coord)

        assert status == 200

    def test_get_reports_returns_coordinator_history(self):
        """GET returns the list from get_investigation_report_history()."""
        coord = self._make_api_coordinator()
        expected = [{"timestamp": "2026-04-06T10:00:00", "result": {"summary": "test"}}]
        coord.get_investigation_report_history.return_value = expected

        result = _simulate_investigation_reports_get(coord)

        assert result == expected

    def test_get_reports_returns_empty_list_initially(self):
        """GET returns [] when there are no stored reports."""
        coord = self._make_api_coordinator()
        coord.get_investigation_report_history.return_value = []

        result = _simulate_investigation_reports_get(coord)

        assert result == []


# ---------------------------------------------------------------------------
# Group 7: Investigation report persistence (coordinator methods)
# ---------------------------------------------------------------------------


class _SimpleReportStore:
    """Minimal implementation of the investigation report storage methods.

    Mirrors coordinator.async_store_investigation_report and
    coordinator.get_investigation_report_history without requiring a full
    coordinator or HA instance. Used to test the cap logic in isolation.
    """

    def __init__(self) -> None:
        self._investigation_report_history: list[dict] = []

    def get_investigation_report_history(self) -> list[dict]:
        return list(self._investigation_report_history)

    def store_investigation_report(self, result: dict) -> None:
        """Synchronous variant (no disk I/O) for unit testing."""
        entry = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "result": result,
        }
        self._investigation_report_history.append(entry)
        if len(self._investigation_report_history) > INVESTIGATION_REPORT_HISTORY_CAP:
            self._investigation_report_history = self._investigation_report_history[-INVESTIGATION_REPORT_HISTORY_CAP:]


class TestInvestigationReportPersistence:
    """Tests for investigation report storage — cap, structure, and initial state."""

    def test_history_is_empty_initially(self):
        """get_investigation_report_history() returns [] on a fresh store."""
        store = _SimpleReportStore()

        assert store.get_investigation_report_history() == []

    def test_store_adds_entry_with_timestamp_and_result(self):
        """Storing a result adds a dict with 'timestamp' and 'result' keys."""
        store = _SimpleReportStore()
        result = {"summary": "All good.", "source": "ai"}

        store.store_investigation_report(result)
        history = store.get_investigation_report_history()

        assert len(history) == 1
        entry = history[0]
        assert "timestamp" in entry
        assert "result" in entry
        assert entry["result"] == result

    def test_store_multiple_results_in_order(self):
        """Multiple stored results appear in insertion order."""
        store = _SimpleReportStore()
        for i in range(3):
            store.store_investigation_report({"index": i})

        history = store.get_investigation_report_history()

        assert len(history) == 3
        assert history[0]["result"]["index"] == 0
        assert history[2]["result"]["index"] == 2

    def test_store_trims_to_cap(self):
        """Storing more than INVESTIGATION_REPORT_HISTORY_CAP entries keeps only the cap."""
        store = _SimpleReportStore()
        overflow = INVESTIGATION_REPORT_HISTORY_CAP + 5
        for i in range(overflow):
            store.store_investigation_report({"index": i})

        history = store.get_investigation_report_history()

        assert len(history) == INVESTIGATION_REPORT_HISTORY_CAP

    def test_store_trims_oldest_entries(self):
        """After trimming, the oldest entries are discarded (most-recent kept)."""
        store = _SimpleReportStore()
        overflow = INVESTIGATION_REPORT_HISTORY_CAP + 5
        for i in range(overflow):
            store.store_investigation_report({"index": i})

        history = store.get_investigation_report_history()

        # The first remaining entry should be the (overflow - cap)th
        expected_first_index = overflow - INVESTIGATION_REPORT_HISTORY_CAP
        assert history[0]["result"]["index"] == expected_first_index

    def test_get_returns_copy_not_reference(self):
        """Mutating the returned list does not affect the internal store."""
        store = _SimpleReportStore()
        store.store_investigation_report({"x": 1})

        history = store.get_investigation_report_history()
        history.clear()

        assert len(store.get_investigation_report_history()) == 1


# ---------------------------------------------------------------------------
# Group 8: Fresh hvac_runtime_today calculation
# ---------------------------------------------------------------------------


class TestFreshHvacRuntime:
    """Tests for the fresh hvac_runtime_today calculation in context builders."""

    def test_investigator_context_uses_live_session_elapsed(self):
        """Context includes accumulated runtime when an HVAC session is active.

        Coordinator has _today_record.hvac_runtime_minutes=5.0 and
        _hvac_on_since set to 20 minutes ago.  The context should report ~25 min,
        not the stale coordinator.data value of 0.
        """
        coord = _make_coordinator(data_overrides={"hvac_runtime_today": 0})
        hass = _make_hass()

        # Set up the fresh-runtime fields
        now = datetime.datetime.now(datetime.UTC)
        on_since = now - datetime.timedelta(minutes=20)
        coord._today_record = MagicMock()
        coord._today_record.hvac_runtime_minutes = 5.0
        coord._hvac_on_since = on_since

        import custom_components.climate_advisor.ai_skills_investigator as _inv_mod

        with patch.object(_inv_mod.dt_util, "now", return_value=now):
            context = asyncio.run(async_build_investigator_context(hass, coord))

        # Expected: 5.0 + 20.0 = 25.0 min
        assert "25.0" in context

    def test_investigator_context_no_active_session(self):
        """When _hvac_on_since is None, only base runtime is used."""
        coord = _make_coordinator(data_overrides={"hvac_runtime_today": 0})
        hass = _make_hass()

        now = datetime.datetime.now(datetime.UTC)
        coord._today_record = MagicMock()
        coord._today_record.hvac_runtime_minutes = 42.0
        coord._hvac_on_since = None

        import custom_components.climate_advisor.ai_skills_investigator as _inv_mod

        with patch.object(_inv_mod.dt_util, "now", return_value=now):
            context = asyncio.run(async_build_investigator_context(hass, coord))

        assert "42.0" in context

    def test_investigator_context_no_today_record(self):
        """When _today_record is None, runtime defaults to 0.0."""
        coord = _make_coordinator(data_overrides={"hvac_runtime_today": 99})
        hass = _make_hass()

        now = datetime.datetime.now(datetime.UTC)
        coord._today_record = None
        coord._hvac_on_since = None

        import custom_components.climate_advisor.ai_skills_investigator as _inv_mod

        with patch.object(_inv_mod.dt_util, "now", return_value=now):
            context = asyncio.run(async_build_investigator_context(hass, coord))

        # Stale coordinator.data value (99) must NOT appear; fresh value (0.0) must appear
        assert "hvac_runtime_today:  0.0 min" in context


# ---------------------------------------------------------------------------
# Group 9: Compliance display in daily records
# ---------------------------------------------------------------------------


class TestComplianceDisplay:
    """Tests for the fixed window_rec= field in daily records context."""

    def _run_context_with_records(self, records: list) -> str:
        """Build investigator context with the given daily records and return it."""
        learning = _make_learning_mock(records=records)
        coord = _make_coordinator(learning=learning)
        hass = _make_hass()
        return asyncio.run(async_build_investigator_context(hass, coord))

    def test_recommended_and_opened_shows_opened(self):
        """window_rec=opened when windows_recommended=True and windows_physically_opened=True."""
        records = [
            {
                "date": "2026-04-01",
                "windows_recommended": True,
                "windows_physically_opened": True,
                "hvac_runtime_minutes": 30,
                "manual_overrides": 0,
            }
        ]
        context = self._run_context_with_records(records)

        assert "window_rec=opened" in context

    def test_recommended_but_not_opened_shows_not_opened(self):
        """window_rec=not-opened when recommended=True but physically opened=False."""
        records = [
            {
                "date": "2026-04-02",
                "windows_recommended": True,
                "windows_physically_opened": False,
                "hvac_runtime_minutes": 60,
                "manual_overrides": 1,
            }
        ]
        context = self._run_context_with_records(records)

        assert "window_rec=not-opened" in context

    def test_not_recommended_shows_na(self):
        """window_rec=n/a when windows_recommended=False regardless of opened state."""
        records = [
            {
                "date": "2026-04-03",
                "windows_recommended": False,
                "windows_physically_opened": True,
                "hvac_runtime_minutes": 20,
                "manual_overrides": 0,
            }
        ]
        context = self._run_context_with_records(records)

        assert "window_rec=n/a" in context

    def test_question_mark_compliance_never_appears(self):
        """The old 'compliance=?' placeholder is gone from the output."""
        records = [
            {
                "date": "2026-04-04",
                "windows_recommended": True,
                "windows_physically_opened": True,
                "hvac_runtime_minutes": 45,
                "manual_overrides": 2,
            }
        ]
        context = self._run_context_with_records(records)

        assert "compliance=?" not in context

    def test_falls_back_to_windows_opened_field(self):
        """When windows_physically_opened is absent, windows_opened is used as fallback."""
        records = [
            {
                "date": "2026-04-05",
                "windows_recommended": True,
                "windows_opened": True,  # no windows_physically_opened key
                "hvac_runtime_minutes": 15,
                "manual_overrides": 0,
            }
        ]
        context = self._run_context_with_records(records)

        assert "window_rec=opened" in context


# ---------------------------------------------------------------------------
# Group 10: Version context (Issue #105)
# ---------------------------------------------------------------------------


class TestInvestigatorVersionContext:
    """Version and release notes appear in investigator context (Issue #105)."""

    def test_context_includes_running_version(self):
        """_build_version_context returns string containing the current VERSION."""
        from custom_components.climate_advisor import const
        from custom_components.climate_advisor.ai_skills_investigator import _build_version_context

        # Temporarily patch VERSION
        original = const.VERSION
        try:
            const.VERSION = "0.0.test"
            result = _build_version_context(None)  # coordinator not needed for this helper
            assert "0.0.test" in result
        finally:
            const.VERSION = original

    def test_context_includes_release_notes_section(self):
        """_build_version_context includes a RELEASE NOTES header."""
        from custom_components.climate_advisor import const
        from custom_components.climate_advisor.ai_skills_investigator import _build_version_context

        original = getattr(const, "RELEASE_NOTES", {})
        try:
            const.RELEASE_NOTES = {"0.1.0": ["Fixed something"]}
            result = _build_version_context(None)
            assert "RELEASE NOTES" in result.upper()
            assert "Fixed something" in result
        finally:
            const.RELEASE_NOTES = original

    def test_context_includes_at_most_5_versions(self):
        """_build_version_context limits output to the 5 most recent versions."""
        from custom_components.climate_advisor import const
        from custom_components.climate_advisor.ai_skills_investigator import _build_version_context

        original = getattr(const, "RELEASE_NOTES", {})
        try:
            const.RELEASE_NOTES = {f"0.{i}.0": [f"Note {i}"] for i in range(10)}
            result = _build_version_context(None)
            # Count version headers; should be at most 5
            version_count = sum(1 for line in result.splitlines() if line.startswith("### v"))
            assert version_count <= 5
        finally:
            const.RELEASE_NOTES = original

    def test_startup_version_logged(self):
        """async_setup_entry logs the current VERSION at INFO level."""
        # This test verifies that _LOGGER.info is called with VERSION somewhere
        # in the async_setup_entry code path. Since we can't easily run full
        # setup, we check that the startup log call exists by importing and
        # inspecting the module.
        import importlib
        import inspect

        init_mod = importlib.import_module("custom_components.climate_advisor")
        source = inspect.getsource(init_mod.async_setup_entry)
        # The startup log should explicitly say "Climate Advisor" and reference VERSION
        # using the %s placeholder pattern (not just any _LOGGER.info call)
        assert "Climate Advisor" in source and "started" in source and "VERSION" in source


# ---------------------------------------------------------------------------
# Group 11: GitHub context (Issue #105)
# ---------------------------------------------------------------------------


class TestInvestigatorGithubContext:
    """async_build_github_context fetches and formats GitHub issues (Issue #105)."""

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def _make_mock_hass(self, mock_session):
        """Create a hass-like object that returns mock_session from aiohttp_client."""
        from unittest.mock import MagicMock

        hass = MagicMock()
        hass.helpers = MagicMock()
        hass.helpers.aiohttp_client = MagicMock()
        hass.helpers.aiohttp_client.async_get_clientsession = MagicMock(return_value=mock_session)
        return hass

    def _make_mock_session(self, status=200, json_data=None):
        """Create a mock aiohttp session that returns a response."""
        from unittest.mock import AsyncMock, MagicMock

        session = MagicMock()
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_data or [])
        # Support async context manager: async with session.get(...) as resp:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=cm)
        return session

    def test_github_context_includes_issues(self):
        from custom_components.climate_advisor.ai_skills_investigator import async_build_github_context

        issues = [
            {"number": 105, "state": "open", "title": "Enhancement: AI Investigator", "labels": []},
            {"number": 99, "state": "closed", "title": "Fix: Fan runs too long", "labels": [{"name": "bug"}]},
        ]
        session = self._make_mock_session(status=200, json_data=issues)
        hass = self._make_mock_hass(session)
        result = self._run(async_build_github_context(hass))
        assert "#105" in result
        assert "#99" in result
        assert "Enhancement: AI Investigator" in result

    def test_github_context_graceful_on_http_error(self):
        from custom_components.climate_advisor.ai_skills_investigator import async_build_github_context

        session = self._make_mock_session(status=404)
        hass = self._make_mock_hass(session)
        result = self._run(async_build_github_context(hass))
        assert result == ""

    def test_github_context_graceful_on_timeout(self):
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.ai_skills_investigator import async_build_github_context

        session = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=cm)
        hass = self._make_mock_hass(session)
        result = self._run(async_build_github_context(hass))
        assert result == ""

    def test_github_context_graceful_on_exception(self):
        from unittest.mock import MagicMock

        from custom_components.climate_advisor.ai_skills_investigator import async_build_github_context

        session = MagicMock()
        session.get = MagicMock(side_effect=RuntimeError("network error"))
        hass = self._make_mock_hass(session)
        result = self._run(async_build_github_context(hass))
        assert result == ""

    def test_github_context_labels_included(self):
        from custom_components.climate_advisor.ai_skills_investigator import async_build_github_context

        issues = [
            {"number": 42, "state": "open", "title": "A bug", "labels": [{"name": "bug"}, {"name": "urgent"}]},
        ]
        session = self._make_mock_session(status=200, json_data=issues)
        hass = self._make_mock_hass(session)
        result = self._run(async_build_github_context(hass))
        assert "bug" in result
        assert "urgent" in result


# ---------------------------------------------------------------------------
# Group: CA Operational Design context (Issue #113)
# ---------------------------------------------------------------------------


class TestCAOperationalDesignContext:
    """Verify async_build_investigator_context includes the CA operational design
    section added to stop the AI from hallucinating 'physical switch' explanations."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_context_includes_ca_operational_design_section(self):
        """Context output must contain the CA OPERATIONAL DESIGN header."""
        coord = _make_coordinator()
        hass = _make_hass()
        ctx = self._run(async_build_investigator_context(hass, coord))
        assert "CA OPERATIONAL DESIGN" in ctx

    def test_context_explains_fan_status_values(self):
        """Context must describe all four fan_status values so AI can interpret them."""
        coord = _make_coordinator()
        hass = _make_hass()
        ctx = self._run(async_build_investigator_context(hass, coord))
        assert "running (untracked)" in ctx
        assert "running (manual override)" in ctx
        assert "active" in ctx  # fan_status=active description

    def test_context_mentions_deadband(self):
        """Context must explain thermostat deadband so AI doesn't misread idle as failure."""
        coord = _make_coordinator()
        hass = _make_hass()
        ctx = self._run(async_build_investigator_context(hass, coord))
        assert "deadband" in ctx.lower()

    def test_context_clarifies_programmatic_control(self):
        """Context must assert CA has 100% programmatic control (no physical switch)."""
        coord = _make_coordinator()
        hass = _make_hass()
        ctx = self._run(async_build_investigator_context(hass, coord))
        assert "programmatic control" in ctx.lower() or "100%" in ctx


# ---------------------------------------------------------------------------
# Group: Window compliance per-record bug (Issue #113)
# ---------------------------------------------------------------------------


class TestWindowCompliancePerRecordFix:
    """Verify the investigator incongruity check uses per-record DailyRecord fields
    (windows_recommended, windows_opened) instead of the non-existent window_compliance
    per-record field.

    These tests use investigation_fallback() because incongruity detection lives there,
    not in the context builder (which only builds the AI prompt string).
    """

    def test_recommended_not_opened_flags_incongruity(self):
        """windows_recommended=True + windows_opened=False → incongruity detected."""
        records = [{"date": "2026-04-10", "windows_recommended": True, "windows_opened": False}]
        learning = _make_learning_mock(records=records)
        coord = _make_coordinator(learning=learning)
        result = investigation_fallback(coord)
        assert "windows_recommended=True" in result["incongruities"]
        assert "windows_opened=False" in result["incongruities"]

    def test_recommended_and_opened_no_incongruity(self):
        """windows_recommended=True + windows_opened=True → no incongruity for that record."""
        records = [{"date": "2026-04-10", "windows_recommended": True, "windows_opened": True}]
        learning = _make_learning_mock(records=records)
        coord = _make_coordinator(learning=learning)
        result = investigation_fallback(coord)
        # Compliant day — no incongruity entry for this specific check
        assert "2026-04-10: windows_recommended=True" not in result["incongruities"]

    def test_not_recommended_no_incongruity(self):
        """windows_recommended=False → not a compliance day, no incongruity."""
        records = [{"date": "2026-04-10", "windows_recommended": False, "windows_opened": False}]
        learning = _make_learning_mock(records=records)
        coord = _make_coordinator(learning=learning)
        result = investigation_fallback(coord)
        # Not a windows-recommended day — should not trigger incongruity
        assert "2026-04-10" not in result["incongruities"]
