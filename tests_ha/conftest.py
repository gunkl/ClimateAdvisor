"""Real-Home-Assistant test tier (Issue #1102 follow-up S1, Step 2 #1127).

These tests run Climate Advisor inside a real Home Assistant core via
pytest-homeassistant-custom-component, so HA's own ``verify_cleanup`` fixture
(autouse) fails a test that leaves a timer, task or thread running.

Kept apart from ``tests/`` on purpose: ``tests/conftest.py`` replaces every
``homeassistant.*`` module with stubs for the whole process, so the two tiers
can never share a pytest run. Run from the repo root with:

    python scripts/test.py ha

(uv dependency group ``test-ha``) and never install that group into an environment that
also runs ``tests/`` (the plugin auto-loads through its entry point).

Loader (design 02-step2-design.md finding F2)
---------------------------------------------
Two integrations must be found under ``custom_components``: Climate Advisor (repo root)
and the dev thermostat sim (``dev_tools/``, deliberately NOT under ``custom_components/``
so deploy/HACS/hassfest never see it). The pytest plugin also ships a ``custom_components``
package, so whichever import happens first pins ``custom_components.__path__``. We therefore
copy the sim into ``<tmp>/custom_components/ca_dev_thermostat_sim`` (a namespace portion, no
``__init__.py``), put the repo root and ``<tmp>`` on ``sys.path``, and import
``custom_components`` HERE, at conftest import time, before any fixture or HA loader runs.
Keep that import above everything else that could touch HA's loader.

``CA_COMPONENT_ROOT`` (optional) points at a directory holding an alternate
``custom_components/climate_advisor`` tree (an old commit extracted by
``scripts/revert_proof.py``). It is placed ahead of the repo root so the overlay wins.
"""

from __future__ import annotations

import atexit
import importlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SIM_SRC = _REPO / "dev_tools" / "ha_test_integrations" / "ca_dev_thermostat_sim"

# Issue #1518: a run killed before exit never runs the atexit below; sweep this user's own stale copies first.
for _old in Path(tempfile.gettempdir()).glob("ca_sim_portion_*"):
    try:
        if _old.is_dir() and not _old.is_symlink() and time.time() - _old.stat().st_mtime > 6 * 3600:
            shutil.rmtree(_old, ignore_errors=True)
    except OSError:
        pass
_sim_portion = tempfile.mkdtemp(prefix="ca_sim_portion_")
shutil.copytree(
    _SIM_SRC,
    Path(_sim_portion) / "custom_components" / "ca_dev_thermostat_sim",
    ignore=shutil.ignore_patterns("__pycache__", "test_*.py"),
)
atexit.register(shutil.rmtree, _sim_portion, True)

_overlay = os.environ.get("CA_COMPONENT_ROOT")
for _p in (str(_REPO), _sim_portion, *([_overlay] if _overlay else [])):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
# Order now: overlay (if any), sim portion, repo root. First import pins the merged namespace.
importlib.import_module("custom_components")

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def snapshot(snapshot):
    """Pin the Home Assistant snapshot extension (``tests_ha/snapshots/``) regardless of plugin load order.

    Both syrupy and pytest-homeassistant-custom-component register a plugin-level ``snapshot`` fixture; whichever
    registers last wins, and that order differs between machines (Issue #1125: on GitHub CI syrupy's plain fixture
    won and looked in ``__snapshots__/``, reporting every snapshot as missing). A conftest fixture always outranks
    plugin fixtures, and re-applying the same extension is a no-op.
    """
    from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension

    return snapshot.use_extension(HomeAssistantSnapshotExtension)


# Determinism stress knob (Issue #1125): ``CA_HA_EXECUTOR_JITTER_MS=40`` delays every thread-pool job by a random
# 0-40 ms of REAL time, the way a loaded CI runner does. The tier must give identical results with and without it;
# a test that only passes on a fast machine is a test that races the executor.
_JITTER_MS = float(os.environ.get("CA_HA_EXECUTOR_JITTER_MS", "0") or 0)
if _JITTER_MS > 0:
    import asyncio
    import functools
    import random
    import time

    _real_sleep = time.sleep
    _real_run_in_executor = asyncio.BaseEventLoop.run_in_executor

    def _delayed(func, *args):
        _real_sleep(random.uniform(0, _JITTER_MS) / 1000)
        return func(*args)

    def _jittered_run_in_executor(self, executor, func, *args):
        return _real_run_in_executor(self, executor, functools.partial(_delayed, func, *args))

    asyncio.BaseEventLoop.run_in_executor = _jittered_run_in_executor


def pytest_addoption(parser):
    from tools.pytest_shard import add_shard_option

    add_shard_option(parser)  # CI slices (--ca-shard K/N, CA_HA_SHARD), shared with the unit tier
    parser.addoption(
        "--ca-order",
        default="",
        help="reverse | shuffle[:SEED] - run the tier in another order (every test must pass in any order)",
    )


def pytest_collection_modifyitems(config, items):
    from tools.pytest_shard import apply_shard

    apply_shard(config, items)  # first, on the collection order, so the slices stay a partition under --ca-order
    order = config.getoption("--ca-order")
    if order == "reverse":
        items.reverse()
    elif order.startswith("shuffle"):
        import random

        random.Random(int(order.partition(":")[2] or 0)).shuffle(items)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load integrations from custom_components/."""
    yield


@pytest.fixture(autouse=True)
def _fixed_ha_timer_jitter(monkeypatch):
    """Home Assistant offsets its time-change listeners and every coordinator's refresh by a RANDOM 0.05-0.5 s
    (``randint(RANDOM_MICROSECOND_MIN, RANDOM_MICROSECOND_MAX)``) so real installs do not fire together. On the
    frozen, step-driven clock that random fraction decides which side of a step boundary a timer lands on, so the
    same commit ordered events differently from run to run (Issue #1125). Pin it to one value."""
    from homeassistant.helpers import event, update_coordinator

    fixed = (event.RANDOM_MICROSECOND_MIN + event.RANDOM_MICROSECOND_MAX) // 2
    monkeypatch.setattr(event, "randint", lambda _a, _b: fixed)
    monkeypatch.setattr(update_coordinator, "randint", lambda _a, _b: fixed)


@pytest.fixture
async def make_house(hass, freezer, tmp_path):
    """Factory for houses; every house built is closed (reverse order) even if the test fails."""
    from tests_ha.house import build_house

    built = []

    async def _make(**kwargs):
        house = await build_house(hass, freezer, tmp_path, **kwargs)
        built.append(house)
        return house

    try:
        yield _make
    finally:
        for house in reversed(built):
            await house.close()


@pytest.fixture
async def make_sim(make_house):
    """Factory for houses whose simulated thermostat runs a given HVAC profile (owner request 2026-11-12).

    ``await make_sim(profile=SimProfile(heat_rate="normal", companion_sensor=True), **build_house_kwargs)``; the
    profile's keys go under any ``sim_config`` passed (which still wins), and ``physics`` sets build_house's own
    ``profile`` ("closed_loop", the default, or "scripted"). See tests_ha/house/sim_profile.py.
    """
    from tests_ha.house.sim_profile import SimProfile

    async def _make(profile: SimProfile | None = None, *, physics: str = "closed_loop", **kwargs):
        sim_config = {**(profile or SimProfile()).to_sim_config(), **(kwargs.pop("sim_config", None) or {})}
        return await make_house(profile=physics, sim_config=sim_config, **kwargs)

    return _make
