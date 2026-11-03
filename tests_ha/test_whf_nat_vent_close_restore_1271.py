"""Real-HA test for Issue #1271: the AC stays off after whole-house-fan ventilation ends on a window close.

Occupant story: a warm evening, the window is open and Climate Advisor ventilates with the whole-house fan (AC held
Off). She closes the window; the fan stops. The AC never came back (78 F against a 72 F ceiling for 2+ hours with the
thermostat Off) because #1215 made the restore wait for the fan to read physically off, and the nat-vent exit asked
that question before its own ``turn_off`` had taken effect, then nothing was left to release the thermostat.

Both fan shapes: ``whf`` (one entity that follows the fan) and ``whf_dual`` with the production-like lag (a transmitter
entity that flips at once and a power reading ~15 s behind). Prove the test is not vacuous with
``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

QUIET_S = 6.0  # WHF_RESTORE_QUIET_S


class _OccupantOutcomeViolated(AssertionError):
    """The thermostat was not given back after the ventilation fan stopped (the #1271 symptom)."""


async def _seconds(house, n: int) -> None:
    for _ in range(int(n)):
        await house.advance(seconds=1)


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_closing_the_window_on_nat_vent_gives_the_ac_back_once_the_fan_stops(make_house, fan) -> None:
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",  # indoor stays put: the only exit is the window close the test causes
        start=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        initial_indoor_f=72.0,
        outdoor_f=65.0,
        high_f=70.0,
        low_f=55.0,
        initial_hvac_mode="cool",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    )
    if fan == "whf_dual":
        house.fan.enable_realistic_lag(remote_updates_command_entity=True)
    ae = house.coordinator.automation_engine
    try:
        for _ in range(36):
            await house.advance(seconds=10)  # past the 300 s start-up window
        await house.open("window")
        await _seconds(house, 90)
        for _ in range(35):
            await house.advance(seconds=10)  # past the 5-minute fan toggle floor: the close's stop must not be deferred
        assert ae._natural_vent_active and ae._fan_active, "precondition: Climate Advisor is ventilating"
        assert house.fan.is_on and ae._pre_fan_hvac_mode == "cool", "precondition: the AC is held for the fan"

        await house.close_contact("window")
        t0 = dt_util.utcnow()
        await _seconds(house, 90)

        assert not house.fan.is_on, "the fan never stopped after the window closed"
        off_at = next((t for t, on in house.fan.physical_log if not on and t >= t0), None)  # whf_dual only
        restores = [
            c
            for c in house.calls
            if c.origin.startswith("ca:")
            and c.domain == "climate"
            and c.service == "set_hvac_mode"
            and c.at >= t0
            and c.data.get("hvac_mode") == "cool"
        ]
        if not restores or ae._whf_owns_hvac():
            raise _OccupantOutcomeViolated(
                "the window was closed, the whole-house fan stopped, and the AC never came back "
                f"(restores={restores!r}, whf_owns_hvac={ae._whf_owns_hvac()}, "
                f"restore_pending={ae._whf_restore_pending}, pre_fan_mode={ae._pre_fan_hvac_mode})"
            )
        if off_at is not None:
            waited = (restores[0].at - off_at).total_seconds()
            assert waited >= QUIET_S - 1.0, (
                f"the AC came back {waited:.1f} s after the fan read off (quiet {QUIET_S} s)"
            )
        assert not [
            c
            for c in house.calls
            if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_on" and c.at >= t0
        ], "the fan was restarted"
    finally:
        house.fan.close()
        await house.open("window")
        await _seconds(house, 20)


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_a_house_left_stranded_by_the_old_release_recovers_after_the_deploy_restart(make_house, fan) -> None:
    """Recovery on deploy: a zone already stranded by 0.8.29-0.8.45 gets its AC back after the upgrade's restart.

    Occupant story: the window was closed hours ago, the fan is off, the house is sealed, and the thermostat sits Off
    because ``_pre_fan_hvac_mode`` (persisted) still says the fan owns it. Installing the fix restarts HA. The
    restart's fan reconcile must see the fan off and give the AC back (``_whf_restore_pending`` is not persisted, so
    nothing defers it), not wait for a fan session that no longer exists.
    """
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",
        start=datetime(2026, 7, 15, 21, 0, tzinfo=UTC),
        initial_indoor_f=78.0,
        outdoor_f=70.0,
        high_f=90.0,
        low_f=60.0,
        initial_hvac_mode="off",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    )
    if fan == "whf_dual":
        house.fan.enable_realistic_lag(remote_updates_command_entity=True)
    try:
        ae = house.coordinator.automation_engine
        assert not house.fan.is_on and not ae._fan_active and not ae._natural_vent_active
        ae._pre_fan_hvac_mode = "cool"  # the stranded snapshot the old release left behind (persisted)
        await ae._set_hvac_mode("off", reason="WHF suppression (the old session's own off)")  # CA's write, not hers
        await _seconds(house, 5)
        assert ae._whf_owns_hvac() and house.thermostat.mode == "off", "precondition: the stranded state"

        t0 = dt_util.utcnow()
        await house.restart(settle=True)
        await _seconds(house, 30)

        ae = house.coordinator.automation_engine
        restores = [
            c
            for c in house.calls
            if c.origin.startswith("ca:")
            and c.domain == "climate"
            and c.service == "set_hvac_mode"
            and c.at >= t0
            and c.data.get("hvac_mode") == "cool"
        ]
        assert ae._whf_owns_hvac() is False and ae._pre_fan_hvac_mode is None, (
            f"still stranded after the restart: pre_fan_mode={ae._pre_fan_hvac_mode}"
        )
        assert restores and house.thermostat.mode == "cool", (
            f"the AC was not given back after the restart (mode={house.thermostat.mode}, restores={restores!r})"
        )
        assert not house.fan.is_on
    finally:
        house.fan.close()
