#!/usr/bin/env python3
"""Pre-deployment validation for Climate Advisor integration.

Runs syntax checks, manifest validation, import consistency checks,
strings.json validation, and a secrets scan. Uses only the Python
standard library so it works on any machine without dependencies.

Usage:
    python tools/validate.py
    python tools/validate.py --verbose

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import ast
import json
import os
import py_compile
import re
import sys

COMPONENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "climate_advisor",
)

REQUIRED_MANIFEST_KEYS = {
    "domain",
    "name",
    "version",
    "config_flow",
    "dependencies",
    "requirements",
    "iot_class",
}

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

HARDCODED_UNIT_EXCEPTION_PHRASES: list[str] = []

SECRET_PATTERNS = [
    re.compile(r"""['"]([^'"]*(?:password|token|api_key|secret|credential)[^'"]*)['"]""", re.IGNORECASE),
    re.compile(r"""['"]([^'"]*(?:private_key|ssh_key|bearer|authorization)[^'"]*)['"]""", re.IGNORECASE),
    re.compile(r"""https?://[^'"\s]+:[^'"\s@]+@[^'"\s]+""", re.IGNORECASE),
]

verbose = "--verbose" in sys.argv


class CheckResult:
    def __init__(self, name):
        self.name = name
        self.passed = True
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.passed = False
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def check_syntax(component_dir):
    """Check all .py files compile without syntax errors."""
    result = CheckResult("Syntax Check")
    py_files = [f for f in os.listdir(component_dir) if f.endswith(".py")]

    if not py_files:
        result.error("No .py files found in component directory")
        return result

    for filename in sorted(py_files):
        filepath = os.path.join(component_dir, filename)
        try:
            py_compile.compile(filepath, doraise=True)
            if verbose:
                print(f"  OK: {filename}")
        except py_compile.PyCompileError as e:
            result.error(f"{filename}: {e}")

    return result


def check_manifest(component_dir):
    """Validate manifest.json has required keys and valid values."""
    result = CheckResult("Manifest Validation")
    manifest_path = os.path.join(component_dir, "manifest.json")

    if not os.path.exists(manifest_path):
        result.error("manifest.json not found")
        return result

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"manifest.json is not valid JSON: {e}")
        return result

    # Check required keys
    missing = REQUIRED_MANIFEST_KEYS - set(manifest.keys())
    if missing:
        result.error(f"Missing required keys: {', '.join(sorted(missing))}")

    # Check domain matches directory name
    if "domain" in manifest:
        dir_name = os.path.basename(component_dir)
        if manifest["domain"] != dir_name:
            result.error(f"Domain '{manifest['domain']}' does not match directory name '{dir_name}'")

    # Check version is valid semver
    if "version" in manifest and not SEMVER_PATTERN.match(manifest["version"]):
        result.error(f"Version '{manifest['version']}' is not valid semver (expected X.Y.Z)")

    # Check config_flow is boolean
    if "config_flow" in manifest and not isinstance(manifest["config_flow"], bool):
        result.error("config_flow must be a boolean")

    if verbose and result.passed:
        print(f"  OK: domain={manifest.get('domain')}, version={manifest.get('version')}")

    return result


def check_imports(component_dir):
    """Verify that all relative imports reference files that exist."""
    result = CheckResult("Import Consistency")
    py_files = [f for f in os.listdir(component_dir) if f.endswith(".py")]
    available_modules = {f[:-3] for f in py_files}  # strip .py

    for filename in sorted(py_files):
        filepath = os.path.join(component_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            # Syntax errors are caught by check_syntax
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                # Relative import: from .module import ...
                module_name = node.module.split(".")[0]
                if module_name not in available_modules:
                    result.error(f"{filename}: imports '.{node.module}' but '{module_name}.py' does not exist")
                elif verbose:
                    print(f"  OK: {filename} -> .{node.module}")

    return result


def check_strings(component_dir):
    """Validate strings.json is valid JSON with expected structure."""
    result = CheckResult("Strings Validation")
    strings_path = os.path.join(component_dir, "strings.json")

    if not os.path.exists(strings_path):
        result.error("strings.json not found")
        return result

    try:
        with open(strings_path, encoding="utf-8") as f:
            strings = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"strings.json is not valid JSON: {e}")
        return result

    # Check for config flow user step
    try:
        _ = strings["config"]["step"]["user"]
        if verbose:
            print("  OK: config.step.user exists")
    except KeyError, TypeError:
        result.error("Missing required key path: config.step.user")

    # Issue #1061: HA reads translations/en.json at runtime, so it must equal strings.json.
    en_path = os.path.join(component_dir, "translations", "en.json")
    try:
        with open(strings_path, encoding="utf-8") as f_strings, open(en_path, encoding="utf-8") as f_en:
            if f_strings.read() != f_en.read():
                result.error("translations/en.json differs from strings.json (copy strings.json over it)")
            elif verbose:
                print("  OK: translations/en.json matches strings.json")
    except OSError as e:
        result.error(f"Cannot compare translations/en.json with strings.json: {e}")

    return result


def check_secrets(component_dir):
    """Scan for potential secrets in source code (warnings only)."""
    result = CheckResult("Secrets Scan")
    py_files = [f for f in os.listdir(component_dir) if f.endswith(".py")]

    for filename in sorted(py_files):
        filepath = os.path.join(component_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue

        for line_num, line in enumerate(lines, 1):
            # Skip comments and import lines
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("import ") or stripped.startswith("from "):
                continue

            for pattern in SECRET_PATTERNS:
                matches = pattern.findall(line)
                for match in matches:
                    # Filter out common false positives (variable names, dict keys, log messages)
                    is_secret_ref = any(fp in match.lower() for fp in ["api_key", "token", "password", "secret"])
                    # Only warn if it looks like an actual value assignment
                    if is_secret_ref and "=" in line and ('""' not in line) and ("''" not in line):
                        result.warn(f"{filename}:{line_num}: possible secret reference: {match[:50]}")

    return result


def check_hardcoded_units(component_dir):
    """Ensure index.html has no hardcoded °F symbols outside unit-variable contexts."""
    result = CheckResult("Hardcoded Unit Check")
    html_path = os.path.join(component_dir, "frontend", "index.html")
    if not os.path.exists(html_path):
        result.warn("frontend/index.html not found — skipping unit check")
        return result
    unit_var_patterns = (
        "unitSym",
        "unitLabel",
        "tm.unit",
        "data.unit",
        "unit ===",
        "unit ?",
        "unit:",
        "=== 'celsius'",
    )
    with open(html_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            # Check for °F in any form
            if "\u00b0F" not in line and "\\u00b0F" not in line and "\xb0F" not in line:
                continue
            # Line has a dynamic unit reference — OK
            if any(p in line for p in unit_var_patterns):
                continue
            # Known-safe threshold description
            if any(exc in line for exc in HARDCODED_UNIT_EXCEPTION_PHRASES):
                continue
            result.error(
                f"frontend/index.html:{line_num}: hardcoded \u00b0F — use a unit variable (unitSym/unitLabel) instead"
            )
    return result


def check_python_temperature_literals(component_dir: str) -> CheckResult:
    """Flag bare numeric temperature literals in comparison contexts in Python files.

    Catches patterns like `outdoor < 60` that should reference a named _F constant.
    """
    import re as _re

    _TEMP_VAR_PATTERN = _re.compile(
        r"\b(outdoor|indoor|today_low|tomorrow_low|today_high|tomorrow_high"
        r"|comfort_heat|comfort_cool|setback_heat|setback_cool)\s*"
        r"(?:<=|>=|<|>|==)\s*(\d+\.?\d*)\b"
    )
    result = CheckResult("Python Temperature Literal Check")
    for fname in os.listdir(component_dir):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(component_dir, fname)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if line.strip().startswith("#"):
                    continue
                for m in _TEMP_VAR_PATTERN.finditer(line):
                    result.error(
                        f"{fname}:{lineno}: bare temperature literal "
                        f"`{m.group(0).strip()}` — use a named _F constant from const.py"
                    )
    return result


def check_ha_translations(component_dir):
    """Issue #1341 (D1/D6): hassfest-style checks of the HA-rendered catalog, using only the standard library.

    Mirrors the rules Home Assistant core's hassfest enforces on ``strings.json`` for the sections this integration
    uses, plus the cross-checks hassfest cannot do for a custom integration: every ``translation_key`` the code or
    ``services.yaml`` names exists in the catalog, and the entity/service names are not hard-coded in Python.
    """
    import string  # noqa: PLC0415

    result = CheckResult("HA translations (entity, selector, services, exceptions)")
    strings_path = os.path.join(component_dir, "strings.json")
    try:
        with open(strings_path, encoding="utf-8") as f:
            strings = json.load(f)
    except OSError, json.JSONDecodeError:
        return result  # check_strings reports it

    allowed = {
        "config",
        "options",
        "issues",
        "entity",
        "selector",
        "services",
        "exceptions",
        "common",
        "title",
        "device",
    }
    for section in strings:
        if section not in allowed:
            result.error(f"strings.json: top-level section '{section}' is not one hassfest accepts")

    key_re = re.compile(r"^[a-z0-9]+([-_][a-z0-9]+)*$")

    def walk(node, path, check_keys):
        if isinstance(node, dict):
            for k, v in node.items():
                if check_keys(path, k) and not key_re.match(k):
                    result.error(
                        f"strings.json: {'.'.join([*path, k])}: key must match [a-z0-9-_]+ (no leading/trailing _ or -)"
                    )
                walk(v, [*path, k], check_keys)
        elif isinstance(node, str):
            try:
                fields = [f for _, f, _, _ in string.Formatter().parse(node) if f is not None]
            except ValueError as err:
                result.error(f"strings.json: {'.'.join(path)}: placeholder syntax HA cannot parse ({err})")
                return
            for field in fields:
                if not field.isidentifier():
                    result.error(f"strings.json: {'.'.join(path)}: placeholder {{{field}}} must be a plain name")

    # Slug-keyed sections: every key below these is a translation key (names, states, options, services, errors).
    def slug_keys(path, key):
        if not path:
            return False
        head = path[0]
        if head in ("entity", "selector", "exceptions", "common"):
            return True
        if head == "services":
            return len(path) == 1 or (len(path) >= 2 and path[1:] and path[-1] == "fields")
        return False

    walk(strings, [], slug_keys)

    entity = strings.get("entity", {})
    selector_keys = set(strings.get("selector", {}))
    exceptions = set(strings.get("exceptions", {}))

    # services.yaml <-> services section.
    services = strings.get("services", {})
    yaml_path = os.path.join(component_dir, "services.yaml")
    if os.path.exists(yaml_path):
        with open(yaml_path, encoding="utf-8") as f:
            yaml_lines = f.read().splitlines()
        current = None
        in_fields = False
        yaml_services: dict[str, set[str]] = {}
        for line in yaml_lines:
            m = re.match(r"^([a-z0-9_]+):\s*$", line)
            if m:
                current, in_fields = m.group(1), False
                yaml_services[current] = set()
                continue
            if current and re.match(r"^  fields:\s*$", line):
                in_fields = True
                continue
            m = re.match(r"^    ([a-z0-9_]+):\s*$", line)
            if current and in_fields and m:
                yaml_services[current].add(m.group(1))
            if re.match(r"^\s+(name|description):", line) and not line.lstrip().startswith("#"):
                result.error(f"services.yaml: '{line.strip()[:40]}' belongs in strings.json (services), not the yaml")
            m = re.match(r"^\s+translation_key:\s*([a-z0-9_]+)\s*$", line)
            if m and m.group(1) not in selector_keys:
                result.error(f"services.yaml: translation_key '{m.group(1)}' has no strings.json selector entry")
        for svc, fields in yaml_services.items():
            entry = services.get(svc)
            if not entry or not entry.get("name") or not entry.get("description"):
                result.error(f"strings.json: services.{svc} needs a name and a description")
                continue
            for field in fields:
                fe = entry.get("fields", {}).get(field)
                if not fe or not fe.get("name") or not fe.get("description"):
                    result.error(f"strings.json: services.{svc}.fields.{field} needs a name and a description")
        for svc in services:
            if svc not in yaml_services:
                result.error(f"strings.json: services.{svc} has no entry in services.yaml")

    # Python <-> catalog.
    for filename, platform in (("sensor.py", "sensor"), ("switch.py", "switch")):
        path = os.path.join(component_dir, filename)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if re.search(r"_attr_name\s*=", text):
            result.error(f"{filename}: _attr_name overrides the translated entity name; use _attr_translation_key")
        for k, v in entity.get(platform, {}).items():
            if not v.get("name"):
                result.error(f"strings.json: entity.{platform}.{k} has no name")
    for filename in sorted(os.listdir(component_dir)):
        if not filename.endswith(".py"):
            continue
        with open(os.path.join(component_dir, filename), encoding="utf-8") as f:
            text = f.read()
        known = selector_keys | exceptions | set(strings.get("issues", {}))
        for m in re.finditer(r'translation_key\s*=\s*"([a-z0-9_]+)"', text):
            # sensor.py/switch.py set an entity translation_key (checked against the entity section by the tests)
            if filename not in ("sensor.py", "switch.py") and m.group(1) not in known:
                result.error(
                    f"{filename}: translation_key '{m.group(1)}' is not in strings.json (selector/exceptions/issues)"
                )
        if filename == "config_flow.py":
            for m in re.finditer(r'^([A-Z_]+_SELECTOR)\s*=\s*"([a-z0-9_]+)"', text, re.M):
                if m.group(2) not in selector_keys:
                    result.error(f"config_flow.py: {m.group(1)} '{m.group(2)}' has no strings.json selector entry")
    if verbose and result.passed:
        print("  OK: HA catalog sections, keys, placeholders and cross-references")
    return result


def check_i18n(component_dir):
    """Issue #1341: translation parity (both catalogs) and staleness locks; bodies live in tools/i18n_checks.py."""
    result = CheckResult("i18n catalogs (parity, HA parity, staleness)")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import i18n_checks  # noqa: PLC0415

    for name, errors in i18n_checks.run_all().items():
        for err in errors:
            result.error(f"{name}: {err}")
    return result


def main():
    print("Climate Advisor Pre-Deploy Validation")
    print(f"Component dir: {COMPONENT_DIR}")
    print("=" * 60)

    if not os.path.isdir(COMPONENT_DIR):
        print(f"\nERROR: Component directory not found: {COMPONENT_DIR}")
        sys.exit(1)

    checks = [
        check_syntax(COMPONENT_DIR),
        check_manifest(COMPONENT_DIR),
        check_imports(COMPONENT_DIR),
        check_strings(COMPONENT_DIR),
        check_ha_translations(COMPONENT_DIR),
        check_secrets(COMPONENT_DIR),
        check_hardcoded_units(COMPONENT_DIR),
        check_python_temperature_literals(COMPONENT_DIR),
        check_i18n(COMPONENT_DIR),
    ]

    all_passed = True
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        icon = "+" if check.passed else "X"
        print(f"\n[{icon}] {check.name}: {status}")

        for err in check.errors:
            print(f"    ERROR: {err}")
            all_passed = False

        for warn in check.warnings:
            print(f"    WARN:  {warn}")

    print("\n" + "=" * 60)
    if all_passed:
        print("All checks passed. Safe to deploy.")
        sys.exit(0)
    else:
        print("Validation FAILED. Fix errors before deploying.")
        sys.exit(1)


if __name__ == "__main__":
    main()
