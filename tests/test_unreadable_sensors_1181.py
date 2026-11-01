"""Issues #1181 / #1177 / #1176: what Climate Advisor does when a sensor it relies on goes dark.

Occupant story. A window is open and Climate Advisor has paused the air conditioning. The window sensor drops
off the network. Climate Advisor used to read the dead sensor as "closed", end the pause and run the air
conditioner into the open window (rule 3). It must instead keep the pause until the sensor reports again (or the
occupant changes the thermostat), tell the occupant what it is doing, and agree with itself about what a dark
sensor means for the whole-house fan (rule 1) and for the heating/cooling restore after a fan stop (rule 2).

Every test drives the real coordinator / engine (the production-harness session for the door/window path, the
enforcer test harness for the fan path). Nothing re-implements a decision.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.climate_advisor.coordinator as coordinator_module
from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.entity_health import EntityHealthIssue, entity_health_consequence
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome
from custom_components.climate_advisor.whf_safety_decision import (
    SensorReading,
    WhfSafetySnapshot,
    house_is_sealed,
)
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.run_production import FakeHassSession

_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
WINDOW = "binary_sensor.living_room_window"
DOOR = "binary_sensor.front_door"
REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Door/window path: the production-harness session (real coordinator + real engine)
# ---------------------------------------------------------------------------

_CONFIG = {
    "comfort_heat": 68,
    "comfort_cool": 74,
    "setback_heat": 60,
    "setback_cool": 80,
    "sleep_heat": 66,
    "sleep_cool": 72,
    "sleep_time": "22:30",
    "wake_time": "06:30",
    "manual_grace_seconds": 3600,
    "override_confirm_seconds": 600,
    "initial_thermostat_mode": "cool",
    "door_window_sensors": [WINDOW],
}


def _t(hhmm: str) -> str:
    return f"2026-07-15T{hhmm}:00"


def _at(hhmm: str) -> datetime:
    return datetime.fromisoformat(_t(hhmm)).replace(tzinfo=UTC)


def _session(sensors: list[str] | None = None, first_event: str = "13:00") -> FakeHassSession:
    scenario = {
        "name": "unreadable_sensors_1181",
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "config": {**_CONFIG, "door_window_sensors": sensors or [WINDOW]},
        "events": [{"time": _t(first_event), "type": "temp_update", "indoor_f": 78.0, "outdoor_f": 93.0}],
    }
    session = FakeHassSession(scenario, use_coordinator=True)
    session.feed(scenario["events"][0])
    return session


def _go(session: FakeHassSession, hhmm: str, event: dict) -> None:
    session.advance_to(_at(hhmm))
    session.feed({"time": _t(hhmm), **event})
    # Let the zero-length debounce timer the event armed fire (the harness settles only what is already due).
    session.advance_to(_at(hhmm) + timedelta(seconds=30))


def _hot_day(session: FakeHassSession) -> None:
    _go(session, "13:01", {"type": "classification", "day_type": "hot", "hvac_mode": "cool"})


def _open(session: FakeHassSession, hhmm: str, sensor: str = WINDOW) -> None:
    _go(session, hhmm, {"type": "sensor_open", "entity": sensor})


def _dark(session: FakeHassSession, hhmm: str, sensor: str = WINDOW) -> None:
    _go(session, hhmm, {"type": "sensor_unavailable", "entity": sensor})


def _close(session: FakeHassSession, hhmm: str, sensor: str = WINDOW) -> None:
    _go(session, hhmm, {"type": "sensor_close", "entity": sensor})


def _commanded_active(session: FakeHassSession, since: str) -> list[dict]:
    """Every climate command that put heating or cooling on at/after ``since``."""
    cutoff = _at(since)
    out = []
    for entry in session.fake_hass.action_log:
        if entry.get("domain") != "climate":
            continue
        data = entry.get("data", {})
        if data.get("hvac_mode") in ("cool", "heat", "heat_cool", "auto"):
            ts = entry.get("ts")
            if ts is None or ts >= cutoff:
                out.append(entry)
    return out


class TestDarkSensorKeepsThePauseItCaused:
    """Owner decision D1 (rule 3)."""

    def test_open_then_unavailable_keeps_cooling_off(self):
        with _session() as s:
            _hot_day(s)
            _open(s, "14:00")
            assert s.engine._paused_by_door is True

            _dark(s, "14:20")
            _go(s, "14:30", {"type": "classification", "day_type": "hot", "hvac_mode": "cool"})

            assert s.engine._paused_by_door is True, "a dead window sensor ended the pause with the window still open"
            assert s.thermostat().mode == "off"
            assert _commanded_active(s, "14:00") == []
            assert s.coordinator._any_sensor_holding_open() is True

    def test_dark_sensor_then_real_close_ends_the_pause(self):
        """Symmetric control: the sensor reporting closed again is a real close and resumes cooling."""
        with _session() as s:
            _hot_day(s)
            _open(s, "14:00")
            _dark(s, "14:20")
            assert s.engine._paused_by_door is True

            _close(s, "14:40")

            assert s.engine._paused_by_door is False
            assert s.thermostat().mode == "cool"
            assert s.coordinator._any_sensor_holding_open() is False

    def test_plain_close_without_an_outage_still_resumes(self):
        """Symmetric control: no dark sensor involved, the ordinary close path is unchanged."""
        with _session() as s:
            _hot_day(s)
            _open(s, "14:00")
            _close(s, "14:20")
            assert s.engine._paused_by_door is False
            assert s.thermostat().mode == "cool"

    def test_dark_sensor_that_last_read_closed_does_not_block_another_windows_close(self):
        with _session([WINDOW, DOOR]) as s:
            _hot_day(s)
            _open(s, "13:10", DOOR)
            _close(s, "13:20", DOOR)  # the door's last real reading is "closed"
            _dark(s, "13:30", DOOR)  # ... then it goes dark
            _open(s, "14:00", WINDOW)
            assert s.engine._paused_by_door is True

            _close(s, "14:20", WINDOW)

            assert s.engine._paused_by_door is False, "a dark sensor that last read closed blocked the real close"
            assert s.thermostat().mode == "cool"

    def test_dark_sensor_that_last_read_open_blocks_another_windows_close(self):
        """Symmetric control for the case above: the same shape, but the dark sensor last read OPEN."""
        with _session([WINDOW, DOOR]) as s:
            _hot_day(s)
            _open(s, "13:10", DOOR)
            _dark(s, "13:30", DOOR)  # last real reading: open
            _open(s, "14:00", WINDOW)
            _close(s, "14:20", WINDOW)

            assert s.engine._paused_by_door is True
            assert s.thermostat().mode == "off"

    def test_dark_at_start_never_starts_a_pause(self):
        """A sensor that has never reported (dark since start-up) counts closed; cooling is not held back."""
        with _session() as s:
            s.fake_hass.states.set_simple(WINDOW, "unavailable")
            _hot_day(s)
            _go(s, "13:30", {"type": "classification", "day_type": "hot", "hvac_mode": "cool"})

            assert s.engine._paused_by_door is False
            assert s.thermostat().mode == "cool"
            assert s.coordinator._any_sensor_holding_open() is False

    def test_occupant_turning_cooling_on_during_the_dark_period_still_wins(self):
        """The existing rule-3 exception: an occupant change while paused turns HVAC on and is kept."""
        with _session() as s:
            _hot_day(s)
            _open(s, "14:00")
            _dark(s, "14:20")
            assert s.thermostat().mode == "off"

            # A mode the automation does not want ("heat" on a cooling day), so it is a real, protected change
            # rather than one that "agrees with automation" and is handed back to the open-window hold.
            _go(s, "14:30", {"type": "thermostat_state_changed", "hvac_mode": "heat", "temperature": 72.0})
            s.advance_to(_at("14:45"))

            assert s.thermostat().mode == "heat", "the occupant's change was switched off again"
            assert s.engine._manual_override_active is True

    def test_grace_expiry_with_the_sensor_still_dark_after_open_repauses(self):
        """When the occupant's grace ends and the sensor is still dark (last read open) the pause returns."""
        with _session() as s:
            _hot_day(s)
            _open(s, "14:00")
            _dark(s, "14:20")
            _go(s, "14:30", {"type": "thermostat_state_changed", "hvac_mode": "heat", "temperature": 72.0})
            s.advance_to(_at("16:45"))  # confirm (10 min) + 60 min grace long over

            assert s.engine._paused_by_door is True
            assert s.thermostat().mode == "off"


class TestLastReadingIsPersisted:
    def test_a_restart_during_the_outage_keeps_holding(self):
        config_dir = tempfile.mkdtemp(prefix="ca_1181_")
        with _session() as s1:
            s1.coordinator._state_persistence = type(s1.coordinator._state_persistence)(
                Path(config_dir), getattr(s1.coordinator, "_entry_id", "")
            )
            _hot_day(s1)
            _open(s1, "14:00")
            _dark(s1, "14:20")
            run_coro(s1.coordinator._async_save_state())

        saved = json.loads(next(Path(config_dir).glob("climate_advisor_state*.json")).read_text())
        assert saved["sensor_last_real_reading"] == {WINDOW: "open"}

        coord2, hass2, scheduler2, _log = build_headless_coordinator(
            config={**_CONFIG}, config_dir=config_dir, skip_startup_coalesce=True
        )
        hass2.states.set_simple(WINDOW, "unavailable")  # still dark after the restart
        with scheduler2.installed():
            run_coro(coord2.async_restore_state())
            coord2._resolved_sensors = [WINDOW]
            assert coord2._sensor_last_readings() == {WINDOW: "open"}
            assert coord2._any_sensor_holding_open() is True

    def test_a_fresh_install_with_no_record_holds_nothing(self):
        coord, hass, scheduler, _log = build_headless_coordinator(config={**_CONFIG}, skip_startup_coalesce=True)
        coord._resolved_sensors = [WINDOW]
        hass.states.set_simple(WINDOW, "unavailable")
        with scheduler.installed():
            assert coord._any_sensor_holding_open() is False

    def test_a_live_real_reading_replaces_a_restored_record(self):
        coord, hass, scheduler, _log = build_headless_coordinator(config={**_CONFIG}, skip_startup_coalesce=True)
        coord._resolved_sensors = [WINDOW]
        coord._sensor_last_readings()[WINDOW] = "open"
        hass.states.set_simple(WINDOW, "off")  # reports closed after the restart
        with scheduler.installed():
            assert coord._sync_sensor_last_readings() is True
            assert coord._sensor_last_readings() == {WINDOW: "closed"}
            assert coord._any_sensor_holding_open() is False

    def test_a_record_for_a_sensor_no_longer_configured_is_dropped(self):
        """A removed sensor's stale "open" record must not survive in the state file."""
        coord, hass, scheduler, _log = build_headless_coordinator(config={**_CONFIG}, skip_startup_coalesce=True)
        coord._resolved_sensors = [WINDOW]
        hass.states.set_simple(WINDOW, "unavailable")
        coord._sensor_last_readings().update({WINDOW: "closed", "binary_sensor.removed_door": "open"})
        with scheduler.installed():
            assert coord._sync_sensor_last_readings() is True
            assert coord._sensor_last_readings() == {WINDOW: "closed"}
            assert coord._build_state_dict()["sensor_last_real_reading"] == {WINDOW: "closed"}
            assert coord._any_sensor_holding_open() is False


# ---------------------------------------------------------------------------
# Owner decision D2 / D5: telling the occupant (entity health, dashboard, contact status)
# ---------------------------------------------------------------------------


def _health_coordinator(states: dict[str, str | None], *, sensors: list[str], last: dict[str, str] | None = None):
    """A real coordinator's real entity-health + contact methods over a state table (None = missing)."""
    cls = coordinator_module.ClimateAdvisorCoordinator
    coord = object.__new__(cls)
    coord.config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.home",
        "notify_service": "notify.mobile_app",
        "door_window_sensors": sensors,
        "fan_mode": "whole_house_fan",
        "fan_entity": "fan.whf",
    }
    coord._resolved_sensors = list(sensors)
    coord._entity_health_state = {}
    coord._startup_coalesce_active = False
    coord._sensor_last_real_reading = dict(last or {})

    def _get(entity_id):
        raw = states.get(entity_id)
        if raw is None:
            return None
        st = MagicMock()
        st.state = raw
        return st

    hass = MagicMock()
    hass.states.get.side_effect = _get
    hass.services.has_service.side_effect = lambda domain, service: True
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    coord.hass = hass
    coord.automation_engine = MagicMock(_fan_active=False, _fan_override_active=False)
    coord._get_fan_physical_state = lambda: False
    sent: list[tuple[str, str, str]] = []
    coord._notify_in_background = lambda title, message, kind: sent.append((title, message, kind))
    return coord, sent


_BASE_STATES = {"climate.thermostat": "cool", "weather.home": "sunny", "fan.whf": "off"}


@pytest.fixture
def _real_clock(monkeypatch):
    """Entity-health reminders do real datetime arithmetic; the stub layer's dt_util.now() is a MagicMock."""
    moments = iter(_NOW + timedelta(minutes=30 * i) for i in range(1000))
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: next(moments))


@pytest.mark.usefixtures("_real_clock")
class TestOccupantIsToldAboutADarkSensor:
    def test_last_read_open_says_heating_and_cooling_stay_paused(self):
        coord, sent = _health_coordinator(
            {**_BASE_STATES, WINDOW: "unavailable"}, sensors=[WINDOW], last={WINDOW: "open"}
        )
        issues = coord._run_entity_health_check()

        assert len(sent) == 1
        title, message, kind = sent[0]
        assert kind == "entity_health"
        assert WINDOW in message
        assert "last read open" in message
        assert "heating and cooling stay paused" in message
        assert "until it reports again or you change the thermostat" in message
        assert issues[0].detail in message

    def test_last_read_closed_says_it_is_treated_as_closed(self):
        coord, sent = _health_coordinator(
            {**_BASE_STATES, WINDOW: "unavailable"}, sensors=[WINDOW], last={WINDOW: "closed"}
        )
        coord._run_entity_health_check()

        message = sent[0][1]
        assert "last read closed" in message
        assert "treats it as closed" in message
        assert "heating or cooling may run" in message
        assert "stay paused" not in message

    def test_second_sensor_failing_during_the_first_outage_is_announced(self):
        states = {**_BASE_STATES, WINDOW: "unavailable", DOOR: "off"}
        coord, sent = _health_coordinator(states, sensors=[WINDOW, DOOR], last={WINDOW: "open", DOOR: "closed"})
        coord._run_entity_health_check()
        assert len(sent) == 1

        states[DOOR] = "unavailable"  # the door drops off too while the window is still dark
        coord._run_entity_health_check()

        assert len(sent) == 2, "the second sensor's outage was never announced (shared config_key slot)"
        assert DOOR in sent[1][1]
        assert WINDOW not in sent[1][1], "the first sensor was announced again"

    def test_a_still_dark_sensor_is_not_announced_every_cycle(self):
        states = {**_BASE_STATES, WINDOW: "unavailable"}
        coord, sent = _health_coordinator(states, sensors=[WINDOW], last={WINDOW: "open"})
        for _ in range(3):
            coord._run_entity_health_check()
        assert len(sent) == 1

    def test_recovery_clears_the_slot_and_the_next_outage_is_announced_again(self):
        states = {**_BASE_STATES, WINDOW: "unavailable"}
        coord, sent = _health_coordinator(states, sensors=[WINDOW], last={WINDOW: "open"})
        coord._run_entity_health_check()
        states[WINDOW] = "off"
        assert coord._run_entity_health_check() == []
        assert coord._entity_health_state == {}
        states[WINDOW] = "unavailable"
        coord._run_entity_health_check()
        assert len(sent) == 2

    def test_issue_state_keys_are_per_sensor_for_door_sensors_and_per_key_otherwise(self):
        a = EntityHealthIssue("door_window_sensors", WINDOW, "Door/window sensor", "degraded", "unavailable")
        b = EntityHealthIssue("door_window_sensors", DOOR, "Door/window sensor", "degraded", "unavailable")
        t = EntityHealthIssue("climate_entity", "climate.thermostat", "Thermostat", "critical", "unavailable")
        assert a.state_key != b.state_key
        assert t.state_key == "climate_entity"

    def test_dashboard_issue_line_and_contact_status_show_the_dark_sensor(self):
        coord, _sent = _health_coordinator(
            {**_BASE_STATES, WINDOW: "unavailable", DOOR: "off"},
            sensors=[WINDOW, DOOR],
            last={WINDOW: "open", DOOR: "closed"},
        )
        issues = coord._run_entity_health_check()
        window_issue = next(i for i in issues if i.entity_id == WINDOW)
        assert "heating and cooling stay paused" in window_issue.detail  # the text the dashboard line carries

        assert coord._compute_contact_status() != "all closed"
        assert "not responding" in coord._compute_contact_status()
        details = {d["entity_id"]: d for d in coord._compute_contact_details()}
        assert details[WINDOW]["unreadable"] is True
        assert details[WINDOW]["last_reading"] == "open"
        assert details[WINDOW]["treated_as"] == "open"
        assert details[DOOR]["unreadable"] is False
        assert details[DOOR]["treated_as"] == "closed"

    def test_contact_status_is_all_closed_only_when_every_sensor_reads(self):
        coord, _ = _health_coordinator({**_BASE_STATES, WINDOW: "off", DOOR: "off"}, sensors=[WINDOW, DOOR])
        assert coord._compute_contact_status() == "all closed"
        assert all(d["unreadable"] is False for d in coord._compute_contact_details())

    def test_the_dashboard_files_render_the_new_fields(self):
        html = (REPO_ROOT / "custom_components/climate_advisor/frontend/index.html").read_text(encoding="utf-8")
        assert "i.detail" in html
        assert "c.unreadable" in html


@pytest.mark.usefixtures("_real_clock")
class TestThermostatDarkWhileTheFanRuns:
    """Owner decision D5 (rule 2): notice only, reusing entity health."""

    def _coordinator(self, *, fan_running: bool):
        coord, sent = _health_coordinator({"weather.home": "sunny", "fan.whf": "on"}, sensors=[])
        coord._get_fan_physical_state = lambda: fan_running
        return coord, sent

    def test_says_the_fan_and_heating_cooling_cannot_be_checked(self):
        coord, sent = self._coordinator(fan_running=True)  # climate.thermostat is missing from HA
        coord._run_entity_health_check()
        message = sent[0][1]
        assert "never on together" in message
        assert "until the thermostat responds" in message

    def test_believed_running_counts_even_when_the_fan_state_is_unreadable(self):
        coord, sent = self._coordinator(fan_running=False)
        coord._get_fan_physical_state = lambda: None
        coord.automation_engine._fan_override_active = True
        coord._run_entity_health_check()
        assert "never on together" in sent[0][1]

    def test_fan_off_keeps_the_generic_text(self):
        coord, sent = self._coordinator(fan_running=False)
        coord._run_entity_health_check()
        assert "never on together" not in sent[0][1]
        assert "may not be able to control your HVAC" in sent[0][1]

    def test_other_entities_never_get_the_fan_text(self):
        issue = EntityHealthIssue("weather_entity", "weather.home", "Weather source", "critical", "unavailable")
        assert entity_health_consequence(issue, whf_running=True) == ""


# ---------------------------------------------------------------------------
# Owner decision D3 (#1177): one reading of the sensors for the enforcer, the promise and the ceiling guard
# ---------------------------------------------------------------------------


def _reading_coordinator(states: dict[str, tuple[str, datetime | None] | None], *, started_s_ago: float | None):
    """A real coordinator whose real snapshot builder and promise reading share one state table."""
    cls = coordinator_module.ClimateAdvisorCoordinator
    coord = object.__new__(cls)
    coord.config = {CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whf", "climate_entity": "climate.t"}
    coord._resolved_sensors = list(states)
    coord._whf_safety_started_at = _NOW - timedelta(seconds=started_s_ago) if started_s_ago is not None else None

    def _get(entity_id):
        if entity_id == "fan.whf":
            fan = MagicMock()
            fan.state = "on"
            fan.last_changed = _NOW - timedelta(seconds=600)
            return fan
        if entity_id == "climate.t":
            cs = MagicMock()
            cs.state = "off"
            cs.attributes = {}
            return cs
        row = states.get(entity_id)
        if row is None:
            return None
        st = MagicMock()
        st.state, st.last_changed = row
        return st

    coord.hass = MagicMock()
    coord.hass.states.get = _get
    coord._get_fan_physical_state = lambda: True
    coord._get_indoor_temp = lambda: 72.0
    coord._current_classification = None
    coord._last_outdoor_temp = 80.0
    coord._automation_enabled = True
    coord._occupant_fan_on_at_from_ground_truth = lambda *a, **k: None
    coord._occupant_hvac_on_at_from_ground_truth = lambda *a, **k: None
    return coord


def _both_judges(coord) -> tuple[bool, bool | None]:
    """(enforcer says sealed, P9 reading says contacts_open)."""
    with patch.object(coordinator_module.dt_util, "utcnow", return_value=_NOW):
        sealed = house_is_sealed(coord._build_whf_safety_snapshot().sensors)
        contacts_open = coord._promise_reading_now(_NOW).contacts_open
    return sealed, contacts_open


class TestEnforcerAndPromiseAgree:
    def test_a_missing_sensor_just_after_start_up_counts_open_for_both(self):
        coord = _reading_coordinator({WINDOW: None}, started_s_ago=10.0)
        sealed, contacts_open = _both_judges(coord)
        assert sealed is False
        assert contacts_open is True, "P9 read a MISSING sensor as closed while the enforcer still counted it open"

    def test_a_missing_sensor_after_the_grace_counts_closed_for_both(self):
        coord = _reading_coordinator({WINDOW: None}, started_s_ago=300.0)
        sealed, contacts_open = _both_judges(coord)
        assert sealed is True
        assert contacts_open is False

    def test_a_missing_sensor_with_no_known_start_time_counts_closed_for_both(self):
        # Rule 1 fails safe: a sensor of unknown age must never keep the fan running on a sealed house
        # (counting it "dark for 0 s" read it open forever).
        coord = _reading_coordinator({WINDOW: None}, started_s_ago=None)
        sealed, contacts_open = _both_judges(coord)
        assert sealed is True, "a missing sensor with no known start time kept the house 'unsealed' forever"
        assert contacts_open is False

    @pytest.mark.parametrize(("dark_for_s", "sealed"), [(30.0, False), (90.0, True)])
    def test_an_unavailable_sensor_flips_for_both_at_the_same_moment(self, dark_for_s, sealed):
        coord = _reading_coordinator(
            {WINDOW: ("unavailable", _NOW - timedelta(seconds=dark_for_s))}, started_s_ago=3600.0
        )
        enforcer_sealed, contacts_open = _both_judges(coord)
        assert enforcer_sealed is sealed
        assert contacts_open is (not sealed)

    def test_a_really_open_sensor_is_open_for_both(self):
        coord = _reading_coordinator({WINDOW: ("on", _NOW - timedelta(seconds=500))}, started_s_ago=3600.0)
        sealed, contacts_open = _both_judges(coord)
        assert (sealed, contacts_open) == (False, True)

    def test_a_really_closed_sensor_is_closed_for_both(self):
        coord = _reading_coordinator({WINDOW: ("off", _NOW - timedelta(seconds=500))}, started_s_ago=3600.0)
        sealed, contacts_open = _both_judges(coord)
        assert (sealed, contacts_open) == (True, False)

    def test_no_sensors_configured_is_unknown_for_both(self):
        coord = _reading_coordinator({}, started_s_ago=3600.0)
        sealed, contacts_open = _both_judges(coord)
        assert sealed is False
        assert contacts_open is None


def _precool_engine(coord) -> AutomationEngine:
    """A WHF engine whose snapshot is the coordinator's REAL snapshot builder (the enforcer's own reading)."""
    from tests.test_savings_ceiling_guard_1093 import _make_engine

    engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=list(coord._resolved_sensors))
    engine._whf_safety_snapshot_callback = coord._build_whf_safety_snapshot
    engine._sensor_check_callback = coord._any_sensor_open
    return engine


class TestPreCoolArmsWhenTheFanCannotRun:
    def _run(self, coord):
        from tests.test_savings_ceiling_guard_1093 import _BREACH_77, _run

        engine = _precool_engine(coord)
        with (
            patch.object(coordinator_module.dt_util, "utcnow", return_value=_NOW),
            patch("custom_components.climate_advisor.automation.dt_util.now", return_value=_NOW),
        ):
            decision, _events = asyncio.run(_run(engine, _BREACH_77))
        return engine, decision

    def test_a_sensor_dark_past_the_grace_arms_the_pre_cool_guard(self):
        coord = _reading_coordinator({WINDOW: ("unavailable", _NOW - timedelta(seconds=300))}, started_s_ago=3600.0)
        engine, decision = self._run(coord)
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE, "fan cannot run but the compressor guard slept"
        assert engine._set_temperature.await_args.args[0] == 77.0

    def test_a_sensor_that_blipped_a_few_seconds_does_not_arm_it(self):
        coord = _reading_coordinator({WINDOW: ("unavailable", _NOW - timedelta(seconds=20))}, started_s_ago=3600.0)
        _engine, decision = self._run(coord)
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD

    def test_an_open_window_does_not_arm_it(self):
        coord = _reading_coordinator({WINDOW: ("on", _NOW - timedelta(seconds=300))}, started_s_ago=3600.0)
        _engine, decision = self._run(coord)
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD

    def test_it_agrees_with_the_enforcers_own_start_gate(self):
        for state, age in (("unavailable", 300.0), ("unavailable", 20.0), ("on", 300.0), ("off", 300.0)):
            coord = _reading_coordinator({WINDOW: (state, _NOW - timedelta(seconds=age))}, started_s_ago=3600.0)
            engine = _precool_engine(coord)
            with patch.object(coordinator_module.dt_util, "utcnow", return_value=_NOW):
                assert engine._whf_house_sealed_with_known_sensors() is engine._whf_start_blocked_by_sealed_house()


# ---------------------------------------------------------------------------
# Owner decision D4 (#1176): the restore after a sealed-house stop waits for the fan to read off
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 30, 21, 32, 0, tzinfo=UTC)
CLOSED = SensorReading(is_open=False, stable_for_s=3600.0)


class _Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t


@pytest.fixture
def clock():
    c = _Clock()
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", side_effect=c.now),
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
    ):
        yield c


def _fan_engine(*, fan_on: bool | None = True):
    """Real engine, WHF install, house sealed, HVAC suppressed under the fan (pre-fan mode "cool")."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    thermostat = MagicMock()
    thermostat.state = "off"
    thermostat.attributes = {"fan_modes": ["auto", "on"]}
    fan_state = MagicMock()
    fan_state.state = "on"
    hass.states = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: fan_state if eid == "fan.whf" else thermostat)
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.home",
        door_window_sensors=["binary_sensor.window"],
        notify_service="notify.notify",
        config={
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.whf",
        },
    )
    world = {"fan_on": fan_on}
    engine._get_fan_physical_state_callback = lambda: world["fan_on"]
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=world["fan_on"], fan_on_for_s=300.0, sensors=(CLOSED,)
    )
    engine._sensor_check_callback = lambda: False
    engine._pre_fan_hvac_mode = "cool"
    engine._fan_override_active = True
    engine._set_hvac_mode = AsyncMock()
    engine._reclassify_callback = MagicMock()
    return engine, world


def _enforce(engine, trigger="tick"):
    return asyncio.run(engine.enforce_whf_safety(trigger))


def _restored_cool(engine) -> bool:
    return any(c.args and c.args[0] == "cool" for c in engine._set_hvac_mode.await_args_list)


class TestRestoreWaitsForTheFanToReadOff:
    def test_fan_still_reading_on_right_after_the_stop_does_not_get_cooling_started_under_it(self, clock):
        engine, world = _fan_engine(fan_on=True)  # the off command is out, the fan has not stopped spinning

        assert _enforce(engine) is FanCommandResult.EXECUTED

        assert not _restored_cool(engine), "cooling was restored while the fan still read on (rule 2)"
        engine._reclassify_callback.assert_not_called()
        assert engine._pre_fan_hvac_mode == "cool", "the WHF released the thermostat while the fan was still on"
        assert engine._whf_safety_release_blocked() is True

    def test_once_the_fan_reads_off_the_thermostat_is_given_back_exactly_once(self, clock):
        engine, world = _fan_engine(fan_on=True)
        _enforce(engine)
        engine._reclassify_callback.assert_not_called()

        world["fan_on"] = False
        engine.request_whf_safety_check("fan_entity")  # the fan-state listener's trigger
        assert engine._whf_safety_queued is True, "a fan that now reads off never reached the release"
        engine._whf_safety_queued = False
        _enforce(engine, "fan_entity")

        engine._reclassify_callback.assert_called_once()
        assert _restored_cool(engine), "the pre-fan mode was not given back once the fan read off"
        assert engine._pre_fan_hvac_mode is None
        assert engine._whf_restore_pending is False
        _enforce(engine, "tick")
        engine._reclassify_callback.assert_called_once()  # not again
        assert len([c for c in engine._set_hvac_mode.await_args_list if c.args[0] == "cool"]) == 1

    def test_a_fan_that_reads_off_the_moment_it_is_stopped_is_released_at_once(self, clock):
        """Symmetric control: the physical reading is off immediately, so nothing waits."""
        engine, world = _fan_engine(fan_on=True)

        async def _turn_off(domain, service, *a, **k):
            if (domain, service) == ("fan", "turn_off"):
                world["fan_on"] = False

        engine.hass.services.async_call.side_effect = _turn_off
        assert _enforce(engine) is FanCommandResult.EXECUTED

        engine._reclassify_callback.assert_called_once()
        assert _restored_cool(engine)
        assert engine._pre_fan_hvac_mode is None
        assert engine._whf_restore_pending is False

    def test_a_command_only_fan_is_released_after_the_verify_window_not_before(self, clock):
        engine, world = _fan_engine(fan_on=None)  # no readable physical state

        assert _enforce(engine) is FanCommandResult.EXECUTED
        assert not _restored_cool(engine)
        engine._reclassify_callback.assert_not_called()
        assert engine._whf_safety_release_blocked() is True

        _enforce(engine, "tick")  # still inside the verify window
        engine._reclassify_callback.assert_not_called()

        _enforce(engine, "stop_verify")  # the 12 s verify has passed
        engine._reclassify_callback.assert_called_once()
        assert _restored_cool(engine)
        assert engine._pre_fan_hvac_mode is None

    def test_the_verify_check_is_never_coalesced_away(self, clock):
        engine, _world = _fan_engine(fan_on=None)
        _enforce(engine)
        engine._whf_safety_queued = True  # another check is already queued
        engine.request_whf_safety_check("stop_verify")
        assert engine.hass.async_create_task.called, "the verify that ends a command-only wait was dropped"

    def test_a_stop_that_does_not_take_keeps_the_hvac_held_until_it_does(self, clock):
        engine, world = _fan_engine(fan_on=True)
        _enforce(engine)
        engine._fan_override_active = True
        assert _enforce(engine, "stop_verify") is FanCommandResult.EXECUTED  # fan still on: stopped again
        engine._reclassify_callback.assert_not_called()
        assert not _restored_cool(engine)

        world["fan_on"] = False
        _enforce(engine, "fan_entity")
        engine._reclassify_callback.assert_called_once()
        assert _restored_cool(engine)

    def test_a_dark_sensor_that_last_read_open_gets_no_hvac_restore_after_the_sealed_stop(self, clock):
        """D1 x D4: dark > 60 s counts closed for the fan (so it stops), but the window it last saw open still
        holds the pause -- the fan's release must not write cooling back into that window."""
        engine, world = _fan_engine(fan_on=True)
        engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
            fan_on=world["fan_on"],
            fan_on_for_s=300.0,
            sensors=(SensorReading(is_open=False, unknown_for_s=120.0, stable_for_s=120.0),),
        )
        engine._sensor_hold_callback = lambda: True  # dark, last real reading open
        _enforce(engine)
        world["fan_on"] = False
        _enforce(engine, "fan_entity")

        assert not _restored_cool(engine), "cooling was written back into a window whose sensor last read open"
        assert engine._pre_fan_hvac_mode is None, "the fan's suppression was never released"
        engine._reclassify_callback.assert_called_once()  # the reclassify pauses for the held-open window

    @pytest.mark.parametrize("held_open", [True, False])
    def test_the_fan_off_reconcile_never_writes_cooling_into_a_window_held_open(self, clock, held_open):
        """D1 mirror: the reconcile that releases a stranded fan suppression (restart / backstop) restores the
        pre-fan mode only when no window is held open (dark after last reading open); control: it still does."""
        engine, world = _fan_engine(fan_on=False)
        engine._fan_override_active = False
        engine._sensor_hold_callback = lambda: held_open
        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=76.0, outdoor=70.0, thermostat_fan_running=False, any_sensor_open=False
            )
        )
        assert _restored_cool(engine) is (not held_open)
        assert engine._pre_fan_hvac_mode is None, "the stranded fan suppression was not released"

    def test_a_mutex_stop_is_unchanged_the_occupants_hvac_is_never_overwritten(self, clock):
        """Symmetric control: the other stop reason (HVAC turned on) neither restores nor waits."""
        engine, world = _fan_engine(fan_on=True)
        engine.hass.states.get = MagicMock(
            side_effect=lambda eid: MagicMock(state="on") if eid == "fan.whf" else MagicMock(state="heat")
        )
        engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
            fan_on=True, fan_on_for_s=300.0, sensors=(SensorReading(is_open=True, stable_for_s=3600.0),)
        )
        engine._occupant_hvac_on_at = T0 + timedelta(seconds=5)
        engine._pre_fan_hvac_mode = None
        engine._fan_override_active = True
        _enforce(engine)
        assert not _restored_cool(engine)
        assert engine._whf_restore_pending is False
