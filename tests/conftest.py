"""Shared pytest fixtures for Climate Advisor tests."""

from __future__ import annotations

import os
import sys

# Ensure the project root is on sys.path so imports from
# custom_components.climate_advisor resolve correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install homeassistant.* stub modules — single source of truth lives in
# tools/sim_harness/ha_stubs.py so the test suite and headless harness
# stay automatically in sync.
from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

# Now safe to import Climate Advisor modules
import pytest  # noqa: E402

from custom_components.climate_advisor.classifier import ForecastSnapshot  # noqa: E402


@pytest.fixture
def basic_forecast() -> ForecastSnapshot:
    """A typical mid-season ForecastSnapshot with stable trend."""
    return ForecastSnapshot(
        today_high=72.0,
        today_low=55.0,
        tomorrow_high=73.0,
        tomorrow_low=56.0,
        current_outdoor_temp=65.0,
        current_indoor_temp=70.0,
        current_humidity=45.0,
    )
