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
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "climate_advisor"
ENV_FILE = REPO_ROOT / ".deploy.env"
LOG_DIR = REPO_ROOT / "logs"
BACKUP_DIR = REPO_ROOT / "backups"
BACKUP_KEEP_COUNT = 5

_log = logging.getLogger("deploy")
_log_path: Path | None = None


def setup_logging() -> Path:
    """Configure file logging. Returns the log file path."""
    global _log_path
    LOG_DIR.mkdir(exist_ok=True)
    if sys.platform != "win32":
        os.chmod(LOG_DIR, 0o700)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _log_path = LOG_DIR / f"deploy-{timestamp}.log"

    handler = logging.FileHandler(_log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    _log.setLevel(logging.DEBUG)
    _log.addHandler(handler)
    _log.info("Deploy log started: %s", _log_path)
    return _log_path


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
    if _log_path:
        print(f"   {Color.YELLOW}[LOG]{Color.RESET} See {_log_path}")


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


def validate_config(config: dict[str, str]) -> list[str]:
    """Validate deployment configuration values. Returns list of error messages."""
    errors = []
    # Port must be numeric 1-65535
    try:
        port = int(config["HA_SSH_PORT"])
        if not 1 <= port <= 65535:
            errors.append(f"HA_SSH_PORT out of range: {port}")
    except ValueError:
        errors.append(f"HA_SSH_PORT must be numeric, got: {config['HA_SSH_PORT']}")
    # Hostname: alphanumeric, dots, hyphens only
    if not re.match(r"^[a-zA-Z0-9._-]+$", config["HA_HOST"]):
        errors.append(f"HA_HOST contains invalid characters: {config['HA_HOST']}")
    # Config path must be absolute
    if not config["HA_CONFIG_PATH"].startswith("/"):
        errors.append(f"HA_CONFIG_PATH must be absolute, got: {config['HA_CONFIG_PATH']}")
    # SSH key must exist if specified
    key = config.get("HA_SSH_KEY", "")
    if key and not Path(key).expanduser().exists():
        errors.append(f"HA_SSH_KEY file not found: {key}")
    return errors


def ssh_args(config: dict[str, str]) -> list[str]:
    """Build SSH command-line arguments."""
    args = ["ssh", "-p", config["HA_SSH_PORT"], "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if config["HA_SSH_KEY"]:
        args.extend(["-i", config["HA_SSH_KEY"]])
    if config["HA_SSH_KEY"] and sys.platform != "win32":
        key_path = Path(config["HA_SSH_KEY"])
        if key_path.exists() and key_path.stat().st_mode & 0o077:
            _log.warning(
                "SSH key %s has permissive permissions (%s) — recommend chmod 600",
                key_path,
                oct(key_path.stat().st_mode & 0o777),
            )
    return args


def ssh_target(config: dict[str, str]) -> str:
    return f"{config['HA_SSH_USER']}@{config['HA_HOST']}"


def scp_args(config: dict[str, str]) -> list[str]:
    """Build SCP command-line arguments."""
    args = ["scp", "-P", config["HA_SSH_PORT"], "-o", "StrictHostKeyChecking=accept-new", "-r"]
    if config["HA_SSH_KEY"]:
        args.extend(["-i", config["HA_SSH_KEY"]])
    return args


def remote_path(config: dict[str, str]) -> str:
    return f"{config['HA_CONFIG_PATH']}/custom_components/climate_advisor"


def resolve_ssh_identity(config: dict[str, str]) -> str | None:
    """Locally resolve which SSH identity file would be used, without connecting.

    Issue #547: on Windows, two different ssh.exe binaries (Git's MSYS build and
    Windows' native OpenSSH client) commonly sit on PATH and can resolve default
    identities differently depending on invocation context, making key-related
    connection failures hard to diagnose. This surfaces the answer up front —
    `ssh -G` only resolves config locally, it never opens a network connection.
    Returns None if resolution fails or no candidate identity file exists on disk.
    """
    if config["HA_SSH_KEY"]:
        return config["HA_SSH_KEY"]

    result = subprocess.run(["ssh", "-G", ssh_target(config)], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("identityfile "):
            candidate = line.split(None, 1)[1]
            if Path(candidate).expanduser().exists():
                return candidate
    return None


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def run_ssh(config: dict[str, str], command: str) -> tuple[int, str]:
    """Run a command on the remote server via SSH. Returns (returncode, output)."""
    cmd = ssh_args(config) + [ssh_target(config), command]
    _log.debug("SSH cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    _log.debug("SSH rc=%d stdout=%r stderr=%r", result.returncode, result.stdout.strip(), result.stderr.strip())
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def run_local(command: list[str]) -> tuple[int, str]:
    """Run a local command. Returns (returncode, output)."""
    _log.debug("Local cmd: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    _log.debug("Local rc=%d stdout=%r stderr=%r", result.returncode, result.stdout.strip(), result.stderr.strip())
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


# ---------------------------------------------------------------------------
# Deploy steps
# ---------------------------------------------------------------------------


def test_ssh(config: dict[str, str]) -> bool:
    step(f"Testing SSH connection to {config['HA_HOST']}:{config['HA_SSH_PORT']}")
    identity = resolve_ssh_identity(config)
    if identity:
        info(f"Using SSH key: {identity}")
    else:
        info(
            "No SSH key file resolved (HA_SSH_KEY unset, no default identity file found) — "
            "relying on ssh-agent or other auth."
        )
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
    step("Downloading backup from HA server")
    rpath = remote_path(config)
    parent = f"{config['HA_CONFIG_PATH']}/custom_components"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Issue #551: combine the existence check and the tar creation into one round trip
    # (reduces connection count — see docs/SSH-SETUP.md's rate-limit note). Cleanup of
    # /tmp/ca_backup.tar.gz happens later, batched into prep_remote_target().
    cmd = (
        f"if [ -d {shlex.quote(rpath)} ]; then "
        f"tar czf /tmp/ca_backup.tar.gz -C {shlex.quote(parent)} climate_advisor && echo TARED; "
        f"else echo NOEXIST; fi"
    )
    rc, output = run_ssh(config, cmd)
    if "NOEXIST" in output:
        info("No existing installation found. Skipping backup.")
        return
    if "TARED" not in output:
        fail(f"Remote tar failed: {output}")
        return

    BACKUP_DIR.mkdir(exist_ok=True)
    if sys.platform != "win32":
        os.chmod(BACKUP_DIR, 0o700)
    local_tar = BACKUP_DIR / f"climate_advisor-{timestamp}.tar.gz"
    target = ssh_target(config)

    cmd = scp_args(config) + [f"{target}:/tmp/ca_backup.tar.gz", str(local_tar)]
    rc, output = run_local(cmd)
    if rc != 0:
        fail(f"Backup download failed: {output}")
        return

    ok(f"Backup saved: {local_tar}")


def prune_backups(config: dict[str, str]) -> None:
    step(f"Pruning old backups (keeping last {BACKUP_KEEP_COUNT})")
    if not BACKUP_DIR.exists():
        ok("No backups directory yet")
        return

    backups = sorted(BACKUP_DIR.glob("climate_advisor-*.tar.gz"), reverse=True)
    removed = 0
    for old in backups[BACKUP_KEEP_COUNT:]:
        old.unlink()
        removed += 1
    ok(f"Pruned {removed} old backup(s), {min(len(backups), BACKUP_KEEP_COUNT)} kept")


def prep_remote_target(config: dict[str, str]) -> None:
    """Clean up temp/legacy state and ensure the target directory exists — one round trip.

    Issue #551: combines what used to be 3+ separate SSH connections (temp backup-tar
    cleanup, legacy climate_advisor.bak.* directory removal, mkdir -p) into a single
    command, to reduce the total connection count deploy.py opens per run — the HA SSH
    add-on's rate-limit protection can block a source IP after several connections in a
    short window (see docs/SSH-SETUP.md).

    Legacy .bak.* directories contain manifest.json files that cause HA's loader to
    discover them as duplicate integrations, breaking import — removed unconditionally
    if any are found.
    """
    rpath = remote_path(config)
    cmd = (
        f"rm -f /tmp/ca_backup.tar.gz; "
        f"ls -1d {shlex.quote(rpath)}.bak.* 2>/dev/null | xargs -r rm -rf; "
        f"mkdir -p {shlex.quote(rpath)}"
    )
    run_ssh(config, cmd)


def ensure_brand_dir() -> None:
    """Populate the brand/ subdirectory with icon and logo files.

    HA 2026.3+ serves custom-integration brand images from a brand/
    subdirectory inside the integration folder.  icon.png is square
    (256/512px); logo.png can be the same image if no landscape
    variant is provided.
    """
    import shutil

    brand_dir = COMPONENT_DIR / "brand"
    brand_dir.mkdir(exist_ok=True)

    for suffix in ("", "@2x"):
        icon = COMPONENT_DIR / f"icon{suffix}.png"
        if not icon.exists():
            continue
        for name in (f"icon{suffix}.png", f"logo{suffix}.png"):
            dest = brand_dir / name
            if not dest.exists():
                shutil.copy2(icon, dest)
                ok(f"Created brand/{name} from {icon.name}")


def deploy_files(config: dict[str, str]) -> bool:
    step("Deploying files to HA server")
    rpath = remote_path(config)
    target = ssh_target(config)

    # Ensure brand/ dir has icon + logo for HA's Add Integration dialog
    ensure_brand_dir()

    # Remote target directory already ensured by prep_remote_target() (Issue #551)

    # Copy files and subdirectories (e.g. brand/), excluding __pycache__
    local_items = [str(f) for f in COMPONENT_DIR.iterdir() if (f.is_file() or f.is_dir()) and f.name != "__pycache__"]
    local_count = len(local_items)

    cmd = scp_args(config) + local_items + [f"{target}:{rpath}/"]
    _log.debug("SCP cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    _log.debug("SCP rc=%d stdout=%r stderr=%r", result.returncode, result.stdout.strip(), result.stderr.strip())
    if result.returncode != 0:
        _log.error("SCP failed: rc=%d stderr=%s", result.returncode, result.stderr.strip())
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
    rc, output = run_ssh(config, "ha core logs 2>/dev/null | grep -i 'climate_advisor' | tail -30")

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
    step("Listing available local backups")

    if not BACKUP_DIR.exists():
        fail("No backups/ directory found")
        sys.exit(1)

    backups = sorted(BACKUP_DIR.glob("climate_advisor-*.tar.gz"), reverse=True)
    if not backups:
        fail("No backup tarballs found in backups/")
        sys.exit(1)

    info("Available backups:")
    for i, b in enumerate(backups):
        print(f"   [{i}] {b.name}")

    if not test_ssh(config):
        sys.exit(1)

    latest = backups[0]
    step(f"Restoring from: {latest.name}")

    rpath = remote_path(config)
    target = ssh_target(config)
    parent = f"{config['HA_CONFIG_PATH']}/custom_components"

    # Upload tarball and extract on server
    cmd = scp_args(config) + [str(latest), f"{target}:/tmp/ca_restore.tar.gz"]
    rc, output = run_local(cmd)
    if rc != 0:
        fail(f"Upload failed: {output}")
        sys.exit(1)

    resp = input(f"   This will DELETE the current installation and restore from {latest.name}. Continue? [y/N] ")
    if resp.strip().lower() != "y":
        info("Rollback cancelled.")
        return

    # Issue #551: combined into one round trip (reduces connection count)
    run_ssh(
        config,
        f"rm -rf {shlex.quote(rpath)} && tar xzf /tmp/ca_restore.tar.gz -C {shlex.quote(parent)}; "
        f"rm -f /tmp/ca_restore.tar.gz",
    )
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

    setup_logging()

    config = load_config()
    config_errors = validate_config(config)
    if config_errors:
        for e in config_errors:
            fail(e)
        sys.exit(1)
    rpath = remote_path(config)

    _log.info(
        "Config: host=%s port=%s user=%s target=%s",
        config["HA_HOST"],
        config["HA_SSH_PORT"],
        config["HA_SSH_USER"],
        remote_path(config),
    )

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
        ensure_brand_dir()
        print(f"\n{Color.CYAN}============================================{Color.RESET}")
        print(f"{Color.YELLOW}  DRY RUN complete. No changes made.{Color.RESET}")
        print(f"{Color.CYAN}============================================{Color.RESET}")
        print("\nFiles that would be deployed:")
        for f in sorted(COMPONENT_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                gray(str(f.relative_to(COMPONENT_DIR)))
        sys.exit(0)

    # Step 2: Test SSH
    if not test_ssh(config):
        sys.exit(1)

    # Step 3: Backup + remote prep (Issue #551: batched into few round trips —
    # see prep_remote_target()/create_backup())
    create_backup(config)
    prune_backups(config)
    prep_remote_target(config)

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
