"""Coverage of the five zone-scoped services' success and failure paths (quality scale Silver, test-coverage).

Drives the REAL handlers ``services.async_setup_services`` registers, through ``build_headless_multi_zone()``'s
fake ``hass.services.async_call`` (the same harness ``test_service_zone_scoping.py`` uses).

Occupant view: an automation or the Developer Tools call one of these services. A call that fails must say so (a
translated ``HomeAssistantError`` naming the service), never log and report success: otherwise the owner believes a
reset, a re-sent briefing or a reclassification happened when it did not.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.const import DOMAIN
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_multi_zone


def _errors():
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: PLC0415

    return HomeAssistantError, ServiceValidationError


@pytest.fixture
def zone():
    zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)
    return zones["zone_0"]["coordinator"], zones["zone_0"]["entry"].entry_id, fake_hass


def _call(fake_hass, service, data):
    return run_coro(fake_hass.services.async_call(DOMAIN, service, data))


def _assert_failed(exc_info, service: str, cause: type[Exception]):
    err = exc_info.value
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "service_failed"
    assert err.translation_placeholders["service"] == service
    assert isinstance(err.__cause__, cause)


class TestRespondToSuggestion:
    def test_accept_records_the_acknowledgement_and_saves(self, zone):
        """Accept is an acknowledgement (#1652): it is recorded and saved, and no setting changes."""
        coord, entry_id, fake_hass = zone
        coord.learning.save_state = MagicMock()
        before = len(coord.learning._state.settings_history)

        _call(
            fake_hass,
            "respond_to_suggestion",
            {"entry_id": entry_id, "action": "accept", "suggestion_key": "frequent_overrides"},
        )

        history = coord.learning._state.settings_history
        assert len(history) == before + 1
        assert history[-1]["suggestion"] == "frequent_overrides"
        assert history[-1]["changes"] == {}
        coord.learning.save_state.assert_called_once()

    def test_unknown_key_is_a_validation_error(self, zone):
        _, entry_id, fake_hass = zone
        _, service_validation_error = _errors()
        with pytest.raises(service_validation_error) as exc_info:
            _call(
                fake_hass,
                "respond_to_suggestion",
                {"entry_id": entry_id, "action": "dismiss", "suggestion_key": "no_such_key"},
            )
        assert exc_info.value.translation_key == "unknown_suggestion"

    def test_a_failed_save_is_reported(self, zone):
        coord, entry_id, fake_hass = zone
        coord.learning.save_state = MagicMock(side_effect=OSError("disk full"))
        home_assistant_error, _ = _errors()

        with pytest.raises(home_assistant_error) as exc_info:
            _call(
                fake_hass,
                "respond_to_suggestion",
                {"entry_id": entry_id, "action": "dismiss", "suggestion_key": "frequent_overrides"},
            )

        _assert_failed(exc_info, "respond_to_suggestion", OSError)
        assert "OSError: disk full" in exc_info.value.translation_placeholders["error"]

    def test_schema_only_accepts_accept_or_dismiss(self):
        """The action set is closed by the schema, so the handler only ever sees accept or dismiss."""
        import voluptuous as vol

        from custom_components.climate_advisor.services import RESPOND_SUGGESTION_SCHEMA

        with pytest.raises(vol.Invalid):
            RESPOND_SUGGESTION_SCHEMA({"entry_id": "e", "action": "snooze", "suggestion_key": "frequent_overrides"})


class TestForceReclassify:
    def test_a_failed_refresh_is_reported(self, zone):
        coord, entry_id, fake_hass = zone
        coord.async_request_refresh = AsyncMock(side_effect=RuntimeError("weather down"))
        home_assistant_error, _ = _errors()

        with pytest.raises(home_assistant_error) as exc_info:
            _call(fake_hass, "force_reclassify", {"entry_id": entry_id})

        _assert_failed(exc_info, "force_reclassify", RuntimeError)


class TestResendBriefing:
    def test_resend_clears_the_sent_flag_and_sends(self, zone):
        """The owner asked for the briefing again: today's "already sent" flag is cleared, then it is sent."""
        coord, entry_id, fake_hass = zone
        coord._briefing_sent_today = True
        seen = {}

        async def _send(now):
            seen["flag_at_send"] = coord._briefing_sent_today
            seen["now"] = now

        coord._async_send_briefing = _send

        _call(fake_hass, "resend_briefing", {"entry_id": entry_id})

        assert seen["flag_at_send"] is False
        assert seen["now"] is not None

    def test_a_failed_send_is_reported(self, zone):
        coord, entry_id, fake_hass = zone
        coord._async_send_briefing = AsyncMock(side_effect=RuntimeError("notify service missing"))
        home_assistant_error, _ = _errors()

        with pytest.raises(home_assistant_error) as exc_info:
            _call(fake_hass, "resend_briefing", {"entry_id": entry_id})

        _assert_failed(exc_info, "resend_briefing", RuntimeError)


class TestDumpDiagnostics:
    def test_payload_is_logged(self, zone, caplog):
        _, entry_id, fake_hass = zone
        payload = {"zone": "zone_0", "marker": "diag-marker"}
        with (
            patch(
                "custom_components.climate_advisor.diagnostics.async_get_diagnostics_payload",
                AsyncMock(return_value=payload),
            ),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.services"),
        ):
            _call(fake_hass, "dump_diagnostics", {"entry_id": entry_id})

        assert "Diagnostic dump requested" in caplog.text
        assert '"marker": "diag-marker"' in caplog.text

    def test_a_failed_payload_is_reported(self, zone):
        _, entry_id, fake_hass = zone
        home_assistant_error, _ = _errors()
        with (
            patch(
                "custom_components.climate_advisor.diagnostics.async_get_diagnostics_payload",
                AsyncMock(side_effect=KeyError("thermal")),
            ),
            pytest.raises(home_assistant_error) as exc_info,
        ):
            _call(fake_hass, "dump_diagnostics", {"entry_id": entry_id})

        _assert_failed(exc_info, "dump_diagnostics", KeyError)


class TestResetLearningData:
    def test_a_failed_reset_is_reported_not_logged_as_done(self, zone, caplog):
        coord, entry_id, fake_hass = zone
        coord.learning.reset = MagicMock(side_effect=OSError("read-only"))
        home_assistant_error, _ = _errors()

        with (
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.services"),
            pytest.raises(home_assistant_error) as exc_info,
        ):
            _call(fake_hass, "reset_learning_data", {"entry_id": entry_id, "scope": "weather_bias"})

        _assert_failed(exc_info, "reset_learning_data", OSError)
        coord.learning.reset.assert_called_once_with("weather_bias")
        assert "Learning data reset via service" not in caplog.text

    def test_default_scope_is_all_and_logged(self, zone, caplog):
        coord, entry_id, fake_hass = zone
        coord.learning.reset = MagicMock()
        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.services"):
            _call(fake_hass, "reset_learning_data", {"entry_id": entry_id})
        coord.learning.reset.assert_called_once_with("all")
        assert f"entry_id={entry_id} scope=all" in caplog.text
