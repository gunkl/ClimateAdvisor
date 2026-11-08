"""strip_check - the chart's activity strips right of NOW against the plan (Issue #1436, P8 surface ``chart_strips``).

Occupant first: the plan says "7:00 PM: close the windows", and the strips under the chart must not show the windows
plan, natural ventilation or the fan running all night. Both things the chart draws from for the future are judged:

* ``strips``: the timeline payload's ``strips`` (``timeline_surfaces.strip_spans``), what the faded strips draw since
  #1436 (``None`` when the payload has no such field: a tree from before #1436, whose chart drew the future from the
  next item);
* ``activity``: the chart payload's ``predicted_activity`` (``windows_recommended`` / ``fan_active`` per hour), what the
  faded strips drew before #1436 and what the forward Target line's nat-vent tier still reads.

The reference is the plan's own window periods, read with the same production functions the briefing's window times use
(``timeline_surfaces.window_periods`` + ``is_direction_vetoed``, a period the live direction rule vetoes is not one the
plan expects), its predicted runs and its setback / resume steps. Nothing is re-planned here.

Pure (no Home Assistant import): the FakeHass P8 probe, the scenario capture shared with real HA and the real-HA
replay test all call it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from custom_components.climate_advisor.operation_timeline import StepKind, Timeline
from custom_components.climate_advisor.timeline_surfaces import is_direction_vetoed, window_periods

GRID_STEP = timedelta(minutes=30)
GRID_OFFSET = timedelta(minutes=15)  # sample between half hours: step times sit on round minutes
HORIZON = timedelta(hours=24)  # the faded strips a person reads: the next day


def plan_windows_open(tl: Timeline, at: datetime) -> bool:
    """The reference: one of the plan's (not vetoed) window periods covers ``at``."""
    for o, c in window_periods(tl):
        if o is not None and is_direction_vetoed(o):
            continue
        if (o is None or o.ts <= at) and at < c.ts:
            return True
    return False


def _plan_fan_advised(tl: Timeline, at: datetime) -> bool:
    """A fan the plan's own fan steps hold at ``at`` (the night strategy's fan on ... fan off)."""
    on: datetime | None = None
    for s in sorted(tl.steps, key=lambda x: x.ts):
        if s.kind == StepKind.FAN_ON:
            on = on or s.ts
        elif s.kind == StepKind.FAN_OFF:
            if on is not None and on <= at < s.ts:
                return True
            on = None
    return False


def _covers(spans: Iterable[Mapping[str, Any]], row: str, at: datetime) -> bool:
    for d in spans:
        if d.get("row") != row:
            continue
        try:
            if datetime.fromisoformat(str(d["start"])) <= at < datetime.fromisoformat(str(d["end"])):
                return True
        except KeyError, TypeError, ValueError:
            continue
    return False


def _close_after(tl: Timeline, at: datetime) -> str:
    close = next(
        (s for s in sorted(tl.steps, key=lambda x: x.ts) if s.kind == StepKind.WINDOWS_CLOSE and s.ts > at), None
    )
    return f"; next close step {close.ts:%H:%M}" if close is not None else ""


def strip_problems(
    tl: Timeline,
    now: datetime,
    strips: list[Mapping[str, Any]] | None,
    activity: list[Mapping[str, Any]] | None,
    *,
    fan_mode: str = "",
) -> list[str]:
    """Sentences, one per disagreement (first time each rule fails), empty when the strips follow the plan."""
    problems: dict[str, str] = {}

    def flag(key: str, sentence: str) -> None:
        problems.setdefault(key, sentence)

    end = min(now + HORIZON, tl.horizon_end)
    runs = list(getattr(tl, "runs", ()) or ())
    has_setback = any(s.kind == StepKind.SETBACK_START for s in tl.steps)
    resumes = [s.ts for s in tl.steps if s.kind == StepKind.RESUME_SCHEDULE]
    if strips is not None:
        t = now + GRID_OFFSET
        while t < end:
            open_ = plan_windows_open(tl, t)
            win = _covers(strips, "windowsplan", t)
            if win and not open_:
                flag("windowsplan+", f"Win plan strip shows {t:%a %H:%M} but the plan has the windows closed then")
            if open_ and not win:
                flag("windowsplan-", f"Win plan strip is empty at {t:%a %H:%M} but the plan has the windows open then")
            if _covers(strips, "natvent", t) and not open_:
                flag(
                    "natvent",
                    f"Nat-vent strip shows {t:%a %H:%M} but the plan has the windows closed then"
                    f"{_close_after(tl, now)}",
                )
            if _covers(strips, "fan", t) and not (open_ or _plan_fan_advised(tl, t)):
                flag("fan", f"Fan strip shows {t:%a %H:%M} but the plan holds no fan then")
            for row, mode in (("heating", "heat"), ("cooling", "cool")):
                if _covers(strips, row, t) and not any(r.mode == mode and r.start <= t < r.end for r in runs):
                    flag(row, f"{row.capitalize()} strip shows {t:%a %H:%M} but the plan predicts no {mode} run then")
            if _covers(strips, "away", t) and not has_setback:
                flag("away", f"Away strip shows {t:%a %H:%M} but the plan holds no setback")
            if _covers(strips, "override", t) and not any(t < r for r in resumes):
                flag("override", f"Override strip shows {t:%a %H:%M} but the plan resumes no schedule after it")
            if _covers(strips, "windows", t):
                flag("windows", f"Windows-open strip shows {t:%a %H:%M} in the future: the plan cannot know it")
            t += GRID_STEP
    for e in activity or ():
        try:
            at = datetime.fromisoformat(str(e.get("ts")))
        except TypeError, ValueError:
            continue
        if not (now < at <= end):
            continue
        open_ = plan_windows_open(tl, at)
        if bool(e.get("windows_recommended")) != open_:
            flag(
                "activity.windows",
                f"predicted_activity says windows_recommended={bool(e.get('windows_recommended'))} at {at:%a %H:%M} "
                f"but the plan has the windows {'open' if open_ else 'closed'} then{_close_after(tl, now)}",
            )
        if fan_mode != "on" and e.get("fan_active") and not open_:
            flag(
                "activity.fan",
                f"predicted_activity says fan_active at {at:%a %H:%M} but the plan has the windows closed then"
                f"{_close_after(tl, now)}",
            )
    return list(problems.values())


def summarize_strips(strips: list[Mapping[str, Any]] | None, now: datetime) -> dict[str, list[str]] | None:
    """``{row: ["HH:MM-HH:MM", ...]}`` of the next 24 h (a golden asserts this with ``field_equals``)."""
    if strips is None:
        return None
    out: dict[str, list[str]] = {}
    for d in strips:
        try:
            a, b = datetime.fromisoformat(str(d["start"])), datetime.fromisoformat(str(d["end"]))
        except KeyError, TypeError, ValueError:
            continue
        if a >= now + HORIZON:
            continue
        out.setdefault(str(d.get("row")), []).append(f"{a:%H:%M}-{min(b, now + HORIZON):%H:%M}")
    return out


def activity_hours(activity: list[Mapping[str, Any]] | None, now: datetime, key: str) -> list[str]:
    """The ``HH:MM`` of each hour in the next 24 h whose ``predicted_activity`` entry has ``key`` true."""
    out: list[str] = []
    for e in activity or ():
        try:
            at = datetime.fromisoformat(str(e.get("ts")))
        except TypeError, ValueError:
            continue
        if now < at <= now + HORIZON and e.get(key):
            out.append(f"{at:%H:%M}")
    return out


def chart_activity(coord: Any, now: datetime, outdoor_f: float | None = None) -> list[Mapping[str, Any]]:
    """The chart payload's ``predicted_activity`` from the REAL ``coordinator.get_chart_data()``.

    Harness input shim (read-only, restored after): a run with no weather forecast has no hourly timestamps, and then
    the chart builds no future at all. As in ``surfaces.read_chart_band``, a flat hourly forecast at the current outdoor
    reading is supplied for the duration of the call; a run that has a forecast (a scenario ``hourly_forecast``, real
    HA's weather entity) is used as it is.
    """
    had = getattr(coord, "_hourly_forecast_temps", None)
    shim = not had
    if shim:
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        out = float(outdoor_f) if isinstance(outdoor_f, (int, float)) else 60.0
        coord._hourly_forecast_temps = [
            {"datetime": (hour0 + timedelta(hours=h)).isoformat(), "temperature": out} for h in range(-24, 49)
        ]
    try:
        data = coord.get_chart_data("24h")
    finally:
        if shim:
            coord._hourly_forecast_temps = had
    return list((data or {}).get("predicted_activity") or [])


def check_coordinator(coord: Any, now: datetime, outdoor_f: float | None = None) -> dict[str, Any] | None:
    """Read both chart sources from a live coordinator and judge them (None before the coordinator has a plan)."""
    tl = coord._surface_timeline() if hasattr(coord, "_surface_timeline") else None
    if tl is None:
        return None
    payload = coord.timeline_payload() if hasattr(coord, "timeline_payload") else None
    strips = payload.get("strips") if isinstance(payload, dict) else None
    activity = chart_activity(coord, now, outdoor_f)
    fan_mode = str(coord.config.get("fan_mode", ""))
    problems = strip_problems(tl, now, strips, activity, fan_mode=fan_mode)
    return {
        "strips": summarize_strips(strips, now),
        "activity_windows_hours": activity_hours(activity, now, "windows_recommended"),
        "activity_fan_hours": activity_hours(activity, now, "fan_active"),
        "problems": problems,
        "agree": not problems,
    }
