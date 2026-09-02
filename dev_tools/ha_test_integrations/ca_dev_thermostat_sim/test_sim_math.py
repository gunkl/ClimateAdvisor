"""Verification script for the ODE math used by SimulatedThermostat (climate.py).

NOT a pytest test — plain script, run directly: `python test_sim_math.py`.

Why this isn't a real import test
----------------------------------
The plan for this dev tool was to import `_simulate_indoor_physics` directly
from `custom_components/climate_advisor/coordinator.py` and run it against a
few known input/output pairs. That import was checked in this environment
and does NOT work standalone:

    $ python -c "import homeassistant"
    ModuleNotFoundError: No module named 'homeassistant'

`coordinator.py` imports `homeassistant.*` at module level (EVENT_CALL_SERVICE,
HomeAssistant, entity_registry, async_track_time_interval, DataUpdateCoordinator,
dt_util, etc. — see the top of the file), so merely importing the module to reach
the one pure function inside it requires a working `homeassistant` package. No
such package is installed in this repo/venv, and none is expected to be (this
repo is the integration source, not an HA runtime).

So instead, this script hand-derives `_simulate_indoor_physics`'s formula from
its source (coordinator.py:9313-9363, read in full — not just the signature)
and re-implements it here ONLY for verification purposes — this file is never
imported by climate.py or any shipped code, so it does not violate the DRY
requirement that the real simulator entity call the production function.

The formula, transcribed from the real function:

    k_p = k_passive
    q = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint: q = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint: q = -abs(k_active)
        else:
            # legacy threshold inference (not used by climate.py, which always
            # passes hvac_mode explicitly)
            if setpoint >= comfort_heat and t_start < setpoint: q = abs(k_active)
            elif setpoint <= comfort_cool and t_start > setpoint: q = -abs(k_active)

    exp_kp = exp(k_p * dt_hours)
    t_next = (
        t_outdoor + (t_start - t_outdoor) * exp_kp + (q / k_p) * (exp_kp - 1)
        if k_p != 0 else
        t_start + q * dt_hours
    )
    # Clamp: heating won't overshoot setpoint; cooling won't undershoot.
    if setpoint is not None:
        if q > 0: t_next = min(t_next, setpoint)
        elif q < 0: t_next = max(t_next, setpoint)

Three cases are hand-verified below against a from-scratch re-derivation of
that formula (computed independently with Python's math.exp, not copy-pasted
from any cached run) to sanity-check the transcription above matches what
climate.py actually calls.

If a `homeassistant` package is ever installed in this venv, replace this
script's `_reference_simulate_indoor_physics` with a real
`from custom_components.climate_advisor.coordinator import _simulate_indoor_physics`
import and delete the hand-transcribed copy — the whole point of this file is
to fall back to hand-verification only when the real import is impossible.
"""

from __future__ import annotations

import math


def _reference_simulate_indoor_physics(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float | None,
    dt_hours: float,
    setpoint: float | None,
    *,
    comfort_heat: float,
    comfort_cool: float,
    hvac_mode: str | None = None,
) -> float:
    """Hand-transcribed copy of coordinator.py:9313-9363, for verification only.

    climate.py does NOT use this function — it imports the real one. See the
    module docstring above for why this duplicate exists.
    """
    k_p = k_passive
    q = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint:
                q = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint:
                q = -abs(k_active)
        else:
            if setpoint >= comfort_heat and t_start < setpoint:
                q = abs(k_active)
            elif setpoint <= comfort_cool and t_start > setpoint:
                q = -abs(k_active)

    exp_kp = math.exp(k_p * dt_hours)
    t_next = (
        t_outdoor + (t_start - t_outdoor) * exp_kp + (q / k_p) * (exp_kp - 1) if k_p != 0 else t_start + q * dt_hours
    )

    if setpoint is not None:
        if q > 0:
            t_next = min(t_next, setpoint)
        elif q < 0:
            t_next = max(t_next, setpoint)
    return t_next


def _check(label: str, actual: float, expected: float, tol: float = 1e-9) -> None:
    ok = abs(actual - expected) < tol
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: actual={actual!r} expected={expected!r}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    print(__doc__.splitlines()[0])
    print()

    # Case 1: pure passive decay, no HVAC (q=0).
    # t_next = 60 + (75-60)*exp(-0.15*1) = 60 + 15*0.8607... = 72.9106...
    _check(
        "passive decay, k_passive=-0.15, dt=1h",
        _reference_simulate_indoor_physics(75.0, 60.0, -0.15, None, 1.0, None, comfort_heat=68, comfort_cool=76),
        72.91061964637586,
    )

    # Case 2: active heating below setpoint, no clamp reached.
    _check(
        "heating below setpoint, k_active_heat=3.0, dt=2h",
        _reference_simulate_indoor_physics(
            65.0, 40.0, -0.15, 3.0, 2.0, 70.0, comfort_heat=68, comfort_cool=76, hvac_mode="heat"
        ),
        63.70409110340859,
    )

    # Case 3: active cooling with a long enough dt that the ODE would overshoot
    # past the setpoint — the clamp must pin the result to exactly setpoint.
    _check(
        "cooling, long dt forces clamp to setpoint=76.0",
        _reference_simulate_indoor_physics(
            78.0, 95.0, -0.15, -3.0, 20.0, 76.0, comfort_heat=68, comfort_cool=76, hvac_mode="cool"
        ),
        76.0,
    )

    print()
    print("All hand-verified cases pass. This confirms the formula transcribed")
    print("from coordinator.py:9313-9363 is internally consistent, but it does")
    print("NOT confirm the transcription itself is byte-for-byte identical to")
    print("the real function — that requires a real import, which needs a")
    print("`homeassistant` package not present in this environment. Verify on")
    print("a real HA instance, or re-run this check with the import swapped in")
    print("once `homeassistant` is installed locally.")


if __name__ == "__main__":
    main()
