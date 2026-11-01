#!/usr/bin/env python3
"""Run every CHANGED unit test file in its own pytest process (Issue #1186).

    python scripts/test_changed_isolated.py [--base origin/main] [--all] [--strict]

The full unit run shares one interpreter, so a test file that only passes because an earlier
file installed something (a stub attribute, a frozen clock) goes unnoticed. Running each
changed file alone, exactly as ``python scripts/test.py unit <file>``, catches that class of
order dependence on the PR that introduces it. With no changed test files (or when the base
ref is unavailable) this is a successful no-op. ``--all`` sweeps every ``tests/test_*.py``.
``--strict`` (CI) turns a git failure into a non-zero exit instead of a silent no-op.

Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def changed_test_files(base: str) -> list[str] | None:
    """Test files added/modified/renamed between ``base`` and HEAD; None if git cannot tell."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD", "--", "tests/test_*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"git diff against {base} failed ({proc.stderr.strip()}); nothing to run.", file=sys.stderr)
        return None
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip() and (REPO / ln.strip()).is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--all", action="store_true", help="sweep every tests/test_*.py")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if git cannot compute the changed files")
    args = ap.parse_args()

    if args.all:
        files = sorted(p.relative_to(REPO).as_posix() for p in (REPO / "tests").glob("test_*.py"))
    else:
        changed = changed_test_files(args.base)
        if changed is None and args.strict:
            print(f"--strict: could not determine changed test files against {args.base}.", file=sys.stderr)
            return 2
        files = changed or []
    if not files:
        print("No changed unit test files; skipping the isolated-file check.")
        return 0

    failed: list[str] = []
    for f in files:
        print(f"== isolated: {f}", flush=True)
        rc = subprocess.run([sys.executable, "scripts/test.py", "unit", "-q", f], cwd=REPO, check=False).returncode
        if rc != 0:
            failed.append(f)
    if failed:
        print("\nFAILED when run alone (order-dependent):\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    print(f"\nAll {len(files)} changed test file(s) pass when run alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
