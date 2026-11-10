"""The HA stub's ``@callback`` must be the identity, as in real Home Assistant (Issue #1278).

Occupant story: the nightly job runs thousands of generated household days to catch a rare-day
regression before it reaches the house. With ``homeassistant.core`` a MagicMock, ``@callback``
replaced each decorated coordinator method with one shared mock: the sealed-house whole-house-fan
safety tick, the thermal sample tick and the post-grace fan check never ran in simulation, and
every call was recorded on that mock forever (~25 MB per generated day), so the nightly runner ran
out of memory and was killed every night.
"""

from __future__ import annotations

import gc
import inspect
import logging
from unittest import mock

import tools.sim_harness.ha_stubs  # noqa: F401  (installs the stubs via tests/conftest.py)
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator


def test_stub_callback_returns_the_function_unchanged():
    from homeassistant.core import callback

    def f(x):
        return x + 1

    assert callback(f) is f
    assert f(1) == 2


def test_decorated_coordinator_methods_are_real_functions():
    for name in ("_async_whf_safety_tick", "_async_thermal_sample_tick", "_on_post_grace_fan_check"):
        assert inspect.isfunction(getattr(ClimateAdvisorCoordinator, name)), name


def test_no_production_class_attribute_is_a_mock():
    """A stubbed HA decorator must never swallow a production method, whichever decorator it is."""
    for cls in (ClimateAdvisorCoordinator, AutomationEngine):
        swallowed = [n for n, v in vars(cls).items() if isinstance(v, mock.NonCallableMock)]
        assert not swallowed, f"{cls.__name__}: {swallowed}"


def test_generated_days_do_not_accumulate_recorded_mock_calls():
    """Leak guard: before the fix one generated day left ~24,000 ``mock._Call`` objects behind."""
    from tools.sim_harness import replay
    from tools.sim_harness.generator import day_seed_of, run_day

    def calls() -> int:
        gc.collect()
        return sum(1 for o in gc.get_objects() if type(o) is mock._Call)

    # ``_quiet()`` disables logging process-wide; restore it, or every later test on this xdist worker that captures
    # log records (tests/test_log_capture.py) sees none and fails, depending only on how the tests are distributed.
    replay._quiet()
    try:
        run_day(day_seed_of(1278, 0), "coordinator")  # warm-up: imports and one-time caches
        before = calls()
        for i in range(1, 3):
            run_day(day_seed_of(1278, i), "coordinator")
    finally:
        logging.disable(logging.NOTSET)
    assert calls() - before < 1000


def test_generated_days_do_not_keep_earlier_days_alive():
    """Issue #1508: a recorded stub call holds its arguments, and ``er.async_get(hass)`` runs on every chart-log row,
    so each day's FakeHass (and through it the day's coordinator, engine and promise journal) stayed reachable from
    the process-wide ``homeassistant.helpers.entity_registry`` MagicMock: ~550 MB per 250-day nightly seed. Only a few
    dozen ``_Call`` objects per day did it, under the guard above, so this counts whole days kept alive instead."""
    from tools.sim_harness import replay
    from tools.sim_harness.fake_hass import FakeHass
    from tools.sim_harness.generator import day_seed_of, run_day

    def live_hasses() -> int:
        gc.collect()
        return sum(1 for o in gc.get_objects() if type(o) is FakeHass)

    replay._quiet()
    try:
        for i in range(3):
            run_day(day_seed_of(1508, i), "coordinator")
    finally:
        logging.disable(logging.NOTSET)
    # The latest day may still be pinned until the next one starts; no earlier day may be.
    assert live_hasses() <= 1
