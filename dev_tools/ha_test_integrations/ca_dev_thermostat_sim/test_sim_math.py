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
    clamp_bound: float | None = None,
) -> float:
    """Hand-transcribed copy of coordinator.py's _simulate_indoor_physics, for
    verification only.

    climate.py does NOT use this function — it imports the real one. See the
    module docstring above for why this duplicate exists. `clamp_bound`
    mirrors the Issue #887 parameter: when None, the clamp target defaults to
    `setpoint` (original behavior); climate.py passes target±deadband instead
    so the simulated compressor can actually reach its configured deadband
    edge before the FSM shuts it off.
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

    bound = setpoint if clamp_bound is None else clamp_bound
    if bound is not None:
        if q > 0:
            t_next = min(t_next, bound)
        elif q < 0:
            t_next = max(t_next, bound)
    return t_next


def _check(label: str, actual: float, expected: float, tol: float = 1e-9) -> None:
    ok = abs(actual - expected) < tol
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: actual={actual!r} expected={expected!r}")
    if not ok:
        raise SystemExit(1)


def _check_bool(label: str, actual: bool, expected: bool) -> None:
    ok = actual is expected
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: actual={actual!r} expected={expected!r}")
    if not ok:
        raise SystemExit(1)


class _ReferenceCompressorFSM:
    """Hand-transcribed copy of climate.py's SimulatedThermostat compressor on/off
    state machine (deadband + min-run/min-off dwell), for verification only.

    Mirrors the _async_tick() pseudocode exactly: mode/deadband selection, the
    not-on -> wants_on/can_turn_on branch, and the on -> wants_off/can_turn_off
    branch. `now` is a plain float of elapsed seconds (no datetime needed for this
    hand-check — only deltas matter).
    """

    def __init__(
        self,
        *,
        deadband_heat_f: float = 1.5,
        deadband_cool_f: float = 1.5,
        min_run_seconds: float = 300,
        min_off_seconds: float = 300,
    ) -> None:
        self.deadband_heat_f = deadband_heat_f
        self.deadband_cool_f = deadband_cool_f
        self.min_run_seconds = min_run_seconds
        self.min_off_seconds = min_off_seconds
        self.compressor_on = False
        self.last_on_ts: float | None = None
        self.last_off_ts: float | None = None

    def tick(self, *, now: float, hvac_mode: str | None, current_temp: float, target_temp: float | None) -> bool:
        """Advance the FSM one tick and return the resulting compressor_on state."""
        if hvac_mode == "heat":
            mode, deadband = "heat", self.deadband_heat_f
        elif hvac_mode == "cool":
            mode, deadband = "cool", self.deadband_cool_f
        else:
            mode, deadband = None, None

        if mode is None:
            self.compressor_on = False
        elif target_temp is not None:
            if not self.compressor_on:
                wants_on = (mode == "heat" and current_temp <= target_temp - deadband) or (
                    mode == "cool" and current_temp >= target_temp + deadband
                )
                can_turn_on = self.last_off_ts is None or (now - self.last_off_ts) >= self.min_off_seconds
                if wants_on and can_turn_on:
                    self.compressor_on = True
                    self.last_on_ts = now
            else:
                wants_off = (mode == "heat" and current_temp >= target_temp + deadband) or (
                    mode == "cool" and current_temp <= target_temp - deadband
                )
                can_turn_off = self.last_on_ts is None or (now - self.last_on_ts) >= self.min_run_seconds
                if wants_off and can_turn_off:
                    self.compressor_on = False
                    self.last_off_ts = now

        return self.compressor_on


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

    # Case 4: heating compressor does NOT turn on before crossing the deadband edge.
    # target=70, deadband_heat=1.5 -> turn-on threshold is 68.5. At 69.0 (above
    # threshold), the compressor must stay off.
    fsm = _ReferenceCompressorFSM(deadband_heat_f=1.5, min_off_seconds=0)
    on = fsm.tick(now=0, hvac_mode="heat", current_temp=69.0, target_temp=70.0)
    _check_bool("heat: stays off above deadband edge (69.0 > 68.5)", on, False)
    on = fsm.tick(now=1, hvac_mode="heat", current_temp=68.5, target_temp=70.0)
    _check_bool("heat: turns on exactly at deadband edge (68.5 <= 68.5)", on, True)

    # Case 5: cooling compressor does NOT turn on before crossing the deadband edge.
    # target=76, deadband_cool=1.5 -> turn-on threshold is 77.5.
    fsm = _ReferenceCompressorFSM(deadband_cool_f=1.5, min_off_seconds=0)
    on = fsm.tick(now=0, hvac_mode="cool", current_temp=77.0, target_temp=76.0)
    _check_bool("cool: stays off below deadband edge (77.0 < 77.5)", on, False)
    on = fsm.tick(now=1, hvac_mode="cool", current_temp=77.5, target_temp=76.0)
    _check_bool("cool: turns on exactly at deadband edge (77.5 >= 77.5)", on, True)

    # Case 6 (Issue #887): once on, compressor does NOT turn off until reaching the
    # deadband edge on the FAR side of setpoint — not the bare setpoint itself.
    # target=70, deadband_heat=1.5 -> turn-off threshold is 71.5. min_run_seconds=0
    # so dwell isn't the reason it stays on here; only "hasn't crossed 71.5 yet" is.
    fsm = _ReferenceCompressorFSM(deadband_heat_f=1.5, min_run_seconds=0, min_off_seconds=0)
    fsm.tick(now=0, hvac_mode="heat", current_temp=68.0, target_temp=70.0)  # crosses on
    _check_bool("heat: compressor is on after crossing threshold", fsm.compressor_on, True)
    on = fsm.tick(now=60, hvac_mode="heat", current_temp=70.0, target_temp=70.0)
    _check_bool("heat: stays on at setpoint, below turn-off edge (70.0 < 71.5)", on, True)
    on = fsm.tick(now=61, hvac_mode="heat", current_temp=71.4, target_temp=70.0)
    _check_bool("heat: stays on just below turn-off edge (71.4 < 71.5)", on, True)
    on = fsm.tick(now=62, hvac_mode="heat", current_temp=71.5, target_temp=70.0)
    _check_bool("heat: turns off exactly at turn-off edge (71.5 >= 71.5)", on, False)

    # Case 7: min_run_seconds delays a turn-off that the deadband condition alone
    # would have triggered. Compressor turns on at t=0; the turn-off edge (71.5) is
    # reached at t=60s, well before min_run_seconds=300 has elapsed — it must stay
    # on until t=300.
    fsm = _ReferenceCompressorFSM(deadband_heat_f=1.5, min_run_seconds=300, min_off_seconds=0)
    fsm.tick(now=0, hvac_mode="heat", current_temp=68.0, target_temp=70.0)  # crosses on
    on = fsm.tick(now=60, hvac_mode="heat", current_temp=71.5, target_temp=70.0)
    _check_bool("heat: min_run_seconds holds compressor on despite reaching turn-off edge", on, True)
    on = fsm.tick(now=299, hvac_mode="heat", current_temp=71.5, target_temp=70.0)
    _check_bool("heat: still held on just before min_run_seconds elapses", on, True)
    on = fsm.tick(now=300, hvac_mode="heat", current_temp=71.5, target_temp=70.0)
    _check_bool("heat: turns off once min_run_seconds has elapsed", on, False)

    # Case 8: min_off_seconds delays a turn-on that the deadband condition alone
    # would have triggered. Compressor turns off at t=1 (reaches the 71.5 turn-off
    # edge); indoor drifts back below the turn-on threshold (68.5) at t=61s, well
    # before min_off_seconds=300 has elapsed — it must stay off until t=300.
    fsm = _ReferenceCompressorFSM(deadband_heat_f=1.5, min_run_seconds=0, min_off_seconds=300)
    fsm.tick(now=0, hvac_mode="heat", current_temp=68.0, target_temp=70.0)  # crosses on
    fsm.tick(now=1, hvac_mode="heat", current_temp=71.5, target_temp=70.0)  # crosses off
    _check_bool("heat: compressor is off after reaching turn-off edge", fsm.compressor_on, False)
    on = fsm.tick(now=61, hvac_mode="heat", current_temp=68.5, target_temp=70.0)
    _check_bool("heat: min_off_seconds holds compressor off despite crossing deadband edge again", on, False)
    on = fsm.tick(now=301, hvac_mode="heat", current_temp=68.5, target_temp=70.0)
    _check_bool("heat: turns on once min_off_seconds has elapsed", on, True)

    print()
    print("All hand-verified cases pass. This confirms the formula transcribed")
    print("from coordinator.py's _simulate_indoor_physics is internally consistent,")
    print("but it does NOT confirm the transcription itself is byte-for-byte")
    print("identical to the real function — that requires a real import, which")
    print("needs a `homeassistant` package not present in this environment. Verify")
    print("on a real HA instance, or re-run this check with the import swapped in")
    print("once `homeassistant` is installed locally.")


if __name__ == "__main__":
    main()
