"""Subprocess offload for CPU-bound v4 fitting and night replay (Issue #1722).

Occupant view: on slower hardware (such as a Raspberry Pi 4), a pure-Python v4 fit takes ~60-100 s and
starves Home Assistant's event loop via Python's GIL when run in a thread-pool executor. Running the fit
in a child Python process isolates the GIL on another core and lets unload kill the worker immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import v4_shadow
from tests.v4_fake_predictor import install


async def _sync_executor(fn, *args):
    return fn(*args)


def _sample_rows(now: datetime, *, hours: int = 36) -> list[dict[str, Any]]:
    start = now - timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    for step in range(hours * 2):
        ts = start + timedelta(minutes=30 * step)
        heating = (step % 6) in (0, 1)
        indoor = 68.5 + (0.6 if heating else -0.3)
        rows.append(
            {
                "ts": ts.isoformat(),
                "hvac": "heating" if heating else "idle",
                "fan": False,
                "indoor": indoor,
                "outdoor": 30.0,
                "pred_outdoor": 30.0,
                "setpoint": 69.0,
                "indoor_sensors": {"climate.living_room": indoor, "sensor.bedroom": indoor - 1.0},
            }
        )
    return rows


def test_tz_round_trip() -> None:
    now = datetime(2026, 11, 24, 15, 0, tzinfo=ZoneInfo("America/Chicago"))
    ser = v4_shadow._serialize_tz(ZoneInfo("America/Chicago"), now)
    assert ser == {"key": "America/Chicago"}
    assert getattr(v4_shadow._deserialize_tz(ser), "key", None) == "America/Chicago"

    fixed = timezone(timedelta(hours=-6))
    ser_fixed = v4_shadow._serialize_tz(fixed, now)
    assert ser_fixed == {"offset_s": -21600}
    assert v4_shadow._deserialize_tz(ser_fixed).utcoffset(now) == timedelta(hours=-6)

    assert v4_shadow._deserialize_tz(None) is UTC


def test_make_in_sleep_matches_wrap_and_post_midnight() -> None:
    wrap = v4_shadow._make_in_sleep(("22:00", "06:00"))
    assert wrap is not None
    assert wrap(datetime(2026, 11, 24, 22, 0)) is True
    assert wrap(datetime(2026, 11, 25, 2, 30)) is True
    assert wrap(datetime(2026, 11, 25, 6, 0)) is False
    assert wrap(datetime(2026, 11, 24, 15, 0)) is False

    nowrap = v4_shadow._make_in_sleep(("01:00", "07:00"))
    assert nowrap is not None
    assert nowrap(datetime(2026, 11, 24, 23, 30)) is False
    assert nowrap(datetime(2026, 11, 25, 1, 0)) is True
    assert nowrap(datetime(2026, 11, 25, 6, 59)) is True
    assert nowrap(datetime(2026, 11, 25, 7, 0)) is False

    same = v4_shadow._make_in_sleep(("22:00", "22:00"))
    assert same is not None
    assert same(datetime(2026, 11, 24, 22, 0)) is False
    assert v4_shadow._make_in_sleep(None) is None


def test_async_run_fit_executes_in_real_subprocess() -> None:
    tz = ZoneInfo("America/Chicago")
    now = datetime(2026, 11, 24, 15, 0, tzinfo=tz)
    rows = _sample_rows(now)
    executor_calls: list[Any] = []

    async def recording_executor(fn, *args):
        executor_calls.append(fn)
        return fn(*args)

    def snapshots_fn(_start: datetime, _end: datetime) -> list[dict]:
        return []

    model_dict, seconds, fit_line = asyncio.run(
        v4_shadow.async_run_fit(
            recording_executor,
            chart_entries=rows,
            snapshots_fn=snapshots_fn,
            tz=tz,
            now=now,
            primary="climate.living_room",
            sensors=["climate.living_room", "sensor.bedroom"],
            sleep_window=("22:00", "06:00"),
            train_days=30,
            climate_entity="climate.living_room",
            use_subprocess=True,
        )
    )

    assert isinstance(model_dict, dict)
    assert model_dict.get("schema_version") == 1
    assert seconds >= 0.0
    assert isinstance(fit_line, str)
    # Only snapshots_fn and JSON encode/decode ran in the executor; run_fit itself ran in the child process.
    assert snapshots_fn in executor_calls
    assert not any(getattr(fn, "func", fn) is v4_shadow.run_fit for fn in executor_calls)


def test_async_run_display_fit_and_night_replay_in_real_subprocess() -> None:
    tz = ZoneInfo("America/Chicago")
    now = datetime(2026, 11, 24, 15, 0, tzinfo=tz)
    rows = _sample_rows(now)

    model_dict, _sec, _line = asyncio.run(
        v4_shadow.async_run_fit(
            _sync_executor,
            chart_entries=rows,
            snapshots_fn=None,
            tz=tz,
            now=now,
            primary="climate.living_room",
            sensors=["climate.living_room", "sensor.bedroom"],
            train_days=30,
            use_subprocess=True,
        )
    )

    new_dict, disp_sec, disp_line = asyncio.run(
        v4_shadow.async_run_display_fit(
            _sync_executor,
            chart_entries=rows,
            tz=tz,
            now=now,
            primary="climate.living_room",
            sensors=["climate.living_room", "sensor.bedroom"],
            model_dict=model_dict,
            train_days=30,
            use_subprocess=True,
        )
    )
    assert isinstance(new_dict, dict)
    assert new_dict.get("primary") == model_dict.get("primary")
    assert disp_sec >= 0.0
    assert "v4 display nodes" in disp_line

    record = asyncio.run(
        v4_shadow.async_run_night_replay(
            _sync_executor,
            chart_entries=rows,
            snapshots_fn=None,
            tz=tz,
            now=now,
            primary="climate.living_room",
            sensors=["climate.living_room", "sensor.bedroom"],
            bed=(22, 0),
            wake=(6, 0),
            train_days=30,
            model_fitted_at=str(model_dict.get("fitted_at") or ""),
            use_subprocess=True,
        )
    )
    assert isinstance(record, dict)
    assert "n_nights_replayed" in record


def test_subprocess_cancellation_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[bool] = []
    waited: list[bool] = []
    real_create = asyncio.create_subprocess_exec

    async def slow_subprocess(*_args, **_kwargs):
        proc = await real_create(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        orig_kill = proc.kill
        orig_wait = proc.wait

        def track_kill():
            killed.append(True)
            orig_kill()

        async def track_wait():
            waited.append(True)
            return await orig_wait()

        monkeypatch.setattr(proc, "kill", track_kill)
        monkeypatch.setattr(proc, "wait", track_wait)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_subprocess)

    async def scenario() -> None:
        task = asyncio.create_task(v4_shadow._run_in_subprocess(_sync_executor, "fit", {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert killed == [True]
    assert waited == [True]


def test_subprocess_oserror_falls_back_to_executor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    fallback_called: list[bool] = []

    async def fake_executor(fn, *args):
        if getattr(fn, "func", fn) is v4_shadow.run_fit:
            fallback_called.append(True)
            return ({"schema_version": 1, "confidence": "none"}, 0.1, "fallback")
        return fn(*args)

    tz = ZoneInfo("America/Chicago")
    now = datetime(2026, 11, 24, 15, 0, tzinfo=tz)
    with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.v4_shadow"):
        model_dict, seconds, line = asyncio.run(
            v4_shadow.async_run_fit(
                fake_executor,
                chart_entries=[],
                snapshots_fn=None,
                tz=tz,
                now=now,
                primary="climate.living_room",
                sensors=["climate.living_room"],
                use_subprocess=True,
            )
        )

    assert fallback_called == [True]
    assert line == "fallback"
    assert seconds == 0.1
    assert model_dict["confidence"] == "none"
    assert any("falling back to executor thread" in r.message for r in caplog.records)


def test_fake_predictor_control_uses_executor_not_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    assert v4_shadow._is_real_predictor() is False
