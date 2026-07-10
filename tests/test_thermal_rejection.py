"""Tests for thermal rejection reason codes, rejection log accumulation,
learning health aggregation, and sensor attribute exposure (Issue #121 Phase 2).

Coverage:
- compute_k_passive() 3-tuple reason codes for all rejection paths
- coordinator _rejection_log accumulation, capping, and per-type isolation
- _build_learning_health() reason-code counting
- ClimateAdvisorComplianceSensor.extra_state_attributes thermal_learning_health key
"""

from __future__ import annotations

import importlib
import random
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_WRONG_SIGN,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_SAMPLES,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_MIN_R_SQUARED,
)
from custom_components.climate_advisor.learning import (  # noqa: E402
    LearningEngine,
    compute_k_passive,
)
from custom_components.climate_advisor.sensor import ClimateAdvisorComplianceSensor  # noqa: E402

_TODAY = "2026-03-27"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _make_decay_sample(indoor_f: float, outdoor_f: float, elapsed_minutes: float) -> dict:
    """Build a sample dict in the format expected by compute_k_passive."""
    return {
        "indoor_temp_f": indoor_f,
        "outdoor_temp_f": outdoor_f,
        "elapsed_minutes": elapsed_minutes,
    }


def _make_well_behaved_samples(n: int = 15) -> list[dict]:
    """Generate exponential decay samples converging toward outdoor temp (50°F).

    Indoor starts at 72°F and decays toward 50°F at roughly -0.05 hr⁻¹.
    """
    samples = []
    t_indoor = 72.0
    t_outdoor = 50.0
    k = -0.05  # hr⁻¹
    for i in range(n):
        elapsed = i * 5.0  # 5-minute intervals
        dt_hours = 5.0 / 60.0
        t_indoor += k * (t_indoor - t_outdoor) * dt_hours
        samples.append(_make_decay_sample(t_indoor, t_outdoor, elapsed))
    return samples


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


# ---------------------------------------------------------------------------
# TestComputeKPassiveReasonCodes
# ---------------------------------------------------------------------------


class TestComputeKPassiveReasonCodes:
    """Verify that compute_k_passive returns the correct 3-tuple reason codes."""

    def test_too_few_samples_returns_reason_code(self):
        """Fewer than THERMAL_MIN_POST_HEAT_SAMPLES + 1 total → REJECT_TOO_FEW_SAMPLES."""
        # Need < min_samples + 1 total; use only 3 samples (well below 11)
        samples = [_make_decay_sample(70.0 - i * 0.1, 50.0, i * 5.0) for i in range(3)]
        k_p, r2, code = compute_k_passive(samples)
        assert k_p is None
        assert code == REJECT_TOO_FEW_SAMPLES

    def test_small_delta_returns_reason_code(self):
        """All samples at same temperature → sum_d2 == 0 → REJECT_SMALL_DELTA.

        When indoor == outdoor for all samples, delta_i = 0 for every interval,
        so sum_d2 = 0 and the OLS denominator is zero.
        """
        # Indoor == outdoor for all samples; enough samples to pass the count gate
        n = THERMAL_MIN_POST_HEAT_SAMPLES + 2
        samples = [_make_decay_sample(60.0, 60.0, i * 5.0) for i in range(n)]
        k_p, r2, code = compute_k_passive(samples)
        assert k_p is None
        assert code == REJECT_SMALL_DELTA

    def test_ols_bad_fit_returns_reason_code(self):
        """Noisy uncorrelated data → R² < THERMAL_MIN_R_SQUARED → REJECT_OLS_BAD_FIT.

        Strategy: generate samples where the rate has no relationship to the
        delta, while still having enough samples and a valid k_p sign.
        We inject data where indoor oscillates randomly around outdoor + offset
        so that k_p will land in [-0.5, -0.001] but R² is very low.
        """
        rng = random.Random(42)
        n = THERMAL_MIN_POST_HEAT_SAMPLES + 5
        # Build samples where indoor wobbles with large noise around a mild gradient
        # The goal is k_p < 0 (so sign check passes) but fit is terrible
        samples = []
        for i in range(n):
            # Baseline indoor: 70°F with large random noise
            indoor = 70.0 + rng.uniform(-5.0, 5.0)
            outdoor = 50.0
            samples.append(_make_decay_sample(indoor, outdoor, i * 5.0))

        # It's theoretically possible this passes or fails sign check depending on noise;
        # iterate until we get a bad-fit result (cap iterations to keep test deterministic)
        seeds = [42, 99, 7, 123, 555, 1001, 2024]
        got_bad_fit = False
        for seed in seeds:
            rng2 = random.Random(seed)
            s2 = [_make_decay_sample(70.0 + rng2.uniform(-6.0, 6.0), 50.0, i * 5.0) for i in range(n)]
            k_p2, r2_2, code2 = compute_k_passive(s2)
            if code2 == REJECT_OLS_BAD_FIT:
                got_bad_fit = True
                assert k_p2 is None
                break
        # If none of the random seeds produce bad fit, manufacture it deterministically:
        # Force very scattered rates by alternating high/low indoor temps
        if not got_bad_fit:
            alternating = []
            for i in range(n):
                indoor = 72.0 if i % 2 == 0 else 68.0
                alternating.append(_make_decay_sample(indoor, 50.0, i * 5.0))
            k_p3, r2_3, code3 = compute_k_passive(alternating)
            assert code3 == REJECT_OLS_BAD_FIT, (
                f"Expected REJECT_OLS_BAD_FIT from alternating samples, got code={code3!r} k_p={k_p3} r2={r2_3:.3f}"
            )

    def test_success_returns_none_reason(self):
        """Well-behaved exponential decay → k_p < 0, reason_code is None."""
        samples = _make_well_behaved_samples(n=20)
        k_p, r2, code = compute_k_passive(samples)
        assert k_p is not None
        assert k_p < 0
        assert code is None
        assert r2 >= THERMAL_MIN_R_SQUARED

    def test_wrong_sign_returns_reason_code(self):
        """Monotonically increasing indoor temp (heating) → k_p > 0 → REJECT_OLS_WRONG_SIGN."""
        n = THERMAL_MIN_POST_HEAT_SAMPLES + 2
        samples = []
        for i in range(n):
            # Indoor rising: 60 + 0.2*i; outdoor at 80 (indoor < outdoor so delta negative)
            # rate = +0.2*12 = +2.4°F/hr; delta = (60+0.2i - 80) < 0
            # k_p = sum(rate*delta)/sum(delta^2) = positive/positive ... wait:
            # delta is negative (indoor < outdoor), rate is positive → product is negative
            # so k_p = negative/positive = negative → wrong sign test won't trigger.
            # Instead: indoor > outdoor and rising → delta positive, rate positive → k_p positive.
            indoor = 50.0 + i * 0.5  # rising from 50°F
            outdoor = 30.0  # outdoor cold, indoor warmer and rising
            samples.append(_make_decay_sample(indoor, outdoor, i * 5.0))
        k_p, r2, code = compute_k_passive(samples)
        assert k_p is None
        assert code == REJECT_OLS_WRONG_SIGN


# ---------------------------------------------------------------------------
# TestRejectionLog
# ---------------------------------------------------------------------------


def _make_coordinator_stub_for_rejection() -> object:
    """Build a minimal coordinator stub with _abandon_observation bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    # Minimal attributes _abandon_observation needs
    coord._rejection_log = {}
    coord._pending_observations = {}

    # Mock hass — async_create_task must consume coroutines
    hass = MagicMock()

    def _consume_coroutine(coro):
        if hasattr(coro, "close"):
            coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.async_add_executor_job = MagicMock(return_value=MagicMock())
    coord.hass = hass

    # Mock learning with _state.rejection_log and set_pending_thermal_event
    mock_learning = MagicMock()
    mock_learning._state = MagicMock()
    mock_learning._state.rejection_log = {}
    mock_learning.save_state = MagicMock()
    coord.learning = mock_learning

    # Bind the real methods
    coord._abandon_observation = types.MethodType(ClimateAdvisorCoordinator._abandon_observation, coord)
    coord._ensure_pending_observations = types.MethodType(ClimateAdvisorCoordinator._ensure_pending_observations, coord)

    return coord


class TestRejectionLog:
    """Verify _rejection_log accumulation, capping, and per-type isolation."""

    def _call_abandon(self, coord, obs_type: str, reason: str = "test_reason") -> None:
        """Seed a pending observation then abandon it."""
        coord._pending_observations[obs_type] = {
            "obs_type": obs_type,
            "samples": [{"indoor_temp_f": 70.0, "outdoor_temp_f": 50.0, "elapsed_minutes": i * 5.0} for i in range(3)],
        }
        mock_now = MagicMock()
        mock_now.isoformat.return_value = f"{_TODAY}T12:00:00+00:00"
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value = mock_now
            coord._abandon_observation(obs_type, reason)

    def test_rejection_log_accumulates_on_abandon(self):
        """Two distinct obs_types both appear in _rejection_log after abandon."""
        coord = _make_coordinator_stub_for_rejection()
        self._call_abandon(coord, OBS_TYPE_PASSIVE_DECAY, "test_a")
        self._call_abandon(coord, OBS_TYPE_HVAC_HEAT, "test_b")
        assert OBS_TYPE_PASSIVE_DECAY in coord._rejection_log
        assert OBS_TYPE_HVAC_HEAT in coord._rejection_log
        assert len(coord._rejection_log[OBS_TYPE_PASSIVE_DECAY]) == 1
        assert len(coord._rejection_log[OBS_TYPE_HVAC_HEAT]) == 1

    def test_rejection_log_capped_at_100_per_obs_type(self):
        """After 105 abandons for the same obs_type, bucket length is capped at 100."""
        coord = _make_coordinator_stub_for_rejection()
        for _ in range(105):
            self._call_abandon(coord, OBS_TYPE_PASSIVE_DECAY, "overflow_test")
        assert len(coord._rejection_log[OBS_TYPE_PASSIVE_DECAY]) == 100

    def test_rejection_log_per_type_independent(self):
        """50 events for type A and 50 for type B keep independent counts."""
        coord = _make_coordinator_stub_for_rejection()
        for _ in range(50):
            self._call_abandon(coord, OBS_TYPE_PASSIVE_DECAY, "type_a")
        for _ in range(50):
            self._call_abandon(coord, OBS_TYPE_HVAC_COOL, "type_b")
        assert len(coord._rejection_log[OBS_TYPE_PASSIVE_DECAY]) == 50
        assert len(coord._rejection_log[OBS_TYPE_HVAC_COOL]) == 50


# ---------------------------------------------------------------------------
# TestLearningHealth
# ---------------------------------------------------------------------------


class TestLearningHealth:
    """Verify get_thermal_model learning_health forwarding and _build_learning_health logic."""

    def test_learning_health_included_when_no_rejections(self, tmp_path: Path):
        """get_thermal_model(learning_health={}) includes key with empty dict value."""
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model(learning_health={})
        assert "learning_health" in model
        assert model["learning_health"] == {}

    def test_learning_health_forwarded_verbatim(self, tmp_path: Path):
        """Arbitrary learning_health dict is forwarded verbatim."""
        engine = _make_engine(tmp_path)
        health_stub = {OBS_TYPE_PASSIVE_DECAY: {"attempts": 3, "committed": 1}}
        model = engine.get_thermal_model(learning_health=health_stub)
        assert model["learning_health"][OBS_TYPE_PASSIVE_DECAY]["attempts"] == 3

    def test_learning_health_absent_gives_empty_dict(self, tmp_path: Path):
        """Calling get_thermal_model() without kwarg yields learning_health == {}."""
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert model["learning_health"] == {}

    def test_build_learning_health_counts_by_reason(self):
        """_build_learning_health() aggregates rejection events into per-reason counts."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        # Seed rejection log: 3 too_few_samples + 1 ols_bad_fit for passive_decay
        coord._rejection_log = {
            OBS_TYPE_PASSIVE_DECAY: [
                {"reason_code": REJECT_TOO_FEW_SAMPLES},
                {"reason_code": REJECT_TOO_FEW_SAMPLES},
                {"reason_code": REJECT_TOO_FEW_SAMPLES},
                {"reason_code": REJECT_OLS_BAD_FIT},
            ]
        }
        mock_learning = MagicMock()
        mock_learning._state.thermal_observations = []
        coord.learning = mock_learning

        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)

        health = coord._build_learning_health()
        pd = health[OBS_TYPE_PASSIVE_DECAY]
        assert pd["rejections"][REJECT_TOO_FEW_SAMPLES] == 3
        assert pd["rejections"][REJECT_OLS_BAD_FIT] == 1
        assert pd["rejections"][REJECT_SMALL_DELTA] == 0
        assert pd["committed"] == 0
        assert pd["attempts"] == 4  # 0 committed + 4 rejections

    def test_build_learning_health_last_rejection_is_most_recent(self):
        """_build_learning_health() last_rejection is the last event in the bucket."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        events = [
            {"reason_code": REJECT_TOO_FEW_SAMPLES, "n_samples": 2},
            {"reason_code": REJECT_OLS_BAD_FIT, "n_samples": 12},
        ]
        coord._rejection_log = {OBS_TYPE_HVAC_HEAT: events}
        mock_learning = MagicMock()
        mock_learning._state.thermal_observations = []
        coord.learning = mock_learning

        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)

        health = coord._build_learning_health()
        # last_rejection should be the final event in the list
        assert health[OBS_TYPE_HVAC_HEAT]["last_rejection"]["reason_code"] == REJECT_OLS_BAD_FIT


# ---------------------------------------------------------------------------
# TestSensorAttributes
# ---------------------------------------------------------------------------


def _compliance_extra_state_attributes(coordinator) -> dict:
    """Return the real ClimateAdvisorComplianceSensor.extra_state_attributes for `coordinator`.

    HomeAssistantView/sensor bases are real minimal classes now (Issue #452), so
    the sensor can be instantiated directly instead of replicated — this was
    already independently duplicated in test_status_sensors.py, a live drift risk
    this consolidation removes.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry"
    sensor = ClimateAdvisorComplianceSensor(coordinator, entry)
    return sensor.extra_state_attributes


def _make_compliance_coordinator_stub(learning_health: dict) -> MagicMock:
    """Build a minimal coordinator mock for sensor attribute tests."""
    coord = MagicMock()
    coord.data = {}
    coord.today_record = None
    coord.config = {"comfort_heat": 70, "comfort_cool": 75, "temp_unit": "fahrenheit"}
    coord.learning.get_thermal_model.return_value = {
        "heating_rate_f_per_hour": None,
        "cooling_rate_f_per_hour": None,
        "confidence": "none",
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "learning_health": learning_health,
    }
    coord.learning.get_weather_bias.return_value = {
        "high_bias": 0.0,
        "low_bias": 0.0,
        "confidence": "none",
    }
    return coord


class TestSensorAttributes:
    """Verify thermal_learning_health in ClimateAdvisorComplianceSensor attributes."""

    def test_thermal_learning_health_key_present(self):
        """thermal_learning_health must appear in extra_state_attributes."""
        health = {
            OBS_TYPE_PASSIVE_DECAY: {
                "attempts": 5,
                "committed": 2,
                "rejections": {REJECT_TOO_FEW_SAMPLES: 3},
                "last_rejection": {"reason_code": REJECT_TOO_FEW_SAMPLES},
            }
        }
        coord = _make_compliance_coordinator_stub(learning_health=health)
        attrs = _compliance_extra_state_attributes(coord)
        assert "thermal_learning_health" in attrs

    def test_thermal_learning_health_exposes_counts_only(self):
        """The thermal_learning_health value must not contain raw ThermalRejectionEvent dicts.

        Allowed keys per obs_type: attempts, committed, rejections, last_rejection_reason.
        Raw event dicts (with keys like n_samples, r_squared, timestamp) must NOT appear.
        """
        raw_event = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "reason_code": REJECT_OLS_BAD_FIT,
            "n_samples": 8,
            "n_required": 10,
            "r_squared": 0.1,
            "r_squared_required": 0.2,
            "delta_t_f": 1.5,
            "delta_t_required": None,
            "elapsed_minutes": 45,
            "timestamp": "2026-03-27T12:00:00",
        }
        health = {
            OBS_TYPE_PASSIVE_DECAY: {
                "attempts": 1,
                "committed": 0,
                "rejections": {REJECT_OLS_BAD_FIT: 1},
                "last_rejection": raw_event,
            }
        }
        coord = _make_compliance_coordinator_stub(learning_health=health)
        attrs = _compliance_extra_state_attributes(coord)
        tlh = attrs["thermal_learning_health"]
        assert OBS_TYPE_PASSIVE_DECAY in tlh
        pd = tlh[OBS_TYPE_PASSIVE_DECAY]

        # Only these four keys are allowed
        allowed_keys = {"attempts", "committed", "rejections", "last_rejection_reason"}
        assert set(pd.keys()) == allowed_keys

        # last_rejection_reason must be a string (the reason_code), not a dict
        assert isinstance(pd["last_rejection_reason"], str)
        assert pd["last_rejection_reason"] == REJECT_OLS_BAD_FIT

        # Raw event fields must not appear
        for raw_key in ("n_samples", "r_squared", "timestamp", "elapsed_minutes"):
            assert raw_key not in pd, f"Raw field {raw_key!r} must not be exposed in sensor attributes"

    def test_thermal_learning_health_empty_when_no_health(self):
        """When learning_health is empty dict, thermal_learning_health is empty dict."""
        coord = _make_compliance_coordinator_stub(learning_health={})
        attrs = _compliance_extra_state_attributes(coord)
        assert attrs["thermal_learning_health"] == {}

    def test_thermal_learning_health_last_rejection_reason_none_when_no_rejection(self):
        """last_rejection_reason is None when last_rejection is not set."""
        health = {
            OBS_TYPE_HVAC_HEAT: {
                "attempts": 3,
                "committed": 3,
                "rejections": {},
                "last_rejection": None,
            }
        }
        coord = _make_compliance_coordinator_stub(learning_health=health)
        attrs = _compliance_extra_state_attributes(coord)
        tlh = attrs["thermal_learning_health"]
        assert tlh[OBS_TYPE_HVAC_HEAT]["last_rejection_reason"] is None
