"""Daily briefing generator for Climate Advisor.

Voice rules for the conversational body:
- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- Cause-and-effect: explain *why* before asking the user to do something
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times
- No emoji in body text (emoji only in the structured header and learning section)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from homeassistant.util import dt as dt_util

from . import i18n
from .automation import (
    ComfortBand,
    _in_sleep_window,
    awake_comfort_edges,
    compute_pre_cool_target,
    hold_after_text,
    hold_sentence,
    resolve_pre_cool_modifier,
    select_comfort_band,
)
from .classifier import DayClassification
from .const import (
    CONF_VACATION_COOL,
    CONF_VACATION_HEAT,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_DISABLED,
    OCCUPANCY_SETBACK_MINUTES,
    WINDOWS_NEAR_MISS_F,
)
from .nat_vent_plan import (
    close_timing_msg,
    compute_nat_vent_plan,
    cutoff_reason_msg,
    effective_window_close,
    ensure_catalogs,
    reopen_clause_msg,
    resolve_window_pair,
    resolve_with_fallback,
)
from .temperature import FAHRENHEIT, format_temp, free_cooling_direction_ok, from_fahrenheit
from .timeline_surfaces import TimelineWindows, operations_lines, timeline_windows

_LOGGER = logging.getLogger(__name__)

_DAY_TYPE_NAMES = (DAY_TYPE_HOT, DAY_TYPE_WARM, DAY_TYPE_MILD, DAY_TYPE_COOL, DAY_TYPE_COLD)


class BriefingText(str):
    """A briefing's English text that also carries the message it was rendered from (Issue #1341).

    It IS the English string (``_last_briefing`` consumers, the AI context and tests read it as before); ``msg`` lets
    the coordinator re-render the same briefing in the current language and persist it. A plain ``str`` has no
    ``msg`` (a legacy stored briefing, or a test stub) and shows as it is.
    """

    msg: i18n.Msg | None
    _by_language: dict[str, str]

    def __new__(cls, text: str, msg: i18n.Msg | None = None) -> BriefingText:
        obj = super().__new__(cls, text)
        obj.msg = msg
        obj._by_language = {}
        return obj

    def rendered(self, lang: str) -> str:
        """The briefing in ``lang`` (English and a message-less text are the stored string itself)."""
        lang = i18n.resolve_language(lang)
        if self.msg is None or lang == i18n.DEFAULT_LANGUAGE:
            return str(self)
        if lang not in self._by_language:
            ensure_catalogs()
            self._by_language[lang] = i18n.render(self.msg, lang)
        return self._by_language[lang]


def _clock(value: datetime | time) -> i18n.Time:
    """A ``Time`` param for a datetime, or for a bare time of day (placed on today's date, HA-local zone)."""
    if isinstance(value, datetime):
        return i18n.Time(value)
    ref = dt_util.now()
    return i18n.Time(datetime.combine(ref.date(), value, tzinfo=ref.tzinfo))


def _slots(key: str, **slots: Any) -> i18n.Msg:
    """A message whose named paragraph slots may be absent (``None``): each slot ``x`` is passed as itself (``""``
    when absent) plus a ``has_x`` yes/no flag the catalog message selects on, so the catalog (not code) decides
    where each whole paragraph goes and what separates it."""
    params: dict[str, Any] = {}
    for name, value in slots.items():
        params[name] = "" if value is None else value
        params[f"has_{name}"] = "no" if value is None else "yes"
    return i18n.Msg(key, **params)


@dataclass(frozen=True)
class _CloseFact:
    """The dated moment today's planned window period ends, resolved by ``effective_window_close()``.

    ``dynamic``: the forecast-derived cutoff is in play (dated today, or already behind us), so its reason may be
    stated. False means today's static configured close (a cutoff dated another day says nothing about today).
    ``already``: the close condition was already true when the plan was built -> the briefing says "now".
    """

    at: datetime
    dynamic: bool
    already: bool

    @property
    def msg(self) -> i18n.Msg:
        """The close time, or 'now' when the close condition was already reached, as one message."""
        return close_timing_msg(_clock(self.at), self.already)

    @property
    def text(self) -> str:
        """'11:00 AM', or 'now' when the close condition was already reached (English)."""
        return i18n.render_en(self.msg)


@dataclass(frozen=True)
class _BriefingFacts:
    """Everything the briefing says about the house, gathered ONCE at write time (Issue #1231, absorbs #1219).

    The briefing is written on demand or on the morning schedule from what is known at that moment, so every
    sentence and TLDR row reads from here -- never from its own re-derivation -- and a value that is unknown or
    not applied is simply absent (None) rather than guessed. ``unoccupied`` (away/vacation) means the engine
    holds the away/vacation setback all day and all night (``handle_bedtime`` defers to it), so nothing at-home
    -- no hold temperature, bedtime, pre-cool or window advice -- is promised.
    """

    occupancy_mode: str
    unoccupied: bool
    band: ComfortBand  # the band the thermostat is armed with right now (awake) for this occupancy
    awake_heat: float
    awake_cool: float
    normal_bedtime: float | None  # the edge the engine arms at bedtime at home (None: no setback / unoccupied)
    bedtime_target: float | None  # adaptive value when learned, else normal_bedtime (None when unoccupied)
    windows_advice: bool  # warm/mild: windows recommended today AND someone home
    near_miss: bool
    close: _CloseFact | None  # warm/mild/hot-morning planned close; None when no window advice applies
    evening_open: datetime | None  # warm/mild reopen, pair-validated against the dynamic close
    ceiling_breach: datetime | None  # warm: indoor forecast to reach the ceiling TODAY (another day -> None)
    hot_morning: bool
    hot_evening: bool
    hot_evening_open: time | None
    pre_cool_target: float | None  # warm-day "tonight I'll cool the home" target (None when unoccupied)
    hot_pre_cool_target: float | None  # hot-day overnight pre-cool target (None when unoccupied / not expected)
    economizer_threshold: float  # comfort_cool + ECONOMIZER_TEMP_DELTA: the outdoor temperature windows close at
    # Issue #1369: vacation band deeper than away on the edge that matters (heat day: floor, cool day: ceiling,
    # otherwise both edges). False when not on vacation. Separate vacation setpoints (#1347) may be equal/milder.
    vacation_deeper: bool = False
    # Issue #1230 R3b: the planned open still ahead today (warm/mild), so the Windows row names BOTH times;
    # and the live direction veto ("heat" or "warm") on a period that is running, so the row says "closed for now".
    open_at: datetime | None = None
    window_veto: str | None = None
    # Issue #1404: the time-of-use bank holding the house right now (``automation.CurrentHold`` with a bank), or None.
    current_hold: Any = None


def _local(moment: datetime, now: datetime) -> datetime:
    """``moment`` in ``now``'s timezone (both aware), unchanged otherwise."""
    if moment.tzinfo is not None and now.tzinfo is not None:
        return moment.astimezone(now.tzinfo)
    return moment


def _close_fact(cutoff: datetime | None, already: bool, static_close: time | None, now: datetime) -> _CloseFact | None:
    """Resolve a close time through the ONE shared helper (``effective_window_close()``)."""
    at = effective_window_close(cutoff, already, static_close, now)
    if at is None:
        return None
    dynamic = cutoff is not None and (already or at == cutoff)
    return _CloseFact(at=at, dynamic=dynamic, already=cutoff is not None and already)


def _gives_window_advice(c: DayClassification, occupancy_mode: str) -> bool:
    """Whether the briefing may talk about opening/closing windows today (Issue #1211).

    Only when windows are recommended for the day (outside air helps today) AND someone is home: an empty house
    (away/vacation) gets no window instructions even though the flag stays set for the automation.
    """
    # The briefing is the day's plan, so it follows the classifier's forecast-based answer. The coordinator's
    # live outdoor gate can clear ``windows_recommended`` for a few hours (a cold house on a cool morning) and
    # restore it later; the briefing text neither flaps with it nor depends on the moment it was rendered, and
    # the regeneration trigger keys on the same ``forecast_windows_recommended``. (The live direction is the
    # Next User Action card's job.) ``windows_recommended`` alone still counts, for callers that build a
    # classification by hand.
    recommended = bool(c.forecast_windows_recommended or c.windows_recommended)
    return recommended and occupancy_mode not in ("away", "vacation")


def _is_mild_near_miss(c: DayClassification, comfort_heat: float, *, unoccupied: bool, advice: bool) -> bool:
    """A mild day whose forecast high is just under the comfort floor (Issue #1211, owner decision O7).

    The forecast ran 5-10 F low on every recorded flip day, so instead of going silent the briefing says
    outside air may help later. Never on an estimated low, never away, never when windows are already advised.
    """
    return (
        c.day_type == DAY_TYPE_MILD
        and not advice
        and not unoccupied
        and not c.low_is_estimated
        and comfort_heat - WINDOWS_NEAR_MISS_F <= c.today_high < comfort_heat
    )


def _vacation_band_is_deeper(c: DayClassification, band: ComfortBand, config: dict) -> bool:
    """Issue #1369: whether the vacation band is deeper than away on the edge that matters today.

    A heat day arms only the floor and a cool day only the ceiling, so that edge decides; any other day arms both
    edges, so both must be deeper. Equal is not deeper.
    """
    floor_deeper = band.floor < float(config["setback_heat"])
    ceiling_deeper = band.ceiling > float(config["setback_cool"])
    if c.hvac_mode == "heat":
        return floor_deeper
    if c.hvac_mode == "cool":
        return ceiling_deeper
    return floor_deeper and ceiling_deeper


def _gather_briefing_facts(
    c: DayClassification,
    config: dict,
    *,
    occupancy_mode: str = "home",
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    warm_events: dict | None = None,
    mild_events: dict | None = None,
    hot_events: dict | None = None,
    runtime_config: dict | None = None,
    now: datetime | None = None,
    awake_edges: tuple[float, float] | None = None,
    windows: TimelineWindows | None = None,
) -> _BriefingFacts:
    """Gather the briefing's facts once (Issue #1231). ``config`` is the TLDR-shape dict built by
    ``generate_briefing()`` (comfort/setback/sleep_time/wake_time/aggressive_savings)."""
    now = now if now is not None else dt_util.now()
    unoccupied = occupancy_mode in ("away", "vacation")
    savings = bool(config.get("aggressive_savings", False))
    # The engine's band selector reads sleep_heat/sleep_cool from its config; carry them from the runtime config
    # when we have it (else it falls back to its own documented defaults, exactly as the engine would).
    band_cfg = dict(config)
    # Issue #1369: also the optional separate vacation setpoints (#1347), so the vacation band is the one
    # vacation_edges() resolves for the engine rather than always away -/+ 3.
    for key in ("sleep_heat", "sleep_cool", CONF_VACATION_HEAT, CONF_VACATION_COOL):
        if runtime_config and key in runtime_config:
            band_cfg[key] = runtime_config[key]
    band = select_comfort_band(
        c, band_cfg, occupancy_mode=occupancy_mode, in_sleep_window=False, aggressive_savings=savings
    )
    vacation_deeper = occupancy_mode == "vacation" and _vacation_band_is_deeper(c, band, config)
    awake_heat, awake_cool = (
        awake_edges if awake_edges is not None else awake_comfort_edges(config, aggressive_savings=savings)
    )

    # Bedtime: the edge handle_bedtime() arms at home is the sleep band's edge on this day's side. The adaptive
    # value (when learned) wins; unoccupied there is no at-home bedtime at all (the away setback is held).
    normal_bedtime: float | None = None
    bedtime_target: float | None = None
    if not unoccupied and c.hvac_mode in ("heat", "cool"):
        asleep = select_comfort_band(
            c, band_cfg, occupancy_mode="home", in_sleep_window=True, aggressive_savings=savings
        )
        normal_bedtime = asleep.floor if c.hvac_mode == "heat" else asleep.ceiling
        adaptive = bedtime_setback_heat if c.hvac_mode == "heat" else bedtime_setback_cool
        bedtime_target = adaptive if adaptive is not None else normal_bedtime

    windows_advice = _gives_window_advice(c, occupancy_mode)
    if windows is not None and not windows.periods and c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD):
        # Issue #1230 R3b: the operation timeline is the plan; when it holds no window period today (the direction
        # rule or bedtime removed it) the briefing does not talk about opening windows either.
        windows_advice = False
    near_miss = _is_mild_near_miss(c, config["comfort_heat"], unoccupied=unoccupied, advice=windows_advice)

    close: _CloseFact | None = None
    evening_open: datetime | None = None
    hot_morning = hot_evening = False
    hot_evening_open: time | None = None
    events = warm_events or mild_events
    # A breach dated another day says nothing about today, so the briefing says less (no "around 2:00 PM").
    _breach = events.get("ceiling_breach_time") if events else None
    ceiling_breach = _local(_breach, now) if _breach is not None else None
    if ceiling_breach is not None and ceiling_breach.date() != now.date():
        ceiling_breach = None
    if c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD) and windows_advice:
        cutoff = events.get("nat_vent_cutoff") if events else None
        # Never let the forecast cutoff produce a close at or before the open time: fall back to the static hour.
        if cutoff is not None and c.window_open_time and cutoff.time() <= c.window_open_time:
            cutoff = None
        already = bool(events and events.get("nat_vent_cutoff_already_reached"))
        close = _close_fact(cutoff, already, c.window_close_time, now)
        _open = events.get("evening_open_time") if events else None
        if close is not None and close.dynamic and cutoff is not None and _open is not None:
            _, ok = resolve_window_pair(cutoff, None, _open, None)
            evening_open = _local(_open, now) if ok is not None else None
    elif c.day_type == DAY_TYPE_HOT and not unoccupied:
        hot_morning = _window_event_available(hot_events, "nat_vent_cutoff", c.window_opportunity_morning)
        hot_evening = _window_event_available(hot_events, "evening_open_time", c.window_opportunity_evening)
        cutoff = hot_events.get("nat_vent_cutoff") if hot_events else None
        already = bool(hot_events and hot_events.get("nat_vent_cutoff_already_reached"))
        evening_dt = hot_events.get("evening_open_time") if hot_events else None
        if hot_morning:
            close = _close_fact(cutoff, already, c.window_opportunity_morning_end, now)
        if hot_morning and hot_evening:
            # Issue #878: close/open resolved TOGETHER so an open before its own close is never shown.
            _dyn_close = cutoff if close is not None and close.dynamic else None
            _, hot_evening_open = resolve_window_pair(
                _dyn_close, c.window_opportunity_morning_end, evening_dt, c.window_opportunity_evening_start
            )
            hot_evening = hot_evening_open is not None
        elif hot_evening:
            hot_evening_open = resolve_with_fallback(evening_dt, c.window_opportunity_evening_start)

    open_at: datetime | None = None
    window_veto = windows.vetoed if windows is not None and not unoccupied else None
    if windows is not None and not unoccupied:
        # Issue #1230 R3b: the window times the briefing states are the timeline's, never its own re-derivation.
        periods = [(o, e) for o, e in windows.periods if e > now]
        if c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD) and windows_advice:
            if periods:
                close = _CloseFact(at=_local(periods[0][1], now), dynamic=True, already=False)
                _o = periods[0][0]
                open_at = _local(_o, now) if _o is not None and _local(_o, now) > now else None
                later = [o for o, _e in periods[1:] if o is not None]
                evening_open = _local(later[0], now) if later else None
            else:
                close = None
                evening_open = None
        elif c.day_type == DAY_TYPE_HOT:
            morning = [p for p in periods if p[0] is None or p[0].hour < 12]
            evening = [p for p in periods if p[0] is not None and p[0].hour >= 15]
            hot_morning = bool(morning)
            hot_evening = bool(evening)
            close = _CloseFact(at=_local(morning[0][1], now), dynamic=True, already=False) if morning else None
            hot_evening_open = _local(evening[0][0], now).time() if evening and evening[0][0] is not None else None
            hot_evening = hot_evening_open is not None

    # Overnight pre-cool: only when it is actually expected to run tonight and someone is home to be promised it.
    pre_cool_target = None if unoccupied else bedtime_setback_cool
    hot_pre_cool_target: float | None = None
    if c.day_type == DAY_TYPE_HOT and not unoccupied and runtime_config is not None:
        _modifier = resolve_pre_cool_modifier(c, runtime_config)
        if _modifier is not None:
            hot_pre_cool_target = compute_pre_cool_target(runtime_config, _modifier)

    return _BriefingFacts(
        occupancy_mode=occupancy_mode,
        unoccupied=unoccupied,
        band=band,
        awake_heat=awake_heat,
        awake_cool=awake_cool,
        normal_bedtime=normal_bedtime,
        bedtime_target=bedtime_target,
        windows_advice=windows_advice,
        near_miss=near_miss,
        close=close,
        evening_open=evening_open,
        ceiling_breach=ceiling_breach,
        hot_morning=hot_morning,
        hot_evening=hot_evening,
        hot_evening_open=hot_evening_open,
        pre_cool_target=pre_cool_target,
        hot_pre_cool_target=hot_pre_cool_target,
        economizer_threshold=config["comfort_cool"] + ECONOMIZER_TEMP_DELTA,
        vacation_deeper=vacation_deeper,
        open_at=open_at,
        window_veto=window_veto,
    )


def generate_briefing_msg(
    classification: DayClassification,
    comfort_heat: float,
    comfort_cool: float,
    setback_heat: float,
    setback_cool: float,
    wake_time: time,
    sleep_time: time,
    learning_suggestions: list[str] | None = None,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
    verbosity: str = "normal",
    fan_mode: str = FAN_MODE_DISABLED,
    occupancy_mode: str = "home",
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    adaptive_thermal_active: bool = False,
    predicted_indoor_future: list[dict] | None = None,
    predicted_outdoor_future: list[dict] | None = None,
    runtime_config: dict | None = None,
    nat_vent_plan: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = False,
    automation_overridden: bool = False,
    now: datetime | None = None,
    timeline: Any | None = None,
    current_hold: Any | None = None,
) -> i18n.Msg:
    """Build the daily climate briefing as ONE message (``briefing.full.<day type>``; ``briefing.tldr`` for
    ``verbosity="tldr_only"``). ``generate_briefing()`` renders it.

    Issue #1341: every section is a whole message placed by the catalog's top-level message, so a translator can
    reorder sections and nothing joins rendered sentences in code.

    ``current_hold`` (Issue #1404): the coordinator's ``current_hold()``. While a time-of-use bank owns the
    thermostat the HVAC Mode row quotes the bank target and when the schedule band returns, and Today's Steps opens
    with the same sentence Status shows; otherwise (None, or no bank) the briefing is unchanged.

    ``timeline`` (Issue #1230 R3b): the zone's operation timeline. When given, the briefing's window times are the
    timeline's window steps and a "Today's steps" section lists the timeline's step sentences; without it (direct
    calls, tests, a degraded coordinator) the briefing computes its facts as before.

    Args:
        classification: Today's day classification and recommendations.
        comfort_heat / comfort_cool: User's comfort setpoints.
        setback_heat / setback_cool: User's setback setpoints.
        wake_time / sleep_time: User's schedule.
        learning_suggestions: Any pending suggestions from the learning system.
        debounce_seconds: How long a door/window must be open before HVAC pauses.
        manual_grace_seconds: Grace period after a manual HVAC override.
        automation_grace_seconds: Grace period after Climate Advisor resumes HVAC.
        grace_active: Whether a grace period is currently active.
        grace_source: "manual" or "automation" if a grace period is active.
        verbosity: "tldr_only" (header + table only), "normal" (header + table + trimmed
            body), or "verbose" (header + table + full original body).
        fan_mode: Fan control mode — one of the FAN_MODE_* constants.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
        runtime_config: Full runtime config dict, used only to compute whether tonight's overnight
            pre-cool (``resolve_pre_cool_modifier()``) is expected to run, for the hot-day plan's
            narrative (Issue #558). If omitted, that narrative line is left out rather than
            guessed. Named distinctly from the local ``config`` dict built below (TLDR-table
            shape) to avoid shadowing it.
        nat_vent_plan: Issue #817 — the coordinator's single per-cycle
            ``nat_vent_plan.compute_nat_vent_plan()`` result
            (``self._nat_vent_plan``), when the caller already has one. When given, this
            is used directly instead of deriving a fresh one from
            ``predicted_indoor_future``/``predicted_outdoor_future`` — the same result the
            "Next Automation"/"Next User Action" status cards read this cycle, so the
            briefing can never disagree with them. When omitted (e.g. direct/standalone
            calls, tests), behavior is unchanged: derived locally from the prediction
            curves below.
        current_indoor_temp / current_outdoor_temp: Issue #847/#430 — live readings used
            only as a sanity check before the WARM/MILD-day plan text asserts a
            ``comfort_floor`` reason for the nat-vent close time: if outdoor has already
            risen above indoor by render time (the ``comfort_floor`` prediction's
            underlying condition no longer holds against live data), the text falls back
            to ``outdoor_rise`` phrasing instead of asserting a stale claim. Omitted
            (None), the sanity check cannot rule anything out and the reason is trusted
            as computed (unchanged prior behavior).
        overnight_heat_engaged: Issue #948 (Root Cause 1) — real runtime signal
            (``coordinator.get_hvac_runtime_today() > 0``) gating the MILD-day "I
            warmed to X before sunrise" claim and the WARM-day "HVAC is off this
            morning." claim, so neither is asserted unless heat actually ran
            overnight. Threaded identically into both ``_mild_day_plan()`` and
            ``_warm_day_plan()``. Default False (assume no evidence of overnight
            heat — the "don't assert what you can't verify" thesis this whole fix
            is built on) for callers that don't pass it; at this default,
            ``_mild_day_plan()`` renders the neutral wording (not "I warmed to X").
            Issue #1211: the WARM-day "HVAC is off this morning." sentence no longer
            exists (a comfort band is armed on a warm day); with this flag False and no
            window advice, ``_warm_day_plan()`` says the thermostat holds the band (and,
            only on a real forecast low, that outside air won't do much).
        automation_overridden: Issue #948 (Root Cause 1, third site) — whether
            automation is disabled, manually overridden, or paused (the same
            signal ``_compute_automation_status()`` reads). Gates
            ``_leaving_home_section()``'s away+cool/away+heat "I've applied
            setback..."/"I've dropped to X..." claims so they aren't asserted
            when the setback was never actually commanded. Default False for
            callers that don't pass it.

    Returns:
        The briefing message; ``i18n.render(msg, lang)`` is the text for email or notification.
    """
    ensure_catalogs()
    c = classification

    _LOGGER.debug(
        "Generating briefing — day_type=%s, trend=%s, comfort_heat=%.0f\u00b0F, comfort_cool=%.0f\u00b0F, verbosity=%s",
        c.day_type,
        c.trend_direction,
        comfort_heat,
        comfort_cool,
        verbosity,
    )

    # TLDR table — used standalone for push notifications, embedded in full briefing
    # Issue #1091: Prefer savings widens the awake home band the thermostat is armed with;
    # derive the flag once and the awake edges once so every display site quotes the same band.
    _savings = bool((runtime_config or {}).get("aggressive_savings", False))
    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": setback_heat,
        "setback_cool": setback_cool,
        "sleep_time": sleep_time,
        "wake_time": wake_time,
        "aggressive_savings": _savings,
    }
    awake_heat, awake_cool = awake_comfort_edges(config, aggressive_savings=_savings)
    # Issue #535: comfort-floor awareness for the nat-vent cutoff prediction — the real
    # activation gate (decide_nat_vent_gate()) requires indoor > comfort_heat as one of
    # its four conditions, but this predictive curve scan historically only modeled the
    # outdoor-vs-indoor half. When runtime_config is available, resolve comfort_heat_raw/
    # sleep_heat from it and pass an in_sleep_window_fn so compute_nat_vent_plan() can
    # also scan for a floor crossing. Omitted (None) when runtime_config isn't provided —
    # callers relying on that (e.g. tests that call generate_briefing() directly without
    # it) keep today's outdoor-only behavior rather than guessing at missing config.
    _comfort_heat_raw = float(runtime_config.get("comfort_heat", comfort_heat)) if runtime_config else None
    _sleep_heat = float(runtime_config.get("sleep_heat", comfort_heat)) if runtime_config else None
    _in_sleep_window_fn: Callable[[datetime], bool] | None = (
        (lambda ts: _in_sleep_window(ts, runtime_config)) if runtime_config else None
    )

    # Single source of truth for warm/mild-day window/AC timing (Issue #518, extended
    # in #817): when the caller already computed this cycle's nat_vent_plan (the same
    # value the status cards read), use it directly rather than deriving a second copy
    # here — this is what makes the header table and conversational body, plus every
    # other consumer, incapable of disagreeing about when windows close or whether AC
    # is expected. Falls back to a local derivation only when no plan was supplied
    # (direct/standalone calls, tests that pass raw prediction curves instead).
    if nat_vent_plan is not None:
        warm_events = nat_vent_plan if c.day_type == DAY_TYPE_WARM else None
        mild_events = nat_vent_plan if c.day_type == DAY_TYPE_MILD else None
        hot_events = nat_vent_plan if c.day_type == DAY_TYPE_HOT else None
    else:
        warm_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                comfort_heat_raw=_comfort_heat_raw,
                sleep_heat=_sleep_heat,
                in_sleep_window_fn=_in_sleep_window_fn,
                window_open_time=c.window_open_time,
            )
            if c.day_type == DAY_TYPE_WARM and predicted_indoor_future and predicted_outdoor_future
            else None
        )
        # Issue #534: MILD-day window close time was documented
        # (docs/08-COMPUTATION-REFERENCE.md §6d) as ODE-dynamic but never actually wired
        # up — _mild_day_plan() always used the static classifier hour. Uses
        # compute_nat_vent_plan() here too (the dead `_derive_natural_vent_events()`
        # sibling — built for a list[float] hour-indexed curve shape that
        # _build_predicted_indoor_future() has never actually produced — was removed in
        # #535).
        mild_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                comfort_heat_raw=_comfort_heat_raw,
                sleep_heat=_sleep_heat,
                in_sleep_window_fn=_in_sleep_window_fn,
                window_open_time=c.window_open_time,
            )
            if c.day_type == DAY_TYPE_MILD and predicted_indoor_future and predicted_outdoor_future
            else None
        )
        # Issue #876: HOT day's morning-opportunity close/evening-reopen times had no
        # dynamic path at all (unlike WARM/MILD above) — always the raw
        # window_opportunity_morning_end/evening_start hour constants, with no
        # connection to the actual forecast. Bounded by window_opportunity_morning_start
        # (HOT's own open-time field, distinct from WARM/MILD's window_open_time —
        # see nat_vent_plan.py's compute_nat_vent_plan() window_open_time param, which
        # is a generic "don't scan before this hour" bound regardless of which field
        # supplies it). comfort_heat_raw/sleep_heat/in_sleep_window_fn are intentionally
        # omitted — HOT's hvac_mode is "cool", so the comfort-floor scan (a heating-mode
        # concept) doesn't apply.
        hot_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                window_open_time=c.window_opportunity_morning_start,
            )
            if c.day_type == DAY_TYPE_HOT and predicted_indoor_future and predicted_outdoor_future
            else None
        )

    # Issue #1231: every fact the briefing states, gathered once here (the TLDR header's comfort band included).
    facts = _gather_briefing_facts(
        c,
        config,
        occupancy_mode=occupancy_mode,
        bedtime_setback_heat=bedtime_setback_heat,
        bedtime_setback_cool=bedtime_setback_cool,
        warm_events=warm_events,
        mild_events=mild_events,
        hot_events=hot_events,
        runtime_config=runtime_config,
        now=now,
        awake_edges=(awake_heat, awake_cool),
        windows=timeline_windows(timeline, (now or dt_util.now()).date()) if timeline is not None else None,
    )
    if current_hold is not None and hasattr(current_hold, "band"):
        facts = replace(facts, current_hold=current_hold)
    tldr = _tldr_msg(c, config, temp_unit=temp_unit, facts=facts)

    if verbosity == "tldr_only":
        _LOGGER.debug("Briefing generated (tldr_only)")
        return tldr

    # Structured header (kept for full briefing / email)
    # Note: Today/Tomorrow temps and Day Type are already in the TLDR table,
    # so the header is only the title and separator, to avoid duplication (Issue #52).

    # Conversational body
    body: i18n.Msg | None = None
    if c.day_type == DAY_TYPE_HOT:
        body = _hot_day_msg(c, facts, fan_mode=fan_mode, temp_unit=temp_unit)
    elif c.day_type == DAY_TYPE_WARM:
        body = _warm_day_msg(
            c,
            facts,
            fan_mode=fan_mode,
            temp_unit=temp_unit,
            warm_events=warm_events,
            current_indoor_temp=current_indoor_temp,
            current_outdoor_temp=current_outdoor_temp,
            overnight_heat_engaged=overnight_heat_engaged,
        )
    elif c.day_type == DAY_TYPE_MILD:
        body = _mild_day_msg(
            c,
            facts,
            temp_unit=temp_unit,
            mild_events=mild_events,
            current_indoor_temp=current_indoor_temp,
            current_outdoor_temp=current_outdoor_temp,
            overnight_heat_engaged=overnight_heat_engaged,
        )
    elif c.day_type == DAY_TYPE_COOL:
        body = _cool_day_msg(c, facts, temp_unit=temp_unit)
    elif c.day_type == DAY_TYPE_COLD:
        body = _cold_day_msg(c, facts, temp_unit=temp_unit)

    _LOGGER.debug("Dispatched %s day plan", c.day_type)

    leaving = _leaving_home_msg(
        c,
        setback_heat,
        setback_cool,
        occupancy_mode=occupancy_mode,
        temp_unit=temp_unit,
        automation_overridden=automation_overridden,
        facts=facts,
    )
    fresh_air = _fresh_air_msg(c, facts, debounce_seconds, temp_unit=temp_unit)

    # Grace period status — only shown when a grace period is currently active,
    # or when grace periods are configured to a non-default value worth explaining
    grace = _grace_period_msg(
        debounce_seconds=debounce_seconds,
        manual_grace_seconds=manual_grace_seconds,
        automation_grace_seconds=automation_grace_seconds,
        grace_active=grace_active,
        grace_source=grace_source,
    )
    if grace is not None:
        _LOGGER.debug("Grace section included — source=%s", grace_source)

    steps: i18n.Msg | None = None
    _bank_line = hold_sentence(facts.current_hold, temp_unit) if facts.current_hold is not None else None
    if timeline is not None or _bank_line is not None:
        ops = operations_lines(timeline, now or dt_util.now()) if timeline is not None else []
        now_msg = i18n.Msg("briefing.ops_now", line=_bank_line) if _bank_line is not None else None
        if now_msg is not None or ops:
            # The step lines are the operation timeline's own sentences (already one line each).
            steps = _slots("briefing.steps", now=now_msg, ops="\n".join(ops) if ops else None)

    tonight = _tonight_msg(c, facts, temp_unit=temp_unit, adaptive_thermal_active=adaptive_thermal_active)

    # Learning suggestions (kept structured for accept/dismiss clarity): the learner's own sentences as bullets.
    suggestions = None
    if learning_suggestions:
        suggestions = i18n.Msg("briefing.suggestions", items="\n".join(f"  \u2022 {s}" for s in learning_suggestions))

    key = f"briefing.full.{c.day_type}" if c.day_type in _DAY_TYPE_NAMES else "briefing.full.other"
    msg = _slots(
        key,
        title=i18n.Msg("briefing.title"),
        tldr=tldr,
        body=body,
        leaving=leaving,
        fresh_air=fresh_air,
        grace=grace,
        steps=steps,
        tonight=tonight,
        suggestions=suggestions,
    )
    _LOGGER.debug(
        "Briefing generated — %s, %d learning suggestions",
        key,
        len(learning_suggestions) if learning_suggestions else 0,
    )
    return msg


def generate_briefing(*args: Any, lang: str = "en", **kwargs: Any) -> BriefingText:
    """Generate the daily climate briefing text (English unless ``lang``); the arguments are those of
    ``generate_briefing_msg()``. The result is a ``str`` that also carries its message (``.msg``) so the caller can
    persist it and re-render it in another language."""
    msg = generate_briefing_msg(*args, **kwargs)
    return BriefingText(i18n.render(msg, lang), msg)


def _window_event_available(events: dict | None, key: str, opportunity_flag: bool) -> bool:
    """Single shared check (Issue #878) for "is there a window event to show here" —
    used identically by both Hot render call sites (_generate_tldr_table() and
    _hot_day_plan()) so the eligibility-override rule can't be written twice and
    drift apart, which is exactly the duplication class this feature keeps failing on.

    A real ODE-computed crossing (``events[key] is not None``) always wins. The
    coarse today/tomorrow-low ``opportunity_flag`` heuristic only matters as a
    fallback signal when no forecast curve exists yet — it has no relationship to
    when an actual computed crossing falls, so it must never override real data.
    """
    return (events is not None and events.get(key) is not None) or opportunity_flag


def _says_air_wont_help(c: DayClassification) -> bool:
    """Whether the briefing may SAY outside air will not help today (Issue #1211).

    Only when the classifier decided so on a real forecast low: not recommended by the forecast answer nor the
    live flag, and the low is not a stand-in. When the low is estimated the answer is unknown, so the text
    asserts neither that air helps nor that it does not. Away/vacation never reaches this (no window advice there
    says nothing about the air at all).
    """
    return not c.forecast_windows_recommended and not c.windows_recommended and not c.low_is_estimated


def _band_msg(floor: float, ceiling: float, temp_unit: str) -> i18n.Msg:
    """The armed comfort band as a message ('68–74°F'): the floor is the bare number, the ceiling carries the unit."""
    return i18n.Msg(
        "briefing.band",
        lo=int(f"{from_fahrenheit(floor, temp_unit):.0f}"),
        hi=i18n.Temp(ceiling, temp_unit),
    )


def _band_text(floor: float, ceiling: float, temp_unit: str) -> str:
    """'68-74°F' -- the armed comfort band, quoted from the band the thermostat actually holds (English)."""
    return i18n.render_en(_band_msg(floor, ceiling, temp_unit))


def _tldr_msg(
    c: DayClassification,
    config: dict,
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    occupancy_mode: str = "home",
    warm_events: dict | None = None,
    mild_events: dict | None = None,
    hot_events: dict | None = None,
    facts: _BriefingFacts | None = None,
    now: datetime | None = None,
) -> i18n.Msg:
    """The TLDR summary table as ONE message (``briefing.tldr``); each row is its own whole message.

    Args:
        c: Today's day classification.
        config: Dict with comfort_heat, comfort_cool, setback_heat, setback_cool,
            sleep_time, wake_time keys.
        temp_unit: Display unit — "fahrenheit" or "celsius".
        facts: Issue #1231 — the briefing facts gathered once by ``generate_briefing()`` (every row reads
            from them, so the header can never disagree with the body). When omitted (direct calls, tests)
            they are gathered here from the remaining arguments, through the same function.
        bedtime_setback_heat / bedtime_setback_cool, occupancy_mode, warm_events / mild_events / hot_events,
        now: inputs to that gather; ignored when ``facts`` is given. The events are the result of
            compute_nat_vent_plan(), pre-computed once in generate_briefing() (Issue #518) so the header's
            window-close time always agrees with the conversational body.
    """
    if facts is None:
        facts = _gather_briefing_facts(
            c,
            config,
            occupancy_mode=occupancy_mode,
            bedtime_setback_heat=bedtime_setback_heat,
            bedtime_setback_cool=bedtime_setback_cool,
            warm_events=warm_events,
            mild_events=mild_events,
            hot_events=hot_events,
            now=now,
        )
    sleep_time = config["sleep_time"]
    occupancy_mode = facts.occupancy_mode

    # --- Day Type row ---
    day_type_row = i18n.Msg(
        "briefing.tldr.day_type", day_type=_day_type_msg(c.day_type), high=i18n.Temp(c.today_high, temp_unit)
    )

    # --- HVAC Mode row (Issue #85: show setback temps when away/vacation) ---
    # Issue #1088: print the edge the thermostat is actually armed with -- vacation's extra setback
    # included -- by reading the engine's band selector (resolved once into the facts), not the raw config keys.
    # A morning snapshot: the TOU away/vacation widening is not threaded in. Issue #1091: the band reflects the
    # real Prefer savings flag, so the row quotes the awake band the thermostat is armed with.
    _band = facts.band
    _hold = facts.current_hold
    _bank = getattr(_hold, "bank", None)
    # Issue #1404 (mirrors): with the live hold, an empty house quotes the band in force (the Away/Vacation band
    # widened for a high-cost period: the thermostat is told cool 83, not 80), and an occupied, awake home quotes the
    # family the thermostat is armed in (after a cool bank: "Heat at 68", not "Cool at 76").
    _day_mode = c.hvac_mode
    if _hold is not None and _bank is None:
        if facts.unoccupied:
            _band = _hold.band
        elif c.hvac_mode in ("heat", "cool") and getattr(_hold, "family", None) in ("heat", "cool"):
            _day_mode = _hold.family
    if _bank is not None:
        # Issue #1404: a time-of-use bank owns the thermostat: the row quotes what it is told and what follows.
        hvac_row = i18n.Msg(
            "briefing.tldr.hvac_bank_cool" if _bank.mode == "cool" else "briefing.tldr.hvac_bank_heat",
            target=i18n.Temp(_bank.target, temp_unit),
            until=_clock(dt_util.as_local(_bank.ends)),
            after=hold_after_text(facts.current_hold, temp_unit),
        )
    elif facts.unoccupied:
        if _day_mode == "cool":
            hvac_row = i18n.Msg("briefing.tldr.hvac_cool", target=i18n.Temp(_band.ceiling, temp_unit))
        elif _day_mode == "heat":
            hvac_row = i18n.Msg("briefing.tldr.hvac_heat", target=i18n.Temp(_band.floor, temp_unit))
        else:
            # Issue #1253 (mirror of #1211): hvac_mode "off" on a warm/mild day is NOT "nothing runs" -- the
            # away/vacation band is armed (heat below its floor, cool above its ceiling). Name the held band.
            hvac_row = i18n.Msg(
                "briefing.tldr.hvac_vacation_band" if occupancy_mode == "vacation" else "briefing.tldr.hvac_away_band",
                band=_band_msg(_band.floor, _band.ceiling, temp_unit),
            )
    elif _day_mode == "cool":
        hvac_row = i18n.Msg("briefing.tldr.hvac_cool", target=i18n.Temp(_band.ceiling, temp_unit))
    elif _day_mode == "heat":
        hvac_row = i18n.Msg("briefing.tldr.hvac_heat", target=i18n.Temp(_band.floor, temp_unit))
    else:
        # Issue #1211: hvac_mode "off" on a warm/mild day is NOT "off": the comfort band is armed (heat below
        # the floor, cool above the ceiling). Say what is held, and "(windows day)" only when it is true.
        hvac_row = i18n.Msg(
            "briefing.tldr.hvac_comfort_band_windows" if facts.windows_advice else "briefing.tldr.hvac_comfort_band",
            band=_band_msg(_band.floor, _band.ceiling, temp_unit),
        )

    # --- Windows row ---
    # Issue #1231: every close time is the dated one from the facts (``effective_window_close()``), so a cutoff
    # dated another day is never shown as today's time; an empty house gets no window advice at all.
    threshold = i18n.Temp(facts.economizer_threshold, temp_unit)
    if c.day_type != DAY_TYPE_HOT and facts.windows_advice and c.window_open_time and facts.close is not None:
        # Issue #876: the morning OPEN time is intentionally dropped from this row -- it carries no useful
        # information -- so the row shows only the close time, matching the conversational body.
        if facts.open_at is not None:
            windows_row = i18n.Msg("briefing.tldr.windows_open_close", open=_clock(facts.open_at), when=facts.close.msg)
        else:
            windows_row = i18n.Msg("briefing.tldr.windows_close_by", when=facts.close.msg)
    elif facts.hot_morning and facts.hot_evening and facts.close is not None:
        # Issue #878: close/open resolved TOGETHER (see resolve_window_pair()) -- the incident (Close by 6:00 PM /
        # Open 5:00 PM+) is prevented when the facts were gathered.
        windows_row = i18n.Msg(
            "briefing.tldr.windows_hot_both",
            when=facts.close.msg,
            threshold=threshold,
            open=_clock(facts.hot_evening_open),
        )
    elif facts.hot_morning and facts.close is not None:
        windows_row = i18n.Msg("briefing.tldr.windows_hot_morning", when=facts.close.msg, threshold=threshold)
    elif facts.hot_evening and facts.hot_evening_open is not None:
        windows_row = i18n.Msg("briefing.tldr.windows_hot_evening", open=_clock(facts.hot_evening_open))
    elif facts.window_veto == "heat":
        windows_row = i18n.Msg("briefing.tldr.windows_veto_heat")
    elif facts.window_veto == "warm":
        windows_row = i18n.Msg("briefing.tldr.windows_veto_warm")
    else:
        windows_row = i18n.Msg("briefing.tldr.windows_closed")

    # --- Bedtime Setback row ---
    # Issue #1231: one bedtime number (adaptive when learned, else the normal bedtime setpoint) shared with the
    # body; while away/vacation the engine holds the away setback all night, so that is what the row says.
    if facts.unoccupied:
        bedtime_row = i18n.Msg("briefing.tldr.bedtime_away")
    elif facts.bedtime_target is not None:
        bedtime_row = i18n.Msg(
            "briefing.tldr.bedtime_target", target=i18n.Temp(facts.bedtime_target, temp_unit), time=_clock(sleep_time)
        )
    else:
        bedtime_row = i18n.Msg("briefing.tldr.bedtime_none")

    # --- Tomorrow row ---
    tomorrow_row = i18n.Msg(
        "briefing.tldr.tomorrow",
        trend=_trend_msg(c, temp_unit=temp_unit),
        high=i18n.Temp(c.tomorrow_high, temp_unit),
    )

    # Issue #85: show occupancy status when not home
    occupancy_row: i18n.Msg | None = None
    if occupancy_mode == "away":
        occupancy_row = i18n.Msg("briefing.tldr.occupancy_away")
    elif occupancy_mode == "vacation":
        # Issue #1369: "deep" only when the vacation band is deeper than away on the edge that matters.
        occupancy_row = i18n.Msg(
            "briefing.tldr.occupancy_vacation_deep" if facts.vacation_deeper else "briefing.tldr.occupancy_vacation"
        )
    elif occupancy_mode == "guest":
        occupancy_row = i18n.Msg("briefing.tldr.occupancy_guest")
    return _slots(
        "briefing.tldr",
        day_type=day_type_row,
        hvac=hvac_row,
        occupancy=occupancy_row,
        windows=windows_row,
        bedtime=bedtime_row,
        tomorrow=tomorrow_row,
    )


def _generate_tldr_table(c: DayClassification, config: dict, *args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """The TLDR summary as a list of rendered rows (English unless ``lang``); see ``_tldr_msg`` for the arguments."""
    ensure_catalogs()
    return i18n.render(_tldr_msg(c, config, *args, **kwargs), lang).split("\n")


def _day_type_msg(day_type: str) -> i18n.Msg | str:
    """The day type's name as a message; an unknown type falls back to its title-cased code."""
    if day_type in _DAY_TYPE_NAMES:
        return i18n.Msg(f"briefing.day_type.{day_type}")
    return day_type.title()


def _trend_msg(c: DayClassification, temp_unit: str = FAHRENHEIT) -> i18n.Msg:
    """Trend description as one message."""
    delta = i18n.TempDelta(c.trend_magnitude, temp_unit)
    if c.trend_direction == "warming":
        if c.trend_magnitude >= 10:
            return i18n.Msg("briefing.trend.warming_big", delta=delta)
        return i18n.Msg("briefing.trend.warming", delta=delta)
    if c.trend_direction == "cooling":
        if c.trend_magnitude >= 10:
            return i18n.Msg("briefing.trend.cooling_big", delta=delta)
        return i18n.Msg("briefing.trend.cooling", delta=delta)
    return i18n.Msg("briefing.trend.stable")


def _trend_description(c: DayClassification, temp_unit: str = FAHRENHEIT) -> str:
    """Human-readable trend description (English)."""
    return i18n.render_en(_trend_msg(c, temp_unit))


def _hot_day_msg(
    c,
    facts: _BriefingFacts,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
) -> i18n.Msg:
    """Conversational plan for hot days (85°F+).

    Issue #1231: every value comes from ``facts``. An empty house (away/vacation) gets the lead line only --
    the engine holds the away setback all day and night, so no hold temperature, pre-cool, window or blind
    advice is promised.

    Issue #876: morning-close and evening-open times are derived from the same ODE-crossing mechanism
    WARM/MILD use (nat_vent_plan.compute_nat_vent_plan()), falling back to the static classifier hour when no
    forecast curve is available yet (resolved in ``_gather_briefing_facts()`` through
    ``effective_window_close()``); the morning OPEN time is intentionally dropped from the displayed text.

    Issue #878: has_morning/has_evening reflect whether a REAL crossing was computed, not just the coarse
    today/tomorrow-low eligibility heuristic, and close/open are resolved TOGETHER via resolve_window_pair() so
    a pairing like "Close by 6:00 PM / Open 5:00 PM+" can never render.

    Issue #1341: the plan is ONE message (``briefing.hot.plan``) whose paragraph slots (opener, close, blinds,
    reopen, fan) are whole-paragraph messages; an absent paragraph is simply not placed.
    """
    if facts.unoccupied:
        return i18n.Msg("briefing.hot.away")

    threshold = i18n.Temp(facts.economizer_threshold, temp_unit)
    # Issue #1091: display-only armed ceiling (Prefer savings widens it); thresholds stay on raw comfort_cool.
    hold = i18n.Temp(facts.awake_cool, temp_unit)
    close = facts.close
    has_morning = facts.hot_morning and close is not None
    has_evening = facts.hot_evening and facts.hot_evening_open is not None

    # Issue #558: only claim overnight pre-cool banking when it's actually expected to run tonight
    # (resolve_pre_cool_modifier() -- the same gate handle_pre_cool() uses), and phrase it prospectively.
    if facts.hot_pre_cool_target is not None:
        opener = i18n.Msg("briefing.hot.opener_precool", target=i18n.Temp(facts.hot_pre_cool_target, temp_unit))
    else:
        opener = i18n.Msg("briefing.hot.opener_hold", high=i18n.Temp(c.today_high, temp_unit), hold=hold)

    close_p = blinds_p = reopen_p = None
    if has_morning:
        # Issue #878-followup: "already reached" is only meaningful when the dynamic cutoff is in play -- a
        # static fallback hour (including a cutoff dated another day) is a plain configured hour.
        if close.already:
            close_p = i18n.Msg("briefing.hot.close_already", threshold=threshold)
        else:
            close_p = i18n.Msg("briefing.hot.close_at", threshold=threshold, time=_clock(close.at))
        blinds_p = i18n.Msg("briefing.hot.blinds_after_close", hold=hold, evening="yes" if has_evening else "no")
        if has_evening:
            reopen_p = reopen_clause_msg(_clock(facts.hot_evening_open), reopening_from_closed_all_day=False)
    elif has_evening:
        blinds_p = i18n.Msg("briefing.hot.sealed_evening", hold=hold)
        reopen_p = reopen_clause_msg(_clock(facts.hot_evening_open), reopening_from_closed_all_day=True)
    else:
        blinds_p = i18n.Msg("briefing.hot.sealed_rest", hold=hold)

    fan_p = i18n.Msg("briefing.hot.fan") if fan_mode != FAN_MODE_DISABLED and (has_morning or has_evening) else None
    return _slots("briefing.hot.plan", opener=opener, close=close_p, blinds=blinds_p, reopen=reopen_p, fan=fan_p)


def _warm_day_msg(
    c,
    facts: _BriefingFacts,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
    warm_events: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = False,
) -> i18n.Msg:
    """Conversational plan for warm days (75-85°F).

    Issue #518: window/AC timing is derived once in generate_briefing() and passed
    in as `warm_events` so this never disagrees with the header table — this is now
    the only source (Issue #817 removed the local-recompute fallback, which no
    caller, production or test, ever actually reached).

    Issue #847: the reason → phrase mapping now flows through the shared
    ``cutoff_reason_msg()`` helper (``nat_vent_plan.py``) instead of an inline branch, so this can never phrase
    ``nat_vent_cutoff_reason`` differently than the "Next Automation" status card (``coordinator.py``'s
    ``_compute_next_automation_action()``), which calls the same helper. Also closes
    #430 (filed alongside #428/#535 and never fixed): before asserting a
    ``comfort_floor`` reason in text, a live outdoor-vs-indoor sanity check
    (``free_cooling_direction_ok()``, the same #428 guard) confirms the reason still
    holds — if outdoor has already risen above indoor by render time, the sentence
    falls back to ``outdoor_rise`` phrasing instead of asserting a stale claim.

    Issue #948 (Root Cause 1; the sentence itself was removed by Issue #1211, which keeps this gate on the
    "heater ran overnight" alternative): ``overnight_heat_engaged`` (real runtime signal) gates the claim that heat
    ran overnight. Default False preserves prior behavior for callers that don't pass the signal.

    Issue #1211: every window sentence (the tip, the fan-ventilation line, "close up by", "reopen") is gated
    on ``windows_advice`` -- windows recommended today AND someone home (default: ``c.windows_recommended``).
    Otherwise the plan says what is true: the thermostat holds the comfort band. ``away`` (away/vacation) gets a
    neutral line with no claim about the air and no awake band; "outside air won't do much" is said only when
    the classifier decided so on a real forecast low (``_says_air_wont_help()``), and an estimated low asserts
    neither way.

    Issue #1341: ONE message (``briefing.warm.plan``) with whole-paragraph slots (main, fan, ac, precool, reopen).
    """
    windows_advice = facts.windows_advice
    away = facts.unoccupied
    # Issue #1211: the band the thermostat actually holds today, quoted from the armed edges.
    _held_cool = facts.awake_cool
    band = _band_msg(facts.awake_heat, _held_cool, temp_unit)
    # Issue #1093: the AC-handoff sentences quote the ceiling the thermostat is armed with (Prefer
    # savings widens it), read from awake_comfort_edges() -- never comfort_cool + margin here.
    hold = i18n.Temp(_held_cool, temp_unit)

    _events = warm_events
    # Issue #1231: the close is the dated one from the facts. A forecast cutoff dated another day is the static
    # path (no dynamic reason / already-reached phrasing); a cutoff in play carries its reason.
    _close = facts.close
    _dynamic = _close is not None and _close.dynamic
    _nat_vent_cutoff_reason = _events.get("nat_vent_cutoff_reason") if _events and _dynamic else None
    _ceiling_breach = facts.ceiling_breach  # dated today only (Issue #1231)
    _evening_open_time = facts.evening_open  # Issue #940: pair-validated against the close when gathered

    # Issue #876: the morning OPEN time is intentionally dropped from this sentence —
    # it carries no useful information (windows day starts, full stop). Only the
    # dynamically-computed close time matters here.
    if windows_advice and c.window_open_time:
        if _dynamic:
            # Issue #878-followup: the ODE crossing scan is forward-only from "now", so
            # when the close condition is already true at render time, the scan can only
            # return the nearest future grid point — not a real future prediction. Asserting
            # it as "Close up at {HH:MM}" hours after the real (earlier) crossing already
            # passed is a false future-scheduled claim.
            _already = _close.already
            # Issue #535: two distinct reasons the cutoff can fire — outdoor air rising
            # above indoor (the original predicate), or indoor forecast to reach the
            # comfort floor first. Same close time either way; different sentence why
            # (Issue #847: phrasing now shared via cutoff_reason_msg()).
            _effective_reason = _nat_vent_cutoff_reason
            if _effective_reason == "comfort_floor" and not free_cooling_direction_ok(
                current_outdoor_temp, current_indoor_temp
            ):
                # #430 live sanity check: comfort_floor was predicted, but outdoor has
                # already risen above indoor by render time — the comfort_floor risk
                # this text is about to assert is no longer live. Fall back to
                # outdoor_rise phrasing rather than stating a stale reason.
                _LOGGER.warning(
                    "Nat-vent cutoff reason overridden for briefing text — comfort_floor no longer"
                    " live at render time: indoor=%.1f°F outdoor=%.1f°F (outdoor has already risen"
                    " above indoor). reason %s -> outdoor_rise",
                    current_indoor_temp,
                    current_outdoor_temp,
                    _nat_vent_cutoff_reason,
                )
                _effective_reason = "outdoor_rise"
            if _already and _effective_reason == "outdoor_rise":
                # "before outdoor air warms past indoor" reads backwards once that's
                # already happened — a self-contained present-tense sentence instead
                # of composing the (now-wrong-tense) reason fragment.
                main = i18n.Msg("briefing.warm.close_now_outdoor")
            else:
                main = i18n.Msg(
                    "briefing.warm.close_up",
                    already="yes" if _already else "no",
                    time=_clock(_close.at),
                    reason=cutoff_reason_msg(_effective_reason),
                )
        else:
            main = i18n.Msg("briefing.warm.windows_open_morning")
    elif away:
        # Nobody is home: no claim about the air, and no awake band (the away section quotes the setbacks).
        main = i18n.Msg("briefing.warm.away")
    elif overnight_heat_engaged:
        main = i18n.Msg("briefing.warm.heater_ran", band=band)
    elif _says_air_wont_help(c):
        main = i18n.Msg("briefing.warm.air_wont_help", band=band)
    else:
        # Low is estimated: say neither that windows help nor that they don't.
        main = i18n.Msg("briefing.warm.holds_band", band=band)

    fan = i18n.Msg("briefing.warm.fan") if windows_advice and fan_mode != FAN_MODE_DISABLED else None

    # Issue #518: this used to independently promise "I'll run the AC starting around
    # X — no action needed from you", ignoring window state entirely — contradicting
    # the real automation guard (automation.py apply_classification()'s DEFER_PAUSED
    # branch actually suppresses AC the whole time a window is open) and duplicating
    # _fresh_air_section()'s already-correct, debounce-aware version of this same fact.
    # This section now only states the forecast and ties the AC to windows being
    # closed; _fresh_air_section owns the debounce/pause mechanics, so it's said once.
    ac: i18n.Msg | None
    if away:
        # Nobody is home: the AC-handoff sentences quote the awake ceiling, which is not what the thermostat holds
        # while away (the away section states the setback band).
        ac = None
    elif _ceiling_breach is not None:
        ac = i18n.Msg(
            "briefing.warm.ac_breach",
            hold=hold,
            time=_clock(_ceiling_breach),
            windows="yes" if windows_advice else "no",
        )
    elif windows_advice and _close is not None and not _close.already:
        ac = i18n.Msg("briefing.warm.ac_close_by", time=_clock(_close.at), hold=hold)
    elif windows_advice and _close is None:
        ac = i18n.Msg("briefing.warm.ac_safety_net", hold=hold)
    else:
        ac = i18n.Msg("briefing.warm.ac_steps_in", hold=hold)

    # Pre-cool night mention: warming trend + a lower sleep ceiling is planned
    precool = None
    if not away and facts.pre_cool_target is not None and getattr(c, "setback_modifier", 0.0) < 0:
        precool = i18n.Msg("briefing.warm.pre_cool", target=i18n.Temp(facts.pre_cool_target, temp_unit))

    reopen = None
    if windows_advice and _evening_open_time is not None:
        # Issue #876 (renamed from recovery_time/nat_vent_recovers): nat_vent_plan.py's
        # compute_nat_vent_plan() only ever populates evening_open_time for an
        # "outdoor_rise" cutoff — for a "comfort_floor" cutoff (an early/cold-morning
        # close held for comfort, not outdoor heat) reopening was never a genuine,
        # actionable later event, so evening_open_time is always None there and this
        # block is never reached for that reason. Only the outdoor_rise phrasing
        # applies here.
        # Issue #518: only claim "I'll turn off the AC" when the AC could
        # plausibly have engaged first (breach predicted before recovery) —
        # otherwise this contradicted itself by canceling an action that was
        # never actually started.
        ac_off = _ceiling_breach is not None and _ceiling_breach < _evening_open_time
        reopen = i18n.Msg("briefing.reopen_evening", time=_clock(_evening_open_time), ac_off="yes" if ac_off else "no")

    return _slots("briefing.warm.plan", main=main, fan=fan, ac=ac, precool=precool, reopen=reopen)


def _mild_day_msg(
    c,
    facts: _BriefingFacts,
    temp_unit: str = FAHRENHEIT,
    mild_events: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = True,
) -> i18n.Msg:
    """Conversational plan for mild days (60-74°F).

    Issue #847: previously had no reason branch at all — always said "to trap the
    warmth" regardless of ``nat_vent_cutoff_reason``, an asymmetry with
    ``_warm_day_plan()``. Now shares the same ``cutoff_reason_msg()`` helper and the same #430 live
    sanity check as ``_warm_day_plan()``, so the two day types (and the "Next
    Automation" card) can't disagree about what a given reason means.

    Issue #948 (Root Cause 1): the opening line used to unconditionally claim
    "I warmed to X before sunrise" regardless of whether heat ever actually ran
    — a house that coasts at comfort_heat purely on thermal mass has zero HVAC
    runtime, making that claim false. ``overnight_heat_engaged`` (real runtime
    signal, ``coordinator.get_hvac_runtime_today() > 0``) gates which opening
    line renders. Default True preserves prior behavior for callers that don't
    pass the signal (e.g. direct/standalone calls, older tests).

    Issue #1211: the window tip, the "close up by" line and the evening reopen line are gated on
    ``windows_advice`` (windows recommended today AND someone home; default ``c.windows_recommended``), and the
    plan never claims "HVAC is off" -- the comfort band is armed. ``near_miss`` (forecast high just under the
    floor) says outside air may help later instead of going silent (owner decision O7). ``away`` gets a neutral
    line with no claim about the air and no awake band; "isn't going to warm the house" is said only when the
    classifier decided so on a real forecast low (``_says_air_wont_help()``), and an estimated low asserts
    neither way.

    Issue #1341: ONE message (``briefing.mild.plan``) with whole-paragraph slots (first, near, tip, close, reopen).
    """
    windows_advice = facts.windows_advice
    away = facts.unoccupied
    near_miss = facts.near_miss
    # Issue #1091: display-only armed floor (Prefer savings widens it).
    _shown_heat = facts.awake_heat
    floor = i18n.Temp(_shown_heat, temp_unit)
    band = _band_msg(_shown_heat, facts.awake_cool, temp_unit)
    near = None
    if not windows_advice:
        if away:
            # Nobody is home: no claim about the air, and no awake band (the away section quotes the setbacks).
            first = i18n.Msg("briefing.mild.away")
        elif _says_air_wont_help(c):
            first = i18n.Msg("briefing.mild.air_wont_help", band=band)
        else:
            # Low is estimated: the answer is unknown, so say neither that windows help nor that they don't.
            first = i18n.Msg("briefing.mild.holds_band", band=band)
        if near_miss:
            near = i18n.Msg("briefing.mild.near_miss", high=i18n.Temp(c.today_high, temp_unit), floor=floor)
    elif overnight_heat_engaged:
        first = i18n.Msg("briefing.mild.self_care_warmed", floor=floor, band=band)
    else:
        first = i18n.Msg("briefing.mild.self_care_held", floor=floor)

    # Issue #876: the morning OPEN time is intentionally dropped from the tip -- it carries no useful information.
    tip = i18n.Msg("briefing.mild.tip") if windows_advice and c.window_open_time else None

    # Issue #534: prefer the ODE-derived cutoff when available, matching docs/08-COMPUTATION-REFERENCE.md
    # §6d. Issue #1231: the close is the dated one from the facts (``effective_window_close()``) -- a cutoff
    # dated another day is the static path (no reason, no already-reached phrasing).
    _close = facts.close
    _dynamic = _close is not None and _close.dynamic
    _mild_cutoff_reason = mild_events.get("nat_vent_cutoff_reason") if mild_events and _dynamic else None
    close_p = None
    if windows_advice and _close is not None:
        # Issue #878-followup: "already reached" is only meaningful when the dynamic cutoff is in play.
        _already = _close.already
        # Issue #847: same reason branch + #430 live sanity check as _warm_day_plan()
        # — only meaningful when the ODE-derived cutoff (with its reason) is actually
        # in play; the static classifier-hour fallback has no reason to branch on.
        _effective_reason = _mild_cutoff_reason if _dynamic else None
        if _effective_reason == "comfort_floor" and not free_cooling_direction_ok(
            current_outdoor_temp, current_indoor_temp
        ):
            _LOGGER.warning(
                "Nat-vent cutoff reason overridden for briefing text — comfort_floor no longer"
                " live at render time: indoor=%.1f°F outdoor=%.1f°F (outdoor has already risen"
                " above indoor). reason %s -> outdoor_rise",
                current_indoor_temp,
                current_outdoor_temp,
                _mild_cutoff_reason,
            )
            _effective_reason = "outdoor_rise"
        if _already and _effective_reason == "outdoor_rise":
            close_p = i18n.Msg("briefing.mild.close_now", floor=floor)
        else:
            close_p = i18n.Msg(
                "briefing.mild.close_up",
                already="yes" if _already else "no",
                time=_clock(_close.at),
                reason=cutoff_reason_msg(_effective_reason) if _dynamic else i18n.Msg("briefing.reason.trap_warmth"),
                floor=floor,
            )

    # Issue #876: MILD days had no evening-reopen sentence at all — mirroring _warm_day_plan()'s equivalent
    # (outdoor_rise-only; see nat_vent_plan.py's compute_nat_vent_plan() docstring). Issue #940: the open is
    # pair-validated against the dynamic close when the facts are gathered.
    reopen = None
    if windows_advice and facts.evening_open is not None:
        reopen = i18n.Msg("briefing.reopen_evening", time=_clock(facts.evening_open), ac_off="no")

    return _slots("briefing.mild.plan", first=first, near=near, tip=tip, close=close_p, reopen=reopen)


def _cool_day_msg(c, facts: _BriefingFacts, temp_unit: str = FAHRENHEIT) -> i18n.Msg:
    """Conversational plan for cool days (45-59°F).

    Issue #1231: an empty house (away/vacation) gets the lead line only -- the engine holds the away setback
    all day and night, so no hold temperature and no bedtime set-back is promised. At home the bedtime number is
    the one the TLDR row shows (``facts.bedtime_target``).
    """
    if facts.unoccupied:
        return i18n.Msg("briefing.cool.away")
    # Issue #1091: display-only armed floor (Prefer savings widens it).
    lead = i18n.Msg("briefing.cool.lead", hold=i18n.Temp(facts.awake_heat, temp_unit))
    bedtime = None
    if facts.bedtime_target is not None:
        bedtime = i18n.Msg("briefing.cool.bedtime", target=i18n.Temp(facts.bedtime_target, temp_unit))
    return _slots("briefing.cool.plan", lead=lead, bedtime=bedtime)


def _cold_day_msg(c, facts: _BriefingFacts, temp_unit: str = FAHRENHEIT) -> i18n.Msg:
    """Conversational plan for cold days (below 45°F).

    Issue #1231: an empty house (away/vacation) gets the lead line only. At home, the bedtime target is compared
    against the NORMAL bedtime setpoint (``facts.normal_bedtime``, the edge the engine arms at bedtime) -- never
    against ``setback_heat``, which is the AWAY setpoint. The engine arms that same edge every heating night, so
    when the two are the same number there is no cold-day adjustment to describe (owner decision, option A): the
    plain "At bedtime I'll set back to X." Only a learned value that differs is a "conservative setback ... instead
    of the usual" one. Body and TLDR quote the same number.
    """
    if facts.unoccupied:
        return i18n.Msg("briefing.cold.away")
    lead = i18n.Msg("briefing.cold.lead")

    colder = None
    if c.pre_condition and c.trend_direction == "cooling":
        # Issue #1211: this used to promise "I'll bump to X around 7pm for a couple hours". Nothing performs
        # that (the pre-heat intent is written to config and never read), so say only what is true.
        colder = i18n.Msg("briefing.cold.colder")

    bedtime = None
    if facts.bedtime_target is not None:
        _target = format_temp(facts.bedtime_target, temp_unit)
        target = i18n.Temp(facts.bedtime_target, temp_unit)
        if facts.normal_bedtime is None or format_temp(facts.normal_bedtime, temp_unit) == _target:
            bedtime = i18n.Msg("briefing.cold.bedtime", target=target)
        else:
            bedtime = i18n.Msg(
                "briefing.cold.bedtime_conservative",
                target=target,
                usual=i18n.Temp(facts.normal_bedtime, temp_unit),
            )
    return _slots("briefing.cold.plan", lead=lead, colder=colder, bedtime=bedtime)


def _leaving_home_msg(
    c,
    setback_heat,
    setback_cool,
    occupancy_mode: str = "home",
    temp_unit: str = FAHRENHEIT,
    automation_overridden: bool = False,
    facts: _BriefingFacts | None = None,
) -> i18n.Msg:
    """Conversational section about what happens when they leave.

    Args:
        c: Today's day classification.
        setback_heat: Heating setback temperature.
        setback_cool: Cooling setback temperature.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
        automation_overridden: Issue #948 (Root Cause 1, third site) — whether
            automation is disabled, manually overridden, or paused (the same
            signal ``_compute_automation_status()`` already reads). The
            away+cool/away+heat branches below used to unconditionally claim
            "I've applied setback temperatures..."/"I've dropped to X..." even
            though a disabled/overridden/paused automation never actually
            commanded that setback. When True, a hedged phrasing is used
            instead that doesn't claim the setback was actually applied.
            Default False preserves prior behavior for callers that don't
            pass the signal.
        facts: Issue #1369 -- the briefing facts. On vacation, "deeper" is said only when
            ``facts.vacation_deeper``; otherwise the vacation band from ``facts.band`` is quoted
            neutrally (a separate vacation setpoint may be equal to or milder than away). None keeps
            the pre-#1347 wording (vacation was always away -/+ 3, i.e. deeper).
    """
    Msg = i18n.Msg
    if occupancy_mode == "vacation":
        if facts is not None and not facts.vacation_deeper:
            return Msg(
                "briefing.leaving.vacation_band", band=_band_msg(facts.band.floor, facts.band.ceiling, temp_unit)
            )
        return Msg("briefing.leaving.vacation_deep")
    if occupancy_mode == "guest":
        return Msg("briefing.leaving.guest")
    if occupancy_mode == "away":
        if c.hvac_mode == "cool":
            return Msg(
                "briefing.leaving.away_cool_unconfirmed" if automation_overridden else "briefing.leaving.away_cool",
                target=i18n.Temp(setback_cool, temp_unit),
            )
        if c.hvac_mode == "heat":
            return Msg(
                "briefing.leaving.away_heat_unconfirmed" if automation_overridden else "briefing.leaving.away_heat",
                target=i18n.Temp(setback_heat, temp_unit),
            )
        # Issue #1211: the comfort band is armed on a warm/mild day (HVAC mode "off" is not "nothing runs"),
        # so say what is held while away instead of "the HVAC is off".
        return Msg("briefing.leaving.away_band", band=_band_msg(setback_heat, setback_cool, temp_unit))
    # occupancy_mode == "home" — default hypothetical text
    if c.hvac_mode == "cool":
        return Msg(
            "briefing.leaving.home_cool",
            minutes=OCCUPANCY_SETBACK_MINUTES,
            target=i18n.Temp(setback_cool, temp_unit),
        )
    if c.hvac_mode == "heat":
        return Msg(
            "briefing.leaving.home_heat",
            minutes=OCCUPANCY_SETBACK_MINUTES,
            target=i18n.Temp(setback_heat, temp_unit),
        )
    return Msg(
        "briefing.leaving.home_band",
        minutes=OCCUPANCY_SETBACK_MINUTES,
        band=_band_msg(setback_heat, setback_cool, temp_unit),
    )


def _fresh_air_msg(
    c,
    facts: _BriefingFacts,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    temp_unit: str = FAHRENHEIT,
    natural_vent_active: bool = False,
    current_outdoor_temp: float | None = None,
) -> i18n.Msg:
    """User-centric section about opening windows/doors for fresh air.

    Issue #1231: while the house is empty (away/vacation) the thermostat is holding the setback band, not the
    awake band, so no branch quotes the awake comfort value ("pull back down to 76°F", "hold your 76°F
    target", "holding 68-74°F today") -- it is left out rather than quoting the wrong value (Issue #1211).

    Affirms the user's choice first, then explains impact and recovery.
    Varies by HVAC mode since the impact differs significantly.
    Uses the configured debounce duration so the timing matches actual behavior.
    """
    Msg = i18n.Msg
    minutes = max(1, debounce_seconds // 60)
    away = "yes" if facts.unoccupied else "no"
    comfort_cool = facts.awake_cool

    if natural_vent_active and current_outdoor_temp is not None:
        return Msg(
            "briefing.fresh.vent_active",
            outdoor=i18n.Temp(current_outdoor_temp, temp_unit),
            away=away,
            target=i18n.Temp(comfort_cool, temp_unit),
        )
    if c.hvac_mode == "cool":
        return Msg("briefing.fresh.cool", minutes=minutes, away=away, target=i18n.Temp(comfort_cool, temp_unit))
    if c.hvac_mode == "heat":
        return Msg("briefing.fresh.heat", minutes=minutes)
    return Msg(
        "briefing.fresh.other",
        minutes=minutes,
        away=away,
        band=_band_msg(facts.awake_heat, comfort_cool, temp_unit),
    )


def _grace_period_msg(
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
) -> i18n.Msg | None:
    """Explain active grace periods and configured timings.

    Only included in the briefing when a grace period is currently active,
    so users aren't surprised that door/window sensors aren't pausing HVAC.
    Returns None when there is nothing noteworthy to report.
    """
    if not grace_active or not grace_source:
        return None
    if grace_source == "manual":
        return i18n.Msg("briefing.grace.manual", minutes=max(1, manual_grace_seconds // 60))
    # automation grace
    return i18n.Msg("briefing.grace.automation", minutes=max(1, automation_grace_seconds // 60))


def _tonight_msg(
    c,
    facts: _BriefingFacts,
    temp_unit: str = FAHRENHEIT,
    adaptive_thermal_active: bool = False,
) -> i18n.Msg:
    """Conversational preview of tonight and tomorrow based on trend.

    Issue #1231: while the house is empty the engine holds the away setback all night, so no "I'll set back more
    aggressively tonight" promise and no bedtime-tuning line is made.
    """
    _LOGGER.debug(
        "Tonight preview — trend=%s, magnitude=%.1f°F",
        c.trend_direction,
        c.trend_magnitude,
    )
    high = i18n.Temp(c.tomorrow_high, temp_unit)
    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        first = i18n.Msg("briefing.tonight.warming_away" if facts.unoccupied else "briefing.tonight.warming", high=high)
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        first = i18n.Msg("briefing.tonight.cooling", high=high)
    else:
        first = i18n.Msg("briefing.tonight.similar", high=high)
    adaptive = None
    if adaptive_thermal_active and c.hvac_mode in ("heat", "cool") and not facts.unoccupied:
        adaptive = i18n.Msg("briefing.tonight.adaptive")
    return _slots("briefing.tonight.plan", first=first, adaptive=adaptive)


# --- list[str] views of the sections (English unless ``lang``): the pre-#1341 shape the callers and tests read ---
def _lines(msg: i18n.Msg | None, lang: str) -> list[str]:
    ensure_catalogs()
    return i18n.render(msg, lang).split("\n") if msg is not None else []


def _hot_day_plan(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_hot_day_msg`` (paragraphs separated by an empty line)."""
    return _lines(_hot_day_msg(*args, **kwargs), lang)


def _warm_day_plan(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_warm_day_msg``."""
    return _lines(_warm_day_msg(*args, **kwargs), lang)


def _mild_day_plan(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_mild_day_msg``."""
    return _lines(_mild_day_msg(*args, **kwargs), lang)


def _cool_day_plan(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_cool_day_msg``."""
    return _lines(_cool_day_msg(*args, **kwargs), lang)


def _cold_day_plan(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_cold_day_msg``."""
    return _lines(_cold_day_msg(*args, **kwargs), lang)


def _leaving_home_section(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_leaving_home_msg``."""
    return _lines(_leaving_home_msg(*args, **kwargs), lang)


def _fresh_air_section(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_fresh_air_msg``."""
    return _lines(_fresh_air_msg(*args, **kwargs), lang)


def _grace_period_section(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_grace_period_msg`` (empty when nothing to report)."""
    return _lines(_grace_period_msg(*args, **kwargs), lang)


def _tonight_preview(*args: Any, lang: str = "en", **kwargs: Any) -> list[str]:
    """Rendered lines of ``_tonight_msg``."""
    return _lines(_tonight_msg(*args, **kwargs), lang)
