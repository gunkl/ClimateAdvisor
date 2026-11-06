"""Issue #1061: enforce the Config UI Field Standard (CLAUDE.md) on strings.json / translations/en.json.

Reads the real step handlers in ``config_flow.py`` (via ``ast``) so every schema key, step and
raised error is checked against the strings HA actually renders. Rules enforced:

- strings.json and translations/en.json are byte-identical (HA reads en.json at runtime).
- every step handler has a strings block, and every strings step has a handler.
- every schema key has both a ``data`` label and a ``data_description`` (a missing label is what
  makes HA show a raw key such as ``temp_unit``); no stale strings for keys the step lacks.
- labels are at most 5 words, with no parentheses and no unit text (units come from the selector).
- every ``errors[...] = "x"`` raised by a flow has a string under that flow's ``error``.
- no mojibake.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_STRINGS = _PKG / "strings.json"
_EN = _PKG / "translations" / "en.json"
_FLOW_CLASSES = {"config": "ClimateAdvisorConfigFlow", "options": "ClimateAdvisorOptionsFlow"}
_MAX_LABEL_WORDS = 5
_UNIT_WORDS = re.compile(r"\b(minutes?|mins?|seconds?|secs?|degrees?|usd)\b|°\s*[FC]|\$", re.IGNORECASE)
_MOJIBAKE = ("Â", "â€", "�")
# Keys built by shared helpers (the key is a variable at the call site, so the AST cannot see it).
_HELPER_BUILT_KEYS = {
    "options": {
        "advanced": {
            "max_setback_depth_f",
            "comfort_deadband_hot_f",
            "comfort_deadband_warm_f",
            "comfort_deadband_mild_f",
            "comfort_deadband_cool_f",
            "comfort_deadband_cold_f",
        },
        # Issue #1347: the setpoints schema is built in a loop over _SETPOINT_KEYS / _VACATION_KEYS.
        "setpoints": {
            "comfort_heat",
            "comfort_cool",
            "setback_heat",
            "setback_cool",
            "sleep_heat",
            "sleep_cool",
            "vacation_heat",
            "vacation_cool",
        },
    },
    "config": {
        "setpoints": {"comfort_heat", "comfort_cool", "setback_heat", "setback_cool", "sleep_heat", "sleep_cool"},
    },
}
# Steps whose strings block exists for HA but whose handler builds no schema of its own.
_MENU_STEPS = {"options": {"init"}}


def _const_value(name: str):
    const = importlib.import_module("custom_components.climate_advisor.const")
    return getattr(const, name, None)


def _load_strings() -> dict:
    return json.loads(_STRINGS.read_text(encoding="utf-8"))


def _handler_info() -> dict[str, dict[str, dict]]:
    """Per flow, per step: schema keys and raised error keys, read from ``config_flow.py``."""
    tree = ast.parse((_PKG / "config_flow.py").read_text(encoding="utf-8"))
    info: dict[str, dict[str, dict]] = {"config": {}, "options": {}}
    for flow, class_name in _FLOW_CLASSES.items():
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        for fn in cls.body:
            if not isinstance(fn, ast.AsyncFunctionDef) or not fn.name.startswith("async_step_"):
                continue
            step = fn.name[len("async_step_") :]
            keys: set[str] = set()
            errors: set[str] = set()
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("Required", "Optional")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "vol"
                    and node.args
                ):
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        keys.add(arg.value)
                    elif isinstance(arg, ast.Name):
                        resolved = _const_value(arg.id)
                        if isinstance(resolved, str):
                            keys.add(resolved)
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and any(
                        isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == "errors" for t in node.targets
                    )
                ):
                    errors.add(node.value.value)
            info[flow][step] = {"keys": keys, "errors": errors}
    return info


def test_strings_json_equals_en_json():
    assert _STRINGS.read_text(encoding="utf-8") == _EN.read_text(encoding="utf-8"), (
        "strings.json and translations/en.json must be identical — copy strings.json to en.json"
    )


def test_no_mojibake():
    text = _STRINGS.read_text(encoding="utf-8")
    bad = [m for m in _MOJIBAKE if m in text]
    assert not bad, f"mojibake sequences in strings.json: {bad}"


@pytest.mark.parametrize("flow", ["config", "options"])
def test_every_step_has_strings_and_every_strings_step_has_a_handler(flow):
    steps = _load_strings()[flow]["step"]
    handlers = set(_handler_info()[flow])
    missing_strings = {s for s in handlers if s not in steps and s not in _MENU_STEPS.get(flow, set())}
    stale_strings = {s for s in steps if s not in handlers}
    assert not missing_strings, f"{flow}: steps with no strings block: {sorted(missing_strings)}"
    assert not stale_strings, f"{flow}: strings blocks with no step handler: {sorted(stale_strings)}"


@pytest.mark.parametrize("flow", ["config", "options"])
def test_every_schema_key_has_label_and_description(flow):
    steps = _load_strings()[flow]["step"]
    problems = []
    for step, details in _handler_info()[flow].items():
        keys = set(details["keys"]) | _HELPER_BUILT_KEYS.get(flow, {}).get(step, set())
        block = steps.get(step, {})
        data, desc = block.get("data", {}), block.get("data_description", {})
        for key in sorted(keys):
            if not data.get(key, "").strip():
                problems.append(f"{flow}.{step}.{key}: missing data label (HA would show the raw key)")
            if not desc.get(key, "").strip():
                problems.append(f"{flow}.{step}.{key}: missing data_description")
        for key in sorted(set(data) | set(desc)):
            if keys and key not in keys:
                problems.append(f"{flow}.{step}.{key}: string for a key the step does not have")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("flow", ["config", "options"])
def test_labels_follow_the_standard(flow):
    problems = []
    for step, block in _load_strings()[flow]["step"].items():
        for key, label in block.get("data", {}).items():
            where = f"{flow}.{step}.{key}"
            if "(" in label or ")" in label:
                problems.append(f"{where}: parentheses in label {label!r}")
            if len(label.split()) > _MAX_LABEL_WORDS:
                problems.append(f"{where}: more than {_MAX_LABEL_WORDS} words: {label!r}")
            if _UNIT_WORDS.search(label):
                problems.append(f"{where}: unit text in label {label!r} (use unit_of_measurement)")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("flow", ["config", "options"])
def test_every_raised_error_has_a_string(flow):
    declared = set(_load_strings()[flow].get("error", {}))
    raised = {e for details in _handler_info()[flow].values() for e in details["errors"]}
    missing = sorted(raised - declared)
    assert not missing, f"{flow}: errors raised with no string: {missing}"
