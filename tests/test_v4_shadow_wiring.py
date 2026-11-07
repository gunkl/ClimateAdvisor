"""v4 in shadow inside the coordinator (Issue #1230 R1): fit scheduling, teardown, per-cycle predict + archive.

Occupant view: v4 predicts beside today's prediction on the real house so its accuracy can be compared later. It must
have ZERO effect on how the house is run or what Status shows, and any v4 error must be invisible (caught, logged).
Runs the REAL coordinator headless (FakeHass + FakeScheduler) with a fake ``predictor_v4``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.const import V4_FIT_HOUR, V4_FIT_MINUTE, V4_FIT_STAGGER_S
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.v4_archive import V4ModelStore, slot_key
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
    # (03:15 plus this zone's stable offset of up to 30 minutes - zones must not all fit at 03:15:00)
    off = v4_shadow.fit_offset_s(entry.entry_id, V4_FIT_STAGGER_S)
    want = (V4_FIT_HOUR, V4_FIT_MINUTE + off // 60, off % 60)
    armed = [
        fire_at for (_t, fire_at, cb) in scheduler.armed_log if (fire_at.hour, fire_at.minute, fire_at.second) == want
    ]
    assert armed, f"no timer armed for {want}"
    assert any((e.fire_at.hour, e.fire_at.minute, e.fire_at.second) == want for e in scheduler.live_callbacks())

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
    stored = coord._v4_store.v4_model
    assert stored is not None and stored["confidence"] == "ok"
    assert any("v4 fit (nightly)" in r.getMessage() and "confidence=ok" in r.getMessage() for r in caplog.records)
    # persisted in the model file (not the learning file)
    reloaded = V4ModelStore(Path(coord.hass.config.config_dir), entry_id=coord._entry_id)
    reloaded.load()
    assert reloaded.v4_model == stored and reloaded.last_fit["ok"] is True


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
    assert len(fake.fit_calls) == 1 and coord._v4_store.v4_model is not None
    _cycle(coord, scheduler)  # a model exists now: no second first-fit
    assert len(fake.fit_calls) == 1


def test_failed_first_fit_retries_only_after_the_back_off(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    fake.fail_fit = True
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1 and coord._v4_store.v4_model is None
    assert coord._v4_last_fit["ok"] is False
    _cycle(coord, scheduler, drain=True)  # immediately again: backed off
    assert len(fake.fit_calls) == 1
    coord._v4_last_fit_attempt -= 7 * 3600
    fake.fail_fit = False
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 2 and coord._v4_store.v4_model is not None


def test_failed_first_fit_is_persisted_at_once(fake: FakePredictor) -> None:
    """With no model the cycle returns before its own save: the failure must reach the file anyway."""
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    fake.fail_fit = True
    _cycle(coord, scheduler, drain=True)
    reloaded = V4ModelStore(Path(coord.hass.config.config_dir), entry_id=coord._entry_id)
    reloaded.load()
    assert reloaded.v4_model is None
    assert reloaded.last_fit_error == "fit failed; see the log" and reloaded.last_fit["ok"] is False
    assert coord._v4_store.dirty is False


def test_confidence_none_model_is_retried_like_no_model(fake: FakePredictor) -> None:
    """A fit on too little history stores a "none" model; it is refitted within 6 h, not only at 03:15."""
    coord, fake_hass, scheduler = _build(coalesced=False)
    coord._v4_store.v4_model = _fake_model_dict(coord, confidence="none")
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler, drain=True)  # startup coalescing: never during it
    assert fake.fit_calls == []
    coord._startup_coalesce_active = False
    fake.confidence = "none"
    _cycle(coord, scheduler, drain=True)  # first cycle after coalescing: retried at once
    assert len(fake.fit_calls) == 1 and coord._v4_last_fit["reason"] == "retry"
    _cycle(coord, scheduler, drain=True)  # still "none", but backed off
    assert len(fake.fit_calls) == 1
    coord._v4_last_fit_attempt -= 7 * 3600
    fake.confidence = "ok"
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 2 and coord._v4_store.v4_model["confidence"] == "ok"
    coord._v4_last_fit_attempt -= 7 * 3600
    _cycle(coord, scheduler, drain=True)  # a usable model: only the nightly timer refits it
    assert len(fake.fit_calls) == 2


def test_failed_nightly_fit_keeps_the_previous_model(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    previous = _fake_model_dict(coord, fitted_at="2026-01-14T03:15:00+00:00")
    coord._v4_store.v4_model = dict(previous)
    fake.fail_fit = True
    with scheduler.installed():
        assert coord._v4_start_fit("nightly")
        scheduler.advance_by(0)
    assert coord._v4_store.v4_model == previous
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
    assert coord._v4_store.v4_model is None, "a fit finishing after unload must not store its model"
    assert coord._v4_fit_running is False
    assert coord._v4_start_fit("nightly") is False  # a shut-down instance never starts one


def test_missing_engine_disables_the_shadow_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    uninstall(monkeypatch)
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)  # no model, no engine: nothing happens, nothing raises
    assert coord._v4_store.v4_model is None and coord._v4_fit_running is False
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _cycle(coord, scheduler)  # a stored model but no engine: logged once, cycle unaffected
    assert coord._v4_archive.data == {}
    summary = coord.v4_model_summary()
    assert summary["engine_available"] is False and summary["has_model"] is True


# ---------------------------------------------------------------------------
# Per-cycle predict + archive
# ---------------------------------------------------------------------------


def test_cycle_predicts_24h_and_archives_first_write_wins(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
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
    coord._v4_store.v4_model = _fake_model_dict(coord)
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
    state = coord._v4_store
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
    coord._v4_store.v4_model = _fake_model_dict(coord, confidence="none")
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert coord._v4_archive.data == {} and coord._v4_last_cycle["predicted"] is False


def test_cycle_keeps_the_future_lines_for_the_status_chart(fake: FakePredictor) -> None:
    """Issue #1230 R2: the newest 24 h lines, corrected outdoor forecast and no-plan baseline are kept (display)."""
    coord, fake_hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    assert coord._v4_last_future is None
    _cycle(coord, scheduler)
    future = coord._v4_last_future
    assert future is not None and future["at"] == scheduler.now().isoformat()
    climate = coord.config["climate_entity"]
    assert len(future["predictions"][climate]) == 96
    # corrected: the raw forecast plus the (fake) learned bias, on the same grid
    assert len(future["corrected"]) == 96
    assert future["corrected"][0][1] == pytest.approx(fake.predict_calls[0]["inputs"][0].outdoor_f + 1.5)
    # baseline: a second predict over the same plan with no thermostat setpoints (the house with no HVAC)
    base_call = fake.predict_calls[-1]
    assert all(i.heat_sp_f is None and i.cool_sp_f is None for i in base_call["inputs"])
    assert len(fake.predict_calls) == 2 and len(future["baseline"][climate]) == 96
    # a cycle with nothing to predict from drops the stale lines instead of drawing yesterday's future
    coord._hourly_forecast_temps = []
    _tick(scheduler, 1800)
    _seed_cycle_inputs(coord, scheduler)
    coord._hourly_forecast_temps = []
    _cycle(coord, scheduler)
    assert coord._v4_last_future is None


def test_cycle_error_is_swallowed_and_warned_once(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    coord, fake_hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
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
    coord._v4_store.v4_model = _fake_model_dict(coord)
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
    coord._v4_store.v4_model = _fake_model_dict(coord, primary="climate.zone_a", sensors=["climate.zone_a"])
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
    assert set(on_disk["sensors"]) == {"climate.zone_a"}  # "_ca" is derived from the primary line


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
            coord._v4_store.v4_model = _fake_model_dict(coord)
        outcomes.append((mode, _full_cycle(coord, fake_hass, scheduler)))
        if mode == "working":
            assert fake.predict_calls, "v4 did not run in the working case: this comparison would prove nothing"
            assert coord._v4_archive.data, "v4 archived nothing in the working case"
    baseline = outcomes[0][1]
    for mode, got in outcomes[1:]:
        assert got == baseline, f"v3 results changed when v4 was {mode}"


# ---------------------------------------------------------------------------
# Persistence: the model file, and the learning file stays as 0.9.3 reads it
# ---------------------------------------------------------------------------


def test_learning_file_carries_no_v4_key_so_a_rollback_reads_it(tmp_path: Path) -> None:
    """Rollback safety: 0.9.3 does LearningState(**data), which rejects unknown keys and resets the history."""
    engine = LearningEngine(tmp_path)
    engine.save_state()
    saved = json.loads((tmp_path / "climate_advisor_learning.json").read_text())
    assert not [k for k in saved if "v4" in k.lower()], saved.keys()
    assert not hasattr(engine._state, "v4_model")


def test_unknown_top_level_key_is_ignored_and_known_fields_survive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "climate_advisor_learning.json"
    path.write_text(
        json.dumps(
            {
                "records": [{"date": "2026-01-01"}],
                "dismissed_suggestions": ["x"],
                "clean_shutdown": True,
                "from_a_newer_release": {"a": 1},
                "another_new_key": 5,
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level(logging.DEBUG)
    engine = LearningEngine(tmp_path)
    engine.load_state()
    assert engine._state.records == [{"date": "2026-01-01"}]
    assert engine._state.dismissed_suggestions == ["x"] and engine._state.clean_shutdown is True
    drops = [r.getMessage() for r in caplog.records if "ignoring unknown keys" in r.getMessage()]
    assert len(drops) == 1 and "another_new_key" in drops[0] and "from_a_newer_release" in drops[0]


def test_a_file_with_no_learning_field_is_warned_not_silently_emptied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The tolerance must not hide a real problem: another file written over the learning file is a WARNING."""
    (tmp_path / "climate_advisor_learning.json").write_text(json.dumps({"entries": [{"ts": "x"}]}), encoding="utf-8")
    caplog.set_level(logging.INFO)
    engine = LearningEngine(tmp_path)
    engine.load_state()
    assert engine._state.records == []
    drops = [r for r in caplog.records if "ignoring unknown keys" in r.getMessage()]
    assert len(drops) == 1 and drops[0].levelno == logging.WARNING and "no known field" in drops[0].getMessage()


def _store(coord) -> V4ModelStore:
    return V4ModelStore(Path(coord.hass.config.config_dir), entry_id=coord._entry_id)


def test_model_file_round_trip_private_and_atomic(fake: FakePredictor, tmp_path: Path) -> None:
    store = V4ModelStore(tmp_path, entry_id="zoneA")
    assert store._path.name == "climate_advisor_v4_model_zoneA.json"
    store.v4_model = {"schema_version": 1, "primary": "climate.a", "confidence": "low"}
    store.v4_mass_f, store.v4_mass_at = 66.25, "2026-11-06T10:00:00+00:00"
    store.last_fit, store.last_fit_error = {"ok": False, "reason": "nightly"}, "fit failed; see the log"
    assert store.save() is True
    assert not list(tmp_path.glob("*.tmp"))
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(store._path).st_mode) == 0o600
    assert set(json.loads(store._path.read_text())) == {"v", "model", "mass_f", "mass_at", "last_fit", "last_fit_error"}
    again = V4ModelStore(tmp_path, entry_id="zoneA")
    again.load()
    assert (again.v4_model, again.v4_mass_f, again.v4_mass_at) == (store.v4_model, 66.25, "2026-11-06T10:00:00+00:00")
    assert again.last_fit == store.last_fit and again.last_fit_error == store.last_fit_error


@pytest.mark.parametrize("content", [None, "", "not json{", "[]", '{"model": "oops", "mass_f": true, "mass_at": 5}'])
def test_missing_or_corrupt_model_file_means_no_model(tmp_path: Path, content: str | None) -> None:
    store = V4ModelStore(tmp_path, entry_id="zoneA")
    if content is not None:
        store._path.write_text(content, encoding="utf-8")
    store.load()  # never raises
    assert store.v4_model is None and store.v4_mass_f is None and store.v4_mass_at is None


def test_corrupt_model_file_at_restore_triggers_a_first_fit(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store._path.write_text("garbage", encoding="utf-8")
    with scheduler.installed():
        run_coro(coord.async_restore_state())
    assert coord._v4_store.v4_model is None
    coord._chart_log.append(hvac="idle", fan=False, indoor=70.0, outdoor=41.0, ts=scheduler.now().isoformat())
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1 and coord._v4_store.v4_model is not None


def test_restart_restores_the_model_and_hidden_node_without_refitting(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _cycle(coord, scheduler)
    saved_mass = coord._v4_store.v4_mass_f
    assert saved_mass is not None
    with scheduler.installed():
        run_coro(coord._v4_flush_on_shutdown())
    fresh = _store(coord)
    fresh.load()
    assert fresh.v4_model == _fake_model_dict(coord) and fresh.v4_mass_f == saved_mass


def test_shutdown_flush_retries_once_then_warns_with_the_loss(
    fake: FakePredictor, caplog: pytest.LogCaptureFixture
) -> None:
    coord, fake_hass, scheduler = _build()
    coord._v4_archive.data = {"_ca": {1800: {"4h": 1.0}, 3600: {"4h": 2.0}}}
    coord._v4_archive.dirty = True
    coord._v4_store.dirty = True
    calls = {"archive": 0, "store": 0}

    def archive_flaky() -> bool:
        calls["archive"] += 1
        return calls["archive"] >= 2  # fails once, the retry works

    def store_dead() -> bool:
        calls["store"] += 1
        return False

    coord._v4_archive.save = archive_flaky  # type: ignore[method-assign]
    coord._v4_store.save = store_dead  # type: ignore[method-assign]
    with scheduler.installed():
        run_coro(coord._v4_flush_on_shutdown())
    assert calls == {"archive": 2, "store": 2}
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("v4 archive" in w for w in warns), "a retry that worked must not warn"
    assert any("v4 model: could not be written" in w for w in warns)
    # and a double failure names the slots lost
    calls["archive"] = -10
    caplog.clear()
    with scheduler.installed():
        run_coro(coord._v4_flush_on_shutdown())
    assert any("2 slots" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ---------------------------------------------------------------------------
# Debug visibility
# ---------------------------------------------------------------------------


def test_debug_summary_shows_confidence_fit_time_span_and_training_mae(fake: FakePredictor) -> None:
    coord, fake_hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
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
    coord._v4_store.v4_model = _fake_model_dict(coord, fit={"span_days": 3, "mae_by_lead": {"4h": 1.8}})
    coord._v4_store.v4_mass_f = 68.0
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
    coord._v4_store.v4_model = dict(previous)
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
    coord._v4_store.v4_model = _fake_model_dict(coord, confidence="low")
    _seed_cycle_inputs(coord, scheduler)
    _cycle(coord, scheduler)
    assert coord._v4_archive.data and coord.v4_model_summary()["confidence"] == "low"


def test_a_fit_that_cannot_be_scheduled_does_not_block_later_fits(fake: FakePredictor, monkeypatch) -> None:
    """If spawning the fit task fails, "running" must not stay stuck (every later fit would be skipped)."""
    coord, fake_hass, scheduler = _build()

    def boom(coro, **_kw):
        raise RuntimeError("entry not loaded")

    monkeypatch.setattr(coord, "_spawn", boom)
    with pytest.raises(RuntimeError):
        coord._v4_start_fit("nightly")
    assert coord._v4_fit_running is False
    monkeypatch.undo()
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
