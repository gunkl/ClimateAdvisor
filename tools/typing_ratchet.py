#!/usr/bin/env python3
"""Strict-typing ratchet for the integration (Issue #1645, Platinum ``strict-typing``).

``.strict-typing`` (repo root, the same name and idea as Home Assistant core's file) lists every module of
``custom_components/climate_advisor`` that passes strict mypy (the ``[tool.mypy]`` settings in ``pyproject.toml``:
Home Assistant's general settings plus its strict set). This script runs mypy once over the whole package and:

* fails when a listed module has a mypy error (a listed module stays clean);
* fails when a module that passes is not listed (the list only grows: add it, ``--update`` does that);
* fails when a listed module no longer exists (stale entry: rename or remove it, ``--update`` does that);
* with ``--base REF`` (CI passes the PR's base branch): fails when a module listed at ``REF`` was taken off the list,
  and when a module that is new since ``REF`` does not pass (new modules are strict from day one).

Modules not listed may have errors; the typing work is incremental, module by module.

mypy has to see the real Home Assistant package, so run it in the real-HA environment::

    uv run --locked --only-group test-ha python tools/typing_ratchet.py [--base origin/main] [--update]

``--mypy-output FILE`` reads a saved mypy report instead of running mypy (tests). ``--summary FILE`` appends a
Markdown summary (CI passes ``$GITHUB_STEP_SUMMARY``). Exit 0 clean, 1 ratchet failure, 2 mypy did not run.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path("custom_components") / "climate_advisor"
PACKAGE = "custom_components.climate_advisor"
LIST_FILE = ".strict-typing"
UPDATE_HINT = "python tools/typing_ratchet.py --update"

_HEADER = """\
# Modules of custom_components/climate_advisor that pass strict mypy (Issue #1645, Platinum strict-typing).
# Checked by tools/typing_ratchet.py in CI: a listed module must stay clean, and a module that becomes clean must
# be added, so this list only grows. New modules must pass from day one. Update with:
#   uv run --locked --only-group test-ha python tools/typing_ratchet.py --update
"""

# path:line[:col]: error: message  [code]
_ERROR_RE = re.compile(r"^(?P<path>[^:\s][^:]*\.py):\d+(?::\d+)?: error: (?P<msg>.*)$")
_FOUND_RE = re.compile(r"^Found \d+ errors? in \d+ files?", re.MULTILINE)
_SUCCESS_RE = re.compile(r"^Success: no issues found", re.MULTILINE)


def module_name(path: str | Path) -> str:
    """``custom_components/climate_advisor/sensor.py`` -> ``custom_components.climate_advisor.sensor``."""
    parts = list(Path(str(path).replace("\\", "/")).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_modules(root: Path = REPO) -> set[str]:
    """Every module of the integration package (one per ``.py`` file, subpackages included)."""
    files = (p for p in (root / PACKAGE_DIR).rglob("*.py") if "__pycache__" not in p.parts)
    return {module_name(p.relative_to(root)) for p in files}


def parse_mypy(output: str) -> dict[str, list[str]]:
    """mypy error lines grouped by module (notes and the summary line are ignored)."""
    errors: dict[str, list[str]] = {}
    for line in output.splitlines():
        if m := _ERROR_RE.match(line.strip()):
            errors.setdefault(module_name(m["path"]), []).append(line.strip())
    return errors


def read_list(text: str) -> list[str]:
    """Module names from ``.strict-typing`` text (blank lines and ``#`` comments ignored)."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def render_list(modules: set[str]) -> str:
    return _HEADER + "".join(f"{m}\n" for m in sorted(modules))


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    clean: set[str] = field(default_factory=set)
    listed: set[str] = field(default_factory=set)


def evaluate(
    modules: set[str],
    mypy_errors: dict[str, list[str]],
    listed: list[str],
    base_listed: list[str] | None = None,
    base_modules: set[str] | None = None,
) -> Result:
    """Apply the ratchet rules; ``base_*`` are the list and the package's modules at ``--base`` (None: no base)."""
    result = Result(listed=set(listed))
    result.clean = {m for m in modules if not mypy_errors.get(m)}
    for dup in sorted({m for m in listed if listed.count(m) > 1}):
        result.errors.append(f"{dup}: listed more than once in {LIST_FILE}")
    for mod in sorted(result.listed):
        if mod not in modules:
            result.errors.append(f"{mod}: listed in {LIST_FILE} but no such module (renamed?): `{UPDATE_HINT}`")
        elif mod in mypy_errors:
            shown = "\n    ".join(mypy_errors[mod][:10])
            more = "" if len(mypy_errors[mod]) <= 10 else f"\n    ... {len(mypy_errors[mod]) - 10} more"
            count = len(mypy_errors[mod])
            result.errors.append(f"{mod}: listed as strict but has {count} mypy error(s):\n    {shown}{more}")
    for mod in sorted(result.clean - result.listed):
        result.errors.append(f"{mod}: passes strict mypy but is not in {LIST_FILE}; add it (`{UPDATE_HINT}`)")
    if base_listed is not None:
        for mod in sorted(set(base_listed) - result.listed):
            if mod in modules:
                result.errors.append(f"{mod}: was in {LIST_FILE} on the base branch; the list only grows")
    if base_modules is not None:
        for mod in sorted(modules - base_modules):
            if mod in mypy_errors:
                result.errors.append(
                    f"{mod}: new module with {len(mypy_errors[mod])} strict mypy error(s); new modules must pass "
                    f"from day one:\n    " + "\n    ".join(mypy_errors[mod][:10])
                )
    return result


def run_mypy(root: Path = REPO) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", str(PACKAGE_DIR)], cwd=root, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


def _git_show(ref: str, path: str, root: Path) -> str | None:
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def base_state(ref: str, root: Path = REPO) -> tuple[list[str], set[str]] | None:
    """The list and the package's modules at ``ref``; None when ``ref`` cannot be read."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", PACKAGE_DIR.as_posix()],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    mods = {module_name(p) for p in proc.stdout.splitlines() if p.endswith(".py")}
    listed = read_list(_git_show(ref, LIST_FILE, root) or "")
    return listed, mods


def summary_markdown(result: Result, modules: set[str], mypy_errors: dict[str, list[str]]) -> str:
    lines = [
        "## Strict typing (Issue #1645)",
        "",
        f"{len(result.listed & modules)} of {len(modules)} modules listed in `{LIST_FILE}` "
        f"({len(result.clean)} pass strict mypy today).",
        "",
    ]
    if result.errors:
        lines += ["**Ratchet failures:**", "", *(f"- {e.splitlines()[0]}" for e in result.errors), ""]
    left = sorted(((len(v), k) for k, v in mypy_errors.items() if k in modules), reverse=True)
    if left:
        lines += ["| Module not yet strict | mypy errors |", "|---|---:|"]
        lines += [f"| `{k.removeprefix(PACKAGE + '.') or '__init__'}` | {n} |" for n, k in left]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="git ref to compare with (the PR's base branch)")
    parser.add_argument("--update", action="store_true", help=f"add newly clean modules to {LIST_FILE}, drop gone ones")
    parser.add_argument("--mypy-output", type=Path, help="read this saved mypy report instead of running mypy")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary to this file")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root: Path = args.root

    if args.mypy_output:
        output = args.mypy_output.read_text(encoding="utf-8")
        code = 1 if _FOUND_RE.search(output) else 0
    else:
        code, output = run_mypy(root)
    mypy_errors = parse_mypy(output)
    finished = _FOUND_RE.search(output) if code == 1 else _SUCCESS_RE.search(output)
    if code not in (0, 1) or not finished or (code == 1 and not mypy_errors):
        print(output, file=sys.stderr)
        print(f"mypy did not run cleanly (exit {code}); no ratchet verdict", file=sys.stderr)
        return 2

    modules = package_modules(root)
    list_path = root / LIST_FILE
    listed = read_list(list_path.read_text(encoding="utf-8")) if list_path.is_file() else []

    if args.update:
        regressed = sorted(m for m in set(listed) & modules if m in mypy_errors)
        if regressed:
            print(f"Not updating: listed modules have mypy errors (fix them, the list never shrinks): {regressed}")
            return 1
        new = (set(listed) & modules) | {m for m in modules if m not in mypy_errors}
        list_path.write_text(render_list(new), encoding="utf-8")
        added, dropped = sorted(new - set(listed)), sorted(set(listed) - new)
        print(f"{LIST_FILE}: {len(new)} modules (added {len(added)}: {added}; dropped {len(dropped)}: {dropped})")
        return 0

    base_listed = base_modules = None
    if args.base:
        state = base_state(args.base, root)
        if state is None:
            print(f"cannot read git ref {args.base!r}; is the checkout shallow?", file=sys.stderr)
            return 2
        base_listed, base_modules = state

    result = evaluate(modules, mypy_errors, listed, base_listed, base_modules)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(summary_markdown(result, modules, mypy_errors))
    for err in result.errors:
        print(f"FAIL {err}")
    total = sum(len(v) for k, v in mypy_errors.items() if k in modules)
    print(
        f"strict typing: {len(result.listed & modules)}/{len(modules)} modules listed, {len(result.clean)} clean, "
        f"{total} mypy errors in the {len(modules) - len(result.clean)} not yet strict"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
