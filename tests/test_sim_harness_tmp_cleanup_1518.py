"""Issue #1518: the simulation harness removes the temp config folders it creates.

Every headless coordinator built without a ``config_dir`` used to leave a ``ca_sim_coordinator_*`` folder in /tmp;
a development container collected about 581,000 of them. The folder is now removed with the FakeHass it belongs
to (at the latest when the process exits), and folders left by a killed run are swept once per process.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tools.sim_harness.build_coordinator as bc


def _folder_left_after_process_exit(build_call: str) -> tuple[str, bool]:
    """Build in a child process, print the config_dir, exit; return (path, still_exists)."""
    root = Path(__file__).resolve().parent.parent
    code = (
        f"import sys; sys.path.insert(0, {str(root)!r})\n"
        "import tools.sim_harness.build_coordinator as bc\n"
        f"out = {build_call}\n"
        "print(out[1].config.config_dir)\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=root)
    assert res.returncode == 0, res.stderr[-2000:]
    path = res.stdout.strip().splitlines()[-1]
    return path, Path(path).exists()


def test_a_headless_coordinators_temp_folder_is_gone_when_the_process_exits():
    """The FakeHass stays referenced by harness stubs until the process ends, so the folder goes at exit."""
    path, exists = _folder_left_after_process_exit("bc.build_headless_coordinator({})")
    assert Path(path).name.startswith("ca_sim_coordinator_") and not exists


def test_a_multi_zone_temp_folder_is_gone_when_the_process_exits():
    path, exists = _folder_left_after_process_exit("bc.build_headless_multi_zone()")
    assert Path(path).name.startswith("ca_sim_multi_zone_") and not exists


def test_a_caller_supplied_config_dir_is_never_removed(tmp_path):
    coordinator, fake_hass, scheduler, _log = bc.build_headless_coordinator({}, config_dir=str(tmp_path))
    del coordinator, fake_hass, scheduler, _log
    gc.collect()
    assert tmp_path.is_dir()


def test_stale_harness_folders_are_swept_and_fresh_or_foreign_ones_are_kept(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="ca_test_sweep_1518_"))
    monkeypatch.setattr(bc.tempfile, "gettempdir", lambda: str(base))
    monkeypatch.setattr(bc, "_swept_stale_sim_dirs", False)
    old = base / "ca_sim_coordinator_old"
    fresh = base / "ca_sim_multi_zone_fresh"
    foreign = base / "someone_elses_folder"
    for p in (old, fresh, foreign):
        p.mkdir()
    stale = time.time() - bc._SIM_TMP_STALE_S - 60
    os.utime(old, (stale, stale))
    os.utime(foreign, (stale, stale))
    bc._sweep_stale_sim_dirs()
    assert not old.exists() and fresh.is_dir() and foreign.is_dir()
    for p in (fresh, foreign):
        p.rmdir()
    base.rmdir()
