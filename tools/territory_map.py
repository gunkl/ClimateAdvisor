"""territory_map — instrument the golden corpus to measure which decision functions fire.

This is the first cut of the architecture-reset Step-1 "territory map" gating
artifact. It wraps every known decision function (and control primitive) on the
production ``AutomationEngine`` with a recorder, replays every golden scenario
through the real engine, and reports:

  - **Coverage:** how many times each decision function fired across the corpus.
  - **Blind spots:** decision functions that NEVER fire under any golden — these are
    exactly where a subtle boundary bug would go undetected by the current suite, and
    what the synthetic enumerator + new tests must target.
  - **Input signatures (first cut):** the distinct explicit-argument shapes each
    decision function was called with. This is a *partial* feature list — it captures
    the explicit call arguments, not yet the full set of ``self._`` / config reads
    inside each function. Comprehensive read-instrumentation is a later deliverable;
    this establishes the scaffold and the honest coverage picture now.

Pure test/tooling: monkeypatches the engine class with pass-through recording
wrappers and reads golden JSON. Touches no production source.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from tools.sim_harness._loop import close_loop  # noqa: E402
from tools.sim_harness.run_production import run_production_scenario  # noqa: E402

_GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"

# The ~19 decision functions (re-verified inventory) plus the 5 control primitives
# every decision converges on. Async unless noted.
_DECISION_METHODS = [
    "apply_classification",
    "check_natural_vent_conditions",
    "fan_thermostat_check",
    "check_window_cooling_opportunity",
    "_nat_vent_may_reactivate",  # sync gate helper (returns bool)
    "_re_pause_for_open_sensor",
    "handle_door_window_open",
    "handle_all_doors_windows_closed",
    "reconcile_fan_on_startup",
    "handle_bedtime",
    "handle_morning_wakeup",
    "handle_occupancy_home",
    "handle_occupancy_away",
    "handle_occupancy_vacation",
    "nat_vent_temperature_check",
    "_apply_nat_vent_hvac_state",
    "_exit_nat_vent",
    "handle_pre_cool",
    "_reconcile_fan_physical_drift",
]
_CONTROL_PRIMITIVES = [
    "_set_hvac_mode",
    "_set_temperature",
    "_set_temperature_for_mode",
    "_activate_fan",
    "_deactivate_fan",
]


def _safe_sig(args: tuple, kwargs: dict) -> str:
    """A compact, stable signature of the explicit call arguments (skip self)."""
    parts: list[str] = []
    for a in args[1:]:  # skip self
        parts.append(_render_arg(a))
    for k, v in sorted(kwargs.items()):
        parts.append(f"{k}={_render_arg(v)}")
    return "(" + ", ".join(parts) + ")"


def _render_arg(v: Any) -> str:
    """Render a single arg to a coarse, stable token (type/bucketed, not raw value)."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return repr(v) if len(v) <= 20 else "str"
    if v is None:
        return "None"
    # DayClassification / objects: show class + day_type/hvac_mode if present
    cls = type(v).__name__
    dt = getattr(v, "day_type", None)
    hv = getattr(v, "hvac_mode", None)
    if dt is not None or hv is not None:
        return f"{cls}(day_type={dt},hvac_mode={hv})"
    if isinstance(v, (list, tuple)):
        return f"{cls}[{len(v)}]"
    if isinstance(v, dict):
        return f"dict[{len(v)}]"
    return cls


class _Recorder:
    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)
        self.signatures: dict[str, set[str]] = defaultdict(set)

    def note(self, method: str, args: tuple, kwargs: dict) -> None:
        self.calls[method] += 1
        with contextlib.suppress(Exception):
            self.signatures[method].add(_safe_sig(args, kwargs))


@contextlib.contextmanager
def _instrumented(recorder: _Recorder):
    """Wrap decision methods + control primitives with pass-through recorders."""
    import inspect  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415

    patches = []
    for method in _DECISION_METHODS + _CONTROL_PRIMITIVES:
        original = getattr(AutomationEngine, method, None)
        if original is None:
            continue

        if inspect.iscoroutinefunction(original):

            def _make_async(orig: Any, mname: str):
                async def _wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                    recorder.note(mname, (self, *args), kwargs)
                    return await orig(self, *args, **kwargs)

                return _wrapper

            patches.append(patch.object(AutomationEngine, method, _make_async(original, method)))
        else:

            def _make_sync(orig: Any, mname: str):
                def _wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                    recorder.note(mname, (self, *args), kwargs)
                    return orig(self, *args, **kwargs)

                return _wrapper

            patches.append(patch.object(AutomationEngine, method, _make_sync(original, method)))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _load_goldens() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        with path.open(encoding="utf-8") as fh, contextlib.suppress(json.JSONDecodeError):
            out.append((path.stem, json.load(fh)))
    return out


def build_map(verbose: bool = False) -> dict[str, Any]:
    goldens = _load_goldens()
    recorder = _Recorder()
    errors: list[str] = []
    with _instrumented(recorder):
        for name, scen in goldens:
            try:
                run_production_scenario(scen)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

    fired = {m: recorder.calls.get(m, 0) for m in _DECISION_METHODS}
    prims = {m: recorder.calls.get(m, 0) for m in _CONTROL_PRIMITIVES}
    blind = [m for m, c in fired.items() if c == 0]
    return {
        "n_goldens": len(goldens),
        "decision_calls": fired,
        "control_calls": prims,
        "blind_spots": blind,
        "signatures": {m: sorted(recorder.signatures.get(m, set())) for m in _DECISION_METHODS},
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Territory map: decision-function coverage over goldens.")
    parser.add_argument("--json", action="store_true", help="Emit the raw map as JSON.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show per-function input signatures.")
    args = parser.parse_args(argv)

    m = build_map(verbose=args.verbose)
    if args.json:
        print(json.dumps(m, indent=2, default=str))
        return 0

    print(f"=== TERRITORY MAP — {m['n_goldens']} golden scenarios ===\n")
    print("Decision-function coverage (calls across the corpus):")
    for method, count in sorted(m["decision_calls"].items(), key=lambda kv: -kv[1]):
        flag = "  <-- BLIND SPOT (never fires)" if count == 0 else ""
        print(f"  {count:5d}  {method}{flag}")
    print("\nControl primitives:")
    for method, count in sorted(m["control_calls"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:5d}  {method}")

    print(f"\nBlind spots ({len(m['blind_spots'])} decision functions never exercised by any golden):")
    for method in m["blind_spots"]:
        print(f"  - {method}")
    if not m["blind_spots"]:
        print("  (none — every catalogued decision function fires at least once)")

    if args.verbose:
        print("\nInput signatures (explicit call-argument shapes — partial feature list):")
        for method, sigs in m["signatures"].items():
            if sigs:
                print(f"  {method}:")
                for s in sigs:
                    print(f"      {s}")

    if m["errors"]:
        print(f"\nScenario errors ({len(m['errors'])}):")
        for e in m["errors"]:
            print(f"  - {e}")
    return 0


if __name__ == "__main__":
    _exit_code = main()
    close_loop()  # avoid a ResourceWarning: unclosed event loop at interpreter exit
    raise SystemExit(_exit_code)
