"""Pure decision core for mid-session nat-vent fan cycling (Issue #698, Epic
#594 Phase R, Phase 2d).

Extends the "architecture-reset" functional-core methodology (``nat_vent_gate.py``,
``nat_vent_exit.py``) to the one part of the nat-vent lifecycle that was still
100% legacy: the thermostat-style cycling *within* an already-active session —
turning the fan hardware off when indoor cools to the comfort midpoint minus
hysteresis, and back on when indoor warms to the midpoint plus hysteresis
(subject to the outdoor-warm reactivation guard). This is deliberately NOT an
exit decision — the *session* stays active in every case this module covers;
only the fan hardware's on/off state is decided here.

Pure reimplementation of ``automation.py``'s ``nat_vent_temperature_check()``
cycling branches ONLY (the code below its hard-floor exit check). Callers MUST
run the exit chain (``nat_vent_exit.decide_nat_vent_exit()``, or
``nat_vent_fsm.transition()`` which already sequences it first) and only call
``decide_nat_vent_cycling()`` when that returned ``NatVentExitReason.NONE`` —
hard-floor and the other 4 exit reasons take priority over cycling, exactly as
``nat_vent_temperature_check()``'s own priority order (hard-floor check first,
cycling second) already establishes.

Delegates the on-threshold outdoor-warm guard to
``fan_thermostat_decision.is_outdoor_rise_exit()`` — the single source of truth
already shared by ``nat_vent_exit.py``'s OUTDOOR_RISE check and
``fan_thermostat_decision.py``'s own Check 1 — rather than re-deriving the
``outdoor >= indoor`` comparison a third time (Issue #698's Decision 3; this
was previously hand-duplicated inline in ``nat_vent_temperature_check()``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .fan_thermostat_decision import is_outdoor_rise_exit


@dataclass(frozen=True)
class NatVentCyclingInputs:
    """Every input the cycling decision may read — explicit, nothing hidden.

    Field-by-field correspondence to ``nat_vent_temperature_check()``:
      indoor, outdoor      -> current_temp / outdoor (caller-sourced, both params)
      comfort_heat_raw     -> config comfort_heat
      sleep_heat           -> config CONF_SLEEP_HEAT, falls back to comfort_heat_raw
      in_sleep_window      -> _in_sleep_window(dt_util.now(), config)
      comfort_cool         -> config comfort_cool
      hysteresis           -> config CONF_NAT_VENT_HYSTERESIS_F
      fan_hardware_active  -> self._fan_active (the live hardware-on/off flag,
                               distinct from the session flag _natural_vent_active)
    """

    indoor: float | None
    outdoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    comfort_cool: float
    hysteresis: float
    fan_hardware_active: bool


@dataclass(frozen=True)
class NatVentCyclingDecision:
    """The cycling decision, plus the computed thresholds the shell's logging/
    event payload needs (avoids re-deriving them a second time in
    ``automation.py`` — the pure function computes them once, the shell just
    reads them off the decision, matching ``NatVentExitDecision``'s precedent)."""

    fan_should_be_active: bool
    off_threshold: float
    on_threshold: float
    outdoor_rise_blocked: bool = False


def decide_nat_vent_cycling(inputs: NatVentCyclingInputs) -> NatVentCyclingDecision:
    """Pure reimplementation of ``nat_vent_temperature_check()``'s two cycling
    branches ONLY (automation.py, the code below its hard-floor exit check).
    Same threshold math, same priority (cycle-off checked before cycle-on),
    same outdoor-warm reactivation guard — exists to be differentially
    validated against the real method, not to introduce new behavior.

    Callers must only invoke this after confirming
    ``decide_nat_vent_exit()``/``nat_vent_fsm.transition()`` returned no exit
    for this tick — this function does not re-check the hard floor or any of
    the other 4 exit reasons (the session-active flag isn't one of its inputs,
    same exclusion-by-construction as ``nat_vent_exit.py``'s own docstring).

    NOTE on this function's own ``outdoor_rise_blocked`` branch: when called
    correctly (only after the exit chain returns NONE), that branch is
    structurally unreachable in practice — ``decide_nat_vent_exit()``'s check 4
    (OUTDOOR_RISE) already ends the whole session on ANY ``outdoor >= indoor``
    reading, unconditionally, so a NONE exit result already guarantees
    ``outdoor < indoor`` by the time this function runs. It's kept for this
    function's own standalone correctness (direct unit tests call it in
    isolation, without an exit-chain check first) and any future caller that
    doesn't share that same precondition — not because it's expected to fire
    on the wired ``nat_vent_temperature_check()``/``transition()`` call paths.
    """
    if inputs.in_sleep_window:
        # e.g. 65+1=66; off at 65, on at 67 — matches nat_vent_temperature_check()'s
        # sleep-window comment exactly.
        nat_vent_target = inputs.sleep_heat + inputs.hysteresis
    else:
        nat_vent_target = (inputs.comfort_heat_raw + inputs.comfort_cool) / 2.0
    off_threshold = nat_vent_target - inputs.hysteresis
    on_threshold = nat_vent_target + inputs.hysteresis

    if inputs.indoor is None:
        # No live reading -- hold the fan in its current state (matches
        # nat_vent_temperature_check(), which never fires either threshold
        # branch when current_temp is unavailable -- the caller only invokes
        # this with a real current_temp float, see that method's own
        # signature, but this keeps the pure function total/defensive).
        return NatVentCyclingDecision(
            fan_should_be_active=inputs.fan_hardware_active,
            off_threshold=off_threshold,
            on_threshold=on_threshold,
        )

    if inputs.fan_hardware_active and inputs.indoor <= off_threshold:
        return NatVentCyclingDecision(
            fan_should_be_active=False, off_threshold=off_threshold, on_threshold=on_threshold
        )

    if not inputs.fan_hardware_active and inputs.indoor >= on_threshold:
        if is_outdoor_rise_exit(indoor=inputs.indoor, outdoor=inputs.outdoor):
            # Outdoor has also warmed past indoor -- reactivating would run the
            # fan against reversed airflow for no cooling benefit. Skip
            # reactivation; fan stays off (session remains active).
            return NatVentCyclingDecision(
                fan_should_be_active=False,
                off_threshold=off_threshold,
                on_threshold=on_threshold,
                outdoor_rise_blocked=True,
            )
        return NatVentCyclingDecision(fan_should_be_active=True, off_threshold=off_threshold, on_threshold=on_threshold)

    # Neither threshold crossed -- fan holds its current hardware state.
    return NatVentCyclingDecision(
        fan_should_be_active=inputs.fan_hardware_active, off_threshold=off_threshold, on_threshold=on_threshold
    )
