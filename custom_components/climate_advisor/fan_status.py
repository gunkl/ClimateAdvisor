"""Fan-status suppression predicate for Climate Advisor.

``ClimateAdvisorCoordinator._compute_fan_status()`` returns one of seven string
values (see CLAUDE.md's "Fan Status Values" table). Multiple call sites need to
answer the same question — "is the thermostat reporting fan activity that CA's
own fan control already explains, or is this a genuine contradiction worth a
warning?" — and each has historically hand-rolled its own allow-list of "active"
values (Issue #458, following the same "sibling threshold drift" pattern as
#400/#402/#417/#456). This module is the single source of truth for that
predicate, mirroring the existing ``temperature.py`` precedent: a tiny,
dependency-free utility module imported by both ``coordinator.py`` and
``ai_skills_activity.py``.
"""

from __future__ import annotations

from .const import REMOTE_TIMER_EVENT_HOURS

FAN_STATUS_ACTIVE_VALUES: frozenset[str] = frozenset(
    {
        "active",
        "active (unconfirmed)",
        "running (manual override)",
        "running (untracked)",
    }
)


def is_ca_fan_running(fan_status: str) -> bool:
    """True if `fan_status` represents fan activity CA can account for.

    `fan_status` is the return value of ``_compute_fan_status()``. Used to
    suppress a false "state contradiction" warning when the thermostat reports
    `hvac_action="fan"` while `hvac_mode="off"` — expected whenever CA itself
    activated fan-only mode (natural ventilation, manual override, or an
    untracked-but-real fan run), not a genuine data inconsistency.

    Must include all four non-inactive, non-disabled active values from the
    "Fan Status Values" table — omitting any one (as `ai_skills_activity.py`
    omitted `"active (unconfirmed)"` before Issue #458) causes a fan legitimately
    in that state to be misreported as a contradiction.
    """
    return fan_status in FAN_STATUS_ACTIVE_VALUES


def parse_remote_timer_event(event_type: str | None) -> tuple[bool, float | None]:
    """Parse a QuietCool RF remote ``event_type`` token into a timer decision.

    `event_type` is read from an ``event.*`` entity's
    ``attributes["event_type"]`` (see docs/fan-remote-spec.md for the firmware
    contract — ``gunkl/quietcool-house-fan``). This is the single source of
    truth for the token-to-hours mapping (``const.REMOTE_TIMER_EVENT_HOURS``);
    callers must not re-implement the mapping inline (Issue #486, following the
    "sibling threshold drift" lesson from #400/#402/#417/#456/#458).

    Returns ``(is_timer_event, hours)``:
    - Recognized timer token (``timer_1h``..``timer_12h``) -> ``(True, <hours>)``
    - ``timer_none`` -> ``(True, None)`` (use CA's configured grace duration)
    - Any other token (``on``, ``off``, speed tokens, unknown, ``None``) ->
      ``(False, None)`` — out of scope for this feature; caller should ignore.
    """
    if event_type not in REMOTE_TIMER_EVENT_HOURS:
        return False, None
    return True, REMOTE_TIMER_EVENT_HOURS[event_type]
