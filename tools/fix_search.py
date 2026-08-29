#!/usr/bin/env python3
"""Dev-facing search over Climate Advisor's fix history (Issue #702).

Thin CLI over `fix_history.search_records` — the same relevance search the AI
Investigator uses (`custom_components/climate_advisor/fix_history.py`), available
standalone for a developer asking "has this already been fixed?" without going
through the Investigator. No HA runtime needed — this is static repo data.

Usage:
    python tools/fix_search.py "nat vent grace period"
    python tools/fix_search.py --limit 5 "fan status reconcile"
    python tools/fix_search.py --recent          # most recent entries, no query
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"


def _load_module(name: str):
    """Load a climate_advisor submodule directly, bypassing the package __init__.

    `custom_components/climate_advisor/__init__.py` imports `homeassistant.*` at
    module scope, so a normal `from custom_components.climate_advisor.X import Y`
    would fail without Home Assistant installed. `fix_history.py` (and `const.py`)
    have no such dependency, so load them as standalone files instead.
    """
    spec = importlib.util.spec_from_file_location(name, _COMPONENT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fix_history = _load_module("fix_history")
search_records = fix_history.search_records
parse_version = fix_history.parse_version


def _current_version() -> tuple[int, ...]:
    const = _load_module("const")
    return parse_version(const.VERSION)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", default="", help="search terms (omit for --recent)")
    parser.add_argument("--limit", type=int, default=15, help="max results (default 15)")
    args = parser.parse_args()

    results = search_records(query=args.query, limit=args.limit, current_version=_current_version())

    if not results:
        print("No matching fix-history entries.")
        return 0

    for rec in results:
        issue = rec.get("issue")
        version = rec.get("version_fixed", "?")
        summary = rec.get("user_summary") or rec.get("title", "")
        merged = rec.get("merged_from")
        header = f"#{issue} (fixed in v{version})"
        if merged:
            header += f" [consolidated: {', '.join('#' + str(n) for n in merged)}]"
        print(header)
        print(f"  {summary}")
        if rec.get("scope_covered"):
            print(f"  scope: {rec['scope_covered']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
