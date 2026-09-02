"""ha_stubs — idempotent HA sys.modules stub installer.

This module is the single source of truth for the homeassistant.* mock layer.
Both ``tests/conftest.py`` and the sim_harness runtime call ``install_ha_stubs()``
so the two environments stay in sync automatically.

The function is idempotent: calling it multiple times is safe (each module is
only injected if it is not already present in sys.modules).
"""

from __future__ import annotations

import enum as _enum
import os
import sys
import uuid
from typing import Any
from unittest.mock import MagicMock


def _make_mock_module(name: str) -> MagicMock:
    """Create a MagicMock that works as a module for 'from X import Y' statements."""
    mod = MagicMock()
    mod.__name__ = name
    mod.__path__ = []
    mod.__file__ = None
    mod.__spec__ = None
    mod.__loader__ = None
    mod.__package__ = name
    return mod


_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.weather",
    "homeassistant.components.climate",
    "homeassistant.data_entry_flow",
    "homeassistant.exceptions",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.components.http",
    "homeassistant.components.repairs",
    "homeassistant.components.diagnostics",
    # Issue #796: async_setup_entry()/async_unload_entry() in __init__.py do
    # `from homeassistant.components.frontend import async_register_built_in_panel`
    # (and async_remove_panel). Confirmed by direct reproduction: without this
    # module registered, that import raises ModuleNotFoundError — Python's
    # import machinery looks for "frontend" under
    # sys.modules["homeassistant.components"].__path__, which is `[]` (a mock
    # module, not a real package), and fails before ever reaching the
    # MagicMock auto-attribute fallback that makes other "from X import Y"
    # statements silently succeed elsewhere in this file. No prior test or
    # harness code exercised async_setup_entry()/async_unload_entry() (the
    # exact gap this build_headless_multi_zone() closes), so this was never
    # hit before.
    "homeassistant.components.frontend",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.config_validation",
    # Issue #519: entity/device registry, for the QuietCool ambient-speed sensor's
    # sibling-entity discovery (coordinator._resolve_fan_remote_speed_sensor()) — the
    # first feature in this codebase needing registry stubs.
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.device_registry",
    "aiohttp",
    "aiohttp.web",
]


# ---------------------------------------------------------------------------
# Real minimal base classes needed so HA-subclassing modules don't hit
# the metaclass conflict (MagicMock instances cannot be base classes).
# ---------------------------------------------------------------------------


class _MockHomeAssistantError(Exception):
    """Minimal stand-in for homeassistant.exceptions.HomeAssistantError.

    Issue #796 Gap 5: production's _resolve_zone_coordinator() (__init__.py)
    raises ServiceValidationError for an unknown/unloaded entry_id. Without a
    real Exception subclass here, ``from homeassistant.exceptions import
    ServiceValidationError`` would resolve to a bare MagicMock attribute —
    ``raise MagicMock(...)`` fails with "exceptions must derive from
    BaseException" before production's own validation logic ever runs, and
    ``pytest.raises(ServiceValidationError)`` in tests couldn't match it
    either. homeassistant.exceptions itself stays an auto-mocked module (see
    _HA_MODULES) — only these two names are realified, mirroring the pattern
    already used for RepairsFlow/DataUpdateCoordinator/etc. below.
    """


class _MockServiceValidationError(_MockHomeAssistantError):
    """Minimal stand-in for homeassistant.exceptions.ServiceValidationError."""


class _MockRepairsFlow:
    """Minimal stand-in for homeassistant.components.repairs.RepairsFlow."""

    hass = None

    def async_show_form(self, *, step_id, data_schema, errors=None):
        result = {"type": "form", "step_id": step_id, "data_schema": data_schema}
        if errors:
            result["errors"] = errors
        return result

    def async_create_entry(self, *, title="", data):
        return {"type": "create_entry", "title": title, "data": data}


class _MockConfirmRepairFlow(_MockRepairsFlow):
    """Minimal stand-in for homeassistant.components.repairs.ConfirmRepairFlow."""


class _MockContext:
    """Minimal stand-in for homeassistant.core.Context (Issue #482).

    Real HA's Context carries an ``id`` (ulid), optional ``parent_id``, and
    optional ``user_id``. Every real HA ``Event`` carries a ``Context`` — this
    stub is attached to the mocked ``homeassistant.core`` module so production
    code (``automation.py``) can do ``from homeassistant.core import Context``
    and construct real, comparable id values in the test/sim environment, the
    same way it would against real HA.
    """

    def __init__(self, user_id: str | None = None, parent_id: str | None = None, id: str | None = None) -> None:  # noqa: A002
        self.id = id or uuid.uuid4().hex
        self.parent_id = parent_id
        self.user_id = user_id


class _MockDataUpdateCoordinator:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.DataUpdateCoordinator."""

    def __init__(self, *args, **kwargs):
        # Real DataUpdateCoordinator.__init__(self, hass, logger, *, name, ...) — first
        # positional arg is hass. Never previously captured here (pre-#474 gap): any
        # coordinator method reading self.hass after full ClimateAdvisorCoordinator(hass,
        # config) construction would hit AttributeError. Existing tests that use this
        # pattern (test_occupancy.py, test_weather_bias.py, test_learning_toggle.py)
        # happened not to exercise a method needing self.hass before this fix.
        self.hass = args[0] if args else kwargs.get("hass")
        self.data = None
        self.last_update_success = False

    async def async_request_refresh(self):
        """Stub for triggering a data refresh."""
        await self.async_config_entry_first_refresh()

    async def async_config_entry_first_refresh(self):
        """Run the first data fetch (Issue #474 — coordinator-level Tier A coverage).

        Real HA's DataUpdateCoordinator calls ``_async_update_data()`` and
        raises ConfigEntryNotReady on failure; the harness only needs the
        success path since scenarios drive a synthetic, always-ready
        environment (real weather/forecast entities are seeded before this
        runs).
        """
        self.data = await self._async_update_data()
        self.last_update_success = True


class _MockCoordinatorEntity:
    """Minimal stand-in for homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator


class _MockSensorEntity:
    """Minimal stand-in for homeassistant.components.sensor.SensorEntity."""


class _MockJsonResponse:
    """Minimal stand-in for the aiohttp.web.Response a real HomeAssistantView.json() returns.

    Exposes ``status`` and ``json_data`` so tests can assert on both without
    round-tripping through a real aiohttp response body.
    """

    def __init__(self, data, status_code: int = 200) -> None:
        self.status = status_code
        self.json_data = data


class _MockHomeAssistantView:
    """Minimal stand-in for homeassistant.components.http.HomeAssistantView.

    Real subclasses in api.py set ``url``/``name``/``requires_auth`` as class
    attributes and call ``self.json(data, status_code=...)`` from their
    ``get``/``post`` handlers — this provides both without pulling in aiohttp.
    """

    requires_auth = True
    cors_allowed = False

    def json(self, result, status_code: int = 200, headers=None):
        return _MockJsonResponse(result, status_code)

    def json_message(self, message, status_code: int = 200, message_code=None, headers=None):
        return _MockJsonResponse({"message": message}, status_code)


class _MockAbortFlow(Exception):
    """Minimal stand-in for homeassistant.data_entry_flow.AbortFlow.

    Real HA raises this from ``_abort_if_unique_id_configured()`` and the
    FlowManager catches it to build the abort FlowResult. Since tests here
    call step handlers directly (bypassing the FlowManager), test drivers
    must catch this themselves and convert it to an abort result — mirroring
    what the real FlowManager does, not the config flow's own dedup logic.
    """

    def __init__(self, reason: str, description_placeholders: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.description_placeholders = description_placeholders


class _MockConfigFlow:
    """Minimal stand-in for homeassistant.config_entries.ConfigFlow.

    Real ``ConfigFlow`` subclasses pass ``domain=DOMAIN`` as a class keyword
    argument (e.g. ``class ClimateAdvisorConfigFlow(ConfigFlow, domain=DOMAIN)``).
    ``object.__init_subclass__`` rejects keyword arguments, so a plain base class
    would raise ``TypeError`` at import time — this swallows them. Realifying this
    base (instead of leaving it a ``MagicMock`` attribute) is what lets the flow
    subclass become a *real* class whose methods can be exercised directly in
    tests (mirrors the SensorEntity/HomeAssistantView realification, Issue #452).
    """

    unique_id: str | None = None

    def __init_subclass__(cls, **kwargs):  # noqa: ANN001, ANN003
        # Consume HA's ``domain=`` (and any future) class kwargs.
        super().__init_subclass__()

    # Flow-result helpers — return simple sentinel dicts mirroring the shape of
    # HA's FlowResult so real step handlers can call them without aiohttp/HA.
    def async_show_form(self, **kwargs):  # noqa: ANN003
        return {"type": "form", **kwargs}

    def async_show_menu(self, **kwargs):  # noqa: ANN003
        return {"type": "menu", **kwargs}

    def async_create_entry(self, **kwargs):  # noqa: ANN003
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):  # noqa: ANN003
        return {"type": "abort", **kwargs}

    # --- unique_id / dedup helpers (Issue #808) ---------------------------
    # Minimal stand-ins for HA's ConfigFlow.async_set_unique_id() /
    # _abort_if_unique_id_configured(). Real HA looks up existing entries via
    # self._async_current_entries(), which resolves through
    # self.hass.config_entries.async_entries(self.handler). Tests configure
    # hass.config_entries.async_entries(...) to return the entries to check
    # against — the entry-matching/dedup decision itself still runs for real.
    async def async_set_unique_id(self, unique_id: str | None = None, *, raise_on_progress: bool = True):
        self.unique_id = unique_id
        return None

    def _async_current_entries(self, include_ignore: bool = True):
        entries_fn = self.hass.config_entries.async_entries
        return list(entries_fn())

    def _abort_if_unique_id_configured(
        self,
        updates: dict | None = None,
        reload_on_update: bool = True,
        *,
        error: str | None = None,
    ) -> None:
        if self.unique_id is None:
            return
        for entry in self._async_current_entries():
            if getattr(entry, "unique_id", None) == self.unique_id:
                raise _MockAbortFlow(error or "already_configured")


class _MockOptionsFlow:
    """Minimal stand-in for homeassistant.config_entries.OptionsFlow.

    Provides the same FlowResult helpers as :class:`_MockConfigFlow`. ``hass`` and
    ``config_entry`` are left as plain instance attributes (set by the flow
    manager in production, set directly by tests here) rather than properties, so
    tests can assign them on a partially-instantiated instance.
    """

    def async_show_form(self, **kwargs):  # noqa: ANN003
        return {"type": "form", **kwargs}

    def async_show_menu(self, **kwargs):  # noqa: ANN003
        return {"type": "menu", **kwargs}

    def async_create_entry(self, **kwargs):  # noqa: ANN003
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):  # noqa: ANN003
        return {"type": "abort", **kwargs}


REDACTED = "**REDACTED**"


def _redact_data(data, to_redact):
    """Real (non-mocked) equivalent of homeassistant.components.diagnostics.async_redact_data.

    Recursively walks dicts/lists and replaces the value of any key present in
    ``to_redact`` with the REDACTED sentinel — same behavior as HA core's helper.
    A bare MagicMock stand-in would not actually redact anything, which would make
    any test asserting redaction a no-op mirror test (see the project's "never
    mirror the logic under test" testing doctrine) — so this is a real
    implementation, not a mock.
    """
    if isinstance(data, dict):
        return {key: REDACTED if key in to_redact else _redact_data(value, to_redact) for key, value in data.items()}
    if isinstance(data, list):
        return [_redact_data(item, to_redact) for item in data]
    return data


def _register_built_in_panel(
    hass: Any,
    component_name: str,
    *,
    sidebar_title: str | None = None,
    sidebar_icon: str | None = None,
    frontend_url_path: str | None = None,
    require_admin: bool = False,
    config: dict | None = None,
    **_kwargs: Any,
) -> None:
    """Real (non-mocked) equivalent of homeassistant.components.frontend.async_register_built_in_panel.

    Tracks registered panels on ``hass.data["_panels"]`` keyed by
    ``frontend_url_path``, so harness assertions (Issue #796's
    ``teardown_cleanup`` assertion type — see multi_zone_assertions.py) can
    verify panel presence/absence across a multi-zone setup/unload sequence.
    A bare MagicMock would silently accept any call and remember nothing,
    making that assertion type unimplementable — same "real implementation,
    not a mock" reasoning as ``_redact_data`` above.
    """
    panels = hass.data.setdefault("_panels", {})
    panels[frontend_url_path] = {
        "component_name": component_name,
        "sidebar_title": sidebar_title,
        "sidebar_icon": sidebar_icon,
        "require_admin": require_admin,
        "config": config,
    }


def _remove_panel(hass: Any, frontend_url_path: str) -> None:
    """Real equivalent of homeassistant.components.frontend.async_remove_panel."""
    hass.data.get("_panels", {}).pop(frontend_url_path, None)


class ConfigEntry:
    """Minimal stand-in for homeassistant.config_entries.ConfigEntry (Issue #796).

    Real ``ConfigEntry`` carries dozens of fields (source, unique_id, state,
    disabled_by, pref_disable_new_entities, ...). ``async_setup_entry()``/
    ``async_unload_entry()`` in ``__init__.py`` only ever read ``entry_id``,
    ``data``, ``title``, ``version``, and ``options`` (options is unused by
    setup/unload today but included for forward compatibility with a future
    options-flow-driven multi-zone scenario). Not subclassed anywhere in
    production, so — unlike ``_MockConfigFlow``/``_MockOptionsFlow`` above —
    this needs no ``__init_subclass__`` kwarg-swallowing; it is instantiated
    directly by harness code, never used as a base class.

    ``version`` defaults to ``config_flow.ClimateAdvisorConfigFlow.VERSION``
    (18 as of Issue #796) so a harness-built entry starts "current" and never
    accidentally trips ``async_migrate_entry()`` (a different function,
    not invoked by ``async_setup_entry()`` and not exercised by this stub).
    """

    def __init__(
        self,
        entry_id: str,
        data: dict | None = None,
        title: str = "Climate Advisor",
        version: int = 18,
        options: dict | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.data = dict(data or {})
        self.title = title
        self.version = version
        self.options = dict(options or {})


class _SensorStateClass(_enum.StrEnum):
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class _SensorDeviceClass(_enum.StrEnum):
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    PRESSURE = "pressure"
    POWER = "power"
    ENERGY = "energy"


class _UnitOfTemperature(_enum.StrEnum):
    FAHRENHEIT = "°F"
    CELSIUS = "°C"
    KELVIN = "K"


class _EntityCategory(_enum.StrEnum):
    """Issue #613: first diagnostic-category entity in this codebase
    (ClimateAdvisorShadowEngineStatusSensor)."""

    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


def install_ha_stubs() -> None:
    """Install homeassistant.* mock modules into sys.modules (idempotent).

    Safe to call multiple times — each module is only injected once.
    Also ensures the project root is on sys.path so
    ``custom_components.climate_advisor`` resolves.
    """
    # Ensure project root on path so custom_components imports work.
    # The project root is two directories above this file:
    #   tools/sim_harness/ha_stubs.py → tools/ → <project root>
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    # Inject all HA mock modules (idempotent guard)
    for mod_name in _HA_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = _make_mock_module(mod_name)

    # Attach real base classes to the already-installed mock modules.
    # These assignments are idempotent — re-assigning the same class is harmless.
    repairs = sys.modules["homeassistant.components.repairs"]
    repairs.RepairsFlow = _MockRepairsFlow
    repairs.ConfirmRepairFlow = _MockConfirmRepairFlow

    duc = sys.modules["homeassistant.helpers.update_coordinator"]
    duc.DataUpdateCoordinator = _MockDataUpdateCoordinator
    duc.CoordinatorEntity = _MockCoordinatorEntity

    # Issue #519: `from homeassistant.helpers import entity_registry as er` resolves via
    # the PARENT mock's attribute, not sys.modules[...] directly, when the parent
    # (homeassistant.helpers) is itself a MagicMock — an auto-mocked attribute access
    # returns a NEW, unrelated MagicMock, not the real registered submodule. Same failure
    # mode already documented below for `homeassistant.config_entries`; same fix: pin the
    # parent's attribute to the actual registered submodule object.
    helpers = sys.modules["homeassistant.helpers"]
    helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
    helpers.device_registry = sys.modules["homeassistant.helpers.device_registry"]
    helpers.entity = sys.modules["homeassistant.helpers.entity"]

    entity_mod = sys.modules["homeassistant.helpers.entity"]
    entity_mod.EntityCategory = _EntityCategory

    core = sys.modules["homeassistant.core"]
    core.Context = _MockContext

    exceptions_mod = sys.modules["homeassistant.exceptions"]
    exceptions_mod.HomeAssistantError = _MockHomeAssistantError
    exceptions_mod.ServiceValidationError = _MockServiceValidationError

    sensor = sys.modules["homeassistant.components.sensor"]
    sensor.SensorEntity = _MockSensorEntity
    sensor.SensorStateClass = _SensorStateClass
    sensor.SensorDeviceClass = _SensorDeviceClass

    http = sys.modules["homeassistant.components.http"]
    http.HomeAssistantView = _MockHomeAssistantView

    diagnostics = sys.modules["homeassistant.components.diagnostics"]
    diagnostics.async_redact_data = _redact_data
    diagnostics.REDACTED = REDACTED

    # Issue #796: real (not MagicMock) panel-tracking functions — see
    # _register_built_in_panel/_remove_panel docstrings for why a MagicMock
    # is insufficient here (the teardown_cleanup assertion type needs to
    # observe actual panel presence/absence).
    frontend = sys.modules["homeassistant.components.frontend"]
    frontend.async_register_built_in_panel = _register_built_in_panel
    frontend.async_remove_panel = _remove_panel

    # Realify the flow base classes so config_flow.py's ConfigFlow/OptionsFlow
    # subclasses become *real* classes (a MagicMock base makes the subclass a
    # MagicMock, forcing mirror-logic tests). See _MockConfigFlow docstring.
    config_entries = sys.modules["homeassistant.config_entries"]
    config_entries.ConfigFlow = _MockConfigFlow
    config_entries.OptionsFlow = _MockOptionsFlow
    # config_entries.ConfigEntry (Issue #796) — only ever used as a type
    # annotation in production (`from __future__ import annotations` makes
    # it a string, never evaluated at runtime), so this is attached for
    # parity/forward-compatibility rather than because anything currently
    # evaluates it at import time.
    config_entries.ConfigEntry = ConfigEntry

    # Issue #808: real AbortFlow exception so _abort_if_unique_id_configured()
    # can raise it and test drivers can catch it, mirroring the real
    # FlowManager's step-exception handling.
    sys.modules["homeassistant.data_entry_flow"].AbortFlow = _MockAbortFlow
    # config_flow.py uses ``from homeassistant import config_entries`` (parent +
    # attribute), which otherwise binds an auto-generated child MagicMock instead
    # of the patched submodule above. Pin the parent attribute to the real
    # sys.modules entry so the realified bases are actually seen.
    sys.modules["homeassistant"].config_entries = config_entries

    const = sys.modules["homeassistant.const"]
    const.UnitOfTemperature = _UnitOfTemperature

    # voluptuous — use real package if available, mock otherwise
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous as _vol_check  # noqa: F401
            import voluptuous.error  # noqa: F401
        except ImportError:
            sys.modules["voluptuous"] = _make_mock_module("voluptuous")
            sys.modules["voluptuous.error"] = _make_mock_module("voluptuous.error")
