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

SECRET_PATTERNS = [
    re.compile(r"""['"]([^'"]*(?:password|token|api_key|secret|credential)[^'"]*)['"]""", re.IGNORECASE),
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
        with open(manifest_path, "r", encoding="utf-8") as f:
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
            result.error(
                f"Domain '{manifest['domain']}' does not match "
                f"directory name '{dir_name}'"
            )

    # Check version is valid semver
    if "version" in manifest:
        if not SEMVER_PATTERN.match(manifest["version"]):
            result.error(
                f"Version '{manifest['version']}' is not valid semver (expected X.Y.Z)"
            )

    # Check config_flow is boolean
    if "config_flow" in manifest:
        if not isinstance(manifest["config_flow"], bool):
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
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            # Syntax errors are caught by check_syntax
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and node.module:
                    # Relative import: from .module import ...
                    module_name = node.module.split(".")[0]
                    if module_name not in available_modules:
                        result.error(
                            f"{filename}: imports '.{node.module}' but "
                            f"'{module_name}.py' does not exist"
                        )
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
        with open(strings_path, "r", encoding="utf-8") as f:
            strings = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"strings.json is not valid JSON: {e}")
        return result

    # Check for config flow user step
    try:
        _ = strings["config"]["step"]["user"]
        if verbose:
            print("  OK: config.step.user exists")
    except (KeyError, TypeError):
        result.error("Missing required key path: config.step.user")

    return result


def check_secrets(component_dir):
    """Scan for potential secrets in source code (warnings only)."""
    result = CheckResult("Secrets Scan")
    py_files = [f for f in os.listdir(component_dir) if f.endswith(".py")]

    for filename in sorted(py_files):
        filepath = os.path.join(component_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
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
                    if any(fp in match.lower() for fp in [
                        "api_key", "token", "password", "secret"
                    ]):
                        # Only warn if it looks like an actual value assignment
                        if "=" in line and ('""' not in line) and ("''" not in line):
                            result.warn(f"{filename}:{line_num}: possible secret reference: {match[:50]}")

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
        check_secrets(COMPONENT_DIR),
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
