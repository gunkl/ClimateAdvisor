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

Scope: this covers END instants only (``now + duration``). Elapsed time measured as
``dt_util.now() - self._stored_local_now`` has the same wall-clock flaw and is a separate,
wider class (see the Issue #1199 follow-up).
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
