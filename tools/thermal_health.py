#!/usr/bin/env python3
"""Thermal learning health diagnostic tool for Climate Advisor.

Fetches the compliance sensor from Home Assistant and prints a per-obs-type
breakdown of thermal observation attempts, commits, and rejections.

Usage:
    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=<your-long-lived-access-token>
    python3 tools/thermal_health.py
    python3 tools/thermal_health.py --entry-id <zone entry_id>   # multi-zone installs (required there)

    # Or create .env at the repo root (template: tools/.env.example):
    # HA_URL=http://homeassistant.local:8123
    # HA_TOKEN=<your-long-lived-access-token>

Exit codes:
    0 = report printed successfully
    1 = configuration or connection error
    2 = integration data not available (sensor missing or attribute absent)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _reject_labels import REJECT_LABELS  # noqa: E402
from _zone_storage import ZoneSelectionRequired, format_zone_choices, resolve_api_zone, with_zone  # noqa: E402


def _load_dotenv() -> None:
    """Load HA_URL and HA_TOKEN from .env file if not already set in environment."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k in ("HA_URL", "HA_TOKEN") and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")


SENSOR_ENTITY_ID = "sensor.climate_advisor_comfort_score"
OBS_TYPES = [
    "passive_decay",
    "vent_window_decay",
    "vent_fan_decay",
    "solar_gain",
    "hvac_heat",
    "hvac_cool",
]

COL_OBS = 20
COL_ATT = 10
COL_COM = 10
COL_REJ = 12
COL_LAST = 30

DIVIDER = "-" * (COL_OBS + COL_ATT + COL_COM + COL_REJ + COL_LAST + 4)
HEADER_LINE = "=" * (COL_OBS + COL_ATT + COL_COM + COL_REJ + COL_LAST + 4)


def _pad(text: str, width: int) -> str:
    return str(text).ljust(width)


def _zone_sensor_template(entry_id: str) -> str:
    """HA template listing the zone's Climate Advisor sensor that carries ``thermal_learning_health``."""
    return (
        "{% for e in integration_entities('climate_advisor') %}"
        f"{{% if e.startswith('sensor.') and config_entry_id(e) == '{entry_id}'"
        " and state_attr(e, 'thermal_learning_health') is not none %}{{ e }} {% endif %}"
        "{% endfor %}"
    )


def _resolve_sensor_entity(ha_url: str, ha_token: str, entry_id: str | None) -> str:
    """The zone's comfort-score sensor entity id (Issue #1418).

    Each zone has its own sensor (unique id ``<entry_id>_comfort_score``), so a fixed entity id reads the first
    zone's. With no ``entry_id`` (single-zone install) the historical default is kept.
    """
    if not entry_id:
        return SENSOR_ENTITY_ID
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entry_id):
        print(f"Error: --entry-id {entry_id!r} is not a config entry id.")
        sys.exit(1)
    body = json.dumps({"template": _zone_sensor_template(entry_id)}).encode("utf-8")
    req = urllib.request.Request(
        f"{ha_url}/api/template",
        data=body,
        headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            found = resp.read().decode("utf-8").split()
    except (urllib.error.URLError, OSError) as exc:
        print(f"Error: could not look up zone {entry_id}'s sensor: {exc}")
        sys.exit(1)
    if not found:
        print(f"Error: no Climate Advisor sensor with thermal_learning_health found for zone {entry_id}.")
        sys.exit(2)
    return found[0]


def _fetch_sensor(ha_url: str, ha_token: str, entity_id: str = SENSOR_ENTITY_ID) -> dict:
    """Fetch the compliance sensor state from HA REST API."""
    url = f"{ha_url}/api/states/{entity_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("Error: HA_TOKEN is invalid or expired (HTTP 401).")
        elif exc.code == 404:
            print(f"Error: Sensor '{SENSOR_ENTITY_ID}' not found (HTTP 404).")
            print("  Is the Climate Advisor integration installed and loaded?")
        else:
            print(f"Error: HTTP {exc.code} fetching sensor state.")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Error: Could not connect to Home Assistant: {exc.reason}")
        print(f"  URL: {url}")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Error: Unexpected non-JSON response from Home Assistant.")
        sys.exit(1)

    if not isinstance(data, dict):
        print("Error: Sensor response has unexpected structure.")
        sys.exit(1)

    return data


DEBUG_STATE_URL_PATH = "/api/climate_advisor/automation_state"

COL_PL_TYPE = 20
COL_PL_STATUS = 12
COL_PL_ELAPSED = 11
COL_PL_SAMPLES = 9
COL_PL_LAST = 11
COL_PL_INDOOR = 17
COL_PL_OUTDOOR = 11
COL_PL_DELTA = 7


def _fetch_debug_state(ha_url: str, ha_token: str, entry_id: str | None = None) -> dict | None:
    """Fetch the debug-state API for ``entry_id``'s zone and return the parsed JSON dict, or None on any failure."""
    url = with_zone(f"{ha_url}{DEBUG_STATE_URL_PATH}", entry_id)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError, urllib.error.URLError, OSError:
        print("Warning: could not fetch live pipeline state -- skipping Current Observations section")
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Warning: could not fetch live pipeline state -- skipping Current Observations section")
        return None

    if not isinstance(data, dict) or "thermal_pipeline" not in data:
        print("Warning: could not fetch live pipeline state -- skipping Current Observations section")
        return None

    return data


def _fmt_optional(value: float | None, suffix: str, decimals: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f} {suffix}"


def _print_pipeline_section(pipeline: dict) -> None:
    """Print the Current Observations section from a thermal_pipeline dict."""
    pending = pipeline.get("pending") or []
    rejection_counts = pipeline.get("rejection_log_counts") or {}

    print("Current Observations")
    print("-" * 20)

    header = (
        _pad("obs_type", COL_PL_TYPE)
        + _pad("status", COL_PL_STATUS)
        + _pad("elapsed", COL_PL_ELAPSED)
        + _pad("samples", COL_PL_SAMPLES)
        + _pad("last_smp", COL_PL_LAST)
        + _pad("indoor", COL_PL_INDOOR)
        + _pad("outdoor", COL_PL_OUTDOOR)
        + "delta"
    )
    print(header)

    if not pending:
        print("  (no active observations)")
    else:
        for obs in pending:
            obs_type = obs.get("obs_type", "?")
            status = obs.get("status", "?")
            elapsed = _fmt_optional(obs.get("elapsed_minutes"), "min")
            samples = str(obs.get("sample_count", "-"))
            last_smp = _fmt_optional(obs.get("last_sample_age_minutes"), "min")
            indoor_range = obs.get("indoor_range_f")
            if indoor_range and len(indoor_range) == 2:
                indoor = f"{indoor_range[0]:.1f}-{indoor_range[1]:.1f} F"
            else:
                indoor = "-"
            outdoor = _fmt_optional(obs.get("outdoor_f"), "F")
            delta = _fmt_optional(obs.get("indoor_delta_f"), "F", decimals=2)

            row = (
                _pad(obs_type, COL_PL_TYPE)
                + _pad(status, COL_PL_STATUS)
                + _pad(elapsed, COL_PL_ELAPSED)
                + _pad(samples, COL_PL_SAMPLES)
                + _pad(last_smp, COL_PL_LAST)
                + _pad(indoor, COL_PL_INDOOR)
                + _pad(outdoor, COL_PL_OUTDOOR)
                + delta
            )
            print(row)

    if rejection_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(rejection_counts.items()))
        print(f"\n(Rejection log entries: {parts})")

    print()


def _print_report(health: dict) -> None:
    """Print the formatted health report table."""
    print()
    print("Thermal Learning Health Report")
    print(HEADER_LINE)
    header = (
        _pad("obs_type", COL_OBS)
        + _pad("attempts", COL_ATT)
        + _pad("committed", COL_COM)
        + _pad("rejections", COL_REJ)
        + "last rejection"
    )
    print(header)
    print(DIVIDER)

    for obs_type in OBS_TYPES:
        h = health.get(obs_type) or {}
        attempts = h.get("attempts", 0)
        committed = h.get("committed", 0)
        rejections = h.get("rejections") or {}
        total_rej = sum(rejections.values()) if isinstance(rejections, dict) else 0
        last_rejection = h.get("last_rejection") or {}
        last_reason = last_rejection.get("reason_code", "-") if isinstance(last_rejection, dict) else "-"
        if not last_reason:
            last_reason = "-"

        row = (
            _pad(obs_type, COL_OBS)
            + _pad(attempts, COL_ATT)
            + _pad(committed, COL_COM)
            + _pad(total_rej, COL_REJ)
            + last_reason
        )
        print(row)

    print()

    # Rejection breakdown per type (only for types with rejections)
    any_rejections = False
    for obs_type in OBS_TYPES:
        h = health.get(obs_type) or {}
        rejections = h.get("rejections") or {}
        if isinstance(rejections, dict) and any(rejections.values()):
            if not any_rejections:
                print("Rejection breakdown:")
                any_rejections = True
            counts = sorted(rejections.items(), key=lambda x: -x[1])
            reasons_str = ", ".join(
                f"{code}: {count}" + (f" ({REJECT_LABELS[code]})" if code in REJECT_LABELS else "")
                for code, count in counts
            )
            print(f"  {obs_type}: {reasons_str}")
            last = h.get("last_rejection")
            if isinstance(last, dict):
                # Issues #1512/#1516: the numbers behind the most recent heating/cooling run rejection.
                bits = [
                    f"{key}={last[key]}"
                    for key in ("signal_f", "minutes", "k_passive_source")
                    if last.get(key) is not None
                ]
                if bits:
                    print(f"    last rejection: {', '.join(bits)}")

    if any_rejections:
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Climate Advisor thermal learning health report")
    parser.add_argument(
        "--entry-id",
        default=None,
        help="Zone config entry_id (required when several zones are loaded; listed when omitted there).",
    )
    args = parser.parse_args(argv)
    _load_dotenv()
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")

    if not ha_url or not ha_token:
        print("Error: HA_URL and HA_TOKEN environment variables are required.")
        print()
        print("  export HA_URL=http://homeassistant.local:8123")
        print("  export HA_TOKEN=<your-long-lived-access-token>")
        sys.exit(1)

    # Issue #1418: with several zones, an unscoped report silently describes the first zone.
    try:
        entry_id = resolve_api_zone(ha_url, ha_token, args.entry_id)
    except ZoneSelectionRequired as exc:
        print("Error: several Climate Advisor zones are loaded — pass --entry-id, one of:")
        print(format_zone_choices(exc.zones))
        sys.exit(1)
    entity_id = _resolve_sensor_entity(ha_url, ha_token, entry_id)
    print(f"Fetching {entity_id} from {ha_url} ...")
    sensor_data = _fetch_sensor(ha_url, ha_token, entity_id)

    attributes = sensor_data.get("attributes")
    if not isinstance(attributes, dict):
        print("Error: Sensor response missing 'attributes' field.")
        sys.exit(2)

    health = attributes.get("thermal_learning_health")
    if health is None:
        print()
        print("thermal_learning_health not found in sensor attributes. Is the integration running v0.3.30+?")
        print()
        print("Available attribute keys:")
        for key in sorted(attributes.keys()):
            print(f"  {key}")
        sys.exit(2)

    if not isinstance(health, dict):
        print("Error: thermal_learning_health attribute has unexpected type.")
        sys.exit(2)

    _print_report(health)

    debug_data = _fetch_debug_state(ha_url, ha_token, entry_id)
    if debug_data is not None:
        _print_pipeline_section(debug_data["thermal_pipeline"])


if __name__ == "__main__":
    main()
