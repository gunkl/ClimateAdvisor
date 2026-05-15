#!/usr/bin/env python3
"""Thermal learning database diagnostic tool for Climate Advisor.

Reads climate_advisor_learning.json directly from HA via SSH and prints
a structured report of the rejection log, committed observations, and
model summary. No HA_URL or HA_TOKEN required.

Usage:
    python3 tools/learning_db.py                # full report
    python3 tools/learning_db.py --rejections   # rejection log only
    python3 tools/learning_db.py --committed    # committed obs only
    python3 tools/learning_db.py --model        # model summary only
    python3 tools/learning_db.py --thermal      # chart_log endpoint observations only
    python3 tools/learning_db.py --last N       # last N rejections per type (default 5)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from ha_logs import load_config, ssh_args, ssh_target  # noqa: E402

LEARNING_DB_PATH = "/config/climate_advisor_learning.json"

OBS_TYPES = [
    "passive_decay",
    "fan_only_decay",
    "ventilated_decay",
    "solar_gain",
    "hvac_heat",
    "hvac_cool",
]

DEBUG_STATE_URL_PATH = "/api/climate_advisor/automation_state"


# ---------------------------------------------------------------------------
# .env loader (same logic as thermal_health.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SSH fetch
# ---------------------------------------------------------------------------


def fetch_learning_db(config: dict[str, str]) -> dict:
    """SSH into HA and read the learning JSON file. Returns parsed dict."""
    cmd = ssh_args(config) + [ssh_target(config), f"cat {LEARNING_DB_PATH}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("ERROR: SSH timed out reading learning DB.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0 and not result.stdout.strip():
        stderr = result.stderr.strip()
        if stderr:
            print(f"SSH error: {stderr}", file=sys.stderr)
        else:
            print(f"ERROR: Could not read {LEARNING_DB_PATH} (file may not exist yet).", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse learning DB JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("ERROR: Learning DB has unexpected structure (expected dict).", file=sys.stderr)
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Optional REST fetch for live pending observations
# ---------------------------------------------------------------------------


def _fetch_live_pipeline(ha_url: str, ha_token: str) -> dict | None:
    """Fetch live pipeline state from the debug API. Returns dict or None."""
    import urllib.error
    import urllib.request

    url = f"{ha_url}{DEBUG_STATE_URL_PATH}"
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
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "thermal_pipeline" not in data:
        return None

    return data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _val(v: Any, suffix: str = "", decimals: int = 3) -> str:
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.{decimals}f}{suffix}"
    return str(v)


def _pad(text: str, width: int) -> str:
    return str(text).ljust(width)


# ---------------------------------------------------------------------------
# Section A: Model Summary
# ---------------------------------------------------------------------------


def _print_model_summary(db: dict) -> None:
    cache = db.get("thermal_model_cache")
    if not isinstance(cache, dict):
        print("Thermal Model Summary")
        print("=====================")
        print("(no thermal_model_cache found in learning DB)")
        print()
        return

    def _model_field(key: str, suffix: str, obs_key: str | None = None) -> str:
        val = cache.get(key)
        if val is None:
            return "None"
        result = f"{val:.4f} {suffix}"
        if obs_key:
            n = cache.get(obs_key)
            if n is not None:
                result += f"  ({n} obs)"
        r2_key = key.replace("k_", "r2_") if key.startswith("k_") else None
        if r2_key and r2_key in cache:
            result += f"  avg_R2={cache[r2_key]:.3f}"
        return result

    conf_passive = cache.get("confidence_k_passive", cache.get("confidence", "?"))
    conf_hvac = cache.get("confidence_k_hvac", cache.get("confidence", "?"))
    last_obs = cache.get("last_observation_date") or cache.get("last_obs") or "none"

    print("Thermal Model Summary")
    print("=====================")
    print(f"k_passive:     {_model_field('k_passive', 'hr^-1', 'n_passive')}")
    print(f"k_active_heat: {_model_field('k_active_heat', 'F/hr', 'n_hvac_heat')}")
    print(f"k_active_cool: {_model_field('k_active_cool', 'F/hr', 'n_hvac_cool')}")
    print(f"k_vent:        {_model_field('k_vent', 'hr^-1', 'n_vent')}")
    print(f"k_vent_window: {_model_field('k_vent_window', 'hr^-1', 'n_vent_window')}")
    print(f"k_solar:       {_model_field('k_solar', 'F/hr', 'n_solar')}")

    def _swing_field(key: str, cnt_key: str) -> str:
        val = cache.get(key)
        cnt = cache.get(cnt_key, 0)
        if val is None:
            return f"None  ({cnt} obs)"
        return f"{val:.2f} F  ({cnt} obs)"

    print(f"swing_heat:    {_swing_field('swing_heat_f', 'observation_count_swing_heat')}")
    print(f"swing_cool:    {_swing_field('swing_cool_f', 'observation_count_swing_cool')}")
    print(f"conf_passive:  {conf_passive}")
    print(f"conf_hvac:     {conf_hvac}")
    print(f"last_obs:      {last_obs}")
    print()


# ---------------------------------------------------------------------------
# Section B: Rejection Log
# ---------------------------------------------------------------------------


def _print_rejection_log(db: dict, last_n: int) -> None:
    rejection_log = db.get("rejection_log")
    if not isinstance(rejection_log, dict):
        print("Rejection Log")
        print("-------------")
        print("(no rejection_log found in learning DB)")
        print()
        return

    print("Rejection Log")
    print("-------------")

    any_printed = False
    for obs_type in OBS_TYPES:
        entries = rejection_log.get(obs_type)
        if not entries:
            print(f"{obs_type}: no rejections")
            continue

        if not isinstance(entries, list):
            print(f"{obs_type}: (unexpected format)")
            continue

        # Stored oldest-first; show newest first
        reversed_entries = list(reversed(entries))
        total = len(reversed_entries)
        shown = reversed_entries[:last_n]

        print(f"{obs_type} ({total} total, showing last {min(last_n, total)}):")
        for entry in shown:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "?")
            reason = entry.get("reason_code", entry.get("reason", "?"))
            n_samples = entry.get("n_samples", entry.get("sample_count", "-"))
            elapsed_raw = entry.get("elapsed_minutes")
            elapsed = f"{elapsed_raw}min" if elapsed_raw is not None else "?min"
            delta_t = entry.get("delta_t_f", entry.get("indoor_delta_f", 0.0))
            r2 = entry.get("r_squared", entry.get("r2"))
            r2_str = f"{r2:.3f}" if isinstance(r2, float) else "None"
            sf_range_raw = entry.get("sf_range")
            sf_range_str = f"{sf_range_raw:.2f}" if isinstance(sf_range_raw, (int, float)) else "n/a"
            indoor_dir = entry.get("indoor_direction", "n/a")
            print(
                f"  {ts}  {str(reason):<16} n={str(n_samples):<3} "
                f"elapsed={elapsed:<7} delta_t={float(delta_t) if delta_t is not None else 0.0:.2f}F  "
                f"r2={r2_str}  sf_range={sf_range_str}  indoor={indoor_dir}"
            )
        any_printed = True

    # Also print any obs_types in the log that aren't in our canonical list
    for obs_type, entries in rejection_log.items():
        if obs_type not in OBS_TYPES and entries:
            reversed_entries = list(reversed(entries)) if isinstance(entries, list) else []
            total = len(reversed_entries)
            shown = reversed_entries[:last_n]
            print(f"{obs_type} [{total} total, showing last {min(last_n, total)}] (unknown type):")
            for entry in shown:
                if isinstance(entry, dict):
                    print(f"  {entry}")
            any_printed = True

    if not any_printed:
        print("(no rejections recorded)")

    print()


# ---------------------------------------------------------------------------
# Section C: Committed Observations
# ---------------------------------------------------------------------------


def _print_committed(db: dict) -> None:
    observations = db.get("thermal_observations")
    if not isinstance(observations, list):
        print("Committed Observations")
        print("----------------------")
        print("(no thermal_observations found in learning DB)")
        print()
        return

    last_10 = list(reversed(observations))[:10]
    total = len(observations)

    print(f"Committed Observations (last 10 of {total})")
    print("-" * 60)

    header = _pad("date", 12) + _pad("obs_type", 20) + _pad("k_passive", 12) + _pad("R2", 7) + _pad("grade", 8) + "n"
    print(header)
    print("-" * 60)

    for obs in last_10:
        if not isinstance(obs, dict):
            continue
        date = obs.get("date", "?")
        obs_type = obs.get("hvac_mode", obs.get("obs_type", "?"))
        k_passive = obs.get("k_passive")
        k_passive_str = f"{k_passive:.4f}" if isinstance(k_passive, float) else "-"
        r2 = obs.get("r_squared_passive", obs.get("r_squared", obs.get("r2")))
        r2_str = f"{r2:.3f}" if isinstance(r2, float) else "-"
        grade = obs.get("confidence_grade", "-")
        n = obs.get("sample_count_post", obs.get("sample_count_pre", obs.get("sample_count", "-")))

        row = (
            _pad(str(date), 12)
            + _pad(str(obs_type), 20)
            + _pad(k_passive_str, 12)
            + _pad(r2_str, 7)
            + _pad(str(grade), 8)
            + str(n)
        )
        print(row)

    print()


# ---------------------------------------------------------------------------
# Section D: Chart-log endpoint observations
# ---------------------------------------------------------------------------


def _print_chart_log_endpoint_obs(db: dict) -> None:
    """Print committed observations that came from the chart_log endpoint estimator."""
    observations = db.get("thermal_observations")
    if not isinstance(observations, list):
        print("Chart-Log Endpoint Observations")
        print("--------------------------------")
        print("(no thermal_observations found in learning DB)")
        print()
        return

    endpoint_obs = [o for o in observations if isinstance(o, dict) and o.get("source") == "chart_log_endpoint"]
    total = len(endpoint_obs)
    # Show most-recent first
    recent = list(reversed(endpoint_obs))[:20]

    print(f"Chart-Log Endpoint Observations (last {min(len(recent), total)} of {total})")
    print("-" * 68)

    header = (
        _pad("date", 12)
        + _pad("hvac_mode", 12)
        + _pad("k", 10)
        + _pad("dt_h", 8)
        + _pad("delta_F", 9)
        + _pad("grade", 8)
        + "ratio"
    )
    print(header)
    print("-" * 68)

    for obs in recent:
        date = obs.get("date", "?")
        hvac_mode = obs.get("hvac_mode", "?")
        k = obs.get("k_passive")
        k_str = f"{k:.4f}" if isinstance(k, float) else "-"
        dt_h = obs.get("elapsed_hours")
        dt_str = f"{dt_h:.1f}" if isinstance(dt_h, float) else "-"
        delta = obs.get("delta_t_f")
        delta_str = f"{delta:.1f}" if isinstance(delta, float) else "-"
        grade = obs.get("confidence_grade", "-")
        ratio = obs.get("ratio")
        ratio_str = f"{ratio:.3f}" if isinstance(ratio, float) else "-"

        row = (
            _pad(str(date), 12)
            + _pad(str(hvac_mode), 12)
            + _pad(k_str, 10)
            + _pad(dt_str, 8)
            + _pad(delta_str, 9)
            + _pad(str(grade), 8)
            + ratio_str
        )
        print(row)

    if total == 0:
        print("  (none yet — backfill fires on next HA restart if not already done)")

    print()


# ---------------------------------------------------------------------------
# Section E: Live pending observations (optional, requires HA_URL + HA_TOKEN)
# ---------------------------------------------------------------------------

COL_PL_TYPE = 20
COL_PL_STATUS = 12
COL_PL_ELAPSED = 11
COL_PL_SAMPLES = 9
COL_PL_LAST = 11
COL_PL_INDOOR = 17
COL_PL_OUTDOOR = 11


def _fmt_optional(value: float | None, suffix: str, decimals: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f} {suffix}"


def _print_live_pipeline(pipeline: dict) -> None:
    pending = pipeline.get("pending") or []
    rejection_counts = pipeline.get("rejection_log_counts") or {}

    print("Current Observations (live)")
    print("-" * 28)

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


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Climate Advisor thermal learning DB diagnostic")
    parser.add_argument("--rejections", action="store_true", help="Show rejection log only")
    parser.add_argument("--committed", action="store_true", help="Show committed observations only")
    parser.add_argument("--model", action="store_true", help="Show model summary only")
    parser.add_argument("--thermal", action="store_true", help="Show chart_log endpoint observations only")
    parser.add_argument("--last", type=int, default=5, metavar="N", help="Last N rejections per type (default 5)")
    args = parser.parse_args()

    # Determine which sections to show
    section_flag = args.rejections or args.committed or args.model or args.thermal
    show_model = args.model or args.thermal or not section_flag
    show_rejections = args.rejections or not section_flag
    show_committed = args.committed or not section_flag
    show_thermal = args.thermal or not section_flag

    config = load_config()

    print(f"Reading {LEARNING_DB_PATH} from {config['HA_HOST']} ...")
    db = fetch_learning_db(config)
    print()

    if show_model:
        _print_model_summary(db)

    if show_rejections:
        _print_rejection_log(db, last_n=args.last)

    if show_committed:
        _print_committed(db)

    if show_thermal:
        _print_chart_log_endpoint_obs(db)

    # Live pending observations via REST API (optional)
    _load_dotenv()
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")

    if ha_url and ha_token:
        live_data = _fetch_live_pipeline(ha_url, ha_token)
        if live_data is not None:
            _print_live_pipeline(live_data["thermal_pipeline"])
        else:
            print("(could not fetch live pending observations — is the integration running?)")
    else:
        print("(set HA_URL and HA_TOKEN in .env to also see live pending observations)")


if __name__ == "__main__":
    main()
