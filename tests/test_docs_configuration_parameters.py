"""Quality scale Silver ``docs-configuration-parameters``: every setting is documented for users.

Occupant view: a setting that is explained only in the form's small print cannot be looked up before
you open the form, compared with its default, or found from a search. ``docs/configuration-options.md``
lists every setting of the setup wizard, the reconfigure and reauth steps and every Configure section,
with the label the person sees, what it does, its default and its values.

The setting keys are derived, never copied: from ``strings.json`` (the ``data`` labels Home Assistant
renders for each config and options step) and from ``config_flow.py`` itself (every ``vol.Required`` /
``vol.Optional`` key the step handlers build, read with ``ast``). ``tests/test_config_flow_strings.py``
keeps those two in step with each other. A new option therefore fails here until it has a row in the
reference, and a removed one fails until its row is gone.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "climate_advisor"
REFERENCE = ROOT / "docs" / "configuration-options.md"
STRINGS = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
_FLOWS = ("config", "options")

# Keys a flow shows but that are not user settings and so need no row. Each entry says why.
# Keep this empty unless a key is truly internal or hidden; a real setting belongs in the reference.
_EXCLUDED_KEYS: dict[str, str] = {}

# One reference row: | **Label** `key` | what it does | default | values |
_ROW = re.compile(
    r"^\| \*\*(?P<label>[^*]+)\*\* `(?P<key>[a-z0-9_]+)` "
    r"\| (?P<what>[^|]+?) \| (?P<default>[^|]+?) \| (?P<values>[^|]+?) \|$",
    flags=re.M,
)


def _ui_labels() -> dict[str, str]:
    """Every field key of every config/options step, with the label the UI shows for it."""
    labels: dict[str, str] = {}
    for flow in _FLOWS:
        for step in STRINGS[flow]["step"].values():
            for key, label in step.get("data", {}).items():
                labels.setdefault(key, label)
    return labels


def _schema_keys() -> set[str]:
    """Every schema key the flow handlers build, read from ``config_flow.py`` (literal or ``const`` name).

    Keys built in a loop from a variable (the setpoint and delta helpers) are not visible here; they are
    in ``strings.json``, which ``test_config_flow_strings.py`` holds equal to the handlers' schemas.
    """
    const = importlib.import_module("custom_components.climate_advisor.const")
    tree = ast.parse((PKG / "config_flow.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "vol"
            and node.func.attr in ("Required", "Optional")
            and node.args
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            keys.add(arg.value)
        elif isinstance(arg, ast.Name) and isinstance(getattr(const, arg.id, None), str):
            keys.add(getattr(const, arg.id))
    return keys


def _option_keys() -> set[str]:
    return (set(_ui_labels()) | _schema_keys()) - set(_EXCLUDED_KEYS)


def _reference_rows() -> dict[str, re.Match[str]]:
    rows: dict[str, re.Match[str]] = {}
    for match in _ROW.finditer(REFERENCE.read_text(encoding="utf-8")):
        assert match["key"] not in rows, f"`{match['key']}` has two rows in {REFERENCE.name}"
        rows[match["key"]] = match
    return rows


def test_derivation_finds_the_flow_options():
    """Guard the derivation itself: both sources see the settings, so an empty result cannot pass."""
    schema = _schema_keys()
    labels = _ui_labels()
    assert len(labels) > 100, len(labels)
    assert {"weather_entity", "sensor_debounce_seconds", "applies_to", "github_repo"} <= schema
    assert {"comfort_heat", "comfort_deadband_hot_f", "vacation_cool"} <= set(labels)


def test_every_option_is_documented():
    missing = sorted(_option_keys() - set(_reference_rows()))
    assert not missing, (
        f"settings with no row in docs/{REFERENCE.name} (add '| **Label** `key` | what | default | values |'): "
        f"{missing}"
    )


def test_no_row_for_a_setting_that_no_longer_exists():
    stale = sorted(set(_reference_rows()) - _option_keys())
    assert not stale, f"docs/{REFERENCE.name} documents settings the flows no longer show: {stale}"


def test_rows_use_the_label_the_form_shows():
    """The person looks for the label on screen, so the row must quote it exactly."""
    labels = _ui_labels()
    wrong = {
        key: (row["label"], labels[key])
        for key, row in _reference_rows().items()
        if key in labels and row["label"] != labels[key]
    }
    assert not wrong, f"row label differs from the strings.json label (row, UI): {wrong}"


def test_rows_say_what_it_does_and_the_default():
    for key, row in _reference_rows().items():
        assert len(row["what"].split()) >= 4, f"`{key}`: say what the setting does"
        assert row["default"].strip(), f"`{key}`: state the default"
        assert row["values"].strip(), f"`{key}`: state the allowed values"


def test_sections_follow_the_configure_menu():
    """One section per Configure menu entry, titled as the menu shows it, in menu order."""
    text = REFERENCE.read_text(encoding="utf-8")
    menu = list(STRINGS["options"]["step"]["init"]["menu_options"].values())
    positions = []
    for label in menu:
        match = re.search(rf"^## {re.escape(label)}$", text, flags=re.M)
        assert match, f"no '## {label}' section for the Configure menu entry"
        positions.append(match.start())
    assert positions == sorted(positions), "sections should follow the Configure menu order"


def test_readme_links_the_reference():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/configuration-options.md)" in readme
