"""Issue #1367: every setpoint Climate Advisor sends is fitted to the thermostat's own min_temp/max_temp.

Occupant outcome: an away heat of 42 F on a thermostat whose minimum is 45 F used to be refused by Home Assistant
(``temp_out_of_range``), so the empty house stayed at the comfort setpoint for the whole absence. Now 45 F is sent.
The limits are in Home Assistant's system unit (the unit the setpoint is sent in), so on a Celsius install the limit
itself is sent, with no unit round trip that could land a hair outside it. End-to-end proof through real Home
Assistant: ``tests_ha/test_setpoint_outside_thermostat_limits_1367.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.temperature import from_fahrenheit


def _engine(attrs: dict | None, unit: str = "fahrenheit") -> AutomationEngine:
    eng = object.__new__(AutomationEngine)
    eng.climate_entity = "climate.t"
    eng.config = {"temp_unit": unit}
    state = None if attrs is None else SimpleNamespace(state="heat", attributes=attrs)
    eng.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: state))
    return eng


def _fit(eng: AutomationEngine, value_f: float) -> tuple[float, float]:
    return eng._fit_to_thermostat_limits(from_fahrenheit(value_f, eng._temp_unit), value_f)


@pytest.mark.parametrize(
    ("value_f", "expected"),
    [
        (42.0, (45.0, 45.0)),  # heat setback below min_temp
        (39.0, (45.0, 45.0)),  # vacation heat (away - 3) below min_temp
        (88.0, (85.0, 85.0)),  # cool setback above max_temp
        (45.0, (45.0, 45.0)),  # exactly the limit: unchanged
        (68.0, (68.0, 68.0)),  # inside: unchanged
    ],
)
def test_fahrenheit_install(value_f, expected):
    assert _fit(_engine({"min_temp": 45, "max_temp": 85}), value_f) == expected


def test_celsius_install_sends_the_limit_itself():
    eng = _engine({"min_temp": 7, "max_temp": 35}, unit="celsius")
    service, internal_f = _fit(eng, 42.0)  # 5.56 C, below the 7 C minimum
    assert service == 7.0, "the limit itself, never a converted value a hair below it"
    assert internal_f == pytest.approx(44.6)
    service, internal_f = _fit(eng, 97.0)  # 36.1 C, above the 35 C maximum
    assert (service, internal_f) == (35.0, pytest.approx(95.0))
    service, internal_f = _fit(eng, 68.0)
    assert service == pytest.approx(20.0) and internal_f == 68.0


@pytest.mark.parametrize(
    "attrs",
    [
        None,  # entity missing
        {},  # no limits advertised
        {"min_temp": None, "max_temp": None},
        {"min_temp": "45", "max_temp": "85"},  # not numeric
        {"min_temp": float("nan"), "max_temp": float("inf")},
        {"min_temp": True, "max_temp": False},
        {"min_temp": 90, "max_temp": 50},  # inconsistent
    ],
)
def test_unusable_limits_leave_the_setpoint_alone(attrs):
    assert _fit(_engine(attrs), 42.0) == (42.0, 42.0)
    assert _fit(_engine(attrs), 99.0) == (99.0, 99.0)


def test_one_limit_only():
    assert _fit(_engine({"min_temp": 45}), 42.0) == (45.0, 45.0)
    assert _fit(_engine({"min_temp": 45}), 99.0) == (99.0, 99.0)
    assert _fit(_engine({"max_temp": 85}), 99.0) == (85.0, 85.0)
