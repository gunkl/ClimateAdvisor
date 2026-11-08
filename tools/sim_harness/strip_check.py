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


ROWS = ("heating", "cooling", "fan", "natvent", "windows", "windowsplan", "away", "override")
LABEL = {
    "heating": "Heating",
    "cooling": "Cooling",
    "fan": "Fan",
    "natvent": "Nat-vent",
    "windows": "Windows-open",
    "windowsplan": "Win plan",
    "away": "Away",
    "override": "Override",
}
EDGE = timedelta(seconds=30)  # samples either side of every span end and step time: a 1-minute overrun is visible


class _Reference:
    """What the plan expects on each strip at an instant, read from the plan's own steps and runs (production
    functions only), BOTH directions: a strip where the plan has nothing, and a plan item with no strip.

    * Win plan: a (not vetoed) window period covers the instant (``window_periods`` + ``is_direction_vetoed``).
    * Nat-vent: a plan session covers it (the latest nat-vent step at or before it is a start; or, with none before, the
      next one after it is a stop: a session running now) AND a window period does (the session runs only while a
      window the plan opens is open). A start that follows a vetoed open is not a session.
    * Fan: a nat-vent session as above, or the plan's own fan steps (fan on ... fan off).
    * Heating / Cooling: one of the plan's predicted runs RUNS at that instant: its placed minutes
      (``PredictedRun.bursts``: each simulation step's minutes from that step's start), or, without a placement, its
      minutes from the window's start (owner decision: the bar is the run's minutes, never its window).
    * Away: a setback step, from it to the next midnight (Away) or the end of the plan (Vacation).
    * Override: before the plan's resume step. Windows-open: never (the plan cannot know it).
    """

    def __init__(self, tl: Timeline, now: datetime) -> None:
        self.tl, self.now = tl, now
        steps = sorted(tl.steps, key=lambda x: x.ts)
        vetoed = {s.id for s in steps if is_direction_vetoed(s) and s.id}
        self.nv = [
            s
            for s in steps
            if s.kind == StepKind.NAT_VENT_STOP or (s.kind == StepKind.NAT_VENT_START and s.after not in vetoed)
        ]
        self.runs = list(getattr(tl, "runs", ()) or ())
        self.away: list[tuple[datetime, datetime]] = []
        for s in steps:
            if s.kind == StepKind.SETBACK_START:
                stop = (
                    tl.horizon_end
                    if s.reason.endswith(".vacation")
                    else datetime.combine(s.ts.date() + timedelta(days=1), datetime.min.time(), tzinfo=s.ts.tzinfo)
                )
                self.away.append((s.ts, stop))
        self.resumes = [s.ts for s in steps if s.kind == StepKind.RESUME_SCHEDULE]

    def _session(self, t: datetime) -> bool:
        prev = [s for s in self.nv if s.ts <= t]
        if prev:
            return prev[-1].kind == StepKind.NAT_VENT_START
        nxt = next((s for s in self.nv if s.ts > t), None)
        return nxt is not None and nxt.kind == StepKind.NAT_VENT_STOP

    def _running(self, mode: str, t: datetime) -> bool:
        for r in self.runs:
            if r.mode != mode or not (r.start <= t < r.end):
                continue
            bursts = list(getattr(r, "bursts", ()) or ())
            if bursts:
                if any(b0 <= t < b0 + timedelta(minutes=float(m)) for b0, m in bursts if m > 0):
                    return True
            elif t < r.start + timedelta(minutes=float(r.minutes)):
                return True
        return False

    def covers(self, row: str, t: datetime) -> tuple[bool, str]:
        open_ = plan_windows_open(self.tl, t)
        if row == "windowsplan":
            return open_, f"the plan has the windows {'open' if open_ else 'closed'} then{_close_after(self.tl, t)}"
        if row == "natvent":
            sess = self._session(t)
            want = sess and open_
            why = (
                "the plan runs a nat-vent session then"
                if want
                else ("the plan has the windows closed then" if sess else "the plan holds no nat-vent session then")
            )
            return want, why + _close_after(self.tl, t)
        if row == "fan":
            want = (self._session(t) and open_) or _plan_fan_advised(self.tl, t)
            return want, "the plan runs the fan then" if want else "the plan holds no fan then"
        if row in ("heating", "cooling"):
            mode = "heat" if row == "heating" else "cool"
            want = self._running(mode, t)
            return want, f"the plan expects a {mode} run's minutes then" if want else f"the plan runs no {mode} then"
        if row == "away":
            want = any(a <= t < b for a, b in self.away)
            return want, "the plan holds the setback then" if want else "the plan holds no setback then"
        if row == "override":
            want = any(t < r for r in self.resumes)
            return want, "CA resumes the schedule only later" if want else "the plan has resumed the schedule by then"
        return False, "the plan cannot know whether a window will be open"


def _samples(tl: Timeline, now: datetime, end: datetime, strips: list[Mapping[str, Any]]) -> list[datetime]:
    """The 30-minute grid plus EDGE either side of every strip end, every step time and every run / burst bound."""
    points: set[datetime] = set()
    t = now + GRID_OFFSET
    while t < end:
        points.add(t)
        t += GRID_STEP
    marks: list[datetime] = [s.ts for s in tl.steps]
    for d in strips:
        for k in ("start", "end"):
            try:
                marks.append(datetime.fromisoformat(str(d[k])))
            except KeyError, TypeError, ValueError:
                continue
    for r in list(getattr(tl, "runs", ()) or ()):
        marks += [r.start, r.end]
        for b0, m in getattr(r, "bursts", ()) or ():
            marks += [b0, b0 + timedelta(minutes=float(m))]
    for m in marks:
        for p in (m - EDGE, m + EDGE):
            if now < p < end:
                points.add(p)
    return sorted(points)


def _check_window_ends(tl: Timeline, now: datetime, end: datetime, strips, flag) -> None:
    """Exact endpoints: the Win plan spans are the plan's (not vetoed) window periods clipped to [now, horizon]."""
    want = []
    for o, c in window_periods(tl):
        if o is not None and is_direction_vetoed(o):
            continue
        a, b = max(o.ts if o is not None else now, now), min(c.ts, tl.horizon_end)
        if b > a and a < end:
            want.append((a, b))
    got = []
    for d in strips:
        if d.get("row") != "windowsplan":
            continue
        try:
            a, b = datetime.fromisoformat(str(d["start"])), datetime.fromisoformat(str(d["end"]))
        except KeyError, TypeError, ValueError:
            continue
        if a < end:
            got.append((a, b))
    if sorted(got) != sorted(want):
        flag(
            "windowsplan.ends",
            "Win plan spans "
            + ", ".join(f"{a:%a %H:%M}-{b:%H:%M}" for a, b in sorted(got))
            + " are not the plan's window periods "
            + ", ".join(f"{a:%a %H:%M}-{b:%H:%M}" for a, b in sorted(want)),
        )


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
    if strips is not None:
        ref = _Reference(tl, now)
        for t in _samples(tl, now, end, strips):
            for row in ROWS:
                want, why = ref.covers(row, t)
                got = _covers(strips, row, t)
                if got and not want:
                    flag(f"{row}+", f"{LABEL[row]} strip shows {t:%a %H:%M} but {why}")
                elif want and not got:
                    flag(f"{row}-", f"{LABEL[row]} strip is empty at {t:%a %H:%M} but {why}")
        _check_window_ends(tl, now, end, strips, flag)
        _check_run_lengths(tl, now, end, strips, flag)
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
                f"but the plan has the windows {'open' if open_ else 'closed'} then{_close_after(tl, at)}",
            )
        if fan_mode != "on" and e.get("fan_active") and not open_:
            flag(
                "activity.fan",
                f"predicted_activity says fan_active at {at:%a %H:%M} but the plan has the windows closed then"
                f"{_close_after(tl, at)}",
            )
    return list(problems.values())


RUN_LENGTH_TOL_MIN = 1.0  # bars are the plan's per-step minutes; only rounding may differ


def _check_run_lengths(tl: Timeline, now: datetime, end: datetime, strips, flag) -> None:
    """Owner decision (Issue #1436): a Heating / Cooling bar is the run's expected MINUTES, inside the run's window,
    never the whole window. For each predicted run ahead, the bars drawn inside its window must add up to its minutes
    (a run already under way: no more than them)."""
    for r in list(getattr(tl, "runs", ()) or ()):
        row = "heating" if r.mode == "heat" else "cooling" if r.mode == "cool" else None
        if row is None or r.start >= end:
            continue
        total = 0.0
        for d in strips:
            if d.get("row") != row:
                continue
            try:
                a, b = datetime.fromisoformat(str(d["start"])), datetime.fromisoformat(str(d["end"]))
            except KeyError, TypeError, ValueError:
                continue
            if b <= r.start or a >= r.end:
                continue
            if a < r.start or b > r.end:
                flag(f"{row}.outside", f"{row.capitalize()} bar {a:%H:%M}-{b:%H:%M} runs outside its run window")
            total += (min(b, r.end) - max(a, r.start)).total_seconds() / 60.0
        expected = float(r.minutes)
        whole = r.start >= now and r.end <= end
        if (whole and abs(total - expected) > RUN_LENGTH_TOL_MIN) or (total > expected + RUN_LENGTH_TOL_MIN):
            flag(
                f"{row}.length",
                f"{row.capitalize()} bars in the {r.start:%a %H:%M}-{r.end:%H:%M} run add up to {total:.0f} min "
                f"but the plan expects about {expected:.0f} min",
            )


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
