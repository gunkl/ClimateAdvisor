#!/usr/bin/env python3
"""Prove a real-HA test is not vacuous: it must FAIL on the pre-fix tree and PASS on HEAD (Step 2).

    python scripts/revert_proof.py <commit> <pytest-node-id> [<node-id> ...]

* Extracts ``custom_components/`` at ``<commit>`` with ``git archive`` into a scratch dir
  (never checks out, resets or stashes anything in this repo).
* Runs the node ids with ``CA_COMPONENT_ROOT`` pointing at that scratch tree through the same
  path ``scripts/test.py ha`` uses; every named test must fail there.
* Runs the same node ids on the working tree; every named test must pass there.

Exit 0 only when both hold. Distinct exit codes: 1 = did not fail pre-fix, 2 = failed on the
working tree, 3 = pre-fix tree could not be imported/collected (use the reverse-patch fallback:
``--reverse-patch <fix-commit>`` applies ``git diff <fix>^ <fix> -- custom_components`` in reverse
to a copy of the working-tree ``custom_components/`` instead of extracting an old commit).

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_PY = REPO / "scripts" / "test.py"


def _git(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, check=False, **kw)


def extract_tree(commit: str, dest: Path) -> None:
    """Write ``custom_components/`` at ``commit`` under ``dest`` (git archive, no checkout)."""
    archive = dest / "tree.tar"
    res = _git("archive", "--format=tar", "-o", str(archive), commit, "custom_components")
    if res.returncode != 0:
        raise SystemExit(f"git archive {commit} failed")
    shutil.unpack_archive(str(archive), str(dest), "tar")
    archive.unlink()


def reverse_patch_tree(fix_commit: str, dest: Path) -> None:
    """Copy the working-tree package and un-apply ``fix_commit``'s production change."""
    shutil.copytree(
        REPO / "custom_components", dest / "custom_components", ignore=shutil.ignore_patterns("__pycache__")
    )
    diff = _git("diff", f"{fix_commit}^", fix_commit, "--", "custom_components", capture_output=True, text=True)
    if diff.returncode != 0 or not diff.stdout.strip():
        raise SystemExit(f"no custom_components diff for {fix_commit}")
    patch = dest / "fix.diff"
    patch.write_text(diff.stdout, encoding="utf-8", newline="\n")
    res = subprocess.run(
        ["git", "apply", "-R", "--whitespace=nowarn", str(patch)], cwd=dest, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        raise SystemExit(f"reverse patch did not apply:\n{res.stderr}")


def run_tests(nodes: list[str], component_root: Path | None) -> tuple[int, str]:
    """Run the ha tier on ``nodes``; returns (exit code, combined output)."""
    env = dict(**__import__("os").environ)
    env.pop("CA_COMPONENT_ROOT", None)
    if component_root is not None:
        env["CA_COMPONENT_ROOT"] = str(component_root)
    env["CA_HA_WORKERS"] = "0"
    proc = subprocess.run(
        [sys.executable, str(TEST_PY), "ha", "-p", "no:cacheprovider", *nodes],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


_IMPORT_FAIL = re.compile(
    r"(ImportError|ModuleNotFoundError|Integration not found|Setup failed for|error during collection)", re.I
)


def _summ(out: str, limit: int = 60) -> str:
    """Keep the result lines and the failing assertions; drop the captured-log noise."""
    keep = []
    for ln in out.splitlines():
        if ln.startswith(("E  ", "FAILED", "ERROR", "=====")) or re.match(r"tests_ha/\S+ (PASSED|FAILED|ERROR)", ln):
            keep.append(ln)
    return "\n".join((keep or out.splitlines())[-limit:])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "commit", help="pre-fix commit (tree to run the tests against), or the fix commit with --reverse-patch"
    )
    ap.add_argument("nodes", nargs="+", help="pytest node ids, e.g. tests_ha/test_x.py::test_y")
    ap.add_argument("--reverse-patch", action="store_true", help="treat COMMIT as the fix and un-apply it from HEAD")
    args = ap.parse_args(argv)

    nodes = [n.replace("tests_ha/", "tests_ha/", 1) for n in args.nodes]
    scratch = Path(tempfile.mkdtemp(prefix="ca_revert_proof_"))
    try:
        label = f"{args.commit} (reverse patch)" if args.reverse_patch else args.commit
        if args.reverse_patch:
            reverse_patch_tree(args.commit, scratch)
        else:
            extract_tree(args.commit, scratch)

        print(f"== PRE-FIX tree: {label} -> expecting FAIL ==", flush=True)
        rc_pre, out_pre = run_tests(nodes, scratch)
        print(_summ(out_pre))
        if rc_pre == 0:
            print("\nREVERT-PROOF FAILED: the test(s) did not fail on the pre-fix tree.", file=sys.stderr)
            return 1
        if " passed" not in out_pre and " failed" not in out_pre and _IMPORT_FAIL.search(out_pre):
            print(
                "\nREVERT-PROOF INCONCLUSIVE: the pre-fix tree failed to import/collect, which is not a behavior "
                "failure. Retry with --reverse-patch <fix-commit>.",
                file=sys.stderr,
            )
            return 3

        print("\n== WORKING TREE (HEAD + local edits) -> expecting PASS ==", flush=True)
        rc_head, out_head = run_tests(nodes, None)
        print(_summ(out_head, 12))
        if rc_head != 0:
            print("\nREVERT-PROOF FAILED: the test(s) do not pass on the working tree.", file=sys.stderr)
            return 2

        print(f"\nREVERT-PROOF OK: fails on {label}, passes on the working tree.")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
