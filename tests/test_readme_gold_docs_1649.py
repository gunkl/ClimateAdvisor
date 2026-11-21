"""Issue #1649: the README's user docs (quality scale Gold docs rules) stay true to the integration.

The Troubleshooting section quotes Repairs card titles and AI Status labels, and the Examples section
calls services and uses Day Type values. If one of those is renamed in the code, an occupant following
the README looks for a card or a state that no longer exists. These tests tie the README text to the
strings Home Assistant actually shows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PKG = ROOT / "custom_components" / "climate_advisor"
STRINGS = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))


def _section(title: str) -> str:
    """Return the text of a level-2 README section, up to the next level-2 heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## )", README, flags=re.S | re.M)
    assert match, f"README has no '## {title}' section"
    return match.group(1)


def test_gold_docs_sections_present():
    """Every Gold docs rule has a README home (docs-troubleshooting, -examples, -supported-devices, ...)."""
    for heading in (
        "## Troubleshooting",
        "### Reporting a problem",
        "## Examples",
        "### Supported devices",
        "### Known limitations",
        "### How data updates",
        "## Entities",
        "## Services",
    ):
        assert re.search(rf"^{re.escape(heading)}$", README, flags=re.M), heading


def test_troubleshooting_quotes_real_repairs_titles():
    text = _section("Troubleshooting")
    issues = STRINGS["issues"]
    for key in ("weather_entity_not_found", "reload_needed", "zone_resolution_ambiguous", "builtin_claude_deprecated"):
        title = issues[key]["title"].rstrip(".")
        assert title in text, f"Troubleshooting should name the '{title}' Repairs card ({key})"


def test_troubleshooting_quotes_real_ai_status_labels():
    text = _section("Troubleshooting")
    states = STRINGS["entity"]["sensor"]["ai_status"]["state"]
    for key in ("circuit_open", "budget_exceeded"):
        assert states[key] in text, f"Troubleshooting should use the AI Status label for {key}"


def test_examples_use_real_services_and_day_types():
    text = _section("Examples")
    services = yaml.safe_load((PKG / "services.yaml").read_text(encoding="utf-8"))
    used = set(re.findall(r"climate_advisor\.([a-z_]+)", text))
    assert used, "Examples should show at least one Climate Advisor service"
    assert used <= set(services), f"unknown services in Examples: {used - set(services)}"
    day_types = set(STRINGS["entity"]["sensor"]["day_type"]["state"])
    for value in re.findall(r'entity_id: sensor\.living_room_day_type\n\s+state: "([a-z]+)"', text):
        assert value in day_types
