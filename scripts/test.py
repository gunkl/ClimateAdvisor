#!/usr/bin/env python3
"""Single entry point for the Climate Advisor test suites (Issue #1126).

    python scripts/test.py [unit|ha|all] [pytest args...]

* ``unit``: ``tests/`` against stubbed Home Assistant (dependency group ``test``).
* ``ha``:   ``tests_ha/`` against the real Home Assistant (dependency group ``test-ha``).
* ``all``:  both, in that order (default).

Environment knobs for the ha tier:

* ``CA_HA_WORKERS``: number of pytest-xdist workers (``auto`` for all cores). Unset or ``0``
  runs serially (the local default; CI sets ``auto``).
* ``CA_COMPONENT_ROOT``: directory holding an alternate ``custom_components/climate_advisor``
  tree. Used by ``scripts/revert_proof.py`` to run the tests against an old commit; a Windows
  path is translated for WSL.

The two tiers can never share an environment (the real-HA plugin auto-loads into any
pytest run, while ``tests/conftest.py`` stubs homeassistant), so each gets its own
uv-managed venv. Real Home Assistant imports the Unix-only ``fcntl``; on Windows the ha
tier therefore runs inside WSL (distro from ``CA_WSL_DISTRO``, default ``Ubuntu``).

Standard library only. Extra arguments are passed through to pytest.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIERS = ("unit", "ha", "all")

UNIT_ARGS = ["-W", "error::pytest.PytestUnraisableExceptionWarning", "-W", "error::RuntimeWarning"]
HA_ARGS = ["-c", "tests_ha/pytest.ini", "--rootdir=tests_ha"]

UV_INSTALL_WINDOWS = "python -m pip install --user uv   (or: winget install --id=astral-sh.uv -e)"
UV_INSTALL_UNIX = "curl -LsSf https://astral.sh/uv/install.sh | sh"


def _uv_cmd() -> list[str] | None:
    """Return the command prefix that runs uv, or None when uv is not installed."""
    exe = shutil.which("uv")
    if exe:
        return [exe]
    # `pip install --user uv` may leave the scripts dir off PATH; the module still works.
    probe = subprocess.run([sys.executable, "-m", "uv", "--version"], capture_output=True, check=False)
    if probe.returncode == 0:
        return [sys.executable, "-m", "uv"]
    return None


def _run_local(group: str, venv: str, pytest_args: list[str]) -> int:
    uv = _uv_cmd()
    if uv is None:
        install = UV_INSTALL_WINDOWS if os.name == "nt" else UV_INSTALL_UNIX
        print(f"uv is not installed. Install it with: {install}", file=sys.stderr)
        return 2
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(REPO / venv)}
    cmd = [*uv, "run", "--locked", "--only-group", group, "pytest", *pytest_args]
    print(f"+ [{venv}] {shlex.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=REPO, env=env, check=False).returncode


def _ha_env_passthrough() -> dict[str, str]:
    """Env vars that steer the ha tier (see module docstring)."""
    return {k: os.environ[k] for k in ("CA_COMPONENT_ROOT",) if os.environ.get(k)}


def _worker_args() -> list[str]:
    workers = os.environ.get("CA_HA_WORKERS", "").strip()
    return [] if workers in ("", "0") else ["-n", workers]


def _run_ha_in_wsl(pytest_args: list[str]) -> int:
    if shutil.which("wsl") is None:
        print(
            "The real-HA tier cannot run natively on Windows (Home Assistant imports the Unix-only "
            "'fcntl') and WSL is not available. Install WSL: wsl --install -d Ubuntu",
            file=sys.stderr,
        )
        return 2
    distro = os.environ.get("CA_WSL_DISTRO", "Ubuntu")
    probe = subprocess.run(["wsl", "-d", distro, "--", "true"], capture_output=True, check=False)
    if probe.returncode != 0:
        print(
            f"WSL distro '{distro}' is not usable (real Home Assistant needs Linux: it imports 'fcntl'). "
            "Install it with `wsl --install -d Ubuntu` or point CA_WSL_DISTRO at an existing distro.",
            file=sys.stderr,
        )
        return 2
    conv = subprocess.run(
        ["wsl", "-d", distro, "--", "wslpath", "-a", str(REPO).replace("\\", "/")],
        capture_output=True,
        text=True,
        check=False,
    )
    wsl_repo = conv.stdout.strip()
    if conv.returncode != 0 or not wsl_repo:
        print(f"Could not translate {REPO} with wslpath in distro '{distro}'.", file=sys.stderr)
        return 2
    exports = ""
    for key, value in _ha_env_passthrough().items():
        if key == "CA_COMPONENT_ROOT":
            tr = subprocess.run(
                ["wsl", "-d", distro, "--", "wslpath", "-a", value.replace("\\", "/")],
                capture_output=True,
                text=True,
                check=False,
            )
            value = tr.stdout.strip() or value
        exports += f"export {key}={shlex.quote(value)}; "
    script = (
        'export PATH="$HOME/.local/bin:$PATH"; '
        # Keep bytecode on the ext4 home: the /mnt/c mount is slow for pycache writes.
        'export PYTHONPYCACHEPREFIX="$HOME/.cache/climate-advisor/pycache"; '
        f"{exports}"
        "if ! command -v uv >/dev/null 2>&1; then "
        f'echo "uv is not installed in WSL. Install it with: {UV_INSTALL_UNIX}" >&2; exit 2; fi; '
        'export UV_PROJECT_ENVIRONMENT="$HOME/.cache/climate-advisor/venv-test-ha"; '
        f"cd {shlex.quote(wsl_repo)} && "
        f"uv run --locked --only-group test-ha pytest {shlex.join(pytest_args)}"
    )
    print(f"+ [wsl:{distro}] {script}", flush=True)
    return subprocess.run(["wsl", "-d", distro, "--", "bash", "-lc", script], check=False).returncode


def run_ha(extra: list[str]) -> int:
    # Default to the whole tier unless the caller named a test path or node id.
    targets = [] if any(a.startswith("tests_ha") or "::" in a for a in extra) else ["tests_ha/"]
    args = [*HA_ARGS, *_worker_args(), *targets, *extra]
    if os.name == "nt":
        return _run_ha_in_wsl(args)
    env_extra = _ha_env_passthrough()
    if env_extra:
        os.environ.update(env_extra)
    return _run_local("test-ha", ".venv-test-ha", args)


def run_unit(extra: list[str]) -> int:
    # Default to the whole suite unless the caller named a test path or node id.
    targets = [] if any(a.startswith("tests") or a.endswith(".py") or "::" in a for a in extra) else ["tests/"]
    return _run_local("test", ".venv-test", [*targets, *UNIT_ARGS, *extra])


def main(argv: list[str]) -> int:
    tier = "all"
    if argv and argv[0] in TIERS:
        tier, argv = argv[0], argv[1:]
    codes: list[int] = []
    if tier in ("unit", "all"):
        codes.append(run_unit(argv))
    if tier in ("ha", "all"):
        codes.append(run_ha(argv))
    return next((c for c in codes if c), 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
