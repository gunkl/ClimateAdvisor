#!/usr/bin/env python3
"""Deploy Climate Advisor integration to a Home Assistant OS instance.

Validates, backs up, deploys, and optionally restarts the Climate Advisor
integration on a remote HAOS server via SSH/SCP.

Usage:
    python tools/deploy.py                  # Full deploy
    python tools/deploy.py --dry-run        # Validate only, show what would deploy
    python tools/deploy.py --skip-restart   # Deploy without restarting HA
    python tools/deploy.py --rollback       # Restore most recent backup
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "climate_advisor"
ENV_FILE = REPO_ROOT / ".deploy.env"
BACKUP_KEEP_COUNT = 5


# ---------------------------------------------------------------------------
# Terminal colors (works on Windows 10+ with ANSI support)
# ---------------------------------------------------------------------------

class Color:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    GRAY = "\033[90m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{Color.CYAN}>> {msg}{Color.RESET}")


def ok(msg: str) -> None:
    print(f"   {Color.GREEN}[OK]{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"   {Color.RED}[FAIL]{Color.RESET} {msg}")


def info(msg: str) -> None:
    print(f"   {Color.YELLOW}[INFO]{Color.RESET} {msg}")


def gray(msg: str) -> None:
    print(f"   {Color.GRAY}{msg}{Color.RESET}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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
        print(f"{Color.YELLOW}WARNING: .deploy.env not found. Using defaults.{Color.RESET}")
        print("   Copy .deploy.env.sample to .deploy.env and update with your values.")

    return config


def ssh_args(config: dict[str, str]) -> list[str]:
    """Build SSH command-line arguments."""
    args = ["ssh", "-p", config["HA_SSH_PORT"],
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]
    if config["HA_SSH_KEY"]:
        args.extend(["-i", config["HA_SSH_KEY"]])
    return args


def ssh_target(config: dict[str, str]) -> str:
    return f"{config['HA_SSH_USER']}@{config['HA_HOST']}"


def scp_args(config: dict[str, str]) -> list[str]:
    """Build SCP command-line arguments."""
    args = ["scp", "-P", config["HA_SSH_PORT"],
            "-o", "StrictHostKeyChecking=no", "-r"]
    if config["HA_SSH_KEY"]:
        args.extend(["-i", config["HA_SSH_KEY"]])
    return args


def remote_path(config: dict[str, str]) -> str:
    return f"{config['HA_CONFIG_PATH']}/custom_components/climate_advisor"


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def run_ssh(config: dict[str, str], command: str) -> tuple[int, str]:
    """Run a command on the remote server via SSH. Returns (returncode, output)."""
    cmd = ssh_args(config) + [ssh_target(config), command]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def run_local(command: list[str]) -> tuple[int, str]:
    """Run a local command. Returns (returncode, output)."""
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


# ---------------------------------------------------------------------------
# Deploy steps
# ---------------------------------------------------------------------------

def test_ssh(config: dict[str, str]) -> bool:
    step(f"Testing SSH connection to {config['HA_HOST']}:{config['HA_SSH_PORT']}")
    rc, output = run_ssh(config, "echo ok")
    if rc == 0 and "ok" in output:
        ok("SSH connection successful")
        return True
    fail("Cannot connect via SSH. Check .deploy.env and SSH setup.")
    info("See docs/SSH-SETUP.md for configuration instructions.")
    return False


def run_validation() -> bool:
    step("Running pre-deploy validation")
    validate_script = str(REPO_ROOT / "tools" / "validate.py")
    rc = subprocess.run([sys.executable, validate_script]).returncode
    if rc != 0:
        fail("Validation failed. Fix errors before deploying.")
        return False
    ok("All validation checks passed")
    return True


def create_backup(config: dict[str, str]) -> None:
    step("Creating backup on HA server")
    rpath = remote_path(config)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{rpath}.bak.{timestamp}"

    rc, output = run_ssh(config, f"test -d '{rpath}' && echo yes || echo no")
    if "yes" in output:
        run_ssh(config, f"cp -r '{rpath}' '{backup_path}'")
        ok(f"Backup created: {backup_path}")
    else:
        info("No existing installation found. Skipping backup.")


def prune_backups(config: dict[str, str]) -> None:
    step(f"Pruning old backups (keeping last {BACKUP_KEEP_COUNT})")
    rpath = remote_path(config)
    cmd = (
        f"ls -1d {rpath}.bak.* 2>/dev/null | sort -r | "
        f"tail -n +{BACKUP_KEEP_COUNT + 1} | xargs rm -rf 2>/dev/null; echo done"
    )
    run_ssh(config, cmd)
    ok("Old backups pruned")


def deploy_files(config: dict[str, str]) -> bool:
    step("Deploying files to HA server")
    rpath = remote_path(config)
    target = ssh_target(config)

    # Ensure remote directory exists
    run_ssh(config, f"mkdir -p '{rpath}'")

    # Copy files
    local_files = [str(f) for f in COMPONENT_DIR.iterdir() if f.is_file()]
    local_count = len(local_files)

    cmd = scp_args(config) + local_files + [f"{target}:{rpath}/"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail("File copy failed")
        if result.stderr:
            print(f"   {result.stderr.strip()}")
        return False

    # Verify
    rc, output = run_ssh(config, f"ls -1 '{rpath}' | wc -l")
    remote_count = output.strip()
    ok(f"Deployed {local_count} files to {rpath} (remote has {remote_count} files)")
    return True


def restart_ha(config: dict[str, str], skip: bool = False) -> None:
    if skip:
        info("Skipping restart (--skip-restart). Remember to restart HA manually.")
        return

    step("Restarting Home Assistant core")
    run_ssh(config, "ha core restart")
    ok("HA core restart initiated")

    step("Waiting 60 seconds for HA to restart")
    for i in range(60, 0, -10):
        print(f"   {Color.GRAY}{i}s remaining...{Color.RESET}", end="\r")
        time.sleep(10)
    print("   " + " " * 30)  # clear the countdown line


def check_logs(config: dict[str, str]) -> None:
    step("Checking HA logs for errors")
    log_path = f"{config['HA_CONFIG_PATH']}/home-assistant.log"
    rc, output = run_ssh(config, f"grep -i 'climate_advisor' '{log_path}' 2>/dev/null | tail -30")

    if not output:
        info("No log entries found for climate_advisor yet.")
        return

    lines = output.splitlines()
    error_lines = [line for line in lines if "ERROR" in line]

    if error_lines:
        fail("Errors found in HA logs:")
        for line in error_lines:
            print(f"   {Color.RED}{line}{Color.RESET}")
        info("Consider running: python tools/deploy.py --rollback")
    else:
        ok("No errors found in recent logs")
        for line in lines[-5:]:
            gray(line)


def do_rollback(config: dict[str, str]) -> None:
    step("Listing available backups")

    if not test_ssh(config):
        sys.exit(1)

    rpath = remote_path(config)
    rc, output = run_ssh(config, f"ls -1d {rpath}.bak.* 2>/dev/null | sort -r")

    if rc != 0 or not output or "No such file" in output:
        fail("No backups found on server")
        sys.exit(1)

    backups = [b.strip() for b in output.splitlines() if b.strip()]
    info("Available backups:")
    for i, b in enumerate(backups):
        name = b.rsplit("/", 1)[-1]
        print(f"   [{i}] {name}")

    latest = backups[0]
    name = latest.rsplit("/", 1)[-1]
    step(f"Restoring from: {name}")

    run_ssh(config, f"rm -rf '{rpath}' && cp -r '{latest}' '{rpath}'")
    ok("Backup restored")

    step("Restarting Home Assistant core")
    run_ssh(config, "ha core restart")
    ok("HA core restart initiated after rollback")

    step("Waiting 60 seconds for HA to restart")
    for i in range(60, 0, -10):
        print(f"   {Color.GRAY}{i}s remaining...{Color.RESET}", end="\r")
        time.sleep(10)
    print("   " + " " * 30)

    check_logs(config)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Enable ANSI colors on Windows
    if sys.platform == "win32":
        os.system("")

    parser = argparse.ArgumentParser(description="Deploy Climate Advisor to Home Assistant")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, show what would deploy")
    parser.add_argument("--skip-restart", action="store_true", help="Deploy without restarting HA")
    parser.add_argument("--rollback", action="store_true", help="Restore most recent backup")
    args = parser.parse_args()

    config = load_config()
    rpath = remote_path(config)

    print(f"{Color.CYAN}============================================{Color.RESET}")
    print(f"{Color.CYAN}  Climate Advisor Deployment Tool{Color.RESET}")
    print(f"{Color.CYAN}============================================{Color.RESET}")
    print(f"  Host: {config['HA_HOST']}:{config['HA_SSH_PORT']}")
    print(f"  Target: {rpath}")

    if args.rollback:
        do_rollback(config)
        sys.exit(0)

    # Step 1: Validate
    if not run_validation():
        sys.exit(1)

    if args.dry_run:
        print(f"\n{Color.CYAN}============================================{Color.RESET}")
        print(f"{Color.YELLOW}  DRY RUN complete. No changes made.{Color.RESET}")
        print(f"{Color.CYAN}============================================{Color.RESET}")
        print("\nFiles that would be deployed:")
        for f in sorted(COMPONENT_DIR.iterdir()):
            if f.is_file():
                gray(f.name)
        sys.exit(0)

    # Step 2: Test SSH
    if not test_ssh(config):
        sys.exit(1)

    # Step 3: Backup
    create_backup(config)
    prune_backups(config)

    # Step 4: Deploy
    if not deploy_files(config):
        sys.exit(1)

    # Step 5: Restart
    restart_ha(config, skip=args.skip_restart)

    # Step 6: Verify
    if not args.skip_restart:
        check_logs(config)

    print(f"\n{Color.GREEN}============================================{Color.RESET}")
    print(f"{Color.GREEN}  Deployment complete!{Color.RESET}")
    print(f"{Color.GREEN}============================================{Color.RESET}")


if __name__ == "__main__":
    main()
