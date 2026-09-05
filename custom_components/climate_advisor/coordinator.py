"""Data coordinator for Climate Advisor.

The coordinator is the central brain. It runs on a schedule, pulls forecast
data, classifies the day, triggers automations, sends briefings, and feeds
data to the learning engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import math
from collections.abc import Callable, Container
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .ai_skills import AISkillRegistry
    from .claude_api import ClaudeAPIClient
    from .override_grace_fsm import OverrideGraceFsmEventKind

from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import log_capture
from .automation import (
    AutomationEngine,
    AutomationEngineCallbacks,
    _in_sleep_window,
    compute_bedtime_setback,
    compute_pre_cool_target,
    resolve_pre_cool_modifier,
    select_comfort_band,
)
from .briefing import generate_briefing
from .chart_log import ChartStateLog
from .classifier import DayClassification, ForecastSnapshot, classify_day
from .const import (
    _VENT_SPLIT_TYPES,
    ATTR_AI_STATUS,
    ATTR_AUTOMATION_STATUS,
    ATTR_BRIEFING,
    ATTR_BRIEFING_SHORT,
    ATTR_COMPLIANCE_SCORE,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_OVERRIDE_SINCE,
    ATTR_FAN_RUNNING,
    ATTR_FAN_RUNTIME,
    ATTR_FAN_STATUS,
    ATTR_FORECAST_HIGH,
    ATTR_FORECAST_HIGH_TOMORROW,
    ATTR_FORECAST_LOW,
    ATTR_FORECAST_LOW_TOMORROW,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_FAN_STATUS,
    ATTR_HVAC_RUNTIME_TODAY,
    ATTR_INDOOR_TEMP,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_OUTDOOR_TEMP,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    ATTR_WHF_STATUS,
    BRIEFING_NAT_VENT_CUTOFF_DRIFT_THRESHOLD_MINUTES,
    BRIEFING_TODAY_HIGH_DRIFT_THRESHOLD_F,
    CHART_LOG_MAX_DAYS,
    CONF_AI_API_KEY,
    CONF_AI_ENABLED,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_BRIEFING_NOTIFICATIONS_ENABLED,
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_FAN_REMOTE_ENTITY,
    CONF_FAN_STATE_ENTITY,
    CONF_FAN_STATE_FEEDBACK,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_NAT_VENT_HYSTERESIS_F,
    CONF_NATURAL_VENT_DELTA,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_SLEEP_HEAT,
    CONF_THRESHOLD_COOL,
    CONF_THRESHOLD_HOT,
    CONF_THRESHOLD_MILD,
    CONF_THRESHOLD_WARM,
    CONF_VACATION_TOGGLE,
    CONF_VACATION_TOGGLE_INVERT,
    CONF_WEATHER_BIAS,
    DAY_TYPE_COLD,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_NATURAL_VENT_DELTA,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    DEFAULT_SETBACK_HEAT,
    DEFAULT_THRESHOLD_COOL,
    DEFAULT_THRESHOLD_HOT,
    DEFAULT_THRESHOLD_MILD,
    DEFAULT_THRESHOLD_WARM,
    DOMAIN,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_TEMP_DELTA,
    EVENT_LOG_CAP,
    EVENT_LOG_MAX_AGE_HOURS,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    INVESTIGATION_REPORT_HISTORY_CAP,
    INVESTIGATION_REPORTS_FILE,
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_WEATHER_BIAS_APPLY_F,
    NAT_VENT_HYSTERESIS_F,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENT_FAN_DECAY,
    OBS_TYPE_VENT_WINDOW_DECAY,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_SETBACK_MINUTES,
    OCCUPANCY_VACATION,
    OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
    PRED_ARCHIVE_HORIZON_HOURS,
    REJECT_ABANDONED,
    REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY,
    REJECT_AC_NO_COOL_SETPOINTS,
    REJECT_AC_NO_SETPOINT_BREACH,
    REJECT_AC_SETPOINT_OUT_OF_RANGE,
    REJECT_AC_SETPOINT_UNSTABLE,
    REJECT_NO_INTERIOR_PEAK,
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_BOUNDS,
    REJECT_OLS_WRONG_SIGN,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_BLOCKS,
    REJECT_TOO_FEW_SAMPLES,
    REJECT_WINDOW_TOO_SHORT,
    REMOTE_BURST_WINDOW_SECONDS,
    REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    THERMAL_BUCKET_INTERP_HALF_F,
    THERMAL_CHART_LOG_PASSIVE_MIN_DT_F,
    THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES,
    THERMAL_CHART_LOG_VENT_MIN_MINUTES,
    THERMAL_COLD_BUCKET_LIMIT_F,
    THERMAL_DUAL_AGREE_REL,
    THERMAL_DUAL_OLS_GOOD,
    THERMAL_DUAL_OLS_OK,
    THERMAL_HVAC_MIN_DECAY_F,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
    THERMAL_MAX_ACTIVE_SAMPLES,
    THERMAL_MAX_OBS_SAMPLES,
    THERMAL_MAX_POST_HEAT_SAMPLES,
    THERMAL_MILD_BUCKET_LIMIT_F,
    THERMAL_MIN_DECAY_SAMPLES,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_MIN_R_SQUARED,
    THERMAL_PASSIVE_MIN_DELTA_F,
    THERMAL_PASSIVE_MIN_SAMPLES,
    THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
    THERMAL_POST_HEAT_TIMEOUT_MINUTES,
    THERMAL_ROLLING_MAX_WINDOW_MINUTES,
    THERMAL_ROLLING_MIN_DELTA_T_F,
    THERMAL_ROLLING_MIN_WINDOW_MINUTES,
    THERMAL_SOLAR_DAYTIME_END_H,
    THERMAL_SOLAR_DAYTIME_START_H,
    THERMAL_SOLAR_FACTOR_MIN_RANGE,
    THERMAL_SOLAR_MIN_RATE_F_PER_HR,
    THERMAL_SOLAR_MIN_SAMPLES,
    THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES,
    THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H,
    THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F,
    THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H,
    THERMAL_SOLAR_PHASE_ALPHA,
    THERMAL_SOLAR_PHASE_MIN_DT_F,
    THERMAL_SOLAR_PHASE_MIN_ENTRIES,
    THERMAL_SOLAR_PHASE_MIN_WINDOW_H,
    THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT,
    THERMAL_SOLAR_PHASE_OFFSET_MAX,
    THERMAL_SOLAR_PHASE_OFFSET_MIN,
    THERMAL_SOLAR_SAMPLE_INTERVAL_S,
    THERMAL_VENT_MIN_SAMPLES,
    THERMAL_VENT_MIN_SIGNAL_F,
    THERMAL_VENTILATED_MIN_DELTA_F,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    VACATION_SETBACK_EXTRA,
    VERSION,
)
from .entity_health import run_entity_health_sweep
from .fan_status import (
    is_ca_fan_running,
    parse_remote_speed_event,
    parse_remote_timer_event,
    resolve_untracked_fan_status,
)
from .indoor_temp import resolve_indoor_temp_f
from .invariant_watchdog import run_invariant_checks
from .learning import DailyRecord, LearningEngine, compute_k_passive_blocks, compute_k_passive_endpoint
from .nat_vent_cycling import compute_nat_vent_target
from .nat_vent_exit import NatVentExitInputs, NatVentExitReason, decide_nat_vent_exit
from .nat_vent_gate import NatVentGateInputs, decide_nat_vent_gate
from .nat_vent_plan import compute_nat_vent_plan
from .occupancy_priority import OccupancyPriorityInputs, decide_occupancy_priority
from .ode_ceiling_guard import OdeCeilingGuardInputs, OdeCeilingGuardOutcome, decide_ode_ceiling_guard
from .override_grace_lifecycle import GraceState, OverrideConfirmState, OverrideGraceLifecycleState
from .scheduler import COST_TAG_HIGH, Schedule, TOUPhase, resolve_active_schedules, resolve_tou_phase
from .state import StatePersistence
from .temperature import (
    convert_delta,
    find_temperature_crossing,
    format_temp,
    free_cooling_direction_ok,
    from_fahrenheit,
    to_fahrenheit,
)

_LOGGER = logging.getLogger(__name__)

# Degrees below comfort_heat at which outdoor temp is too cold to recommend opening windows.
# With default comfort_heat=70°F this means outdoor must be ≥ 55°F for windows to be recommended.
_WINDOWS_EXTREME_COLD_MARGIN = 15.0

# Maximum rejection events retained per obs_type in the in-memory rejection log.
# Matches the per-obs-type cap enforced by LearningState.rejection_log on load.
_REJECTION_LOG_CAP: int = 100

# Issue #805: how often to re-notify about an entity that is still missing/unavailable
# after the initial ok->missing transition notification. Long enough to never feel like
# spam (one message per day, not per 30-min cycle — the original bug's failure mode),
# short enough that a problem discovered "yesterday" surfaces again "today" if truly
# still unresolved rather than being silently forgotten after a single missed alert.
_ENTITY_HEALTH_REMINDER_SECONDS: float = 24 * 60 * 60

# Issue #625: short, Fan(WHF)-card-style cause labels for AutomationEngine._last_grace_trigger,
# shown on the Status card's grace branch instead of a free-text _last_action_reason sentence
# (which duplicated the Fan (WHF) card for fan-triggered grace periods, and was blank/stale
# for manual thermostat overrides, which never populate _last_action_reason at all). An
# unmapped/unknown trigger falls back to no cause segment rather than leaking a raw internal
# string like "sensor_closed_resume" onto the UI.
_GRACE_TRIGGER_LABELS: Final[dict[str, str]] = {
    "fan_manual_override": "WHF override",
    "fan_off": "WHF turned off",
    "physical_drift_correction": "fan drift correction",
    "override_confirmed": "thermostat override",
    "sensor_closed_resume": "door/window closed",
    "nat_vent_exit_resume": "nat-vent exit",
}

# Registered exceptions to the Status Card Ontology's "Next Automation must not
# contain time-of-day phrasing" rule (CLAUDE.md, Issue #527). The nat_vent_cutoff
# grant (Issue #534/#847-followup) is retired as of Issue #849: that candidate told
# the occupant to close/reopen windows, an action CA cannot execute (no window
# actuator) — the candidate itself was removed rather than reworded, so the
# exception it needed no longer applies. The mechanism stays available here for a
# future candidate that is a genuinely automation-executed action and needs a
# time-disambiguation exception — see tests/test_status_card_ontology.py, which
# structurally enforces that every clock-time-bearing candidate in
# _compute_next_automation_action() is either exception-tagged and listed here, or
# doesn't exist. Do not add an entry here without also adding the matching
# `# ontology-exception: <slug>` comment directly above the candidate, and vice versa
# — the test checks both directions.
_ONTOLOGY_TIME_EXCEPTIONS: Final[set[str]] = set()


@dataclass
class _PendingFanRemoteBurst:
    """Accumulates a single physical QuietCool remote interaction's speed/timer fields
    (Issue #519) so they flush as ONE decision instead of two — a speed confirmation and a
    timer confirmation from one interaction arrive as separate packets moments apart (see
    docs/remote-capture-protocol.md in gunkl/quietcool-house-fan).

    ``was_running_before`` is snapshotted exactly ONCE, when the burst opens (the first
    speed/timer event of a new interaction) — critically NOT re-read at flush time, since by
    then the fan has typically already turned on and a post-hoc read would answer "yes" for
    nearly every case, including genuine off->on overrides. See
    ``ClimateAdvisorCoordinator._flush_fan_remote_burst()`` for how this drives the
    override-vs-comfort-only classification.
    """

    speed: str | None = None
    timer_hours: float | None = None
    has_timer: bool = False
    was_running_before: bool | None = None


# Next User Action flavor text for occupancy modes where there's nothing for the
# occupant to do (Issue #527). Date-seeded rotation (see _pick_daily_line()) picks
# one line per calendar day so the dashboard doesn't flicker between refreshes.
_AWAY_ACTION_MESSAGES: tuple[str, ...] = (
    "You're away. The house is holding steady.",
    "Away mode: nothing needs you right now.",
    "No action needed. The thermostat has it handled.",
    "You're away — temperature's stable, no news is good news.",
    "Nothing to do. The house is behaving.",
    "You're away — the house says it's fine, promise.",
    "We've got the house-sitting covered.",
    "Away and comfortable. Go enjoy wherever you are.",
    "Nothing to do — even the thermostat's taking it easy.",
    "Away mode: the house is behaving better than usual.",
)
_VACATION_ACTION_MESSAGES: tuple[str, ...] = (
    "Vacation mode: deep setback engaged, nothing for you to do.",
    "On vacation. The thermostat is unbothered.",
    "Deep setback active — the house is coasting.",
    "Nothing needed here. Go be on vacation.",
    "Setback engaged. This message is the only work being done.",
    "Vacation mode — the house is on autopilot, saving you money while you're gone.",
    "Go have fun. The thermostat's got the boring part.",
    "On vacation — sit back, we're saving energy for you.",
    "Deep setback engaged. The house is basically hibernating politely.",
    "Vacation: lights out, temps relaxed, wallet happy.",
)


def _pick_daily_line(pool: tuple[str, ...], salt: str) -> str:
    """Deterministically pick one line from ``pool`` per calendar day.

    Stable across repeated calls within the same day (no flicker on the ~30-min
    update cycle); changes the next day. ``salt`` distinguishes independent pools
    (e.g. "away" vs "vacation") so they don't rotate in lockstep.
    """
    today = dt_util.now().date().isoformat()
    index = int(hashlib.sha256(f"{today}:{salt}".encode()).hexdigest(), 16) % len(pool)
    return pool[index]


def _prune_event_log(event_log: list[dict], now: datetime) -> list[dict]:
    """Evict entries older than EVENT_LOG_MAX_AGE_HOURS, then enforce EVENT_LOG_CAP
    as a memory-safety backstop (Issue #432).

    Single source of truth for both live emit (_emit_event) and restore-from-disk
    (async_restore_state) so the two paths can never disagree on retention.
    """
    cutoff = (now - timedelta(hours=EVENT_LOG_MAX_AGE_HOURS)).isoformat()
    pruned = [e for e in event_log if e.get("time", "") >= cutoff]
    if len(pruned) > EVENT_LOG_CAP:
        pruned = pruned[-EVENT_LOG_CAP:]
    return pruned


# Issue #757 Phase 6 Step 4: _DOOR_WINDOW_FSM_EVENT_KINDS/
# _DOOR_WINDOW_SYNC_RECONCILE_TRIGGER_METHODS/_DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES
# (Issue #637/#594 Phase R Step 1b) were removed here — they fed only the now-deleted
# door/window shadow-comparison axes (door_window_mirror_agrees/door_window_fsm_agrees)
# via _evaluate_door_window_fsm(), which had zero other consumers. "override_adopted"/
# "grace_expired" — the event-type strings the removed
# _DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES also fed — are still consumed by
# _OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP below for that FSM's own still-active
# GRACE_TIMER_EXPIRED concept.

# Issue #639/#643: which automation.py entry-point methods correspond to which
# override/grace FSM event kind. Of the 8 OverrideGraceFsmEventKind members, 3 are
# fed here via _feed_override_grace_fsm_on_detect() (added Issue #757 Phase 6 Step 8,
# replacing the removed dual-engine shell's _mirror_to_shadow() finally block, which
# used to derive the same lookup as an incidental side effect of replaying the call
# onto the now-deleted shadow engine). OVERRIDE_CONFIRM_EXPIRED/OVERRIDE_SUPERSEDED/
# OVERRIDE_CANCELLED are fed separately by _feed_override_grace_fsm_cancelled() and
# the ``_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP``-driven exit paths below.
# GRACE_TIMER_EXPIRED is fed the same way (see that map).
_OVERRIDE_GRACE_FSM_EVENT_KINDS: dict[str, str] = {
    "handle_manual_override_during_pause": "manual_override_during_pause",
    "resume_from_pause": "dashboard_resume",
    # Issue #661: was "override_detected" — handle_fan_manual_override() never
    # routes through start_override_confirmation()'s confirm-delay machinery
    # like the two OVERRIDE_DETECTED call sites below do, so it was landing the
    # FSM on a spurious PENDING confirm state that production never creates.
    # FAN_OVERRIDE_DETECTED is a dedicated kind the dispatcher short-circuits to
    # (IDLE, ACTIVE_PROTECTING_OVERRIDE) immediately, matching production's
    # actual unconditional behavior.
    "handle_fan_manual_override": "fan_override_detected",
    # Issue #651: same entry-wiring gap #643 fixed for handle_fan_manual_override,
    # never done for the thermostat-level override path. Maps to the same
    # OVERRIDE_DETECTED kind — OverrideGraceFsmInputs.setpoint_override already
    # differentiates a setpoint-only override from a mode-change override.
    "handle_manual_override": "override_detected",
}

# Issue #647: the mirror-name-keyed dicts above only ever covered FSM *entry* paths —
# every FSM *exit* path (override confirmed/self-resolved/cleared/cancelled, grace
# naturally expiring) has no `_mirror_to_shadow()` call site at all (see the block
# comment above) and was therefore silently unreachable, leaving each FSM's carried
# state permanently stuck once it entered a non-idle state. `automation.py` already
# emits a named event via `_emit_event_callback` at every one of these transitions —
# that stream reaches `_emit_event()` (below) regardless of shadow-mirror status, so
# `_feed_lifecycle_fsms_from_event()` hooks there instead of adding new dedicated call
# sites. `OverrideGraceFsmEventKind` values are looked up dynamically (not hardcoded
# here) to avoid importing the FSM module at coordinator import time.
_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP: dict[str, str] = {
    # _confirm_override_expired() closure (automation.py) — both branches resolve the
    # same pending confirm window; the FSM re-derives which branch from its own fresh
    # inputs, so one event kind covers both. Both branches emit AFTER their respective
    # state mutation completes, so `self.automation_engine`'s live flags are already
    # correct when `_feed_lifecycle_fsms_from_event()` reads them.
    "override_confirmed": "override_confirm_expired",
    "override_self_resolved": "override_confirm_expired",
    # _on_grace_expired() — "adopted" is a distinct emitted event type for the one
    # branch that returns early without also emitting "grace_expired"; both are the
    # same underlying grace-timer-expiry transition from the FSM's point of view. Both
    # emit only after `clear_manual_override()` has fully returned, same
    # already-correct-by-the-time-we-read-it reasoning as above.
    "grace_expired": "grace_timer_expired",
    "override_adopted": "grace_timer_expired",
    # Deliberately NOT "override_cleared": clear_manual_override() emits it *before*
    # clearing `_manual_override_active` (the event payload needs the pre-clear
    # was_mode/active_since values) — feeding the FSM there would read stale
    # still-active state. cancel_override()/clear_manual_override() are called from a
    # handful of real coordinator.py/api.py sites instead; those are fed directly,
    # post-return, via `_feed_override_grace_fsm_cancelled()` below.
    # Issue #672: _start_grace_period()'s "every other trigger" callers (fan-off,
    # window-close, nat-vent-exit, drift-correction) emit this DISTINCT event type —
    # deliberately not the pre-existing generic "grace_started" (which also fires for
    # the 3 protecting triggers via their own direct dispatcher call sites; reusing it
    # here would wrongly feed UNPROTECTED_GRACE_STARTED for those too). Confirmed live:
    # this closes the "production=idle/active_unprotected fsm=idle/none" disagreement
    # that persisted indefinitely because none of these 4 triggers had ANY feed at all.
    "unprotected_grace_started": "unprotected_grace_started",
}

# Issue #757 Phase 6 Step 4: _DOOR_WINDOW_NAT_VENT_EXIT_EVENT_TYPES (Issue #647) and
# _DOOR_WINDOW_NAT_VENT_REACTIVATED_EVENT_TYPES (Issue #668) were removed here — both
# fed only the now-deleted door/window shadow-comparison axes via
# _evaluate_door_window_fsm()/_evaluate_door_window_fsm_nat_vent_exit(), which had zero
# other consumers.


class ClimateAdvisorCoordinator(DataUpdateCoordinator):
    """Coordinate all Climate Advisor activities."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str = "") -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
        )
        self.config = config
        # Issue #563: DataUpdateCoordinator.config_entry is intentionally never set
        # (see class docstring/CLAUDE.md — always None), so a plain entry_id is kept
        # separately for the one case that needs to write back to the config entry
        # at runtime: async_persist_model_fallback(). Empty string when not supplied
        # (e.g. the simulation harness, which has no real ConfigEntry) — callers that
        # need it check for a resolvable entry via hass.config_entries.async_get_entry.
        self._entry_id: str = entry_id
        self._unsub_listeners: list[Any] = []
        self._unsub_dw_listeners: list[Any] = []
        self._resolved_sensors: list[str] = []
        # Restart-cause diagnostics (Issue #403): set True when a homeassistant.restart/stop
        # service call is observed before shutdown, so async_shutdown() can distinguish a
        # user-initiated restart from a crash.
        self._user_initiated_shutdown = False
        # TOU scheduler (Issue #786): resolved once per apply_classification() cycle by
        # _apply_tou_schedule() — cached (not recomputed per-request) so chart rendering
        # (get_chart_data(), _build_predicted_indoor_future()) and status/API reads all see
        # the same resolution, matching the project's "computed once, not per-hour"
        # invariant already established for _compute_target_band_schedule().
        self._tou_phase_resolution: Any = None
        self._tou_active_cost_resolution: Any = None
        # Phase 3d (Investigation D): dedup guard for the tou_schedule_window_active
        # Activity Record event — True while a resolved high-cost schedule window
        # currently covers `now`, reset when it no longer does, so the event fires once
        # per window-becomes-active transition rather than once every 30-min cycle.
        self._tou_active_window_notified: bool = False
        # Target-band single-choke-point resolution (Issue #514): resolved once per cycle
        # by _resolve_target_band_schedule(), cached here, consumed by the main-cycle and
        # daily-briefing calls to _build_predicted_indoor_future() via band_schedule=.
        self._target_band_schedule: list[dict] | None = None

        # Sub-components
        # Issue #796: entry_id threaded through so each zone's persistence,
        # chart log, and learning DB write to their own entry-scoped file
        # instead of colliding on a shared fixed filename — see storage_paths.py.
        self._state_persistence = StatePersistence(Path(hass.config.config_dir), entry_id=self._entry_id)
        # Issue #543: chart_log.load() does blocking file I/O — moved to
        # async_restore_state() where it can be awaited via the executor.
        self._chart_log = ChartStateLog(
            Path(hass.config.config_dir), max_days=CHART_LOG_MAX_DAYS, entry_id=self._entry_id
        )
        self.learning = LearningEngine(Path(hass.config.config_dir), entry_id=self._entry_id)
        # Issue #757 Phase 6 Step 8: this used to construct two live AutomationEngine
        # instances (_engine_a/_engine_b) behind a runtime-switchable primary/shadow
        # routing (automation_engine/shadow_automation_engine properties, Issue #727/
        # #729) so a second, permanently-dry-run FSM engine could be compared against
        # production (Issue #613) while every subsystem migrated onto FSM dispatch.
        # That migration is complete (Phase 6 Steps 1-7 removed every per-subsystem
        # legacy branch), so both engines had become identical in behavior — this
        # collapses back to the single engine every other part of the codebase
        # (api.py, sensor.py, tests) already expects `coordinator.automation_engine`
        # to be. See #757 for the full migration history.
        self._override_grace_fsm_state: OverrideGraceLifecycleState = (OverrideConfirmState.IDLE, GraceState.NONE)
        self.automation_engine: AutomationEngine = AutomationEngine(
            hass=hass,
            climate_entity=config["climate_entity"],
            weather_entity=config["weather_entity"],
            door_window_sensors=config.get("door_window_sensors", []),
            notify_service=config["notify_service"],
            config=config,
            sensor_polarity_inverted=config.get(CONF_SENSOR_POLARITY_INVERTED, False),
            callbacks=self._build_production_automation_callbacks(),
            role="production",
        )
        _LOGGER.debug(
            "Climate Advisor startup: temp_unit=%s, comfort_heat=%.1f, comfort_cool=%.1f",
            config.get("temp_unit", "fahrenheit"),
            config.get("comfort_heat", 0),
            config.get("comfort_cool", 0),
        )

        # Event log ring buffer (Issue #76) — timestamped automation events for debug download
        self._event_log: list[dict] = []

        # AI subsystem (only if enabled and API key present). The actual
        # ClaudeAPIClient construction does blocking I/O (AsyncAnthropic's
        # constructor reads ~/.config/anthropic/active_config, loads the TLS
        # cert bundle, imports pydantic — confirmed live via HA's own
        # "blocking call" WARNING) and __init__ is synchronous, so it can't be
        # offloaded here. Deferred to async_restore_state() (already offloads
        # other blocking startup I/O the same way), which sets
        # self.claude_client once the executor job completes.
        self.claude_client: ClaudeAPIClient | None = None
        self.ai_skills: AISkillRegistry | None = None
        self._investigation_report_history: list[dict] = []
        self._ai_client_config: dict[str, Any] | None = None
        if config.get(CONF_AI_ENABLED) and config.get(CONF_AI_API_KEY):
            from .ai_skills import AISkillRegistry as _AISkillRegistry
            from .ai_skills_investigator import register_investigator_skill

            self._ai_client_config = config
            self.ai_skills = _AISkillRegistry()
            # Registers whenever AI is enabled (Issue #563) — the merged skill serves
            # both the always-available narration mode (formerly "activity_report",
            # ungated) and the on-demand investigation mode. CONF_AI_INVESTIGATOR_ENABLED
            # no longer gates registration; api.py's ClimateAdvisorInvestigateView still
            # checks it as a cost-control gate on the on-demand/focus-driven call path.
            register_investigator_skill(self.ai_skills)
            _LOGGER.debug("AI subsystem registered — client build deferred to async_restore_state()")
        else:
            _LOGGER.debug(
                "AI subsystem disabled — enabled: %s, key present: %s",
                config.get(CONF_AI_ENABLED, False),
                bool(config.get(CONF_AI_API_KEY)),
            )

        # Startup safety — first update checks HVAC state before applying classification
        self._first_run: bool = True

        # State
        self._current_classification: DayClassification | None = None
        # Issue #817 Part 2: when self._current_classification was last (re)computed from a
        # real forecast fetch — lets _async_send_briefing() reuse a same-cycle classification
        # instead of independently re-fetching/re-classifying, which was the source of two
        # back-to-back weather.get_forecasts calls returning different today_high within under
        # a second. Not set on state-restore (_async_restore_state) — a restored classification
        # is intentionally treated as not fresh, so the first briefing after a restart still
        # does a real fetch.
        self._classification_fetched_at: datetime | None = None
        self._today_record: DailyRecord | None = None
        self._briefing_sent_today = False
        self._last_briefing: str = ""
        self._last_briefing_short: str = ""
        self._briefing_day_type: str | None = None
        # today_high baked into the last-generated briefing text, tracked
        # alongside _briefing_day_type so the mid-day regen gate can also
        # fire on a meaningful value drift, not just a category change.
        self._briefing_today_high: float | None = None
        # nat_vent_cutoff/reason baked into the last-generated briefing text (Issue #847)
        # — tracked alongside _briefing_day_type/_briefing_today_high so the mid-day
        # regen gate can also fire when the WARM/MILD-day window-close time or its
        # reason has drifted, even when day_type/today_high haven't moved.
        self._briefing_nat_vent_cutoff: datetime | None = None
        self._briefing_nat_vent_cutoff_reason: str | None = None
        self._door_open_timers: dict[str, Any] = {}
        self._door_open_timer_expiry: dict[str, str] = {}
        # Issue #645: last_changed timestamps known to be a reconnect/availability blip
        # (old_state was unavailable/unknown, not a genuine off->on transition) rather than
        # a real door-open event — see _async_door_window_changed()/_sensor_debounce_pending().
        self._sensor_reconnect_blip_last_changed: dict[str, Any] = {}

        # Overnight pre-cool phase (Issue #258): scheduled once per warming-trend day
        self._pre_cool_trigger_scheduled: bool = False
        self._pre_cool_trigger_cancel: Any | None = None
        self._pre_cool_status: str | None = None  # surfaced in status API
        self._pre_cool_trigger_dt: datetime | None = None  # full tz-aware trigger datetime
        self._pre_cool_target: float | None = None  # pre-cool ceiling target temp
        # #437 follow-up: tracks natural_vent_active across _emit_event() calls so a
        # True->False transition (nat-vent genuinely exiting, any of its 6 real exit
        # paths) can pull a pending pre-cool trigger earlier instead of leaving it on
        # the STATIC classification-time schedule.
        self._nat_vent_was_active: bool = False

        # Startup coalescing (Issue #321): suppress override detection for 5 min after restart
        self._startup_coalesce_active: bool = True
        self._startup_timer_fired: bool = False
        self._startup_coalesce_expiry: str | None = None

        # Startup retry state — gentle backoff when weather entity isn't ready
        self._startup_retries_remaining: int = 5
        self._startup_retry_delay: int = 30  # seconds; doubles each attempt

        # Temperature history for dashboard chart (cleared at end of day)
        self._outdoor_temp_history: list[tuple[str, float]] = []
        self._indoor_temp_history: list[tuple[str, float]] = []
        self._hourly_forecast_temps: list[dict] = []
        self._last_predicted_indoor: list[dict] = []
        # Issue #817: the single per-cycle nat-vent window/cutoff computation — briefing
        # text, the TLDR table, and the Next Automation/Next User Action cards all read
        # this instead of independently recomputing it, so they can never disagree.
        self._nat_vent_plan: dict | None = None
        self._pred_archive: dict[int, float] = {}
        self._thermal_factors: dict | None = None

        # Observe-only mode: when disabled, automation still runs but skips actions
        self._automation_enabled: bool = True

        # HVAC runtime tracking
        self._hvac_on_since: datetime | None = None
        self._last_outdoor_temp: float | None = None  # most recent outdoor reading for gate checks
        # Issue #130 D16: fallback outdoor temp when weather entity is temporarily unavailable
        self._last_known_outdoor_f: float | None = None
        self._last_known_outdoor_ts: datetime | None = None
        # Thermal observation pipeline (Issue #114)
        self._pending_thermal_event: dict | None = None
        self._pending_observations: dict = {}  # keyed by obs_type string
        self._rejection_log: dict[str, list[dict]] = {}  # keyed by obs_type; capped at _REJECTION_LOG_CAP
        self._pre_heat_sample_buffer: list[dict] = []  # rolling pre-heat window, max 15
        self._startup_hvac_initialized: bool = False  # Issue #96: prevents repeated late-start init
        self._untracked_fan_active: bool = False  # Issue #331 follow-up: entry/exit dedup for fan_running_untracked
        self._fan_state_entity_unavailable_warned: bool = False  # Issue #359: WHF Type 2 fallback warning dedup
        # Issue #805: transient (not persisted) entity-health transition tracker, keyed by
        # config_key -> {"status": str, "first_seen": datetime, "last_notified": datetime}.
        # Not persisted across restarts by design — a restart re-evaluates from scratch,
        # which is desirable since a restart is itself the most common way an entity
        # reappears (and is exactly when "still missing" should notify fresh, not wait
        # out a stale reminder window from before the restart).
        self._entity_health_state: dict[str, dict[str, Any]] = {}
        self._last_commanded_fan_state: bool | None = None  # Issue #361: command-only mode — last on/off commanded
        # Issue #495: last QuietCool RF remote event.* state (== the event's own timestamp)
        # that was actually acted on — dedups a stale unavailable->restore re-announcing an
        # old event as if it were a fresh press. Not persisted: a stale restore right after
        # restart is already covered by _suppress_during_startup_coalescing.
        self._last_fan_remote_event_ts: str | None = None
        # Issue #519: pending burst accumulator for combining a single physical remote
        # interaction (e.g. speed + timer confirmed as separate packets moments apart) into
        # one decision. None when no burst is pending. Session-only, never persisted.
        self._fan_remote_burst: _PendingFanRemoteBurst | None = None
        self._fan_remote_burst_cancel: Callable[[], None] | None = None
        # Issue #519: resolved sibling ambient-speed sensor entity_id (via entity/device
        # registry, keyed off CONF_FAN_REMOTE_ENTITY). Cached once found; a miss is NEVER
        # cached (the registry can populate asynchronously at startup — see
        # _resolve_fan_remote_speed_sensor()).
        self._fan_remote_speed_sensor_eid: str | None = None
        self._last_violation_check: datetime | None = None
        # Chart_log endpoint estimator backfill flags (Issue #137)
        self._passive_k_backfilled: bool = False  # True after chart_log passive windows processed
        # Issue #587: renamed from _vent_k_backfilled/_vent_k_backfill_v2 — deliberate,
        # not just additive. Reusing the old flag name would make an in-place upgrade
        # silently skip re-backfilling under the new narrower (fan-off-only) definition.
        # The rename forces a fresh one-time 30-day backfill under the new filter on
        # upgrade, which is desirable and costs nothing (backfill is idempotent).
        self._vent_window_k_backfilled: bool = False  # True after vent_window_decay backfill processed
        self._vent_fan_k_backfilled: bool = False  # True after vent_fan_decay backfill processed
        # Dual-estimator backfill flags (v2): runs block-OLS alongside endpoint estimator
        self._passive_k_backfill_v2: bool = False
        self._vent_window_k_backfill_v2: bool = False
        self._vent_fan_k_backfill_v2: bool = False
        # Solar phase offset (Issue #147)
        self._solar_phase_offset: float = THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
        self._solar_phase_backfill: bool = False
        self._solar_phase_ac_backfill: bool = False  # Issue #312: AC duty cycle estimator
        # Periodic daily re-fit tracker (Issue #310): date of last incremental fit
        self._last_solar_phase_fit_date: date | None = None

        # GitHub issues cache — separate TTL for open vs closed issues
        self._github_open_cache: list[dict] | None = None
        self._github_open_cache_ts: float = 0.0
        self._github_closed_cache: list[dict] | None = None
        self._github_closed_cache_ts: float = 0.0

        # Occupancy state machine
        self._occupancy_mode: str = OCCUPANCY_HOME
        self._occupancy_away_since: datetime | None = None
        self._unsub_occupancy_listeners: list[Any] = []
        self._occupancy_away_timer_cancel: Any | None = None

        # Coordinator health observability (Issue #480): durable record of the most
        # recent _async_update_data() failure, persisted to survive HA restarts and
        # Docker log rotation — see async_restore_state()/_build_state_dict() below.
        # This is a side-channel record only; it does not replace HA's own
        # last_update_success/UpdateFailed handling, which still governs entity
        # availability.
        self.last_update_error: str | None = None
        self.last_update_error_time: str | None = None
        self.consecutive_failure_count: int = 0

    def _build_production_automation_callbacks(self) -> AutomationEngineCallbacks:
        """Build the callback bundle wired onto the real, acting AutomationEngine.

        Issue #604 (Block 5, subtask N2): extracted verbatim from the 9 post-hoc
        ``self.automation_engine._x_callback = ...`` assignments this coordinator used
        to make right after construction — same callables, same behavior, just built
        as a named bundle so construction is one step instead of nine. A future shadow
        engine (Block 5 subtask Q) MUST NOT be given this bundle — several of these
        callables reach into real production state or trigger real side effects
        regardless of which engine instance invoked them (see
        docs/02-ARCHITECTURE-REFERENCE.md "Engine Callback Isolation").
        """
        return AutomationEngineCallbacks(
            revisit=self.async_request_refresh,
            sensor_check=self._any_sensor_open,
            # Issue #504: lets check_natural_vent_conditions()'s idle_open branch tell
            # whether any currently-open monitored sensor is still within its
            # CONF_SENSOR_DEBOUNCE settle window. Issue #623: _door_open_timers alone
            # raced against the event listener that populates it — see
            # _sensor_debounce_pending()'s docstring for why last_changed is also
            # checked.
            sensor_debounce_pending=self._sensor_debounce_pending,
            emit_event=self._emit_event,
            request_refresh=lambda: self.hass.async_create_task(self.async_request_refresh()),
            # Issue #359: post-grace fan check callback — called by engine when any
            # grace period expires.
            post_grace_fan_check=self._on_post_grace_fan_check,
            # Issue #423: physical fan ground-truth callbacks for
            # _reconcile_fan_physical_drift().
            get_fan_physical_state=self._get_fan_physical_state,
            is_recent_fan_command=self._is_recent_fan_command,
            # Issue #495: reclassify callback — called by _release_whf_and_reclassify()
            # when a manual/remote WHF session ends, reusing the existing fan-off
            # reassert path (Issue #359 Fix A) so the thermostat converges on CA's
            # current classification rather than a blindly-restored, potentially
            # hours-stale captured mode.
            reclassify=self._on_whf_release_reclassify,
        )

    def _dispatch_fsm_evaluators(
        self,
        key: str,
        dispatches: list[tuple[Container[str], Callable[[], None], str]],
    ) -> None:
        """Shared try/except-and-log dispatch loop for FSM re-evaluation (Issue #660,
        Phase R Step 0).

        Each dispatch tuple is ``(registry, evaluator, label)``. If ``key`` (a
        mirrored method name or an emitted event type, depending on caller) is a
        member of ``registry``, ``evaluator()`` runs isolated: any exception is
        logged under ``label`` and swallowed, never allowed to affect production.
        Replaces what used to be a hand-copied ``if key in <registry>: try: ...
        except: _LOGGER.warning(...)`` block per FSM — both in ``_mirror_to_shadow()``'s
        ``finally`` block and in ``_feed_lifecycle_fsms_from_event()`` — so adding a
        future FSM (or fixing a missing registration, as Issue #660 did for
        ``check_natural_vent_conditions()``) is a one-line registry addition instead
        of a new copy-pasted try/except block.
        """
        for registry, evaluator, label in dispatches:
            if key in registry:
                try:
                    evaluator()
                except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
                    _LOGGER.warning("%s failed (isolated, no production impact): %s", label, fsm_exc)

    def _evaluate_override_grace_fsm(self, event_kind: OverrideGraceFsmEventKind) -> None:
        """Run the unified override/grace joint-lifecycle FSM (Issue #639) against
        production's current live readings and track its own independently-
        derived state.

        Issue #647: takes an explicit ``event_kind`` rather than deriving one from a
        ``_mirror_to_shadow()`` method name — the FSM reads its inputs fresh from
        **production** ``self.automation_engine`` every call (see below), never from
        the shadow engine, so it has no actual dependency on whether the triggering
        production call site also happens to replay onto the shadow engine. Coupling
        "does this feed the FSM" to "does this have a mirror call site" (the original
        v1 design) is what left every override/grace *exit* path — confirm, cancel,
        clear, grace-timer-expiry — permanently unreachable, since none of those have
        (or need) a shadow replay. Callers now pass the event kind explicitly: the 3
        original mirrored entry points still resolve theirs via
        ``_OVERRIDE_GRACE_FSM_EVENT_KINDS`` at the ``_mirror_to_shadow()`` call site;
        the new exit-path callers (``_feed_lifecycle_fsms_from_event()``, driven by
        ``_emit_event()``'s already-comprehensive event stream) resolve theirs via
        ``_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP``. The FSM's own tracked state
        (``self._override_grace_fsm_state``) is never written back onto either engine
        — a third, independent computation, purely for comparison against
        production's real derived state. Isolated the same way: any exception here is
        logged and swallowed by the caller, never allowed to affect production.

        ``current_setpoint_f``/``target_setpoint_f`` are resolved inline via the
        same ``select_comfort_band()`` + ``to_fahrenheit()`` sequence
        ``AutomationEngine._override_matches_current_decision()`` itself uses,
        wrapped in the same defensive try/except that method's own live-state
        reads implicitly tolerate (a missing/unparseable setpoint just means "no
        setpoint to compare" — mode match alone is still meaningful evidence).
        """
        from .override_grace_fsm import (
            OverrideGraceFsmEvent,
            OverrideGraceFsmInputs,
        )
        from .override_grace_fsm import (
            transition as _override_grace_transition,
        )

        ae = self.automation_engine
        now = dt_util.now()
        config = self.config

        classification = ae._current_classification
        classification_mode = classification.hvac_mode if classification else None

        current_setpoint_f: float | None = None
        target_setpoint_f: float | None = None
        try:
            if classification is not None and classification_mode in ("heat", "cool"):
                band = select_comfort_band(
                    classification,
                    config,
                    occupancy_mode=ae._occupancy_mode,
                    in_sleep_window=_in_sleep_window(now, config),
                    aggressive_savings=bool(config.get("aggressive_savings", False)),
                )
                target_setpoint_f = band.floor if classification_mode == "heat" else band.ceiling
                state = self.hass.states.get(ae.climate_entity)
                raw_setpoint = state.attributes.get("temperature") if state else None
                if raw_setpoint is not None:
                    unit = config.get("temp_unit", "fahrenheit")
                    current_setpoint_f = to_fahrenheit(float(raw_setpoint), unit)
        except (TypeError, ValueError, AttributeError):
            current_setpoint_f = None
            target_setpoint_f = None

        event = OverrideGraceFsmEvent(
            kind=event_kind,
            inputs=OverrideGraceFsmInputs(
                confirm_seconds=float(config.get(CONF_OVERRIDE_CONFIRM_PERIOD, DEFAULT_OVERRIDE_CONFIRM_SECONDS)),
                setpoint_override=bool(ae._override_confirm_source == "setpoint"),
                current_mode=self._current_hvac_mode(),
                classification_mode=classification_mode,
                manual_override_active=bool(ae._manual_override_active),
                manual_override_mode=ae._manual_override_mode,
                manual_override_source=ae._manual_override_source,
                fan_override_active=bool(ae._fan_override_active),
                current_setpoint_f=current_setpoint_f,
                target_setpoint_f=target_setpoint_f,
                tolerance_f=OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
                within_planned_window=ae._is_within_planned_window_period(),
                any_sensor_open=ae._any_monitored_sensor_open(),
                grace_source=ae._last_resume_source or "automation",
                now=now,
            ),
        )
        result = _override_grace_transition(self._override_grace_fsm_state, event)
        self._override_grace_fsm_state = result.to_state

    def _feed_override_grace_fsm_on_detect(self, method_name: str) -> None:
        """Feed the override/grace FSM an entry event for a former ``_mirror_to_shadow()``
        call site (Issue #757 Phase 6 Step 8).

        The dual-engine shell (and ``_mirror_to_shadow()`` itself) was removed, but 8 of
        its call sites secretly drove the still-live override/grace FSM via its
        ``finally`` block (see the removed ``_OVERRIDE_GRACE_FSM_EVENT_KINDS`` dispatch).
        This preserves that FSM feed exactly, keyed the same way (mirrored method name ->
        event kind via ``_OVERRIDE_GRACE_FSM_EVENT_KINDS``). Isolated the same way every
        other FSM-feed call site is: any exception here is logged and swallowed, never
        allowed to affect production.
        """
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        try:
            self._evaluate_override_grace_fsm(_OGFEventKind(_OVERRIDE_GRACE_FSM_EVENT_KINDS[method_name]))
        except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
            _LOGGER.warning(
                "Override/grace FSM evaluation failed (isolated, no production impact): %s",
                fsm_exc,
            )

    def _feed_override_grace_fsm_cancelled(self) -> None:
        """Feed the override/grace FSM an ``OVERRIDE_CANCELLED`` event (Issue #647).

        ``cancel_override()``/``clear_manual_override()`` emit their own
        ``"override_cleared"`` event *before* clearing ``_manual_override_active``
        (the event payload needs the pre-clear values), so
        ``_feed_lifecycle_fsms_from_event()`` deliberately does not hook that event
        type — it would read stale, still-active state. Call this instead, after the
        real ``cancel_override()``/``clear_manual_override()`` call has fully
        returned, at each of the handful of real coordinator.py/api.py call sites —
        by then ``self.automation_engine``'s flags are correctly cleared. Isolated the
        same way every other FSM-feed call site is: an exception here is logged and
        swallowed, never allowed to affect production.
        """
        try:
            from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

            self._evaluate_override_grace_fsm(_OGFEventKind.OVERRIDE_CANCELLED)
        except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
            _LOGGER.warning(
                "Override/grace FSM evaluation (cancel-driven) failed (isolated, no production impact): %s",
                fsm_exc,
            )

    def _any_override_active(self) -> bool:
        """True if either a thermostat-level or fan-only override is currently active.

        Used to detect a same-cycle clear across ``handle_bedtime()``/
        ``handle_morning_wakeup()`` (Issue #651) — see
        ``_feed_override_grace_fsm_if_cleared()``.
        """
        ae = self.automation_engine
        return bool(ae._manual_override_active or ae._fan_override_active)

    def _feed_override_grace_fsm_if_cleared(self, was_active: bool) -> None:
        """Feed OVERRIDE_CANCELLED if an override that was active before a call is gone after it (Issue #651).

        ``handle_bedtime()``/``handle_morning_wakeup()`` can silently clear a fan-only
        override via ``clear_manual_override()`` — its ``"override_cleared"`` emit is
        gated on ``_manual_override_active`` (false for a fan-only override), so no
        event reaches ``_feed_lifecycle_fsms_from_event()``. Without this, the FSM
        still self-heals within one update cycle via ``_check_orphaned_grace()``'s
        orphaned-grace backstop, but produces a transient shadow-disagreement blip
        until then. Call with the pre-call ``_any_override_active()`` result; this
        reads state strictly after the real engine call has returned, so no staleness
        risk (same ordering guarantee as ``_feed_override_grace_fsm_cancelled()``).
        """
        if was_active and not self._any_override_active():
            self._feed_override_grace_fsm_cancelled()

    def _current_hvac_mode(self) -> str | None:
        """Live thermostat mode read, shared by the door/window FSM evaluation
        (matches ``_pause_for_door_window()``'s own ``self.hass.states.get(
        self.climate_entity)`` read)."""
        state = self.hass.states.get(self.automation_engine.climate_entity)
        return state.state if state else None

    @property
    def automation_enabled(self) -> bool:
        """Whether automation actions are enabled (False = observe-only)."""
        return self._automation_enabled

    def set_automation_enabled(self, enabled: bool) -> None:
        """Enable or disable automation actions (observe-only mode)."""
        self._automation_enabled = enabled
        self.automation_engine.dry_run = not enabled
        _LOGGER.info(
            "Automation %s",
            "enabled" if enabled else "disabled (observe-only)",
        )
        self.hass.async_create_task(self._async_save_state())

    async def async_setup(self) -> None:
        """Set up scheduled events and state listeners."""

        # Parse schedule times
        briefing_time = _parse_time(self.config.get("briefing_time", "06:00"))
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        # Schedule: daily briefing
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_send_briefing_scheduled,
                hour=briefing_time.hour,
                minute=briefing_time.minute,
                second=0,
            )
        )

        # Schedule: morning wake-up
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_morning_wakeup,
                hour=wake_time.hour,
                minute=wake_time.minute,
                second=0,
            )
        )

        # Schedule: bedtime
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_bedtime,
                hour=sleep_time.hour,
                minute=sleep_time.minute,
                second=0,
            )
        )

        # Schedule: midnight — finalize daily record and reset
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_end_of_day,
                hour=23,
                minute=59,
                second=0,
            )
        )

        # Schedule: thermal observation sampler (5-min independent of 30-min update cycle)
        # Decay obs need ~6 samples per 30-min rolling window; the coordinator cycle alone
        # yields only 1 sample per window, which is below the OLS floor.
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_thermal_sample_tick,
                timedelta(minutes=5),
            )
        )

        # Listeners: door/window sensors (resolve groups into individual sensors)
        self._resolved_sensors = self._resolve_monitored_sensors()
        self._subscribe_door_window_listeners()

        # Listeners: occupancy toggles
        self._subscribe_occupancy_listeners()
        self._occupancy_mode = self._compute_occupancy_mode()

        # Listeners: thermostat state (for tracking manual overrides and runtime)
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                self.config["climate_entity"],
                self._async_thermostat_changed,
            )
        )

        # Listeners: fan entity (for detecting manual fan overrides)
        fan_entity = self.config.get(CONF_FAN_ENTITY)
        if fan_entity:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    fan_entity,
                    self._async_fan_entity_changed,
                )
            )

        # Listeners: fan state entity (Issue #359: WHF Type 2 dual-entity support)
        # When a separate physical-state entity is configured and differs from the command entity,
        # register an additional listener so physical on/off transitions are detected.
        _fan_state_entity = self.config.get(CONF_FAN_STATE_ENTITY)
        if _fan_state_entity and _fan_state_entity != fan_entity:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    _fan_state_entity,
                    self._async_fan_entity_changed,
                )
            )

        # Listeners: fan RF remote event entity (Issue #486). Optional — when unset, no
        # subscription is created and behavior is byte-for-byte unchanged from before this
        # feature existed. See docs/fan-remote-spec.md for the firmware event contract.
        fan_remote_entity = self.config.get(CONF_FAN_REMOTE_ENTITY)
        if fan_remote_entity:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    fan_remote_entity,
                    self._async_fan_remote_changed,
                )
            )

        # Issue #361: log fan control mode (state-feedback vs command-only) at startup
        _fan_mode_cfg = self.config.get(CONF_FAN_MODE, "")
        if _fan_mode_cfg not in ("", "none", None, FAN_MODE_DISABLED):
            _feedback_mode = "state-feedback" if self._fan_state_feedback_enabled() else "command-only"
            _LOGGER.info(
                "Fan control mode: %s (fan_entity=%s, fan_state_entity=%s, fan_state_feedback=%s)",
                _feedback_mode,
                self.config.get(CONF_FAN_ENTITY, ""),
                self.config.get(CONF_FAN_STATE_ENTITY, ""),
                self._fan_state_feedback_enabled(),
            )

        # Listeners: indoor and outdoor temp entities — re-evaluate fan on every temp change (Issue #327).
        # Indoor temp: only register a dedicated listener when a separate sensor entity is configured;
        # when indoor comes from the thermostat's current_temperature attribute the existing
        # _async_thermostat_changed dispatch (below) already fires on attribute changes.
        _indoor_temp_source = self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)
        _indoor_temp_entity = (
            self.config.get("indoor_temp_entity")
            if _indoor_temp_source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
            else None
        )
        if _indoor_temp_entity:

            @callback
            def _async_indoor_temp_changed(event: Any) -> None:
                ae = self.automation_engine
                if ae._fan_active or ae._natural_vent_active:
                    self.hass.async_create_task(
                        ae.fan_thermostat_check(
                            indoor=self._get_indoor_temp(),
                            outdoor=self._last_outdoor_temp,
                            trigger="indoor",
                        )
                    )

            self._unsub_listeners.append(
                async_track_state_change_event(self.hass, _indoor_temp_entity, _async_indoor_temp_changed)
            )

        # Outdoor temp: register a listener on the configured outdoor sensor entity (Issue #327).
        # The thermostat listener does NOT carry outdoor temp, so outdoor sensor changes are invisible
        # until the 30-min cycle without this listener.
        _outdoor_temp_source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)
        _outdoor_temp_entity = (
            self.config.get("outdoor_temp_entity")
            if _outdoor_temp_source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
            else None
        )
        if _outdoor_temp_entity:

            @callback
            def _async_outdoor_temp_changed(event: Any) -> None:
                ae = self.automation_engine
                if ae._fan_active or ae._natural_vent_active:
                    new_state = event.data.get("new_state")
                    if new_state is not None:
                        try:
                            unit = self.config.get("temp_unit", "fahrenheit")
                            new_outdoor = to_fahrenheit(float(new_state.state), unit)
                            self._last_outdoor_temp = new_outdoor
                        except (ValueError, TypeError):
                            pass
                    self.hass.async_create_task(
                        ae.fan_thermostat_check(
                            indoor=self._get_indoor_temp(),
                            outdoor=self._last_outdoor_temp,
                            trigger="outdoor",
                        )
                    )

            self._unsub_listeners.append(
                async_track_state_change_event(self.hass, _outdoor_temp_entity, _async_outdoor_temp_changed)
            )

        _LOGGER.info(
            "Fan control: watching indoor=%s outdoor=%s thermostat=%s for thermostatic re-eval",
            _indoor_temp_entity or "(thermostat attr)",
            _outdoor_temp_entity or "(weather service / 30-min poll)",
            self.config["climate_entity"],
        )

        # Startup coalescing: suppress override detection for 5 minutes, then evaluate state (Issue #321)
        _coalesce_expiry = dt_util.now() + timedelta(seconds=300)
        self._startup_coalesce_expiry = _coalesce_expiry.isoformat()

        @callback
        def _on_startup_coalesce_timer(_now: Any) -> None:
            self._startup_timer_fired = True
            self.hass.async_create_task(self.async_request_refresh())

        async_call_later(self.hass, 300, _on_startup_coalesce_timer)
        _LOGGER.info(
            "Startup coalescing window started — override detection suppressed for 300s, coalescing at %s",
            _coalesce_expiry.strftime("%H:%M:%S"),
        )

        # Start minimum fan runtime rolling cycle (Issue #77) — not clock-aligned
        await self.automation_engine.start_min_fan_runtime_cycles()

        # Listener: detect user-initiated HA restart/stop (Issue #403) — best-effort restart
        # cause diagnostics. Distinguishes a user pressing "Restart Home Assistant" from a
        # crash so async_restore_state() can classify the boundary correctly.
        @callback
        def _async_call_service_event(event: Event) -> None:
            if event.data.get("domain") == "homeassistant" and event.data.get("service") in (
                "restart",
                "stop",
            ):
                self._user_initiated_shutdown = True

        self._unsub_listeners.append(self.hass.bus.async_listen(EVENT_CALL_SERVICE, _async_call_service_event))

        # Listener: persist restart-cause diagnostics on a real HA restart (Issue #413).
        # EVENT_HOMEASSISTANT_STOP fires on homeassistant.restart/stop and on HAOS/deploy
        # restarts, but async_unload_entry() (which calls async_shutdown()) does NOT — HA
        # only unloads config entries on entry removal/reload, not on a full restart. Without
        # this listener, clean_shutdown/last_shutdown_version/user_initiated_restart were only
        # ever written on the rare entry-unload path, so the restart-cause classifier in
        # async_restore_state() fell through to "unknown" on every real-world restart.
        @callback
        def _async_homeassistant_stop(_event: Event) -> None:
            self.hass.async_create_task(self._persist_shutdown_diagnostics())

        self._unsub_listeners.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_homeassistant_stop))

        _LOGGER.info("Climate Advisor v%s coordinator setup complete", VERSION)

    @callback
    def _async_thermal_sample_tick(self, now: datetime) -> None:
        """Sample active thermal observations on the 5-min tick."""
        self._refresh_weather_service_outdoor_temp()
        self._sample_all_observations()

    def _refresh_weather_service_outdoor_temp(self) -> None:
        """Refresh the interpolated outdoor estimate every 5 min (Issue #511).

        Weather-service installs only — sensor/input_number installs already get
        live updates via their own state-change listener (see coordinator.py ~608)
        and must not be touched here, since they have a true live reading already.
        """
        source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)
        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            return
        weather_entity = self.config.get("weather_entity")
        weather_state = self.hass.states.get(weather_entity) if weather_entity else None
        if not weather_state:
            return
        self._apply_outdoor_temp(self._get_outdoor_temp(weather_state.attributes), record_history=False)

    @property
    def zone_label(self) -> str | None:
        """Identifying label for this zone's log_capture attribution (Issue #812).

        `self._entry_id` is the only identifying info reliably available on
        every coordinator instance (including the simulation harness, which
        has no real ConfigEntry and passes ``entry_id=""``) — falls back to
        None (log_capture's "unknown zone" marker) when unset. Uses
        ``getattr`` (not a bare attribute read) because several existing
        tests partially instantiate the coordinator via
        ``object.__new__(ClimateAdvisorCoordinator)`` (bypassing ``__init__``)
        and bind only the method(s) under test — same established pattern
        ``_async_update_data()``'s own defensive defaults already follow.
        """
        return getattr(self, "_entry_id", None) or None

    def _executor_job(self, fn: Any, *args: Any) -> Any:
        """Same as ``self.hass.async_add_executor_job(fn, *args)``, zone-tagged.

        Issue #812: a plain ``ContextVar`` set by ``log_capture.zone_scope()``
        does not propagate into work submitted via
        ``hass.async_add_executor_job()`` (verified empirically — see
        log_capture.py's module docstring). Every coordinator call site that
        previously called ``self.hass.async_add_executor_job()`` directly now
        routes through here so any ``_LOGGER`` call made inside ``fn`` is
        still tagged with the calling zone. Purely additive: same return
        value (the executor future) and exception propagation as before.
        """
        return self.hass.async_add_executor_job(log_capture.bind_zone_for_executor(fn), *args)

    async def async_restore_state(self) -> None:
        """Restore operational state from disk after startup."""
        _LOGGER.info("Climate Advisor v%s starting up", VERSION)
        ai_client_config = getattr(self, "_ai_client_config", None)
        if ai_client_config is not None:
            from .claude_api import ClaudeAPIClient as _ClaudeAPIClient

            self.claude_client = await self._executor_job(functools.partial(_ClaudeAPIClient, ai_client_config))
            _LOGGER.info("AI subsystem initialized — model: %s", ai_client_config.get("ai_model", "unknown"))
            self._ai_client_config = None
        # Issue #543: chart_log.load() does blocking file I/O — offload to executor.
        await self._executor_job(self._chart_log.load)
        await self._executor_job(self.learning.load_state)
        # Restore rejection_log from LearningState (load_state() already validated and capped it)
        loaded_rl = self.learning._state.rejection_log
        if isinstance(loaded_rl, dict):
            self._rejection_log = {
                k: v[-_REJECTION_LOG_CAP:] if isinstance(v, list) else [] for k, v in loaded_rl.items()
            }
        else:
            self._rejection_log = {}
        state = await self._executor_job(self._state_persistence.load)
        if not state:
            _LOGGER.debug("No persisted state found — starting fresh")
            return

        today_str = dt_util.now().strftime("%Y-%m-%d")
        state_date = state.get("date", "")
        yesterday_str = (dt_util.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # If the state is from yesterday, recover the DailyRecord to learning
        if state_date == yesterday_str and state.get("today_record"):
            try:
                rec_data = state["today_record"]
                # Normalize suggestion_sent for backward compat
                sent = rec_data.get("suggestion_sent")
                if sent is None:
                    rec_data["suggestion_sent"] = []
                elif isinstance(sent, str):
                    rec_data["suggestion_sent"] = [sent]
                recovered = DailyRecord(**rec_data)
                self.learning.record_day(recovered)
                await self._executor_job(self.learning.save_state)
                _LOGGER.info("Recovered yesterday's record during startup")
            except (TypeError, KeyError) as err:
                _LOGGER.warning("Failed to recover yesterday's record: %s", err)

        # Restore AI stats regardless of date boundary — monthly budget and cumulative
        # counters must persist across reboots. Daily counters self-correct via
        # _reset_daily_counters_if_needed() inside restore_persistent_stats().
        if self.claude_client:
            ai_stats = state.get("ai_stats")
            if ai_stats and isinstance(ai_stats, dict):
                self.claude_client.restore_persistent_stats(ai_stats)

        # Coordinator health (Issue #480): restore regardless of date boundary, same
        # reasoning as ai_stats above — a failure recorded just before an overnight
        # restart is still the answer to "why did the dashboard freeze last night",
        # and that question doesn't respect the day boundary the rest of this
        # function gates on.
        self.last_update_error = state.get("last_update_error")
        self.last_update_error_time = state.get("last_update_error_time")
        try:
            self.consecutive_failure_count = int(state.get("consecutive_failure_count", 0) or 0)
        except (TypeError, ValueError):
            self.consecutive_failure_count = 0

        if state_date != today_str:
            _LOGGER.debug(
                "Persisted state is from %s (today is %s) — starting fresh",
                state_date,
                today_str,
            )
            return

        # Same-day restore
        _LOGGER.info("Restoring same-day state from %s", state.get("last_saved"))

        # Classification
        cls_data = state.get("classification")
        if cls_data:
            try:
                wot = cls_data.get("window_open_time")
                wct = cls_data.get("window_close_time")
                self._current_classification = DayClassification(
                    day_type=cls_data["day_type"],
                    trend_direction=cls_data["trend_direction"],
                    trend_magnitude=cls_data.get("trend_magnitude", 0),
                    today_high=cls_data["today_high"],
                    today_low=cls_data["today_low"],
                    tomorrow_high=cls_data["tomorrow_high"],
                    tomorrow_low=cls_data["tomorrow_low"],
                    hvac_mode=cls_data.get("hvac_mode", ""),
                    pre_condition=cls_data.get("pre_condition", False),
                    pre_condition_target=cls_data.get("pre_condition_target"),
                    windows_recommended=cls_data.get("windows_recommended", False),
                    window_open_time=(time.fromisoformat(wot) if wot else None),
                    window_close_time=(time.fromisoformat(wct) if wct else None),
                    setback_modifier=cls_data.get("setback_modifier", 0.0),
                )
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Failed to restore classification: %s", err)

        # Temperature history
        temp_hist = state.get("temp_history", {})
        self._outdoor_temp_history = [(ts, t) for ts, t in temp_hist.get("outdoor", [])]
        self._indoor_temp_history = [(ts, t) for ts, t in temp_hist.get("indoor", [])]
        # Issue #540: mirror the restored buffer's peak/count immediately, so soft-start's
        # minimum-sample guard doesn't see an artificially thin buffer for up to 30 min
        # after a same-day restart (this restore path only runs when the persisted date
        # matches today's local date — see the state_date == today_str gate above).
        if self._outdoor_temp_history:
            _restored_outdoor_temps = [t for _, t in self._outdoor_temp_history]
            self.automation_engine._outdoor_temp_today_peak = max(_restored_outdoor_temps)
            self.automation_engine._outdoor_temp_today_sample_count = len(_restored_outdoor_temps)

        # Today's record
        record_data = state.get("today_record")
        if record_data:
            try:
                # Normalize suggestion_sent for backward compat (was str|None, now list)
                sent = record_data.get("suggestion_sent")
                if sent is None:
                    record_data["suggestion_sent"] = []
                elif isinstance(sent, str):
                    record_data["suggestion_sent"] = [sent]
                self._today_record = DailyRecord(**record_data)
            except (TypeError, KeyError) as err:
                _LOGGER.warning("Failed to restore today's record: %s", err)

        # Briefing state
        briefing = state.get("briefing_state", {})
        self._briefing_sent_today = briefing.get("sent_today", False)
        self._last_briefing = briefing.get("last_text", "")
        self._last_briefing_short = briefing.get("last_text_short", "")
        self._briefing_day_type = briefing.get("briefing_day_type")
        self._briefing_today_high = briefing.get("briefing_today_high")
        _restored_nat_vent_cutoff = briefing.get("briefing_nat_vent_cutoff")
        try:
            self._briefing_nat_vent_cutoff = (
                datetime.fromisoformat(_restored_nat_vent_cutoff) if _restored_nat_vent_cutoff else None
            )
        except (TypeError, ValueError):
            self._briefing_nat_vent_cutoff = None
        self._briefing_nat_vent_cutoff_reason = briefing.get("briefing_nat_vent_cutoff_reason")

        # Automation state
        auto_state = state.get("automation_state", {})
        if auto_state:
            self.automation_engine.restore_state(auto_state)
            # Issue #615: restore the same fan-activity hints onto the shadow engine —
            # now load-bearing since reconcile_fan_on_startup() (which reads them) is
            # mirrored too. Still does not restore _natural_vent_active or
            # _current_classification on either engine (restore_state()'s own
            # documented clean-slate design).

        # Grace state is cleared by restore_state() (clean-slate design, Issue #282).
        # The coordinator does not reschedule grace timers on restart.

        # Observe-only mode
        self._automation_enabled = state.get("automation_enabled", True)
        self.automation_engine.dry_run = not self._automation_enabled

        # Occupancy state — sync to engine so guards are active from startup (Issue #85)
        self._occupancy_mode = state.get("occupancy_mode", OCCUPANCY_HOME)
        self.automation_engine.set_occupancy_mode(self._occupancy_mode)
        away_since = state.get("occupancy_away_since")
        if away_since:
            try:
                self._occupancy_away_since = datetime.fromisoformat(away_since)
            except (ValueError, TypeError):
                self._occupancy_away_since = None

        # Chart_log endpoint estimator backfill flags (Issue #137)
        self._passive_k_backfilled = bool(state.get("passive_k_backfilled", False))
        # Issue #587: new flag names (not vent_k_backfilled) — see __init__ comment.
        self._vent_window_k_backfilled = bool(state.get("vent_window_k_backfilled", False))
        self._vent_fan_k_backfilled = bool(state.get("vent_fan_k_backfilled", False))
        # Dual-estimator backfill flags (v2)
        self._passive_k_backfill_v2 = bool(state.get("passive_k_backfill_v2", False))
        self._vent_window_k_backfill_v2 = bool(state.get("vent_window_k_backfill_v2", False))
        self._vent_fan_k_backfill_v2 = bool(state.get("vent_fan_k_backfill_v2", False))
        # Solar phase offset backfill flag (Issue #147)
        self._solar_phase_backfill = bool(state.get("solar_phase_backfill", False))
        self._solar_phase_ac_backfill = bool(state.get("solar_phase_ac_backfill", False))  # Issue #312
        # Periodic daily re-fit tracker (Issue #310)
        _fit_date_str = state.get("last_solar_phase_fit_date")
        self._last_solar_phase_fit_date = date.fromisoformat(_fit_date_str) if _fit_date_str else None

        # Prediction archive — restore only on same-day restores (already gated above)
        raw_archive = state.get("pred_archive")
        if isinstance(raw_archive, dict):
            restored: dict[int, float] = {}
            for k, v in raw_archive.items():
                try:
                    restored[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            self._pred_archive = restored

        # Load AI report history if AI subsystem is active
        if self.claude_client:
            await self._executor_job(self._load_investigation_reports)

        # Restore event log ring buffer and emit restart boundary marker
        saved_log = state.get("event_log")
        if isinstance(saved_log, list):
            self._event_log = _prune_event_log(saved_log, dt_util.now())

        # Restart-cause classification (Issue #403): compare the persisted last-shutdown
        # version against VERSION, and check whether the prior shutdown was clean.
        _last_shutdown_version = self.learning._state.last_shutdown_version
        _clean_shutdown = self.learning._state.clean_shutdown
        _restart_payload: dict[str, Any] = {"recovered_events": len(self._event_log)}
        if isinstance(_last_shutdown_version, str) and _last_shutdown_version and _last_shutdown_version != VERSION:
            _cause = "version_changed"
            _LOGGER.info("Version changed: %s -> %s", _last_shutdown_version, VERSION)
            self._emit_event(
                "version_changed",
                {"old_version": _last_shutdown_version, "new_version": VERSION},
            )
            _restart_payload["old_version"] = _last_shutdown_version
            _restart_payload["new_version"] = VERSION
        elif _clean_shutdown:
            _cause = "user_restart"
        else:
            _cause = "unknown"
        _restart_payload["cause"] = _cause
        self._emit_event("system_restarted", _restart_payload)

        # Reset in-memory clean_shutdown so an unclean exit before the next clean shutdown
        # is correctly classified as "unknown" rather than stale-carrying "user_restart".
        # Not persisted here — it will be written on the next save_state() call.
        self.learning._state.clean_shutdown = False

        _LOGGER.info("State restore complete")

    def _build_state_dict(self) -> dict[str, Any]:
        """Serialize current operational state for persistence."""
        c = self._current_classification
        cls_dict = None
        if c:
            cls_dict = {
                "day_type": c.day_type,
                "trend_direction": c.trend_direction,
                "trend_magnitude": c.trend_magnitude,
                "today_high": c.today_high,
                "today_low": c.today_low,
                "tomorrow_high": c.tomorrow_high,
                "tomorrow_low": c.tomorrow_low,
                "hvac_mode": c.hvac_mode,
                "pre_condition": c.pre_condition,
                "pre_condition_target": c.pre_condition_target,
                "windows_recommended": c.windows_recommended,
                "window_open_time": (c.window_open_time.isoformat() if c.window_open_time else None),
                "window_close_time": (c.window_close_time.isoformat() if c.window_close_time else None),
                "setback_modifier": c.setback_modifier,
                "window_opportunity_morning": c.window_opportunity_morning,
                "window_opportunity_evening": c.window_opportunity_evening,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat() if c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat() if c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat() if c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat() if c.window_opportunity_evening_end else None
                ),
            }

        record_dict = None
        if self._today_record:
            from dataclasses import asdict

            record_dict = asdict(self._today_record)

        return {
            "date": dt_util.now().strftime("%Y-%m-%d"),
            "last_saved": dt_util.now().isoformat(),
            "classification": cls_dict,
            "temp_history": {
                "outdoor": list(self._outdoor_temp_history),
                "indoor": list(self._indoor_temp_history),
            },
            "automation_state": self.automation_engine.get_serializable_state(),
            "today_record": record_dict,
            "briefing_state": {
                "sent_today": self._briefing_sent_today,
                "last_text": self._last_briefing,
                "last_text_short": self._last_briefing_short,
                "briefing_day_type": self._briefing_day_type,
                "briefing_today_high": getattr(self, "_briefing_today_high", None),
                "briefing_nat_vent_cutoff": (
                    getattr(self, "_briefing_nat_vent_cutoff", None).isoformat()
                    if getattr(self, "_briefing_nat_vent_cutoff", None)
                    else None
                ),
                "briefing_nat_vent_cutoff_reason": getattr(self, "_briefing_nat_vent_cutoff_reason", None),
            },
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
            "occupancy_away_since": (self._occupancy_away_since.isoformat() if self._occupancy_away_since else None),
            "ai_stats": self.claude_client.get_persistent_stats() if self.claude_client else {},
            "pred_archive": {str(k): v for k, v in self._pred_archive.items()},
            "passive_k_backfilled": self._passive_k_backfilled,
            "vent_window_k_backfilled": self._vent_window_k_backfilled,
            "vent_fan_k_backfilled": self._vent_fan_k_backfilled,
            "passive_k_backfill_v2": self._passive_k_backfill_v2,
            "vent_window_k_backfill_v2": self._vent_window_k_backfill_v2,
            "vent_fan_k_backfill_v2": self._vent_fan_k_backfill_v2,
            "solar_phase_backfill": self._solar_phase_backfill,
            "solar_phase_ac_backfill": self._solar_phase_ac_backfill,  # Issue #312
            "last_solar_phase_fit_date": (
                self._last_solar_phase_fit_date.isoformat() if self._last_solar_phase_fit_date else None
            ),
            "event_log": list(self._event_log),
            # Coordinator health (Issue #480). getattr() defaults, not direct
            # attribute access: several existing tests build a coordinator via
            # object.__new__() (bypassing __init__, which is where these are
            # normally set) and call _build_state_dict() directly.
            "last_update_error": getattr(self, "last_update_error", None),
            "last_update_error_time": getattr(self, "last_update_error_time", None),
            "consecutive_failure_count": getattr(self, "consecutive_failure_count", 0),
        }

    async def _async_save_state(self) -> None:
        """Persist current operational state to disk."""
        state_dict = self._build_state_dict()
        await self._executor_job(self._state_persistence.save, state_dict)

    async def async_store_investigation_report(self, result: dict) -> None:
        """Store an investigation report result in history and persist to disk."""

        entry = {
            "timestamp": dt_util.now().isoformat(),
            "report_type": "investigation",
            "result": result,
        }
        self._investigation_report_history.append(entry)
        if len(self._investigation_report_history) > INVESTIGATION_REPORT_HISTORY_CAP:
            self._investigation_report_history = self._investigation_report_history[-INVESTIGATION_REPORT_HISTORY_CAP:]
        await self._executor_job(self._save_investigation_reports)

    async def async_persist_model_fallback(self, new_model: str) -> None:
        """Persist an automatic AI-model deprecation fallback (Issue #563).

        Called when claude_api.py's ClaudeResponse.resolved_model differs from the
        model that was actually requested — meaning a configured model was rejected
        as invalid/deprecated and claude_api.py substituted a same-tier replacement
        for that one request. This updates the persisted config entry so future
        requests use the replacement directly, and updates the in-memory config dict
        immediately so this doesn't need a full integration reload. Silent + logged
        (WARNING) — no user-facing notification, per design decision.
        """
        from .const import CONF_AI_MODEL  # noqa: PLC0415

        old_model = self.config.get(CONF_AI_MODEL, "")
        if not self._entry_id:
            _LOGGER.warning(
                "AI model auto-migrated %s -> %s (previous model no longer available), "
                "but no config entry id is available to persist it — will re-detect next request",
                old_model,
                new_model,
            )
            self.config[CONF_AI_MODEL] = new_model
            return

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            _LOGGER.warning(
                "AI model auto-migrated %s -> %s, but config entry '%s' could not be found — not persisted",
                old_model,
                new_model,
                self._entry_id,
            )
            return

        new_data = {**entry.data, CONF_AI_MODEL: new_model}
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        self.config[CONF_AI_MODEL] = new_model
        _LOGGER.warning(
            "AI model auto-migrated: %s -> %s (previous model no longer available)",
            old_model,
            new_model,
        )

    def get_investigation_report_history(self) -> list[dict]:
        """Return a copy of the investigation report history."""
        return list(self._investigation_report_history)

    def delete_investigation_report(self, timestamp: str) -> bool:
        """Remove an investigation report by timestamp. Returns True if removed."""
        before = len(self._investigation_report_history)
        self._investigation_report_history = [
            e for e in self._investigation_report_history if e.get("timestamp") != timestamp
        ]
        return len(self._investigation_report_history) < before

    def _save_investigation_reports(self) -> None:
        """Save investigation report history to disk (atomic write)."""
        import json
        import os
        import sys

        filepath = self.hass.config.path(INVESTIGATION_REPORTS_FILE)
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._investigation_report_history, f, indent=2, default=str)
            if sys.platform != "win32":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, filepath)
        except Exception:
            _LOGGER.exception("Failed to save investigation reports to %s", filepath)
            import contextlib

            with contextlib.suppress(OSError):
                os.remove(tmp_path)

    def _load_investigation_reports(self) -> None:
        """Load investigation report history from disk."""
        import json

        filepath = self.hass.config.path(INVESTIGATION_REPORTS_FILE)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
                recent = [e for e in data if isinstance(e, dict) and e.get("timestamp", "") >= cutoff]
                self._investigation_report_history = recent[-INVESTIGATION_REPORT_HISTORY_CAP:]
                _LOGGER.debug(
                    "Loaded %d investigation reports from disk",
                    len(self._investigation_report_history),
                )
            else:
                _LOGGER.warning("Investigation reports file has unexpected format, starting fresh")
                self._investigation_report_history = []
        except FileNotFoundError:
            self._investigation_report_history = []
        except Exception:
            _LOGGER.exception("Failed to load investigation reports from %s", filepath)
            self._investigation_report_history = []

    def _flush_hvac_runtime(self) -> None:
        """Flush accumulated HVAC runtime to today's record."""
        if self._hvac_on_since and self._today_record:
            now = dt_util.now()
            elapsed = (now - self._hvac_on_since).total_seconds() / 60.0
            self._today_record.hvac_runtime_minutes += elapsed
            self._hvac_on_since = now  # Reset to now for continued tracking

    def _resolve_monitored_sensors(self) -> list[str]:
        """Resolve all monitored sensor entity IDs.

        Returns the configured door_window_sensors list directly. Binary sensor
        groups in HA are themselves binary_sensor entities, so they can be
        monitored without expansion — their state reflects member states.
        """
        return list(self.config.get("door_window_sensors", []))

    def _subscribe_door_window_listeners(self) -> None:
        """Subscribe to state changes for all resolved door/window sensors."""
        for sensor_id in self._resolved_sensors:
            self._unsub_dw_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    sensor_id,
                    self._async_door_window_changed,
                )
            )

    def _unsubscribe_door_window_listeners(self) -> None:
        """Unsubscribe all door/window sensor listeners."""
        for unsub in self._unsub_dw_listeners:
            unsub()
        self._unsub_dw_listeners.clear()

    # ── Occupancy toggle methods ─────────────────────────────────────

    def _is_toggle_on(self, entity_id: str, invert: bool) -> bool:
        """Check if a toggle entity is effectively ON, respecting invert."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown"):
            if state:
                _LOGGER.warning(
                    "Occupancy toggle %s is %s — treating as OFF",
                    entity_id,
                    state.state,
                )
            return False
        raw_on = state.state == "on"
        return not raw_on if invert else raw_on

    def _compute_occupancy_mode(self) -> str:
        """Compute effective occupancy mode from toggle entities (priority order).

        Issue #744 (strangler-fig completion Phase 4): resolves each toggle's
        effective boolean via ``self._is_toggle_on()`` (still the only HA-state
        touchpoint — reads ``hass.states`` and applies the invert flag), then
        delegates the guest > vacation > home/away priority decision itself to
        ``occupancy_priority.decide_occupancy_priority()``, a pure leaf. Not flag-
        gated — see that module's own docstring for why this extraction needed no
        A/B shadow proof (it was already effectively pure).
        """
        cfg = self.config
        guest_entity = cfg.get(CONF_GUEST_TOGGLE)
        vacation_entity = cfg.get(CONF_VACATION_TOGGLE)
        home_entity = cfg.get(CONF_HOME_TOGGLE)
        inputs = OccupancyPriorityInputs(
            guest_configured=bool(guest_entity),
            guest_on=bool(guest_entity) and self._is_toggle_on(guest_entity, cfg.get(CONF_GUEST_TOGGLE_INVERT, False)),
            vacation_configured=bool(vacation_entity),
            vacation_on=bool(vacation_entity)
            and self._is_toggle_on(vacation_entity, cfg.get(CONF_VACATION_TOGGLE_INVERT, False)),
            home_configured=bool(home_entity),
            home_on=bool(home_entity) and self._is_toggle_on(home_entity, cfg.get(CONF_HOME_TOGGLE_INVERT, False)),
        )
        return decide_occupancy_priority(inputs)

    def _subscribe_occupancy_listeners(self) -> None:
        """Subscribe to state changes for all configured occupancy toggles."""
        for conf_key in (CONF_HOME_TOGGLE, CONF_VACATION_TOGGLE, CONF_GUEST_TOGGLE):
            entity_id = self.config.get(conf_key)
            if entity_id:
                self._unsub_occupancy_listeners.append(
                    async_track_state_change_event(
                        self.hass,
                        entity_id,
                        self._async_occupancy_toggle_changed,
                    )
                )

    def _unsubscribe_occupancy_listeners(self) -> None:
        """Unsubscribe all occupancy toggle listeners."""
        for unsub in self._unsub_occupancy_listeners:
            unsub()
        self._unsub_occupancy_listeners.clear()

    def _cancel_occupancy_away_timer(self) -> None:
        """Cancel any pending occupancy away setback timer."""
        if self._occupancy_away_timer_cancel:
            self._occupancy_away_timer_cancel()
            self._occupancy_away_timer_cancel = None
            _LOGGER.debug("Occupancy away timer cancelled")

    async def _async_occupancy_toggle_changed(self, event: Event) -> None:
        """Handle an occupancy toggle state change."""
        new_mode = self._compute_occupancy_mode()

        if new_mode == self._occupancy_mode:
            return  # No effective change

        old_mode = self._occupancy_mode
        _LOGGER.info(
            "Occupancy mode changed: %s -> %s (trigger: %s)",
            old_mode,
            new_mode,
            event.data.get("entity_id", "unknown"),
        )

        # Track away minutes
        now = dt_util.now()
        present_modes = {OCCUPANCY_HOME, OCCUPANCY_GUEST}
        was_present = old_mode in present_modes
        is_present = new_mode in present_modes

        if was_present and not is_present:
            # Leaving home
            self._occupancy_away_since = now
        elif not was_present and is_present:
            # Cancel pending away setback timer
            self._cancel_occupancy_away_timer()
            # Returning home
            if self._occupancy_away_since and self._today_record:
                elapsed = (now - self._occupancy_away_since).total_seconds() / 60.0
                self._today_record.occupancy_away_minutes += elapsed
                _LOGGER.debug(
                    "Away duration: %.1f minutes added to daily record",
                    elapsed,
                )
            self._occupancy_away_since = None

        self._occupancy_mode = new_mode
        # Issue #85: sync occupancy mode to engine immediately so guards
        # take effect even before the delayed away timer fires
        self.automation_engine.set_occupancy_mode(new_mode)

        # Call appropriate automation handler
        if new_mode == OCCUPANCY_VACATION:
            self._cancel_occupancy_away_timer()
            await self.automation_engine.handle_occupancy_vacation()
        elif new_mode == OCCUPANCY_AWAY:
            delay_seconds = OCCUPANCY_SETBACK_MINUTES * 60
            _LOGGER.info(
                "Starting %d-minute occupancy away timer before applying setback",
                OCCUPANCY_SETBACK_MINUTES,
            )
            self._cancel_occupancy_away_timer()

            @callback
            def _occupancy_away_timer_expired(_now: Any) -> None:
                self._occupancy_away_timer_cancel = None
                _LOGGER.info("Occupancy away timer expired — applying setback")
                self.hass.async_create_task(self.automation_engine.handle_occupancy_away())

            self._occupancy_away_timer_cancel = async_call_later(
                self.hass,
                delay_seconds,
                _occupancy_away_timer_expired,
            )
        elif new_mode in present_modes:
            self._cancel_occupancy_away_timer()
            await self.automation_engine.handle_occupancy_home()

        await self._async_save_state()

    # ── End occupancy methods ──────────────────────────────────────

    def _cancel_all_debounce_timers(self) -> None:
        """Cancel all pending door/window debounce timers.

        Called when a manual HVAC override is detected so that orphaned
        debounce timers for still-open sensors cannot interfere with the
        manual grace period.
        """
        if self._door_open_timers:
            _LOGGER.info(
                "Cancelling %d pending debounce timer(s) due to manual override",
                len(self._door_open_timers),
            )
            for cancel in self._door_open_timers.values():
                cancel()
            self._door_open_timers.clear()
            self._door_open_timer_expiry.clear()

    def _is_sensor_open(self, entity_id: str) -> bool:
        """Check if a door/window sensor is in the 'open' state, respecting polarity."""
        inverted = self.config.get(CONF_SENSOR_POLARITY_INVERTED, False)
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        if inverted:
            return state.state == "off"
        return state.state == "on"

    def _is_recent_hvac_command(self, threshold_seconds: float = 3.0) -> bool:
        """Check if an HVAC command was issued very recently (race guard)."""
        cmd_time = self.automation_engine._hvac_command_time
        if cmd_time is None:
            return False
        return (dt_util.now() - cmd_time).total_seconds() < threshold_seconds

    def _is_recent_temp_command(self, threshold_seconds: float = 30.0) -> bool:
        """Check if a temperature setpoint command was issued recently (race guard)."""
        cmd_time = self.automation_engine._temp_command_time
        if cmd_time is None:
            return False
        return (dt_util.now() - cmd_time).total_seconds() < threshold_seconds

    def _is_recent_fan_command(self, threshold_seconds: float = 30.0) -> bool:
        """Check if a fan command was issued recently (race guard).

        Adding a new fan-state listener (physical entity, RF remote, or otherwise)?
        This is the shared echo-suppression primitive — call it before treating any
        transition/event as external. Current call sites: coordinator.py ~3627/3785/3862
        (see the Issue #417 sibling list at ~3621), coordinator.py ~4029
        (_async_fan_entity_changed), and coordinator.py ~4106 (_async_fan_remote_changed,
        Issue #567). Missing this guard on a new listener has shipped as a production bug
        twice (#417, #567) — don't add a third.
        """
        cmd_time = self.automation_engine._fan_command_time
        if cmd_time is None:
            return False
        return (dt_util.now() - cmd_time).total_seconds() < threshold_seconds

    def _any_sensor_open(self) -> bool:
        """Return True if any monitored contact sensor is currently open."""
        return any(self._is_sensor_open(s) for s in self._resolved_sensors)

    def _sensor_debounce_pending(self) -> bool:
        """True if any monitored sensor is open and still within its debounce window.

        Checks both the internal timer registry (_door_open_timers, populated once
        _async_door_window_changed() has processed the state-changed event) and each
        open sensor's own HA-authoritative last_changed timestamp — so a sensor that
        has JUST opened, but whose debounce-timer registration hasn't run yet (a race
        between this coordinator's periodic refresh cycle and the event bus dispatching
        the state-changed callback — Issue #623), is still correctly treated as
        pending rather than settled. This is the single shared signal consumed by both
        automation.py's _sync_paused_by_door_with_live_sensors() (Issue #620) and
        _idle_open (Issue #504) — fixing it here fixes both callers.

        Issue #645: a sensor re-reporting "on" after an unavailable/unknown blip (e.g. a
        group/helper entity re-registering during an HA restart) stamps a fresh
        last_changed exactly like a genuine open would, even though the window has
        physically been open for hours — _async_door_window_changed() records that
        specific last_changed value in _sensor_reconnect_blip_last_changed so it's excluded
        here rather than read as "just opened." A later GENUINE off->on transition moves
        last_changed again, which no longer matches the recorded blip value, so this
        exclusion never masks a real open.
        """
        if self._door_open_timers:
            return True
        debounce_sec = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        now = dt_util.now()
        for sensor_id in self._resolved_sensors:
            if not self._is_sensor_open(sensor_id):
                continue
            state = self.hass.states.get(sensor_id)
            last_changed = getattr(state, "last_changed", None) if state else None
            if last_changed is None:
                continue
            if self._sensor_reconnect_blip_last_changed.get(sensor_id) == last_changed:
                continue
            if (now - last_changed).total_seconds() < debounce_sec:
                return True
        return False

    def _apply_outdoor_windows_gate(self) -> None:
        """Gate windows_recommended against current outdoor temp (Issue #111).

        The classifier sets windows_recommended based on forecast day-type only.
        This method clears the flag when current outdoor conditions would push
        indoor temps outside the comfort zone:
          - outdoor > comfort_cool  → opening windows would overheat the house
          - outdoor < comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN  → extreme cold

        Called after every classify_day() in _async_update_data() and
        async_send_briefing(). No-op when classification is None,
        windows_recommended is already False, or outdoor temp is unavailable.
        """
        c = self._current_classification
        if c is None or not c.windows_recommended:
            return

        outdoor = self._last_outdoor_temp
        if outdoor is None:
            return  # No current data — keep classifier's recommendation

        comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
        comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))

        if outdoor > comfort_cool:
            _LOGGER.debug(
                "windows_recommended → False: outdoor %.1f°F above comfort_cool %.1f°F",
                outdoor,
                comfort_cool,
            )
            c.windows_recommended = False
        elif outdoor < comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN:
            _LOGGER.debug(
                "windows_recommended → False: outdoor %.1f°F below extreme-cold threshold %.1f°F",
                outdoor,
                comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN,
            )
            c.windows_recommended = False

    def _apply_outdoor_temp(self, value: float | None, *, record_history: bool) -> None:
        """Propagate a newly-read outdoor temperature to every downstream consumer.

        Consolidates what used to be 3-4 independently-written touch points (the
        30-min main cycle, the daily briefing, and now the 5-min tick added by
        Issue #511) into a single shared function, so a new consumer only needs
        to be wired in once.

        record_history=True only from the 30-min cycle — the observed-extremes
        history used for classification's high/low correction intentionally stays
        on its existing 30-min cadence; sampling it 6x more often via the 5-min
        tick could subtly affect classification stability and is out of scope.
        """
        if value is None:
            return
        previous = getattr(self, "_last_outdoor_temp", None)
        self._last_outdoor_temp = value
        self.automation_engine.update_outdoor_temp(value)
        self._apply_outdoor_windows_gate()
        if record_history:
            self._outdoor_temp_history.append((dt_util.now().isoformat(), value))
            # Issue #540: nat-vent soft-start needs "today's observed peak so far" to
            # detect a past-peak/declining trend. Computed here (single source of truth,
            # same buffer used for forecast high/low correction below) and plumbed to the
            # automation engine the same way _hourly_forecast_temps already is.
            observed_temps = [t for _, t in self._outdoor_temp_history]
            self.automation_engine._outdoor_temp_today_peak = max(observed_temps)
            self.automation_engine._outdoor_temp_today_sample_count = len(observed_temps)
        if getattr(self, "data", None):
            self.data[ATTR_OUTDOOR_TEMP] = value
            self.async_update_listeners()
        if previous is None or abs(value - previous) >= 0.1:
            _LOGGER.info(
                "Outdoor temp updated: %s → %.1f°F",
                f"{previous:.1f}°F" if previous is not None else "unknown",
                value,
            )

    async def _do_startup_coalesce(self) -> bool:
        """Proactively coalesce HVAC and nat-vent state 5 minutes after restart (Issue #321).

        Returns whether this call already invoked ``apply_classification()`` — Issue #591:
        the caller uses this to skip the redundant same-cycle regular-cycle
        ``apply_classification()`` call that otherwise always follows immediately after.
        """
        open_sensors = [s for s in self._resolved_sensors if self._is_sensor_open(s)]
        indoor = self._get_indoor_temp()
        outdoor = self._last_outdoor_temp
        c = self._current_classification

        _LOGGER.info(
            "Startup coalescing: outdoor=%s°F, indoor=%s°F, open_sensors=%s, classification=%s",
            f"{outdoor:.1f}" if outdoor is not None else "?",
            f"{indoor:.1f}" if indoor is not None else "?",
            open_sensors,
            c.day_type if c else "none",
        )

        nat_vent_activated = False
        hvac_commanded = False

        if open_sensors:
            # Issue #523: this call site used to hand-roll its own incomplete copy of the
            # nat-vent gate purely to decide WHETHER to call handle_door_window_open() — a
            # third parallel copy of the threshold logic already consolidated once for
            # #400/#402 (see project_natvent_duplicate_threshold_logic). When that cheap
            # pre-check said "no nat-vent", handle_door_window_open() — the only function
            # that can set _paused_by_door — was never called at all, so an open window at
            # restart could silently fall through to apply_classification() unsuppressed.
            # Delegate unconditionally instead: handle_door_window_open() already runs the
            # full _nat_vent_may_reactivate() gate (fan-mode archetype, aggressive_savings,
            # forecast/floor guards, missing-temp safety) and falls through to the pause
            # branch itself when nat-vent isn't viable — single source of truth.
            first_sensor = open_sensors[0]
            _LOGGER.debug("[coalesce-diag] before handle_door_window_open(%s)", first_sensor)
            await self.automation_engine.handle_door_window_open(first_sensor)
            _LOGGER.debug("[coalesce-diag] after handle_door_window_open(%s)", first_sensor)
            nat_vent_activated = self.automation_engine._natural_vent_active
            _LOGGER.info(
                "Startup coalescing: door/window handling complete for %s — nat_vent_activated=%s, paused_by_door=%s",
                first_sensor,
                nat_vent_activated,
                self.automation_engine._paused_by_door,
            )

        if not nat_vent_activated and c:
            climate_state = self.hass.states.get(self.config.get("climate_entity", ""))
            current_mode = climate_state.state if climate_state else "unknown"
            _LOGGER.info(
                "Startup coalescing: HVAC mode=%s, classification=%s — applying classification",
                current_mode,
                c.hvac_mode,
            )
            indoor_temp = self._get_indoor_temp()
            _LOGGER.debug("[coalesce-diag] before apply_classification() [coalesce path]")
            await self.automation_engine.apply_classification(
                c,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=indoor_temp,
                nat_vent_cutoff=(getattr(self, "_nat_vent_plan", None) or {}).get("nat_vent_cutoff"),
                comfort_floor_crossing_time=(getattr(self, "_nat_vent_plan", None) or {}).get(
                    "comfort_floor_crossing_time"
                ),
            )
            _LOGGER.debug("[coalesce-diag] after apply_classification() [coalesce path]")
            hvac_commanded = True

        # Issue #327: Startup fan reconciliation.
        # Read the physical fan state from the thermostat and let the engine decide whether
        # to adopt it (nat-vent eligible), turn it off (no longer eligible), or leave it alone.
        # Runs AFTER nat-vent + classification so the engine has a settled HVAC state to reconcile
        # against.  The 5-min coalescing window already suppresses override detection in
        # _async_thermostat_changed, so the fan command here won't be misread as a manual override.
        _climate_state_reconcile = self.hass.states.get(self.config.get("climate_entity", ""))
        if _climate_state_reconcile:
            _attrs_reconcile = _climate_state_reconcile.attributes
            _fan_mode_reconcile = _attrs_reconcile.get("fan_mode", "")
            _hvac_action_reconcile = _attrs_reconcile.get("hvac_action", "")
        else:
            _fan_mode_reconcile = "unknown"
            _hvac_action_reconcile = "unknown"
        # Issue #423: archetype-aware — for FAN_MODE_WHOLE_HOUSE this reads the real configured
        # WHF entity's physical state instead of the thermostat's own attributes.
        _thermostat_fan_running = self._derive_thermostat_fan_running_for_reconcile(
            fan_mode_attr=_fan_mode_reconcile,
            hvac_action_attr=_hvac_action_reconcile,
        )
        # Issue #677: live read (not persisted state) of whatever timer token the RF remote
        # entity is still re-announcing at restart — lets reconcile re-arm the remaining
        # grace instead of misreading the eventual natural hardware shutoff as a fresh
        # manual action hours later. See _read_live_remote_timer_provenance()'s docstring.
        _remote_timer_provenance = self._read_live_remote_timer_provenance()
        _LOGGER.debug("[coalesce-diag] before reconcile_fan_on_startup()")
        await self.automation_engine.reconcile_fan_on_startup(
            indoor=indoor,
            outdoor=outdoor,
            thermostat_fan_running=_thermostat_fan_running,
            any_sensor_open=self._any_sensor_open(),
            trigger="ha_restart",
            remote_timer_provenance=_remote_timer_provenance,
        )
        _LOGGER.debug("[coalesce-diag] after reconcile_fan_on_startup()")

        # Issue #707: reconcile_fan_on_startup()'s RF-timer-survives-restart branch
        # (_reconcile_fan_on_startup_locked(), automation.py ~5005-5022) calls
        # handle_fan_manual_override() INTERNALLY when a live remote timer is still
        # valid — that inner call correctly updates production's real override/grace
        # flags via _resolve_override_grace_fsm_state(), but is invisible to
        # _mirror_to_shadow()'s dispatch above, since "reconcile_fan_on_startup" (the
        # outer mirrored method name) isn't a key in _OVERRIDE_GRACE_FSM_EVENT_KINDS —
        # only "handle_fan_manual_override" is, and that key is only ever consulted
        # when _mirror_to_shadow() itself is invoked with that method name, which never
        # happens here since the inner call is a direct synchronous call inside
        # automation.py, not a coordinator-level mirrored call. Left the shadow tracker
        # permanently stuck reporting "none" after every restart with an active RF
        # timer (confirmed live: production=idle/active_protecting_override vs
        # fsm=idle/none, sustained 993s+, 2026-08-20 06:54-07:11).
        #
        # Fix: feed the FSM tracker directly here, gated on the EXACT same condition
        # _reconcile_fan_on_startup_locked() uses to decide whether to call
        # handle_fan_manual_override() (remote_timer_provenance is not None and
        # thermostat_fan_running) — so this only fires when the inner override call
        # actually happened, never on the plain "nothing to reconcile" path. By this
        # point reconcile_fan_on_startup() has already fully returned (including its
        # synchronous inner handle_fan_manual_override() call), so
        # self.automation_engine's live flags are already correct — same
        # read-fresh-from-production pattern _check_orphaned_grace() uses at ~2865-2869.
        if _remote_timer_provenance is not None and _thermostat_fan_running:
            from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

            try:
                self._evaluate_override_grace_fsm(_OGFEventKind.FAN_OVERRIDE_DETECTED)
            except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
                _LOGGER.warning(
                    "Override/grace FSM evaluation (restart-resume RF-timer-driven) failed"
                    " (isolated, no production impact): %s",
                    fsm_exc,
                )

        self._emit_event(
            "startup_coalesced",
            {
                "nat_vent_activated": nat_vent_activated,
                "hvac_commanded": hvac_commanded,
                "sensors_open_count": len(open_sensors),
                "indoor_f": indoor,
                "outdoor_f": outdoor,
                "fan_archetype": self.config.get("fan_mode"),
            },
        )
        self._startup_coalesce_active = False
        _LOGGER.info("Startup coalescing complete — startup grace period ended")
        self.hass.async_create_task(self.async_request_refresh())
        return hvac_commanded

    def _check_orphaned_grace(self) -> None:
        """Self-heal a grace period left active with no override to protect (Issue #508).

        Mirror of the Issue #321 stuck-grace check (grace_end_time in the past while
        grace_active=False) but the opposite shape: here grace_end_time is typically still in
        the future — the timer would fire correctly on its own — but no override (thermostat or
        fan) is currently active to justify keeping it. Defense-in-depth for anything that clears
        an override without going through AutomationEngine.cancel_override() (e.g. an exception
        mid-cancel, or a future third endpoint that bypasses it). Runs every regular update cycle
        (~30s), so any residual inconsistency self-heals within one cycle instead of persisting
        for hours.

        Scoped via ``ae._grace_protects_override`` (Issue #530): only grace periods started for
        a real override (``_start_grace_period(trigger=...)`` with a trigger in
        ``automation._GRACE_TRIGGERS_PROTECTING_OVERRIDE``) can be "orphaned" in the sense this
        check means. Fan-off cooldown, physical-drift-correction, window-close-resume, and
        nat-vent-exit-resume grace never set an override flag in the first place — checking only
        flag-absence (pre-#530) misread every one of those as orphaned and killed them within
        about one event-loop tick of starting, defeating Issue #359's fan-off protection almost
        universally. This does not weaken the original Issue #508 protection: every trigger that
        genuinely represents an override-confirmation grace is still covered.
        """
        ae = self.automation_engine
        if not (
            ae._grace_active
            and getattr(ae, "_grace_protects_override", False)
            and not ae._manual_override_active
            and not ae._fan_override_active
        ):
            return
        _LOGGER.error(
            "Stuck grace detected: grace_active=True but no override is active — force-cancelling grace (Issue #508)."
        )
        _grace_end = ae._grace_end_time
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        # Issue #664: this clears `_grace_active` directly, not via `clear_manual_override()`/
        # `_on_grace_expired()` — confirm was never involved here (Issue #647's own comment:
        # "not via clear_manual_override()"), so only the grace half is dispatched. Closest
        # semantic match is GRACE_TIMER_EXPIRED (grace ending with nothing left to protect).
        ae._cancel_grace_timers_action()
        ae._resolve_override_grace_fsm_state(kind=_OGFEventKind.GRACE_TIMER_EXPIRED)
        try:
            self._evaluate_override_grace_fsm(_OGFEventKind.GRACE_TIMER_EXPIRED)
        except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
            _LOGGER.warning(
                "Override/grace FSM evaluation (orphaned-grace-driven) failed (isolated, no production impact): %s",
                fsm_exc,
            )
        # Issue #679: derive_door_window_lifecycle_state() also takes grace_active as an
        # input; this method used to also re-notify the door/window shadow FSM here
        # (Issue #679's own comment) — that call and the door/window shadow-comparison
        # axis it fed were both removed in Issue #757 Phase 6 Step 4 once door/window's
        # dispatcher became unconditionally FSM-authoritative in production (no shadow
        # replica left to resync).
        self._emit_event(
            "stuck_grace_recovered",
            {"grace_end_time": _grace_end, "reason": "grace_without_override"},
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min).

        Thin wrapper around ``_async_update_data_impl()`` (Issue #480). HA's own
        ``DataUpdateCoordinator`` already catches exceptions raised from this method
        and marks entities unavailable (``last_update_success = False``) — that
        behavior is preserved unchanged; this wrapper does not swallow the
        exception, it only re-raises after recording a durable side-channel.

        Without this, the *fact* and *cause* of a coordinator-wide failure lived
        only in the HA core log tail, which rotates out within days — during the
        Issue #478 investigation the 06:35 crash that took down every
        ``climate_advisor_*`` entity was unrecoverable after the fact for exactly
        this reason. Persisting ``last_update_error``/``last_update_error_time``/
        ``consecutive_failure_count`` via ``_async_save_state()`` means the next
        occurrence survives both an HA restart and log rotation.

        Issue #812: the entire body runs inside ``log_capture.zone_scope()``
        so every ``_LOGGER`` call reached from here (directly, or via any
        awaited coroutine including ``_async_update_data_impl()``) is tagged
        with this coordinator's zone in the log_capture ring buffer. Purely
        additive — no change to control flow, return value, or exception
        propagation below.
        """
        with log_capture.zone_scope(self.zone_label):
            # Defensive defaults: several existing tests partially instantiate the
            # coordinator via object.__new__() (bypassing __init__) and bind only the
            # methods under test. Falling back here (rather than requiring __init__ to
            # have run) keeps this wrapper safe under that established test pattern
            # without needing to touch every such test file.
            if not hasattr(self, "consecutive_failure_count"):
                self.consecutive_failure_count = 0
            if not hasattr(self, "last_update_error"):
                self.last_update_error = None
            if not hasattr(self, "last_update_error_time"):
                self.last_update_error_time = None

            try:
                result = await self._async_update_data_impl()
            except Exception as err:
                self.consecutive_failure_count += 1
                self.last_update_error = f"{type(err).__name__}: {err}"
                self.last_update_error_time = dt_util.now().isoformat()
                _LOGGER.error(
                    "Coordinator update failed (consecutive_failure_count=%d): %s",
                    self.consecutive_failure_count,
                    self.last_update_error,
                )
                with contextlib.suppress(Exception):
                    await self._async_save_state()
                raise
            else:
                if self.consecutive_failure_count or self.last_update_error:
                    _LOGGER.info(
                        "Coordinator update recovered after %d consecutive failure(s); clearing last_update_error",
                        self.consecutive_failure_count,
                    )
                    self.consecutive_failure_count = 0
                    self.last_update_error = None
                    self.last_update_error_time = None
                    with contextlib.suppress(Exception):
                        await self._async_save_state()
                return result

    async def _async_update_data_impl(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min)."""
        # Re-resolve group membership in case it changed
        _LOGGER.debug("[coalesce-diag] _async_update_data: enter")
        new_resolved = self._resolve_monitored_sensors()
        if set(new_resolved) != set(self._resolved_sensors):
            _LOGGER.info("Door/window sensor membership changed; updating listeners")
            self._unsubscribe_door_window_listeners()
            self._resolved_sensors = new_resolved
            self._subscribe_door_window_listeners()

        _LOGGER.debug("[coalesce-diag] _async_update_data: before _get_forecast()")
        forecast = await self._get_forecast()
        _LOGGER.debug("[coalesce-diag] _async_update_data: after _get_forecast() — forecast=%s", forecast is not None)
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        self.automation_engine._hourly_forecast_temps = self._hourly_forecast_temps
        if forecast:
            prev_type = self._current_classification.day_type if self._current_classification else None
            _thresh = {
                "threshold_hot": self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
                "threshold_warm": self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
                "threshold_mild": self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
                "threshold_cool": self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
            }
            self._current_classification = classify_day(forecast, previous_day_type=prev_type, **_thresh)
            self._classification_fetched_at = dt_util.now()
            # Issue #602: ensure today's DailyRecord exists on this already-resilient,
            # every-30-min classification path — not only the once-daily briefing_time
            # trigger, which has no retry and previously left setpoint-override detection
            # (and every other _today_record-gated bookkeeping counter) silently dark for
            # the rest of the day whenever weather was unavailable at that one moment.
            self._ensure_today_record(self._current_classification)
            # record_history=True here; the 5-min tick (Issue #511) always passes False
            # so _outdoor_temp_history keeps its existing 30-min cadence.
            self._apply_outdoor_temp(forecast.current_outdoor_temp, record_history=True)

            # Chart log: emit classification_change event when day type changes
            if prev_type is not None and prev_type != self._current_classification.day_type:
                with contextlib.suppress(Exception):
                    _chart_hvac_cc = self._read_chart_hvac_action()
                    # Issue #510 0.4: compute once, reuse below — avoids duplicate
                    # _compute_fan_status() calls (and duplicate WARNING logs) for the same instant.
                    _fan_status_cc = self._compute_fan_status() if self.automation_engine else "disabled"
                    _LOGGER.debug(
                        "chart_log append: event=classification_change hvac=%r fan=%s",
                        _chart_hvac_cc,
                        self._fan_is_running(_fan_status_cc) if self.automation_engine else False,
                    )
                    _band_lower_cc, _band_upper_cc = self._target_band_lower_upper_now()
                    self._chart_log.append(
                        hvac=_chart_hvac_cc,
                        fan=self._fan_is_running(_fan_status_cc) if self.automation_engine else False,
                        indoor=forecast.current_indoor_temp,
                        outdoor=forecast.current_outdoor_temp,
                        windows_open=self._any_sensor_open(),
                        windows_recommended=bool(self._current_classification.windows_recommended),
                        setpoint=self._read_chart_setpoint(),
                        event="classification_change",
                        fan_running=self._fan_physically_running(_fan_status_cc) if self.automation_engine else False,
                        nat_vent_active=bool(
                            self.automation_engine._natural_vent_active if self.automation_engine else False
                        ),
                        lower=_band_lower_cc,
                        upper=_band_upper_cc,
                        nat_vent_target=self._nat_vent_target_now(),
                    )

            # Startup safety: on first run, skip override detection — coalescing window handles it (Issue #321)
            if self._first_run:
                self._first_run = False
                # Recover v3 pending_observations that survived restart
                _pending_obs = self.learning._state.pending_observations
                if isinstance(_pending_obs, dict):
                    for _obs_type, _obs in list(_pending_obs.items()):
                        if not isinstance(_obs, dict):
                            continue
                        if _obs.get("_legacy_event"):
                            # Legacy HVAC event migrated into pending_observations — use HVAC commit path
                            session_mode = _obs.get("session_mode") or _obs.get("hvac_mode") or "heat"
                            _obs["session_mode"] = session_mode
                            self._pending_observations[_obs_type] = _obs
                        else:
                            # Bug 2 fix: For HVAC obs, check the right sample list based
                            # on the current phase.  Pre-fix obs had 'samples': [] which
                            # shadowed active_samples in the generic fallback, causing all
                            # HVAC observations to be discarded on every HA restart.
                            _hvac_types_sr = {OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL}
                            if _obs_type in _hvac_types_sr:
                                _phase_sr = _obs.get("_phase", "active")
                                if _phase_sr == "post_heat":
                                    samples = _obs.get("post_heat_samples", [])
                                    min_s = THERMAL_MIN_POST_HEAT_SAMPLES
                                else:
                                    # Active phase: any sample is worth recovering so
                                    # post-heat observation window can continue after restart.
                                    samples = _obs.get("active_samples", [])
                                    # Fall back to generic 'samples' key for pre-fix persisted obs
                                    if not samples:
                                        samples = _obs.get("samples", [])
                                    min_s = 1
                            else:
                                samples = _obs.get("samples", _obs.get("active_samples", []))
                                min_s = {
                                    OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_MIN_SAMPLES,
                                    OBS_TYPE_VENT_WINDOW_DECAY: THERMAL_VENT_MIN_SAMPLES,
                                    OBS_TYPE_VENT_FAN_DECAY: THERMAL_VENT_MIN_SAMPLES,
                                    OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_MIN_SAMPLES,
                                }.get(_obs_type, 10)
                            if len(samples) >= min_s:
                                self._pending_observations[_obs_type] = _obs
                                _LOGGER.info(
                                    "Startup: recovered v3 observation type=%s obs_id=%s samples=%d phase=%s",
                                    _obs_type,
                                    _obs.get("obs_id", "?"),
                                    len(samples),
                                    _obs.get("_phase", "active"),
                                )

                # Chart_log endpoint estimator backfill (Issue #137): run once on first startup
                # after the new code is deployed. The chart_log is loaded in __init__ so entries
                # are already available. Flags survive restart so backfill runs exactly once.
                if self.config.get("learning_enabled", True):
                    if not self._passive_k_backfilled:
                        self._run_passive_chart_log_fit(backfill=True)
                        self._passive_k_backfilled = True
                        _LOGGER.info("chart_log_endpoint: passive k_passive backfill complete")
                    if not self._vent_window_k_backfilled:
                        self._run_vent_window_chart_log_fit(backfill=True)
                        self._vent_window_k_backfilled = True
                        _LOGGER.info("chart_log_endpoint: vent_window k_vent_window backfill complete")
                    if not self._vent_fan_k_backfilled:
                        self._run_vent_fan_chart_log_fit(backfill=True)
                        self._vent_fan_k_backfilled = True
                        _LOGGER.info("chart_log_endpoint: vent_fan k_vent_fan backfill complete (Issue #587)")
                    if not self._passive_k_backfill_v2:
                        self._run_passive_chart_log_fit(backfill=True)
                        self._passive_k_backfill_v2 = True
                        _LOGGER.info("chart_log_endpoint v2: passive k_passive dual-estimator backfill complete")
                    if not self._vent_window_k_backfill_v2:
                        self._run_vent_window_chart_log_fit(backfill=True)
                        self._vent_window_k_backfill_v2 = True
                        _LOGGER.info(
                            "chart_log_endpoint v2: vent_window k_vent_window dual-estimator backfill complete"
                        )
                    if not self._vent_fan_k_backfill_v2:
                        self._run_vent_fan_chart_log_fit(backfill=True)
                        self._vent_fan_k_backfill_v2 = True
                        _LOGGER.info(
                            "chart_log_endpoint v2: vent_fan k_vent_fan dual-estimator backfill complete (Issue #587)"
                        )
                    if not self._solar_phase_backfill:
                        self._run_solar_phase_chart_log_fit(backfill=True)
                        self._solar_phase_backfill = True
                        self._last_solar_phase_fit_date = dt_util.now().date()
                        _LOGGER.info("chart_log solar_phase: phase offset backfill complete")
                    if not self._solar_phase_ac_backfill:
                        self._run_ac_duty_solar_phase_fit()
                        # Flag is set inside the method after completion

            # Bug 1 (Issue #321): Startup coalescing — evaluate state at t+5min instead of detecting override at t+30s
            _LOGGER.debug(
                "[coalesce-diag] coalesce condition check: startup_timer_fired=%s"
                " startup_coalesce_active=%s current_classification=%s",
                self._startup_timer_fired,
                self._startup_coalesce_active,
                self._current_classification is not None,
            )
            # Issue #591: track whether _do_startup_coalesce() already ran apply_classification()
            # this cycle, so the unconditional regular-cycle call below can skip it — closing the
            # most direct duplicate-call path (coalesce path + regular-cycle path, same invocation).
            _coalesce_already_classified = False
            if self._startup_timer_fired and self._startup_coalesce_active and self._current_classification:
                _LOGGER.debug("[coalesce-diag] before _do_startup_coalesce()")
                _coalesce_already_classified = await self._do_startup_coalesce()
                _LOGGER.debug("[coalesce-diag] after _do_startup_coalesce()")

            # Periodic daily solar phase re-fit (Issue #310): run once per calendar day
            # using only the last 2 days of chart_log (backfill=False). Stamping
            # _last_solar_phase_fit_date in the one-shot block above prevents a double-fit
            # on the same cycle when a fresh install runs both blocks back-to-back.
            if self.config.get("learning_enabled", True):
                self._maybe_run_periodic_solar_phase_fit()

            # Refresh the thermal model on every 30-min cycle, not just at the daily
            # briefing. get_thermal_model() is a pure computation (no I/O), so calling it
            # 48×/day is negligible. Refreshing here:
            #   1. Restores the model after HA restart (daily briefing is the only other
            #      writer, so _thermal_model is {} for the rest of the day after a restart)
            #   2. Keeps thermal_equilibrium_f current as outdoor_temp and solar_factor
            #      change through the day (6 AM conditions are wrong by afternoon)
            #   3. Applies mid-day observation commits to same-day automation decisions
            if self.config.get("learning_enabled", True) and self.automation_engine:
                self.automation_engine._thermal_model = self.learning.get_thermal_model(
                    outdoor_temp_f=self._last_outdoor_temp,
                    solar_factor=_solar_factor(dt_util.now().hour),
                )
                self._solar_phase_offset = (
                    self.automation_engine._thermal_model.get("solar_phase_offset_h")
                    or THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
                )
                _LOGGER.debug(
                    "thermal model refreshed (30-min cycle): confidence=%s k_passive=%s solar_phase_offset=%.1f",
                    self.automation_engine._thermal_model.get("confidence", "none"),
                    self.automation_engine._thermal_model.get("k_passive"),
                    self._solar_phase_offset,
                )

            # Issue #786: resolve TOU schedule state before the ODE prediction executor
            # call below, which computes its own internal band (see
            # _resolve_tou_schedule_state()'s docstring on why the ordering matters).
            self._resolve_tou_schedule_state()
            # Issue #514: resolve and cache this cycle's target-band schedule (same
            # timing reason as above) so the executor call below reuses the canonical
            # band instead of falling through to its own divergent internal recompute.
            self._resolve_target_band_schedule()

            # Compute and cache ODE prediction for ceiling guard + chart reuse.
            # Offloaded to executor — ODE integration + OLS math blocks the event loop otherwise.
            self._last_predicted_indoor = await self._executor_job(
                functools.partial(
                    _build_predicted_indoor_future,
                    self._hourly_forecast_temps,
                    self.config,
                    dt_util.now(),
                    current_indoor_temp=self._get_indoor_temp(),
                    thermal_model=self.automation_engine._thermal_model if self.automation_engine else {},
                    occupancy_mode=self._occupancy_mode,
                    classification=self._current_classification,
                    band_schedule=self._target_band_schedule,
                    tou_precondition_window=self._tou_precondition_window_tuple(),
                )
            )
            _LOGGER.debug(
                "Caching predicted indoor curve: %d points, [0]=%s",
                len(self._last_predicted_indoor),
                f"{self._last_predicted_indoor[0]['temp']:.1f}°F" if self._last_predicted_indoor else "none",
            )
            self._compute_and_cache_nat_vent_plan()

            # Populate first-write-wins prediction archive (PRED_ARCHIVE_HORIZON_HOURS lookahead).
            # setdefault ensures the earliest (most advance) prediction is kept per 30-min slot.
            _archive_cutoff = dt_util.now() + timedelta(hours=PRED_ARCHIVE_HORIZON_HOURS)
            for _ae in self._last_predicted_indoor:
                try:
                    _ae_dt = datetime.fromisoformat(_ae["ts"])
                except (ValueError, KeyError):
                    continue
                if _ae_dt > _archive_cutoff:
                    break
                self._pred_archive.setdefault(self._pred_archive_key(_ae_dt), _ae["temp"])

            # Bug 2 (Issue #321): Detect stuck grace — override active but timer callback
            # never fired. Force-clear so automation resumes on this cycle.
            _ae = self.automation_engine
            if _ae._manual_override_active and not _ae._grace_active and _ae._grace_end_time is not None:
                _stuck_end = dt_util.parse_datetime(_ae._grace_end_time)
                if _stuck_end is not None and dt_util.now() > _stuck_end:
                    _LOGGER.error(
                        "Stuck grace detected: manual_override_active=True but grace_end_time"
                        " %s is in the past and no grace timer is active. Force-clearing"
                        " override (Issue #321).",
                        _ae._grace_end_time,
                    )
                    _stale_mode = _ae._manual_override_mode
                    _stale_since = _ae._manual_override_time
                    _ae.clear_manual_override(reason="stuck_grace_recovery")
                    self._feed_override_grace_fsm_cancelled()
                    self._emit_event(
                        "stuck_grace_recovered",
                        {
                            "grace_end_time": _ae._grace_end_time,
                            "stale_mode": _stale_mode,
                            "stale_since": _stale_since,
                        },
                    )

            self._check_orphaned_grace()

            if _coalesce_already_classified:
                _LOGGER.debug(
                    "[coalesce-diag] skipping apply_classification() [regular cycle path] —"
                    " _do_startup_coalesce() already applied classification this cycle (Issue #591)"
                )
            else:
                _LOGGER.debug("[coalesce-diag] before apply_classification() [regular cycle path]")
                await self.automation_engine.apply_classification(
                    self._current_classification,
                    predicted_indoor=self._last_predicted_indoor,
                    indoor_temp=self._get_indoor_temp(),
                    nat_vent_cutoff=(getattr(self, "_nat_vent_plan", None) or {}).get("nat_vent_cutoff"),
                )
                _LOGGER.debug("[coalesce-diag] after apply_classification() [regular cycle path]")

            # Issue #786: TOU scheduler — resolve cost-period schedule state and, if a
            # pre-conditioning window is active, drive the banking setpoint. Runs after
            # apply_classification() regardless of which branch above ran it, so
            # self._current_classification is always fresh.
            await self._apply_tou_schedule()

            if self._maybe_regenerate_briefing_for_drift():
                await self._async_save_state()

            # Reset startup retry state on success
            if self._startup_retries_remaining < 5:
                _LOGGER.info(
                    "Weather entity now available; classified as %s day",
                    self._current_classification.day_type,
                )
                self._startup_retries_remaining = 5
                self._startup_retry_delay = 30

            # Outdoor temp history + automation-engine mirror now handled by
            # _apply_outdoor_temp() above (Issue #511 consolidation).
            now_str = dt_util.now().isoformat()
            if forecast.current_indoor_temp is not None:
                self._indoor_temp_history.append((now_str, forecast.current_indoor_temp))

                # Track comfort violations (elapsed minutes since last check, capped at 30)
                if self._today_record:
                    comfort_low = self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT)
                    comfort_high = self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)
                    now = dt_util.now()
                    if self._last_violation_check is not None:
                        elapsed_minutes = min((now - self._last_violation_check).total_seconds() / 60, 30.0)
                    else:
                        elapsed_minutes = 30.0
                    self._last_violation_check = now
                    if (
                        forecast.current_indoor_temp < comfort_low or forecast.current_indoor_temp > comfort_high
                    ) and not self._is_nat_vent_tolerated_deviation(
                        forecast.current_indoor_temp, comfort_low, comfort_high
                    ):
                        self._today_record.comfort_violations_minutes += elapsed_minutes

            # Check economizer opportunity (window cooling on hot days)
            if self._should_run_regular_cycle_window_cooling_check():
                windows_open = self._today_record.windows_physically_opened and (
                    self._today_record.window_physical_close_time is None
                )
                await self.automation_engine.check_window_cooling_opportunity(
                    forecast.current_outdoor_temp,
                    forecast.current_indoor_temp,
                    windows_open,
                    current_hour=dt_util.now().hour,
                )

            # Re-evaluate natural vent conditions while any sensor is open
            if self._should_run_regular_cycle_nat_vent_check():
                _LOGGER.debug("[coalesce-diag] before check_natural_vent_conditions()")
                await self.automation_engine.check_natural_vent_conditions()
                _LOGGER.debug("[coalesce-diag] after check_natural_vent_conditions()")

            # Save state after classification update
            _LOGGER.debug("[coalesce-diag] before _async_save_state()")
            await self._async_save_state()
            _LOGGER.debug("[coalesce-diag] after _async_save_state() — _async_update_data exiting normally")
        else:
            # Weather entity not ready yet (common after HA restart).
            # Retry with gentle backoff: 30s → 60s → 120s → 240s → 480s
            # Total wait ≈ 15 min before falling back to normal 30-min poll.
            if self._startup_retries_remaining > 0:
                delay = self._startup_retry_delay
                self._startup_retries_remaining -= 1
                self._startup_retry_delay = min(delay * 2, 480)
                _LOGGER.warning(
                    "Weather entity not ready; retry %d remaining in %ds",
                    self._startup_retries_remaining + 1,
                    delay,
                )

                @callback
                def _schedule_retry(_now: Any) -> None:
                    self.hass.async_create_task(self.async_request_refresh())

                async_call_later(self.hass, delay, _schedule_retry)
            else:
                _LOGGER.warning(
                    "Weather entity still unavailable after startup retries; will try again at next scheduled update"
                )

        # Build the data dict that sensors will read
        c = self._current_classification
        suggestions = self.learning.generate_suggestions()
        compliance = self.learning.get_compliance_summary()

        # HVAC action (compressor/fan actual operation state) and today's runtime
        _climate_entity_id = self.config.get("climate_entity", "")
        _cs = self.hass.states.get(_climate_entity_id) if _climate_entity_id else None
        hvac_action = _cs.attributes.get("hvac_action", "") if _cs else ""
        hvac_mode = _cs.state if _cs else ""
        # Issue #835: track last-heating/last-cooling timestamps for the
        # hvac_fan_restrict_mode guard in _activate_fan() — the only per-cycle
        # ground-truth read of hvac_action, so this piggybacks on it rather than
        # polling separately.
        if self.automation_engine:
            _hvac_action_lower = str(hvac_action).lower()
            if _hvac_action_lower == "heating":
                self.automation_engine._last_hvac_heating_active = dt_util.now().isoformat()
            elif _hvac_action_lower == "cooling":
                self.automation_engine._last_hvac_cooling_active = dt_util.now().isoformat()
            # Issue #843: same piggyback pattern, extended to fan/nat-vent activity
            # for the comfort-family FSM's recency-gated deadband.
            if self.automation_engine._fan_active:
                self.automation_engine._last_fan_active = dt_util.now().isoformat()
            if self.automation_engine._natural_vent_active:
                self.automation_engine._last_natvent_active = dt_util.now().isoformat()
        # Issue #466: setpoint fields, so consumers that don't need live sub-cycle
        # freshness (ai_skills_activity.py/ai_skills_context.py) can read from
        # coordinator.data instead of independently re-fetching hass.states.get().
        # api.py's own status endpoint deliberately keeps its own live read — it
        # powers the ca_target_heat/cool divergence check (#402/#462), which exists
        # to compare CA's computed target against the REAL thermostat right now, not
        # against a snapshot that can be up to ~30 min old.
        _target_temp = _cs.attributes.get("temperature") if _cs else None
        _target_temp_low = _cs.attributes.get("target_temp_low") if _cs else None
        _target_temp_high = _cs.attributes.get("target_temp_high") if _cs else None

        # Issue #96 Root Cause D: Late-start thermal session for HVAC running at HA startup.
        # _hvac_on_since is only set via state transitions in _async_thermostat_changed.
        # If HA restarts mid-HVAC-session, no transition fires and thermal obs are skipped.
        if (
            _cs is not None
            and str(hvac_action).lower() in {"heating", "cooling"}
            and self._hvac_on_since is None
            and not self._startup_hvac_initialized
        ):
            self._startup_hvac_initialized = True
            await self._initialize_hvac_session_from_current_state(_cs)

        # Issue #510 0.4: compute once, reuse below (including at the untracked-fan check
        # further down) — avoids repeated duplicate _compute_fan_status() calls (and duplicate
        # WARNING logs) for the same instant within a single update cycle. Independent of
        # hvac_mode/hvac_action, so safe to compute unconditionally here.
        _fan_status_uc = self._compute_fan_status()

        # Issue #749: hard-invariant watchdog. Deliberately reads ground truth directly
        # (hvac_action above, _get_fan_physical_state() below) rather than _fan_status_uc —
        # that computed status blends in CA's own session/override bookkeeping, which is
        # exactly what stayed self-consistent while reality diverged during the 2026-08-22
        # incident (#739/#748) this module exists to catch.
        _invariant_violations = self._run_invariant_watchdog(hvac_action=hvac_action)

        # Issue #805: generic entity-availability sweep. Detects a removed/unavailable
        # configured entity (thermostat, weather source, sensors, fan, toggles, notify
        # service) that would otherwise degrade silently at every one of its own read
        # sites. Returns transition-debounced issues and fires at most one notification
        # per outage-start (plus a daily reminder) — see _run_entity_health_check().
        _entity_health_issues = self._run_entity_health_check()

        # Emit a structured warning event when the HVAC entity reports an active action
        # (heating/cooling/fan) while hvac_mode is "off".  This surfaces the contradiction
        # in the investigator event log so it is not invisible outside the AI narrative.
        # Suppress when Climate Advisor itself activated fan-only mode (natural ventilation).
        _active_hvac_actions = {"heating", "cooling", "fan"}
        if hvac_mode == "off" and str(hvac_action).lower() in _active_hvac_actions:
            # Suppress when CA's own fan activity explains the reading (Issue #458 —
            # is_ca_fan_running() is the single source of truth for this, consolidating
            # what was a separate ad hoc flag check here). _natural_vent_active is kept
            # as an explicit extra condition — it covers the nat-vent-armed-but-idle
            # moment between cycles, a distinct state _compute_fan_status() reports as
            # "nat-vent (session active, fan idle)" (not one of the four active values,
            # since the physical fan genuinely isn't running then).
            #
            # Consolidating onto is_ca_fan_running() also fixes a second latent gap this
            # issue found: previously a manual fan override confirmed running via physical
            # state (ae._fan_override_active=True, ae._fan_active=False) was NOT suppressed
            # here, even though ai_skills_activity.py's independent check already treated
            # "running (manual override)" as expected — the two sites had silently
            # disagreed on this case.
            _ca_fan_running = self.automation_engine._natural_vent_active or is_ca_fan_running(_fan_status_uc)
            _is_expected_fan = str(hvac_action).lower() == "fan" and _ca_fan_running
            # Issue #591: migrated onto the shared AutomationEngine._recent_duplicate() helper,
            # keeping the original 30-minute window (event-only, no side-effecting action here —
            # Issue #591/#590 Finding C). Short-circuits before _recent_duplicate() when
            # _is_expected_fan is True so the expected-fan case never records a signature —
            # otherwise a real contradiction right after an expected-fan window ends could be
            # wrongly suppressed as "already recorded."
            _contradiction_sig = (hvac_mode, str(hvac_action).lower())
            if not _is_expected_fan and not self.automation_engine._recent_duplicate(
                "state_contradiction_warning", _contradiction_sig, window_seconds=1800
            ):
                self._emit_event(
                    "state_contradiction_warning",
                    {"hvac_mode": hvac_mode, "hvac_action": hvac_action},
                )

        # Issue #331 follow-up: surface an UNTRACKED fan (the thermostat running its own
        # blower/fan that CA did not command) in the event log so it is not invisible.
        # Deduped entry/exit: emit once when the fan enters the untracked-running state and
        # once when it clears — never per cooling-cycle. Classify the inferred source.
        _is_untracked = _fan_status_uc == "running (untracked)"
        _untracked_logged = getattr(self, "_untracked_fan_active", False)
        if _is_untracked and not _untracked_logged:
            _cs2 = self.hass.states.get(self.config.get("climate_entity", ""))
            _t_mode = _cs2.state if _cs2 else "unknown"
            _t_action = str(_cs2.attributes.get("hvac_action", "")) if _cs2 else ""
            _t_fan = str(_cs2.attributes.get("fan_mode", "")) if _cs2 else ""
            _source = (
                f"thermostat blower during {_t_mode} cycle"
                if _t_mode in ("cool", "heat", "heat_cool")
                else "thermostat fan schedule/circulation"
            )
            _fan_mode_val = self.automation_engine.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _fan_device = (
                "whf"
                if _fan_mode_val == FAN_MODE_WHOLE_HOUSE
                else "hvac_fan"
                if _fan_mode_val == FAN_MODE_HVAC
                else "both"
                if _fan_mode_val == FAN_MODE_BOTH
                else "none"
            )
            self._emit_event(
                "fan_running_untracked",
                {
                    "hvac_action": _t_action,
                    "fan_mode": _t_fan,
                    "thermostat_mode": _t_mode,
                    "source": _source,
                    "fan_device": _fan_device,
                },
            )
            self._untracked_fan_active = True
        elif not _is_untracked and _untracked_logged and not is_ca_fan_running(_fan_status_uc):
            # Genuinely stopped (or disabled) — not just reclassified to a different active
            # status (e.g. an override/reconcile now owning the same still-running fan).
            _fan_mode_clr = self.automation_engine.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _fan_device_clr = (
                "whf"
                if _fan_mode_clr == FAN_MODE_WHOLE_HOUSE
                else "hvac_fan"
                if _fan_mode_clr == FAN_MODE_HVAC
                else "both"
                if _fan_mode_clr == FAN_MODE_BOTH
                else "none"
            )
            self._emit_event("fan_untracked_cleared", {"fan_device": _fan_device_clr})
            self._untracked_fan_active = False
        elif not _is_untracked and _untracked_logged:
            # Issue #774: the fan is still running — _compute_fan_status() just started
            # explaining it via a different active status (override, active, nat-vent) in
            # this same cycle. That is a reclassification, not a stop; don't emit a
            # misleading "Fan stopped" event for it, but do clear our own tracking flag
            # since it is no longer "untracked" by any definition.
            self._untracked_fan_active = False

        # Issue #359 Fix D: periodic backstop — reconcile an untracked fan at each 30-min cycle.
        # The one-shot trigger in _async_thermostat_changed (~line 2826) is guarded by
        # not _fan_override_active, but that flag may already be True from Block 3 in the same
        # event, leaving the untracked fan permanently unresolved.  This backstop catches it.
        #
        # Issue #571: this backstop no longer fires in the few seconds right after CA's own
        # nat-vent exit commands the fan off — _is_untracked is derived from
        # _compute_fan_status(), and that function's ground-truth fallbacks now route through
        # resolve_untracked_fan_status(), which returns "inactive" (not "running (untracked)")
        # while a very recent CA-issued off-command's propagation to the physical entity is
        # still pending. Previously every legitimate exit briefly looked externally-owned to
        # this backstop, triggering a spurious reconcile_fan_on_startup() adopt/re-exit right
        # after the real exit already happened.
        #
        # Issue #510 0.2: this backstop now ALSO catches the mirror-direction drift bug
        # (a stale _natural_vent_active flag masking a physically-running WHF) with zero
        # additional code — _is_untracked is derived from _compute_fan_status(), and 0.1b's
        # fix in that function makes the nat-vent-stale case resolve to "running (untracked)"
        # too (previously it resolved to "nat-vent (session active, fan idle)", which this
        # backstop's `_is_untracked` check never matched). A separate 2-tick-confirm pure
        # decision function was drafted for this direction and then deliberately discarded
        # once this was discovered — reusing this already-shipped, already-tested mechanism
        # is strictly simpler and lower-risk than adding a parallel one that does the same
        # job. The ~30-min cadence here (vs. the primary direction's ~10-min 2-tick confirm)
        # is acceptable for this direction: it's an automation-ownership bookkeeping concern,
        # not a display concern (0.1a/0.1b already fix display immediately and independently).
        # Issue #627: gate this backstop behind the startup-coalescing window, the same
        # idiom every sibling override-detection check in this file already uses (see the
        # "Startup coalescing active — suppressing X detection" log lines below). Without
        # this gate, this backstop fires on the very first _async_update_data() cycle after
        # restart — before restore_state()'s Issue #263/#327 clean-slate settle window has
        # had any chance to elapse — and its only gate (_fan_override_active) is exactly the
        # flag that clean-slate just wiped to False. That let it misread a whole-house fan
        # still legitimately running under a pre-restart RF-remote timer as "unwarranted,"
        # turn it off, and release _pre_fan_hvac_mode (the flag _whf_owns_hvac() depends on)
        # — which then let apply_classification() commit the thermostat to Cool mode moments
        # later with nothing left to stop it (a real AC/whole-house-fan mutex violation). The
        # premature correction also armed the 5-minute correction cooldown, silently
        # suppressing the properly-designed _do_startup_coalesce() -> reconcile_fan_on_startup
        # (trigger="ha_restart") call at the real 300s coalescing boundary from ever
        # re-evaluating. Delaying this backstop until the window closes costs nothing in
        # steady-state operation (_startup_coalesce_active is False the rest of the time) and
        # lets the intended ha_restart-triggered reconcile make the first real decision.
        if self._should_run_untracked_fan_backstop(_is_untracked):
            _LOGGER.info("Fan running untracked with no active override/grace — triggering periodic reconciliation")
            _cs_bst = self.hass.states.get(self.config.get("climate_entity", ""))
            _bst_fan_mode = str(_cs_bst.attributes.get("fan_mode", "")) if _cs_bst else ""
            _bst_hvac_action = str(_cs_bst.attributes.get("hvac_action", "")).lower() if _cs_bst else ""
            if _bst_hvac_action not in ("heating", "cooling"):
                await self.automation_engine.reconcile_fan_on_startup(
                    indoor=self._get_indoor_temp(),
                    outdoor=self._last_outdoor_temp,
                    # Issue #423: archetype-aware — WHF mode checks the real fan entity's
                    # physical state instead of always trusting the thermostat's attributes.
                    thermostat_fan_running=self._derive_thermostat_fan_running_for_reconcile(
                        fan_mode_attr=_bst_fan_mode,
                        hvac_action_attr=_bst_hvac_action,
                    ),
                    any_sensor_open=self._any_sensor_open(),
                    trigger="backstop_30min",
                )
            else:
                _LOGGER.warning(
                    "Periodic reconciliation skipped: HVAC actively %s — fan is thermostat blower",
                    _bst_hvac_action,
                )
        elif _is_untracked and self._startup_coalesce_active:
            _LOGGER.debug("Startup coalescing active — suppressing untracked-fan backstop reconciliation")

        # Issue #361: command-only fan reconciliation (fan_state_feedback=False).
        # When the fan entity only echoes the last command, we cannot detect physical overrides
        # via state changes.  Instead, assert the desired state idempotently each 30-min cycle.
        _ae_cmd = self.automation_engine
        if not self._fan_state_feedback_enabled() and _ae_cmd is not None:
            _fan_mode_cmd = self.config.get(CONF_FAN_MODE, "")
            if _fan_mode_cmd not in ("", "none", None, FAN_MODE_DISABLED):
                _desired_on = bool(_ae_cmd._fan_active)
                _grace_on = bool(_ae_cmd._grace_active)
                _override_on = bool(_ae_cmd._fan_override_active)
                _last_cmd = self._last_commanded_fan_state
                if _desired_on and _last_cmd is not True and not _grace_on and not _override_on:
                    _LOGGER.info(
                        "Fan command-only assert: desired=on last_commanded=%s → issuing on command (fan_entity=%s)",
                        _last_cmd,
                        self.config.get(CONF_FAN_ENTITY, ""),
                    )
                    if await self._async_command_fan_entity(on=True):
                        self._last_commanded_fan_state = True
                elif not _desired_on and _last_cmd is not False and not _grace_on and not _override_on:
                    _LOGGER.info(
                        "Fan command-only assert: desired=off last_commanded=%s → issuing off command (fan_entity=%s)",
                        _last_cmd,
                        self.config.get(CONF_FAN_ENTITY, ""),
                    )
                    if await self._async_command_fan_entity(on=False):
                        self._last_commanded_fan_state = False
                else:
                    _LOGGER.debug(
                        "Fan command-only assert: desired=%s last_commanded=%s — no command needed",
                        "on" if _desired_on else "off",
                        _last_cmd,
                    )

        hvac_runtime_today = self.get_hvac_runtime_today()

        # --- Thermal observation pipeline sampling ---
        self._update_pre_heat_buffer()
        self._sample_all_observations()
        if hasattr(self, "_pending_observations") and self._pending_observations:
            _LOGGER.info(
                "Thermal pipeline: %d pending observations active",
                len(self._pending_observations),
            )
        for _hvac_obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
            await self._check_hvac_stabilization(_hvac_obs_type)

        # --- Temperatures for coordinator.data (sensor entities + AI context) ---
        _indoor_temp = self._get_indoor_temp()
        _outdoor_temp = forecast.current_outdoor_temp if forecast else None

        # Schedule overnight pre-cool if a warming trend is active (idempotent — runs once per day)
        self._maybe_schedule_pre_cool()

        next_auto = self._compute_next_automation_action(c)
        fan_running = self.automation_engine._fan_active
        result = {
            ATTR_DAY_TYPE: c.day_type if c else "unknown",
            ATTR_TREND: c.trend_direction if c else "unknown",
            ATTR_TREND_MAGNITUDE: c.trend_magnitude if c else 0,
            ATTR_BRIEFING: self._last_briefing,
            ATTR_BRIEFING_SHORT: self._last_briefing_short,
            ATTR_NEXT_ACTION: self._compute_next_action(
                c,
                indoor_temp=_indoor_temp,
                outdoor_temp=_outdoor_temp,
                windows_physically_open=self._any_sensor_open(),
                ae=self.automation_engine,
            ),
            ATTR_AUTOMATION_STATUS: self._compute_automation_status(),
            ATTR_LEARNING_SUGGESTIONS: suggestions,
            ATTR_COMPLIANCE_SCORE: compliance.get("comfort_score", 1.0),
            ATTR_NEXT_AUTOMATION_ACTION: next_auto[0],
            ATTR_NEXT_AUTOMATION_TIME: next_auto[1],
            ATTR_OCCUPANCY_MODE: self._occupancy_mode,
            ATTR_LAST_ACTION_TIME: self.automation_engine._last_action_time,
            ATTR_LAST_ACTION_REASON: self.automation_engine._last_action_reason,
            ATTR_FAN_STATUS: self._compute_fan_status(),
            ATTR_WHF_STATUS: self._compute_whf_status(),
            ATTR_HVAC_FAN_STATUS: self._compute_hvac_fan_status(),
            ATTR_FAN_RUNTIME: self.automation_engine._get_fan_runtime_minutes(),
            ATTR_FAN_OVERRIDE_SINCE: self.automation_engine._fan_override_time,
            ATTR_FAN_RUNNING: fan_running,
            ATTR_HVAC_ACTION: hvac_action,
            "invariant_violations": [{"invariant": v.name, "detail": v.detail} for v in _invariant_violations],
            "entity_health_issues": [
                {
                    "config_key": i.config_key,
                    "entity_id": i.entity_id,
                    "friendly_name": i.friendly_name,
                    "criticality": i.criticality,
                    "status": i.status,
                }
                for i in _entity_health_issues
            ],
            "hvac_mode": hvac_mode,
            "target_temp": _target_temp,
            "target_temp_low": _target_temp_low,
            "target_temp_high": _target_temp_high,
            ATTR_HVAC_RUNTIME_TODAY: hvac_runtime_today,
            ATTR_CONTACT_STATUS: self._compute_contact_status(),
            ATTR_AI_STATUS: self.claude_client.get_status()["status"] if self.claude_client else "disabled",
            ATTR_INDOOR_TEMP: _indoor_temp,
            ATTR_OUTDOOR_TEMP: _outdoor_temp,
            ATTR_FORECAST_HIGH: c.today_high if c else None,
            ATTR_FORECAST_LOW: c.today_low if c else None,
            ATTR_FORECAST_HIGH_TOMORROW: c.tomorrow_high if c else None,
            ATTR_FORECAST_LOW_TOMORROW: c.tomorrow_low if c else None,
            "pre_cool_status": self._pre_cool_status,
            # Issue #361: WHF command-only mode status fields
            "whf_mode": (
                "disabled"
                if self.config.get(CONF_FAN_MODE, "") in ("", "none", None, FAN_MODE_DISABLED)
                else ("state-feedback" if self._fan_state_feedback_enabled() else "command-only")
            ),
            "whf_last_commanded": (
                "on"
                if self._last_commanded_fan_state is True
                else "off"
                if self._last_commanded_fan_state is False
                else None
            ),
            "whf_desired": bool(self.automation_engine._fan_active) if self.automation_engine else None,
        }

        # Append chart log entry (every coordinator tick — 30-min cadence)
        with contextlib.suppress(Exception):
            indoor_temp = forecast.current_indoor_temp if forecast else None
            outdoor_temp = forecast.current_outdoor_temp if forecast else None
            # Extract current-hour prediction to persist alongside actual reading
            _pred_outdoor_val: float | None = None
            _pred_indoor_val: float | None = None
            if indoor_temp is None:
                _LOGGER.debug(
                    "chart log: indoor_temp unavailable — skipping pred_indoor write"
                    " (thermostat may be unknown/unavailable)"
                )
            _now_dt = dt_util.now()
            _pred_outdoor_val = _extract_current_hour_forecast_temp(self._hourly_forecast_temps, _now_dt)
            # First-write-wins archive: pred_indoor reflects ODE made ~4h ago.
            # Falls back to current ODE[0] only during warmup (first 4h after restart/install).
            _archived_pred = self._lookup_pred_archive(_now_dt)
            if _archived_pred is not None:
                _pred_indoor_val = _archived_pred
            elif self._last_predicted_indoor:
                _pred_indoor_val = self._last_predicted_indoor[0].get("temp")  # warmup fallback
            _chart_hvac_poll = self._read_chart_hvac_action()
            # Read thermostat setpoint and convert to °F for chart_log storage.
            _setpoint_f: float | None = None
            _chart_unit = self.config.get("temp_unit", "fahrenheit")
            _climate_state = self.hass.states.get(self.config["climate_entity"])
            if _climate_state and _climate_state.state in ("heat", "cool"):
                _raw_sp = _climate_state.attributes.get("target_temperature")
                if _raw_sp is not None:
                    _setpoint_f = to_fahrenheit(float(_raw_sp), _chart_unit)
            _LOGGER.debug(
                "chart_log append: event=30min_poll hvac=%r fan=%s",
                _chart_hvac_poll,
                self._fan_is_running(),
            )
            _band_lower_poll, _band_upper_poll = self._target_band_lower_upper_now()
            self._chart_log.append(
                hvac=_chart_hvac_poll,
                fan=self._fan_is_running(),
                indoor=indoor_temp,
                outdoor=outdoor_temp,
                windows_open=self._any_sensor_open(),
                windows_recommended=bool(self._current_classification.windows_recommended)
                if self._current_classification
                else False,
                pred_outdoor=_pred_outdoor_val,
                pred_indoor=_pred_indoor_val,
                setpoint=_setpoint_f,
                fan_running=self._fan_physically_running(),
                nat_vent_active=bool(self.automation_engine._natural_vent_active if self.automation_engine else False),
                lower=_band_lower_poll,
                upper=_band_upper_poll,
                nat_vent_target=self._nat_vent_target_now(),
            )
            await self._executor_job(self._chart_log.save)
            _LOGGER.debug(
                "chart_log pred_indoor=%.1f indoor=%.1f delta=%+.1f (%s)",
                _pred_indoor_val if _pred_indoor_val is not None else float("nan"),
                indoor_temp if indoor_temp is not None else float("nan"),
                (_pred_indoor_val - indoor_temp)
                if (_pred_indoor_val is not None and indoor_temp is not None)
                else float("nan"),
                "archive" if _archived_pred is not None else ("ode-warmup" if self._last_predicted_indoor else "none"),
            )

        with contextlib.suppress(Exception):
            self._thermal_factors = _compute_thermal_factors(self._chart_log.get_entries("7d"))

        # Purge archive entries older than 7 days (bounded at ≤336 entries at 30-min resolution).
        _archive_expire_cutoff = int((dt_util.now() - timedelta(days=7)).timestamp())
        self._pred_archive = {k: v for k, v in self._pred_archive.items() if k >= _archive_expire_cutoff}

        # Detect and emit post-cycle incidents
        self._detect_and_emit_incidents()

        # Issue #524: fan_remote_speed/fan_remote_timer_hours/fan_remote_timer_ends previously
        # only existed inside get_debug_state() (debug endpoint + diagnostics download), never in
        # this method's result -- which is what becomes coordinator.data and what api.py's main
        # status view actually reads. The dashboard's WHF card was unconditionally dark as a
        # result. See _compute_fan_remote_status_fields()'s docstring.
        result.update(self._compute_fan_remote_status_fields())

        return result

    def _get_outdoor_temp(self, weather_attrs: dict) -> float:
        """Read outdoor temperature based on configured source type."""
        source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)
        unit = self.config.get("temp_unit", "fahrenheit")

        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            entity_id = self.config.get("outdoor_temp_entity")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state:
                    try:
                        return to_fahrenheit(float(state.state), unit)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Outdoor temp entity %s has non-numeric state %r; falling back to weather attribute",
                            entity_id,
                            state.state,
                        )

        # weather_service source or fallback: interpolate between the two nearest
        # hourly-forecast points instead of trusting the weather integration's live
        # "temperature" attribute directly — that attribute is itself often just a
        # coarse point-sample of the same hourly model (e.g. Met.no refreshes ~hourly),
        # so it can lag or lead true current conditions by up to ~an hour during a
        # temperature ramp (Issue #511).
        interpolated, method = _interpolate_hourly_outdoor_temp(
            getattr(self, "_hourly_forecast_temps", None), dt_util.now()
        )
        if interpolated is not None:
            # Hourly forecast entries report temperature in the weather entity's
            # native unit, same as weather_attrs["temperature"] below — must go
            # through the same conversion, not just the live-attribute fallback.
            interpolated_f = to_fahrenheit(interpolated, unit)
            if method == "edge-nearest":
                _LOGGER.debug(
                    "Outdoor temp: edge-clamped interpolation (%.1f°F) — now is outside the hourly forecast range",
                    interpolated_f,
                )
            return interpolated_f

        _LOGGER.warning(
            "Hourly forecast interpolation unavailable for outdoor temp — "
            "falling back to weather nowcast attribute (integration may not support hourly forecasts)"
        )
        return to_fahrenheit(float(weather_attrs.get("temperature", 65)), unit)

    def _get_indoor_temp(self) -> float | None:
        """Read indoor temperature based on configured source type.

        Delegates to the shared ``indoor_temp.resolve_indoor_temp_f()`` helper
        (Issue #796, Step 10) so the coordinator and ``AutomationEngine`` cannot
        drift out of sync on source resolution or the plausibility guard again.
        Reads ``self.config``/``self.hass`` fresh on every call — no caching.
        """
        return resolve_indoor_temp_f(
            hass=self.hass,
            source=self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK),
            unit=self.config.get("temp_unit", "fahrenheit"),
            indoor_temp_entity=self.config.get("indoor_temp_entity"),
            climate_entity=self.config["climate_entity"],
        )

    async def _get_forecast_data(self) -> list:
        """Get forecast data using the weather.get_forecasts service.

        Falls back to the deprecated forecast attribute if the service
        call is unavailable.
        """
        weather_entity = self.config["weather_entity"]
        if not self.hass.states.get(weather_entity):
            return []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            forecasts = response.get(weather_entity, {}).get("forecast", []) if response else []
            if forecasts:
                return forecasts
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "weather.get_forecasts service call failed for %s; falling back to forecast attribute",
                weather_entity,
            )

        # Fallback: deprecated forecast attribute
        weather_state = self.hass.states.get(weather_entity)
        if weather_state:
            return weather_state.attributes.get("forecast", [])
        return []

    async def _get_hourly_forecast_data(self) -> list:
        """Get hourly forecast data from the weather entity.

        Returns a list of hourly forecast dicts, or [] if the weather
        integration does not support hourly forecasts or the call fails.
        """
        weather_entity = self.config["weather_entity"]
        if not self.hass.states.get(weather_entity):
            return []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            return response.get(weather_entity, {}).get("forecast", []) if response else []
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Hourly forecast not available for %s; using cosine model",
                weather_entity,
            )
            return []

    async def _get_forecast(self) -> ForecastSnapshot | None:
        """Pull forecast data from the weather entity."""
        weather_entity = self.config["weather_entity"]
        weather_state = self.hass.states.get(weather_entity)
        if not weather_state:
            _LOGGER.debug(
                "Weather entity %s not found — repair issue should be active",
                weather_entity,
            )
            return None

        # Entity exists but isn't reporting data yet (common after restart)
        if weather_state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Weather entity %s is %s — treating as not ready",
                weather_entity,
                weather_state.state,
            )
            return None

        attrs = weather_state.attributes

        current_outdoor = self._get_outdoor_temp(attrs)
        current_indoor = self._get_indoor_temp()
        forecast = await self._get_forecast_data()

        # Extract today and tomorrow from forecast by matching dates.
        # HA daily forecasts vary by provider: some include today, some start
        # from tomorrow. Some use UTC midnight datetimes (e.g.
        # 2026-05-16T00:00:00+00:00 = 2026-05-15 17:00 PDT), which
        # dt_util.as_local() shifts to the previous local day. Build a
        # date-keyed dict so we never assume array position == calendar day.
        today_high = current_outdoor
        today_low = current_outdoor
        tomorrow_high = current_outdoor
        tomorrow_low = current_outdoor

        today_fc = None
        tomorrow_fc = None
        if forecast:
            # Use local calendar date for "today" and extract the raw date from
            # each forecast entry without timezone conversion. Weather APIs that
            # use UTC midnight timestamps (e.g. 2026-05-31T00:00:00Z) intend
            # the date portion (2026-05-31) as the forecast date — comparing
            # that raw date against the local calendar date is correct at all
            # hours. Using UTC for "now" breaks in the evening when UTC has
            # rolled to the next calendar day but local time hasn't (Issue #190).
            now_local = dt_util.now()
            now_date = now_local.date()
            tomorrow_date = now_date + timedelta(days=1)
            _LOGGER.debug(
                "_get_forecast raw datetimes (first 5): %s",
                [e.get("datetime") for e in forecast[:5]],
            )
            forecast_by_date: dict = {}
            for entry in forecast:
                fc_dt = entry.get("datetime", "")
                try:
                    fc_obj = datetime.fromisoformat(fc_dt)
                    # Raw date: no tz conversion. API date intent, compared against local now_date.
                    fc_date = fc_obj.date()
                    forecast_by_date.setdefault(fc_date, entry)
                except (ValueError, TypeError):
                    continue
            today_fc = forecast_by_date.get(now_date)
            tomorrow_fc = forecast_by_date.get(tomorrow_date)
            available_dates = sorted(forecast_by_date.keys())
            if today_fc is None and available_dates:
                _LOGGER.warning(
                    "_get_forecast: no entry for today (%s local); available dates: %s",
                    now_date,
                    available_dates,
                )
            if tomorrow_fc is None and available_dates:
                _LOGGER.warning(
                    "_get_forecast: no entry for tomorrow (%s local); available dates: %s",
                    tomorrow_date,
                    available_dates,
                )
            _LOGGER.info(
                "_get_forecast matched: today=%s raw_temp=%s, tomorrow=%s raw_temp=%s",
                now_date,
                today_fc.get("temperature") if today_fc else f"none→{current_outdoor}°F fallback",
                tomorrow_date,
                tomorrow_fc.get("temperature") if tomorrow_fc else f"none→{current_outdoor}°F fallback",
            )

        if today_fc:
            today_high = today_fc.get("temperature", today_fc.get("tempHigh", current_outdoor))
            today_low = today_fc.get("templow", today_fc.get("tempLow", current_outdoor - 15))
        if tomorrow_fc:
            tomorrow_high = tomorrow_fc.get("temperature", tomorrow_fc.get("tempHigh", current_outdoor))
            tomorrow_low = tomorrow_fc.get("templow", tomorrow_fc.get("tempLow", current_outdoor - 15))

        unit = self.config.get("temp_unit", "fahrenheit")
        today_high = to_fahrenheit(today_high, unit)
        today_low = to_fahrenheit(today_low, unit)
        tomorrow_high = to_fahrenheit(tomorrow_high, unit)
        tomorrow_low = to_fahrenheit(tomorrow_low, unit)

        # The forecast API returns "remaining period" data — as the day
        # progresses, today's high drops to the current temp and today's low
        # becomes tonight's expected low (not this morning's actual low).
        # Fix: use observed temperature history to capture the true daily
        # high and low, so the classification stays stable all day.
        if self._outdoor_temp_history:
            observed_temps = [t for _, t in self._outdoor_temp_history]
            observed_high = max(observed_temps)
            observed_low = min(observed_temps)
            today_high = max(today_high, observed_high)
            today_low = min(today_low, observed_low)

        # Apply learned weather bias correction to tomorrow's forecast
        if self.config.get("learning_enabled", True) and self.config.get(CONF_WEATHER_BIAS, True):
            weather_bias = self.learning.get_weather_bias()
            if weather_bias["confidence"] != "none":
                bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, weather_bias["high_bias"]))
                bias_l = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, weather_bias["low_bias"]))
                if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F:
                    tomorrow_high += bias_h
                if abs(bias_l) >= MIN_WEATHER_BIAS_APPLY_F:
                    tomorrow_low += bias_l
                _LOGGER.debug(
                    "Weather bias applied: high_bias=%.1f°F low_bias=%.1f°F → tomorrow_high=%.1f°F tomorrow_low=%.1f°F",
                    bias_h,
                    bias_l,
                    tomorrow_high,
                    tomorrow_low,
                )
        else:
            _LOGGER.debug("Skipping weather bias correction: learning_enabled or weather_bias_enabled is False")

        _LOGGER.debug(
            "Forecast parse — entries=%d, today_match=%s, tomorrow_match=%s, "
            "today_high=%.1f, today_low=%.1f, tomorrow_high=%.1f, "
            "tomorrow_low=%.1f (outdoor=%.1f)",
            len(forecast) if forecast else 0,
            today_fc.get("datetime", "?") if today_fc else "NONE",
            tomorrow_fc.get("datetime", "?") if tomorrow_fc else "NONE",
            today_high,
            today_low,
            tomorrow_high,
            tomorrow_low,
            current_outdoor,
        )

        return ForecastSnapshot(
            today_high=float(today_high),
            today_low=float(today_low),
            tomorrow_high=float(tomorrow_high),
            tomorrow_low=float(tomorrow_low),
            current_outdoor_temp=float(current_outdoor),
            current_indoor_temp=float(current_indoor) if current_indoor is not None else None,
            current_humidity=attrs.get("humidity"),
            timestamp=dt_util.now(),
        )

    def _maybe_regenerate_briefing_for_drift(self) -> bool:
        """Regenerate the briefing text in place if it's gone stale mid-day.

        Three independent triggers, any one is sufficient:
        - the classified day_type category changed (Issue #78's original check)
        - today_high has drifted >= BRIEFING_TODAY_HIGH_DRIFT_THRESHOLD_F from
          what's baked into the currently-displayed briefing text, even within
          the same category (added after a live report showed a stale
          today_high for hours — the category-only check never caught this,
          since day_type had stayed "warm" the whole time).
        - Issue #847: on a WARM/MILD day, the live self._nat_vent_plan's
          nat_vent_cutoff has drifted >= BRIEFING_NAT_VENT_CUTOFF_DRIFT_THRESHOLD_MINUTES
          from what's baked into the briefing text, or nat_vent_cutoff_reason has
          flipped (e.g. comfort_floor -> outdoor_rise) — without this, a briefing
          generated early in the day can bake in a comfort_floor cutoff/reason that
          the live self._nat_vent_plan (read every cycle by the "Next Automation"
          card) has long since moved past, producing the exact briefing-vs-card
          contradiction this issue reported (8 AM "hold the heat in" vs. 11 AM
          "outdoor will stop helping" for the same underlying event).

        Does not send notifications — only updates self._last_briefing/
        self._last_briefing_short/self._briefing_day_type/self._briefing_today_high/
        self._briefing_nat_vent_cutoff/self._briefing_nat_vent_cutoff_reason in place.
        Returns True if it regenerated (caller is then responsible for persisting
        state), False otherwise (including when no briefing has been sent yet today,
        since there is nothing to keep in sync with). Extracted as its own method
        (rather than inline in the update cycle) so this drift logic has exactly one
        real implementation callers can invoke directly — including tests — instead
        of a second copy that can silently drift from what production actually does.
        """
        if not self._briefing_sent_today:
            return False
        classification = self._current_classification
        briefing_today_high = getattr(self, "_briefing_today_high", None)
        today_high_drift = (
            abs(classification.today_high - briefing_today_high) if briefing_today_high is not None else 0.0
        )
        day_type_changed = self._briefing_day_type is not None and classification.day_type != self._briefing_day_type
        high_drifted = today_high_drift >= BRIEFING_TODAY_HIGH_DRIFT_THRESHOLD_F

        # nat_vent_cutoff/reason drift — only meaningful on WARM/MILD days, the only
        # day types whose briefing text (_warm_day_plan()/_mild_day_plan()) actually
        # renders these fields. Gating avoids regeneration thrash on other day types,
        # where self._nat_vent_plan's cutoff (computed regardless of day type) is
        # never shown in text anyway.
        briefing_cutoff = getattr(self, "_briefing_nat_vent_cutoff", None)
        briefing_cutoff_reason = getattr(self, "_briefing_nat_vent_cutoff_reason", None)
        live_plan = getattr(self, "_nat_vent_plan", None) or {}
        live_cutoff = live_plan.get("nat_vent_cutoff")
        live_cutoff_reason = live_plan.get("nat_vent_cutoff_reason")
        cutoff_drift_minutes = 0.0
        cutoff_drifted = False
        reason_flipped = False
        if classification.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD):
            if briefing_cutoff is not None and live_cutoff is not None:
                cutoff_drift_minutes = abs((live_cutoff - briefing_cutoff).total_seconds()) / 60.0
                cutoff_drifted = cutoff_drift_minutes >= BRIEFING_NAT_VENT_CUTOFF_DRIFT_THRESHOLD_MINUTES
            elif briefing_cutoff != live_cutoff:
                # One side has a cutoff and the other doesn't (e.g. the nat-vent
                # window has appeared or disappeared entirely since the briefing was
                # generated) — a meaningful change in what's shown, not just a small
                # time shift, so always regenerate.
                cutoff_drifted = True
            reason_flipped = briefing_cutoff_reason != live_cutoff_reason

        if not (day_type_changed or high_drifted or cutoff_drifted or reason_flipped):
            return False

        _LOGGER.info(
            "Regenerating briefing text — day_type %s → %s, today_high drift %.1f°F (%s → %s),"
            " nat_vent_cutoff %s → %s (drift %.1fmin), nat_vent_cutoff_reason %s → %s",
            self._briefing_day_type,
            classification.day_type,
            today_high_drift,
            briefing_today_high,
            classification.today_high,
            briefing_cutoff,
            live_cutoff,
            cutoff_drift_minutes,
            briefing_cutoff_reason,
            live_cutoff_reason,
        )
        self._last_briefing, self._last_briefing_short = self._build_briefing_text(classification)
        self._briefing_day_type = classification.day_type
        self._briefing_today_high = classification.today_high
        self._briefing_nat_vent_cutoff = live_cutoff
        self._briefing_nat_vent_cutoff_reason = live_cutoff_reason
        return True

    def _build_briefing_text(
        self, classification: DayClassification, suggestions: list | None = None
    ) -> tuple[str, str]:
        """Generate briefing text for the given classification.

        Returns (briefing_full, briefing_short).  No notifications are sent.
        """
        if suggestions is None:
            suggestions = self.learning.generate_suggestions()
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        thermal_model = {}
        if self.config.get("learning_enabled", True):
            thermal_model = self.learning.get_thermal_model(learning_health=self._build_learning_health())
        adaptive_thermal_active = thermal_model.get("confidence", "none") != "none"

        bedtime_setback_heat: float | None = None
        bedtime_setback_cool: float | None = None
        if classification is not None:
            hvac_mode = classification.hvac_mode
            if hvac_mode == "heat":
                bedtime_setback_heat = compute_bedtime_setback(self.config, thermal_model, classification)
            elif hvac_mode == "cool":
                bedtime_setback_cool = compute_bedtime_setback(self.config, thermal_model, classification)

        _LOGGER.debug(
            "Bedtime setback: heat=%s cool=%s",
            bedtime_setback_heat,
            bedtime_setback_cool,
        )

        briefing_kwargs = dict(
            classification=classification,
            comfort_heat=self.config["comfort_heat"],
            comfort_cool=self.config["comfort_cool"],
            setback_heat=self.config["setback_heat"],
            setback_cool=self.config["setback_cool"],
            wake_time=wake_time,
            sleep_time=sleep_time,
            learning_suggestions=suggestions if suggestions else None,
            debounce_seconds=self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS),
            manual_grace_seconds=self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS),
            automation_grace_seconds=self.config.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS),
            grace_active=self.automation_engine._grace_active,
            grace_source=self.automation_engine._last_resume_source,
            temp_unit=self.config.get("temp_unit", "fahrenheit"),
            bedtime_setback_heat=bedtime_setback_heat,
            bedtime_setback_cool=bedtime_setback_cool,
            adaptive_thermal_active=adaptive_thermal_active,
            occupancy_mode=self._occupancy_mode,
            nat_vent_plan=self._nat_vent_plan,
            runtime_config=self.config,
            # Issue #847/#430: live readings for the WARM/MILD-day comfort_floor
            # sanity check in _warm_day_plan()/_mild_day_plan().
            current_indoor_temp=self._get_indoor_temp(),
            current_outdoor_temp=self.data.get(ATTR_OUTDOOR_TEMP) if self.data else None,
        )
        return generate_briefing(**briefing_kwargs), generate_briefing(**briefing_kwargs, verbosity="tldr_only")

    def _ensure_today_record(self, classification: DayClassification) -> None:
        """Create or roll over today's ``DailyRecord`` if needed (Issue #602).

        Extracted verbatim from ``_async_send_briefing()`` so it can also be called
        from the regular (every-30-min, self-healing per Issue #588) classification
        cycle, not only from the once-daily ``briefing_time`` trigger. Previously
        ``_today_record`` was created ONLY inside ``_async_send_briefing()``, which
        bails out early whenever the weather entity has no forecast at that one
        fixed daily moment — silently blocking manual-override detection and every
        other ``_today_record``-gated bookkeeping counter for the rest of that day.
        Idempotent: no-ops when a record for today already exists.
        """
        _today_str = dt_util.now().strftime("%Y-%m-%d")
        if self._today_record is not None and self._today_record.date == _today_str:
            return
        _prev = self._today_record if (self._today_record and self._today_record.date == _today_str) else None
        self._today_record = DailyRecord(
            date=_today_str,
            day_type=classification.day_type,
            trend_direction=classification.trend_direction,
            windows_recommended=classification.windows_recommended,
            window_open_time=(classification.window_open_time.isoformat() if classification.window_open_time else None),
            window_close_time=(
                classification.window_close_time.isoformat() if classification.window_close_time else None
            ),
            hvac_mode_recommended=classification.hvac_mode,
            hvac_runtime_minutes=_prev.hvac_runtime_minutes if _prev else 0.0,
            comfort_violations_minutes=_prev.comfort_violations_minutes if _prev else 0.0,
            manual_overrides=_prev.manual_overrides if _prev else 0,
            thermal_session_count=_prev.thermal_session_count if _prev else 0,
            occupancy_away_minutes=_prev.occupancy_away_minutes if _prev else 0.0,
            windows_opened=_prev.windows_opened if _prev else False,
            window_open_actual_time=_prev.window_open_actual_time if _prev else None,
            override_details=list(_prev.override_details) if _prev else [],
        )
        # Capture raw forecast high/low for weather bias learning
        if self.config.get("learning_enabled", True):
            self._today_record.forecast_high_f = classification.today_high
            self._today_record.forecast_low_f = classification.today_low

    async def _async_send_briefing(
        self,
        now: datetime,
        *,
        send_notifications: bool = True,
        respect_notification_mute: bool = False,
    ) -> None:
        """Generate and send the daily briefing.

        Issue #812: the entire body runs inside ``log_capture.zone_scope()``
        so every ``_LOGGER`` call reached from here is tagged with this
        coordinator's zone in the log_capture ring buffer. Purely additive —
        no change to control flow, return value, or exception propagation
        below.

        Issue #817 Part 3/4 — two independent gates, shared by every caller (one
        pipeline, not a forked copy per caller):

        - ``send_notifications``: when False, briefing text is generated/cached exactly as
          normal but zero ``notify.*`` service calls are made. Used by the dashboard's
          Regenerate button — the user is already looking at the screen, so a real push/email
          is unnecessary. Default True (every other caller: the debug tab's Send Briefing
          button, and — see below — the scheduled daily trigger).
        - ``respect_notification_mute``: when True, notifications are further gated by this
          zone's own ``CONF_BRIEFING_NOTIFICATIONS_ENABLED`` config (see
          ``zone_registry.default_briefing_notifications_enabled`` and the v19->v20 migration)
          — on a multi-zone install, only the designated zone sends. Default False, so a
          manual button press always means what it says regardless of this zone's mute state
          (deliberate — a manual "Send Briefing" from the debug tab on a muted zone is a real
          test action, not the unattended daily spam this mute exists to prevent). Only the
          scheduled ``briefing_time`` trigger (``_async_send_briefing_scheduled`` below) passes
          True.
        """
        with log_capture.zone_scope(self.zone_label):
            if self._briefing_sent_today:
                return

            # Issue #817 Part 2: reuse the current cycle's classification instead of
            # independently re-fetching forecast + re-running classify_day() when it's
            # fresh enough — this was the source of two back-to-back weather.get_forecasts
            # calls returning different today_high within under a second. "Fresh enough"
            # mirrors this coordinator's own update cadence (self.update_interval, 30 min):
            # anything computed within the current cycle window is the same data the regular
            # _async_update_data_impl() cycle already fetched and applied.
            _fetched_at = self._classification_fetched_at
            _reuse_classification = (
                self._current_classification is not None
                and _fetched_at is not None
                and (dt_util.now() - _fetched_at) < self.update_interval
            )
            if _reuse_classification:
                classification = self._current_classification
                _LOGGER.debug(
                    "Briefing reusing same-cycle classification (fetched %s ago) — skipping forecast re-fetch",
                    dt_util.now() - _fetched_at,
                )
            else:
                forecast = await self._get_forecast()
                self._hourly_forecast_temps = await self._get_hourly_forecast_data()
                if not forecast:
                    return

                prev_type = self._current_classification.day_type if self._current_classification else None
                _thresh = {
                    "threshold_hot": self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
                    "threshold_warm": self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
                    "threshold_mild": self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
                    "threshold_cool": self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
                }
                classification = classify_day(forecast, previous_day_type=prev_type, **_thresh)
                self._current_classification = classification
                self._classification_fetched_at = dt_util.now()
                # Issue #511: also mirrors to automation_engine now (previously this call
                # site didn't — a minor pre-existing gap closed as a side effect of the
                # single-function consolidation).
                self._apply_outdoor_temp(forecast.current_outdoor_temp, record_history=False)

            # Daily incremental solar phase re-fit (Issue #310/#312)
            if self.config.get("learning_enabled", True):
                self._maybe_run_periodic_solar_phase_fit()

            # Inject thermal model into automation engine for adaptive scheduling
            if self.config.get("learning_enabled", True):
                thermal_model = self.learning.get_thermal_model(
                    learning_health=self._build_learning_health(),
                    # self._last_outdoor_temp: same reading forecast.current_outdoor_temp
                    # would give this cycle — set by _apply_outdoor_temp() above on a real
                    # fetch, or by the regular update cycle moments earlier when reusing.
                    outdoor_temp_f=self._last_outdoor_temp,
                    solar_factor=_solar_factor(now.hour),
                )
                self.automation_engine._thermal_model = thermal_model
                self._solar_phase_offset = (
                    thermal_model.get("solar_phase_offset_h") or THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
                )
            else:
                thermal_model = {}
                self.automation_engine._thermal_model = {}
            confidence = thermal_model.get("confidence", "none")
            obs_count = thermal_model.get("observation_count_heat", 0) + thermal_model.get("observation_count_cool", 0)
            _LOGGER.debug(
                "Thermal model: confidence=%s observations=%d heat_rate=%s cool_rate=%s",
                confidence,
                obs_count,
                thermal_model.get("heating_rate_f_per_hour"),
                thermal_model.get("cooling_rate_f_per_hour"),
            )
            # Issue #786: refresh TOU schedule state before this executor call, same
            # ordering reason as the main update cycle (_resolve_tou_schedule_state()'s
            # docstring) — this path recomputes classification/thermal_model locally above,
            # so re-resolving here keeps the ODE curve's TOU override in sync with them.
            self._resolve_tou_schedule_state()
            # Issue #514: resolve and cache this cycle's target-band schedule, same reason —
            # closes the divergence where this call site previously fell through to
            # _build_predicted_indoor_future()'s own internal (not-identical) band recompute.
            self._resolve_target_band_schedule()

            # Update cached ODE prediction for ceiling guard.
            # thermal_model is already computed from self.learning.get_thermal_model() above.
            # Offloaded to executor — ODE integration + OLS math blocks the event loop otherwise.
            self._last_predicted_indoor = await self._executor_job(
                functools.partial(
                    _build_predicted_indoor_future,
                    self._hourly_forecast_temps,
                    self.config,
                    dt_util.now(),
                    current_indoor_temp=self._get_indoor_temp(),
                    thermal_model=thermal_model,
                    occupancy_mode=self._occupancy_mode,
                    band_schedule=self._target_band_schedule,
                    classification=classification,
                    tou_precondition_window=self._tou_precondition_window_tuple(),
                )
            )
            _LOGGER.debug(
                "Caching predicted indoor curve (briefing): %d points",
                len(self._last_predicted_indoor),
            )
            self._compute_and_cache_nat_vent_plan()
            await self.automation_engine.apply_classification(
                classification,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=self._get_indoor_temp(),
                nat_vent_cutoff=(getattr(self, "_nat_vent_plan", None) or {}).get("nat_vent_cutoff"),
                comfort_floor_crossing_time=(getattr(self, "_nat_vent_plan", None) or {}).get(
                    "comfort_floor_crossing_time"
                ),
            )

            # Initialize today's learning record, preserving any counters already accumulated
            # today (e.g. after an HA restart mid-day that fires briefing again). Issue #602:
            # extracted to _ensure_today_record() so the regular classification cycle can also
            # call it — this call site is now just the once-daily "make sure it's current" path.
            self._ensure_today_record(classification)

            # Generate briefing text and track which suggestions were sent
            suggestions = self.learning.generate_suggestions()
            if self._today_record:
                self._today_record.suggestion_sent = self.learning.get_last_suggestion_keys()

            self._last_briefing, self._last_briefing_short = self._build_briefing_text(
                classification, suggestions=suggestions
            )
            self._briefing_day_type = classification.day_type
            self._briefing_today_high = classification.today_high
            # Issue #847: bake in this cycle's nat_vent_cutoff/reason alongside
            # day_type/today_high so _maybe_regenerate_briefing_for_drift()'s new
            # third trigger has a correct starting point to compare against.
            _plan_at_generation = getattr(self, "_nat_vent_plan", None) or {}
            self._briefing_nat_vent_cutoff = _plan_at_generation.get("nat_vent_cutoff")
            self._briefing_nat_vent_cutoff_reason = _plan_at_generation.get("nat_vent_cutoff_reason")

            # In observe-only mode, skip sending the notification
            if not self._automation_enabled:
                _LOGGER.info("[DRY RUN] Briefing generated but notification skipped (automation disabled)")
                self._briefing_sent_today = True
                await self._async_save_state()
                return

            # Issue #817 Part 3/4: combine this call's own request (send_notifications — False
            # for Regenerate) with the zone-mute gate (only checked when the caller opts in via
            # respect_notification_mute — the scheduled trigger only). One boolean, computed
            # once, applied uniformly to both the push and email blocks below.
            _zone_muted = respect_notification_mute and not self.config.get(CONF_BRIEFING_NOTIFICATIONS_ENABLED, True)
            _should_notify = send_notifications and not _zone_muted
            if not _should_notify:
                _LOGGER.info(
                    "Briefing generated but notification skipped (%s)",
                    "zone not the designated notifier" if _zone_muted else "notify=False (regenerate)",
                )
            else:
                # Send push notification — short TLDR summary
                _notify_svc = self.config["notify_service"]
                _notify_name = _notify_svc.split(".")[-1] if "." in _notify_svc else _notify_svc
                if self.config.get("push_briefing", True):
                    await self.hass.services.async_call(
                        "notify",
                        _notify_name,
                        {"message": self._last_briefing_short, "title": "🏠 Your Home Climate Plan for Today"},
                    )
                # Send email — full briefing
                if self.config.get("email_briefing", True):
                    await self.hass.services.async_call(
                        "notify",
                        "send_email",
                        {"message": self._last_briefing, "title": "🏠 Your Home Climate Plan for Today"},
                    )
                _LOGGER.info("Daily briefing sent — day type: %s", classification.day_type)

            self._briefing_sent_today = True
            await self._async_save_state()

    async def _async_send_briefing_scheduled(self, now: datetime) -> None:
        """Scheduled ``briefing_time`` trigger — the one call site that respects this zone's mute.

        Issue #817 Part 3: registered with ``async_track_time_change`` instead of
        ``_async_send_briefing`` directly, so the automatic daily trigger (the actual source of
        cross-zone notification spam) is the only caller that opts into
        ``respect_notification_mute=True``. Manual invocations (both dashboard buttons, via
        ``api.py``) call ``_async_send_briefing`` directly and never set this — see that
        method's docstring for why.
        """
        await self._async_send_briefing(now, respect_notification_mute=True)

    async def _async_morning_wakeup(self, now: datetime) -> None:
        """Handle morning wake-up."""
        _was_overridden = self._any_override_active()
        _indoor_temp = self._get_indoor_temp()
        await self.automation_engine.handle_morning_wakeup(indoor_temp=_indoor_temp)
        self._feed_override_grace_fsm_if_cleared(_was_overridden)

    async def _async_bedtime(self, now: datetime) -> None:
        """Handle bedtime setback."""
        _was_overridden = self._any_override_active()
        await self.automation_engine.handle_bedtime()
        self._feed_override_grace_fsm_if_cleared(_was_overridden)

    def _resolve_tou_schedule_state(self) -> None:
        """Resolve TOU cost-period schedule state for this cycle (Issue #786) — cheap,
        pure (no HVAC writes), cached on ``self._tou_phase_resolution``/
        ``_tou_active_cost_resolution``.

        Called EARLY in ``_async_update_data_impl()`` — right after the classification/
        thermal-model refresh but BEFORE the ODE prediction executor call — so that when
        that executor call computes its own internal band (it doesn't receive a
        pre-computed ``band_schedule``, see ``_build_predicted_indoor_future()``'s
        docstring), it sees THIS cycle's fresh resolution rather than last cycle's stale
        one. ``get_chart_data()`` reuses the same cached value later in the cycle.
        Actually acting on a ``PRECONDITIONING`` result happens later, in
        ``_apply_tou_schedule()``, after ``apply_classification()`` has run (so the
        pre-conditioning setpoint is the final word for the thermostat that cycle, not the
        day's normal comfort-band edge).
        """
        raw_schedules = self.config.get("schedules") or []
        if not raw_schedules or self._current_classification is None:
            self._tou_phase_resolution = None
            self._tou_active_cost_resolution = None
            self._tou_active_window_notified = False
            return

        schedules = [Schedule(**s) for s in raw_schedules]
        now = dt_util.now()
        self._tou_active_cost_resolution = resolve_active_schedules(schedules, now)
        self._tou_phase_resolution = resolve_tou_phase(
            schedules,
            now,
            self._get_indoor_temp(),
            self._current_classification.hvac_mode,
            self.automation_engine._thermal_model if self.automation_engine else None,
            self.config,
        )
        self._maybe_emit_tou_active_window_event()

    def _maybe_emit_tou_active_window_event(self) -> None:
        """Investigation D / Phase 3d: a configured TOU schedule can silently do nothing
        with zero visibility — the live-instance finding. A ``cost_tag="high"`` window
        covering `now` previously left no trace anywhere: the Status card only ever
        showed TOU text during ``PRECONDITIONING`` (never the active window itself, and
        never on a day where the classification ruled out banking entirely), and the
        Activity Record had no ``tou_*`` event of any kind for a day like that. This emits
        a deduped ``tou_schedule_window_active`` event once per window-becomes-active
        transition (``self._tou_active_window_notified`` guards re-firing every cycle
        while the same window stays active), carrying whether pre-conditioning was
        actually possible for this window (``hvac_mode`` in ``heat``/``cool`` at
        activation) — so the record shows *why* nothing else happened on an off/windows
        day, rather than showing nothing at all. INFO-level logging on the same
        transition, matching this project's Observability Requirements.
        """
        resolution = self._tou_active_cost_resolution
        is_active_high = resolution is not None and resolution.cost_tag == COST_TAG_HIGH
        if not is_active_high:
            self._tou_active_window_notified = False
            return
        if self._tou_active_window_notified:
            return
        self._tou_active_window_notified = True
        mode = self._current_classification.hvac_mode if self._current_classification else None
        preconditioned = mode in ("heat", "cool")
        _LOGGER.info(
            "TOU schedule window active: schedule_ids=%s preconditioned=%s hvac_mode=%s",
            resolution.active_schedule_ids,
            preconditioned,
            mode,
        )
        self._emit_event(
            "tou_schedule_window_active",
            {
                "active_schedule_ids": list(resolution.active_schedule_ids),
                "preconditioned": preconditioned,
                "hvac_mode": mode,
            },
        )

    async def _apply_tou_schedule(self) -> None:
        """Act on ``self._tou_phase_resolution`` (resolved earlier this cycle by
        ``_resolve_tou_schedule_state()``) — drives the banking setpoint if a
        pre-conditioning window is currently active (Issue #786).
        """
        resolution = self._tou_phase_resolution
        if resolution is not None and resolution.phase == TOUPhase.PRECONDITIONING:
            await self.automation_engine.apply_tou_precondition(
                self._current_classification, resolution.target, resolution.schedule_id
            )

    def _tou_precondition_window_tuple(self) -> tuple[datetime, datetime, float, str] | None:
        """Build the ``tou_precondition_window`` tuple ``_compute_target_band_schedule()``
        expects from the cached ``self._tou_phase_resolution`` (Issue #786), or ``None`` if
        no upcoming ``high`` schedule was resolved this cycle. Shared by both call sites
        (chart band builder, ODE curve builder) so they always agree — mandatory Chart
        Coverage rule.

        ``getattr`` with a default (not a plain attribute read): several tests
        partially-instantiate the coordinator via ``object.__new__()`` + bound methods
        (bypassing ``__init__`` — an established, accepted pattern in this codebase, see
        CLAUDE.md's Testing Requirements), so this attribute may not exist yet.
        """
        resolution = getattr(self, "_tou_phase_resolution", None)
        if resolution is None or resolution.schedule_id is None:
            return None
        return (resolution.precondition_start, resolution.schedule_start, resolution.target, resolution.mode)

    def _compute_pre_cool_trigger_time(self) -> datetime | None:
        """Compute the pre-cool trigger time for tonight.

        Thin ``self``-bound wrapper around the pure ``_compute_pre_cool_trigger_time_pure()``
        (Issue #514) — see that module-level function's docstring for the algorithm
        (nat-vent window close + delay, or wake_time - offset fallback). Kept as a method
        for existing callers that expect bound access to live classification/config/clock.
        """
        return _compute_pre_cool_trigger_time_pure(self._current_classification, self.config, dt_util.now())

    def _build_target_band_for(self, now: datetime, thermal_model: dict | None) -> list[dict]:
        """Compute the target-band schedule for *now* — the single shared implementation
        behind both ``get_chart_data()``'s own on-demand computation (which supplies its
        own live ``now`` and freshly-fetched ``thermal_model``) and the once-per-cycle
        cache built by ``_resolve_target_band_schedule()`` below (Issue #514).

        Extracted verbatim from ``get_chart_data()`` (the canonical "call site A" per the
        Issue #514/#470 investigation) so there is exactly one place that builds the
        hourly-timestamp list, resolves the pre-cool trigger/target, and calls
        ``_compute_target_band_schedule()`` — no second, independently-maintained copy of
        this block anywhere in the module.
        """
        _band_timestamps = []
        for _fc_entry in self._hourly_forecast_temps or []:
            _dt_str = _fc_entry.get("datetime") or _fc_entry.get("time")
            if not _dt_str:
                continue
            try:
                _dt_obj = datetime.fromisoformat(_dt_str)
                _band_timestamps.append(dt_util.as_local(_dt_obj) if _dt_obj.tzinfo else _dt_obj)
            except (ValueError, TypeError):
                continue

        # Compute pre-cool band parameters for chart dip visualization
        _pc_trigger_h: float | None = None
        _pc_target: float | None = None
        _pc_modifier = (
            resolve_pre_cool_modifier(self._current_classification, self.config)
            if self._current_classification
            else None
        )
        if _pc_modifier is not None:
            _pc_trigger_time = self._compute_pre_cool_trigger_time()
            if _pc_trigger_time is not None:
                _pc_trigger_h = _pc_trigger_time.hour + _pc_trigger_time.minute / 60.0
                _pc_target = compute_pre_cool_target(self.config, _pc_modifier)

        return list(
            _compute_target_band_schedule(
                _band_timestamps,
                self.config,
                self._occupancy_mode,
                now,
                setback_modifier=(
                    getattr(self._current_classification, "setback_modifier", 0.0)
                    if self._current_classification is not None
                    else 0.0
                ),
                thermal_model=thermal_model,
                classification=self._current_classification,
                pre_cool_trigger_h=_pc_trigger_h,
                pre_cool_target=_pc_target,
                tou_precondition_window=self._tou_precondition_window_tuple(),
            )
        )

    def _resolve_target_band_schedule(self) -> None:
        """Resolve and cache this cycle's target-band schedule (Issue #514) — cached on
        ``self._target_band_schedule``.

        Mirrors ``_resolve_tou_schedule_state()``'s "resolve once early, cache, multiple
        consumers read it later" pattern (Issue #786) — call this at the same point in the
        cycle (immediately after ``_resolve_tou_schedule_state()``), before the ODE
        prediction executor call.

        This closes the pre-existing call-site divergence: the main 30-min cycle and the
        daily briefing previously called ``_build_predicted_indoor_future()`` without a
        ``band_schedule=`` argument, silently falling through to that function's internal
        fallback recompute — an independent, not-identical formula (different pre-cool
        trigger-time logic, a ``sleep_heat``/``sleep_cool``-overridden config copy, a
        dropped ``setback_modifier``, and a differently-filtered timestamp array) than the
        canonical one ``get_chart_data()`` already used for the displayed band. Both the
        main cycle and the briefing now pass ``band_schedule=self._target_band_schedule``
        (cached here) to ``_build_predicted_indoor_future()``, so that fallback is never
        reached by any production caller again — only by direct/standalone tests of
        ``_build_predicted_indoor_future()`` that don't supply ``band_schedule``.

        ``get_chart_data()`` is unaffected by this cache — it continues to compute its own
        band on demand (live ``now``, freshly-fetched ``thermal_model``) via
        ``_build_target_band_for()`` directly, since it must reflect the exact moment a
        chart request arrives, not the last 30-min cycle's snapshot.
        """
        self._target_band_schedule = self._build_target_band_for(
            dt_util.now(),
            self.automation_engine._thermal_model if self.automation_engine else None,
        )

    def _compute_and_cache_nat_vent_plan(self) -> None:
        """Issue #817: single per-cycle computation of warm/mild-day nat-vent window
        and cutoff timing, cached on ``self._nat_vent_plan``.

        Before this existed, briefing text, the TLDR table, and the "Next Automation"
        status card each independently called what is now
        ``nat_vent_plan.compute_nat_vent_plan()`` with their own locally-rebuilt
        inputs — the exact shape of bug that let #528 silently reintroduce a duplicate
        computation 2 days after #518 promised there'd never be one. Every consumer
        now reads this one cached value instead.

        Call this immediately after ``self._last_predicted_indoor`` is (re)built, in
        both the main 30-min cycle and the daily-briefing pipeline — the same point in
        the cycle ``_resolve_target_band_schedule()`` documents for the target-band
        cache, for the same reason (downstream consumers must see this cycle's curve,
        not a stale one).
        """
        c = self._current_classification
        if c is None or not self._last_predicted_indoor:
            self._nat_vent_plan = None
            return
        _comfort_heat_raw = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        _sleep_heat = float(self.config.get("sleep_heat", _comfort_heat_raw))
        _outdoor_curve = _build_future_forecast_outdoor(self._hourly_forecast_temps, c)
        self._nat_vent_plan = compute_nat_vent_plan(
            predicted_indoor=self._last_predicted_indoor,
            predicted_outdoor=_outdoor_curve,
            comfort_cool=float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)),
            comfort_heat_raw=_comfort_heat_raw,
            sleep_heat=_sleep_heat,
            in_sleep_window_fn=lambda ts: _in_sleep_window(ts, self.config),
            window_open_time=c.window_open_time,
        )

    def _target_band_lower_upper_now(self) -> tuple[float | None, float | None]:
        """Return this cycle's cached target-band lower/upper for the current instant
        (Issue #514).

        Reads ``self._target_band_schedule`` — resolved once per cycle by
        ``_resolve_target_band_schedule()`` above — rather than recomputing the band.
        Per the Issue #514 design decision, ``chart_log`` entries persist an immutable
        per-cycle snapshot of "what was the target at time T"; that snapshot must come
        from the same single choke-point computation every other consumer reads this
        cycle, never a fresh independent recompute (that would just reintroduce the
        call-site-divergence bug class Phase 2 closed).

        The schedule is keyed by hourly forecast timestamps, not "now" itself, so this
        picks the latest entry at-or-before now (the hour bucket currently in effect),
        falling back to the earliest available entry if now precedes every entry (e.g.
        immediately after startup, before the first forecast hour has passed).

        Returns ``(None, None)`` if no schedule has been resolved yet (e.g. an
        event-driven chart_log write firing before the first cycle's
        ``_resolve_target_band_schedule()`` call, or a partially-instantiated test
        coordinator) — callers persist that as a null-safe "unknown", never a guess.
        """
        schedule = getattr(self, "_target_band_schedule", None)
        if not schedule:
            return None, None
        now = dt_util.now()
        best: tuple[datetime, dict] | None = None
        for entry in schedule:
            ts_str = entry.get("ts")
            if not ts_str:
                continue
            try:
                ts_dt = datetime.fromisoformat(ts_str)
                ts_dt = dt_util.as_local(ts_dt) if ts_dt.tzinfo else ts_dt
            except (ValueError, TypeError):
                continue
            if ts_dt <= now and (best is None or ts_dt > best[0]):
                best = (ts_dt, entry)
        chosen = best[1] if best is not None else schedule[0]
        return chosen.get("lower"), chosen.get("upper")

    def _nat_vent_target_now(self) -> float | None:
        """This cycle's real nat-vent thermostatic cycling target, or None if nat-vent is
        not currently active (Phase 3a, chart target-line).

        Uses the shared ``nat_vent_cycling.compute_nat_vent_target()`` helper (Phase 3a-pre
        DRY consolidation) — the same formula ``automation.py``'s live
        ``nat_vent_temperature_check()`` decision path uses — rather than re-deriving the
        comfort-midpoint/sleep-floor math a third time here. Only meaningful while
        ``automation_engine._natural_vent_active`` is True; callers persist ``None``
        otherwise, matching the field's documented semantics in ``chart_log.append()``.
        """
        if not (self.automation_engine and self.automation_engine._natural_vent_active):
            return None
        comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        sleep_heat = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
        in_sleep_window = _in_sleep_window(dt_util.now(), self.config)
        return compute_nat_vent_target(
            sleep_heat=sleep_heat,
            in_sleep_window=in_sleep_window,
            comfort_heat_raw=comfort_heat,
            comfort_cool=comfort_cool,
            hysteresis=hysteresis,
        )

    def _maybe_schedule_pre_cool(self) -> None:
        """Schedule the overnight pre-cool trigger if tonight is eligible and not yet scheduled."""
        if self._pre_cool_trigger_scheduled:
            return
        trigger_time = self._compute_pre_cool_trigger_time()
        if trigger_time is None:
            return
        now = dt_util.now()
        if trigger_time <= now:
            _LOGGER.info("Pre-cool trigger time %s already passed; skipping scheduling", trigger_time.strftime("%H:%M"))
            return

        # Build the pre-cool target for status display
        c = self._current_classification
        _modifier = resolve_pre_cool_modifier(c, self.config) if c else None
        pre_cool_target = compute_pre_cool_target(self.config, _modifier if _modifier is not None else 0.0)

        self._pre_cool_trigger_cancel = async_track_point_in_time(self.hass, self._async_pre_cool_trigger, trigger_time)
        self._pre_cool_trigger_scheduled = True
        self._pre_cool_trigger_dt = trigger_time
        self._pre_cool_target = pre_cool_target
        self._pre_cool_status = (
            f"Pre-cool scheduled ({pre_cool_target:.0f}°F @ {trigger_time.strftime('%I:%M %p').lstrip('0')})"
        )

    async def _async_pre_cool_trigger(self, now: datetime) -> None:
        """Handle the overnight pre-cool trigger point."""
        self._pre_cool_trigger_dt = None  # trigger has fired; no longer a future candidate
        nat_vent_just_closed = not self.automation_engine.natural_vent_active
        indoor_temp = self._get_indoor_temp()
        result = await self.automation_engine.handle_pre_cool(
            indoor_temp=indoor_temp,
            nat_vent_just_closed=nat_vent_just_closed,
        )
        _LOGGER.info("Pre-cool trigger handler completed: %s", result)
        if "suppressed" in result:
            self._pre_cool_status = result.replace("suppressed: ", "pre-cool suppressed — ")
        elif "applied" in result:
            self._pre_cool_status = result.replace("applied: ", "pre-cool active (").rstrip() + ")"
        await self.async_refresh()

    def _maybe_reschedule_pre_cool_on_nat_vent_exit(self) -> None:
        """Pull a pending pre-cool trigger earlier when nat-vent exits for real, ahead
        of its originally-scheduled window_close_time (#437 follow-up).

        Occupant impact without this: a nat-vent session that ends early — the
        reactivation gate exiting, a sensor closing, outdoor rising, an away/vacation
        ceiling exit, or a startup reconcile — leaves pre-cool waiting on the STATIC
        classification-time schedule (window_close_time + 30 min, computed once at
        classification time), wasting the AC-vs-free-cooling decision gap between the
        real exit and the stale trigger time. Called from _emit_event() on every
        natural_vent_active True->False transition.
        """
        from .const import PRE_COOL_POST_NAT_VENT_DELAY_MINUTES

        if not self._current_classification:
            return
        _modifier = resolve_pre_cool_modifier(self._current_classification, self.config)
        new_trigger = _decide_pre_cool_reschedule(
            current_trigger_at=self._pre_cool_trigger_dt,
            pre_cool_eligible=_modifier is not None,
            nat_vent_close_delay_minutes=PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
            now=dt_util.now(),
        )
        if new_trigger is None:
            return
        if self._pre_cool_trigger_cancel is not None:
            self._pre_cool_trigger_cancel()
        self._pre_cool_trigger_cancel = async_track_point_in_time(self.hass, self._async_pre_cool_trigger, new_trigger)
        self._pre_cool_trigger_dt = new_trigger
        self._pre_cool_status = (
            f"Pre-cool rescheduled ({new_trigger.strftime('%I:%M %p').lstrip('0')}) — nat-vent exited early"
        )
        _LOGGER.info(
            "Pre-cool trigger pulled earlier to %s — nat-vent exited ahead of its scheduled window close",
            new_trigger.strftime("%H:%M"),
        )

    async def _async_end_of_day(self, now: datetime) -> None:
        """Finalize the day's record and reset for tomorrow."""
        if self._today_record:
            # Compute avg indoor temp from history
            if self._indoor_temp_history:
                self._today_record.avg_indoor_temp = round(
                    sum(t for _, t in self._indoor_temp_history) / len(self._indoor_temp_history),
                    1,
                )
            # Capture observed outdoor high/low for weather bias learning
            if self.config.get("learning_enabled", True) and self._outdoor_temp_history:
                observed_temps = [t for _, t in self._outdoor_temp_history]
                self._today_record.observed_high_f = round(max(observed_temps), 1)
                self._today_record.observed_low_f = round(min(observed_temps), 1)
            # Flush any accumulated HVAC runtime
            self._flush_hvac_runtime()
            # Watchdog: if HVAC ran significantly but no thermal observations were recorded, warn
            if self._today_record.hvac_runtime_minutes > 30.0 and self._today_record.thermal_session_count == 0:
                _LOGGER.warning(
                    "Thermal learning watchdog: %.1f min HVAC runtime today but zero thermal"
                    " observations recorded — check HA logs for 'Thermal obs skipped' entries",
                    self._today_record.hvac_runtime_minutes,
                )
                self._emit_event(
                    "thermal_learning_no_observations",
                    {
                        "hvac_runtime_minutes": round(self._today_record.hvac_runtime_minutes, 1),
                        "thermal_session_count": self._today_record.thermal_session_count,
                    },
                )
            self.learning.record_day(self._today_record)
            await self._executor_job(self.learning.save_state)
            _LOGGER.info("Day record saved for learning")

        self._today_record = None
        self._briefing_sent_today = False
        self._briefing_day_type = None
        self._briefing_today_high = None
        self._briefing_nat_vent_cutoff = None
        self._briefing_nat_vent_cutoff_reason = None
        self._hvac_on_since = None
        self._last_violation_check = None
        self._outdoor_temp_history.clear()
        self._indoor_temp_history.clear()
        self._hourly_forecast_temps.clear()
        # Issue #540: reset the soft-start peak-tracking mirror alongside the buffer it's
        # derived from, so a stale "yesterday's peak" can't leak into the new day before
        # the first post-midnight sample arrives.
        self.automation_engine._outdoor_temp_today_peak = None
        self.automation_engine._outdoor_temp_today_sample_count = 0
        # Issue #511: refetch immediately rather than waiting for the next 30-min
        # cycle — otherwise outdoor-temp interpolation has nothing to interpolate
        # against for up to ~30 min after every midnight reset, degrading nightly
        # to the raw (pre-Issue #511) weather attribute with a WARNING each time.
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        self.automation_engine._hourly_forecast_temps = self._hourly_forecast_temps

        # Reset pre-cool state for the new day
        if self._pre_cool_trigger_cancel is not None:
            self._pre_cool_trigger_cancel()
            self._pre_cool_trigger_cancel = None
        self._pre_cool_trigger_scheduled = False
        self._pre_cool_status = None
        self._pre_cool_trigger_dt = None
        self._pre_cool_target = None

        await self._async_save_state()

    async def _async_door_window_changed(self, event: Event) -> None:
        """Handle a door/window sensor state change with debounce."""
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")
        if not new_state:
            return

        # Issue #489: refresh immediately on every raw transition (open or closed) so
        # contact_status/contact_sensors reflect live sensor state right away. This is
        # display-only — it does not affect the debounce below, which still exclusively
        # gates the HVAC pause/resume and nat-vent decision.
        self.hass.async_create_task(self.async_request_refresh())

        if self._is_sensor_open(entity_id):
            # Issue #645: a group/helper sensor re-reporting "on" after being unavailable
            # (e.g. re-registering during an HA restart/integration reload) is not a genuine
            # door-open event — the physical state never changed, HA's state machine just
            # stamped a fresh last_changed on the unavailable->on transition. Starting a
            # normal debounce timer here would treat a window that's been open for hours the
            # same as a window that just opened, silently re-arming the ~10-minute debounce
            # protection on every restart. Record the blip's last_changed so
            # _sensor_debounce_pending() can exclude it, and skip debounce entirely — a
            # genuinely brand-new sensor (never seen this session) has no _door_open_timers
            # entry either way, so live guards (_apply_comfort_band(), etc.) still see it as
            # open via _any_monitored_sensor_open(); nothing here weakens that.
            # old_state is deliberately NOT treated as a blip when None — HA's state-changed
            # listener only fires with old_state=None when the entity had no prior state at
            # all (not the case for an already-registered sensor waking from unavailable),
            # and treating it as a blip would risk silently skipping debounce on a genuine
            # first-ever open in some other, more exotic startup ordering.
            old_state = event.data.get("old_state")
            if old_state is not None and old_state.state in ("unavailable", "unknown"):
                if new_state.last_changed is not None:
                    self._sensor_reconnect_blip_last_changed[entity_id] = new_state.last_changed
                _LOGGER.info(
                    "Contact sensor reconnected while open: %s (was %s) — not a genuine open event, skipping debounce",
                    entity_id,
                    old_state.state if old_state else "none",
                )
                return

            # Sensor transitioned to open — start debounce timer if not already running
            if entity_id in self._door_open_timers:
                return  # Timer already pending for this sensor

            debounce_sec = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
            _expiry_time = dt_util.now() + timedelta(seconds=debounce_sec)
            _expiry_iso = _expiry_time.isoformat()
            self._door_open_timer_expiry[entity_id] = _expiry_iso
            _LOGGER.info(
                "Contact sensor opened: %s — debounce started (%ds), nat vent eval at %s",
                entity_id,
                debounce_sec,
                _expiry_time.strftime("%H:%M:%S"),
            )

            @callback
            def _debounce_expired(_now: Any, eid: str = entity_id) -> None:
                """Debounce period elapsed — schedule async check."""

                async def _do_debounce() -> None:
                    self._door_open_timers.pop(eid, None)
                    self._door_open_timer_expiry.pop(eid, None)
                    if self._is_sensor_open(eid):
                        c = self._current_classification
                        _LOGGER.info(
                            "Debounce expired for %s — evaluating nat vent conditions "
                            "(classification=%s, hvac_mode=%s, windows_recommended=%s)",
                            eid,
                            c.day_type if c else "none",
                            c.hvac_mode if c else "none",
                            c.windows_recommended if c else False,
                        )
                        await self.automation_engine.handle_door_window_open(eid)
                        # Trigger coordinator refresh so sensor entities reflect post-evaluation state
                        self.hass.async_create_task(self.async_request_refresh())
                        if self._today_record:
                            self._today_record.door_window_pause_events += 1
                            sensor_key = eid.split(".")[-1]
                            self._today_record.door_pause_by_sensor[sensor_key] = (
                                self._today_record.door_pause_by_sensor.get(sensor_key, 0) + 1
                            )

                            # Track window compliance — credit any open during a windows-recommended day
                            c = self._current_classification
                            if c and c.windows_recommended and not self._today_record.windows_opened:
                                self._today_record.windows_opened = True
                                self._today_record.window_open_actual_time = dt_util.now().isoformat()

                            # Always track physical window opens (independent of recommendations)
                            if not self._today_record.windows_physically_opened:
                                self._today_record.windows_physically_opened = True
                                self._today_record.window_physical_open_time = dt_util.now().isoformat()

                            await self._async_save_state()

                self.hass.async_create_task(_do_debounce())

            cancel = async_call_later(self.hass, debounce_sec, _debounce_expired)
            self._door_open_timers[entity_id] = cancel
        else:
            # Sensor transitioned to closed — cancel any pending debounce timer
            cancel = self._door_open_timers.pop(entity_id, None)
            self._door_open_timer_expiry.pop(entity_id, None)
            self._sensor_reconnect_blip_last_changed.pop(entity_id, None)
            if cancel:
                cancel()
                _LOGGER.info("Contact sensor closed: %s — debounce cancelled", entity_id)

            # Check if ALL monitored sensors are now closed
            all_closed = all(not self._is_sensor_open(s) for s in self._resolved_sensors)
            if all_closed:
                # Track window close time if we were tracking compliance
                if (
                    self._today_record
                    and self._today_record.windows_opened
                    and self._today_record.window_close_actual_time is None
                ):
                    self._today_record.window_close_actual_time = dt_util.now().isoformat()
                # Track physical close time (independent of recommendations)
                if (
                    self._today_record
                    and self._today_record.windows_physically_opened
                    and self._today_record.window_physical_close_time is None
                ):
                    self._today_record.window_physical_close_time = dt_util.now().isoformat()
                await self.automation_engine.handle_all_doors_windows_closed()
                # Issue #489: post-decision refresh, mirroring the open path's
                # post-handle_door_window_open refresh above — covers the real-pause-
                # resume case (HVAC mode/temp restored, grace started), not just the
                # display-only case already handled by the top-of-function refresh.
                self.hass.async_create_task(self.async_request_refresh())
                await self._async_save_state()

    def _suppress_during_startup_coalescing(self, description: str) -> bool:
        """Return True (and log why) if the caller should bail due to active startup
        coalescing.

        Issue #321 established this suppression for _async_thermostat_changed; Issue
        #491 generalizes it to every override-detection listener via a single shared
        implementation, so a future new listener can't independently forget it — the
        exact gap that let #491 ship (neither fan listener had this guard).
        """
        if not self._startup_coalesce_active:
            return False
        _LOGGER.debug("Startup coalescing active — suppressing %s", description)
        return True

    async def _async_thermostat_changed(self, event: Event) -> None:
        """Track thermostat changes for learning (detect manual overrides)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        # Bug 1 (Issue #321): Suppress override detection during startup coalescing window
        if self._suppress_during_startup_coalescing(f"thermostat override detection for {new_state.state}"):
            return

        # Bug 3 (Issue #321): Per-temperature-tick nat-vent cycling re-evaluation.
        # Fires on every thermostat state event (including attribute-only changes) when
        # a nat-vent session is active so the fan cycles before the hard comfort-floor exit.
        _new_temp_attr = new_state.attributes.get("current_temperature")
        _old_temp_attr = old_state.attributes.get("current_temperature")
        if (
            _new_temp_attr is not None
            and _new_temp_attr != _old_temp_attr
            and self.automation_engine._natural_vent_active
        ):
            await self.automation_engine.nat_vent_temperature_check(
                float(_new_temp_attr), outdoor=self._last_outdoor_temp
            )

        # Issue #327: Thermostatic fan re-evaluation on every indoor temp tick.
        # Fires whenever the thermostat reports a new current_temperature and a CA fan is running
        # (nat-vent OR regular fan-only).  The engine method is idempotent; calling it here when
        # nat_vent_temperature_check already ran above is safe — they target different exit paths.
        if (
            _new_temp_attr is not None
            and _new_temp_attr != _old_temp_attr
            and (self.automation_engine._fan_active or self.automation_engine._natural_vent_active)
        ):
            await self.automation_engine.fan_thermostat_check(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                trigger="tick",
            )

        # Expected-state confirmation suppression: if thermostat is confirming an automation
        # command (same mode, within 2 minutes), this is not a user override.
        # Covers cloud-thermostat lag where _hvac_command_pending is already cleared by the time
        # the state-change event arrives (e.g. 3–30s for Ecobee/Nest cloud round-trips).
        _last_cmd_mode = self.automation_engine._last_commanded_hvac_mode
        _last_cmd_time = self.automation_engine._last_commanded_hvac_time
        # Single-setpoint only (Issue #301): the dual-setpoint heat_cool path is removed.
        # CA always sends one call with hvac_mode + single temperature; no _pending_setpoint_low/high.
        _is_expected_confirmation = (
            _last_cmd_mode is not None
            and _last_cmd_time is not None
            and new_state.state == _last_cmd_mode
            and (dt_util.now() - _last_cmd_time).total_seconds() < 120
        )

        # Detect manual HVAC override during a door/window pause.
        # Note: we intentionally do NOT require old_state == "off" here.
        # The async _set_hvac_mode("off") service call may not have
        # propagated to HA's state machine yet when the user quickly
        # turns HVAC back on, so old_state could still be the pre-pause
        # mode (e.g. "cool"). The paused_by_door flag is authoritative.
        # We DO require old_state != new_state to skip attribute-only events
        # (e.g. hvac_action idle→cooling) where the HVAC mode didn't change.
        if (
            self.automation_engine.is_paused_by_door
            and old_state.state != new_state.state
            and new_state.state not in ("off", "unavailable", "unknown")
        ):
            _any_command_pending = (
                self.automation_engine._hvac_command_pending
                or self.automation_engine._fan_command_pending
                or self.automation_engine._temp_command_pending
            )
            if _is_expected_confirmation:
                _LOGGER.debug(
                    "Skipping pause-override: thermostat confirmed automation command (mode=%s, commanded %.1fs ago)",
                    _last_cmd_mode,
                    (dt_util.now() - _last_cmd_time).total_seconds(),
                )
            elif not _any_command_pending and not self._is_recent_hvac_command(threshold_seconds=3.0):
                _LOGGER.info(
                    "Manual HVAC override detected during door/window pause: %s -> %s",
                    old_state.state,
                    new_state.state,
                )
                await self.automation_engine.handle_manual_override_during_pause(
                    old_mode=old_state.state,
                    new_mode=new_state.state,
                    classification_mode=(
                        self._current_classification.hvac_mode if self._current_classification else None
                    ),
                )
                self._feed_override_grace_fsm_on_detect("handle_manual_override_during_pause")
                self._cancel_all_debounce_timers()
            else:
                _LOGGER.debug(
                    "Skipping pause-override detection: HVAC mode change was automation-initiated "
                    "(hvac_pending=%s, fan_pending=%s, temp_pending=%s, recent_command=%s)",
                    self.automation_engine._hvac_command_pending,
                    self.automation_engine._fan_command_pending,
                    self.automation_engine._temp_command_pending,
                    self._is_recent_hvac_command(threshold_seconds=3.0),
                )
        elif (
            # Fix D (Issue #282): mode change to a DIFFERENT mode while grace is active.
            # The existing elif below guards with `not _manual_override_active`, so this
            # branch fires first and handles the re-override case.
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and self.automation_engine._manual_override_active
            and new_state.state != self.automation_engine._manual_override_mode
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self.automation_engine._temp_command_pending
            and not self._is_recent_hvac_command()
            and not _is_expected_confirmation
        ):
            # User switched to a different mode while grace was running — clear the
            # old override and register the new one so the grace period restarts.
            _LOGGER.info(
                "New mode change during active override grace: %s → was overriding %s — restarting",
                new_state.state,
                self.automation_engine._manual_override_mode,
            )
            # Issue #664: this is the real production trigger for OVERRIDE_SUPERSEDED,
            # not OVERRIDE_CANCELLED — clear_manual_override() here never calls
            # _cancel_grace_timers() (Issue #282's "Fix D" deliberately leaves the
            # still-running grace protecting the NEW override handle_manual_override()
            # is about to (re)detect below), unlike cancel_override()'s real
            # OVERRIDE_CANCELLED behavior which clears both confirm AND grace. Feeding
            # this as OVERRIDE_CANCELLED (as Issue #647 originally did, shadow-only)
            # would have made an authoritative FSM wrongly force grace to NONE here.
            ae = self.automation_engine
            _was_confirm_pending = ae._override_confirm_pending
            if _was_confirm_pending:
                ae._clear_override_confirm_action()

            from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

            ae._resolve_override_grace_fsm_state(kind=_OGFEventKind.OVERRIDE_SUPERSEDED)
            ae._clear_manual_override_active("new_override_during_grace")
            try:
                self._evaluate_override_grace_fsm(_OGFEventKind.OVERRIDE_SUPERSEDED)
            except Exception as fsm_exc:  # noqa: BLE001 — FSM errors must never affect production
                _LOGGER.warning(
                    "Override/grace FSM evaluation (supersession-driven) failed (isolated, no production impact): %s",
                    fsm_exc,
                )
            self.automation_engine.handle_manual_override(
                old_mode=old_state.state,
                new_mode=new_state.state,
                classification_mode=(self._current_classification.hvac_mode if self._current_classification else None),
            )
            self._feed_override_grace_fsm_on_detect("handle_manual_override")
        elif (
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and not self.automation_engine._manual_override_active
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self.automation_engine._temp_command_pending
            and not self._is_recent_hvac_command()
            and not _is_expected_confirmation
            and self._current_classification
            # Issue #618 investigation note: an earlier version of this fix flipped this
            # comparison to check classification.hvac_mode first. Reverted — the full test
            # suite caught test_heat_cool_override.py::test_heat_cool_to_cool_fires_override_
            # when_ca_commanded_heat_cool failing, which documents that comparing against
            # _last_commanded_hvac_mode (not classification.hvac_mode) was itself a deliberate
            # prior fix ("Bug C"): when CA is running heat_cool banding, its actual commanded
            # mode ("heat_cool") legitimately differs from classification's simplified
            # heat/cool/off recommendation, and a user switching away from heat_cool must still
            # be detected as a real override even though the new mode happens to match
            # classification. Flipping priority here silently reintroduced Bug C. The
            # 2026-08-10 incident's 14:20 "Manual override detected" was, on reflection,
            # correct: CA's last real command was genuinely "off" (from the WHF-suppression
            # bug fixed elsewhere in this issue), so a change away from that CA-issued state IS
            # a real, undirected deviation — PATH B's existing self-resolution (10-minute
            # confirm window, no grace started, "transient" notification) is the correct,
            # already-working handling for "turned out to match what CA wants anyway." See the
            # PR discussion for this issue for the full analysis.
            and new_state.state
            != (
                self.automation_engine._last_commanded_hvac_mode
                or (self._current_classification.hvac_mode if self._current_classification else None)
            )
        ):
            # Mode changed outside of door/window pause to something
            # different from what CA is actively controlling — manual override
            _LOGGER.info(
                "Manual HVAC override detected: %s -> %s (classification wants %s)",
                old_state.state,
                new_state.state,
                self._current_classification.hvac_mode,
            )
            with contextlib.suppress(Exception):
                _indoor = self._get_indoor_temp()
                _ov_weather_entity = self.config.get("weather_entity")
                _ov_weather_attrs = (
                    self.hass.states.get(_ov_weather_entity).attributes
                    if _ov_weather_entity and self.hass.states.get(_ov_weather_entity)
                    else {}
                )
                _outdoor_val = self._get_outdoor_temp(_ov_weather_attrs)
                _chart_hvac_ov = self._read_chart_hvac_action()
                _LOGGER.debug(
                    "chart_log append: event=override hvac=%r fan=%s",
                    _chart_hvac_ov,
                    self._fan_is_running(),
                )
                _band_lower_ov, _band_upper_ov = self._target_band_lower_upper_now()
                self._chart_log.append(
                    hvac=_chart_hvac_ov,
                    fan=self._fan_is_running(),
                    indoor=_indoor,
                    outdoor=_outdoor_val,
                    windows_open=self._any_sensor_open(),
                    windows_recommended=(
                        bool(self._current_classification.windows_recommended)
                        if self._current_classification
                        else False
                    ),
                    setpoint=self._read_chart_setpoint(),
                    event="override",
                    fan_running=self._fan_physically_running(),
                    nat_vent_active=bool(
                        self.automation_engine._natural_vent_active if self.automation_engine else False
                    ),
                    lower=_band_lower_ov,
                    upper=_band_upper_ov,
                    nat_vent_target=self._nat_vent_target_now(),
                )
            self.automation_engine.handle_manual_override(
                old_mode=old_state.state,
                new_mode=new_state.state,
                classification_mode=(self._current_classification.hvac_mode if self._current_classification else None),
            )
            self._feed_override_grace_fsm_on_detect("handle_manual_override")

        # HVAC runtime tracking via hvac_action (preferred) or mode
        new_action = new_state.attributes.get("hvac_action", "").lower()
        old_action = old_state.attributes.get("hvac_action", "").lower()
        running_actions = {"heating", "cooling"}

        if old_action in running_actions or new_action in running_actions:
            # At least one side shows active heating/cooling — hvac_action is providing a
            # meaningful signal, prefer it for precise on/off edge detection.
            was_running = old_action in running_actions
            is_running = new_action in running_actions
        else:
            # hvac_action gives no heating/cooling signal (both are "fan", "idle", or absent).
            # Some thermostats report hvac_action="fan" persistently (even when off/idle),
            # which would trap this branch indefinitely if we used the old `new_action and
            # old_action` guard.  Fall back to hvac_mode state for reliable edge detection.
            idle_modes = {"off", "unavailable", "unknown", ""}
            was_running = old_state.state not in idle_modes
            is_running = new_state.state not in idle_modes

        _LOGGER.info(
            "_async_thermostat_changed: hvac action=%s was_running=%s is_running=%s",
            new_action,
            was_running,
            is_running,
        )

        if not was_running and is_running:
            # HVAC just turned on — determine session_mode from hvac_action or hvac_mode
            self._hvac_on_since = dt_util.now()
            action = new_action
            if action == "heating":
                session_mode = "heat"
            elif action == "cooling":
                session_mode = "cool"
            elif new_state.state == "heat":
                # Fallback: some thermostats report hvac_action="fan" or "idle" briefly
                # at compressor startup before transitioning to "heating".
                session_mode = "heat"
            elif new_state.state == "cool":
                session_mode = "cool"
            elif new_state.state == "fan_only":
                session_mode = "fan_only"
            else:
                session_mode = None
            if session_mode:
                await self._start_hvac_observation(session_mode)
        elif was_running and not is_running:
            # HVAC just turned off — flush runtime and end active phase
            self._flush_hvac_runtime()
            for _hvac_ot in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                self._end_hvac_active_phase(_hvac_ot)
            self._hvac_on_since = None
            self.hass.async_create_task(self._async_save_state())
        elif was_running and is_running and old_action != new_action:
            # heat_cool mode: hvac_action switched heating↔cooling mid-session
            if old_action in running_actions and new_action in running_actions:
                _LOGGER.info(
                    "heat_cool mid-session switch %s → %s: abandoning current event",
                    old_action,
                    new_action,
                )
                for _hvac_ot in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                    self._abandon_observation(_hvac_ot, "heat_cool mode switch mid-session")
                new_session_mode = "heat" if new_action == "heating" else "cool"
                await self._start_hvac_observation(new_session_mode)

        # Issue #347: Post-startup reconcile for thermostat-autonomous fan-on.
        # When hvac_action transitions to "fan" (e.g. thermostat fan-circulation between
        # AC cycles) outside the startup coalesce window, and CA does not own the fan,
        # enforce the invariant: a running fan always has an explicit owner — adopt as
        # nat-vent or turn off, never indefinite limbo.
        # Guard: skip if fan_mode also changed in this same event — that signals a
        # user action whose override detection runs in the §9b block below (line ~3004).
        _old_fan_mode_347 = old_state.attributes.get("fan_mode", "")
        _new_fan_mode_347 = new_state.attributes.get("fan_mode", "")
        _ae_347 = self.automation_engine
        if (
            old_action != "fan"
            and new_action == "fan"
            and _old_fan_mode_347 == _new_fan_mode_347
            and not _ae_347._fan_active
            and not _ae_347._natural_vent_active
            and not _ae_347._fan_override_active
            # Issue #417: every sibling race-sensitive check in this file (e.g. lines
            # ~2875, ~2916, ~2939, ~3225, ~3300, and _async_fan_remote_changed() ~4106,
            # added for Issue #567) guards against CA's own in-flight fan commands with
            # these two checks — this was the one place that didn't, letting a CA-issued
            # nat-vent cycle-on transiently look "unowned" to this listener before
            # _activate_fan()'s flags settle.
            and not _ae_347._fan_command_pending
            and not self._is_recent_fan_command(threshold_seconds=30.0)
        ):
            _LOGGER.info(
                "hvac_action transitioned to fan while CA does not own fan — "
                "trigger=post_startup_reconcile old_action=%s",
                old_action,
            )
            # Issue #618: old_action in ("cooling", "heating") -> "fan" is the thermostat's own
            # normal post-compressor blower phase, not an out-of-band fan appearance — never let
            # the reconcile below force HVAC off because of it (2026-08-10 incident: this exact
            # transition force-cancelled AC that had started cooling 5 minutes earlier).
            _recent_hvac_session_ended_618 = old_action in ("cooling", "heating")
            _thermostat_fan_running_347 = self._derive_thermostat_fan_running_for_reconcile(
                fan_mode_attr=_new_fan_mode_347,
                hvac_action_attr=new_action,
            )
            await _ae_347.reconcile_fan_on_startup(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                # Issue #423: this is the exact site that could misfire on a WHF config —
                # hvac_action transitioning to "fan" is a thermostat-internal event unrelated
                # to a physically separate whole-house fan. Archetype-aware derivation checks
                # the real fan entity's state instead of assuming this transition means it.
                thermostat_fan_running=_thermostat_fan_running_347,
                any_sensor_open=self._any_sensor_open(),
                trigger="thermostat_state_change",
                recent_hvac_session_ended=_recent_hvac_session_ended_618,
            )

        # If thermostat is now fully off, clear any stale HVAC-based fan active flag.
        # Only applies to HVAC/Both fan modes — whole-house fans run independently.
        # Natural ventilation is intentionally hvac_mode=off + fan active — do not clear.
        ae = self.automation_engine
        if new_state.state == "off" and ae._fan_active and not ae._fan_override_active:
            _fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            if _fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH) and not ae._natural_vent_active:
                _LOGGER.warning("Thermostat set to off while HVAC fan was marked active — clearing stale fan state")
                ae._fan_active = False

        # Chart_log: event-driven write when hvac_action transitions in/out of heating/cooling.
        # 30-minute polling can miss short cycles entirely — this captures the start and end
        # edge of every real heating/cooling event regardless of when the next poll fires.
        _chart_active_actions = {"heating", "cooling"}
        _was_chart_active = old_action in _chart_active_actions
        _is_chart_active = new_action in _chart_active_actions
        if _was_chart_active != _is_chart_active:
            with contextlib.suppress(Exception):
                _LOGGER.debug(
                    "chart_log append: event=hvac_action_change hvac=%r fan=%s",
                    new_action,
                    self._fan_is_running(),
                )
                _band_lower_hac, _band_upper_hac = self._target_band_lower_upper_now()
                self._chart_log.append(
                    hvac=new_action,
                    fan=self._fan_is_running(),
                    indoor=self._get_indoor_temp(),
                    outdoor=None,
                    windows_open=self._any_sensor_open(),
                    windows_recommended=(
                        bool(self._current_classification.windows_recommended)
                        if self._current_classification
                        else False
                    ),
                    setpoint=self._read_chart_setpoint(),
                    event="hvac_action_change",
                    fan_running=self._fan_physically_running(),
                    nat_vent_active=bool(
                        self.automation_engine._natural_vent_active if self.automation_engine else False
                    ),
                    lower=_band_lower_hac,
                    upper=_band_upper_hac,
                    nat_vent_target=self._nat_vent_target_now(),
                )
                await self._executor_job(self._chart_log.save)

        # Bug 3 fix: Event-driven sampling for active HVAC observations.
        # The 5-min polling tick (_sample_all_observations) can miss short HVAC cycles
        # (<5 min) entirely, leaving active_samples with only the 1 initial sample.
        # With n=1 there are 0 consecutive pairs — OLS cannot run and k_active is never
        # fitted.  Adding a sample here on every thermostat state change (temperature
        # update, attribute change) during an active HVAC session ensures short cycles
        # accumulate enough samples for OLS.
        # Guard: only sample if HVAC is still actively heating/cooling (same phase),
        # and at least 60 seconds have elapsed since the last sample to avoid flooding.
        if new_action in ("heating", "cooling") and old_action == new_action:
            _active_obs_type = OBS_TYPE_HVAC_HEAT if new_action == "heating" else OBS_TYPE_HVAC_COOL
            self._ensure_pending_observations()
            _active_obs = self._pending_observations.get(_active_obs_type)
            _obs_phase_ok = _active_obs is not None and _active_obs.get("_phase") == "active"
            if _obs_phase_ok and _active_obs.get("status") == "monitoring":
                _active_start_str = _active_obs.get("active_start")
                try:
                    _active_start_ts = dt_util.parse_datetime(_active_start_str) if _active_start_str else None
                    _elapsed_active = (
                        (dt_util.now() - _active_start_ts).total_seconds() / 60.0 if _active_start_ts else 0.0
                    )
                except Exception:
                    _elapsed_active = 0.0
                # Decimation gate: at least 60 s between event-driven samples
                _last_evt_str = _active_obs.get("last_event_sample_time")
                _elapsed_since_last = 61.0  # default: allow first sample
                if _last_evt_str:
                    try:
                        _last_evt_ts = dt_util.parse_datetime(_last_evt_str)
                        if _last_evt_ts:
                            _elapsed_since_last = (dt_util.now() - _last_evt_ts).total_seconds()
                    except Exception:
                        pass
                if _elapsed_since_last >= 60.0:
                    _evt_sample = self._get_current_sample(_elapsed_active)
                    _active_samples = _active_obs.get("active_samples", [])
                    from custom_components.climate_advisor.const import (
                        THERMAL_MAX_ACTIVE_SAMPLES as _THERMAL_MAX_ACTIVE,
                    )

                    if len(_active_samples) < _THERMAL_MAX_ACTIVE:
                        _active_samples.append(_evt_sample)
                        _active_obs["last_event_sample_time"] = dt_util.now().isoformat()
                        _ind = _evt_sample.get("indoor_temp_f")
                        _cur_peak = _active_obs.get("peak_indoor_f")
                        if _ind and (_cur_peak is None or _ind > _cur_peak):
                            _active_obs["peak_indoor_f"] = _ind
                        _LOGGER.debug(
                            "Event-driven HVAC sample added: type=%s n_active=%d elapsed=%.1fmin",
                            _active_obs_type,
                            len(_active_samples),
                            _elapsed_active,
                        )

        # Detect manual override: temperature changed but not by us
        # In heat_cool mode the thermostat exposes target_temp_high/target_temp_low, not temperature.
        # _setpoint_override_detected gates Block 3 (fan_mode): a single thermostat event that
        # includes BOTH a setpoint change AND a fan_mode change must only fire the setpoint path.

        # Issue #359 Fix A: compute fan-cancel flag BEFORE Block 2 so it can guard setpoint
        # override detection.  When an ecobee user turns the fan off, the thermostat simultaneously
        # restores its comfort-program setpoint — Block 2 would otherwise misread that as a manual
        # setpoint override and start a grace period that blocks CA's intended setpoint.
        _b2_old_fan_mode = old_state.attributes.get("fan_mode")
        _b2_new_fan_mode = new_state.attributes.get("fan_mode")
        _fan_cancel_in_this_event = (
            _b2_old_fan_mode is not None
            and _b2_old_fan_mode == "on"
            and _b2_new_fan_mode is not None
            and _b2_new_fan_mode != "on"
        )

        _setpoint_override_detected = False
        if new_state.state == "heat_cool":
            _new_high = new_state.attributes.get("target_temp_high")
            _old_high = old_state.attributes.get("target_temp_high")
            _new_low = new_state.attributes.get("target_temp_low")
            _old_low = old_state.attributes.get("target_temp_low")
            _setpoint_changed = (_new_high != _old_high) or (_new_low != _old_low)
            # Use the cooling (high) setpoint as the representative value for override_details
            new_temp, old_temp = _new_high, _old_high
        else:
            new_temp = new_state.attributes.get("temperature")
            old_temp = old_state.attributes.get("temperature")
            _setpoint_changed = new_temp != old_temp

        if (
            _setpoint_changed
            and self._today_record
            and not self.automation_engine._temp_command_pending
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self._is_recent_hvac_command(threshold_seconds=30.0)
            and not self._is_recent_temp_command(threshold_seconds=30.0)
            and not self._is_recent_fan_command(threshold_seconds=30.0)
            and not _fan_cancel_in_this_event  # Issue #359 Fix A: fan-off echo suppresses grace
        ):
            # Mark setpoint detection as fired so Block 3 (fan_mode) is suppressed for this event.
            # A single event that changes both setpoint and fan_mode has one root cause; two
            # simultaneous grace periods from one event would confuse the automation engine.
            _setpoint_override_detected = True
            try:
                old_val = float(old_temp)
                new_val = float(new_temp)
                magnitude = round(new_val - old_val, 1)
                # Issue #583: increment the counter only after the append below is guaranteed
                # to succeed, so manual_overrides and len(override_details) can never diverge.
                self._today_record.manual_overrides += 1
                self._today_record.override_details.append(
                    {
                        "time": dt_util.now().strftime("%H:%M"),
                        "old_temp": old_val,
                        "new_temp": new_val,
                        "direction": "up" if magnitude > 0 else "down",
                        "magnitude": abs(magnitude),
                    }
                )
            except (ValueError, TypeError):
                # Issue #583: diagnostic evidence for the still-open Phase (b) investigation
                # (leading hypothesis: a WHF-triggered HVAC-off transition producing a None
                # temperature reading outside the ~30s command-recency window). Do NOT count
                # this as an override — old_temp/new_temp isn't a real setpoint pair.
                try:
                    _cmd_age = (
                        f"{(dt_util.now() - _last_cmd_time).total_seconds():.0f}s ago"
                        if _last_cmd_time is not None
                        else "None"
                    )
                except (TypeError, AttributeError):
                    _cmd_age = "None"
                _LOGGER.debug(
                    "Setpoint override swallowed (non-numeric temp): old_temp=%s new_temp=%s "
                    "hvac_mode=%s last_hvac_command=%s",
                    old_temp,
                    new_temp,
                    new_state.state,
                    _cmd_age,
                )
            _LOGGER.debug("Possible manual override detected: %s -> %s", old_temp, new_temp)
            await self._async_save_state()
            # Setpoint-only override: mode matches what CA is actively controlling.
            # Use _last_commanded_hvac_mode so heat_cool mode is handled (classification.hvac_mode
            # is always "cool"/"heat"/"off" — never "heat_cool" — so the old check missed heat_cool).
            ae = self.automation_engine
            _ca_active_mode = ae._last_commanded_hvac_mode or (
                self._current_classification.hvac_mode if self._current_classification else None
            )
            if (
                not ae._manual_override_active
                and not ae._override_confirm_pending
                and self._current_classification is not None
                and new_state.state == _ca_active_mode
            ):
                _LOGGER.info(
                    "Setpoint-only manual override detected: %s -> %s (mode=%s matches CA active mode)",
                    old_temp,
                    new_temp,
                    new_state.state,
                )
                ae.handle_manual_override(
                    source="setpoint",
                    old_mode=old_state.state,
                    new_mode=new_state.state,
                    classification_mode=(
                        self._current_classification.hvac_mode if self._current_classification else None
                    ),
                    old_setpoint_f=old_temp,
                    new_setpoint_f=new_temp,
                )
                self._feed_override_grace_fsm_on_detect("handle_manual_override")
        elif _fan_cancel_in_this_event and _setpoint_changed:
            # Issue #359 Fix A: fan-off echo branch — the thermostat restored its comfort-program
            # setpoint as a side-effect of the fan being turned off (ecobee behavior).  Do NOT start
            # a grace period.  Instead, schedule a re-assertion so CA's intended setpoint wins after
            # the thermostat settles.
            _LOGGER.info(
                "Setpoint override suppressed: fan-off echo detected, scheduling re-assertion (thermostat=%s)",
                new_temp,
            )
            self.hass.async_create_task(self._async_reassert_setpoint_after_fan_off())

        # Detect manual fan_mode attribute changes on thermostat (Issue #37)
        new_fan_mode = new_state.attributes.get("fan_mode")
        old_fan_mode = old_state.attributes.get("fan_mode")
        if (
            new_fan_mode is not None
            and old_fan_mode is not None
            and new_fan_mode != old_fan_mode
            and not self.automation_engine._fan_command_pending
            # Issue #774: an active fan override must not block re-detection of the fan
            # turning OFF — that's the override's own defining condition ending, and is
            # exactly what `_fan_cancel_in_this_event` (computed above) identifies. Only an
            # ON-direction re-detection while already overridden is the redundant case this
            # guard exists to skip.
            and (not self.automation_engine._fan_override_active or _fan_cancel_in_this_event)
            and not self.automation_engine._hvac_command_pending
            and not self._is_recent_hvac_command(threshold_seconds=30.0)
            and not _is_expected_confirmation
            and not self._is_recent_fan_command(threshold_seconds=30.0)
            and not _setpoint_override_detected
        ):
            _fan_ct = self.automation_engine._fan_command_time
            try:
                _fan_cmd_age = (
                    f"{(dt_util.now() - _fan_ct).total_seconds():.0f}s ago" if _fan_ct is not None else "None"
                )
            except (TypeError, AttributeError):
                _fan_cmd_age = "None"
            try:
                _hvac_cmd_age = (
                    f"{(dt_util.now() - _last_cmd_time).total_seconds():.0f}s ago"
                    if _last_cmd_time is not None
                    else "None"
                )
            except (TypeError, AttributeError):
                _hvac_cmd_age = "None"
            _LOGGER.info(
                "Manual HVAC fan_mode change detected: %s -> %s (fan_cmd=%s, hvac_cmd=%s, expected_confirmation=%s)",
                old_fan_mode,
                new_fan_mode,
                _fan_cmd_age,
                _hvac_cmd_age,
                _is_expected_confirmation,
            )
            # Issue #359 Fix B: direction-aware dispatch — fan-off routes to on_fan_turned_off()
            # (clears fan state, gates nat-vent re-activation) instead of handle_fan_manual_override()
            # (which sets the "user turned fan ON" override flag).
            if _fan_cancel_in_this_event:
                self.automation_engine.on_fan_turned_off(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))
            else:
                self.automation_engine.handle_fan_manual_override(
                    fan_before=str(old_fan_mode), fan_after=str(new_fan_mode)
                )
                self._feed_override_grace_fsm_on_detect("handle_fan_manual_override")

    async def _async_command_fan_entity(self, *, on: bool) -> bool:
        """Issue a turn_on or turn_off service call to the configured WHF fan entity (Issue #361).

        Used by command-only reconciliation; reuses the same domain-split pattern as
        automation.py ``_activate_fan()`` / ``_deactivate_fan()``.

        Issue #589: this is the automation's only action choke point that did not honor
        ``_automation_enabled``/``dry_run`` — disabling automation left this specific
        fan-reconciliation path free to keep issuing real hardware commands. Gated here,
        matching the "[DRY RUN] Would ..." convention used by automation.py's other
        choke points (``_activate_fan``/``_deactivate_fan``/``_set_hvac_mode``/etc.).

        Returns True if a real service call was issued, False if skipped (no fan entity
        configured, or automation disabled).
        """
        fan_entity_id = self.config.get(CONF_FAN_ENTITY)
        if not fan_entity_id:
            _LOGGER.debug("_async_command_fan_entity: no fan_entity configured — skipping")
            return False
        domain = fan_entity_id.split(".")[0]  # "fan" or "switch"
        service = "turn_on" if on else "turn_off"
        if not self._automation_enabled:
            _LOGGER.info(
                "[DRY RUN] Would command fan entity %s.%s entity_id=%s (automation disabled)",
                domain,
                service,
                fan_entity_id,
            )
            return False
        _LOGGER.debug(
            "_async_command_fan_entity: %s.%s entity_id=%s",
            domain,
            service,
            fan_entity_id,
        )
        await self.hass.services.async_call(domain, service, {"entity_id": fan_entity_id})
        return True

    async def _async_fan_entity_changed(self, event: Event) -> None:
        """Detect manual fan entity state changes (Issue #37)."""
        # Issue #361: command-only mode — entity state changes are command echoes, not physical signals.
        if not self._fan_state_feedback_enabled():
            _LOGGER.debug(
                "fan_entity state change ignored — fan_state_feedback=False"
                " (command echo only, not a physical override signal)"
            )
            return

        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        # Issue #491: suppress fan-entity override detection during startup coalescing —
        # a real WHF entity can report a transient state blip while HA is still settling
        # right after restart, which was previously misread as a fresh manual override.
        if self._suppress_during_startup_coalescing(f"fan override detection for {new_state.state}"):
            return

        if new_state.state == old_state.state:
            return

        # Issue #510: refresh immediately on every genuine physical transition, regardless of
        # whether an override is currently active or what caused it -- so the displayed status
        # never waits on the next scheduled coordinator poll (up to 30 min) to reflect reality.
        # Display-only: does not affect the override-detection/decision logic below. Previously
        # this refresh only fired from inside the "override already active" branch further down,
        # leaving the display stale whenever a physical transition happened with NO override
        # active (the exact mechanism behind Issue #510's reported staleness — a nat-vent
        # session flag masked a confirmed-running WHF for hours because nothing prompted a
        # recompute). Mirrors the identical Issue #489 pattern already used for door/window.
        _LOGGER.debug(
            "fan_entity physical transition %s -> %s — requesting refresh so displayed status stays live",
            old_state.state,
            new_state.state,
        )
        self.hass.async_create_task(self.async_request_refresh())

        # Issue #482: HA attaches the originating service call's Context to every
        # state-changed Event. When CA itself issued the fan command (via
        # automation.py's _call_fan_service_with_context), it records that Context's id.
        # If this event's own context.id (or its parent_id, for cases where the target
        # integration wraps CA's context in a child context) matches a recently-issued
        # CA command, that is an authoritative "CA caused this" signal — logged here on
        # EVERY change (matched or not) so a future investigation has direct evidence
        # instead of needing cross-source timestamp archaeology (the gap this issue
        # closes).
        #
        # Issue #561: matches against a short-lived set of recently-issued command
        # contexts (automation_engine.fan_command_context_matches()) rather than a
        # single last-write-wins id — two fan commands issued in close succession (e.g.
        # a duplicate reactivation attempt from a since-fixed reentrancy gap) could
        # otherwise have the second command's context overwrite the first's before this
        # listener evaluated the first command's resulting event, misattributing CA's
        # own action to the user.
        #
        # This is treated as an ADDITIONAL/corroborating signal alongside the
        # existing _fan_command_pending/timing checks below, not a replacement for
        # them: context propagation through third-party fan/switch integrations
        # (especially a one-way RF transmitter entity with no feedback of its own)
        # is not guaranteed reliable by HA core, so a non-match here does not prove
        # the change was external — it only fails to prove it was CA's. A match,
        # however, is conclusive.
        event_context = getattr(event, "context", None)
        event_context_id = getattr(event_context, "id", None) if event_context is not None else None
        event_context_parent_id = getattr(event_context, "parent_id", None) if event_context is not None else None
        # Issue #561: matches against a short-lived set of recently-issued CA command
        # contexts rather than a single last-write-wins id — a second overlapping fan
        # command could otherwise overwrite the first's id before this listener saw the
        # first command's resulting event, causing CA's own action to be misattributed
        # as a manual override.
        context_confirms_ca = self.automation_engine.fan_command_context_matches(
            event_context_id, event_context_parent_id
        )
        _LOGGER.debug(
            "fan_entity state change provenance: %s -> %s event_context_id=%s"
            " event_context_parent_id=%s context_confirms_ca=%s",
            old_state.state,
            new_state.state,
            event_context_id,
            event_context_parent_id,
            context_confirms_ca,
        )

        # Skip if this change was initiated by us — either the transient
        # command-pending bookkeeping (existing guard) or a confirmed event.context
        # match (Issue #482, additional signal).
        if self.automation_engine._fan_command_pending or context_confirms_ca:
            if context_confirms_ca and not self.automation_engine._fan_command_pending:
                _LOGGER.info(
                    "Fan entity change attributed to CA via event.context match (id=%s) —"
                    " suppressing (would otherwise have been evaluated as external)",
                    event_context_id,
                )
            return

        # Issue #787: `unavailable` on this entity can reflect a transient connectivity/API
        # dropout (e.g. an ESPHome encryption-handshake hiccup between Home Assistant and the
        # device) rather than a real physical change. The sibling remote listener
        # (_async_fan_remote_changed, Issue #495) already guards `unavailable` for the same
        # physical device family; this listener never had the equivalent treatment, so a brief
        # connectivity blip was misread as a manual override (going unavailable->on) followed
        # immediately by a manual cancel (on->unavailable), each starting its own bogus grace
        # period. Cross-check the ground-truth fan_state_entity (when fan_state_feedback is
        # enabled) before trusting either direction of an `unavailable` transition.
        if new_state.state == "unavailable" or old_state.state == "unavailable":
            ground_truth_on = self._get_fan_physical_state()
            if ground_truth_on is not None:
                # Use the ground-truth reading in place of the (possibly bogus) raw
                # entity state for the dispatch decision below. The existing dispatch
                # conditions already produce "no dispatch" whenever this matches what
                # CA currently believes (_fan_active) — no separate early-return needed
                # here, and none should be added: an `unavailable->on` recovery must
                # still correctly dispatch handle_fan_manual_override() if ground truth
                # shows a real state CA doesn't already expect (e.g. a genuine override
                # that happens to coincide with a connectivity blip).
                _LOGGER.info(
                    "Fan entity availability blip (%s -> %s) — using ground truth"
                    " (fan_state_entity) instead of the raw transition: physically %s",
                    old_state.state,
                    new_state.state,
                    "on" if ground_truth_on else "off",
                )
                is_on = ground_truth_on
            else:
                # No ground-truth fan_state_entity configured (Type-1 install) — there is no
                # way to confidently distinguish a connectivity blip from a real physical
                # change here. Do not dispatch a manual override/cancel off an `unavailable`
                # transition with no corroborating signal; a debounce-based mitigation for
                # this case is tracked as a follow-up (see Issue #787).
                _LOGGER.warning(
                    "Fan entity unavailable transition (%s -> %s) with no fan_state_entity"
                    " configured — cannot confirm this is a real change; skipping"
                    " override/cancel classification",
                    old_state.state,
                    new_state.state,
                )
                return
        else:
            is_on = new_state.state in {"on"}

        # Skip if fan override is already active AND this is not the fan turning off — the
        # display refresh already happened unconditionally above (Issue #510); an "on"
        # transition here is a redundant re-announcement, already accounted for. An "off"
        # transition, however, is the override's own defining condition ending and must not
        # be swallowed here (Issue #774) — it falls through to the direction-aware dispatch
        # below instead of returning.
        if self.automation_engine._fan_override_active and is_on:
            _LOGGER.info(
                "Fan/state entity changed while override already active (%s -> %s) — "
                "skipping override re-detection (display already refreshed above)",
                old_state.state,
                new_state.state,
            )
            return

        # Skip if a fan command was issued recently (cloud thermostat echo guard)
        if self._is_recent_fan_command(threshold_seconds=30.0):
            return

        if is_on and not self.automation_engine._fan_active:
            # Fan turned on externally — manual override
            _LOGGER.info(
                "Manual fan override detected: %s -> %s (integration expected fan off)",
                old_state.state,
                new_state.state,
            )
            self.automation_engine.handle_fan_manual_override(
                fan_before=str(old_state.state), fan_after=str(new_state.state), event_context_id=event_context_id
            )
            self._feed_override_grace_fsm_on_detect("handle_fan_manual_override")
        elif not is_on and (self.automation_engine._fan_active or self.automation_engine._fan_override_active):
            # Fan turned off externally — route to on_fan_turned_off() to clear fan state and
            # gate nat-vent re-activation (Issue #359 Fix C).  handle_fan_manual_override() is
            # the "user turned fan ON" path and must NOT be called here.
            #
            # Issue #774: the `_fan_override_active` half of this condition matters when an
            # active fan override (user turned the fan on, CA backed off) ends because the
            # fan turns back off — `_fan_active` stays False for the whole life of an override
            # (CA never "owns" that run), so checking `_fan_active` alone would silently drop
            # this dispatch for exactly the case an override protects.
            _LOGGER.info(
                "Fan turned off externally: %s -> %s (integration expected fan on)",
                old_state.state,
                new_state.state,
            )
            self.automation_engine.on_fan_turned_off(
                fan_before=str(old_state.state), fan_after=str(new_state.state), event_context_id=event_context_id
            )

    async def _async_fan_remote_changed(self, event: Event) -> None:
        """Handle a QuietCool RF wall remote event (Issue #486, extended by Issue #519).

        `event` is a state-changed event for the configured ``fan_remote_entity`` (an
        HA ``event.*`` entity — each remote press fires as a state change to a new
        timestamp, with the decoded command in ``attributes['event_type']``). See
        docs/fan-remote-spec.md for the firmware contract
        (gunkl/quietcool-house-fan).

        Issue #519: timer AND speed tokens are now both acted on (previously only timer);
        `on` is only meaningful as context within an already-open burst (a bare `on` with
        nothing else pending is NOT actionable on its own — physical fan-entity detection
        already covers plain on/off, see fan-remote-spec.md); `off` cancels any pending
        burst outright. Speed/timer events accumulate into a short-lived burst
        (`_arm_fan_remote_burst`/`_flush_fan_remote_burst`) instead of triggering
        `handle_fan_manual_override()` directly, so a single physical interaction that
        touches both fields (common — see docs/remote-capture-protocol.md) produces ONE
        decision, not two.
        """
        new_state = event.data.get("new_state")
        if not new_state or new_state.state in ("unknown", "unavailable"):
            return

        # Issue #495: dedup on the event's own timestamp (the entity's `state` field IS the
        # firmware event timestamp — confirmed via live history: e.g. state=
        # "2026-07-13T03:48:40.960+00:00"). The QuietCool event.* entity flaps to
        # `unavailable` at arbitrary times (observed independent of restart — 08:13, 08:46,
        # 16:58, 17:40, 18:03, 19:05 in one day) and restores its STALE last event_type with
        # the SAME timestamp. Without this guard, that restore is processed as a fresh press:
        # confirmed live — a phantom 2h override (`fan_manual_override
        # {remote_timer_hours: 2.0}`) fired at 16:58:02 with zero user action, exactly when
        # the entity restored to its stale timer_2h (frozen since 06:41). This generalizes
        # Issue #491's restart-only stale-event guard to every unavailable->restore flap.
        if new_state.state == self._last_fan_remote_event_ts:
            _LOGGER.debug(
                "Fan RF remote event ignored — already acted on this event (ts=%s, stale unavailable->restore)",
                new_state.state,
            )
            return

        event_type = new_state.attributes.get("event_type")

        # Issue #491: suppress during startup coalescing — the QuietCool remote's event.*
        # entity can re-announce its last retained event_type (a stale timer press) while
        # HA is still settling right after restart, indistinguishable from a fresh press.
        if self._suppress_during_startup_coalescing(f"fan remote event_type={event_type}"):
            return

        # Issue #567: echo guard — the QuietCool device transmits AND receives on the same
        # RF channel (see fan-remote-spec.md), so a CA-issued fan command can be heard back
        # by this same receive-side entity and misread as a fresh manual press. Mirrors the
        # existing echo guard in _async_fan_entity_changed() (see the sibling-site list at
        # _is_recent_fan_command()'s definition) — event.context matching isn't available
        # here since CA never calls a service on this receive-only entity.
        if self._is_recent_fan_command(threshold_seconds=30.0):
            _LOGGER.debug(
                "Fan RF remote event ignored — recent CA-issued fan command (echo guard), event_type=%s",
                event_type,
            )
            return

        is_timer, hours = parse_remote_timer_event(event_type)
        speed = parse_remote_speed_event(event_type)

        if is_timer or speed is not None:
            self._last_fan_remote_event_ts = new_state.state
            is_new_burst = self._fan_remote_burst is None
            burst = self._fan_remote_burst or _PendingFanRemoteBurst()
            if is_new_burst:
                # Snapshot "was it already running" exactly once, right now, before
                # anything in THIS interaction has had a chance to change the fan's state —
                # Part 2's override-vs-comfort decision needs the PRE-interaction state, not
                # a later re-read (see _flush_fan_remote_burst()'s docstring for why).
                physical_on = self._get_fan_physical_state()
                burst.was_running_before = (
                    physical_on if physical_on is not None else self.automation_engine._fan_active
                )
            if speed is not None:
                burst.speed = speed
            if is_timer:
                burst.timer_hours = hours
                burst.has_timer = True
            self._fan_remote_burst = burst
            self._arm_fan_remote_burst()
            _LOGGER.info(
                "Fan remote burst %s: speed=%s timer_hours=%s was_running_before=%s",
                "started" if is_new_burst else "extended",
                burst.speed,
                burst.timer_hours,
                burst.was_running_before,
            )
        elif event_type == "off":
            self._cancel_fan_remote_burst()  # supersedes any not-yet-applied override intent
        # else: unknown/ignored token, or a bare "on" with no open burst — unchanged behavior.

    def _cancel_fan_remote_burst(self) -> None:
        """Cancel any pending fan-remote burst without flushing it (Issue #519).

        Called when `off` arrives — turning the fan off supersedes any not-yet-applied
        override/comfort intent from a burst still being combined.
        """
        if self._fan_remote_burst_cancel is not None:
            self._fan_remote_burst_cancel()
            self._fan_remote_burst_cancel = None
        if self._fan_remote_burst is not None:
            _LOGGER.info("Fan remote burst cancelled — off received")
            self._fan_remote_burst = None

    def _arm_fan_remote_burst(self) -> None:
        """(Re-)arm the burst-combining window (Issue #519).

        Cancels any in-flight timer and starts a fresh one — called on every new
        speed/timer event within an open burst, so the window extends (rather than
        flushing twice) as long as related events keep arriving.
        """
        if self._fan_remote_burst_cancel is not None:
            self._fan_remote_burst_cancel()

        @callback
        def _burst_window_elapsed(_now: Any) -> None:
            self._fan_remote_burst_cancel = None
            self.hass.async_create_task(self._flush_fan_remote_burst())

        self._fan_remote_burst_cancel = async_call_later(self.hass, REMOTE_BURST_WINDOW_SECONDS, _burst_window_elapsed)

    async def _flush_fan_remote_burst(self) -> None:
        """Apply the accumulated burst's decision once the combining window elapses (Issue #519).

        Classification (see Part 2 of the #519 design):
        1. A timer selection (with or without speed) is ALWAYS an override — an explicit
           timer press is always manual intent, matching the pre-#519 behavior exactly.
        2. A bare speed press (no timer) is an override only if the fan was NOT already
           running before this interaction started (``was_running_before``, snapshotted at
           burst-open time in ``_async_fan_remote_changed`` — see that snapshot's own
           comment for why it must NOT be re-read here at flush time: by now the fan has
           typically already turned on, which would make a fresh read say "yes" for nearly
           every case, including genuine off->on overrides, silently misclassifying them as
           comfort-only). If the fan was already running, this is a comfort-only speed
           adjustment — record it via ``handle_fan_speed_observed()`` without arming an
           override/grace/HVAC-suppression.
        """
        burst = self._fan_remote_burst
        self._fan_remote_burst = None
        self._fan_remote_burst_cancel = None
        if burst is None:
            return

        if burst.has_timer:
            is_override = True
            reason = "timer selected"
        elif burst.speed is not None:
            is_override = not bool(burst.was_running_before)
            reason = (
                "fan was off/unknown before this press" if is_override else "fan already running, speed-only change"
            )
        else:
            return  # nothing actionable accumulated (shouldn't normally happen)

        _LOGGER.info(
            "Fan remote burst flushed: outcome=%s speed=%s timer_hours=%s has_timer=%s was_running_before=%s reason=%s",
            "override" if is_override else "comfort-only",
            burst.speed,
            burst.timer_hours,
            burst.has_timer,
            burst.was_running_before,
            reason,
        )
        if is_override:
            duration_seconds = burst.timer_hours * 3600 if burst.timer_hours is not None else None
            self.automation_engine.handle_fan_manual_override(
                fan_before="?",
                fan_after="on",
                duration_override=duration_seconds,
                remote_timer_hours=burst.timer_hours if burst.has_timer else None,
                remote_speed=burst.speed,
                is_remote_event=True,
            )
            self._feed_override_grace_fsm_on_detect("handle_fan_manual_override")
        else:
            self.automation_engine.handle_fan_speed_observed(burst.speed, is_remote_event=True)
        await self.async_request_refresh()

    def _resolve_fan_remote_speed_sensor(self) -> str | None:
        """Resolve the sibling ambient-speed `text_sensor` on the same ESPHome device as
        `fan_remote_entity`, via HA's entity/device registry (Issue #519).

        No new user-facing config: keyed entirely off the already-configured
        `CONF_FAN_REMOTE_ENTITY`. Caches the resolved entity_id once found (registry
        relationships don't change during a running session); NEVER caches a negative
        result — HA's entity/device registry can populate asynchronously at startup, so a
        too-early miss must self-correct on a later call rather than permanently disabling
        the feature for the rest of the session.

        Returns None if `fan_remote_entity` is unset, unregistered, has no device, or no
        sibling `sensor.*` entity matches `REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS` — in every
        case, the caller (`_read_fan_remote_speed`) degrades to "speed unknown," which IS
        the auto-detect + fallback mechanism for installs without the new firmware feature.
        """
        if self._fan_remote_speed_sensor_eid is not None:
            return self._fan_remote_speed_sensor_eid
        remote_entity_id = self.config.get(CONF_FAN_REMOTE_ENTITY)
        if not remote_entity_id:
            return None
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(remote_entity_id)
        if entry is None or entry.device_id is None:
            return None
        for sibling in er.async_entries_for_device(ent_reg, entry.device_id, include_disabled_entities=False):
            if sibling.domain != "sensor":
                continue
            object_id = sibling.entity_id.split(".", 1)[-1]
            if any(hint in object_id for hint in REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS):
                _LOGGER.info("Fan remote ambient speed sensor discovered: %s", sibling.entity_id)
                self._fan_remote_speed_sensor_eid = sibling.entity_id
                return sibling.entity_id
        return None

    def _read_fan_remote_speed(self) -> str | None:
        """Live, stateless read of the ambient current-speed sensor (Issue #519).

        Mirrors the existing `_get_thermostat_capabilities()` precedent (automation.py): a
        fresh capability/value read every call, degrading to None on anything missing —
        NOT an accumulated "have we ever seen a speed event" persisted boolean. This
        live-read-degrades-to-None behavior IS the auto-detect + fallback mechanism: no
        sibling sensor (older/un-updated firmware) or an unknown/unavailable state both
        resolve to None, identical to today's behavior for installs without this feature.
        """
        entity_id = self._resolve_fan_remote_speed_sensor()
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state

    def _read_live_remote_timer_provenance(self) -> tuple[float, float] | None:
        """Read the QuietCool RF remote's live re-announced timer state at restart (Issue #677).

        The clean-slate restart policy (#282/#327) never persists CA's own
        ``_fan_remote_timer_hours``/``_grace_active``/``_timer_boundary_settle_until`` —
        deliberately unchanged here, this reads nothing new into ``climate_advisor_state.json``.
        But the ``fan_remote_entity`` (the same ``event.*`` entity ``_async_fan_remote_changed()``
        listens to) independently re-announces its last retained ``event_type`` as the ESPHome
        device reconnects after restart (see docs/fan-remote-spec.md § Restart Behavior, already
        relied on by Issue #491's startup-coalescing suppression). This is a LIVE read of that
        re-announcement — the same category of live-entity read ``_do_startup_coalesce()``
        already performs for the thermostat mode/fan mode — not new persisted state.

        Without this, when the physical hardware timer naturally shuts the fan off hours after
        a restart wiped CA's in-memory timer bookkeeping, ``on_fan_turned_off()`` starts a fresh
        3-hour "manual" grace period with no memory this was expected, blocking nat-vent
        reactivation for hours despite favorable outdoor air (Issue #677).

        Returns ``(remaining_seconds, token_hours)`` if a genuine, still-unexpired timer token
        is currently retained by the entity, else ``None`` — mirrors ``_read_fan_remote_speed()``'s
        "returns None on anything not applicable" convention. Never raises: any parse failure or
        missing data degrades to None rather than affecting startup.
        """
        try:
            remote_entity_id = self.config.get(CONF_FAN_REMOTE_ENTITY)
            if not remote_entity_id:
                return None
            state = self.hass.states.get(remote_entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                _LOGGER.debug(
                    "RF timer provenance: fan_remote_entity %s unavailable at startup — nothing to resume",
                    remote_entity_id,
                )
                return None
            event_type = state.attributes.get("event_type")
            is_timer, token_hours = parse_remote_timer_event(event_type)
            if not is_timer or token_hours is None:
                # Not a timer token, or `timer_none` (no fixed duration to resume from) —
                # both degrade to "nothing to resume", matching parse_remote_timer_event()'s
                # own contract.
                _LOGGER.debug(
                    "RF timer provenance: event_type=%s is not a resumable timer token — nothing to resume",
                    event_type,
                )
                return None
            press_time = dt_util.parse_datetime(state.state)
            if press_time is None:
                _LOGGER.debug("RF timer provenance: could not parse press timestamp %r", state.state)
                return None
            expires_at = press_time + timedelta(hours=token_hours)
            now = dt_util.now()
            if now >= expires_at:
                # The timer's own natural end already passed — this is what makes a long
                # power outage self-resolving; no separate TTL constant needed.
                _LOGGER.info(
                    "RF timer provenance: %sh timer pressed at %s already expired before restart — nothing to resume",
                    token_hours,
                    state.state,
                )
                return None
            remaining_seconds = (expires_at - now).total_seconds()
            _LOGGER.info(
                "RF timer provenance: live re-announced %sh timer still has %.0fs remaining at"
                " restart — will re-arm on reconcile if fan is still physically running",
                token_hours,
                remaining_seconds,
            )
            return remaining_seconds, token_hours
        except Exception:
            _LOGGER.debug(
                "RF timer provenance read failed — treating as no active timer",
                exc_info=True,
            )
            return None

    async def _async_reassert_setpoint_after_fan_off(self) -> None:
        """Re-assert CA's intended setpoint after an ecobee fan-off echo (Issue #359 Fix A).

        Ecobee simultaneously restores its comfort-program setpoint when the user turns the fan
        off.  We wait 5 s for the thermostat to settle, then push CA's current classification
        back so the comfort-program setpoint does not win.
        """
        await asyncio.sleep(5)
        try:
            classification = self._current_classification
            if classification is None:
                _LOGGER.warning("Setpoint re-assertion after fan-off: no current classification — skipping")
                return
            await self.automation_engine.apply_classification(
                classification,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=self._get_indoor_temp(),
                nat_vent_cutoff=(getattr(self, "_nat_vent_plan", None) or {}).get("nat_vent_cutoff"),
                comfort_floor_crossing_time=(getattr(self, "_nat_vent_plan", None) or {}).get(
                    "comfort_floor_crossing_time"
                ),
            )
            _LOGGER.info(
                "Setpoint re-asserted after fan-off echo: reasserted day_type=%s hvac_mode=%s",
                classification.day_type,
                classification.hvac_mode,
            )
        except Exception:
            _LOGGER.warning(
                "Setpoint re-assertion after fan-off failed — thermostat left as-is",
                exc_info=True,
            )

    @callback
    def _on_whf_release_reclassify(self) -> None:
        """Called by the engine when a manual/remote WHF session releases HVAC suppression
        (Issue #495, ``AutomationEngine._release_whf_and_reclassify``).

        Reuses ``_async_reassert_setpoint_after_fan_off`` (Issue #359 Fix A) rather than a
        separate reclassify path — both scenarios are "a WHF-related HVAC suppression just
        ended, push CA's current classification back to the thermostat" and share the same
        5s-settle-then-reclassify mechanics.
        """
        self.hass.async_create_task(self._async_reassert_setpoint_after_fan_off())

    @callback
    def _on_post_grace_fan_check(self) -> None:
        """Called by automation engine after any grace period expires (Issue #359 Fix D).

        Schedules a fan-reconciliation check so an untracked fan is caught as soon as
        grace clears rather than waiting for the next 30-min coordinator cycle.
        """
        self.hass.async_create_task(self._async_post_grace_fan_reconcile())

    async def _async_post_grace_fan_reconcile(self) -> None:
        """After grace expires, check if fan is still running and reconcile if needed (Issue #359 Fix D)."""
        # Issue #361: command-only mode — no physical-state entity to read.
        # Reset last_commanded so the next _async_update_data() cycle re-asserts the desired state.
        if not self._fan_state_feedback_enabled():
            ae = self.automation_engine
            if ae is not None:
                desired = bool(ae._fan_active)
                _LOGGER.info(
                    "Post-grace fan reconcile (command-only): asserting desired_state=%s"
                    " (feedback unavailable — will re-assert on next cycle)",
                    "on" if desired else "off",
                )
                self._last_commanded_fan_state = None
            return

        ae = self.automation_engine
        if ae is None:
            return
        _cs_pg = self.hass.states.get(self.config.get("climate_entity", ""))
        if _cs_pg is None:
            return
        fan_mode = str(_cs_pg.attributes.get("fan_mode", ""))
        hvac_action = str(_cs_pg.attributes.get("hvac_action", "")).lower()
        # Issue #510 0.3: the outer gate previously used the THERMOSTAT's own fan_mode/
        # hvac_action directly (fan_mode == "on" or hvac_action == "fan"), unconditionally —
        # correct for FAN_MODE_HVAC (thermostat blower IS the fan) but wrong for a WHF
        # install where the fan is a physically separate device: the thermostat's own fan
        # attributes normally show no activity there, so the gate was always False and
        # reconcile_fan_on_startup() below never ran for WHF-only installs, even though the
        # archetype-aware value was ALREADY being computed correctly (just only as an inner
        # call argument, never consulted for the gate itself). Fixed by computing the
        # archetype-aware value once and using it for both the gate and the call — also
        # removes a redundant duplicate computation of "is the fan running" (previously
        # computed once wrong, once right).
        archetype_fan_running = self._derive_thermostat_fan_running_for_reconcile(
            fan_mode_attr=fan_mode,
            hvac_action_attr=hvac_action,
        )
        _LOGGER.info(
            "Post-grace fan check: fan_mode=%s hvac_action=%s archetype_fan_running=%s",
            fan_mode,
            hvac_action,
            archetype_fan_running,
        )
        if archetype_fan_running and hvac_action not in ("heating", "cooling"):
            await ae.reconcile_fan_on_startup(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                thermostat_fan_running=archetype_fan_running,
                any_sensor_open=self._any_sensor_open(),
                trigger="post_grace_expiry",
            )

    def _fan_state_feedback_enabled(self) -> bool:
        """Return True if the fan entity provides reliable physical-state feedback (Issue #361)."""
        return bool(self.config.get(CONF_FAN_STATE_FEEDBACK, False))

    def _get_fan_physical_state(self) -> bool | None:
        """Return whether the fan is physically running, or None if feedback is disabled (Issue #361).

        When ``fan_state_feedback`` is False (command-only mode), returns None — the entity
        state only echoes the last command and cannot be used for override detection.

        When ``CONF_FAN_STATE_ENTITY`` is configured and feedback is enabled, reads that entity's
        state for physical on/off detection.  Falls back to ``CONF_FAN_ENTITY`` state if the
        state entity is unavailable or not configured.  Logs a WARNING (once per unavailability)
        when falling back.

        Returns:
            True/False if the fan is physically running (feedback mode), None if command-only.
        """
        if not self._fan_state_feedback_enabled():
            _LOGGER.debug("_get_fan_physical_state: returning None — fan_state_feedback=False (command-only mode)")
            return None
        fan_state_entity_id = self.config.get(CONF_FAN_STATE_ENTITY)
        if fan_state_entity_id:
            state = self.hass.states.get(fan_state_entity_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                self._fan_state_entity_unavailable_warned = False  # reset on success
                return state.state.lower() in ("on", "true")
            # Unavailable or missing — warn once then fall back
            if not self._fan_state_entity_unavailable_warned:
                _LOGGER.warning(
                    "Fan state entity %s is unavailable — falling back to fan command entity for physical state",
                    fan_state_entity_id,
                )
                self._fan_state_entity_unavailable_warned = True
        # Fallback: read the fan command entity
        fan_entity_id = self.config.get(CONF_FAN_ENTITY)
        if fan_entity_id:
            fan_state = self.hass.states.get(fan_entity_id)
            if fan_state is not None:
                return fan_state.state.lower() in ("on", "true")
        return False

    def _run_invariant_watchdog(self, *, hvac_action: str | None) -> list:
        """Check hard system invariants and alert on any violation (Issue #749).

        Detect-and-alert only — never issues a corrective command. The automation
        engine's own command-layer fixes (e.g. ``_deactivate_fan()``) are the sole
        enforcement path for the invariants checked here; see invariant_watchdog.py's
        module docstring for the full rationale. Reads ground truth directly
        (``hvac_action`` from the caller, ``_get_fan_physical_state()`` here) rather than
        any derived/session-blended status — see Issue #739/#748.
        """
        violations = run_invariant_checks(
            hvac_action=hvac_action,
            whf_physically_on=self._get_fan_physical_state(),
            fan_mode=self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
        )
        for violation in violations:
            _LOGGER.critical("Hard invariant violated: %s — %s", violation.name, violation.detail)
            if self.automation_engine is None or self.automation_engine._recent_duplicate(
                "invariant_violation", (violation.name,), window_seconds=300
            ):
                continue
            self._emit_event("invariant_violation", {"invariant": violation.name, "detail": violation.detail})
            self.hass.async_create_task(
                self.automation_engine._notify(
                    f"{violation.detail} Climate Advisor is stepping in to prevent this, but"
                    " flagging it because it should never happen.",
                    "Climate Advisor — invariant violated",
                    notification_type="invariant_violation",
                )
            )
        return violations

    def _run_entity_health_check(self) -> list:
        """Detect missing/unavailable configured entities and notify on new outages (Issue #805).

        Runs every update cycle via ``run_entity_health_sweep()`` (entity_health.py).
        Debounced (edge-triggered): notifies once when an entity transitions from OK to
        missing/unavailable, then at most once per ``_ENTITY_HEALTH_REMINDER_SECONDS``
        while it stays missing — never every cycle, which is exactly the every-30-min
        spam the reporter's own log showed for a different bug and is what motivated
        this debounce shape here too. Recovery is logged at INFO but does not notify
        (keeps this quiet by default).

        Suppressed entirely during the startup-coalesce window
        (``self._startup_coalesce_active``) so entities that simply haven't loaded yet
        at boot never false-positive — the same window ``_compute_automation_status()``
        already uses to suppress alarm-shaped states for the same race condition.

        Isolated in its own try/except: a bug in this detector must never be able to
        abort the update cycle whose only other job that instant is detecting a
        *different* problem — this fix exists to catch silent failures, not add one.
        """
        if self._startup_coalesce_active:
            return []
        try:
            issues = run_entity_health_sweep(self.hass, self.config)
            self._process_entity_health_transitions(issues)
            return issues
        except Exception:
            _LOGGER.error("Entity health sweep failed internally", exc_info=True)
            return []

    def _process_entity_health_transitions(self, issues: list) -> None:
        """Diff the current sweep result against tracked state and notify on new/stale outages."""
        now = dt_util.now()
        current_keys = set()
        to_notify: list = []

        for issue in issues:
            current_keys.add(issue.config_key)
            tracked = self._entity_health_state.get(issue.config_key)
            if tracked is None:
                # ok -> missing/unavailable transition — a brand new outage.
                self._entity_health_state[issue.config_key] = {
                    "status": issue.status,
                    "first_seen": now,
                    "last_notified": now,
                }
                to_notify.append(issue)
            else:
                tracked["status"] = issue.status
                if (now - tracked["last_notified"]).total_seconds() >= _ENTITY_HEALTH_REMINDER_SECONDS:
                    tracked["last_notified"] = now
                    to_notify.append(issue)

        for config_key in list(self._entity_health_state.keys()):
            if config_key not in current_keys:
                _LOGGER.info("Entity health: %s is available again", config_key)
                del self._entity_health_state[config_key]

        if to_notify:
            self._notify_entity_health_issues(to_notify)

    def _notify_entity_health_issues(self, issues: list) -> None:
        """Log every issue unconditionally, then send one batched user notification.

        The unconditional log happens regardless of whether the push/email call below
        succeeds — ``notify_service`` is itself one of the monitored entities (Issue
        #805), so a broken notify target must not mean the failure goes unrecorded
        anywhere. The HA log is the fallback channel of last resort.
        """
        for issue in issues:
            log_fn = _LOGGER.error if issue.criticality == "critical" else _LOGGER.warning
            log_fn("Entity health: %s (%s) is %s", issue.friendly_name, issue.entity_id, issue.status)

        if len(issues) == 1:
            issue = issues[0]
            message = (
                f"{issue.friendly_name} ('{issue.entity_id}') isn't responding. Climate Advisor "
                "may not be able to control your HVAC correctly until it's fixed. Check "
                "Settings > Devices & Services."
            )
        else:
            names = ", ".join(f"{i.friendly_name} ({i.entity_id})" for i in issues)
            message = (
                f"{len(issues)} entities aren't responding: {names}. Climate Advisor may not be "
                "able to control your HVAC correctly until they're fixed. Check Settings > "
                "Devices & Services."
            )

        if self.automation_engine is not None:
            self.hass.async_create_task(
                self.automation_engine._notify(
                    message,
                    "Climate Advisor — entity not found",
                    notification_type="entity_health",
                )
            )

    def _should_run_untracked_fan_backstop(self, is_untracked: bool) -> bool:
        """Whether the periodic ``backstop_30min`` untracked-fan reconcile should fire now.

        Issue #627: previously this was an inline condition that checked only
        ``_fan_override_active``/``_grace_active`` — not ``_startup_coalesce_active``, unlike
        every sibling override-detection check in this file. That let the backstop fire on
        the very first ``_async_update_data()`` cycle after a restart, before
        ``restore_state()``'s Issue #263/#327 clean-slate settle window (300s) had elapsed,
        using a flag (``_fan_override_active``) that clean-slate had just wiped. It
        misclassified a whole-house fan still legitimately running under a pre-restart
        RF-remote timer as "unwarranted," turned it off, and released ``_pre_fan_hvac_mode``
        (the flag ``_whf_owns_hvac()`` depends on) — letting ``apply_classification()``
        commit the thermostat to Cool mode moments later with nothing left to stop it (a
        real AC/whole-house-fan mutex violation). Extracted to its own method so this exact
        predicate can be unit tested directly.
        """
        return (
            is_untracked
            and not self._startup_coalesce_active
            and not self.automation_engine._fan_override_active
            and not self.automation_engine._grace_active
        )

    def _should_run_regular_cycle_nat_vent_check(self) -> bool:
        """Whether the regular-cycle ``check_natural_vent_conditions()`` call should fire now.

        Issue #670: same bug class as #627 above, a different call site. This check can
        activate real hardware (the WHF) and had no ``_startup_coalesce_active`` gate at
        all — unlike every sibling override-detection check in this file. HA-restart-
        triggered extra coordinator refreshes (fan-state listener churn) gave this
        ungated call multiple chances to activate nat-vent for real before
        ``_do_startup_coalesce()``'s ``reconcile_fan_on_startup()`` — the single-shot,
        purpose-built startup-reconciliation mechanism (#321/#327) — had run, so the
        reconciliation's own decision arrived minutes late and against a fan state it
        never actually chose. Extracted to its own method so this exact predicate can be
        unit tested directly, matching ``_should_run_untracked_fan_backstop``'s pattern.
        """
        return self._any_sensor_open() and not self._suppress_during_startup_coalescing(
            "check_natural_vent_conditions (regular cycle)"
        )

    def _should_run_regular_cycle_window_cooling_check(self) -> bool:
        """Whether the regular-cycle ``check_window_cooling_opportunity()`` call should fire now.

        Issue #670: sibling gap to ``_should_run_regular_cycle_nat_vent_check`` above, same
        file, same missing gate, same fix. This check can command real AC and was never
        observed live only because it's independently gated to ``day_type == "hot"`` — the
        gap is real, just not yet triggered on a warm day.
        """
        return bool(self._today_record) and not self._suppress_during_startup_coalescing(
            "check_window_cooling_opportunity (regular cycle)"
        )

    def _derive_thermostat_fan_running_for_reconcile(self, *, fan_mode_attr: str, hvac_action_attr: str) -> bool:
        """Archetype-aware 'is a fan running' signal for reconcile_fan_on_startup() (Issue #423).

        Every caller of reconcile_fan_on_startup() previously derived this signal purely from
        the thermostat's own fan_mode/hvac_action attributes, regardless of configured fan_mode.
        That's correct for FAN_MODE_HVAC (the thermostat's own blower IS the fan), but wrong for
        FAN_MODE_WHOLE_HOUSE — a physically separate switch/relay whose real state the
        thermostat's attributes say nothing about. A thermostat-internal fan-schedule blip could
        (and did — Issue #423) cause reconcile to "adopt" a whole-house fan that was never
        actually turned on, permanently wedging _fan_active=True with no physical entity state
        ever changing to trigger the normal _async_fan_entity_changed() self-correction.

        FAN_MODE_HVAC: thermostat attributes ARE the fan — trust them, unchanged.
        FAN_MODE_WHOLE_HOUSE: use _get_fan_physical_state() (the real configured WHF entity)
            when fan_state_feedback is enabled; falls back to the thermostat signal only when
            physical feedback is unavailable (command-only mode) — there is no better ground
            truth in that case, so this is an explicit, documented fallback, not silent reuse
            of the wrong signal.
        FAN_MODE_BOTH: ORs both signals. This is a strict superset of the old (wrong) behavior,
            not a true per-device model — the WHF and HVAC blower can genuinely be in different
            states and a single boolean can't represent that. Tracked as a known gap in a
            separate follow-up issue; do not treat this as a full BOTH-archetype fix.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        thermostat_signal = fan_mode_attr == "on" or hvac_action_attr == "fan"
        if fan_mode == FAN_MODE_HVAC:
            return thermostat_signal
        if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            physical = self._get_fan_physical_state()
            if physical is not None:
                return physical if fan_mode == FAN_MODE_WHOLE_HOUSE else (physical or thermostat_signal)
            return thermostat_signal  # command-only mode — no independent ground truth available
        return thermostat_signal

    async def _initialize_hvac_session_from_current_state(self, climate_state: Any) -> None:
        """Late-start HVAC session when HA restarted mid-session (Issue #96).

        Sets session start from current time. Thermal observations will cover
        only the post-restart portion — better than zero observations.
        Called from _async_update_data() on first update if HVAC is already running.
        """
        self._hvac_on_since = dt_util.now()
        action = climate_state.attributes.get("hvac_action", "").lower()
        if action == "heating":
            session_mode = "heat"
        elif action == "cooling":
            session_mode = "cool"
        elif climate_state.state == "heat":
            session_mode = "heat"
        elif climate_state.state == "cool":
            session_mode = "cool"
        elif climate_state.state == "fan_only":
            session_mode = "fan_only"
        else:
            session_mode = None
        _LOGGER.warning(
            "Late-start HVAC session initialized: mode=%s (HVAC was running at HA startup — "
            "session duration will be shorter than actual)",
            session_mode,
        )
        if session_mode:
            await self._start_hvac_observation(session_mode)

    # ------------------------------------------------------------------
    # Thermal observation pipeline (Issue #114)
    # ------------------------------------------------------------------

    def _get_current_sample(self, elapsed_minutes: float) -> dict:
        """Build a sample dict from current sensor readings."""
        indoor = self._get_indoor_temp()
        weather_entity = self.config.get("weather_entity")
        weather_attrs = (
            self.hass.states.get(weather_entity).attributes
            if weather_entity and self.hass.states.get(weather_entity)
            else {}
        )
        outdoor = self._get_outdoor_temp(weather_attrs)
        return {
            "timestamp": dt_util.now().isoformat(),
            "indoor_temp_f": indoor if indoor is not None else 0.0,
            "outdoor_temp_f": outdoor if outdoor is not None else 0.0,
            "elapsed_minutes": elapsed_minutes,
        }

    def _update_pre_heat_buffer(self) -> None:
        """Append current reading to the rolling pre-heat buffer (max 15 entries).

        Called every update cycle when no active thermal event is running.
        """
        if self._pending_thermal_event is not None:
            return
        from .const import THERMAL_PRE_HEAT_BUFFER_MINUTES

        now = dt_util.now()
        sample = self._get_current_sample(0.0)
        sample["timestamp"] = now.isoformat()
        self._pre_heat_sample_buffer.append(sample)
        # Keep only entries within the buffer window
        cutoff = (now - timedelta(minutes=THERMAL_PRE_HEAT_BUFFER_MINUTES)).isoformat()
        self._pre_heat_sample_buffer = [s for s in self._pre_heat_sample_buffer if s["timestamp"] >= cutoff]
        # Hard cap at 15
        if len(self._pre_heat_sample_buffer) > 15:
            self._pre_heat_sample_buffer = self._pre_heat_sample_buffer[-15:]

    # ------------------------------------------------------------------
    # Thermal observation pipeline v3 (multi-type obs)
    # ------------------------------------------------------------------

    def _ensure_pending_observations(self) -> None:
        """Lazily initialize _pending_observations if missing (e.g. test stubs)."""
        if not hasattr(self, "_pending_observations"):
            self._pending_observations = {}

    async def _start_hvac_observation(self, session_mode: str) -> None:
        """Begin a new HVAC thermal observation (heat or cool)."""
        self._ensure_pending_observations()
        if not self.config.get("learning_enabled", True):
            return
        if not hasattr(self, "learning"):
            return
        obs_type = OBS_TYPE_HVAC_HEAT if session_mode == "heat" else OBS_TYPE_HVAC_COOL

        # Abandon any active non-HVAC observations — HVAC start contaminates them.
        # Reads the vent-split pair from _VENT_SPLIT_TYPES rather than hardcoding it a
        # third time (Issue #587).
        for _contaminated in (
            OBS_TYPE_PASSIVE_DECAY,
            *(t for t, _ in _VENT_SPLIT_TYPES),
            OBS_TYPE_SOLAR_GAIN,
        ):
            if _contaminated in self._pending_observations:
                self._commit_observation_if_sufficient(_contaminated, "hvac_started")

        if obs_type in self._pending_observations:
            self._abandon_observation(obs_type, "new HVAC session started")

        _LOGGER.info(
            "_start_hvac_observation: type=%s starting (prior obs=%s)",
            obs_type,
            list(self._pending_observations.keys()),
        )

        now = dt_util.now()
        pre_samples = []
        for s in self._pre_heat_sample_buffer:
            try:
                ts = dt_util.parse_datetime(s["timestamp"])
                elapsed = (now - ts).total_seconds() / 60.0 if ts else 0.0
            except Exception:
                elapsed = 0.0
            pre_samples.append(
                {
                    "timestamp": s["timestamp"],
                    "indoor_temp_f": s["indoor_temp_f"],
                    "outdoor_temp_f": s["outdoor_temp_f"],
                    "elapsed_minutes": -elapsed,
                }
            )

        indoor = self._get_indoor_temp()
        import uuid as _uuid_mod

        obs: dict = {
            "obs_type": obs_type,
            "obs_id": str(_uuid_mod.uuid4()),
            "start_time": now.isoformat(),
            "status": "monitoring",
            # NOTE: HVAC obs intentionally omit 'samples' key.  Non-HVAC (passive, fan,
            # vent, solar) use 'samples'.  HVAC obs use 'active_samples' (active phase)
            # and 'post_heat_samples' (post-heat phase).  Adding a 'samples': [] here
            # would shadow active_samples in every fallback read that uses
            # obs.get('samples', obs.get('active_samples', [])), causing n=0 in
            # rejection logs and discarding all HVAC obs on restart. (Bug 1 fix)
            "flags_at_start": {},
            "schema_version": 1,
            # HVAC-specific fields (compatible with _commit_event_from_dict HVAC path)
            "event_id": str(_uuid_mod.uuid4()),
            "created_at": now.isoformat(),
            "hvac_mode": session_mode,
            "session_mode": session_mode,
            "active_start": now.isoformat(),
            "active_end": None,
            "stabilized_at": None,
            "pre_heat_samples": pre_samples,
            "active_samples": [],
            "post_heat_samples": [],
            "start_indoor_f": indoor,
            "end_indoor_f": None,
            "peak_indoor_f": indoor,
            "start_outdoor_f": None,
            "session_minutes": None,
            "_phase": "active",
        }
        first_sample = self._get_current_sample(0.0)
        obs["active_samples"].append(first_sample)
        obs["start_outdoor_f"] = first_sample["outdoor_temp_f"]

        # Capture setpoint for diagnostic storage — not used in swing formula
        _climate_id = self.config.get("climate_entity", "")
        _cs_sw = self.hass.states.get(_climate_id) if _climate_id else None
        if _cs_sw is not None:
            _sp = _cs_sw.attributes.get("target_temperature")
            if _sp is None:
                _sp = _cs_sw.attributes.get("target_temp_low" if session_mode == "heat" else "target_temp_high")
            if _sp is not None:
                with contextlib.suppress(ValueError, TypeError):
                    obs["setpoint_f"] = round(float(_sp), 1)

        self._pending_observations[obs_type] = obs
        await self._executor_job(self.learning.save_state)
        _LOGGER.info(
            "Thermal HVAC observation started: obs_id=%s mode=%s indoor=%.1f°F",
            obs["obs_id"],
            session_mode,
            indoor if indoor is not None else 0.0,
        )

    def _sample_all_observations(self) -> None:
        """Sample all active observations and check trigger conditions for new ones."""
        self._ensure_pending_observations()
        if not self.config.get("learning_enabled", True):
            return
        if not hasattr(self, "learning"):
            return

        indoor = self._get_indoor_temp()
        outdoor = getattr(self, "_last_outdoor_temp", None)

        # Issue #130 D16: Use last-known outdoor temp if current reading is unavailable.
        # Outdoor temp changes slowly; a 30-min-stale reading is accurate to ±2°F —
        # sufficient for trigger gating and OLS.  Better than skipping samples entirely.
        if outdoor is None:
            _last_known = getattr(self, "_last_known_outdoor_f", None)
            _last_known_ts = getattr(self, "_last_known_outdoor_ts", None)
            if (
                _last_known is not None
                and _last_known_ts is not None
                and (dt_util.now() - _last_known_ts).total_seconds() < 1800  # 30 min
            ):
                outdoor = _last_known
        if outdoor is not None and outdoor != getattr(self, "_last_known_outdoor_f", None):
            self._last_known_outdoor_f = outdoor
            self._last_known_outdoor_ts = dt_util.now()

        ae = self.automation_engine

        now = dt_util.now()

        # A. Sample all active observations
        for obs_type, obs in list(self._pending_observations.items()):
            if obs.get("status") != "monitoring":
                continue

            if indoor is None:
                continue

            if obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                phase = obs.get("_phase", "active")
                active_start_str = obs.get("active_start")
                try:
                    active_start = dt_util.parse_datetime(active_start_str) if active_start_str else now
                except Exception:
                    active_start = now
                elapsed = (now - active_start).total_seconds() / 60.0

                sample = self._get_current_sample(elapsed)
                if phase == "active":
                    samples = obs["active_samples"]
                    if len(samples) < THERMAL_MAX_ACTIVE_SAMPLES:
                        samples.append(sample)
                    cur_peak = obs.get("peak_indoor_f")
                    if indoor and (cur_peak is None or indoor > cur_peak):
                        obs["peak_indoor_f"] = indoor
                else:  # post_heat
                    samples = obs["post_heat_samples"]
                    if len(samples) < THERMAL_MAX_POST_HEAT_SAMPLES:
                        samples.append(sample)
            else:
                # passive/fan/vent/solar: append to samples list
                elapsed = 0.0
                start_str = obs.get("start_time")
                if start_str:
                    try:
                        start_ts = dt_util.parse_datetime(start_str)
                        if start_ts:
                            elapsed = (now - start_ts).total_seconds() / 60.0
                    except Exception:
                        pass
                sample = self._get_current_sample(elapsed)
                samples_list = obs.setdefault("samples", [])
                if len(samples_list) >= THERMAL_MAX_OBS_SAMPLES:
                    self._commit_observation_if_sufficient(obs_type, "max_samples_reached")
                else:
                    # H1: per-type decimation gate — slow phenomena at full poll rate yield noise
                    _interval_map = {
                        OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
                        OBS_TYPE_VENT_WINDOW_DECAY: THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
                        OBS_TYPE_VENT_FAN_DECAY: THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
                        OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_SAMPLE_INTERVAL_S,
                    }
                    _interval_s = _interval_map.get(obs_type, 0)
                    _last_s = obs.get("last_sample_time")
                    _elapsed_since_last = (
                        (now - dt_util.parse_datetime(_last_s)).total_seconds() if _last_s else _interval_s + 1
                    )
                    if _elapsed_since_last >= _interval_s:
                        if obs_type in (OBS_TYPE_VENT_WINDOW_DECAY, OBS_TYPE_VENT_FAN_DECAY):
                            _sf_offset = getattr(self, "_solar_phase_offset", THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT)
                            sample["solar_factor"] = _solar_factor(now.hour, _sf_offset)
                        samples_list.append(sample)
                        obs["last_sample_time"] = now.isoformat()

        # B. Check trigger conditions for new non-HVAC observations
        _hvac_active = (
            OBS_TYPE_HVAC_HEAT in self._pending_observations
            and self._pending_observations[OBS_TYPE_HVAC_HEAT].get("_phase") == "active"
        ) or (
            OBS_TYPE_HVAC_COOL in self._pending_observations
            and self._pending_observations[OBS_TYPE_HVAC_COOL].get("_phase") == "active"
        )
        # Also check live HVAC action from thermostat
        _cs = self.hass.states.get(self.config["climate_entity"])
        _hvac_action_str = _cs.attributes.get("hvac_action", "").lower() if _cs else ""
        _is_heating_cooling = _hvac_action_str in ("heating", "cooling")
        _fan_active = ae._fan_active or ae._natural_vent_active
        _sensor_open = self._any_sensor_open()

        if indoor is not None and outdoor is not None:
            _delta = abs(indoor - outdoor)

            _LOGGER.debug(
                "Thermal trigger eval: zone=%s indoor=%.1f outdoor=%.1f delta=%.1f "
                "fan=%s nat_vent=%s sensor_open=%s hvac_action=%s pending=%s",
                self.config.get("climate_entity"),
                indoor,
                outdoor,
                _delta,
                ae._fan_active,
                ae._natural_vent_active,
                _sensor_open,
                _hvac_action_str,
                list(self._pending_observations.keys()),
            )

            if (
                OBS_TYPE_PASSIVE_DECAY not in self._pending_observations
                and not _is_heating_cooling
                and not _hvac_active
                and not _fan_active
                and not _sensor_open
                and _delta >= THERMAL_PASSIVE_MIN_DELTA_F
            ):
                self._start_decay_observation(OBS_TYPE_PASSIVE_DECAY)

            # Issue #587: fan_only_decay (fan on, windows closed) is retired outright —
            # it is NOT folded into vent_fan_decay (which requires windows open AND fan
            # on). ventilated_decay is split into vent_window_decay (fan off) and
            # vent_fan_decay (fan on) via one shared table-driven loop rather than two
            # near-identical `if` blocks — see _VENT_SPLIT_TYPES' module docstring for
            # why (this codebase has a documented history of exactly that drift, in
            # nat-vent's parallel-function precedent).
            for _vt_type, _vt_fan_state in _VENT_SPLIT_TYPES:
                if (
                    _vt_type not in self._pending_observations
                    and _sensor_open
                    and _fan_active == _vt_fan_state
                    and not _is_heating_cooling
                    and _delta >= THERMAL_VENTILATED_MIN_DELTA_F
                ):
                    self._start_decay_observation(_vt_type)

            _hour = now.hour
            if (
                OBS_TYPE_SOLAR_GAIN not in self._pending_observations
                and not _is_heating_cooling
                and not _fan_active
                and not _sensor_open
                and THERMAL_SOLAR_DAYTIME_START_H <= _hour < THERMAL_SOLAR_DAYTIME_END_H
            ):
                self._start_decay_observation(OBS_TYPE_SOLAR_GAIN)

        # C. Check commit/abandon conditions for each monitoring observation
        for obs_type in list(self._pending_observations.keys()):
            obs = self._pending_observations.get(obs_type)
            if obs is None or obs.get("status") != "monitoring":
                continue

            if obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                # HVAC stabilization is handled by _check_hvac_stabilization
                continue

            samples_list = obs.get("samples", [])

            if obs_type == OBS_TYPE_PASSIVE_DECAY:
                # Issue #137: consecutive-pair OLS replaced by chart_log endpoint estimator.
                # passive_decay observation tracks passive conditions (no HVAC/fan/sensors);
                # when it ends, trigger the chart_log fit rather than running OLS.
                if _is_heating_cooling or _hvac_active:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "hvac_started")
                elif _sensor_open:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "sensor_opened")
                elif _fan_active:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "fan_activated")
                elif indoor is not None and outdoor is not None and abs(indoor - outdoor) < THERMAL_PASSIVE_MIN_DELTA_F:
                    recent_temps = [s["indoor_temp_f"] for s in samples_list[-5:]] if len(samples_list) >= 5 else []
                    if recent_temps and (max(recent_temps) - min(recent_temps)) < 0.1:
                        self._run_passive_chart_log_fit(backfill=False)
                        self._abandon_observation(obs_type, "equilibrium_reached")
                else:
                    _max_samples = THERMAL_ROLLING_MAX_WINDOW_MINUTES // (THERMAL_PASSIVE_SAMPLE_INTERVAL_S // 60)
                    if len(samples_list) >= _max_samples:
                        # Hard time cap reached — trigger fit and end observation
                        self._run_passive_chart_log_fit(backfill=False)
                        self._abandon_observation(obs_type, "max_window_reached")

            elif obs_type in (OBS_TYPE_VENT_WINDOW_DECAY, OBS_TYPE_VENT_FAN_DECAY):
                self._evaluate_vent_split_observation(
                    obs_type,
                    obs,
                    samples_list,
                    now,
                    indoor,
                    outdoor,
                    _fan_active,
                    _sensor_open,
                    _is_heating_cooling,
                    _hvac_active,
                )

            elif obs_type == OBS_TYPE_SOLAR_GAIN:
                # Two-threshold accumulation (Issue #126): signal = indoor ΔT sufficient
                _sg_temps = [s["indoor_temp_f"] for s in samples_list if "indoor_temp_f" in s]
                _sg_signal = (max(_sg_temps) - min(_sg_temps)) >= THERMAL_ROLLING_MIN_DELTA_T_F if _sg_temps else False
                if self._evaluate_rolling_window(obs_type, obs, _sg_signal, skip_delta_guard=False):
                    continue
                if _is_heating_cooling or _hvac_active:
                    self._abandon_observation(obs_type, "hvac_started")
                elif _sensor_open:
                    self._abandon_observation(obs_type, "sensor_opened")
                elif _fan_active:
                    self._abandon_observation(obs_type, "fan_activated")
                elif not (THERMAL_SOLAR_DAYTIME_START_H <= now.hour < THERMAL_SOLAR_DAYTIME_END_H):
                    self._abandon_observation(obs_type, "outside_daytime")
                elif len(samples_list) >= 5:
                    recent_indoor = [s["indoor_temp_f"] for s in samples_list[-5:]]
                    # Only abandon if 3+ consecutive samples are each lower than the previous
                    # (guards against brief cloud-pass dips triggering premature abandonment)
                    _falling_streak = sum(
                        1 for i in range(1, len(recent_indoor)) if recent_indoor[i] < recent_indoor[i - 1]
                    )
                    if _falling_streak >= 3:
                        self._commit_observation_if_sufficient(obs_type, "temperature_falling")
                    elif len(samples_list) >= THERMAL_SOLAR_MIN_SAMPLES and indoor is not None:
                        _first_ts = dt_util.parse_datetime(samples_list[0]["timestamp"])
                        elapsed_h = (now - _first_ts).total_seconds() / 3600.0 if _first_ts else 0.0
                        if elapsed_h > 0:
                            mean_rate = (
                                samples_list[-1]["indoor_temp_f"] - samples_list[0]["indoor_temp_f"]
                            ) / elapsed_h
                            if mean_rate >= THERMAL_SOLAR_MIN_RATE_F_PER_HR:
                                self._commit_observation_if_sufficient(obs_type, "insufficient_rate")

    def _evaluate_vent_split_observation(
        self,
        obs_type: str,
        obs: dict,
        samples_list: list[dict],
        now: datetime,
        indoor: float | None,
        outdoor: float | None,
        _fan_active: bool,
        _sensor_open: bool,
        _is_heating_cooling: bool,
        _hvac_active: bool,
    ) -> None:
        """Shared commit/abandon evaluation for both vent_window_decay and vent_fan_decay.

        Single source of truth for the split's commit/abandon logic (Issue #587) — do
        not reimplement this per-type; see the nat-vent parallel-function drift this
        pattern avoids (this codebase's own documented history of exactly that failure
        mode when two near-identical trigger/commit blocks are written separately).
        """
        my_fan_state = dict(_VENT_SPLIT_TYPES)[obs_type]
        run_chart_log_fit = (
            self._run_vent_window_chart_log_fit if not my_fan_state else self._run_vent_fan_chart_log_fit
        )

        # Two-threshold accumulation: signal = indoor sample range (max-min).
        # Uses indoor movement, not snapshot differential, so keep-alive fires when
        # the integer thermostat is flat even with a large indoor-outdoor gap.
        _temps = [s["indoor_temp_f"] for s in samples_list if "indoor_temp_f" in s]
        _signal_sufficient = (max(_temps) - min(_temps)) >= THERMAL_ROLLING_MIN_DELTA_T_F if _temps else False
        # Solar accumulation guard: during daytime, suppress early commit if sf_range
        # has not yet reached the 2-param OLS threshold.  Without this, obs commits at
        # 30 min (sf_range ≈ 0.05–0.15) before 2-param can fire, producing
        # ols_wrong_sign rejections on solar-gain mornings.  The 240-min hard cap in
        # _evaluate_rolling_window fires normally.
        _sf_vals = [s.get("solar_factor", 0.0) for s in samples_list if "solar_factor" in s]
        _sf_range = max(_sf_vals) - min(_sf_vals) if len(_sf_vals) >= 2 else 0.0
        if 8 <= now.hour < 18 and _sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE:
            _signal_sufficient = False
        if self._evaluate_rolling_window(obs_type, obs, _signal_sufficient, skip_delta_guard=True):
            return

        if _fan_active != my_fan_state:
            # Regime flipped to the sibling's — commit what's accumulated (valid data
            # for the regime it was collected under), let the sibling's trigger (same
            # poll cycle) pick up the continuation. Not a discard: samples collected
            # before the toggle are genuinely valid data for the regime they were
            # collected under — discarding would starve whichever type has the
            # shorter typical dwell time. Symmetric with the retired fan_only_decay's
            # own "fan_stopped → commit" convention. _commit_observation_if_sufficient
            # already falls back to _abandon_observation internally when the
            # accumulated count is below minimum, so short toggles are naturally
            # discarded via the existing mechanism.
            toggle_reason = "fan_activated" if not my_fan_state else "fan_stopped"
            self._commit_observation_if_sufficient(obs_type, toggle_reason)
            run_chart_log_fit(backfill=False)
        elif not _sensor_open:
            self._commit_observation_if_sufficient(obs_type, "sensors_closed")
            run_chart_log_fit(backfill=False)
        elif _is_heating_cooling or _hvac_active:
            run_chart_log_fit(backfill=False)
            self._abandon_observation(obs_type, "hvac_started")
        elif (
            len(samples_list) >= THERMAL_VENT_MIN_SAMPLES
            and indoor is not None
            and outdoor is not None
            and abs(indoor - outdoor) >= THERMAL_VENT_MIN_SIGNAL_F
        ):
            self._commit_observation_if_sufficient(obs_type, "insufficient_signal")

    # ------------------------------------------------------------------
    # Chart-log dual-estimator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_solar_hour(ts_str: str) -> bool:
        """Return True if the timestamp falls in local hours 08:00–19:59 (solar guard)."""
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            local_hour = dt_util.as_local(ts).hour
            return 8 <= local_hour <= 19
        except (ValueError, AttributeError):
            return False

    def _select_estimator(
        self,
        result_a: dict | None,
        result_b: dict | None,
    ) -> dict | None:
        """Choose between endpoint (A) and block-OLS (B) estimates.

        Decision table:
          A=no, B=no               → None
          A=yes, B=no              → A, grade=low
          A=no,  B=yes, R²<0.20   → None (B unreliable, A absent)
          A=no,  B=yes, R²≥0.20   → B, grade=low(R²<0.50) or medium(R²≥0.50)
          A=yes, B=yes, R²<0.20   → A, grade=low
          A=yes, B=yes, R²0.20-0.50, agree → B, grade=low
          A=yes, B=yes, R²0.20-0.50, disagree → A, grade=low
          A=yes, B=yes, R²≥0.50, agree → B, grade=medium
          A=yes, B=yes, R²≥0.50, disagree → A, grade=low
        """
        a_valid = result_a is not None and result_a.get("k") is not None
        b_valid = result_b is not None and result_b.get("k") is not None

        if not a_valid and not b_valid:
            return None

        if a_valid and not b_valid:
            chosen = dict(result_a)
            chosen["grade"] = "low"
            return chosen

        r2_b = result_b.get("r_squared") if result_b else None

        if not a_valid and b_valid:
            if r2_b is None or r2_b < THERMAL_DUAL_OLS_OK:
                return None
            chosen = dict(result_b)
            chosen["grade"] = "medium" if r2_b >= THERMAL_DUAL_OLS_GOOD else "low"
            return chosen

        # Both valid
        if r2_b is None or r2_b < THERMAL_DUAL_OLS_OK:
            chosen = dict(result_a)
            chosen["grade"] = "low"
            _LOGGER.info(
                "chart_log dual_estimator: k_A=%.4f k_B=%s R²_B=%s agree=%s → source=%s grade=%s",
                result_a["k"],
                f"{result_b['k']:.4f}" if result_b else "n/a",
                f"{r2_b:.2f}" if r2_b is not None else "n/a",
                "n/a",
                chosen["source"],
                chosen["grade"],
            )
            return chosen

        denom_agree = (abs(result_a["k"]) + abs(result_b["k"])) / 2.0
        agree = denom_agree > 0 and (abs(result_a["k"] - result_b["k"]) / denom_agree) <= THERMAL_DUAL_AGREE_REL

        if r2_b >= THERMAL_DUAL_OLS_GOOD and agree:
            chosen = dict(result_b)
            chosen["grade"] = "medium"
        elif r2_b >= THERMAL_DUAL_OLS_OK and agree:
            chosen = dict(result_b)
            chosen["grade"] = "low"
        else:
            chosen = dict(result_a)
            chosen["grade"] = "low"

        _LOGGER.info(
            "chart_log dual_estimator: k_A=%.4f k_B=%s R²_B=%s agree=%s → source=%s grade=%s",
            result_a["k"],
            f"{result_b['k']:.4f}" if result_b else "n/a",
            f"{r2_b:.2f}" if r2_b is not None else "n/a",
            agree,
            chosen["source"],
            chosen["grade"],
        )
        return chosen

    def _extract_passive_windows(self, entries: list[dict], days: int) -> list[list[dict]]:
        """Extract passive decay windows from chart_log entries.

        Regime: HVAC=off/idle, fan=off, windows=closed.
        Solar guard: rejects any window whose start OR end timestamp falls in local hours 08–19.
        """
        cutoff = dt_util.now() - timedelta(days=days)
        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush() -> None:
            if len(current) < 2:
                current.clear()
                return
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                ts1 = datetime.fromisoformat(current[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                elapsed_min = (ts1 - ts0).total_seconds() / 60.0
                # Solar guard: reject windows that start or end in daytime hours
                if (
                    elapsed_min >= THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES
                    and not self._is_solar_hour(current[0]["ts"])
                    and not self._is_solar_hour(current[-1]["ts"])
                ):
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = entry.get("hvac", "")
            fan = entry.get("fan")
            windows_open = entry.get("windows_open")

            if indoor is None or outdoor is None:
                _flush()
                continue

            hvac_idle = hvac in ("idle", "off", "", "fan") or (
                "heat" not in (hvac or "") and "cool" not in (hvac or "")
            )
            fan_off = fan is False or fan is None
            win_closed = not windows_open

            if not hvac_idle or not fan_off or not win_closed:
                _flush()
                continue

            current.append({"ts": ts_str, "indoor": float(indoor), "outdoor": float(outdoor)})

        _flush()
        return windows

    def _passive_endpoint_estimate(self, window: list[dict]) -> dict | None:
        """Compute endpoint k_passive estimate for a passive decay window.

        Returns {"k": float, "r_squared": None, "source": "endpoint", "grade": "low"} or None.
        """
        t_start = window[0]["indoor"]
        t_end = window[-1]["indoor"]

        try:
            ts0 = datetime.fromisoformat(window[0]["ts"])
            ts1 = datetime.fromisoformat(window[-1]["ts"])
            if ts0.tzinfo is None:
                ts0 = ts0.replace(tzinfo=UTC)
            if ts1.tzinfo is None:
                ts1 = ts1.replace(tzinfo=UTC)
            dt_hours = (ts1 - ts0).total_seconds() / 3600.0
        except (ValueError, KeyError):
            return None

        if abs(t_end - t_start) < THERMAL_CHART_LOG_PASSIVE_MIN_DT_F:
            return None
        if dt_hours < THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES / 60.0:
            return None

        # Issue #587 Defect A: root-finds k against the window's real per-sample
        # outdoor trace (RK4 forward-integration) instead of assuming a constant
        # window-average outdoor temperature — see compute_k_passive_endpoint().
        k = compute_k_passive_endpoint(window, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)
        if k is None:
            return None

        return {"k": k, "r_squared": None, "source": "endpoint", "grade": "low"}

    def _run_passive_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Estimate k_passive from chart_log passive-only windows using dual-estimator.

        Endpoint estimator (A) and block-OLS estimator (B) are both computed for each
        window. _select_estimator() picks the best result. The chosen source and grade
        are recorded in the observation.

        Solar guard: windows starting or ending in local hours 08–19 are rejected.

        If backfill=True, processes up to 30 days of history (called once at startup).
        If backfill=False, processes only the most recent complete passive window.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            return

        days = 30 if backfill else 2
        windows = self._extract_passive_windows(entries, days)
        if not windows:
            return

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        today_str = dt_util.now().strftime("%Y-%m-%d")

        for window in target_windows:
            result_a = self._passive_endpoint_estimate(window)
            b_raw = compute_k_passive_blocks(window)
            result_b = (
                {"k": b_raw[0], "r_squared": b_raw[1], "source": "block_ols", "grade": "low"}
                if b_raw is not None and b_raw[0] is not None
                else None
            )
            chosen = self._select_estimator(result_a, result_b)
            if chosen is None:
                continue

            k = chosen["k"]
            try:
                ts0 = datetime.fromisoformat(window[0]["ts"])
                ts1 = datetime.fromisoformat(window[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                dt_hours = (ts1 - ts0).total_seconds() / 3600.0
            except (ValueError, KeyError):
                continue

            t_start = window[0]["indoor"]
            t_end = window[-1]["indoor"]
            t_out_avg = sum(s["outdoor"] for s in window) / len(window)
            denom = t_start - t_out_avg
            # Diagnostic-only (approximate): the committed k itself comes from
            # _passive_endpoint_estimate() above (compute_k_passive_endpoint, Issue #587
            # Defect A), which uses the real per-sample outdoor trace rather than this
            # window-average. `ratio` here is retained purely as a diagnostic field on
            # the observation record, not as the actual k derivation.
            ratio = (t_end - t_out_avg) / denom if abs(denom) >= 0.01 else None

            obs = {
                "hvac_mode": "passive",
                "k_passive": k,
                "confidence_grade": chosen["grade"],
                "date": today_str,
                "source": chosen["source"],
                "r_squared": chosen.get("r_squared"),
                "elapsed_hours": round(dt_hours, 2),
                "delta_t_f": round(t_end - t_start, 2),
                "ratio": round(ratio, 4) if ratio is not None else None,
            }
            self.learning.record_thermal_observation(obs)
            committed += 1
            _LOGGER.debug(
                "chart_log passive: k=%.4f source=%s conf=%s dt=%.1fh dT=%.1fF",
                k,
                chosen["source"],
                chosen["grade"],
                dt_hours,
                t_end - t_start,
            )
            if chosen["source"] == "endpoint":
                # Shadow-log (Issue #587 Part 2.10 / Defect A observability): what the
                # old constant-outdoor closed-form formula would have produced for the
                # same window, alongside the new RK4/bisection k. Grep-able proof the new
                # estimator ran and by how much it diverged — a check that greps for
                # "new_k" and finds none is itself evidence the new code path never ran.
                k_old = math.log(ratio) / dt_hours if ratio is not None and ratio > 0 else None
                _LOGGER.info(
                    "thermal endpoint estimator [Issue #587]: obs_type=passive new_k=%.4f "
                    "old_formula_k=%s delta_pct=%s n=%d dt_hours=%.2f",
                    k,
                    f"{k_old:.4f}" if k_old is not None else "n/a",
                    f"{100 * (k - k_old) / k_old:.1f}%" if k_old else "n/a",
                    len(window),
                    dt_hours,
                )

        if committed > 0:
            _LOGGER.info(
                "chart_log passive: committed %d observations%s",
                committed,
                " (backfill)" if backfill else "",
            )

    def _extract_ventilated_windows(self, entries: list[dict], days: int, *, fan_state: bool) -> list[list[dict]]:
        """Extract ventilated decay windows from chart_log entries.

        Regime: HVAC=off/idle, windows=open, T_out < T_in throughout, fan/WHF state ==
        fan_state (Issue #587 — one parameterized function serving both vent_window_decay
        (fan_state=False) and vent_fan_decay (fan_state=True) call sites, rather than two
        near-duplicate bodies).
        Solar guard: rejects any window whose start OR end timestamp falls in local hours 08–19.
        """
        cutoff = dt_util.now() - timedelta(days=days)
        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush() -> None:
            if len(current) < 2:
                current.clear()
                return
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                ts1 = datetime.fromisoformat(current[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                elapsed_min = (ts1 - ts0).total_seconds() / 60.0
                # Solar guard: reject windows that start or end in daytime hours
                if (
                    elapsed_min >= THERMAL_CHART_LOG_VENT_MIN_MINUTES
                    and all(s["outdoor"] < s["indoor"] for s in current)
                    and not self._is_solar_hour(current[0]["ts"])
                    and not self._is_solar_hour(current[-1]["ts"])
                ):
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = entry.get("hvac", "")
            windows_open = entry.get("windows_open")
            fan_on = bool(entry.get("fan"))

            if indoor is None or outdoor is None:
                _flush()
                continue

            hvac_idle = hvac in ("idle", "off", "", "fan") or (
                "heat" not in (hvac or "") and "cool" not in (hvac or "")
            )
            win_open = bool(windows_open)

            if not hvac_idle or not win_open or fan_on != fan_state:
                _flush()
                continue

            current.append({"ts": ts_str, "indoor": float(indoor), "outdoor": float(outdoor)})

        _flush()
        return windows

    def _vent_endpoint_estimate(self, window: list[dict]) -> dict | None:
        """Compute endpoint k estimate for a ventilated decay window (vent_window or vent_fan).

        Returns {"k": float, "r_squared": None, "source": "endpoint", "grade": "low"} or None.

        Shared by both vent_window_decay and vent_fan_decay call sites (Issue #587) — the
        endpoint math doesn't care why the window is a decay window. This is the one
        function Defect A's RK4/bisection fix modifies exactly once, covering both
        call sites.
        """
        t_start = window[0]["indoor"]
        t_end = window[-1]["indoor"]

        try:
            ts0 = datetime.fromisoformat(window[0]["ts"])
            ts1 = datetime.fromisoformat(window[-1]["ts"])
            if ts0.tzinfo is None:
                ts0 = ts0.replace(tzinfo=UTC)
            if ts1.tzinfo is None:
                ts1 = ts1.replace(tzinfo=UTC)
            dt_hours = (ts1 - ts0).total_seconds() / 3600.0
        except (ValueError, KeyError):
            return None

        if abs(t_end - t_start) < THERMAL_CHART_LOG_PASSIVE_MIN_DT_F:
            return None
        if dt_hours < THERMAL_CHART_LOG_VENT_MIN_MINUTES / 60.0:
            return None

        # Issue #587 Defect A: root-finds k against the window's real per-sample
        # outdoor trace (RK4 forward-integration) instead of assuming a constant
        # window-average outdoor temperature — see compute_k_passive_endpoint().
        k = compute_k_passive_endpoint(window, THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX)
        if k is None:
            return None

        return {"k": k, "r_squared": None, "source": "endpoint", "grade": "low"}

    def _run_vent_window_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Thin wrapper: vent_window_decay (fan off) chart_log dual-estimator fit."""
        self._run_vent_chart_log_fit_impl(fan_state=False, hvac_mode_tag="vent_window", backfill=backfill)

    def _run_vent_fan_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Thin wrapper: vent_fan_decay (fan/WHF on) chart_log dual-estimator fit."""
        self._run_vent_chart_log_fit_impl(fan_state=True, hvac_mode_tag="vent_fan", backfill=backfill)

    def _run_vent_chart_log_fit_impl(self, *, fan_state: bool, hvac_mode_tag: str, backfill: bool) -> None:
        """Estimate k_vent_window/k_vent_fan from ventilated chart_log windows (dual-estimator).

        Endpoint estimator (A) and block-OLS estimator (B) are both computed for each
        window. _select_estimator() picks the best result.

        Natural regime filter: only windows where T_out < T_in throughout are used
        (overnight conditions), further filtered by fan_state.
        Solar guard: windows starting or ending in local hours 08–19 are rejected.

        If backfill=True, processes up to 30 days of history (once on startup).
        If backfill=False, processes only the most recent ventilated window.

        Shared body for both vent_window_decay (fan_state=False) and vent_fan_decay
        (fan_state=True) — kept as two thin wrappers (_run_vent_window_chart_log_fit /
        _run_vent_fan_chart_log_fit) around this impl, not one parameterized method with
        branching log lines, so _LOGGER messages and the committed obs["hvac_mode"] stay
        unambiguous per call site (Issue #587).
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            return

        days = 30 if backfill else 2
        windows = self._extract_ventilated_windows(entries, days, fan_state=fan_state)
        if not windows:
            return

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        today_str = dt_util.now().strftime("%Y-%m-%d")

        for window in target_windows:
            result_a = self._vent_endpoint_estimate(window)
            b_raw = compute_k_passive_blocks(window)
            result_b = (
                {"k": b_raw[0], "r_squared": b_raw[1], "source": "block_ols", "grade": "low"}
                if b_raw is not None and b_raw[0] is not None
                else None
            )
            chosen = self._select_estimator(result_a, result_b)
            if chosen is None:
                continue

            k = chosen["k"]
            try:
                ts0 = datetime.fromisoformat(window[0]["ts"])
                ts1 = datetime.fromisoformat(window[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                dt_hours = (ts1 - ts0).total_seconds() / 3600.0
            except (ValueError, KeyError):
                continue

            t_start = window[0]["indoor"]
            t_end = window[-1]["indoor"]
            t_out_avg = sum(s["outdoor"] for s in window) / len(window)
            denom = t_start - t_out_avg
            # Diagnostic-only (approximate): the committed k itself comes from
            # _vent_endpoint_estimate() above (compute_k_passive_endpoint, Issue #587
            # Defect A), which uses the real per-sample outdoor trace rather than this
            # window-average. `ratio` here is retained purely as a diagnostic field on
            # the observation record, not as the actual k derivation.
            ratio = (t_end - t_out_avg) / denom if abs(denom) >= 0.01 else None

            obs = {
                "hvac_mode": hvac_mode_tag,
                "k_passive": k,
                "confidence_grade": chosen["grade"],
                "date": today_str,
                "source": chosen["source"],
                "r_squared": chosen.get("r_squared"),
                "elapsed_hours": round(dt_hours, 2),
                "delta_t_f": round(t_end - t_start, 2),
                "ratio": round(ratio, 4) if ratio is not None else None,
            }
            self.learning.record_thermal_observation(obs)
            committed += 1
            _LOGGER.debug(
                "chart_log %s: k=%.4f source=%s conf=%s dt=%.1fh dT=%.1fF",
                hvac_mode_tag,
                k,
                chosen["source"],
                chosen["grade"],
                dt_hours,
                t_end - t_start,
            )
            if chosen["source"] == "endpoint":
                # Shadow-log (Issue #587 Part 2.10 / Defect A observability): what the
                # old constant-outdoor closed-form formula would have produced for the
                # same window, alongside the new RK4/bisection k. Grep-able proof the new
                # estimator ran and by how much it diverged — a check that greps for
                # "new_k" and finds none is itself evidence the new code path never ran.
                k_old = math.log(ratio) / dt_hours if ratio is not None and ratio > 0 else None
                _LOGGER.info(
                    "thermal endpoint estimator [Issue #587]: obs_type=%s new_k=%.4f "
                    "old_formula_k=%s delta_pct=%s n=%d dt_hours=%.2f",
                    hvac_mode_tag,
                    k,
                    f"{k_old:.4f}" if k_old is not None else "n/a",
                    f"{100 * (k - k_old) / k_old:.1f}%" if k_old else "n/a",
                    len(window),
                    dt_hours,
                )

        if committed > 0:
            _LOGGER.info(
                "chart_log %s: committed %d observations%s",
                hvac_mode_tag,
                committed,
                " (backfill)" if backfill else "",
            )

    def _maybe_run_periodic_solar_phase_fit(self) -> None:
        """Run the incremental daily solar phase re-fit if due (Issue #310).

        Fires at most once per calendar day after the one-shot startup backfill
        (_solar_phase_backfill=True). Uses backfill=False (last 2 days only).
        """
        if not self._solar_phase_backfill:
            return
        _today = dt_util.now().date()
        if self._last_solar_phase_fit_date == _today:
            return
        self._run_solar_phase_chart_log_fit(backfill=False)
        self._last_solar_phase_fit_date = _today
        _LOGGER.info("chart_log solar_phase: daily incremental re-fit complete (date=%s)", _today)

    def _run_solar_phase_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Estimate solar_phase_offset_h from daytime passive chart_log windows.

        Regime: HVAC=off, fan=off, windows_open=False, daytime local hours (8–20).
        Calls _estimate_solar_phase_offset() for each qualifying window.
        On success, updates EWMA via self.learning.update_solar_phase_offset().

        If backfill=True, processes up to 30 days of history (called once at startup).
        If backfill=False, processes only the most recent qualifying window.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            _LOGGER.debug("Solar phase fit: chart_log not initialized — skipping")
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            _LOGGER.debug("Solar phase fit: chart_log empty — skipping")
            return

        days = 30 if backfill else 2
        cutoff = dt_util.now() - timedelta(days=days)

        # Structured entry log: total entries and date range for observability
        try:
            _ts_first = entries[0].get("ts", "?")
            _ts_last = entries[-1].get("ts", "?")
        except (IndexError, AttributeError):
            _ts_first = _ts_last = "?"
        _LOGGER.info(
            "Solar phase fit: %d chart_log entries available, scanning last %d day(s) (%s–%s)",
            len(entries),
            days,
            _ts_first,
            _ts_last,
        )

        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush_solar() -> None:
            if len(current) < THERMAL_SOLAR_PHASE_MIN_ENTRIES:
                current.clear()
                return
            # Only keep windows that are clearly daytime (start hour 8–20)
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                local0 = dt_util.as_local(ts0)
                if 8 <= local0.hour < 20:
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = str(entry.get("hvac", "")).lower()
            fan = str(entry.get("fan", "")).lower()
            windows_open = entry.get("windows_open", False)

            if indoor is None or outdoor is None:
                _flush_solar()
                continue

            # Regime: HVAC off, fan off, windows closed
            _hvac_off = hvac in ("off", "idle", "")
            _fan_off = fan in ("off", "false", "")
            if not (_hvac_off and _fan_off and not windows_open):
                _flush_solar()
                continue

            # Daytime guard: entry must be in local hours 8–20
            try:
                local_ts = dt_util.as_local(ts)
            except Exception:
                _flush_solar()
                continue
            if not (8 <= local_ts.hour < 20):
                _flush_solar()
                continue

            current.append(entry)

        _flush_solar()

        if not windows:
            _current_offset = (
                getattr(self, "learning", None)
                and getattr(self.learning, "_state", None)
                and (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
            )
            _LOGGER.info(
                "Solar phase fit: 0 qualifying passive-daytime windows — offset unchanged at %.2fh",
                _current_offset if isinstance(_current_offset, (int, float)) else 0.0,
            )
            return

        _LOGGER.info(
            "Solar phase fit: %d passive-daytime windows found, evaluating%s",
            len(windows),
            " (backfill)" if backfill else "",
        )

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        rejected = 0

        for window in target_windows:
            obs, reject_reason = _estimate_solar_phase_offset(window)
            if obs is None:
                _LOGGER.debug(
                    "chart_log solar_phase: rejected window (%d entries): %s",
                    len(window),
                    reject_reason,
                )
                rejected += 1
                continue
            _old_offset = (
                (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
                if hasattr(self, "learning") and hasattr(self.learning, "_state")
                else None
            )
            self.learning.update_solar_phase_offset(obs, THERMAL_SOLAR_PHASE_ALPHA)
            _new_offset = (
                (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
                if hasattr(self, "learning") and hasattr(self.learning, "_state")
                else None
            )
            committed += 1
            _LOGGER.info(
                "Solar phase EWMA: observed=%.2fh old=%.2f→new=%.2fh (window %d entries)",
                obs,
                _old_offset if isinstance(_old_offset, (int, float)) else 0.0,
                _new_offset if isinstance(_new_offset, (int, float)) else obs,
                len(window),
            )
            _LOGGER.debug(
                "chart_log solar_phase: committed obs=%.2f (window %d entries)",
                obs,
                len(window),
            )

        _LOGGER.info(
            "Solar phase fit: %d/%d windows committed%s (%d rejected)",
            committed,
            len(target_windows),
            " (backfill)" if backfill else "",
            rejected,
        )

    def _run_ac_duty_solar_phase_fit(self) -> None:
        """Estimate solar phase offset from AC duty cycle pattern (Issue #312).

        Secondary estimator — only used when the primary (passive window) method has
        never produced an observation. Reads the chart_log, groups entries by local
        calendar date, applies quality filter, estimates peak-load hour, and updates
        the EWMA via learning.update_ac_duty_solar_phase_offset().

        Sets self._solar_phase_ac_backfill = True on completion.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            _LOGGER.debug("AC duty solar phase fit: chart_log not initialized — skipping")
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            _LOGGER.debug("AC duty solar phase fit: chart_log empty — skipping")
            return

        # Group entries by local calendar date
        from collections import defaultdict

        days: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            h = _entry_hour(entry)
            if h is None:
                continue
            try:
                day_str = datetime.fromisoformat(entry["ts"]).strftime("%Y-%m-%d")
            except (KeyError, ValueError):
                continue
            days[day_str].append(entry)

        committed = 0
        rejected = 0
        for day_str, day_entries in sorted(days.items()):
            ok, reason = _is_ac_duty_solar_day(day_entries)
            if not ok:
                rejected += 1
                _LOGGER.debug("AC duty solar phase: skip day=%s reason=%s", day_str, reason)
                continue
            offset = _estimate_ac_duty_solar_phase(day_entries)
            if offset is None:
                rejected += 1
                continue
            self.learning.update_ac_duty_solar_phase_offset(offset, day_str)
            committed += 1

        current: float | None = None
        if hasattr(self, "learning") and hasattr(self.learning, "_state"):
            current = (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_ac_h")
        _LOGGER.info(
            "AC duty solar phase fit: committed=%d rejected=%d current_offset=%s",
            committed,
            rejected,
            f"{current:.2f}h" if current is not None else "none",
        )
        self._solar_phase_ac_backfill = True

    def _maybe_run_periodic_solar_phase_fit(self) -> None:
        """Run the incremental daily solar phase re-fit if due (Issue #310/#312).

        Called once per day from _async_send_briefing after the primary backfill has
        completed. Runs both the primary passive-window estimator (backfill=False) and
        the secondary AC duty cycle estimator to pick up any new observations since the
        last backfill.

        No-ops until the initial backfill has been run at least once
        (_solar_phase_backfill must be True).
        """
        if not self._solar_phase_backfill:
            return
        _today = dt_util.now().date()
        if getattr(self, "_last_solar_phase_fit_date", None) == _today:
            return
        self._run_solar_phase_chart_log_fit(backfill=False)
        self._run_ac_duty_solar_phase_fit()
        self._last_solar_phase_fit_date = _today
        _LOGGER.info("chart_log solar_phase: daily incremental re-fit complete (date=%s)", _today)

    def _start_decay_observation(self, obs_type: str) -> None:
        """Create a new monitoring observation for a passive/fan/vent/solar type."""
        import uuid as _uuid_mod

        now = dt_util.now()
        obs: dict = {
            "obs_type": obs_type,
            "obs_id": str(_uuid_mod.uuid4()),
            "start_time": now.isoformat(),
            "status": "monitoring",
            "samples": [],
            "last_sample_time": None,
            "flags_at_start": {
                "sensor_open": self._any_sensor_open(),
                "fan_active": self.automation_engine._fan_active,
                "nat_vent_active": self.automation_engine._natural_vent_active,
            },
            "schema_version": 1,
        }
        self._pending_observations[obs_type] = obs
        _LOGGER.debug("Thermal decay observation started: obs_id=%s type=%s", obs["obs_id"], obs_type)

    def _end_hvac_active_phase(self, obs_type: str) -> None:
        """Transition HVAC observation active → post_heat when HVAC action stops."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None or obs.get("_phase") != "active":
            return
        now = dt_util.now()

        # Capture indoor temp at the exact HVAC-off moment so swing uses the true shutoff temperature.
        _final_indoor = self._get_indoor_temp()
        if _final_indoor is not None:
            try:
                _elapsed = (
                    now - dt_util.parse_datetime(obs.get("active_start", now.isoformat()))
                ).total_seconds() / 60.0
            except Exception:
                _elapsed = 0.0
            active_samples = obs.get("active_samples", [])
            if len(active_samples) < THERMAL_MAX_ACTIVE_SAMPLES:
                active_samples.append(self._get_current_sample(_elapsed))
            _cur_peak = obs.get("peak_indoor_f")
            if obs_type == OBS_TYPE_HVAC_COOL:
                # For cooling, peak is the minimum (lowest indoor temp reached)
                if _cur_peak is None or _final_indoor < _cur_peak:
                    obs["peak_indoor_f"] = _final_indoor
            else:
                # For heating (and any other type), peak is the maximum
                if _cur_peak is None or _final_indoor > _cur_peak:
                    obs["peak_indoor_f"] = _final_indoor

        obs["_phase"] = "post_heat"
        obs["active_end"] = now.isoformat()

        active_start_str = obs.get("active_start")
        try:
            active_start = dt_util.parse_datetime(active_start_str) if active_start_str else now
        except Exception:
            active_start = now
        obs["session_minutes"] = (now - active_start).total_seconds() / 60.0

        _LOGGER.info(
            "Thermal HVAC observation active → post_heat: obs_id=%s session=%.1f min",
            obs.get("obs_id", "?"),
            obs["session_minutes"],
        )

    async def _check_hvac_stabilization(self, obs_type: str) -> None:
        """Check if post-HVAC temperature has stabilized or timed out."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None or obs.get("_phase") != "post_heat":
            return

        active_end_str = obs.get("active_end")
        try:
            active_end = dt_util.parse_datetime(active_end_str) if active_end_str else dt_util.now()
        except Exception:
            active_end = dt_util.now()

        elapsed_post = (dt_util.now() - active_end).total_seconds() / 60.0

        if elapsed_post > THERMAL_POST_HEAT_TIMEOUT_MINUTES:
            _n_active = len(obs.get("active_samples", []))
            _n_post = len(obs.get("post_heat_samples", []))
            _LOGGER.info(
                "_check_hvac_stabilization: type=%s timeout n_active=%d n_post=%d elapsed_post=%.0fmin — abandoning",
                obs_type,
                _n_active,
                _n_post,
                elapsed_post,
            )
            self._abandon_observation(obs_type, "post_heat timeout exceeded")
            await self._executor_job(self.learning.save_state)
            return

        post_samples = obs.get("post_heat_samples", [])

        # Issue #130 D24: When k_vent_window proxy is available (bridge home), post-heat OLS
        # is not needed — k_passive comes from the proxy and k_active from single-point
        # timestamps.  Single-point only needs post[0] for the HVAC-off timestamp, so the
        # minimum drops from THERMAL_MIN_POST_HEAT_SAMPLES (4) to 1.  Proxy-unaware paths
        # (normal homes, fresh installs) are unchanged.
        _cache = getattr(self.learning, "_state", None)
        _cache = _cache.thermal_model_cache if _cache is not None else None
        _k_vent_window = _cache.get("k_vent_window") if isinstance(_cache, dict) else None
        _proxy_available = _k_vent_window is not None and _k_vent_window < 0
        _min_post = 1 if _proxy_available else THERMAL_MIN_POST_HEAT_SAMPLES

        if len(post_samples) < _min_post:
            return

        # Issue #130 D15: Remove stabilization-wait gate.  Once min samples are collected,
        # commit immediately via OLS — the R² already governs quality.  Waiting for ±0.3°F
        # stability (THERMAL_STABILIZATION_THRESHOLD_F) over the last 5 min is redundant
        # and systematically blocks short-cycle (5–30 min) observations from ever committing.
        obs["status"] = "stabilized"
        obs["stabilized_at"] = dt_util.now().isoformat()
        obs["end_indoor_f"] = post_samples[-1]["indoor_temp_f"]

        peak_f = obs.get("peak_indoor_f")
        end_f = obs["end_indoor_f"]

        # Issue #130 D25: Plateau guard validates post-heat decay quality for k_passive OLS.
        # When proxy is available, k_passive comes from k_vent_window — no OLS decay needed.
        # Bypass the guard so short-cycle bridge homes are not incorrectly abandoned.
        if not _proxy_available and peak_f is not None and (peak_f - end_f) < THERMAL_HVAC_MIN_DECAY_F:
            _n_active_pg = len(obs.get("active_samples", []))
            _n_post_pg = len(obs.get("post_heat_samples", []))
            _LOGGER.info(
                "_check_hvac_stabilization: type=%s plateau guard n_active=%d n_post=%d elapsed_post=%.0fmin",
                obs_type,
                _n_active_pg,
                _n_post_pg,
                elapsed_post,
            )
            _LOGGER.info(
                "Thermal HVAC plateau guard: obs_id=%s peak=%.2f end=%.2f decay=%.2f < %.2f — abandoning",
                obs.get("obs_id", "?"),
                peak_f,
                end_f,
                peak_f - end_f,
                THERMAL_HVAC_MIN_DECAY_F,
            )
            self._abandon_observation(obs_type, "plateau guard: insufficient post-heat decay")
            await self._async_save_state()
            return

        _LOGGER.info(
            "Thermal HVAC observation min-samples reached: obs_id=%s post_samples=%d — committing",
            obs.get("obs_id", "?"),
            len(post_samples),
        )
        await self._commit_observation(obs_type)

    async def _commit_observation(self, obs_type: str, force_grade: str | None = None) -> None:
        """Commit a pending observation to the learning engine."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None:
            return
        if not self.config.get("learning_enabled", True):
            self._pending_observations.pop(obs_type, None)
            await self._executor_job(self.learning.save_state)
            return

        obs_result, reject_code, r_squared = await self._executor_job(
            self.learning._commit_event_from_dict,
            obs,
            force_grade,
            obs_type,
        )

        if obs_result is not None:
            if self._today_record is not None and obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                self._today_record.thermal_session_count += 1
            self._pending_observations.pop(obs_type, None)
            await self._executor_job(self.learning.save_state)
        else:
            # Learning engine rejected (OLS bad fit, wrong sign, bounds, etc.).
            # Route through _abandon_observation so the rejection enters _rejection_log
            # and the health surface stays accurate.
            self._abandon_observation(
                obs_type,
                "ols_rejected",
                reason_code=reject_code or REJECT_OLS_BAD_FIT,
                r_squared=r_squared,
                n_required=THERMAL_MIN_DECAY_SAMPLES,
            )
            await self._executor_job(self.learning.save_state)

    def _abandon_observation(
        self,
        obs_type: str,
        reason: str,
        *,
        reason_code: str | None = None,
        r_squared: float | None = None,
        n_required: int | None = None,
        delta_t_required: float | None = None,
        elapsed_minutes: int | None = None,
    ) -> None:
        """Discard a pending observation and emit a structured rejection event."""
        self._ensure_pending_observations()
        obs = self._pending_observations.pop(obs_type, None)
        if obs is None:
            return
        if elapsed_minutes is None and obs is not None:
            _start_str = obs.get("start_time")
            if _start_str:
                try:
                    _start_ts = dt_util.parse_datetime(_start_str)
                    if _start_ts:
                        elapsed_minutes = int((dt_util.now() - _start_ts).total_seconds() / 60)
                except Exception:
                    pass
        # Bug 1 fix: For HVAC obs, prefer active_samples (or post_heat_samples when in
        # post_heat phase) over the generic 'samples' key.  Pre-fix HVAC obs dicts had
        # 'samples': [] which shadows active_samples in the fallback chain, causing n=0
        # to be logged even when active_samples has real data.
        _obs_type_ab = obs.get("obs_type", obs_type)
        _hvac_types_ab = {OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL}
        if _obs_type_ab in _hvac_types_ab:
            _phase_ab = obs.get("_phase", "active")
            samples = obs.get("post_heat_samples", []) if _phase_ab == "post_heat" else obs.get("active_samples", [])
            # If still empty, fall back to generic 'samples' (legacy migration path)
            if not samples:
                samples = obs.get("samples", [])
        else:
            samples = obs.get("samples", obs.get("active_samples", []))
        delta_f = 0.0
        if len(samples) >= 2:
            first = samples[0].get("indoor_temp_f", samples[0].get("indoor_f", 0))
            last = samples[-1].get("indoor_temp_f", samples[-1].get("indoor_f", 0))
            delta_f = round(abs(last - first), 2)
        _sf_vals_ab = [s.get("solar_factor", 0.0) for s in samples if "solar_factor" in s]
        _sf_range_ab = round(max(_sf_vals_ab) - min(_sf_vals_ab), 2) if len(_sf_vals_ab) >= 2 else 0.0
        _temps_ab = [s.get("indoor_temp_f", 0.0) for s in samples if "indoor_temp_f" in s]
        _dir_ab = (
            "rising"
            if len(_temps_ab) >= 2 and _temps_ab[-1] > _temps_ab[0] + 0.1
            else "falling"
            if len(_temps_ab) >= 2 and _temps_ab[-1] < _temps_ab[0] - 0.1
            else "flat"
        )
        _LOGGER.info(
            "Thermal obs abandoned [type=%s reason=%s n=%d/%s dt=%.2f°F/%s elapsed=%sm]",
            obs_type,
            reason_code or reason,
            len(samples),
            str(n_required) if n_required is not None else "?",
            delta_f,
            f"{delta_t_required:.2f}" if delta_t_required is not None else "?",
            str(elapsed_minutes) if elapsed_minutes is not None else "?",
        )
        event = {
            "obs_type": obs_type,
            "reason_code": reason_code or REJECT_ABANDONED,
            "n_samples": len(samples),
            "n_required": n_required,
            "r_squared": r_squared,
            "r_squared_required": THERMAL_MIN_R_SQUARED,
            "delta_t_f": delta_f,
            "delta_t_required": delta_t_required,
            "elapsed_minutes": elapsed_minutes,
            "sf_range": _sf_range_ab,
            "indoor_direction": _dir_ab,
            "timestamp": dt_util.now().isoformat(),
        }
        if not hasattr(self, "_rejection_log"):
            self._rejection_log = {}
        bucket = self._rejection_log.setdefault(obs_type, [])
        bucket.append(event)
        if len(bucket) > _REJECTION_LOG_CAP:
            bucket.pop(0)
        # Sync to LearningState so rejection_log is persisted by save_state()
        self.learning._state.rejection_log = self._rejection_log
        # Issue #491: async_add_executor_job() already returns a scheduled awaitable —
        # wrapping it in async_create_task() (which requires a coroutine, not a Future)
        # raised "TypeError: a coroutine was expected, got <Future ...>" on every restart
        # that hit this abandonment path, crashing the whole coordinator update.
        self._executor_job(self.learning.save_state)

    def _build_learning_health(self) -> dict:
        """Aggregate _rejection_log into a per-obs-type health dict for get_thermal_model().

        Returns a dict keyed by obs_type, each value containing:
          - attempts: committed + total rejections
          - committed: number of successfully committed observations
          - rejections: per-reason-code counts
          - last_rejection: the most recent rejection event dict, or None
        """
        all_obs_types = [
            OBS_TYPE_PASSIVE_DECAY,
            OBS_TYPE_VENT_WINDOW_DECAY,
            OBS_TYPE_VENT_FAN_DECAY,
            OBS_TYPE_SOLAR_GAIN,
            OBS_TYPE_HVAC_HEAT,
            OBS_TYPE_HVAC_COOL,
        ]
        all_reason_codes = [
            REJECT_TOO_FEW_SAMPLES,
            REJECT_TOO_FEW_BLOCKS,
            REJECT_SMALL_DELTA,
            REJECT_OLS_BAD_FIT,
            REJECT_OLS_WRONG_SIGN,
            REJECT_OLS_BOUNDS,
            REJECT_ABANDONED,
            REJECT_WINDOW_TOO_SHORT,
            REJECT_NO_INTERIOR_PEAK,
        ]
        _hvac_mode_to_obs_type = {
            "passive": OBS_TYPE_PASSIVE_DECAY,
            "vent_window": OBS_TYPE_VENT_WINDOW_DECAY,
            "vent_fan": OBS_TYPE_VENT_FAN_DECAY,
            "solar": OBS_TYPE_SOLAR_GAIN,
            "heat": OBS_TYPE_HVAC_HEAT,
            "cool": OBS_TYPE_HVAC_COOL,
        }
        health = {}
        thermal_observations = getattr(self.learning._state, "thermal_observations", [])
        rejection_log = getattr(self, "_rejection_log", {})
        for obs_type in all_obs_types:
            events = rejection_log.get(obs_type, [])
            rejection_counts: dict[str, int] = {rc: 0 for rc in all_reason_codes}
            for ev in events:
                rc = ev.get("reason_code", REJECT_ABANDONED)
                if rc in rejection_counts:
                    rejection_counts[rc] += 1
            last = events[-1] if events else None
            committed = (
                sum(
                    1
                    for o in thermal_observations
                    if isinstance(o, dict) and _hvac_mode_to_obs_type.get(o.get("hvac_mode")) == obs_type
                )
                if isinstance(thermal_observations, list)
                else 0
            )
            health[obs_type] = {
                "attempts": committed + sum(rejection_counts.values()),
                "committed": committed,
                "rejections": rejection_counts,
                "last_rejection": last,
            }

        # Per-source observation counts (dual-estimator instrumentation)
        if isinstance(thermal_observations, list):
            health["source_endpoint_count"] = sum(
                1 for o in thermal_observations if isinstance(o, dict) and o.get("source") == "endpoint"
            )
            health["source_block_ols_count"] = sum(
                1 for o in thermal_observations if isinstance(o, dict) and o.get("source") == "block_ols"
            )
        else:
            health["source_endpoint_count"] = 0
            health["source_block_ols_count"] = 0

        return health

    def _commit_observation_if_sufficient(self, obs_type: str, abandon_reason: str) -> None:
        """Commit if enough samples exist, else abandon."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None:
            return
        samples = obs.get("samples", obs.get("active_samples", []))
        min_samples = {
            OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_MIN_SAMPLES,
            OBS_TYPE_VENT_WINDOW_DECAY: THERMAL_VENT_MIN_SAMPLES,
            OBS_TYPE_VENT_FAN_DECAY: THERMAL_VENT_MIN_SAMPLES,
            OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_MIN_SAMPLES,
            OBS_TYPE_HVAC_HEAT: THERMAL_MIN_POST_HEAT_SAMPLES,
            OBS_TYPE_HVAC_COOL: THERMAL_MIN_POST_HEAT_SAMPLES,
        }.get(obs_type, 10)
        if len(samples) >= min_samples:
            # H2: total-ΔT guard for short windows — prevent noise-fitting on near-flat data
            if len(samples) < 10:
                temps = [s["indoor_temp_f"] for s in samples]
                if temps and (max(temps) - min(temps)) < THERMAL_ROLLING_MIN_DELTA_T_F:
                    _LOGGER.debug(
                        "Abandoning %s: insufficient total delta in short window (%.3f°F < %.3f°F)",
                        obs_type,
                        max(temps) - min(temps),
                        THERMAL_ROLLING_MIN_DELTA_T_F,
                    )
                    self._pending_observations.pop(obs_type, None)
                    return
            obs["status"] = "committing"  # prevent duplicate commit on next poll
            self.hass.async_create_task(self._commit_observation(obs_type, force_grade="low"))
        else:
            self._abandon_observation(obs_type, abandon_reason)

    def _evaluate_rolling_window(
        self,
        obs_type: str,
        obs: dict,
        signal_sufficient: bool,
        skip_delta_guard: bool = False,
    ) -> bool:
        """Evaluate whether a condition-bounded observation should commit, keep alive, or abandon.

        Returns True if the observation was committed or abandoned (caller should ``continue``).
        Returns False if the observation should keep collecting samples.

        Two-threshold logic (Issue #126):
        - Before THERMAL_ROLLING_MIN_WINDOW_MINUTES AND no signal: keep collecting.
        - After THERMAL_ROLLING_MIN_WINDOW_MINUTES AND signal sufficient: commit now.
        - After THERMAL_ROLLING_MAX_WINDOW_MINUTES: commit if enough samples, else abandon.
        - Between min and max with insufficient signal: log and keep collecting.
        """
        now = dt_util.now()
        start_str = obs.get("start_time")
        elapsed = 0.0
        if start_str:
            try:
                start_ts = dt_util.parse_datetime(start_str)
                if start_ts:
                    elapsed = (now - start_ts).total_seconds() / 60.0
            except Exception:
                pass

        # Too early and no signal yet — keep accumulating
        if elapsed < THERMAL_ROLLING_MIN_WINDOW_MINUTES and not signal_sufficient:
            return False

        # Ready to commit: min window elapsed AND signal is present
        if elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES and signal_sufficient:
            self._commit_rolling_window_obs(obs_type, obs, skip_delta_guard=skip_delta_guard)
            return True

        # Hard cap reached — commit if enough samples, else abandon
        if elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES:
            samples = obs.get("samples", [])
            if len(samples) >= THERMAL_MIN_DECAY_SAMPLES + 1:
                self._commit_rolling_window_obs(obs_type, obs, skip_delta_guard=True)
            else:
                self._abandon_observation(
                    obs_type,
                    "max_window_exceeded",
                    reason_code="max_window_exceeded",
                    elapsed_minutes=int(elapsed),
                )
            return True

        # Between min and max window, signal not yet sufficient — log and keep alive
        if elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES:
            samples = obs.get("samples", [])
            temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
            delta = round(max(temps) - min(temps), 2) if temps else 0.0
            _LOGGER.info(
                "Thermal rolling window: obs_type=%s keeping alive "
                "(elapsed=%.0fmin delta=%.2f degF < %.2f degF needed, max=%dmin)",
                obs_type,
                elapsed,
                delta,
                THERMAL_ROLLING_MIN_DELTA_T_F,
                THERMAL_ROLLING_MAX_WINDOW_MINUTES,
            )
            # Trim oldest samples to prevent unbounded growth (~96 max at 5-min cadence over 4h)
            if len(samples) > 96:
                obs["samples"] = samples[-96:]
        return False

    def _commit_rolling_window_obs(self, obs_type: str, obs: dict, *, skip_delta_guard: bool = False) -> None:
        """Commit a rolling-window observation, bypassing the full min_samples threshold.

        Rolling windows are short by design (THERMAL_ROLLING_MIN_WINDOW_MINUTES = 30 min,
        THERMAL_PASSIVE_SAMPLE_INTERVAL_S = 300 s → ~6 samples). The normal min_samples
        threshold (e.g. THERMAL_PASSIVE_MIN_SAMPLES = 30) is calibrated for long overnight
        obs. For rolling windows we require ≥ THERMAL_MIN_DECAY_SAMPLES + 1 (= 5) samples
        and a ΔT ≥ THERMAL_ROLLING_MIN_DELTA_T_F to ensure the OLS regression has signal.

        ``skip_delta_guard`` should be set for vent/fan obs types where the signal
        guarantee is the indoor-outdoor differential (already checked by caller) rather
        than the indoor temperature trend.
        """
        self._ensure_pending_observations()
        samples = obs.get("samples", [])
        _start_ts = dt_util.parse_datetime(obs.get("start_time", "")) if obs.get("start_time") else None
        _elapsed = round((dt_util.now() - _start_ts).total_seconds() / 60.0, 1) if _start_ts else None
        _temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
        _outdoor = samples[-1].get("outdoor_temp_f") if samples else None
        _LOGGER.info(
            "Thermal rolling window: obs_type=%s n=%d elapsed=%.1fmin indoor=[%.1f..%.1f] (ΔT=%.2f°F) outdoor=%s",
            obs_type,
            len(samples),
            _elapsed or 0,
            min(_temps) if _temps else 0,
            max(_temps) if _temps else 0,
            (max(_temps) - min(_temps)) if _temps else 0,
            f"{_outdoor:.1f}" if _outdoor is not None else "?",
        )
        if len(samples) < THERMAL_MIN_DECAY_SAMPLES + 1:
            self._abandon_observation(obs_type, "window_elapsed_too_few_samples")
            return
        if not skip_delta_guard:
            temps = [s["indoor_temp_f"] for s in samples]
            if max(temps) - min(temps) < THERMAL_ROLLING_MIN_DELTA_T_F:
                _LOGGER.info(
                    "Abandoning rolling window %s: insufficient total ΔT (%.3f degF < %.3f degF)",
                    obs_type,
                    max(temps) - min(temps),
                    THERMAL_ROLLING_MIN_DELTA_T_F,
                )
                self._pending_observations.pop(obs_type, None)
                return
        obs["status"] = "committing"
        self.hass.async_create_task(self._commit_observation(obs_type, force_grade="low"))

    def _compute_next_action(
        self,
        c: DayClassification | None,
        *,
        indoor_temp: float | None = None,
        outdoor_temp: float | None = None,
        windows_physically_open: bool = False,
        ae: AutomationEngine | None = None,
    ) -> str:
        """Compute the next recommended human action for display.

        Every window/fan cooling (or heating) suggestion is gated on
        free_cooling_direction_ok() — the same live outdoor-vs-indoor direction
        guard already enforced in automation.py's economizer/nat-vent gates
        (Issue #327) — so this display text can never recommend an action that
        would work against the occupant's actual comfort goal (Issue #428).
        """
        unit = self.config.get("temp_unit", "fahrenheit")
        comfort_cool = self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)
        comfort_heat = self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT)
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        fan_enabled = fan_mode != FAN_MODE_DISABLED

        _LOGGER.info(
            "Next-action evaluation: zone=%s day_type=%s indoor=%s outdoor=%s windows_open=%s"
            " nat_vent=%s economizer=%s override=%s grace=%s paused_by_door=%s occupancy=%s",
            self.config.get("climate_entity"),
            c.day_type if c else "none",
            f"{indoor_temp:.1f}" if indoor_temp is not None else "?",
            f"{outdoor_temp:.1f}" if outdoor_temp is not None else "?",
            windows_physically_open,
            ae._natural_vent_active if ae else "?",
            ae._economizer_active if ae else "?",
            ae._manual_override_active if ae else "?",
            ae._grace_active if ae else "?",
            ae.is_paused_by_door if ae else "?",
            self._occupancy_mode,
        )

        def _decide(msg: str, *, warn: bool = False, **ctx: Any) -> str:
            ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items())
            (_LOGGER.warning if warn else _LOGGER.info)("Next-action: %s (%s)", msg, ctx_str)
            return msg

        def _close_windows_msg(od: float, id_: float) -> str:
            return f"Close windows — outdoor's not helping now ({format_temp(od, unit)} vs {format_temp(id_, unit)})."

        def _windows_helping_msg(od: float, id_: float) -> str:
            return f"Windows open — outdoor's helping ({format_temp(od, unit)} vs {format_temp(id_, unit)})."

        if not c:
            return _decide("Waiting for forecast data...")

        # Occupancy guards (Issue #527): these answer "what should I do" with "nothing —
        # you're not here," which is legitimate comfort-relevant guidance, not automation
        # mechanism narration, so they stay ahead of the comfort-guidance branches below.
        if self._occupancy_mode == OCCUPANCY_VACATION:
            return _decide(_pick_daily_line(_VACATION_ACTION_MESSAGES, "vacation"))
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return _decide(_pick_daily_line(_AWAY_ACTION_MESSAGES, "away"))

        # Issue #527: automation-mechanism state (override/grace/pause) used to be
        # narrated here directly, pre-empting the comfort guidance below and duplicating
        # what the Status card already says (_compute_automation_status()). That guard
        # block was removed — this card answers "what should I do for comfort," which is
        # true independent of whether the automation itself is currently paused/grace/
        # confirming. See docs/08-COMPUTATION-REFERENCE.md §9d for the prior (Issue #495)
        # instance of this exact duplication class, and CLAUDE.md's card-ontology table.

        now = dt_util.now().time()
        direction_ok = free_cooling_direction_ok(outdoor_temp, indoor_temp)

        # Issue #817: prefer the ODE-adjusted cutoff (self._nat_vent_plan, the same
        # value the briefing/TLDR table and the Next Automation card read) over the
        # static classifier close time — this card previously always used the static
        # hour, which could disagree with what "Today's Strategy" told the occupant.
        # Falls back to c.window_close_time when no plan is cached (e.g. before the
        # first cycle) or it has no cutoff for today.
        _plan = getattr(self, "_nat_vent_plan", None)
        _close_time_dt = _plan.get("nat_vent_cutoff") if _plan else None
        _effective_close_time = _close_time_dt.time() if _close_time_dt else c.window_close_time

        if c.windows_recommended:
            if c.window_open_time and now < c.window_open_time:
                return _decide(f"Open windows at {c.window_open_time.strftime('%I:%M %p').lstrip('0')}")
            elif _effective_close_time and now < _effective_close_time:
                return _decide(f"Close windows by {_effective_close_time.strftime('%I:%M %p').lstrip('0')}")
            elif now >= time(ECONOMIZER_EVENING_START_HOUR, 0):
                if windows_physically_open:
                    if outdoor_temp is not None and indoor_temp is not None and not direction_ok:
                        return _decide(
                            _close_windows_msg(outdoor_temp, indoor_temp),
                            warn=True,
                            outdoor=outdoor_temp,
                            indoor=indoor_temp,
                        )
                    return _decide("Windows open — you're all set.")
                if outdoor_temp is not None and indoor_temp is not None:
                    if direction_ok:
                        return _decide(
                            f"Open windows — outdoor's cooler now ({format_temp(outdoor_temp, unit)} vs"
                            f" {format_temp(indoor_temp, unit)}).",
                            outdoor=outdoor_temp,
                            indoor=indoor_temp,
                        )
                    return _decide(
                        f"Keep windows closed for now — outdoor ({format_temp(outdoor_temp, unit)}) isn't"
                        f" cooler than indoor ({format_temp(indoor_temp, unit)}) yet.",
                        warn=True,
                        outdoor=outdoor_temp,
                        indoor=indoor_temp,
                    )
                return _decide("Open windows — outdoor air may be cooler now.")

        if c.day_type == DAY_TYPE_HOT:
            threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
            if c.window_opportunity_morning and now < time(ECONOMIZER_MORNING_END_HOUR, 0):
                end_t = time(ECONOMIZER_MORNING_END_HOUR, 0).strftime("%I:%M %p").lstrip("0")
                return _decide(f"Open windows if outdoor temp is below {format_temp(threshold, unit)} (until {end_t})")
            elif c.window_opportunity_evening and now >= time(ECONOMIZER_EVENING_START_HOUR, 0):
                return _decide(f"Open windows if outdoor temp is below {format_temp(threshold, unit)}")
            if ae is not None and (ae._natural_vent_active or ae._economizer_active):
                return _decide("-")
            return _decide("Keep windows and blinds closed.")
        elif c.day_type == DAY_TYPE_COLD:
            if windows_physically_open:
                return _decide("Close doors to help the heater.")
            return _decide("Keep doors closed.")

        # WARM/MILD/COOL fallback: symmetric cooling-direction and heating-direction
        # checks, both gated on live outdoor-vs-indoor direction (Issue #428).
        cooling_needed = indoor_temp is not None and indoor_temp > comfort_cool
        heating_needed = indoor_temp is not None and indoor_temp < comfort_heat

        if cooling_needed:
            if ae is not None and (ae._natural_vent_active or ae._economizer_active):
                return _decide("-")
            if windows_physically_open:
                if outdoor_temp is not None and not direction_ok:
                    return _decide(
                        _close_windows_msg(outdoor_temp, indoor_temp),
                        warn=True,
                        outdoor=outdoor_temp,
                        indoor=indoor_temp,
                    )
                if outdoor_temp is not None:
                    return _decide(_windows_helping_msg(outdoor_temp, indoor_temp))
                return _decide("Windows open — you're all set.")
            if outdoor_temp is None:
                return _decide(
                    f"Indoor is {format_temp(indoor_temp, unit)} — no outdoor reading available.",
                    indoor=indoor_temp,
                )
            if direction_ok:
                fan_clause = " or turn on the fan" if fan_enabled else ""
                return _decide(
                    f"Open windows{fan_clause} — outdoor's cooler now ({format_temp(outdoor_temp, unit)} vs"
                    f" {format_temp(indoor_temp, unit)}).",
                    outdoor=outdoor_temp,
                    indoor=indoor_temp,
                )
            return _decide(
                f"Outdoor ({format_temp(outdoor_temp, unit)}) isn't cooler than indoor"
                f" ({format_temp(indoor_temp, unit)}) yet — windows/fan won't help.",
                warn=True,
                outdoor=outdoor_temp,
                indoor=indoor_temp,
            )

        if heating_needed:
            if windows_physically_open:
                return _decide("Close windows to enable the heater.")
            if outdoor_temp is not None and outdoor_temp > indoor_temp:
                return _decide(
                    f"Open windows briefly — outdoor's warmer ({format_temp(outdoor_temp, unit)} vs"
                    f" {format_temp(indoor_temp, unit)}).",
                    outdoor=outdoor_temp,
                    indoor=indoor_temp,
                )
            return _decide(
                f"Keep windows and doors closed to hold heat (indoor {format_temp(indoor_temp, unit)}).",
                indoor=indoor_temp,
            )

        return _decide("Comfortable — no action needed.")

    def _emit_event(self, event_type: str, data: dict) -> None:
        """Append a timestamped event to the in-memory event log ring buffer (Issue #76)."""
        _now = dt_util.now()
        entry: dict[str, Any] = {"time": _now.isoformat(), "type": event_type, **data}
        # Normalize alternate temp field names used by automation events
        for _src in ("indoor_temp", "indoor"):
            if _src in entry:
                entry.setdefault("indoor_f", entry[_src])
                break
        for _src in ("outdoor_temp", "outdoor"):
            if _src in entry:
                entry.setdefault("outdoor_f", entry[_src])
                break
        if getattr(self, "config", None):
            entry.setdefault("indoor_f", self._get_indoor_temp())
            entry.setdefault("outdoor_f", getattr(self, "_last_outdoor_temp", None))
        self._event_log.append(entry)
        self._event_log = _prune_event_log(self._event_log, _now)

        # #437 follow-up: detect a genuine nat-vent True->False exit transition (any of
        # the 6 real exit paths — comfort-floor, away-ceiling, predicted-floor,
        # outdoor-rise, reconcile, or all-sensors-closed — this deliberately does NOT
        # enumerate event-type strings, which would silently miss a future exit path)
        # and pull a pending pre-cool trigger earlier if it's still on the stale
        # classification-time schedule.
        _nat_vent_active_now = bool(self.automation_engine._natural_vent_active) if self.automation_engine else False
        if getattr(self, "_nat_vent_was_active", False) and not _nat_vent_active_now:
            self._maybe_reschedule_pre_cool_on_nat_vent_exit()
        self._nat_vent_was_active = _nat_vent_active_now

        self._feed_lifecycle_fsms_from_event(event_type)

    def _feed_lifecycle_fsms_from_event(self, event_type: str) -> None:
        """Re-evaluate the lifecycle FSMs whose tracked state this event just changed
        in production (Issue #647).

        Called from ``_emit_event`` (the real ``AutomationEngine``'s callback). Each
        branch is isolated the same way ``_feed_override_grace_fsm_on_detect()`` is:
        an exception here is logged and swallowed, never allowed to affect production.
        """
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        self._dispatch_fsm_evaluators(
            event_type,
            [
                (
                    _OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP,
                    lambda: self._evaluate_override_grace_fsm(
                        _OGFEventKind(_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP[event_type])
                    ),
                    "Override/grace FSM evaluation (event-driven)",
                ),
            ],
        )

    def _resolve_active_comfort_band(self) -> tuple[float | None, float | None]:
        """Resolve the currently-active (comfort_heat, comfort_cool) band.

        Issue #481: incident detection previously compared indoor temp against the static
        daytime ``comfort_heat``/``comfort_cool`` config values, even during the sleep window
        (or away/vacation setback) — producing false-positive ``comfort_undertemp`` incidents
        when indoor was correctly within the active sleep band but below the (inapplicable)
        daytime floor. Routes through ``select_comfort_band()`` — the same resolver
        ``api.py``'s ``ca_target_heat``/``ca_target_cool`` fields and ``automation.py``'s
        setpoint-writing handlers already use (Issue #402/#462) — instead of a third
        independent inline implementation of this branch.

        Falls back to the same sleep/day-only heuristic ``api.py`` uses when no classification
        is available yet (e.g. right after HA restart, before the first classification cycle
        completes), matching that precedent rather than inventing a new fallback.
        """
        classification = self.current_classification
        if classification is not None:
            band = select_comfort_band(
                classification,
                self.config,
                occupancy_mode=(self.automation_engine._occupancy_mode if self.automation_engine else OCCUPANCY_HOME),
                in_sleep_window=_in_sleep_window(dt_util.now(), self.config),
                aggressive_savings=bool(self.config.get("aggressive_savings", False)),
            )
            return band.floor, band.ceiling
        if _in_sleep_window(dt_util.now(), self.config):
            comfort_heat = self.config.get("sleep_heat", self.config.get("comfort_heat"))
            comfort_cool = self.config.get("sleep_cool", self.config.get("comfort_cool"))
        else:
            comfort_heat = self.config.get("comfort_heat")
            comfort_cool = self.config.get("comfort_cool")
        return comfort_heat, comfort_cool

    def _emit_incident(
        self,
        incident_class: str,
        incident_id: str,
        extra: dict | None = None,
        *,
        comfort_heat: float | None = None,
        comfort_cool: float | None = None,
    ) -> None:
        """Emit an incident_detected event into the event log.

        ``comfort_heat``/``comfort_cool`` default to the currently-active band (Issue #481,
        via ``_resolve_active_comfort_band()``) rather than the static daytime config values,
        so the persisted incident record reflects the band that was actually active — not
        always the flat daytime numbers — for anyone reviewing incident history later. Callers
        that already resolved the active band (e.g. ``_detect_and_emit_incidents()``) may pass
        it explicitly to avoid re-resolving it.
        """
        if comfort_heat is None or comfort_cool is None:
            _resolved_heat, _resolved_cool = self._resolve_active_comfort_band()
            comfort_heat = _resolved_heat if comfort_heat is None else comfort_heat
            comfort_cool = _resolved_cool if comfort_cool is None else comfort_cool
        payload: dict = {
            "incident_class": incident_class,
            "incident_id": incident_id,
            "comfort_cool": comfort_cool,
            "comfort_heat": comfort_heat,
            "occupancy_mode": (self.automation_engine._occupancy_mode if self.automation_engine else None),
        }
        if extra:
            payload.update(extra)
        self._emit_event("incident_detected", payload)

    def _is_nat_vent_tolerated_deviation(self, indoor: float, comfort_heat: float, comfort_cool: float) -> bool:
        """True if this comfort-band deviation is expected nat-vent cycling tolerance, not a violation.

        WHF nat-vent cycling (see automation.py's nat_vent_temperature_check()) is designed to
        let indoor oscillate slightly past the comfort_heat/comfort_cool band edges — the fan
        cycles on/off around a midpoint using the same CONF_NAT_VENT_HYSTERESIS_F hysteresis band.
        A momentary deviation within that hysteresis tolerance, while a nat-vent session is
        actively running, is the system successfully exercising control (CLAUDE.md
        "Goal-Oriented Comfort Model", Issue #74) — not a comfort failure. Only used to gate
        false-positive detection (incident emission, violation-minute accumulation); it does not
        change the underlying >0.5°F trigger threshold or any other decision logic.
        """
        if not (self.automation_engine and self.automation_engine._natural_vent_active):
            return False
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        return (comfort_heat - hysteresis) <= indoor <= (comfort_cool + hysteresis)

    def _detect_and_emit_incidents(self) -> None:
        """Scan recent event_log and state for noteworthy production incidents.

        Emits incident_detected events for patterns that should trigger
        auto-scenario generation in the simulation feedback loop.
        """
        now = dt_util.now()
        recent_events = [
            e for e in self._event_log[-20:] if e.get("time", "") >= (now - timedelta(minutes=35)).isoformat()
        ]
        event_types = [e.get("type") for e in recent_events]

        if "occupancy_change" in event_types or any(
            t in event_types for t in ["occupancy_away", "occupancy_home", "occupancy_vacation"]
        ):
            occ_event = next(
                (
                    e
                    for e in recent_events
                    if e.get("type") in ["occupancy_change", "occupancy_away", "occupancy_home", "occupancy_vacation"]
                ),
                None,
            )
            if occ_event:
                self._emit_incident(
                    "occupancy_transition",
                    occ_event.get("time", now.isoformat()),
                    extra={
                        "occupancy_mode": self.automation_engine._occupancy_mode if self.automation_engine else None,
                        "manual_override_active": (
                            self.automation_engine._manual_override_active if self.automation_engine else None
                        ),
                    },
                )

        override_events = [e for e in recent_events if e.get("type") == "override_detected"]
        automation_event_types = {
            "classification_applied",
            "classification_suppressed_paused",
            "warm_day_setback_applied",
            "warm_day_state_confirmed",
            "ceiling_guard_fired",
            "nat_vent_ceiling_escalation",
            "nat_vent_ac_assist_armed",
        }
        for ov_event in override_events:
            ov_time_str = ov_event.get("time", "")
            if not ov_time_str:
                continue
            try:
                ov_time = datetime.fromisoformat(ov_time_str)
            except (ValueError, TypeError):
                continue
            preceding = [
                e for e in recent_events if e.get("type") in automation_event_types and e.get("time", "") < ov_time_str
            ]
            if preceding:
                last_auto = preceding[-1]
                try:
                    last_auto_time = datetime.fromisoformat(last_auto.get("time", ""))
                    gap_seconds = (ov_time - last_auto_time).total_seconds()
                    if gap_seconds < 60:
                        self._emit_incident(
                            "rapid_override_after_automation",
                            ov_time_str,
                            extra={
                                "automation_event_type": last_auto.get("type"),
                                "gap_seconds": round(gap_seconds),
                            },
                        )
                except (ValueError, TypeError):
                    pass

        current_data = self.data or {}
        indoor = current_data.get("indoor_temp")
        # Issue #481: resolve the currently-active band (sleep/away/vacation-aware) once,
        # instead of reading the static daytime comfort_heat/comfort_cool config directly —
        # used for both the violation-detection comparison below and the incident payload
        # stamped by _emit_incident() so a reviewer sees the band that was actually active.
        comfort_heat, comfort_cool = self._resolve_active_comfort_band()
        if (
            indoor
            and comfort_cool
            and indoor > comfort_cool + 0.5
            and not self._is_nat_vent_tolerated_deviation(indoor, comfort_heat or 0.0, comfort_cool)
        ):
            recent_violations = [
                e
                for e in self._event_log[-50:]
                if e.get("type") == "incident_detected"
                and e.get("incident_class") == "comfort_violation"
                and e.get("time", "") >= (now - timedelta(minutes=30)).isoformat()
            ]
            if not recent_violations:
                self._emit_incident(
                    "comfort_violation",
                    now.isoformat(),
                    extra={
                        "indoor_f": indoor,
                        "outdoor_f": current_data.get("outdoor_temp"),
                        "hvac_mode": current_data.get("hvac_mode"),
                        "nat_vent_active": (
                            self.automation_engine._natural_vent_active if self.automation_engine else None
                        ),
                    },
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                )
        elif (
            indoor
            and comfort_heat
            and indoor < comfort_heat - 0.5
            and not self._is_nat_vent_tolerated_deviation(indoor, comfort_heat, comfort_cool or 999.0)
        ):
            recent_violations = [
                e
                for e in self._event_log[-50:]
                if e.get("type") == "incident_detected"
                and e.get("incident_class") == "comfort_undertemp"
                and e.get("time", "") >= (now - timedelta(minutes=30)).isoformat()
            ]
            if not recent_violations:
                self._emit_incident(
                    "comfort_undertemp",
                    now.isoformat(),
                    extra={
                        "indoor_f": indoor,
                        "outdoor_f": current_data.get("outdoor_temp"),
                        "hvac_mode": current_data.get("hvac_mode"),
                        "nat_vent_active": (
                            self.automation_engine._natural_vent_active if self.automation_engine else None
                        ),
                    },
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                )

    def _compute_automation_status(self) -> str:
        """Compute the current automation status string."""
        if not self._automation_enabled:
            return "disabled"
        # Bug 1 (Issue #321): Surface startup coalescing window in status
        if self._startup_coalesce_active:
            # Issue #396: the coalesce check only runs once weather data is available
            # (it lives inside `if forecast:` in _async_update_data()) — if the 5-min
            # timer has already fired but classification still isn't set, the real
            # blocker is the weather entity, not coalescing itself. Without this
            # distinction the status card says "waiting for coalescing" indefinitely
            # with no clue that the actual cause is a weather integration that hasn't
            # come back after restart.
            if self._startup_timer_fired and self._current_classification is None:
                return "starting — waiting for weather data"
            return "starting — initializing"
        # Check if windows are open during a planned window period (not a pause)
        if self.automation_engine._is_within_planned_window_period() and self._any_sensor_open():
            return "windows open (as planned)"
        if self.automation_engine.natural_vent_active:
            # Issue #415: no numeric target here. This string is cached for up to
            # update_interval (30 min), but api.py recomputes compute_nat_vent_cycling_band()
            # live on every dashboard poll for the cycling-band line — so a number embedded
            # here can silently drift from the live band across a sleep-window boundary
            # (e.g. cached "71°F" vs. live "64°F–66°F"). The live band is the only place
            # this temperature is shown now; don't reintroduce it here.
            # Issue #540: soft-start is a distinct sub-mode (purge/comfort at parity, not
            # bulk free-cooling) — surfaced here per the Status Card Ontology, since this
            # is the one card where mechanism state belongs.
            if self.automation_engine._nat_vent_soft_start:
                return "nat-vent — soft-start (purge)"
            return "nat-vent"
        if self.automation_engine.is_paused_by_door:
            if self._occupancy_mode == OCCUPANCY_AWAY:
                return "paused — away (setback deferred: windows open)"
            if self._occupancy_mode == OCCUPANCY_VACATION:
                return "paused — vacation (setback deferred: windows open)"
            return "paused — door/window open"
        if self.automation_engine._override_confirm_pending:
            return "override pending (confirming...)"
        # Bug 2 (Issue #321): detect stuck grace before the normal grace-active path
        _ae2 = self.automation_engine
        if _ae2._manual_override_active and not _ae2._grace_active and _ae2._grace_end_time is not None:
            _se = dt_util.parse_datetime(_ae2._grace_end_time)
            if _se is not None and dt_util.now() > _se:
                return "override (grace stuck — check logs)"
        if self.automation_engine._grace_active:
            if self.automation_engine._resumed_from_pause:
                return "resumed — door/window override"
            source = self.automation_engine._last_resume_source or "automation"
            # Issue #625: previously appended the full _last_action_reason sentence here
            # (Issue #620) — for fan-triggered grace periods that duplicated what the Fan
            # (WHF) card already says, and for manual thermostat overrides
            # (_confirm_override() never calls _record_action()) it was blank or a stale
            # leftover from an unrelated earlier action. Use the short _GRACE_TRIGGER_LABELS
            # lookup instead — same compact "cause — duration (ends HH:MM)" shape the Fan
            # (WHF) card already uses. Full detail remains on the Debug tab
            # (_last_action_reason), unaffected by this change.
            _trigger_label = _GRACE_TRIGGER_LABELS.get(self.automation_engine._last_grace_trigger or "", "")
            _cause_suffix = f" — {_trigger_label}" if _trigger_label else ""
            return f"grace period ({source}){_cause_suffix}{self._format_grace_remaining(self.automation_engine)}"
        # Issue #786: TOU scheduler pre-conditioning — a mechanism reason, per the Status
        # Card Ontology this is the one card it belongs on. Same compact "short label —
        # duration (ends HH:MM)" shape _format_grace_remaining() already established.
        _tou = getattr(self, "_tou_phase_resolution", None)
        if _tou is not None and _tou.phase == TOUPhase.PRECONDITIONING:
            _label = "pre-cooling" if _tou.mode == "cool" else "pre-heating"
            _ends = self._format_tou_ends(_tou.schedule_start)
            return f"{_label} — TOU high-cost period{_ends}"
        # Investigation D / Phase 3d: the active cost_period window itself (not just the
        # PRECONDITIONING lead-time before it) — previously invisible on the Status card
        # entirely, including the case David actually hit (a configured schedule covering
        # `now`, but `hvac_mode="off"` that day so resolve_tou_phase() correctly never
        # entered PRECONDITIONING — nothing anywhere told the occupant this was evaluated
        # and found inapplicable, vs. silently broken). Reads the already-resolved,
        # previously write-only self._tou_active_cost_resolution (from
        # resolve_active_schedules(), cached each cycle by _resolve_tou_schedule_state())
        # — no new resolution logic, just a new consumer of data that already exists.
        _tou_active = getattr(self, "_tou_active_cost_resolution", None)
        if _tou_active is not None and _tou_active.cost_tag == COST_TAG_HIGH:
            _ends = self._format_tou_ends(_tou_active.schedule_end)
            _mode = getattr(self._current_classification, "hvac_mode", None) if self._current_classification else None
            if _mode in ("heat", "cool"):
                return f"TOU high-cost period active{_ends}"
            return f"TOU high-cost period active{_ends} — no pre-conditioning needed today"
        if self._occupancy_mode == OCCUPANCY_VACATION:
            return "active (vacation)"
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return "active (away)"
        if self._occupancy_mode == OCCUPANCY_GUEST:
            return "active (guest)"
        return "active"

    @staticmethod
    def _pred_archive_key(dt: datetime) -> int:
        """Return Unix timestamp floored to nearest 30-min boundary (UTC-safe)."""
        ts = int(dt.timestamp())
        return ts - (ts % 1800)

    def _lookup_pred_archive(self, now_dt: datetime) -> float | None:
        """Return first-written ODE prediction for this 30-min slot (None on cache miss)."""
        return self._pred_archive.get(self._pred_archive_key(now_dt))

    def _read_chart_hvac_action(self) -> str:
        """Return the thermostat's current hvac_action string for chart logging.

        Applies the #109 fan→heating/cooling remap: only remaps when fan_mode is
        auto (fan is part of the HVAC cycle). When fan_mode=on, the fan is
        circulating independently — hvac_action="fan" does NOT imply active
        heating or cooling.

        Returns "" if the climate entity is unavailable.
        """
        climate_id = self.config.get("climate_entity", "")
        cs = self.hass.states.get(climate_id) if climate_id else None
        if cs is None:
            _LOGGER.debug("chart_hvac_action: climate entity unavailable, logging ''")
            return ""
        hvac_action = str(cs.attributes.get("hvac_action", "")).lower()
        hvac_mode = cs.state.lower()
        fan_mode = str(cs.attributes.get("fan_mode", "")).lower()
        fan_is_auto = not fan_mode or fan_mode.startswith("auto")
        if hvac_action == "fan" and fan_is_auto:
            if hvac_mode == "heat":
                _LOGGER.debug("chart_hvac_action: remapping fan→heating (fan_mode=%s)", fan_mode or "empty")
                return "heating"
            if hvac_mode in ("cool", "heat_cool"):
                _LOGGER.debug("chart_hvac_action: remapping fan→cooling (fan_mode=%s)", fan_mode or "empty")
                return "cooling"
        return hvac_action

    def _read_chart_setpoint(self) -> float | None:
        """Return the live thermostat's ``target_temperature`` in °F, for chart_log's
        ``setpoint`` field (Phase 3a).

        Mirrors the read the 30-min poll ``chart_log.append()`` site has always done
        (only heat/cool modes carry a real commanded setpoint) — extracted here so the
        3 event-driven ``chart_log.append()`` call sites (``classification_change``/
        ``override``/``hvac_action_change``) can share it instead of writing ``None`` for
        ``setpoint`` (the gap Investigation B found: those 3 sites never populated it,
        leaving the historical "effective target" series with holes at exactly the moments
        an event fired between 30-min polls).
        """
        unit = self.config.get("temp_unit", "fahrenheit")
        climate_id = self.config.get("climate_entity", "")
        cs = self.hass.states.get(climate_id) if climate_id else None
        if cs is None or cs.state not in ("heat", "cool"):
            return None
        raw_sp = cs.attributes.get("target_temperature")
        if raw_sp is None:
            return None
        return to_fahrenheit(float(raw_sp), unit)

    def _fan_is_running(self, _status: str | None = None) -> bool:
        """Return True if the fan is running for any reason.

        Covers CA-activated, manual override, and untracked states so that
        chart_log entries correctly reflect fan activity even when CA's own
        _fan_active flag is False (e.g. post-heat blowdown still in progress).

        Issue #510 0.4: accepts an optional pre-computed status string so a caller that
        already invoked ``_compute_fan_status()`` this cycle (e.g. ``_async_update_data_impl``,
        which calls it repeatedly and was producing duplicate WARNING log lines) can pass it
        through instead of triggering another full recomputation. Defaults to None (computes
        fresh, exactly as before) — every other caller is unaffected.
        """
        status = _status if _status is not None else self._compute_fan_status()
        return status not in {"inactive", "disabled"}

    def _fan_physically_running(self, _status: str | None = None) -> bool:
        """Return True iff the fan is physically spinning right now.

        Differs from _fan_is_running() by excluding the
        'nat-vent (session active, fan idle)' state — nat-vent armed but
        between cycles means the session is active but the blower is not on.

        Used for the chart_log ``fan_running`` field so the frontend can
        distinguish a spinning fan from a merely armed nat-vent session.

        Issue #510 0.4: see _fan_is_running()'s docstring — same optional-cache pattern.
        """
        status = _status if _status is not None else self._compute_fan_status()
        return status in {
            "active",
            "running (manual override)",
            "running (untracked)",
        }

    def _compute_fan_status(self) -> str:
        """Compute the current fan status string.

        Priority order:
        1. CA-activated fan (_fan_active=True) → "active" (or "active (unconfirmed)" briefly,
           see Issue #510 0.1c below)
        2. Manual override → "running (manual override)" / "off (manual override)"
        3. Ground-truth fallback: physical WHF entity, then thermostat fan_mode/hvac_action —
           catches post-restart state, user/Ecobee-initiated fan runs that CA didn't command.
        4. "inactive"

        Issue #510: ground truth (the physical WHF entity, when configured with
        fan_state_feedback) now wins over CA's own internal session flags wherever it's
        available, rather than being consulted only as a last resort — see 0.1b (nat-vent
        branch) and 0.1c (active-unconfirmed settling) below.

        Issue #571: the ground-truth fallbacks (WHF and HVAC/BOTH) route through
        ``resolve_untracked_fan_status()`` — the OFF-direction mirror of 0.1c's ON-direction
        guard. When CA has just cleared its own ownership flags and commanded the fan off,
        the physical/thermostat signal may not have caught up yet; without this guard that
        brief propagation window was misread as an externally-owned fan and force-corrected
        by the ``backstop_30min`` reconcile (see that block's comment below).
        """
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return "disabled"
        # Issue #510 0.1b: read ground truth at most once, reused by every branch below that
        # needs it (including the nat-vent session-flag branch, which previously never
        # consulted it at all) — but still lazily, exactly like the pre-#510 code: the
        # override_active+fan_active=True fast path below must NOT trigger a physical-state
        # read at all (an existing, deliberate property this codebase already tests for).
        _physical_on_cache: list[bool | None] = []

        def _physical_on() -> bool | None:
            if not _physical_on_cache:
                _physical_on_cache.append(
                    self._get_fan_physical_state() if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) else None
                )
            return _physical_on_cache[0]

        if ae._fan_override_active:
            if ae._fan_active:
                return "running (manual override)"
            # _fan_active=False: check physical state to distinguish
            # "user is running it" from "user turned it on then off"
            if _physical_on() is True:
                return "running (manual override)"
            return "off (manual override)"
        if ae._fan_active:
            if _physical_on() is False:
                # Issue #510 0.1c: a brand-new CA command hasn't had time to propagate to the
                # physical entity yet — that's expected and NOT the bug; only treat this as a
                # stale flag once enough time has passed that ground truth should be trusted.
                if self._is_recent_fan_command(threshold_seconds=30.0):
                    return "active (unconfirmed)"
                _LOGGER.warning("WHF _fan_active=True but physical state=off — possible stale flag after manual stop")
                return "inactive"
            return "active"
        # Issue #510 0.1b: nat-vent session flag can go stale (still "active" between cycles,
        # or after an external change CA hasn't reconciled) while the fan is genuinely running
        # for an unrelated reason — trust confirmed ground truth over the session flag.
        if ae._natural_vent_active:
            if _physical_on() is True:
                _LOGGER.info(
                    "WHF nat-vent session flag stale but physical state confirms running — "
                    "displaying running (untracked) instead of trusting the session flag"
                )
                return "running (untracked)"
            return "nat-vent (session active, fan idle)"
        # WHF ground-truth fallback: reads fan_state_entity (Type 2) or fan_entity (Type 1).
        # Catches post-restart and externally-run WHF when CA's internal flags are all clear.
        if _physical_on() is True:
            return resolve_untracked_fan_status(recent_fan_command=self._is_recent_fan_command(threshold_seconds=30.0))
        # Ground-truth fallback: CA's flag says inactive, but check what the
        # thermostat is actually doing. Catches post-restart and externally-run fan.
        if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
            climate_entity_id = self.config.get("climate_entity", "")
            cs = self.hass.states.get(climate_entity_id) if climate_entity_id else None
            if cs is not None:
                thermostat_fan_mode = cs.attributes.get("fan_mode", "")
                thermostat_hvac_action = str(cs.attributes.get("hvac_action", "")).lower()
                if thermostat_fan_mode == "on" or thermostat_hvac_action == "fan":
                    return resolve_untracked_fan_status(
                        recent_fan_command=self._is_recent_fan_command(threshold_seconds=30.0)
                    )
        return "inactive"

    def _compute_whf_status(self) -> str | None:
        """Return WHF-specific status, or None when WHF is not configured.

        Issue #510: same ground-truth-first priority as _compute_fan_status() — see that
        method's docstring and 0.1b/0.1c below for the rationale.
        """
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return None
        physical_on = self._get_fan_physical_state()
        if ae._fan_override_active:
            status = "running (manual override)" if ae._fan_active or physical_on is True else "off (manual override)"
        elif ae._fan_active:
            if physical_on is False:
                if self._is_recent_fan_command(threshold_seconds=30.0):
                    status = "active (unconfirmed)"
                else:
                    _LOGGER.warning(
                        "WHF _fan_active=True but physical state=off — possible stale flag after manual stop"
                    )
                    status = "inactive"
            else:
                status = "active"
        elif ae._natural_vent_active:
            if physical_on is True:
                _LOGGER.info(
                    "WHF nat-vent session flag stale but physical state confirms running — "
                    "displaying running (untracked) instead of trusting the session flag"
                )
                status = "running (untracked)"
            else:
                status = "nat-vent (session active, fan idle)"
        elif physical_on is True:
            status = resolve_untracked_fan_status(
                recent_fan_command=self._is_recent_fan_command(threshold_seconds=30.0)
            )
        else:
            status = "inactive"
        return status + self._whf_rate_limit_suffix(ae)

    def _whf_rate_limit_suffix(self, ae: AutomationEngine) -> str:
        """Append '(<direction> pending — 5-min floor, applies at HH:MM:SS)' to the WHF
        status while a toggle is still deferred by the Issue #641 cooldown — Status Card
        Ontology rule: extend the existing card's value string, don't add a new one.
        Returns "" once the cooldown has elapsed; never raises on a mocked/partial engine
        (isinstance guard, matching _format_grace_remaining()'s existing defensive pattern
        for the same reason).

        Issue #649: reworded from the original "(rate-limited Xs ago)", which didn't say
        what was pending or when it would resolve — this names the direction (using the
        same "activate"/"deactivate" strings _fan_toggle_rate_limited() already tracks)
        and the exact clock time the deferred toggle will apply.
        """
        until = getattr(ae, "_fan_rate_limited_until", None)
        if not isinstance(until, datetime):
            return ""
        remaining = (until - dt_util.now()).total_seconds()
        if remaining <= 0:
            return ""
        direction = getattr(ae, "_fan_rate_limited_direction", None)
        pending = "on" if direction == "activate" else "off"
        return f" ({pending} pending — 5-min floor, applies at {until.strftime('%H:%M:%S')})"

    def _compute_hvac_fan_status(self) -> str | None:
        """Return HVAC-fan-blower-specific status, or None when HVAC fan is not configured.

        Issue #571: mirrors _compute_whf_status()'s ground-truth-first shape — including
        the ON-direction "active (unconfirmed)"/stale-flag guard, which this function never
        had until now (unlike its two siblings, which gained it under Issue #510). Reads the
        thermostat fan_mode/hvac_action ground truth via a memoized closure so it's computed
        at most once per call, shared between the ON-direction guard and the OFF-direction
        fallback below.
        """
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_HVAC, FAN_MODE_BOTH):
            return None

        _thermostat_on_cache: list[bool | None] = []

        def _thermostat_fan_on() -> bool | None:
            if not _thermostat_on_cache:
                climate_entity_id = self.config.get("climate_entity", "")
                cs = self.hass.states.get(climate_entity_id) if climate_entity_id else None
                if cs is None:
                    _thermostat_on_cache.append(None)
                else:
                    thermostat_fan_mode = cs.attributes.get("fan_mode", "")
                    thermostat_hvac_action = str(cs.attributes.get("hvac_action", "")).lower()
                    _thermostat_on_cache.append(thermostat_fan_mode == "on" or thermostat_hvac_action == "fan")
            return _thermostat_on_cache[0]

        if ae._fan_override_active:
            if ae._fan_active:
                return "running (manual override)"
            return "off (manual override)"
        if ae._fan_active:
            if _thermostat_fan_on() is False:
                if self._is_recent_fan_command(threshold_seconds=30.0):
                    return "active (unconfirmed)"
                _LOGGER.warning(
                    "HVAC fan _fan_active=True but thermostat reports fan off — possible stale flag after manual stop"
                )
                return "inactive"
            return "active"
        if ae._natural_vent_active:
            return "nat-vent (session active, fan idle)"
        if _thermostat_fan_on() is True:
            return resolve_untracked_fan_status(recent_fan_command=self._is_recent_fan_command(threshold_seconds=30.0))
        return "inactive"

    def _compute_contact_status(self) -> str:
        """Compute the contact sensor summary string."""
        if not self._resolved_sensors:
            return "no sensors"
        open_count = sum(1 for s in self._resolved_sensors if self._is_sensor_open(s))
        if open_count == 0:
            return "all closed"
        return f"{open_count} open"

    def _compute_contact_details(self) -> list[dict[str, Any]]:
        """Return per-sensor details for contact status attributes."""
        details = []
        for sensor_id in self._resolved_sensors:
            friendly = sensor_id.split(".")[-1].replace("_", " ").title()
            details.append(
                {
                    "entity_id": sensor_id,
                    "friendly_name": friendly,
                    "open": self._is_sensor_open(sensor_id),
                }
            )
        return details

    def _format_tou_ends(self, schedule_start: datetime | None) -> str:
        """Format the TOU pre-conditioning window's end (the schedule's own start instant)
        as " (ends H:MM AM)", same convention as _format_grace_remaining(). Returns "" if
        unavailable — never raises."""
        if not isinstance(schedule_start, datetime):
            return ""
        end_str = dt_util.as_local(schedule_start).strftime("%I:%M %p").lstrip("0")
        return f" (ends {end_str})"

    def _format_grace_remaining(self, ae: AutomationEngine) -> str:
        """Format grace duration + end time as ' — 30 min (ends H:MM AM)', matching the
        Fan (WHF) card's 'remote timer: Xh (ends HH:MM)' style (Issue #625).

        Returns "" if the timestamp/duration is missing, unparseable, or already in the
        past — never raises (Issue #498: dashboard showed grace was active but never said
        when it would end, the single most useful piece of information during an override).
        """
        end_iso = getattr(ae, "_grace_end_time", None)
        duration_s = getattr(ae, "_grace_duration_seconds", None)
        # isinstance guards: tests frequently stub the automation engine as a bare
        # MagicMock() without setting these attributes — an unset MagicMock attribute is
        # truthy, so `if not end_iso` alone wouldn't catch it. Treat anything that isn't a
        # real string/number as "no data", matching the existing defensive pattern used for
        # mocked dt_util elsewhere in this codebase (never raise on test doubles).
        if not isinstance(end_iso, str) or not end_iso:
            return ""
        if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool):
            return ""
        end_dt = dt_util.parse_datetime(end_iso)
        if not isinstance(end_dt, datetime):
            return ""
        now = dt_util.now()
        if not isinstance(now, datetime):
            return ""
        remaining_s = (end_dt - now).total_seconds()
        if remaining_s <= 0:
            return ""
        end_str = dt_util.as_local(end_dt).strftime("%I:%M %p").lstrip("0")
        minutes = max(1, round(duration_s / 60))
        if minutes >= 60 and minutes % 60 == 0:
            dur_str = f"{minutes // 60}h"
        elif minutes >= 60:
            dur_str = f"{minutes / 60:.1f}h"
        else:
            dur_str = f"{minutes} min"
        return f" — {dur_str} (ends {end_str})"

    def _compute_next_automation_action(self, c: DayClassification | None) -> tuple[str, str]:
        """Compute the next scheduled automation action and its time.

        Returns:
            Tuple of (action_description, execution_time_str).
        """
        # Bug 1 (Issue #321): Surface coalescing as the next imminent action
        if self._startup_coalesce_active and self._startup_coalesce_expiry:
            # Format as a local-time label like the other branches below; the field's
            # contract is a display-ready label, never a raw ISO timestamp (Issue #324).
            coalesce_dt = dt_util.parse_datetime(self._startup_coalesce_expiry)
            time_str = dt_util.as_local(coalesce_dt).strftime("%I:%M %p").lstrip("0") if coalesce_dt else ""
            return ("Startup coalescing", time_str)

        if not c:
            return ("Waiting for classification...", "")

        now = dt_util.now()
        today = now.date()

        # Issue #527: this function used to short-circuit here whenever automation was
        # paused by an open door/window, in a debounce window, or in a grace period,
        # returning mechanism text ("Waiting — HVAC paused...", "Grace period active...")
        # instead of the real next plan step. That duplicated what the Status card
        # already says (_compute_automation_status()) and hid the actual answer to "what
        # will the automation do next" — which is unaffected by those mechanism states;
        # it's simply deferred until they clear. Always fall through to the real
        # schedule-candidate list below. See docs/08-COMPUTATION-REFERENCE.md §9d and
        # CLAUDE.md's card-ontology table.
        ae = self.automation_engine
        _LOGGER.info(
            "Next-automation evaluation: day_type=%s hvac_mode=%s paused_by_door=%s grace_active=%s"
            " debounce_pending=%s startup_coalesce_active=%s",
            c.day_type,
            c.hvac_mode,
            ae.is_paused_by_door,
            ae._grace_active,
            bool(self._door_open_timers),
            self._startup_coalesce_active,
        )

        # Build list of upcoming scheduled events as (datetime, description).
        # Using full datetimes (not time objects) so cross-midnight events like
        # pre-cool (e.g. 2:30 AM tomorrow) compare correctly against now.
        wake_time = self.config.get("wake_time", "06:30:00")
        sleep_time = self.config.get("sleep_time", "22:30:00")
        briefing_time = self.config.get("briefing_time", "06:00:00")

        def _parse_time(t: str) -> time:
            parts = t.split(":")
            return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

        def _to_dt(t: time) -> datetime:
            return datetime.combine(today, t, tzinfo=now.tzinfo)

        candidates: list[tuple[datetime, str]] = []

        bt_dt = _to_dt(_parse_time(briefing_time))
        if bt_dt > now:
            candidates.append((bt_dt, "Send daily briefing"))

        wt_dt = _to_dt(_parse_time(wake_time))
        if wt_dt > now:
            if c.hvac_mode in ("heat", "cool"):
                candidates.append((wt_dt, f"Morning wake-up — restore {c.hvac_mode} comfort"))
            else:
                candidates.append((wt_dt, "Morning wake-up check"))

        st_dt = _to_dt(_parse_time(sleep_time))
        if st_dt > now:
            unit = self.config.get("temp_unit", "fahrenheit")
            if c.hvac_mode in ("heat", "cool"):
                # Use the raw configured sleep temp — matches what handle_bedtime() actually
                # sends to the thermostat via select_comfort_band(in_sleep_window=True).
                # The warming-trend modifier surfaces separately as the Pre-cool candidate below.
                from .const import CONF_SLEEP_COOL, CONF_SLEEP_HEAT, DEFAULT_SLEEP_COOL, DEFAULT_SLEEP_HEAT

                bedtime_target = float(
                    self.config.get(CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT)
                    if c.hvac_mode == "heat"
                    else self.config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL)
                )
                mode_label = "heat" if c.hvac_mode == "heat" else "cool"
                candidates.append((st_dt, f"Bedtime — {mode_label} setback to {format_temp(bedtime_target, unit)}"))
            else:
                candidates.append((st_dt, "Bedtime check"))

        # Pre-cool is scheduled for early tomorrow morning — its trigger_dt crosses
        # midnight, so only full-datetime comparison handles it correctly.
        if self._pre_cool_trigger_dt and self._pre_cool_trigger_dt > now and self._pre_cool_target is not None:
            unit = self.config.get("temp_unit", "fahrenheit")
            pc_desc = f"Pre-cool ceiling ({format_temp(self._pre_cool_target, unit)})"
            candidates.append((self._pre_cool_trigger_dt, pc_desc))

        # Issue #528: self._last_predicted_indoor is always set in __init__() in
        # production, but several test files build a coordinator via object.__new__()
        # without running __init__() — getattr() keeps those minimal stubs working
        # rather than requiring every one of them to know about this new attribute.
        # Only needed by the nat-vent-start-prediction block below now (Issue #817
        # moved the WARM/MILD-day events block off this and onto self._nat_vent_plan).
        _predicted_indoor = getattr(self, "_last_predicted_indoor", None)

        # Issue #535: shared comfort-floor inputs, hoisted above the nat-vent
        # start-prediction block below so it doesn't independently re-read the same
        # config keys. "sleep_heat" literal, not the CONF_SLEEP_HEAT constant: the
        # bedtime block above does a local `from .const import ... CONF_SLEEP_HEAT`
        # inside an `if c.hvac_mode in ("heat", "cool")` branch, which makes
        # CONF_SLEEP_HEAT a local name for this ENTIRE function regardless of whether
        # that branch actually runs — referencing the module-level import here raises
        # UnboundLocalError whenever hvac_mode is "off"/"auto". Same value either way.
        _comfort_heat_raw = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        _sleep_heat = float(self.config.get("sleep_heat", _comfort_heat_raw))

        # WARM/MILD-day forecast-derived events (Issue #528) — the same nat_vent_plan
        # already computed for the briefing. Issue #817: this used to independently
        # re-derive its own copy via a second compute_nat_vent_plan() call with its own
        # locally-rebuilt inputs — now reads the coordinator's single per-cycle
        # self._nat_vent_plan (_compute_and_cache_nat_vent_plan()) instead, so this card
        # can never disagree with the briefing or TLDR table about the same numbers.
        # Issue #849: nat_vent_cutoff and recovery_time candidates were removed — both
        # told the occupant to close/reopen windows, an action CA cannot execute (no
        # window actuator). ceiling_breach_time is the only automation-executed action
        # in this plan (the AC actually turns on) and is kept.
        _warm_events = getattr(self, "_nat_vent_plan", None)
        if (
            c.windows_recommended
            and _warm_events
            and _warm_events["ceiling_breach_time"]
            and _warm_events["ceiling_breach_time"] > now
        ):
            candidates.append((_warm_events["ceiling_breach_time"], "AC turns on to hold the ceiling"))

        # Nat-vent/WHF start prediction (Issue #528). Uses the real activation gate
        # (decide_nat_vent_gate(), the same pure function automation.py's
        # check_natural_vent_conditions() calls) — not compute_nat_vent_cycling_band(),
        # which describes the fan's cycling band once ALREADY active, a different
        # threshold entirely (see docs/08-COMPUTATION-REFERENCE.md's #528 note).
        # Gated on a door/window already being open (or grace), mirroring
        # check_natural_vent_conditions()'s own precondition — nat-vent cannot start
        # with everything closed, so this never promises a time that depends on the
        # occupant opening a window first.
        if (
            self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) != FAN_MODE_DISABLED
            and not ae._natural_vent_active
            and (ae.is_paused_by_door or self._any_sensor_open())
            and _predicted_indoor
        ):
            # _comfort_heat_raw / _sleep_heat computed once above, shared with the
            # WARM/MILD-day events block.
            _comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
            _nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
            _hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            _fan_mode_val = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _aggressive_savings = bool(self.config.get("aggressive_savings", False))

            def _nat_vent_gate_comparator(ts: datetime, outdoor: float, indoor: float) -> bool:
                return decide_nat_vent_gate(
                    NatVentGateInputs(
                        outdoor=outdoor,
                        indoor=indoor,
                        comfort_heat_raw=_comfort_heat_raw,
                        sleep_heat=_sleep_heat,
                        in_sleep_window=_in_sleep_window(ts, self.config),
                        comfort_cool=_comfort_cool,
                        nat_vent_delta=_nat_vent_delta,
                        hysteresis=_hysteresis,
                        fan_mode=_fan_mode_val,
                        aggressive_savings=_aggressive_savings,
                    )
                )

            _nat_vent_outdoor_curve = _build_future_forecast_outdoor(self._hourly_forecast_temps, c)
            _nat_vent_dt = find_temperature_crossing(
                _predicted_indoor, _nat_vent_outdoor_curve, _nat_vent_gate_comparator, after=now
            )
            if _nat_vent_dt:
                candidates.append((_nat_vent_dt, "Natural ventilation"))

        # Phase 3e (Issue #786 follow-up): TOU pre-conditioning start. Per CLAUDE.md's
        # Status Card Ontology, "Next Automation" answers "what will the automation do
        # next" — TOU pre-conditioning is exactly that shape and was previously entirely
        # absent here (confirmed zero references). Guarded the same way the existing
        # pre-cool-ceiling candidate is (precondition_start is not None and
        # precondition_start > now) — this alone correctly excludes the case where
        # pre-conditioning has already started (once phase == PRECONDITIONING,
        # precondition_start <= now by the resolver's own definition), so no separate
        # phase check is needed. Pure consumer of Phase 2's already-cached
        # self._tou_phase_resolution — zero new resolution logic, same DRY shape as the
        # Status-card wiring above.
        _tou_next = getattr(self, "_tou_phase_resolution", None)
        if (
            _tou_next is not None
            and _tou_next.precondition_start is not None
            and _tou_next.precondition_start > now
            and _tou_next.target is not None
        ):
            _tou_unit = self.config.get("temp_unit", "fahrenheit")
            _tou_action = "Pre-cool" if _tou_next.mode == "cool" else "Pre-heat"
            candidates.append(
                (
                    _tou_next.precondition_start,
                    f"{_tou_action} for TOU schedule ({format_temp(_tou_next.target, _tou_unit)})",
                )
            )

        if not candidates:
            _LOGGER.info("Next-automation: No more actions today")
            return ("No more actions today", "")

        candidates.sort(key=lambda e: e[0])
        next_dt, next_desc = candidates[0]
        time_str = dt_util.as_local(next_dt).strftime("%I:%M %p").lstrip("0")
        _LOGGER.info("Next-automation: %s at %s", next_desc, time_str)
        return (next_desc, time_str)

    @property
    def current_classification(self) -> DayClassification | None:
        """Return the current day classification."""
        return self._current_classification

    @property
    def today_record(self) -> DailyRecord | None:
        """Return today's learning record."""
        return self._today_record

    def get_hvac_runtime_today(self) -> float:
        """Return today's HVAC runtime in minutes, computed live (Issue #464).

        `coordinator.data[ATTR_HVAC_RUNTIME_TODAY]` is only refreshed once per
        update cycle (up to ~30 min stale) — this method is the single source of
        truth for consumers that need the current value right now (AI context
        builders previously hand-copied this exact formula for that reason).
        Adds the accumulated base runtime from today's record to the elapsed
        time of any HVAC session currently in progress.
        """
        base_runtime = self._today_record.hvac_runtime_minutes if self._today_record is not None else 0.0
        session_elapsed = (
            (dt_util.now() - self._hvac_on_since).total_seconds() / 60.0 if self._hvac_on_since is not None else 0.0
        )
        return round(base_runtime + session_elapsed, 1)

    @property
    def yesterday_record(self) -> dict | None:
        """Return yesterday's learning record, if available."""
        yesterday_str = (dt_util.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return self.learning.get_record_by_date(yesterday_str)

    @property
    def tomorrow_plan(self) -> dict | None:
        """Return a projected plan for tomorrow based on current classification."""
        c = self._current_classification
        if not c:
            return None

        tomorrow_str = (dt_util.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # Classify tomorrow by swapping tomorrow's temps into "today" position.
        # Trend will show as "stable" since we lack the day-after-tomorrow forecast.
        tomorrow_forecast = ForecastSnapshot(
            today_high=c.tomorrow_high,
            today_low=c.tomorrow_low,
            tomorrow_high=c.tomorrow_high,
            tomorrow_low=c.tomorrow_low,
            current_outdoor_temp=c.today_low,
        )
        tomorrow_class = classify_day(
            tomorrow_forecast,
            threshold_hot=self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
            threshold_warm=self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
            threshold_mild=self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
            threshold_cool=self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
        )

        return {
            "date": tomorrow_str,
            "day_type": tomorrow_class.day_type,
            "trend_direction": tomorrow_class.trend_direction,
            "hvac_mode": tomorrow_class.hvac_mode,
            "windows_recommended": tomorrow_class.windows_recommended,
            "window_open_time": (
                tomorrow_class.window_open_time.isoformat() if tomorrow_class.window_open_time else None
            ),
            "window_close_time": (
                tomorrow_class.window_close_time.isoformat() if tomorrow_class.window_close_time else None
            ),
            "pre_condition": tomorrow_class.pre_condition,
            "expected_high": c.tomorrow_high,
            "expected_low": c.tomorrow_low,
        }

    def get_chart_data(self, range_str: str = "24h", before_ts: float | None = None) -> dict[str, Any]:
        """Build chart data for the dashboard panel.

        Returns a dict with four series: predicted outdoor, predicted indoor,
        actual outdoor, and actual indoor temperatures over a 24-hour period,
        plus a rolling state log filtered/downsampled to the requested range.

        range_str: one of "6h", "12h", "24h", "3d", "7d", "30d", "1y"
        before_ts: optional Unix epoch *seconds* upper-bound anchor.  When
            provided the chart log is queried for [anchor - range, anchor)
            instead of [now - range, now).  Forecast and prediction series are
            suppressed for historical views (more than 1 h before now).
        """
        now = dt_util.now()
        current_hour = now.hour + now.minute / 60.0

        # Resolve the optional historical anchor.
        # Use UTC for the anchor datetime — chart log entries are stored in UTC
        # (or UTC-offset ISO strings), so UTC comparison is always correct.
        anchor_dt: datetime | None = None
        is_historical = False
        if before_ts is not None:
            anchor_dt = datetime.fromtimestamp(before_ts, tz=UTC)
            # View is "historical" when the anchor is more than 1 hour before now
            is_historical = (now - anchor_dt).total_seconds() > 3600

        thermal_model = (
            self.learning.get_thermal_model(learning_health=self._build_learning_health()) if self.learning else {}
        )
        unit = self.config.get("temp_unit", "fahrenheit")
        _LOGGER.debug(
            "Chart data: thermal_model conf_passive=%s conf_hvac=%s passive=%d fan=%d vent=%d solar=%d heat=%d cool=%d",
            thermal_model.get("confidence_k_passive", "none"),
            thermal_model.get("confidence", "none"),
            thermal_model.get("observation_count_passive", 0),
            thermal_model.get("observation_count_fan_only", 0),
            thermal_model.get("observation_count_vent", 0),
            thermal_model.get("observation_count_solar", 0),
            thermal_model.get("observation_count_heat", 0),
            thermal_model.get("observation_count_cool", 0),
        )
        log_entries = self._chart_log.get_entries(range_str, before=anchor_dt)
        actual_outdoor = []
        actual_indoor = []
        for _e in log_entries:
            _ts = _e.get("ts")
            if not _ts:
                continue
            # Raw/hourly buckets use "indoor"/"outdoor"; daily buckets use "indoor_avg"/"outdoor_avg"
            _indoor = _e.get("indoor") if _e.get("indoor") is not None else _e.get("indoor_avg")
            _outdoor = _e.get("outdoor") if _e.get("outdoor") is not None else _e.get("outdoor_avg")
            if _indoor is not None:
                actual_indoor.append({"time": _ts, "temp": _indoor})
            if _outdoor is not None:
                actual_outdoor.append({"time": _ts, "temp": _outdoor})

        def _conv(v: float | None) -> float | None:
            return round(from_fahrenheit(v, unit), 1) if v is not None else None

        actual_outdoor = [{"time": p["time"], "temp": _conv(p["temp"])} for p in actual_outdoor]
        actual_indoor = [{"time": p["time"], "temp": _conv(p["temp"])} for p in actual_indoor]

        # Issue #470/#514: the band (timestamps, pre-cool trigger/target, and the
        # _compute_target_band_schedule() call itself) is computed once, HERE, via the
        # shared _build_target_band_for() helper — the canonical "call site A" — and
        # threaded into _build_predicted_indoor_future() below via its band_schedule=
        # parameter, so the displayed band and the ODE prediction curve always agree.
        _hvac_mode = getattr(self._current_classification, "hvac_mode", None) if self._current_classification else None
        if is_historical:
            # Issue #514: a historical viewport must show what the target band
            # actually was at that past time, not today's live band recomputed
            # against today's config/occupancy/classification. Read the immutable
            # per-cycle lower/upper snapshot persisted into chart_log at that time
            # (Phase 2 steps 6-7) instead of calling _build_target_band_for() with
            # today's `now`. Pre-fix entries (written before this field existed)
            # come back as {"lower": None, "upper": None} via plain dict.get() —
            # same null-safe shape as _extract_historical_setpoint() already uses.
            _raw_band = None
            _conv_band = [
                {"ts": e["ts"], "lower": _conv(e["lower"]), "upper": _conv(e["upper"])}
                for e in _extract_historical_target_band(log_entries)
            ]
        else:
            _raw_band = self._build_target_band_for(now, thermal_model)
            _conv_band = [{"ts": e["ts"], "lower": _conv(e["lower"]), "upper": _conv(e["upper"])} for e in _raw_band]

        # Historical views suppress forward-looking series (prediction + forecast).
        # They are meaningless for a window anchored in the past and would confuse
        # the chart by overlaying future data on a historical viewport.
        if is_historical:
            predicted_indoor = []
            forecast_outdoor = []
            _raw_predicted_indoor: list[dict] = []
            _raw_forecast_outdoor: list[dict] = []
        else:
            # Issue #802: kept in raw (internal Fahrenheit) units alongside the display-unit
            # `predicted_indoor`/`forecast_outdoor` below — the regime walk's pure decision
            # functions (decide_nat_vent_gate/decide_nat_vent_exit/decide_ode_ceiling_guard)
            # must be fed the same unit system self.config's raw comfort_heat/comfort_cool/
            # sleep_heat/etc. are stored in (matching the existing convention already used by
            # _compute_next_automation_action()'s own decide_nat_vent_gate() call, which reads
            # self._last_predicted_indoor / _build_future_forecast_outdoor() directly, never a
            # display-unit-converted copy) — converting first and comparing against raw config
            # thresholds would silently corrupt every decision for a non-Fahrenheit-display
            # user. Converting only once, at the end, also fixes a pre-existing bug in this
            # exact code path: _compute_effective_target_forward() previously received the
            # already-converted _conv_band as its `target_band` input, then its output was
            # converted a second time via _conv(e["target"]) below — a real double-conversion
            # for Celsius-display users, invisible on an all-Fahrenheit install.
            _raw_predicted_indoor = _build_predicted_indoor_future(
                self._hourly_forecast_temps,
                self.config,
                now,
                current_indoor_temp=self._get_indoor_temp(),
                thermal_model=thermal_model,
                occupancy_mode=self._occupancy_mode,
                classification=self._current_classification,
                band_schedule=_raw_band,
            )
            predicted_indoor = [{"ts": p["ts"], "temp": _conv(p["temp"])} for p in _raw_predicted_indoor]
            _raw_forecast_outdoor = _build_future_forecast_outdoor(
                self._hourly_forecast_temps,
                classification=self._current_classification,
            )
            forecast_outdoor = [{"ts": p["ts"], "temp": _conv(p["temp"])} for p in _raw_forecast_outdoor]

        def _conv_log_entry(e: dict) -> dict:
            e = dict(e)
            for k in ("pred_outdoor", "pred_indoor", "pred_outdoor_avg", "pred_indoor_avg"):
                if e.get(k) is not None:
                    e[k] = _conv(e[k])
            # Back-compat: old entries written before Issue #331 lack these keys.
            # Default to False so state_log always carries both fields.
            e.setdefault("fan_running", False)
            e.setdefault("nat_vent_active", False)
            return e

        log_entries = [_conv_log_entry(e) for e in log_entries]

        # Phase 3 (Investigation B, #786 follow-up): unified "effective target" chart line —
        # the real system target at any point in time regardless of source (comfort band,
        # TOU banking, or nat-vent thermostatic cycling). Historical portion reads chart_log's
        # setpoint/nat_vent_target fields directly; forward portion derives from the same
        # target_band/predicted_activity/TOU-window data already computed above — no new
        # resolution logic. predicted_activity is computed here (rather than inline in the
        # return dict below, as it was before this field existed) so both it and the new
        # forward effective-target series can share the one computation.
        #
        # Issue #802: the forward regime (which hours are nat-vent-eligible vs. HVAC, and
        # whether an off-classified day escalates to active cooling mid-day) is now resolved
        # ONCE here via _walk_forward_regime() — a genuine forward walk of the same
        # decide_nat_vent_gate()/decide_nat_vent_exit()/decide_ode_ceiling_guard() pure
        # functions production uses live — and shared by both _compute_predicted_activity()
        # (fan_active/windows_recommended/hvac_mode per hour) and
        # _compute_effective_target_forward()'s tier 3 (hvac_mode_by_ts). This replaces the
        # old standalone temperature-inequality heuristic, which had no session memory and
        # was self-defeating against nat-vent's own predicted cooling effect.
        if is_historical:
            _regime_by_ts: dict[str, dict] = {}
        else:
            _ae = getattr(self, "automation_engine", None)
            _day_modes = _compute_day_hvac_modes(self._hourly_forecast_temps, now, self._current_classification)
            _comfort_cool_raw = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
            _ceiling_threshold = _ae._ceiling_threshold(_comfort_cool_raw) if _ae else None
            _regime_by_ts = _walk_forward_regime(
                _day_modes,
                _raw_predicted_indoor,
                _raw_forecast_outdoor,
                _raw_band or [],
                self.config,
                self._occupancy_mode,
                thermal_model,
                _ae._manual_override_active if _ae else False,
                _ae._manual_override_mode if _ae else None,
                _ceiling_threshold,
                _ae._natural_vent_active if _ae else False,
            )

        _predicted_activity = (
            [] if is_historical else _compute_predicted_activity(_conv_band, _regime_by_ts, self.config)
        )
        _hvac_mode_by_ts = {ts: regime.get("hvac_mode", "off") for ts, regime in _regime_by_ts.items()}
        _effective_target_history = [
            {"ts": e["ts"], "target": _conv(e["target"])} for e in _extract_historical_effective_target(log_entries)
        ]
        if is_historical:
            _effective_target_forecast: list[dict] = []
        else:
            _nv_hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            _effective_target_forecast = [
                {"ts": e["ts"], "target": _conv(e["target"])}
                for e in _compute_effective_target_forward(
                    _raw_band or [],
                    _predicted_activity,
                    _hvac_mode_by_ts,
                    _nv_hysteresis,
                    self.config,
                    tou_precondition_window=self._tou_precondition_window_tuple(),
                )
            ]

        return {
            "predicted_indoor": predicted_indoor,
            "forecast_outdoor": forecast_outdoor,
            "actual_outdoor": actual_outdoor,
            "actual_indoor": actual_indoor,
            "current_hour": round(current_hour, 1),
            "thermal_model": {
                "confidence": thermal_model.get("confidence", "none"),
                "observation_count_heat": thermal_model.get("observation_count_heat", 0),
                "observation_count_cool": thermal_model.get("observation_count_cool", 0),
                "heating_rate": (
                    convert_delta(thermal_model["heating_rate_f_per_hour"], unit)
                    if thermal_model.get("heating_rate_f_per_hour") is not None
                    else None
                ),
                "cooling_rate": (
                    convert_delta(thermal_model["cooling_rate_f_per_hour"], unit)
                    if thermal_model.get("cooling_rate_f_per_hour") is not None
                    else None
                ),
                "unit": unit,
                "learning_health": thermal_model.get("learning_health", {}),
                "confidence_k_passive": thermal_model.get("confidence_k_passive", "none"),
                "k_passive": thermal_model.get("k_passive"),
                "k_vent_window": thermal_model.get("k_vent_window"),
                "k_vent_fan": thermal_model.get("k_vent_fan"),
                "k_solar": (
                    convert_delta(thermal_model["k_solar"], unit) if thermal_model.get("k_solar") is not None else None
                ),
                "avg_r_squared_passive": thermal_model.get("avg_r_squared_passive"),
                "last_observation_date": thermal_model.get("last_observation_date"),
                "observation_count_passive": thermal_model.get("observation_count_passive", 0),
                "observation_count_vent_window": thermal_model.get("observation_count_vent_window", 0),
                "observation_count_vent_fan": thermal_model.get("observation_count_vent_fan", 0),
                "observation_count_solar": thermal_model.get("observation_count_solar", 0),
                "swing_heat": round(convert_delta(thermal_model.get("swing_heat_f_display", 1.5), unit), 2),
                "swing_cool": round(convert_delta(thermal_model.get("swing_cool_f_display", 1.5), unit), 2),
                "swing_heat_measured": thermal_model.get("swing_heat_f") is not None,
                "swing_cool_measured": thermal_model.get("swing_cool_f") is not None,
                "observation_count_swing_heat": thermal_model.get("observation_count_swing_heat", 0),
                "observation_count_swing_cool": thermal_model.get("observation_count_swing_cool", 0),
                "confidence_swing_heat": thermal_model.get("confidence_swing_heat", "none"),
                "confidence_swing_cool": thermal_model.get("confidence_swing_cool", "none"),
                "solar_phase_offset_h": thermal_model.get("solar_phase_offset_h"),
            },
            "state_log": log_entries,
            "target_band": _conv_band,
            "predicted_setpoint": _derive_predicted_setpoint(_conv_band, _hvac_mode),
            "historical_setpoint": [
                {"ts": e["ts"], "setpoint": _conv(e["setpoint"])} for e in _extract_historical_setpoint(log_entries)
            ],
            "defense_lines": [] if is_historical else _compute_defense_lines(_conv_band),
            "predicted_activity": _predicted_activity,
            "effective_target_history": _effective_target_history,
            "effective_target_forecast": _effective_target_forecast,
            "unit": unit,
        }

    def _build_thermal_pipeline_summary(self) -> dict:
        """Build a snapshot of the current thermal observation pipeline state."""
        self._ensure_pending_observations()
        now = dt_util.now()
        pending = []
        for obs_type, obs in self._pending_observations.items():
            start_str = obs.get("start_time")
            elapsed = None
            if start_str:
                try:
                    start_ts = dt_util.parse_datetime(start_str)
                    if start_ts:
                        elapsed = round((now - start_ts).total_seconds() / 60.0, 1)
                except Exception:
                    pass
            samples = obs.get("samples", obs.get("active_samples", []))
            temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
            last_s = obs.get("last_sample_time")
            last_age = None
            if last_s:
                try:
                    last_ts = dt_util.parse_datetime(last_s)
                    if last_ts:
                        last_age = round((now - last_ts).total_seconds() / 60.0, 1)
                except Exception:
                    pass
            outdoor = samples[-1].get("outdoor_temp_f") if samples else getattr(self, "_last_outdoor_temp", None)
            pending.append(
                {
                    "obs_type": obs_type,
                    "status": obs.get("status", "unknown"),
                    "elapsed_minutes": elapsed,
                    "sample_count": len(samples),
                    "last_sample_age_minutes": last_age,
                    "indoor_range_f": [round(min(temps), 1), round(max(temps), 1)] if temps else None,
                    "indoor_delta_f": round(max(temps) - min(temps), 2) if temps else None,
                    "outdoor_f": round(outdoor, 1) if outdoor is not None else None,
                }
            )
        return {
            "pending": pending,
            "rejection_log_counts": {ot: len(evts) for ot, evts in getattr(self, "_rejection_log", {}).items()},
        }

    def compute_nat_vent_cycling_band(self) -> dict[str, float | None]:
        """Return the WHF fan's on/off cycling band (Issue #400/#402).

        Mirrors automation.py's nat_vent_temperature_check() sleep-window branch exactly,
        so the dashboard always matches the fan's actual cycling behavior. This is the
        single source of truth for the cycling target/on_threshold/off_threshold — extracted
        (Issue #402 follow-up) so get_debug_state() and the main status endpoint both call
        this instead of each recomputing the formula, which is exactly the "fix one
        duplicate implementation, miss the sibling" pattern that caused #400 and part of
        #402 in the first place.

        NOTE: despite the "target" name, these describe the WHF fan's on/off CYCLING
        midpoint — the range the fan hunts within while a nat-vent session is active. This
        is NOT a thermostat setpoint and is never written to the climate entity; do not
        confuse it with comfort_heat/comfort_cool or the armed comfort-band ceiling/floor.
        """
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        # Phase 3a-pre: routed through the shared self._nat_vent_target_now() (which itself
        # delegates to nat_vent_cycling.compute_nat_vent_target()) instead of an independent
        # third inline copy of the same formula — this function pre-dated the DRY
        # consolidation and was found to be a third duplicate not named in the original
        # audit (which only tracked automation.py and nat_vent_cycling.py).
        target = self._nat_vent_target_now()
        if target is None:
            return {"nat_vent_target": None, "nat_vent_on_threshold": None, "nat_vent_off_threshold": None}
        return {
            "nat_vent_target": target,
            "nat_vent_on_threshold": target + hysteresis,
            "nat_vent_off_threshold": target - hysteresis,
        }

    def _compute_fan_remote_status_fields(self) -> dict[str, Any]:
        """Return the QuietCool RF remote's timer/speed status-display fields (Issue #519).

        Single source of truth for `fan_remote_timer_hours`/`fan_remote_timer_ends`/
        `fan_remote_speed` — extracted so `_async_update_data_impl()` (the real path
        `coordinator.data` and the dashboard actually read) and `get_debug_state()` (debug
        endpoint + diagnostics download only) can't drift apart, following the exact
        precedent `compute_nat_vent_cycling_band()` set for this same class of bug
        (Issue #400/#402: fix one duplicate implementation, miss the sibling). Before this
        extraction, these fields existed only inside `get_debug_state()` and never reached
        `coordinator.data` at all, so the dashboard's WHF card never showed a remote speed
        (Issue #524).
        """
        ae = self.automation_engine
        return {
            # Issue #486: QuietCool RF remote timer selection, for status-card display only.
            "fan_remote_timer_hours": ae._fan_remote_timer_hours,
            "fan_remote_timer_ends": (ae._grace_end_time if ae._fan_remote_timer_hours is not None else None),
            # Issue #519: live ambient speed read wins (always current); falls back to the
            # engine's last press-derived value so the card isn't blank between beacons on
            # installs where the ambient sensor isn't discoverable (older firmware).
            "fan_remote_speed": self._read_fan_remote_speed() or ae._fan_remote_speed,
        }

    def get_debug_state(self) -> dict[str, Any]:
        """Return serializable debug state for the dashboard."""
        ae = self.automation_engine
        c = self._current_classification
        unit = self.config.get("temp_unit", "fahrenheit")

        # Door/window sensor states
        sensor_states = {}
        for sensor_id in self._resolved_sensors:
            sensor_states[sensor_id] = {
                "open": self._is_sensor_open(sensor_id),
                "friendly_name": sensor_id.split(".")[-1].replace("_", " ").title(),
            }

        _nat_vent_band = self.compute_nat_vent_cycling_band()
        _fan_remote_fields = self._compute_fan_remote_status_fields()

        return {
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
            "paused_by_door": ae.is_paused_by_door,
            "pause_suppressed_classification": (
                bool(ae.is_paused_by_door) and ae._last_classification_applied is not None
            ),
            "pre_pause_mode": ae._pre_pause_mode,
            "grace_active": ae._grace_active,
            "last_resume_source": ae._last_resume_source,
            "grace_end_time": getattr(ae, "_grace_end_time", None),
            "door_window_sensors": sensor_states,
            "pending_debounce_timers": list(self._door_open_timers.keys()),
            "classification": {
                "day_type": c.day_type if c else None,
                "trend_direction": c.trend_direction if c else None,
                "trend_magnitude": round(convert_delta(c.trend_magnitude, unit), 1) if c else None,
                "hvac_mode": c.hvac_mode if c else None,
                "windows_recommended": c.windows_recommended if c else None,
                "window_open_time": (c.window_open_time.isoformat() if c and c.window_open_time else None),
                "window_close_time": (c.window_close_time.isoformat() if c and c.window_close_time else None),
                "window_opportunity_morning": c.window_opportunity_morning if c else None,
                "window_opportunity_evening": c.window_opportunity_evening if c else None,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat() if c and c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat() if c and c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat() if c and c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat() if c and c.window_opportunity_evening_end else None
                ),
                "pre_condition": c.pre_condition if c else None,
                "pre_condition_target": (
                    round(from_fahrenheit(c.pre_condition_target, unit), 1)
                    if c and c.pre_condition_target is not None
                    else None
                ),
                "setback_modifier": round(convert_delta(c.setback_modifier or 0, unit), 1) if c else None,
                "today_low": (round(from_fahrenheit(c.today_low, unit), 1) if c and c.today_low is not None else None),
                "tomorrow_low": (
                    round(from_fahrenheit(c.tomorrow_low, unit), 1) if c and c.tomorrow_low is not None else None
                ),
            },
            "last_action_time": ae._last_action_time,
            "last_action_reason": ae._last_action_reason,
            "manual_override_active": ae._manual_override_active,
            "manual_override_mode": ae._manual_override_mode,
            "manual_override_time": ae._manual_override_time,
            "manual_grace_duration": ae.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS),
            "next_automation_action": self.data.get(ATTR_NEXT_AUTOMATION_ACTION, "") if self.data else "",
            "next_automation_time": self.data.get(ATTR_NEXT_AUTOMATION_TIME, "") if self.data else "",
            # Fan state (Issue #37)
            "fan_active": ae._fan_active,
            "fan_on_since": ae._fan_on_since,
            "fan_runtime_minutes": ae._get_fan_runtime_minutes(),
            "fan_override_active": ae._fan_override_active,
            "fan_override_time": ae._fan_override_time,
            "fan_remote_timer_hours": _fan_remote_fields["fan_remote_timer_hours"],
            "fan_remote_timer_ends": _fan_remote_fields["fan_remote_timer_ends"],
            "fan_remote_speed": _fan_remote_fields["fan_remote_speed"],
            "fan_mode_config": ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
            "economizer_active": ae._economizer_active,
            "economizer_phase": ae._economizer_phase,
            "resumed_from_pause": ae._resumed_from_pause,
            "occupancy_away_timer_pending": self._occupancy_away_timer_cancel is not None,
            "unit": unit,
            "thermal_pipeline": self._build_thermal_pipeline_summary(),
            "startup_coalesce_active": self._startup_coalesce_active,
            "startup_coalesce_seconds_remaining": (
                max(
                    0.0,
                    (dt_util.parse_datetime(self._startup_coalesce_expiry) - dt_util.now()).total_seconds(),
                )
                if self._startup_coalesce_expiry and self._startup_coalesce_active
                else None
            ),
            # Issue #396: surface decision-lock holder so a stuck coalesce/decision pass is
            # diagnosable from the dashboard, not just backend logs — "waiting on X since Y"
            # instead of a generic "waiting for coalescing" with no further detail.
            "decision_lock_holder": ae._decision_lock_holder,
            "decision_lock_held_seconds": (
                (dt_util.now() - ae._decision_lock_held_since).total_seconds()
                if ae._decision_lock_held_since is not None
                else None
            ),
            # Bug 2 (Issue #321): stuck-grace detection for debug pane
            "grace_stuck": (
                ae._manual_override_active
                and not ae._grace_active
                and ae._grace_end_time is not None
                and dt_util.parse_datetime(ae._grace_end_time) is not None
                and dt_util.now() > dt_util.parse_datetime(ae._grace_end_time)
            ),
            # Bug 3 (Issue #321): nat-vent cycling visibility in debug pane
            "nat_vent_active": ae._natural_vent_active,
            # Issue #540: soft-start qualifier — only meaningful when nat_vent_active is True.
            "nat_vent_soft_start": ae._nat_vent_soft_start,
            # Issue #338: AC assist status — true when nat-vent is active with FAN_MODE_HVAC
            # and aggressive_savings is off (full comfort band armed, compressor may assist).
            # FAN_MODE_BOTH excluded: _activate_fan() suppresses HVAC for BOTH (same as WHOLE_HOUSE).
            "nat_vent_ac_assist": (
                bool(ae._natural_vent_active)
                and self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) == FAN_MODE_HVAC
                and not self.config.get("aggressive_savings", False)
            ),
            "nat_vent_target": _nat_vent_band["nat_vent_target"],
            "nat_vent_on_threshold": _nat_vent_band["nat_vent_on_threshold"],
            "nat_vent_off_threshold": _nat_vent_band["nat_vent_off_threshold"],
            "nat_vent_cycling_paused": ae._natural_vent_active and not ae._fan_active,
        }

    async def _persist_shutdown_diagnostics(self) -> None:
        """Persist restart-cause diagnostics (Issue #403/#413).

        Shared by async_shutdown() (fires on config-entry unload/reload) and the
        EVENT_HOMEASSISTANT_STOP listener registered in async_setup() (fires on a real
        HA restart/deploy, which does NOT call async_unload_entry). Both paths must
        write these fields for the restart-cause classifier in async_restore_state()
        to work on the restarts that actually happen in practice.
        """
        self.learning._state.clean_shutdown = True
        self.learning._state.last_shutdown_version = VERSION
        self.learning._state.user_initiated_restart = self._user_initiated_shutdown
        await self._executor_job(self.learning.save_state)
        _LOGGER.info(
            "Shutdown diagnostics persisted: version=%s user_initiated=%s",
            VERSION,
            self._user_initiated_shutdown,
        )

    async def async_shutdown(self) -> None:
        """Clean up on shutdown."""
        _LOGGER.info("Climate Advisor v%s shutting down", VERSION)

        # Restart-cause diagnostics (Issue #403): mark this as a clean shutdown so the
        # next startup can distinguish a routine restart from a crash.
        await self._persist_shutdown_diagnostics()

        # Flush HVAC runtime and save state before cleanup
        self._flush_hvac_runtime()
        await self._async_save_state()

        # Cancel any pending occupancy away setback timer
        self._cancel_occupancy_away_timer()

        # Cancel any pending debounce timers
        for cancel in self._door_open_timers.values():
            cancel()
        self._door_open_timers.clear()
        self._door_open_timer_expiry.clear()

        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        self._unsubscribe_door_window_listeners()
        self.automation_engine.cleanup()


def _decide_pre_cool_reschedule(
    *,
    current_trigger_at: datetime | None,
    pre_cool_eligible: bool,
    nat_vent_close_delay_minutes: float,
    now: datetime,
) -> datetime | None:
    """Pure decision for _maybe_reschedule_pre_cool_on_nat_vent_exit() (#437 follow-up):
    should a pending pre-cool trigger be pulled earlier because nat-vent just exited
    for real, ahead of its originally-scheduled window_close_time?

    Returns the new (earlier) trigger time, or None when no reschedule should happen:
      - no trigger is currently pending (already fired today, or none was ever needed —
        `current_trigger_at is None`)
      - tonight isn't pre-cool eligible per ``resolve_pre_cool_modifier()`` (neither a
        warming trend nor tomorrow's forecast classifying hot — pre-cool wouldn't have
        been scheduled in the first place; Issue #558)
      - the candidate time (now + the same nat-vent-close delay the original schedule
        uses) is NOT earlier than what's already scheduled — this only ever pulls the
        trigger EARLIER, never later, so a nat-vent exit that happens to occur close to
        (or after) the already-scheduled time can't accidentally push pre-cool back.
    """
    if current_trigger_at is None:
        return None
    if not pre_cool_eligible:
        return None
    candidate = now + timedelta(minutes=nat_vent_close_delay_minutes)
    if candidate >= current_trigger_at:
        return None
    return candidate


def _compute_thermal_factors(chart_entries: list[dict]) -> dict:
    """Compute thermal lag and conditional differential from historical chart data.

    Returns:
        {
            "time_lag_hours": float,
            "cold_diff": float,    # indoor-outdoor when outdoor < THERMAL_COLD_BUCKET_LIMIT_F
            "mild_diff": float,    # THERMAL_COLD_BUCKET_LIMIT_F <= outdoor < THERMAL_MILD_BUCKET_LIMIT_F
            "warm_diff": float,    # indoor-outdoor when outdoor >= THERMAL_MILD_BUCKET_LIMIT_F
            "has_data": bool,
        }
    """
    valid = [e for e in chart_entries if e.get("indoor") is not None and e.get("outdoor") is not None]
    if len(valid) < 20:
        return {
            "time_lag_hours": 1.0,
            "cold_diff": 15.0,
            "mild_diff": 8.0,
            "warm_diff": 0.0,
            "has_data": False,
        }

    # Time lag: cross-correlation of consecutive outdoor vs indoor changes
    outdoors = [e["outdoor"] for e in valid]
    indoors = [e["indoor"] for e in valid]
    d_out = [outdoors[i + 1] - outdoors[i] for i in range(len(outdoors) - 1)]
    d_in = [indoors[i + 1] - indoors[i] for i in range(len(indoors) - 1)]
    best_lag, best_score = 0, float("-inf")
    for lag in range(min(5, len(d_out))):
        score = sum(d_out[i] * d_in[i + lag] for i in range(len(d_out) - lag))
        if score > best_score:
            best_score, best_lag = score, lag

    # Conditional differential from HVAC-idle entries
    idle_hvac = {"", "idle", "off"}
    buckets: dict[str, list[float]] = {"cold": [], "mild": [], "warm": []}
    for e in valid:
        if str(e.get("hvac", "")).lower() not in idle_hvac:
            continue
        delta = e["indoor"] - e["outdoor"]
        outdoor = e["outdoor"]
        if outdoor < THERMAL_COLD_BUCKET_LIMIT_F:
            buckets["cold"].append(delta)
        elif outdoor < THERMAL_MILD_BUCKET_LIMIT_F:
            buckets["mild"].append(delta)
        else:
            buckets["warm"].append(delta)

    def _median(vals: list[float], fallback: float) -> float:
        if len(vals) < 3:
            return fallback
        s = sorted(vals)
        return s[len(s) // 2]

    return {
        "time_lag_hours": float(best_lag),
        "cold_diff": round(_median(buckets["cold"], 15.0), 1),
        "mild_diff": round(_median(buckets["mild"], 8.0), 1),
        "warm_diff": round(_median(buckets["warm"], 0.0), 1),
        "has_data": True,
    }


def _outdoor_conditional_diff(outdoor: float, thermal_factors: dict) -> float:
    """Return the learned indoor-outdoor differential for a given outdoor temp.

    Linear interpolation over ±THERMAL_BUCKET_INTERP_HALF_F transition zones at bucket
    boundaries (THERMAL_COLD_BUCKET_LIMIT_F, THERMAL_MILD_BUCKET_LIMIT_F) eliminates the
    hard jump that occurs when outdoor crosses a threshold.
    """
    cold = thermal_factors.get("cold_diff", 15.0)
    mild = thermal_factors.get("mild_diff", 8.0)
    warm = thermal_factors.get("warm_diff", 0.0)

    _cold_lo = THERMAL_COLD_BUCKET_LIMIT_F - THERMAL_BUCKET_INTERP_HALF_F
    _cold_hi = THERMAL_COLD_BUCKET_LIMIT_F + THERMAL_BUCKET_INTERP_HALF_F
    _mild_lo = THERMAL_MILD_BUCKET_LIMIT_F - THERMAL_BUCKET_INTERP_HALF_F
    _mild_hi = THERMAL_MILD_BUCKET_LIMIT_F + THERMAL_BUCKET_INTERP_HALF_F

    if outdoor <= _cold_lo:
        return cold
    elif outdoor < _cold_hi:
        frac = (outdoor - _cold_lo) / (2 * THERMAL_BUCKET_INTERP_HALF_F)
        return cold + frac * (mild - cold)
    elif outdoor <= _mild_lo:
        return mild
    elif outdoor < _mild_hi:
        frac = (outdoor - _mild_lo) / (2 * THERMAL_BUCKET_INTERP_HALF_F)
        return mild + frac * (warm - mild)
    else:
        return warm


def _simulate_indoor_physics(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float | None,
    dt_hours: float,
    setpoint: float | None,
    *,
    comfort_heat: float,
    comfort_cool: float,
    hvac_mode: str | None = None,
) -> float:
    """Advance indoor temperature by dt_hours using the two-parameter ODE.

    dT/dt = k_passive * (T - T_outdoor) + Q
    Q = k_active when HVAC is driving toward setpoint, 0 otherwise.

    Pass hvac_mode="heat" or "cool" for correct behavior with sleep setback setpoints
    (sleep_heat < comfort_heat). When hvac_mode is None, falls back to threshold
    inference — only valid for comfort-range setpoints.
    """
    import math

    k_p = k_passive
    q = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint:
                q = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint:
                q = -abs(k_active)
        else:
            # legacy: threshold inference — backward-compat for callers without hvac_mode
            if setpoint >= comfort_heat and t_start < setpoint:
                q = abs(k_active)  # heating: always positive
            elif setpoint <= comfort_cool and t_start > setpoint:
                q = -abs(k_active)  # cooling: always negative

    exp_kp = math.exp(k_p * dt_hours)
    t_next = (
        t_outdoor + (t_start - t_outdoor) * exp_kp + (q / k_p) * (exp_kp - 1) if k_p != 0 else t_start + q * dt_hours
    )

    # Clamp: heating won't overshoot setpoint; cooling won't undershoot
    if setpoint is not None:
        if q > 0:
            t_next = min(t_next, setpoint)
        elif q < 0:
            t_next = max(t_next, setpoint)
    return t_next


def _solar_factor(
    local_hour: int,
    phase_offset_h: float = THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT,
) -> float:
    """Return a 0–1 solar intensity factor for the given local hour.

    phase_offset_h shifts the effective peak: effective_hour = local_hour − offset.
    With offset=0 the peak is at local hour 13. With the default offset=2 the peak
    is at local hour 15 (3pm), matching typical thermal-mass lag.
    """
    try:
        h = int(local_hour)
    except (TypeError, ValueError):
        return 0.0
    effective_hour = h - int(round(phase_offset_h))
    if effective_hour < THERMAL_SOLAR_DAYTIME_START_H or effective_hour >= THERMAL_SOLAR_DAYTIME_END_H:
        return 0.0
    span = (THERMAL_SOLAR_DAYTIME_END_H - THERMAL_SOLAR_DAYTIME_START_H) / 2.0
    return math.sin(math.pi * (effective_hour - THERMAL_SOLAR_DAYTIME_START_H) / (span * 2))


def _estimate_solar_phase_offset(
    window_entries: list[dict],
) -> tuple[float | None, str | None]:
    """Estimate solar phase offset from a daytime passive window.

    Returns (phase_obs, None) on success, (None, reject_reason) on failure.
    phase_obs = actual_indoor_peak_hour − 13, clamped to [OFFSET_MIN, OFFSET_MAX].

    Quality gates:
      - ≥ THERMAL_SOLAR_PHASE_MIN_ENTRIES entries
      - window span ≥ THERMAL_SOLAR_PHASE_MIN_WINDOW_H hours
      - indoor ΔT ≥ THERMAL_SOLAR_PHASE_MIN_DT_F°F
      - peak is interior (not first or last entry)
    """
    if len(window_entries) < THERMAL_SOLAR_PHASE_MIN_ENTRIES:
        return None, REJECT_TOO_FEW_SAMPLES

    # Parse timestamps
    try:
        times = [datetime.fromisoformat(str(e["ts"])) for e in window_entries]
    except (KeyError, ValueError, TypeError):
        return None, REJECT_TOO_FEW_SAMPLES

    # Window span check
    span_h = (times[-1] - times[0]).total_seconds() / 3600.0
    if span_h < THERMAL_SOLAR_PHASE_MIN_WINDOW_H:
        return None, REJECT_WINDOW_TOO_SHORT

    # Extract indoor temps
    try:
        indoor_temps = [float(e["indoor"]) for e in window_entries]
    except (KeyError, ValueError, TypeError):
        return None, REJECT_SMALL_DELTA

    # Indoor ΔT check
    temp_range = max(indoor_temps) - min(indoor_temps)
    if temp_range < THERMAL_SOLAR_PHASE_MIN_DT_F:
        return None, REJECT_SMALL_DELTA

    # Peak must not be at the first entry — a first-entry peak means the window
    # captured the tail of a prior peak, not the rise. A last-entry peak is
    # acceptable: the window end may have truncated a still-rising temperature.
    peak_idx = indoor_temps.index(max(indoor_temps))
    if peak_idx == 0:
        return None, REJECT_NO_INTERIOR_PEAK

    # Peak local hour — prefer as_local(); fall back to raw UTC hour if the
    # as_local result is not a real datetime (e.g. in test stubs).
    peak_time = times[peak_idx]
    peak_local = dt_util.as_local(peak_time)
    peak_hour = peak_local.hour if isinstance(peak_local, datetime) else peak_time.hour

    # phase_obs = peak_hour − 13, clamped to [MIN, MAX]
    phase_obs = float(peak_hour - 13)
    phase_obs_clamped = max(
        float(THERMAL_SOLAR_PHASE_OFFSET_MIN),
        min(float(THERMAL_SOLAR_PHASE_OFFSET_MAX), phase_obs),
    )

    return phase_obs_clamped, None


def _entry_hour(entry: dict) -> int | None:
    """Parse local hour from a chart_log entry ts field. Returns None on failure."""
    try:
        return datetime.fromisoformat(entry["ts"]).hour
    except (KeyError, ValueError, TypeError):
        return None


def _is_ac_duty_solar_day(day_entries: list[dict]) -> tuple[bool, str]:
    """Quality filter for AC duty cycle solar phase estimation.

    Returns (True, "") if the day qualifies, or (False, reject_reason) otherwise.
    Pure function — no instance state.

    Quality gates (in order):
      1. At least one entry in 11:00-18:00 has setpoint_cool field.
      1b. Setpoint must be in [SETPOINT_MIN_F, SETPOINT_MAX_F].
      2. Setpoint spread across 11:00-18:00 < SETPOINT_STABILITY_F.
      3. >= AC_MIN_COOL_ENTRIES cool entries in 11:00-16:00.
      4. At least one 11:00-16:00 entry has indoor > median setpoint.
    """
    # Collect entries in the stability window (11:00-18:00)
    stability_entries = [
        e
        for e in day_entries
        if _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H
    ]

    # Gate 1: must have setpoint_cool field in at least one stability-window entry
    setpoints = [e["setpoint_cool"] for e in stability_entries if e.get("setpoint_cool") is not None]
    if not setpoints:
        return False, REJECT_AC_NO_COOL_SETPOINTS

    # Gate 1b: setpoint must be in a reasonable range
    if min(setpoints) < THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F or max(setpoints) > THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F:
        return False, REJECT_AC_SETPOINT_OUT_OF_RANGE

    # Gate 2: setpoint must be stable across the stability window
    if max(setpoints) - min(setpoints) > THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F:
        return False, REJECT_AC_SETPOINT_UNSTABLE

    # Gate 3: >= AC_MIN_COOL_ENTRIES cool entries in peak window (11:00-16:00)
    peak_cool_count = sum(
        1
        for e in day_entries
        if e.get("hvac") == "cool"
        and _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
    )
    if peak_cool_count < THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES:
        return False, REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY

    # Gate 4: at least one peak-window entry has indoor > median setpoint
    median_setpoint = sorted(setpoints)[len(setpoints) // 2]
    breach = any(
        e.get("indoor", 0) > median_setpoint
        for e in day_entries
        if _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
    )
    if not breach:
        return False, REJECT_AC_NO_SETPOINT_BREACH

    return True, ""


def _estimate_ac_duty_solar_phase(day_entries: list[dict]) -> float | None:
    """Estimate solar phase offset from AC duty cycle peak hour.

    Counts cool entries per hour in the 11:00-16:00 window, computes duty fraction,
    finds the peak-duty hour, and returns (peak_hour - 13) clamped to
    [THERMAL_SOLAR_PHASE_OFFSET_MIN, THERMAL_SOLAR_PHASE_OFFSET_MAX].

    Returns None if no cool entries exist in the window.
    Pure function — no instance state.
    """
    # Count cool and total entries per hour in 11:00-16:00 window
    cool_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for e in day_entries:
        h = _entry_hour(e)
        _start = THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H
        _end = THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
        in_peak = h is not None and _start <= h < _end
        if not in_peak:
            continue
        total_counts[h] = total_counts.get(h, 0) + 1
        if e.get("hvac") == "cool":
            cool_counts[h] = cool_counts.get(h, 0) + 1

    if not cool_counts:
        return None

    # Duty fraction per hour
    duty = {h: cool_counts[h] / total_counts[h] for h in cool_counts if total_counts.get(h, 0) > 0}
    if not duty:
        return None

    peak_hour = max(duty, key=lambda h: duty[h])
    offset = float(peak_hour - 13)
    return max(float(THERMAL_SOLAR_PHASE_OFFSET_MIN), min(float(THERMAL_SOLAR_PHASE_OFFSET_MAX), offset))


def _simulate_indoor_physics_v3(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float | None,
    dt_hours: float,
    setpoint: float | None,
    *,
    comfort_heat: float,
    comfort_cool: float,
    k_solar: float | None = None,
    solar_factor: float = 0.0,
    hvac_mode: str | None = None,
) -> float:
    """Advance indoor temperature using the v3 ODE with a solar term.

    Issue #587: k_vent/ventilation_active removed — k_vent was confirmed dead (the
    sole caller always passed ventilation_active=False, so it never affected a live
    prediction). Ventilation is represented instead via k_passive_for_hour substitution
    at the call site (k_vent_window swapped in for window-open hours).
    """
    k_eff = k_passive

    q_hvac = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint:
                q_hvac = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint:
                q_hvac = -abs(k_active)
        else:
            # legacy: threshold inference — backward-compat for callers without hvac_mode
            if setpoint >= comfort_heat and t_start < setpoint:
                q_hvac = abs(k_active)
            elif setpoint <= comfort_cool and t_start > setpoint:
                q_hvac = -abs(k_active)

    q_solar = (k_solar * solar_factor) if (k_solar is not None) else 0.0
    q_total = q_hvac + q_solar

    exp_keff = math.exp(k_eff * dt_hours)
    if k_eff != 0:
        t_next = t_outdoor + (t_start - t_outdoor) * exp_keff + (q_total / k_eff) * (exp_keff - 1)
    else:
        t_next = t_start + q_total * dt_hours

    if setpoint is not None:
        if q_hvac > 0:
            t_next = min(t_next, setpoint)
        elif q_hvac < 0:
            t_next = max(t_next, setpoint)
    return t_next


def _compute_pre_cool_trigger_time_pure(
    classification: DayClassification | None,
    config: dict[str, Any],
    now: datetime,
) -> datetime | None:
    """Pure computation behind ``ClimateAdvisorCoordinator._compute_pre_cool_trigger_time()``
    (Issue #514) — takes ``classification``/``config``/``now`` explicitly instead of reading
    ``self.*`` or calling ``dt_util.now()`` internally, mirroring ``_compute_target_band_schedule()``'s
    own already-pure shape. This is the root fix for the call-site-divergence bug class: because
    the canonical trigger-time logic previously lived only as a bound method,
    ``_build_predicted_indoor_future()`` (a module-level function with no ``self``) couldn't call
    it directly and reimplemented it inline instead — see that function's ``band_schedule`` fallback
    branch. Any future caller can now import and call this function directly instead of writing a
    third divergent copy.

    Primary: nat-vent window close time + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES.
    Fallback: wake_time - PRE_COOL_WAKE_OFFSET_HOURS.
    Returns None if tonight isn't eligible for pre-cool — see ``resolve_pre_cool_modifier()``
    (warming trend, or tomorrow independently forecast hot).
    """
    from .const import (
        CONF_SLEEP_COOL,
        DEFAULT_SLEEP_COOL,
        PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
        PRE_COOL_WAKE_OFFSET_HOURS,
    )

    c = classification
    _modifier = resolve_pre_cool_modifier(c, config) if c else None
    if _modifier is None:
        return None

    # Verify the pre-cool target would actually differ from sleep_cool
    sleep_cool = float(config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL))
    pre_cool_target = compute_pre_cool_target(config, _modifier)
    if pre_cool_target >= sleep_cool:
        _LOGGER.info(
            "Pre-cool scheduling: clamped target (%.1f°F) == sleep_cool (%.1f°F); skipping",
            pre_cool_target,
            sleep_cool,
        )
        return None

    today = now.date()

    # Primary: nat-vent window close time + delay
    if c.window_close_time is not None:
        wct_dt = dt_util.as_local(datetime.combine(today, c.window_close_time).replace(tzinfo=None))
        # If window close is before midnight (typical), use today; else tomorrow
        if wct_dt < now:
            wct_dt = wct_dt + timedelta(days=1)
        trigger = wct_dt + timedelta(minutes=PRE_COOL_POST_NAT_VENT_DELAY_MINUTES)
        _LOGGER.info(
            "Pre-cool scheduled for %s (nat-vent close %s + %dmin); target %.1f°F",
            trigger.strftime("%H:%M"),
            c.window_close_time.strftime("%H:%M"),
            PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
            pre_cool_target,
        )
        return trigger

    # Fallback: wake_time - offset
    wake_str = config.get("wake_time", "06:30")
    wake_h, wake_m = int(wake_str.split(":")[0]), int(wake_str.split(":")[1])
    wake_dt = dt_util.as_local(datetime.combine(today, time(wake_h, wake_m)).replace(tzinfo=None))
    # If wake_time already passed, schedule for tomorrow night
    if wake_dt < now:
        wake_dt = wake_dt + timedelta(days=1)
    trigger = wake_dt - timedelta(hours=PRE_COOL_WAKE_OFFSET_HOURS)
    _LOGGER.info(
        "Pre-cool scheduled for %s (wake_time %s - %.0fh fallback); target %.1f°F",
        trigger.strftime("%H:%M"),
        wake_str,
        PRE_COOL_WAKE_OFFSET_HOURS,
        pre_cool_target,
    )
    return trigger


def _compute_target_band_schedule(
    hourly_timestamps: list,
    config: dict,
    occupancy_mode: str,
    now: Any,
    setback_modifier: float = 0.0,
    thermal_model: dict | None = None,
    classification: Any | None = None,
    pre_cool_trigger_h: float | None = None,
    pre_cool_target: float | None = None,
    tou_precondition_window: tuple[datetime, datetime, float, str] | None = None,
) -> list[dict]:
    """Compute the dynamic target band (lower/upper) for each hourly timestamp.

    Returns a list of dicts: [{"ts": ISO_str, "lower": float, "upper": float}].

    Logic per timestamp:
    - Away today: flat setback band (shifted by setback_modifier).
    - Vacation (any day): deep setback band (setback ± VACATION_SETBACK_EXTRA + modifier).
    - Home/guest or future days when away: wake/sleep schedule with ramps.
      Wake ramp: 2h linear interpolation from sleep setback → comfort band.
      Sleep ramp: 1h linear interpolation from comfort band → sleep setback.

    Night-owl schedules (sleep_time < wake_time across midnight) are handled by
    normalising sleep_h += 24 and h += 24 when h < wake_h, keeping comparisons
    in chronological order.

    When thermal_model and classification are both provided, sleep_heat is derived
    via compute_bedtime_setback() — matching automation.py's adaptive setpoint logic.

    ``tou_precondition_window`` (Issue #786): ``(window_start, window_end, target, mode)``
    — when a timestamp falls in ``[window_start, window_end)``, the resolved TOU banking
    target overrides ``lower`` (mode="cool", banking toward the floor) or ``upper``
    (mode="heat", banking toward the ceiling), applied as the final step after whichever
    branch above computed the base band — an additive override layer, same shape as the
    existing ``pre_cool_target`` mechanism. This guards the rare case where the
    pre-conditioning window overlaps a wake/sleep ramp transition, where the base band's
    computed value would otherwise not exactly match the commanded setpoint (mandatory
    Chart Coverage rule — the chart must never show a band conflicting with what the
    engine actually commands).
    """
    comfort_heat = float(config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    comfort_cool = float(config.get("comfort_cool", DEFAULT_COMFORT_COOL))
    setback_heat = float(config.get("setback_heat", DEFAULT_SETBACK_HEAT))
    setback_cool = float(config.get("setback_cool", DEFAULT_SETBACK_COOL))
    sleep_heat = float(config.get("sleep_heat", comfort_heat - DEFAULT_SETBACK_DEPTH_F))
    sleep_cool = float(config.get("sleep_cool", comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F))

    # G1/G2: use compute_bedtime_setback() when thermal model + classification available —
    # aligns chart band with the adaptive sleep setpoint used by automation.py for both
    # heat (sleep_heat raised toward comfort) and cool (sleep_cool lowered toward comfort).
    if thermal_model is not None and classification is not None:
        _hvac_mode = getattr(classification, "hvac_mode", None)
        if _hvac_mode == "heat":
            sleep_heat = compute_bedtime_setback(config, thermal_model, classification)
        elif _hvac_mode == "cool":
            sleep_cool = compute_bedtime_setback(config, thermal_model, classification)

    # I3: apply setback_modifier to setback bounds (mirrors automation.py behaviour)
    setback_heat_eff = setback_heat + setback_modifier
    setback_cool_eff = setback_cool + setback_modifier

    wake_time = _parse_time(config.get("wake_time", "06:30"))
    sleep_time_cfg = _parse_time(config.get("sleep_time", "22:30"))
    wake_h = wake_time.hour + wake_time.minute / 60.0
    sleep_h = sleep_time_cfg.hour + sleep_time_cfg.minute / 60.0
    wake_ramp_h = 2.0
    sleep_ramp_h = 1.0

    # I6: midnight wraparound — night-owl schedules where sleep < wake (e.g. sleep=01:00, wake=09:00)
    night_owl = wake_h > sleep_h
    if night_owl:
        sleep_h += 24  # normalise to a > wake_h value (e.g. 1 → 25)

    now_date = now.date() if hasattr(now, "date") else None

    result = []
    for ts in hourly_timestamps:
        if ts is None:
            continue
        ts_date = ts.date() if hasattr(ts, "date") else None

        # I5: vacation applies setback to ALL days (not just today); away only applies to today
        if occupancy_mode == OCCUPANCY_VACATION:
            lower = setback_heat_eff - VACATION_SETBACK_EXTRA
            upper = setback_cool_eff + VACATION_SETBACK_EXTRA
        elif occupancy_mode == OCCUPANCY_AWAY and ts_date == now_date:
            lower = setback_heat_eff
            upper = setback_cool_eff
        else:
            # Home/guest schedule (or future days when away)
            h = ts.hour + ts.minute / 60.0
            # I6: normalise h for night-owl schedules
            h_n = h + 24 if (night_owl and h < wake_h) else h

            if h_n < wake_h:
                # Pre-wake: sleep band; apply pre-cool ceiling from trigger time onward
                lower = sleep_heat
                if pre_cool_trigger_h is not None and pre_cool_target is not None and h_n >= pre_cool_trigger_h:
                    upper = pre_cool_target
                else:
                    upper = sleep_cool
            elif h_n < wake_h + wake_ramp_h:
                # Wake ramp: interpolate toward comfort
                frac = (h_n - wake_h) / wake_ramp_h
                lower = sleep_heat + frac * (comfort_heat - sleep_heat)
                upper = sleep_cool + frac * (comfort_cool - sleep_cool)
            elif h_n < sleep_h:
                # Awake: comfort band
                lower = comfort_heat
                upper = comfort_cool
            elif h_n < sleep_h + sleep_ramp_h:
                # Sleep ramp: interpolate toward sleep setback
                frac = (h_n - sleep_h) / sleep_ramp_h
                lower = comfort_heat + frac * (sleep_heat - comfort_heat)
                upper = comfort_cool + frac * (sleep_cool - comfort_cool)
            else:
                # Post-sleep: sleep band; apply pre-cool ceiling from trigger time onward
                lower = sleep_heat
                if pre_cool_trigger_h is not None and pre_cool_target is not None and h_n >= pre_cool_trigger_h:
                    upper = pre_cool_target
                else:
                    upper = sleep_cool

        # Issue #786: TOU pre-conditioning override — additive, applied after whichever
        # branch above computed the base band (see docstring).
        if tou_precondition_window is not None:
            _window_start, _window_end, _tou_target, _tou_mode = tou_precondition_window
            if _window_start <= ts < _window_end:
                if _tou_mode == "cool":
                    lower = _tou_target
                elif _tou_mode == "heat":
                    upper = _tou_target

        result.append({"ts": ts.isoformat(), "lower": round(lower, 1), "upper": round(upper, 1)})

    return result


def _find_ceiling_breach_time(
    predicted_indoor: list[dict] | None,
    comfort_cool: float,
    tolerance: float = 0.0,
) -> datetime | None:
    """Return the first timestamp in predicted_indoor where temp > comfort_cool + tolerance.

    Args:
        predicted_indoor: List of {"ts": ISO-string, "temp": float} dicts from ODE curve.
        comfort_cool: Upper comfort bound (°F).
        tolerance: Additional threshold buffer (°F). Use CEILING_BRIDGE_TOLERANCE_F for
            bridge homes where k_vent_window proxy is less accurate for closed-window phase.

    Returns:
        datetime of first breach entry, or None if no breach or empty curve.
    """
    if not predicted_indoor:
        return None
    threshold = comfort_cool + tolerance
    for entry in predicted_indoor:
        temp = entry.get("temp")
        if temp is not None and temp > threshold:
            ts_str = entry.get("ts")
            if ts_str:
                try:
                    return datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue
    return None


def _compute_day_hvac_modes(
    hourly_forecast: list[dict] | None,
    now: Any,
    classification: Any | None = None,
) -> dict[Any, str]:
    """Classify each future calendar day's HVAC mode ("heat"/"cool"/"off") from its
    forecast high (Issue #802 — extracted verbatim from
    ``_build_predicted_indoor_future()``'s former inline block; that function now calls
    this in place of its own copy — bit-identical output required. The chart's forward
    regime prediction (``_walk_forward_regime()``) is this function's second caller).

    Today's entry is overridden with the live classification's real ``hvac_mode`` —
    ``_day_mode()``'s own threshold logic only sees remaining forecast entries, which in
    the evening are cold night temps (max<60°F even on a 68°F day), causing a spurious
    "heat" mode that triggers the Q_hvac bug.

    Returns ``{}`` when ``hourly_forecast`` has no parseable entries (mirrors the
    original code's "no valid entries" early-return path — callers must handle an empty
    dict explicitly, the same way the original handled an empty ``day_highs``).
    """
    day_highs: dict = {}
    parse_errors = 0
    for entry in hourly_forecast or []:
        dt_str = entry.get("datetime") or entry.get("time")
        if not dt_str:
            parse_errors += 1
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            local_ts = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
            temp = entry.get("temperature")
            if temp is not None:
                day_highs.setdefault(local_ts.date(), []).append(float(temp))
        except (ValueError, TypeError) as exc:
            parse_errors += 1
            _LOGGER.debug("_compute_day_hvac_modes: skipping %r — %s", dt_str, exc)

    if parse_errors:
        _LOGGER.warning(
            "_compute_day_hvac_modes: %d entries failed to parse",
            parse_errors,
        )
    if not day_highs:
        return {}

    def _day_mode(temps: list[float]) -> str:
        high = max(temps)
        if high >= THRESHOLD_HOT:
            return "cool"
        if high >= THRESHOLD_WARM or high >= THRESHOLD_MILD:
            return "off"
        return "heat"

    day_modes = {d: _day_mode(t) for d, t in day_highs.items()}
    _today_date = dt_util.as_local(now).date()
    if classification is not None and hasattr(classification, "hvac_mode"):
        day_modes[_today_date] = classification.hvac_mode
        _LOGGER.debug(
            "_compute_day_hvac_modes: today mode overridden from classification: %s",
            classification.hvac_mode,
        )
    _LOGGER.debug(
        "_compute_day_hvac_modes: %d days classified: %s",
        len(day_modes),
        {str(d): m for d, m in sorted(day_modes.items())},
    )
    return day_modes


def _build_predicted_indoor_future(
    hourly_forecast: list[dict] | None,
    config: dict[str, Any],
    now: Any,
    current_indoor_temp: float | None = None,
    thermal_model: dict | None = None,
    occupancy_mode: str = OCCUPANCY_HOME,
    classification: Any | None = None,
    band_schedule: list[dict] | None = None,
    tou_precondition_window: tuple[datetime, datetime, float, str] | None = None,
) -> list[dict]:
    """Build future predicted indoor temps from the automation plan.

    When thermal_model has "low" confidence or above, uses the physics ODE:
      T(t+dt) = T_outdoor + (T - T_outdoor)*exp(k_p*dt) + (Q/k_p)*(exp(k_p*dt) - 1)
    Otherwise falls back to the setpoint-schedule approach (mirrors automation plan).

    Fallback (setpoint-schedule):
    - heat days: sleep_heat (or comfort_heat−4°F default) overnight, comfort_heat waking
    - cool days: sleep_cool (or comfort_cool+3°F default) overnight, comfort_cool waking
    - off days: outdoor + 2°F buffer, floored at setback_heat

    Args:
        band_schedule: Pre-computed _compute_target_band_schedule() output (Issue #470).
            When provided, reused directly instead of recomputing pre-cool trigger/target
            and calling _compute_target_band_schedule() again — the caller (get_chart_data())
            already computes it once via the canonical self._compute_pre_cool_trigger_time().
            When omitted (e.g. direct unit tests of this function), falls back to computing
            it internally as before, for full backward compatibility.

    Returns list of {"ts": ISO_str, "temp": float} for hours strictly after now.
    """
    if not hourly_forecast:
        if classification is not None:
            _LOGGER.debug("_build_predicted_indoor_future: no hourly_forecast — using cosine fallback")
            # Build synthetic hourly list from cosine model so the function can proceed normally
            now_local = dt_util.as_local(dt_util.now())
            cosine = _build_outdoor_curve(
                high=classification.today_high,
                low=classification.today_low,
                hourly_forecast=None,
            )
            synthetic = []
            for entry in cosine:
                h = entry["hour"]
                future_dt = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
                if future_dt <= now_local:
                    future_dt += timedelta(days=1)
                synthetic.append(
                    {
                        "datetime": future_dt.isoformat(),
                        "temperature": entry["temp"],
                    }
                )
            hourly_forecast = synthetic
        else:
            _LOGGER.debug("_build_predicted_indoor_future: no hourly_forecast — returning empty")
            return []

    _LOGGER.debug(
        "_build_predicted_indoor_future: %d forecast entries, now=%s",
        len(hourly_forecast),
        now.isoformat() if hasattr(now, "isoformat") else now,
    )

    comfort_heat = float(config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    comfort_cool = float(config.get("comfort_cool", DEFAULT_COMFORT_COOL))
    setback_heat = float(config.get("setback_heat", DEFAULT_SETBACK_HEAT))  # absolute floor for heat
    setback_cool = float(config.get("setback_cool", DEFAULT_SETBACK_COOL))  # absolute ceiling for cool

    # Mirror automation engine (automation.py compute_setback_temp) — use
    # sleep_heat/sleep_cool if configured; otherwise default to comfort ± DEFAULT_SETBACK_DEPTH_*F.
    # setback_heat/setback_cool remain as hard floor/ceiling guards.
    setback_temp_heat = float(config.get("sleep_heat", comfort_heat - DEFAULT_SETBACK_DEPTH_F))
    setback_temp_heat = max(setback_temp_heat, setback_heat)
    setback_temp_cool = float(config.get("sleep_cool", comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F))
    setback_temp_cool = min(setback_temp_cool, setback_cool)

    # --- Classify each future day by forecast high (Issue #802: extracted to
    # _compute_day_hvac_modes() so the chart's forward regime prediction can reuse the
    # exact same per-day classification instead of a second, divergent copy) ---
    day_modes = _compute_day_hvac_modes(hourly_forecast, now, classification)
    if not day_modes:
        _LOGGER.warning(
            "_build_predicted_indoor_future: no valid entries in %d-entry forecast — "
            "predicted indoor will be empty. First entry: %r",
            len(hourly_forecast),
            hourly_forecast[0] if hourly_forecast else None,
        )
        return []

    # Decide whether to use physics simulation or setpoint-schedule fallback.
    # Physics requires: k_passive from any confident source, and a seed temp.
    _use_physics = False
    _k_passive: float | None = None
    _k_active_heat: float | None = None
    _k_active_cool: float | None = None
    _k_solar: float | None = None
    _k_vent_window: float | None = None
    # Issue #587 (2.8 scope boundary): k_vent_fan is learned/displayed but NOT wired
    # into per-hour forecast selection below — there is no forecast-time fan/WHF
    # schedule to key a per-hour fan-active computation off (classification's window
    # fields are window_open_time/window_close_time/windows_recommended only, no
    # fan/WHF-schedule field exists). Retrofitting a forecast fan-schedule concept is
    # its own future dedicated design pass. Its only live use here is the conservative
    # "does *any* solar/vent signal exist" branch condition below.
    _k_vent_fan: float | None = None
    _k_passive_via_bridge: bool = False
    # _phase_offset: when model has a learned value use it; otherwise 0.0 preserves
    # pre-feature behavior for callers that do not supply solar_phase_offset_h.
    # (THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT=2 is applied by the coordinator's
    # self._solar_phase_offset instance attribute, not by this standalone function.)
    _phase_offset: float = 0.0
    if thermal_model and current_indoor_temp is not None:
        _conf = thermal_model.get("confidence", "none")
        _conf_k_passive = thermal_model.get("confidence_k_passive")
        _k_passive = thermal_model.get("k_passive")
        _k_active_heat = thermal_model.get("k_active_heat")
        _k_active_cool = thermal_model.get("k_active_cool")
        _k_solar = thermal_model.get("k_solar")
        _k_vent_window = thermal_model.get("k_vent_window")
        _k_vent_fan = thermal_model.get("k_vent_fan")
        _raw_phase = thermal_model.get("solar_phase_offset_h")
        _phase_offset = float(_raw_phase) if _raw_phase is not None else 0.0
        # Gate bridge: when k_passive is absent but k_vent_window is learned, use it as
        # a proxy k_passive so the ODE can activate for thermally inert homes that only
        # have ventilated observations.  k_vent_window is always ≤ 0 for valid commits
        # (inert home → k≈0 accepted by widened ventilated bounds in learning.py).
        # When k_vent_window = 0.0 exactly the ODE produces a flat prediction (T stays at
        # current_indoor_temp), which is correct for a perfectly inert home.
        _k_passive_via_bridge = False
        if (_k_passive is None or _conf_k_passive == "none") and _k_vent_window is not None and _k_vent_window <= 0:
            _k_passive = _k_vent_window
            _k_passive_via_bridge = True
            _LOGGER.debug(
                "_build_predicted_indoor_future: gate bridge — using k_vent_window=%.4f as proxy k_passive",
                _k_passive,
            )
        _physics_eligible = (
            (
                _conf != "none"
                or (_conf_k_passive is not None and _conf_k_passive not in (None, "none"))
                or _k_passive_via_bridge  # bridge-provided k needs no confidence count
            )
            and _k_passive is not None
            and (_k_passive < 0 or _k_passive_via_bridge)
        )
        if _physics_eligible:
            _use_physics = True
            _LOGGER.debug(
                "_build_predicted_indoor_future: using physics model "
                "(conf=%s conf_k_passive=%s k_passive=%.4f k_active_heat=%s k_active_cool=%s)",
                _conf,
                _conf_k_passive,
                _k_passive,
                f"{_k_active_heat:.2f}" if _k_active_heat is not None else "None",
                f"{_k_active_cool:.2f}" if _k_active_cool is not None else "None",
            )
        else:
            _LOGGER.debug(
                "_build_predicted_indoor_future: using fallback ramp (conf=%s k_passive=%s indoor=%s)",
                _conf,
                f"{_k_passive:.4f}" if _k_passive is not None else "None",
                f"{current_indoor_temp:.1f}" if current_indoor_temp is not None else "None",
            )
    elif not _use_physics:
        _LOGGER.debug("_build_predicted_indoor_future: using fallback ramp (no model or no indoor temp)")

    # B3: Pre-compute the full band schedule for all future timestamps in one call,
    # then look up per entry. Avoids re-parsing config + ramp math 24+ times.
    #
    # Issue #470: when the caller (get_chart_data()) already computed this schedule
    # once via the canonical self._compute_pre_cool_trigger_time(), reuse it directly
    # instead of recomputing it here via an independent (and not identical) inline
    # pre-cool trigger-time formula. This also fixes a pre-existing, unrelated
    # divergence: the internal recompute pinned sleep_heat/sleep_cool to this
    # function's own raw-clamped setback_temp_heat/cool before calling
    # _compute_target_band_schedule(), which — via compute_bedtime_setback()'s
    # "explicit value takes priority" branch — silently skipped the adaptive
    # thermal-model-derived sleep floor the DISPLAYED chart band uses whenever
    # sleep_heat/sleep_cool weren't explicitly configured. Reusing the caller's
    # schedule (built from the real, unmodified config) makes the ODE prediction
    # curve agree with the displayed band in that case, instead of silently disagreeing.
    if band_schedule is not None:
        _band_lookup: dict[str, dict] = {b["ts"]: b for b in band_schedule}
    else:
        _band_config = dict(config)
        _band_config["sleep_heat"] = setback_temp_heat
        _band_config["sleep_cool"] = setback_temp_cool
        _future_timestamps_for_band: list = []
        for _fc in hourly_forecast:
            _dt_s = _fc.get("datetime") or _fc.get("time")
            if not _dt_s:
                continue
            try:
                _dt_o = datetime.fromisoformat(_dt_s)
                _lts = dt_util.as_local(_dt_o) if _dt_o.tzinfo else _dt_o
                if _lts > now:
                    _future_timestamps_for_band.append(_lts)
            except (ValueError, TypeError):
                pass
        # Compute pre-cool band parameters so the prediction curve tracks the pre-cool setpoint
        _ode_pc_trigger_h: float | None = None
        _ode_pc_target: float | None = None
        _ode_pc_modifier = resolve_pre_cool_modifier(classification, config) if classification is not None else None
        if _ode_pc_modifier is not None:
            from .const import (
                PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
                PRE_COOL_WAKE_OFFSET_HOURS,
            )

            _wct = getattr(classification, "window_close_time", None)
            if _wct is not None:
                _ode_pc_trigger_h = _wct.hour + _wct.minute / 60.0 + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES / 60.0
            else:
                _wake_str = config.get("wake_time", "06:30")
                _wake_h_raw = int(_wake_str.split(":")[0]) + int(_wake_str.split(":")[1]) / 60.0
                _ode_pc_trigger_h = _wake_h_raw - PRE_COOL_WAKE_OFFSET_HOURS
            _ode_pc_target = compute_pre_cool_target(config, _ode_pc_modifier)

        _computed_band_schedule = _compute_target_band_schedule(
            _future_timestamps_for_band,
            _band_config,
            occupancy_mode,
            now,
            thermal_model=thermal_model,
            classification=classification,
            pre_cool_trigger_h=_ode_pc_trigger_h,
            pre_cool_target=_ode_pc_target,
            tou_precondition_window=tou_precondition_window,
        )
        _band_lookup = {b["ts"]: b for b in _computed_band_schedule}

    # Pre-compute window schedule for per-hour ventilation switching (Phase 2C).
    # k_vent_window is the total measured k during ventilated conditions — replacement
    # semantics, not addition. Window-open hours substitute k_vent_window for k_passive.
    _windows_recommended = bool(classification.windows_recommended) if classification else False
    _window_open_time = getattr(classification, "window_open_time", None) if classification else None
    _window_close_time = getattr(classification, "window_close_time", None) if classification else None

    result = []
    skipped_past = 0
    _t_current = current_indoor_temp  # running indoor temp for physics simulation
    _prev_ts = now  # previous timestamp for dt calculation

    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        if not dt_str:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            local_ts = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
        except (ValueError, TypeError):
            continue
        if local_ts <= now:
            skipped_past += 1
            continue
        outdoor = entry.get("temperature")
        mode = day_modes.get(local_ts.date(), "off")

        # Look up pre-computed band entry for this timestamp
        _band = _band_lookup.get(local_ts.isoformat(), {"lower": comfort_heat, "upper": comfort_cool})

        # Per-hour window-open check (computed before bridge guard so both can reference it).
        # Guard: skip substitution when gate bridge already used k_vent_window as
        # k_passive for all hours (_k_passive_via_bridge=True).
        _hour_windows_open = (
            _windows_recommended
            and _k_vent_window is not None
            and _window_open_time is not None
            and _window_close_time is not None
            and _window_open_time <= local_ts.time() < _window_close_time
        )

        # Bridge guard: k_vent_window is measured during open-window conditions
        # (envelope k + ventilation k).  Applying it to window-closed hours overpredicts
        # decay (τ≈7h) — the true envelope τ is much longer (≈50h).  Fall back to ramp
        # only when the classification schedules windows for today but the current hour
        # falls outside the open window.  When windows are not recommended at all (no
        # window schedule), k_vent_window is the best available proxy and physics runs
        # for all hours (behaviour consistent with pre-guard bridge semantics).
        _bridge_guard_applies = (
            _k_passive_via_bridge
            and _windows_recommended  # classification has a window schedule today
            and not _hour_windows_open  # but this hour is outside the open window
        )
        _use_physics_for_hour = _use_physics and not _bridge_guard_applies
        if _bridge_guard_applies and _use_physics:
            _LOGGER.debug(
                "_build_predicted_indoor_future: bridge hour=%s windows-closed, using ramp "
                "(k_vent_window not valid for envelope-only decay)",
                local_ts.strftime("%H:%M"),
            )

        if _use_physics_for_hour and _t_current is not None and outdoor is not None:
            if mode == "heat":
                setpoint = _band["lower"]
                k_active_for_mode = _k_active_heat
            elif mode == "cool":
                setpoint = _band["upper"]
                k_active_for_mode = _k_active_cool
            else:
                setpoint = None  # HVAC off — pure passive decay
                k_active_for_mode = None

            # Time step in hours between consecutive entries
            try:
                dt_hours = (local_ts - _prev_ts).total_seconds() / 3600.0
            except Exception:
                dt_hours = 1.0
            dt_hours = max(dt_hours, 1 / 60.0)  # floor at 1 min

            # Per-hour k selection: window-open hours use k_vent_window (total ventilated
            # rate) as a replacement for k_passive. k_vent_window is measured as the total
            # effective k during ventilated conditions — replacement semantics, not addition.
            _k_passive_for_hour = _k_vent_window if (_hour_windows_open and not _k_passive_via_bridge) else _k_passive
            if _hour_windows_open and not _k_passive_via_bridge:
                _LOGGER.debug(
                    "_build_predicted_indoor_future: hour=%s using k_vent_window=%.4f (windows open %s–%s)",
                    local_ts.strftime("%H:%M"),
                    _k_vent_window,
                    _window_open_time,
                    _window_close_time,
                )

            # Issue #587: conservative branch-selection form — includes k_vent_fan
            # alongside k_solar/k_vent_window rather than assuming it's never
            # independently populated (verified: it can be, once vent_fan_decay
            # observations commit; see 2.8 for why it's not used in _k_passive_for_hour
            # selection above even when this branch is taken for its sake).
            if _k_solar is not None or _k_vent_window is not None or _k_vent_fan is not None:
                _t_current = _simulate_indoor_physics_v3(
                    _t_current,
                    float(outdoor),
                    _k_passive_for_hour,  # type: ignore[arg-type]
                    k_active_for_mode,
                    dt_hours,
                    setpoint,
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                    k_solar=_k_solar,
                    solar_factor=_solar_factor(local_ts.hour, _phase_offset),
                    hvac_mode=mode,
                )
            else:
                _t_current = _simulate_indoor_physics(
                    _t_current,
                    float(outdoor),
                    _k_passive_for_hour,  # type: ignore[arg-type]
                    k_active_for_mode,
                    dt_hours,
                    setpoint,
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                    hvac_mode=mode,
                )
            temp = _t_current
        else:
            # Setpoint-schedule fallback
            if mode == "heat":
                temp = _band["lower"]
            elif mode == "cool":
                temp = _band["upper"]
            else:
                # Off-day ramp: anchor to current indoor when available — a stable home
                # sitting at 69°F is better predicted by its actual reading than by
                # outdoor+2°F (which would be ~58°F on a cold day).  Fall back to
                # outdoor+2°F only when no indoor seed exists.
                if _t_current is not None:
                    temp = _t_current
                elif outdoor is not None:
                    temp = max(setback_heat, float(outdoor) + 2.0)
                else:
                    temp = comfort_heat

        _prev_ts = local_ts
        result.append({"ts": local_ts.isoformat(), "temp": round(temp, 1)})

    _LOGGER.debug(
        "_build_predicted_indoor_future: %d past skipped, %d future returned",
        skipped_past,
        len(result),
    )
    if not result:
        _LOGGER.warning(
            "_build_predicted_indoor_future: zero future entries (now=%s, forecast %r → %r)",
            now.isoformat() if hasattr(now, "isoformat") else now,
            ((hourly_forecast[0].get("datetime") or hourly_forecast[0].get("time")) if hourly_forecast else None),
            ((hourly_forecast[-1].get("datetime") or hourly_forecast[-1].get("time")) if hourly_forecast else None),
        )

    # Expand hourly ODE output to 30-min resolution via linear interpolation.
    # This gives the prediction archive 30-min granularity matching chart_log cadence
    # and eliminates the step-function artifact on the historical chart.
    _interp: list[dict] = []
    for _i, _pt in enumerate(result):
        _interp.append(_pt)
        if _i + 1 < len(result):
            _next = result[_i + 1]
            try:
                _pt_dt = datetime.fromisoformat(_pt["ts"])
                _next_dt = datetime.fromisoformat(_next["ts"])
            except (ValueError, KeyError):
                continue
            _mid_dt = _pt_dt + (_next_dt - _pt_dt) / 2
            _mid_temp = round((_pt["temp"] + _next["temp"]) / 2, 1)
            _interp.append({"ts": _mid_dt.isoformat(), "temp": _mid_temp})
    result = _interp

    return result


def _cosine_outdoor_curve(high: float, low: float) -> list[dict]:
    """Sinusoidal outdoor temperature model (peak 3 PM, trough 3 AM).

    This is the original prediction model, now used as a fallback when
    hourly forecast data is not available from the weather integration.
    """
    mid = (high + low) / 2.0
    amp = (high - low) / 2.0
    return [
        {
            "hour": h,
            "temp": round(mid + amp * math.cos(2 * math.pi * (h - 15) / 24), 1),
        }
        for h in range(24)
    ]


def _build_outdoor_curve(
    high: float,
    low: float,
    hourly_forecast: list[dict] | None,
) -> list[dict]:
    """Build 24 hourly outdoor temperature predictions.

    Uses actual hourly forecast data for the *shape* of the curve (when
    peaks and troughs occur), then normalises the result so the range
    spans the daily forecast ``high`` / ``low``.  Falls back to the
    sinusoidal model when no usable hourly data is available.
    """
    if not hourly_forecast:
        return _cosine_outdoor_curve(high, low)

    # Parse hourly entries into an integer-hour lookup (today only).
    # Use dt_util for timezone-aware "today" so UTC datetimes are
    # compared against the correct local date.
    today = dt_util.now().date()
    known: dict[int, float] = {}
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            # Convert to local time before extracting the date so that
            # UTC timestamps map to the correct calendar day.
            local_dt = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
            if local_dt.date() != today:
                continue
            known[local_dt.hour] = float(temp)
        except (ValueError, TypeError):
            continue

    if not known:
        return _cosine_outdoor_curve(high, low)

    # Fill all 24 hours: known values, linear interpolation for gaps,
    # cosine fallback at the edges.
    cosine = {p["hour"]: p["temp"] for p in _cosine_outdoor_curve(high, low)}
    known_hours = sorted(known)
    raw: list[float] = []

    for h in range(24):
        if h in known:
            raw.append(known[h])
        else:
            before = [k for k in known_hours if k < h]
            after = [k for k in known_hours if k > h]
            if before and after:
                h0, h1 = before[-1], after[0]
                frac = (h - h0) / (h1 - h0)
                raw.append(known[h0] + frac * (known[h1] - known[h0]))
            else:
                raw.append(cosine[h])

    # Normalise so the curve spans the daily high/low.  The hourly
    # forecast often has a narrower range than the daily summary; this
    # keeps the shape realistic while honouring the reported extremes.
    raw_min = min(raw)
    raw_max = max(raw)
    if raw_max - raw_min > 0.1:
        scale = (high - low) / (raw_max - raw_min)
        result = [{"hour": h, "temp": round(low + (t - raw_min) * scale, 1)} for h, t in enumerate(raw)]
    else:
        # Flat or near-flat hourly data — fall back to cosine
        result = _cosine_outdoor_curve(high, low)

    return result


def _parse_forecast_entries(hourly_forecast: list[dict] | None) -> list[tuple[datetime, float]]:
    """Extract raw (datetime, temperature) pairs from hourly forecast entries.

    Shared field-extraction/validation used by every function that reads
    self._hourly_forecast_temps, so entry-shape parsing (datetime/time key
    fallback, temperature/temp key fallback, ISO parse failure handling) only
    needs to be correct in one place. Timestamps are returned exactly as
    parsed (naive or aware, in original order) — callers apply their own
    timezone normalization, since existing callers intentionally differ in
    how they treat naive timestamps (UTC-anchored delta comparison vs.
    local-display formatting).
    """
    if not hourly_forecast:
        return []
    result: list[tuple[datetime, float]] = []
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            result.append((dt_obj, float(temp)))
        except (ValueError, TypeError):
            continue
    return result


def _build_future_forecast_outdoor(
    hourly_forecast: list[dict] | None,
    classification: Any | None = None,
) -> list[dict]:
    """Extract future hourly outdoor temps from the weather forecast.

    Returns all entries at or after now as {"ts": ISO_string, "temp": float}.
    Covers all available forecast days (2-10+), not just today.
    Unlike _build_outdoor_curve, values are NOT normalised to daily high/low —
    the raw forecast temperatures are used directly.

    If hourly_forecast is empty or yields no future entries and classification
    is provided, falls back to a cosine curve using today's high/low so the
    chart future region is never blank on daily-only weather integrations.
    """
    now = dt_util.now()
    result = []
    for dt_obj, temp in _parse_forecast_entries(hourly_forecast):
        local_dt = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
        if local_dt < now:
            continue
        result.append({"ts": local_dt.isoformat(), "temp": round(temp, 1)})
    if not result and classification is not None:
        # Hourly forecast unavailable — build cosine curve for display
        cosine = _cosine_outdoor_curve(classification.today_high, classification.today_low)
        for entry in cosine:
            h = entry["hour"]
            future_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h)
            if future_dt < now:
                future_dt += timedelta(days=1)
            result.append({"ts": future_dt.isoformat(), "temp": round(float(entry["temp"]), 1)})
    result.sort(key=lambda x: x["ts"])
    return result


def _extract_current_hour_forecast_temp(
    hourly_forecast: list[dict] | None,
    now: datetime,
) -> float | None:
    """Return the forecast temp for the entry nearest to now, within ±2 hours.

    HA's hourly forecast returns entries starting at the next full hour, so
    exact hour matching would never find the current hour. Instead, find the
    entry with minimum absolute time delta to now.
    """
    entries = _parse_forecast_entries(hourly_forecast)
    if not entries:
        return None
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    best_temp: float | None = None
    best_delta: float = float("inf")
    for dt_obj, temp in entries:
        entry_utc = dt_obj.replace(tzinfo=UTC) if dt_obj.tzinfo is None else dt_obj.astimezone(UTC)
        delta = abs((entry_utc - now_utc).total_seconds())
        if delta < best_delta and delta <= 7200:
            best_delta = delta
            best_temp = round(temp, 1)
    return best_temp


def _interpolate_hourly_outdoor_temp(
    hourly_forecast: list[dict] | None,
    now: datetime,
) -> tuple[float | None, str]:
    """Estimate current outdoor temp by linearly interpolating between the two
    hourly forecast entries bracketing `now`.

    HA's hourly forecast returns entries starting at the next full hour, so a
    single reading nearest to `now` (as _extract_current_hour_forecast_temp
    does) can be up to ~59 minutes out of phase with where outdoor temp
    actually is within that hour. Interpolating between the two bracketing
    entries estimates the current position along that trajectory instead.

    Returns (temp, method):
      "interpolated": now falls between two usable entries — true linear interpolation.
      "edge-nearest": now is within 2h before the first entry or after the last —
          clamped to that single edge value (mirrors the ±2h tolerance already
          used by _extract_current_hour_forecast_temp).
      "unavailable": empty/unparseable forecast, or now is outside the ±2h edge
          tolerance on both ends — caller must fall back.

    All comparisons use absolute UTC time deltas, never wall-clock hour
    arithmetic, so this is safe across DST transitions.
    """
    entries = _parse_forecast_entries(hourly_forecast)
    if not entries:
        return None, "unavailable"

    def _to_utc(dt_obj: datetime) -> datetime:
        return dt_obj.replace(tzinfo=UTC) if dt_obj.tzinfo is None else dt_obj.astimezone(UTC)

    now_utc = _to_utc(now)
    normalized = sorted(((_to_utc(dt_obj), temp) for dt_obj, temp in entries), key=lambda pair: pair[0])

    first_dt, first_temp = normalized[0]
    last_dt, last_temp = normalized[-1]

    if now_utc <= first_dt:
        if (first_dt - now_utc).total_seconds() <= 7200:
            return round(first_temp, 1), "edge-nearest"
        return None, "unavailable"

    if now_utc >= last_dt:
        if (now_utc - last_dt).total_seconds() <= 7200:
            return round(last_temp, 1), "edge-nearest"
        return None, "unavailable"

    for (dt_before, temp_before), (dt_after, temp_after) in zip(normalized, normalized[1:], strict=False):
        if dt_before <= now_utc <= dt_after:
            span = (dt_after - dt_before).total_seconds()
            if span <= 0:
                return round(temp_before, 1), "edge-nearest"
            fraction = (now_utc - dt_before).total_seconds() / span
            interpolated = temp_before + (temp_after - temp_before) * fraction
            return round(interpolated, 1), "interpolated"

    return None, "unavailable"


def _derive_predicted_setpoint(
    target_band: list[dict],
    hvac_mode: str | None,
) -> list[dict]:
    """Derive predicted setpoint list from target_band entries.

    Heat mode: lower bound; cool mode: upper bound; off/None: null.
    """
    result = []
    for entry in target_band:
        ts = entry.get("ts")
        if hvac_mode == "heat":
            sp = entry.get("lower")
        elif hvac_mode == "cool":
            sp = entry.get("upper")
        else:
            sp = None
        result.append({"ts": ts, "setpoint": sp})
    return result


def _extract_historical_setpoint(log_entries: list[dict]) -> list[dict]:
    """Extract {ts, setpoint} pairs from state_log entries."""
    result = []
    for e in log_entries:
        ts = e.get("ts")
        if not ts:
            continue
        result.append({"ts": ts, "setpoint": e.get("setpoint")})
    return result


def _extract_historical_target_band(log_entries: list[dict]) -> list[dict]:
    """Extract {ts, lower, upper} pairs from state_log entries (Issue #514).

    Mirrors ``_extract_historical_setpoint()`` exactly — same null-safe shape via
    plain ``dict.get()``, which returns ``None`` for both an explicit ``null`` and a
    missing key. Entries persisted before this fix simply have no "lower"/"upper"
    keys at all (old ``chart_log`` schema); ``.get()`` returns ``None`` for those the
    same way it already does for setpoint on entries predating that field, requiring
    no separate "is this an old entry" branch.
    """
    result = []
    for e in log_entries:
        ts = e.get("ts")
        if not ts:
            continue
        result.append({"ts": ts, "lower": e.get("lower"), "upper": e.get("upper")})
    return result


def _compute_defense_lines(target_band: list[dict]) -> list[dict]:
    """Return [{ts, heat, cool}] from target_band — always both bounds, never null.

    Unlike _derive_predicted_setpoint (single bound per hvac_mode), this always
    exposes both the heat-defense threshold (lower) and cool-defense threshold (upper)
    so the frontend can render them as always-present automation intent lines.
    """
    return [{"ts": e["ts"], "heat": e.get("lower"), "cool": e.get("upper")} for e in target_band]


def _walk_forward_regime(
    day_modes: dict[date, str],
    predicted_indoor: list[dict],
    forecast_outdoor: list[dict],
    target_band: list[dict],
    config: dict,
    occupancy_mode: str,
    thermal_model: dict | None,
    manual_override_active: bool,
    manual_override_mode: str | None,
    ceiling_threshold: float | None,
    initial_session_active: bool,
) -> dict[str, dict]:
    """Forward-walk the real, already-validated production nat-vent gate/exit/ceiling-guard
    functions hour by hour (Issue #802), replacing the old standalone temperature-inequality
    heuristic that had no session memory and was self-defeating against nat-vent's own
    predicted cooling effect.

    All temperature inputs (``predicted_indoor``, ``forecast_outdoor``, ``target_band``,
    ``config`` values, ``ceiling_threshold``) must be in the SAME (raw, internal Fahrenheit)
    unit system — matching how ``decide_nat_vent_gate()``/``decide_nat_vent_exit()``/
    ``decide_ode_ceiling_guard()`` are already used elsewhere in this file (e.g.
    ``_compute_next_automation_action()``'s own nat-vent-start prediction, Issue #528).

    Per calendar day (from ``day_modes``, itself derived once via
    ``_compute_day_hvac_modes()``):
      - Day mode ``heat``/``cool`` -> HVAC regime for the whole day; nat-vent is not
        evaluated at all.
      - Day mode ``off`` -> nat-vent-eligible, subject to same-day escalation. Each hour,
        in order:
          1. Resolve nat-vent's session-active state FIRST (``decide_nat_vent_exit()`` if
             currently active, ``decide_nat_vent_gate()`` if not) — this hour's result, not
             a stale one, since step 2 depends on it.
          2. Check ``decide_ode_ceiling_guard()`` using THIS hour's session-active state as
             its ``natural_vent_active`` input, scanning only the *remaining* predicted-indoor
             curve from this hour forward (matching production's own "if evaluated right now"
             semantics — never scanning past-already-walked entries, which would let an
             already-resolved earlier breach masquerade as a future one).
          3. On ``ESCALATE``: the rest of this calendar day becomes HVAC-cool regime; the
             nat-vent walk stops for the remainder of the day. A new day gets a fresh
             evaluation (escalation is a same-day event).

    Returns ``{ts: {"nat_vent_active": bool, "hvac_mode": str}}`` — ``hvac_mode`` is the day's
    classified mode, overridden to ``"cool"`` for hours at/after an escalation. No new
    threshold math: composition of three pre-existing, differentially-validated pure
    functions plus the day-mode lookup, per the approved plan.
    """
    indoor_by_ts = {e["ts"]: e.get("temp") for e in predicted_indoor if e.get("ts")}
    outdoor_by_ts = {e["ts"]: e.get("temp") for e in forecast_outdoor if e.get("ts")}
    predicted_indoor_index = {e["ts"]: i for i, e in enumerate(predicted_indoor) if e.get("ts")}

    comfort_heat_raw = float(config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    sleep_heat = float(config.get("sleep_heat", comfort_heat_raw))
    comfort_cool = float(config.get("comfort_cool", DEFAULT_COMFORT_COOL))
    nat_vent_delta = float(config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
    hysteresis = float(config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
    fan_mode = str(config.get(CONF_FAN_MODE, FAN_MODE_DISABLED))
    aggressive_savings = bool(config.get("aggressive_savings", False))

    _tm = thermal_model or {}
    k_passive = _tm.get("k_passive")
    confidence_k_passive = _tm.get("confidence_k_passive") or _tm.get("confidence", "none")
    k_passive_via_bridge = bool(_tm.get("k_passive_via_bridge"))
    k_active_cool = _tm.get("k_active_cool")

    result: dict[str, dict] = {}
    session_active = initial_session_active
    current_day: date | None = None
    escalated_to_cool = False

    for entry in target_band:
        ts_str = entry.get("ts")
        if not ts_str:
            continue
        ts_dt: datetime | None = None
        with contextlib.suppress(ValueError, TypeError):
            ts_dt = datetime.fromisoformat(ts_str)
        if ts_dt is None:
            continue

        local_dt = dt_util.as_local(ts_dt) if ts_dt.tzinfo else ts_dt
        day = local_dt.date()
        if day != current_day:
            current_day = day
            escalated_to_cool = False  # a fresh calendar day gets a fresh evaluation

        day_mode = day_modes.get(day, "off")

        if day_mode != "off" or escalated_to_cool:
            effective_mode = "cool" if escalated_to_cool else day_mode
            result[ts_str] = {"nat_vent_active": False, "hvac_mode": effective_mode}
            continue

        lower = entry.get("lower")
        upper = entry.get("upper")
        indoor = indoor_by_ts.get(ts_str)
        outdoor = outdoor_by_ts.get(ts_str)
        in_sleep_window = _in_sleep_window(local_dt, config)

        # Step 1: resolve nat-vent's session-active state for THIS hour first.
        if session_active:
            exit_decision = decide_nat_vent_exit(
                NatVentExitInputs(
                    indoor=indoor,
                    outdoor=outdoor,
                    comfort_heat_raw=comfort_heat_raw,
                    sleep_heat=sleep_heat,
                    in_sleep_window=in_sleep_window,
                    hysteresis=hysteresis,
                    comfort_cool=comfort_cool,
                    nat_vent_delta=nat_vent_delta,
                    occupancy_mode=occupancy_mode,
                    thermal_confidence=confidence_k_passive,
                    k_passive=k_passive,
                    manual_override_active=manual_override_active,
                    manual_override_mode=manual_override_mode,
                )
            )
            if exit_decision.reason != NatVentExitReason.NONE:
                session_active = False
        elif lower is not None and upper is not None:
            session_active = decide_nat_vent_gate(
                NatVentGateInputs(
                    outdoor=outdoor,
                    indoor=indoor,
                    comfort_heat_raw=comfort_heat_raw,
                    sleep_heat=sleep_heat,
                    in_sleep_window=in_sleep_window,
                    comfort_cool=comfort_cool,
                    nat_vent_delta=nat_vent_delta,
                    hysteresis=hysteresis,
                    fan_mode=fan_mode,
                    aggressive_savings=aggressive_savings,
                )
            )

        # Step 2: ceiling-guard escalation check, using THIS hour's session_active — only
        # scans the remaining predicted-indoor curve from this hour forward.
        _idx = predicted_indoor_index.get(ts_str)
        _remaining_predicted_indoor = predicted_indoor[_idx:] if _idx is not None else []
        guard_decision = decide_ode_ceiling_guard(
            OdeCeilingGuardInputs(
                predicted_indoor=_remaining_predicted_indoor,
                hvac_mode=day_mode,
                k_passive=k_passive,
                confidence_k_passive=confidence_k_passive,
                k_passive_via_bridge=k_passive_via_bridge,
                k_active_cool=k_active_cool,
                comfort_cool=comfort_cool,
                outdoor=outdoor,
                indoor=indoor,
                natural_vent_active=session_active,
                ceiling_threshold=ceiling_threshold,
                now=ts_dt,
            )
        )
        if guard_decision.outcome == OdeCeilingGuardOutcome.ESCALATE:
            escalated_to_cool = True
            result[ts_str] = {"nat_vent_active": False, "hvac_mode": "cool"}
            continue

        result[ts_str] = {"nat_vent_active": session_active, "hvac_mode": day_mode}

    return result


def _compute_predicted_activity(
    target_band: list[dict],
    regime_by_ts: dict[str, dict],
    config: dict,
) -> list[dict]:
    """Per forecast hour: hvac_mode intent, fan_active, windows_recommended.

    Issue #802: reads the already-resolved per-hour regime map from
    ``_walk_forward_regime()`` (a genuine forward walk of the real production
    decide_nat_vent_gate()/decide_nat_vent_exit()/decide_ode_ceiling_guard() functions)
    instead of an independent, session-memory-less temperature-inequality heuristic —
    ``fan_active`` and ``windows_recommended`` now mirror the same signal rather than being
    two separately-computed, near-identical, similarly-flickery formulas.
    """
    fan_mode = str(config.get("fan_mode", "auto"))

    result = []
    for band_entry in target_band:
        ts = band_entry.get("ts")
        if not ts:
            continue
        regime = regime_by_ts.get(ts, {})
        hvac_mode = regime.get("hvac_mode", "off")

        fan_active = True if fan_mode == "on" else bool(regime.get("nat_vent_active"))
        windows_recommended = fan_active

        result.append(
            {
                "ts": ts,
                "hvac_mode": hvac_mode,
                "fan_active": fan_active,
                "windows_recommended": windows_recommended,
            }
        )
    return result


def _extract_historical_effective_target(log_entries: list[dict]) -> list[dict]:
    """Historical (past) portion of the unified "effective target" chart line (Phase 3a).

    Per cycle: ``chart_log``'s real ``setpoint`` when present (compressor-commanded,
    genuinely source-agnostic — comfort-band and TOU banking both land here identically
    since it only reads what the thermostat's ``target_temperature`` attribute reads),
    else ``nat_vent_target`` when ``nat_vent_active`` was true that cycle (the real
    thermostatic value the fan was cycling around — not a band-edge approximation),
    else ``None`` only when genuinely undefined (thermostat off, no nat-vent, no
    comfort-band-active mode).

    This is the corrected replacement for the dead ``historical_setpoint``/
    ``_extract_historical_setpoint()`` field (Investigation B): that field was a naive
    setpoint-only pass-through, ``None`` for 3 of chart_log's 4 write call sites (fixed
    in Phase 3a step 2 above) and always ``None`` while nat-vent was active (nat-vent
    never calls ``set_temperature``, so it never populates ``setpoint``). Kept as a
    separate function/field rather than repurposing ``_extract_historical_setpoint()``
    in place — that function and its ``historical_setpoint`` API field are directly
    pinned by ``tests/test_chart_setpoint.py``'s locked band-edge-only contract and are
    left untouched for backward compatibility.
    """
    result = []
    for e in log_entries:
        ts = e.get("ts")
        if not ts:
            continue
        setpoint = e.get("setpoint")
        if setpoint is not None:
            target = setpoint
        elif e.get("nat_vent_active"):
            target = e.get("nat_vent_target")
        else:
            target = None
        result.append({"ts": ts, "target": target})
    return result


def _compute_effective_target_forward(
    target_band: list[dict],
    predicted_activity: list[dict],
    hvac_mode_by_ts: dict[str, str],
    hysteresis: float,
    config: dict,
    tou_precondition_window: tuple[datetime, datetime, float, str] | None = None,
) -> list[dict]:
    """Forward (future) portion of the unified "effective target" chart line (Phase 3b).

    Per future timestamp, in priority order:
      1. The TOU banking target (``tou_precondition_window``'s resolved ``target``)
         while ``ts`` falls inside ``[precondition_start, schedule_start)`` — reusing
         Phase 2's already-cached band-schedule input verbatim, no new resolution.
      2. Else ``nat_vent_cycling.compute_nat_vent_target()``'s output — fed from this
         timestamp's own ``target_band.lower``/``.upper`` (already sleep/wake/TOU-ramp
         -aware per Phase 2) and this timestamp's in-sleep-window state — while
         ``predicted_activity[].fan_active`` is true for this timestamp (Investigation
         B's pre-existing, already-accepted forward nat-vent-active proxy; not
         re-derived or made more accurate here, per Assumption Audit #5).
      3. Else the plain active comfort-band edge for this timestamp's effective HVAC mode
         (heat: lower, cool: upper), read from ``hvac_mode_by_ts`` — a per-timestamp map
         (Issue #802) rather than one static mode for the whole forecast, so a day the
         forecast classifies differently from today, or an hour the ceiling-guard walk
         escalated to active cooling, picks the correct edge instead of a stale whole-
         forecast value. Same derivation ``_derive_predicted_setpoint()`` used, now only
         the fallback tier instead of the whole answer.

    Degrades gracefully to tier 3 whenever tiers 1/2 don't apply or their inputs are
    incomplete (missing band bounds, unparseable timestamp) — never raises, never
    fabricates a value out of tier 3's band-edge range.
    """
    activity_fan_active_by_ts = {e.get("ts"): bool(e.get("fan_active")) for e in predicted_activity if e.get("ts")}

    result: list[dict] = []
    for entry in target_band:
        ts_str = entry.get("ts")
        if not ts_str:
            continue
        lower = entry.get("lower")
        upper = entry.get("upper")

        ts_dt: datetime | None = None
        with contextlib.suppress(ValueError, TypeError):
            ts_dt = datetime.fromisoformat(ts_str)

        target: float | None = None

        # Tier 1: TOU banking target.
        if tou_precondition_window is not None and ts_dt is not None:
            _window_start, _window_end, _tou_target, _tou_mode = tou_precondition_window
            if _window_start <= ts_dt < _window_end:
                target = _tou_target

        # Tier 2: nat-vent thermostatic cycling target.
        if (
            target is None
            and activity_fan_active_by_ts.get(ts_str)
            and lower is not None
            and upper is not None
            and ts_dt is not None
        ):
            in_sleep_window = _in_sleep_window(ts_dt, config)
            target = compute_nat_vent_target(
                sleep_heat=lower,
                in_sleep_window=in_sleep_window,
                comfort_heat_raw=lower,
                comfort_cool=upper,
                hysteresis=hysteresis,
            )

        # Tier 3: plain active comfort-band edge for this timestamp's effective mode.
        if target is None:
            _ts_hvac_mode = hvac_mode_by_ts.get(ts_str)
            if _ts_hvac_mode == "heat":
                target = lower
            elif _ts_hvac_mode == "cool":
                target = upper

        result.append({"ts": ts_str, "target": target})
    return result


def _parse_time(time_str: str) -> time:
    """Parse a time string like '06:30' into a time object."""
    try:
        parts = time_str.split(":")
        if len(parts) < 2:
            raise ValueError(f"Expected HH:MM format, got {time_str!r}")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, AttributeError):
        _LOGGER.warning(
            "Could not parse time %r — defaulting to 06:00",
            time_str,
        )
        return time(6, 0)
