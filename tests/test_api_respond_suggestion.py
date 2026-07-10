"""Tests for respond_suggestion endpoint feedback handling (Issue #84).

Exercises the real ClimateAdvisorRespondSuggestionView.post() handler —
HomeAssistantView is now a real minimal base class (Issue #452), so the
view no longer needs to be hand-replicated in this test file.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.api import ClimateAdvisorRespondSuggestionView
from custom_components.climate_advisor.const import API_RESPOND_SUGGESTION, DOMAIN


def _make_request(body: dict, coordinator) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}
    hass.async_add_executor_job = AsyncMock()

    req = MagicMock()
    req.app = {"hass": hass}
    req.json = AsyncMock(return_value=body)
    return req


def _post(body: dict, coordinator):
    view = ClimateAdvisorRespondSuggestionView()
    request = _make_request(body, coordinator)
    return asyncio.run(view.post(request))


def _coord() -> MagicMock:
    coord = MagicMock()
    coord.config = {}
    coord.learning.record_feedback = MagicMock()
    coord.learning.dismiss_suggestion = MagicMock()
    coord.learning.accept_suggestion = MagicMock(return_value={})
    coord.learning.save_state = MagicMock()
    return coord


# ---------------------------------------------------------------------------
# TestRespondSuggestionEndpoint — route constant
# ---------------------------------------------------------------------------


class TestRespondSuggestionEndpoint:
    """Verify the endpoint path constant is correctly defined."""

    def test_path_is_under_api_base(self):
        assert API_RESPOND_SUGGESTION.startswith("/api/climate_advisor/")

    def test_path_contains_respond_suggestion(self):
        assert "respond_suggestion" in API_RESPOND_SUGGESTION


# ---------------------------------------------------------------------------
# TestRespondSuggestionFeedback — real view.post() tests
# ---------------------------------------------------------------------------


class TestRespondSuggestionFeedback:
    """Tests against the real ClimateAdvisorRespondSuggestionView.post() handler."""

    def test_feedback_correct_recorded(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance", "feedback": "correct"}, coord)
        assert resp.status == 200
        assert resp.json_data["status"] == "ok"
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "correct")

    def test_feedback_incorrect_recorded(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance", "feedback": "incorrect"}, coord)
        assert resp.status == 200
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "incorrect")

    def test_feedback_invalid_value_rejected(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance", "feedback": "maybe"}, coord)
        assert resp.status == 400
        coord.learning.record_feedback.assert_not_called()

    def test_feedback_requires_suggestion_key(self):
        coord = _coord()
        resp = _post({"feedback": "correct"}, coord)
        assert resp.status == 400
        coord.learning.record_feedback.assert_not_called()

    def test_action_and_feedback_together(self):
        coord = _coord()
        resp = _post(
            {
                "suggestion_key": "low_window_compliance",
                "action": "dismiss",
                "feedback": "incorrect",
            },
            coord,
        )
        assert resp.status == 200
        assert resp.json_data["dismissed"] == "low_window_compliance"
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "incorrect")
        coord.learning.dismiss_suggestion.assert_called_once_with("low_window_compliance")

    def test_feedback_only_no_action_returns_ok(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance", "feedback": "correct"}, coord)
        assert resp.status == 200
        coord.learning.dismiss_suggestion.assert_not_called()
        coord.learning.accept_suggestion.assert_not_called()

    def test_no_action_no_feedback_returns_400(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance"}, coord)
        assert resp.status == 400
        coord.learning.record_feedback.assert_not_called()

    def test_invalid_action_returns_400(self):
        coord = _coord()
        resp = _post({"suggestion_key": "low_window_compliance", "action": "snooze"}, coord)
        assert resp.status == 400
        coord.learning.record_feedback.assert_not_called()
        coord.learning.dismiss_suggestion.assert_not_called()
        coord.learning.accept_suggestion.assert_not_called()

    def test_accept_action_applies_changes_to_config(self):
        coord = _coord()
        coord.learning.accept_suggestion = MagicMock(return_value={"comfort_heat": 68})
        resp = _post({"suggestion_key": "low_window_compliance", "action": "accept"}, coord)
        assert resp.status == 200
        assert resp.json_data["changes"] == {"comfort_heat": 68}
        assert coord.config["comfort_heat"] == 68
