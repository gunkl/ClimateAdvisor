#!/usr/bin/env python3
"""Closed-loop simulation feedback agent.

Polls production event_log for new incident_detected events, generates
pending BSpecs via build_historical_scenario.py, runs logic-track validation
via simulate.py, and writes Validation Records to tools/simulations/results/.

Usage:
    python tools/simulation_loop.py [--once] [--hours 2]
    python tools/simulation_loop.py --dry-run   # show incidents without acting
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "tools" / "simulations" / "results"
PROCESSED_PATH = RESULTS_DIR / "processed_incidents.json"
STATS_PATH = RESULTS_DIR / "pending_stats.json"

# Map incident_class values to --type arg for build_historical_scenario.py
INCIDENT_CLASS_TO_TYPE: dict[str, str] = {
    "comfort_violation": "comfort_violation",
    "nat_vent": "nat_vent",
    "natural_ventilation": "nat_vent",
    "occupancy_transition": "occupancy_transition",
    "setpoint_mode_inconsistency": "setpoint_mode_inconsistency",
    "rapid_override_after_automation": "rapid_override_after_automation",
}

CONSECUTIVE_FAILURE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Config (.env loading — same pattern as thermal_replay.py)
# ---------------------------------------------------------------------------


def _load_dotenv(path: str) -> dict[str, str]:
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
    env = _load_dotenv(str(REPO_ROOT / "tools" / ".env")) or _load_dotenv(str(REPO_ROOT / ".env"))
    deploy = _load_dotenv(str(REPO_ROOT / "tools" / ".deploy.env")) or _load_dotenv(str(REPO_ROOT / ".deploy.env"))
    merged = {**deploy, **env}
    if not merged.get("HA_URL") and merged.get("HA_HOST"):
        merged["HA_URL"] = f"http://{merged['HA_HOST']}:8123"
    if not merged.get("HA_TOKEN") and merged.get("HA_API_TOKEN"):
        merged["HA_TOKEN"] = merged["HA_API_TOKEN"]
    return merged


# ---------------------------------------------------------------------------
# HA REST API
# ---------------------------------------------------------------------------


def fetch_event_log(ha_url: str, ha_token: str, hours: int) -> list[dict]:
    """GET /api/climate_advisor/event_log?hours=N and return entries list."""
    url = f"{ha_url.rstrip('/')}/api/climate_advisor/event_log?hours={hours}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if not isinstance(data, dict):
        return []
    return data.get("entries", [])


def notify_ha(ha_url: str, ha_token: str, service: str, message: str) -> None:
    """Send a notification via HA notify service (best-effort)."""
    url = f"{ha_url.rstrip('/')}/api/services/notify/{service}"
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"  [warn] HA notification failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Atomic JSON helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path, default) -> object:
    """Read JSON from path; return default if missing or corrupt."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, type(default)):
            return default
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, data: object) -> None:
    """Write JSON to path atomically (tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# build_historical_scenario.py subprocess
# ---------------------------------------------------------------------------


def run_build_scenario(incident_class: str, incident_id: str, hours: int) -> Path | None:
    """Run build_historical_scenario.py and return the Path of the created file.

    Returns None if the subprocess failed or produced no output file.
    Raises on SSH/config errors so the caller can skip without marking processed.
    """
    scenario_type = INCIDENT_CLASS_TO_TYPE.get(incident_class)
    if not scenario_type:
        print(f"  [skip] unknown incident_class={incident_class!r} — no type mapping", file=sys.stderr)
        return None

    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "build_historical_scenario.py"),
        "--type",
        scenario_type,
        "--hours",
        str(hours),
    ]
    print(f"  Running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # SSH / HA unreachable — caller should skip, not mark processed
        if any(kw in stderr for kw in ("SSH", "ssh", "Connection", "timeout", "Failed to fetch")):
            raise RuntimeError(f"Build failed (infra): {stderr[:200]}")
        print(f"  [warn] build_historical_scenario returned {result.returncode}: {stderr[:200]}", file=sys.stderr)
        return None

    # Parse "-> Saved: tools/simulations/pending/<filename>" from stdout
    for line in result.stdout.splitlines():
        if "-> Saved:" in line:
            rel = line.split("-> Saved:", 1)[1].strip()
            candidate = REPO_ROOT / rel
            if candidate.exists():
                return candidate
    return None


# ---------------------------------------------------------------------------
# simulate.py subprocess
# ---------------------------------------------------------------------------


def run_simulate(scenario_path: Path) -> dict:
    """Run simulate.py -s <name> and parse the text output into a result dict.

    Returns a dict with keys: passed, assertion_count, assertion_passes,
    assertion_failures, assertion_skips, assertions, error.
    """
    name = scenario_path.stem
    cmd = [sys.executable, str(REPO_ROOT / "tools" / "simulate.py"), "-s", name]
    print(f"  Running: {' '.join(cmd)}", file=sys.stderr)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "error": "simulate.py timed out",
            "assertion_count": 0,
            "assertion_passes": 0,
            "assertion_failures": 0,
            "assertion_skips": 0,
            "assertions": [],
        }

    if result.returncode not in (0, 1):
        # Non-zero exit codes other than "scenario failed" = import/syntax error
        return {
            "passed": False,
            "error": result.stderr.strip()[:300],
            "assertion_count": 0,
            "assertion_passes": 0,
            "assertion_failures": 0,
            "assertion_skips": 0,
            "assertions": [],
        }

    # Parse human-readable output
    assertions: list[dict] = []
    passed_flag: bool | None = None
    for line in result.stdout.splitlines():
        line_s = line.strip()
        if line_s.startswith("[OK]"):
            assertions.append({"status": "pass", "text": line_s})
        elif line_s.startswith("[FAIL]"):
            assertions.append({"status": "fail", "text": line_s})
        elif line_s.startswith("[SKIP]"):
            assertions.append({"status": "skip", "text": line_s})
        elif "Status: PASS" in line_s:
            passed_flag = True
        elif "Status: FAIL" in line_s:
            passed_flag = False

    # Fall back: infer from return code
    if passed_flag is None:
        passed_flag = result.returncode == 0

    passes = sum(1 for a in assertions if a["status"] == "pass")
    failures = sum(1 for a in assertions if a["status"] == "fail")
    skips = sum(1 for a in assertions if a["status"] == "skip")

    return {
        "passed": passed_flag,
        "assertion_count": len(assertions),
        "assertion_passes": passes,
        "assertion_failures": failures,
        "assertion_skips": skips,
        "assertions": assertions,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Validation Record writer
# ---------------------------------------------------------------------------


def write_validation_record(
    scenario_name: str,
    incident_id: str,
    incident_class: str,
    sim_result: dict,
) -> None:
    """Write a timestamped logic validation record under results/<scenario_name>/."""
    now_iso = datetime.now(UTC).isoformat()
    record = {
        "scenario": scenario_name,
        "incident_id": incident_id,
        "incident_class": incident_class,
        "run_at": now_iso,
        "passed": sim_result.get("passed"),
        "assertion_count": sim_result.get("assertion_count", 0),
        "assertion_passes": sim_result.get("assertion_passes", 0),
        "assertion_skips": sim_result.get("assertion_skips", 0),
        "assertion_failures": sim_result.get("assertion_failures", 0),
        "assertions": sim_result.get("assertions", []),
    }
    if sim_result.get("error"):
        record["error"] = sim_result["error"]

    ts_slug = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RESULTS_DIR / scenario_name
    out_path = out_dir / f"{ts_slug}-logic.json"
    _write_json_atomic(out_path, record)
    print(f"  [record] {out_path.relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Stats rebuild
# ---------------------------------------------------------------------------


def update_stats(
    stats: dict,
    scenario_name: str,
    incident_class: str,
    sim_result: dict,
    first_seen: str,
) -> None:
    """Update pending_stats in-place for one scenario run."""
    now_iso = datetime.now(UTC).isoformat()
    entry = stats.get(scenario_name, {})
    passed = sim_result.get("passed")

    stats[scenario_name] = {
        "incident_class": incident_class,
        "first_seen": entry.get("first_seen", first_seen),
        "runs": entry.get("runs", 0) + 1,
        "logic_passes": entry.get("logic_passes", 0) + (1 if passed is True else 0),
        "logic_failures": entry.get("logic_failures", 0) + (1 if passed is False else 0),
        "logic_skips": entry.get("logic_skips", 0) + (1 if passed is None else 0),
        "integration_passes": entry.get("integration_passes", 0),
        "integration_failures": entry.get("integration_failures", 0),
        "last_run": now_iso,
        "last_passed": passed,
    }


# ---------------------------------------------------------------------------
# Consecutive failure check
# ---------------------------------------------------------------------------


def check_consecutive_failures(stats: dict, config: dict) -> None:
    """Warn (and optionally notify via HA) when any incident_class has 3+ consecutive logic failures."""
    # Group by incident_class; find classes where all recent scenarios are failing
    class_failures: dict[str, int] = {}
    for _name, entry in stats.items():
        ic = entry.get("incident_class", "unknown")
        if entry.get("last_passed") is False:
            class_failures[ic] = class_failures.get(ic, 0) + 1

    ha_url = config.get("HA_URL", "")
    ha_token = config.get("HA_TOKEN", "")
    notify_service = config.get("NOTIFY_SERVICE", "persistent_notification")

    for ic, count in class_failures.items():
        if count >= CONSECUTIVE_FAILURE_THRESHOLD:
            msg = (
                f"[simulation_loop] WARNING: {count} consecutive logic failures "
                f"for incident_class={ic!r}. Review pending scenarios."
            )
            print(msg)
            if ha_url and ha_token:
                notify_ha(ha_url, ha_token, notify_service, msg)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _bootstrap_pending_bspecs(stats: dict) -> int:
    """Run simulate.py on authored pending BSpecs that have no recent result.

    This runs WITHOUT HA access — simulate.py is pure Python. It ensures that
    manually authored scenarios appear in pending_stats.json immediately on the
    first loop run, without needing a production incident to trigger them.
    """
    pending_dir = REPO_ROOT / "tools" / "simulations" / "pending"
    if not pending_dir.exists():
        return 0

    count = 0
    now = datetime.now(UTC)
    for f in sorted(pending_dir.glob("*.json")):
        name = f.stem
        entry = stats.get(name, {})
        last_run = entry.get("last_run", "")
        if last_run:
            try:
                age = (now - datetime.fromisoformat(last_run)).total_seconds()
                if age < 86400:  # skip if run in last 24 hours
                    continue
            except (ValueError, TypeError):
                pass
        print(f"  [bootstrap] running simulate.py on authored BSpec: {name}")
        sim_result = run_simulate(f)  # run_simulate expects a Path
        if sim_result is not None:
            update_stats(
                stats,
                name,
                entry.get("incident_class", "authored"),
                sim_result,
                entry.get("first_seen") or now.isoformat(),
            )
            count += 1
        else:
            print(f"  [bootstrap] simulate.py returned no result for {name}", file=sys.stderr)

    return count


def run_once(config: dict, hours: int, dry_run: bool) -> int:
    """Single pass: bootstrap authored BSpecs, then fetch and process new incidents."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stats: dict = _read_json(STATS_PATH, {})

    # Phase 1: Bootstrap — run simulate.py on authored pending BSpecs (no HA needed).
    # This ensures manually authored scenarios appear in the Tests tab immediately.
    bootstrap_count = _bootstrap_pending_bspecs(stats)
    if bootstrap_count:
        print(f"[bootstrap] ran {bootstrap_count} authored BSpec(s)")
        _write_json_atomic(STATS_PATH, stats)

    ha_url = config.get("HA_URL", "")
    ha_token = config.get("HA_TOKEN", "")

    if not ha_url or not ha_token:
        print("WARNING: HA_URL/HA_TOKEN not set — skipping incident fetch. Bootstrap only.", file=sys.stderr)
        return bootstrap_count

    # Phase 2: Incident polling — process new production incidents from HA.
    print(f"Fetching event_log (last {hours}h) from {ha_url}...", file=sys.stderr)
    try:
        entries = fetch_event_log(ha_url, ha_token, hours)
    except Exception as exc:
        print(f"ERROR: Failed to fetch event_log: {exc}", file=sys.stderr)
        return bootstrap_count

    incidents = [e for e in entries if e.get("type") == "incident_detected"]
    print(f"Found {len(incidents)} incident_detected event(s).")

    if dry_run:
        for inc in incidents:
            inc_id = inc.get("id", "?")
            inc_class = inc.get("incident_class", "?")
            inc_time = inc.get("time", "?")
            print(f"  [dry-run] id={inc_id!r} class={inc_class!r} at={inc_time!r}")
        return bootstrap_count

    processed: dict = _read_json(PROCESSED_PATH, {})

    processed_count = 0

    for incident in incidents:
        incident_id = str(incident.get("id") or incident.get("time") or "unknown")
        incident_class = incident.get("incident_class", "unknown")
        first_seen = incident.get("time", datetime.now(UTC).isoformat())

        if incident_id in processed:
            continue

        print(f"\nProcessing incident id={incident_id!r} class={incident_class!r}")

        # Step A: build scenario
        try:
            scenario_path = run_build_scenario(incident_class, incident_id, hours)
        except RuntimeError as exc:
            print(f"  [skip] infra error — will retry: {exc}", file=sys.stderr)
            continue  # do NOT mark as processed

        if scenario_path is None:
            # Build succeeded but produced no scenario (no matching windows) — mark done
            print(f"  [info] no scenario file produced for {incident_id!r}; marking processed")
            processed[incident_id] = {
                "incident_class": incident_class,
                "processed_at": datetime.now(UTC).isoformat(),
                "scenario": None,
                "result": "no_windows",
            }
            _write_json_atomic(PROCESSED_PATH, processed)
            continue

        scenario_name = scenario_path.stem
        print(f"  Scenario: {scenario_name}")

        # Step B: run simulate.py
        sim_result = run_simulate(scenario_path)

        if sim_result.get("error") and sim_result.get("assertion_count", 0) == 0:
            # Hard error (import/syntax) — mark processed with error flag
            print(f"  [error] simulate.py error: {sim_result['error']}", file=sys.stderr)
            processed[incident_id] = {
                "incident_class": incident_class,
                "processed_at": datetime.now(UTC).isoformat(),
                "scenario": scenario_name,
                "result": "error",
                "error": sim_result["error"],
            }
            _write_json_atomic(PROCESSED_PATH, processed)
            continue

        # Step C: write validation record
        write_validation_record(scenario_name, incident_id, incident_class, sim_result)

        # Step D: mark processed
        processed[incident_id] = {
            "incident_class": incident_class,
            "processed_at": datetime.now(UTC).isoformat(),
            "scenario": scenario_name,
            "result": "passed" if sim_result.get("passed") else "failed",
        }
        _write_json_atomic(PROCESSED_PATH, processed)

        # Step E: update stats
        update_stats(stats, scenario_name, incident_class, sim_result, first_seen)
        processed_count += 1

    # Rebuild stats file
    _write_json_atomic(STATS_PATH, stats)

    # Check for consecutive failures
    check_consecutive_failures(stats, config)

    return processed_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Closed-loop simulation feedback agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one pass and exit (default behavior; kept for clarity)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=2,
        help="Event log lookback window in hours (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print incidents without generating scenarios or validation records",
    )
    args = parser.parse_args()

    config = load_config()
    processed = run_once(config, args.hours, args.dry_run)
    if not args.dry_run:
        print(f"\nDone. {processed} new incident(s) processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
