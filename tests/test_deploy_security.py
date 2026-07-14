"""Tests for security hardening in deployment scripts.

Covers input validation in validate_config() (tools/deploy.py) and
shell-injection resistance in fetch_logs() (tools/ha_logs.py).

All user-controlled values that reach shell commands must pass through
shlex.quote() — these tests verify that contract.

See: GitHub Issue #48 Phase 6
"""

from __future__ import annotations

import os
import shlex
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — tools/ is not a package, add it to sys.path directly
# ---------------------------------------------------------------------------

_TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from deploy import validate_config  # noqa: E402
from ha_logs import fetch_logs  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VALID_CONFIG = {
    "HA_HOST": "homeassistant.local",
    "HA_SSH_PORT": "22",
    "HA_SSH_USER": "hassio",
    "HA_SSH_KEY": "",
    "HA_CONFIG_PATH": "/config",
}

_MOCK_SSH_CONFIG = {
    "HA_HOST": "homeassistant.local",
    "HA_SSH_PORT": "22",
    "HA_SSH_USER": "hassio",
    "HA_SSH_KEY": "",
}


def _cfg(**overrides) -> dict[str, str]:
    """Return a copy of the valid config with the given keys overridden."""
    c = dict(_VALID_CONFIG)
    c.update(overrides)
    return c


# ---------------------------------------------------------------------------
# 1. validate_config() — input validation
# ---------------------------------------------------------------------------


class TestDeployConfigValidation:
    """validate_config() must accept clean values and reject malformed ones."""

    def test_valid_config(self):
        """Standard valid config returns an empty errors list."""
        errors = validate_config(_VALID_CONFIG)
        assert errors == []

    # ---- port validation ----

    def test_invalid_port_string(self):
        """Non-numeric port must produce an error mentioning 'numeric'."""
        errors = validate_config(_cfg(HA_SSH_PORT="abc"))
        assert errors, "Expected at least one error for non-numeric port"
        assert any("numeric" in e.lower() for e in errors), f"Expected error containing 'numeric', got: {errors}"

    def test_invalid_port_zero(self):
        """Port 0 is outside the valid range — error must mention 'range'."""
        errors = validate_config(_cfg(HA_SSH_PORT="0"))
        assert errors, "Expected at least one error for port 0"
        assert any("range" in e.lower() for e in errors), f"Expected error containing 'range', got: {errors}"

    def test_invalid_port_high(self):
        """Port 99999 is above 65535 — error must mention 'range'."""
        errors = validate_config(_cfg(HA_SSH_PORT="99999"))
        assert errors, "Expected at least one error for port 99999"
        assert any("range" in e.lower() for e in errors), f"Expected error containing 'range', got: {errors}"

    def test_valid_port_boundaries(self):
        """Ports 1 and 65535 are the legal boundary values — both must pass."""
        assert validate_config(_cfg(HA_SSH_PORT="1")) == []
        assert validate_config(_cfg(HA_SSH_PORT="65535")) == []

    # ---- hostname validation ----

    def test_invalid_hostname_semicolon(self):
        """Hostname with semicolon must be rejected (shell-injection risk)."""
        errors = validate_config(_cfg(HA_HOST="foo;rm"))
        assert errors, "Expected at least one error for semicolon in hostname"

    def test_invalid_hostname_space(self):
        """Hostname with a space must be rejected."""
        errors = validate_config(_cfg(HA_HOST="foo bar"))
        assert errors, "Expected at least one error for space in hostname"

    def test_valid_hostname_ip(self):
        """IPv4 address is a valid hostname."""
        assert validate_config(_cfg(HA_HOST="192.168.1.1")) == []

    def test_valid_hostname_dotlocal(self):
        """mDNS .local hostname is valid."""
        assert validate_config(_cfg(HA_HOST="ha.local")) == []

    # ---- config path validation ----

    def test_relative_path_rejected(self):
        """A relative config path must be rejected."""
        errors = validate_config(_cfg(HA_CONFIG_PATH="config/"))
        assert errors, "Expected at least one error for relative path"

    # ---- SSH key validation ----

    def test_missing_ssh_key(self):
        """A non-existent SSH key path must produce an error mentioning 'not found'."""
        errors = validate_config(_cfg(HA_SSH_KEY="/nonexistent/key"))
        assert errors, "Expected at least one error for missing key file"
        assert any("not found" in e.lower() for e in errors), f"Expected error containing 'not found', got: {errors}"

    def test_empty_ssh_key_ok(self):
        """Empty SSH key string is valid (key is optional; password auth is allowed)."""
        assert validate_config(_cfg(HA_SSH_KEY="")) == []


# ---------------------------------------------------------------------------
# 2. fetch_logs() — shell injection resistance via shlex.quote()
# ---------------------------------------------------------------------------


class TestShellSanitization:
    """User-supplied filter strings must be quoted before they reach the shell.

    fetch_logs() builds a remote command string passed to SSH.  Any value
    accepted from the caller (extra_filter, component_filter) must be wrapped
    by shlex.quote() so that special characters cannot break out of the grep
    argument and execute arbitrary commands on the remote host.
    """

    @patch("ha_logs.subprocess.run")
    def test_filter_with_semicolon_is_quoted(self, mock_run):
        """Semicolons inside extra_filter must be safely quoted, not raw."""
        mock_run.return_value = MagicMock(returncode=0, stdout="log line", stderr="")

        malicious = "ERROR'; rm -rf /; echo '"
        fetch_logs(_MOCK_SSH_CONFIG, component_filter="climate_advisor", extra_filter=malicious)

        cmd = mock_run.call_args[0][0]  # first positional arg to subprocess.run
        remote_cmd = cmd[-1]  # SSH passes the remote command as the last arg

        quoted = shlex.quote(malicious)
        # The quoted form must appear in the command…
        assert quoted in remote_cmd, f"Expected shlex-quoted form {quoted!r} in remote_cmd: {remote_cmd!r}"
        # …and no bare (unquoted) semicolons should remain outside of the
        # quoted token, which we verify by checking the raw injection string
        # is not present verbatim.
        assert malicious not in remote_cmd, f"Raw injection string appeared unquoted in remote_cmd: {remote_cmd!r}"

    @patch("ha_logs.subprocess.run")
    def test_component_filter_with_backticks_is_quoted(self, mock_run):
        """Backtick command substitution in extra_filter must be neutralised.

        shlex.quote wraps the value in single quotes, producing '`id`'.  Inside
        POSIX single quotes, backticks are completely inert — the shell cannot
        execute them.  The test verifies that the shlex-quoted token is present
        in the remote command, confirming that quoting has been applied.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        backtick_payload = "`id`"
        fetch_logs(_MOCK_SSH_CONFIG, component_filter="climate_advisor", extra_filter=backtick_payload)

        cmd = mock_run.call_args[0][0]
        remote_cmd = cmd[-1]

        # shlex.quote("`id`") == "'`id`'" — backticks inside single quotes are inert
        quoted = shlex.quote(backtick_payload)
        assert quoted in remote_cmd, f"Expected shlex-quoted form {quoted!r} in remote_cmd: {remote_cmd!r}"
        # The dangerous form would be "grep -i `id`" — raw payload after the grep flag.
        # Verify that pattern does not appear unquoted in the command.
        assert f"-i {backtick_payload}" not in remote_cmd, (
            f"Backtick payload appeared raw after grep -i in remote_cmd: {remote_cmd!r}"
        )

    @patch("ha_logs.subprocess.run")
    def test_normal_filter_works(self, mock_run):
        """A benign extra_filter value must appear as a grep argument."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2026-01-01 ERROR foo", stderr="")

        fetch_logs(_MOCK_SSH_CONFIG, component_filter="climate_advisor", extra_filter="ERROR")

        cmd = mock_run.call_args[0][0]
        remote_cmd = cmd[-1]

        # The remote pipeline must contain grep -i with the filter
        assert "grep -i" in remote_cmd, f"Expected 'grep -i' in remote_cmd: {remote_cmd!r}"
        assert "ERROR" in remote_cmd, f"Expected 'ERROR' to appear in remote_cmd: {remote_cmd!r}"

    @patch("ha_logs.subprocess.run")
    def test_no_filter_no_grep(self, mock_run):
        """full_dump=True must produce an unfiltered 'ha core logs --lines N' command with no grep.

        Issue #502: `ha core logs` with no `--lines` silently defaults to only its last
        100 lines on HAOS, so full_dump must always pass an explicit `--lines` — a bare
        `ha core logs` is not actually a full dump.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="lots of logs", stderr="")

        fetch_logs(_MOCK_SSH_CONFIG, lines=50, full_dump=True)

        cmd = mock_run.call_args[0][0]
        remote_cmd = cmd[-1]

        assert remote_cmd.strip() == "ha core logs --lines 50", f"Expected explicit --lines, got: {remote_cmd!r}"
        assert "grep" not in remote_cmd, f"Expected no grep in full_dump mode, got: {remote_cmd!r}"
