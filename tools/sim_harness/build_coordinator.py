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

    # 2. Import ClimateAdvisorCoordinator AFTER stubs are installed.
    #
    #    Issue #497 follow-up: some test files (test_occupancy.py) temporarily
    #    swap homeassistant.helpers.update_coordinator.DataUpdateCoordinator for
    #    a minimal stand-in — missing real methods like
    #    async_config_entry_first_refresh — to force-reimport the coordinator
    #    module for their own narrow purposes, and never restore it. Because
    #    Python binds a class's bases at class-statement time (not on later
    #    attribute lookup), once that's happened for this process,
    #    ClimateAdvisorCoordinator stays bound to the crippled stand-in for
    #    the rest of the session — install_ha_stubs() resetting the attribute
    #    above doesn't retroactively change it.
    #
    #    Deliberately NOT fixed by deleting/reimporting the coordinator module
    #    here: other test files (test_thermal_predictions.py) hold module-level
    #    references to functions from this module captured at collection time
    #    and patch it by string path at test-run time — forcing a second
    #    reimport mid-session makes those two resolve to different module
    #    objects and silently breaks their patches (found via full-suite run
    #    after trying that approach). Patch the specific missing method
    #    directly onto the class instead — additive only, doesn't touch
    #    sys.modules identity, so no other test's cached references shift.
    from custom_components.climate_advisor.coordinator import (  # noqa: PLC0415
        ClimateAdvisorCoordinator,
    )

    # coordinator.py's __init__ calls super().__init__(hass, _LOGGER, name=...,
    # update_interval=...) — that resolves via the base bound at class-statement
    # time, not a name lookup fixed up by patching individual methods. If the
    # base is the crippled stand-in, super().__init__() never sets self.data/
    # self.last_update_success either (confirmed: patching just the two async
    # methods alone still left self.data missing at runtime). __bases__
    # reassignment is layout-compatible here — both candidates are plain
    # single-inheritance-from-object classes with no __slots__ — so this fixes
    # __init__ and every other inherited method in one place instead of
    # patching methods one at a time as gaps are discovered.
    from tools.sim_harness.ha_stubs import _MockDataUpdateCoordinator  # noqa: PLC0415

    if not issubclass(ClimateAdvisorCoordinator, _MockDataUpdateCoordinator):
        ClimateAdvisorCoordinator.__bases__ = (_MockDataUpdateCoordinator,)

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

    # 7b. Issue #481 / Issue #666: coordinator-originated events (e.g.
    #     _emit_incident()'s incident_detected, occupancy_transition/
    #     rapid_override_after_automation from _detect_and_emit_incidents()) call
    #     self._emit_event() DIRECTLY — a bound coordinator method — while
    #     engine-originated events reach the identical coordinator._emit_event()
    #     only via automation_engine._emit_event_callback. In real production both
    #     paths funnel into the same method (coordinator.py wires
    #     automation_engine._emit_event_callback = self._emit_event), which is also
    #     the ONLY place _feed_lifecycle_fsms_from_event() is called — the real
    #     side effect that feeds the nat-vent/door-window/override-grace shadow
    #     FSMs their event-driven transitions.
    #
    #     Issue #666: this used to point automation_engine._emit_event_callback at
    #     a bare local function that only appended to the flat scenario event_log,
    #     bypassing coordinator._emit_event (and therefore
    #     _feed_lifecycle_fsms_from_event()) entirely for every engine-originated
    #     event. The accompanying comment claimed this was "purely additive" and
    #     "does not change what any existing event type is captured as" — false:
    #     it silently dropped the FSM-feed side effect for the whole coordinator-
    #     level Tier A suite, which is exactly why #647 (a real fix to this event-
    #     feed wiring) could ship with green tests and still leave the live bug
    #     class (#613/#633/#637/#647) unresolved — no test in this harness could
    #     ever have caught a regression there. Fix: wrap the REAL
    #     coordinator._emit_event once, point automation_engine._emit_event_callback
    #     at the wrapped version (mirroring production's own wiring exactly), and
    #     have the wrapper append to the flat event_log as an additional step, not
    #     a replacement one.
    _coordinator_emit_event = coordinator._emit_event

    def _emit_event_and_capture(event_type: str, data: dict) -> None:
        _coordinator_emit_event(event_type, data)
        _emit_event(event_type, data)

    coordinator._emit_event = _emit_event_and_capture
    coordinator.automation_engine._emit_event_callback = _emit_event_and_capture

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
        # Settle any fire-and-forget hass.async_create_task() calls AND any
        # heap entries (async_call_later) they schedule, before leaving the
        # patched context — see run_production.py's matching fix for why a
        # bare _drain_tasks() call is insufficient (Issue #476).
        scheduler.advance_to(scheduler.now())

    if skip_startup_coalesce:
        coordinator._startup_coalesce_active = False

    return coordinator, fake_hass, scheduler, event_log


def build_headless_multi_zone(
    zone_count: int = 2,
    *,
    configs: list[dict[str, Any]] | None = None,
    start_time: datetime | None = None,
    config_dir: str | None = None,
) -> tuple[dict[str, Any], FakeHass, FakeScheduler]:
    """Build ``zone_count`` real config entries against ONE shared FakeHass.

    Issue #796 (docs/multi-zone-spec.md, "Testing Without Multi-Zone
    Hardware"): unlike ``build_headless_coordinator()`` above, which
    constructs ``ClimateAdvisorCoordinator`` directly and never touches
    ``async_setup_entry()``/``async_unload_entry()`` at all, this function
    drives the REAL ``custom_components.climate_advisor.async_setup_entry()``
    once per zone against a shared ``hass`` — exercising service
    registration, panel registration, and ``hass.data[DOMAIN]`` population
    exactly as production does. This is the only harness path that can
    regression-test Gaps 5/6/8/9 (all of which live inside
    ``async_setup_entry()``/``async_unload_entry()``, code the single-zone
    harness never executes). ``build_headless_coordinator()`` itself is left
    untouched — single-zone tests keep the fast, direct-construction path.

    Args:
        zone_count: Number of zones (config entries) to set up. Ignored if
            ``configs`` is given (its length wins).
        configs: Optional list of per-zone config overrides, one dict per
            zone, each merged over ``_DEFAULT_CONFIG`` the same way
            ``build_headless_coordinator`` does. Each dict may set
            ``climate_entity``, ``zone_title``, and any other runtime config
            key. When omitted, ``zone_count`` zones are generated with
            distinct synthetic entity ids (``climate.zone_a_thermostat``,
            ``climate.zone_b_thermostat``, ...) and titles ("Zone A", "Zone
            B", ...).
        start_time: Shared virtual clock start time for all zones.
        config_dir: Shared StatePersistence/ChartStateLog/LearningEngine
            directory. Defaults to a fresh ``tempfile.mkdtemp()`` — **shared
            across zones on purpose**, since each zone's persistence
            filenames are expected to be entry-scoped in production (that
            expectation is exactly what a ``teardown_cleanup``/
            ``cross_zone_isolation`` scenario would catch if violated); pass
            distinct directories explicitly if a scenario wants to rule out
            file-path collision as a variable.

    Returns:
        ``(zones, fake_hass, scheduler)`` where ``zones`` is
        ``{zone_label: {"coordinator": ..., "entry": ConfigEntry, "climate_entity": str}}``
        in setup order. Each coordinator's own ``coordinator._event_log`` is
        the per-zone event log (Issue #236 single-engine doctrine — read it
        directly, there is no separate flat log for this harness function).
    """
    install_ha_stubs()

    from custom_components.climate_advisor import async_setup_entry  # noqa: PLC0415
    from custom_components.climate_advisor.const import DOMAIN  # noqa: PLC0415
    from custom_components.climate_advisor.coordinator import (  # noqa: PLC0415
        ClimateAdvisorCoordinator,
    )
    from tools.sim_harness.ha_stubs import ConfigEntry, _MockDataUpdateCoordinator  # noqa: PLC0415

    # Same __bases__ guard as build_headless_coordinator() — see that
    # function's comment for the full explanation (Issue #497 follow-up).
    if not issubclass(ClimateAdvisorCoordinator, _MockDataUpdateCoordinator):
        ClimateAdvisorCoordinator.__bases__ = (_MockDataUpdateCoordinator,)

    if configs is None:
        _labels = [chr(ord("a") + i) for i in range(zone_count)]
        configs = [
            {
                "climate_entity": f"climate.zone_{label}_thermostat",
                "zone_title": f"Zone {label.upper()}",
            }
            for label in _labels
        ]

    if start_time is None:
        start_time = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
    scheduler = FakeScheduler(start=start_time)
    fake_hass = FakeHass(clock_fn=scheduler.now)
    fake_hass.set_scheduler(scheduler)
    fake_hass.config.config_dir = config_dir or tempfile.mkdtemp(prefix="ca_sim_multi_zone_")

    zones: dict[str, Any] = {}

    with scheduler.installed():
        for index, zone_config in enumerate(configs):
            zone_config = dict(zone_config)
            climate_entity = zone_config.pop("climate_entity", f"climate.zone_{index}_thermostat")
            zone_title = zone_config.pop("zone_title", f"Zone {index}")
            zone_label = zone_config.pop("zone_label", None) or f"zone_{index}"

            merged_config: dict[str, Any] = {**_DEFAULT_CONFIG, **zone_config}
            merged_config["climate_entity"] = climate_entity

            # Seed this zone's thermostat state on the SHARED fake_hass —
            # mirrors build_headless_coordinator's defaults (dual-setpoint,
            # the most capable real-world unit).
            _default_hvac_modes = merged_config.get(
                "thermostat_hvac_modes",
                ["off", "heat", "cool", "heat_cool"],
            )
            _default_features = int(merged_config.get("thermostat_supported_features", 2))
            fake_hass.states.set(
                climate_entity,
                FakeState(
                    state="off",
                    attributes={
                        "fan_mode": "auto",
                        "hvac_modes": _default_hvac_modes,
                        "supported_features": _default_features,
                    },
                ),
            )

            entry = ConfigEntry(entry_id=f"{zone_label}_entry", data=merged_config, title=zone_title)
            # Real HA registers the entry in its config-entries registry
            # BEFORE calling async_setup_entry() — mirror that ordering so
            # async_entries(DOMAIN) (read by repairs.py and diagnostics.py's
            # entry_setup_order field) sees every already-set-up zone plus
            # itself, matching production's actual sequencing.
            fake_hass.config_entries.register_entry(entry)

            run_coro(async_setup_entry(fake_hass, entry))
            # Settle any fire-and-forget hass.async_create_task() calls /
            # scheduled callbacks queued during this zone's setup before
            # moving on to the next zone — same reasoning as
            # build_headless_coordinator's post-startup scheduler.advance_to.
            scheduler.advance_to(scheduler.now())

            coordinator = fake_hass.data[DOMAIN][entry.entry_id]
            zones[zone_label] = {
                "coordinator": coordinator,
                "entry": entry,
                "climate_entity": climate_entity,
            }

    return zones, fake_hass, scheduler
