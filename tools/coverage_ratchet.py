#!/usr/bin/env python3
"""Per-module coverage report and ratchet for the integration (Issue #1644, Silver ``test-coverage``).

The Silver rule asks for more than 95 % coverage in every module. ``tools/coverage_floors.json`` records each
module's floor: its line-and-branch coverage, both test tiers combined, when the floor was last raised. CI
(``validate.yml``, job ``coverage``) collects coverage in every unit and real-HA slice, combines it, writes the
``coverage json`` report and runs ``check`` on it:

* a module below its floor (less the file's ``tolerance``) fails;
* a new module (no floor yet) below the 95 % target fails: test it, or record its floor deliberately (``update``);
* a floor for a module that no longer exists fails (stale entry: ``update`` drops it);
* a module well above its floor is a note, not a failure: raise the floors with ``update`` (never lowers one).

Usage (``reports/coverage.json`` is a ``coverage json`` report of ``custom_components/climate_advisor``)::

    python tools/coverage_ratchet.py check  reports/coverage.json [--summary FILE]
    python tools/coverage_ratchet.py update reports/coverage.json
    python tools/coverage_ratchet.py report reports/coverage.json [--summary FILE]

``report`` prints the per-module table (modules under the target first, largest gap first) and always exits 0.
``--summary`` appends the table as Markdown (CI passes ``$GITHUB_STEP_SUMMARY``). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_PREFIX = "custom_components/climate_advisor/"
FLOORS_FILE = REPO / "tools" / "coverage_floors.json"
DEFAULT_TARGET = 95.0
UPDATE_HINT = "python tools/coverage_ratchet.py update reports/coverage.json"
# A floor this far below the measured value is worth raising (a note, never a failure).
RAISE_NOTE_MARGIN = 0.5


@dataclass(frozen=True)
class ModuleCoverage:
    module: str  # path inside the package, e.g. "coordinator.py"
    percent: float
    missing_lines: int
    missing_branches: int


def read_coverage(path: Path) -> dict[str, ModuleCoverage]:
    """Per-module coverage from a ``coverage json`` report (only the integration package's files)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, ModuleCoverage] = {}
    for name, entry in data.get("files", {}).items():
        rel = name.replace("\\", "/")
        if PACKAGE_PREFIX not in rel:
            continue
        module = rel.split(PACKAGE_PREFIX, 1)[1]
        summary = entry["summary"]
        out[module] = ModuleCoverage(
            module=module,
            percent=float(summary["percent_covered"]),
            missing_lines=int(summary.get("missing_lines", 0)),
            missing_branches=int(summary.get("missing_branches", 0)) + int(summary.get("num_partial_branches", 0)),
        )
    return out


def floor_of(percent: float) -> float:
    """The floor recorded for a measured percentage: rounded down to one decimal."""
    return math.floor(round(percent, 6) * 10) / 10


@dataclass
class Floors:
    target: float = DEFAULT_TARGET
    tolerance: float = 0.0
    modules: dict[str, float] = field(default_factory=dict)
    description: str = ""


def load_floors(path: Path = FLOORS_FILE) -> Floors:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Floors(
        target=float(data.get("target", DEFAULT_TARGET)),
        tolerance=float(data.get("tolerance", 0.0)),
        modules={k: float(v) for k, v in data.get("modules", {}).items()},
        description=str(data.get("description", "")),
    )


def save_floors(floors: Floors, path: Path = FLOORS_FILE) -> None:
    payload = {
        "description": floors.description,
        "target": floors.target,
        "tolerance": floors.tolerance,
        "modules": dict(sorted(floors.modules.items())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check(coverage: dict[str, ModuleCoverage], floors: Floors) -> CheckResult:
    result = CheckResult()
    for module, cov in sorted(coverage.items()):
        floor = floors.modules.get(module)
        if floor is None:
            if cov.percent < floors.target:
                result.errors.append(
                    f"{module}: new module at {cov.percent:.1f}% is below the {floors.target:.0f}% target; add tests, "
                    f"or record its floor deliberately with `{UPDATE_HINT}`"
                )
            else:
                result.notes.append(f"{module}: new module at {cov.percent:.1f}%; record its floor: `{UPDATE_HINT}`")
        elif cov.percent < floor - floors.tolerance:
            result.errors.append(
                f"{module}: coverage {cov.percent:.2f}% fell below its floor {floor:.1f}% "
                f"({cov.missing_lines} lines, {cov.missing_branches} branches not covered); cover the new code"
            )
        elif floor_of(cov.percent) >= floor + RAISE_NOTE_MARGIN:
            result.notes.append(f"{module}: {cov.percent:.1f}% is above its floor {floor:.1f}%")
    for module in sorted(set(floors.modules) - set(coverage)):
        result.errors.append(f"{module}: has a floor but no coverage data (renamed or deleted?); run `{UPDATE_HINT}`")
    if any("above its floor" in n for n in result.notes):
        result.notes.append(f"raise the floors with `{UPDATE_HINT}`")
    return result


def update(coverage: dict[str, ModuleCoverage], floors: Floors) -> tuple[Floors, list[str]]:
    """Raise floors to the measured values (never lower one), add new modules, drop modules that are gone."""
    changes: list[str] = []
    new = dict(floors.modules)
    for module, cov in sorted(coverage.items()):
        measured = floor_of(cov.percent)
        old = new.get(module)
        if old is None:
            new[module] = measured
            changes.append(f"{module}: added at {measured:.1f}%")
        elif measured > old:
            new[module] = measured
            changes.append(f"{module}: {old:.1f}% -> {measured:.1f}%")
    for module in sorted(set(new) - set(coverage)):
        del new[module]
        changes.append(f"{module}: dropped (no such module)")
    return Floors(floors.target, floors.tolerance, new, floors.description), changes


def report_markdown(coverage: dict[str, ModuleCoverage], floors: Floors | None, target: float) -> str:
    """The per-module table: modules under the target first, largest gap first, then the rest by coverage."""
    rows = sorted(coverage.values(), key=lambda c: (c.percent, c.module))
    under = [c for c in rows if c.percent < target]
    lines = [
        "## Coverage per module (Issue #1644)",
        "",
        f"{len(coverage) - len(under)} of {len(coverage)} modules at or above the {target:.0f}% target; "
        f"{len(under)} below it, largest gap first.",
        "",
        "| Module | Coverage | Floor | Gap to target | Lines missed | Branches missed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for c in rows:
        floor = floors.modules.get(c.module) if floors else None
        gap = f"{target - c.percent:.1f}" if c.percent < target else ""
        floor_txt = f"{floor:.1f}" if floor is not None else "new"
        lines.append(
            f"| `{c.module}` | {c.percent:.1f}% | {floor_txt} | {gap} | {c.missing_lines} | {c.missing_branches} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "update", "report"))
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--floors", type=Path, default=FLOORS_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--summary", type=Path, help="append the Markdown table to this file")
    args = parser.parse_args(argv)

    try:
        coverage = read_coverage(args.coverage_json)
    except (OSError, ValueError, KeyError) as err:
        print(f"cannot read coverage report {args.coverage_json}: {err}", file=sys.stderr)
        return 0 if args.command == "report" else 2
    if not coverage:
        print(f"no {PACKAGE_PREFIX} files in {args.coverage_json}", file=sys.stderr)
        return 0 if args.command == "report" else 2
    floors = load_floors(args.floors) if args.floors.is_file() else None

    if args.command == "update":
        new, changes = update(coverage, floors or Floors())
        save_floors(new, args.floors)
        print("\n".join(changes) or "floors unchanged")
        return 0

    table = report_markdown(coverage, floors, floors.target if floors else DEFAULT_TARGET)
    if args.command == "report":
        print(table)
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as fh:
                fh.write(table)
        return 0

    if floors is None:
        print(f"{args.floors} is missing; create it with `{UPDATE_HINT}`", file=sys.stderr)
        return 2
    result = check(coverage, floors)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            if result.errors:
                fh.write("**Coverage ratchet failures:**\n\n" + "".join(f"- {e}\n" for e in result.errors) + "\n")
            fh.write(table)
    for note in result.notes:
        print(f"note: {note}")
    for err in result.errors:
        print(f"FAIL {err}")
    print(f"coverage ratchet: {len(coverage)} modules, {len(result.errors)} failure(s)")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
