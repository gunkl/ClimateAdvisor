"""Issue #1673: every zone's first refresh after a restart renders with the translation catalogs loaded.

Occupant: after a Home Assistant restart on a multi-zone install, the timeline, status card, briefing and the
activity-log reasons must read as sentences, never as raw keys like ``status.promise.comfort_kept``; and the AI
Investigator's log records must not fill up with ``catalogs not loaded`` warnings.

Cause (regression e510baee, #1341): ``i18n.ensure_catalogs`` marked the attempt done BEFORE reading the files.
Home Assistant sets zones up concurrently, each in its own executor thread; the first started the read and every
other zone returned at once and rendered with empty catalogs. The session fixtures preload the catalogs and
FakeHass runs executor jobs synchronously, so no earlier test ever overlapped two loads.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from custom_components.climate_advisor import async_setup, i18n
from tools.sim_harness.fake_hass import FakeHass

# The three keys seen in the 2026-11-21 17:51 restart log, with the parameters their English needs.
_STARTUP_KEYS = {
    "timeline.step.sleep_band.sensor_swap": {
        "t": "10:00 PM",
        "floor": "66",
        "ceiling": "70",
        "sensor": "bedroom sensor",
        "wake": "6:30 AM",
    },
    "timeline.step.wake_band.sensor_swap": {"t": "6:30 AM", "floor": "68", "ceiling": "74", "sensor": "hallway"},
    "status.promise.comfort_kept": {"n": "Tonight"},
}


@pytest.fixture
def slow_first_load(monkeypatch):
    """Empty catalogs and a ``load_catalogs`` that holds inside the read until released (a slow SD card)."""
    original = i18n.load_catalogs
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(10), "test never released the load"
        original(*args, **kwargs)

    i18n.reset_catalogs()
    monkeypatch.setattr(i18n, "load_catalogs", slow)
    try:
        yield started, release, calls
    finally:
        release.set()
        monkeypatch.undo()
        i18n.reset_catalogs()
        i18n.load_catalogs()


def test_a_second_caller_waits_for_a_load_in_progress(slow_first_load):
    """Zone B's ensure_catalogs, called while zone A's read is running, returns only once the catalogs are in."""
    started, release, calls = slow_first_load
    seen_by_b: list[bool] = []

    a = threading.Thread(target=i18n.ensure_catalogs)
    a.start()
    assert started.wait(5)

    def zone_b():
        i18n.ensure_catalogs()
        seen_by_b.append(i18n.catalogs_loaded())

    b = threading.Thread(target=zone_b)
    b.start()
    b.join(timeout=0.3)
    assert b.is_alive(), "zone B returned while zone A was still reading the catalogs"
    release.set()
    a.join(5)
    b.join(5)
    assert seen_by_b == [True]
    assert calls == [1]  # one read, not one per zone


def test_concurrent_zone_setups_all_render_real_text(slow_first_load, caplog):
    """The production shape: zones gathered on the loop, each loading in the executor, then rendering."""
    started, release, _calls = slow_first_load
    i18n.set_strict(False)  # the production path: a missing key renders as the key and logs a warning

    async def zone() -> list[str]:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, i18n.ensure_catalogs)
        return [i18n.render_en(i18n.Msg(key, **params)) for key, params in _STARTUP_KEYS.items()]

    async def restart() -> list[list[str]]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, release.set)  # zone A is still reading while the other zones reach the loader
        return await asyncio.gather(*(zone() for _ in range(4)))

    with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.i18n"):
        rendered = asyncio.run(restart())
    for texts in rendered:
        for key, text in zip(_STARTUP_KEYS, texts, strict=True):
            assert text != key, f"a zone rendered the raw key {key!r}"
    assert "comfort kept" in rendered[-1][2]
    assert not [r for r in caplog.records if "catalogs not loaded" in r.getMessage()]


def test_a_failed_load_is_seen_as_done_by_waiting_callers(monkeypatch):
    """A broken catalog: the waiting zone gets the fallbacks (no second read, no raise), as before."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def boom(*_a, **_k):
        calls.append(1)
        started.set()
        release.wait(10)
        raise i18n.I18nError("catalog is bad")

    i18n.reset_catalogs()
    monkeypatch.setattr(i18n, "load_catalogs", boom)
    try:
        a = threading.Thread(target=i18n.ensure_catalogs)
        a.start()
        assert started.wait(5)
        b = threading.Thread(target=i18n.ensure_catalogs)
        b.start()
        b.join(timeout=0.2)
        assert b.is_alive()
        release.set()
        a.join(5)
        b.join(5)
        assert calls == [1]
        assert not i18n.catalogs_loaded()
        i18n.ensure_catalogs()  # later render paths never retry
        assert calls == [1]
    finally:
        release.set()
        monkeypatch.undo()
        i18n.reset_catalogs()
        i18n.load_catalogs()


def test_async_setup_loads_the_catalogs_before_any_zone(monkeypatch):
    """Home Assistant finishes ``async_setup`` before it sets up any entry, so the catalogs are in first."""
    seen: list = []
    original = FakeHass.async_add_executor_job

    def spy(self, fn, *args):
        seen.append(fn)
        return original(self, fn, *args)

    monkeypatch.setattr(FakeHass, "async_add_executor_job", spy)
    i18n.reset_catalogs()
    try:
        assert asyncio.run(async_setup(FakeHass(), {})) is True
        assert i18n.ensure_catalogs in seen  # through the executor, never a blocking read on the loop
        assert i18n.catalogs_loaded()
        for key, params in _STARTUP_KEYS.items():
            assert i18n.render_en(i18n.Msg(key, **params)) != key
    finally:
        i18n.reset_catalogs()
        i18n.load_catalogs()


def test_no_production_module_drops_the_catalogs():
    """Unload/reload mirror: the catalogs are install-wide and read-only; unloading a zone (even the last one)
    must never forget them (a failed read is never retried, so a reloaded zone would show raw keys). Only tests
    reset them."""
    package = Path(i18n.__file__).parent
    offenders = [
        p.name for p in package.rglob("*.py") if p.name != "i18n.py" and "reset_catalogs" in p.read_text("utf-8")
    ]
    assert offenders == []
