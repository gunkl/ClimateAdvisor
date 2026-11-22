"""Issue #1676: the thermostat reads 6-7 F high after the furnace trips its high limit; CA must not act on it.

Occupant view: a few times a week the hallway Ecobee jumps from about 68 F to 75-76 F the moment the heating stops
(the furnace overheated and its high-limit safety cut it), and drifts back over 9-42 minutes. The house is really
68-69 F. Climate Advisor now spots that jump, warns once in the log, raises a Repairs notice the occupant can ignore,
reads the room from another sensor (the bedroom sensor on the owner's install) until the thermostat settles, switches
back afterwards, and keeps every false reading out of thermal learning and the v4 fit. Without another sensor it warns
and keeps learning clean.

The episode shapes below are the owner's own readings (HA history Nov 13-21): Fri 11-20 11:54 (a 3 s 32 F blip, then
+7.2 F) and Thu 11-19 09:55 (a second +3.4 F jump inside the episode).
"""

from __future__ import annotations

import datetime as _dt
import logging
import sys
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor.ai_skills_context import _NO_DEDUP, EVENT_RENDERERS  # noqa: E402
from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.chart_log import ChartStateLog  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    RECAL_HEATING_RECENT_S,
    RECAL_JUMP_WINDOW_S,
    RECAL_MAX_S,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import (  # noqa: E402
    CompanionSensors,
    resolve_indoor_temp_with_provenance,
)
from custom_components.climate_advisor.predictor_v4 import samples_from_chart_log  # noqa: E402
from custom_components.climate_advisor.thermostat_recal import (  # noqa: E402
    SETTLED,
    STARTED,
    TIMED_OUT,
    ThermostatRecal,
)

T0 = _dt.datetime(2026, 11, 20, 19, 40, tzinfo=_dt.UTC)
CLIMATE = "climate.santa_maria_hallway"
COMP = "sensor.santa_maria_hallway_current_temperature"
SLEEP = "sensor.primary_bedroom_temperature_temperature"
DEDICATED = "sensor.dedicated_indoor"
COORD = "custom_components.climate_advisor.coordinator"


def _at(minutes: float) -> _dt.datetime:
    return T0 + _dt.timedelta(minutes=minutes)


# --------------------------------------------------------------------------------------------- the state machine


def _heated(recal: ThermostatRecal, minute: float = 0.0, reading: float = 68.9) -> None:
    assert recal.observe(_at(minute), reading, "heating") is None


def test_jump_right_after_heating_starts_an_episode():
    r = ThermostatRecal()
    _heated(r, 0, 69.08)
    step = r.observe(_at(1.8), 76.28, "idle")
    assert step is not None and step.kind == STARTED
    assert (step.baseline_f, step.reading_f, step.peak_f) == (69.08, 76.28, 76.28)
    assert r.active and r.count == 1 and r.since == _at(1.8)


def test_owner_episode_with_32f_blip_settles_against_the_last_real_reading():
    """Fri 11-20 11:54: 69.08 -> 32 (3 s, implausible = None) -> 76.28, decaying back to 69.4 within ~12 min."""
    r = ThermostatRecal()
    _heated(r, 0, 69.08)
    assert r.observe(_at(1.75), None, "idle") is None  # the resolver rejects 32 F as implausible
    step = r.observe(_at(1.8), 76.28, "idle")
    assert step.kind == STARTED and step.baseline_f == 69.08
    trace = [(2.3, 76.1), (3.0, 75.92), (4.1, 75.56), (5.1, 70.88), (6.1, 70.16), (7.0, 69.98), (9.0, 69.8)]
    for minute, reading in trace:
        assert r.observe(_at(minute), reading, "idle") is None
    assert r.active
    end = r.observe(_at(12.0), 69.44, "idle")  # 5 min within 1 F of 69.08 since 7.0
    assert end is not None and end.kind == SETTLED
    assert end.peak_f == 76.28 and end.minutes == pytest.approx(10.2)
    assert not r.active and r.baseline_f is None


def test_second_jump_inside_an_episode_restarts_the_hold():
    """Thu 11-19 09:55: +6.8 F, back to 68.7 within 3 min, then +3.4 F again at 09:59; one episode only."""
    r = ThermostatRecal()
    _heated(r, 0, 67.82)
    assert r.observe(_at(1.0), 74.66, "idle").kind == STARTED
    assert r.observe(_at(4.0), 68.72, "idle") is None  # within 1 F: hold starts
    assert r.observe(_at(4.3), 72.14, "idle") is None  # second jump: hold reset, still the same episode
    assert r.observe(_at(8.0), 68.5, "idle") is None
    assert r.observe(_at(12.9), 68.5, "idle") is None  # 4.9 min held
    assert r.observe(_at(13.0), 68.5, "idle").kind == SETTLED
    assert r.count == 1


def test_no_episode_without_recent_heating():
    r = ThermostatRecal()
    assert r.observe(_at(0), 68.0, "idle") is None
    assert r.observe(_at(1), 76.0, "idle") is None  # sun on the thermostat, a heater nearby: not a trip
    assert not r.active
    r2 = ThermostatRecal()
    _heated(r2, 0, 68.0)
    stale = RECAL_HEATING_RECENT_S / 60 + 1
    assert r2.observe(_at(stale - 1), 68.0, "idle") is None
    assert r2.observe(_at(stale), 76.0, "idle") is None  # heating more than 10 min ago
    assert not r2.active


def test_no_episode_for_a_slow_rise_or_readings_far_apart():
    r = ThermostatRecal()
    _heated(r, 0, 68.0)
    for i, reading in enumerate((68.36, 68.72, 69.08, 69.44, 69.8, 70.16)):  # a real warm-up, 0.36 F per update
        assert r.observe(_at(0.5 * (i + 1)), reading, "heating") is None
    assert not r.active
    r2 = ThermostatRecal()
    _heated(r2, 0, 68.0)
    assert r2.observe(_at(RECAL_JUMP_WINDOW_S / 60 + 0.1), 71.0, "heating") is None  # > 5 min between readings
    assert not r2.active


def test_previous_action_counts_as_recent_heating():
    r = ThermostatRecal()
    assert r.observe(_at(0), 68.0, "idle") is None
    r.note_heating(_at(0.9))  # the old state of the event that reports idle was heating
    assert r.observe(_at(1), 75.0, "idle").kind == STARTED


def test_timed_out_after_an_hour_of_high_readings():
    r = ThermostatRecal()
    _heated(r, 0, 68.0)
    r.observe(_at(1), 75.0, "idle")
    assert r.observe(_at(30), 72.0, "idle") is None
    end = r.observe(_at(1 + RECAL_MAX_S / 60), 71.5, "idle")
    assert end.kind == TIMED_OUT and end.minutes == pytest.approx(60.0)
    assert not r.active
    # the next reading is compared with the last one; a fall is never a jump
    assert r.observe(_at(62), 69.0, "idle") is None and not r.active


def test_missing_readings_change_nothing_while_active():
    r = ThermostatRecal()
    _heated(r, 0, 68.0)
    r.observe(_at(1), 75.0, "idle")
    assert r.observe(_at(2), None, "idle") is None
    assert r.active


# --------------------------------------------------------------------------------------------- the resolver


def _state(value, name=None, attributes=None, age_h=0):
    s = MagicMock()
    s.state = value
    s.attributes = dict(attributes or {})
    if name:
        s.attributes["friendly_name"] = name
    # fresh against the test clock and the wall clock alike (assertions run outside the patched clock)
    s.last_reported = max(T0, _dt.datetime.now(_dt.UTC)) - _dt.timedelta(hours=age_h)
    s.last_updated = s.last_reported
    return s


def _hass(states):
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


def _active_recal() -> ThermostatRecal:
    r = ThermostatRecal()
    _heated(r, 0, 68.0)
    r.observe(_at(1), 75.0, "idle")
    assert r.active
    return r


def _resolve(states, recal, *, source="climate_fallback", sleep_window=False, companion=None):
    return resolve_indoor_temp_with_provenance(
        hass=_hass(states),
        source=source,
        unit="fahrenheit",
        indoor_temp_entity=DEDICATED,
        climate_entity=CLIMATE,
        in_sleep_window=sleep_window,
        sleep_indoor_temp_entity=SLEEP,
        now=T0,
        companion=companion,
        recal=recal,
    )


def _house(climate=75, sleep="70.5", dedicated=None):
    out = {CLIMATE: _state("heat", "Hallway", {"current_temperature": climate, "hvac_action": "idle"})}
    if sleep is not None:
        out[SLEEP] = _state(sleep, "Primary Bedroom")
    if dedicated is not None:
        out[DEDICATED] = _state(dedicated, "Dedicated")
    return out


@pytest.mark.parametrize(
    ("sleep", "dedicated", "value", "entity"),
    [
        ("70.5", "69.0", 70.5, SLEEP),  # the sleep sensor first
        ("unavailable", "69.0", 69.0, DEDICATED),  # then a dedicated sensor left in the config
        ("unavailable", None, 75.0, CLIMATE),  # none reads: the thermostat's own (warn only)
    ],
)
def test_resolver_reads_the_alternate_while_recalibrating(sleep, dedicated, value, entity):
    reading = _resolve(_house(sleep=sleep, dedicated=dedicated), _active_recal())
    assert (reading.value, reading.source_entity) == (value, entity)
    assert reading.primary_value == 75.0  # learning never sees the alternate
    assert reading.recal_entity == (None if entity == CLIMATE else entity)
    assert reading.remote_context is None  # the remote-sensor loop does not engage for a fallback


def test_resolver_unchanged_when_inactive_or_not_the_thermostat_source():
    assert _resolve(_house(), ThermostatRecal()).source_entity == CLIMATE
    assert _resolve(_house(), None).source_entity == CLIMATE
    states = _house(dedicated="69.0")
    reading = _resolve(states, _active_recal(), source="sensor")
    assert (reading.source_entity, reading.recal_entity) == (DEDICATED, None)


def test_resolver_sleep_window_keeps_the_sleep_swap():
    reading = _resolve(_house(), _active_recal(), sleep_window=True)
    assert reading.source_entity == SLEEP and reading.recal_entity is None and reading.remote_context is not None


def test_resolver_never_falls_back_to_a_sensor_on_the_thermostat():
    states = _house(sleep=None, dedicated=None)
    states[COMP] = _state("75.2", "Hallway Current Temperature")
    reading = resolve_indoor_temp_with_provenance(
        hass=_hass(states),
        source="climate_fallback",
        unit="fahrenheit",
        indoor_temp_entity=COMP,  # a misconfiguration: the companion as the dedicated sensor
        climate_entity=CLIMATE,
        now=T0,
        companion=CompanionSensors((COMP,)),
        recal=_active_recal(),
    )
    assert reading.recal_entity is None


def test_resolver_uses_the_alternate_when_the_thermostat_reads_nothing():
    reading = _resolve(_house(climate=None), _active_recal())
    assert (reading.value, reading.source_entity, reading.primary_value) == (70.5, SLEEP, None)


# --------------------------------------------------------------------------------------------- the coordinator


class Env:
    """Bare coordinator driving the real funnel, recalibration watch and announcement."""

    def __init__(self, *, sleep="70.5", dedicated=None, guard=True, source="climate_fallback"):
        self.states = _house(climate=68, sleep=sleep, dedicated=dedicated)
        self.states[CLIMATE] = _state("heat", "Hallway", {"current_temperature": 68, "hvac_action": "heating"})
        self.now = T0
        coord = object.__new__(ClimateAdvisorCoordinator)
        hass = MagicMock()
        hass.states.get = lambda eid: self.states.get(eid)
        entry = MagicMock()
        entry.title = "Zone 1"
        hass.config_entries.async_get_entry = lambda _eid: entry
        hass.config.language = "en"
        coord.hass = hass
        coord._entry_id = "01ABC"
        coord.config = {
            "climate_entity": CLIMATE,
            "indoor_temp_source": source,
            "indoor_temp_entity": DEDICATED if dedicated is not None else None,
            "sleep_indoor_temp_entity": SLEEP if sleep is not None else None,
            "temp_unit": "fahrenheit",
            "thermostat_recalibration_guard": guard,
        }
        coord._companion = CompanionSensors(())
        coord._recal = ThermostatRecal()
        coord._startup_coalesce_active = False
        coord._coast_candidates = {"heat": {"off_dt": T0}}
        coord._cycle_gap = MagicMock()
        self.coord = coord
        self.emit = MagicMock()
        self.ir = MagicMock()

    def thermostat(self, reading, action, minute):
        self.now = _at(minute)
        self.states[CLIMATE] = _state("heat", "Hallway", {"current_temperature": reading, "hvac_action": action})

    def tick(self):
        with (
            patch(f"{COORD}._in_sleep_window", return_value=False),
            patch(f"{COORD}.dt_util.now", side_effect=lambda: self.now),
            patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", side_effect=lambda: self.now),
            patch.object(ClimateAdvisorCoordinator, "_emit_event", self.emit),
            patch(f"{COORD}.ir", self.ir),
        ):
            self.coord._refresh_indoor_sensor_health()

    def events(self, kind):
        return [c.args[1] for c in self.emit.call_args_list if c.args[0] == kind]

    def trip(self):
        """Heating at 68 F, then the jump to 75 F as the heating stops, as on the owner's thermostat."""
        self.thermostat(68, "heating", 0)
        self.tick()
        self.thermostat(75, "idle", 1)
        self.tick()

    def settle(self):
        for minute in (5, 8, 11, 14):
            self.thermostat(68, "idle", minute)
            self.tick()


@pytest.fixture(autouse=True)
def _debug(caplog):
    caplog.set_level(logging.DEBUG)


def _warnings(caplog, text):
    return [r for r in caplog.records if r.levelno == logging.WARNING and text in r.getMessage()]


def test_enter_and_leave_with_the_bedroom_sensor(caplog):
    env = Env()
    env.trip()
    coord = env.coord
    # detected: one WARNING, one event, one Repairs notice, the room from the bedroom sensor
    assert len(_warnings(caplog, "Thermostat recalibration detected")) == 1
    started = env.events("thermostat_recalibration_started")
    assert len(started) == 1 and started[0]["alternate_entity"] == SLEEP and started[0]["jump_f"] == 7.0
    kwargs = env.ir.async_create_issue.call_args.kwargs
    assert env.ir.async_create_issue.call_args.args[2] == "thermostat_recalibration_01ABC"
    assert kwargs["translation_key"] == "thermostat_recalibration" and kwargs["is_fixable"] is False
    assert kwargs["is_persistent"] is True and kwargs["translation_placeholders"]["sensor"] == "Primary Bedroom"
    assert kwargs["translation_placeholders"]["time"]  # the local date and time, in the zone's language
    switch = env.events("indoor_source_changed")
    assert [e["to_entity"] for e in switch] == [SLEEP]
    assert switch[0]["reason_key"] == "status.indoor_source.reason_recal_started"
    assert coord._get_indoor_temp() == 70.5
    # learning: no reading, coast windows dropped, cycle recorder gap; chart rows flag the thermostat
    assert coord._learning_indoor_f() is None
    assert coord._coast_candidates == {}
    coord._cycle_gap.assert_called_once_with("unavailable")
    assert coord._chart_recal_false() == [CLIMATE]
    assert coord._thermostat_recal_status()["entity"] == SLEEP

    env.settle()
    ended = env.events("thermostat_recalibration_ended")
    assert len(ended) == 1 and ended[0]["how"] == "settled"
    switch = env.events("indoor_source_changed")
    assert [e["to_entity"] for e in switch] == [SLEEP, CLIMATE]
    assert switch[1]["reason_key"] == "status.indoor_source.reason_recal_settled"
    assert coord._get_indoor_temp() == 68.0 and coord._learning_indoor_f() == 68.0
    assert coord._chart_recal_false() is None and coord._thermostat_recal_status() is None
    assert len(_warnings(caplog, "Thermostat recalibration")) == 1  # the settle line is INFO


def test_enter_and_leave_with_no_other_sensor_warns_only(caplog):
    env = Env(sleep=None)
    env.trip()
    assert len(_warnings(caplog, "no other temperature sensor configured")) == 1
    assert env.ir.async_create_issue.call_args.kwargs["translation_key"] == "thermostat_recalibration_no_alternate"
    assert env.events("indoor_source_changed") == []  # nothing to switch to
    assert env.coord._get_indoor_temp() == 75.0  # still the thermostat's reading ...
    assert env.coord._learning_indoor_f() is None  # ... but never learned from
    assert env.coord._thermostat_recal_status() == {"since": _at(1).isoformat(), "entity": None, "name": None}
    env.settle()
    assert [e["how"] for e in env.events("thermostat_recalibration_ended")] == ["settled"]
    assert env.events("indoor_source_changed") == []
    assert env.coord._learning_indoor_f() == 68.0


def test_dedicated_sensor_is_the_alternate_when_there_is_no_sleep_sensor():
    env = Env(sleep=None, dedicated="69.2")
    env.trip()
    assert env.events("thermostat_recalibration_started")[0]["alternate_entity"] == DEDICATED
    assert env.coord._get_indoor_temp() == 69.2


def test_timed_out_episode_logs_a_warning(caplog):
    env = Env()
    env.trip()
    env.thermostat(73, "idle", 61.5)
    env.tick()
    ended = env.events("thermostat_recalibration_ended")
    assert ended and ended[0]["how"] == "timed_out"
    assert len(_warnings(caplog, "Thermostat recalibration ended")) == 1
    assert env.coord._get_indoor_temp() == 73.0


@pytest.mark.parametrize(("guard", "source"), [(False, "climate_fallback"), (True, "sensor")])
def test_guard_off_or_dedicated_source_does_nothing(caplog, guard, source):
    env = Env(guard=guard, source=source, dedicated="69.0")
    env.trip()
    assert env.events("thermostat_recalibration_started") == []
    assert _warnings(caplog, "Thermostat recalibration") == []
    env.ir.async_create_issue.assert_not_called()
    assert env.coord._learning_indoor_f() is not None


def test_previous_action_from_the_listener_counts_as_heating():
    env = Env()
    env.thermostat(68, "idle", 0)
    env.tick()  # never saw "heating" ...
    env.thermostat(75, "idle", 0.1)
    with (
        patch(f"{COORD}.dt_util.now", side_effect=lambda: env.now),
        patch.object(ClimateAdvisorCoordinator, "_emit_event", env.emit),
        patch(f"{COORD}.ir", env.ir),
    ):
        env.coord._observe_thermostat_recal(previous_action="heating")  # ... but the event's old state was heating
    assert env.coord._recal.active


def test_sleep_window_taking_over_the_fallback_sensor_is_not_a_settle():
    env = Env()
    env.trip()
    coord = env.coord
    assert coord._indoor_source_announced_recal is True
    with (
        patch(f"{COORD}._in_sleep_window", return_value=True),
        patch(f"{COORD}.dt_util.now", side_effect=lambda: env.now),
        patch.object(ClimateAdvisorCoordinator, "_emit_event", env.emit),
        patch(f"{COORD}.ir", env.ir),
    ):
        coord._refresh_indoor_sensor_health()  # the sleep window starts: same bedroom sensor, now as sleep sensor
    assert coord._indoor_source_announced_recal is False


def test_watch_never_raises(caplog):
    env = Env()
    env.coord.hass.states.get = MagicMock(side_effect=RuntimeError("boom"))
    with patch(f"{COORD}.dt_util.now", return_value=T0):
        env.coord._observe_thermostat_recal()
    assert any("Thermostat recalibration watch failed" in r.getMessage() for r in caplog.records)


def test_repairs_failure_never_raises(caplog):
    env = Env()
    env.ir.async_create_issue.side_effect = RuntimeError("registry down")
    env.trip()
    assert env.events("thermostat_recalibration_started")
    assert any("Repairs notice failed" in r.getMessage() for r in caplog.records)


def test_helpers_on_a_coordinator_without_the_state():
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"climate_entity": CLIMATE}
    assert coord._recal_for_resolver() is None
    assert coord._thermostat_recal_active() is False
    assert coord._thermostat_recal_status() is None
    coord._observe_thermostat_recal()  # no state: a no-op


def test_close_learning_windows_without_coast_windows():
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._coast_candidates = None
    coord._cycle_gap = MagicMock()
    coord._recal_close_learning_windows()
    coord._cycle_gap.assert_called_once_with("unavailable")


# --------------------------------------------------------------------------------------------- engine, chart, v4


def test_engine_shares_the_state_unless_the_guard_is_off():
    engine = object.__new__(AutomationEngine)
    engine.hass = MagicMock()
    engine.climate_entity = CLIMATE
    engine.indoor_recal = ThermostatRecal()
    engine.config = {"temp_unit": "fahrenheit"}
    with patch("custom_components.climate_advisor.automation._in_sleep_window", return_value=False):
        assert engine._indoor_resolver_kwargs()["recal"] is engine.indoor_recal
        engine.config = {"temp_unit": "fahrenheit", "thermostat_recalibration_guard": False}
        assert engine._indoor_resolver_kwargs()["recal"] is None


def test_chart_row_carries_recal_false_only_when_given(tmp_path):
    log = ChartStateLog(tmp_path)
    log.append(hvac="idle", fan=False, indoor=70.5, outdoor=50.0, recal_false=[CLIMATE, COMP], ts=T0.isoformat())
    log.append(hvac="idle", fan=False, indoor=68.0, outdoor=50.0, ts=_at(30).isoformat())
    rows = log._entries
    assert rows[0]["recal_false"] == [CLIMATE, COMP] and "recal_false" not in rows[1]


def _row(minute, sensors, *, indoor, band, recal=None):
    row = {"ts": _at(minute).isoformat(), "hvac": "idle", "indoor": indoor, "outdoor": 50.0, "band_sensor": band}
    row["indoor_sensors"] = sensors
    if recal:
        row["recal_false"] = recal
    return row


def test_v4_skips_the_recalibrating_readings():
    clean = {CLIMATE: 68.0, COMP: 68.18, SLEEP: 70.0}
    false = {CLIMATE: 75.0, COMP: 75.38, SLEEP: 70.5}
    rows = [
        _row(-25, clean, indoor=68.18, band=COMP),
        _row(5, false, indoor=70.5, band=SLEEP, recal=[CLIMATE, COMP]),  # with an alternate: indoor is the bedroom
        _row(10, false, indoor=75.38, band=COMP, recal=[CLIMATE, COMP]),  # without: indoor is the thermostat's
        _row(25, false, indoor=75.2, band=COMP, recal=[CLIMATE, COMP]),
    ]
    samples = samples_from_chart_log(rows, tz=_dt.UTC, primary=COMP, primary_aliases=(CLIMATE,))
    late = [s for s in samples if s.ts >= _at(15)]
    assert late, samples
    for s in late:
        assert s.readings[COMP] == 68.18 and s.readings[CLIMATE] == 68.0  # the last real readings, never 75
        assert s.readings[SLEEP] == 70.5  # the bedroom is not affected
        assert s.readings["_ca"] == 70.5  # the bedroom row kept, the thermostat's own indoor row dropped


def test_v4_ignores_a_malformed_marker():
    rows = [_row(-25, {COMP: 68.0}, indoor=68.0, band=COMP), _row(5, {COMP: 75.0}, indoor=75.0, band=COMP)]
    rows[1]["recal_false"] = "climate.x"  # not a list: ignored
    samples = samples_from_chart_log(rows, tz=_dt.UTC, primary=COMP)
    assert samples[-1].readings[COMP] == 75.0


# --------------------------------------------------------------------------------------------- Activity rows


def test_activity_rows_name_the_recalibration():
    label, settings = EVENT_RENDERERS["thermostat_recalibration_started"](
        {"thermostat_name": "Hallway", "alternate_name": "Primary Bedroom", "reading_f": 75.56, "baseline_f": 68.18},
        "fahrenheit",
    )
    assert (
        label == "Hallway recalibrating after the heating stopped (reading too high) — room read from Primary Bedroom"
    )
    assert "now 75.6°F" in settings and "was 68.2°F" in settings
    label, _ = EVENT_RENDERERS["thermostat_recalibration_started"]({"thermostat": CLIMATE}, "fahrenheit")
    assert label == f"{CLIMATE} recalibrating after the heating stopped (reading too high)"
    label, settings = EVENT_RENDERERS["thermostat_recalibration_ended"](
        {"thermostat_name": "Hallway", "how": "settled", "minutes": 19.8, "peak_f": 75.56}, "fahrenheit"
    )
    assert label == "Hallway settled after recalibrating" and "19.8 min" in settings and "peak 75.6°F" in settings
    label, _ = EVENT_RENDERERS["thermostat_recalibration_ended"]({"how": "timed_out"}, "fahrenheit")
    assert "still reading high after an hour" in label
    assert {"thermostat_recalibration_started", "thermostat_recalibration_ended"} <= _NO_DEDUP


@pytest.mark.parametrize("lang", ["de", "ja", "ar", "zh-Hant"])
def test_activity_and_reason_texts_render_in_every_language(lang):
    started = i18n.render(
        i18n.Msg("activity.thermostat_recal_started", name="Hallway", has_alt="yes", alt="Bedroom"), lang
    )
    assert "Hallway" in started and "Bedroom" in started
    ended = i18n.render(i18n.Msg("activity.thermostat_recal_ended", name="Hallway", how="timed_out"), lang)
    assert "Hallway" in ended
    for key in ("status.indoor_source.reason_recal_started", "status.indoor_source.reason_recal_settled"):
        assert i18n.render(i18n.Msg(key), lang)
