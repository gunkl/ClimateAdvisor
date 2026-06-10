"""
conftest.py — pytest fixtures for the Climate Advisor integration harness.

This module provides fixtures that boot a real Home Assistant instance in Docker,
authenticate against it, and tear it down after the test session.

USAGE
-----
    pytest tools/integration_harness/ -m integration -v

DESIGN
------
- All fixtures are session-scoped so the Docker stack boots once per pytest run.
- The ``ha_url`` and ``ha_token`` fixtures are available to all tests in this
  directory.
- Tests are automatically skipped when Docker is unavailable (e.g. in normal
  unit-test CI runs).  The ``integration`` mark is the gate.

AUTH FLOW
---------
HA uses a 3-step OAuth2-like auth flow, not a simple password grant:

    Step 1 — POST /auth/login_flow
        body: {"client_id":"http://localhost/","handler":["homeassistant",null],"redirect_uri":"http://localhost/"}
        → returns {flow_id, ...}

    Step 2 — POST /auth/login_flow/{flow_id}
        body: {"client_id":"http://localhost/","username":"testadmin","password":"testpassword123"}
        → returns {type:"create_entry", result:"<code>", ...}

    Step 3 — POST /auth/token
        Content-Type: application/x-www-form-urlencoded
        body: grant_type=authorization_code&client_id=...&code=<code>
        → returns {access_token:"<JWT>", ...}

This returns a short-lived access_token (JWT, 30-min TTL) that we use as
``Authorization: Bearer <token>`` for all subsequent calls.

The credentials (testadmin / testpassword123) are pre-seeded in:
    config/.storage/auth                          — user record + credential
    config/.storage/auth_provider.homeassistant   — bcrypt password hash (base64)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HARNESS_DIR = Path(__file__).parent
COMPOSE_FILE = HARNESS_DIR / "docker-compose.yml"
SEED_DIR = HARNESS_DIR / "config-seed"  # canonical seed state (git-tracked)
CONFIG_DIR = HARNESS_DIR / "config"  # live HA config (mutated by HA at runtime)
HA_HOST = "localhost"
HA_PORT = 18123
HA_BASE_URL = f"http://{HA_HOST}:{HA_PORT}"

_TEST_USERNAME = "testadmin"
_TEST_PASSWORD = "testpassword123"
_CLIENT_ID = "http://localhost/"

_STARTUP_TIMEOUT_S = 120  # seconds to wait for HA to become healthy
_POLL_INTERVAL_S = 3


def _is_windows() -> bool:
    """Return True when running in a Windows Python environment.

    Uses os.name instead of platform.system() to avoid a WMI access-violation
    in Python 3.14 on Windows (platform._wmi_query crash in background thread).
    """
    return os.name == "nt"


def _docker_cmd() -> list[str]:
    """
    Return the Docker command prefix for the current environment.

    On Windows, Docker Desktop runs inside WSL and is not accessible via the
    normal Windows named-pipe endpoint.  We prefix commands with ``wsl`` so
    they execute inside the WSL default distribution where Docker is available.
    """
    if _is_windows():
        return ["wsl", "docker"]
    return ["docker"]


def _compose_file_arg() -> str:
    """
    Return the compose file path suitable for the Docker command invocation.

    When running via ``wsl docker`` on Windows, the path must be translated to
    a WSL POSIX path (e.g. /mnt/c/…) because the wsl process sees the Linux
    filesystem, not the Windows one.
    """
    win_path = str(COMPOSE_FILE)
    if _is_windows():
        # Convert C:\foo\bar → /mnt/c/foo/bar
        drive = win_path[0].lower()
        rest = win_path[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return win_path


def _docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        result = subprocess.run(
            [*_docker_cmd(), "info"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose(*args: str) -> subprocess.CompletedProcess:
    """Run a docker compose command against the harness compose file."""
    cmd = [
        *_docker_cmd(),
        "compose",
        "-f",
        _compose_file_arg(),
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _reset_config_to_seed() -> None:
    """
    Reset the live ``config/`` directory to the canonical seed state.

    HA mutates ``.storage/`` files (adding system tokens, extra config entries,
    entity registries, etc.) during every run.  Without this reset, successive
    test runs start with stale state: stale entity registries, accumulated
    refresh tokens, or old config-entry data.

    Strategy: copy every file from ``config-seed/`` into ``config/``, then
    delete any ``.storage/`` files that are NOT in the seed (e.g. entity
    registry, recorder, expose-entities) so HA re-creates them cleanly.

    ``configuration.yaml`` is not touched if it already matches the seed
    (no spurious writes for git).
    """
    if not SEED_DIR.exists():
        return  # no seed directory — skip reset (first-time setup)

    # Copy seed files over, preserving directory structure
    for seed_file in SEED_DIR.rglob("*"):
        if seed_file.is_file():
            rel = seed_file.relative_to(SEED_DIR)
            dest = CONFIG_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed_file, dest)

    # Delete .storage/ files not present in the seed (HA-created runtime state)
    storage_seed = {f.relative_to(SEED_DIR / ".storage") for f in (SEED_DIR / ".storage").rglob("*") if f.is_file()}
    storage_dir = CONFIG_DIR / ".storage"
    if storage_dir.exists():
        for live_file in storage_dir.rglob("*"):
            if live_file.is_file():
                rel = live_file.relative_to(storage_dir)
                if rel not in storage_seed:
                    live_file.unlink(missing_ok=True)


def _wait_for_ha(timeout_s: int = _STARTUP_TIMEOUT_S) -> bool:
    """Poll /api/ until HA responds (200 or 401 = it's up), or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{HA_BASE_URL}/api/", timeout=5)
            if resp.status_code in (200, 401):
                return True
        except requests.RequestException:
            pass
        time.sleep(_POLL_INTERVAL_S)
    return False


def _get_token() -> str:
    """Authenticate with the pre-seeded credentials and return a Bearer token.

    HA uses a 3-step flow (initiate → credentials → token exchange), not a
    direct password grant.  This function performs all three steps.
    """
    # Step 1: initiate login flow
    resp1 = requests.post(
        f"{HA_BASE_URL}/auth/login_flow",
        json={
            "client_id": _CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": _CLIENT_ID,
        },
        timeout=15,
    )
    resp1.raise_for_status()
    flow_id = resp1.json().get("flow_id")
    if not flow_id:
        raise RuntimeError(f"No flow_id in login_flow response: {resp1.text}")

    # Step 2: submit credentials
    resp2 = requests.post(
        f"{HA_BASE_URL}/auth/login_flow/{flow_id}",
        json={
            "client_id": _CLIENT_ID,
            "username": _TEST_USERNAME,
            "password": _TEST_PASSWORD,
        },
        timeout=15,
    )
    resp2.raise_for_status()
    data2 = resp2.json()
    if data2.get("type") != "create_entry":
        raise RuntimeError(f"Unexpected login_flow result type: {data2}")
    code = data2.get("result")
    if not code:
        raise RuntimeError(f"No auth code in login_flow step 2 response: {data2}")

    # Step 3: exchange code for access token
    resp3 = requests.post(
        f"{HA_BASE_URL}/auth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": _CLIENT_ID,
            "code": code,
        },
        timeout=15,
    )
    resp3.raise_for_status()
    token = resp3.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in token response: {resp3.text}")
    return token


# ---------------------------------------------------------------------------
# Marks + auto-skip
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Register the ``integration`` mark."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as Docker-based integration tests (skipped when Docker unavailable)",
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=False)
def _docker_check():
    """Skip the entire session if Docker is unavailable."""
    if not _docker_available():
        pytest.skip("Docker daemon not available — skipping integration tests")


@pytest.fixture(scope="session")
def ha_stack(_docker_check):
    """
    Boot the HA Docker stack and yield.  Tears down (with volume removal)
    after the test session completes.

    Uses ``docker compose up --build -d`` so the image is rebuilt if the
    Dockerfile or bind-mounted source changed.
    """
    # Tear down any leftover stack from a previous interrupted run
    _compose("down", "-v", "--remove-orphans")

    # Restore config/ to the canonical seed state so each test run is deterministic
    _reset_config_to_seed()

    result = _compose("up", "--build", "-d")
    if result.returncode != 0:
        pytest.fail(f"docker compose up failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

    healthy = _wait_for_ha()
    if not healthy:
        # Grab logs for diagnosis before failing
        logs = _compose("logs", "--no-color", "--tail=100")
        pytest.fail(f"HA did not become healthy within {_STARTUP_TIMEOUT_S}s.\nLogs:\n{logs.stdout}\n{logs.stderr}")

    yield  # tests run here

    _compose("down", "-v", "--remove-orphans")


@pytest.fixture(scope="session")
def ha_url(ha_stack) -> str:
    """Return the base URL of the running HA instance."""
    return HA_BASE_URL


@pytest.fixture(scope="session")
def ha_token(ha_stack) -> str:
    """
    Return a valid HA Bearer token for the pre-seeded testadmin user.

    The token is obtained via the standard HA OAuth2 password grant flow.
    It is short-lived (~30 min) but sufficient for a test session.
    """
    return _get_token()


@pytest.fixture(scope="session")
def ha_headers(ha_token) -> dict:
    """Return Authorization headers for HA REST API calls."""
    return {"Authorization": f"Bearer {ha_token}"}
