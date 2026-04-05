"""Tests for respond_suggestion endpoint feedback handling (Issue #84).

The view class cannot be directly instantiated in the lightweight test
environment (same metaclass constraint as sensor entities). Tests verify:
- The endpoint path constant is correct
- The underlying record_feedback / dismiss_suggestion integration logic
- Validation conditions that the post() handler checks
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.const import API_RESPOND_SUGGESTION

# ---------------------------------------------------------------------------
# Helpers — replicate the logic the post() handler executes
# ---------------------------------------------------------------------------


def _simulate_post(body: dict, coordinator: MagicMock) -> str:
    """Simulate the validation + dispatch logic of ClimateAdvisorRespondSuggestionView.post().

    Returns the logical outcome: 'ok', '400_feedback', '400_key', '400_none',
    '400_action', or 'feedback_only_ok'.
    """
    action = body.get("action")
    suggestion_key = body.get("suggestion_key")
    feedback = body.get("feedback")

    if feedback is not None and feedback not in ("correct", "incorrect"):
        return "400_feedback"
    if not suggestion_key:
        return "400_key"
    if action is None and feedback is None:
        return "400_none"
    if action is not None and action not in ("accept", "dismiss"):
        return "400_action"

    if feedback is not None:
        coordinator.learning.record_feedback(suggestion_key, feedback)

    if action == "accept":
        coordinator.learning.accept_suggestion(suggestion_key)
        return "ok"
    elif action == "dismiss":
        coordinator.learning.dismiss_suggestion(suggestion_key)
        return "ok"
    else:
        return "ok"


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
# TestRespondSuggestionFeedback — logic-level tests
# ---------------------------------------------------------------------------


class TestRespondSuggestionFeedback:
    """Logic-level tests for the feedback dispatch in post()."""

    def _coord(self) -> MagicMock:
        coord = MagicMock()
        coord.learning.record_feedback = MagicMock()
        coord.learning.dismiss_suggestion = MagicMock()
        coord.learning.accept_suggestion = MagicMock(return_value={})
        return coord

    def test_feedback_correct_recorded(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance", "feedback": "correct"}, coord)
        assert result == "ok"
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "correct")

    def test_feedback_incorrect_recorded(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance", "feedback": "incorrect"}, coord)
        assert result == "ok"
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "incorrect")

    def test_feedback_invalid_value_rejected(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance", "feedback": "maybe"}, coord)
        assert result == "400_feedback"
        coord.learning.record_feedback.assert_not_called()

    def test_feedback_requires_suggestion_key(self):
        coord = self._coord()
        result = _simulate_post({"feedback": "correct"}, coord)
        assert result == "400_key"
        coord.learning.record_feedback.assert_not_called()

    def test_action_and_feedback_together(self):
        coord = self._coord()
        result = _simulate_post(
            {
                "suggestion_key": "low_window_compliance",
                "action": "dismiss",
                "feedback": "incorrect",
            },
            coord,
        )
        assert result == "ok"
        coord.learning.record_feedback.assert_called_once_with("low_window_compliance", "incorrect")
        coord.learning.dismiss_suggestion.assert_called_once_with("low_window_compliance")

    def test_feedback_only_no_action_returns_ok(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance", "feedback": "correct"}, coord)
        assert result == "ok"
        coord.learning.dismiss_suggestion.assert_not_called()
        coord.learning.accept_suggestion.assert_not_called()

    def test_no_action_no_feedback_returns_400(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance"}, coord)
        assert result == "400_none"
        coord.learning.record_feedback.assert_not_called()

    def test_invalid_action_returns_400(self):
        coord = self._coord()
        result = _simulate_post({"suggestion_key": "low_window_compliance", "action": "snooze"}, coord)
        assert result == "400_action"
        coord.learning.record_feedback.assert_not_called()
        coord.learning.dismiss_suggestion.assert_not_called()
        coord.learning.accept_suggestion.assert_not_called()
