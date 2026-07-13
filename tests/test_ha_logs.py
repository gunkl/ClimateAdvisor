"""Regression tests for tools/ha_logs.py's remote `ha core logs` command construction.

Issue #502: fetch_logs() previously ran bare `ha core logs` in every branch, which
defaults to only the last 100 lines on HAOS with no `--lines` flag. Any local
grep/tail filtering happened after that 100-line cap, so `--lines`/`--full`/
`--thermal` silently returned far less history than requested. These tests assert
the real `fetch_logs()` function always forwards an explicit `--lines <N>` to the
remote command — no live SSH/network calls are made; `subprocess.run` is mocked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_ha_logs_module():
    """Load tools/ha_logs.py as a module without touching .deploy.env or SSH."""
    spec = importlib.util.spec_from_file_location("ha_logs", _TOOLS_DIR / "ha_logs.py")
    mod = importlib.util.module_from_spec(spec)
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    spec.loader.exec_module(mod)
    return mod


_ha_logs = _load_ha_logs_module()


def _fake_config() -> dict:
    return {
        "HA_HOST": "homeassistant.local",
        "HA_SSH_PORT": "22",
        "HA_SSH_USER": "hassio",
        "HA_SSH_KEY": "",
        "HA_CONFIG_PATH": "/config",
        "HA_API_TOKEN": "",
    }


def _mock_subprocess_result() -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _captured_remote_cmd(mock_run: MagicMock) -> str:
    """Extract the remote command string (last element of the ssh argv list)."""
    (cmd_args,), _kwargs = mock_run.call_args
    return cmd_args[-1]


class TestFullDumpForwardsLines:
    def test_full_dump_passes_explicit_lines_flag(self):
        with patch.object(_ha_logs.subprocess, "run", return_value=_mock_subprocess_result()) as mock_run:
            _ha_logs.fetch_logs(_fake_config(), lines=12345, full_dump=True)
        remote_cmd = _captured_remote_cmd(mock_run)
        assert "--lines 12345" in remote_cmd
        assert remote_cmd.startswith("ha core logs --lines 12345")


class TestFilteredModeForwardsRawLines:
    def test_component_filter_pulls_generous_raw_depth_before_grep(self):
        with patch.object(_ha_logs.subprocess, "run", return_value=_mock_subprocess_result()) as mock_run:
            _ha_logs.fetch_logs(
                _fake_config(),
                lines=500,
                component_filter="climate_advisor",
                raw_lines=20000,
            )
        remote_cmd = _captured_remote_cmd(mock_run)
        assert "--lines 20000" in remote_cmd
        assert "grep -i climate_advisor" in remote_cmd
        assert "tail -n 500" in remote_cmd

    def test_extra_filter_still_applies_after_raw_pull(self):
        with patch.object(_ha_logs.subprocess, "run", return_value=_mock_subprocess_result()) as mock_run:
            _ha_logs.fetch_logs(
                _fake_config(),
                lines=200,
                component_filter="climate_advisor",
                extra_filter="paused-by-door",
                raw_lines=20000,
            )
        remote_cmd = _captured_remote_cmd(mock_run)
        assert "--lines 20000" in remote_cmd
        assert "grep -i paused-by-door" in remote_cmd


class TestUnfilteredAllModeForwardsRawLines:
    def test_no_component_filter_still_requests_raw_lines(self):
        with patch.object(_ha_logs.subprocess, "run", return_value=_mock_subprocess_result()) as mock_run:
            _ha_logs.fetch_logs(
                _fake_config(),
                lines=500,
                component_filter="",
                raw_lines=20000,
            )
        remote_cmd = _captured_remote_cmd(mock_run)
        assert "--lines 20000" in remote_cmd
        assert "tail -n 500" in remote_cmd


class TestDefaultRawLinesConstant:
    def test_default_raw_lines_used_when_not_overridden(self):
        with patch.object(_ha_logs.subprocess, "run", return_value=_mock_subprocess_result()) as mock_run:
            _ha_logs.fetch_logs(_fake_config(), lines=500, component_filter="climate_advisor")
        remote_cmd = _captured_remote_cmd(mock_run)
        assert f"--lines {_ha_logs.DEFAULT_RAW_LINES}" in remote_cmd
