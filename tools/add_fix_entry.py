#!/usr/bin/env python3
"""Append one fix-history entry to fix_history.jsonl (Issue #702).

Replaces the old release-checklist step of hand-editing `RELEASE_NOTES`/`KNOWN_FIXES`
dicts directly in const.py. Appending a JSONL line by hand is still error-prone at the
frequency this project ships PRs (a malformed line breaks the streaming parser for
every record after it), so this validates required fields before writing.

Usage:
    python tools/add_fix_entry.py --issue 706 --version 0.6.46 \\
        --title "Nat-vent FSM's production input builder..." \\
        --scope "automation.py: ..." \\
        --user-summary "Fix #706: closes a gap where..."

`--user-summary` is optional — a meaningful fraction of historical entries are
internal/process changes with no occupant-facing outcome (do not invent one).

`--allow-reopened` permits a second entry for one already-recorded --issue value,
for the case where an issue was reopened and received a materially different,
deeper fix (not a re-filing of the same fix). It only lifts the duplicate check
for the exact issue number passed alongside it in that invocation — every other
issue number is still subject to the normal duplicate rejection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_FIX_HISTORY_FILE = (
    Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "fix_history.jsonl"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    parser.add_argument("--version", required=True, help="version_fixed, e.g. 0.6.46")
    parser.add_argument("--title", required=True, help="short dev-facing description")
    parser.add_argument("--scope", required=True, dest="scope_covered", help="code paths touched")
    parser.add_argument("--user-summary", default=None, help="occupant-facing outcome bullet, if any")
    parser.add_argument(
        "--allow-reopened",
        action="store_true",
        help=(
            "Allow a second entry for the exact --issue value given, when that issue was "
            "reopened and received a materially different, deeper fix. Does not disable "
            "the duplicate check for any other issue number."
        ),
    )
    args = parser.parse_args()

    if not _FIX_HISTORY_FILE.exists():
        print(f"error: {_FIX_HISTORY_FILE} does not exist", file=sys.stderr)
        return 1

    existing_issues: set[int] = set()
    with _FIX_HISTORY_FILE.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"error: existing file has a malformed line {lineno} — fix before appending", file=sys.stderr)
                return 1
            existing_issues.add(rec.get("issue"))
            existing_issues.update(rec.get("merged_from", []))

    if args.issue in existing_issues and not args.allow_reopened:
        print(f"error: issue #{args.issue} already has a fix_history entry", file=sys.stderr)
        return 1

    record = {
        "issue": args.issue,
        "version_fixed": args.version,
        "title": args.title,
        "scope_covered": args.scope_covered,
    }
    if args.user_summary:
        record["user_summary"] = args.user_summary

    with _FIX_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Appended issue #{args.issue} (v{args.version}) to {_FIX_HISTORY_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
