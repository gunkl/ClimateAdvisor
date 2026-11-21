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
        --ledger tools/sim_harness/realha_ledger.json \\
        --quality-scale custom_components/climate_advisor/quality_scale.yaml \\
        --strict-typing .strict-typing --package custom_components/climate_advisor

Issue #1668 adds two badges that need no test run: the quality-scale tier, read from the integration's
``quality_scale.yaml`` (our self-assessment, #1637), and strict typing, the modules listed in
``.strict-typing`` out of every module in the package.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# The parametrised tests that run every golden scenario on each world.
GOLDEN_TEST = "test_golden_passes_production_engine"
REALHA_GOLDEN_TEST = "test_golden_on_real_ha"

UNKNOWN_COLOR = "lightgrey"

# Issue #1668: the 54 quality-scale rules by tier (https://contributors.home-assistant.io/core/quality-scale/rules/).
# A tier is reached when every rule in it and in every lower tier is done or exempt.
QUALITY_TIERS: dict[str, tuple[str, ...]] = {
    "Bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
        "docs-conditions",
        "docs-triggers",
    ),
    "Silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "Gold": (
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "Platinum": ("async-dependency", "inject-websession", "strict-typing"),
}
QUALITY_STATUSES = ("done", "exempt", "todo")
# Medal colours; shields.io picks dark or light text to suit each.
TIER_COLORS = {"Bronze": "cd7f32", "Silver": "a8a9ad", "Gold": "d4af37", "Platinum": "8fb8c9"}
QUALITY_LABEL = "quality scale (self-assessed)"


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


_RULE_RE = re.compile(r"^  (?P<rule>[a-z][a-z0-9-]*):(?:\s+(?P<status>[a-z]+))?\s*(?:#.*)?$")
_STATUS_RE = re.compile(r"^    status:\s+(?P<status>[a-z]+)\s*(?:#.*)?$")


def read_quality_scale(path: Path) -> dict[str, str] | None:
    """Rule -> status from a Home Assistant ``quality_scale.yaml``; None when missing or malformed.

    Standard library only, so it reads just the file's fixed shape: a top-level ``rules:`` block whose
    two-space keys are either ``rule: status`` or ``rule:`` with an indented ``status: ...`` line.
    tests/test_quality_scale_1668.py checks this agrees with a real YAML parser on the committed file.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return None
    rules: dict[str, str] = {}
    in_rules = False
    pending: str | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_rules = line.rstrip() == "rules:"
            pending = None
            continue
        if not in_rules:
            continue
        rule = _RULE_RE.match(line)
        if rule:
            if pending is not None:
                return None  # the previous rule had no status
            if rule["status"]:
                rules[rule["rule"]] = rule["status"]
            else:
                pending = rule["rule"]
            continue
        status = _STATUS_RE.match(line)
        if status and pending is not None:
            rules[pending] = status["status"]
            pending = None
    if pending is not None or not rules:
        return None
    if any(value not in QUALITY_STATUSES for value in rules.values()):
        return None
    return rules


def quality_tier(rules: dict[str, str]) -> str | None:
    """Highest tier whose rules, and every lower tier's, are all done or exempt; None below Bronze."""
    reached = None
    for tier, names in QUALITY_TIERS.items():
        if all(rules.get(name) in ("done", "exempt") for name in names):
            reached = tier
        else:
            break
    return reached


def quality_badge(label: str, rules: dict[str, str] | None) -> dict:
    """``Bronze · 50/54``; ``working toward Bronze · 30/54`` in grey below the first tier.

    Unknown when the file is missing or does not list exactly the 54 rules, so a typo cannot inflate the tier.
    """
    every = {name for names in QUALITY_TIERS.values() for name in names}
    if rules is None or set(rules) != every:
        return unknown(label)
    met = sum(1 for name in every if rules[name] in ("done", "exempt"))
    tier = quality_tier(rules)
    count = f"{met}/{len(every)}"
    if tier is None:
        return endpoint(label, f"working toward Bronze · {count}", UNKNOWN_COLOR)
    return endpoint(label, f"{tier} · {count}", TIER_COLORS[tier])


def read_strict_typing(list_file: Path, package: Path) -> tuple[int, int] | None:
    """(listed modules, package modules), the same module count as tools/typing_ratchet.py; None when unreadable."""
    try:
        listed = [
            line.strip()
            for line in list_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError, UnicodeDecodeError:
        return None
    total = sum(1 for p in package.rglob("*.py") if "__pycache__" not in p.parts)
    if not listed or not total:
        return None
    return len(listed), total


def typing_badge(label: str, counts: tuple[int, int] | None) -> dict:
    """``63/113 modules``, coloured by the share that is strict."""
    if counts is None:
        return unknown(label)
    listed, total = counts
    return endpoint(label, f"{listed}/{total} modules", coverage_color(100 * listed / total))


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
        "quality-scale": quality_badge(QUALITY_LABEL, read_quality_scale(args.quality_scale)),
        "strict-typing": typing_badge("strict typing", read_strict_typing(args.strict_typing, args.package)),
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
    parser.add_argument(
        "--quality-scale", type=Path, default=Path("custom_components/climate_advisor/quality_scale.yaml")
    )
    parser.add_argument("--strict-typing", type=Path, default=Path(".strict-typing"))
    parser.add_argument("--package", type=Path, default=Path("custom_components/climate_advisor"))
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for name, doc in build(args).items():
        (args.out / f"{name}.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        print(f"{name}: {doc['label']} = {doc['message']} ({doc['color']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
