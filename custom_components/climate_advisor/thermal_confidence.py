"""Thermal-model confidence grades and physics eligibility (Issue #1277).

The single implementation of how the learning engine grades its thermal parameters from the
observation counts in ``thermal_model_cache``, and of the rule ``_build_predicted_indoor_future``
uses to pick the physics ODE over the fallback ramp. Shared by ``learning.py``, ``coordinator.py``
and the diagnostic tools (``tools/engine_status.py``, ``tools/learning_db.py --model``), which
used to read never-refreshed stored ``confidence_k_*`` fields and reported physics as ineligible
while the engine was using it.

Kept dependency-free (no Home Assistant import) and loadable as a standalone file by tools/.
"""

from __future__ import annotations

from datetime import UTC, datetime

try:
    from .const import (
        CYCLE_RETENTION_DAYS,
        MIN_THERMAL_OBSERVATIONS,
        THERMAL_HVAC_CONF_HIGH,
        THERMAL_HVAC_CONF_MEDIUM,
        THERMAL_PASSIVE_CONF_HIGH,
        THERMAL_PASSIVE_CONF_LOW,
        THERMAL_PASSIVE_CONF_MEDIUM,
        THERMAL_RATE_HALF_LIFE_DAYS,
    )
except ImportError:  # loaded by path from tools/ (no package)
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location("_ca_const_standalone", _Path(__file__).with_name("const.py"))
    _const = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_const)  # type: ignore[union-attr]
    CYCLE_RETENTION_DAYS = _const.CYCLE_RETENTION_DAYS
    MIN_THERMAL_OBSERVATIONS = _const.MIN_THERMAL_OBSERVATIONS
    THERMAL_HVAC_CONF_HIGH = _const.THERMAL_HVAC_CONF_HIGH
    THERMAL_HVAC_CONF_MEDIUM = _const.THERMAL_HVAC_CONF_MEDIUM
    THERMAL_PASSIVE_CONF_HIGH = _const.THERMAL_PASSIVE_CONF_HIGH
    THERMAL_PASSIVE_CONF_LOW = _const.THERMAL_PASSIVE_CONF_LOW
    THERMAL_PASSIVE_CONF_MEDIUM = _const.THERMAL_PASSIVE_CONF_MEDIUM
    THERMAL_RATE_HALF_LIFE_DAYS = _const.THERMAL_RATE_HALF_LIFE_DAYS


# Grades the engine once persisted in thermal_model_cache but never refreshed (Issue #1277): they are
# always computed live from the counts now, and dropped from the persisted cache on load.
STALE_STORED_GRADE_KEYS: tuple[str, ...] = ("confidence_k_passive", "confidence_k_hvac")


def drop_stored_grades(cache: dict) -> bool:
    """Remove the never-refreshed stored grade keys from a thermal_model_cache; True if any was present."""
    found = False
    for key in STALE_STORED_GRADE_KEYS:
        if key in cache:
            del cache[key]
            found = True
    return found


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fade(stamp: object, now: datetime | None) -> float:
    """``0.5 ** (age_days / THERMAL_RATE_HALF_LIFE_DAYS)`` of an ISO stamp (an unreadable stamp is age 0)."""
    try:
        then = datetime.fromisoformat(str(stamp))
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        age_days = max(0.0, ((now or datetime.now(UTC)) - then).total_seconds() / 86400.0)
    except ValueError, TypeError:
        age_days = 0.0
    return 0.5 ** (age_days / THERMAL_RATE_HALF_LIFE_DAYS)


def _legacy_hvac_evidence(cache: dict, mode: str, now: datetime | None) -> float:
    """Evidence behind the pre-upgrade heat/cool rate: ``rate_prior`` n, fading on the 21-day schedule (#1512).

    A mode with no prior entry (a pre-upgrade cache, or no cached rate at upgrade) reads its observation counter."""
    rp = cache.get("rate_prior")
    rec = rp.get(f"k_active_{mode}") if isinstance(rp, dict) else None
    if not isinstance(rec, dict) or not _num(rec.get("value")):
        v = cache.get(f"observation_count_{mode}", 0)
        return float(v) if _num(v) and v > 0 else 0.0
    n = rec.get("n")
    n = float(n) if _num(n) and n > 0 else 1.0
    return n * _fade(rec.get("ts"), now)


def _cycle_hvac_evidence(cache: dict, mode: str, now: datetime | None) -> float:
    """Evidence the pooled cycle model adds: its eligible cycles' age weights, fading since the fit (#1512).

    A held fit (its cycles left the pool: a sensor swap, or they aged past the retention) still counts, fading, while
    no fit has replaced it (no eligible cycle now) or while it is the prior the current fit was weighed against."""
    fit = cache.get(f"cycle_fit_{mode}")
    if not isinstance(fit, dict):
        return 0.0
    ne = fit.get("ne")
    ne = float(ne) if _num(ne) and ne > 0 else 0.0
    w = fit.get("ne_w")
    w = float(w) if _num(w) and w >= 0 else ne
    out = w * _fade(fit.get("fitted_at"), now) if ne else 0.0
    held = fit.get("held")
    if isinstance(held, dict) and (not ne or fit.get("prior_source") == "held"):
        hw = held.get("ne_w")
        out += (float(hw) if _num(hw) and hw >= 0 else 1.0) * _fade(held.get("at"), now)
    return out


def hvac_evidence_count(cache: dict, mode: str, now: datetime | None = None) -> float:
    """Effective evidence behind the ``mode`` ("heat"/"cool") rate (Issue #1512 verifier finding 2).

    The legacy evidence carried at upgrade (``rate_prior["k_active_<mode>"]["n"]``, halved every
    THERMAL_RATE_HALF_LIFE_DAYS) plus the cycle model's eligible cycles (``_cycle_hvac_evidence``). Nothing is
    reset: at upgrade the evidence equals the old observation count, and it declines only as the old evidence ages
    out without new cycles replacing it. ``observation_count_<mode>`` is the legacy counter (the cycle model never
    rewrites it); it is the legacy evidence only for a mode with no ``rate_prior`` entry."""
    if not isinstance(cache, dict):
        return 0.0
    return _legacy_hvac_evidence(cache, mode, now) + _cycle_hvac_evidence(cache, mode, now)


def _count_tier(total: float) -> str:
    if total < MIN_THERMAL_OBSERVATIONS:
        return "none"
    if total < THERMAL_HVAC_CONF_MEDIUM:
        return "low"
    if total < THERMAL_HVAC_CONF_HIGH:
        return "medium"
    return "high"


def _fit_agrees_with_prior(fit: dict) -> bool:
    """The data did not move q more than two prior sds: the carried evidence still backs the rate."""
    q, pq, psd = fit.get("q"), fit.get("prior_q"), fit.get("prior_sd_q")
    if not (_num(q) and _num(pq) and _num(psd)) or psd <= 0:
        return True  # nothing to compare against: keep the carried evidence (never a drop from bookkeeping)
    return abs(float(q) - float(pq)) <= 2.0 * float(psd)


def passive_evidence_count(cache: dict, now: datetime | None = None) -> float:
    """Effective evidence count behind k_passive.

    Heat/cool runs never write k_passive any more (Issue #1512): it is an aged median of passive observations
    (chart_log passive fits and the retired duty_balance passive estimates still stored) plus, on an
    upgraded cache, the ``rate_prior`` pseudo-observation that carries the ``n`` observations behind the
    pre-upgrade value. That prior fades with the
    same half-life as the value itself, so confidence neither cliffs on upgrade nor stays inflated forever. A cache
    with no prior yet (pre-upgrade shape) keeps counting passive + heat + cool, as those fed the old value.
    """
    passive = cache.get("observation_count_passive", 0)
    prior = (cache.get("rate_prior") or {}).get("k_passive") if isinstance(cache.get("rate_prior"), dict) else None
    if not isinstance(prior, dict):
        return passive + cache.get("observation_count_heat", 0) + cache.get("observation_count_cool", 0)
    n = prior.get("n")
    n = float(n) if _num(n) and n > 0 else 1.0
    try:
        stamp = datetime.fromisoformat(str(prior.get("ts")))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        age_days = max(0.0, ((now or datetime.now(UTC)) - stamp).total_seconds() / 86400.0)
    except ValueError, TypeError:
        age_days = 0.0
    return passive + n * 0.5 ** (age_days / THERMAL_RATE_HALF_LIFE_DAYS)


def grade_passive_confidence(cache: dict, now: datetime | None = None) -> str:
    """Confidence tier for k_passive from the evidence that still backs it (see ``passive_evidence_count``).

    Counts passive observations (chart_log passive fits and the retired duty_balance ones still stored) plus
    the fading evidence of the pre-upgrade value. vent_window/vent_fan write k_vent_window/k_vent_fan and
    solar writes k_solar, so their counts are excluded
    (Issue #587: window-open or fan-driven decay measures a materially larger rate than the envelope alone).
    """
    count = passive_evidence_count(cache, now)
    if count < THERMAL_PASSIVE_CONF_LOW:
        return "none"
    if count < THERMAL_PASSIVE_CONF_MEDIUM:
        return "low"
    if count < THERMAL_PASSIVE_CONF_HIGH:
        return "medium"
    return "high"


_CYCLE_GRADE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def grade_hvac_confidence(cache: dict, now: datetime | None = None) -> str:
    """Confidence tier for the HVAC rates (k_active_heat/k_active_cool), from the evidence the rates carry.

    Issue #1512 (verifier finding 2: confidence must never drop at upgrade, only age out). One rule for both engines:

    * the count tier (MIN_THERMAL_OBSERVATIONS / THERMAL_HVAC_CONF_MEDIUM / _HIGH) of the evidence summed over heat
      and cool: each mode's legacy evidence (``rate_prior`` n, fading on the 21-day schedule) plus, for a mode
      whose rate is NOT the cycle model's applied fit (a v4 zone, or no eligible cycle yet), the cycle model's
      eligible cycles (``hvac_evidence_count``);
    * a mode whose rate IS the applied fit (v3 primary) is graded by the fit's calibrated grade, and the tier never
      falls below what the carried legacy evidence justifies while the data agrees with it (q within two prior sds
      of the prior it was weighed against: a small er_q against a strong, agreeing prior is not low confidence).
      Data that contradicts the old value drops that mode's legacy evidence: the fit's own grade then speaks.

    The result is the better of the two (a home that only heats is graded on its heating). An applied fit older than
    the cycle retention (``CYCLE_RETENTION_DAYS``: its cycles are gone from the store, no refit since) no longer
    grades on its own; its evidence keeps fading through the count tier like everything else."""
    if not isinstance(cache, dict):
        return "none"
    best = -1
    total = 0.0
    stale_below = 0.5 ** (CYCLE_RETENTION_DAYS / THERMAL_RATE_HALF_LIFE_DAYS)
    for mode in ("heat", "cool"):
        fit = cache.get(f"cycle_fit_{mode}")
        applied = isinstance(fit, dict) and bool(fit.get("applied")) and _fade(fit.get("fitted_at"), now) >= stale_below
        if applied:
            best = max(best, _CYCLE_GRADE_RANK.get(str(fit.get("grade")), 0))
            if _fit_agrees_with_prior(fit):
                total += _legacy_hvac_evidence(cache, mode, now)
        else:
            total += hvac_evidence_count(cache, mode, now)
    tier = _count_tier(total)
    return tier if _CYCLE_GRADE_RANK[tier] >= best else next(g for g, r in _CYCLE_GRADE_RANK.items() if r == best)


def grade_solar_confidence(count: int) -> str:
    """Confidence tier for k_solar from its observation count."""
    if count >= 100:
        return "high"
    if count >= 50:
        return "medium"
    if count >= 20:
        return "low"
    return "none"


def physics_eligibility(
    confidence_hvac: str | None,
    confidence_k_passive: str | None,
    k_passive: float | None,
    k_vent_window: float | None,
) -> tuple[bool, float | None]:
    """Whether the predicted-indoor curve uses the physics ODE (``_build_predicted_indoor_future``).

    Returns ``(eligible, k_passive_used)``. A real closed-house rate is required: either grade above
    ``none`` plus a negative ``k_passive``. The old gate bridge (``k_vent_window`` standing in for an unlearned
    ``k_passive``) was removed in Issue #1512 (k_vent_window is a windows-open rate, not a closed-house rate);
    ``k_vent_window`` is accepted only to keep callers' signatures and is never promoted.
    """
    eligible = (
        (confidence_hvac != "none" or confidence_k_passive not in (None, "none"))
        and k_passive is not None
        and k_passive < 0
    )
    return eligible, k_passive


def explain_physics_eligibility(
    confidence_hvac: str | None,
    confidence_k_passive: str | None,
    k_passive: float | None,
    k_vent_window: float | None,
) -> tuple[bool, str]:
    """``physics_eligibility`` plus the human-readable reason the engine status reports."""
    eligible, _k_used = physics_eligibility(confidence_hvac, confidence_k_passive, k_passive, k_vent_window)
    if eligible:
        return True, f"k_passive + confidence (passive={confidence_k_passive}, hvac={confidence_hvac})"
    if k_passive is None:
        return False, "k_passive not yet learned (needs a closed-house rate; no k_vent_window substitute)"
    if k_passive >= 0:
        return False, "k_passive has wrong sign"
    return False, "confidence insufficient (none)"
