"""full_surface_integration — Step 4: prove the WHOLE nat-vent decision surface is
load-bearing simultaneously, not just each pure function individually.

Every mechanism below already has its own dedicated positive control
(nat_vent_gate_integration.py, fan_thermostat_decision_integration.py) or was
proven load-bearing inline in its own test file (fan_drift_reconciliation,
nat_vent_reactivation_lockout, grace, setpoint retry/verify, pre-cool target,
pre-cool reschedule). Those checks corrupt ONE function at a time. What they
cannot show on their own is that the surface holds together as a whole: that
corrupting every extracted decision point AT ONCE still produces detectable,
non-cancelling divergence across the full scenario corpus — i.e. that nothing
is silently masking or overriding anything else when everything is wrong
simultaneously, and that "end-to-end" (the plan's own Step-4 language) is
genuinely validated, not just each piece in isolation.

`break_entire_nat_vent_surface()` composes every individual corruption into
one context manager. Each corruption preserves the same safety invariants its
own dedicated positive control already established (e.g. the gate's None-input
guarantee) so a scenario doesn't crash instead of diverging.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from .fan_thermostat_decision_integration import break_fan_thermostat_decision
from .nat_vent_gate_integration import break_nat_vent_gate


@contextlib.contextmanager
def _break_fan_drift_reconciliation():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import (
        decide_fan_drift_reconciliation as original,  # noqa: PLC0415
    )
    from custom_components.climate_advisor.fan_drift_reconciliation import FanDriftOutcome  # noqa: PLC0415

    rotation = {
        FanDriftOutcome.RESET: FanDriftOutcome.AWAITING,
        FanDriftOutcome.NOOP: FanDriftOutcome.CORRECT,
        FanDriftOutcome.AWAITING: FanDriftOutcome.NOOP,
        FanDriftOutcome.CORRECT: FanDriftOutcome.RESET,
    }

    def _broken(inputs: Any) -> tuple[Any, int]:
        outcome, tick_count = original(inputs)
        return rotation[outcome], tick_count

    with patch("custom_components.climate_advisor.automation.decide_fan_drift_reconciliation", _broken):
        yield


@contextlib.contextmanager
def _break_reactivation_lockout():
    from unittest.mock import patch  # noqa: PLC0415

    # Must capture `original` BEFORE the patch is applied — importing it lazily inside
    # _broken() would re-read automation.py's module-global AFTER patching, resolving
    # to _broken itself and recursing infinitely (caught the hard way: RecursionError).
    from custom_components.climate_advisor.automation import is_reactivation_locked_out as original  # noqa: PLC0415

    def _broken(*, outdoor_exit_time: datetime | None, now: datetime, lockout_seconds: float) -> bool:
        # Invert, but preserve "no prior exit recorded -> never locked out" — a blind
        # invert would force True with no exit time on record, which no real call
        # site's caller expects (the lockout concept requires an exit to have
        # happened at all).
        if outdoor_exit_time is None:
            return False
        return not original(outdoor_exit_time=outdoor_exit_time, now=now, lockout_seconds=lockout_seconds)

    with patch("custom_components.climate_advisor.automation.is_reactivation_locked_out", _broken):
        yield


@contextlib.contextmanager
def _break_grace_start():
    from unittest.mock import patch  # noqa: PLC0415

    def _broken(**_kwargs: Any) -> None:
        # Always disable grace entirely — the strongest, simplest observable
        # corruption: any scenario that starts a grace period and later checks
        # its expiry/notification behavior will diverge.
        return None

    with patch("custom_components.climate_advisor.automation.decide_grace_start", _broken):
        yield


@contextlib.contextmanager
def _break_setpoint_retry_action():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import decide_setpoint_retry_action as original  # noqa: PLC0415
    from custom_components.climate_advisor.desired_state import SetpointRetryAction  # noqa: PLC0415

    rotation = {
        SetpointRetryAction.SUPERSEDED: SetpointRetryAction.DIRECT_RETRY,
        SetpointRetryAction.DIRECT_RETRY: SetpointRetryAction.NUDGE_THEN_TARGET,
        SetpointRetryAction.NUDGE_THEN_TARGET: SetpointRetryAction.SUPERSEDED,
    }

    def _broken(*, current_write_seq: int, retry_write_seq: int, reject_streak: int) -> Any:
        return rotation[
            original(current_write_seq=current_write_seq, retry_write_seq=retry_write_seq, reject_streak=reject_streak)
        ]

    with patch("custom_components.climate_advisor.automation.decide_setpoint_retry_action", _broken):
        yield


@contextlib.contextmanager
def _break_setpoint_verify():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import decide_setpoint_verify as original  # noqa: PLC0415
    from custom_components.climate_advisor.setpoint_verify_decision import SetpointVerifyOutcome  # noqa: PLC0415

    rotation = {
        SetpointVerifyOutcome.STALE: SetpointVerifyOutcome.WITHIN_TOLERANCE,
        SetpointVerifyOutcome.NO_SETPOINT: SetpointVerifyOutcome.REASSERT,
        SetpointVerifyOutcome.OVERRIDE_ACTIVE: SetpointVerifyOutcome.STALE,
        SetpointVerifyOutcome.NO_READING: SetpointVerifyOutcome.OVERRIDE_ACTIVE,
        SetpointVerifyOutcome.WITHIN_TOLERANCE: SetpointVerifyOutcome.REASSERT,
        SetpointVerifyOutcome.REASSERT: SetpointVerifyOutcome.WITHIN_TOLERANCE,
    }

    def _broken(**kwargs: Any) -> Any:
        return rotation[original(**kwargs)]

    with patch("custom_components.climate_advisor.automation.decide_setpoint_verify", _broken):
        yield


@contextlib.contextmanager
def _break_pre_cool_target():
    from unittest.mock import patch  # noqa: PLC0415

    # Capture the real implementation directly from its SOURCE module (automation.py),
    # not via a lazy re-import inside _broken() — the latter would resolve to whichever
    # of the two patches below is applied at call time and recurse infinitely.
    from custom_components.climate_advisor.automation import compute_pre_cool_target as original  # noqa: PLC0415

    def _broken(config: dict, setback_modifier: float) -> float:
        # A large, obviously-wrong offset — not a sign flip (which could coincidentally
        # land back on a valid clamp boundary for some configs) and not a huge enough
        # jump to push the value out of a physically plausible temperature range that
        # downstream formatting/logging might choke on.
        return original(config, setback_modifier) + 15.0

    # Patch BOTH import sites: automation.py's own module-global reference (used by
    # handle_pre_cool()) and coordinator.py's separately-bound imported reference
    # (used by the 4 chart/scheduling call sites) — patching one does not affect
    # the other's already-resolved module-global binding.
    with (
        patch("custom_components.climate_advisor.automation.compute_pre_cool_target", _broken),
        patch("custom_components.climate_advisor.coordinator.compute_pre_cool_target", _broken),
    ):
        yield


@contextlib.contextmanager
def _break_pre_cool_reschedule():
    from unittest.mock import patch  # noqa: PLC0415

    def _broken(**_kwargs: Any) -> None:
        # Always suppress the reschedule — the strongest observable corruption for
        # this mechanism (no scenario currently exercises a nat-vent-exit-ahead-of-
        # schedule transition, a known, named coverage gap — see the CLI report).
        return None

    with patch("custom_components.climate_advisor.coordinator._decide_pre_cool_reschedule", _broken):
        yield


@contextlib.contextmanager
def break_entire_nat_vent_surface():
    """Compose every individual corruption into one simultaneous break.

    Order does not matter for correctness (each corruption patches a distinct
    name), but is listed in roughly call-order within a typical nat-vent cycle:
    gate -> tick-level stop check -> physical drift reconciliation ->
    reactivation lockout -> grace -> setpoint verify/retry -> pre-cool target
    and reschedule.
    """
    with (
        break_nat_vent_gate(),
        break_fan_thermostat_decision(),
        _break_fan_drift_reconciliation(),
        _break_reactivation_lockout(),
        _break_grace_start(),
        _break_setpoint_retry_action(),
        _break_setpoint_verify(),
        _break_pre_cool_target(),
        _break_pre_cool_reschedule(),
    ):
        yield
