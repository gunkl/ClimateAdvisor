#!/usr/bin/env python3
"""Build the README's test badges as shields.io endpoint JSON (Issue #1343).

``.github/workflows/badges.yml`` runs both test tiers with coverage and JUnit XML, then calls this
script and pushes the JSON files to the ``badges`` branch. The README shows each one through
``https://img.shields.io/endpoint?url=<raw file>&style=flat-square``, the same flat-square look as the
released/development version badges.

Standard library only. A missing or unreadable input never fails the run: the badge says
``unknown`` in grey, so a broken pipeline is visible instead of a stale green number.

Usage (all paths relative to the repo root)::

    python tools/badges.py --out badges \\
        --unit-junit reports/unit.xml --unit-coverage reports/coverage-unit.json \\
        --ha-junit reports/ha.xml --ha-coverage reports/coverage-ha.json \\
        --coverage reports/coverage.json \\
        --ledger tools/sim_harness/realha_ledger.json
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# The parametrised tests that run every golden scenario on each world.
GOLDEN_TEST = "test_golden_passes_production_engine"
REALHA_GOLDEN_TEST = "test_golden_on_real_ha"

UNKNOWN_COLOR = "lightgrey"


@dataclass
class JUnitCounts:
    """Pass/fail counts from a JUnit XML report, overall and per test function."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    by_test: dict[str, list[int]] = field(default_factory=dict)  # name -> [passed, failed]

    def for_test(self, name: str) -> tuple[int, int]:
        passed, failed = self.by_test.get(name, [0, 0])
        return passed, failed


def read_junit(path: Path) -> JUnitCounts | None:
    """Count the testcases in a JUnit XML file; None when it is missing or not JUnit."""
    try:
        root = ET.parse(path).getroot()
    except OSError, ET.ParseError:
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    counts = JUnitCounts()
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        name = case.get("name", "").split("[", 1)[0]
        tally = counts.by_test.setdefault(name, [0, 0])
        if "skipped" in tags:
            counts.skipped += 1
        elif tags & {"failure", "error"}:
            counts.failed += 1
            tally[1] += 1
        else:
            counts.passed += 1
            tally[0] += 1
    return counts


def read_coverage_percent(path: Path) -> float | None:
    """``totals.percent_covered`` from a ``coverage json`` report; None when unavailable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data["totals"]["percent_covered"])
    except OSError, ValueError, KeyError, TypeError:
        return None


def read_ledger_count(path: Path) -> int | None:
    """Number of triaged real-HA golden divergences in the ledger; None when unavailable."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    except OSError, ValueError, KeyError, TypeError:
        return None
    return len(entries) if isinstance(entries, list) else None


def coverage_color(percent: float) -> str:
    """shields.io colour for a coverage percentage (higher is greener)."""
    for floor, color in ((90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow"), (50, "orange")):
        if percent >= floor:
            return color
    return "red"


def endpoint(label: str, message: str, color: str) -> dict:
    """One shields.io endpoint document (https://shields.io/badges/endpoint-badge)."""
    return {"schemaVersion": 1, "label": label, "message": message, "color": color}


def unknown(label: str) -> dict:
    return endpoint(label, "unknown", UNKNOWN_COLOR)


def tier_badge(label: str, junit: JUnitCounts | None, coverage: float | None) -> dict:
    """``12,107 passed · 81% cov``; red with the failure count when any test failed."""
    if junit is None:
        return unknown(label)
    message = f"{junit.passed:,} passed"
    if junit.failed:
        message += f", {junit.failed:,} failed"
    if coverage is not None:
        message += f" · {coverage:.0f}% cov"
    return endpoint(label, message, "red" if junit.failed else "brightgreen")


def coverage_badge(label: str, coverage: float | None) -> dict:
    if coverage is None:
        return unknown(label)
    return endpoint(label, f"{coverage:.0f}%", coverage_color(coverage))


def goldens_badge(label: str, junit: JUnitCounts | None) -> dict:
    """Tier A goldens through the real engine: ``94/94 pass``."""
    if junit is None:
        return unknown(label)
    passed, failed = junit.for_test(GOLDEN_TEST)
    if passed + failed == 0:
        return unknown(label)
    return endpoint(label, f"{passed}/{passed + failed} pass", "red" if failed else "brightgreen")


def realha_goldens_badge(label: str, junit: JUnitCounts | None, triaged: int | None) -> dict:
    """Goldens on real Home Assistant: ``89 match · 5 triaged``.

    A golden listed in the ledger passes its test (the ledger is a ratchet: the listed divergence is the
    expected result), so it is counted as triaged, not as a match. Any failure turns the badge red.
    """
    if junit is None:
        return unknown(label)
    passed, failed = junit.for_test(REALHA_GOLDEN_TEST)
    if passed + failed == 0:
        return unknown(label)
    if failed:
        return endpoint(label, f"{failed} failing", "red")
    if triaged is None:
        return endpoint(label, f"{passed} pass", "brightgreen")
    match = max(passed - triaged, 0)
    if not triaged:
        return endpoint(label, f"{match} match", "brightgreen")
    return endpoint(label, f"{match} match · {triaged} triaged", "green")


def build(args: argparse.Namespace) -> dict[str, dict]:
    """Every badge document, keyed by file stem."""
    unit = read_junit(args.unit_junit)
    ha = read_junit(args.ha_junit)
    return {
        "coverage": coverage_badge("coverage", read_coverage_percent(args.coverage)),
        "unit": tier_badge("unit tests", unit, read_coverage_percent(args.unit_coverage)),
        "real-ha": tier_badge("real HA tests", ha, read_coverage_percent(args.ha_coverage)),
        "goldens": goldens_badge("goldens", unit),
        "real-ha-goldens": realha_goldens_badge("goldens on real HA", ha, read_ledger_count(args.ledger)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="Directory for the <name>.json badge files.")
    parser.add_argument("--unit-junit", type=Path, default=Path("reports/unit.xml"))
    parser.add_argument("--unit-coverage", type=Path, default=Path("reports/coverage-unit.json"))
    parser.add_argument("--ha-junit", type=Path, default=Path("reports/ha.xml"))
    parser.add_argument("--ha-coverage", type=Path, default=Path("reports/coverage-ha.json"))
    parser.add_argument("--coverage", type=Path, default=Path("reports/coverage.json"))
    parser.add_argument("--ledger", type=Path, default=Path("tools/sim_harness/realha_ledger.json"))
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for name, doc in build(args).items():
        (args.out / f"{name}.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        print(f"{name}: {doc['label']} = {doc['message']} ({doc['color']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
