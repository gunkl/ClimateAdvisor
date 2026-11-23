"""Issue #1665: the dashboard shows a backend code as a word in the viewer's language, never the raw code.

Occupant view: in the 16 non-English languages, the night card's confidence, the Analysis tab's grades, day type,
trend, recommended mode, setback-skipped reason, AI status and circuit, the Activity record "Source" column, the
report source, the chart hover's event codes and the remote fan speed showed an English code inside translated text.

These pins read the JS and the backend, and fail when a spot passes the raw code again, or when a code the backend
emits has no word in a language. The Debug tab stays English (owner decision); engine ids (v3/v4) are product names.
The Node half (``tests/i18n_dashboard.mjs``) renders the words through the real ``CAI18N.word`` in every language.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CA = ROOT / "custom_components" / "climate_advisor"
FE = CA / "frontend"
I18N = FE / "i18n"
LANGS = json.loads((I18N / "index.json").read_text(encoding="utf-8"))["languages"]


def _js(name: str) -> str:
    return (FE / name).read_text(encoding="utf-8")


def _cat(area: str, lang: str) -> dict[str, str]:
    return json.loads((I18N / area / f"{lang}.json").read_text(encoding="utf-8"))


def _py(name: str) -> str:
    return (CA / name).read_text(encoding="utf-8")


ANALYSIS, NIGHT, STATUS, CHART = _js("nd_analysis.js"), _js("nd_night_card.js"), _js("nd_status.js"), _js("chart_v2.js")


# ---- the backend's codes, read from the code that emits them ----
def _setback_reasons() -> set[str]:
    return set(re.findall(r'setback_skipped_reason = "(\w+)"', _py("automation.py")))


def _chart_events() -> set[str]:
    src = _py("coordinator.py")
    return set(re.findall(r'^\s+event="(\w+)",', src, re.M)) | set(
        re.findall(r'_append_occupancy_chart_row\("(\w+)"\)', src)
    )


def _report_sources() -> set[str]:
    return set(re.findall(r'"source": "(\w+)"', _py("ai_skills.py")))


def _activity_sources() -> set[str]:
    src = _py("ai_skills_context.py")
    body = re.search(r"def _event_source_label\(.*?\n(?=def )", src, re.S).group(0)
    found = set(re.findall(r'return "(\w+)"', body)) | set(re.findall(r'source in \("(\w+)", "(\w+)"\)', body)[0])
    assert re.search(r'_event_source_label\(event_type, payload\) or "sensor"', src)  # the row's default
    return found | {"sensor"}


def _grades() -> set[str]:
    return set(re.findall(r'return "(none|low|medium|high)"', _py("thermal_confidence.py")))


def _offered_night_grades() -> set[str]:
    m = re.search(r"^OFFERED_GRADES = \(([^)]*)\)", _py("night_strategy.py"), re.M)
    return set(re.findall(r'"(\w+)"', m.group(1)))


def _remote_speeds() -> set[str]:
    m = re.search(r"^REMOTE_SPEED_TOKENS = frozenset\(\{([^}]*)\}\)", _py("const.py"), re.M)
    return set(re.findall(r'"(\w+)"', m.group(1)))


def _day_types() -> set[str]:
    return set(re.findall(r'^DAY_TYPE_\w+ = "(\w+)"', _py("const.py"), re.M))


def test_backend_code_sets_are_found():
    # a refactor that moves a code must move this pin with it, never empty it silently
    assert _setback_reasons() == {
        "override_confirm_pending",
        "occupancy",
        "manual_override",
        "paused_by_door",
        "no_classification",
    }
    assert _chart_events() >= {
        "classification_change",
        "override",
        "hvac_action_change",
        "occupancy_change",
        "occupancy_setback",
    }
    assert _report_sources() >= {"ai", "fallback", "error"}
    assert _activity_sources() == {"automation", "manual", "system", "sensor"}
    assert _grades() == {"none", "low", "medium", "high"}
    assert _offered_night_grades() == {"medium", "high"}
    assert _remote_speeds() == {"low", "medium", "high"}
    assert _day_types() == {"hot", "warm", "mild", "cool", "cold"}


# (catalog area, key prefix, codes): every code the backend emits has a word in every language
WORD_TABLES = [
    ("dashboard", "dashboard.analysis.level.", lambda: _grades() | {"na"}),
    ("dashboard", "dashboard.analysis.day_type.", _day_types),
    ("dashboard", "dashboard.analysis.trend.", lambda: {"warming", "cooling", "stable"}),
    ("dashboard", "dashboard.analysis.days.skip_reason.", _setback_reasons),
    ("dashboard", "dashboard.analysis.source.", lambda: _activity_sources() | _report_sources() | {"unknown"}),
    ("dashboard", "dashboard.night.level.", _offered_night_grades),
    ("dashboard", "dashboard.chart.event.", _chart_events),
    ("dashboard", "dashboard.status.fan_speed_", _remote_speeds),
    (
        "settings",
        "settings.ai.status.",
        lambda: {"active", "disabled", "circuit_open", "budget_exceeded", "inactive", "error"},
    ),
    ("settings", "settings.ai.breaker.", lambda: {"closed", "open", "half_open"}),
    ("common", "common.hvac_mode_word.", lambda: {"heat", "cool", "off", "auto", "heat_cool", "fan_only", "dry"}),
]


def test_every_backend_code_has_a_word_in_every_language():
    missing = []
    for area, prefix, codes in WORD_TABLES:
        for lang in LANGS:
            cat = _cat(area, lang)
            missing += [f"{lang}:{prefix}{c}" for c in sorted(codes()) if not str(cat.get(prefix + c, "")).strip()]
    assert not missing, missing


def test_trend_codes_are_the_classifiers():
    assert set(re.findall(r'trend_direction = "(\w+)"', _py("classifier.py"))) == {"warming", "cooling", "stable"}


def test_non_english_words_are_translated():
    # A word equal to the English code in most languages means it was never translated (loanwords like "AI" or
    # "sensor" are fine in a few languages, so the bar is "translated in at least 12 of the 16").
    en = _cat("dashboard", "en")
    keys = [
        k
        for k in en
        if k.startswith(
            (
                "dashboard.analysis.level.",
                "dashboard.analysis.day_type.",
                "dashboard.analysis.trend.",
                "dashboard.analysis.days.skip_reason.",
                "dashboard.analysis.source.",
                "dashboard.night.level.",
                "dashboard.chart.event.",
                "dashboard.status.fan_speed_",
            )
        )
    ]
    assert len(keys) == 37  # + setpoint_change chart event (Issue #1694)
    for key in keys:
        same = [lang for lang in LANGS[1:] if _cat("dashboard", lang)[key] == en[key]]
        assert len(same) <= 4, (key, same)


def test_english_words_keep_the_english_text():
    # English's word is the code (spaced out where the old code did), so English text is unchanged by the fix
    en = _cat("dashboard", "en")
    for prefix, codes in (
        ("dashboard.analysis.level.", _grades()),
        ("dashboard.analysis.day_type.", _day_types()),
        ("dashboard.analysis.source.", _activity_sources() | _report_sources()),
        ("dashboard.night.level.", _offered_night_grades()),
        ("dashboard.status.fan_speed_", _remote_speeds()),
    ):
        for c in codes:
            assert en[prefix + c] == c, prefix + c
    for c in _setback_reasons():
        assert en["dashboard.analysis.days.skip_reason." + c] == c.replace("_", " ")
    for c in _chart_events():
        assert en["dashboard.chart.event." + c] == c.replace("_", " ")


# ---- each spot looks the code up (the raw-code forms are gone) ----
SPOTS = [
    # (source, the fixed form, the old raw form)
    (NIGHT, "level: I.word(N + 'level.', o.confidence || 'medium')", "level: o.confidence || 'medium'"),
    (
        ANALYSIS,
        "grade: iso(CAI18N.word('dashboard.analysis.level.', c.grade || 'none'))",
        "grade: iso(c.grade || 'none')",
    ),
    (ANALYSIS, "value: iso(CAI18N.word('dashboard.analysis.level.', c || 'na'))", "value: iso(c || 'n/a')"),
    (
        ANALYSIS,
        "house: iso(CAI18N.word('dashboard.analysis.level.', tm.confidence_k_passive || 'none'))",
        "house: iso(tm.confidence_k_passive",
    ),
    (
        ANALYSIS,
        "hvac: iso(CAI18N.word('dashboard.analysis.level.', tm.confidence || 'none'))",
        "hvac: iso(tm.confidence ||",
    ),
    (
        ANALYSIS,
        "day_type: r.day_type ? CAI18N.word('dashboard.analysis.day_type.', r.day_type) : '?'",
        "day_type: r.day_type || '?'",
    ),
    (
        ANALYSIS,
        "CAI18N.word('dashboard.analysis.day_type.', r.day_type || '—') + ' · ' "
        "+ CAI18N.word('dashboard.analysis.trend.', r.trend_direction || '—')",
        "(r.trend_direction || '—')",
    ),
    (ANALYSIS, "p ? CAI18N.word('dashboard.analysis.day_type.', p.day_type || '—')", "(p.day_type || '—')"),
    (
        ANALYSIS,
        "CAI18N.word('common.hvac_mode_word.', r.hvac_mode_recommended || '—')",
        "return r.hvac_mode_recommended || '—'",
    ),
    (ANALYSIS, "CAI18N.word('common.hvac_mode_word.', p.hvac_mode || '—')", "(p.hvac_mode || '—')"),
    (
        ANALYSIS,
        "reason: CAI18N.word('dashboard.analysis.days.skip_reason.', r.setback_skipped_reason,",
        "reason: String(r.setback_skipped_reason)",
    ),
    (ANALYSIS, "CAI18N.word('settings.ai.status.', a.status)", "status: iso(a.status || 'unknown')"),
    (
        ANALYSIS,
        "circuit: iso(CAI18N.word('settings.ai.breaker.', a.circuit_breaker_state || ''))",
        "circuit: iso(a.circuit_breaker_state",
    ),
    (ANALYSIS, "esc(CAI18N.word('dashboard.analysis.source.', r.source))", "esc(r.source)"),
    (ANALYSIS, "source: iso(CAI18N.word('dashboard.analysis.source.', result.source))", "source: iso(result.source)"),
    (
        ANALYSIS,
        "source: iso(CAI18N.word('dashboard.analysis.source.', res.source || 'error'))",
        "source: iso(res.source || 'error')",
    ),
    (
        ANALYSIS,
        "esc(CAI18N.word('dashboard.analysis.source.', res.source || 'unknown'))",
        "esc(res.source || 'unknown')",
    ),
    (STATUS, "speed: word('fan_speed', d.fan_remote_speed)", "speed: d.fan_remote_speed"),
    (CHART, "EN['event.' + ev] !== undefined ? _t('event.' + ev)", "escapeHtml(String(closest.event)"),
]


def test_each_spot_looks_the_code_up():
    for src, fixed, raw in SPOTS:
        assert fixed in src, fixed
        assert raw not in src, raw


def test_chart_event_words_are_in_the_in_file_english_map():
    # the classic page renders the chart from the EN map (no i18n.js): each event word must be there too
    for c in _chart_events():
        assert f'"event.{c}": "{c.replace("_", " ")}"' in CHART, c


def test_status_word_helper_is_the_one_used_for_fan_speed():
    # nd_status's word(group, code) builds `dashboard.status.<group>_<code>`; an unknown speed shows as sent
    assert "var k = S + group + '_' + String(code" in STATUS


def test_word_helper_is_documented():
    spec = (ROOT / "docs" / "i18n-spec.md").read_text(encoding="utf-8")
    assert "`word(prefix, code, fallback)`" in spec
