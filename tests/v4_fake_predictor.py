"""A stand-in for ``predictor_v4`` (Issue #1230 R1, lane A's engine) that implements the contract's API.

The shadow wiring (scheduling, archive, endpoint) is tested against this, not against the real fit, so these tests keep
passing whatever the engine's maths do. ``install()`` swaps it in at the single access point ``v4_shadow._predictor``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

V4_SCHEMA_VERSION = 1
V4_STEP_MINUTES = 15


@dataclass(frozen=True)
class V4Sample:
    ts: datetime
    outdoor_f: float | None
    forecast_outdoor_f: float | None
    hvac_action: str | None
    fan_running: bool
    windows_open: bool
    heat_sp_f: float | None
    cool_sp_f: float | None
    readings: dict


@dataclass(frozen=True)
class V4Input:
    ts: datetime
    outdoor_f: float
    heat_sp_f: float | None
    cool_sp_f: float | None
    windows_open: bool = False
    fan_running: bool = False


@dataclass
class FakeModel:
    primary: str
    sensors: tuple[str, ...]
    confidence: str = "ok"
    fitted_at: str = "2026-11-06T03:15:00+00:00"
    fit: dict = field(
        default_factory=lambda: {
            "n_anchors": 100,
            "span_days": 30.0,
            "mae_by_lead": {"1h": 0.3, "4h": 0.76, "12h": 1.2},
        }
    )


@dataclass
class FakePredictor:
    """Records every call; ``fail_fit`` / ``fail_predict`` raise; ``confidence`` sets the fitted model's grade."""

    V4_SCHEMA_VERSION = V4_SCHEMA_VERSION
    V4_STEP_MINUTES = V4_STEP_MINUTES
    V4Sample = V4Sample
    V4Input = V4Input

    fail_fit: bool = False
    fail_predict: bool = False
    confidence: str = "ok"
    fit_calls: list[dict] = field(default_factory=list)
    predict_calls: list[dict] = field(default_factory=list)
    step_calls: list[dict] = field(default_factory=list)
    sample_calls: list[dict] = field(default_factory=list)

    def samples_from_chart_log(self, entries: Any, *, tz: Any, archive_snapshots: Any = None) -> list:
        self.sample_calls.append({"entries": len(entries), "snapshots": archive_snapshots})
        return list(entries)

    def fit(self, samples: Any, *, primary: str, sensors: Any, tz: Any, now: datetime, **_kw: Any) -> FakeModel:
        self.fit_calls.append(
            {
                "samples": len(samples),
                "primary": primary,
                "sensors": tuple(sensors),
                "now": now,
                "warm_start": _kw.get("warm_start"),
            }
        )
        if self.fail_fit:
            raise RuntimeError("fit diverged")
        return FakeModel(primary=primary, sensors=tuple(sensors), confidence=self.confidence)

    def to_dict(self, model: FakeModel) -> dict:
        return {
            "schema_version": V4_SCHEMA_VERSION,
            "fitted_at": model.fitted_at,
            "primary": model.primary,
            "sensors": list(model.sensors),
            "confidence": model.confidence,
            "fit": dict(model.fit),
        }

    def from_dict(self, d: dict) -> FakeModel | None:
        if d.get("schema_version") != V4_SCHEMA_VERSION or "primary" not in d:
            return None
        return FakeModel(
            primary=d["primary"],
            sensors=tuple(d.get("sensors") or ()),
            confidence=d.get("confidence", "none"),
            fitted_at=d.get("fitted_at", ""),
            fit=dict(d.get("fit") or {}),
        )

    def step_mass(self, model: FakeModel, *, mass_f: float | None, readings: dict, sample: V4Sample) -> float:
        self.step_calls.append({"mass_in": mass_f, "ts": sample.ts})
        base = readings.get(model.primary) if mass_f is None else mass_f
        return float(base if base is not None else 70.0) + 0.1

    def predict(
        self,
        model: FakeModel,
        *,
        now: datetime,
        readings: dict,
        mass_f: float | None,
        inputs: Any,
        tz: Any,
        outdoor_now_f: float | None = None,
        forecast_now_f: float | None = None,
    ) -> dict | None:
        self.predict_calls.append(
            {
                "now": now,
                "inputs": list(inputs),
                "mass_f": mass_f,
                "readings": dict(readings),
                "outdoor_now_f": outdoor_now_f,
                "forecast_now_f": forecast_now_f,
            }
        )
        if self.fail_predict:
            raise RuntimeError("predict exploded")
        if model.confidence == "none" or readings.get(model.primary) is None:
            return None
        out = {}
        for sid in model.sensors:
            base = readings.get(sid)
            if base is None:
                continue
            out[sid] = [(inp.ts, base + 0.01 * (k + 1)) for k, inp in enumerate(inputs)]
        return out


def install(monkeypatch: Any, fake: FakePredictor | None = None) -> FakePredictor:
    """Make the shadow use ``fake`` as the v4 engine."""
    from custom_components.climate_advisor import v4_shadow

    fake = fake or FakePredictor()
    monkeypatch.setattr(v4_shadow, "_predictor", lambda: fake)
    return fake


def uninstall(monkeypatch: Any) -> None:
    """Make the shadow see no v4 engine (the module is missing)."""
    from custom_components.climate_advisor import v4_shadow

    def _missing() -> Any:
        raise ImportError("predictor_v4 not available")

    monkeypatch.setattr(v4_shadow, "_predictor", _missing)
