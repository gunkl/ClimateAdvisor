"""Prediction engine v4 running in shadow (Issue #1230 R1): the pure, executor-safe halves of the wiring.

Occupant view: v4 runs beside today's prediction on the real house so its forecasts accumulate and can be compared
later. It changes nothing the occupant sees or feels: no decision, setpoint, Status number or chart line reads it, and
any error here is caught by the coordinator and only logged. The Status chart keeps drawing v3.

This module holds no state and touches no Home Assistant objects, so every function is safe to run on an executor
thread. The coordinator (``_v4_*`` methods) owns scheduling, the persisted model and the archive; the lane-A engine
(``predictor_v4``) is imported lazily through ``_predictor()`` so a missing or broken engine disables the shadow
instead of breaking setup.

* ``run_fit``    - chart log + forecast archive snapshots -> ``samples_from_chart_log`` -> ``fit`` -> model dict.
* ``run_cycle``  - advance the hidden thermal-mass node, then ``predict`` the next 24 h.
* ``build_inputs`` - the planned future (raw hourly forecast, the plan's setpoints, windows) on the model's grid.
* ``model_summary`` - what the Debug tab shows.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as dtime
from typing import Any

from .const import V4_PREDICT_HORIZON_HOURS

_LOGGER = logging.getLogger(__name__)

DEFAULT_STEP_MINUTES = 15  # predictor_v4.V4_STEP_MINUTES; used only when the engine does not export it
WINDOW_PLAN_FALLBACK_HOURS = 4.0  # windows recommended but no planned times known: open for this long from the anchor


def _predictor() -> Any:
    """The lane-A engine module, or ImportError. The single access point (tests replace it with a fake)."""
    from . import predictor_v4

    return predictor_v4


def predictor_available() -> bool:
    """True when the v4 engine imports; the shadow quietly does nothing otherwise."""
    try:
        _predictor()
    except Exception:  # noqa: BLE001 - a broken engine must disable the shadow, never setup
        return False
    return True


def step_minutes() -> int:
    try:
        return int(getattr(_predictor(), "V4_STEP_MINUTES", DEFAULT_STEP_MINUTES))
    except Exception:  # noqa: BLE001
        return DEFAULT_STEP_MINUTES


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def build_samples(
    p: Any,
    entries: Sequence[Mapping[str, Any]],
    *,
    snapshots: Sequence[dict] | None,
    tz: tzinfo,
    primary: str,
    sensors: Sequence[str],
    aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
) -> tuple[list[Any], str, list[str]]:
    """``(samples, primary, ordered nodes)`` from chart rows: the ONE rule the live fit, the backtest and the nightly
    night-strategy replay share (``resolve_nodes`` chooses the nodes; rows older than the entity primary map onto it).
    Blocking CPU: executor only."""
    if hasattr(p, "resolve_nodes"):
        primary, ordered = p.resolve_nodes(
            entries, primary=primary, sensors=list(sensors), aliases=tuple(aliases), in_sleep=in_sleep, tz=tz
        )
        samples = p.samples_from_chart_log(
            entries,
            tz=tz,
            archive_snapshots=snapshots,
            primary=None if primary == getattr(p, "CA_NODE", "_ca") else primary,
            primary_aliases=tuple(aliases),
            in_sleep=in_sleep,
        )
    else:
        samples = p.samples_from_chart_log(entries, tz=tz, archive_snapshots=snapshots)
        ordered = [primary, *(s for s in sensors if s != primary)]
    return samples, primary, list(ordered)


def run_fit(
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    snapshots_fn: Callable[[datetime, datetime], list[dict]] | None,
    tz: tzinfo,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    previous_model: Mapping[str, Any] | None = None,
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    train_days: float | None = None,
) -> tuple[dict, float, str]:
    """Fit a model from the retained history. Blocking and CPU-bound: run via the executor only.

    Returns ``(model_dict, seconds, summary_line)``. The previous stored model warm-starts the refit.
    Raises on any failure; the caller keeps the previous model.

    The nodes are chosen by ``predictor_v4.resolve_nodes`` - the same rule the backtest uses (Issue #1230 R1b, M1):
    the entity ``primary`` is kept when it reads on 80 % of the history (older rows are mapped onto it through
    ``primary_aliases`` / ``indoor_primary`` / ``indoor`` outside the sleep window ``in_sleep``), else the model is
    the ``_ca`` reading alone.

    ``train_days`` bounds the training window to the last N days before ``now`` (rows and forecast snapshots): the
    same rule as the backtest's ``--train-days`` (Issue #1230 R1b). Months-old rows describe a different season, a
    different sensor era and a different thermostat behaviour, and contaminated the all-history fit (the fan rate
    ``a_f`` came out at 0.014/h on Zone 1). ``None`` = every retained row.
    """
    p = _predictor()
    t0 = time.perf_counter()
    snapshots: list[dict] = []
    if snapshots_fn is not None:
        try:
            snapshots = snapshots_fn(now - timedelta(days=(train_days or 400) + 1), now + timedelta(hours=1))
        except Exception:  # noqa: BLE001 - a bad forecast archive degrades the fit to chart-log-only, not to no fit
            _LOGGER.warning("v4 fit: forecast archive unreadable; fitting from the chart log alone", exc_info=True)
            snapshots = []
    entries = list(chart_entries)
    if train_days:
        entries = rows_in_window(entries, now, train_days)
    samples, primary, ordered = build_samples(
        p,
        entries,
        snapshots=snapshots,
        tz=tz,
        primary=primary,
        sensors=sensors,
        aliases=tuple(primary_aliases),
        in_sleep=in_sleep,
    )
    warm = p.from_dict(dict(previous_model)) if previous_model else None
    model = p.fit(samples, primary=primary, sensors=ordered, tz=tz, now=now, warm_start=warm)
    line = p.summary_line(model) if hasattr(p, "summary_line") else ""
    return p.to_dict(model), time.perf_counter() - t0, line


# ---------------------------------------------------------------------------
# Per-cycle prediction
# ---------------------------------------------------------------------------


def _interp(points: Sequence[tuple[float, float]], t: float) -> float:
    """Linear interpolation over ascending ``(t, value)``; clamped to the first/last value outside the range."""
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (t0, v0), (t1, v1) = points[lo], points[hi]
    return v0 if t1 <= t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def rows_in_window(entries: Sequence[Mapping[str, Any]], now: datetime, days: float) -> list[Mapping[str, Any]]:
    """The chart rows from the last ``days`` days up to ``now`` (rows with an unreadable ``ts`` are dropped)."""
    lo = now.timestamp() - days * 86400.0
    out = []
    for e in entries:
        ts = _parse_row_ts(e.get("ts")) if isinstance(e, Mapping) else None
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        if lo <= ts.timestamp() <= now.timestamp() + 1.0:
            out.append(e)
    return out


def fit_offset_s(entry_id: str, span_s: int = 1800) -> int:
    """Stable per-zone offset in ``[0, span_s)`` seconds added to the nightly fit time, so zones do not all fit at
    03:15:00. A pure hash of the entry id: the same zone always fits at the same minute, restarts included."""
    return zlib.crc32(str(entry_id).encode("utf-8")) % max(1, span_s)


def run_state_from_rows(rows: Sequence[Mapping[str, Any]], now: datetime) -> tuple[str | None, float | None]:
    """``(action, minutes)`` of the heating / cooling run in progress at ``now`` from the newest chart rows, else
    ``(None, None)``. ``rows`` is time-ordered; the run is the trailing stretch of rows with the same active action
    (a gap over 2 h ends it); its minutes are measured from the first such row (an estimate: the true start lies
    between that row and the one before)."""
    if not rows:
        return None, None
    action = str(rows[-1].get("hvac") or "").lower()
    if action not in ("heating", "cooling"):
        return None, None
    first_ts = None
    prev_ts = None
    for r in reversed(rows):
        if str(r.get("hvac") or "").lower() != action:
            break
        ts = _parse_row_ts(r.get("ts"))
        if ts is None or (prev_ts is not None and (prev_ts - ts).total_seconds() > 7200):
            break
        first_ts, prev_ts = ts, ts
    if first_ts is None:
        return action, 0.0
    return action, max(0.0, (now - first_ts).total_seconds() / 60.0)


def _parse_row_ts(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return dt


def _plan_setpoints_fallback(lo: float | None, hi: float | None, last: tuple[float | None, float | None]):
    """Used only when the engine module lacks ``plan_setpoints`` (a fake in tests): same rule."""
    return (lo if lo is not None else last[0], hi if hi is not None else last[1])


def planned_window_span(
    anchor: datetime,
    recommended: bool,
    open_t: dtime | None,
    close_t: dtime | None,
    *,
    fallback_hours: float = WINDOW_PLAN_FALLBACK_HOURS,
) -> tuple[datetime, datetime] | None:
    """The one window assumption v4 predicts under, live AND in the backtest (Issue #1230 R6 task 3).

    Occupant view: when Climate Advisor recommends opening the windows, the prediction shows the house cooling (or
    warming) only while the windows are planned to be open, from the time the engine planned, not for an arbitrary
    four hours from whenever the prediction was made.

    Rule: windows not recommended -> ``None``. Recommended with the engine's own planned ``open_t`` / ``close_t`` (the
    classification live; the day's record ``window_open_time`` / ``window_close_time`` in the backtest) -> that span on
    the ANCHOR's local day (a close at or before the open runs past midnight); a span already over at the anchor is
    ``None``. Recommended without planned times -> the documented fallback, ``fallback_hours`` from the anchor.
    The span covers one plan day only: a recommendation is today's, it is never repeated at the same clock time
    tomorrow.
    """
    if not recommended:
        return None
    if open_t is not None and close_t is not None:
        day = anchor.date()
        start = datetime.combine(day, open_t, tzinfo=anchor.tzinfo)
        end = datetime.combine(day, close_t, tzinfo=anchor.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        return (start, end) if end > anchor else None
    return (anchor, anchor + timedelta(hours=fallback_hours)) if fallback_hours > 0 else None


def window_open_at(span: tuple[datetime, datetime] | None, ts: datetime) -> bool:
    """True when ``ts`` is inside the planned window span (``[start, end)``)."""
    return span is not None and span[0] <= ts < span[1]


def build_inputs(
    *,
    now: datetime,
    hourly: Sequence[tuple[datetime, float]],
    band: Sequence[tuple[datetime, float | None, float | None]],
    day_modes: Mapping[Any, str] | None = None,
    windows: tuple[bool, Any, Any] = (False, None, None),
    horizon_hours: int = V4_PREDICT_HORIZON_HOURS,
    step_min: int = DEFAULT_STEP_MINUTES,
) -> list[Any]:
    """The planned future on the model grid: one ``V4Input`` every ``step_min`` minutes after ``now``.

    Same plan v3 follows (``_build_predicted_indoor_future``): the RAW hourly forecast (the engine corrects it itself),
    the target-band schedule for setpoints (BOTH edges at every step, whatever the day's mode: heat setpoint = band
    floor, cool setpoint = band ceiling - exactly the backtest's inputs, ``predictor_v4.plan_setpoints``; the emulated
    thermostat starts whichever side the room crosses), windows open inside the classification's window span when
    windows are recommended (``planned_window_span``: the same rule the backtest uses),
    fan off (v3 has no forecast fan schedule). The forecast is not extrapolated: the list stops half an hour after the
    last forecast hour. Returns [] when there is not enough forecast to predict from.
    """
    p = _predictor()
    pts = sorted((dt.timestamp(), float(temp)) for dt, temp in hourly if dt.tzinfo is not None)
    if len(pts) < 2:
        return []
    band_pts_lower = sorted((dt.timestamp(), float(lo)) for dt, lo, _hi in band if lo is not None)
    band_pts_upper = sorted((dt.timestamp(), float(hi)) for dt, _lo, hi in band if hi is not None)
    recommended, open_t, close_t = windows
    span = planned_window_span(now, bool(recommended), open_t, close_t)
    last_fc = pts[-1][0] + 1800
    inputs: list[Any] = []
    _plan_setpoints = getattr(p, "plan_setpoints", None) or _plan_setpoints_fallback
    last_sp: tuple[float | None, float | None] = (None, None)
    for k in range(1, int(horizon_hours * 60 / step_min) + 1):
        # Absolute (UTC) stepping: wall-clock "+ timedelta" on a local datetime repeats or skips a point across DST.
        ts = (now.astimezone(UTC) + timedelta(minutes=step_min * k)).astimezone(now.tzinfo)
        t = ts.timestamp()
        if t > last_fc:
            break
        heat_sp, cool_sp = _plan_setpoints(
            _interp(band_pts_lower, t) if band_pts_lower else None,
            _interp(band_pts_upper, t) if band_pts_upper else None,
            last_sp,
        )
        last_sp = (heat_sp, cool_sp)
        windows_open = window_open_at(span, ts)
        inputs.append(
            p.V4Input(
                ts=ts,
                outdoor_f=_interp(pts, t),
                heat_sp_f=heat_sp,
                cool_sp_f=cool_sp,
                windows_open=windows_open,
                fan_running=False,
            )
        )
    return inputs


def run_cycle(
    *,
    model_dict: Mapping[str, Any],
    now: datetime,
    tz: tzinfo,
    readings: Mapping[str, float | None],
    mass_f: float | None,
    mass_steps: int,
    sample_fields: Mapping[str, Any],
    inputs: Sequence[Any],
    outdoor_now_f: float | None = None,
    forecast_now_f: float | None = None,
    hvac_action_now: str | None = None,
    run_elapsed_min: float | None = None,
    ca_sleep_node: str | None = None,
    ca_sleep_time: str | None = None,
    ca_wake_time: str | None = None,
) -> tuple[dict[str, list[tuple[datetime, float]]] | None, float | None]:
    """Advance the hidden node ``mass_steps`` grid steps, then predict. Blocking CPU: run via the executor.

    Returns ``(predictions, mass_f)``. ``predictions`` is ``None`` when the confidence is ``none`` or the primary
    reading is missing (the engine's own fallback signal); ``mass_f`` is then returned unchanged.
    ``hvac_action_now`` / ``run_elapsed_min`` carry a heating or cooling run already in progress into the thermostat
    emulation; ``ca_sleep_node`` (+ the sleep window) makes the result carry the ``_ca`` composite line.
    """
    p = _predictor()
    model = p.from_dict(dict(model_dict))
    if model is None:
        raise ValueError("stored v4 model is not readable (unknown schema or malformed)")
    step = timedelta(minutes=step_minutes())
    mass = mass_f
    for i in range(mass_steps):
        ts = (now.astimezone(UTC) - step * (mass_steps - 1 - i)).astimezone(now.tzinfo)
        sample = p.V4Sample(ts=ts, readings=dict(readings), **sample_fields)
        stepped = p.step_mass(model, mass_f=mass, readings=dict(readings), sample=sample)
        mass = stepped if math.isfinite(stepped) else mass  # a missing reading leaves NaN: keep what we had
    if not inputs:
        return None, mass  # no forecast to predict from: nothing to compute (the node was still advanced)
    extra: dict[str, Any] = {}
    if hvac_action_now:
        extra.update(hvac_action_now=hvac_action_now, run_elapsed_min=run_elapsed_min)
    if ca_sleep_node:
        extra.update(ca_sleep_node=ca_sleep_node, ca_sleep_time=ca_sleep_time, ca_wake_time=ca_wake_time)
    predictions = p.predict(
        model,
        now=now,
        readings=dict(readings),
        mass_f=mass,
        inputs=list(inputs),
        tz=tz,
        outdoor_now_f=outdoor_now_f,
        forecast_now_f=forecast_now_f,
        **extra,
    )
    return predictions, mass


def corrected_forecast(
    *,
    model_dict: Mapping[str, Any],
    times: Sequence[datetime],
    raw_f: Sequence[float],
    tz: tzinfo,
    now: datetime | None = None,
    outdoor_now_f: float | None = None,
    forecast_now_f: float | None = None,
) -> list[tuple[datetime, float]]:
    """``(time, degrees F)`` of the weather service's outdoor values after v4's learned correction (display only).

    Used by the Status chart's "corrected forecast" line (Issue #1230 R2). ``now`` given -> a future line (the current
    miss decays over the nowcast time); omitted -> a past line (hourly bias only). Pure CPU, no I/O.
    """
    if not times:
        return []
    p = _predictor()
    model = p.from_dict(dict(model_dict))
    if model is None:
        raise ValueError("stored v4 model is not readable (unknown schema or malformed)")
    vals = p.corrected_forecast(
        model,
        times=list(times),
        raw_f=list(raw_f),
        tz=tz,
        now=now,
        outdoor_now_f=outdoor_now_f,
        forecast_now_f=forecast_now_f,
    )
    return [(t, v) for t, v in zip(times, vals, strict=True) if math.isfinite(v)]


def chart_extras(
    *,
    model_dict: Mapping[str, Any],
    now: datetime,
    tz: tzinfo,
    readings: Mapping[str, float | None],
    mass_f: float | None,
    inputs: Sequence[Any],
    outdoor_now_f: float | None = None,
    forecast_now_f: float | None = None,
) -> tuple[list[tuple[datetime, float]], dict[str, list[tuple[datetime, float]]]]:
    """The two extra future lines the Status chart draws: ``(corrected_outdoor, baseline_by_sensor)`` (Issue #1230 R2).

    * corrected outdoor: the service's hourly outdoor values after v4's learned correction;
    * baseline: the same predict with no thermostat setpoints ("the house with no HVAC"), windows as planned.

    Display only. Each half fails on its own into an empty result, so one broken line never hides the other. Blocking
    CPU (about 1 ms): run via the executor.
    """
    corrected: list[tuple[datetime, float]] = []
    baseline: dict[str, list[tuple[datetime, float]]] = {}
    if not inputs:
        return corrected, baseline
    try:
        corrected = corrected_forecast(
            model_dict=model_dict,
            times=[i.ts for i in inputs],
            raw_f=[i.outdoor_f for i in inputs],
            tz=tz,
            now=now,
            outdoor_now_f=outdoor_now_f,
            forecast_now_f=forecast_now_f,
        )
    except Exception:  # noqa: BLE001 - optional line
        _LOGGER.debug("v4 chart extras: corrected forecast failed", exc_info=True)
    try:
        p = _predictor()
        model = p.from_dict(dict(model_dict))
        if model is not None:
            plain = [dataclasses.replace(i, heat_sp_f=None, cool_sp_f=None) for i in inputs]
            out = p.predict(
                model,
                now=now,
                readings=dict(readings),
                mass_f=mass_f,
                inputs=plain,
                tz=tz,
                outdoor_now_f=outdoor_now_f,
                forecast_now_f=forecast_now_f,
            )
            baseline = {sid: list(pts) for sid, pts in (out or {}).items()}
    except Exception:  # noqa: BLE001 - optional line
        _LOGGER.debug("v4 chart extras: baseline failed", exc_info=True)
    return corrected, baseline


# ---------------------------------------------------------------------------
# Debug visibility
# ---------------------------------------------------------------------------


def model_summary(model_dict: Mapping[str, Any] | None) -> dict[str, Any]:
    """Confidence, fitted_at, span and per-lead training MAE of a stored model, in internal degrees F.

    Reads the persisted dict directly (``predictor_v4.to_dict``'s shape) so the Debug tab works even when the engine
    module is unavailable. Missing keys are None, never an error.
    """
    if not isinstance(model_dict, Mapping):
        return {"has_model": False, "confidence": "none"}
    fit = model_dict.get("fit")
    fit = fit if isinstance(fit, Mapping) else {}
    mae = fit.get("mae_by_lead")
    return {
        "has_model": True,
        "confidence": model_dict.get("confidence", "none"),
        "fitted_at": model_dict.get("fitted_at"),
        "span_days": fit.get("span_days"),
        "n_anchors": fit.get("n_anchors"),
        "mae_by_lead": {str(k): v for k, v in mae.items()} if isinstance(mae, Mapping) else {},
        "primary": model_dict.get("primary"),
        "sensors": list(model_dict.get("sensors") or []),
        "schema_version": model_dict.get("schema_version"),
    }


# ---------------------------------------------------------------------------
# Tonight's bedroom strategy (Issue #1230 R6): the pure input builders
# ---------------------------------------------------------------------------

NIGHT_PLAN_LEAD_H = 6.0  # the evening plan appears this long before bedtime (and stays until wake time)
NIGHT_MIN_HOURS = 2.0  # a night shorter than this (late bedtime, almost wake time) is not planned


def night_window(now: datetime, sleep_time: dtime, wake_time: dtime) -> tuple[datetime, datetime] | None:
    """``(start, end)`` of the night in view, or None outside the planning window.

    Occupant view: from early evening until you get up, "Tonight in the bedroom" describes the coming (or current)
    night: bedtime to wake time, starting NOW when bedtime has already passed. Outside that (daytime) there is no
    night plan, the card says so rather than showing last night's.

    ``start`` = the later of bedtime and ``now``; ``end`` = the next wake time. A bedtime after midnight (sleep time
    earlier than wake time on the clock) is handled. DST-safe: wall-clock combine on the local zone.
    """
    tz = now.tzinfo
    today = now.date()

    def at(day: Any, t: dtime) -> datetime:
        return datetime.combine(day, t, tzinfo=tz)

    one = timedelta(days=1)
    if sleep_time > wake_time:  # the usual evening bedtime
        if now.time() < wake_time:  # after midnight, before wake: last evening's night is in progress
            bed, end = at(today - one, sleep_time), at(today, wake_time)
        else:
            bed, end = at(today, sleep_time), at(today + one, wake_time)
    else:  # bedtime after midnight (sleep_time <= wake_time on the clock)
        if now.time() < wake_time:
            bed, end = at(today, sleep_time), at(today, wake_time)
        else:
            bed, end = at(today + one, sleep_time), at(today + one, wake_time)
    if now < bed - timedelta(hours=NIGHT_PLAN_LEAD_H) or now >= end:
        return None
    start = max(bed, now)
    if (end - start) < timedelta(hours=NIGHT_MIN_HOURS):
        return None
    return start, end


def _at_time(series: Sequence[tuple[datetime, float]] | None, ts: datetime) -> float | None:
    """Linear interpolation of a ``(time, value)`` series at ``ts`` (None when the series is empty or ``ts`` is more
    than 45 minutes outside it)."""
    pts = sorted((t.timestamp(), float(v)) for t, v in (series or ()) if math.isfinite(float(v)))
    if not pts:
        return None
    x = ts.timestamp()
    if x < pts[0][0] - 2700 or x > pts[-1][0] + 2700:
        return None
    return _interp(pts, x)


def _mass_at_bedtime(
    p4: Any,
    model: Any,
    mass_f: float | None,
    primary_path: Sequence[tuple[datetime, float]] | None,
    now: datetime,
    start: datetime,
) -> float | None:
    """The hidden-mass node at bedtime: the live state relaxed along v4's predicted primary path from now to
    ``start`` (the same filter the live shadow runs every cycle, so the plan starts from the state a running house would
    have, the one the nightly replay validates). None -> seed the node at the bedtime reading (no live state)."""
    if mass_f is None or not math.isfinite(mass_f) or not primary_path:
        return None
    mass = float(mass_f)
    for ts, value in sorted(primary_path, key=lambda p: p[0]):
        if ts <= now:
            continue
        if ts > start:
            break
        mass = p4.step_mass(model, mass_f=mass, readings={model.primary: float(value)})
    return mass


def night_inputs(
    *,
    model_dict: Mapping[str, Any],
    start: datetime,
    end: datetime,
    now: datetime,
    fan_mode: str,
    corrected_outdoor: Sequence[tuple[datetime, float]],
    predictions: Mapping[str, Sequence[tuple[datetime, float]]] | None,
    current_readings: Mapping[str, float | None],
    floor_f: float,
    ceiling_f: float,
    target_sensor: str | None,
    evidence: Any = None,
    validation: Any = None,
    can_heat: bool = True,
    can_cool: bool = True,
    swing_heat_f: float | None = None,
    swing_cool_f: float | None = None,
    mass_f: float | None = None,
    path: Sequence[tuple[datetime, float]] | None = None,
    unit: str = "fahrenheit",
) -> Any:
    """``night_strategy.NightInputs`` from the live state, or None when the night cannot be planned.

    The state at ``start``: when bedtime is still ahead, each model sensor's value is the PLAN's predicted reading at
    bedtime (``predictions``: v4's per-sensor path under the same plan the timeline follows); the primary sensor falls
    back to the timeline's ``_ca`` ``path`` and any other sensor to its current reading. When the night is already
    under way (``start`` == ``now``) it is the live reading. ``corrected_outdoor`` is tonight's forecast after v4's
    learned correction. Pure: no clock, no I/O.
    """
    from . import night_strategy as ns  # noqa: PLC0415 - lazy: a broken engine disables the plan, not setup
    from . import predictor_v4 as p4  # noqa: PLC0415

    model = p4.from_dict(dict(model_dict))
    if model is None or model.confidence == "none":
        return None
    outdoor = [(t, v) for t, v in corrected_outdoor if start - timedelta(hours=2) <= t <= end + timedelta(hours=2)]
    if len(outdoor) < 2:
        return None
    ahead = start > now + timedelta(minutes=5)
    mass = _mass_at_bedtime(p4, model, mass_f, (predictions or {}).get(model.primary), now, start) if ahead else mass_f
    readings: dict[str, float] = {}
    for sid in model.sensors:
        value: float | None = None
        if ahead:
            value = _at_time((predictions or {}).get(sid), start)
            if value is None and sid == model.primary:
                value = _at_time(path, start)
        if value is None:
            raw = current_readings.get(sid)
            if sid == model.primary and raw is None:
                raw = current_readings.get(p4.CA_NODE)
            value = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else None
        if value is not None:
            readings[sid] = value
    if model.primary not in readings:
        return None
    return ns.NightInputs(
        model=model,
        start=start,
        end=end,
        tz=now.tzinfo or UTC,
        fan_mode=fan_mode,
        outdoor=outdoor,
        readings=readings,
        floor_f=floor_f,
        ceiling_f=ceiling_f,
        target_sensor=target_sensor,
        mass_f=mass,
        swing_heat_f=swing_heat_f if swing_heat_f else ns.THERMAL_SWING_DEFAULT_F,
        swing_cool_f=swing_cool_f if swing_cool_f else ns.THERMAL_SWING_DEFAULT_F,
        can_heat=can_heat,
        can_cool=can_cool,
        evidence=evidence,
        validation=validation,
        unit=unit,
    )
