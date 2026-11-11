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

from datetime import datetime

try:
    from .const import (
        MIN_THERMAL_OBSERVATIONS,
        THERMAL_EVIDENCE_HALF_N,
        THERMAL_EVIDENCE_HALF_N_CONTRADICT,
        THERMAL_HVAC_CONF_HIGH,
        THERMAL_HVAC_CONF_MEDIUM,
        THERMAL_PASSIVE_CONF_HIGH,
        THERMAL_PASSIVE_CONF_LOW,
        THERMAL_PASSIVE_CONF_MEDIUM,
    )
except ImportError:  # loaded by path from tools/ (no package)
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location("_ca_const_standalone", _Path(__file__).with_name("const.py"))
    _const = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_const)  # type: ignore[union-attr]
    MIN_THERMAL_OBSERVATIONS = _const.MIN_THERMAL_OBSERVATIONS
    THERMAL_HVAC_CONF_HIGH = _const.THERMAL_HVAC_CONF_HIGH
    THERMAL_HVAC_CONF_MEDIUM = _const.THERMAL_HVAC_CONF_MEDIUM
    THERMAL_PASSIVE_CONF_HIGH = _const.THERMAL_PASSIVE_CONF_HIGH
    THERMAL_PASSIVE_CONF_LOW = _const.THERMAL_PASSIVE_CONF_LOW
    THERMAL_PASSIVE_CONF_MEDIUM = _const.THERMAL_PASSIVE_CONF_MEDIUM
    THERMAL_EVIDENCE_HALF_N = _const.THERMAL_EVIDENCE_HALF_N
    THERMAL_EVIDENCE_HALF_N_CONTRADICT = _const.THERMAL_EVIDENCE_HALF_N_CONTRADICT


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


_CYCLE_GRADE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def evidence_keep(rec: object) -> float:
    """The share of its evidence a carried record still holds (``rec["keep"]``, 1.0 when absent; Issue #1512).

    Owner decision 2026-11-11 ("Fade only on new evidence"): evidence behind a learned rate shrinks only when newer
    measured evidence replaces or contradicts it, never with the passage of time alone. ``keep`` is lowered only by
    ``displaced_keep`` when new evidence arrives; nothing reads a clock."""
    k = rec.get("keep") if isinstance(rec, dict) else None
    return min(1.0, float(k)) if _num(k) and k >= 0 else 1.0


def displaced_keep(keep: float, n_new: float, contradicts: bool) -> float:
    """``keep`` after ``n_new`` new pieces of evidence: halved every THERMAL_EVIDENCE_HALF_N of them (agreeing) or
    every THERMAL_EVIDENCE_HALF_N_CONTRADICT (contradicting). No new evidence leaves it unchanged."""
    if not _num(n_new) or n_new <= 0:
        return keep
    half = THERMAL_EVIDENCE_HALF_N_CONTRADICT if contradicts else THERMAL_EVIDENCE_HALF_N
    return keep * 0.5 ** (float(n_new) / float(half))


def _legacy_hvac_evidence(cache: dict, mode: str) -> float:
    """Evidence behind the pre-upgrade heat/cool rate: ``rate_prior`` n times the share new cycles left it (#1512).

    A mode with no prior entry (a pre-upgrade cache, or no cached rate at upgrade) reads its observation counter."""
    rp = cache.get("rate_prior")
    rec = rp.get(f"k_active_{mode}") if isinstance(rp, dict) else None
    if not isinstance(rec, dict) or not _num(rec.get("value")):
        v = cache.get(f"observation_count_{mode}", 0)
        return float(v) if _num(v) and v > 0 else 0.0
    n = rec.get("n")
    n = float(n) if _num(n) and n > 0 else 1.0
    return n * evidence_keep(rec)


def _usable(fit: object) -> bool:
    """A fit with eligible cycles (its grade is above ``none``; ``cycle_model.grade`` is ``none`` iff ne == 0)."""
    return isinstance(fit, dict) and _CYCLE_GRADE_RANK.get(str(fit.get("grade")), 0) > 0


def _cycle_hvac_evidence(cache: dict, mode: str) -> float:
    """Evidence the pooled cycle model adds: its eligible cycles' weights (``ne_w``), plus a held fit's share (#1512).

    The weights count newer eligible cycles, not days (``cycle_model.fit_cycles``), so a quiet season changes nothing.
    A held fit (its cycles left the pool: a sensor swap) counts ``ne_w * keep`` while no fit has replaced it (no
    eligible cycle now) or while it is the prior the current fit was weighed against."""
    fit = cache.get(f"cycle_fit_{mode}")
    if not isinstance(fit, dict):
        return 0.0
    ne = fit.get("ne")
    ne = float(ne) if _num(ne) and ne > 0 else 0.0
    w = fit.get("ne_w")
    w = float(w) if _num(w) and w >= 0 else ne
    out = w if ne else 0.0
    held = fit.get("held")
    if isinstance(held, dict) and (not ne or fit.get("prior_source") == "held"):
        hw = held.get("ne_w")
        out += (float(hw) if _num(hw) and hw >= 0 else 1.0) * evidence_keep(held)
    return out


def hvac_evidence_count(cache: dict, mode: str, now: datetime | None = None) -> float:
    """Effective evidence behind the ``mode`` ("heat"/"cool") rate (Issue #1512).

    The legacy evidence carried at upgrade (``rate_prior["k_active_<mode>"]["n"] * keep``) plus the cycle model's
    eligible cycles (``_cycle_hvac_evidence``). Nothing is reset and nothing fades with time (owner decision
    2026-11-11): at upgrade the evidence equals the old observation count; the legacy share ``keep`` falls only as
    new eligible cycles displace it (``learning.apply_cycle_fit``). ``now`` is accepted for callers and ignored.
    ``observation_count_<mode>`` is the legacy counter (the cycle model never rewrites it); it is the legacy evidence
    only for a mode with no ``rate_prior`` entry."""
    del now  # evidence has no clock
    if not isinstance(cache, dict):
        return 0.0
    return _legacy_hvac_evidence(cache, mode) + _cycle_hvac_evidence(cache, mode)


def _count_tier(total: float) -> str:
    if total < MIN_THERMAL_OBSERVATIONS:
        return "none"
    if total < THERMAL_HVAC_CONF_MEDIUM:
        return "low"
    if total < THERMAL_HVAC_CONF_HIGH:
        return "medium"
    return "high"


def fit_agrees_with_prior(fit: dict) -> bool:
    """The data did not move q more than two prior sds (``|q - prior_q| <= 2 * prior_sd_q``): the evidence carried
    into that prior still backs the rate. False = the new cycles contradict it."""
    q, pq, psd = fit.get("q"), fit.get("prior_q"), fit.get("prior_sd_q")
    if not (_num(q) and _num(pq) and _num(psd)) or psd <= 0:
        return True  # nothing to compare against: keep the carried evidence (never a drop from bookkeeping)
    return abs(float(q) - float(pq)) <= 2.0 * float(psd)


def passive_evidence_count(cache: dict, now: datetime | None = None) -> float:
    """Effective evidence count behind k_passive.

    Heat/cool runs never write k_passive any more (Issue #1512): it is an aged median of passive observations
    (chart_log passive fits and the retired duty_balance passive estimates still stored) plus, on an
    upgraded cache, the ``rate_prior`` pseudo-observation that carries the ``n`` observations behind the
    pre-upgrade value. That prior's evidence is ``n * keep``: ``keep`` halves for every THERMAL_EVIDENCE_HALF_N new
    passive observations (``learning._update_thermal_model_cache``) and never with time alone (owner decision
    2026-11-11), so confidence neither cliffs on upgrade nor fades over a season with no passive window. A cache
    with no prior yet (pre-upgrade shape) keeps counting passive + heat + cool, as those fed the old value. ``now``
    is accepted for callers and ignored.
    """
    del now  # evidence has no clock
    passive = cache.get("observation_count_passive", 0)
    prior = (cache.get("rate_prior") or {}).get("k_passive") if isinstance(cache.get("rate_prior"), dict) else None
    if not isinstance(prior, dict):
        return passive + cache.get("observation_count_heat", 0) + cache.get("observation_count_cool", 0)
    n = prior.get("n")
    n = float(n) if _num(n) and n > 0 else 1.0
    return passive + n * evidence_keep(prior)


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


def grade_hvac_confidence(cache: dict, now: datetime | None = None) -> str:
    """Confidence tier for the HVAC rates (k_active_heat/k_active_cool), from the evidence the rates carry.

    Issue #1512, one rule for heat and cool, v3 and v4 zones (owner decision 2026-11-11: evidence gives way only to
    newer evidence, never to time alone; ``now`` is accepted for callers and ignored):

    * per mode, the cycle model's evidence speaks through its calibrated grade: the current fit's grade when it has
      eligible cycles, else a held fit's grade while it stands in (a sensor swap), and also while it is the prior of
      a current fit that agrees with it (a few new cycles against a strong held prior give a small ``er_q``; that
      is not low confidence). So a whole-degree sensor's own cycles never reach ``high`` (``cycle_model.grade``);
    * the count tier (MIN_THERMAL_OBSERVATIONS / THERMAL_HVAC_CONF_MEDIUM / _HIGH) of the legacy evidence summed over
      heat and cool (``rate_prior`` n times the share new cycles left it): a mode's legacy evidence counts while its
      current fit agrees with the prior it was weighed against (``fit_agrees_with_prior``) or it has no eligible
      cycle; data that contradicts the old value drops it, and that mode's fit grade speaks.

    The result is the better of the two (a home that only heats is graded on its heating). With no new eligible
    cycle nothing here changes, however long the house is quiet."""
    del now  # evidence has no clock
    if not isinstance(cache, dict):
        return "none"
    best = 0
    total = 0.0
    for mode in ("heat", "cool"):
        fit = cache.get(f"cycle_fit_{mode}")
        held = fit.get("held") if isinstance(fit, dict) else None
        if _usable(fit):
            best = max(best, _CYCLE_GRADE_RANK.get(str(fit.get("grade")), 0))
            if fit_agrees_with_prior(fit):
                total += _legacy_hvac_evidence(cache, mode)
                if fit.get("prior_source") == "held" and _usable(held):
                    # the held fit is this fit's prior and the new cycles agree with it: its grade still stands
                    best = max(best, _CYCLE_GRADE_RANK.get(str(held.get("grade")), 0))
            continue
        if _usable(held):
            best = max(best, _CYCLE_GRADE_RANK.get(str(held.get("grade")), 0))
        total += _legacy_hvac_evidence(cache, mode)
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
