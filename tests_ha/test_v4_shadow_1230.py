"""v4 in shadow on real Home Assistant (Issue #1230 R1): clean unload mid-fit, entry-owned task, endpoint auth.

Occupant view: saving Climate Advisor settings reloads the zone. The v4 shadow adds a nightly 03:15 timer and a fit that
can run for up to a minute in the executor; neither may outlive the old zone instance (no lingering timer, task or
thread: HA's autouse ``verify_cleanup`` fails the test otherwise), and a fit finishing after the unload must not store
a model into the dead instance. The prediction-accuracy page is behind HA's login like every other view.

Uses a fake ``predictor_v4`` (the real fit's maths are tested elsewhere); this tier checks the wiring against HA itself.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.const import DOMAIN, V4_FIT_HOUR, V4_FIT_MINUTE
from tests_ha.test_unload_cleanup import _setup_zone


class _FakeEngine:
    """The slice of predictor_v4 the fit path calls. ``fit`` can be held open to model a long fit."""

    V4_STEP_MINUTES = 15

    def __init__(self) -> None:
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.fit_calls = 0

    def samples_from_chart_log(self, entries, *, tz, archive_snapshots=None):
        return list(entries)

    def fit(self, samples, *, primary, sensors, tz, now, **_kw):
        self.fit_calls += 1
        self.started.set()
        assert self.release.wait(10), "test never released the fit"
        return {"primary": primary, "sensors": list(sensors)}

    def to_dict(self, model):
        return {
            "schema_version": 1,
            "fitted_at": "2026-11-06T03:15:00+00:00",
            "primary": model["primary"],
            "sensors": model["sensors"],
            "confidence": "ok",
            "fit": {"n_anchors": 1, "span_days": 5.0, "mae_by_lead": {"4h": 0.7}},
        }


def _install(monkeypatch) -> _FakeEngine:
    engine = _FakeEngine()
    monkeypatch.setattr(v4_shadow, "_predictor", lambda: engine)
    return engine


async def test_nightly_timer_fits_the_zone_and_unload_leaves_nothing(
    hass: HomeAssistant, tmp_path, monkeypatch
) -> None:
    engine = _install(monkeypatch)
    entry = await _setup_zone(hass, tmp_path)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.learning._state.v4_model is None

    # The real 03:15 local timer fires the fit through HA's own scheduler.
    now = dt_util.now()
    fire = now.replace(hour=V4_FIT_HOUR, minute=V4_FIT_MINUTE, second=0, microsecond=0)
    if fire <= now:
        fire += timedelta(days=1)
    async_fire_time_changed(hass, fire + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert engine.fit_calls == 1
    assert coordinator.learning._state.v4_model["confidence"] == "ok"
    assert coordinator._v4_fit_running is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    # verify_cleanup (autouse): no lingering timer, task or thread


async def test_unload_during_a_running_fit_drops_its_result_and_cancels_the_task(
    hass: HomeAssistant, tmp_path, monkeypatch
) -> None:
    engine = _install(monkeypatch)
    entry = await _setup_zone(hass, tmp_path)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    engine.release.clear()  # the next fit blocks in the executor
    assert coordinator._v4_start_fit("nightly") is True
    task = coordinator._v4_fit_task
    assert task is not None and task in entry._background_tasks  # HA cancels it on unload instead of waiting a minute
    await hass.async_add_executor_job(engine.started.wait, 5)
    assert coordinator._v4_start_fit("nightly") is False  # never two at once

    assert await hass.config_entries.async_unload(entry.entry_id)
    engine.release.set()  # the executor thread now finishes; its result has nowhere to go
    await hass.async_block_till_done()
    assert task.done()
    assert coordinator.learning._state.v4_model is None
    assert engine.fit_calls == 1


async def test_prediction_accuracy_needs_a_login_and_serves_a_logged_in_client(
    hass: HomeAssistant, tmp_path, hass_client_no_auth, hass_client, monkeypatch
) -> None:
    _install(monkeypatch)
    entry = await _setup_zone(hass, tmp_path)

    anonymous = await hass_client_no_auth()
    resp = await anonymous.get("/api/climate_advisor/prediction_accuracy")
    assert resp.status == 401

    client = await hass_client()
    resp = await client.get(
        "/api/climate_advisor/prediction_accuracy", params={"zone": entry.entry_id, "range": "6h", "lead": "1h"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["zone"] == entry.entry_id and body["range"] == "6h" and body["lead"] == "1h"
    assert body["points"] == [] and body["model"]["confidence"] == "none"  # a fresh zone: an empty state, not an error
    assert body["sensors"][0]["id"] == "_ca"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
