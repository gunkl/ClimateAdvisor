#!/usr/bin/env python3
"""Deploy the CA dev-only test fixtures (thermostat sim + weather proxy) to a
Home Assistant OS instance.

DELIBERATELY SEPARATE FROM tools/deploy.py. That script deploys the shipped
custom_components/climate_advisor production integration and must never be
capable of touching dev-only fixtures (or vice versa) — see the invariant
documented in dev_tools/ha_test_integrations/README.md. This script only ever
reads/writes dev_tools/ha_test_integrations/ subdirectories and their remote
custom_components/ca_dev_* counterparts; it has no code path that references
custom_components/climate_advisor at all.

Backs up, deploys, and optionally restarts two dev fixture integrations on a
remote HAOS server via SSH, reusing tools/deploy.py's proven connection-safety
helpers (config loading, SSH arg building, piped tar transfer, atomic
extract-then-swap, StrictHostKeyChecking=accept-new) rather than duplicating
them.

Usage:
    python tools/deploy_dev_sim.py                  # Full deploy
    python tools/deploy_dev_sim.py --dry-run        # Show what would deploy, no changes
    python tools/deploy_dev_sim.py --skip-restart   # Deploy without restarting HA
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

# Reuse deploy.py's proven SSH/config helpers directly rather than duplicating them —
# per the plan, this is preferred over a copy as long as deploy.py's own
# COMPONENT_DIR-shaped globals aren't relied on (this module builds its own remote
# paths and tar payloads instead of calling deploy.py's COMPONENT_DIR-scoped
# functions like remote_path()/deploy_files()/create_backup()/ensure_brand_dir()).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import (  # noqa: E402
    Color,
    _build_component_tar,
    _split_marked_output,
    fail,
    gray,
    info,
    load_config,
    ok,
    resolve_ssh_identity,
    run_local,
    run_ssh,
    run_ssh_piped,
    scp_args,
    setup_logging,
    ssh_target,
    step,
    validate_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_INTEGRATIONS_ROOT = REPO_ROOT / "dev_tools" / "ha_test_integrations"
COMPONENT_DIRS = [
    DEV_INTEGRATIONS_ROOT / "ca_dev_thermostat_sim",
    DEV_INTEGRATIONS_ROOT / "ca_dev_weather_proxy",
]
BACKUP_DIR = REPO_ROOT / "backups"
BACKUP_KEEP_COUNT = 5

_log = None  # set by setup_logging() import side effect (module-level _log in deploy.py)


def remote_path_for(config: dict[str, str], component_dir: Path) -> str:
    """Remote target path for one dev fixture component directory."""
    return f"{config['HA_CONFIG_PATH']}/custom_components/{component_dir.name}"


def create_backup_for(config: dict[str, str], component_dir: Path) -> bool:
    """Back up one remote dev-fixture directory (if it exists) before overwriting it.

    Mirrors deploy.py's create_backup() shape but scoped to a single component_dir /
    remote_path pair, since this script deploys two fixtures per run.
    """
    rpath = remote_path_for(config, component_dir)
    name = component_dir.name
    step(f"Connecting to {config['HA_HOST']}:{config['HA_SSH_PORT']} and preparing backup for {name}")

    cmd = (
        f"if [ -d {shlex.quote(rpath)} ]; then "
        f"tar czf /tmp/ca_dev_backup_{shlex.quote(name)}.tar.gz -C {shlex.quote(rpath)} . && echo TARED; "
        f"else echo NOEXIST; fi; "
        f"mkdir -p {shlex.quote(rpath)}"
    )
    rc, output = run_ssh(config, cmd)
    if "TARED" not in output and "NOEXIST" not in output:
        fail(f"Cannot connect via SSH while backing up {name}. Check .deploy.env and SSH setup.")
        info("See docs/SSH-SETUP.md for configuration instructions.")
        return False
    ok(f"SSH connection successful ({name})")

    if "NOEXIST" in output:
        info(f"No existing installation found for {name}. Skipping backup.")
        return True
    if "TARED" not in output:
        fail(f"Remote tar failed for {name}: {output}")
        return True

    BACKUP_DIR.mkdir(exist_ok=True)
    if sys.platform != "win32":
        os.chmod(BACKUP_DIR, 0o700)
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_tar = BACKUP_DIR / f"{name}-{timestamp}.tar.gz"
    target = ssh_target(config)

    cmd = scp_args(config) + [f"{target}:/tmp/ca_dev_backup_{name}.tar.gz", str(local_tar)]
    rc, output = run_local(cmd)
    if rc != 0:
        fail(f"Backup download failed for {name}: {output}")
        return True

    ok(f"Backup saved: {local_tar}")
    return True


def prune_backups() -> None:
    step(f"Pruning old dev-fixture backups (keeping last {BACKUP_KEEP_COUNT} per fixture)")
    if not BACKUP_DIR.exists():
        ok("No backups directory yet")
        return

    for component_dir in COMPONENT_DIRS:
        name = component_dir.name
        backups = sorted(BACKUP_DIR.glob(f"{name}-*.tar.gz"), reverse=True)
        removed = 0
        for old in backups[BACKUP_KEEP_COUNT:]:
            old.unlink()
            removed += 1
        ok(f"{name}: pruned {removed} old backup(s), {min(len(backups), BACKUP_KEEP_COUNT)} kept")


def deploy_one(config: dict[str, str], component_dir: Path) -> tuple[bool, str]:
    """Tar-and-extract one dev fixture directory to its remote target.

    Same atomic extract-to-temp-then-swap pattern as deploy.py's deploy_files() —
    extraction happens in a temp dir first, and only the final rm+mv (milliseconds)
    touches the live directory, minimizing the window exposed to the HA SSH add-on's
    observed mid-command connection resets.
    """
    name = component_dir.name
    rpath = remote_path_for(config, component_dir)
    step(f"Deploying {name} to {rpath}")

    tar_bytes = _build_component_tar(component_dir)
    local_count = sum(1 for f in component_dir.iterdir() if f.name != "__pycache__")

    tmp_dir = f"/tmp/ca_dev_deploy_tmp_{name}"
    script_steps = [
        f"rm -f /tmp/ca_dev_backup_{name}.tar.gz",
        f"rm -rf {shlex.quote(tmp_dir)}",
        f"mkdir -p {shlex.quote(tmp_dir)}",
        f"tar xzf - -C {shlex.quote(tmp_dir)}",
        f"rm -rf {shlex.quote(rpath)}",
        f"mv {shlex.quote(tmp_dir)} {shlex.quote(rpath)}",
        "echo ___FILES___",
        f"ls -1 {shlex.quote(rpath)} | wc -l",
    ]
    script = " && ".join(script_steps)

    rc, output = run_ssh_piped(config, script, tar_bytes)
    sections = _split_marked_output(output)

    if "FILES" not in sections:
        fail(f"File transfer/extraction failed for {name}")
        if output:
            print(f"   {output.strip()}")
        return False, ""

    remote_count = sections["FILES"].strip()
    ok(f"Deployed {local_count} files to {rpath} (remote reports {remote_count} files)")
    return True, ""


def restart_and_check_logs(config: dict[str, str]) -> bool:
    """Restart HA once (covers both fixtures) and grep the log for ca_dev_thermostat_sim.

    A single restart after both fixtures are deployed, not one per fixture — matches
    the plan's "two tar-and-extract passes" + shared restart/log-tail flow.
    """
    step("Restarting HA and checking logs")
    script = " && ".join(
        [
            "echo ___RESTARTING___",
            "ha core restart",
            "sleep 60",
            "echo ___LOGS___",
            "ha core logs 2>/dev/null | grep -i ca_dev_thermostat_sim | tail -30",
        ]
    )
    rc, output = run_ssh(config, script)
    sections = _split_marked_output(output)

    if "LOGS" not in sections:
        fail("HA core restart did not complete successfully (script stopped before log fetch)")
        if output:
            print(f"   {output.strip()}")
        return False

    ok("HA core restart initiated and wait completed")
    log_output = sections["LOGS"]

    if not log_output.strip():
        info("No log entries found for ca_dev_thermostat_sim yet.")
        return True

    lines = log_output.strip().splitlines()
    error_lines = [line for line in lines if "ERROR" in line]
    if error_lines:
        fail("Errors found in HA logs:")
        for line in error_lines:
            print(f"   {Color.RED}{line}{Color.RESET}")
    else:
        ok("No errors found in recent logs")
        for line in lines[-5:]:
            gray(line)
    return True


def main() -> None:
    if sys.platform == "win32":
        os.system("")

    parser = argparse.ArgumentParser(
        description="Deploy CA dev-only test fixtures (ca_dev_thermostat_sim, ca_dev_weather_proxy) to Home Assistant"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would deploy, without making changes")
    parser.add_argument("--skip-restart", action="store_true", help="Deploy without restarting HA")
    args = parser.parse_args()

    setup_logging()

    config = load_config()
    config_errors = validate_config(config)
    if config_errors:
        for e in config_errors:
            fail(e)
        sys.exit(1)

    print(f"{Color.CYAN}============================================{Color.RESET}")
    print(f"{Color.CYAN}  CA Dev Test Fixture Deployment Tool{Color.RESET}")
    print(f"{Color.CYAN}  (dev_tools/ha_test_integrations only — never{Color.RESET}")
    print(f"{Color.CYAN}  touches custom_components/climate_advisor){Color.RESET}")
    print(f"{Color.CYAN}============================================{Color.RESET}")
    print(f"  Host: {config['HA_HOST']}:{config['HA_SSH_PORT']}")
    for component_dir in COMPONENT_DIRS:
        print(f"  Target: {remote_path_for(config, component_dir)}")

    if args.dry_run:
        print(f"\n{Color.CYAN}============================================{Color.RESET}")
        print(f"{Color.YELLOW}  DRY RUN complete. No changes made.{Color.RESET}")
        print(f"{Color.CYAN}============================================{Color.RESET}")
        for component_dir in COMPONENT_DIRS:
            print(f"\nFiles that would be deployed for {component_dir.name}:")
            for f in sorted(component_dir.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    gray(str(f.relative_to(component_dir)))
        sys.exit(0)

    identity = resolve_ssh_identity(config)
    if identity:
        info(f"Using SSH key: {identity}")
    else:
        info(
            "No SSH key file resolved (HA_SSH_KEY unset, no default identity file found) — "
            "relying on ssh-agent or other auth."
        )

    for component_dir in COMPONENT_DIRS:
        if not create_backup_for(config, component_dir):
            sys.exit(1)
    prune_backups()

    for component_dir in COMPONENT_DIRS:
        success, _ = deploy_one(config, component_dir)
        if not success:
            sys.exit(1)

    if args.skip_restart:
        info("Skipping restart (--skip-restart). Remember to restart HA manually.")
    else:
        if not restart_and_check_logs(config):
            sys.exit(1)

    print(f"\n{Color.GREEN}============================================{Color.RESET}")
    print(f"{Color.GREEN}  Dev fixture deployment complete!{Color.RESET}")
    print(f"{Color.GREEN}============================================{Color.RESET}")


if __name__ == "__main__":
    main()
