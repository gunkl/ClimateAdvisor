"""Diagnostic snapshot builder for Climate Advisor (Issue #968).

Single source of truth for "what does the configured HVAC/fan entity actually
report right now, and what CA version is running" — both the investigator
report footer (`ai_skills_investigator.py`) and the "Download Logs"/"Download
Full Logs" export (`api.py`) render this same data, one as text and one as
JSON, rather than each independently re-deriving it (the DRY concern this
module exists to close, mirroring the `fan_status.py`/`fan_mode_resolver.py`
"tiny, dependency-free utility module" precedent already used in this
codebase).

Confirmed no new data-exposure concern: `entity_id` is already printed in
every existing investigator report (`ai_skills_context.py`'s "=== HVAC
ENTITY ===" section), and entity attribute values (temperature, hvac_mode,
fan_mode, hvac_action) carry no PII by nature.
"""

from __future__ import annotations

from typing import Any

from .const import VERSION


def build_diagnostic_snapshot(hass: Any, coordinator: Any) -> dict[str, Any]:
    """Return version + live entity-attribute snapshot for diagnostics/reports."""
    config = getattr(coordinator, "config", None) or {}
    snapshot: dict[str, Any] = {
        "version": VERSION,
        "config_summary": {
            "climate_entity": config.get("climate_entity"),
            "fan_mode_archetype": config.get("fan_mode"),
            "fan_entity": config.get("fan_entity"),
            "fan_state_entity": config.get("fan_state_entity"),
        },
    }

    climate_entity_id = config.get("climate_entity")
    climate_state = hass.states.get(climate_entity_id) if climate_entity_id else None
    snapshot["climate_entity"] = (
        {
            "entity_id": climate_entity_id,
            "state": climate_state.state,
            "attributes": dict(climate_state.attributes),
        }
        if climate_state is not None
        else {"entity_id": climate_entity_id, "state": None, "attributes": None}
    )

    fan_entity_id = config.get("fan_entity") or config.get("fan_state_entity")
    fan_state = hass.states.get(fan_entity_id) if fan_entity_id else None
    snapshot["fan_entity"] = (
        {
            "entity_id": fan_entity_id,
            "state": fan_state.state,
            "attributes": dict(fan_state.attributes),
        }
        if fan_state is not None
        else None
    )

    return snapshot


def render_diagnostic_snapshot_text(snapshot: dict[str, Any]) -> str:
    """Render `build_diagnostic_snapshot()`'s output as the investigator report footer."""
    lines = ["=== DIAGNOSTIC SNAPSHOT ===", f"  climate_advisor_version: {snapshot.get('version')}"]

    cfg = snapshot.get("config_summary") or {}
    lines.append(
        "  config: climate_entity={} fan_mode={} fan_entity={} fan_state_entity={}".format(
            cfg.get("climate_entity"),
            cfg.get("fan_mode_archetype"),
            cfg.get("fan_entity"),
            cfg.get("fan_state_entity"),
        )
    )

    climate = snapshot.get("climate_entity") or {}
    lines.append(f"  climate_entity.state: {climate.get('state')}")
    attrs = climate.get("attributes")
    if attrs:
        for key in sorted(attrs):
            lines.append(f"    {key}: {attrs[key]!r}")
    else:
        lines.append("    (entity unavailable)")

    fan = snapshot.get("fan_entity")
    if fan is not None:
        lines.append(f"  fan_entity.state: {fan.get('state')}")
        fan_attrs = fan.get("attributes") or {}
        for key in sorted(fan_attrs):
            lines.append(f"    {key}: {fan_attrs[key]!r}")

    return "\n".join(lines) + "\n"
