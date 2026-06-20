#!/usr/bin/env python3
"""Show prediction engine status from the Climate Advisor learning DB.

Reads climate_advisor_learning.json directly from HA via SSH (same SSH config
as tools/learning_db.py).  No HA_URL or HA_TOKEN required.

Usage:
    python tools/engine_status.py
    python tools/engine_status.py --json    # raw JSON output
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ha_logs import load_config  # noqa: E402
from learning_db import fetch_learning_db  # noqa: E402

LEARNING_DB_PATH = "/config/climate_advisor_learning.json"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _pad(text: str, width: int) -> str:
    return str(text).ljust(width)


def _fmt_value(value: float | None, unit: str, decimals: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{decimals}f}{unit}"


def _conf_str(conf: str | None) -> str:
    if not conf or conf == "none":
        return "none"
    return conf


# ---------------------------------------------------------------------------
# Build engine status from thermal_model_cache
# ---------------------------------------------------------------------------


def build_engine_status(cache: dict) -> dict:
    """Derive engine status dict from a thermal_model_cache dict.

    Mirrors the shape returned by learning.get_engine_status() so that
    this CLI tool and the REST API are consistent.  If the cache does not
    yet have get_engine_status() fields (pre-v0.3.46 learning DB), the
    tool falls back to inferring 'active' from whether the value is non-None.
    """

    def _engine(key: str, conf_key: str | None) -> dict:
        value = cache.get(key)
        active = value is not None
        confidence = cache.get(conf_key) if conf_key else None
        return {
            "active": active,
            "value": value,
            "confidence": confidence,
        }

    k_passive = _engine("k_passive", "confidence_k_passive")
    k_solar = _engine("k_solar", None)
    solar_phase = _engine("solar_phase_offset_h", None)
    k_vent_window = _engine("k_vent_window", None)

    # HVAC engines (heat + cool)
    k_heat = cache.get("k_active_heat")
    k_cool = cache.get("k_active_cool")
    hvac_active = k_heat is not None or k_cool is not None
    k_active_hvac = {
        "active": hvac_active,
        "k_active_heat": k_heat,
        "k_active_cool": k_cool,
        "confidence": cache.get("confidence_k_hvac"),
    }

    # ODE version: v3 if any extended params are present
    has_v3 = any(cache.get(k) is not None for k in ("k_solar", "k_vent", "k_vent_window", "solar_phase_offset_h"))
    ode_version = "v3" if has_v3 else "basic"

    # Physics eligible: k_passive present and confidence > "none"
    conf_passive = cache.get("confidence_k_passive", cache.get("confidence", "none"))
    physics_eligible = k_passive["active"] and conf_passive not in (None, "none", "?")
    if physics_eligible:
        eligible_reason = "k_passive + confidence sufficient"
    elif not k_passive["active"]:
        eligible_reason = "k_passive not yet learned"
    else:
        eligible_reason = f"confidence too low ({conf_passive})"

    return {
        "k_passive": k_passive,
        "k_solar": k_solar,
        "solar_phase_offset_h": solar_phase,
        "k_vent_window": k_vent_window,
        "k_active_hvac": k_active_hvac,
        "ode_version": ode_version,
        "physics_eligible": physics_eligible,
        "physics_eligible_reason": eligible_reason,
    }


# ---------------------------------------------------------------------------
# Print table
# ---------------------------------------------------------------------------

COL_NAME = 22
COL_VALUE = 16
COL_CONF = 12


def print_engine_status(status: dict) -> None:
    today_str = date.today().isoformat()
    print(f"Prediction Engine Status (as of {today_str})")
    print("=" * 60)

    def _row(label: str, info: dict, unit: str = "", value_override: str | None = None) -> None:
        if not info.get("active"):
            print(f"{_pad(label + ':', COL_NAME)} (not active)")
            return
        val = value_override if value_override is not None else _fmt_value(info.get("value"), unit)
        conf = _conf_str(info.get("confidence"))
        conf_col = f"{conf} confidence" if conf and conf != "none" else ""
        detail = f" | {conf_col}" if conf_col else ""
        print(f"{_pad(label + ':', COL_NAME)} {_pad(val, COL_VALUE)}{detail}")

    _row("k_passive", status["k_passive"], " hr⁻¹")
    _row("k_solar", status["k_solar"], " °F/hr")
    _row("solar_phase_offset", status["solar_phase_offset_h"], "h")
    _row("k_vent_window", status["k_vent_window"], " hr⁻¹")

    hvac = status["k_active_hvac"]
    if hvac.get("active"):
        heat = _fmt_value(hvac.get("k_active_heat"), " °F/hr")
        cool = _fmt_value(hvac.get("k_active_cool"), " °F/hr")
        print(f"{'k_active (HVAC):':{COL_NAME}} heat={heat} cool={cool}")
    else:
        print(f"{'k_active (HVAC):':{COL_NAME}} (not active)")

    ode_ver = status.get("ode_version", "unknown")
    eligible = "YES" if status.get("physics_eligible") else "NO"
    reason = status.get("physics_eligible_reason", "")
    reason_str = f"  ({reason})" if reason else ""
    print(f"\n{'Physics ODE:':{COL_NAME}} {ode_ver}, eligible: {eligible}{reason_str}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Climate Advisor prediction engine status")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw engine status JSON instead of formatted table",
    )
    args = parser.parse_args()

    config = load_config()
    print(f"Reading {LEARNING_DB_PATH} from {config['HA_HOST']} ...\n")
    db = fetch_learning_db(config)

    cache = db.get("thermal_model_cache")
    if not isinstance(cache, dict):
        print("(no thermal_model_cache found in learning DB — thermal model not yet initialized)")
        sys.exit(0)

    status = build_engine_status(cache)

    if args.json:
        print(json.dumps(status, indent=2, default=str))
    else:
        print_engine_status(status)


if __name__ == "__main__":
    main()
