"""scenario_model - pure, stub-free helpers shared by every scenario runner adapter.

Issue #1127 Increment 5 / #1128 I2 (the Session split).  Nothing in this module installs
Home Assistant stubs or imports ``homeassistant``; the one production type it builds
(``DayClassification``) is imported lazily inside the function that needs it.  That is what lets
a real-Home-Assistant runner import the scenario model without the FakeHass stub layer.

Lifted verbatim from ``run_production.py`` (no behavior change): the result dataclass, event time
parsing, the scenario config merge / start-time rule, the hand-built ``DayClassification`` and the
final engine-state snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from tools.sim_harness.build_engine import _DEFAULT_CONFIG

_FALLBACK_START = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)


@dataclass
class ProductionRunResult:
    """Result of running a scenario through the production engine."""

    event_log: list[tuple[str, dict, datetime | None]] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)
    engine_state: dict[str, Any] = field(default_factory=dict)
    callback_errors: list[tuple[datetime, BaseException]] = field(default_factory=list)
    # Optional: a PromiseReport when a PromiseProbe was attached (never read by outcomes.py).
    promise_report: Any = None


RunResult = ProductionRunResult


def parse_event_time(time_str: str, default: datetime) -> datetime:
    """Parse ISO timestamp string to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError, AttributeError:
        return default


# Issue #1230 R6b: the PRIMARY prediction engine drives the prediction-dependent decisions and defaults to v4 in
# production. Every golden was authored and signed against v3's behaviour (a thermal model pinned in the scenario) and a
# scenario home has no fitted v4 model, so under the production default the decisions would fall back to the
# learning-not-finished defaults and the ceiling-guard goldens would stop describing what they were written to describe.
# Scenarios therefore run with v3 as the primary unless one states ``config.primary_engine``. That returns the goldens
# to the engine they were signed against; it changes no assertion and no outcome mapping. Direct builders
# (``build_headless_coordinator`` in unit tests) keep the production default.
SCENARIO_PRIMARY_ENGINE = "v3"


def merged_scenario_config(scenario: dict) -> dict[str, Any]:
    """Scenario ``config`` laid over the harness defaults (and the scenario primary engine, see above)."""
    scenario_config: dict[str, Any] = scenario.get("config", {})
    return {**_DEFAULT_CONFIG, "primary_engine": SCENARIO_PRIMARY_ENGINE, **scenario_config}


def sorted_events(scenario: dict) -> list[dict]:
    return sorted(scenario.get("events", []), key=lambda e: e["time"])


def scenario_start_time(events: list[dict]) -> datetime:
    """One second before the first event (or a fixed fallback for an event-less scenario)."""
    if events:
        return parse_event_time(events[0]["time"], _FALLBACK_START) - timedelta(seconds=1)
    return _FALLBACK_START


# A forecast typical of each day type, used ONLY for classification events that do not state their own
# ``today_high`` / ``today_low``. (Issue #1211) These replace the old blanket 75 / 60 default, which was a
# "warm" high for every day type. Hot days get a warm night (low above the window-opportunity cutoff) so a
# scenario that never mentions the economizer does not silently gain one.
_REPRESENTATIVE_LOW_F = {"hot": 82.0, "warm": 60.0, "mild": 50.0, "cool": 40.0, "cold": 25.0}


def _representative_high_f(day_type: str, comfort_heat: float) -> float:
    """A high that classifies as ``day_type`` (default thresholds); a mild one also reaches the comfort floor."""
    from custom_components.climate_advisor.const import (  # noqa: PLC0415
        THRESHOLD_COOL,
        THRESHOLD_HOT,
        THRESHOLD_MILD,
        THRESHOLD_WARM,
    )

    if day_type == "hot":
        return float(THRESHOLD_HOT + 5)
    if day_type == "warm":
        return float(THRESHOLD_WARM + 5)
    if day_type == "mild":
        return float(min(max(comfort_heat + 2.0, THRESHOLD_MILD), THRESHOLD_WARM - 1))
    if day_type == "cool":
        return float(THRESHOLD_COOL + 7)
    return float(THRESHOLD_COOL - 10)


def build_classification_from_event(event: dict, config: dict | None = None) -> Any:
    """Build a real DayClassification from a scenario classification event.

    Issue #1211 (C6): every recommendation field the scenario does NOT state is filled by the REAL
    classifier -- ``DayClassification.__post_init__`` -- from the scenario's day type, forecast and comfort
    band (``config``), instead of the old hand-built defaults (high 75 / low 60, hvac_mode "off",
    windows_recommended False, no window times). A field the scenario DOES state still wins, so every
    hand-authored classification keeps meaning exactly what its author wrote.

    Why the old default was wrong (semantic justification, per the test-infrastructure rule): it made an
    event that said ``day_type: warm`` and nothing else describe a day production's classifier would never
    produce (a warm day whose low is 60 gets windows recommended and window times; a mild day whose high
    is 75 is a warm day). Scenarios therefore exercised a state production cannot reach, and could not
    notice when the classifier changed. It cannot make a real failure pass silently: it only makes unstated
    inputs match the classifier; every stated field is applied after, unchanged.
    """
    from datetime import time as dt_time  # noqa: PLC0415

    from custom_components.climate_advisor.classifier import DayClassification  # noqa: PLC0415
    from custom_components.climate_advisor.const import DEFAULT_COMFORT_COOL, DEFAULT_COMFORT_HEAT  # noqa: PLC0415

    cfg = config or {}
    comfort_heat = float(cfg.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    comfort_cool = float(cfg.get("comfort_cool", DEFAULT_COMFORT_COOL))
    day_type = event.get("day_type", "mild")
    today_high = float(event.get("today_high", _representative_high_f(day_type, comfort_heat)))
    today_low = float(event.get("today_low", _REPRESENTATIVE_LOW_F.get(day_type, 50.0)))

    c = DayClassification(
        day_type=day_type,
        trend_direction=event.get("trend_direction", "stable"),
        trend_magnitude=float(event.get("trend_magnitude", 0.0)),
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=float(event.get("tomorrow_high", today_high)),
        tomorrow_low=float(event.get("tomorrow_low", today_low)),
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
    )

    # Scenario JSON uses "HH:MM" strings; DayClassification stores datetime.time objects.
    def _parse_time(val: str | None) -> dt_time | None:
        if not val:
            return None
        try:
            h, m = val.split(":")
            return dt_time(int(h), int(m))
        except ValueError, AttributeError:
            return None

    # --- Stated fields win over the classifier's ---
    if "hvac_mode" in event:
        c.hvac_mode = event["hvac_mode"]
    if "windows_recommended" in event:
        c.windows_recommended = bool(event["windows_recommended"])
        c.forecast_windows_recommended = c.windows_recommended
    if "setback_modifier" in event:
        c.setback_modifier = float(event["setback_modifier"])
    if "window_open_time" in event:
        c.window_open_time = _parse_time(event.get("window_open_time"))
    if "window_close_time" in event:
        c.window_close_time = _parse_time(event.get("window_close_time"))
    if "pre_condition" in event:
        c.pre_condition = bool(event["pre_condition"])
    if "pre_condition_target" in event:
        c.pre_condition_target = event.get("pre_condition_target")
    for flag in ("window_opportunity_morning", "window_opportunity_evening"):
        if flag in event:
            setattr(c, flag, bool(event[flag]))
    for key in (
        "window_opportunity_morning_start",
        "window_opportunity_morning_end",
        "window_opportunity_evening_start",
        "window_opportunity_evening_end",
    ):
        if key in event:
            setattr(c, key, _parse_time(event.get(key)))

    return c


def v4_model_from_spec(spec: dict) -> dict:
    """A valid stored v4 model dict from a compact scenario ``v4_model`` spec (Issue #1230 R6b).

    ``{"confidence": "ok" | "low" | "none", "primary": "<node>", "sensors": [...], "house": {"a_o": 0.05, ...}}``;
    every key is optional. The default primary node is ``_ca`` (the reading Climate Advisor itself uses, the node a
    model falls back to when no entity reads often enough), which every world fills the same way. ``{"model": {...}}``
    is a whole stored model (e.g. one fitted from a recorded history), used as given. Goes through the predictor's own
    ``_default_model`` / ``to_dict`` so ``from_dict`` accepts it exactly as it accepts a fitted model (the house
    parameters not given keep the predictor's start values).
    """
    from custom_components.climate_advisor import predictor_v4 as p  # noqa: PLC0415

    if isinstance(spec.get("model"), dict):  # a whole stored model (e.g. one fitted from a recorded history)
        return dict(spec["model"])
    primary = str(spec.get("primary", "_ca"))
    sensors = [primary, *[s for s in spec.get("sensors", []) if s != primary]]
    model = p._default_model(  # noqa: SLF001 - the fixture builder is the predictor's sanctioned constructor
        primary,
        sensors,
        str(spec.get("fitted_at", "2026-01-01T00:00:00+00:00")),
        p.thermostat_default(),
        p.V4ForecastCorrection(hourly_bias_f=tuple([0.0] * 24), nowcast_tau_h=3.0),
        {"reason": ""},
    )
    house = dict(model.house)
    house.update({k: float(v) for k, v in (spec.get("house") or {}).items()})
    model = replace(model, house=house, confidence=str(spec.get("confidence", "ok")))
    return p.to_dict(model)


def pin_scenario_v4_model(coordinator: Any, spec: dict) -> None:
    """Install the scenario's fitted v4 model and keep it for the whole run (Issue #1230 R6b).

    The scenario's model IS the zone's fitted model for that run (the way ``thermal_model`` is the learned v3 model,
    see ``pin_scenario_thermal_model``). A run that crosses the nightly 03:15 fit would otherwise refit it from the
    scenario home's near-empty chart log and replace it with a model of confidence "none", so the decisions would fall
    back mid-run for a reason no home has. Test-infrastructure semantics: the previous behaviour (no way to give a
    scenario a v4 model) could not express the situation at all; pinning cannot hide a real failure because a fit's
    own correctness is covered by its own tests (``test_v4_shadow_*``), not by a scenario.
    """
    store = getattr(coordinator, "_v4_store", None)
    if store is None:
        return
    store.v4_model = v4_model_from_spec(spec)
    coordinator._v4_start_fit = lambda *_a, **_k: False  # noqa: SLF001 - the pinned model is not refitted


def pin_scenario_thermal_model(coordinator: Any, model: dict) -> None:
    """Make a coordinator-mode run use the scenario's ``thermal_model`` through every refresh (Issue #1211).

    A scenario's ``thermal_model`` used to be written only to ``engine._thermal_model``. In production that
    attribute is whatever ``learning.get_thermal_model()`` last returned, and ``_async_update_data_impl``
    rewrites it from that call every 30-minute cycle -- so in coordinator mode the first refresh silently
    replaced the scenario's model with the harness's empty learning store, and the two runner modes diverged.
    The scenario's model IS the learned model for that run, so the coordinator's learning store answers with it.

    Why the old semantics were wrong (test-infrastructure rule): a scenario asserting a decision that depends
    on the ODE model passed or failed depending on whether a refresh had run yet. This cannot let a real
    failure pass silently: the engine sees the same model as before, now also after the first refresh.
    """
    learning = getattr(coordinator, "learning", None)
    if learning is not None:
        learning.get_thermal_model = lambda *args, **kwargs: model


def snapshot_engine_state(engine: Any) -> dict[str, Any]:
    """Read key engine flag attributes and return a plain dict snapshot."""
    snap: dict[str, Any] = {}

    for attr in (
        "_natural_vent_active",
        "_manual_override_active",
        "_grace_active",
        "_paused_by_door",
        "_fan_active",
        "_fan_override_active",
        "_occupancy_mode",
        "_override_confirm_pending",
        "_economizer_active",
        "_economizer_phase",
        "_pre_condition_achieved",  # Issue #295
        "_pre_condition_achieved_date",  # Issue #295
        "_nat_vent_soft_start",  # Issue #606
        "_nat_vent_outdoor_exit_time",  # Issue #606
        "_last_outdoor_temp",  # Issue #633
        "_outdoor_temp_today_peak",  # Issue #633
        "_outdoor_temp_today_sample_count",  # Issue #633
        "_comfort_mode_family",  # Issue #821 - "heating"/"cooling"/None
        "_fan_manual_origin_unclosed",  # Issue #829
        "_nat_vent_cutoff_already_reached",  # Issue #1230 R6b
    ):
        snap[attr] = getattr(engine, attr, None)
    # Issue #1230 R6b: the nat-vent plan's cutoff the engine acts on (ISO string; None when no plan set one).
    _cutoff = getattr(engine, "_nat_vent_cutoff", None)
    snap["_nat_vent_cutoff"] = _cutoff.isoformat() if hasattr(_cutoff, "isoformat") else None

    # Issue #633: indoor temp is never cached on the engine itself (always a
    # live hass state read) - snapshot it explicitly the same way any other
    # post-run inspection would, using the engine's own reader.
    get_indoor = getattr(engine, "_get_indoor_temp_f", None)
    snap["_last_indoor_temp"] = get_indoor() if callable(get_indoor) else None

    # Issue #1048: the thermostat's ACTUAL mode (ground truth, not what CA commanded).
    _hass = getattr(engine, "hass", None)
    _st = _hass.states.get(engine.climate_entity) if _hass is not None else None
    snap["_thermostat_hvac_mode"] = getattr(_st, "state", None)

    # Include classification summary if available
    c = getattr(engine, "_current_classification", None)
    if c is not None:
        snap["_current_classification"] = {
            "day_type": getattr(c, "day_type", None),
            "hvac_mode": getattr(c, "hvac_mode", None),
            "windows_recommended": getattr(c, "windows_recommended", None),
        }
    else:
        snap["_current_classification"] = None

    return snap


def capture_next_action(coordinator: Any, engine: Any, scenario: dict) -> dict[str, Any]:
    """The surfaces an occupant reads, for the run's final state (Issue #1276; extended by Issue #1230 R3b).

    Shared by both adapters (FakeHass Tier A and real-HA Tier B) so a scenario that sets
    ``"capture_next_action": true`` is judged the same way on both. Same arguments production passes in
    ``_async_update_data`` (classification, indoor reading, outdoor, physical window state, engine);
    outdoor is the scenario's last ``temp_update`` ``outdoor_f`` (Tier A runs no weather entity, so this
    keeps the two tiers on one input). ``advises_opening_windows`` lets a scenario assert the card's
    direction with the existing ``field_equals`` assertion, so ``outcomes.py`` is unchanged.

    R3b additions (all read-only, same call, same clock): ``source`` ("timeline" when the card/automation read the
    operation timeline, "legacy" for the degraded path), ``window_step`` (the timeline step the card came from, or
    None), ``next_automation`` (the Next Automation card's action and time), ``timeline`` (the occupant steps and the
    CA step kinds the plan holds, as ``HH:MM`` strings), ``briefing_windows_row`` (the briefing TLDR "Windows" row),
    ``agrees`` (the card never invites a window the timeline does not hold), and inside ``timeline``: ``held_ca_steps``,
    ``sensor_swap`` and ``has_resume_step``. Existing keys are unchanged.
    """
    updates = [e for e in sorted_events(scenario) if e.get("type") == "temp_update" and "outdoor_f" in e]
    windows_open = coordinator._any_sensor_open()
    text = coordinator._compute_next_action(
        coordinator._current_classification,
        indoor_temp=coordinator._get_indoor_temp(),
        outdoor_temp=float(updates[-1]["outdoor_f"]) if updates else None,
        windows_physically_open=windows_open,
        ae=engine,
    )
    out: dict[str, Any] = {"text": text, "advises_opening_windows": str(text).startswith("Open windows")}
    tl = coordinator._surface_timeline() if hasattr(coordinator, "_surface_timeline") else None
    out["source"] = "timeline" if tl is not None else "legacy"
    action, at = coordinator._compute_next_automation_action(coordinator._current_classification)
    out["next_automation"] = {"action": action, "time": at}
    if tl is not None:
        from homeassistant.util import dt as _dt  # noqa: PLC0415

        from custom_components.climate_advisor.timeline_surfaces import card_user_step  # noqa: PLC0415

        now = _dt.now()
        step = card_user_step(tl, now, windows_open=windows_open)
        you = [s for s in tl.steps if s.actor.value == "you"]
        out["window_step"] = None if step is None else {"kind": step.kind.value, "at": step.ts.strftime("%H:%M")}
        # the card step's state (R3b redesign): "held" with the veto rule id when the live direction rule vetoes it, and
        # the sentence the plan list shows for it
        out["window_step_state"] = (
            None if step is None else {"status": step.status, "hold": step.meta.get("hold"), "text": step.text}
        )
        _resume = next((s for s in tl.steps if s.kind.value == "resume_schedule"), None)
        out["timeline"] = {
            "you_steps": [f"{s.ts:%H:%M} {s.kind.value}" for s in you],
            "you_step_states": [f"{s.ts:%H:%M} {s.kind.value} {s.status}" for s in you],
            "resume_step": None if _resume is None else {"at": f"{_resume.ts:%H:%M}", "text": _resume.text},
            "ca_kinds": sorted({s.kind.value for s in tl.steps if s.actor.value == "ca"}),
            "ca_steps": [f"{s.ts:%H:%M} {s.kind.value}" for s in tl.steps if s.actor.value == "ca"],
            "window_confidence": sorted({s.confidence for s in you}),
            # steps CA words as a held step (an override/grace or a door pause is in force) and the sensor swap
            # sentences
            "held_ca_steps": [
                f"{s.ts:%H:%M} {s.kind.value}" for s in tl.steps if s.actor.value == "ca" and s.status == "held"
            ],
            "sensor_swap": {
                "wake": any(s.reason == "wake_band.sensor_swap" for s in tl.steps),
                "sleep": any(s.reason == "sleep_band.sensor_swap" for s in tl.steps),
            },
            "has_resume_step": any(s.kind.value == "resume_schedule" for s in tl.steps),
        }
        # the card may only invite a window the plan holds (a period open now, or ahead today)
        out["agrees"] = (not out["advises_opening_windows"]) or any(
            s.kind.value == "windows_open" and s.ts.date() == now.date() and s.status != "done" for s in you
        )
    # Issue #1230 R6 (X7): the time-of-use bank window the plan shows (the coordinator's resolved window), as HH:MM, so
    # a scenario can pin the exact start the surfaces quote against the time the thermostat is first told (additive).
    _tw = (
        coordinator._tou_precondition_window_tuple() if hasattr(coordinator, "_tou_precondition_window_tuple") else None
    )
    out["tou_window"] = (
        None
        if _tw is None
        else {"start": f"{_tw[0]:%H:%M}", "end": f"{_tw[1]:%H:%M}", "target": _tw[2], "mode": _tw[3]}
    )
    classification = coordinator._current_classification
    if classification is not None:
        _full, short = coordinator._build_briefing_text(classification, suggestions=[])
        row = next((ln.strip() for ln in short.splitlines() if ln.strip().startswith("Windows:")), None)
        out["briefing_windows_row"] = row
    # Issue #1404 (additive): what each surface says the house is held to, so a time-of-use bank scenario can pin that
    # Status, the thresholds (ca_target_heat/cool, the reading cards), the briefing's HVAC Mode row, the chart band now,
    # Next Automation and the plan list's "Now" row tell the same story as the thermostat writes.
    out["hold"] = _capture_hold(coordinator, engine)
    return out


def _capture_hold(coordinator: Any, engine: Any) -> dict[str, Any]:
    """The hold every surface quotes, read from the real production functions (Issue #1404). Read-only."""
    from datetime import timedelta  # noqa: PLC0415

    from homeassistant.util import dt as _dt  # noqa: PLC0415

    from custom_components.climate_advisor.automation import active_thresholds_for  # noqa: PLC0415

    th = active_thresholds_for(coordinator)
    payload_fn = getattr(coordinator, "current_hold_payload", None)
    payload = payload_fn() if callable(payload_fn) else None
    classification = coordinator._current_classification
    hvac_row = None
    if classification is not None:
        _full, short = coordinator._build_briefing_text(classification, suggestions=[])
        hvac_row = next((ln.strip() for ln in short.splitlines() if ln.strip().startswith("HVAC Mode:")), None)
    # The chart band in force now: the band the chart draws (the display band where production has one), fed the same
    # synthetic hourly forecast the P8 chart reader uses (Tier A runs no weather entity).
    now = _dt.now()
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    hourly = [{"datetime": (hour0 + timedelta(hours=h)).isoformat(), "temperature": 60.0} for h in range(-24, 49)]
    builder = getattr(coordinator, "_chart_display_band", None) or coordinator._build_target_band_for
    band = builder(now, engine._thermal_model, hourly_forecast_override=hourly)
    current = [e for e in band if _dt.parse_datetime(str(e["ts"])) <= now]
    chart_now = None
    if current:
        last = max(current, key=lambda e: str(e["ts"]))
        chart_now = [last.get("lower"), last.get("upper")]
    tl = coordinator.timeline_payload() if hasattr(coordinator, "timeline_payload") else None
    plan_now = ((tl or {}).get("current_hold") or {}).get("text")
    return {
        "status": coordinator._compute_automation_status(),
        "thresholds": [th.get("heat"), th.get("cool")],
        "threshold_context": th.get("context"),
        "briefing_hvac_row": hvac_row,
        "chart_band_now": chart_now,
        "plan_now": plan_now,
        "text": (payload or {}).get("text"),
    }


def sync_coordinator_occupancy(coordinator: Any, mode: str) -> None:
    """Production's occupancy toggle writes the COORDINATOR's mode, hands it to the engine, then regenerates the
    operation timeline (``coordinator._async_occupancy_toggle_changed``). The Tier A event driver only reached the
    engine, so a surface the coordinator computes (Next Action, the timeline, the briefing) kept reading ``home``.
    Shared by the FakeHass and real-HA adapters.
    Semantic justification (Issue #1230 R3b, test-infrastructure rule): the previous driver modelled one half of a
    two-part production step; scenarios that read coordinator surfaces after an occupancy event were therefore
    exercising a state production cannot reach (engine away, coordinator home). Could this let a real failure pass
    silently? No: it only makes the coordinator's mode equal the engine's, as production does; every engine decision
    is unchanged (the engine call above is untouched) and the timeline run is the same call production makes.
    """
    if coordinator is None or not hasattr(coordinator, "_occupancy_mode"):
        return
    coordinator._occupancy_mode = mode
    if hasattr(coordinator, "_timeline_shadow_run"):
        coordinator._timeline_shadow_run("occupancy")


# Issue #1420: the coordinator's own daily schedule (``async_setup``: ``async_track_time_change`` at ``wake_time`` /
# ``sleep_time``, production defaults 06:30 / 22:30) for the two schedule events a scenario can name.
_DAILY_SCHEDULE = {"wakeup": ("wake_time", "06:30"), "bedtime": ("sleep_time", "22:30")}


def daily_schedule_fires_at(coordinator: Any, etype: str, now: datetime) -> bool:
    """Whether the coordinator's own daily ``etype`` timer fires in ``now``'s minute (Issue #1420).

    Production has only that timer, so a ``bedtime`` / ``wakeup`` event in the same minute IS that timer: the
    session's clock already fired it when it reached the event time, and calling the engine handler as well would
    be a second write production never makes.  Reads the time the coordinator itself scheduled (its config, its
    defaults, its parser), not the scenario's config, so an unset ``wake_time`` still matches the 06:30 default.
    Shared by the FakeHass (coordinator) and real-HA adapters.
    """
    if coordinator is None or etype not in _DAILY_SCHEDULE:
        return False
    from custom_components.climate_advisor import coordinator as coordinator_module  # noqa: PLC0415

    key, default = _DAILY_SCHEDULE[etype]
    at = coordinator_module._parse_time(coordinator.config.get(key, default))
    local = coordinator_module.dt_util.as_local(now)
    return (local.hour, local.minute) == (at.hour, at.minute)


class _PreCoolFireRecorder:
    """Reference-counted class-level wrapper; see ``record_pre_cool_fires``."""

    _installs: dict[Any, tuple[Any, int]] = {}  # coordinator class -> (original method, live sessions)

    def __init__(self, coordinator_cls: Any) -> None:
        self._cls = coordinator_cls
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        cls = self._cls
        if cls in self._installs:
            original, count = self._installs[cls]
            self._installs[cls] = (original, count + 1)
            return
        original = cls.__dict__["_async_pre_cool_trigger"]

        async def _async_pre_cool_trigger(coordinator_self: Any, now: datetime) -> Any:
            fired = getattr(coordinator_self, "_sim_pre_cool_fired_at", None)
            if not isinstance(fired, list):
                fired = []
                coordinator_self._sim_pre_cool_fired_at = fired
            fired.append(now)
            return await original(coordinator_self, now)

        cls._async_pre_cool_trigger = _async_pre_cool_trigger
        self._installs[cls] = (original, 1)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        original, count = self._installs[self._cls]
        if count > 1:
            self._installs[self._cls] = (original, count - 1)
            return
        del self._installs[self._cls]
        self._cls._async_pre_cool_trigger = original


def record_pre_cool_fires(coordinator_cls: Any) -> _PreCoolFireRecorder:
    """A class-level wrapper that notes each time the coordinator's own pre-cool timer fires (Issue #1420).

    ``coordinator._maybe_schedule_pre_cool`` arms ``_async_pre_cool_trigger`` with ``async_track_point_in_time``;
    the wrapper appends the fire time (the callback's ``now``) to ``coordinator_self._sim_pre_cool_fired_at`` and
    then runs the real trigger unchanged.  Class-level so a timer armed during set-up or after a restart (a new
    instance) is seen too; reference-counted so sessions that overlap in one process (in any close order) install
    it once and restore the original when the last one stops.  Returns an unstarted recorder (``start`` / ``stop``).
    """
    return _PreCoolFireRecorder(coordinator_cls)


def pre_cool_timer_fired_at(coordinator: Any, now: datetime) -> bool:
    """Whether the coordinator's own pre-cool timer fired in ``now``'s minute (needs ``record_pre_cool_fires``).

    Production has only that timer, so a ``pre_cool`` event in the same minute IS that timer (Issue #1420).
    """
    fired = getattr(coordinator, "_sim_pre_cool_fired_at", None) if coordinator is not None else None
    if not isinstance(fired, list):
        return False
    minute = now.replace(second=0, microsecond=0)
    return any(t.replace(second=0, microsecond=0) == minute for t in fired)
