"""Code-to-word params for Activity Log messages (Issue #1664, the Activity Log mirror of #1659).

An Activity Log row used to pass a raw code (``cool``, ``hot``, ``away``, ``manual``...) as a plain message argument,
so a translated row read "Thermostat set to cool" with the English code in the middle. Each helper here returns the
code's catalog word as an ``i18n.Code`` when the value is exactly a known code, and the value unchanged otherwise
(a ``Msg``, ``""``, ``None``, a code nobody listed). Every English catalog word IS the code, so the English text of a
row (the stored ``event_log`` record, the AI input) stays byte-identical.

Not covered on purpose (identifiers, shown as written in every language): trigger / origin / context names, the CA
fan configuration id, a thermostat's or RF remote's own fan-mode and speed values (vendor-defined), invariant and
promise ids, versions, entity ids.
"""

from __future__ import annotations

from typing import Any

from . import i18n
from .hvac_action import HVAC_ACTION_WORD_CODES, HVAC_MODE_WORD_CODES

_DAY = {
    "hot": "activity.word.day.hot",
    "warm": "activity.word.day.warm",
    "mild": "activity.word.day.mild",
    "cool": "activity.word.day.cool",
    "cold": "activity.word.day.cold",
}
_TREND = {
    "warming": "activity.word.trend.warming",
    "cooling": "activity.word.trend.cooling",
    "stable": "activity.word.trend.stable",
}
_OCCUPANCY = {
    "home": "activity.word.occupancy.home",
    "away": "activity.word.occupancy.away",
    "vacation": "activity.word.occupancy.vacation",
    "guest": "activity.word.occupancy.guest",
}
_SOURCE = {
    "automation": "activity.word.source.automation",
    "manual": "activity.word.source.manual",
    "normal": "activity.word.source.normal",
    "setpoint": "activity.word.source.setpoint",
}
_OUTCOME = {
    "blocked": "activity.word.outcome.blocked",
    "dry_run": "activity.word.outcome.dry_run",
    "failed": "activity.word.outcome.failed",
    "superseded": "activity.word.outcome.superseded",
    "not_sent": "activity.word.outcome.not_sent",
}
_EDGE = {
    "floor": "activity.word.edge.floor",
    "ceiling": "activity.word.edge.ceiling",
}
_BAND = {
    "comfort": "activity.word.band.comfort",
    "sleep": "activity.word.band.sleep",
    "away": "activity.word.band.away",
    "vacation": "activity.word.band.vacation",
}
# The ``thermostat_mode_set`` reason codes (``automation._MODE_SET_REASON_LABELS``), as "requested by" words.
_REQUESTED_BY = {
    "selector": "activity.word.requested_by.selector",
    "pause": "activity.word.requested_by.pause",
    "restore": "activity.word.requested_by.restore",
    "tou": "activity.word.requested_by.tou",
    "ode_guard": "activity.word.requested_by.ode_guard",
    "pre_cool": "activity.word.requested_by.pre_cool",
    "nat_vent": "activity.word.requested_by.nat_vent",
    "user": "activity.word.requested_by.user",
    "startup": "activity.word.requested_by.startup",
    "other": "activity.word.requested_by.other",
}


def _from(table: dict[str, str], value: Any) -> Any:
    return i18n.Code(table[value]) if isinstance(value, str) and value in table else value


def mode_w(value: Any) -> Any:
    """An ``hvac_mode`` (or comfort family heat/cool) code as its word; anything else unchanged."""
    if isinstance(value, str) and value in HVAC_MODE_WORD_CODES:
        return i18n.Code(f"common.hvac_mode_word.{value}")
    return value


def action_w(value: Any) -> Any:
    """An ``hvac_action`` code as its word; anything else unchanged."""
    if isinstance(value, str) and value in HVAC_ACTION_WORD_CODES:
        return i18n.Code(f"common.hvac_action.{value}")
    return value


def family_w(value: Any) -> Any:
    """A comfort family (``heating`` / ``cooling``, or a ``heat`` / ``cool`` mode) as its word."""
    if isinstance(value, str) and value in ("heating", "cooling"):
        return action_w(value)
    return mode_w(value)


def day_w(value: Any) -> Any:
    """A day-type code (hot / warm / mild / cool / cold) as its word."""
    return _from(_DAY, value)


def trend_w(value: Any) -> Any:
    """A trend code (warming / cooling / stable) as its word."""
    return _from(_TREND, value)


def occupancy_w(value: Any) -> Any:
    """An occupancy code (home / away / vacation / guest) as its word."""
    return _from(_OCCUPANCY, value)


def source_w(value: Any) -> Any:
    """An override / grace source code (automation / manual / normal / setpoint) as its word."""
    return _from(_SOURCE, value)


def outcome_w(value: Any) -> Any:
    """A write outcome code (blocked / dry_run / failed / superseded / not_sent) as its word."""
    return _from(_OUTCOME, value)


def edge_w(value: Any) -> Any:
    """A band edge code (floor / ceiling) as its word."""
    return _from(_EDGE, value)


def band_w(value: Any) -> Any:
    """A band context code (comfort / sleep / away / vacation) as its word."""
    return _from(_BAND, value)


def requested_by_w(value: Any) -> Any:
    """A ``thermostat_mode_set`` reason code as its "requested by" word."""
    return _from(_REQUESTED_BY, value)
