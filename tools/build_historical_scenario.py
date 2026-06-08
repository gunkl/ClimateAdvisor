#!/usr/bin/env python3
"""Build pending simulation scenarios from historical chart_log comfort violations.

Reads the chart_log from HA via SSH and extracts time windows matching specified
criteria (comfort violations, natural ventilation, system restarts), then generates
scenario JSON files for pending simulation.

Usage:
    python tools/build_historical_scenario.py [--hours 72] [--type comfort_violation|nat_vent|
        occupancy_transition|setpoint_mode_inconsistency|rapid_override_after_automation]
    python tools/build_historical_scenario.py --hours 48 --type comfort_violation
    python tools/build_historical_scenario.py --hours 168 --type nat_vent --comfort-cool 76
    python tools/build_historical_scenario.py --hours 24 --type occupancy_transition
    python tools/build_historical_scenario.py --hours 72 --type setpoint_mode_inconsistency
    python tools/build_historical_scenario.py --hours 48 --type rapid_override_after_automation

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


def _fetch_event_log(config: dict, hours: int) -> list[dict]:
    """Fetch event_log from HA REST API if HA_URL and HA_TOKEN are available.

    Returns list of event entries, or empty list if credentials unavailable.
    Prints clear message and returns empty if fetch fails.
    """
    ha_url = config.get("HA_URL", "").rstrip("/")
    ha_token = config.get("HA_TOKEN") or config.get("HA_API_TOKEN")

    if not ha_url or not ha_token:
        print(
            "Warning: HA_URL and HA_TOKEN not configured in .deploy.env or .env.",
            "Event log fetch disabled. Set HA_URL and HA_TOKEN to enable incident class detection.",
            file=sys.stderr,
        )
        return []

    try:
        import urllib.request

        url = f"{ha_url}/api/climate_advisor/event_log?hours={hours}"

        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            return data.get("entries", [])
    except Exception as e:
        print(f"Warning: Failed to fetch event_log from HA: {e}", file=sys.stderr)
        return []


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


def find_occupancy_transition_windows(
    chart_entries: list[dict],
    event_entries: list[dict],
    hours: int,
    comfort_cool_f: float,
    comfort_heat_f: float,
) -> list[dict]:
    """Find windows around occupancy transitions from event_log.

    Looks for occupancy_change, occupancy_away, occupancy_home, or occupancy_vacation events.
    Extracts 30-min context around each transition.
    """
    if not event_entries:
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    windows = []

    occupancy_events = [
        e
        for e in event_entries
        if e.get("type") in ("occupancy_change", "occupancy_away", "occupancy_home", "occupancy_vacation")
    ]

    for event in occupancy_events:
        try:
            event_ts = _parse_ts(event.get("time", ""))
        except Exception:
            continue

        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=UTC)
        if event_ts < cutoff:
            continue

        before_ts = event_ts - timedelta(minutes=30)
        after_ts = event_ts + timedelta(minutes=15)

        window_entries = [
            e
            for e in chart_entries
            if (
                e.get("ts")
                and (lambda ts: before_ts <= ts <= after_ts)(
                    _parse_ts(e["ts"]).replace(tzinfo=UTC) if _parse_ts(e["ts"]).tzinfo is None else _parse_ts(e["ts"])
                )
            )
        ]

        if not window_entries:
            continue

        occ_before = event.get("occupancy_before", "unknown")
        occ_after = event.get("occupancy_after", "unknown")
        hvac_mode = event.get("hvac_mode", "off")
        override_active = event.get("manual_override_active", False)
        setpoint = event.get("setpoint_f")

        description = f"occupancy_{occ_before}_to_{occ_after}"

        windows.append(
            {
                "start_ts": before_ts.isoformat(),
                "end_ts": after_ts.isoformat(),
                "transition_ts": event_ts.isoformat(),
                "occupancy_before": occ_before,
                "occupancy_after": occ_after,
                "hvac_mode_at_transition": hvac_mode,
                "manual_override_active": override_active,
                "setpoint_at_transition": setpoint,
                "description": description,
                "entries": window_entries,
                "event_source": event,
            }
        )

    return windows


def find_setpoint_mode_inconsistency_windows(
    chart_entries: list[dict],
    event_entries: list[dict],
    hours: int,
) -> list[dict]:
    """Find windows around setpoint_mode_inconsistency incidents from event_log.

    Looks for incident_detected events with incident_class=setpoint_mode_inconsistency.
    Extracts 30-min before, 15-min after for context.
    """
    if not event_entries:
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    windows = []

    incident_events = [
        e
        for e in event_entries
        if e.get("type") == "incident_detected" and e.get("incident_class") == "setpoint_mode_inconsistency"
    ]

    for event in incident_events:
        try:
            event_ts = _parse_ts(event.get("time", ""))
        except Exception:
            continue

        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=UTC)
        if event_ts < cutoff:
            continue

        before_ts = event_ts - timedelta(minutes=30)
        after_ts = event_ts + timedelta(minutes=15)

        window_entries = [
            e
            for e in chart_entries
            if (
                e.get("ts")
                and (lambda ts: before_ts <= ts <= after_ts)(
                    _parse_ts(e["ts"]).replace(tzinfo=UTC) if _parse_ts(e["ts"]).tzinfo is None else _parse_ts(e["ts"])
                )
            )
        ]

        if not window_entries:
            continue

        setpoint = event.get("setpoint_f")
        hvac_mode = event.get("hvac_mode", "off")
        description = f"setpoint_inconsistency_mode_{hvac_mode}_setpoint_{setpoint:.0f}F"

        windows.append(
            {
                "start_ts": before_ts.isoformat(),
                "end_ts": after_ts.isoformat(),
                "incident_ts": event_ts.isoformat(),
                "setpoint_applied": setpoint,
                "hvac_mode": hvac_mode,
                "description": description,
                "entries": window_entries,
                "incident_source": event,
            }
        )

    return windows


def find_rapid_override_windows(
    chart_entries: list[dict],
    event_entries: list[dict],
    hours: int,
) -> list[dict]:
    """Find windows where override_detected fires within 60s of automation event.

    Looks for override_detected events preceded by automation-like events within 60 seconds.
    Extracts context around the pair.
    """
    if not event_entries:
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    windows = []

    override_events = [e for e in event_entries if e.get("type") == "override_detected"]

    automation_event_types = (
        "setpoint_change_applied",
        "mode_change_applied",
        "occupancy_away",
        "occupancy_home",
        "occupancy_vacation",
        "classification_applied",
    )

    for override_event in override_events:
        try:
            override_ts = _parse_ts(override_event.get("time", ""))
        except Exception:
            continue

        if override_ts.tzinfo is None:
            override_ts = override_ts.replace(tzinfo=UTC)
        if override_ts < cutoff:
            continue

        # Find automation event within 60s before
        preceding_automation = None
        gap_seconds = None

        for event in sorted(event_entries, key=lambda e: _parse_ts(e.get("time", "")), reverse=True):
            try:
                event_ts = _parse_ts(event.get("time", ""))
            except Exception:
                continue

            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=UTC)

            if event.get("type") in automation_event_types and event_ts < override_ts:
                gap = (override_ts - event_ts).total_seconds()
                if 0 < gap <= 60:
                    preceding_automation = event
                    gap_seconds = gap
                    break

        if not preceding_automation:
            continue

        # Extract window around the pair
        auto_ts = _parse_ts(preceding_automation.get("time", "")) if preceding_automation else None
        before_ts = (auto_ts - timedelta(minutes=5)) if auto_ts else (override_ts - timedelta(minutes=5))
        after_ts = override_ts + timedelta(minutes=5)

        window_entries = [
            e
            for e in chart_entries
            if (
                e.get("ts")
                and (lambda ts: before_ts <= ts <= after_ts)(
                    _parse_ts(e["ts"]).replace(tzinfo=UTC) if _parse_ts(e["ts"]).tzinfo is None else _parse_ts(e["ts"])
                )
            )
        ]

        if not window_entries:
            continue

        automation_type = preceding_automation.get("type", "unknown")
        description = f"rapid_override_after_{automation_type}_{gap_seconds:.0f}s_gap"

        windows.append(
            {
                "start_ts": before_ts.isoformat(),
                "end_ts": after_ts.isoformat(),
                "automation_ts": preceding_automation.get("time", ""),
                "override_ts": override_event.get("time", ""),
                "gap_seconds": gap_seconds,
                "automation_type": automation_type,
                "description": description,
                "entries": window_entries,
                "automation_event": preceding_automation,
                "override_event": override_event,
            }
        )

    return windows


def build_scenario_json(
    window: dict,
    window_type: str,
    comfort_cool_f: float,
    comfort_heat_f: float,
) -> dict:
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

    # Add incident-specific event for new handlers
    if window_type == "occupancy_transition":
        event_source = window.get("event_source", {})
        occ_event_type = event_source.get("type", "occupancy_change")

        events.append(
            {
                "time": window.get("transition_ts", ""),
                "type": occ_event_type,
                "occupancy_before": window.get("occupancy_before", "unknown"),
                "occupancy_after": window.get("occupancy_after", "unknown"),
            }
        )

    scenario = {
        "name": f"{start_ts_str[:10]}-{window_type}-{window.get('description', 'scenario')}",
        "description": f"{window_type} window: {window.get('description', '')}",
        "source": "build_historical_scenario",
        "issue": "#223",
        "notes": [
            "Generated from real climate_advisor_chart_log entries and event_log.",
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
        "anonymized": True,
    }

    # Add track-appropriate assertions based on window type
    if window_type == "occupancy_transition":
        scenario["assertions"] = [
            {
                "at": window.get("transition_ts", ""),
                "expect": "setback_applied",
                "track": "logic",
                "reason": "Occupancy transition should apply setpoint setback",
            }
        ]
    elif window_type == "setpoint_mode_inconsistency":
        scenario["assertions"] = [
            {
                "at": window.get("incident_ts", ""),
                "expect": "setpoint_consistent_with_mode",
                "track": "integration",
                "reason": "Applied setpoint must be consistent with HVAC mode",
            }
        ]
    elif window_type == "rapid_override_after_automation":
        scenario["assertions"] = [
            {
                "at": window.get("override_ts", ""),
                "expect": "override_detected",
                "track": "integration",
                "reason": "Override detected after automation action within 60s gap",
            }
        ]

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


def extract_from_issue(issue_number: int, output_dir: Path) -> Path | None:
    """Fetch GitHub issue body and extract embedded incident JSON block."""
    import re

    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "body"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Could not fetch issue #{issue_number}: {result.stderr}", file=sys.stderr)
        return None

    body = json.loads(result.stdout).get("body", "")
    # Find JSON code block containing "incident_class" (typically after "## Incident Package")
    match = re.search(r'```json\s*\n(\{.*?"incident_class".*?\})\s*\n```', body, re.DOTALL)
    if not match:
        print(f"No incident package found in issue #{issue_number}", file=sys.stderr)
        return None

    try:
        data = json.loads(match.group(1))
    except Exception as e:
        print(f"ERROR: Could not parse incident JSON: {e}", file=sys.stderr)
        return None

    incident_class = data.get("incident_class", "unknown")
    name = f"{incident_class}_from_issue_{issue_number}"
    out_path = output_dir / f"{name}.json"

    # Build a minimal BSpec-compatible scenario directly from incident package fields
    chart_log_window = data.get("chart_log_window", [])
    event_log_window = data.get("event_log_window", [])

    events: list[dict] = []
    for entry in chart_log_window:
        ts_str = entry.get("ts", "")
        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "off")
        fan = entry.get("fan", False)
        windows_open = entry.get("windows_open", False)
        if indoor is not None:
            event: dict = {"time": ts_str, "type": "temp_update", "indoor_f": indoor}
            if outdoor is not None:
                event["outdoor_f"] = outdoor
            event["note"] = f"hvac={hvac}, fan={fan}, windows_open={windows_open}"
            events.append(event)

    for evt in event_log_window:
        events.append(evt)

    scenario = {
        "name": name,
        "description": f"From GitHub issue #{issue_number}: {incident_class}",
        "source": "build_historical_scenario --from-issue",
        "issue": f"#{issue_number}",
        "notes": [
            f"Extracted from GitHub issue #{issue_number}.",
            "Manual review recommended before simulation.",
        ],
        "config": {
            "comfort_cool": data.get("comfort_cool", 74),
            "comfort_heat": data.get("comfort_heat", 70),
        },
        "verdict": {
            "type": "pending",
            "summary": f"Real {incident_class} incident from issue #{issue_number}",
            "observed_behavior": "N/A (pending simulation)",
            "expected_behavior": "N/A (pending simulation)",
        },
        "events": events,
        "assertions": [],
        "anonymized": True,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(scenario, f, indent=2)

    print(f"-> Saved: {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build pending simulation scenarios from historical chart_log and event_log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hours", type=int, default=72, help="Look back N hours (default: 72)")
    parser.add_argument(
        "--type",
        choices=[
            "comfort_violation",
            "nat_vent",
            "occupancy_transition",
            "setpoint_mode_inconsistency",
            "rapid_override_after_automation",
        ],
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
    parser.add_argument(
        "--from-issue",
        type=int,
        metavar="N",
        help="Extract incident package from GitHub issue #N and create pending BSpec",
    )

    args = parser.parse_args()

    if args.from_issue:
        output_dir = Path(args.output_dir)
        extract_from_issue(args.from_issue, output_dir)
        return

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

    # Fetch event_log for incident handlers
    event_entries = []
    if args.type in ("occupancy_transition", "setpoint_mode_inconsistency", "rapid_override_after_automation"):
        try:
            config = load_config()
            event_entries = _fetch_event_log(config, args.hours)
            if not event_entries:
                print(
                    f"ERROR: --type {args.type} requires event_log from HA API.",
                    "Configure HA_URL and HA_TOKEN in .deploy.env or .env",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Loaded {len(event_entries)} event_log entries.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: Failed to load event_log: {e}", file=sys.stderr)
            sys.exit(1)

    output_dir = Path(args.output_dir)
    windows = []

    if args.type == "comfort_violation":
        windows = find_comfort_violations(entries, args.hours, args.comfort_cool, args.comfort_heat)
        print(f"\nFound {len(windows)} comfort_violation window(s):\n")
    elif args.type == "nat_vent":
        windows = find_nat_vent_windows(entries, args.hours, args.comfort_cool)
        print(f"\nFound {len(windows)} natural_ventilation window(s):\n")
    elif args.type == "occupancy_transition":
        windows = find_occupancy_transition_windows(
            entries, event_entries, args.hours, args.comfort_cool, args.comfort_heat
        )
        print(f"\nFound {len(windows)} occupancy_transition window(s):\n")
    elif args.type == "setpoint_mode_inconsistency":
        windows = find_setpoint_mode_inconsistency_windows(entries, event_entries, args.hours)
        print(f"\nFound {len(windows)} setpoint_mode_inconsistency window(s):\n")
    elif args.type == "rapid_override_after_automation":
        windows = find_rapid_override_windows(entries, event_entries, args.hours)
        print(f"\nFound {len(windows)} rapid_override_after_automation window(s):\n")

    if not windows:
        print(f"No {args.type} windows found in the last {args.hours} hours.")
        return

    for i, window in enumerate(windows, 1):
        start_ts = _parse_ts(window["start_ts"])
        end_ts = _parse_ts(window["end_ts"])
        duration_min = window.get("duration_minutes", 0)
        peak_indoor = window.get("peak_indoor", "?")

        print(f"Window {i}: {start_ts.strftime('%Y-%m-%d %H:%M')} UTC → {end_ts.strftime('%Y-%m-%d %H:%M')} UTC")
        if peak_indoor != "?":
            print(f"  Indoor peak: {peak_indoor:.1f}F")
        if duration_min:
            print(f"  Duration: {int(duration_min // 60)}h {int(duration_min % 60)}m")
        if args.type == "occupancy_transition":
            print(f"  Occupancy: {window.get('occupancy_before', '?')} → {window.get('occupancy_after', '?')}")
            print(f"  HVAC mode: {window.get('hvac_mode_at_transition', '?')}")
            if window.get("manual_override_active"):
                print("  Override active: True")
            if window.get("setpoint_at_transition") is not None:
                print(f"  Setpoint: {window.get('setpoint_at_transition', '?'):.1f}F")
        elif args.type in ("setpoint_mode_inconsistency", "rapid_override_after_automation"):
            print(f"  Description: {window.get('description', 'N/A')}")
        else:
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
