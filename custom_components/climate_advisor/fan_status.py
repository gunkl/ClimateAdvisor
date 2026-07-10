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
