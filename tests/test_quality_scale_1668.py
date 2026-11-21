"""Issue #1668: the quality-scale self-assessment file and the two badges built from it.

Reader view: the README's quality badge must name the tier the checklist really reaches. A rule left out,
misspelt or given an unknown status makes the badge say "unknown" instead of claiming a tier.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
QUALITY_FILE = REPO / "custom_components" / "climate_advisor" / "quality_scale.yaml"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


badges = _load("ca_badges_1668", "tools/badges.py")
typing_ratchet = _load("ca_typing_ratchet_1668", "tools/typing_ratchet.py")

ALL_RULES = {name for names in badges.QUALITY_TIERS.values() for name in names}


def _statuses_via_yaml() -> dict[str, str]:
    data = yaml.safe_load(QUALITY_FILE.read_text(encoding="utf-8"))
    return {rule: (value if isinstance(value, str) else value["status"]) for rule, value in data["rules"].items()}


def test_tier_table_has_the_54_rules_once_each():
    flat = [name for names in badges.QUALITY_TIERS.values() for name in names]
    assert len(flat) == 54
    assert len(set(flat)) == 54
    assert [len(names) for names in badges.QUALITY_TIERS.values()] == [20, 10, 21, 3]


def test_file_lists_every_rule_with_a_known_status():
    statuses = _statuses_via_yaml()
    assert set(statuses) == ALL_RULES
    assert set(statuses.values()) <= set(badges.QUALITY_STATUSES)


def test_every_exempt_or_todo_rule_says_why():
    data = yaml.safe_load(QUALITY_FILE.read_text(encoding="utf-8"))["rules"]
    missing = [
        rule
        for rule, value in data.items()
        if (value if isinstance(value, str) else value["status"]) != "done"
        and not (isinstance(value, dict) and str(value.get("comment", "")).strip())
    ]
    assert missing == []


def test_stdlib_reader_agrees_with_a_real_yaml_parser():
    assert badges.read_quality_scale(QUALITY_FILE) == _statuses_via_yaml()


def test_committed_file_reaches_bronze():
    badge = badges.quality_badge(badges.QUALITY_LABEL, badges.read_quality_scale(QUALITY_FILE))
    assert badge["message"].startswith("Bronze · ")
    assert badge["color"] == badges.TIER_COLORS["Bronze"]


def _rules(**overrides: str) -> dict[str, str]:
    rules = dict.fromkeys(ALL_RULES, "done")
    rules.update({k.replace("_", "-"): v for k, v in overrides.items()})
    return rules


def test_tier_is_the_highest_with_every_lower_tier_met():
    assert badges.quality_tier(_rules()) == "Platinum"
    assert badges.quality_tier(_rules(strict_typing="todo")) == "Gold"
    assert badges.quality_tier(_rules(test_coverage="todo")) == "Bronze"
    assert badges.quality_tier(_rules(brands="todo")) is None
    # A met higher tier does not count while a lower one is open.
    assert badges.quality_tier(_rules(brands="todo", test_coverage="todo")) is None


def test_exempt_counts_as_met():
    assert badges.quality_tier(_rules(discovery="exempt", docs_triggers="exempt")) == "Platinum"


def test_badge_message_and_grey_below_bronze():
    assert badges.quality_badge("q", _rules(test_coverage="todo"))["message"] == "Bronze · 53/54"
    below = badges.quality_badge("q", _rules(brands="todo"))
    assert below["message"] == "working toward Bronze · 53/54"
    assert below["color"] == badges.UNKNOWN_COLOR


def test_missing_extra_or_bad_rules_say_unknown(tmp_path):
    rules = _rules()
    del rules["brands"]
    assert badges.quality_badge("q", rules)["message"] == "unknown"
    assert badges.quality_badge("q", {**_rules(), "brand": "done"})["message"] == "unknown"
    assert badges.quality_badge("q", None)["message"] == "unknown"
    bad = tmp_path / "q.yaml"
    bad.write_text("rules:\n  brands: finished\n", encoding="utf-8")
    assert badges.read_quality_scale(bad) is None
    nostatus = tmp_path / "n.yaml"
    nostatus.write_text("rules:\n  brands:\n    comment: x\n", encoding="utf-8")
    assert badges.read_quality_scale(nostatus) is None
    assert badges.read_quality_scale(tmp_path / "absent.yaml") is None


def test_typing_badge_uses_the_ratchet_module_count():
    counts = badges.read_strict_typing(REPO / ".strict-typing", REPO / "custom_components" / "climate_advisor")
    listed = typing_ratchet.read_list((REPO / ".strict-typing").read_text(encoding="utf-8"))
    assert counts == (len(listed), len(typing_ratchet.package_modules(REPO)))
    assert badges.typing_badge("t", counts)["message"] == f"{counts[0]}/{counts[1]} modules"
    assert badges.typing_badge("t", None)["message"] == "unknown"
