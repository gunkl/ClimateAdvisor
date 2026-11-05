"""Issue #1343: tools/badges.py turns test reports into the README's shields.io endpoint badges.

Reader view: each badge must say what the last main run really found. A failure turns it red, and a missing
report says "unknown" in grey instead of a number that looks like a pass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "tools" / "badges.py"
_spec = importlib.util.spec_from_file_location("ca_badges", _PATH)
badges = importlib.util.module_from_spec(_spec)
sys.modules["ca_badges"] = badges
_spec.loader.exec_module(badges)


def _junit(tmp_path: Path, cases: list[tuple[str, str | None]]) -> Path:
    """cases: (name, None | "failure" | "error" | "skipped")."""
    body = "".join(
        f'<testcase classname="t" name="{name}">' + (f"<{kind}/>" if kind else "") + "</testcase>"
        for name, kind in cases
    )
    path = tmp_path / "r.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>', encoding="utf-8")
    return path


def test_counts_pass_fail_and_skip_per_test(tmp_path):
    path = _junit(
        tmp_path,
        [
            ("test_golden_passes_production_engine[a]", None),
            ("test_golden_passes_production_engine[b]", "failure"),
            ("test_other", "error"),
            ("test_skip", "skipped"),
            ("test_ok", None),
        ],
    )
    counts = badges.read_junit(path)
    assert (counts.passed, counts.failed, counts.skipped) == (2, 2, 1)
    assert counts.for_test("test_golden_passes_production_engine") == (1, 1)


def test_tier_badge_is_green_with_coverage_and_red_on_any_failure(tmp_path):
    green = badges.tier_badge("unit tests", badges.read_junit(_junit(tmp_path, [("t", None)] * 3)), 81.4)
    assert green == {"schemaVersion": 1, "label": "unit tests", "message": "3 passed · 81% cov", "color": "brightgreen"}
    red = badges.tier_badge("unit tests", badges.read_junit(_junit(tmp_path, [("t", None), ("u", "failure")])), None)
    assert red["message"] == "1 passed, 1 failed" and red["color"] == "red"


def test_large_counts_use_thousands_separators(tmp_path):
    counts = badges.JUnitCounts(passed=12107)
    assert badges.tier_badge("unit tests", counts, None)["message"] == "12,107 passed"


def test_missing_or_broken_reports_say_unknown(tmp_path):
    assert badges.read_junit(tmp_path / "absent.xml") is None
    (tmp_path / "bad.xml").write_text("not xml", encoding="utf-8")
    assert badges.read_junit(tmp_path / "bad.xml") is None
    assert badges.tier_badge("unit tests", None, 90.0)["message"] == "unknown"
    assert badges.coverage_badge("coverage", None)["color"] == "lightgrey"
    assert badges.read_coverage_percent(tmp_path / "absent.json") is None


def test_coverage_colour_thresholds():
    assert [badges.coverage_color(p) for p in (95, 85, 75, 65, 55, 10)] == [
        "brightgreen",
        "green",
        "yellowgreen",
        "yellow",
        "orange",
        "red",
    ]


def test_goldens_badge_counts_only_the_golden_test(tmp_path):
    path = _junit(tmp_path, [(f"{badges.GOLDEN_TEST}[g{i}]", None) for i in range(3)] + [("test_x", None)])
    assert badges.goldens_badge("goldens", badges.read_junit(path))["message"] == "3/3 pass"
    assert badges.goldens_badge("goldens", badges.read_junit(_junit(tmp_path, [("test_x", None)])))["message"] == (
        "unknown"
    )


def test_real_ha_goldens_separate_matches_from_ledgered_divergences(tmp_path):
    path = _junit(tmp_path, [(f"{badges.REALHA_GOLDEN_TEST}[g{i}]", None) for i in range(10)])
    doc = badges.realha_goldens_badge("goldens on real HA", badges.read_junit(path), 3)
    assert doc["message"] == "7 match · 3 triaged" and doc["color"] == "green"
    clean = badges.realha_goldens_badge("goldens on real HA", badges.read_junit(path), 0)
    assert clean == {"schemaVersion": 1, "label": "goldens on real HA", "message": "10 match", "color": "brightgreen"}
    failing = _junit(
        tmp_path, [(f"{badges.REALHA_GOLDEN_TEST}[a]", None), (f"{badges.REALHA_GOLDEN_TEST}[b]", "failure")]
    )
    assert badges.realha_goldens_badge("goldens on real HA", badges.read_junit(failing), 3)["color"] == "red"


def test_ledger_count_reads_the_real_ledger():
    ledger = Path(__file__).resolve().parent.parent / "tools" / "sim_harness" / "realha_ledger.json"
    assert badges.read_ledger_count(ledger) == len(json.loads(ledger.read_text(encoding="utf-8"))["entries"])


def test_main_writes_one_endpoint_file_per_badge(tmp_path):
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps({"totals": {"percent_covered": 83.2}}), encoding="utf-8")
    out = tmp_path / "badges"
    badges.main(["--out", str(out), "--coverage", str(cov), "--unit-junit", str(tmp_path / "none.xml")])
    names = sorted(p.stem for p in out.glob("*.json"))
    assert names == ["coverage", "goldens", "real-ha", "real-ha-goldens", "unit"]
    assert json.loads((out / "coverage.json").read_text(encoding="utf-8"))["message"] == "83%"
    assert json.loads((out / "unit.json").read_text(encoding="utf-8"))["message"] == "unknown"
