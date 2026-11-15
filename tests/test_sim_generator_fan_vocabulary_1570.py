"""Generated days exercise vendor fan vocabularies (Issue #1570).

Occupant view: a regression for a thermostat whose fan modes have their own names (a Honeywell T6 Pro's fan ON is
``Low``, #1556) must not slip past the nightly generated days. Before this, every generated blower press was the
literal ``"on"`` and every generated thermostat advertised ``["auto", "on"]``.
"""

from __future__ import annotations

from tools.sim_harness.generator import (
    FAN_VOCABULARIES,
    build_plan,
    day_seed_of,
    run_plan,
    strip_fan_vocabulary,
)

T6 = ["Auto low", "Low", "Circulation"]


def _plans(n: int = 600):
    return [build_plan(day_seed_of(21, i), "coordinator") for i in range(n)]


def _vocab_blower_plan(modes: list[str]):
    for p in _plans(1500):
        if p.config.get("thermostat_fan_modes") == modes and p.whf and p.whf["order"] == "blower_press_hvac_fan":
            return p
    raise AssertionError(f"no generated blower-press day with {modes}")


def test_vendor_vocabularies_are_drawn_on_hvac_fan_days_only():
    plans = _plans()
    with_vocab = [p for p in plans if "thermostat_fan_modes" in p.config]
    assert with_vocab, "no generated day uses a vendor fan vocabulary"
    assert all(p.config["fan_mode"] == "hvac_fan" for p in with_vocab)
    assert {tuple(p.config["thermostat_fan_modes"]) for p in with_vocab} == set(FAN_VOCABULARIES)
    hvac_fan_days = [p for p in plans if p.config["fan_mode"] == "hvac_fan"]
    assert 0.15 < len(with_vocab) / len(hvac_fan_days) < 0.45


def test_blower_presses_use_the_thermostats_own_values():
    p = _vocab_blower_plan(T6)
    presses = [args["fan_mode"] for _m, kind, args in p.timed if kind == "blower"]
    assert presses == ["Auto low", "Low", "Auto low"]


def test_strip_restores_the_default_vocabulary_plan():
    p = _vocab_blower_plan(T6).to_dict()
    stripped = strip_fan_vocabulary(p)
    assert "thermostat_fan_modes" not in stripped["config"]
    assert [e[2]["fan_mode"] for e in stripped["timed"] if e[1] == "blower"] == ["auto", "on", "auto"]
    assert [e[2]["fan_mode"] for e in stripped["whf"]["events"] if e[1] == "blower"] == ["auto", "on", "auto"]


def _presses(rec):
    return [e for e in rec.scenario["events"] if e["type"] == "thermostat_state_changed" and "thermostat_fan_mode" in e]


def test_a_t6_blower_press_is_a_tracked_fan_override_through_the_real_engine():
    """The occupant presses 'Low' (fan ON on a T6 Pro): production must see it as the blower turned on."""
    rec = run_plan(_vocab_blower_plan(T6))
    assert rec.callback_errors == 0
    overrides = [e for e in rec.event_log if e[0] == "fan_manual_override"]
    assert overrides and overrides[0][1]["fan_device"] == "hvac_fan"


def test_the_toy_thermostat_reports_its_blower_while_low_is_pressed():
    """Day 21000354: the equipment cycles while 'Low' is pressed; between runs the toy reports "fan", never "idle"."""
    plan = build_plan(day_seed_of(21, 354), "coordinator")
    assert plan.config.get("thermostat_fan_modes") == T6 and plan.whf["order"] == "blower_press_hvac_fan"
    rec = run_plan(plan, probe=False)
    presses = _presses(rec)
    on_at = next(e["time"] for e in presses if e["thermostat_fan_mode"] == "Low")
    off_at = next(e["time"] for e in presses if e["time"] > on_at and e["thermostat_fan_mode"] == "Auto low")
    actions = [
        e["hvac_action"]
        for e in rec.scenario["events"]
        if e["type"] == "thermostat_state_changed" and on_at <= e["time"] < off_at and "hvac_action" in e
    ]
    assert "fan" in actions and not {"idle", "off"} & set(actions), actions
