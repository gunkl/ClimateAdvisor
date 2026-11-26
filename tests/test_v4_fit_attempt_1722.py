"""A restart must not refit at once (Issue #1722, corrected candidate B).

Occupant view: restarting Home Assistant (an update, a reload) used to start a fresh v4 fit within minutes when the
model was missing, "none" or unusable, because the 6 h retry throttle lived only in memory. On a Raspberry Pi that
fit starved the event loop right after every restart. The start of every fit / night replay is now stamped in the
model file (wall clock, saved BEFORE the work starts) and ``async_restore_state`` seeds the throttle from it.

Runs the REAL coordinator headless (the ``tests/test_v4_shadow_wiring.py`` harness) with the fake predictor.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import night_replay, v4_shadow
from custom_components.climate_advisor.const import V4_FIT_RETRY_MIN_INTERVAL_S
from custom_components.climate_advisor.v4_archive import V4ModelStore
from tests.test_v4_shadow_wiring import _build, _cycle, _fake_model_dict, _seed_cycle_inputs
from tests.v4_fake_predictor import FakePredictor, install
from tools.sim_harness._loop import run_coro


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePredictor:
    return install(monkeypatch)


def _file(coord) -> V4ModelStore:
    """The model file as a fresh process would read it."""
    store = V4ModelStore(Path(coord.hass.config.config_dir), entry_id=coord._entry_id)
    store.load()
    return store


def _restart(coord, scheduler) -> None:
    """What a restart does to the fit throttle: memory gone, the files read back."""
    coord._v4_last_fit_attempt = None
    coord._v4_store.fit_attempt_at = None

    async def _read() -> None:
        await coord._executor_job(coord._v4_store.load)

    with scheduler.installed():
        run_coro(_read())
        coord._v4_restore_attempt_throttle()


def _write_stamp(coord, stamp: Any) -> None:
    store = _file(coord)
    store.fit_attempt_at = stamp
    store.v4_model = None
    assert store.save() is True


# ---------------------------------------------------------------------------
# the stamp is saved before the work starts
# ---------------------------------------------------------------------------


def test_fit_attempt_stamp_is_on_disk_before_the_fit_runs(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    seen: dict[str, Any] = {}
    real = v4_shadow.run_fit

    def spy(**kw: Any):
        seen["on_disk"] = _file(coord).fit_attempt_at  # read while the fit is running
        return real(**kw)

    monkeypatch.setattr(v4_shadow, "run_fit", spy)
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
        scheduler.advance_by(0)
        expected = scheduler.now().isoformat()
    assert seen["on_disk"] is not None, "the attempt stamp was not saved before the fit started"
    assert abs((_parse(seen["on_disk"]) - _parse(expected)).total_seconds()) < 5


def test_night_replay_stamp_is_on_disk_before_the_replay_runs(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    seen: dict[str, Any] = {}

    def spy(**_kw: Any) -> dict:
        seen["on_disk"] = _file(coord).fit_attempt_at
        return {"generated": "x", "n_nights_replayed": 0, "n_ok_confidence_fits": 0, "provisional": True}

    monkeypatch.setattr(night_replay, "run_nightly", spy)
    with scheduler.installed():
        assert coord._v4_start_night_replay("night_replay") is True
        scheduler.advance_by(0)
    assert seen["on_disk"] is not None, "the attempt stamp was not saved before the replay started"


def _parse(raw: str):
    from datetime import datetime

    return datetime.fromisoformat(raw)


# ---------------------------------------------------------------------------
# a restart within 6 h does not refit; after 6 h it does
# ---------------------------------------------------------------------------


def test_restart_within_the_retry_interval_does_not_refit(
    fake: FakePredictor, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _write_stamp(coord, (scheduler.now() - timedelta(hours=1)).isoformat())
    caplog.set_level(logging.INFO)
    _restart(coord, scheduler)
    assert coord._v4_last_fit_attempt is not None
    _cycle(coord, scheduler, drain=True)  # no model: the "first" fit would start here without the throttle
    assert fake.fit_calls == [], "a restart one hour after an attempt must not refit"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("v4 fit retry throttled after restart: last attempt 60 min ago" in m for m in msgs)


def test_async_restore_state_seeds_the_throttle(fake: FakePredictor, caplog: pytest.LogCaptureFixture) -> None:
    """The real restore path (not just the helper) reads the stamp: a restart one hour after an attempt waits."""
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _write_stamp(coord, (scheduler.now() - timedelta(hours=1)).isoformat())
    coord._v4_last_fit_attempt = None
    caplog.set_level(logging.INFO)
    with scheduler.installed():
        run_coro(coord.async_restore_state())
    assert coord._v4_last_fit_attempt is not None
    assert any("v4 fit retry throttled after restart" in r.getMessage() for r in caplog.records)
    _cycle(coord, scheduler, drain=True)
    assert fake.fit_calls == []


def test_restart_after_the_retry_interval_refits(fake: FakePredictor) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _write_stamp(coord, (scheduler.now() - timedelta(seconds=V4_FIT_RETRY_MIN_INTERVAL_S + 60)).isoformat())
    _restart(coord, scheduler)
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1 and coord._v4_store.v4_model is not None


def test_throttle_ends_when_the_remaining_time_has_passed(fake: FakePredictor) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _write_stamp(coord, (scheduler.now() - timedelta(hours=5)).isoformat())
    _restart(coord, scheduler)
    _cycle(coord, scheduler, drain=True)
    assert fake.fit_calls == []
    coord._v4_last_fit_attempt -= 2 * 3600  # 7 h since the attempt now
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1


def test_restart_throttles_the_night_replay_too(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    monkeypatch.setattr(night_replay, "run_nightly", lambda **_k: pytest.fail("a restarted replay must wait"))
    store = _file(coord)
    store.v4_model = _fake_model_dict(coord)
    store.fit_attempt_at = (scheduler.now() - timedelta(minutes=30)).isoformat()
    assert store.save() is True
    _restart(coord, scheduler)
    assert coord._v4_store.v4_model is not None and coord._v4_store.night_validation is None
    _cycle(coord, scheduler, drain=True)  # a model without a night record: replay due, but throttled
    assert coord._v4_night_replay_running is False


@pytest.mark.parametrize(
    "stamp",
    [
        "garbage",
        "",
        12345,
        None,
        "2026-13-45T99:99:99",
        "2026-11-25T10:00:00",  # naive: not comparable with the wall clock, ignored
    ],
)
def test_garbage_stamp_is_ignored(fake: FakePredictor, stamp: Any) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    store = _file(coord)
    store.fit_attempt_at = stamp if isinstance(stamp, str) else None
    store.save()
    _restart(coord, scheduler)
    assert coord._v4_last_fit_attempt is None
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1  # unthrottled: the first fit runs


def test_a_non_string_stamp_in_the_file_loads_as_none(tmp_path: Path) -> None:
    store = V4ModelStore(tmp_path, entry_id="z")
    store._path.write_text('{"v": 1, "fit_attempt_at": 123}', encoding="utf-8")
    store.load()
    assert store.fit_attempt_at is None


def test_future_dated_stamp_is_ignored(fake: FakePredictor) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _write_stamp(coord, (scheduler.now() + timedelta(hours=2)).isoformat())
    _restart(coord, scheduler)
    assert coord._v4_last_fit_attempt is None
    _cycle(coord, scheduler, drain=True)
    assert len(fake.fit_calls) == 1


# ---------------------------------------------------------------------------
# a restart DURING a fit still throttles (the fit wrote no last_fit)
# ---------------------------------------------------------------------------


def test_restart_during_a_fit_still_throttles(fake: FakePredictor) -> None:
    """The fit task saved its stamp first, then the restart killed it: no ``last_fit``, no model, only the stamp."""
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    with scheduler.installed():
        coord._v4_stamp_attempt()  # what _v4_start_fit does ...
        run_coro(coord._v4_persist_attempt())  # ... and the task's first step: the stamp reaches the file, no work yet
    on_disk = _file(coord)
    assert on_disk.fit_attempt_at is not None and on_disk.last_fit is None and on_disk.v4_model is None
    _restart(coord, scheduler)
    assert coord._v4_last_fit_attempt is not None
    _cycle(coord, scheduler, drain=True)
    assert fake.fit_calls == [], "a restart during a fit must not start another at once"


# ---------------------------------------------------------------------------
# the nightly fit is unaffected
# ---------------------------------------------------------------------------


def test_nightly_fit_ignores_the_throttle(fake: FakePredictor) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _write_stamp(coord, (scheduler.now() - timedelta(minutes=5)).isoformat())
    _restart(coord, scheduler)
    assert coord._v4_last_fit_attempt is not None  # throttled for the cycle's first/retry path ...
    with scheduler.installed():
        run_coro(coord._v4_nightly_fit())  # ... but the 03:15 timer does not consult it
        scheduler.advance_by(0)
    assert len(fake.fit_calls) == 1
    assert coord._v4_last_fit["reason"] == "nightly"


# ---------------------------------------------------------------------------
# the file format stays compatible
# ---------------------------------------------------------------------------


def test_stamp_is_additive_and_an_older_reader_ignores_it(tmp_path: Path) -> None:
    """An older release reads the file with ``.get`` per key and never checks ``v``: the extra key is ignored."""
    import json

    store = V4ModelStore(tmp_path, entry_id="z")
    store.fit_attempt_at = "2026-11-26T03:15:00+00:00"
    assert store.save() is True
    raw = json.loads(store._path.read_text())
    assert raw["v"] == 1 and raw["fit_attempt_at"] == "2026-11-26T03:15:00+00:00"
    older = {k: v for k, v in raw.items() if k != "fit_attempt_at"}  # what an older release understands
    store._path.write_text(json.dumps(older), encoding="utf-8")
    store.load()
    assert store.fit_attempt_at is None and store.v4_model is None
