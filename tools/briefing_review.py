#!/usr/bin/env python3
"""Briefing coherence checker (Issue #518).

Renders generate_briefing() across a matrix of day_type x hvac_mode x
setback-active combinations and runs deterministic coherence assertions —
no contradicting window/AC times, no AC promise that ignores window state,
no unconditional setback footer, no "no action needed" boilerplate.

This is the mechanical half of the issue's requested validation loop: it
runs in CI/dev like any other check, with fully deterministic pass/fail
(the shipped briefing has no LLM in its runtime path — see briefing.py's
module docstring). When AUTHORING a new scenario, the recommended workflow
is to also have an agent review the rendered text for user-outcome
soundness before promoting it (see docs/briefing-spec.md); that review is
a development-time step, not something this script performs automatically.

Usage:
  python tools/briefing_review.py          # run the full scenario matrix
  python tools/briefing_review.py -v       # also print each rendered briefing
"""

import argparse
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from custom_components.climate_advisor.briefing import generate_briefing  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402

COMFORT_HEAT = 68.0
COMFORT_COOL = 75.0
SETBACK_HEAT = 60.0
SETBACK_COOL = 80.0
WAKE_TIME = time(6, 30)
SLEEP_TIME = time(22, 30)


def _curve(temps: list[float], start_hour_utc: int = 13) -> list[dict]:
    base = datetime(2026, 7, 25, start_hour_utc, 0, 0, tzinfo=UTC)
    return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]


def _make_classification(day_type: str, today_high: float, today_low: float) -> DayClassification:
    return DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=1.0,
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=today_high,
        tomorrow_low=today_low,
    )


def _check_no_contradicting_window_times(name: str, text: str) -> list[str]:
    """Header 'Windows: Open X - Y' close time must match the body's 'Close up at Y'."""
    problems = []
    header_line = next((line for line in text.splitlines() if "Windows:" in line), None)
    if header_line and "–" in header_line and "Close up at" in text:
        header_close = header_line.split("–")[-1].strip()
        body_close = None
        for line in text.splitlines():
            if "Close up at" in line:
                body_close = line.split("Close up at", 1)[1].split("—")[0].strip()
                break
        if body_close and header_close != body_close:
            problems.append(f"[{name}] header close time '{header_close}' != body close time '{body_close}'")
    return problems


def _check_no_orphaned_ac_promise(name: str, text: str) -> list[str]:
    """AC-safety-net language must not claim a fixed start time or ignore window state."""
    problems = []
    if "I'll run the AC starting around" in text:
        problems.append(f"[{name}] AC sentence promises a fixed clock time (window-state unaware)")
    if "no action needed" in text.lower():
        problems.append(f"[{name}] contains flagged boilerplate phrase 'no action needed'")
    return problems


def _check_footer_matches_header(name: str, text: str) -> list[str]:
    """The adaptive-setback footer must not appear when the header says 'No setback'."""
    problems = []
    has_no_setback_header = any("Bedtime Setback:" in line and "No setback" in line for line in text.splitlines())
    has_footer = "tuned to your home's actual heating performance" in text
    if has_no_setback_header and has_footer:
        problems.append(f"[{name}] footer claims tuned setback timing but header says 'No setback'")
    return problems


def _check_no_dangling_ac_off_claim(name: str, text: str) -> list[str]:
    """'I'll turn off the AC' must not appear without an earlier AC-forecast/ceiling sentence."""
    problems = []
    if "I'll turn off the AC" in text and "forecast to reach" not in text:
        problems.append(f"[{name}] claims 'I'll turn off the AC' with no preceding forecast/ceiling-breach sentence")
    return problems


CHECKS = [
    _check_no_contradicting_window_times,
    _check_no_orphaned_ac_promise,
    _check_footer_matches_header,
    _check_no_dangling_ac_off_claim,
]


def _scenario_matrix() -> list[tuple[str, dict]]:
    """day_type x hvac_mode x setback-active combinations relevant to Issue #518."""
    scenarios = []

    # Issue #518's reported case: warm/windows day, ceiling breach + recovery predicted,
    # adaptive thermal model confident (so the footer-suppression fix is exercised).
    c = _make_classification("warm", today_high=81, today_low=60)
    indoor = _curve([68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 78, 77, 76])
    outdoor = _curve([60, 63, 66, 69, 72, 75, 78, 80, 82, 83, 82, 79, 76, 73])
    scenarios.append(
        (
            "warm_windows_day_breach_and_recovery",
            dict(
                classification=c,
                predicted_indoor_future=indoor,
                predicted_outdoor_future=outdoor,
                adaptive_thermal_active=True,
            ),
        )
    )

    # Warm day, no ODE data at all — pure fallback path (classifier constants only).
    scenarios.append(
        (
            "warm_windows_day_no_forecast_data",
            dict(classification=_make_classification("warm", today_high=80, today_low=60)),
        )
    )

    # Warm day, ceiling breach predicted but no recovery before end of curve.
    c2 = _make_classification("warm", today_high=84, today_low=64)
    indoor2 = _curve([70, 72, 74, 76, 78])
    outdoor2 = _curve([65, 70, 76, 82, 86])
    scenarios.append(
        (
            "warm_windows_day_breach_no_recovery",
            dict(classification=c2, predicted_indoor_future=indoor2, predicted_outdoor_future=outdoor2),
        )
    )

    # Cold day, heat mode, adaptive setback active — footer SHOULD appear (control case).
    scenarios.append(
        (
            "cold_heat_day_adaptive_setback_active",
            dict(
                classification=_make_classification("cold", today_high=38, today_low=22),
                adaptive_thermal_active=True,
            ),
        )
    )

    # Hot day, cool mode — unrelated to warm-day timing bug, included for matrix coverage.
    scenarios.append(
        (
            "hot_cool_day",
            dict(classification=_make_classification("hot", today_high=95, today_low=72)),
        )
    )

    # Mild day, HVAC off, no setback — mirrors the warm/off footer-suppression case.
    scenarios.append(
        (
            "mild_off_day_adaptive_model_present",
            dict(
                classification=_make_classification("mild", today_high=68, today_low=48),
                adaptive_thermal_active=True,
            ),
        )
    )

    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="print each rendered briefing")
    args = parser.parse_args()

    all_problems: list[str] = []
    for name, kwargs in _scenario_matrix():
        text = generate_briefing(
            comfort_heat=COMFORT_HEAT,
            comfort_cool=COMFORT_COOL,
            setback_heat=SETBACK_HEAT,
            setback_cool=SETBACK_COOL,
            wake_time=WAKE_TIME,
            sleep_time=SLEEP_TIME,
            **kwargs,
        )
        if args.verbose:
            print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
            print(text.encode("ascii", "replace").decode("ascii"))

        for check in CHECKS:
            all_problems.extend(check(name, text))

    print(f"\n{len(_scenario_matrix())} scenarios checked, {len(all_problems)} coherence problems found.")
    for problem in all_problems:
        print(f"  FAIL: {problem}")

    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
