"""Shared lead-time-from-thermal-rate calculation (Issue #786).

Consolidates a formula that was independently duplicated three times before this module
existed: ``automation.py``'s adaptive pre-heat lead time (``_schedule_pre_condition()``),
``ode_ceiling_guard.py``'s ODE ceiling-guard escalation lead time, and ``briefing.py``'s
warm-day pre-cool lead time (``_derive_warm_day_events()``). Each independently computed
``delta_t / rate * 60 * safety_multiplier``, clamped to its own ``[min, max]``, falling back
to its own fixed constant when the learned rate is unavailable or non-positive — same shape,
three separate implementations and three separate constant sets (one of which,
``briefing.py``'s ``_CEILING_PRECOOL_FALLBACK_MIN``, was a locally-redefined copy of
``const.py``'s ``CEILING_PRECOOL_FALLBACK_MIN`` diverging only by name).

This module is the one source of truth for the formula's *shape*. Callers keep their own
constants — min/max/safety/fallback genuinely differ per use (heat vs. cool, different
confidence gating) — and pass them in; nothing here is a policy decision.
"""

from __future__ import annotations


def compute_lead_minutes_from_rate(
    delta_t: float,
    rate: float | None,
    *,
    min_minutes: float,
    max_minutes: float,
    safety_multiplier: float,
    fallback_minutes: float,
) -> float:
    """Minutes needed to close ``delta_t`` degrees at the learned ``rate`` (°F/hr).

    Returns ``fallback_minutes`` (clamped) when ``rate`` is ``None`` or non-positive — no
    confident thermal-model rate to compute from. Otherwise returns
    ``(delta_t / abs(rate)) * 60 * safety_multiplier``, clamped to ``[min_minutes, max_minutes]``.
    """
    minutes = fallback_minutes if rate is None or abs(rate) <= 0 else (delta_t / abs(rate)) * 60.0 * safety_multiplier
    return max(min_minutes, min(max_minutes, minutes))
