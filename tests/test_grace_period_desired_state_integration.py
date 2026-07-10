"""Regression test proving _start_grace_period() genuinely calls decide_grace_start()
to decide (architecture-reset Step 2 / Step 3 wiring), not some other code path
that happens to agree.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.const import CONF_AUTOMATION_GRACE_PERIOD
from tests.test_door_window import _make_automation_engine  # noqa: E402


def test_forcing_grace_disabled_prevents_a_real_grace_start():
    """decide_grace_start() forced to always return None (disabled) must prevent
    _start_grace_period() from actually starting grace, even with a real nonzero
    duration configured — proving the wiring is load-bearing."""
    engine = _make_automation_engine({CONF_AUTOMATION_GRACE_PERIOD: 300})

    with (
        patch("custom_components.climate_advisor.automation.async_call_later") as mock_call_later,
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda f: f),
        patch("custom_components.climate_advisor.automation.decide_grace_start", return_value=None),
    ):
        mock_call_later.return_value = MagicMock()
        engine._start_grace_period(source="automation", trigger="sensor_closed_resume")

    assert engine._grace_active is False, (
        "forcing decide_grace_start() to return None (disabled) should prevent grace from "
        "starting even with a real nonzero duration configured — the wiring is load-bearing"
    )
    mock_call_later.assert_not_called()
