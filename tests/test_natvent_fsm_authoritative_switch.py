"""Tests for Issue #594 Phase R, Step 4: the nat-vent FSM-authoritative
cutover switch's coordinator-level plumbing.

Binds the REAL ``ClimateAdvisorCoordinator.natvent_fsm_authoritative``/
``set_natvent_fsm_authoritative()`` (object.__new__() + types.MethodType(),
the established partial-instantiation pattern — see test_contact_status.py)
rather than re-implementing the toggle logic in the test body, per this
project's doctrine against mirror tests.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator


def _make_coordinator() -> ClimateAdvisorCoordinator:
    # object.__new__() preserves the real class, so the real `natvent_fsm_authoritative`
    # property (a class-level descriptor) already resolves without manual binding —
    # only the plain method needs explicit types.MethodType() binding.
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.automation_engine = MagicMock()
    coord.automation_engine._natvent_fsm_authoritative = False
    coord.set_natvent_fsm_authoritative = types.MethodType(
        ClimateAdvisorCoordinator.set_natvent_fsm_authoritative, coord
    )
    return coord


class TestNatVentFsmAuthoritativeToggle:
    def test_defaults_reflect_engine_flag(self):
        coord = _make_coordinator()
        assert coord.natvent_fsm_authoritative is False

    def test_enabling_sets_engine_flag(self):
        coord = _make_coordinator()
        coord.set_natvent_fsm_authoritative(True)
        assert coord.automation_engine._natvent_fsm_authoritative is True
        assert coord.natvent_fsm_authoritative is True

    def test_disabling_clears_engine_flag(self):
        coord = _make_coordinator()
        coord.set_natvent_fsm_authoritative(True)
        coord.set_natvent_fsm_authoritative(False)
        assert coord.automation_engine._natvent_fsm_authoritative is False
        assert coord.natvent_fsm_authoritative is False

    def test_not_persisted_no_save_state_call(self):
        """Deliberately does not call _async_save_state — see the method's own
        docstring: a restart must always come back up on the legacy path."""
        coord = _make_coordinator()
        coord.hass = MagicMock()
        coord.set_natvent_fsm_authoritative(True)
        coord.hass.async_create_task.assert_not_called()
