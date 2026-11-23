#!/usr/bin/env python3
"""The #1544 heat-only / cool-only matrix: every golden scenario through the REAL engine as ``[off, heat]`` and
``[off, cool]``.

Occupant view: a home with electric baseboards (``[off, heat]``) must never be sent a cooling command, shown an AC
run or a pre-cool in its plan, or promised air conditioning in its briefing; a cooling-only home (``[off, cool]``) gets
the mirror. This is the method that found #1488 / #1489, made permanent (Issue #1544).

Each golden runs unchanged except for the thermostat's advertised ``hvac_modes`` (scenario ``config``
``thermostat_hvac_modes``; the golden files themselves are never edited). After the run it checks:

* commands: no ``climate.set_hvac_mode`` / ``climate.set_temperature`` naming a mode the device lacks, and no
  dual-setpoint write to a single-mode device;
* plan (scenarios with a real coordinator): no operation-timeline step in the missing family, no pre-cool / ceiling
  guard step on a heat-only device and no pre-heat step on a cool-only one, no plan day naming the missing mode;
* briefing: the rendered English briefing holds none of the promises the device cannot keep (AC / pre-cool /
  "Cool at" for heat-only; heater / pre-heat / "Heat at" for cool-only). Coordinator scenarios render the
  coordinator's own briefing; engine-only scenarios render ``generate_briefing()`` from the engine's live
  classification and its ``zone_service()``.

The golden's own assertions are NOT judged here (they were written for a heat-and-cool thermostat).

Usage::

    python tools/zone_matrix.py                 # every golden, both devices
    python tools/zone_matrix.py --only NAME ... # selected scenarios (stem)
    python tools/zone_matrix.py --json          # machine-readable summary
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tools" / "simulations" / "golden"

DEVICES: dict[str, tuple[str, ...]] = {"heat_only": ("off", "heat"), "cool_only": ("off", "cool")}
# Issue #1699 policy lock, checked on the plan and the briefing only: a heat-and-cool thermostat with the season set to
# Heat only. Its commands are not checked: the safety release may run cool past the settling tolerance (by design).
POLICY: dict[str, tuple[tuple[str, ...], dict[str, Any], str]] = {
    "season_heat_only": (("off", "heat", "cool", "heat_cool"), {"hvac_season": "heat_only"}, "heat_only"),
    "season_cool_only": (("off", "heat", "cool", "heat_cool"), {"hvac_season": "cool_only"}, "cool_only"),
    # An outdoor cool lockout no forecast releases (110 F, released at 113.6 F): cooling is off whenever an outdoor
    # reading has been seen. A scenario that never feeds one has no lockout; that run is not judged (``not_locked``).
    "cool_lockout": (("off", "heat", "cool", "heat_cool"), {"cool_lockout_outdoor_temp": 110.0}, "heat_only"),
}
# The baseline the positive window check compares with: the golden as authored (heat and cool).
BOTH = "both"

# Promises a device without that family can never keep (matched on the rendered English briefing).
_FORBIDDEN_BRIEFING: dict[str, tuple[str, ...]] = {
    "heat_only": (
        r"\bAC\b",
        r"[Pp]re-cool",
        r"HVAC Mode: Cool",
        r"air condition",
        r"I'll cool",
        r"pull it right back down",
    ),
    "cool_only": (
        r"\bheater\b",
        r"[Pp]re-heat",
        r"HVAC Mode: Heat",
        r"the heat (?:kicks|running|off)",
        r"I'll warm",
        r"warm things right up",
        r"I'll drop to",
    ),
}


@dataclass
class MatrixResult:
    scenario: str
    device: str
    problems: list[str] = field(default_factory=list)
    plan_checked: bool = False
    briefing_checked: bool = False
    # Facts the test uses to prove the matrix judged something: a classification existed (a briefing can be written)
    # and the scenario ran a real coordinator (a plan can be built); a lockout device whose lockout never engaged.
    has_classification: bool = False
    has_coordinator: bool = False
    not_locked: bool = False
    window_kinds: frozenset[str] = frozenset()  # accepted window-candidate kinds in the plan
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.error is None


def golden_files() -> list[Path]:
    return sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")


def _profile(device: str) -> str:
    """The equipment profile whose rules a device is judged by (a policy lock is judged as its single family)."""
    return POLICY[device][2] if device in POLICY else device


def _missing_family(device: str) -> str:
    return "cool" if _profile(device) == "heat_only" else "heat"


def _command_problems(action_log: Iterable[dict], device: str) -> list[str]:
    modes = DEVICES[device]
    missing = _missing_family(device)
    out: list[str] = []
    for rec in action_log:
        if rec.get("domain") != "climate":
            continue
        data = rec.get("data") or {}
        service = rec.get("service")
        mode = data.get("hvac_mode")
        if service in ("set_hvac_mode", "set_temperature") and mode is not None and mode not in modes:
            out.append(f"command {service} hvac_mode={mode} at {rec.get('ts')}")
        if service == "set_temperature" and ("target_temp_low" in data or "target_temp_high" in data):
            out.append(f"command set_temperature dual setpoint {data} at {rec.get('ts')} on a {missing}-less device")
    return out


def _plan_problems(timeline: Any, device: str) -> list[str]:
    from custom_components.climate_advisor.operation_timeline import StepKind  # noqa: PLC0415

    missing = _missing_family(device)
    banned = {StepKind.PRECOOL_START, StepKind.CEILING_GUARD} if missing == "cool" else {StepKind.PREHEAT_START}
    out: list[str] = []
    for step in getattr(timeline, "steps", ()) or ():
        if step.mode == missing or (step.mode in ("heat_cool", "auto")):
            out.append(f"plan step {step.kind.value} mode={step.mode} at {step.ts.isoformat()}: {step.text}")
        elif step.kind in banned:
            out.append(f"plan step {step.kind.value} at {step.ts.isoformat()}: {step.text}")
    for day in getattr(timeline, "days", ()) or ():
        if day.get("hvac_mode") == missing:
            out.append(f"plan day {day.get('date')} hvac_mode={missing}")
    return out


def _briefing_problems(text: str, device: str) -> list[str]:
    out = []
    for pattern in _FORBIDDEN_BRIEFING[_profile(device)]:
        m = re.search(pattern, text)
        if m is not None:
            start = max(0, m.start() - 60)
            out.append(f"briefing says {m.group(0)!r}: ...{text[start : m.end() + 60]!r}...")
    return out


def _engine_briefing(engine: Any) -> str | None:
    """Both variants of ``generate_briefing()`` (full and TLDR) from the engine's live classification and its zone
    service (engine-only scenarios)."""
    from datetime import time  # noqa: PLC0415

    from custom_components.climate_advisor.briefing import generate_briefing  # noqa: PLC0415

    c = getattr(engine, "_current_classification", None)
    if c is None:
        return None
    cfg = engine.config
    kwargs: dict[str, Any] = {
        "classification": c,
        "comfort_heat": float(cfg.get("comfort_heat", 70)),
        "comfort_cool": float(cfg.get("comfort_cool", 75)),
        "setback_heat": float(cfg.get("setback_heat", 60)),
        "setback_cool": float(cfg.get("setback_cool", 80)),
        "wake_time": time(6, 30),
        "sleep_time": time(22, 30),
        "occupancy_mode": getattr(engine, "_occupancy_mode", "home") or "home",
        "runtime_config": dict(cfg),
        "zone_service": engine.zone_service(),
    }
    return f"{generate_briefing(**kwargs)}\n{generate_briefing(**kwargs, verbosity='tldr_only')}"


def _coordinator_plan_and_briefing(coord: Any) -> tuple[Any, str | None]:
    """The coordinator's own plan and both briefing variants (full and TLDR). Any failure propagates: ``run_one``
    records it as the run's error, so a surface that cannot be built is never silently "not checked"."""
    c = getattr(coord, "_current_classification", None)
    if c is None:
        return None, None
    coord._timeline_shadow_run_impl("zone_matrix")
    latch = getattr(coord, "_timeline_latch", None)
    timeline = latch.timeline if latch is not None else None
    full, short = coord._build_briefing_text(c)
    return timeline, f"{full}\n{short}"


def run_one(path: Path, device: str) -> MatrixResult:
    """Run one golden as ``device`` through the real engine and check every surface."""
    for p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from tools.sim_harness.run_production import FakeHassSession, drive_scenario  # noqa: PLC0415

    result = MatrixResult(path.stem, device)
    scenario = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
    config = scenario.setdefault("config", {})
    if device == BOTH:
        pass
    elif device in POLICY:
        modes, extra, _p = POLICY[device]
        config["thermostat_hvac_modes"] = list(modes)
        config.update(extra)
    else:
        config["thermostat_hvac_modes"] = list(DEVICES[device])
    session = FakeHassSession(scenario, use_coordinator=bool(scenario.get("use_coordinator", False)))
    try:
        run = drive_scenario(session, scenario)
        coord = session.coordinator
        result.has_coordinator = coord is not None
        result.has_classification = getattr(session.engine, "_current_classification", None) is not None
        if device in DEVICES:
            result.problems.extend(_command_problems(run.action_log, device))
        if coord is not None:
            timeline, briefing = _coordinator_plan_and_briefing(coord)
        else:
            timeline, briefing = None, _engine_briefing(session.engine)
        if timeline is not None:
            result.window_kinds = frozenset(c.kind for c in timeline.candidates if c.accepted)
        if device in POLICY and session.engine.zone_service().serves(_missing_family(device)):
            result.not_locked = True  # e.g. no outdoor reading ever arrived: there is no lockout to judge
        elif device != BOTH:
            if timeline is not None:
                result.plan_checked = True
                result.problems.extend(_plan_problems(timeline, device))
            if briefing:
                result.briefing_checked = True
                result.problems.extend(_briefing_problems(briefing, device))
    except Exception as exc:  # noqa: BLE001 - one broken run is reported, never hides the others
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()
    return result


ALL_DEVICES: tuple[str, ...] = (*DEVICES, *POLICY)


def run_matrix(paths: Iterable[Path], devices: Iterable[str] = ALL_DEVICES) -> list[MatrixResult]:
    return [run_one(p, d) for p in paths for d in devices]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", help="scenario stems to run (default: every golden)")
    ap.add_argument("--device", choices=sorted(ALL_DEVICES), action="append", help="device (default: all)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary")
    args = ap.parse_args(argv)
    paths = golden_files()
    if args.only:
        wanted = set(args.only)
        paths = [p for p in paths if p.stem in wanted]
    results = run_matrix(paths, args.device or ALL_DEVICES)
    bad = [r for r in results if not r.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "runs": len(results),
                    "scenarios": len(paths),
                    "plan_checked": sum(r.plan_checked for r in results),
                    "briefing_checked": sum(r.briefing_checked for r in results),
                    "failures": [r.__dict__ for r in bad],
                },
                indent=2,
                default=str,
            )
        )
    else:
        for r in bad:
            print(f"FAIL {r.scenario} [{r.device}]")
            if r.error:
                print(f"  error: {r.error}")
            for p in r.problems:
                print(f"  {p}")
        print(
            f"{len(paths)} scenarios x {len(args.device or ALL_DEVICES)} devices = {len(results)} runs; "
            f"plan checked {sum(r.plan_checked for r in results)}, briefing checked "
            f"{sum(r.briefing_checked for r in results)}; {len(bad)} failing"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
