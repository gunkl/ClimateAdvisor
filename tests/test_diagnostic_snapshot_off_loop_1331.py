"""Issue #1331: the control-quality report is built in the executor, never on Home Assistant's event loop.

Occupant view: downloading diagnostics or logs, or running the AI Investigator, froze all of Home Assistant
(automations, state updates, Climate Advisor's own decisions) while the week-long control-quality report was computed.
Each caller below runs on a real event loop whose ``async_add_executor_job`` is a real thread pool; the report builder
must run on a worker thread, while the live, loop-mutated state (the engine's mode-change ring buffer and hold log, the
indoor history, the promise counters) is still read on the loop thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor import diagnostic_snapshot
from custom_components.climate_advisor.const import DOMAIN

_CQ = "custom_components.climate_advisor.diagnostic_snapshot.build_control_quality"


class _Threads:
    def __init__(self) -> None:
        self.seen: dict[str, set[int]] = {}

    def note(self, what: str) -> None:
        self.seen.setdefault(what, set()).add(threading.get_ident())


class _Engine:
    def __init__(self, threads: _Threads) -> None:
        self._t = threads

    @property
    def comfort_mode_changes(self) -> list[dict[str, Any]]:
        self._t.note("comfort_mode_changes")
        return [{"ts": "2026-11-18T10:00:00+00:00", "to_mode": "cool"}]

    @property
    def comfort_holds(self) -> list[dict[str, Any]]:
        self._t.note("comfort_holds")
        return []


class _PromiseStats(dict):
    def __init__(self, threads: _Threads, *a: Any) -> None:
        super().__init__(*a)
        self._t = threads

    def items(self):  # the snapshot walks the counters with a comprehension
        self._t.note("promise_stats")
        return super().items()


class _ChartLog:
    def __init__(self, threads: _Threads) -> None:
        self._t = threads

    def get_entries(self, range_str: str = "24h", before: Any = None) -> list[dict[str, Any]]:
        self._t.note("chart_log.get_entries")
        return []


def _coordinator(threads: _Threads) -> MagicMock:
    coord = MagicMock()
    coord.config = {"climate_entity": "climate.t", "temp_unit": "fahrenheit"}
    coord.zone_label = "Zone 1"
    coord._event_log = []
    coord._indoor_temp_history = [("2026-11-18T10:00:00+00:00", 70.0)]
    coord._outdoor_temp_history = []
    coord._chart_log = _ChartLog(threads)
    coord.automation_engine = _Engine(threads)
    coord._promise_stats = _PromiseStats(threads, {"comfort": {"hits": 1, "last_ts": None}})
    coord.get_debug_state.return_value = {}
    coord.learning.get_compliance_summary.return_value = {}
    coord.thermostat_profile_diagnostics.return_value = None
    return coord


def _hass(coord: MagicMock) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coord}}
    hass.states.get.return_value = None

    def _executor_job(fn, *args):
        return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    hass.async_add_executor_job.side_effect = _executor_job
    return hass


async def _via_diagnostics(hass: MagicMock, coord: MagicMock) -> Any:
    from custom_components.climate_advisor.diagnostics import async_get_diagnostics_payload

    entry = MagicMock(entry_id="entry1", title="Zone 1")
    hass.config_entries.async_entries.return_value = [entry]
    return (await async_get_diagnostics_payload(hass, entry))["thermostat_snapshot"]["control_quality"]


async def _via_investigator(hass: MagicMock, coord: MagicMock) -> Any:
    from custom_components.climate_advisor.ai_skills_investigator import async_build_investigator_context

    registry = MagicMock()
    registry.select.return_value = []
    with patch("custom_components.climate_advisor.ai_skills_context.get_provider_registry", return_value=registry):
        return await async_build_investigator_context(hass, coord)


async def _via_log_download(hass: MagicMock, coord: MagicMock) -> Any:
    from custom_components.climate_advisor.api import ClimateAdvisorEventLogView

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {}
    request.rel_url.query = {}
    resp = await ClimateAdvisorEventLogView().get(request)
    return resp.json_data["diagnostics"]["control_quality"]


CALLERS = {"diagnostics": _via_diagnostics, "investigator": _via_investigator, "log_download": _via_log_download}


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_report_is_built_on_a_worker_thread_and_live_state_read_on_the_loop(caller: str) -> None:
    threads = _Threads()
    coord = _coordinator(threads)
    hass = _hass(coord)
    loop_thread: set[int] = set()

    def _report(**kwargs: Any) -> dict[str, Any]:
        threads.note("build_control_quality")
        assert kwargs["mode_changes"] == [{"ts": "2026-11-18T10:00:00+00:00", "to_mode": "cool"}]
        assert kwargs["indoor_history"] == [("2026-11-18T10:00:00+00:00", 70.0)]
        return {"windows": {"marker": True}}

    async def _run() -> Any:
        loop_thread.add(threading.get_ident())
        return await CALLERS[caller](hass, coord)

    with patch(_CQ, side_effect=_report):
        result = asyncio.run(_run())

    (loop_id,) = loop_thread
    assert threads.seen["build_control_quality"] and loop_id not in threads.seen["build_control_quality"]
    assert loop_id not in threads.seen["chart_log.get_entries"]
    for live in ("comfort_mode_changes", "comfort_holds", "promise_stats"):
        assert threads.seen[live] == {loop_id}, live
    if caller == "investigator":
        assert "=== DIAGNOSTIC SNAPSHOT ===" in result
    else:
        assert result == {"windows": {"marker": True}}


def test_a_failing_report_leaves_the_rest_of_the_snapshot() -> None:
    threads = _Threads()
    coord = _coordinator(threads)
    hass = _hass(coord)
    with patch(_CQ, side_effect=RuntimeError("boom")):
        snap = asyncio.run(diagnostic_snapshot.async_build_diagnostic_snapshot(hass, coord))
    assert snap["control_quality"] is None
    assert snap["climate_entity"]["entity_id"] == "climate.t"
    assert snap["promise_stats"]["comfort"]["hits"] == 1


def test_executor_unavailable_still_returns_the_snapshot() -> None:
    # e.g. during shutdown the executor refuses new work: the download keeps everything but the report.
    threads = _Threads()
    coord = _coordinator(threads)
    hass = _hass(coord)
    hass.async_add_executor_job.side_effect = RuntimeError("executor shut down")
    snap = asyncio.run(diagnostic_snapshot.async_build_diagnostic_snapshot(hass, coord))
    assert snap["control_quality"] is None
    assert snap["version"]


def test_async_and_sync_builders_agree() -> None:
    threads = _Threads()
    coord = _coordinator(threads)
    hass = _hass(coord)
    with patch(_CQ, return_value={"windows": {}}):
        sync_snap = diagnostic_snapshot.build_diagnostic_snapshot(hass, coord)
        async_snap = asyncio.run(diagnostic_snapshot.async_build_diagnostic_snapshot(hass, coord))
    assert async_snap == sync_snap
    assert list(async_snap) == list(sync_snap)  # same key order in the downloaded file
