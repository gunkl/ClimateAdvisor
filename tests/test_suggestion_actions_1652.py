"""Issue #1652: accepting a learning suggestion changes a REAL setting, from the zone's current value.

Occupant story: the dashboard says "door pauses are frequent" and the person presses Accept. Before #1652 the card
went away and nothing in the house changed: every suggestion mapped to a key no code reads (``door_pause_seconds``,
``setback_modifier``, ...), and the old "extend to 5 minutes" would even have SHORTENED the real 10-minute debounce.
Now an actionable suggestion steps a real setting (inside bounds, never the wrong way) and an advice-only suggestion
changes nothing and is offered as "Got it", not Accept.

Each mapping test fails if the mapping is removed (``accept_suggestion`` returns ``{}``) or points at a fake key.
The real-HA tier (``tests_ha/test_suggestion_accept_1652.py``) covers persistence across a restart and the behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.const import CONFIG_METADATA, DOMAIN
from custom_components.climate_advisor.learning import SUGGESTION_KEYS, LearningEngine
from custom_components.climate_advisor.setpoint_combinations import (
    MIN_HEAT_COOL_GAP_F,
    setpoint_combination_findings,
)
from custom_components.climate_advisor.suggestion_actions import (
    ACTIONABLE_SUGGESTION_KEYS,
    DOOR_PAUSE_DEBOUNCE_CAP_SECONDS,
    suggestion_change_msgs,
    suggestion_setting_changes,
)

ADVICE_ONLY = sorted(SUGGESTION_KEYS - ACTIONABLE_SUGGESTION_KEYS)

# A typical zone: home 68-75, sleep 64-78, away 62-80 (°F, as stored).
ZONE = {
    "comfort_heat": 68.0,
    "comfort_cool": 75.0,
    "sleep_heat": 64.0,
    "sleep_cool": 78.0,
    "setback_heat": 62.0,
    "setback_cool": 80.0,
    "sensor_debounce_seconds": 600,
}


def test_the_actionable_set_is_exactly_the_two_with_a_real_setting():
    assert {"frequent_door_pauses", "comfort_violations"} == ACTIONABLE_SUGGESTION_KEYS
    assert ACTIONABLE_SUGGESTION_KEYS <= SUGGESTION_KEYS


@pytest.mark.parametrize("key", sorted(ACTIONABLE_SUGGESTION_KEYS))
def test_every_change_is_a_real_setting(key):
    """The pre-#1652 keys (door_pause_seconds, setback_modifier, ...) were in no CONFIG_METADATA: never saved."""
    changes = suggestion_setting_changes(key, ZONE)
    assert changes, f"{key}: accepting it changes nothing"
    assert set(changes) <= set(CONFIG_METADATA), changes


class TestDoorPauses:
    @pytest.mark.parametrize(
        ("current", "expected"),
        [(None, 900), (0, 300), (600, 900), (900, 1200), (1100, 1200)],
    )
    def test_lengthens_by_five_minutes_up_to_twenty(self, current, expected):
        config = {} if current is None else {"sensor_debounce_seconds": current}
        assert suggestion_setting_changes("frequent_door_pauses", config) == {"sensor_debounce_seconds": expected}

    @pytest.mark.parametrize("current", [DOOR_PAUSE_DEBOUNCE_CAP_SECONDS, 1800, 3600])
    def test_never_shortens_a_delay_at_or_above_the_cap(self, current):
        """The old mapping set 300 s, which would have halved the 600 s default."""
        assert suggestion_setting_changes("frequent_door_pauses", {"sensor_debounce_seconds": current}) == {}


class TestComfortViolations:
    def test_away_band_moves_two_degrees_toward_home(self):
        assert suggestion_setting_changes("comfort_violations", ZONE) == {"setback_heat": 64.0, "setback_cool": 78.0}

    def test_never_past_home_or_sleep(self):
        # away heat 63 may rise only to sleep heat 64; away cool 79 may fall only to sleep cool 78
        config = {**ZONE, "setback_heat": 63.0, "setback_cool": 79.0}
        assert suggestion_setting_changes("comfort_violations", config) == {"setback_heat": 64.0, "setback_cool": 78.0}
        # with sleep cool at home cool, the home edge (75) is the limit
        config = {**ZONE, "sleep_cool": 75.0, "setback_cool": 76.0}
        assert suggestion_setting_changes("comfort_violations", config)["setback_cool"] == 75.0

    def test_defaults_when_nothing_is_stored(self):
        # const defaults: home 68-74, sleep 64-72, away 63-79 -> away heat to 64 (sleep), away cool to 77
        assert suggestion_setting_changes("comfort_violations", {}) == {"setback_heat": 64.0, "setback_cool": 77.0}

    def test_never_the_wrong_direction(self):
        """An away band already milder than home on one side is left alone there (never made harsher)."""
        config = {**ZONE, "setback_heat": 70.0, "setback_cool": 74.0, "comfort_cool": 72.0, "sleep_cool": 72.0}
        changes = suggestion_setting_changes("comfort_violations", config)
        assert "setback_heat" not in changes and "setback_cool" not in changes

    def test_at_home_temperatures_there_is_nothing_to_change(self):
        config = {**ZONE, "setback_heat": 64.0, "setback_cool": 78.0}
        assert suggestion_setting_changes("comfort_violations", config) == {}

    def test_celsius_zone_steps_one_degree_celsius(self):
        # 18.5 °C = 65.3 °F away heat, home 21 °C, sleep 20 °C (68 °F): +1 °C = 19.5 °C = 67.1 °F
        config = {
            "temp_unit": "celsius",
            "comfort_heat": 69.8,
            "comfort_cool": 77.0,
            "sleep_heat": 68.0,
            "sleep_cool": 78.8,
            "setback_heat": 65.3,
            "setback_cool": 82.4,
        }
        assert suggestion_setting_changes("comfort_violations", config) == {
            "setback_heat": 67.1,
            "setback_cool": 80.6,
        }

    def test_keeps_the_away_band_at_least_the_minimum_gap(self):
        # home 70-71 (narrow, the occupant's choice), away 66-71.5: only 1.5 °F of room before the 4 °F gap
        config = {
            "comfort_heat": 70.0,
            "comfort_cool": 71.0,
            "sleep_heat": 70.0,
            "sleep_cool": 71.0,
            "setback_heat": 66.0,
            "setback_cool": 71.5,
        }
        changes = suggestion_setting_changes("comfort_violations", config)
        assert changes
        heat = changes.get("setback_heat", 66.0)
        cool = changes.get("setback_cool", 71.5)
        assert heat >= 66.0 and cool <= 71.5
        assert cool - heat >= MIN_HEAT_COOL_GAP_F - 1e-6

    def test_a_band_already_narrow_or_inverted_is_left_alone(self):
        for heat, cool in ((70.0, 73.0), (74.0, 72.0)):
            assert suggestion_setting_changes("comfort_violations", {"setback_heat": heat, "setback_cool": cool}) == {}

    @pytest.mark.parametrize(
        "config",
        [ZONE, {}, {**ZONE, "setback_heat": 63.0, "setback_cool": 76.0}, {**ZONE, "sleep_heat": 61.0}],
    )
    def test_adds_no_setpoint_warning(self, config):
        """The setpoints page's own checks (#1347): Accept never creates a combination it would warn about."""
        from custom_components.climate_advisor.setpoint_combinations import SETPOINT_FIELDS

        def values(c):
            from custom_components.climate_advisor import const

            defaults = {
                "comfort_heat": const.DEFAULT_COMFORT_HEAT,
                "comfort_cool": const.DEFAULT_COMFORT_COOL,
                "setback_heat": const.DEFAULT_SETBACK_HEAT,
                "setback_cool": const.DEFAULT_SETBACK_COOL,
                "sleep_heat": const.DEFAULT_SLEEP_HEAT,
                "sleep_cool": const.DEFAULT_SLEEP_COOL,
            }
            return {k: c.get(k, defaults.get(k)) for k in SETPOINT_FIELDS}

        before = setpoint_combination_findings(values(config))
        after = setpoint_combination_findings(
            values({**config, **suggestion_setting_changes("comfort_violations", config)})
        )
        assert set(after.items()) <= set(before.items()), (before, after)


@pytest.mark.parametrize("key", ADVICE_ONLY)
def test_advice_only_keys_change_nothing(key):
    assert suggestion_setting_changes(key, ZONE) == {}


def test_unknown_key_changes_nothing():
    assert suggestion_setting_changes("door_pause_seconds", ZONE) == {}


class TestAcceptSuggestion:
    def test_uses_the_zone_config_and_records_what_it_was(self, tmp_path):
        engine = LearningEngine(tmp_path)
        changes = engine.accept_suggestion("frequent_door_pauses", {"sensor_debounce_seconds": 900})
        assert changes == {"sensor_debounce_seconds": 1200}
        entry = engine._state.settings_history[-1]
        assert entry["suggestion"] == "frequent_door_pauses"
        assert entry["changes"] == {"sensor_debounce_seconds": 1200}
        assert entry["previous"] == {"sensor_debounce_seconds": 900}

    @pytest.mark.parametrize("key", ADVICE_ONLY)
    def test_advice_only_records_the_acknowledgement_and_changes_nothing(self, tmp_path, key):
        engine = LearningEngine(tmp_path)
        assert engine.accept_suggestion(key, ZONE) == {}
        entry = engine._state.settings_history[-1]
        assert entry["suggestion"] == key and entry["changes"] == {} and "previous" not in entry


class TestChangeMessages:
    def test_debounce_line(self):
        msgs = suggestion_change_msgs({"sensor_debounce_seconds": 900}, {"sensor_debounce_seconds": 600})
        assert [i18n.render_en(m) for m in msgs] == ["Sensor Debounce Delay: 10 min → 15 min"]

    def test_away_lines_fahrenheit(self):
        msgs = suggestion_change_msgs({"setback_heat": 64.0, "setback_cool": 78.0}, ZONE)
        assert [i18n.render_en(m) for m in msgs] == [
            "Away Heating Temperature: 62°F → 64°F",
            "Away Cooling Temperature: 80°F → 78°F",
        ]

    def test_away_line_celsius(self):
        msgs = suggestion_change_msgs({"setback_heat": 67.1}, {"temp_unit": "celsius", "setback_heat": 65.3})
        assert [i18n.render_en(m) for m in msgs] == ["Away Heating Temperature: 18.5°C → 19.5°C"]

    def test_no_change_no_line(self):
        assert suggestion_change_msgs({}, ZONE) == []

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_renders_in_every_language(self, lang):
        debounce, heat = suggestion_change_msgs({"sensor_debounce_seconds": 900, "setback_heat": 64.0}, ZONE)
        assert all(n in i18n.render(debounce, lang) for n in ("10", "15")), lang
        assert all(n in i18n.render(heat, lang) for n in ("62", "64")), lang


# --------------------------------------------------------------------------- the two accept paths


def _rest_request(body: dict, coordinator, entry):
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}
    hass.async_add_executor_job = AsyncMock()
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    hass.config.language = "en"
    req = MagicMock()
    req.app = {"hass": hass}
    req.json = AsyncMock(return_value=body)
    req.query = {}
    return req, hass


def _coordinator(tmp_path, config):
    coord = MagicMock()
    coord.config = dict(config)
    coord.learning = LearningEngine(tmp_path)
    coord.learning.save_state = MagicMock()
    return coord


def test_rest_accept_changes_the_live_config_and_the_entry_and_says_what_changed(tmp_path):
    from custom_components.climate_advisor.api import ClimateAdvisorRespondSuggestionView

    coord = _coordinator(tmp_path, ZONE)
    entry = MagicMock()
    entry.data = dict(ZONE)
    req, hass = _rest_request({"suggestion_key": "comfort_violations", "action": "accept"}, coord, entry)
    resp = asyncio.run(ClimateAdvisorRespondSuggestionView().post(req))
    assert resp.status == 200
    body = resp.json_data
    assert body["changes"] == {"setback_heat": 64.0, "setback_cool": 78.0}
    assert body["changes_text"] == ["Away Heating Temperature: 62°F → 64°F", "Away Cooling Temperature: 80°F → 78°F"]
    assert [m["key"] for m in body["changes_msg"]] == ["learning.change.setback_heat", "learning.change.setback_cool"]
    assert coord.config["setback_heat"] == 64.0 and coord.config["setback_cool"] == 78.0
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry, data={**ZONE, "setback_heat": 64.0, "setback_cool": 78.0}
    )


@pytest.mark.parametrize("key", ADVICE_ONLY)
def test_rest_accept_of_advice_changes_nothing(tmp_path, key):
    from custom_components.climate_advisor.api import ClimateAdvisorRespondSuggestionView

    coord = _coordinator(tmp_path, ZONE)
    entry = MagicMock()
    entry.data = dict(ZONE)
    req, hass = _rest_request({"suggestion_key": key, "action": "accept"}, coord, entry)
    resp = asyncio.run(ClimateAdvisorRespondSuggestionView().post(req))
    assert resp.status == 200
    assert resp.json_data["changes"] == {} and resp.json_data["changes_text"] == []
    assert coord.config == ZONE
    hass.config_entries.async_update_entry.assert_not_called()


def test_learning_view_marks_actionable_and_lists_the_planned_change(tmp_path):
    """The dashboard offers Accept (with the change lines) only when Accept changes something; else "Got it"."""
    from custom_components.climate_advisor.api import ClimateAdvisorLearningView

    coord = MagicMock()
    coord.config = {**ZONE, "sensor_debounce_seconds": 600}
    coord.today_record = None
    coord.yesterday_record = None
    coord.tomorrow_plan = None
    coord.learning.generate_suggestions = MagicMock(return_value=["a", "b", "c"])
    coord.learning.get_last_suggestion_keys = MagicMock(
        return_value=["frequent_door_pauses", "short_departures", "comfort_violations"]
    )
    coord.learning.get_last_suggestion_msgs = MagicMock(
        return_value=[i18n.Msg("learning.suggestion.short_departures")] * 3
    )
    coord.learning.get_compliance_summary = MagicMock(return_value={})
    coord.analysis_today_live = MagicMock(return_value={})
    req, _hass = _rest_request({}, coord, MagicMock())
    coord.config["setback_heat"] = 64.0  # at home/sleep already: heat side has nothing left
    resp = asyncio.run(ClimateAdvisorLearningView().get(req))
    items = {s["key"]: s for s in resp.json_data["suggestions"]}
    assert items["frequent_door_pauses"]["actionable"] is True
    assert items["frequent_door_pauses"]["changes_text"] == ["Sensor Debounce Delay: 10 min → 15 min"]
    assert items["short_departures"]["actionable"] is False
    assert items["short_departures"]["changes"] == {} and items["short_departures"]["changes_msg"] == []
    assert items["comfort_violations"]["changes"] == {"setback_cool": 78.0}
