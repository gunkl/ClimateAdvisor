"""Real-HA test for Issue #1331: the control-quality report is built in HA's executor, not on its event loop.

Occupant story: downloading diagnostics or the Activity log froze all of Home Assistant (automations, state updates,
Climate Advisor's own decisions) while a week of chart history was analysed. Against a real loaded zone and REAL
``hass.async_add_executor_job``, both download paths must build the report on a worker thread and still return a
real report (not ``None``) built from the zone's chart log. No day type or fan mode is involved: the path only reads
the zone's history.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.climate_advisor import diagnostic_snapshot
from custom_components.climate_advisor.api import ClimateAdvisorEventLogView
from custom_components.climate_advisor.diagnostics import async_get_diagnostics_payload

pytestmark = pytest.mark.house


async def _log_download(house) -> dict:
    request = SimpleNamespace(
        app={"hass": house.hass},
        query={},
        rel_url=SimpleNamespace(query={}),
        method="GET",
        path="/api/climate_advisor/event_log",
        remote="t",
    )
    response = await ClimateAdvisorEventLogView().get(request)
    return json.loads(response.text)["diagnostics"]["control_quality"]


async def _diagnostics(house) -> dict:
    payload = await async_get_diagnostics_payload(house.hass, house.ca_entry)
    return payload["thermostat_snapshot"]["control_quality"]


@pytest.mark.parametrize("path", ["log_download", "diagnostics"])
async def test_report_runs_in_the_executor_and_is_returned(make_house, path) -> None:
    house = await make_house()
    await house.advance(minutes=65)  # a few cycles of real chart rows
    assert house.coordinator._chart_log.entry_count > 0

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = diagnostic_snapshot.build_control_quality

    def _recording(**kwargs):
        seen.append(threading.get_ident())
        return real(**kwargs)

    with patch.object(diagnostic_snapshot, "build_control_quality", side_effect=_recording):
        report = await (_log_download(house) if path == "log_download" else _diagnostics(house))

    assert seen and all(t != loop_thread for t in seen), "control-quality report built on the event loop"
    assert isinstance(report, dict) and "windows" in report
