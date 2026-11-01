"""One incident, one record (Issue #1144 follow-up).

Occupant/operator story: the same "you changed the thermostat right after Climate Advisor did" incident used to be
logged again on every coordinator refresh while it stayed in the 35-minute scan window, so the Activity Report and
the AI Investigator counted one event several times. The coordinator's own emitter now skips an incident whose
class and id are already in the event log.
"""

from __future__ import annotations

import importlib
import types
from unittest.mock import MagicMock


def _coordinator():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord._event_log = []
    coord.automation_engine = None
    coord._resolve_active_comfort_band = MagicMock(return_value=(68.0, 76.0))

    def _emit_event(event_type, payload):
        coord._event_log.append({"type": event_type, **payload})

    coord._emit_event = _emit_event
    coord._emit_incident = types.MethodType(mod.ClimateAdvisorCoordinator._emit_incident, coord)
    return coord


def test_same_class_and_id_is_recorded_once():
    coord = _coordinator()
    for _ in range(3):
        coord._emit_incident("rapid_override_after_automation", "2026-08-22T15:00:58+00:00", extra={"gap_seconds": 53})
    assert len([e for e in coord._event_log if e["type"] == "incident_detected"]) == 1


def test_a_different_override_is_a_different_incident():
    coord = _coordinator()
    coord._emit_incident("rapid_override_after_automation", "2026-08-22T15:00:58+00:00")
    coord._emit_incident("rapid_override_after_automation", "2026-08-22T16:30:10+00:00")
    coord._emit_incident("occupancy_transition", "2026-08-22T15:00:58+00:00")
    assert len(coord._event_log) == 3
