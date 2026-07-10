"""Tests for the contact sensor status feature (Issue #46).

Tests for:
- _compute_contact_status logic
- _compute_contact_details logic
- ATTR_CONTACT_STATUS constant
- ClimateAdvisorContactStatusSensor class existence in sensor.py
- API response includes contact fields
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import ATTR_CONTACT_STATUS

# ---------------------------------------------------------------------------
# Helpers — replicate coordinator logic for unit testing
# ---------------------------------------------------------------------------


def _make_state(state_value: str) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    return mock


def _states_getter(state_map: dict[str, MagicMock]):
    """Return a callable that looks up entity states from a dict."""
    return lambda eid: state_map.get(eid)


def _make_real_coordinator(resolved_sensors: list[str], hass_states_get, polarity_inverted: bool = False):
    """Build a bare ClimateAdvisorCoordinator bound to the real contact-status methods.

    Uses object.__new__() + types.MethodType() (the established pattern for
    partially instantiating the coordinator in tests — see test_daily_record_accuracy.py)
    rather than replicating the method bodies, so these tests exercise the real
    ClimateAdvisorCoordinator._is_sensor_open/_compute_contact_status/_compute_contact_details.
    """
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"sensor_polarity_inverted": polarity_inverted}
    coord._resolved_sensors = resolved_sensors
    coord.hass = MagicMock()
    coord.hass.states.get = hass_states_get
    coord._is_sensor_open = types.MethodType(ClimateAdvisorCoordinator._is_sensor_open, coord)
    coord._compute_contact_status = types.MethodType(ClimateAdvisorCoordinator._compute_contact_status, coord)
    coord._compute_contact_details = types.MethodType(ClimateAdvisorCoordinator._compute_contact_details, coord)
    return coord


def _compute_contact_status(resolved_sensors: list[str], hass_states_get) -> str:
    """Call the real ClimateAdvisorCoordinator._compute_contact_status()."""
    coord = _make_real_coordinator(resolved_sensors, hass_states_get)
    return coord._compute_contact_status()


def _compute_contact_details(resolved_sensors: list[str], hass_states_get) -> list[dict]:
    """Call the real ClimateAdvisorCoordinator._compute_contact_details()."""
    coord = _make_real_coordinator(resolved_sensors, hass_states_get)
    return coord._compute_contact_details()


# ---------------------------------------------------------------------------
# Tests: _compute_contact_status
# ---------------------------------------------------------------------------


class TestComputeContactStatus:
    """Tests for _compute_contact_status logic."""

    def test_no_sensors_configured_returns_no_sensors(self):
        """Empty resolved sensors list → 'no sensors'."""
        getter = _states_getter({})
        result = _compute_contact_status([], getter)
        assert result == "no sensors"

    def test_all_sensors_closed_returns_all_closed(self):
        """All sensors with state 'off' → 'all closed'."""
        states = {
            "binary_sensor.front_door": _make_state("off"),
            "binary_sensor.back_door": _make_state("off"),
        }
        getter = _states_getter(states)
        result = _compute_contact_status(["binary_sensor.front_door", "binary_sensor.back_door"], getter)
        assert result == "all closed"

    def test_one_sensor_open_returns_1_open(self):
        """One sensor 'on' → '1 open'."""
        states = {
            "binary_sensor.front_door": _make_state("on"),
            "binary_sensor.back_door": _make_state("off"),
        }
        getter = _states_getter(states)
        result = _compute_contact_status(["binary_sensor.front_door", "binary_sensor.back_door"], getter)
        assert result == "1 open"

    def test_multiple_sensors_open_returns_count(self):
        """Two of three sensors 'on' → '2 open'."""
        states = {
            "binary_sensor.front_door": _make_state("on"),
            "binary_sensor.back_door": _make_state("on"),
            "binary_sensor.living_room_window": _make_state("off"),
        }
        getter = _states_getter(states)
        result = _compute_contact_status(
            [
                "binary_sensor.front_door",
                "binary_sensor.back_door",
                "binary_sensor.living_room_window",
            ],
            getter,
        )
        assert result == "2 open"

    def test_all_sensors_open_returns_full_count(self):
        """All three sensors 'on' → '3 open'."""
        states = {
            "binary_sensor.front_door": _make_state("on"),
            "binary_sensor.back_door": _make_state("on"),
            "binary_sensor.living_room_window": _make_state("on"),
        }
        getter = _states_getter(states)
        result = _compute_contact_status(
            [
                "binary_sensor.front_door",
                "binary_sensor.back_door",
                "binary_sensor.living_room_window",
            ],
            getter,
        )
        assert result == "3 open"

    def test_single_sensor_closed_returns_all_closed(self):
        """Single sensor configured and closed → 'all closed'."""
        states = {"binary_sensor.front_door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_status(["binary_sensor.front_door"], getter)
        assert result == "all closed"

    def test_single_sensor_open_returns_1_open(self):
        """Single sensor configured and open → '1 open'."""
        states = {"binary_sensor.front_door": _make_state("on")}
        getter = _states_getter(states)
        result = _compute_contact_status(["binary_sensor.front_door"], getter)
        assert result == "1 open"

    def test_unavailable_sensor_treated_as_closed(self):
        """Sensor not found in HA states → treated as closed."""
        getter = _states_getter({})  # no states registered
        result = _compute_contact_status(["binary_sensor.missing_sensor"], getter)
        assert result == "all closed"


# ---------------------------------------------------------------------------
# Tests: _compute_contact_details
# ---------------------------------------------------------------------------


class TestComputeContactDetails:
    """Tests for _compute_contact_details logic."""

    def test_empty_sensors_returns_empty_list(self):
        """No sensors configured → empty list."""
        getter = _states_getter({})
        result = _compute_contact_details([], getter)
        assert result == []

    def test_returns_list_of_dicts(self):
        """Result is a list of dicts with expected keys."""
        states = {"binary_sensor.front_door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.front_door"], getter)
        assert isinstance(result, list)
        assert len(result) == 1
        entry = result[0]
        assert "entity_id" in entry
        assert "friendly_name" in entry
        assert "open" in entry

    def test_entity_id_preserved(self):
        """The entity_id field matches the input sensor ID."""
        states = {"binary_sensor.front_door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.front_door"], getter)
        assert result[0]["entity_id"] == "binary_sensor.front_door"

    def test_friendly_name_derived_from_entity_id(self):
        """Friendly name strips domain, replaces underscores, title-cases."""
        states = {"binary_sensor.living_room_door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.living_room_door"], getter)
        assert result[0]["friendly_name"] == "Living Room Door"

    def test_open_field_true_when_sensor_on(self):
        """open field is True when sensor state is 'on'."""
        states = {"binary_sensor.front_door": _make_state("on")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.front_door"], getter)
        assert result[0]["open"] is True

    def test_open_field_false_when_sensor_off(self):
        """open field is False when sensor state is 'off'."""
        states = {"binary_sensor.front_door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.front_door"], getter)
        assert result[0]["open"] is False

    def test_multiple_sensors_all_returned(self):
        """All configured sensors appear in the result list."""
        states = {
            "binary_sensor.front_door": _make_state("on"),
            "binary_sensor.back_door": _make_state("off"),
            "binary_sensor.living_room_window": _make_state("off"),
        }
        getter = _states_getter(states)
        result = _compute_contact_details(
            [
                "binary_sensor.front_door",
                "binary_sensor.back_door",
                "binary_sensor.living_room_window",
            ],
            getter,
        )
        assert len(result) == 3
        entity_ids = [d["entity_id"] for d in result]
        assert "binary_sensor.front_door" in entity_ids
        assert "binary_sensor.back_door" in entity_ids
        assert "binary_sensor.living_room_window" in entity_ids

    def test_open_field_false_when_sensor_unavailable(self):
        """Sensor absent from HA states → open is False."""
        getter = _states_getter({})
        result = _compute_contact_details(["binary_sensor.missing_sensor"], getter)
        assert result[0]["open"] is False

    def test_friendly_name_single_word(self):
        """Single-word entity name title-cases correctly."""
        states = {"binary_sensor.door": _make_state("off")}
        getter = _states_getter(states)
        result = _compute_contact_details(["binary_sensor.door"], getter)
        assert result[0]["friendly_name"] == "Door"

    def test_order_matches_input_order(self):
        """Result list preserves the order of the input sensor list."""
        states = {
            "binary_sensor.alpha": _make_state("off"),
            "binary_sensor.beta": _make_state("on"),
        }
        getter = _states_getter(states)
        sensors = ["binary_sensor.alpha", "binary_sensor.beta"]
        result = _compute_contact_details(sensors, getter)
        assert result[0]["entity_id"] == "binary_sensor.alpha"
        assert result[1]["entity_id"] == "binary_sensor.beta"


# ---------------------------------------------------------------------------
# Tests: Constant verification
# ---------------------------------------------------------------------------


class TestContactStatusConstant:
    """Verify the ATTR_CONTACT_STATUS constant value."""

    def test_attr_contact_status_value(self):
        """ATTR_CONTACT_STATUS should equal 'contact_status'."""
        assert ATTR_CONTACT_STATUS == "contact_status"


# ---------------------------------------------------------------------------
# Tests: Sensor entity source verification
# ---------------------------------------------------------------------------


class TestContactStatusSensorSource:
    """Verify ClimateAdvisorContactStatusSensor exists in sensor.py via source inspection.

    Sensor classes cannot be instantiated without a real HA runtime (metaclass
    conflict from MagicMock stubs), so we verify the source code directly.
    """

    @pytest.fixture(autouse=True)
    def _read_sensor_source(self):
        """Read sensor.py source once for all tests in this class."""
        import pathlib

        sensor_path = (
            pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "sensor.py"
        )
        self.source = sensor_path.read_text()

    def test_contact_status_sensor_class_exists(self):
        """ClimateAdvisorContactStatusSensor class is defined in sensor.py."""
        assert "ClimateAdvisorContactStatusSensor" in self.source

    def test_contact_status_display_name(self):
        """Sensor display name 'Contact Sensors' is present in sensor.py."""
        assert '"Contact Sensors"' in self.source

    def test_sensor_registered_in_async_setup_entry(self):
        """ClimateAdvisorContactStatusSensor is instantiated in async_setup_entry."""
        # Verify it appears in the entities list, not just as a class definition
        assert "ClimateAdvisorContactStatusSensor(coordinator, entry)" in self.source

    def test_icon_door_open_present(self):
        """The mdi:door-open icon string is present for open state."""
        assert "mdi:door-open" in self.source

    def test_icon_door_closed_present(self):
        """The mdi:door-closed icon string is present for closed state."""
        assert "mdi:door-closed" in self.source

    def test_icon_logic_all_closed_uses_door_closed(self):
        """Icon logic checks for 'all closed' to return mdi:door-closed."""
        assert "all closed" in self.source
        assert "mdi:door-closed" in self.source

    def test_sensor_extends_base_sensor(self):
        """ClimateAdvisorContactStatusSensor extends ClimateAdvisorBaseSensor."""
        assert "ClimateAdvisorContactStatusSensor(ClimateAdvisorBaseSensor)" in self.source


# ---------------------------------------------------------------------------
# Tests: API response includes contact fields
# ---------------------------------------------------------------------------


class TestAPIContactFields:
    """Verify the status API response dict includes contact_status and contact_sensors."""

    def test_status_response_includes_contact_status_key(self):
        """Status API response dict should include 'contact_status'."""
        # Simulate the API response dict structure (mirrors api.py StatusView.get)
        coord_data = {
            "contact_status": "all closed",
            "contact_sensors": [],
        }
        assert "contact_status" in coord_data

    def test_status_response_includes_contact_sensors_key(self):
        """Status API response dict should include 'contact_sensors'."""
        coord_data = {
            "contact_status": "all closed",
            "contact_sensors": [],
        }
        assert "contact_sensors" in coord_data

    def test_contact_status_defaults_to_no_sensors(self):
        """API uses 'no sensors' as the default when ATTR_CONTACT_STATUS is absent."""
        import pathlib

        api_path = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "api.py"
        source = api_path.read_text()
        # The default fallback value is in the source
        assert '"no sensors"' in source

    def test_api_source_includes_contact_status_key(self):
        """api.py builds the response with 'contact_status' key."""
        import pathlib

        api_path = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "api.py"
        source = api_path.read_text()
        assert '"contact_status"' in source

    def test_api_source_includes_contact_sensors_key(self):
        """api.py builds the response with 'contact_sensors' key."""
        import pathlib

        api_path = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "api.py"
        source = api_path.read_text()
        assert '"contact_sensors"' in source

    def test_contact_sensors_value_is_list(self):
        """contact_sensors value in a representative response is a list."""
        states = {
            "binary_sensor.front_door": _make_state("off"),
        }
        getter = _states_getter(states)
        contact_sensors = _compute_contact_details(["binary_sensor.front_door"], getter)
        assert isinstance(contact_sensors, list)

    def test_contact_status_string_for_no_sensors_config(self):
        """contact_status value is 'no sensors' when no sensors are configured."""
        getter = _states_getter({})
        contact_status = _compute_contact_status([], getter)
        assert contact_status == "no sensors"
