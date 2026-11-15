"""Issue #1048 (changes 12 + 17, automation half): the engine renders user-visible temperatures
in the household's unit, and stores override-record setpoints in °F.

Occupant: a Celsius household used to read "natural vent exit: outdoor 64.0°F > threshold 75.0°F"
and "sleep band [64/72]" in the Activity Log. Fahrenheit homes must read exactly what they read
before (characterization), Celsius homes the converted value.

No mirrored logic: the real engine runs the real production-harness scenarios (goldens + pending)
and every reason string it records via ``_record_action`` is inspected.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
    select_comfort_band,
)
from tools.sim_harness.build_engine import build_headless_engine  # noqa: E402
from tools.sim_harness.run_production import run_production_scenario  # noqa: E402

_SIM_DIR = TOOLS / "simulations"
_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)°F")


def _run_all(display_unit: str) -> list[str]:
    """Run every golden + pending scenario and return every reason ``_record_action`` saw.

    The scenarios keep their Fahrenheit config so control flow is identical in both runs;
    ``display_unit`` only forces the *rendering* helpers (``format_temp`` and friends, as imported
    into ``automation``) to the given unit, which is exactly the seam change 17 routes every
    reason string through.
    """
    from custom_components.climate_advisor import automation as auto_mod  # noqa: PLC0415

    reasons: list[str] = []
    orig_record = AutomationEngine._record_action
    saved = {n: getattr(auto_mod, n) for n in ("format_temp", "format_temp_delta", "display_temp", "display_delta")}

    def _forced(fn):
        return lambda value, unit, *a, **k: fn(value, display_unit, *a, **k)

    def _spy(self, action, reason):
        reasons.append(reason)
        return orig_record(self, action, reason)

    AutomationEngine._record_action = _spy
    for name, fn in saved.items():
        setattr(auto_mod, name, _forced(fn))
    # Issue #1341: reasons are messages whose temperature parameters carry the display unit, built by the engine's
    # typed twins ``_tp``/``_tdp``; the same seam forces the unit there (control flow still reads ``_temp_unit``).
    from custom_components.climate_advisor import i18n  # noqa: PLC0415

    saved_twins = (AutomationEngine._tp, AutomationEngine._tdp)
    AutomationEngine._tp = lambda self, v, decimals=1: i18n.Temp(v, display_unit, decimals)
    AutomationEngine._tdp = lambda self, d, decimals=1: i18n.TempDelta(d, display_unit, decimals)
    try:
        for path in sorted(list((_SIM_DIR / "golden").glob("*.json")) + list((_SIM_DIR / "pending").glob("*.json"))):
            scenario = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
            try:
                run_production_scenario(scenario)
            except Exception:  # noqa: BLE001 - a scenario that cannot run headless is not this test's concern
                continue
    finally:
        AutomationEngine._record_action = orig_record
        AutomationEngine._tp, AutomationEngine._tdp = saved_twins
        for name, fn in saved.items():
            setattr(auto_mod, name, fn)
    return reasons


@pytest.fixture(scope="module")
def reasons_f() -> list[str]:
    return _run_all("fahrenheit")


@pytest.fixture(scope="module")
def reasons_c() -> list[str]:
    return _run_all("celsius")


# Each (label, regex) is a user-visible reason family from change 17 that production emits in the
# scenarios above. The Fahrenheit text must match the pre-change wording exactly.
_F_FAMILIES = [
    ("airflow reversed", r"^nat vent exit: outdoor \d+\.\d°F >= indoor \d+\.\d°F — airflow reversed$"),
    ("airflow reversed (fast loop)", r"outdoor \d+\.\d°F >= indoor \d+\.\d°F — airflow reversed$"),
    ("cooled to floor", r"indoor \d+\.\d°F ≤ comfort floor \d+\.\d°F \(cooled to floor\)$"),
    ("ceiling threshold", r"outdoor \d+\.\d°F > ceiling threshold$"),
    ("re-engaged", r"^nat-vent re-engaged: outdoor \d+\.\d°F < indoor \d+\.\d°F − \d+\.\d°F hysteresis, "),
    (
        "natural ventilation",
        r"^natural ventilation: outdoor \d+\.\d°F < indoor \d+\.\d°F, outdoor \d+\.\d°F <= \d+\.\d°F$",
    ),
    (
        "grace expired",
        r"^grace expired — nat-vent: outdoor \d+\.\d°F < indoor \d+\.\d°F, outdoor \d+\.\d°F ≤ \d+\.\d°F$",
    ),
    ("startup reconcile", r"reconcile — fan already running, indoor \d+\.\d°F, outdoor \d+\.\d°F, nat-vent"),
    ("bedtime sleep band", r"^bedtime — sleep band \[\d+/\d+\]$"),
    ("morning comfort band", r"^morning wake-up — comfort band \[\d+/\d+\]$"),
    ("economizer cool-down", r"^economizer cool-down — fan assists the band's cooling: indoor \d+°F > comfort \d+°F, "),
]


@pytest.mark.parametrize(("label", "pattern"), _F_FAMILIES, ids=[f[0] for f in _F_FAMILIES])
def test_fahrenheit_reason_text_unchanged(reasons_f, label, pattern):
    """Characterization: a Fahrenheit engine produces the same wording as before the change."""
    assert any(re.search(pattern, r) for r in reasons_f), f"no Fahrenheit reason matched {label!r}"


def test_celsius_engine_never_renders_fahrenheit(reasons_c):
    assert reasons_c, "scenario run recorded no reasons"
    offenders = [r for r in reasons_c if "°F" in r]
    assert offenders == []


def test_celsius_values_are_converted_not_relabelled(reasons_f, reasons_c):
    """Same scenarios, same decisions, Celsius rendering: every absolute temperature in the
    Fahrenheit reason is the converted value in the Celsius one (75.0°F -> 23.9°C)."""
    assert len(reasons_f) == len(reasons_c)
    checked = 0
    for rf, rc in zip(reasons_f, reasons_c, strict=True):
        # hysteresis / trend are deltas (scale-only) -- covered separately
        if "°F" not in rf or "hysteresis" in rf or "trend" in rf:
            continue
        expected = _TEMP_RE.sub(lambda m: f"{(float(m.group(1)) - 32.0) * 5.0 / 9.0:.{_decimals(m.group(1))}f}°C", rf)
        assert rc == expected
        checked += 1
    assert checked > 20


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def test_celsius_delta_is_scale_only(reasons_f, reasons_c):
    """'− 1.0°F hysteresis' is a difference: 1.0°F -> 0.6°C (no -32 offset)."""
    pairs = [(f, c) for f, c in zip(reasons_f, reasons_c, strict=True) if "hysteresis" in f]
    assert pairs, "no hysteresis reason produced by the scenarios"
    for rf, rc in pairs:
        assert "− 1.0°F hysteresis" in rf
        assert "− 0.6°C hysteresis" in rc


# ---------------------------------------------------------------------------
# select_comfort_band text
# ---------------------------------------------------------------------------


def _band(unit: str, **overrides):
    config = {"temp_unit": unit, "comfort_heat": 71.6, "comfort_cool": 75.2, "setback_heat": 60.0, "setback_cool": 80.0}
    config.update(overrides)
    classification = SimpleNamespace(hvac_mode="cool", day_type="warm")
    return select_comfort_band(
        classification, config, occupancy_mode="home", in_sleep_window=False, aggressive_savings=False
    )


def test_comfort_band_text_fahrenheit_unchanged():
    assert _band("fahrenheit").reason.startswith("comfort band [72/75] (day=warm, active=ceiling")


def test_comfort_band_text_celsius():
    band = _band("celsius")
    assert band.reason.startswith("comfort band [22/24] (day=warm, active=ceiling")
    # The numeric band itself stays internal °F.
    assert (band.floor, band.ceiling) == (71.6, 75.2)


def test_band_tou_expansion_delta_in_user_unit():
    classification = SimpleNamespace(hvac_mode="cool", day_type="warm")
    cfg = {"comfort_heat": 70.0, "comfort_cool": 75.0, "setback_heat": 60.0, "setback_cool": 80.0}
    f = select_comfort_band(
        classification,
        {**cfg, "temp_unit": "fahrenheit"},
        occupancy_mode="away",
        in_sleep_window=False,
        aggressive_savings=False,
        tou_expansion_f=3.6,
    )
    c = select_comfort_band(
        classification,
        {**cfg, "temp_unit": "celsius"},
        occupancy_mode="away",
        in_sleep_window=False,
        aggressive_savings=False,
        tou_expansion_f=3.6,
    )
    assert "tou_expansion=3.6)" in f.reason
    assert "tou_expansion=2.0)" in c.reason
    # floor 60°F = 15.6°C -> 16; ceiling 80 + 3.6 = 83.6°F = 28.7°C -> 29
    assert c.reason.startswith("away band [16/29]")


# ---------------------------------------------------------------------------
# Change 12 (producer side): override_cleared record carries °F
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "attr_value", "expected_f"),
    [("celsius", 21.0, 69.8), ("celsius", 22.0, 71.6), ("fahrenheit", 70.0, 70.0)],
)
def test_override_cleared_setpoint_is_stored_in_fahrenheit(unit, attr_value, expected_f):
    engine, _fake_hass, _sched, event_log = build_headless_engine(
        config={"temp_unit": unit},
        climate_state="heat",
        climate_attributes={"temperature": attr_value},
    )
    engine._manual_override_active = True
    engine._manual_override_mode = "heat"
    engine._clear_manual_override_active("test")

    payloads = [p for (name, p, _ts) in event_log if name == "override_cleared"]
    assert len(payloads) == 1
    assert payloads[0]["old_setpoint_f"] == pytest.approx(expected_f, abs=0.01)


def test_override_cleared_without_setpoint_attribute_is_none():
    engine, _fake_hass, _sched, event_log = build_headless_engine(
        config={"temp_unit": "celsius"}, climate_state="heat", climate_attributes={}
    )
    engine._manual_override_active = True
    engine._clear_manual_override_active("test")
    payload = next(p for (name, p, _ts) in event_log if name == "override_cleared")
    assert payload["old_setpoint_f"] is None
