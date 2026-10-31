"""The missing-dependency skip-to-fail guard in tests/conftest.py (Issue #1126 / #1125 review).

Each case runs a real pytest subprocess with ``tests.conftest`` loaded as a plugin against a
throwaway test file, so the real hooks decide the outcome (no re-implemented matching).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run(tmp_path: Path, body: str) -> tuple[int, str]:
    (tmp_path / "test_case.py").write_text(textwrap.dedent(body), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-q", "test_case.py"],
        cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\npytest.importorskip('no_such_pkg_xyz')\ndef test_x():\n    pass\n",
        "import pytest\npytest.skip('the foo package is not installed', allow_module_level=True)\n",
        "import pytest\ndef test_x():\n    pytest.importorskip('no_such_pkg_xyz')\n",
        "import pytest\n@pytest.mark.skip(reason='No module named zzz')\ndef test_x():\n    pass\n",
    ],
    ids=["module-importorskip", "module-level-skip", "runtime-importorskip", "marker-skip"],
)
def test_missing_dependency_skips_become_failures(tmp_path, body):
    code, out = _run(tmp_path, body)
    assert code != 0, out
    assert "missing dependency" in out, out


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\ndef test_x():\n    pytest.skip('sensor unavailable during the outage window')\n",
        "import pytest\npytest.skip('hardware unavailable on this runner', allow_module_level=True)\n",
        "import pytest\ndef test_x():\n    pytest.skip('no final indoor/outdoor reading captured in engine_state')\n",
    ],
    ids=["runtime-unavailable", "module-unavailable", "plain-domain-skip"],
)
def test_ordinary_skips_stay_skips(tmp_path, body):
    code, out = _run(tmp_path, body)
    assert code in (0, 5), out  # 5 = nothing left to run after a module-level skip, still not a failure
    assert "skipped" in out and "failed" not in out and "error" not in out.lower(), out
