"""The house test fixture (Step 2, Issue #1127). See builder.py for the lifecycle contract."""

from .builder import (
    CA_DOMAIN,
    CLIMATE_ENTITY,
    DEFAULT_START,
    SIM_DOMAIN,
    STARTUP_WINDOW_S,
    WEATHER_ENTITY,
    House,
    build_house,
)
from .calls import Call, CallRecorder
from .fans import WholeHouseFan
from .weather import HouseWeather

__all__ = [
    "CA_DOMAIN",
    "CLIMATE_ENTITY",
    "DEFAULT_START",
    "SIM_DOMAIN",
    "STARTUP_WINDOW_S",
    "WEATHER_ENTITY",
    "Call",
    "CallRecorder",
    "House",
    "HouseWeather",
    "WholeHouseFan",
    "build_house",
]
