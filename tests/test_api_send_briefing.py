"""Tests for ClimateAdvisorSendBriefingView (Issue #817 Part 4).

Exercises the real view's post() handler directly — HomeAssistantView is a real
minimal base class in the test stub environment (Issue #452), so no hand-rolled
replica is needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.api import ClimateAdvisorSendBriefingView
from custom_components.climate_advisor.const import DOMAIN


def _make_request(body: dict | None, coordinator) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}

    req = MagicMock()
    req.app = {"hass": hass}
    if body is None:
        # The debug tab's "Send Briefing" button sends no body at all —
        # emulate aiohttp raising when .json() is called on an empty body.
        req.json = AsyncMock(side_effect=ValueError("no body"))
    else:
        req.json = AsyncMock(return_value=body)
    req.query = {}
    return req


def _post(body: dict | None, coordinator):
    view = ClimateAdvisorSendBriefingView()
    request = _make_request(body, coordinator)
    return asyncio.run(view.post(request))


def _coord() -> MagicMock:
    coord = MagicMock()
    coord._briefing_sent_today = True
    coord._async_send_briefing = AsyncMock()
    return coord


class TestSendBriefingNotifyParam:
    """Issue #817 Part 4: {"notify": bool} body controls send_notifications."""

    def test_no_body_defaults_to_notify_true(self):
        """Debug tab's Send Briefing button (no body) always sends — backward compatible."""
        coord = _coord()
        result = _post(None, coord)

        assert result.status == 200
        coord._async_send_briefing.assert_awaited_once()
        _, kwargs = coord._async_send_briefing.call_args
        assert kwargs.get("send_notifications", True) is True

    def test_notify_true_sends(self):
        coord = _coord()
        result = _post({"notify": True}, coord)

        assert result.status == 200
        assert result.json_data["message"] == "Briefing sent"
        _, kwargs = coord._async_send_briefing.call_args
        assert kwargs["send_notifications"] is True

    def test_notify_false_regenerates_without_notifying(self):
        """Dashboard's Regenerate button — text refreshes, no push/email."""
        coord = _coord()
        result = _post({"notify": False}, coord)

        assert result.status == 200
        assert "no notification" in result.json_data["message"]
        _, kwargs = coord._async_send_briefing.call_args
        assert kwargs["send_notifications"] is False

    def test_no_coordinator_returns_503(self):
        view = ClimateAdvisorSendBriefingView()
        hass = MagicMock()
        hass.data = {}
        req = MagicMock()
        req.app = {"hass": hass}
        req.query = {}

        result = asyncio.run(view.post(req))
        assert result.status == 503

    def test_resets_briefing_sent_today_before_calling(self):
        """POST always clears the once-daily guard so a real send can fire again."""
        coord = _coord()
        _post({"notify": True}, coord)
        assert coord._briefing_sent_today is False
