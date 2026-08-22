"""Pure composed-state derivation for the fan/WHF lifecycle (Issue #731, Phase 1).

Distinct from ``fan_drift_reconciliation.py``'s ``decide_fan_drift_reconciliation()``
(which answers "has physical drift persisted long enough to self-correct on THIS
tick?" as a one-shot decision) and from ``fan_toggle_rate_limit.py``'s
``decide_fan_toggle_rate_limit()`` (which answers "is this specific toggle command
blocked right now?"). This module answers a different question: **given the
engine's current flags, what STATE is the fan/WHF subsystem in right now, across
every axis that matters?**

Unlike the nat-vent lifecycle (``nat_vent_lifecycle.py``), which collapses to four
mutually exclusive states, the fan subsystem does not collapse to a single flat
enum: physical on/off/drift-suspected, manual override, min-runtime cycling,
WHF-owns-HVAC suppression, and toggle rate-limiting are FIVE genuinely independent
axes — a WHF can simultaneously be physically on, under manual override, NOT
cycling (override suspends the cycler), owning HVAC suppression, AND rate-limited
against its next toggle. Flattening that into one enum would either explode
combinatorially (5 real axes) or silently drop states a status card / future
decision needs to read independently. ``FanLifecycleState`` is a composed
dataclass of five small enums instead, one per axis, matching this codebase's
"composed, not flat" precedent set by ``nat_vent_lifecycle.py``'s own docstring
reasoning.

Purely a read of existing truth — no new state is stored anywhere. This module is
NOT wired into any production decision path in Phase 1; it exists standalone,
tested against the derivation rules below, as the foundation later phases will
build read-only properties and (eventually) delegation onto. See Issue #731 for
the full 9-phase extraction plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class FanPhysicalState(Enum):
    """Ground-truth-adjacent physical state of the fan, per ``_fan_active`` and
    the drift-reconciliation tick counter (``fan_drift_reconciliation.py``)."""

    OFF = "off"
    ON = "on"
    ON_DRIFT_SUSPECTED = "on_drift_suspected"


class FanOverrideState(Enum):
    """Whether a manual (physical/RF-remote) fan override is currently active,
    per ``handle_fan_manual_override()``."""

    NONE = "none"
    ACTIVE = "active"
    ACTIVE_REMOTE_TIMER = "active_remote_timer"


class FanCyclingState(Enum):
    """Min-runtime cycling state, per ``_fan_min_runtime_active`` /
    ``_stop_fan_min_runtime_cycles()``."""

    IDLE = "idle"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class WhfHvacOwnership(Enum):
    """Whether an active WHF session currently owns (suppresses) the thermostat,
    per ``_whf_owns_hvac()``."""

    NONE = "none"
    SUPPRESSING = "suppressing"


class FanRateLimitState(Enum):
    """Whether the next fan toggle command is currently deferred by the
    rapid-cycling backstop, per ``_fan_toggle_rate_limited()``."""

    NOT_DEFERRED = "not_deferred"
    DEFERRED_ACTIVATE = "deferred_activate"
    DEFERRED_DEACTIVATE = "deferred_deactivate"


@dataclass(frozen=True)
class FanLifecycleState:
    """The fan/WHF subsystem's current state, one field per independent axis.

    Not a flat enum — see module docstring for why the five axes are kept
    separate rather than collapsed into a single combined state.
    """

    physical: FanPhysicalState
    override: FanOverrideState
    cycling: FanCyclingState
    hvac_ownership: WhfHvacOwnership
    rate_limit: FanRateLimitState

    @classmethod
    def initial(cls) -> FanLifecycleState:
        """The all-idle state a freshly-constructed ``AutomationEngine`` starts in
        (matches every axis's default field value in ``__init__``)."""
        return cls(
            physical=FanPhysicalState.OFF,
            override=FanOverrideState.NONE,
            cycling=FanCyclingState.IDLE,
            hvac_ownership=WhfHvacOwnership.NONE,
            rate_limit=FanRateLimitState.NOT_DEFERRED,
        )


@dataclass(frozen=True)
class FanLifecycleInputs:
    """Every flag the derivation reads — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      fan_active              -> self._fan_active
      fan_drift_tick_count    -> self._fan_drift_tick_count (persisted across
                                  backstop ticks; see fan_drift_reconciliation.py's
                                  FanDriftInputs.tick_count for the same field read
                                  by the drift-reconciliation decision itself)
      fan_override_active     -> self._fan_override_active
      fan_remote_timer_hours  -> self._fan_remote_timer_hours (None means either
                                  no override, or a non-remote-triggered override —
                                  disambiguated by fan_override_active)
      fan_min_runtime_active  -> self._fan_min_runtime_active
      fan_mode                -> config CONF_FAN_MODE (this module never reads
                                  self.config directly — the caller resolves it)
      pre_fan_hvac_mode       -> self._pre_fan_hvac_mode (non-None means a WHF
                                  suppression session owns the thermostat)
      fan_rate_limited_until  -> self._fan_rate_limited_until
      fan_rate_limited_direction -> self._fan_rate_limited_direction ("activate" /
                                  "deactivate", set alongside fan_rate_limited_until)
      now                     -> caller-resolved wall-clock time (this module never
                                  touches dt_util.now(), same convention as
                                  nat_vent_gate.py's in_sleep_window) — accepted for
                                  interface symmetry with the other lifecycle/gate
                                  modules and future phases that may need it; no
                                  derivation rule in this phase reads it directly,
                                  since fan_rate_limited_until/direction alone
                                  already answer "deferred or not" without an
                                  elapsed-time comparison (that comparison is
                                  fan_toggle_rate_limit.py's job, not this module's).
    """

    fan_active: bool
    fan_drift_tick_count: int
    fan_override_active: bool
    fan_remote_timer_hours: float | None
    fan_min_runtime_active: bool
    fan_mode: str
    pre_fan_hvac_mode: str | None
    fan_rate_limited_until: datetime | None
    fan_rate_limited_direction: str | None
    now: datetime


_FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
_FAN_MODE_BOTH = "both"


def _derive_physical(inputs: FanLifecycleInputs) -> FanPhysicalState:
    """Pure reimplementation of the physical-state half of
    _reconcile_fan_physical_drift()'s ground truth (Issue #423).

    fan_drift_tick_count > 0 only while a drift AWAITING confirmation is in
    progress (fan_drift_reconciliation.decide_fan_drift_reconciliation()'s
    AWAITING outcome) — by construction this only occurs while fan_active is
    still True (RESET/CORRECT both zero the tick count, and RESET/CORRECT are
    the only outcomes reachable when fan_active is False or drift resolves).
    Safety-critical invariant: a nonzero tick count must never be read as OFF —
    see tests/test_fan_lifecycle.py's explicit regression test.
    """
    if not inputs.fan_active:
        return FanPhysicalState.OFF
    if inputs.fan_drift_tick_count > 0:
        return FanPhysicalState.ON_DRIFT_SUSPECTED
    return FanPhysicalState.ON


def _derive_override(inputs: FanLifecycleInputs) -> FanOverrideState:
    """Pure reimplementation of handle_fan_manual_override()'s override bookkeeping."""
    if not inputs.fan_override_active:
        return FanOverrideState.NONE
    if inputs.fan_remote_timer_hours is not None:
        return FanOverrideState.ACTIVE_REMOTE_TIMER
    return FanOverrideState.ACTIVE


def _derive_cycling(inputs: FanLifecycleInputs) -> FanCyclingState:
    """Pure reimplementation of _stop_fan_min_runtime_cycles()/_fan_cycle_on()'s
    cycling-state bookkeeping. handle_fan_manual_override() always calls
    _stop_fan_min_runtime_cycles() (clearing fan_min_runtime_active) before
    setting fan_override_active=True, so the two flags are never both True in
    practice — SUSPENDED and ACTIVE are mutually reachable-exclusive, but ACTIVE
    is still checked last here in case that invariant ever loosens."""
    if inputs.fan_min_runtime_active:
        return FanCyclingState.ACTIVE
    if inputs.fan_override_active:
        return FanCyclingState.SUSPENDED
    return FanCyclingState.IDLE


def _derive_hvac_ownership(inputs: FanLifecycleInputs) -> WhfHvacOwnership:
    """Pure reimplementation of _whf_owns_hvac() (Issue #392 Fix 1b): WHF/BOTH
    archetype AND an active suppression session (pre_fan_hvac_mode is not None)."""
    if inputs.fan_mode in (_FAN_MODE_WHOLE_HOUSE, _FAN_MODE_BOTH) and inputs.pre_fan_hvac_mode is not None:
        return WhfHvacOwnership.SUPPRESSING
    return WhfHvacOwnership.NONE


def _derive_rate_limit(inputs: FanLifecycleInputs) -> FanRateLimitState:
    """Pure reimplementation of the rate-limit bookkeeping set by
    _fan_toggle_rate_limited() (Issue #641/#649)."""
    if inputs.fan_rate_limited_until is None:
        return FanRateLimitState.NOT_DEFERRED
    if inputs.fan_rate_limited_direction == "activate":
        return FanRateLimitState.DEFERRED_ACTIVATE
    if inputs.fan_rate_limited_direction == "deactivate":
        return FanRateLimitState.DEFERRED_DEACTIVATE
    return FanRateLimitState.NOT_DEFERRED


def derive_fan_lifecycle_state(inputs: FanLifecycleInputs) -> FanLifecycleState:
    """Pure derivation of the current fan/WHF composed state from existing flags.

    Each axis is derived independently — see the per-axis helpers above for the
    precedence within each axis and its correspondence to the real production
    method it mirrors.
    """
    return FanLifecycleState(
        physical=_derive_physical(inputs),
        override=_derive_override(inputs),
        cycling=_derive_cycling(inputs),
        hvac_ownership=_derive_hvac_ownership(inputs),
        rate_limit=_derive_rate_limit(inputs),
    )
