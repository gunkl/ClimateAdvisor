"""Issue #1652 (owner decision): learning suggestions are advice; none of them changes a setting.

Occupant story: before, the dashboard offered "Accept" on a suggestion such as "door pauses are frequent" and the
card went away while nothing changed. The owner decided that learning must never lengthen the door/window pause or
change the away temperatures, so those two suggestions (``frequent_door_pauses`` and ``comfort_violations``) are
gone, and every remaining suggestion is advice: "Got it" (``accept``) records that it was seen and holds it back
for 30 days, and changes nothing. A house with frequent door pauses or comfort misses gets no suggestion about it.
Stored learning data that still names the removed keys loads and is ignored.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.learning import SUGGESTION_KEYS, LearningEngine
from tools.sim_harness.ha_stubs import set_loaded_zones

REMOVED = ("frequent_door_pauses", "comfort_violations")


def _rec(day: int, **fields) -> dict:
    return {"date": f"2026-03-{day:02d}", "day_type": "mild", "occupancy_mode": "home", **fields}


def test_the_removed_suggestions_are_not_keys():
    # Pinned deliberately: a new key must be added here on purpose. Issue #1695 added "humid_overcool" ("Turn on
    # Humid Overcool"), advice only like every key here: "Got it" changes no setting (tests/test_humid_overcool_
    # suggestion_1695.py checks the option is never touched).
    assert {
        "low_window_compliance",
        "frequent_overrides",
        "short_departures",
        "thermal_model_ready",
        "forecast_bias_significant",
        "high_runtime_mild_days",
        "humid_overcool",
    } == SUGGESTION_KEYS
    assert not set(REMOVED) & SUGGESTION_KEYS


def test_frequent_door_pauses_and_comfort_misses_produce_no_suggestion(tmp_path):
    engine = LearningEngine(tmp_path)
    engine._state.records.extend(
        _rec(
            i + 1,
            door_window_pause_events=5,
            door_pause_by_sensor={"front_door": 5},
            comfort_violations_minutes=120.0,
        )
        for i in range(14)
    )
    engine.generate_suggestions()
    keys = engine.get_last_suggestion_keys()
    assert not set(REMOVED) & set(keys), keys


def test_the_removed_strings_are_gone_from_every_catalog():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend" / "i18n"
    gone = {
        "learning.suggestion.comfort_violations",
        "learning.suggestion.frequent_door_pauses",
        "learning.suggestion.frequent_door_pauses_sensor",
        "learning.change.sensor_debounce_seconds",
        "learning.change.setback_heat",
        "learning.change.setback_cool",
        "dashboard.analysis.sug.will_change",
    }
    for path in [*root.glob("learning/*.json"), *root.glob("dashboard/*.json")]:
        if path.name.startswith("_"):
            continue
        assert not gone & set(json.loads(path.read_text(encoding="utf-8"))), path


def test_the_setting_change_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("custom_components.climate_advisor.suggestion_actions")


@pytest.mark.parametrize("key", sorted(SUGGESTION_KEYS))
def test_accept_records_an_acknowledgement_and_changes_nothing(tmp_path, key):
    engine = LearningEngine(tmp_path)
    assert engine.accept_suggestion(key) == {}
    entry = engine._state.settings_history[-1]
    assert entry["suggestion"] == key and entry["changes"] == {}
    assert set(entry) == {"timestamp", "suggestion", "changes"}  # no "previous": nothing was changed
    # held back like any accepted suggestion
    engine._state.records.extend(_rec(i + 1, occupancy_away_minutes=35.0) for i in range(14))
    engine.generate_suggestions()
    assert key not in engine.get_last_suggestion_keys()


def test_old_stored_records_naming_removed_keys_load_and_are_ignored(tmp_path):
    engine = LearningEngine(tmp_path)
    engine.save_state()  # write a valid file, then plant the pre-#1652 entries in it
    path = engine._db_path
    data = json.loads(path.read_text())
    data["dismissed_suggestions"] = ["frequent_door_pauses", "comfort_violations"]
    data["settings_history"] = [
        {
            "timestamp": "2026-11-01T08:00:00",
            "suggestion": "frequent_door_pauses",
            "changes": {"door_pause_seconds": 300},
        },
        {
            "timestamp": "2026-11-02T08:00:00",
            "suggestion": "comfort_violations",
            "changes": {"sensor_debounce_seconds": 900},
            "previous": {"sensor_debounce_seconds": 600},
        },
        {
            "timestamp": "2026-11-03T08:00:00",
            "type": "feedback",
            "suggestion": "comfort_violations",
            "verdict": "correct",
        },
    ]
    data["active_suggestions"] = [{"key": "frequent_door_pauses"}]
    path.write_text(json.dumps(data))

    loaded = LearningEngine(tmp_path)
    loaded.load_state()
    assert loaded._state.dismissed_suggestions == ["frequent_door_pauses", "comfort_violations"]
    assert len(loaded._state.settings_history) == 3
    loaded._state.records.extend(
        _rec(i + 1, door_window_pause_events=5, comfort_violations_minutes=120.0, occupancy_away_minutes=35.0)
        for i in range(14)
    )
    loaded.generate_suggestions()
    keys = loaded.get_last_suggestion_keys()
    assert "short_departures" in keys
    assert not set(REMOVED) & set(keys)


# --------------------------------------------------------------------------- the REST views


def _request(body: dict, coordinator):
    hass = MagicMock()
    set_loaded_zones(hass, {"entry1": coordinator})
    hass.async_add_executor_job = AsyncMock()
    hass.config.language = "en"
    req = MagicMock()
    req.app = {"hass": hass}
    req.json = AsyncMock(return_value=body)
    req.query = {}
    return req, hass


def test_learning_view_items_carry_no_setting_change_fields(tmp_path):
    from custom_components.climate_advisor.api import ClimateAdvisorLearningView

    coord = MagicMock()
    coord.config = {"sensor_debounce_seconds": 600, "setback_heat": 62.0}
    coord.today_record = None
    coord.yesterday_record = None
    coord.tomorrow_plan = None
    coord.learning.generate_suggestions = MagicMock(return_value=["a"])
    coord.learning.get_last_suggestion_keys = MagicMock(return_value=["short_departures"])
    coord.learning.get_last_suggestion_msgs = MagicMock(return_value=[i18n.Msg("learning.suggestion.short_departures")])
    coord.learning.get_compliance_summary = MagicMock(return_value={})
    coord.analysis_today_live = MagicMock(return_value={})
    req, _hass = _request({}, coord)
    resp = asyncio.run(ClimateAdvisorLearningView().get(req))
    (item,) = resp.json_data["suggestions"]
    assert set(item) == {"key", "text", "text_msg"}


def test_rest_accept_changes_no_setting(tmp_path):
    from custom_components.climate_advisor.api import ClimateAdvisorRespondSuggestionView

    coord = MagicMock()
    coord.config = {"sensor_debounce_seconds": 600, "setback_heat": 62.0, "setback_cool": 80.0}
    coord.learning = LearningEngine(tmp_path)
    coord.learning.save_state = MagicMock()
    req, hass = _request({"suggestion_key": "frequent_overrides", "action": "accept"}, coord)
    resp = asyncio.run(ClimateAdvisorRespondSuggestionView().post(req))
    assert resp.status == 200
    assert resp.json_data == {"status": "ok", "acknowledged": "frequent_overrides", "changes": {}}
    assert coord.config == {"sensor_debounce_seconds": 600, "setback_heat": 62.0, "setback_cool": 80.0}
    hass.config_entries.async_update_entry.assert_not_called()
    hass.async_add_executor_job.assert_awaited_once()  # the learning state is saved
