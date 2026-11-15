"""Issue #1572: a timed-out real-HA test is named in the output, not just reported as a crashed xdist worker."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

from timeout_report import MARK

HERE = Path(__file__).resolve().parent


def test_a_timed_out_test_is_named_before_the_worker_is_ended(tmp_path: Path) -> None:
    (tmp_path / "test_sleeps.py").write_text("import time\n\ndef test_sleeps():\n    time.sleep(30)\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "pytest_timeout",
            "-p",
            "timeout_report",
            "-n",
            "1",
            "-o",
            "timeout=2",
            "-o",
            "timeout_method=thread",
            "--rootdir",
            str(tmp_path),
            "-c",
            "/dev/null",
            str(tmp_path / "test_sleeps.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        # only xdist, pytest-timeout and this plugin: Home Assistant's auto-loaded fixtures are not part of the check
        env={**os.environ, "PYTHONPATH": str(HERE), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    out = result.stdout + result.stderr
    assert "crashed while running" in out, out[-2000:]
    assert f"{MARK} after 2 s: test_sleeps.py::test_sleeps" in out, out[-2000:]


def test_the_real_ha_timeout_leaves_room_for_a_loaded_machine() -> None:
    """The slowest real-HA test measured 51 s with the unit suite running alongside (2026-11-15); 60 s ended it."""
    ini = configparser.ConfigParser()
    ini.read(HERE / "pytest.ini")
    assert int(ini["pytest"]["timeout"]) >= 4 * 51
    assert ini["pytest"]["timeout_method"] == "thread"  # #1518: a hang that SIGALRM cannot interrupt still ends
