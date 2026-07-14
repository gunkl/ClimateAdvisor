#!/usr/bin/env python3
"""Fetch Home Assistant logs from a remote HAOS instance via SSH or REST API.

Uses `ha core logs` on the remote (reads Docker log files from disk on HAOS).
Reuses the SSH connection config from .deploy.env.

The remote `ha core logs` command defaults to only its last 100 lines when no
`--lines` is given, so every SSH-mode call here explicitly passes `--lines` to
the remote command — a generous raw depth (DEFAULT_RAW_LINES) when filtering
locally with grep/tail, or the user's requested count directly for --full.

SSH mode (default):
    python3 tools/ha_logs.py                        # Last 500 climate_advisor lines
    python3 tools/ha_logs.py --all                   # Last 500 lines of full HA log
    python3 tools/ha_logs.py --lines 200             # Last 200 climate_advisor lines
    python3 tools/ha_logs.py --filter "ERROR"        # Only ERROR lines for climate_advisor
    python3 tools/ha_logs.py --all --filter "ERROR"  # All ERROR lines in HA log
    python3 tools/ha_logs.py --full                  # Dump entire HA log (large!)
    python3 tools/ha_logs.py --thermal               # Thermal-learning lines only (2000 lines)
    python3 tools/ha_logs.py --save                  # Save output to logs/ directory

REST API history mode (--history):
    python3 tools/ha_logs.py --history               # Last 24h logbook (all entries)
    python3 tools/ha_logs.py --history --filter climate_advisor  # Filter logbook text
    python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status
    python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status --hours 48
    python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status,sensor.climate_advisor_day_type
    python3 tools/ha_logs.py --history --start "2026-03-28T18:00:00"
    python3 tools/ha_logs.py --history --start "2026-03-28T18:00:00" --end "2026-03-29T09:00:00"
    python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status --start "2026-03-28T18:00:00"
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".deploy.env"
LOG_DIR = REPO_ROOT / "logs"

# Raw line depth requested from the remote `ha core logs --lines N` call before local
# grep/tail filtering. Must be generous — grep/tail can only find matches within whatever
# the remote command actually returned, and `ha core logs` defaults to just 100 lines
# with no --lines flag. At current DEBUG-level log volume, 100000 lines is needed to
# reach back roughly 3 days (verified live against a production HAOS install); smaller
# values (e.g. 20000) only reached back ~1 day. SSH round-trip at this depth is ~15s.
DEFAULT_RAW_LINES = 100000

# Regex to strip ANSI color codes from ha core logs output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def load_config() -> dict[str, str]:
    """Load deploy configuration from .deploy.env with defaults."""
    config = {
        "HA_HOST": "homeassistant.local",
        "HA_SSH_PORT": "22",
        "HA_SSH_USER": "hassio",
        "HA_SSH_KEY": "",
        "HA_CONFIG_PATH": "/config",
        "HA_API_TOKEN": "",
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    else:
        print("ERROR: .deploy.env not found. Copy .deploy.env.sample and configure it.", file=sys.stderr)
        sys.exit(1)
    return config


def _build_ha_base_url(config: dict[str, str]) -> str:
    """Build the HA base URL. Uses HTTPS for non-local hostnames."""
    host = config["HA_HOST"]
    # Use HTTPS for non-local hosts (not .local, not an IP address)
    is_local = host.endswith(".local") or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or host == "localhost"
    scheme = "http" if is_local else "https"
    return f"{scheme}://{host}:8123"


def _ha_api_request(url: str, token: str) -> object:
    """Make a GET request to the HA REST API. Returns parsed JSON or raises."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("ERROR: HA API authentication failed. Check HA_API_TOKEN in .deploy.env.", file=sys.stderr)
        else:
            print(f"ERROR: HA API returned HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"ERROR: Could not connect to HA at {url}: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _parse_local_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601-like local timestamp and return a UTC-aware datetime.

    Accepts formats: "2026-03-28T18:00:00", "2026-03-28 18:00:00", "2026-03-28T18:00".
    The value is treated as local system time and converted to UTC.
    """
    ts = ts.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            naive = datetime.strptime(ts, fmt)
            # Convert local naive datetime to UTC
            local_tz = datetime.now(UTC).astimezone().tzinfo
            return naive.replace(tzinfo=local_tz).astimezone(UTC)
        except ValueError:
            continue
    print(f"ERROR: Cannot parse timestamp '{ts}'. Use format: YYYY-MM-DDTHH:MM:SS", file=sys.stderr)
    sys.exit(1)


def _format_api_timestamp(dt: datetime) -> str:
    """Format a UTC datetime as an ISO 8601 string for HA REST API URLs."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def fetch_history(
    config: dict[str, str],
    entity_filter: str = "",
    hours_back: int = 24,
    filter_text: str = "",
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> str:
    """Fetch logbook entries from the HA REST API.

    Queries /api/logbook/{timestamp} for the given time window.
    When start_dt is provided, uses it directly (ignoring hours_back).
    Optionally filters by entity ID and/or text match.
    Returns formatted text suitable for printing.
    """
    token = config.get("HA_API_TOKEN", "")
    if not token:
        print(
            "ERROR: HA_API_TOKEN is not set in .deploy.env.\n"
            "Generate a long-lived access token from your HA Profile page and add it.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = _build_ha_base_url(config)

    start_time = start_dt if start_dt is not None else datetime.now(UTC) - timedelta(hours=hours_back)

    timestamp = _format_api_timestamp(start_time)
    url = f"{base_url}/api/logbook/{timestamp}"

    params = []
    if entity_filter:
        params.append(f"entity_id={urllib.request.quote(entity_filter)}")
    if end_dt is not None:
        params.append(f"end_time={urllib.request.quote(_format_api_timestamp(end_dt))}")
    if params:
        url += "?" + "&".join(params)

    data = _ha_api_request(url, token)

    if not isinstance(data, list):
        return "(no logbook entries returned)"

    lines = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when", "")
        name = entry.get("name", "")
        message = entry.get("message", "")
        entity_id = entry.get("entity_id", "")

        line = f"{when} | {name}"
        if entity_id:
            line += f" ({entity_id})"
        if message:
            line += f" — {message}"

        # Apply text filter if provided
        if filter_text and filter_text.lower() not in line.lower():
            continue

        lines.append(line)

    if not lines:
        window_desc = (
            f"{_format_api_timestamp(start_time)} to {_format_api_timestamp(end_dt)}"
            if end_dt
            else f"the last {hours_back} hours"
        )
        return f"(no logbook entries found for {window_desc})"

    return "\n".join(lines)


def fetch_sensor_history(
    config: dict[str, str],
    entity_id: str,
    hours_back: int = 24,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> str:
    """Fetch state history for a specific sensor entity from the HA REST API.

    Queries /api/history/period/{timestamp}?filter_entity_id={entity_id}.
    When start_dt is provided, uses it directly (ignoring hours_back).
    Returns formatted state changes as: {timestamp} | {state} | {attributes_summary}
    """
    token = config.get("HA_API_TOKEN", "")
    if not token:
        print(
            "ERROR: HA_API_TOKEN is not set in .deploy.env.\n"
            "Generate a long-lived access token from your HA Profile page and add it.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = _build_ha_base_url(config)

    start_time = start_dt if start_dt is not None else datetime.now(UTC) - timedelta(hours=hours_back)

    timestamp = _format_api_timestamp(start_time)
    url = f"{base_url}/api/history/period/{timestamp}?filter_entity_id={urllib.request.quote(entity_id)}"
    if end_dt is not None:
        url += f"&end_time={urllib.request.quote(_format_api_timestamp(end_dt))}"

    data = _ha_api_request(url, token)

    if not isinstance(data, list) or not data:
        window_desc = (
            f"{_format_api_timestamp(start_time)} to {_format_api_timestamp(end_dt)}"
            if end_dt
            else f"the last {hours_back} hours"
        )
        return f"(no history found for {entity_id} in {window_desc})"

    # The API returns a list of lists (one list per entity)
    if start_dt is not None:
        end_label = _format_api_timestamp(end_dt) if end_dt else "now"
        window_label = f"{_format_api_timestamp(start_dt)} to {end_label}"
    else:
        window_label = f"last {hours_back}h"
    lines = [f"=== History for {entity_id} ({window_label}) ==="]
    for entity_states in data:
        if not isinstance(entity_states, list):
            continue
        for state_entry in entity_states:
            if not isinstance(state_entry, dict):
                continue
            last_changed = state_entry.get("last_changed", "")
            state = state_entry.get("state", "")
            attributes = state_entry.get("attributes", {})

            # Build a concise attributes summary (skip large/noisy keys)
            _SKIP_ATTRS = {"friendly_name", "unit_of_measurement", "icon", "device_class"}
            attr_parts = []
            for k, v in attributes.items():
                if k in _SKIP_ATTRS:
                    continue
                v_str = str(v)
                # Truncate long values
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                attr_parts.append(f"{k}={v_str}")
            attr_summary = ", ".join(attr_parts) if attr_parts else ""

            line = f"{last_changed} | {state}"
            if attr_summary:
                line += f" | {attr_summary}"
            lines.append(line)

    if len(lines) == 1:
        window_desc = (
            f"{_format_api_timestamp(start_time)} to {_format_api_timestamp(end_dt)}"
            if end_dt
            else f"the last {hours_back} hours"
        )
        return f"(no state changes found for {entity_id} in {window_desc})"

    return "\n".join(lines)


def ssh_args(config: dict[str, str]) -> list[str]:
    """Build SSH command-line arguments."""
    args = [
        "ssh",
        "-p",
        config["HA_SSH_PORT"],
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
    ]
    if config["HA_SSH_KEY"]:
        args.extend(["-i", config["HA_SSH_KEY"]])
    return args


def ssh_target(config: dict[str, str]) -> str:
    return f"{config['HA_SSH_USER']}@{config['HA_HOST']}"


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


def fetch_logs(
    config: dict[str, str],
    *,
    lines: int = 50,
    component_filter: str = "climate_advisor",
    extra_filter: str = "",
    full_dump: bool = False,
    raw_lines: int = DEFAULT_RAW_LINES,
) -> str:
    """Fetch logs from HA via SSH using `ha core logs`. Returns cleaned log text.

    `ha core logs` defaults to only its last 100 lines when no `--lines` is given, so we
    always pass `--lines` explicitly to the remote command. When filtering locally with
    grep/tail, we pull `raw_lines` from the remote (a generous depth so grep can find
    matches spread across days) and then trim to `lines` after filtering. For a full
    dump (no filtering), the user's requested `lines` count is the raw pull size directly.
    """
    if full_dump:
        remote_cmd = f"ha core logs --lines {lines}"
    elif component_filter:
        remote_cmd = f"ha core logs --lines {raw_lines} 2>/dev/null | grep -i {shlex.quote(component_filter)}"
        if extra_filter:
            remote_cmd += f" | grep -i {shlex.quote(extra_filter)}"
        remote_cmd += f" | tail -n {lines}"
    else:
        remote_cmd = f"ha core logs --lines {raw_lines} 2>/dev/null"
        if extra_filter:
            remote_cmd += f" | grep -i {shlex.quote(extra_filter)}"
        remote_cmd += f" | tail -n {lines}"

    cmd = ssh_args(config) + [ssh_target(config), remote_cmd]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if result.returncode != 0 and not result.stdout.strip():
        stderr = result.stderr.strip()
        if stderr:
            print(f"SSH error: {stderr}", file=sys.stderr)
        return ""

    return strip_ansi(result.stdout)


def main() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI

    parser = argparse.ArgumentParser(description="Fetch Home Assistant logs via SSH or REST API history")
    # SSH mode args
    parser.add_argument("--lines", "-n", type=int, default=500, help="Number of log lines to fetch (default: 500)")
    parser.add_argument("--all", action="store_true", help="Show all HA log lines, not just climate_advisor")
    parser.add_argument("--filter", "-f", type=str, default="", help="Additional grep/text filter (e.g. 'ERROR')")
    parser.add_argument("--full", action="store_true", help="Dump the entire HA log (can be large)")
    parser.add_argument(
        "--thermal",
        action="store_true",
        help=(
            "Show thermal-learning-relevant log lines only. "
            "Uses --lines 2000 unless overridden. "
            "Filters for Thermal/rolling window/keeping alive/commit/abandon/reject/pending obs/pipeline entries "
            "and excludes Chart data/predicted_indoor/Generated 0/Day type/Trend/Recommendations noise."
        ),
    )
    parser.add_argument("--save", "-s", action="store_true", help="Save output to logs/ directory")
    # REST API history mode args
    parser.add_argument(
        "--history",
        action="store_true",
        help="Use HA REST API history mode instead of SSH",
    )
    parser.add_argument(
        "--entity",
        type=str,
        default="",
        help="Entity ID(s) to filter history (comma-separated, e.g. sensor.climate_advisor_status)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Hours of history to fetch in --history mode (default: 24)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="",
        help=(
            "Start of time window as local ISO 8601 timestamp, e.g. '2026-03-28T18:00:00'. "
            "When provided, --hours is ignored."
        ),
    )
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="End of time window as local ISO 8601 timestamp (optional; defaults to now when --start is given).",
    )
    args = parser.parse_args()

    config = load_config()

    # Parse --start / --end into UTC datetimes if provided
    start_dt: datetime | None = _parse_local_timestamp(args.start) if args.start else None
    end_dt: datetime | None = _parse_local_timestamp(args.end) if args.end else None

    if args.history:
        # REST API history mode
        entity_ids = [e.strip() for e in args.entity.split(",") if e.strip()] if args.entity else []

        output_parts = []
        if entity_ids:
            for entity_id in entity_ids:
                output_parts.append(
                    fetch_sensor_history(
                        config,
                        entity_id,
                        hours_back=args.hours,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                )
        else:
            # No specific entity — use logbook
            output_parts.append(
                fetch_history(
                    config,
                    entity_filter=args.entity,
                    hours_back=args.hours,
                    filter_text=args.filter,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            )

        output = "\n\n".join(output_parts)

        if not output.strip():
            print("(no history data found)")
            return

        print(output)

        if args.save:
            LOG_DIR.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            entity_slug = args.entity.replace(",", "_").replace(".", "-") if args.entity else "logbook"
            save_path = LOG_DIR / f"ha-history-{entity_slug}-{timestamp}.log"
            save_path.write_text(output, encoding="utf-8")
            print(f"\nSaved to: {save_path}", file=sys.stderr)

    else:
        # SSH mode (original behaviour)
        if args.thermal:
            thermal_lines = args.lines if args.lines != 500 else 2000  # use 2000 unless user overrode --lines
            raw = fetch_logs(
                config,
                lines=thermal_lines,
                component_filter="climate_advisor",
                extra_filter="",
                full_dump=False,
                raw_lines=max(thermal_lines, DEFAULT_RAW_LINES),
            )
            thermal_keywords = re.compile(
                r"Thermal|thermal|rolling window|keeping alive|max_window|commit|abandon|reject|pending obs|pipeline",
                re.IGNORECASE,
            )
            noise_keywords = re.compile(
                r"Chart data|predicted_indoor|Generated 0|Day type|Trend |Recommendations",
                re.IGNORECASE,
            )
            lines_out = [ln for ln in raw.splitlines() if thermal_keywords.search(ln) and not noise_keywords.search(ln)]
            output = "\n".join(lines_out)
        else:
            component = "" if args.all else "climate_advisor"
            if args.full:
                # Bare --full with no explicit --lines should dump a large window, not argparse's 500 default.
                full_lines = args.lines if args.lines != 500 else DEFAULT_RAW_LINES
                output = fetch_logs(config, lines=full_lines, component_filter=component, full_dump=True)
            else:
                output = fetch_logs(
                    config,
                    lines=args.lines,
                    component_filter=component,
                    extra_filter=args.filter,
                    full_dump=False,
                    raw_lines=max(args.lines, DEFAULT_RAW_LINES),
                )

        if not output.strip():
            print("(no matching log lines found)")
            return

        print(output)

        if args.save:
            LOG_DIR.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            suffix = "full" if args.full else ("all" if args.all else "climate_advisor")
            save_path = LOG_DIR / f"ha-logs-{suffix}-{timestamp}.log"
            save_path.write_text(output, encoding="utf-8")
            print(f"\nSaved to: {save_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
