"""Instant arithmetic that stays correct across a daylight-saving change (Issue #1199).

Occupant experience: a manual-override or fan-off grace is told to last N minutes. If the
end time is computed as "local wall-clock now + N minutes", a grace that starts shortly
before the clocks fall back runs an hour too long (the fan's off-hold keeps the occupant's
fan from restarting for an extra hour), and one that starts before spring-forward ends an
hour early (Climate Advisor takes the thermostat back while the occupant still expects the
hold). Python adds a ``timedelta`` to a timezone-aware local datetime as wall-clock time and
ignores the repeated or skipped hour, so the stored end is off by exactly one hour.

The one rule: an END instant is stored as a UTC datetime, built by :func:`instant_after`.
UTC has no repeated or skipped hour, so adding seconds there is exact; an aware datetime
compares against ``dt_util.now()`` as a true instant whatever zone either side is in.
Display code converts with ``dt_util.as_local()`` before formatting (UTC ``strftime`` would
show UTC clock time).

The mirror rule (Issue #1244): ELAPSED time since a stored START instant is measured by
:func:`seconds_since`, never as ``dt_util.now() - self._started_at``. A start stored as
``dt_util.now()`` carries the local ``ZoneInfo``; Python subtracts two datetimes that share a
tzinfo in wall-clock time and ignores the fold, so across fall-back the elapsed time can come
out NEGATIVE (01:58 PDT -> 01:02 PST reads -56 min instead of 4 min) and every
``elapsed < window`` guard reads "recent": the occupant's own thermostat change in the
repeated hour is taken as Climate Advisor's echo, and the fan toggle floor, nat-vent re-entry
lockout and welcome-home spacing last up to an hour too long. Across spring-forward the
elapsed time is an hour too long and those protections end early. Converting both sides to
UTC first is exact. The stores themselves stay local (``.date()`` comparisons, logs and the
dashboard read them); only the arithmetic changes. ``tests/test_dst_elapsed_static.py``
fails on a new raw ``now - self._x`` elapsed pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util


def instant_after(seconds: float, *, now: datetime | None = None) -> datetime:
    """Return the UTC instant ``seconds`` after ``now`` (default: the real current instant).

    ``now`` may be any datetime (local or UTC). A naive one is read in the configured
    Home Assistant timezone, like every other ``dt_util`` conversion. The result is always
    timezone-aware UTC. Never write ``dt_util.as_local(dt_util.as_utc(now) + delta)``: a
    local result re-introduces the fold ambiguity this helper exists to avoid.
    """
    base = dt_util.utcnow() if now is None else dt_util.as_utc(now)
    return base + timedelta(seconds=seconds)


def seconds_since(start: datetime, *, now: datetime | None = None) -> float:
    """Return the real seconds from ``start`` to ``now`` (default: ``dt_util.now()``) (Issue #1244).

    Both sides are converted to UTC first, so the result is the true elapsed time across a
    daylight-saving change whatever zone (or fold) either side carries. Two naive values
    subtract as they are; a naive/aware mix raises ``TypeError`` exactly like the raw
    subtraction it replaces, so every caller's existing "unusable clock" fallback is kept
    (production clocks are always aware; only stubbed clocks are naive). Callers in a module
    with its own ``dt_util`` import pass ``now=dt_util.now()`` so a test that patches that
    module's clock keeps driving the result. Negative when ``start`` is in the future.
    """
    current = dt_util.now() if now is None else now
    if current.tzinfo is None or start.tzinfo is None:
        return (current - start).total_seconds()  # both naive: as before; a mix raises TypeError as before
    return (dt_util.as_utc(current) - dt_util.as_utc(start)).total_seconds()
