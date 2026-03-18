#!/usr/bin/env python3
"""Fetch Home Assistant logs from a remote HAOS instance via SSH.

Uses `ha core logs` on the remote (HAOS stores logs in the container, not on disk).
Reuses the SSH connection config from .deploy.env.

Usage:
    python3 tools/ha_logs.py                        # Last 50 climate_advisor lines
    python3 tools/ha_logs.py --all                   # Last 100 lines of full HA log
    python3 tools/ha_logs.py --lines 200             # Last 200 climate_advisor lines
    python3 tools/ha_logs.py --filter "ERROR"        # Only ERROR lines for climate_advisor
    python3 tools/ha_logs.py --all --filter "ERROR"  # All ERROR lines in HA log
    python3 tools/ha_logs.py --full                  # Dump entire HA log (large!)
    python3 tools/ha_logs.py --save                  # Save output to logs/ directory
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".deploy.env"
LOG_DIR = REPO_ROOT / "logs"

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
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    else:
        print("ERROR: .deploy.env not found. Copy .deploy.env.sample and configure it.",
              file=sys.stderr)
        sys.exit(1)
    return config


def ssh_args(config: dict[str, str]) -> list[str]:
    """Build SSH command-line arguments."""
    args = [
        "ssh", "-p", config["HA_SSH_PORT"],
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
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
) -> str:
    """Fetch logs from HA via SSH using `ha core logs`. Returns cleaned log text."""
    # HAOS stores logs in the container — `ha core logs` is the only way to access them.
    # The command streams the full log, so we pipe through grep/tail on the remote side.
    if full_dump:
        remote_cmd = "ha core logs"
    elif component_filter:
        remote_cmd = f"ha core logs 2>/dev/null | grep -i '{component_filter}'"
        if extra_filter:
            remote_cmd += f" | grep -i '{extra_filter}'"
        remote_cmd += f" | tail -n {lines}"
    else:
        remote_cmd = "ha core logs 2>/dev/null"
        if extra_filter:
            remote_cmd += f" | grep -i '{extra_filter}'"
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

    parser = argparse.ArgumentParser(description="Fetch Home Assistant logs via SSH")
    parser.add_argument("--lines", "-n", type=int, default=50,
                        help="Number of log lines to fetch (default: 50)")
    parser.add_argument("--all", action="store_true",
                        help="Show all HA log lines, not just climate_advisor")
    parser.add_argument("--filter", "-f", type=str, default="",
                        help="Additional grep filter (e.g. 'ERROR', 'WARNING')")
    parser.add_argument("--full", action="store_true",
                        help="Dump the entire HA log (can be large)")
    parser.add_argument("--save", "-s", action="store_true",
                        help="Save output to logs/ directory")
    args = parser.parse_args()

    config = load_config()

    component = "" if args.all else "climate_advisor"
    output = fetch_logs(
        config,
        lines=args.lines,
        component_filter=component,
        extra_filter=args.filter,
        full_dump=args.full,
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
