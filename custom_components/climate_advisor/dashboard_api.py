"""Read-only REST views for the new dashboard (Issue #1230 R5).

``GET /api/climate_advisor/settings_extra`` carries what the new dashboard shows that no other endpoint
does: which settings differ from their defaults, the time-of-use cost schedules, whether the GitHub issue
integration is set up, and whether saved settings are waiting for a reload (the shell's banner).

Registration is deliberately NOT done here (R5 does not touch ``api.py`` / ``__init__.py``); at merge
time add ``DASHBOARD_VIEWS`` next to ``API_VIEWS`` where the views are registered
(``for view_cls in (*API_VIEWS, *DASHBOARD_VIEWS):`` in ``__init__.py``).

Nothing here writes anything, and no secret leaves the server: the GitHub token is reduced to
``token_set`` and the repository name is the one the options flow already shows.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from . import const, log_capture
from .api import _ambiguous_zone_payload, _get_coordinator
from .const import CONF_GITHUB_REPO, CONF_GITHUB_TOKEN, CONFIG_METADATA, DOMAIN
from .temperature import FAHRENHEIT, UNIT_SYMBOL, display_delta, display_temp

API_SETTINGS_EXTRA = "/api/climate_advisor/settings_extra"

# Defaults that live in config_flow.py / coordinator.py as literals rather than as a DEFAULT_* constant
# or a CONFIG_METADATA "default". Only values read straight from the options-flow schema are listed;
# a key with no known default simply never gets a "changed" marker.
_LITERAL_DEFAULTS: dict[str, Any] = {
    "learning_enabled": True,
    "adaptive_preheat_enabled": True,
    "adaptive_setback_enabled": True,
    "weather_bias_enabled": True,
    "min_preheat_minutes": 30,
    "max_preheat_minutes": 240,
    "default_preheat_minutes": 120,
    "preheat_safety_margin": 1.3,
    "comfort_mode_switch_min_interval_s": 600,
    "aggressive_savings": False,
    "home_toggle_invert": False,
    "briefing_time": "06:00:00",
}


def default_for(key: str, meta: dict[str, Any]) -> Any:
    """The shipped default for ``key`` (internal units), or None when none is known."""
    if "default" in meta:
        return meta["default"]
    if key in _LITERAL_DEFAULTS:
        return _LITERAL_DEFAULTS[key]
    return getattr(const, "DEFAULT_" + key.upper(), None)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a) == str(b)


def _display(value: Any, meta: dict[str, Any], unit: str) -> tuple[Any, str | None]:
    """Mirror ClimateAdvisorConfigView's display transforms for a default value."""
    transform = meta.get("display_transform")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if transform == "seconds_to_minutes":
            return value // 60, None
        if transform in ("temp_abs", "temp_delta"):
            convert = display_temp if transform == "temp_abs" else display_delta
            return convert(value, unit), UNIT_SYMBOL.get(unit, UNIT_SYMBOL[FAHRENHEIT])
    return value, None


def build_settings_extra(config: dict[str, Any]) -> dict[str, Any]:
    """Pure builder (unit-testable without a coordinator)."""
    unit = config.get("temp_unit", FAHRENHEIT)
    changed: dict[str, Any] = {}
    for key, meta in CONFIG_METADATA.items():
        if meta.get("sensitive") or key == "notify_service":
            continue
        default = default_for(key, meta)
        current = config.get(key)
        if default is None or current is None or isinstance(current, (list, dict)):
            continue
        if not _same(current, default):
            shown, sym = _display(default, meta, unit)
            entry: dict[str, Any] = {"default": shown}
            if sym:
                entry["unit"] = sym
            changed[key] = entry

    schedules = []
    for s in config.get("schedules") or []:
        if not isinstance(s, dict):
            continue
        schedules.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "days": list(s.get("days") or []),
                "start": s.get("start"),
                "end": s.get("end"),
                "cost_tag": s.get("cost_tag"),
            }
        )

    repo = config.get(CONF_GITHUB_REPO) or ""
    return {
        "changed": changed,
        "schedules": schedules,
        "github": {
            "repo": repo,
            "token_set": bool(config.get(CONF_GITHUB_TOKEN)),
            "configured": bool(repo and config.get(CONF_GITHUB_TOKEN)),
        },
    }


def _reload_needed(hass: Any, entry_id: str) -> bool:
    """True while the "reload needed" Repairs issue is open for this zone (saved-but-pending settings)."""
    if not entry_id:
        return False
    try:
        from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

        return ir.async_get(hass).async_get_issue(DOMAIN, f"reload_needed_{entry_id}") is not None
    except Exception:  # noqa: BLE001 - a banner hint must never break the Settings tab
        return False


class ClimateAdvisorSettingsExtraView(HomeAssistantView):
    """GET /api/climate_advisor/settings_extra - changed-from-default, TOU schedules, GitHub status."""

    url = API_SETTINGS_EXTRA
    name = "api:climate_advisor:settings_extra"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)
        with log_capture.zone_scope(coordinator.zone_label):
            payload = build_settings_extra(coordinator.config)
            payload["reload_needed"] = _reload_needed(hass, getattr(coordinator, "_entry_id", ""))
            return self.json(payload)


DASHBOARD_VIEWS = [ClimateAdvisorSettingsExtraView]
