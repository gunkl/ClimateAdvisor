"""Issue #1209: a brief thermostat glitch that returns to the mode Climate Advisor set is not a manual override.

Occupant framing: on a warm or mild day the day label is "off" while Climate Advisor runs cool (or heat) on the comfort
band. If the thermostat blips to another mode and is back on Climate Advisor's own mode before the confirmation window
ends, nobody changed anything. Before the fix the expiry compared the live mode with the "off" label, saw cool != off,
confirmed a manual override and froze comfort management for the whole manual-change pause (1 h) -- on the very mode
Climate Advisor itself had chosen.

Real ``AutomationEngine`` (helpers shared with tests/test_override_off_mild_day.py), warm and mild days, both fan modes
(``whole_house_fan`` and ``hvac_fan``; ``both`` is unsupported). Controls pin what must not change: a glitch that
stays, an occupant Off (#1159), a setpoint-only change, and the hot-day blip that already self-resolved.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState
from tests.test_override_off_mild_day import _mode, _Scene
from tests.test_protect_manual_change import timers  # noqa: F401  (pytest fixture, re-exported)

_FAN_MODES = [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC]
# (day, indoor, CA's first write, glitch mode)
_WARM_AND_MILD = [
    pytest.param("warm", 72.0, "cool", "heat", id="warm-ca-cool-glitch-heat"),
    pytest.param("mild", 72.0, "cool", "heat", id="mild-ca-cool-glitch-heat"),
    pytest.param("mild", 64.0, "heat", "cool", id="mild-ca-heat-glitch-cool"),
    pytest.param("warm", 72.0, "cool", "off", id="warm-ca-cool-glitch-off"),
]


def _scene(fan_mode: str, day: str, indoor: float, ca_first: str) -> _Scene:
    from custom_components.climate_advisor.const import CONF_FAN_MODE

    s = _Scene(day=day, indoor=indoor, ca_first=ca_first)
    s.engine.config[CONF_FAN_MODE] = fan_mode
    return s


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(("day", "indoor", "ca_first", "glitch"), _WARM_AND_MILD)
def test_glitch_that_returns_to_the_commanded_mode_self_resolves(fan_mode, day, indoor, ca_first, glitch):
    s = _scene(fan_mode, day, indoor, ca_first)
    s.occupant_sets(glitch, old_mode=ca_first)
    s.engine._test_state.state = ca_first  # back on Climate Advisor's own mode inside the window
    s.confirm_elapses()

    assert "override_self_resolved" in s.events
    assert "override_confirmed" not in s.events
    assert not s.engine._manual_override_active
    assert not s.engine._grace_active, "no 1 h manual-change pause for a blip nobody made"
    # the override/grace FSM (authoritative for grace) takes the same path as the engine's expiry closure
    assert s.engine.override_grace_lifecycle_state == (OverrideConfirmState.IDLE, GraceState.NONE)
    assert _mode(s.engine) == ca_first


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(("day", "indoor", "ca_first", "glitch"), _WARM_AND_MILD)
def test_control_glitch_that_stays_is_confirmed(fan_mode, day, indoor, ca_first, glitch):
    """A change still in force at expiry is the occupant's (heat/cool/off), warm or mild day alike (#1159 for off)."""
    s = _scene(fan_mode, day, indoor, ca_first)
    s.occupant_sets(glitch, old_mode=ca_first)
    s.confirm_elapses()

    assert "override_confirmed" in s.events
    assert "override_self_resolved" not in s.events
    assert s.engine._manual_override_active
    assert s.engine._manual_override_mode == glitch
    assert s.engine._grace_active


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
def test_control_hot_day_blip_still_self_resolves(fan_mode):
    s = _scene(fan_mode, "hot", 76.0, "cool")
    s.occupant_sets("heat", old_mode="cool")
    s.engine._test_state.state = "cool"
    s.confirm_elapses()
    assert "override_self_resolved" in s.events
    assert not s.engine._manual_override_active


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
def test_control_hot_day_return_to_the_label_not_the_commanded_mode_self_resolves(fan_mode):
    """CA last commanded heat; the day label is cool. A return to the label keeps self-resolving (unchanged)."""
    s = _scene(fan_mode, "hot", 76.0, "cool")
    s.engine._last_commanded_hvac_mode = "heat"
    s.occupant_sets("off", old_mode="heat")
    s.engine._test_state.state = "cool"
    s.confirm_elapses()
    assert "override_self_resolved" in s.events


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
def test_commanded_mode_is_read_live_at_expiry_not_at_detection(fan_mode):
    """C2: if Climate Advisor itself commands another mode inside the window, expiry judges against THAT command."""
    s = _scene(fan_mode, "warm", 72.0, "cool")
    s.occupant_sets("heat", old_mode="cool")
    s.engine._last_commanded_hvac_mode = "heat"  # CA's own newer command (e.g. its band switched to heat)
    s.engine._test_state.state = "heat"
    s.confirm_elapses()
    assert "override_self_resolved" in s.events
    assert not s.engine._manual_override_active


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
def test_fsm_input_builder_hands_the_live_commanded_mode_to_the_override_fsm(fan_mode):
    """The FSM must judge the expiry on the same commanded mode as the engine's closure (one source, read live)."""
    s = _scene(fan_mode, "warm", 72.0, "cool")
    inputs = s.engine._build_override_grace_fsm_inputs()
    assert inputs.commanded_mode == "cool"
    s.engine._last_commanded_hvac_mode = "heat"  # a newer CA command
    assert s.engine._build_override_grace_fsm_inputs().commanded_mode == "heat"


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
def test_fsm_input_builder_reads_the_commanded_mode_live(fan_mode):
    """The real builder hands the FSM CA's current commanded mode, and follows it when it changes before expiry."""
    s = _scene(fan_mode, "warm", 72.0, "cool")
    assert s.engine._last_commanded_hvac_mode == "cool"
    assert s.engine._build_override_grace_fsm_inputs().commanded_mode == "cool"
    s.engine._last_commanded_hvac_mode = "heat"
    assert s.engine._build_override_grace_fsm_inputs().commanded_mode == "heat"
    s.engine._last_commanded_hvac_mode = None
    assert s.engine._build_override_grace_fsm_inputs().commanded_mode is None


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(
    ("day", "indoor", "ca_first"),
    [
        pytest.param("warm", 72.0, "cool", id="warm-label-off"),
        pytest.param("hot", 76.0, "cool", id="hot-label-cool"),
    ],
)
def test_second_change_back_to_the_commanded_mode_after_a_confirmed_off(fan_mode, day, indoor, ca_first):
    """Symmetry: Off confirmed, then (inside its grace) the occupant goes back to CA's own mode.

    Modelled as production does it: the coordinator's Fix-D branch (a mode change during an override's grace that
    differs from the override mode) first supersedes the Off override (``_replace_thermostat_override``), then starts
    a new confirmation. At expiry the live mode equals CA's commanded mode, so it self-resolves and the leftover grace
    is cancelled: CA simply resumes its own mode, with no Off override left behind. Warm (new rule: live ==
    commanded) and hot (live == label, the pre-fix path) must agree.
    """
    from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

    s = _scene(fan_mode, day, indoor, ca_first)
    s.occupant_sets("off", old_mode=ca_first)
    s.confirm_elapses()
    assert s.engine._manual_override_active and s.engine._manual_override_mode == "off"

    with s._patch_all():
        s.engine._replace_thermostat_override(
            reason="new_override_during_grace", fsm_kind=OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, clear_fan=False
        )
    s.occupant_sets(ca_first, old_mode="off")
    s.confirm_elapses()

    assert s.events.count("override_confirmed") == 1
    assert "override_self_resolved" in s.events
    assert not s.engine._override_confirm_pending
    assert not s.engine._manual_override_active, "no Off override is left behind while the thermostat runs CA's mode"
    assert not s.engine._grace_active
    assert s.engine.override_grace_lifecycle_state == (OverrideConfirmState.IDLE, GraceState.NONE)
    assert _mode(s.engine) == ca_first


@pytest.mark.parametrize("fan_mode", _FAN_MODES)
@pytest.mark.parametrize(("day", "indoor"), [("hot", 70.0), ("mild", 76.0)], ids=["hot", "mild-label-off"])
def test_off_after_own_cool_in_a_ca_pause_is_kept_1159(timers, fan_mode, day, indoor):  # noqa: F811
    """#1159 guard (verification finding): an Off equal to CA's own commanded Off is still the occupant's decision.

    A nat-vent session ends with the window open: CA's exit writes Off (its last command). The occupant presses Cool
    in that pause (confirmed), then Off inside its grace; the coordinator detects that Off against their Cool override
    (Fix D). At expiry the live Off equals CA's commanded Off -- that must not self-resolve, or the override and grace
    vanish and the next refresh turns cooling back on over the occupant's Off.
    """
    from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE
    from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind
    from tests.test_protect_manual_change import _close_window, _engine
    from tests.test_restore_resolved_band import _run
    from tests.test_window_close_restore_1165_1157 import _mode_or_setpoint_writes

    e = _engine("cool", indoor=indoor, outdoor=62.0, setpoint=72.0, day=day, window_open=True)
    e.config.update({CONF_OVERRIDE_CONFIRM_PERIOD: 600, "manual_grace_seconds": 1800, CONF_FAN_MODE: fan_mode})
    if fan_mode == FAN_MODE_WHOLE_HOUSE:
        e.config[CONF_FAN_ENTITY] = "switch.whf"
    e._natural_vent_active = True
    e._pre_fan_hvac_mode = "cool"
    _run(e._exit_nat_vent(reason="indoor reached the comfort floor"))
    assert e._paused_by_door and e._test_state.state == "off" and e._ca_commanded_mode() == "off"

    e._test_state.state = "cool"  # the occupant's Cool in CA's pause
    _run(
        e.handle_manual_override_during_pause(
            old_mode="off", new_mode="cool", classification_mode=e._current_classification.hvac_mode
        )
    )
    timers.fire(600)
    assert e._manual_override_active and e._manual_override_mode == "cool"

    e._test_state.state = "off"  # then Off inside that grace (coordinator Fix-D branch)
    e._replace_thermostat_override(
        reason="new_override_during_grace", fsm_kind=OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, clear_fan=False
    )
    e.handle_manual_override(old_mode="cool", new_mode="off", classification_mode=e._current_classification.hvac_mode)
    timers.fire(600)

    assert e._manual_override_active and e._manual_override_mode == "off"
    assert e._grace_active and e._grace_protects_override
    e.hass.services.async_call.reset_mock()
    _close_window(e)
    _run(e.apply_classification(e._current_classification))
    assert _mode_or_setpoint_writes(e) == [], "CA must not turn HVAC back on over the occupant's Off"
    assert e._test_state.state == "off"
