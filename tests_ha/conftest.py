"""Real-Home-Assistant test tier (Issue #1102 follow-up S1).

These tests run Climate Advisor inside a real Home Assistant core via
pytest-homeassistant-custom-component, so HA's own ``verify_cleanup`` fixture
(autouse) fails a test that leaves a timer, task or thread running.

Kept apart from ``tests/`` on purpose: ``tests/conftest.py`` replaces every
``homeassistant.*`` module with stubs for the whole process, so the two tiers
can never share a pytest run. Run from the repo root with:

    pytest -c tests_ha/pytest.ini --rootdir=tests_ha tests_ha/

and install ``requirements_test_ha.txt`` only into an environment that does
not also run ``tests/`` (the plugin auto-loads through its entry point).
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the repo's custom_components package importable for HA's loader.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load integrations from custom_components/."""
    yield
