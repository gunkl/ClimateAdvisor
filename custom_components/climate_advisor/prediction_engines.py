"""Prediction-engine registry (Issue #1230): one contract per engine, a primary and an optional shadow per zone.

Occupant view: Settings -> Prediction engine picks the engine whose line is the Status chart's main prediction (v4 by
default) and, optionally, a second one drawn for comparison. Only those two run; any other engine is not fitted, not
predicted and not archived. While the primary has not finished learning, Climate Advisor shows the plain schedule
estimate (the no-model fallback it has always had) and says so; it never silently swaps in the other engine.

Adding an engine (a v5) is ``register(MyEngine())``: the options form, the validation, the "at most two run" rule, the
chart payload's engine catalogue and the archive lookups all read this registry (``tests/test_prediction_engines.py``
proves it with a fake v5).

CONTROL IS NOT HERE. In 0.9.4 every heating, cooling and ventilation decision still uses v3 only: no decision module
imports this one or reads the settings (``tests/test_v4_shadow_structural.py`` guards it). v3 therefore always runs
while it drives control, whatever the two settings say.

An engine is read through a ``source`` (the coordinator, duck-typed), so this module needs no Home Assistant import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .const import (
    CONF_PRIMARY_ENGINE,
    CONF_SHADOW_ENGINE,
    DEFAULT_PRIMARY_ENGINE,
    DEFAULT_SHADOW_ENGINE,
    ENGINE_NONE,
    ENGINE_V3,
    ENGINE_V4,
    V4_ARCHIVE_FILE,
    V4_MODEL_FILE,
)

_LOGGER = logging.getLogger(__name__)

CA_LINE = "_ca"  # the line that follows the sensor Climate Advisor reads (the archive's and the chart's name for it)
USABLE_CONFIDENCE = ("low", "medium", "high")  # "none" = still learning (v3's own ramp-fallback threshold is the same)

Path = list[tuple[datetime, float]]  # [(timestamp, temp in degrees F)]


@runtime_checkable
class PredictionEngine(Protocol):
    """What the coordinator and the display need from an engine. Temperatures are internal degrees F."""

    id: str
    label: str
    state_file: str  # the zone's own state file (the fitted model); v3's is the learning database
    archive_file: str  # what the engine said N hours ahead, for the accuracy views

    def fit(self, history: Mapping[str, Any]) -> Any:
        """Learn from the zone's history and return the engine's state (blocking; run in the executor)."""

    def predict(self, state: Any, horizon_hours: float, plan_inputs: Mapping[str, Any]) -> dict[str, Path]:
        """``{sensor: [(ts, temp_f), ...]}`` for the next ``horizon_hours`` (``"_ca"`` = the line CA reads)."""

    def confidence(self, state: Any) -> str:
        """ "none" | "low" | "medium" | "high"."""

    def state_of(self, source: Any) -> Any:
        """The state the source (coordinator) currently holds for this engine."""

    def future_path(self, source: Any) -> dict[str, Path]:
        """The newest forward prediction the source cached for this engine (what the chart draws)."""

    def past(self, source: Any, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        """What the engine said ``lead`` ahead for each 30-minute slot in ``[start, end)``: ``{slot_epoch: temp_f}``."""


def usable(engine: PredictionEngine, source: Any) -> bool:
    """True once the engine has a usable fit (confidence above "none")."""
    try:
        return engine.confidence(engine.state_of(source)) in USABLE_CONFIDENCE
    except Exception:  # noqa: BLE001 - a display read never raises
        _LOGGER.debug("prediction engine %s: usable() failed", engine.id, exc_info=True)
        return False


def _parse(entries: Sequence[Mapping[str, Any]] | None) -> Path:
    out: Path = []
    for e in entries or []:
        ts, temp = e.get("ts"), e.get("temp")
        if not isinstance(ts, str) or temp is None:
            continue
        try:
            out.append((datetime.fromisoformat(ts), float(temp)))
        except ValueError:
            continue
    return out


class V3Engine:
    """Today's ODE prediction: the learning database's thermal model, ``_last_predicted_indoor``, ``_pred_archive``."""

    id = ENGINE_V3
    label = "v3"
    state_file = "climate_advisor_learning.json"
    archive_file = "climate_advisor_learning.json"  # the 4 h-ahead ``pred_archive`` lives in the learning state

    def fit(self, history: Mapping[str, Any]) -> Any:
        """v3 learns continuously inside the learning engine; its state is the thermal model it holds."""
        learning = history.get("learning")
        getter = getattr(learning, "get_thermal_model", None)
        return getter() if callable(getter) else dict(history.get("thermal_model") or {})

    def predict(self, state: Any, horizon_hours: float, plan_inputs: Mapping[str, Any]) -> dict[str, Path]:
        build: Callable[..., Any] | None = plan_inputs.get("build_predicted_indoor")
        if build is None:
            return {}
        rows = build(thermal_model=state, **dict(plan_inputs.get("kwargs") or {}))
        pts = _parse(rows)
        if pts:
            end = pts[0][0].timestamp() + horizon_hours * 3600.0
            pts = [p for p in pts if p[0].timestamp() <= end]
        return {CA_LINE: pts} if pts else {}

    def confidence(self, state: Any) -> str:
        return str((state or {}).get("confidence", "none")) if isinstance(state, Mapping) else "none"

    def state_of(self, source: Any) -> Any:
        engine = getattr(source, "automation_engine", None)
        model = getattr(engine, "_thermal_model", None)
        return model if isinstance(model, Mapping) else {}

    def future_path(self, source: Any) -> dict[str, Path]:
        pts = _parse(getattr(source, "_last_predicted_indoor", None))
        return {CA_LINE: pts} if pts else {}

    def past(self, source: Any, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        if sensor != CA_LINE or lead != "4h":  # v3 logs one line, made about 4 h ahead
            return {}
        archive = getattr(source, "_pred_archive", None) or {}
        return {int(k): float(v) for k, v in archive.items() if start <= k < end}


class V4Engine:
    """The per-sensor v4 engine: ``v4_shadow`` fit/predict, its model file and its ``V4Archive`` (1/4/12/24 h)."""

    id = ENGINE_V4
    label = "v4"
    state_file = V4_MODEL_FILE
    archive_file = V4_ARCHIVE_FILE

    def fit(self, history: Mapping[str, Any]) -> Any:
        from . import v4_shadow  # noqa: PLC0415

        model, _seconds, _summary = v4_shadow.run_fit(**dict(history))
        return model

    def predict(self, state: Any, horizon_hours: float, plan_inputs: Mapping[str, Any]) -> dict[str, Path]:
        from . import v4_shadow  # noqa: PLC0415

        preds, _mass = v4_shadow.run_cycle(model_dict=state, **dict(plan_inputs))
        if not preds:
            return {}
        out: dict[str, Path] = {}
        for sensor, pts in preds.items():
            if not pts:
                continue
            end = pts[0][0].timestamp() + horizon_hours * 3600.0
            out[str(sensor)] = [(t, float(v)) for t, v in pts if t.timestamp() <= end]
        return out

    def confidence(self, state: Any) -> str:
        return str(state.get("confidence", "none")) if isinstance(state, Mapping) else "none"

    def state_of(self, source: Any) -> Any:
        store = getattr(source, "_v4_store", None)
        model = getattr(store, "v4_model", None)
        return model if isinstance(model, Mapping) else None

    def future_path(self, source: Any) -> dict[str, Path]:
        future = getattr(source, "_v4_last_future", None)
        preds = future.get("predictions") if isinstance(future, Mapping) else None
        if not isinstance(preds, Mapping):
            return {}
        out = {str(k): [(t, float(v)) for t, v in pts] for k, pts in preds.items() if pts}
        if (
            CA_LINE not in out
        ):  # the CA-read line follows the model's primary sensor (the archive derives it the same way)
            model = self.state_of(source)
            primary = model.get("primary") if isinstance(model, Mapping) else None
            if primary in out:
                out[CA_LINE] = list(out[primary])
        return out

    def past(self, source: Any, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        archive = getattr(source, "_v4_archive", None)
        return dict(archive.series(sensor, lead, start, end)) if archive is not None else {}


ENGINES: dict[str, PredictionEngine] = {}


def register(engine: PredictionEngine) -> PredictionEngine:
    """Add (or replace) an engine. A v5 is one call; nothing else in the integration names the engine ids."""
    ENGINES[engine.id] = engine
    return engine


def unregister(engine_id: str) -> None:
    ENGINES.pop(engine_id, None)


register(V3Engine())
register(V4Engine())

_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        _LOGGER.warning(msg, *args)


def primary_id(config: Mapping[str, Any] | None) -> str:
    """The configured primary engine; an unknown value reads as the default (one warning per bad value)."""
    raw = (config or {}).get(CONF_PRIMARY_ENGINE, DEFAULT_PRIMARY_ENGINE)
    if isinstance(raw, str) and raw in ENGINES:
        return raw
    _warn_once(f"p:{raw}", "Unknown primary_engine %r: using %s", raw, DEFAULT_PRIMARY_ENGINE)
    return DEFAULT_PRIMARY_ENGINE if DEFAULT_PRIMARY_ENGINE in ENGINES else next(iter(ENGINES))


def shadow_id(config: Mapping[str, Any] | None) -> str:
    """The configured shadow engine or ``"none"``. Unknown, or equal to the primary, reads as none (one warning)."""
    raw = (config or {}).get(CONF_SHADOW_ENGINE, DEFAULT_SHADOW_ENGINE)
    if raw in (None, "", ENGINE_NONE):
        return ENGINE_NONE
    if not isinstance(raw, str) or raw not in ENGINES:
        _warn_once(f"s:{raw}", "Unknown shadow_engine %r: no comparison engine", raw)
        return ENGINE_NONE
    if raw == primary_id(config):
        _warn_once(f"s=p:{raw}", "shadow_engine %r equals the primary engine: no comparison engine", raw)
        return ENGINE_NONE
    return raw


def running_ids(config: Mapping[str, Any] | None) -> list[str]:
    """The engines that run for this zone: the primary, the shadow, and v3 while it drives control (0.9.4)."""
    ids = [primary_id(config)]
    shadow = shadow_id(config)
    if shadow != ENGINE_NONE:
        ids.append(shadow)
    if ENGINE_V3 in ENGINES and ENGINE_V3 not in ids:
        ids.append(ENGINE_V3)  # control reads v3; it is not a display choice yet
    return ids


def is_running(config: Mapping[str, Any] | None, engine_id: str) -> bool:
    return engine_id in running_ids(config)


def validate(primary: str, shadow: str) -> str | None:
    """An options-form error key, or None. The shadow, when set, must differ from the primary."""
    if primary not in ENGINES:
        return "unknown_engine"
    if shadow not in (ENGINE_NONE, "") and shadow not in ENGINES:
        return "unknown_engine"
    if shadow not in (ENGINE_NONE, "") and shadow == primary:
        return "shadow_same_as_primary"
    return None


def options(include_none: bool) -> list[tuple[str, str]]:
    """``[(id, label)]`` for the settings forms, straight from the registry (plus "none" for the shadow)."""
    out = [(e.id, e.label) for e in ENGINES.values()]
    return ([(ENGINE_NONE, "None")] + out) if include_none else out


def engine_state(source: Any, config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The display block: who is primary/shadow, each engine's status, and the learning/fallback state of the primary.

    ``fallback`` is True while the primary has no usable fit: the chart then shows the schedule estimate (the engine-
    agnostic no-model path), never the other engine. ``fitting`` is True while the primary's fit is running.
    """
    primary, shadow = primary_id(config), shadow_id(config)
    running = running_ids(config)
    catalogue = []
    for e in ENGINES.values():
        is_run = e.id in running
        try:
            ok = usable(e, source) if is_run else False
            conf = e.confidence(e.state_of(source)) if is_run else "none"
        except Exception:  # noqa: BLE001 - display only
            ok, conf = False, "none"
        catalogue.append(
            {
                "id": e.id,
                "label": e.label,
                "running": is_run,
                "primary": e.id == primary,
                "shadow": e.id == shadow,
                "usable": ok,
                "confidence": conf,
            }
        )
    prim = next(c for c in catalogue if c["id"] == primary)
    fitting = bool(getattr(source, "_v4_fit_running", False)) if primary == ENGINE_V4 else False
    fallback = not prim["usable"]
    if fallback and fitting:
        text = f"Learning: {ENGINES[primary].label} is fitting from your history"
    elif fallback:
        text = "Prediction: learning — showing the schedule estimate"
    else:
        text = ""
    return {
        "primary": primary,
        "shadow": shadow,
        "engines": catalogue,
        "primary_state": "learning" if fallback else "ready",
        "fallback": fallback,
        "fitting": fitting,
        "text": text,
    }


def primary_path(path_by_sensor: Mapping[str, Path]) -> Path:
    """The CA-read line of an engine's forward prediction, else the first sensor's, else nothing."""
    if CA_LINE in path_by_sensor:
        return list(path_by_sensor[CA_LINE])
    return list(next(iter(path_by_sensor.values()), []))
