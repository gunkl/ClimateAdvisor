"""Shared lead-time-from-thermal-rate calculation (Issue #786).

Consolidates a formula that was independently duplicated three times before this module
existed: ``automation.py``'s adaptive pre-heat lead time (``_schedule_pre_condition()``),
``ode_ceiling_guard.py``'s ODE ceiling-guard escalation lead time, and ``briefing.py``'s
warm-day pre-cool lead time (``nat_vent_plan.compute_nat_vent_plan()``, moved from
``briefing.py``'s ``_derive_warm_day_events()`` in Issue #817). Each independently computed
``delta_t / rate * 60 * safety_multiplier``, clamped to its own ``[min, max]``, falling back
to its own fixed constant when the learned rate is unavailable or non-positive — same shape,
three separate implementations and three separate constant sets (one of which,
``briefing.py``'s ``_CEILING_PRECOOL_FALLBACK_MIN``, was a locally-redefined copy of
``const.py``'s ``CEILING_PRECOOL_FALLBACK_MIN`` diverging only by name).

This module is the one source of truth for the formula's *shape*. Callers keep their own
constants — min/max/safety/fallback genuinely differ per use (heat vs. cool, different
confidence gating) — and pass them in; nothing here is a policy decision.

Issue #1517: the leak-aware lead. The plain formula treats the equipment as working against
nothing; the house is in fact drifting toward the outdoor temperature (``k_passive``, per hour,
negative) the whole time it runs, so a recovery takes longer than ``delta / rate`` (heating a cold
night) or, with a mild outdoor, shorter. When the caller supplies the start temperature, the
target, the outdoor temperature at the event and a closed-house ``k_passive``, the first-order
model ``T(t) = Ts + (T0 - Ts) * exp(k * t)`` with ``Ts = T_out - q / k`` is solved for the time to
reach the target (a target the equipment can never reach returns the ceiling). Without those
inputs the result is exactly the plain formula, so every caller keeps its old behavior until it
passes them.
"""

from __future__ import annotations

import math

# Issue #1517: safety factor used INSTEAD of the caller's multiplier when the leak term is used. The leak model
# already covers what the fixed 1.3 was guarding against, so a smaller margin suffices.
LEAK_SAFETY_MULTIPLIER = 1.15
# Issue #1517: with an uncertainty (sd) on the rate, plan on |q| - this many sd (a slower rate = an earlier start).
CAUTIOUS_RATE_SD_FRACTION = 0.5
# Never plan on less than this share of the learned rate, however large the stated uncertainty.
_MIN_RATE_SHARE = 0.25


def leak_aware_minutes(
    start_temp: float,
    target_temp: float,
    rate: float,
    k_passive: float,
    outdoor_temp: float,
    *,
    cooling: bool = False,
    rate_sd: float | None = None,
) -> float | None:
    """Minutes (before safety/clamps) for the equipment to move the house ``start_temp`` -> ``target_temp``
    while it leaks toward ``outdoor_temp``; ``None`` when the target is never reached.

    ``rate`` is the equipment's magnitude (degF/hr, sign ignored), ``k_passive`` the closed-house rate per hour
    (negative). Model ``dT/dt = k (T - T_out) + q`` (``q`` > 0 heating, < 0 cooling), solved in closed form:
    ``T(t) = Ts + (T0 - Ts) e^{k t}``, ``Ts = T_out - q / k``. A move already done returns 0.0.
    """
    move = (start_temp - target_temp) if cooling else (target_temp - start_temp)
    if move <= 0:
        return 0.0
    q = abs(rate)
    if rate_sd is not None and rate_sd > 0:
        q = max(q - CAUTIOUS_RATE_SD_FRACTION * rate_sd, _MIN_RATE_SHARE * q)
    if cooling:
        q = -q
    steady = outdoor_temp - q / k_passive  # the temperature the house settles at with the equipment running
    start_gap = start_temp - steady
    target_gap = target_temp - steady
    if start_gap == 0 or target_gap / start_gap <= 0:
        return None  # steady state is on the wrong side of the target: never gets there
    ratio = target_gap / start_gap
    if ratio >= 1:
        return None  # leak pulls away from the target faster than the equipment closes it
    return math.log(ratio) / k_passive * 60.0


def compute_lead_minutes_from_rate(
    delta_t: float,
    rate: float | None,
    *,
    min_minutes: float,
    max_minutes: float,
    safety_multiplier: float,
    fallback_minutes: float,
    start_temp: float | None = None,
    target_temp: float | None = None,
    outdoor_temp: float | None = None,
    k_passive: float | None = None,
    cooling: bool = False,
    rate_sd: float | None = None,
    leak_safety_multiplier: float = LEAK_SAFETY_MULTIPLIER,
) -> float:
    """Minutes needed to close ``delta_t`` degrees at the learned ``rate`` (degF/hr).

    Returns ``fallback_minutes`` (clamped) when ``rate`` is ``None`` or non-positive — no
    confident thermal-model rate to compute from. Otherwise returns
    ``(delta_t / abs(rate)) * 60 * safety_multiplier``, clamped to ``[min_minutes, max_minutes]``.

    Leak-aware (Issue #1517): when ``start_temp``, ``target_temp``, ``outdoor_temp`` and a negative
    ``k_passive`` are all given (``cooling`` says which way the equipment pushes), the time comes from
    ``leak_aware_minutes()`` times ``leak_safety_multiplier`` instead; a target the equipment cannot reach
    returns ``max_minutes``. ``rate_sd`` (optional) makes the rate cautious: ``|q| - 0.5 sd``.
    """
    if (
        rate is not None
        and abs(rate) > 0
        and start_temp is not None
        and target_temp is not None
        and outdoor_temp is not None
        and k_passive is not None
        and k_passive < 0
    ):
        raw = leak_aware_minutes(
            start_temp, target_temp, rate, k_passive, outdoor_temp, cooling=cooling, rate_sd=rate_sd
        )
        minutes = max_minutes if raw is None else raw * leak_safety_multiplier
        return max(min_minutes, min(max_minutes, minutes))
    minutes = fallback_minutes if rate is None or abs(rate) <= 0 else (delta_t / abs(rate)) * 60.0 * safety_multiplier
    return max(min_minutes, min(max_minutes, minutes))
