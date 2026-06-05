#!/usr/bin/env python3
"""Build pending simulation scenarios from historical chart_log comfort violations.

Reads the chart_log from HA via SSH and extracts time windows matching specified
criteria (comfort violations, natural ventilation, system restarts), then generates
scenario JSON files for pending simulation.

Usage:
    python tools/build_historical_scenario.py [--hours 72] [--type comfort_violation|restart|nat_vent]
    python tools/build_historical_scenario.py --hours 48 --type comfort_violation
    python tools/build_historical_scenario.py --hours 168 --type nat_vent --comfort-cool 76

Chart_log entries have fields: ts, hvac, fan, indoor, outdoor, windows_open
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "tools" / "simulations" / "pending"


def _load_dotenv(path: str) -> dict[str, str]:
    """Load a simple key=value dotenv file."""
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return result


def load_config() -> dict[str, str]:
    """Load SSH config from .deploy.env (same as thermal_replay.py)."""
    env = _load_dotenv(str(REPO_ROOT / "tools" / ".deploy.env")) or _load_dotenv(str(REPO_ROOT / ".deploy.env"))
    deploy = _load_dotenv(str(REPO_ROOT / "tools" / ".deploy.env")) or _load_dotenv(str(REPO_ROOT / ".deploy.env"))
    merged = {**deploy, **env}
    if not merged.get("HA_URL") and merged.get("HA_HOST"):
        merged["HA_URL"] = f"http://{merged['HA_HOST']}:8123"
    return merged


_CHART_LOG_REMOTE = "/config/climate_advisor_chart_log.json"


def _ssh_args(config: dict) -> list[str]:
    """Build SSH command args (reuses thermal_replay.py pattern)."""
    host = config.get("HA_HOST", "homeassistant.local")
    user = config.get("HA_SSH_USER", "root")
    key = config.get("HA_SSH_KEY", "")
    port = str(config.get("HA_SSH_PORT", "22"))
    args = ["ssh", f"-p{port}", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        args += ["-i", key]
    args.append(f"{user}@{host}")
    return args


def fetch_chart_log_ssh(config: dict) -> list[dict]:
    """SSH into HA and read the chart_log JSON."""
    result = subprocess.run(
        [*_ssh_args(config), f"cat {_CHART_LOG_REMOTE}"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read failed: {result.stderr.decode()}")
    data = json.loads(result.stdout)
    return data.get("entries", [])


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def find_comfort_violations(
    entries: list[dict],
    hours: int,
    comfort_cool_f: float,
    comfort_heat_f: float,
    min_violation_minutes: int = 15,
) -> list[dict]:
    """Find time windows where indoor temp violates comfort bounds."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    windows = []
    violation_runs = []
    in_violation = False
    violation_start_idx = None
    peak_indoor = None

    for i, entry in enumerate(entries):
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = _parse_ts(ts_str)
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        if indoor is None:
            if in_violation:
                violation_runs.append((violation_start_idx, i - 1, peak_indoor))
                in_violation = False
            continue

        violates = indoor > comfort_cool_f or indoor < comfort_heat_f
        if violates:
            if not in_violation:
                violation_start_idx = i
                in_violation = True
                peak_indoor = indoor
            else:
                peak_indoor = max(peak_indoor, indoor) if indoor > comfort_cool_f else min(peak_indoor, indoor)
        else:
            if in_violation:
                violation_runs.append((violation_start_idx, i - 1, peak_indoor))
                in_violation = False

    if in_violation:
        violation_runs.append((violation_start_idx, len(entries) - 1, peak_indoor))

    for start_idx, end_idx, peak_val in violation_runs:
        start_entry = entries[start_idx]
        end_entry = entries[end_idx]
        try:
            ts_start = _parse_ts(start_entry.get("ts", ""))
            ts_end = _parse_ts(end_entry.get("ts", ""))
        except Exception:
            continue

        duration_min = (ts_end - ts_start).total_seconds() / 60.0
        if duration_min < min_violation_minutes:
            continue

        before_ts = ts_start - timedelta(minutes=30)
        after_ts = ts_end + timedelta(minutes=30)

        window_entries = []
        for entry in entries:
            e_ts_str = entry.get("ts", "")
            if not e_ts_str:
                continue
            try:
                e_ts = _parse_ts(e_ts_str)
            except Exception:
                continue
            if e_ts.tzinfo is None:
                e_ts = e_ts.replace(tzinfo=UTC)
            if before_ts <= e_ts <= after_ts:
                window_entries.append(entry)

        if not window_entries:
            continue

        indoor_start = start_entry.get("indoor")
        indoor_end = end_entry.get("indoor")
        hvac_during = end_entry.get("hvac", "off")
        fan_during = end_entry.get("fan", False)

        violation_type = "too_hot" if peak_val > comfort_cool_f else "too_cold"
        delta_f = abs(peak_val - comfort_cool_f if violation_type == "too_hot" else peak_val - comfort_heat_f)
        threshold = comfort_cool_f if violation_type == "too_hot" else comfort_heat_f
        desc = f"indoor{peak_val:.0f}F-{delta_f:.0f}Fabove{threshold:.0f}F"

        windows.append(
            {
                "start_ts": ts_start.isoformat(),
                "end_ts": ts_end.isoformat(),
                "start_indoor": indoor_start,
                "peak_indoor": peak_val,
                "end_indoor": indoor_end,
                "duration_minutes": duration_min,
                "hvac_mode_during": hvac_during,
                "fan_during": fan_during,
                "description": desc,
                "violation_type": violation_type,
                "entries": window_entries,
            }
        )

    return windows


def find_nat_vent_windows(
    entries: list[dict],
    hours: int,
    comfort_cool_f: float,
) -> list[dict]:
    """Find windows where fan=True and indoor > comfort_cool_f."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    windows = []
    current_window = None
    window_entries = []

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = _parse_ts(ts_str)
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        fan = entry.get("fan")
        if indoor is None or fan is None:
            if current_window is not None:
                if len(window_entries) >= 2:
                    windows.append({**current_window, "entries": window_entries})
                current_window = None
                window_entries = []
            continue

        is_nat_vent = fan and indoor > comfort_cool_f
        if is_nat_vent:
            if current_window is None:
                current_window = {"start_ts": ts.isoformat(), "start_indoor": indoor}
            current_window["end_ts"] = ts.isoformat()
            current_window["peak_indoor"] = max(current_window.get("peak_indoor", indoor), indoor)
            current_window["end_indoor"] = indoor
            window_entries.append(entry)
        else:
            if current_window is not None:
                if len(window_entries) >= 2:
                    start_ts = _parse_ts(current_window["start_ts"])
                    end_ts = _parse_ts(current_window["end_ts"])
                    duration_min = (end_ts - start_ts).total_seconds() / 60.0
                    current_window["duration_minutes"] = duration_min
                    current_window["description"] = f"natvent{current_window['peak_indoor']:.0f}F"
                    windows.append({**current_window, "entries": window_entries})
                current_window = None
                window_entries = []

    if current_window is not None and len(window_entries) >= 2:
        start_ts = _parse_ts(current_window["start_ts"])
        end_ts = _parse_ts(current_window["end_ts"])
        duration_min = (end_ts - start_ts).total_seconds() / 60.0
        current_window["duration_minutes"] = duration_min
        current_window["description"] = f"natvent{current_window['peak_indoor']:.0f}F"
        windows.append({**current_window, "entries": window_entries})

    return windows


def build_scenario_json(window: dict, window_type: str, comfort_cool_f: float, comfort_heat_f: float) -> dict:
    """Build a scenario JSON from a detected window."""
    entries = window.get("entries", [])
    start_ts_str = window.get("start_ts", "")

    events = []
    for entry in entries:
        ts_str = entry.get("ts", "")
        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "off")
        fan = entry.get("fan", False)
        windows_open = entry.get("windows_open", False)

        if indoor is not None:
            event = {
                "time": ts_str,
                "type": "temp_update",
                "indoor_f": indoor,
            }
            if outdoor is not None:
                event["outdoor_f"] = outdoor
            event["note"] = f"hvac={hvac}, fan={fan}, windows_open={windows_open}"
            events.append(event)

    scenario = {
        "name": f"{start_ts_str[:10]}-{window_type}-{window.get('description', 'scenario')}",
        "description": f"{window_type} window: {window.get('description', '')}",
        "source": "build_historical_scenario",
        "issue": None,
        "notes": [
            "Generated from real climate_advisor_chart_log entries.",
            "Manual review recommended before simulation.",
        ],
        "config": {
            "comfort_heat": comfort_heat_f,
            "comfort_cool": comfort_cool_f,
        },
        "verdict": {
            "type": "pending",
            "summary": f"Real {window_type} window from HA history",
            "observed_behavior": "N/A (pending simulation)",
            "expected_behavior": "N/A (pending simulation)",
        },
        "events": events,
        "assertions": [],
    }

    return scenario


def save_scenario(scenario: dict, output_dir: Path) -> Path:
    """Save scenario JSON to pending directory, returning the path."""
    output_dir.mkdir(parents=True, exist_ok=True)

    base_name = scenario["name"].replace(" ", "_")[:40]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}-{base_name}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as f:
        json.dump(scenario, f, indent=2)

    return filepath


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build pending simulation scenarios from historical chart_log comfort violations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hours", type=int, default=72, help="Look back N hours (default: 72)")
    parser.add_argument(
        "--type",
        choices=["comfort_violation", "nat_vent", "restart"],
        default="comfort_violation",
        help="Type of window to extract (default: comfort_violation)",
    )
    parser.add_argument(
        "--comfort-cool", type=float, default=74.0, help="Comfort cool threshold in degrees F (default: 74.0)"
    )
    parser.add_argument(
        "--comfort-heat", type=float, default=70.0, help="Comfort heat threshold in degrees F (default: 70.0)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(PENDING_DIR),
        help="Output directory for pending scenarios (default: tools/simulations/pending/)",
    )

    args = parser.parse_args()

    try:
        config = load_config()
        print("Fetching chart_log from HA via SSH...", file=sys.stderr)
        entries = fetch_chart_log_ssh(config)
    except Exception as e:
        print(f"ERROR: Failed to fetch chart_log: {e}", file=sys.stderr)
        sys.exit(1)

    if not entries:
        print("ERROR: chart_log is empty or unreachable.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(entries)} chart_log entries.", file=sys.stderr)

    output_dir = Path(args.output_dir)
    windows = []

    if args.type == "comfort_violation":
        windows = find_comfort_violations(entries, args.hours, args.comfort_cool, args.comfort_heat)
        print(f"\nFound {len(windows)} comfort_violation window(s):\n")
    elif args.type == "nat_vent":
        windows = find_nat_vent_windows(entries, args.hours, args.comfort_cool)
        print(f"\nFound {len(windows)} natural_ventilation window(s):\n")
    else:
        print("ERROR: restart type not yet implemented.", file=sys.stderr)
        sys.exit(1)

    if not windows:
        print(f"No {args.type} windows found in the last {args.hours} hours.")
        return

    for i, window in enumerate(windows, 1):
        start_ts = _parse_ts(window["start_ts"])
        end_ts = _parse_ts(window["end_ts"])
        duration_min = window.get("duration_minutes", 0)
        peak_indoor = window.get("peak_indoor", "?")

        print(f"Window {i}: {start_ts.strftime('%Y-%m-%d %H:%M')} UTC → {end_ts.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  Indoor peak: {peak_indoor:.1f}F")
        print(f"  Duration: {int(duration_min // 60)}h {int(duration_min % 60)}m")
        print(f"  HVAC during: {window.get('hvac_mode_during', '?')} (fan={window.get('fan_during', '?')})")

        scenario = build_scenario_json(window, args.type, args.comfort_cool, args.comfort_heat)
        try:
            filepath = save_scenario(scenario, output_dir)
            print(f"  -> Saved: {filepath.relative_to(REPO_ROOT)}")
        except Exception as e:
            print(f"  ERROR saving scenario: {e}")

        print()


if __name__ == "__main__":
    main()
