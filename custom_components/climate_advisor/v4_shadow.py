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

from .const import V4_PREDICT_HORIZON_HOURS, V4_UNUSABLE_WARN_REFITS
from .window_span import (  # noqa: F401 - the shared rule lives in the engine-neutral module; re-exported here
    WINDOW_PLAN_FALLBACK_HOURS,
    planned_window_span,
    planned_window_spans,
    window_open_at,
)

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
    sleep_sensor: str | None = None,
    climate_entity: str | None = None,
) -> tuple[dict, float, str]:
    """Fit a model from the retained history. Blocking and CPU-bound: run via the executor only.

    ``climate_entity`` (the configured thermostat, Issue #1525): stored in ``fit["climate_entity"]``; a previous model
    fitted for a DIFFERENT thermostat is neither a warm start nor a carry source (``same_equipment``): another HVAC's
    gains, offsets and grades are not evidence about this one, so the fit starts like a home that never saw a mode.

    Returns ``(model_dict, seconds, summary_line)``. The previous stored model warm-starts the refit.
    Raises on any failure; the caller keeps the previous model.

    The model is chosen by ``predictor_v4.choose_control_model`` - the same rule and gate the backtest uses (Issue
    #1432): B (the entity ``primary`` plus the configured ``sleep_sensor`` as a node, composed by time of day) once
    both have 3+ days of their own readings (or the old whole-window coverage; older rows map onto the primary through
    ``primary_aliases`` / ``indoor_primary`` / ``indoor`` outside the sleep window ``in_sleep``) AND its ``_ca`` replay
    is no worse than A's; else A, the ``_ca`` reading alone. ``sleep_sensor`` None = no separate sleep sensor. The
    record is ``model_dict["fit"]["node_choice"]``.

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
    entity_primary = primary
    warm = p.from_dict(dict(previous_model)) if previous_model else None
    if warm is not None and climate_entity:
        resolve = getattr(p, "carry_source", None)
        src = resolve(warm) if resolve is not None else warm
        if not (same_equipment(p.to_dict(warm), climate_entity) and same_equipment(p.to_dict(src), climate_entity)):
            _LOGGER.info(
                "v4 fit: the stored model was fitted for another thermostat; fitting from this one's history alone"
            )
            warm = None
    choose = getattr(p, "choose_control_model", None)
    if choose is not None:
        # Issue #1432: A (stitched ``_ca``) or B (a node per room), the rule and backtest gate the backtest shares.
        chosen = choose(
            entries,
            tz=tz,
            now=now,
            primary=primary,
            sensors=list(sensors),
            sleep_sensor=sleep_sensor,
            aliases=tuple(primary_aliases),
            in_sleep=in_sleep,
            archive_snapshots=snapshots,
            warm_start=warm,
        )
        model, samples, primary, ordered = chosen.model, chosen.samples, chosen.primary, chosen.nodes
        # Issue #1422: sensors with 3+ days of their OWN history become display-only nodes (chart lines, never control)
        display = display_candidates(
            p, entries, primary=primary, ordered=ordered, entity_primary=entity_primary, sensors=sensors, tz=tz
        )
        if display and model.confidence != "none":
            model = p.with_display_nodes(
                model,
                samples,
                display_sensors=display,
                tz=tz,
                warm=warm.display_nodes if warm is not None else None,
            )
    else:
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
        display = display_candidates(
            p, entries, primary=primary, ordered=ordered, entity_primary=entity_primary, sensors=sensors, tz=tz
        )
        extra: dict[str, Any] = {"display_sensors": display} if display else {}
        model = p.fit(samples, primary=primary, sensors=ordered, tz=tz, now=now, warm_start=warm, **extra)
    line = p.summary_line(model) if hasattr(p, "summary_line") else ""
    out = p.to_dict(model)
    if climate_entity and isinstance(out.get("fit"), dict):
        out["fit"]["climate_entity"] = climate_entity  # Issue #1525: which HVAC this model learned
    return out, time.perf_counter() - t0, line


def model_climate_entity(model_dict: Mapping[str, Any] | None) -> str | None:
    """The thermostat a stored model learned (Issue #1525): ``fit["climate_entity"]``, else the one ``climate.*``
    sensor among its ``sensors`` (the thermostat's own reading as a node), else ``None`` (unknown)."""
    if not isinstance(model_dict, Mapping):
        return None
    fit = model_dict.get("fit")
    rec = fit.get("climate_entity") if isinstance(fit, Mapping) else None
    if isinstance(rec, str) and rec:
        return rec
    climates = [s for s in (model_dict.get("sensors") or ()) if isinstance(s, str) and s.startswith("climate.")]
    return climates[0] if len(climates) == 1 else None


def same_equipment(model_dict: Mapping[str, Any] | None, climate_entity: str | None) -> bool:
    """False only when the model provably learned another thermostat; unknown (an older model, no configured entity)
    counts as the same, so nothing jumps at the upgrade (Issue #1525). A new sensor on the same HVAC is the same."""
    known = model_climate_entity(model_dict)
    return not (known and climate_entity) or known == climate_entity


# ---------------------------------------------------------------------------
# An unusable refit never replaces a usable model (Issue #1529)
# ---------------------------------------------------------------------------


def model_usable(model_dict: Mapping[str, Any] | None) -> bool:
    """A stored model the decisions can use: a mapping whose confidence is not ``none``."""
    return isinstance(model_dict, Mapping) and model_dict.get("confidence", "none") != "none"


def keep_previous_model(
    previous: Mapping[str, Any] | None,
    new: Mapping[str, Any] | None,
    primaries: Sequence[str] | None = None,
    climate_entity: str | None = None,
) -> bool:
    """True when ``new`` (a refit) is unusable and ``previous`` is usable: the previous model stays in use.

    ``primaries`` (the primary nodes the current configuration can fit: ``"_ca"`` and the configured primary entity):
    a previous model on any other primary was fitted for a sensor the zone no longer reads (a reconfiguration), so it
    is not kept; the unusable refit is stored as before and the next usable fit carries what the old model learned
    across the node change (``predictor_v4`` season carry: gains, offsets and grades are the HVAC's and the house's,
    not the sensor's).

    Occupant view: one night with too little data (a sensor outage, a gap in the chart log) or a calm week where the
    refit cannot beat "no change" used to replace the learned model, so pre-heat, the time-of-use bank, setback depth
    and the ceiling guard fell back to the "not learned yet" defaults until a usable fit came back. An unusable fit is
    not evidence against the model (owner rule, D35: learned values give way only to new measured evidence). A home
    that never had a usable model stores the refit as before."""
    if not (model_usable(previous) and not model_usable(new)):
        return False
    if climate_entity and not same_equipment(previous, climate_entity):
        return False  # learned for another thermostat: not kept for this one (Issue #1525)
    return primaries is None or (isinstance(previous, Mapping) and previous.get("primary") in tuple(primaries))


def unusable_fit_record(
    new: Mapping[str, Any],
    *,
    at: str,
    reason: str,
    kept: Mapping[str, Any],
    previous_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The diagnostic record of an unusable refit kept beside the model in use (``V4ModelStore.unusable_fit``);
    ``count`` = unusable refits in a row (``previous_record``'s count + 1)."""
    fit = new.get("fit") if isinstance(new.get("fit"), Mapping) else {}
    prev_n = previous_record.get("count") if isinstance(previous_record, Mapping) else None
    return {
        "count": (prev_n if isinstance(prev_n, int) and prev_n > 0 else 0) + 1,
        "at": at,
        "reason": reason,
        "fit_reason": str(fit.get("reason") or "confidence none"),
        "fitted_at": new.get("fitted_at"),
        "kept_fitted_at": kept.get("fitted_at"),
    }


def describe_unusable_fit(rec: Mapping[str, Any] | None) -> str:
    """ "last refit unusable (<reason>); keeping the model from <date>" for the log and the Debug tab; "" when none."""
    if not isinstance(rec, Mapping):
        return ""
    kept = str(rec.get("kept_fitted_at") or "")[:10] or "an earlier fit"
    n = rec.get("count") if isinstance(rec.get("count"), int) else 1
    head = "last refit unusable" if n <= 1 else f"last {n} refits unusable"
    return f"{head} ({rec.get('fit_reason') or 'confidence none'}); keeping the model from {kept}"


def unusable_fit_notice(rec: Mapping[str, Any] | None) -> str:
    """The occupant-facing sentence once V4_UNUSABLE_WARN_REFITS refits in a row were unusable; "" before that."""
    if not isinstance(rec, Mapping) or not isinstance(rec.get("count"), int) or rec["count"] < V4_UNUSABLE_WARN_REFITS:
        return ""
    kept = str(rec.get("kept_fitted_at") or "")[:10] or "an earlier fit"
    return (
        f"v4 could not refit your home the last {rec['count']} times ({rec.get('fit_reason') or 'confidence none'}); "
        f"pre-heat, pre-cool and the other decisions still use the model from {kept}"
    )


# ---------------------------------------------------------------------------
# Which model the decisions read, in words (Issue #1432)
# ---------------------------------------------------------------------------

MODEL_STITCHED = "stitched"  # predictor_v4.MODEL_STITCHED (A: the stitched Climate Advisor reading)
MODEL_PER_ROOM = "per_room"  # predictor_v4.MODEL_PER_ROOM (B: a node per room, composed by time of day)


def node_choice(model_dict: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The stored model's node-choice record (``fit["node_choice"]``); a model written before Issue #1432 has none and
    reads as ``{"model": ...}`` inferred from its primary (``_ca`` -> stitched, an entity -> per room). ``None``: no
    model."""
    if not isinstance(model_dict, Mapping):
        return None
    fit = model_dict.get("fit")
    rec = fit.get("node_choice") if isinstance(fit, Mapping) else None
    if isinstance(rec, Mapping) and rec.get("model") in (MODEL_STITCHED, MODEL_PER_ROOM):
        return dict(rec)
    primary = model_dict.get("primary")
    if not isinstance(primary, str) or not primary:
        return None
    return {"model": MODEL_STITCHED if primary == "_ca" else MODEL_PER_ROOM, "reason": "inferred"}


def node_choice_changed(previous: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> bool:
    """True when the model the decisions read changed kind between two stored models (first fit: False)."""
    a, b = node_choice(previous), node_choice(new)
    return a is not None and b is not None and a["model"] != b["model"]


_MODE_WORD = {"heat": "heating", "cool": "cooling"}


def describe_grade(summary: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(why, by_mode)`` in plain words for the Debug tab: why v4 has its grade, and each mode's thermostat starts.

    ``summary`` is :func:`model_summary`'s dict. v4 is ``ok`` only when BOTH heating and cooling have enough usable
    thermostat starts in the training window (``predictor_v4.fit``'s gates); this says which input holds it back."""
    if not isinstance(summary, Mapping) or not summary.get("has_model"):
        return "", ""
    try:
        pv = _predictor()
        need_starts, need_days = int(pv.GATE_MIN_ACCEPTED_STARTS), float(pv.GATE_MIN_DAYS_OK)
    except Exception:  # noqa: BLE001 - the words only; the engine's own gates are the source of truth
        need_starts, need_days = 5, 10.0
    modes = summary.get("modes") if isinstance(summary.get("modes"), Mapping) else {}

    def mode_txt(mode: str) -> str:
        m = modes.get(mode) if isinstance(modes.get(mode), Mapping) else {}
        word, acc, tot, grade = _MODE_WORD[mode], m.get("accepted"), m.get("starts"), m.get("grade")
        if m.get("carried_grade"):
            # Issue #1525: what the house learned about this mode stands until new runs of it displace it
            new = "not seen" if m.get("seen") is False else f"{acc if isinstance(acc, int) else 0} usable new starts"
            return f"{word} {grade}: {m.get('reason')} ({new})"
        if grade == "unseen" or m.get("seen") is False or (not tot and m.get("seen") is None):
            return f"{word} not seen"
        if not isinstance(acc, int):
            return f"{word}: no start count"
        enough = "enough" if acc >= need_starts else f"needs {need_starts}"
        starts = f"{acc} usable of {tot} starts ({enough})"
        if grade in ("ok", "low", "none"):
            why = m.get("reason") or ""
            extra = f"; {why}" if grade == "low" and why and "starts" not in why else ""
            return f"{word} {grade}: {starts}{extra}"
        return f"{word} {starts}"

    span = summary.get("span_days")
    window = f" in the last {round(float(span))} days" if isinstance(span, (int, float)) else ""
    by_mode = " · ".join(mode_txt(m) for m in ("heat", "cool")) + window if modes else ""
    days = summary.get("sample_days")
    days_w = f"{float(days):.1f} days" if isinstance(days, (int, float)) else "too few days"
    code = summary.get("confidence_reason") or ""
    carried_reason = next(
        (
            str(modes[m].get("reason"))
            for m in ("heat", "cool")
            if isinstance(modes.get(m), Mapping) and modes[m].get("carried_grade")
        ),
        "",
    )
    carried = [
        _MODE_WORD[m] for m in ("heat", "cool") if isinstance(modes.get(m), Mapping) and modes[m].get("carried_grade")
    ]
    short = [
        _MODE_WORD[m]
        for m in ("heat", "cool")
        if isinstance(modes.get(m), Mapping)
        and (modes[m].get("accepted") or 0) < need_starts
        and _MODE_WORD[m] not in carried
    ]
    unseen = [
        _MODE_WORD[m]
        for m in ("heat", "cool")
        if isinstance(modes.get(m), Mapping) and not modes[m].get("seen") and not modes[m].get("carried")
    ]
    why = {
        "": ("every check passes" + (f"; {' and '.join(carried)} {carried_reason}" if carried else ""))
        if summary.get("confidence") == "ok"
        else "",
        "under 3 days of samples": f"only {days_w} of history (needs 3)",
        "fit diverged": "the last fit did not settle on a model",
        "does not beat no-change at 4 h by 10 %": 'it does not yet beat "no change" by 10 % at 4 h ahead',
        "under 10 days of samples": f"only {days_w} of history (needs {need_days:g} for ok)",
        "heating or cooling not yet observed": f"{' and '.join(unseen) or 'one mode'} not seen{window} "
        "(ok needs both heating and cooling)",
        "too few accepted thermostat starts in a used mode": f"too few usable {' and '.join(short) or ''} starts "
        f"(ok needs {need_starts} in each mode it has seen)".replace("usable  starts", "usable starts"),
    }.get(code, code)
    return why, by_mode


def describe_node_choice(
    choice: Mapping[str, Any] | None,
    *,
    fmt_delta: Callable[[float], str] = lambda f: f"{f:.2f}°F",
    names: Mapping[str, str] | None = None,
    changed: bool = False,
) -> tuple[str, str]:
    """``(headline, reason)`` in plain words for the INFO line, the Activity row and the Debug tab (Issue #1432).

    ``fmt_delta`` formats a temperature DIFFERENCE given in degrees F in the user's unit (``format_temp_delta``);
    ``names`` maps entity ids to friendly names. ``changed`` words the headline as a change ("now forecasts")."""
    if not isinstance(choice, Mapping):
        return "", ""
    names = names or {}
    now_w = "now " if changed else ""
    prim, sleep = choice.get("primary"), choice.get("sleep")
    p_name = names.get(prim, prim) if isinstance(prim, str) else "the thermostat"
    s_name = names.get(sleep, sleep) if isinstance(sleep, str) else "the sleep sensor"
    per_room = choice.get("model") == MODEL_PER_ROOM
    if per_room:
        headline = (
            f"v4 {now_w}forecasts each room separately (thermostat by day, bedroom at night)"
            if sleep
            else f"v4 {now_w}forecasts the thermostat's room from its own readings"
        )
    else:
        headline = f"v4 {now_w}forecasts from the stitched Climate Advisor reading (one model for every room)"

    def days(key: str) -> str:
        d = choice.get(f"{key}_days")
        return f"{float(d):.1f} days" if isinstance(d, (int, float)) else "no days"

    need = choice.get("min_days", 3.0)
    need_w = f"{float(need):g}" if isinstance(need, (int, float)) else "3"
    mae = (choice.get("scores") or {}).get("mae") or {}
    a, b = mae.get(MODEL_STITCHED) or {}, mae.get(MODEL_PER_ROOM) or {}
    lead = str(choice.get("lead") or "4h")

    def score_txt() -> str:
        if b.get(lead) is None or a.get(lead) is None:
            return ""
        who = "the per-room forecast" if sleep else f"{p_name}'s own forecast"
        main = f"{who} scored {fmt_delta(b[lead])} vs {fmt_delta(a[lead])} at {lead[:-1]} h"
        others = [
            f"{k[:-1]} h {fmt_delta(b[k])} vs {fmt_delta(a[k])}"
            for k in ("1h", "12h")
            if k != lead and a.get(k) is not None and b.get(k) is not None
        ]
        return main + (f" ({', '.join(others)})" if others else "")

    reason_code = choice.get("reason")
    rooms = "both rooms have" if sleep else f"{p_name} has"
    qual = f"{rooms} {need_w}+ days of readings ({p_name} {days('primary')}" + (
        f", {s_name} {days('sleep')})" if sleep else ")"
    )
    if reason_code == "per_room_no_worse":
        reason = f"{qual} and {score_txt()} on this home's history"
    elif reason_code == "stitched_not_usable":
        reason = f"{qual} and the stitched model has no usable fit"
    elif reason_code == "per_room_scored_worse":
        tol = choice.get("tolerance_f")
        tol_w = f" (it may be at most {fmt_delta(float(tol))} worse)" if isinstance(tol, (int, float)) else ""
        reason = f"{qual}, but {score_txt()} on this home's history{tol_w}"
    elif reason_code == "too_few_shared_anchors":
        n = (choice.get("scores") or {}).get("n_anchors")
        reason = f"{qual}, but there is not enough shared history yet to compare the two forecasts ({n} points)"
    elif reason_code == "per_room_not_usable":
        why = choice.get("per_room_fit_reason") or "no usable fit"
        reason = f"{qual}, but its own model could not be fitted ({why})"
    elif reason_code == "primary_not_ready":
        reason = f"{p_name} has only {days('primary')} of its own readings (needs {need_w})"
    elif reason_code == "sleep_not_ready":
        reason = (
            f"{s_name} has only {days('sleep')} of its own readings (needs {need_w}) for a model of its own; until "
            f"then the stitched reading (the bedroom at night) is forecast as one line, and sleep hours are never "
            f"forecast from {p_name}'s room"
        )
    elif reason_code == "no_primary_entity":
        reason = "no thermostat sensor to model on its own"
    else:
        reason = ""
    return headline, reason


def display_candidates(
    p: Any,
    entries: Sequence[Mapping[str, Any]],
    *,
    primary: str,
    ordered: Sequence[str],
    entity_primary: str,
    sensors: Sequence[str],
    tz: tzinfo,
) -> list[str]:
    """The display-only nodes for a fit on ``entries`` (Issue #1422, ``predictor_v4.resolve_display_nodes``): the
    sensors the control rule left out that have at least 3 days of their own history. ``[]`` without the helper.
    ``primary`` is the model's primary node (``_ca`` or the entity), ``entity_primary`` the configured entity."""
    fn = getattr(p, "resolve_display_nodes", None)
    if fn is None:
        return []
    nodes = [primary, *(s for s in ordered if s != primary)]
    return list(fn(entries, nodes=nodes, primary=entity_primary, sensors=list(sensors), tz=tz))


def run_display_fit(
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    tz: tzinfo,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    model_dict: Mapping[str, Any],
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    train_days: float | None = None,
) -> tuple[dict, float, str]:
    """Fold newly qualified display-only nodes into the stored model between nightly fits (Issue #1422, option b).

    Occupant view: a bedroom sensor that reaches 3 days of its own history at 10:00 gets its chart line within about
    6 h, not at the next 03:15 refit. Only the display nodes are fitted (house held: seconds on a Pi, not a full fit);
    every other key of ``model_dict`` - house, control nodes, thermostat, confidence, ``fitted_at`` - is copied through
    unchanged, so the decisions read the same model before and after. Blocking CPU: executor only. Raises on failure
    (the caller keeps the stored model)."""
    p = _predictor()
    t0 = time.perf_counter()
    model = p.from_dict(dict(model_dict))
    if model is None:
        raise ValueError("stored v4 model is not readable (unknown schema or malformed)")
    entries = list(chart_entries)
    if train_days:
        entries = rows_in_window(entries, now, train_days)
    display = display_candidates(
        p, entries, primary=model.primary, ordered=list(model.sensors), entity_primary=primary, sensors=sensors, tz=tz
    )
    samples = p.samples_from_chart_log(
        entries,
        tz=tz,
        primary=None if model.primary == getattr(p, "CA_NODE", "_ca") else model.primary,
        primary_aliases=tuple(primary_aliases),
        in_sleep=in_sleep,
    )
    refit = p.with_display_nodes(model, samples, display_sensors=display, tz=tz, warm=model.display_nodes)
    out = dict(model_dict)
    refit_dict = p.to_dict(refit)
    out["display_nodes"] = refit_dict["display_nodes"]
    fit = dict(out["fit"]) if isinstance(out.get("fit"), Mapping) else {}
    fit["display"] = refit_dict["fit"].get("display", {})
    out["fit"] = fit
    kept = list(out["display_nodes"])
    return out, time.perf_counter() - t0, f"v4 display nodes: kept={kept} tried={display}"


def display_pending(
    model_dict: Mapping[str, Any] | None,
    entries: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    tz: tzinfo,
    primary: str,
    sensors: Sequence[str],
    train_days: float | None = None,
) -> list[str]:
    """Sensors that qualify as display nodes now but the stored model has not tried yet (Issue #1422): the trigger for
    the off-cycle display refit. A sensor the last fit tried and rejected waits for the nightly refit, so a sensor that
    never passes the skill bar does not cost a fit every 6 h. Pure and cheap (one pass over the rows)."""
    if not isinstance(model_dict, Mapping) or model_dict.get("confidence", "none") == "none":
        return []
    p = _predictor()
    rows = rows_in_window(list(entries), now, train_days) if train_days else list(entries)
    model_sensors = [s for s in (model_dict.get("sensors") or []) if isinstance(s, str)]
    display = display_candidates(
        p,
        rows,
        primary=str(model_dict.get("primary")),
        ordered=model_sensors,
        entity_primary=primary,
        sensors=sensors,
        tz=tz,
    )
    fit = model_dict.get("fit")
    disp = fit.get("display") if isinstance(fit, Mapping) else None
    tried = set(disp.get("tried") or []) if isinstance(disp, Mapping) else set()
    shown = model_dict.get("display_nodes")
    tried |= set(shown) if isinstance(shown, Mapping) else set()
    return [s for s in display if s not in tried]


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


def build_inputs(
    *,
    now: datetime,
    hourly: Sequence[tuple[datetime, float]],
    band: Sequence[tuple[datetime, float | None, float | None]],
    day_modes: Mapping[Any, str] | None = None,
    windows: tuple[bool, Any, Any] = (False, None, None),
    tomorrow_windows: tuple[bool, Any, Any] | None = None,
    later_windows: Sequence[tuple[bool, Any, Any] | None] = (),
    horizon_hours: int = V4_PREDICT_HORIZON_HOURS,
    step_min: int = DEFAULT_STEP_MINUTES,
) -> list[Any]:
    """The planned future on the model grid: one ``V4Input`` every ``step_min`` minutes after ``now``.

    Same plan v3 follows (``_build_predicted_indoor_future``): the RAW hourly forecast (the engine corrects it itself),
    the target-band schedule for setpoints (BOTH edges at every step, whatever the day's mode: heat setpoint = band
    floor, cool setpoint = band ceiling - exactly the backtest's inputs, ``predictor_v4.plan_setpoints``; the emulated
    thermostat starts whichever side the room crosses), windows open inside the classification's window span when
    windows are recommended (``planned_window_span``: the same rule the backtest uses) and, for tomorrow's hours,
    inside tomorrow's own planned span when ``tomorrow_windows`` is given (Issue #1391; ``None`` = unknown = closed),
    and inside each later day's own span from ``later_windows`` (Issue #1483: the week-long display line),
    fan off (v3 has no forecast fan schedule). The forecast is not extrapolated: the list stops half an hour after the
    last forecast hour, or at ``horizon_hours``, whichever comes first. ``hourly`` may include the hours built from
    the daily highs and lows (the display cycle passes them; the decisions' cycle never does). Returns [] when there
    is not enough forecast to predict from.
    """
    p = _predictor()
    pts = sorted((dt.timestamp(), float(temp)) for dt, temp in hourly if dt.tzinfo is not None)
    if len(pts) < 2:
        return []
    band_pts_lower = sorted((dt.timestamp(), float(lo)) for dt, lo, _hi in band if lo is not None)
    band_pts_upper = sorted((dt.timestamp(), float(hi)) for dt, _lo, hi in band if hi is not None)
    recommended, open_t, close_t = windows
    span = planned_window_spans(now, (bool(recommended), open_t, close_t), tomorrow_windows, later=later_windows)
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
    baseline: bool = False,
    display: bool = False,
) -> tuple[dict[str, list[tuple[datetime, float]]] | None, float | None]:
    """Advance the hidden node ``mass_steps`` grid steps, then predict. Blocking CPU: run via the executor.

    ``baseline=True`` (Issue #1230 R6b) predicts the house with NO thermostat setpoints (windows as planned): "when does
    the house leave the band on its own?", the question the ceiling guard and the nat-vent plan ask. The plan path
    (setpoints at both band edges) cannot answer it: its emulated thermostat holds the room at the edge.

    Returns ``(predictions, mass_f)``. ``predictions`` is ``None`` when the confidence is ``none`` or the primary
    reading is missing (the engine's own fallback signal); ``mass_f`` is then returned unchanged.
    ``hvac_action_now`` / ``run_elapsed_min`` carry a heating or cooling run already in progress into the thermostat
    emulation; ``ca_sleep_node`` (+ the sleep window) makes the result carry the ``_ca`` composite line.
    ``display=True`` (the post-chart shadow cycle only, Issue #1422) adds the model's display-only node lines; the
    pre-decision control cycle never passes it, and every other line is identical either way.
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
    if baseline:
        inputs = [dataclasses.replace(i, heat_sp_f=None, cool_sp_f=None) for i in inputs]
        hvac_action_now = None  # no HVAC in the baseline: nothing is running
    if hvac_action_now:
        extra.update(hvac_action_now=hvac_action_now, run_elapsed_min=run_elapsed_min)
    if ca_sleep_node:
        extra.update(ca_sleep_node=ca_sleep_node, ca_sleep_time=ca_sleep_time, ca_wake_time=ca_wake_time)
    if display and getattr(model, "display_nodes", None):
        extra["display_nodes"] = True
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
    ca_sleep_node: str | None = None,
    ca_sleep_time: str | None = None,
    ca_wake_time: str | None = None,
) -> tuple[list[tuple[datetime, float]], dict[str, list[tuple[datetime, float]]]]:
    """The two extra future lines the Status chart draws: ``(corrected_outdoor, baseline_by_sensor)`` (Issue #1230 R2).

    * corrected outdoor: the service's hourly outdoor values after v4's learned correction;
    * baseline: the same predict with no thermostat setpoints ("the house with no HVAC"), windows as planned.
      ``ca_sleep_node`` (+ the window) composes its ``_ca`` line the way the plan cycle does (Issue #1432), so the
      no-plan line never shows the thermostat's room for a sleep hour.

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
            # display only: the no-plan line includes the display-only nodes too (Issue #1422)
            disp: dict[str, Any] = {"display_nodes": True} if getattr(model, "display_nodes", None) else {}
            if ca_sleep_node:
                disp.update(ca_sleep_node=ca_sleep_node, ca_sleep_time=ca_sleep_time, ca_wake_time=ca_wake_time)
            out = p.predict(
                model,
                now=now,
                readings=dict(readings),
                mass_f=mass_f,
                inputs=plain,
                tz=tz,
                outdoor_now_f=outdoor_now_f,
                forecast_now_f=forecast_now_f,
                **disp,
            )
            baseline = {sid: list(pts) for sid, pts in (out or {}).items()}
    except Exception:  # noqa: BLE001 - optional line
        _LOGGER.debug("v4 chart extras: baseline failed", exc_info=True)
    return corrected, baseline


# ---------------------------------------------------------------------------
# Debug visibility
# ---------------------------------------------------------------------------


def _mode_fit(fit: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    modes = fit.get("modes")
    rec = modes.get(mode) if isinstance(modes, Mapping) else None
    return rec if isinstance(rec, Mapping) else {}


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
        # Issue #1422: chart-only sensor lines (never control)
        "display_sensors": list(model_dict.get("display_nodes") or {})
        if isinstance(model_dict.get("display_nodes"), Mapping)
        else [],
        "schema_version": model_dict.get("schema_version"),
        # Issue #1432: which model the decisions read (stitched or per room) and why; numbers in degrees F
        "node_choice": node_choice(model_dict),
        # Debug tab: why the grade is what it is, and each mode's thermostat starts (the grade's per-mode inputs)
        "confidence_reason": fit.get("reason") or None,
        "sample_days": fit.get("sample_days"),
        "modes": {
            mode: {
                "seen": fit.get(f"{mode}_seen"),
                "starts": (fit.get("starts_total") or {}).get(mode),
                "accepted": (fit.get("starts_accepted") or {}).get(mode),
                # Issue #1469: the mode's own grade ("none" | "unseen" | "low" | "ok"; None before per-mode grades)
                "grade": _mode_fit(fit, mode).get("grade"),
                "reason": _mode_fit(fit, mode).get("reason"),
                # Issue #1525: the mode's learned values are carried from an earlier window (share ``keep`` left)
                "carried": bool(_mode_fit(fit, mode).get("carried")),
                "carried_grade": bool(_mode_fit(fit, mode).get("carried_grade")),  # the grade shown is the carried one
                "keep": _mode_fit(fit, mode).get("keep"),
            }
            for mode in ("heat", "cool")
        },
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
    loop_swing_heat_f: float | None = None,
    loop_swing_cool_f: float | None = None,
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
        loop_swing_heat_f=loop_swing_heat_f or None,  # Issue #1477: the measured swing for the emulated loop
        loop_swing_cool_f=loop_swing_cool_f or None,
        can_heat=can_heat,
        can_cool=can_cool,
        evidence=evidence,
        validation=validation,
        unit=unit,
    )
