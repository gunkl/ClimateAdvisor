"""fan_thermostat_decision_compare — old-vs-new differential comparator (Step 2, slice 2).

Unlike ``_nat_vent_may_reactivate()``, production's ``fan_thermostat_check()``
does not RETURN a value — its outcome is implicit in which downstream method
gets called (``_exit_nat_vent()``, ``_deactivate_fan()``, or neither). This
comparator observes those calls during one real invocation to infer the REAL
outcome, and reconstructs the equivalent ``FanThermostatInputs`` to compute the
NEW pure function's predicted outcome — comparing the two without ever
modifying what `fan_thermostat_check()` itself does.

``_deactivate_fan()`` is called by BOTH ``STOP_DEACTIVATE`` and
``STOP_COOLED_TO_FLOOR`` — disambiguated via the ``reason=`` string production
already passes, using the same literal substrings ("free cooling gone" /
"cooled to floor") the real code emits. If neither substring matches a call
that did happen, that's recorded as an unexpected-observation error rather than
silently misclassified — protects against silent breakage if production
wording ever changes.

Shadow mode only (see the "Substitution mode — DEFERRED" note further down for
why): observes real side effects and compares against the new pure function's
prediction; the wrapped method's real behavior is completely untouched.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any


def _reconstruct_inputs(self: Any, indoor: float | None, outdoor: float | None) -> Any:
    """Rebuild the equivalent FanThermostatInputs from a live fan_thermostat_check call."""
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from custom_components.climate_advisor import const  # noqa: PLC0415
    from custom_components.climate_advisor.automation import _in_sleep_window  # noqa: PLC0415
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatInputs  # noqa: PLC0415

    comfort_heat_raw = float(self.config.get("comfort_heat", 70))
    sleep_heat = float(self.config.get(const.CONF_SLEEP_HEAT, comfort_heat_raw))
    in_sleep_window = _in_sleep_window(dt_util.now(), self.config)
    hysteresis = float(self.config.get(const.CONF_NAT_VENT_HYSTERESIS_F, const.NAT_VENT_HYSTERESIS_F))

    return FanThermostatInputs(
        indoor=indoor,
        outdoor=outdoor,
        comfort_heat_raw=comfort_heat_raw,
        sleep_heat=sleep_heat,
        in_sleep_window=in_sleep_window,
        hysteresis=hysteresis,
        natural_vent_active=self._natural_vent_active,
    )


def _classify_observation(exit_called: bool, deactivate_reason: str | None) -> tuple[Any, str | None]:
    """Map observed calls to a FanThermostatOutcome. Returns (outcome, error_or_none)."""
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome  # noqa: PLC0415

    if exit_called:
        return FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT, None
    if deactivate_reason is not None:
        if "free cooling gone" in deactivate_reason:
            return FanThermostatOutcome.STOP_DEACTIVATE, None
        if "cooled to floor" in deactivate_reason:
            return FanThermostatOutcome.STOP_COOLED_TO_FLOOR, None
        return None, f"_deactivate_fan called with unrecognized reason: {deactivate_reason!r}"
    return FanThermostatOutcome.KEEP, None


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


@dataclass
class FanThermostatCall:
    scenario_name: str
    real_outcome: Any  # FanThermostatOutcome | None (None if precondition guard skipped comparison)
    new_outcome: Any  # FanThermostatOutcome

    @property
    def agrees(self) -> bool:
        return self.real_outcome == self.new_outcome


@dataclass
class FanThermostatComparisonRun:
    calls: list[FanThermostatCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def n_agree(self) -> int:
        return sum(1 for c in self.calls if c.agrees)

    @property
    def disagreements(self) -> list[FanThermostatCall]:
        return [c for c in self.calls if not c.agrees]


@contextlib.contextmanager
def _instrumented_fan_thermostat_check(run: FanThermostatComparisonRun, scenario_name: str):
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415
    from custom_components.climate_advisor.fan_thermostat_decision import decide_fan_thermostat_check  # noqa: PLC0415

    original_check = AutomationEngine.fan_thermostat_check
    original_exit = AutomationEngine._exit_nat_vent
    original_deactivate = AutomationEngine._deactivate_fan

    async def _wrapped_check(self: Any, *, indoor: float | None, outdoor: float | None, trigger: str) -> None:
        # Preconditions the real function itself guards on (fan active, not
        # overridden) — mirrored here only to decide whether comparison is
        # meaningful, never to change behavior.
        ca_fan_active = self._fan_running
        fan_override_active = self._fan_override_active

        # Capture the PRE-decision inputs before calling the real check — critical for
        # natural_vent_active, which the STOP_VIA_NAT_VENT_EXIT branch clears as a side
        # effect (via _exit_nat_vent()) during the call itself. Reconstructing inputs
        # AFTER the call read the POST-decision state, producing a false disagreement
        # (real=STOP_VIA_NAT_VENT_EXIT vs new=STOP_DEACTIVATE) once the harness's
        # temp-tick dispatch fix (Step 2) finally exercised this branch via 2 goldens
        # (nat-vent-outdoor-rises-above-indoor-exit, warm_day_ceiling_breach_ac_defense).
        pre_inputs = _reconstruct_inputs(self, indoor, outdoor) if ca_fan_active and not fan_override_active else None

        observed = {"exit_called": False, "deactivate_reason": None}

        async def _tracking_exit(self2: Any, *a: Any, **kw: Any) -> Any:
            observed["exit_called"] = True
            return await original_exit(self2, *a, **kw)

        async def _tracking_deactivate(self2: Any, *a: Any, **kw: Any) -> Any:
            observed["deactivate_reason"] = kw.get("reason") or (a[0] if a else None)
            return await original_deactivate(self2, *a, **kw)

        with (
            patch.object(AutomationEngine, "_exit_nat_vent", _tracking_exit),
            patch.object(AutomationEngine, "_deactivate_fan", _tracking_deactivate),
        ):
            await original_check(self, indoor=indoor, outdoor=outdoor, trigger=trigger)

        if not (ca_fan_active and not fan_override_active):
            return  # matches the real function's own early-return no-op; nothing to compare

        try:
            real_outcome, obs_error = _classify_observation(observed["exit_called"], observed["deactivate_reason"])
            if obs_error:
                run.errors.append(f"{scenario_name}: {obs_error}")
                return
            new_outcome = decide_fan_thermostat_check(pre_inputs)
            run.calls.append(
                FanThermostatCall(scenario_name=scenario_name, real_outcome=real_outcome, new_outcome=new_outcome)
            )
        except Exception as exc:  # noqa: BLE001
            run.errors.append(f"{scenario_name}: comparator error: {type(exc).__name__}: {exc}")

    with patch.object(AutomationEngine, "fan_thermostat_check", _wrapped_check):
        yield


def compare_scenario(scenario: dict, scenario_name: str, run: FanThermostatComparisonRun) -> None:
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    with _instrumented_fan_thermostat_check(run, scenario_name):
        run_production_scenario(scenario)


# ---------------------------------------------------------------------------
# Substitution mode — DEFERRED, not implemented here (see module note below)
# ---------------------------------------------------------------------------
#
# A first draft of `substitute_new_fan_thermostat_decision()` was written and
# then deleted. Faithfully substituting this function's outcome would require
# hand-reconstructing ALL of production's side effects outside of production
# code: the auxiliary `nat_vent_outdoor_rise_exit` event (with
# `_fan_device_label(self.config)`), and the exact `reason=` string for every
# branch (including Check 2's resolved floor value formatted to match
# production's f-string precisely). That reconstruction is real, duplicate
# logic — if it drifts from production even slightly, this test would either
# report false divergences or, worse, silently pass while masking a real bug.
# That's exactly the "workaround that defeats the purpose" to avoid.
#
# The reactivation gate supported clean substitution because it was ALREADY
# extracted as a single callable returning a value (`_nat_vent_may_reactivate()`,
# itself a product of Issue #411's earlier refactor) — substitution there meant
# swapping one return value for another, no side effects to reconstruct.
# `fan_thermostat_check()` inlines its dispatch instead. The real prerequisite
# for safe substitution here is extracting the decision into a real callable in
# PRODUCTION first (mechanical, low-risk, mirrors #411's precedent) — not a
# parallel-testing trick. That extraction is a legitimate Step 3 candidate, not
# done as part of this Step 2 slice.
#
# Shadow-mode testing above is unaffected by this — it only OBSERVES real
# side effects, never reconstructs them, so it remains fully trustworthy on
# its own terms.
