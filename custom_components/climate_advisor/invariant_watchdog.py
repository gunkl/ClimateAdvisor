"""Hard system invariants that must never be violated (Issue #749).

Deliberately minimal — this is NOT a general validation framework. It holds a small,
explicit list of absolute system states, each checked by a pure function over
**ground-truth** inputs only (live sensor/thermostat reads), never internal automation
bookkeeping (override flags, grace flags, session flags). Ground truth is the point: the
2026-08-22 incident that motivated this module (Issue #739/#748 — the AC and the
whole-house fan running simultaneously) happened while CA's own internal bookkeeping
(``_fan_override_active``, ``_fan_remote_timer_hours``) was entirely self-consistent. Only a
check that ignores that bookkeeping and reads the physical world directly can catch the next
bug of this shape, whatever its internal cause turns out to be.

This module detects and alerts only. It never issues a corrective command — the automation
engine's own command-layer fixes (e.g. ``_deactivate_fan()``/``_suppress_hvac_for_whf()``)
are the sole enforcement path for the invariants checked here. A violation firing after such
a fix has landed is itself a signal that something new needs investigating, not something to
silently paper over with a second, independent enforcement path (this codebase has been bitten
before by two parallel copies of the same rule drifting out of sync).

Changing the invariant set (adding, removing, or loosening a check) is treated the same as
this project's LOCKED golden-scenario policy (see CLAUDE.md's "Golden Simulation Test
Policy"): it requires explicit human review — show the proposed invariant in human-readable
form, confirm it represents a genuine hard constraint, only then land it. Do not add a new
entry to ``INVARIANTS`` without that sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import FAN_MODE_BOTH, FAN_MODE_WHOLE_HOUSE

_ACTIVE_HVAC_ACTIONS = {"heating", "cooling"}


@dataclass(frozen=True)
class InvariantViolation:
    """A single hard-invariant violation, ready to log/notify/surface."""

    name: str
    detail: str


def check_ac_whf_mutex(
    *,
    hvac_action: str | None,
    whf_physically_on: bool | None,
    fan_mode: str,
) -> InvariantViolation | None:
    """AC and the whole-house fan must never both be physically active at once.

    Scoped to ``FAN_MODE_WHOLE_HOUSE``/``FAN_MODE_BOTH`` archetypes only —
    ``FAN_MODE_HVAC`` coexists with the compressor by design (that fan IS the thermostat's
    own blower, not a whole-house fan fighting it for outdoor air exchange). See
    docs/08-COMPUTATION-REFERENCE.md's WHF-vs-HVAC-fan distinct-physics note.

    Args:
        hvac_action: The thermostat's live ``hvac_action`` attribute (ground truth —
            "heating"/"cooling"/"fan"/"idle"/"off"/etc.), not the commanded ``hvac_mode``.
        whf_physically_on: The WHF's live physical state (ground truth, e.g. from
            ``coordinator._get_fan_physical_state()``). ``None`` means unknown (fan state
            feedback not configured/available) — a violation cannot be confirmed without a
            physical read, so this returns no violation rather than guessing.
        fan_mode: The configured ``CONF_FAN_MODE`` value.
    """
    if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if whf_physically_on is not True:
        return None
    if hvac_action is None:
        return None
    if str(hvac_action).lower() not in _ACTIVE_HVAC_ACTIONS:
        return None
    return InvariantViolation(
        name="ac_whf_mutex",
        detail=(
            f"AC is actively {hvac_action} while the whole-house fan is physically running — "
            "these must never run at the same time."
        ),
    )


# Deliberately a flat list, not a registry/plugin system — add a new entry only with the
# explicit human sign-off described in this module's docstring.
INVARIANTS = [check_ac_whf_mutex]


def run_invariant_checks(
    *,
    hvac_action: str | None,
    whf_physically_on: bool | None,
    fan_mode: str,
) -> list[InvariantViolation]:
    """Run every registered hard invariant and return any violations found.

    Cheap, synchronous, pure dict/attribute lookups only — safe to call every coordinator
    update cycle with no executor offload.
    """
    violations = []
    for check in INVARIANTS:
        violation = check(hvac_action=hvac_action, whf_physically_on=whf_physically_on, fan_mode=fan_mode)
        if violation is not None:
            violations.append(violation)
    return violations
