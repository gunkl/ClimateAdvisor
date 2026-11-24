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

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import math
import os
import sys
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from . import i18n
from .const import V4_PREDICT_HORIZON_HOURS, V4_UNUSABLE_WARN_REFITS
from .remote_offset import RemoteLoopEmulator  # noqa: F401 - Issue #1553: the shared emulated loop (re-exported)
from .time_utils import seconds_since
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


# Issue #1341: the words of this module are whole-sentence catalog messages (area ``timeline``, keys
# ``timeline.v4.*``); the ``describe_*`` / ``*_notice`` functions keep their ``str`` API as the English rendering of
# the matching ``*_msg`` function, so the log lines and the Debug tab keep their exact text.

# The engine's stored fit reasons are persisted codes (``predictor_v4``: they stay as stored); each known one has a
# catalog word, an unknown one (``fit failed: ValueError``) is shown as stored.
_FIT_REASON_KEYS: dict[str, str] = {
    "confidence none": "confidence_none",
    "no usable fit": "no_usable_fit",
    "too few scored points": "too_few_scored_points",
    "too few samples": "too_few_samples",
    "too few anchors": "too_few_anchors",
    "non-finite initial cost": "non_finite_cost",
    "under 3 days of samples": "under_3_days",
    "under 10 days of samples": "under_10_days",
    "fit diverged": "fit_diverged",
    "does not beat no-change at 4 h by 10 %": "no_beat_4h",
    "heating or cooling not yet observed": "mode_not_observed",
    "too few accepted thermostat starts in a used mode": "too_few_starts_used_mode",
    "too few accepted thermostat starts": "too_few_starts",
    "carried from last season": "carried",
    "heating not seen": "heating_not_seen",
    "cooling not seen": "cooling_not_seen",
    "too few anchors with heating": "few_anchors_heating",
    "too few anchors with cooling": "few_anchors_cooling",
    "does not beat no-change by 10 % while heating": "no_beat_heating",
    "does not beat no-change by 10 % while cooling": "no_beat_cooling",
}


def _reason_param(code: Any) -> Any:
    """A stored fit reason as a message parameter: its catalog word when known, else the stored text."""

    text = str(code or "")
    key = _FIT_REASON_KEYS.get(text)
    return i18n.Code(f"timeline.v4.reason.{key}") if key else text


def _kept_param(rec: Mapping[str, Any]) -> Any:

    kept = str(rec.get("kept_fitted_at") or "")[:10]
    return kept if kept else i18n.Code("timeline.v4.earlier_fit")


def describe_unusable_fit_msg(rec: Mapping[str, Any] | None) -> Any:
    """``"last refit unusable (<reason>); keeping the model from <date>"`` as a message; None when there is none."""

    if not isinstance(rec, Mapping):
        return None
    n = rec.get("count") if isinstance(rec.get("count"), int) else 1
    return i18n.Msg(
        "timeline.v4.unusable_fit",
        n=max(1, n),
        reason=_reason_param(rec.get("fit_reason") or "confidence none"),
        kept=_kept_param(rec),
    )


def unusable_fit_notice_msg(rec: Mapping[str, Any] | None) -> Any:
    """The occupant-facing sentence once V4_UNUSABLE_WARN_REFITS refits in a row were unusable; None before that."""

    if not isinstance(rec, Mapping) or not isinstance(rec.get("count"), int) or rec["count"] < V4_UNUSABLE_WARN_REFITS:
        return None
    return i18n.Msg(
        "timeline.v4.unusable_notice",
        n=rec["count"],
        reason=_reason_param(rec.get("fit_reason") or "confidence none"),
        kept=_kept_param(rec),
    )


def _en_or_empty(msg: Any) -> str:

    if msg is None:
        return ""
    return i18n.render_en(msg)


def describe_unusable_fit(rec: Mapping[str, Any] | None) -> str:
    """ "last refit unusable (<reason>); keeping the model from <date>" for the log and the Debug tab; "" when none."""
    return _en_or_empty(describe_unusable_fit_msg(rec))


def unusable_fit_notice(rec: Mapping[str, Any] | None) -> str:
    """The occupant-facing sentence once V4_UNUSABLE_WARN_REFITS refits in a row were unusable; "" before that."""
    return _en_or_empty(unusable_fit_notice_msg(rec))


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


def _word_param(mode: str) -> Any:

    return i18n.Code(f"timeline.v4.word.{_MODE_WORD[mode]}")


def _grade_param(grade: Any) -> Any:

    return i18n.Code(f"timeline.v4.grade.{grade}") if grade in ("ok", "low", "none") else str(grade)


def _days_param(days: Any) -> Any:

    if isinstance(days, (int, float)):
        return i18n.Msg("timeline.v4.days", n=float(days))
    return i18n.Msg("timeline.v4.too_few_days")


# The persisted v4 fit codes (predictor_v4 writes these exact strings into the stored model, so they are data codes,
# not display text; #1341). Each maps to the catalog message family describe_* renders for it.
_V4_REASON_KINDS: dict[str, str] = {
    "under 3 days of samples": "under_3_days",
    "fit diverged": "fit_diverged",
    "does not beat no-change at 4 h by 10 %": "no_beat_4h",
    "under 10 days of samples": "under_10_days",
    "heating or cooling not yet observed": "mode_not_observed",
    "too few accepted thermostat starts in a used mode": "too_few_starts",
}


def describe_grade_msgs(summary: Mapping[str, Any] | None) -> tuple[Any, Any]:
    """``(why, by_mode)`` as messages (a stored reason this module has no word for stays text); ``(None, None)`` when
    there is no model. See :func:`describe_grade`."""

    M = i18n.Msg  # noqa: N806 - the message constructor, short at its many call sites
    if not isinstance(summary, Mapping) or not summary.get("has_model"):
        return None, None
    try:
        pv = _predictor()
        need_starts, need_days = int(pv.GATE_MIN_ACCEPTED_STARTS), float(pv.GATE_MIN_DAYS_OK)
    except Exception:  # noqa: BLE001 - the words only; the engine's own gates are the source of truth
        need_starts, need_days = 5, 10.0
    modes = summary.get("modes") if isinstance(summary.get("modes"), Mapping) else {}

    def mode_msg(mode: str) -> Any:
        m = modes.get(mode) if isinstance(modes.get(mode), Mapping) else {}
        word, acc, tot, grade = _word_param(mode), m.get("accepted"), m.get("starts"), m.get("grade")
        if m.get("carried_grade"):
            # Issue #1525: what the house learned about this mode stands until new runs of it displace it
            new = (
                M("timeline.v4.mode.new_not_seen")
                if m.get("seen") is False
                else M("timeline.v4.mode.new_starts", n=acc if isinstance(acc, int) else 0)
            )
            return M(
                "timeline.v4.mode.carried",
                word=word,
                grade=_grade_param(grade),
                reason=_reason_param(m.get("reason")),
                new=new,
            )
        if grade == "unseen" or m.get("seen") is False or (not tot and m.get("seen") is None):
            return M("timeline.v4.mode.not_seen", word=word)
        if not isinstance(acc, int):
            return M("timeline.v4.mode.no_count", word=word)
        enough = M("timeline.v4.mode.enough") if acc >= need_starts else M("timeline.v4.mode.needs", n=need_starts)
        starts = M("timeline.v4.mode.starts", acc=acc, tot=tot if tot is not None else "None", enough=enough)
        if grade in ("ok", "low", "none"):
            why = m.get("reason") or ""
            if grade == "low" and why and "starts" not in why:
                return M(
                    "timeline.v4.mode.graded_why",
                    word=word,
                    grade=_grade_param(grade),
                    starts=starts,
                    why=_reason_param(why),
                )
            return M("timeline.v4.mode.graded", word=word, grade=_grade_param(grade), starts=starts)
        return M("timeline.v4.mode.plain", word=word, starts=starts)

    span = summary.get("span_days")
    span_days = round(float(span)) if isinstance(span, (int, float)) else None
    by_mode: Any = None
    if modes:
        parts = {"heat": mode_msg("heat"), "cool": mode_msg("cool")}
        by_mode = (
            M("timeline.v4.by_mode_window", heat=parts["heat"], cool=parts["cool"], days=span_days)
            if span_days is not None
            else M("timeline.v4.by_mode", heat=parts["heat"], cool=parts["cool"])
        )
    days = summary.get("sample_days")
    days_w = _days_param(days)
    code = summary.get("confidence_reason") or ""
    carried_reason = next(
        (
            str(modes[m].get("reason"))
            for m in ("heat", "cool")
            if isinstance(modes.get(m), Mapping) and modes[m].get("carried_grade")
        ),
        "",
    )

    def words(selected: list[str]) -> Any:
        return i18n.ListOf([_word_param(m) for m in selected], "and")

    carried = [m for m in ("heat", "cool") if isinstance(modes.get(m), Mapping) and modes[m].get("carried_grade")]
    short = [
        m
        for m in ("heat", "cool")
        if isinstance(modes.get(m), Mapping) and (modes[m].get("accepted") or 0) < need_starts and m not in carried
    ]
    unseen = [
        m
        for m in ("heat", "cool")
        if isinstance(modes.get(m), Mapping) and not modes[m].get("seen") and not modes[m].get("carried")
    ]
    why: Any
    kind = _V4_REASON_KINDS.get(code)
    if code == "":
        if summary.get("confidence") != "ok":
            why = ""
        elif carried:
            why = M("timeline.v4.why.ok_carried", modes=words(carried), reason=_reason_param(carried_reason))
        else:
            why = M("timeline.v4.why.ok")
    elif kind == "under_3_days":
        why = M("timeline.v4.why.under_3_days", days=days_w)
    elif kind == "fit_diverged":
        why = M("timeline.v4.why.fit_diverged")
    elif kind == "no_beat_4h":
        why = M("timeline.v4.why.no_beat_4h")
    elif kind == "under_10_days":
        why = M("timeline.v4.why.under_10_days", days=days_w, need=need_days)
    elif kind == "mode_not_observed":
        modes_p = words(unseen) if unseen else M("timeline.v4.one_mode")
        why = (
            M("timeline.v4.why.mode_not_observed_window", modes=modes_p, days=span_days)
            if span_days is not None
            else M("timeline.v4.why.mode_not_observed", modes=modes_p)
        )
    elif kind == "too_few_starts":
        why = (
            M("timeline.v4.why.too_few_starts", modes=words(short), need=need_starts)
            if short
            else M("timeline.v4.why.too_few_starts_any", need=need_starts)
        )
    else:
        why = str(code)
    return why, by_mode


def describe_grade(summary: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(why, by_mode)`` in plain words for the Debug tab: why v4 has its grade, and each mode's thermostat starts.

    ``summary`` is :func:`model_summary`'s dict. v4 is ``ok`` only when BOTH heating and cooling have enough usable
    thermostat starts in the training window (``predictor_v4.fit``'s gates); this says which input holds it back.
    The English rendering of :func:`describe_grade_msgs` (``("", "")`` when there is no model)."""
    why, by_mode = describe_grade_msgs(summary)
    return _en_or_empty(why), _en_or_empty(by_mode)


def describe_node_choice_msgs(
    choice: Mapping[str, Any] | None,
    *,
    unit: str | None = None,
    fmt_delta: Callable[[float], str] | None = None,
    names: Mapping[str, str] | None = None,
    changed: bool = False,
) -> tuple[Any, Any]:
    """``(headline, reason)`` as messages (``(None, None)`` without a choice; the reason is None for an unknown code).

    Temperature differences are ``TempDelta`` parameters in ``unit`` (2 decimals, rendered in the reader's language);
    a caller that has only a formatter (``fmt_delta``, degrees F in, text out) gets its text instead, which is how the
    ``str`` wrapper below keeps its exact output."""

    M = i18n.Msg  # noqa: N806
    if not isinstance(choice, Mapping):
        return None, None
    names = names or {}

    def delta(f: float) -> Any:
        if fmt_delta is not None:
            return fmt_delta(f)
        return i18n.TempDelta(float(f), unit or "fahrenheit", 2)

    now_w = "yes" if changed else "no"
    prim, sleep = choice.get("primary"), choice.get("sleep")
    p_name: Any = names.get(prim, prim) if isinstance(prim, str) else M("timeline.v4.node.the_thermostat")
    s_name: Any = names.get(sleep, sleep) if isinstance(sleep, str) else M("timeline.v4.node.the_sleep_sensor")
    per_room = choice.get("model") == MODEL_PER_ROOM
    if per_room:
        headline = (
            M("timeline.v4.node.headline_rooms", changed=now_w)
            if sleep
            else M("timeline.v4.node.headline_thermostat", changed=now_w)
        )
    else:
        headline = M("timeline.v4.node.headline_stitched", changed=now_w)

    def days(key: str) -> Any:
        d = choice.get(f"{key}_days")
        return M("timeline.v4.days", n=float(d)) if isinstance(d, (int, float)) else M("timeline.v4.no_days")

    need = choice.get("min_days", 3.0)
    need_w: Any = float(need) if isinstance(need, (int, float)) else 3
    mae = (choice.get("scores") or {}).get("mae") or {}
    a, b = mae.get(MODEL_STITCHED) or {}, mae.get(MODEL_PER_ROOM) or {}
    lead = str(choice.get("lead") or "4h")

    def score_msg() -> Any:
        if b.get(lead) is None or a.get(lead) is None:
            return ""
        others = [k for k in ("1h", "12h") if k != lead and a.get(k) is not None and b.get(k) is not None]
        if len(others) == 2:
            more: Any = M(
                "timeline.v4.node.others_two",
                h1=others[0][:-1], b1=delta(b[others[0]]), a1=delta(a[others[0]]),
                h2=others[1][:-1], b2=delta(b[others[1]]), a2=delta(a[others[1]]),
            )  # fmt: skip
        elif others:
            more = M("timeline.v4.node.others_one", h1=others[0][:-1], b1=delta(b[others[0]]), a1=delta(a[others[0]]))
        else:
            more = ""
        return M(
            "timeline.v4.node.score_room" if sleep else "timeline.v4.node.score_own",
            b=delta(b[lead]),
            a=delta(a[lead]),
            h=lead[:-1],
            more="none" if more == "" else "some",
            others=more if more != "" else "",
            p_name=p_name,
        )

    reason_code = choice.get("reason")
    qual = (
        M(
            "timeline.v4.node.qual_rooms",
            need=need_w,
            p_name=p_name,
            pdays=days("primary"),
            s_name=s_name,
            sdays=days("sleep"),
        )
        if sleep
        else M("timeline.v4.node.qual_one", need=need_w, p_name=p_name, pdays=days("primary"))
    )
    reason: Any
    if reason_code == "per_room_no_worse":
        reason = M("timeline.v4.node.reason_no_worse", qual=qual, score=score_msg())
    elif reason_code == "stitched_not_usable":
        reason = M("timeline.v4.node.reason_stitched_unusable", qual=qual)
    elif reason_code == "per_room_scored_worse":
        tol = choice.get("tolerance_f")
        if isinstance(tol, (int, float)):
            reason = M("timeline.v4.node.reason_worse_tol", qual=qual, score=score_msg(), tol=delta(float(tol)))
        else:
            reason = M("timeline.v4.node.reason_worse", qual=qual, score=score_msg())
    elif reason_code == "too_few_shared_anchors":
        n = (choice.get("scores") or {}).get("n_anchors")
        reason = M("timeline.v4.node.reason_few_anchors", qual=qual, n=n if isinstance(n, (int, float)) else str(n))
    elif reason_code == "per_room_not_usable":
        reason = M(
            "timeline.v4.node.reason_not_fitted",
            qual=qual,
            why=_reason_param(choice.get("per_room_fit_reason") or "no usable fit"),
        )
    elif reason_code == "primary_not_ready":
        reason = M("timeline.v4.node.reason_primary_not_ready", p_name=p_name, pdays=days("primary"), need=need_w)
    elif reason_code == "sleep_not_ready":
        reason = M(
            "timeline.v4.node.reason_sleep_not_ready", s_name=s_name, sdays=days("sleep"), need=need_w, p_name=p_name
        )
    elif reason_code == "no_primary_entity":
        reason = M("timeline.v4.node.reason_no_primary")
    else:
        reason = None
    return headline, reason


def describe_node_choice(
    choice: Mapping[str, Any] | None,
    *,
    fmt_delta: Callable[[float], str] = lambda f: f"{f:.2f}°F",
    names: Mapping[str, str] | None = None,
    changed: bool = False,
) -> tuple[str, str]:
    """``(headline, reason)`` in plain words for the INFO line, the Activity row and the Debug tab (Issue #1432).

    ``fmt_delta`` formats a temperature DIFFERENCE given in degrees F in the user's unit (``format_temp_delta``);
    ``names`` maps entity ids to friendly names. ``changed`` words the headline as a change ("now forecasts").
    The English rendering of :func:`describe_node_choice_msgs`."""
    headline, reason = describe_node_choice_msgs(choice, fmt_delta=fmt_delta, names=names, changed=changed)
    return _en_or_empty(headline), _en_or_empty(reason)


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


def live_loop_seed(state: Any, raise_at: datetime | None, now: datetime) -> tuple[float, float | None]:
    """``(offset_f, seconds_since_raise)`` the live bedroom loop holds now, for a forecast that starts mid-loop.

    Issue #1593: every display cycle (and a night re-plan after bedtime) used to start the emulated loop at offset 0,
    so a raise already in force was forgotten and the next warm-up was missed. ``state`` is the engine's ONE
    description of the loop (``AutomationEngine.remote_offset_state()``, read by the caller with its facts passed in):
    the signed offset the thermostat holds (heat +, cool -) while the SLEEP context supplies the reading, else 0
    (another context, the primary sensor, a gate that cleared it: ``None``). ``raise_at`` is when the live loop last
    raised (the engine's ``_remote_raise_at``; None when unknown). Pure; never raises.
    """
    from .const import REMOTE_CONTEXT_SLEEP  # noqa: PLC0415

    offset = getattr(state, "offset_f", None) if state is not None else None
    if getattr(state, "context", None) != REMOTE_CONTEXT_SLEEP or not isinstance(offset, (int, float)):
        return 0.0, None
    if not math.isfinite(offset) or abs(offset) < 1e-6:
        return 0.0, None
    since: float | None = None
    if isinstance(raise_at, datetime):
        try:
            since = max(0.0, seconds_since(raise_at, now=now))  # Issue #1244: real time
        except TypeError:  # naive/aware clock mix (stubs)
            since = None
    return float(offset), since


def live_loop_emulator(
    *,
    state: Any,
    raise_at: datetime | None,
    latency_s: float | None,
    swing_heat_f: float,
    swing_cool_f: float,
    now: datetime,
    unit: str,
    step_minutes: int,
    gated_steps: int = 0,
) -> RemoteLoopEmulator:
    """The emulated bedroom loop for a forecast made NOW, from the live loop's facts (passed in by the caller): the
    offset in force (:func:`live_loop_seed`), the live loop's swing (``AutomationEngine._remote_swing_f``: the measured
    one, Issue #1477) and its startup wait (the measured latency ``_remote_startup_latency_s``), so the forecast and
    the house share one rule and one set of numbers."""
    offset, since = live_loop_seed(state, raise_at, now)
    return RemoteLoopEmulator(
        swing_heat_f=swing_heat_f,
        swing_cool_f=swing_cool_f,
        unit=unit,
        step_minutes=step_minutes,
        offset_f=offset,
        seconds_since_raise=since,
        startup_latency_s=latency_s if isinstance(latency_s, (int, float)) else None,
        gated_steps=gated_steps,
    )


def override_hold(engine: Any, *, mode: str | None, setpoint_f: float | None) -> OverrideHold | None:
    """The occupant override in force now, read-only from the engine (Issue #1616), or None.

    An override is in force while ``_manual_override_active``; Climate Advisor takes the thermostat back when its grace
    ends (``_grace_end_time``). ``mode`` / ``setpoint_f`` are the thermostat's live mode and setpoint: heat or cool
    hold that setpoint on that side, off holds nothing on. No known end (grace disabled), or an end already past ->
    None (the forecast follows the plan, as before)."""
    if getattr(engine, "_manual_override_active", False) is not True:
        return None
    raw = getattr(engine, "_grace_end_time", None)
    try:
        until = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        until = None
    if until is None or until.tzinfo is None:
        return None
    if mode == "heat" and isinstance(setpoint_f, (int, float)):
        return OverrideHold(until, float(setpoint_f), None)
    if mode == "cool" and isinstance(setpoint_f, (int, float)):
        return OverrideHold(until, None, float(setpoint_f))
    if mode == "off":
        return OverrideHold(until, None, None)
    return None


# ``automation.BAND_CONTEXT_*`` of the bands the engine arms for an occupied home (no engine import here)
_ARMED_CONTEXTS = frozenset({"comfort", "prefer_savings", "sleep"})


class OverrideHold(NamedTuple):
    """The occupant's setpoint the thermostat holds until ``until`` (an occupant override, Issue #1616).

    ``heat_sp_f`` / ``cool_sp_f`` are what the thermostat is set to in its mode (the other side ``None``; both
    ``None`` when the occupant switched it off). ``until`` is when Climate Advisor is due to take the thermostat back
    (the override's grace end, ``AutomationEngine._grace_end_time``)."""

    until: datetime
    heat_sp_f: float | None
    cool_sp_f: float | None

    def holds(self, ts: datetime) -> bool:
        return ts < self.until


def with_armed_band(
    inputs: Sequence[Any],
    *,
    band_at: Callable[[datetime, str], Any],
    occupancy_mode: str,
    hold: OverrideHold | None = None,
) -> list[Any]:
    """The forecast steps with the setpoints the thermostat will actually be given (Issues #1593, #1610, #1616).

    Occupant view: the house switches the thermostat to the flat sleep band at bedtime and to the awake comfort band at
    wake time, and holds an occupant's own setpoint until the override is due to end. The forecast fed the target band
    schedule instead: its one-hour ramp into the sleep floor, its two-hour ramp out of it after wake time (interpolated
    over more), and the plan's band while the occupant held another value; the thermostat learned against what it is
    commanded (#1609) then started too late or too early. Per step:

    * while ``hold`` is in force: the occupant's setpoint (the other side not armed);
    * a step whose band (``band_at``: ``automation.band_resolver_for``, the engine's own ``select_comfort_band``) is the
      comfort, prefer-savings or sleep band: its floor as the heat setpoint, its ceiling as the cool setpoint, unless
      the schedule carries a ceiling below every armed ceiling in the horizon (a pre-cool / TOU overlay the engine
      commands on top of the band, as ``operation_timeline.build_segments`` keeps it; the schedule's ramps run
      between two armed ceilings and never get there);
    * every other step (away, vacation: the schedule already holds the setback for today only) as it is.

    Returns a new list; the schedule itself (the chart's band, v3, the comfort score) is not changed.
    """
    bands = [band_at(i.ts, occupancy_mode) for i in inputs]
    armed = [getattr(b, "context", None) in _ARMED_CONTEXTS for b in bands]
    lowest = min((float(b.ceiling) for b, a in zip(bands, armed, strict=True) if a), default=math.inf)
    out: list[Any] = []
    for i, band, is_armed in zip(inputs, bands, armed, strict=True):
        if hold is not None and hold.holds(i.ts):
            out.append(dataclasses.replace(i, heat_sp_f=hold.heat_sp_f, cool_sp_f=hold.cool_sp_f))
            continue
        if not is_armed:
            out.append(i)
            continue
        ceiling = float(band.ceiling)
        if i.cool_sp_f is not None and i.cool_sp_f < lowest - 1e-6:
            ceiling = float(i.cool_sp_f)
        out.append(dataclasses.replace(i, heat_sp_f=float(band.floor), cool_sp_f=ceiling))
    return out


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
    sleep_loop: RemoteLoopEmulator | None = None,
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
    ``sleep_loop`` (Issue #1553, the display cycle only) emulates the remote-sensor outer loop in the sleep window; the
    baseline never uses it (it has no setpoints to move).
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
    if sleep_loop is not None and not baseline and ca_sleep_node:
        extra["sleep_loop"] = sleep_loop
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
    loop_offset_f: float = 0.0,
    loop_since_raise_s: float | None = None,
    loop_startup_latency_s: float | None = None,
    run_now: tuple[int, float] | None = None,
) -> Any:
    """``night_strategy.NightInputs`` from the live state, or None when the night cannot be planned.

    The state at ``start``: when bedtime is still ahead, each model sensor's value is the PLAN's predicted reading at
    bedtime (``predictions``: v4's per-sensor path under the same plan the timeline follows); the primary sensor falls
    back to the timeline's ``_ca`` ``path`` and any other sensor to its current reading. When the night is already
    under way (``start`` == ``now``) it is the live reading. ``corrected_outdoor`` is tonight's forecast after v4's
    learned correction. Pure: no clock, no I/O.

    Issue #1593: a night already under way also starts from the live bedroom loop (``loop_offset_f`` signed,
    ``loop_since_raise_s``: :func:`live_loop_seed`) and the heating / cooling run in progress (``run_now``); planned
    from bedtime the loop starts at offset 0 with no run, as the live loop does when the sleep window opens.
    ``loop_startup_latency_s`` is the thermostat's measured startup latency (the live loop's startup wait).
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
        loop_offset_f=0.0 if ahead else float(loop_offset_f or 0.0),
        loop_since_raise_s=None if ahead else loop_since_raise_s,
        loop_startup_latency_s=loop_startup_latency_s,
        run_now=None if ahead else run_now,
    )


# ---------------------------------------------------------------------------
# Subprocess offload for CPU-bound v4 fitting (Issue #1722)
# ---------------------------------------------------------------------------
# ``predictor_v4`` is pure-Python CPU-bound math (no C extensions releasing the GIL). On slower hardware such as a
# Raspberry Pi 4 a single fit takes ~60-100 s and starves Home Assistant's MainThread event loop when run inside
# ``hass.async_add_executor_job`` (thread pool). Running the fit in a dedicated Python subprocess gives it its own
# GIL on another core, lowers its CPU priority (nice +10) and lets unload cancel/kill it cleanly.

_ORIG_RUN_FIT = run_fit
_ORIG_RUN_DISPLAY_FIT = run_display_fit
_PKG_DIR = Path(__file__).resolve().parent
_PARENT_DIR = _PKG_DIR.parent

_WORKER_BOOTSTRAP = (
    "import sys, types; "
    "from datetime import UTC, datetime; "
    "ha = types.ModuleType('homeassistant'); "
    "ha_u = types.ModuleType('homeassistant.util'); "
    "ha_dt = types.ModuleType('homeassistant.util.dt'); "
    "ha_dt.utcnow = lambda: datetime.now(UTC); "
    "ha_dt.now = lambda tz=None: datetime.now(tz or UTC); "
    "ha_dt.as_utc = lambda d: d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC); "
    "sys.modules.update({'homeassistant': ha, 'homeassistant.util': ha_u, 'homeassistant.util.dt': ha_dt}); "
    "m1 = types.ModuleType('custom_components'); m1.__path__ = [sys.argv[2]]; sys.modules['custom_components'] = m1; "
    "m2 = types.ModuleType('custom_components.climate_advisor'); m2.__path__ = [sys.argv[1]]; "
    "sys.modules['custom_components.climate_advisor'] = m2; "
    "from custom_components.climate_advisor.v4_shadow import _worker_main; "
    "_worker_main()"
)


def _is_real_predictor() -> bool:
    """True when ``predictor_v4`` is the real on-disk module (not a test double like ``FakePredictor``)."""
    try:
        pred = _predictor()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(getattr(pred, "__file__", None), str)


def _serialize_tz(tz: tzinfo | None, now: datetime) -> dict[str, Any] | None:
    if tz is None:
        return None
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return {"key": key}
    offset = tz.utcoffset(now)
    if offset is not None:
        return {"offset_s": int(offset.total_seconds())}
    return None


def _deserialize_tz(raw: Mapping[str, Any] | None) -> tzinfo:
    if not isinstance(raw, Mapping):
        return UTC
    key = raw.get("key")
    if isinstance(key, str) and key:
        try:
            return ZoneInfo(key)
        except Exception:  # noqa: BLE001
            pass
    offset_s = raw.get("offset_s")
    if isinstance(offset_s, (int, float)):
        return timezone(timedelta(seconds=int(offset_s)))
    return UTC


def _make_in_sleep(sleep_window: Sequence[Any] | None) -> Callable[[datetime], bool] | None:
    """Rebuild ``_in_sleep_window`` from ``(sleep_time, wake_time)`` without importing ``automation``."""
    if not sleep_window or len(sleep_window) != 2:
        return None
    sleep_str, wake_str = sleep_window[0], sleep_window[1]
    if not sleep_str or not wake_str:
        return None
    try:
        sp = str(sleep_str).split(":")
        wp = str(wake_str).split(":")
        sleep_t = dtime(int(sp[0]), int(sp[1]))
        wake_t = dtime(int(wp[0]), int(wp[1]))
    except ValueError, IndexError:
        return None
    if sleep_t == wake_t:
        return lambda _dt: False
    if sleep_t < wake_t:
        return lambda dt: sleep_t <= dt.time().replace(second=0, microsecond=0) < wake_t
    return lambda dt: (
        dt.time().replace(second=0, microsecond=0) >= sleep_t or (dt.time().replace(second=0, microsecond=0) < wake_t)
    )


async def _run_in_subprocess(executor_job: Callable[..., Awaitable[Any]], op: str, payload: dict[str, Any]) -> Any:
    """Execute one v4 worker operation in a child Python process; kill and reap it on cancellation."""
    raw_in: bytes = await executor_job(lambda: json.dumps({"op": op, "payload": payload}).encode("utf-8"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _WORKER_BOOTSTRAP,
        str(_PKG_DIR),
        str(_PARENT_DIR),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate(raw_in)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"v4 worker ({op}) exited with code {proc.returncode}: {err_text[-500:]}")
    try:
        resp = await executor_job(lambda: json.loads(stdout.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"v4 worker ({op}) returned invalid JSON: {exc}") from exc
    if not isinstance(resp, dict) or not resp.get("ok"):
        raise RuntimeError(f"v4 worker ({op}) failed: {resp.get('error') if isinstance(resp, dict) else resp}")
    return resp["result"]


def _worker_main() -> None:
    """Subprocess entrypoint: read one JSON job on stdin, run the v4 fit/replay, write JSON on stdout."""
    with contextlib.suppress(OSError, AttributeError):
        os.nice(10)
    try:
        req = json.loads(sys.stdin.read())
        op = req["op"]
        p = req["payload"]
        now = datetime.fromisoformat(p["now"])
        tz = _deserialize_tz(p.get("tz"))
        in_sleep = _make_in_sleep(p.get("sleep_window"))
        if op == "fit":
            snapshots = list(p.get("snapshots") or [])
            model_dict, seconds, fit_line = run_fit(
                chart_entries=p["chart_entries"],
                snapshots_fn=(lambda _a, _b, _s=snapshots: _s) if snapshots else None,
                tz=tz,
                now=now,
                primary=p["primary"],
                sensors=p["sensors"],
                previous_model=p.get("previous_model"),
                primary_aliases=tuple(p.get("primary_aliases") or ()),
                in_sleep=in_sleep,
                train_days=p.get("train_days"),
                sleep_sensor=p.get("sleep_sensor"),
                climate_entity=p.get("climate_entity"),
            )
            result: Any = [model_dict, seconds, fit_line]
        elif op == "display_fit":
            new_dict, seconds, line = run_display_fit(
                chart_entries=p["chart_entries"],
                tz=tz,
                now=now,
                primary=p["primary"],
                sensors=p["sensors"],
                model_dict=p["model_dict"],
                primary_aliases=tuple(p.get("primary_aliases") or ()),
                in_sleep=in_sleep,
                train_days=p.get("train_days"),
            )
            result = [new_dict, seconds, line]
        elif op == "night_replay":
            from . import night_replay  # noqa: PLC0415

            snapshots = list(p.get("snapshots") or [])
            bed = tuple(p["bed"])
            wake = tuple(p["wake"])
            result = night_replay.run_nightly(
                chart_entries=p["chart_entries"],
                snapshots_fn=(lambda _a, _b, _s=snapshots: _s) if snapshots else None,
                tz=tz,
                now=now,
                primary=p["primary"],
                sensors=p["sensors"],
                primary_aliases=tuple(p.get("primary_aliases") or ()),
                in_sleep=in_sleep,
                bed=(int(bed[0]), int(bed[1])),
                wake=(int(wake[0]), int(wake[1])),
                train_days=int(p["train_days"]),
                model_fitted_at=p.get("model_fitted_at"),
                previous=p.get("previous"),
            )
        else:
            raise ValueError(f"unknown v4 worker op: {op!r}")
        sys.stdout.write(json.dumps({"ok": True, "result": result}))
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))


async def _fetch_snapshots_in_executor(
    executor_job: Callable[..., Awaitable[Any]],
    snapshots_fn: Callable[[datetime, datetime], list[dict]] | None,
    start: datetime,
    end: datetime,
) -> list[dict]:
    if snapshots_fn is None:
        return []
    try:
        return list(await executor_job(snapshots_fn, start, end))
    except Exception:  # noqa: BLE001 - matches run_fit / run_nightly degradation
        _LOGGER.warning("v4 fit: forecast archive unreadable; fitting from the chart log alone", exc_info=True)
        return []


async def async_run_fit(
    executor_job: Callable[..., Awaitable[Any]],
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    snapshots_fn: Callable[[datetime, datetime], list[dict]] | None,
    tz: tzinfo | None,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    previous_model: Mapping[str, Any] | None = None,
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    sleep_window: Sequence[Any] | None = None,
    train_days: float | None = None,
    sleep_sensor: str | None = None,
    climate_entity: str | None = None,
    use_subprocess: bool | None = None,
) -> tuple[dict, float, str]:
    """Run ``run_fit`` in a subprocess (avoiding GIL starvation on HA's event loop) with executor fallback."""
    want_sub = (
        use_subprocess
        if use_subprocess is not None
        else (_is_real_predictor() and run_fit is _ORIG_RUN_FIT and (in_sleep is None or sleep_window is not None))
    )
    if want_sub:
        windowed = rows_in_window(chart_entries, now, train_days) if train_days else list(chart_entries)
        snapshots = await _fetch_snapshots_in_executor(
            executor_job, snapshots_fn, now - timedelta(days=(train_days or 400) + 1), now + timedelta(hours=1)
        )
        payload = {
            "chart_entries": windowed,
            "snapshots": snapshots,
            "tz": _serialize_tz(tz, now),
            "now": now.isoformat(),
            "primary": primary,
            "sensors": list(sensors),
            "previous_model": dict(previous_model) if previous_model is not None else None,
            "primary_aliases": list(primary_aliases),
            "sleep_window": list(sleep_window) if sleep_window is not None else None,
            "train_days": train_days,
            "sleep_sensor": sleep_sensor,
            "climate_entity": climate_entity,
        }
        try:
            model_dict, seconds, fit_line = await _run_in_subprocess(executor_job, "fit", payload)
            return dict(model_dict), float(seconds), str(fit_line)
        except OSError:
            _LOGGER.warning("v4 fit: subprocess launch failed; falling back to executor thread", exc_info=True)
    result: tuple[dict, float, str] = await executor_job(
        functools.partial(
            run_fit,
            chart_entries=chart_entries,
            snapshots_fn=snapshots_fn,
            tz=tz or UTC,
            now=now,
            primary=primary,
            sensors=sensors,
            previous_model=previous_model,
            primary_aliases=primary_aliases,
            in_sleep=in_sleep,
            train_days=train_days,
            sleep_sensor=sleep_sensor,
            climate_entity=climate_entity,
        )
    )
    return result


async def async_run_display_fit(
    executor_job: Callable[..., Awaitable[Any]],
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    tz: tzinfo | None,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    model_dict: Mapping[str, Any],
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    sleep_window: Sequence[Any] | None = None,
    train_days: float | None = None,
    use_subprocess: bool | None = None,
) -> tuple[dict, float, str]:
    """Run ``run_display_fit`` in a subprocess with executor fallback."""
    want_sub = (
        use_subprocess
        if use_subprocess is not None
        else (
            _is_real_predictor()
            and run_display_fit is _ORIG_RUN_DISPLAY_FIT
            and (in_sleep is None or sleep_window is not None)
        )
    )
    if want_sub:
        windowed = rows_in_window(chart_entries, now, train_days) if train_days else list(chart_entries)
        payload = {
            "chart_entries": windowed,
            "tz": _serialize_tz(tz, now),
            "now": now.isoformat(),
            "primary": primary,
            "sensors": list(sensors),
            "model_dict": dict(model_dict),
            "primary_aliases": list(primary_aliases),
            "sleep_window": list(sleep_window) if sleep_window is not None else None,
            "train_days": train_days,
        }
        try:
            new_dict, seconds, line = await _run_in_subprocess(executor_job, "display_fit", payload)
            return dict(new_dict), float(seconds), str(line)
        except OSError:
            _LOGGER.warning("v4 display fit: subprocess launch failed; falling back to executor thread", exc_info=True)
    result: tuple[dict, float, str] = await executor_job(
        functools.partial(
            run_display_fit,
            chart_entries=chart_entries,
            tz=tz or UTC,
            now=now,
            primary=primary,
            sensors=sensors,
            model_dict=model_dict,
            primary_aliases=primary_aliases,
            in_sleep=in_sleep,
            train_days=train_days,
        )
    )
    return result


async def async_run_night_replay(
    executor_job: Callable[..., Awaitable[Any]],
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    snapshots_fn: Callable[[datetime, datetime], list[dict]] | None,
    tz: tzinfo | None,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    sleep_window: Sequence[Any] | None = None,
    bed: tuple[int, int] = (22, 30),
    wake: tuple[int, int] = (6, 30),
    train_days: int = 30,
    model_fitted_at: str | None = None,
    previous: Mapping[str, Any] | None = None,
    use_subprocess: bool | None = None,
) -> dict[str, Any]:
    """Run ``night_replay.run_nightly`` in a subprocess with executor fallback."""
    from . import night_replay  # noqa: PLC0415

    unpatched_replay = getattr(night_replay.run_nightly, "__module__", "") == night_replay.__name__
    want_sub = (
        use_subprocess
        if use_subprocess is not None
        else (_is_real_predictor() and unpatched_replay and (in_sleep is None or sleep_window is not None))
    )
    if want_sub:
        horizon_days = float(train_days + night_replay.NIGHT_REPLAY_NIGHTS * 1.1 + night_replay.NIGHT_REPLAY_EXTRA_DAYS)
        windowed = rows_in_window(chart_entries, now, horizon_days)
        snapshots = await _fetch_snapshots_in_executor(
            executor_job, snapshots_fn, now - timedelta(days=horizon_days + 1), now + timedelta(hours=1)
        )
        payload = {
            "chart_entries": windowed,
            "snapshots": snapshots,
            "tz": _serialize_tz(tz, now),
            "now": now.isoformat(),
            "primary": primary,
            "sensors": list(sensors),
            "primary_aliases": list(primary_aliases),
            "sleep_window": list(sleep_window) if sleep_window is not None else None,
            "bed": [int(bed[0]), int(bed[1])],
            "wake": [int(wake[0]), int(wake[1])],
            "train_days": int(train_days),
            "model_fitted_at": model_fitted_at,
            "previous": dict(previous) if previous is not None else None,
        }
        try:
            record = await _run_in_subprocess(executor_job, "night_replay", payload)
            return dict(record)
        except OSError:
            _LOGGER.warning("night replay: subprocess launch failed; falling back to executor thread", exc_info=True)
    result: dict[str, Any] = await executor_job(
        functools.partial(
            night_replay.run_nightly,
            chart_entries=chart_entries,
            snapshots_fn=snapshots_fn,
            tz=tz or UTC,
            now=now,
            primary=primary,
            sensors=sensors,
            primary_aliases=primary_aliases,
            in_sleep=in_sleep,
            bed=bed,
            wake=wake,
            train_days=train_days,
            model_fitted_at=model_fitted_at,
            previous=previous,
        )
    )
    return result
