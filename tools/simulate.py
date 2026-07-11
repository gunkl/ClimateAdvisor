#!/usr/bin/env python3
"""Climate Advisor Behavior Simulator.

Replays activity scenarios through the automation decision logic
to verify expected behavior and detect regressions.

Scenario states:
  pending/       - ingested, awaiting human review
  golden/        - approved, passing — protected regression tests
  pending-fix/   - wrong outcome, code fix needed (linked to issue)
  unsupported/   - wrong outcome, intentionally out of scope
  synthetic/     - constructed (not from real events), lower authority

Usage:
  python3 tools/simulate.py              # run all golden scenarios
  python3 tools/simulate.py -s NAME      # run specific scenario (any state)
  python3 tools/simulate.py --pending    # run all pending scenarios (for review)
  python3 tools/simulate.py --list       # list all scenarios by state
  python3 tools/simulate.py --cases      # summary table of all scenarios across all states
  python3 tools/simulate.py -v           # verbose (show full decision timeline)
  python3 tools/simulate.py --report           # write markdown report to tools/simulations/REPORT.md
  python3 tools/simulate.py --check-integrity  # verify golden hashes against MANIFEST.json
  python3 tools/simulate.py --sign NAME        # sign a golden scenario into MANIFEST.json

Lifecycle:
  Real event → /simulate add → pending/ → review → golden/ or pending-fix/ or unsupported/
  All golden scenarios must pass on every run — failures = regression.
"""

import argparse
import hashlib
import json
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from datetime import date as dt_date
from pathlib import Path

SIMULATIONS_DIR = Path(__file__).parent / "simulations"
STATE_DIRS: dict[str, Path] = {
    "pending": SIMULATIONS_DIR / "pending",
    "golden": SIMULATIONS_DIR / "golden",
    "pending-fix": SIMULATIONS_DIR / "pending-fix",
    "unsupported": SIMULATIONS_DIR / "unsupported",
    "synthetic": SIMULATIONS_DIR / "synthetic",
}
MANIFEST_PATH = SIMULATIONS_DIR / "golden" / "MANIFEST.json"


# ------------------------------------------------------------------
# Scenario I/O
# ------------------------------------------------------------------


def _find_scenario(name: str) -> tuple[Path, str] | None:
    """Find a scenario file by name across all state directories."""
    for state, d in STATE_DIRS.items():
        p = d / f"{name}.json"
        if p.exists():
            return p, state
    return None


# ------------------------------------------------------------------
# Production engine (issue #236) — run the REAL AutomationEngine headless
# ------------------------------------------------------------------


def run_scenario_production(scenario_file: Path, state: str | None = None) -> dict:
    """Run a scenario through the real production AutomationEngine (or coordinator).

    Drives the real ``AutomationEngine`` headless via ``tools/sim_harness``. By
    default, ``track: integration`` assertions are deferred (skipped) since the
    bare engine has no coordinator-listener layer to evaluate them against.

    Issue #474: a scenario JSON may set ``"use_coordinator": true`` to build a
    real ``ClimateAdvisorCoordinator`` (via ``build_headless_coordinator()``)
    instead of a bare engine. When set, ``track: integration`` assertions are
    evaluated for real — the coordinator's actual ``_async_thermostat_changed``
    listener, timers, and startup lifecycle are running, not skipped.
    (``simulator_support: false`` assertions are always evaluated — not skipped
    in either mode.)
    """
    # Lazy imports: ensure the repo root is importable when run standalone
    # (python tools/simulate.py puts tools/ on sys.path, not the repo root).
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from tools.sim_harness import outcomes as _out
    from tools.sim_harness.run_production import run_production_scenario

    # Issue #476: explicit UTF-8 — without it, open() uses the platform default
    # encoding (cp1252 on Windows), which crashes on any scenario file containing
    # non-ASCII characters (em-dashes are common in scenario notes/descriptions).
    with open(scenario_file, encoding="utf-8") as f:
        scenario = json.load(f)

    use_coordinator = bool(scenario.get("use_coordinator", False))
    result = run_production_scenario(scenario, use_coordinator=use_coordinator)
    decisions = _out.production_decisions(result)

    assertion_results: list[dict] = []
    any_real_assertion = False
    for a in scenario.get("assertions", []):
        # Tier separation (issue #236, narrowed by #474): track:"integration"
        # assertions need the coordinator's state-listener layer (e.g.
        # _async_thermostat_changed). When use_coordinator=True, that layer is
        # now real (Issue #474 — coordinator-level Tier A coverage) and these
        # assertions are evaluated like any other. Only skip them when running
        # in bare-engine mode. (simulator_support:false is NEVER skipped — the
        # real production engine/coordinator CAN evaluate those.)
        if a.get("track") == "integration" and not use_coordinator:
            assertion_results.append(
                {
                    "at": a["at"],
                    "expected": a.get("expect", ""),
                    "actual": None,
                    "pass": None,
                    "skipped": True,
                    "reason": "integration-track assertion — needs use_coordinator=True",
                    "track": "integration",
                }
            )
            continue
        any_real_assertion = True
        expect = a.get("expect", "")
        custom_result = _out.check_assertion(result, a, decisions)
        actual_outcome = custom_result if custom_result is not False else _out.production_outcome_at(decisions, a["at"])
        outcome_ok = actual_outcome == expect

        temp_pass = True
        temp_detail = None
        if "expect_temp" in a:
            actual_temp = _out.production_temp_at(decisions, a["at"])
            expected_temp = float(a["expect_temp"])
            if actual_temp is None:
                temp_pass = False
                temp_detail = f"expect_temp={expected_temp} but no target_temp recorded"
            else:
                temp_pass = abs(actual_temp - expected_temp) < 0.01
                temp_detail = f"expect_temp={expected_temp}, actual_temp={actual_temp}"

        assertion_results.append(
            {
                "at": a["at"],
                "expected": expect,
                "actual": actual_outcome,
                "pass": outcome_ok and temp_pass,
                "skipped": False,
                "reason": a.get("reason", ""),
                "temp_detail": temp_detail,
                "track": a.get("track", "logic"),
                "legacy_skipped": a.get("simulator_support") is False or a.get("track") == "integration",
            }
        )

    real_results = [r for r in assertion_results if not r.get("skipped")]
    passed = all(r["pass"] for r in real_results) if any_real_assertion else None
    callback_errors = [f"{ts}: {exc!r}" for ts, exc in result.callback_errors]
    if callback_errors:
        # Unexpected callback errors make the run untrustworthy — never report pass.
        passed = False

    return {
        "name": scenario.get("name", scenario_file.stem),
        "description": scenario.get("description", ""),
        "issue": scenario.get("issue"),
        "verdict": scenario.get("verdict"),
        "state": state,
        "decisions": [
            {"time": d.time, "outcome": d.outcome, "reason": d.event_type, "target_temp": d.target_temp}
            for d in decisions
        ],
        "assertions": assertion_results,
        "passed": passed,
        "callback_errors": callback_errors,
    }


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------


def _status_label(result: dict) -> str:
    """Return the display status string for a result, accounting for pending-fix expected fails."""
    passed = result["passed"]
    state = result.get("state")
    verdict_raw = result.get("verdict")
    verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
    verdict_type = verdict.get("type")

    if state == "pending-fix" and verdict_type == "negative":
        if passed is False:
            return "EXPECTED FAIL"
        if passed is True:
            return "PASS"

    if passed is True:
        return "PASS"
    if passed is False:
        return "FAIL"
    return "SKIP (no assertions)"


def print_result(result: dict, verbose: bool = False) -> None:
    """Print simulation result in human-readable form."""
    status = _status_label(result)
    verdict_raw = result.get("verdict")
    verdict = verdict_raw if isinstance(verdict_raw, dict) else {}

    issue_tag = f" [#{result['issue']}]" if result.get("issue") else ""
    print(f"\n{'=' * 60}")
    print(f"Scenario: {result['name']}{issue_tag}")
    print(f"  {result['description']}")

    if verdict:
        verdict_type = verdict.get("type", "")
        summary = verdict.get("summary", "")
        print(f"  Verdict: {verdict_type} — {summary}")

    print(f"  Status: {status}")

    if status == "PASS" and result.get("state") == "pending-fix":
        print("  NOTE: pending-fix scenario now passes — consider promoting to golden/")

    if verbose and result["decisions"]:
        print("\nDecision timeline:")
        for d in result["decisions"]:
            temp_suffix = f"  → {d['target_temp']}°F" if d.get("target_temp") is not None else ""
            print(f"  {d['time']}  [{d['outcome']}]{temp_suffix}  {d['reason']}")

    if result["assertions"]:
        print("\nAssertions:")
        for a in result["assertions"]:
            if a.get("skipped"):
                print(f"  [SKIP] at {a['at']}: {a['reason']}")
            elif a["pass"]:
                print(f"  [OK]   at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
                if a.get("temp_detail"):
                    print(f"         temp: {a['temp_detail']}")
            else:
                print(f"  [FAIL] at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
                if a.get("temp_detail"):
                    print(f"         temp: {a['temp_detail']}")
                if a["reason"]:
                    print(f"         {a['reason']}")


# ------------------------------------------------------------------
# Cases summary
# ------------------------------------------------------------------


def print_cases_summary() -> None:
    """Scan all directories and print a summary table of all scenarios."""
    print("\nSCENARIO CASE SUMMARY")
    print("======================")

    for state, state_dir in STATE_DIRS.items():
        if not state_dir.exists():
            continue
        files = sorted(p for p in state_dir.glob("*.json") if p.name != "MANIFEST.json")
        if not files:
            continue

        print(f"\n{state.upper()} ({len(files)} scenario{'s' if len(files) != 1 else ''})")

        for f in files:
            try:
                result = run_scenario_production(f, state=state)
            except (json.JSONDecodeError, OSError, KeyError):
                print(f"  [ERROR] {f.stem}: unreadable or invalid")
                continue

            status = _status_label(result)
            verdict_raw = result.get("verdict")
            verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
            verdict_type = verdict.get("type", "")
            issue_tag = f" #{result['issue']}" if result.get("issue") else ""

            verdict_tag = f" [{verdict_type}]" if verdict_type else ""
            print(f"  [{status}]{verdict_tag} {result['name']}{issue_tag}")
            print(f"         {result['description']}")

            if verdict_type == "negative":
                observed = verdict.get("observed_behavior", "")
                expected = verdict.get("expected_behavior", "")
                if observed:
                    print(f"         Observed: {observed}")
                if expected:
                    print(f"         Expected: {expected}")


# ------------------------------------------------------------------
# Golden test integrity — MANIFEST.json
# ------------------------------------------------------------------


def _normalize_lf(path: Path) -> bool:
    """Normalize CRLF to LF in a file in-place. Returns True if file was changed."""
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    if raw == normalized:
        return False
    path.write_bytes(normalized)
    return True


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file, normalizing CRLF→LF for platform consistency."""
    h = hashlib.sha256()
    h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def fix_crlf_all() -> int:
    """Normalize CRLF→LF in all scenario JSON files across all state directories."""
    fixed: list[str] = []
    for state, d in STATE_DIRS.items():
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            if _normalize_lf(path):
                fixed.append(f"  {state}/{path.name}")
    if MANIFEST_PATH.exists() and _normalize_lf(MANIFEST_PATH):
        fixed.append("  golden/MANIFEST.json")
    if fixed:
        print(f"Normalized {len(fixed)} file(s) to LF:")
        for f in fixed:
            print(f)
    else:
        print("All scenario files already use LF — nothing to do.")
    return 0


def check_integrity() -> int:
    """Verify all golden scenario files match their MANIFEST.json hashes.

    Returns 0 if clean, 1 if any mismatch or unlisted file is found.
    """
    if not MANIFEST_PATH.exists():
        print("MANIFEST.json not found — run: python tools/simulate.py --sign <name>")
        return 1

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        print("MANIFEST.json is corrupt — expected a JSON object")
        return 1

    golden_dir = STATE_DIRS["golden"]
    errors: list[str] = []
    golden_files = sorted(p for p in golden_dir.glob("*.json") if p.name != "MANIFEST.json")

    for path in golden_files:
        name = path.stem
        actual_hash = _file_sha256(path)
        if name not in manifest:
            errors.append(f"  UNSIGNED  {name}.json — not in MANIFEST; run --sign {name}")
        elif manifest[name].get("sha256") != actual_hash:
            errors.append(f"  MODIFIED  {name}.json — hash mismatch (run --sign {name} after human review)")

    if errors:
        print("Golden integrity check FAILED:")
        for e in errors:
            print(e)
        return 1

    print(f"Golden integrity OK — {len(golden_files)} scenario(s) verified")
    return 0


def sign_scenario(name: str) -> int:
    """Print a human-readable scenario card and update MANIFEST.json.

    Requires interactive confirmation. Returns 0 on success, 1 on abort.
    """
    golden_dir = STATE_DIRS["golden"]
    path = golden_dir / f"{name}.json"
    if not path.exists():
        print(f"Scenario not found in golden/: {name}.json")
        return 1

    with open(path, encoding="utf-8") as f:
        scenario = json.load(f)

    # Print human-readable card for review
    print("\n" + "=" * 70)
    print(f"GOLDEN TEST SIGNING CEREMONY: {name}")
    print("=" * 70)
    print(f"Description : {scenario.get('description', '(none)')}")
    issue = scenario.get("issue")
    if issue:
        print(f"Issue       : #{issue}")
    verdict_raw = scenario.get("verdict")
    verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
    if verdict:
        print(f"Verdict     : {verdict.get('type', '')} — {verdict.get('summary', '')}")
        if verdict.get("observed_behavior"):
            print(f"  Observed  : {verdict['observed_behavior']}")
        if verdict.get("expected_behavior"):
            print(f"  Expected  : {verdict['expected_behavior']}")
    notes = scenario.get("notes", [])
    if notes:
        print("Notes:")
        for note in notes:
            print(f"  • {note}")

    print("\nEvents:")
    for ev in scenario.get("events", []):
        t = ev.get("time", "?")
        etype = ev.get("type", "?")
        note = ev.get("note", "")
        detail = "  — " + note if note else ""
        print(f"  {t}  [{etype}]{detail}")

    print("\nAssertions:")
    for a in scenario.get("assertions", []):
        at = a.get("at", "?")
        expect = a.get("expect", "?")
        temp = a.get("expect_temp")
        reason = a.get("reason", "")
        skip = " [SKIP — simulator_support=false]" if a.get("simulator_support") is False else ""
        temp_str = f"  → {temp}°F" if temp is not None else ""
        print(f"  {at}: expect={expect!r}{temp_str}  {reason}{skip}")

    print("\n" + "=" * 70)
    print("Review the scenario above.")
    print("Does this accurately represent real HVAC behavior? (Enter to sign, Ctrl-C to abort)")
    try:
        input()
    except KeyboardInterrupt:
        print("\nAborted — MANIFEST not updated.")
        return 1

    # Normalize line endings before hashing so the stored hash is always LF-based
    if _normalize_lf(path):
        print(f"  (normalized CRLF→LF in {path.name} before signing)")

    # Update MANIFEST
    manifest: dict = {}
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict):
            print("MANIFEST.json is corrupt — expected a JSON object. Delete it and re-sign all golden scenarios.")
            return 1

    if "_meta" not in manifest:
        manifest["_meta"] = {
            "description": "SHA-256 hashes of approved golden scenarios. Each entry requires human review.",
            "policy": "Modify only via: python tools/simulate.py --sign <scenario-name>",
        }

    actual_hash = _file_sha256(path)
    manifest[name] = {
        "sha256": actual_hash,
        "signed": str(dt_date.today()),
    }
    if issue:
        manifest[name]["issue"] = issue

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"Signed: {name} → MANIFEST.json updated ({actual_hash[:12]}...)")
    return 0


# ------------------------------------------------------------------
# Markdown report generation
# ------------------------------------------------------------------


def write_report(output_path: Path | None = None) -> None:
    """Write a human-readable markdown report of all scenarios to REPORT.md."""
    if output_path is None:
        output_path = SIMULATIONS_DIR / "REPORT.md"

    lines: list[str] = []
    lines.append("# Climate Advisor Simulation Report")
    lines.append(f"\nGenerated: {dt_date.today()}")
    lines.append("\n---\n")

    state_counts: dict[str, dict[str, int]] = {}
    all_results: list[tuple[str, dict]] = []

    for state, state_dir in STATE_DIRS.items():
        if not state_dir.exists():
            continue
        files = sorted(p for p in state_dir.glob("*.json") if p.name != "MANIFEST.json")
        if not files:
            continue
        counts = {"pass": 0, "fail": 0, "skip": 0}
        for fpath in files:
            try:
                result = run_scenario_production(fpath, state=state)
            except (json.JSONDecodeError, OSError, KeyError) as e:
                result = {
                    "name": fpath.stem,
                    "description": f"ERROR: {e}",
                    "issue": None,
                    "verdict": None,
                    "state": state,
                    "decisions": [],
                    "assertions": [],
                    "passed": False,
                }
            all_results.append((state, result))
            lbl = _status_label(result)
            if lbl == "PASS":
                counts["pass"] += 1
            elif lbl == "FAIL":
                counts["fail"] += 1
            else:
                counts["skip"] += 1
        state_counts[state] = counts

    # Summary table
    lines.append("## Summary\n")
    lines.append("| State | Pass | Fail | Skip |")
    lines.append("|-------|------|------|------|")
    for state, c in state_counts.items():
        lines.append(f"| {state} | {c['pass']} | {c['fail']} | {c['skip']} |")
    lines.append("")

    # Per-scenario sections
    current_state = None
    for state, result in all_results:
        if state != current_state:
            current_state = state
            lines.append(f"\n---\n\n## {state.upper()} Scenarios\n")

        status = _status_label(result)
        status_icon = "✅" if status == "PASS" else ("❌" if status == "FAIL" else "⏭️")
        issue_tag = f" [#{result['issue']}]" if result.get("issue") else ""
        lines.append(f"### {result['name']}{issue_tag} {status_icon} {status}\n")
        lines.append(f"**Description:** {result['description']}\n")

        verdict_raw = result.get("verdict")
        verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
        if verdict:
            vtype = verdict.get("type", "")
            vsummary = verdict.get("summary", "")
            lines.append(f"**Verdict:** {vtype} — {vsummary}\n")

        # Events table
        if result.get("decisions"):
            lines.append("**Decision timeline:**\n")
            lines.append("| Time | Outcome | Temp | Reason |")
            lines.append("|------|---------|------|--------|")
            for d in result["decisions"]:
                temp = f"{d['target_temp']}°F" if d.get("target_temp") is not None else "—"
                reason = d["reason"].replace("|", "\\|")
                lines.append(f"| {d['time']} | `{d['outcome']}` | {temp} | {reason} |")
            lines.append("")

        # Assertions table
        if result.get("assertions"):
            lines.append("**Assertions:**\n")
            lines.append("| Time | Expected | Actual | Result | Reason |")
            lines.append("|------|----------|--------|--------|--------|")
            for a in result["assertions"]:
                if a.get("skipped"):
                    lines.append(f"| {a['at']} | `{a['expected']}` | — | ⏭️ SKIP | {a.get('reason', '')} |")
                else:
                    icon = "✅" if a["pass"] else "❌"
                    actual = f"`{a['actual']}`" if a["actual"] else "—"
                    reason = a.get("reason", "").replace("|", "\\|")
                    lines.append(f"| {a['at']} | `{a['expected']}` | {actual} | {icon} | {reason} |")
            lines.append("")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report written to {output_path}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Climate Advisor behavior simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--scenario", metavar="NAME", help="Run a specific scenario by name (searches all state dirs)"
    )
    parser.add_argument("--pending", action="store_true", help="Run all pending scenarios (for review)")
    parser.add_argument("--list", action="store_true", dest="list_all", help="List all scenarios by state")
    parser.add_argument("--cases", action="store_true", help="Show summary table of all scenarios across all states")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full decision timeline for each scenario")
    parser.add_argument(
        "--report",
        action="store_true",
        help="Write human-readable markdown report to tools/simulations/REPORT.md",
    )
    parser.add_argument(
        "--check-integrity",
        action="store_true",
        dest="check_integrity",
        help="Verify golden scenario hashes against MANIFEST.json",
    )
    parser.add_argument(
        "--sign",
        metavar="NAME",
        help="Sign a golden scenario into MANIFEST.json after human review",
    )
    parser.add_argument(
        "--fix-crlf",
        action="store_true",
        dest="fix_crlf",
        help="Normalize CRLF→LF in all scenario JSON files (run before --sign on Windows)",
    )
    args = parser.parse_args()

    for d in STATE_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

    # Normalize CRLF in all scenario files
    if args.fix_crlf:
        return fix_crlf_all()

    # Golden integrity check
    if args.check_integrity:
        return check_integrity()

    # Sign a golden scenario
    if args.sign:
        return sign_scenario(args.sign)

    # Report generation
    if args.report:
        write_report()
        return 0

    # Cases summary mode
    if args.cases:
        print_cases_summary()
        return 0

    # List mode
    if args.list_all:
        for state, d in STATE_DIRS.items():
            files = sorted(p for p in d.glob("*.json") if p.name != "MANIFEST.json")
            if files:
                print(f"\n{state.upper()} ({len(files)}):")
                for f in files:
                    try:
                        with open(f, encoding="utf-8") as fh:
                            s = json.load(fh)
                        desc = s.get("description", "")[:70]
                        issue = f" [#{s['issue']}]" if s.get("issue") else ""
                        verdict_raw = s.get("verdict")
                        verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
                        verdict_tag = f" [{verdict['type']}]" if verdict.get("type") else ""
                        print(f"  {f.stem}{issue}{verdict_tag}: {desc}")
                    except (json.JSONDecodeError, OSError):
                        print(f"  {f.stem}: [unreadable]")
        return 0

    # Single scenario
    if args.scenario:
        found = _find_scenario(args.scenario)
        if not found:
            print(f"Scenario not found: {args.scenario}")
            print("Available:")
            for state, d in STATE_DIRS.items():
                for f in sorted(p for p in d.glob("*.json") if p.name != "MANIFEST.json"):
                    print(f"  [{state}] {f.stem}")
            return 1
        scenario_path, scenario_state = found
        result = run_scenario_production(scenario_path, state=scenario_state)
        print_result(result, verbose=args.verbose)
        status = _status_label(result)
        return 0 if status != "FAIL" else 1

    # Run a batch (golden by default, pending with --pending)
    source_key = "pending" if args.pending else "golden"
    source_dir = STATE_DIRS[source_key]
    files = sorted(p for p in source_dir.glob("*.json") if p.name != "MANIFEST.json") if source_dir.exists() else []

    if not files:
        print(f"No {source_key} scenarios found.")
        if source_key == "golden":
            print("  Promote a scenario from pending/ to golden/ after review.")
        return 0

    results = [run_scenario_production(f, state=source_key) for f in files]
    for r in results:
        print_result(r, verbose=args.verbose)

    total = len(results)
    passed = sum(1 for r in results if r["passed"] is True)
    failed = sum(1 for r in results if _status_label(r) == "FAIL")
    skipped = sum(1 for r in results if r["passed"] is None)

    print(f"\n{'=' * 60}")
    summary = f"{passed}/{total} {source_key} scenarios passed"
    if failed:
        summary += f" — {failed} FAILED"
    if skipped:
        summary += f" — {skipped} skipped (no assertions)"
    print(summary)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
