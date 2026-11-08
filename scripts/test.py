#!/usr/bin/env python3
"""Single entry point for the Climate Advisor test suites (Issue #1126).

    python scripts/test.py [unit|ha|all] [pytest args...]

* ``unit``: ``tests/`` against stubbed Home Assistant (dependency group ``test``).
* ``ha``:   ``tests_ha/`` against the real Home Assistant (dependency group ``test-ha``).
* ``all``:  both, in that order (default).

Environment knobs for the unit tier:

* ``CA_UNIT_WORKERS``: number of pytest-xdist workers (``auto`` for all cores). Unset or ``0`` runs
  serially (the local default).
* ``CA_UNIT_SHARD``: ``K/N`` runs only the Kth of N round-robin slices of the collected tests (CI runs the
  N slices as parallel jobs), passed to pytest as ``--ca-shard K/N`` (see ``tools/pytest_shard.py``). Unset runs
  everything.
* ``CA_UNIT_SHARDS``: ``N`` (local) runs all N slices at once as separate pytest processes, each with
  ``CA_UNIT_WORKERS`` workers, prefixes each slice's output with ``[K/N]`` and prints each slice's wall time.
  Total processes = N x workers: on a RAM-limited machine keep that near the ``CA_UNIT_WORKERS`` you use today
  (e.g. ``CA_UNIT_SHARDS=4 CA_UNIT_WORKERS=2`` instead of ``CA_UNIT_WORKERS=8``).

Environment knobs for the ha tier:

* ``CA_HA_WORKERS``: number of pytest-xdist workers (``auto`` for all cores). Unset or ``0``
  runs serially (the local default; CI sets ``4``).
* ``CA_HA_SHARD``: ``K/N`` runs only the Kth of N round-robin slices of the tier, passed as ``--ca-shard K/N``
  (CI runs four slices as parallel ``ha-real (K/4)`` jobs; see ``tools/pytest_shard.py``). Unset runs everything.
* ``CA_HA_SHARDS``: ``N`` (local, not on Windows) runs all N of those slices at once as separate pytest processes, each
  with ``CA_HA_WORKERS`` workers, prefixed ``[K/N]`` and timed like ``CA_UNIT_SHARDS``. On a 24-core / 31 GB machine
  the fastest full run is a single process with ``CA_HA_WORKERS=8`` (~90 s); ``CA_HA_SHARDS=4 CA_HA_WORKERS=2`` runs
  CI's four slices in about the same time. More than ~8 processes in total is slower (``docs/DEVELOPMENT.md``).
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
import threading
import time
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


def _local_cmd(group: str, venv: str, pytest_args: list[str]) -> tuple[list[str], dict[str, str]] | None:
    """The ``uv run ... pytest`` command and environment for one tier; None (after a message) without uv."""
    uv = _uv_cmd()
    if uv is None:
        install = UV_INSTALL_WINDOWS if os.name == "nt" else UV_INSTALL_UNIX
        print(f"uv is not installed. Install it with: {install}", file=sys.stderr)
        return None
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(REPO / venv)}
    return [*uv, "run", "--locked", "--only-group", group, "pytest", *pytest_args], env


def _run_local(group: str, venv: str, pytest_args: list[str]) -> int:
    if (local := _local_cmd(group, venv, pytest_args)) is None:
        return 2
    cmd, env = local
    print(f"+ [{venv}] {shlex.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=REPO, env=env, check=False).returncode


def _shard_count(var: str) -> int | None:
    """``N`` from ``CA_UNIT_SHARDS`` / ``CA_HA_SHARDS``; None when unset or ``0``. Exits 2 on a bad value."""
    raw = os.environ.get(var, "").strip()
    if raw in ("", "0"):
        return None
    if not raw.isdigit():
        print(f"{var} must be a whole number of slices, got {raw!r}", file=sys.stderr)
        raise SystemExit(2)
    single = var[:-1]  # CA_UNIT_SHARDS -> CA_UNIT_SHARD
    if os.environ.get(single, "").strip():
        print(f"Set {var} (all slices at once) or {single} (one slice), not both", file=sys.stderr)
        raise SystemExit(2)
    return int(raw)


def _run_shards(tier: str, group: str, venv: str, total: int, pytest_args: list[str]) -> int:
    """``CA_UNIT_SHARDS`` / ``CA_HA_SHARDS``: every slice at once, one pytest process each, output prefixed and timed.

    Slice K gets ``--ca-shard K/N``, the same round-robin split as CI's ``CA_*_SHARD=K/N`` jobs
    (``tools/pytest_shard.py``), so a local slice runs exactly the tests of the matching CI job.
    """
    if (local := _local_cmd(group, venv, ["--version"])) is None:
        return 2
    _, env = local
    # Sync the venv once up front so the slices do not all race to install it.
    if subprocess.run([*_uv_cmd(), "sync", "--locked", "--only-group", group], cwd=REPO, env=env).returncode:
        return 2
    lock = threading.Lock()
    codes: dict[int, int] = {}
    walls: dict[int, float] = {}

    def run(k: int) -> None:
        cmd, env = _local_cmd(group, venv, [*pytest_args, "--ca-shard", f"{k}/{total}"])
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        for line in proc.stdout:
            with lock:
                print(f"[{k}/{total}] {line}", end="", flush=True)
        codes[k] = proc.wait()
        walls[k] = time.monotonic() - start

    print(f"+ [{venv}] {total} slices at once: pytest {shlex.join(pytest_args)} --ca-shard K/{total}", flush=True)
    start = time.monotonic()
    threads = [threading.Thread(target=run, args=(k,)) for k in range(1, total + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"\n{tier} tier in {total} slices: {time.monotonic() - start:.0f}s wall")
    for k in range(1, total + 1):
        print(f"  [{k}/{total}] {'passed' if codes[k] == 0 else f'FAILED (exit {codes[k]})'} in {walls[k]:.0f}s")
    return next((c for c in codes.values() if c), 0)


def _ha_env_passthrough() -> dict[str, str]:
    """Env vars that steer the ha tier (see module docstring)."""
    return {k: os.environ[k] for k in ("CA_COMPONENT_ROOT",) if os.environ.get(k)}


def _worker_args(env_var: str = "CA_HA_WORKERS") -> list[str]:
    """``-n <workers>`` from an environment knob; unset or ``0`` means serial."""
    workers = os.environ.get(env_var, "").strip()
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
    shards = _shard_count("CA_HA_SHARDS")
    shard = os.environ.get("CA_HA_SHARD", "").strip()
    args = [*HA_ARGS, *_worker_args(), *targets, *extra]
    if os.name == "nt":
        if shards:
            print("CA_HA_SHARDS is not supported on Windows (WSL); use CA_HA_WORKERS or CA_HA_SHARD.", file=sys.stderr)
            return 2
        return _run_ha_in_wsl([*args, *(["--ca-shard", shard] if shard else [])])
    env_extra = _ha_env_passthrough()
    if env_extra:
        os.environ.update(env_extra)
    if shards:
        return _run_shards("ha", "test-ha", ".venv-test-ha", shards, args)
    return _run_local("test-ha", ".venv-test-ha", [*args, *(["--ca-shard", shard] if shard else [])])


def run_unit(extra: list[str]) -> int:
    # Default to the whole suite unless the caller named a test path or node id.
    targets = [] if any(a.startswith("tests") or a.endswith(".py") or "::" in a for a in extra) else ["tests/"]
    args = [*targets, *UNIT_ARGS, *_worker_args("CA_UNIT_WORKERS"), *extra]
    if shards := _shard_count("CA_UNIT_SHARDS"):
        return _run_shards("unit", "test", ".venv-test", shards, args)
    shard = os.environ.get("CA_UNIT_SHARD", "").strip()
    return _run_local("test", ".venv-test", [*args, *(["--ca-shard", shard] if shard else [])])


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
