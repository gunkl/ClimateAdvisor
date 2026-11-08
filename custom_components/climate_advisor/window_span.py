"""The one planned-window rule every prediction engine predicts under (Issue #1230 R6 task 3, #1391).

Occupant view: when Climate Advisor plans a window period (today's or tomorrow's), the predicted indoor line cools or
warms only over those planned hours, whichever engine (v3 or v4) is the primary. Pure functions, engine-neutral on
purpose: the v3 builder (``_build_predicted_indoor_future``), the v4 live inputs (``v4_shadow.build_inputs``) and the
backtest (``tools/predict_backtest.py``) all read these, so the engines cannot drift apart on the window assumption.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

WINDOW_PLAN_FALLBACK_HOURS = 4.0  # windows recommended but no planned times known: open for this long from the anchor


def planned_window_span(
    anchor: datetime,
    recommended: bool,
    open_t: dtime | None,
    close_t: dtime | None,
    *,
    fallback_hours: float = WINDOW_PLAN_FALLBACK_HOURS,
    day_offset: int = 0,
) -> tuple[datetime, datetime] | None:
    """The one window assumption v4 predicts under, live AND in the backtest (Issue #1230 R6 task 3).

    Occupant view: when Climate Advisor recommends opening the windows, the prediction shows the house cooling (or
    warming) only while the windows are planned to be open, from the time the engine planned, not for an arbitrary
    four hours from whenever the prediction was made.

    Rule: windows not recommended -> ``None``. Recommended with the engine's own planned ``open_t`` / ``close_t`` (the
    classification live; the day's record ``window_open_time`` / ``window_close_time`` in the backtest) -> that span on
    the ANCHOR's local day (a close at or before the open runs past midnight); a span already over at the anchor is
    ``None``. Recommended without planned times -> the documented fallback, ``fallback_hours`` from the anchor.
    The span covers one plan day only: a recommendation is never repeated at the same clock time on another day.
    ``day_offset=1`` (Issue #1391) is TOMORROW's own plan (the timeline's ``plan_days[1]`` live, the next day's
    record in the backtest): the planned times on the anchor's next local day. Tomorrow's plan has no "from the
    anchor" fallback (nothing says when tomorrow's period starts): recommended without both times -> ``None``.
    Callers pass ``None`` / not-recommended when tomorrow's plan is unknown, so unknown means windows closed.
    """
    if not recommended:
        return None
    if open_t is not None and close_t is not None:
        day = anchor.date() + timedelta(days=day_offset)
        start = datetime.combine(day, open_t, tzinfo=anchor.tzinfo)
        end = datetime.combine(day, close_t, tzinfo=anchor.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        return (start, end) if end > anchor else None
    if day_offset != 0:
        return None
    return (anchor, anchor + timedelta(hours=fallback_hours)) if fallback_hours > 0 else None


def planned_window_spans(
    anchor: datetime,
    today: tuple[bool, dtime | None, dtime | None],
    tomorrow: tuple[bool, dtime | None, dtime | None] | None = None,
    *,
    fallback_hours: float = WINDOW_PLAN_FALLBACK_HOURS,
) -> tuple[tuple[datetime, datetime], ...]:
    """Today's span (``planned_window_span``) plus tomorrow's (``day_offset=1``) when its plan is known (Issue #1391).

    Occupant view: tomorrow morning's planned window period (free cooling) is in the 24 h line after midnight, not
    just today's. ``tomorrow=None`` (plan unknown) -> tomorrow's hours stay windows-closed. Used by the live
    ``build_inputs`` and the backtest, so both read one rule.
    """
    spans = [planned_window_span(anchor, *today, fallback_hours=fallback_hours)]
    if tomorrow is not None:
        spans.append(planned_window_span(anchor, *tomorrow, day_offset=1))
    return tuple(sp for sp in spans if sp is not None)


def window_open_at(span: Any, ts: datetime) -> bool:
    """True when ``ts`` is inside the planned window span (``[start, end)``) or any of several (a tuple of spans)."""
    if not span:
        return False
    if isinstance(span[0], datetime):
        return span[0] <= ts < span[1]
    return any(a <= ts < b for a, b in span)
