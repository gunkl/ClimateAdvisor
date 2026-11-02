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


class TestHistoryWindowSendsAnEndTime:
    """Issue #1233: HA's /api/history/period returns ONE DAY from the start time when no ``end_time`` is
    sent, so ``--hours 72`` silently came back with the oldest 24 h (the owner's swing investigation read
    the wrong three days). The window must always be closed explicitly."""

    @staticmethod
    def _urls(fn, *args, **kwargs):
        seen: list[str] = []

        def fake(url, token):
            seen.append(url)
            return []

        with patch.object(_ha_logs, "_ha_api_request", side_effect=fake):
            fn({**_fake_config(), "HA_API_TOKEN": "t"}, *args, **kwargs)
        return seen

    def test_hours_over_a_day_sends_end_time_for_sensor_history(self):
        (url,) = self._urls(_ha_logs.fetch_sensor_history, "climate.x", hours_back=72)
        assert "end_time=" in url

    def test_hours_over_a_day_sends_end_time_for_the_logbook(self):
        (url,) = self._urls(_ha_logs.fetch_history, "climate.x", hours_back=72)
        assert "end_time=" in url

    def test_the_window_spans_the_requested_hours(self):
        from datetime import datetime, timedelta
        from urllib.parse import unquote

        (url,) = self._urls(_ha_logs.fetch_sensor_history, "climate.x", hours_back=72)
        start = datetime.fromisoformat(unquote(url.split("/api/history/period/")[1].split("?")[0]))
        end = datetime.fromisoformat(unquote(url.split("end_time=")[1]))
        assert timedelta(hours=71, minutes=59) <= end - start <= timedelta(hours=72, minutes=1)

    def test_start_without_end_runs_to_now_not_one_day(self):
        from datetime import UTC, datetime, timedelta

        start = datetime.now(UTC) - timedelta(days=5)
        (url,) = self._urls(_ha_logs.fetch_sensor_history, "climate.x", start_dt=start)
        assert "end_time=" in url


class TestExplicitEndKept:
    def test_an_explicit_end_is_kept(self):
        from datetime import UTC, datetime

        end = datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
        seen: list[str] = []
        with patch.object(_ha_logs, "_ha_api_request", side_effect=lambda url, token: seen.append(url) or []):
            _ha_logs.fetch_sensor_history(
                {**_fake_config(), "HA_API_TOKEN": "t"},
                "climate.x",
                start_dt=datetime(2026, 10, 22, 8, 0, tzinfo=UTC),
                end_dt=end,
            )
        assert "end_time=2026-10-25T08%3A00%3A00%2B00%3A00" in seen[0]
