"""build_coordinator — construct a headless ClimateAdvisorCoordinator (Issue #474).

``build_headless_coordinator()`` is the coordinator-level counterpart to
``build_engine.build_headless_engine()``. It installs HA stubs, imports
``ClimateAdvisorCoordinator``, wires up ``FakeHass`` (with real state-change
dispatch — see ``fake_hass.py``) and ``FakeScheduler`` (with the
coordinator-namespace patches added in ``fake_scheduler.py``), and replicates
``__init__.py``'s exact startup sequence so the constructed coordinator is
production-equivalent, not a hand-approximation.

The coordinator constructs its own internal ``AutomationEngine`` — this
function does NOT also call ``build_headless_engine()``. A coordinator
scenario gets its engine via ``coordinator.automation_engine``.

Returned tuple: ``(coordinator, fake_hass, scheduler, event_log)``

  - ``coordinator`` — the live ``ClimateAdvisorCoordinator`` instance
  - ``fake_hass``   — ``FakeHass``; read ``fake_hass.action_log`` for service
                      calls, use ``fake_hass.states.async_set(...)`` to
                      inject a state change that reaches real coordinator
                      listeners (e.g. ``_async_thermostat_changed``)
  - ``scheduler``   — ``FakeScheduler``; call ``scheduler.advance_to/by(...)``
                      inside the ``scheduler.installed()`` context manager
  - ``event_log``   — ``list[tuple[str, dict, datetime]]``; appended by
                      ``coordinator.automation_engine._emit_event_callback``
"""

from __future__ import annotations

import contextlib
import tempfile
from datetime import UTC, datetime
from typing import Any

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_engine import _DEFAULT_CONFIG
from tools.sim_harness.fake_hass import FakeHass, FakeState
from tools.sim_harness.fake_scheduler import FakeScheduler
from tools.sim_harness.ha_stubs import install_ha_stubs


def build_headless_coordinator(
    config: dict[str, Any] | None = None,
    *,
    climate_entity: str = "climate.test_thermostat",
    climate_state: str = "off",
    climate_attributes: dict[str, Any] | None = None,
    start_time: datetime | None = None,
    config_dir: str | None = None,
    skip_startup_coalesce: bool = False,
) -> tuple[Any, FakeHass, FakeScheduler, list[tuple[str, dict, datetime | None]]]:
    """Build and return a headless ClimateAdvisorCoordinator.

    Args:
        config: Runtime config dict.  Merged over ``_DEFAULT_CONFIG`` (the
                same defaults ``build_headless_engine`` uses); pass only the
                keys you want to differ.
        climate_entity: Entity ID for the climate device.
        climate_state: Initial thermostat state string.
        climate_attributes: Initial thermostat attributes dict.
        start_time: Virtual clock start time.  Defaults to 2024-01-15 08:00 UTC.
        config_dir: Directory for StatePersistence/ChartStateLog/LearningEngine
                    file I/O.  Defaults to a fresh ``tempfile.mkdtemp()`` so
                    scenario runs never read stale state left by a prior run
                    (each coordinator gets an isolated directory, matching the
                    ``tmp_path`` fixture pattern already used by
                    ``test_occupancy.py`` etc.).
        skip_startup_coalesce: When True, clears ``_startup_coalesce_active``
            right after construction. **Default False is the honest default**
            — a freshly built coordinator genuinely has this 5-minute
            post-restart override-detection suppression window active, same
            as real production, and `_async_thermostat_changed` early-returns
            before reaching ANY override-detection guard while it's set. A
            scenario testing steady-state override detection (not
            startup/restart behavior itself) must set this True or every
            dispatched event will vacuously pass through the early-return
            without exercising the guard it's meant to test — this is
            EXACTLY the bug found and fixed for the #474 proving slice
            (`away_setpoint_change_not_override`). Scenarios that are
            specifically ABOUT startup/restart/coalescing behavior (e.g. a
            future migration of `grace_timer_expired_on_restart`) must leave
            this False to exercise the real suppression window.

    Returns:
        ``(coordinator, fake_hass, scheduler, event_log)``
    """
    # 1. Install HA stubs — idempotent, safe to call multiple times
    install_ha_stubs()

    # 2. Import ClimateAdvisorCoordinator AFTER stubs are installed
    from custom_components.climate_advisor.coordinator import (  # noqa: PLC0415
        ClimateAdvisorCoordinator,
    )

    # 3. Build merged config (same defaults as the engine harness)
    merged_config: dict[str, Any] = {**_DEFAULT_CONFIG}
    merged_config["climate_entity"] = climate_entity
    if config:
        merged_config.update(config)

    # 4. Virtual clock + FakeHass
    if start_time is None:
        start_time = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)

    scheduler = FakeScheduler(start=start_time)
    fake_hass = FakeHass(clock_fn=scheduler.now)
    fake_hass.set_scheduler(scheduler)

    # Isolated config_dir — see docstring. Not cleaned up automatically (matches
    # tempfile.mkdtemp's default contract); harness runs are short-lived processes.
    fake_hass.config.config_dir = config_dir or tempfile.mkdtemp(prefix="ca_sim_coordinator_")

    # 5. Seed initial climate entity state (mirrors build_engine's defaults —
    #    a dual-setpoint thermostat, the most capable real-world unit).
    _default_hvac_modes = merged_config.get(
        "thermostat_hvac_modes",
        ["off", "heat", "cool", "heat_cool"],
    )
    _default_features = int(
        merged_config.get("thermostat_supported_features", 2)  # TARGET_TEMP_RANGE
    )
    attrs = {
        "fan_mode": "auto",
        "hvac_modes": _default_hvac_modes,
        "supported_features": _default_features,
    }
    if climate_attributes:
        attrs.update(climate_attributes)
    fake_hass.states.set(climate_entity, FakeState(state=climate_state, attributes=attrs))

    # 6. Event log — shared list the engine's callback appends to (same shape
    #    as build_headless_engine's event_log).
    event_log: list[tuple[str, dict, datetime | None]] = []

    def _emit_event(event_type: str, payload: dict) -> None:
        ts: datetime | None = None
        with contextlib.suppress(Exception):
            ts = scheduler.now()
        event_log.append((event_type, payload, ts))

    # 7. Construct the real coordinator. __init__ never touches async_track_*/
    #    hass.bus (confirmed: already exercised against a bare MagicMock hass
    #    in test_occupancy.py, test_weather_bias.py, test_learning_toggle.py)
    #    — only hass.config.config_dir, set above.
    coordinator = ClimateAdvisorCoordinator(fake_hass, merged_config)

    # Wire the event log the same way build_headless_engine does — the
    # coordinator already wires its own callbacks onto automation_engine in
    # __init__, so overriding _emit_event_callback here replaces the
    # coordinator's own (which forwards to self._event_log, a ring buffer we
    # don't need for scenario assertions) with the flat scenario event_log.
    coordinator.automation_engine._emit_event_callback = _emit_event

    # 8. Replicate __init__.py's exact startup sequence (__init__.py:396-405):
    #      coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data))
    #      await coordinator.async_restore_state()
    #      await coordinator.async_setup()
    #      await coordinator.async_config_entry_first_refresh()
    #
    #    Critical: async_setup() calls async_track_time_change/_state_change_event/
    #    _time_interval/_point_in_time and async_call_later directly — these
    #    resolve to coordinator.py's *module-level* names, which are plain
    #    MagicMock attributes (from ha_stubs.py's mocked homeassistant.helpers.event
    #    module) unless scheduler.installed()'s patches are active. Registering
    #    listeners against an unpatched MagicMock would silently no-op — the
    #    listener would never reach FakeHass's registry, and dispatch fidelity
    #    (the entire point of this harness) would be lost. So the startup
    #    sequence must run inside its own installed() context. Event dispatch
    #    after construction (scenario events, scheduler.advance_to) needs its
    #    own separate `with scheduler.installed():` block from the caller —
    #    same two-phase pattern run_production.py already uses for the engine.
    with scheduler.installed():
        run_coro(coordinator.async_restore_state())
        run_coro(coordinator.async_setup())
        run_coro(coordinator.async_config_entry_first_refresh())
        # Drain any fire-and-forget hass.async_create_task() calls made during
        # startup (e.g. notifications) before leaving the patched context —
        # same reasoning as run_production.py's post-loop drain.
        scheduler._drain_tasks()

    if skip_startup_coalesce:
        coordinator._startup_coalesce_active = False

    return coordinator, fake_hass, scheduler, event_log
