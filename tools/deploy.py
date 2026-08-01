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
import io
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
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


def run_ssh_piped(config: dict[str, str], command: str, input_bytes: bytes) -> tuple[int, str]:
    """Run a command on the remote server via SSH, piping input_bytes to its stdin.

    Issue #553: used to transfer the component directory as a tar stream through the same
    SSH connection that also runs extraction/restart/verification, instead of a separate
    `scp` connection — the HA SSH add-on's rate-limit protection can block a source IP
    after just a handful of connections in a short window (see docs/SSH-SETUP.md), so a
    full deploy needs to fit in as few real connections as possible.
    """
    cmd = ssh_args(config) + [ssh_target(config), command]
    _log.debug("SSH (piped, %d bytes stdin) cmd: %s", len(input_bytes), " ".join(cmd))
    result = subprocess.run(cmd, input=input_bytes, capture_output=True)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    _log.debug("SSH rc=%d stdout=%r stderr=%r", result.returncode, stdout.strip(), stderr.strip())
    return result.returncode, (stdout + stderr).strip()


def _build_component_tar(component_dir: Path) -> bytes:
    """Build an in-memory gzip tar of component_dir's immediate contents, excluding
    __pycache__. Piped through run_ssh_piped()'s stdin so the deploy payload travels over
    the same SSH connection that extracts/restarts/verifies, instead of a separate `scp`.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for item in sorted(component_dir.iterdir()):
            if item.name == "__pycache__":
                continue
            tf.add(item, arcname=item.name)
    return buf.getvalue()


def _split_marked_output(output: str) -> dict[str, str]:
    """Split combined remote-script stdout into named sections delimited by ___MARKER___
    lines (e.g. "___FILES___", "___LOGS___"). Lines before the first marker are dropped.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in output.splitlines():
        if line.startswith("___") and line.endswith("___") and len(line) > 6:
            if current is not None:
                sections[current] = "\n".join(buf)
            current = line.strip("_")
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


# ---------------------------------------------------------------------------
# Deploy steps
# ---------------------------------------------------------------------------


def run_validation() -> bool:
    step("Running pre-deploy validation")
    validate_script = str(REPO_ROOT / "tools" / "validate.py")
    rc = subprocess.run([sys.executable, validate_script]).returncode
    if rc != 0:
        fail("Validation failed. Fix errors before deploying.")
        return False
    ok("All validation checks passed")
    return True


def create_backup(config: dict[str, str]) -> bool:
    """Connection 1: connect + server-side backup tar + legacy-backup cleanup + mkdir,
    combined into one SSH call. Connection 2 (scp): download that backup tar locally, if
    one was created.

    Issue #553: this also serves as the connectivity test — there's no separate "echo ok"
    call. A full deploy now costs at most 3 real connections total: this function's 1-2,
    plus deploy_files()'s 1 (transfer + extract + restart + verify, all piped through a
    single ssh connection's stdin).

    Legacy climate_advisor.bak.* directories contain manifest.json files that cause HA's
    loader to discover them as duplicate integrations, breaking import — removed
    unconditionally if any are found.

    Returns True if the SSH connection itself succeeded (regardless of whether a backup
    existed to create), False if the connection failed.
    """
    step(f"Connecting to {config['HA_HOST']}:{config['HA_SSH_PORT']} and preparing backup")
    identity = resolve_ssh_identity(config)
    if identity:
        info(f"Using SSH key: {identity}")
    else:
        info(
            "No SSH key file resolved (HA_SSH_KEY unset, no default identity file found) — "
            "relying on ssh-agent or other auth."
        )

    rpath = remote_path(config)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Note: the backup step uses if/then/else/fi (not && / || shorthand) so a tar failure
    # (dir exists but tar errors) is distinguishable from "no existing install" — with
    # && / || shorthand, both cases fall through to the same branch, silently misreporting
    # a real tar failure as "nothing to back up". Joined with `;`, not `&&`, to the cleanup
    # and mkdir steps so those still run even if the backup step itself failed.
    #
    # Tar is built flat (-C rpath . — contents of the directory, no wrapping
    # climate_advisor/ level) to match _build_component_tar()'s layout, since
    # do_rollback() extracts a backup tar the same way it extracts a deploy tar
    # (Issue #553). A wrapped tar wrongly produced climate_advisor/climate_advisor/
    # when swapped into place — caught during this fix's live validation.
    cmd = (
        f"if [ -d {shlex.quote(rpath)} ]; then "
        f"tar czf /tmp/ca_backup.tar.gz -C {shlex.quote(rpath)} . && echo TARED; "
        f"else echo NOEXIST; fi; "
        f"ls -1d {shlex.quote(rpath)}.bak.* 2>/dev/null | xargs -r rm -rf; "
        f"mkdir -p {shlex.quote(rpath)}"
    )
    rc, output = run_ssh(config, cmd)
    if "TARED" not in output and "NOEXIST" not in output:
        fail("Cannot connect via SSH. Check .deploy.env and SSH setup.")
        info("See docs/SSH-SETUP.md for configuration instructions.")
        return False
    ok("SSH connection successful")

    if "NOEXIST" in output:
        info("No existing installation found. Skipping backup.")
        return True
    if "TARED" not in output:
        fail(f"Remote tar failed: {output}")
        return True

    BACKUP_DIR.mkdir(exist_ok=True)
    if sys.platform != "win32":
        os.chmod(BACKUP_DIR, 0o700)
    local_tar = BACKUP_DIR / f"climate_advisor-{timestamp}.tar.gz"
    target = ssh_target(config)

    cmd = scp_args(config) + [f"{target}:/tmp/ca_backup.tar.gz", str(local_tar)]
    rc, output = run_local(cmd)
    if rc != 0:
        fail(f"Backup download failed: {output}")
        return True

    ok(f"Backup saved: {local_tar}")
    return True


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


def deploy_files(config: dict[str, str], skip_restart: bool) -> tuple[bool, str]:
    """Connection 3 (final connection of a full deploy): pipe the component directory as a
    tar stream through one ssh connection's stdin, extract it remotely, verify the file
    count, and — unless skip_restart — restart HA core, wait for it, and fetch its log
    tail, all within that same single connection/script (Issue #553).

    Extracts into a temp directory first, then does rm-rf-the-old + mv-the-new-into-place
    as the last step (not extract-directly-on-top-of-the-live-directory): this project's
    HA SSH add-on has been observed to reset SSH connections mid-command under its
    rate-limit protection (see docs/SSH-SETUP.md), and tar extraction of the several-MB
    payload measurably takes ~20s — long enough to be a real window for that. Extracting
    to a temp dir keeps that whole window off the live directory; only the final rm+mv
    (milliseconds) touches it. This also means the deployed directory always exactly
    matches the source tree (no more stale files left over from a previous version that
    no longer exist in the current one, e.g. renamed/removed files — extract-on-top never
    cleaned those up).

    Returns (success, log_output) — log_output is the captured HA log tail when restart
    wasn't skipped, else "".
    """
    step("Deploying files to HA server")
    rpath = remote_path(config)

    # Ensure brand/ dir has icon + logo for HA's Add Integration dialog
    ensure_brand_dir()

    tar_bytes = _build_component_tar(COMPONENT_DIR)
    local_count = sum(1 for f in COMPONENT_DIR.iterdir() if f.name != "__pycache__")

    script_steps = [
        "rm -f /tmp/ca_backup.tar.gz",
        "rm -rf /tmp/ca_deploy_tmp",
        "mkdir -p /tmp/ca_deploy_tmp",
        "tar xzf - -C /tmp/ca_deploy_tmp",
        f"rm -rf {shlex.quote(rpath)}",
        f"mv /tmp/ca_deploy_tmp {shlex.quote(rpath)}",
        "echo ___FILES___",
        f"ls -1 {shlex.quote(rpath)} | wc -l",
    ]
    if not skip_restart:
        script_steps += [
            "echo ___RESTARTING___",
            "ha core restart",
            "sleep 60",
            "echo ___LOGS___",
            "ha core logs 2>/dev/null | grep -i climate_advisor | tail -30",
        ]
    script = " && ".join(script_steps)

    if not skip_restart:
        info("Transferring files, extracting, restarting HA, and waiting ~60s — please wait...")

    rc, output = run_ssh_piped(config, script, tar_bytes)
    sections = _split_marked_output(output)

    if "FILES" not in sections:
        _log.error("SSH (piped) failed: rc=%d output=%s", rc, output)
        fail("File transfer/extraction failed")
        if output:
            print(f"   {output.strip()}")
        return False, ""

    remote_count = sections["FILES"].strip()
    ok(f"Deployed {local_count} files to {rpath} (remote reports {remote_count} files)")

    if skip_restart:
        info("Skipping restart (--skip-restart). Remember to restart HA manually.")
        return True, ""

    if "LOGS" not in sections:
        fail("HA core restart did not complete successfully (script stopped before log fetch)")
        if output:
            print(f"   {output.strip()}")
        return True, ""

    ok("HA core restart initiated and wait completed")
    return True, sections["LOGS"]


def check_logs(log_output: str) -> None:
    """Report on an already-captured HA log tail (Issue #553: log fetching now happens
    server-side as part of deploy_files()'s/do_rollback()'s single connection, not a
    separate ssh call — this function just interprets the text it's given).
    """
    step("Checking HA logs for errors")

    if not log_output.strip():
        info("No log entries found for climate_advisor yet.")
        return

    lines = log_output.strip().splitlines()
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
    """Restore the most recent local backup — one connection total (Issue #553): the
    chosen backup's tar bytes are already sitting locally, so upload + extract + restart
    + wait + log-fetch all pipe through one ssh connection's stdin, no separate scp.

    The destructive-action confirmation prompt happens before that connection opens
    (previously it was after an initial upload) — nothing touches the network until
    after you've confirmed, which is strictly safer than before, not just different.
    """
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

    latest = backups[0]

    resp = input(f"   This will DELETE the current installation and restore from {latest.name}. Continue? [y/N] ")
    if resp.strip().lower() != "y":
        info("Rollback cancelled.")
        return

    step(f"Restoring from: {latest.name}")
    identity = resolve_ssh_identity(config)
    if identity:
        info(f"Using SSH key: {identity}")

    rpath = remote_path(config)
    tar_bytes = latest.read_bytes()

    # Extract into a temp dir first, only rm+mv the live directory as the final,
    # near-instant step — see deploy_files()'s docstring for why (this project's SSH
    # add-on has been observed to reset connections mid-command; a multi-second tar
    # extraction is a real window for that to land badly on the live directory).
    script = " && ".join(
        [
            "rm -rf /tmp/ca_restore_tmp",
            "mkdir -p /tmp/ca_restore_tmp",
            "tar xzf - -C /tmp/ca_restore_tmp",
            f"rm -rf {shlex.quote(rpath)}",
            f"mv /tmp/ca_restore_tmp {shlex.quote(rpath)}",
            "echo ___RESTARTING___",
            "ha core restart",
            "sleep 60",
            "echo ___LOGS___",
            "ha core logs 2>/dev/null | grep -i climate_advisor | tail -30",
        ]
    )

    info("Uploading, extracting, restarting HA, and waiting ~60s — please wait...")
    rc, output = run_ssh_piped(config, script, tar_bytes)
    sections = _split_marked_output(output)

    if "LOGS" not in sections:
        fail(f"Rollback did not complete successfully: {output}")
        sys.exit(1)

    ok("Backup restored, HA core restart initiated")
    check_logs(sections["LOGS"])


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

    # Step 2: Connect + backup (Issue #553: connections 1-2 of at most 3 total — see
    # create_backup()'s docstring). Also serves as the connectivity test.
    if not create_backup(config):
        sys.exit(1)
    prune_backups(config)

    # Step 3: Deploy — connection 3: transfer + extract + verify + restart + wait +
    # log-fetch, all in one connection (Issue #553 — see deploy_files()'s docstring)
    success, log_output = deploy_files(config, skip_restart=args.skip_restart)
    if not success:
        sys.exit(1)

    # Step 4: Verify
    if not args.skip_restart:
        check_logs(log_output)

    print(f"\n{Color.GREEN}============================================{Color.RESET}")
    print(f"{Color.GREEN}  Deployment complete!{Color.RESET}")
    print(f"{Color.GREEN}============================================{Color.RESET}")


if __name__ == "__main__":
    main()
