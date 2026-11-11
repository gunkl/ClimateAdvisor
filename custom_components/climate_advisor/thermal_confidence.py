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

try:
    from .const import (
        MIN_THERMAL_OBSERVATIONS,
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


def grade_passive_confidence(cache: dict) -> str:
    """Confidence tier for k_passive from the observation types that write to it.

    Only passive_decay, hvac_heat and hvac_cool update k_passive. vent_window/vent_fan write
    k_vent_window/k_vent_fan and solar writes k_solar, so their counts are excluded (Issue #587:
    window-open or fan-driven decay measures a materially larger rate than the envelope alone).
    """
    count = (
        cache.get("observation_count_passive", 0)
        + cache.get("observation_count_heat", 0)
        + cache.get("observation_count_cool", 0)
    )
    if count < THERMAL_PASSIVE_CONF_LOW:
        return "none"
    if count < THERMAL_PASSIVE_CONF_MEDIUM:
        return "low"
    if count < THERMAL_PASSIVE_CONF_HIGH:
        return "medium"
    return "high"


def grade_hvac_confidence(cache: dict) -> str:
    """Confidence tier for the HVAC rates (k_active_heat/k_active_cool) from heat + cool counts."""
    total = cache.get("observation_count_heat", 0) + cache.get("observation_count_cool", 0)
    if total < MIN_THERMAL_OBSERVATIONS:
        return "none"
    if total < THERMAL_HVAC_CONF_MEDIUM:
        return "low"
    if total < THERMAL_HVAC_CONF_HIGH:
        return "medium"
    return "high"


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
