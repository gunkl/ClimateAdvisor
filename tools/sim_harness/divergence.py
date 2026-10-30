"""divergence - FakeHass vs real-Home-Assistant scenario comparison and the real-HA ledger (Step 2, #1127 Inc. 6).

Stub-free: this module imports only the outcomes layer, so it runs in either environment.  The two worlds
run in different environments, so the comparison goes through serialized results:

* ``judge_scenario(scenario, result, use_coordinator=...)`` applies a scenario's own assertions to a
  ``RunResult`` with the UNCHANGED outcomes layer (the same per-assertion loop ``tools/simulate.py``'s
  ``run_scenario_production`` applies; ``tests/test_realha_judge_parity.py`` proves the two agree on every golden).
* ``dump_run`` turns a run into a JSON-able dict; ``compare_dumps`` finds the first difference between two;
  ``render_report`` prints it in the design's format (02-step2-design.md section 8.3).
* The ledger (``tools/sim_harness/realha_ledger.json``) is a ratchet: a golden that fails on real HA must be
  listed with a class and evidence, an unlisted failure fails the test, a listed scenario that no longer fails
  (or whose failing assertions changed) fails with "remove / update the stale entry".

CLI (run from the repo root)::

    python tools/sim_harness/divergence.py dump-fake [scenario ...] [--out DIR]   # FakeHass side (unit env)
    # the real-HA side is written by the `goldens` tests:  python scripts/test.py ha -m goldens
    python tools/sim_harness/divergence.py compare [--fake DIR] [--real DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.sim_harness import outcomes as _out  # noqa: E402

GOLDEN_DIR = REPO / "tools" / "simulations" / "golden"
LEDGER_PATH = Path(__file__).resolve().with_name("realha_ledger.json")
DEFAULT_DUMP_DIR = REPO / ".pytest_cache" / "realha"

CLASSES = {
    "E": "environment (adapter or fixture)",
    "F": "fake artifact (FakeHass is more obedient or less strict than real HA)",
    "T": "trigger (scenario pokes the engine where production is listener- or schedule-driven)",
    "P": "production bug (real HA is right)",
    "G": "golden premise (scenario encodes a state production cannot produce)",
}
_LEDGER_REQUIRED = (
    "scenario",
    "class",
    "failing_assertions",
    "failing_outcomes",
    "first_difference",
    "evidence",
    "occupant_view",
    "reviewer",
    "date",
)


# ---------------------------------------------------------------------------------------------- judging
def judge_scenario(scenario: dict, result: Any, *, use_coordinator: bool) -> dict:
    """Apply the scenario's assertions to ``result`` with the unchanged outcomes layer.

    Same per-assertion semantics as ``simulate.run_scenario_production``: ``track: integration`` assertions are
    skipped without a coordinator (the real-HA world always has one, so none are skipped there), and unexpected
    callback errors make the run untrustworthy (never a pass).
    """
    decisions = _out.production_decisions(result)
    rows: list[dict] = []
    any_real = False
    for index, a in enumerate(scenario.get("assertions", [])):
        if a.get("track") == "integration" and not use_coordinator:
            rows.append(
                {
                    "index": index,
                    "at": a["at"],
                    "expected": a.get("expect", ""),
                    "actual": None,
                    "pass": None,
                    "skipped": True,
                }
            )
            continue
        any_real = True
        expect = a.get("expect", "")
        custom = _out.check_assertion(result, a, decisions)
        actual = custom if custom is not False else _out.production_outcome_at(decisions, a["at"])
        ok = actual == expect
        detail = None
        if "expect_temp" in a:
            actual_temp = _out.production_temp_at(decisions, a["at"])
            if actual_temp is None:
                ok, detail = False, f"expect_temp={a['expect_temp']} but no target_temp recorded"
            else:
                temp_ok = abs(actual_temp - float(a["expect_temp"])) < 0.01
                ok = ok and temp_ok
                detail = f"expect_temp={a['expect_temp']}, actual_temp={actual_temp}"
        rows.append(
            {
                "index": index,
                "at": a["at"],
                "expected": expect,
                "actual": actual,
                "pass": ok,
                "skipped": False,
                "detail": detail,
            }
        )
    real = [r for r in rows if not r["skipped"]]
    passed: bool | None = all(r["pass"] for r in real) if any_real else None
    errors = [f"{ts}: {exc!r}" for ts, exc in result.callback_errors]
    if errors:
        passed = False
    return {
        "passed": passed,
        "assertions": rows,
        "callback_errors": errors,
        "decisions": [
            {"time": d.time, "outcome": d.outcome, "reason": d.event_type, "target_temp": d.target_temp}
            for d in decisions
        ],
    }


def _iso(ts: Any) -> str | None:
    return ts.isoformat() if isinstance(ts, datetime) else (None if ts is None else str(ts))


def dump_run(
    name: str,
    adapter: str,
    scenario: dict,
    result: Any,
    *,
    use_coordinator: bool,
    dispatch: dict | None = None,
    notes: list | None = None,
) -> dict:
    """A JSON-able record of one run: verdicts (from the unchanged outcomes layer), logs, final state."""
    judged = judge_scenario(scenario, result, use_coordinator=use_coordinator)
    return {
        "scenario": name,
        "adapter": adapter,
        "use_coordinator": use_coordinator,
        **judged,
        "event_log": [{"time": _iso(ts), "type": t, "payload": p} for t, p, ts in result.event_log],
        "action_log": [
            {"time": _iso(e.get("ts")), "domain": e.get("domain"), "service": e.get("service"), "data": e.get("data")}
            for e in result.action_log
        ],
        "engine_state": result.engine_state,
        "dispatch": dict(dispatch or {}),
        "notes": sorted(set(notes or [])),
    }


def write_dump(dump: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{dump['scenario']}.{dump['adapter']}.json"
    path.write_text(json.dumps(dump, indent=1, default=str, sort_keys=True), encoding="utf-8")
    return path


def load_dump(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------ comparison
@dataclass
class Difference:
    """The first place two runs disagree."""

    kind: str  # "verdict" | "decision" | "service_call" | "state" | "none"
    index: int | None
    when: str | None
    fake: Any
    real: Any
    summary: str


@dataclass
class Comparison:
    scenario: str
    fake_passed: bool | None
    real_passed: bool | None
    differences: list[Difference] = field(default_factory=list)
    failing_assertions: list[int] = field(default_factory=list)
    dispatch: dict = field(default_factory=dict)

    @property
    def diverges(self) -> bool:
        return bool(self.differences) or self.fake_passed != self.real_passed


def _call_key(entry: dict) -> tuple:
    data = entry.get("data") or {}
    keep = {k: data[k] for k in sorted(data) if k not in ("entity_id",)}
    return (
        entry.get("domain"),
        entry.get("service"),
        data.get("entity_id"),
        json.dumps(keep, sort_keys=True, default=str),
    )


def _decision_key(entry: dict) -> tuple:
    return ((entry.get("time") or "")[:19], entry.get("outcome"), entry.get("reason"))


def _first_seq_difference(fake: list[dict], real: list[dict], key: Any) -> tuple[int, dict | None, dict | None] | None:
    for i in range(max(len(fake), len(real))):
        f = fake[i] if i < len(fake) else None
        r = real[i] if i < len(real) else None
        if f is None or r is None or key(f) != key(r):
            return i, f, r
    return None


_STATE_KEYS_IGNORED = frozenset({"_last_indoor_temp"})


def compare_dumps(fake: dict, real: dict) -> Comparison:
    """Compare two dumps of the same scenario; the order is verdicts, decisions, service calls, final state."""
    cmp = Comparison(
        scenario=fake["scenario"],
        fake_passed=fake.get("passed"),
        real_passed=real.get("passed"),
        dispatch=real.get("dispatch", {}),
        failing_assertions=[r["index"] for r in real.get("assertions", []) if r.get("pass") is False],
    )
    if fake.get("passed") != real.get("passed"):
        cmp.differences.append(
            Difference(
                "verdict",
                None,
                None,
                fake.get("passed"),
                real.get("passed"),
                f"verdict fake={fake.get('passed')} real={real.get('passed')}",
            )
        )
    hit = _first_seq_difference(fake.get("decisions", []), real.get("decisions", []), _decision_key)
    if hit:
        i, f, r = hit
        when = (f or r or {}).get("time")
        cmp.differences.append(
            Difference("decision", i, when, f, r, f"decision[{i}] fake={_fmt_decision(f)} real={_fmt_decision(r)}")
        )
    hit = _first_seq_difference(fake.get("action_log", []), real.get("action_log", []), _call_key)
    if hit:
        i, f, r = hit
        when = (f or r or {}).get("time")
        cmp.differences.append(
            Difference("service_call", i, when, f, r, f"service call[{i}] fake={_fmt_call(f)} real={_fmt_call(r)}")
        )
    fs, rs = fake.get("engine_state", {}), real.get("engine_state", {})
    for key in sorted(set(fs) | set(rs)):
        if key in _STATE_KEYS_IGNORED:
            continue
        if fs.get(key) != rs.get(key):
            cmp.differences.append(
                Difference(
                    "state",
                    None,
                    None,
                    fs.get(key),
                    rs.get(key),
                    f"state {key} fake={fs.get(key)!r} real={rs.get(key)!r}",
                )
            )
    return cmp


def _fmt_decision(d: dict | None) -> str:
    return "(none)" if d is None else f"{d['outcome']}@{(d.get('time') or '')[11:19]}"


def _fmt_call(c: dict | None) -> str:
    if c is None:
        return "(none)"
    data = c.get("data") or {}
    brief = {k: v for k, v in data.items() if k != "entity_id"}
    return f"{c['domain']}.{c['service']} {data.get('entity_id')} {brief} @{(c.get('time') or '')[11:19]}"


def render_report(cmp: Comparison, fake: dict, real: dict, *, klass: str = "(unclassified)") -> str:
    """The design's per-scenario report (02-step2-design.md 8.3 step 2)."""

    def verdict(v: bool | None) -> str:
        return {True: "PASS", False: "FAIL", None: "SKIP"}[v]

    dispatch = ", ".join(f"{n} {m}" for m, n in sorted(cmp.dispatch.items()))
    mode = "coordinator-mode" if real.get("use_coordinator") else "engine-only (fake)"
    lines = [
        f"Scenario: {cmp.scenario}   golden   {mode}",
        f"Verdict:  FakeHass {verdict(cmp.fake_passed)}  |  Real HA {verdict(cmp.real_passed)}"
        + (f" (assertions {cmp.failing_assertions})" if cmp.failing_assertions else "")
        + f"     Dispatch: {dispatch or '(none)'}",
    ]
    if not cmp.differences:
        lines.append("No difference.")
    for d in cmp.differences[:6]:
        lines.append(f"  {d.kind:<13} {d.summary}")
    lines.append(f"Class: {klass}")
    for note in real.get("notes", []):
        lines.append(f"  note: {note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- ledger
def load_ledger(path: Path = LEDGER_PATH) -> dict[str, dict]:
    """The ledger entries keyed by scenario; rejects malformed entries so a typo cannot silently tolerate a failure."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    out: dict[str, dict] = {}
    for e in entries:
        missing = [k for k in _LEDGER_REQUIRED if not e.get(k) and e.get(k) != []]
        if missing:
            raise ValueError(f"ledger entry {e.get('scenario')!r} is missing {missing}")
        if e["class"] not in CLASSES:
            raise ValueError(f"ledger entry {e['scenario']!r}: class {e['class']!r} is not one of {sorted(CLASSES)}")
        if e["scenario"] in out:
            raise ValueError(f"ledger lists {e['scenario']!r} twice")
        out[e["scenario"]] = e
    return out


def _exception_name(text: str) -> str:
    """``"<ts>: SomeError('x')"`` -> ``SomeError`` (the repr's class, stable across messages)."""
    m = re.search(r":\s*([A-Za-z_][\w.]*)\(", text)
    return m.group(1) if m else "exception"


def _timer_name(text: str) -> str:
    """A leaked timer's callback qualname without the memory address / argument noise."""
    m = re.search(r"((?:AutomationEngine|[A-Za-z]*Coordinator)\.[\w.<>]+)", text)
    if m:
        return m.group(1)
    m = re.search(
        r"TimerHandle when=[\d.]+ (\w+)", text
    )  # the repr is truncated; the scheduler helper still names the kind
    return m.group(1) if m else "timer"


def failing_outcomes(dump: dict) -> tuple[list[int], dict[str, str]]:
    """``(failing indexes, {index: what actually happened})`` for one real-HA run dump.

    Assertion rows keep their actual outcome; an exception Climate Advisor logged is ``-1`` and is reported
    EVEN WHEN assertions also fail (a crash hidden behind a plain assertion failure is the worse finding);
    a timer left armed after the unload is ``-2``.  The outcome strings are what the ledger pins, so a
    scenario that starts failing a ledgered assertion in a different or worse way is caught.
    """
    outcomes: dict[str, str] = {}
    for r in dump.get("assertions", []):
        if r.get("pass") is False:
            outcomes[str(r["index"])] = str(r.get("actual"))
    errors = dump.get("callback_errors") or []
    if errors:
        outcomes["-1"] = "exception:" + ",".join(sorted({_exception_name(e) for e in errors}))
    timers = dump.get("leaked_timers") or []
    if timers:
        outcomes["-2"] = "timer:" + ",".join(sorted({_timer_name(t) for t in timers}))
    return sorted(int(k) for k in outcomes), outcomes


def check_ledger(
    failing: dict[str, list[int]],
    ledger: dict[str, dict],
    run: set[str] | None = None,
    outcomes: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Ratchet.  ``failing`` maps scenario -> failing assertion indexes (-1 = exception, -2 = leaked timer).

    ``outcomes`` (scenario -> {index: actual outcome}, from ``failing_outcomes``) is compared with the entry's
    ``failing_outcomes``: the same assertion failing in a different or worse way is a new finding, not the
    tolerated one.  Returns problems (empty = clean): an unlisted failure, a changed failing set or outcome,
    and a listed scenario that no longer fails.  ``run`` restricts the stale check to the scenarios actually
    run (so ``-k`` on one golden does not report every other entry stale).
    """
    problems: list[str] = []
    for name, idx in sorted(failing.items()):
        entry = ledger.get(name)
        if entry is None:
            problems.append(f"{name}: fails on real HA (assertions {idx}) and is not in the ledger - triage and add it")
        elif sorted(entry["failing_assertions"]) != sorted(idx):
            problems.append(
                f"{name}: ledger says assertions {sorted(entry['failing_assertions'])} fail, now {sorted(idx)} - "
                "investigate and update the entry"
            )
        elif outcomes is not None and entry.get("failing_outcomes") != outcomes.get(name, {}):
            problems.append(
                f"{name}: same assertions fail but differently - ledger says {entry.get('failing_outcomes')}, "
                f"now {outcomes.get(name, {})} - investigate (a worse failure is not the tolerated one) and update"
            )
    for name in sorted(ledger):
        if (run is None or name in run) and name not in failing:
            problems.append(f"{name}: listed in the ledger but passes on real HA now - remove the stale entry")
    return problems


# --------------------------------------------------------------------------------------------- CLI
def _golden_paths(names: list[str]) -> list[Path]:
    paths = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
    return [p for p in paths if not names or p.stem in names]


def _dump_fake(names: list[str], out: Path) -> int:
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415  (installs the stubs)

    for path in _golden_paths(names):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        use_coord = bool(scenario.get("use_coordinator", False))
        result = run_production_scenario(scenario, use_coordinator=use_coord)
        write_dump(dump_run(path.stem, "fake", scenario, result, use_coordinator=use_coord), out)
    return 0


def _compare(fake_dir: Path, real_dir: Path, out: Path) -> int:
    ledger = load_ledger()
    out.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    n_div = 0
    for real_path in sorted(real_dir.glob("*.real.json")):
        name = real_path.name[: -len(".real.json")]
        fake_path = fake_dir / f"{name}.fake.json"
        if not fake_path.exists():
            rows.append(f"| {name} | (no fake dump) | | |")
            continue
        fake, real = load_dump(fake_path), load_dump(real_path)
        cmp = compare_dumps(fake, real)
        entry = ledger.get(name)
        text = render_report(cmp, fake, real, klass=entry["class"] if entry else "(unclassified)")
        (out / f"{name}.md").write_text(text + "\n", encoding="utf-8")
        if cmp.diverges:
            n_div += 1
        first = cmp.differences[0].summary if cmp.differences else "-"
        klass = entry["class"] if entry else "-"
        rows.append(f"| {name} | {klass} | {'DIVERGES' if cmp.diverges else 'same'} | {first[:100]} |")
    summary = [
        "# Real-HA vs FakeHass divergence summary",
        "",
        f"{n_div} of {len(rows)} scenarios diverge.",
        "",
        "| scenario | class | status | first difference |",
        "|---|---|---|---|",
        *rows,
    ]
    (out / "SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"{n_div} of {len(rows)} scenarios diverge; reports in {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump-fake", help="run goldens on the FakeHass adapter and write dumps")
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--out", type=Path, default=DEFAULT_DUMP_DIR)
    p = sub.add_parser("compare", help="diff the fake and real dumps and write the reports")
    p.add_argument("--fake", type=Path, default=DEFAULT_DUMP_DIR)
    p.add_argument("--real", type=Path, default=DEFAULT_DUMP_DIR)
    p.add_argument("--out", type=Path, default=DEFAULT_DUMP_DIR / "reports")
    args = ap.parse_args(argv)
    if args.cmd == "dump-fake":
        return _dump_fake(args.scenarios, args.out)
    return _compare(args.fake, args.real, args.out)


if __name__ == "__main__":
    sys.exit(main())
