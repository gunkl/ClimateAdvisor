"""v4 in shadow inside the coordinator (Issue #1230 R1): fit scheduling, teardown, per-cycle predict + archive.

Occupant view: v4 predicts beside today's prediction on the real house so its accuracy can be compared later. It must
have ZERO effect on how the house is run or what Status shows, and any v4 error must be invisible (caught, logged).
Runs the REAL coordinator headless (FakeHass + FakeScheduler) with a fake ``predictor_v4``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.const import V4_FIT_HOUR, V4_FIT_MINUTE
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.v4_archive import slot_key
from tests.v4_fake_predictor import FakePredictor, install, uninstall
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import (
    async_unload_entry_like_ha,
    build_headless_coordinator,
    build_headless_multi_zone,
)
from tools.sim_harness.fake_hass import FakeState

# Near the real wall clock: chart_log prunes rows older than a year against it, so a fixed old date would lose them.
_START = (datetime.now(UTC) - timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
_WEATHER = "weather.test_forecast"


def _hourly(start: datetime, hours: int = 60, base: float = 40.0) -> list[dict]:
    first = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return [
        {"datetime": (first + timedelta(hours=h)).isoformat(), "temperature": base + (h % 12)} for h in range(hours)
    ]


def _bands(start: datetime, hours: int = 60) -> list[dict]:
    first = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return [{"ts": (first + timedelta(hours=h)).isoformat(), "lower": 68.0, "upper": 76.0} for h in range(hours)]


def _build(*, coalesced: bool = True):
    coord, fake_hass, scheduler, _events = build_headless_coordinator(
        start_time=_START,
        skip_startup_coalesce=coalesced,
        climate_attributes={"current_temperature": 70, "temperature": 68},
    )
    return coord, fake_hass, scheduler


def _seed_cycle_inputs(coord, scheduler, *, indoor: float = 70.0) -> None:
    """What a cycle leaves behind just before the v4 hook: a chart row, the hourly forecast and the band schedule."""
    now = scheduler.now()
    coord._hourly_forecast_temps = _hourly(now)
    coord._target_band_schedule = _bands(now)
    coord._chart_log.append(
        hvac="idle",
        fan=False,
        indoor=indoor,
        outdoor=41.0,
        pred_outdoor=42.0,
        setpoint=68.0,
        indoor_sensors={coord.config["climate_entity"]: indoor},
        ts=now.isoformat(),
    )


def _fake_model_dict(coord, **over: Any) -> dict:
    d = {
        "schema_version": 1,
        "fitted_at": "2026-01-15T03:15:00+00:00",
        "primary": coord.config["climate_entity"],
        "sensors": [coord.config["climate_entity"]],
        "confidence": "ok",
        "fit": {"n_anchors": 50, "span_days": 14.0, "mae_by_lead": {"1h": 0.3, "4h": 0.8, "12h": 1.4}},
    }
    d.update(over)
    return d


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePredictor:
    return install(monkeypatch)


def _cycle(coord, scheduler, *, drain: bool = False) -> None:
    """Run the hook once. Draining the scheduler is opt-in: it would also run the harness's queued full refreshes."""
    with scheduler.installed():
        run_coro(coord._v4_shadow_cycle())
        if drain:
            scheduler.advance_by(0)


def _tick(scheduler, seconds: float) -> None:
    """Move the virtual clock without firing timers (a fired timer would re-enter the hook through a full refresh)."""
    scheduler._clock = scheduler._clock + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Scheduling and teardown
# ---------------------------------------------------------------------------


def test_nightly_fit_registered_at_0315_and_gone_after_unload(fake: FakePredictor) -> None:
    zones, fake_hass, scheduler = build_headless_multi_zone(
        configs=[{"climate_entity": "climate.zone_a", "zone_title": "Zone A"}], start_time=_START
    )
    zone = zones["zone_0"]
    coord, entry = zone["coordinator"], zone["entry"]

    def nightly():
        return [e for e in scheduler.live_callbacks() if "_v4_nightly_fit" in getattr(e.callback, "__qualname__", "")]

    # the daily job is armed for the next 03:15 local
    armed = [
        fire_at
        for (_t, fire_at, cb) in scheduler.armed_log
        if fire_at.hour == V4_FIT_HOUR and fire_at.minute == V4_FIT_MINUTE
    ]
    assert armed, "no timer armed for 03:15"
    assert any(e.fire_at.hour == V4_FIT_HOUR and e.fire_at.minute == V4_FIT_MINUTE for e in scheduler.live_callbacks())

    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
    leftover = [getattr(e.callback, "__qualname__", repr(e.callback)) for e in scheduler.live_callbacks()]
    assert leftover == [], f"timers still live after unload: {leftover}"
    assert nightly() == []
    assert coord._v4_fit_running is False


def test_nightly_timer_fits_and_persists_model(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    caplog.set_level(logging.INFO)
    with scheduler.installed():
        # 16:00 -> next 03:15 (11 h 15 m)
        scheduler.advance_to(_START.replace(hour=3, minute=15) + timedelta(days=1) + timedelta(seconds=1))
    assert len(fake.fit_calls) >= 1
    call = fake.fit_calls[0]
    assert call["primary"] == coord.config["climate_entity"] and call["samples"] >= 1
    stored = coord.learning._state.v4_model
    assert stored is not None and stored["confidence"] == "ok"
    assert any("v4 fit (nightly)" in r.getMessage() and "confidence=ok" in r.getMessage() for r in caplog.records)
    # persisted with the learning file
    reloaded = LearningEngine(Path(coord.hass.config.config_dir), entry_id=coord._entry_id)
    reloaded.load_state()
    assert reloaded._state.v4_model == stored


def test_fit_inputs_are_chart_rows_and_forecast_archive_snapshots(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    seen: list = []
    coord._forecast_archive.get_snapshots = lambda s, e: seen.append((s, e)) or [{"issued": "x", "hours": []}]  # type: ignore[method-assign]
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
        scheduler.advance_by(0)
    assert seen, "the forecast archive was not asked for snapshots"
    assert fake.sample_calls[0]["entries"] >= 1
    assert fake.sample_calls[0]["snapshots"] == [{"issued": "x", "hours": []}]


def test_first_fit_waits_for_startup_coalescing_then_runs_once(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build(coalesced=False)
    _seed_cycle_inputs(coord, scheduler)
    assert coord._startup_coalesce_active is True
    _cycle(coord, scheduler)
    assert fake.fit_calls == [] and coord._v4_fit_running is False
    coord._startup_coalesce_active = False
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1 and coord.learning._state.v4_model is not None
    _cycle(coord, scheduler)  # a model exists now: no second first-fit
    assert len(fake.fit_calls) == 1


def test_failed_first_fit_retries_only_after_the_back_off(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    fake.fail_fit = True
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1 and coord.learning._state.v4_model is None
    assert coord._v4_last_fit["ok"] is False
    _cycle(coord, scheduler, drain=True)  # immediately again: backed off
    assert len(fake.fit_calls) == 1
    coord._v4_last_fit_attempt -= 7 * 3600
    fake.fail_fit = False
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 2 and coord.learning._state.v4_model is not None


def test_failed_nightly_fit_keeps_the_previous_model(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    previous = _fake_model_dict(coord, fitted_at="2026-01-14T03:15:00+00:00")
    coord.learning._state.v4_model = dict(previous)
    fake.fail_fit = True
    with scheduler.installed():
        assert coord._v4_start_fit("nightly")
        scheduler.advance_by(0)
    assert coord.learning._state.v4_model == previous
    assert coord._v4_fit_running is False  # the next night can try again
    assert any("keeping the previous model" in r.getMessage() for r in caplog.records)


def test_a_fit_never_overlaps_another(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    with scheduler.installed():
        assert coord._v4_start_fit("first") is True
        assert coord._v4_fit_running is True
        # the nightly timer firing, a cycle's first-fit check and a manual call all find the fit running
        assert coord._v4_start_fit("nightly") is False
        run_coro(coord._v4_nightly_fit())
        scheduler.advance_by(0)
    assert len(fake.fit_calls) == 1
    assert coord._v4_fit_running is False
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True  # free again afterwards
        scheduler.advance_by(0)
    assert len(fake.fit_calls) == 2


def test_unload_while_a_fit_is_pending_drops_its_result(fake: FakePredictor) -> None:
    zones, fake_hass, scheduler = build_headless_multi_zone(
        configs=[{"climate_entity": "climate.zone_a", "zone_title": "Zone A"}], start_time=_START
    )
    zone = zones["zone_0"]
    coord, entry = zone["coordinator"], zone["entry"]
    coord.config_entry = None  # the harness entry stub has no task API; _spawn falls back to hass.async_create_task
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        scheduler.advance_by(0)  # the queued fit task now runs on the dead instance
    assert coord.learning._state.v4_model is None, "a fit finishing after unload must not store its model"
    assert coord._v4_fit_running is False
    assert coord._v4_start_fit("nightly") is False  # a shut-down instance never starts one


def test_missing_engine_disables_the_shadow_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    uninstall(monkeypatch)
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)  # no model, no engine: nothing happens, nothing raises
    assert coord.learning._state.v4_model is None and coord._v4_fit_running is False
    coord.learning._state.v4_model = _fake_model_dict(coord)
    _cycle(coord, scheduler)  # a stored model but no engine: logged once, cycle unaffected
    assert coord._v4_archive.data == {}
    summary = coord.v4_model_summary()
    assert summary["engine_available"] is False and summary["has_model"] is True


# ---------------------------------------------------------------------------
# Per-cycle predict + archive
# ---------------------------------------------------------------------------


def test_cycle_predicts_24h_and_archives_first_write_wins(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)

    call = fake.predict_calls[0]
    assert len(call["inputs"]) == 96  # 24 h on the 15-minute grid
    first, last = call["inputs"][0], call["inputs"][-1]
    assert first.ts == scheduler.now() + timedelta(minutes=15) and last.ts == scheduler.now() + timedelta(hours=24)
    assert call["readings"]["_ca"] == 70.0 and call["readings"][coord.config["climate_entity"]] == 70.0
    # outdoor is the RAW hourly forecast interpolated, windows closed, fan off
    assert first.outdoor_f == pytest.approx(_expected_outdoor(scheduler.now(), first.ts))
    assert all(not i.windows_open and not i.fan_running for i in call["inputs"])

    climate = coord.config["climate_entity"]
    assert set(coord._v4_archive.data) == {climate, "_ca"}
    slot = slot_key(scheduler.now() + timedelta(hours=4))
    first_4h = coord._v4_archive.data[climate][slot]["4h"]
    assert set(coord._v4_archive.data[climate][slot]) == {"4h"}

    # a later cycle that predicts something else for the same slot/lead does not overwrite
    _tick(scheduler, 1800)
    _seed_cycle_inputs(coord, scheduler, indoor=75.0)
    _cycle(coord, scheduler)
    assert coord._v4_archive.data[climate][slot]["4h"] == first_4h
    assert coord._v4_last_cycle["predicted"] is True and coord._v4_last_cycle["archived"] > 0


def _expected_outdoor(now: datetime, ts: datetime) -> float:
    pts = [(datetime.fromisoformat(e["datetime"]), e["temperature"]) for e in _hourly(now)]
    if ts <= pts[0][0]:
        return pts[0][1]  # before the first forecast hour: held at it, not extrapolated
    for (t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
        if t0 <= ts <= t1:
            return v0 + (v1 - v0) * (ts - t0).total_seconds() / (t1 - t0).total_seconds()
    raise AssertionError("ts outside the forecast")


def test_setpoints_follow_the_day_mode_like_v3(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    # a cold day: classification says heat -> heat setpoint = band floor, no cool setpoint
    from custom_components.climate_advisor import coordinator as coord_mod

    original = coord_mod._compute_day_hvac_modes
    coord_mod._compute_day_hvac_modes = lambda *_a, **_k: {  # type: ignore[assignment]
        (scheduler.now() + timedelta(days=d)).date(): m for d, m in ((0, "heat"), (1, "cool"), (2, "off"))
    }
    try:
        _cycle(coord, scheduler)
    finally:
        coord_mod._compute_day_hvac_modes = original  # type: ignore[assignment]
    inputs = fake.predict_calls[0]["inputs"]
    today = scheduler.now().date()
    todays = [i for i in inputs if i.ts.date() == today]
    tomorrows = [i for i in inputs if i.ts.date() == today + timedelta(days=1)]
    assert todays and all(i.heat_sp_f == 68.0 and i.cool_sp_f is None for i in todays)
    assert tomorrows and all(i.cool_sp_f == 76.0 and i.heat_sp_f is None for i in tomorrows)


def test_hidden_node_is_stepped_and_reseeded_after_a_long_gap(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    state = coord.learning._state
    state.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert state.v4_mass_f is not None and state.v4_mass_at == scheduler.now().isoformat()
    assert fake.step_calls[0]["mass_in"] is None  # first ever: seeded from the reading
    # 30 minutes later: two grid steps carried on from the stored node
    _tick(scheduler, 1800)
    before = state.v4_mass_f
    _seed_cycle_inputs(coord, scheduler)
    n = len(fake.step_calls)
    _cycle(coord, scheduler)
    assert len(fake.step_calls) - n == 2 and fake.step_calls[n]["mass_in"] == before
    # three hours later (e.g. HA was down): stale, so re-seeded
    _tick(scheduler, 3 * 3600)
    _seed_cycle_inputs(coord, scheduler)
    n = len(fake.step_calls)
    _cycle(coord, scheduler)
    assert len(fake.step_calls) - n == 1 and fake.step_calls[n]["mass_in"] is None


def test_confidence_none_predicts_nothing_and_archives_nothing(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord, confidence="none")
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert coord._v4_archive.data == {} and coord._v4_last_cycle["predicted"] is False


def test_cycle_error_is_swallowed_and_warned_once(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    fake.fail_predict = True
    caplog.set_level(logging.DEBUG)
    _cycle(coord, scheduler)  # does not raise
    _cycle(coord, scheduler)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "v4 shadow cycle failed" in r.getMessage()]
    assert len(warns) == 1, "a repeating failure must not spam WARNINGs"
    assert coord._v4_archive.data == {}


def test_cycle_without_this_cycles_chart_row_is_skipped(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord)
    _cycle(coord, scheduler)  # no chart row at all
    assert fake.predict_calls == []
    coord._hourly_forecast_temps = _hourly(scheduler.now())
    coord._chart_log.append(
        hvac="idle", fan=False, indoor=70.0, outdoor=41.0, ts=(scheduler.now() - timedelta(hours=1)).isoformat()
    )
    _cycle(coord, scheduler)  # a stale row
    assert fake.predict_calls == []


def test_archive_file_write_is_throttled_and_flushed_on_shutdown(fake: FakePredictor) -> None:
    zones, fake_hass, scheduler = build_headless_multi_zone(
        configs=[{"climate_entity": "climate.zone_a", "zone_title": "Zone A"}], start_time=_START
    )
    zone = zones["zone_0"]
    coord, entry = zone["coordinator"], zone["entry"]
    fake_hass.states.set(
        "climate.zone_a", FakeState(state="heat", attributes={"current_temperature": 70, "temperature": 68})
    )
    coord.learning._state.v4_model = _fake_model_dict(coord, primary="climate.zone_a", sensors=["climate.zone_a"])
    path = coord._v4_archive._path
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert path.exists(), "the first dirty archive is written"
    first_mtime = path.stat().st_mtime_ns
    _tick(scheduler, 1800)
    _seed_cycle_inputs(coord, scheduler, indoor=71.0)
    _cycle(coord, scheduler)
    assert coord._v4_archive.dirty is True, "within the hour the file is not rewritten"
    assert path.stat().st_mtime_ns == first_mtime
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
    assert coord._v4_archive.dirty is False, "unload flushes what changed"
    on_disk = json.loads(path.read_text())
    assert set(on_disk["sensors"]) == {"climate.zone_a", "_ca"}


# ---------------------------------------------------------------------------
# The shadow changes nothing the occupant gets
# ---------------------------------------------------------------------------


def _full_cycle(coord, fake_hass, scheduler) -> tuple[list, dict, list]:
    fake_hass.states.set(_WEATHER, FakeState(state="sunny", attributes={"temperature": 41, "forecast": []}))
    coord._hourly_forecast_temps = _hourly(scheduler.now())
    with scheduler.installed():
        run_coro(coord._async_update_data())
        scheduler.advance_by(0)
    calls = [{k: v for k, v in c.items() if k not in ("time", "context")} for c in fake_hass.action_log]
    assert calls, "the cycle issued no service call: this comparison would prove little"
    return list(coord._last_predicted_indoor), dict(coord._pred_archive), calls


def test_a_full_cycle_gives_the_same_v3_results_with_and_without_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    """v3's prediction, its archive and every service call are identical whether v4 is on, failing or absent."""
    outcomes = []
    for mode in ("absent", "working", "failing"):
        if mode == "absent":
            uninstall(monkeypatch)
        else:
            fake = install(monkeypatch, FakePredictor(fail_predict=(mode == "failing")))
        coord, fake_hass, scheduler = _build()
        if mode != "absent":
            coord.learning._state.v4_model = _fake_model_dict(coord)
        outcomes.append((mode, _full_cycle(coord, fake_hass, scheduler)))
        if mode == "working":
            assert fake.predict_calls, "v4 did not run in the working case: this comparison would prove nothing"
            assert coord._v4_archive.data, "v4 archived nothing in the working case"
    baseline = outcomes[0][1]
    for mode, got in outcomes[1:]:
        assert got == baseline, f"v3 results changed when v4 was {mode}"


# ---------------------------------------------------------------------------
# Persistence of the model with the learning file
# ---------------------------------------------------------------------------


def test_learning_file_without_v4_fields_loads(tmp_path: Path) -> None:
    path = tmp_path / "climate_advisor_learning.json"
    path.write_text(json.dumps({"records": [], "thermal_observations": []}), encoding="utf-8")
    engine = LearningEngine(tmp_path)
    engine.load_state()
    assert engine._state.v4_model is None and engine._state.v4_mass_f is None and engine._state.v4_mass_at is None


def test_learning_file_round_trips_the_v4_model(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)
    engine._state.v4_model = {"schema_version": 1, "primary": "climate.a", "confidence": "low"}
    engine._state.v4_mass_f = 66.25
    engine._state.v4_mass_at = "2026-11-06T10:00:00+00:00"
    engine.save_state()
    again = LearningEngine(tmp_path)
    again.load_state()
    assert again._state.v4_model == engine._state.v4_model
    assert again._state.v4_mass_f == 66.25 and again._state.v4_mass_at == "2026-11-06T10:00:00+00:00"
    assert again._state.records == []  # the rest of the file is intact


def test_malformed_v4_values_are_dropped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "climate_advisor_learning.json"
    path.write_text(
        json.dumps({"records": [{"date": "2026-01-01"}], "v4_model": "oops", "v4_mass_f": True, "v4_mass_at": 5}),
        encoding="utf-8",
    )
    engine = LearningEngine(tmp_path)
    engine.load_state()
    assert engine._state.v4_model is None and engine._state.v4_mass_f is None and engine._state.v4_mass_at is None
    assert engine._state.records == [{"date": "2026-01-01"}]


def test_reset_all_clears_the_v4_model(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)
    engine._state.v4_model = {"schema_version": 1, "primary": "climate.a"}
    engine.reset("all")
    assert engine._state.v4_model is None


# ---------------------------------------------------------------------------
# Debug visibility
# ---------------------------------------------------------------------------


def test_debug_summary_shows_confidence_fit_time_span_and_training_mae(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord)
    summary = coord.v4_model_summary()
    assert summary["has_model"] is True and summary["confidence"] == "ok"
    assert summary["fitted_at"] == "2026-01-15T03:15:00+00:00" and summary["span_days"] == 14.0
    assert summary["mae_by_lead"] == {"1h": 0.3, "4h": 0.8, "12h": 1.4}
    assert summary["unit"] == "°F" and summary["engine_available"] is True
    state = coord.get_debug_state()
    assert state["v4_model"]["confidence"] == "ok"


def test_debug_summary_in_celsius_converts_errors_as_deltas(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.config["temp_unit"] = "celsius"
    coord.learning._state.v4_model = _fake_model_dict(coord, fit={"span_days": 3, "mae_by_lead": {"4h": 1.8}})
    coord.learning._state.v4_mass_f = 68.0
    s = coord.v4_model_summary()
    assert s["mae_by_lead"]["4h"] == 1.0  # 1.8 F error = 1.0 C (scale only)
    assert s["mass"] == 20.0 and s["unit"] == "°C"


def test_debug_summary_without_a_model(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    s = coord.v4_model_summary()
    assert s["has_model"] is False and s["confidence"] == "none"


def test_summary_of_a_garbage_model_never_raises() -> None:
    assert v4_shadow.model_summary("garbage")["has_model"] is False  # type: ignore[arg-type]
    assert v4_shadow.model_summary({"fit": 5})["has_model"] is True


def test_nowcast_inputs_and_warm_start_reach_the_engine(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    previous = _fake_model_dict(coord)
    coord.learning._state.v4_model = dict(previous)
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    call = fake.predict_calls[0]
    assert call["outdoor_now_f"] == 41.0 and call["forecast_now_f"] == 42.0  # today's observed outdoor + raw forecast
    with scheduler.installed():
        assert coord._v4_start_fit("nightly")
        scheduler.advance_by(0)
    warm = fake.fit_calls[-1]["warm_start"]
    assert warm is not None and warm.primary == previous["primary"]  # the stored model warm-starts the refit


def test_low_confidence_still_predicts_and_is_archived(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord.learning._state.v4_model = _fake_model_dict(coord, confidence="low")
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert coord._v4_archive.data and coord.v4_model_summary()["confidence"] == "low"
