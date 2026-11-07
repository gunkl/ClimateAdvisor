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

import logging
import math
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from .const import V4_PREDICT_HORIZON_HOURS

_LOGGER = logging.getLogger(__name__)

DEFAULT_STEP_MINUTES = 15  # predictor_v4.V4_STEP_MINUTES; used only when the engine does not export it


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
    if hasattr(p, "resolve_nodes"):
        primary, ordered = p.resolve_nodes(
            entries, primary=primary, sensors=list(sensors), aliases=tuple(primary_aliases), in_sleep=in_sleep, tz=tz
        )
        samples = p.samples_from_chart_log(
            entries,
            tz=tz,
            archive_snapshots=snapshots,
            primary=None if primary == getattr(p, "CA_NODE", "_ca") else primary,
            primary_aliases=tuple(primary_aliases),
            in_sleep=in_sleep,
        )
    else:
        samples = p.samples_from_chart_log(entries, tz=tz, archive_snapshots=snapshots)
        ordered = [primary, *(s for s in sensors if s != primary)]
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


def build_inputs(
    *,
    now: datetime,
    hourly: Sequence[tuple[datetime, float]],
    band: Sequence[tuple[datetime, float | None, float | None]],
    day_modes: Mapping[Any, str],
    windows: tuple[bool, Any, Any],
    horizon_hours: int = V4_PREDICT_HORIZON_HOURS,
    step_min: int = DEFAULT_STEP_MINUTES,
) -> list[Any]:
    """The planned future on the model grid: one ``V4Input`` every ``step_min`` minutes after ``now``.

    Same plan v3 follows (``_build_predicted_indoor_future``): the RAW hourly forecast (the engine corrects it itself),
    the target-band schedule for setpoints (heat setpoint = band floor on a heat day, cool setpoint = band ceiling on a
    cool day, none on an off day), windows open inside the classification's window span when windows are recommended,
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
    last_fc = pts[-1][0] + 1800
    inputs: list[Any] = []
    for k in range(1, int(horizon_hours * 60 / step_min) + 1):
        # Absolute (UTC) stepping: wall-clock "+ timedelta" on a local datetime repeats or skips a point across DST.
        ts = (now.astimezone(UTC) + timedelta(minutes=step_min * k)).astimezone(now.tzinfo)
        t = ts.timestamp()
        if t > last_fc:
            break
        mode = day_modes.get(ts.date(), "off")
        heat_sp = _interp(band_pts_lower, t) if mode == "heat" and band_pts_lower else None
        cool_sp = _interp(band_pts_upper, t) if mode == "cool" and band_pts_upper else None
        windows_open = bool(
            recommended and open_t is not None and close_t is not None and open_t <= ts.time() < close_t
        )
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
