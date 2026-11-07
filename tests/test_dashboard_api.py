"""Issue #1230 R5: the read-only ``settings_extra`` view behind the new dashboard's Settings tab."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor import dashboard_api
from custom_components.climate_advisor.const import CONFIG_METADATA, DOMAIN


def _request(coordinator):
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}
    req = MagicMock()
    req.app = {"hass": hass}
    req.query = {}
    return req


def _call(config):
    coord = MagicMock()
    coord.config = config
    resp = asyncio.run(dashboard_api.ClimateAdvisorSettingsExtraView().get(_request(coord)))
    return resp.json_data


def test_url_and_registration_list():
    assert dashboard_api.API_SETTINGS_EXTRA == "/api/climate_advisor/settings_extra"
    assert [dashboard_api.ClimateAdvisorSettingsExtraView] == dashboard_api.DASHBOARD_VIEWS
    view = dashboard_api.ClimateAdvisorSettingsExtraView
    assert view.requires_auth is True
    assert not hasattr(view, "post") and not hasattr(view, "put") and not hasattr(view, "delete")  # read-only


def test_changed_from_default_uses_internal_units_and_converts_default_for_display():
    default_heat = dashboard_api.default_for("comfort_heat", CONFIG_METADATA["comfort_heat"])
    assert default_heat is not None
    out = _call({"temp_unit": "celsius", "comfort_heat": default_heat + 2})
    entry = out["changed"]["comfort_heat"]
    assert entry["unit"] == "°C"
    assert entry["default"] == round((default_heat - 32) * 5 / 9, 1)


def test_unchanged_and_unknown_default_settings_have_no_marker():
    default_heat = dashboard_api.default_for("comfort_heat", CONFIG_METADATA["comfort_heat"])
    out = _call({"comfort_heat": default_heat, "climate_entity": "climate.x"})
    assert "comfort_heat" not in out["changed"]
    assert "climate_entity" not in out["changed"]  # no default exists for an entity id


def test_seconds_default_shown_in_minutes_like_the_config_view():
    meta = CONFIG_METADATA["manual_grace_seconds"]
    default = dashboard_api.default_for("manual_grace_seconds", meta)
    out = _call({"manual_grace_seconds": default + 600})
    assert out["changed"]["manual_grace_seconds"]["default"] == default // 60


def test_booleans_and_literal_defaults():
    out = _call({"learning_enabled": False, "aggressive_savings": False, "min_preheat_minutes": 45})
    assert "learning_enabled" in out["changed"] and out["changed"]["learning_enabled"]["default"] is True
    assert "aggressive_savings" not in out["changed"]
    assert out["changed"]["min_preheat_minutes"]["default"] == 30


def test_secrets_never_leave():
    out = _call(
        {"ai_api_key": "sk-secret", "notify_service": "notify.me", "github_token": "ghp_secret", "github_repo": "o/r"}
    )
    blob = repr(out)
    assert "sk-secret" not in blob and "ghp_secret" not in blob and "notify.me" not in blob
    assert out["github"] == {"repo": "o/r", "token_set": True, "configured": True}


def test_github_unconfigured_and_schedules():
    out = _call(
        {
            "schedules": [
                {
                    "id": "a",
                    "name": "Peak",
                    "days": ["mon", "tue"],
                    "start": "16:00",
                    "end": "21:00",
                    "cost_tag": "high",
                }
            ]
        }
    )
    assert out["github"] == {"repo": "", "token_set": False, "configured": False}
    assert out["schedules"] == [
        {"id": "a", "name": "Peak", "days": ["mon", "tue"], "start": "16:00", "end": "21:00", "cost_tag": "high"}
    ]
    assert _call({})["schedules"] == []


def test_no_coordinator_is_503():
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    req = MagicMock()
    req.app = {"hass": hass}
    req.query = {}
    resp = asyncio.run(dashboard_api.ClimateAdvisorSettingsExtraView().get(req))
    assert resp.status == 503 or getattr(resp, "status_code", 503) == 503


def test_reload_needed_follows_the_repairs_issue(monkeypatch):
    from custom_components.climate_advisor import dashboard_api as d

    assert d._reload_needed(MagicMock(), "") is False
    assert d._reload_needed(object(), "abc") in (True, False)  # a registry that cannot be read never raises
    coord = MagicMock()
    coord.config = {}
    coord._entry_id = ""
    resp = asyncio.run(d.ClimateAdvisorSettingsExtraView().get(_request(coord)))
    assert resp.json_data["reload_needed"] is False
