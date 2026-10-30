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
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SIM_SRC = _REPO / "dev_tools" / "ha_test_integrations" / "ca_dev_thermostat_sim"

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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load integrations from custom_components/."""
    yield


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
