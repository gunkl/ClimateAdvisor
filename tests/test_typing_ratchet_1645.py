"""Issue #1645 (Platinum strict-typing): tools/typing_ratchet.py keeps the strict-mypy module list clean and growing.

Occupant view: none directly. Strict typing catches a ``None`` where a temperature was expected, or a misspelled
field, before it reaches a house. These tests feed saved mypy output to the ratchet, so they need no mypy here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ca_typing_ratchet", REPO / "tools" / "typing_ratchet.py")
ratchet = importlib.util.module_from_spec(_spec)
sys.modules["ca_typing_ratchet"] = ratchet
_spec.loader.exec_module(ratchet)

P = "custom_components.climate_advisor"
MYPY = """\
custom_components/climate_advisor/dirty.py:12: error: Function is missing a return type annotation  [no-untyped-def]
custom_components/climate_advisor/dirty.py:12: note: Use "-> None" if function does not return a value
custom_components/climate_advisor/dirty.py:30:5: error: Returning Any from function declared to return "int"
custom_components/climate_advisor/__init__.py:7: error: Missing type parameters for generic type "dict"  [type-arg]
Found 3 errors in 2 files (checked 4 source files)
"""


def _mods(*names: str) -> set[str]:
    return {f"{P}.{n}" if n else P for n in names}


def test_module_names_and_error_parsing():
    assert ratchet.module_name("custom_components/climate_advisor/sensor.py") == f"{P}.sensor"
    assert ratchet.module_name("custom_components\\climate_advisor\\__init__.py") == P
    errors = ratchet.parse_mypy(MYPY)
    assert set(errors) == {f"{P}.dirty", P}
    assert len(errors[f"{P}.dirty"]) == 2  # the note line is not an error


def test_listed_module_with_an_error_fails():
    result = ratchet.evaluate(_mods("clean", "dirty", ""), ratchet.parse_mypy(MYPY), [f"{P}.clean", f"{P}.dirty"])
    assert len(result.errors) == 1
    assert result.errors[0].startswith(f"{P}.dirty: listed as strict but has 2 mypy error(s)")


def test_clean_module_not_listed_fails_so_the_list_only_grows():
    result = ratchet.evaluate(_mods("clean", "also_clean", "dirty", ""), ratchet.parse_mypy(MYPY), [f"{P}.clean"])
    assert [e.split(":")[0] for e in result.errors] == [f"{P}.also_clean"]


def test_listed_module_that_is_gone_fails():
    result = ratchet.evaluate(_mods("clean"), {}, [f"{P}.clean", f"{P}.renamed_away"])
    assert [e.split(":")[0] for e in result.errors] == [f"{P}.renamed_away"]


def test_module_taken_off_the_list_since_base_fails():
    errors = ratchet.parse_mypy(MYPY)
    result = ratchet.evaluate(
        _mods("clean", "dirty", ""), errors, [f"{P}.clean"], base_listed=[f"{P}.clean", f"{P}.dirty"]
    )
    assert any(e.startswith(f"{P}.dirty: was in .strict-typing on the base branch") for e in result.errors)


def test_new_module_must_pass_from_day_one():
    errors = ratchet.parse_mypy(MYPY)
    base_modules = _mods("clean", "")
    result = ratchet.evaluate(_mods("clean", "dirty", ""), errors, [f"{P}.clean"], [f"{P}.clean"], base_modules)
    assert any(e.startswith(f"{P}.dirty: new module with 2 strict mypy error(s)") for e in result.errors)
    # The package __init__ existed at base, so its errors are tolerated while it is not listed.
    assert not any(e.startswith(f"{P}: ") for e in result.errors)


def _tree(tmp_path: Path, *files: str) -> Path:
    pkg = tmp_path / "custom_components" / "climate_advisor"
    pkg.mkdir(parents=True)
    for f in files:
        (pkg / f).write_text("", encoding="utf-8")
    (tmp_path / "mypy.txt").write_text(MYPY, encoding="utf-8")
    return tmp_path


def test_update_adds_clean_modules_and_check_then_passes(tmp_path):
    root = _tree(tmp_path, "__init__.py", "clean.py", "dirty.py")
    out = str(root / "mypy.txt")
    assert ratchet.main(["--root", str(root), "--mypy-output", out]) == 1  # clean.py is clean but unlisted
    assert ratchet.main(["--root", str(root), "--mypy-output", out, "--update"]) == 0
    assert ratchet.read_list((root / ".strict-typing").read_text(encoding="utf-8")) == [f"{P}.clean"]
    summary = root / "summary.md"
    assert ratchet.main(["--root", str(root), "--mypy-output", out, "--summary", str(summary)]) == 0
    assert "| `dirty` | 2 |" in summary.read_text(encoding="utf-8")


def test_update_refuses_to_drop_a_listed_module_that_regressed(tmp_path):
    root = _tree(tmp_path, "__init__.py", "clean.py", "dirty.py")
    (root / ".strict-typing").write_text(f"{P}.dirty\n", encoding="utf-8")
    assert ratchet.main(["--root", str(root), "--mypy-output", str(root / "mypy.txt"), "--update"]) == 1
    assert ratchet.read_list((root / ".strict-typing").read_text(encoding="utf-8")) == [f"{P}.dirty"]


def test_mypy_that_did_not_run_gives_no_verdict(tmp_path):
    root = _tree(tmp_path, "clean.py")
    (root / "mypy.txt").write_text("mypy: error: unrecognized arguments\n", encoding="utf-8")
    assert ratchet.main(["--root", str(root), "--mypy-output", str(root / "mypy.txt")]) == 2


def test_committed_list_is_sorted_unique_and_names_real_modules():
    listed = ratchet.read_list((REPO / ".strict-typing").read_text(encoding="utf-8"))
    assert listed == sorted(set(listed))
    assert set(listed) <= ratchet.package_modules(REPO)
    assert listed, "the strict list starts with the modules already clean"


def test_package_ships_py_typed():
    assert (REPO / "custom_components" / "climate_advisor" / "py.typed").is_file()
