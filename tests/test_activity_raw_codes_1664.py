"""Issue #1664 (the Activity Log mirror of #1659): no Activity Log row puts a raw code into a translated sentence.

What the occupant saw: on the Activity Log in any language but English, rows read e.g. "Thermostat auf cool gesetzt"
or "分類已套用：hot" -- the HVAC mode, hvac_action, day type, trend, occupancy, override/grace source, write outcome,
band edge/context or comfort family went into the sentence as the English code.

How this test works:
* every registered Activity Log renderer (``EVENT_RENDERERS[...].msg``, the production builder the dashboard and the
  AI input use) is fed payloads whose code fields hold codes; each row is rendered in zh-Hant and German and must not
  contain any of those codes, and the English rendering must equal the English with every word replaced by its code
  (so the stored English record is byte-identical);
* the automation ``_R(...)`` reasons are driven through ``select_comfort_band`` and ``decide_comfort_family``, and a
  source check pins that every other listed ``_R`` reason wraps its code argument.
Without the fix the rendered rows carry the raw codes and the coverage / source checks fail.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from custom_components.climate_advisor import ai_skills_context as ctx
from custom_components.climate_advisor import i18n

_PKG = Path(i18n.__file__).parent

# Codes injected into payload fields, and whose word in zh-Hant / de differs from the code.
MODE, MODE2, ACTION, DAY, TREND, OCC, SOURCE, OUTCOME, FAMILY = (
    "heat_cool",
    "fan_only",
    "cooling",
    "hot",
    "warming",
    "vacation",
    "manual",
    "superseded",
    "heating",
)
CODES = (MODE, MODE2, ACTION, DAY, TREND, OCC, SOURCE, OUTCOME, FAMILY, "ode_guard")


def _payloads() -> list[dict]:
    base = {
        "mode": MODE,
        "old_mode": MODE2,
        "old_hvac_mode": MODE2,
        "new_hvac_mode": MODE,
        "hvac_mode": MODE,
        "hvac_action": ACTION,
        "thermostat_mode": MODE,
        "thermostat_action": ACTION,
        "to_mode": MODE,
        "from_mode": MODE2,
        "cls_mode": MODE2,
        "detected_mode": MODE2,
        "current_mode": MODE,
        "was_mode": MODE2,
        "requested": MODE,
        "requested_mode": MODE,
        "actual": MODE2,
        "actual_mode": MODE2,
        "restore_mode": MODE,
        "prior_mode": MODE2,
        "attempted_mode": MODE,
        "stale_mode": MODE2,
        "last_mode": MODE,
        "day_type": DAY,
        "trend": TREND,
        "trend_direction": TREND,
        "occupancy": OCC,
        "source": SOURCE,
        "outcome": OUTCOME,
        "candidate": FAMILY,
        "resolved_family": FAMILY,
        "old_setpoint_f": 70.0,
        "new_setpoint_f": 72.0,
        "breach_time": "14:00",
        "lead_time_min": 30,
        "duration_seconds": 600,
        "pre_expiry": True,
        "nudge_f": 71.0,
        "target_f": 72.0,
        "preconditioned": True,
        "candidate_family": FAMILY,
        "source_code": "blower_during_cycle",
    }
    variants = [base]
    variants.append({**base, "mode": "", "has_mode": False})
    variants.append({**base, "re_paused": True, "pre_expiry": False})
    variants.append({**base, "held_reason": None, "mode": MODE, "from_mode": None})
    variants.append({**base, "class": "contradiction", "incident_class": "contradiction"})
    variants.append({k: v for k, v in base.items() if k != "outcome"})
    variants.append({**base, "reason": "occupancy"})
    variants.append({**base, "source_code": "fan_schedule"})
    return variants


def _walk(cell, seen: set[str]) -> None:
    if isinstance(cell, i18n.Msg):
        seen.add(cell.key)
        for v in cell.params.values():
            _walk(v, seen)


def _unword(cell):
    """The same message with every word param put back as its code (the pre-#1664 English)."""
    if isinstance(cell, i18n.Code):
        k = cell.key
        if k.startswith(("common.hvac_action.", "common.hvac_mode_word.", "activity.word.")):
            return k.rsplit(".", 1)[1]
        return cell
    if isinstance(cell, i18n.Msg):
        return i18n.Msg(cell.key, **{n: _unword(v) for n, v in cell.params.items()})
    return cell


def _has_word(cell) -> bool:
    if isinstance(cell, i18n.Code):
        return cell.key.startswith(("common.hvac_action.", "common.hvac_mode_word.", "activity.word."))
    if isinstance(cell, i18n.Msg):
        return any(_has_word(v) for v in cell.params.values())
    return False


# The ``source`` field means different things per event: a mode-set reason code for ``thermostat_mode_set``; for
# ``fan_running_untracked`` the English record next to the ``source_code`` the row is rendered from.
_SOURCE_BY_EVENT = {
    "thermostat_mode_set": "ode_guard",
    "fan_running_untracked": "thermostat blower during heat_cool cycle",
}


def _rendered_rows():
    rows = []
    for etype, renderer in sorted(ctx.EVENT_RENDERERS.items()):
        fn = getattr(renderer, "msg", None)
        if fn is None:
            continue
        for p in _payloads():
            try:
                payload = dict(p)
                if etype in _SOURCE_BY_EVENT:
                    payload["source"] = _SOURCE_BY_EVENT[etype]
                cells = fn(payload, "fahrenheit")
            except Exception:  # noqa: BLE001 - a payload shape this renderer does not take
                continue
            for cell in cells:
                if isinstance(cell, i18n.Msg):
                    rows.append((etype, cell))
    return rows


ROWS = _rendered_rows()
_CODE_RE = re.compile(r"(?<![A-Za-z_])(" + "|".join(map(re.escape, CODES)) + r")(?![A-Za-z_])")

# The activity messages whose code argument this fix turns into a word (each must be reached by the sweep).
EXPECTED = {
    "activity.comfort_band_applied_mode",
    "activity.bedtime_setback_mode",
    "activity.tou_window_coincided",
    "activity.morning_wakeup_mode",
    "activity.occupancy_setback_occ",
    "activity.occupancy_restored_mode",
    "activity.thermostat_unchanged_mode",
    "activity.override_detected_source",
    "activity.classification_applied_day",
    "activity.label_trend",
    "activity.hvac_mode_requested_reports",
    "activity.hvac_mode_now",
    "activity.override_cleared_was",
    "activity.override_confirmed_mode",
    "activity.override_confirmed_cls",
    "activity.override_self_resolved_modes",
    "activity.override_adopted_mode",
    "activity.label_source",
    "activity.grace_expired_source",
    "activity.fact_hvac_to_mode",
    "activity.fact_fan_on_external_action",
    "activity.bedtime_setback_skipped_occ",
    "activity.morning_wakeup_skipped_occ",
    "activity.mode_action_mismatch",
    "activity.occupancy_setback_suppressed_paused",
    "activity.switch_delayed_candidate",
    "activity.switched_to_family",
    "activity.thermostat_set_to",
    "activity.label_was",
    "activity.fact_requested_by",
    "activity.fact_not_applied_outcome",
    "activity.switch_held_candidate",
    "activity.fact_mode_change",
    "activity.fact_trigger",
    "activity.fan_source_blower_during_cycle",
    "activity.fan_source_schedule",
}


def test_the_sweep_reaches_every_fixed_message():
    seen: set[str] = set()
    for _, cell in ROWS:
        _walk(cell, seen)
    missing = sorted(EXPECTED - seen)
    assert not missing, f"not reached by any renderer payload: {missing}"


@pytest.mark.parametrize("lang", ["zh-Hant", "de", "ja", "fr"])
def test_no_raw_code_in_any_translated_row(lang):
    bad = []
    for etype, cell in ROWS:
        text = i18n.render(cell, lang)
        for m in _CODE_RE.finditer(text):
            word = m.group(1)
            # A language whose word happens to equal the code (none of the injected ones today) is not a raw code.
            if lang != "en" and word in ("normal",):
                continue
            bad.append((etype, word, text))
    assert not bad, bad[:20]


def test_rows_with_words_exist_so_the_sweep_is_not_vacuous():
    assert sum(1 for _, c in ROWS if _has_word(c)) > 60


def test_english_is_byte_identical_to_the_code_text():
    for etype, cell in ROWS:
        assert i18n.render_en(cell) == i18n.render_en(_unword(cell)), etype


def test_select_comfort_band_reason_words():
    from custom_components.climate_advisor.automation import select_comfort_band
    from tests.test_api import _make_classification

    c = _make_classification(day_type="hot", hvac_mode="cool")
    band = select_comfort_band(
        c,
        {"comfort_heat": 68, "comfort_cool": 75, "setback_heat": 60, "setback_cool": 80, "temp_unit": "fahrenheit"},
        occupancy_mode="vacation",
        in_sleep_window=False,
        aggressive_savings=False,
    )
    msg = i18n.msg_of(band.reason)
    assert msg is not None and msg.key == "activity.reason_band_select"
    en = i18n.render_en(msg)
    assert en.startswith("vacation band [") and "day=hot" in en and "active=ceiling" in en
    zh = i18n.render(msg, "zh-Hant")
    assert not re.search(r"vacation|(?<![a-z])hot(?![a-z])|ceiling", zh), zh
    assert i18n.render(i18n.Code("activity.word.day.hot"), "zh-Hant") in zh


def test_settling_guard_reason_family_word():
    from custom_components.climate_advisor.comfort_family_decision import decide_comfort_family
    from tests.test_comfort_settling_guard import _leaf

    d = decide_comfort_family(_leaf(indoor=67.0, current="cooling", ran=30.0))
    msg = i18n.msg_of(d.reason)
    assert msg is not None and msg.key == "activity.reason_cf_settling_guard"
    assert i18n.render_en(msg).startswith("settling guard: cooling ran 30 min ago")
    de = i18n.render(msg, "de")
    assert "cooling" not in de and i18n.render(i18n.Code("common.hvac_action.cooling"), "de") in de


# (key, param, helper) for every automation.py ``_R`` reason this fix wraps.
_AUTOMATION_WRAPPED = [
    ("reason_band_select", "ctx", "band_w"),
    ("reason_band_select", "day_type", "day_w"),
    ("reason_band_select", "active", "edge_w"),
    ("reason_manual_override_ending_free_cooling", "mode", "mode_w"),
    ("reason_daily_classification", "day_type", "day_w"),
    ("reason_daily_classification", "trend", "trend_w"),
    ("reason_band_edge_used", "edge", "edge_w"),
    ("action_set_hvac", "mode", "mode_w"),
    ("action_set_temp", "mode", "mode_w"),
    ("reason_pause_was_mode", "mode", "mode_w"),
    ("reason_door_closed_restoring_mode", "mode", "mode_w"),
    ("reason_nv_exit_window_open_was", "mode", "mode_w"),
    ("reason_resend", "live", "mode_w"),
    ("reason_resend_restore", "live", "mode_w"),
    ("reason_occupancy_setback_band", "mode", "occupancy_w"),
    ("reason_occupancy_home_restore", "family", "family_w"),
    ("reason_blower_window_open", "mode", "mode_w"),
    ("reason_whf_already_stopped_restoring", "mode", "mode_w"),
    ("reason_stranded_hvac_restored", "mode", "mode_w"),
    ("reason_whf_stopped_restoring", "mode", "mode_w"),
]


def test_every_listed_automation_reason_wraps_its_code():
    tree = ast.parse((_PKG / "automation.py").read_text(encoding="utf-8"))
    found: dict[tuple[str, str], list[str]] = {}
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_R" and n.args):
            continue
        a0 = n.args[0]
        if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
            continue
        for kw in n.keywords:
            v = kw.value
            fn = getattr(v.func, "id", "") if isinstance(v, ast.Call) else ""
            found.setdefault((a0.value, kw.arg), []).append(fn)
    for key, param, helper in _AUTOMATION_WRAPPED:
        calls = found.get((key, param))
        assert calls, (key, param)
        assert all(fn == helper for fn in calls), (key, param, calls)


def test_word_helpers_pass_through_anything_unlisted():
    from custom_components.climate_advisor import code_words as cw

    for fn in (cw.mode_w, cw.action_w, cw.day_w, cw.trend_w, cw.occupancy_w, cw.source_w, cw.outcome_w, cw.edge_w):
        assert fn("vendor_thing") == "vendor_thing"
        assert fn("") == "" and fn(None) is None
        msg = i18n.Msg("activity.held_default")
        assert fn(msg) is msg
    assert cw.family_w("heating") == i18n.Code("common.hvac_action.heating")
    assert cw.family_w("heat") == i18n.Code("common.hvac_mode_word.heat")
    assert cw.requested_by_w("ode_guard") == i18n.Code("activity.word.requested_by.ode_guard")


def test_every_word_key_exists_in_every_language():
    import json

    base = _PKG / "frontend" / "i18n"
    langs = json.loads((base / "index.json").read_text(encoding="utf-8"))["languages"]
    from custom_components.climate_advisor import code_words as cw

    keys = set()
    for name in dir(cw):
        table = getattr(cw, name)
        if name.startswith("_") and not name.startswith("__") and isinstance(table, dict):
            keys.update(table.values())
    assert len(keys) == 37
    for lang in langs:
        cat = json.loads((base / "activity" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert cat.get(k), (lang, k)
        if lang == "en":
            for k in keys:
                assert cat[k] == k.rsplit(".", 1)[1]


class TestFanRunningUntrackedSource:
    """The external-fan row's source was stored English prose; it is now a code next to the English record."""

    R = staticmethod(lambda p: ctx.EVENT_RENDERERS["fan_running_untracked"].msg(p, "fahrenheit")[0])

    @pytest.mark.parametrize("mode", ["cool", "heat", "heat_cool"])
    def test_blower_during_cycle(self, mode):
        p = {"source": f"thermostat blower during {mode} cycle", "source_code": "blower_during_cycle"}
        msg = self.R({**p, "thermostat_mode": mode, "hvac_action": "fan"})
        assert i18n.render_en(msg) == f"Fan running (external) -- thermostat blower during {mode} cycle"
        for lang in ("de", "zh-Hant", "ja", "fr"):
            text = i18n.render(msg, lang)
            assert "thermostat blower" not in text and not re.search(rf"(?<![a-z_]){mode}(?![a-z_])", text), text
            assert i18n.render(i18n.Code(f"common.hvac_mode_word.{mode}"), lang) in text

    def test_fan_schedule(self):
        msg = self.R(
            {"source": "thermostat fan schedule/circulation", "source_code": "fan_schedule", "thermostat_mode": "off"}
        )
        assert i18n.render_en(msg) == "Fan running (external) -- thermostat fan schedule/circulation"
        assert "schedule" not in i18n.render(msg, "de")

    def test_an_older_row_with_only_prose_shows_it_as_written(self):
        msg = self.R({"source": "thermostat blower during cool cycle", "thermostat_mode": "cool"})
        assert i18n.render(msg, "de").endswith("-- thermostat blower during cool cycle")

    def test_the_coordinator_stores_the_code_with_the_english_record(self):
        src = (_PKG / "coordinator.py").read_text(encoding="utf-8")
        i = src.index('"fan_running_untracked",')
        payload = src[i : i + 400]
        assert '"source": _source,' in payload and '"source_code": _source_code,' in payload
