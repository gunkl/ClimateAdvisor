"""Text that sends the occupant to a setting, menu, tab or button names it as it is shown (Issue #1660).

Occupant view: when an AI report was cut short, the dashboard told the person to raise "Investigator Max
Response Length". No setting had that name any more (Issue #563 removed it; the investigator uses
"Maximum Response Length" in Configure → AI settings), so the person could not find what to change. The
same drift hid in other languages and other messages: a menu translated one way in the options flow and
another way in the message that points to it, a "Send briefing" said to be on the Debug tab when it is in
Actions, an "AI tab" that no dashboard has.

Each entry below is a message that names a label, with the labels it names. The test reads every label
from its own catalog, in every language, and requires the message to contain it (case-insensitive, so a
label may start a sentence or sit mid-sentence). A label written inflected or reworded fails: quote it as
shown instead. A second test keeps the list complete: an English message that looks like it points
somewhere (Configure, Settings →, a tab, a menu label) must be listed here, or listed as reviewed with the
reason it names nothing of Climate Advisor's own.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "climate_advisor"
LANGS = json.loads((PKG / "frontend" / "i18n" / "index.json").read_text(encoding="utf-8"))["languages"]

# Label sources. "ha:<dotted path>" is translations/<lang>.json; "fe:<key>" is frontend/i18n/<area>/<lang>.json.
# A trailing "|head" keeps the part before the first colon (a control label such as "Past v4 lines: predicted").
MENU_AI = "ha:options.step.init.menu_options.ai_settings"
MENU_CORE = "ha:options.step.init.menu_options.core"
MENU_SENSORS = "ha:options.step.init.menu_options.sensors"
MENU_GITHUB = "ha:options.step.init.menu_options.github_settings"
FIELD_MAX_TOKENS = "ha:options.step.ai_settings.data.ai_max_tokens"
FIELD_FAN = "ha:options.step.sensors.data.fan_entity"
PROVIDER_AI_TASK = "ha:selector.ai_provider.options.ha_ai_task"
TAB_SETTINGS = "fe:dashboard.core.tab_settings"
TAB_ANALYSIS = "fe:dashboard.core.tab_analysis"
TAB_DEBUG = "fe:dashboard.core.tab_debug"
ACTIONS = "fe:dashboard.core.actions"
SEND_BRIEFING = "fe:dashboard.core.menu_send_briefing"
ENGINE = "fe:settings.primary_engine.label"

# message -> labels it names
REFERENCES: dict[str, list[str]] = {
    # Options flow (HA) labels named from Climate Advisor text
    "fe:dashboard.analysis.rep.truncated_status": [FIELD_MAX_TOKENS, MENU_AI],
    "fe:dashboard.analysis.rep.error_not_enabled": [MENU_AI],
    "fe:dashboard.core.gh_not_configured": [MENU_GITHUB],
    "fe:settings.github.add_token": [MENU_GITHUB],
    "fe:ai.notice.ai_task_no_entity": [MENU_AI],
    "fe:ai.limits.builtin_place": [MENU_AI],
    "ha:common.ai_limit_builtin_output": [FIELD_MAX_TOKENS],
    "ha:issues.ai_settings_unified.description": [MENU_AI],
    "ha:issues.builtin_claude_deprecated.description": [MENU_AI, PROVIDER_AI_TASK],
    "ha:config.step.unit.description": [MENU_CORE],
    "ha:config.step.unit.data_description.temp_unit": [MENU_CORE],
    "ha:config.error.whf_command_only_warning": [MENU_SENSORS],
    "ha:options.error.whf_command_only_warning": [MENU_SENSORS],
    "ha:options.step.sensors.data_description.fan_state_entity": [FIELD_FAN],
    "fe:settings.fan_state_entity.description": ["fe:settings.fan_entity.label"],
    # Dashboard tabs, Settings groups and settings, buttons, chart controls
    "fe:dashboard.chart.cap.door_sensors": [TAB_SETTINGS, "fe:settings.group.sensors.title"],
    "fe:dashboard.chart.cap.humidity_indoor": [TAB_SETTINGS, "fe:settings.group.sensors.title"],
    "fe:dashboard.chart.cap.humidity_outdoor": [TAB_SETTINGS, "fe:settings.group.sensors.title"],
    "fe:dashboard.chart.cap.outdoor_sensor": [TAB_SETTINGS, "fe:settings.group.sensors.title"],
    "fe:dashboard.chart.cap.fan": [TAB_SETTINGS, "fe:settings.group.equipment.title"],
    "fe:dashboard.chart.cap.occupancy": [TAB_SETTINGS, "fe:settings.group.occupancy.title"],
    "fe:dashboard.core.report_ai_disabled": [TAB_SETTINGS, "fe:settings.group.ai.title"],
    "fe:dashboard.chart.cap.v4_running": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.chart.engines.settings_link": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.chart.series.why_v4_off": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.core.analysis_pa_sub": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.pa.gate.footer": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.pa.gate.sub": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.pa.nov4.not_running": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.pa.tile.v3_why_off": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.status.night_v4_off": [TAB_SETTINGS, ENGINE],
    "fe:dashboard.analysis.days.cost_set_rate": [TAB_SETTINGS],
    "fe:dashboard.chart.role.aria.other": [TAB_SETTINGS],
    "fe:dashboard.chart.role.word.other": [TAB_SETTINGS],
    "fe:dashboard.chart.empty.v4_other_leads": ["fe:dashboard.chart.lead.label|head"],
    "fe:dashboard.analysis.days.tip.hvac_mode": ["fe:dashboard.analysis.days.row.hvac_runtime"],
    "fe:status.auto.override_stuck": [TAB_DEBUG],
    "fe:briefing.suggestions": [TAB_ANALYSIS],
    # Learning advice (#1652) sends the occupant to the Settings tab
    "fe:learning.suggestion.frequent_overrides": [TAB_SETTINGS],
    "fe:learning.suggestion.frequent_overrides_no_detail": [TAB_SETTINGS],
    "fe:learning.suggestion.high_runtime_mild_days": [TAB_SETTINGS],
    "fe:learning.suggestion.short_departures": [TAB_SETTINGS],
    # Issue #1695: "Turn on Humid Overcool" names the setting and the tab it is on
    "fe:learning.suggestion.humid_overcool": [TAB_SETTINGS, "fe:settings.humid_overcool_enabled.label"],
    "fe:settings.energy_rate.description": [TAB_ANALYSIS],
    "fe:settings.briefing_notifications_enabled.description": [SEND_BRIEFING, ACTIONS],
    "ha:options.step.notifications.data_description.briefing_notifications_enabled": [SEND_BRIEFING, ACTIONS],
}

# Setting names a message puts in quotes: the quoted text must be exactly the label (no "Max." shortening,
# no extra words). Issue #1660's message is the one that was wrong.
QUOTED: dict[str, str] = {
    "fe:dashboard.analysis.rep.truncated_status": FIELD_MAX_TOKENS,
}

# Dashboard copies of an options-flow label (the dashboard cannot read translations/<lang>.json): equal in
# every language. Built-in Claude's limit warnings name its setting with these (Issue #1660 mirror).
COPIES: dict[str, str] = {
    "fe:ai.limits.builtin_setting": FIELD_MAX_TOKENS,
}

# English messages that match the "points somewhere" pattern but name nothing of Climate Advisor's own.
REVIEWED: dict[str, str] = {
    # labels and titles themselves
    "fe:dashboard.core.actions": "label",
    "fe:settings.ai_max_tokens.label": "label",
    "fe:settings.fan_entity.label": "label",
    "ha:config.step.sensors.data.fan_entity": "label",
    "ha:options.step.sensors.data.fan_entity": "label",
    "ha:options.step.ai_settings.data.ai_max_tokens": "label",
    "ha:options.step.init.menu_options.core": "label",
    "ha:options.step.init.menu_options.sensors": "label",
    "ha:options.step.init.menu_options.ai_settings": "label",
    "ha:options.step.init.menu_options.github_settings": "label",
    "ha:options.step.core.title": "label",
    "ha:options.step.sensors.title": "label",
    "ha:options.step.github_settings.title": "label",
    "ha:issues.ai_settings_unified.title": "headline, names no place",
    # "Configure ..." as a verb in a step's own description
    "ha:config.step.user.description": "verb",
    "ha:config.step.occupancy.description": "verb",
    "ha:options.step.core.description": "verb",
    "ha:options.step.setpoints.description": "verb",
    "ha:options.step.sensors.description": "verb",
    "ha:options.step.occupancy.description": "verb",
    "ha:options.step.advanced.description": "verb",
    "ha:options.step.github_settings.description": "verb",
    # Home Assistant's own pages (Settings > Devices & services, Settings > System > AI) or another integration's
    "fe:notify.entity_health_multi": "Home Assistant page",
    "fe:notify.entity_health_single": "Home Assistant page",
    "fe:settings.ai_task_entity.description": "Home Assistant page",
    "ha:config.step.schedule.data_description.zone_name": "Home Assistant page",
    "ha:exceptions.unknown_zone.message": "Home Assistant page",
    "ha:options.step.ai_limits_warning.description": "the AI integration's own options",
    "ha:issues.zone_resolution_ambiguous.description": "a browser tab",
}

_POINTS_SOMEWHERE = re.compile(
    r"Configure|Settings ?[→>,]|\bunder Settings\b|in Settings\b|\b[Tt]ab\b|AI settings|Core Settings|Sensors & Fan"
    r"|Fan Entity|GitHub [Ii]ntegration|Response Length|\bunder \"|Actions"
)


@functools.cache
def _catalog(kind: str, area: str, lang: str) -> dict:
    path = PKG / "translations" / f"{lang}.json" if kind == "ha" else PKG / "frontend" / "i18n" / area / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _get(ref: str, lang: str) -> str:
    ref, _, transform = ref.partition("|")
    kind, key = ref.split(":", 1)
    if kind == "ha":
        value = _catalog("ha", "", lang)
        for part in key.split("."):
            value = value[part]
    else:
        value = _catalog("fe", key.split(".")[0], lang)[key]
    assert isinstance(value, str), ref
    if transform == "head":
        value = re.split(r"[:：]", value, maxsplit=1)[0]
    return value.strip()


def _flat(d: dict, prefix: str = ""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


_QUOTES = re.compile(r'"([^"]+)"|“([^”]+)”|„([^“”]+)[“”]|«\s*([^»]+?)\s*»|「([^」]+)」|‘([^’]+)’|\'([^\']+)\'')


def _quoted(text: str) -> list[str]:
    return [next(g for g in m.groups() if g) for m in _QUOTES.finditer(text)]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("message", sorted(REFERENCES))
def test_message_names_each_label_as_shown(message: str, lang: str) -> None:
    text = _get(message, lang).casefold()
    missing = [_get(ref, lang) for ref in REFERENCES[message] if _get(ref, lang).casefold() not in text]
    assert not missing, f"[{lang}] {message} does not name {missing} as shown: {_get(message, lang)!r}"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("message", sorted(QUOTED))
def test_quoted_setting_name_is_exactly_the_label(message: str, lang: str) -> None:
    label = _get(QUOTED[message], lang)
    quoted = _quoted(_get(message, lang))
    assert label in quoted, f"[{lang}] {message} quotes {quoted}, not the setting's label {label!r}"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("copy", sorted(COPIES))
def test_dashboard_copy_of_a_label_equals_it(copy: str, lang: str) -> None:
    assert _get(copy, lang) == _get(COPIES[copy], lang), f"[{lang}] {copy} drifted from {COPIES[copy]}"


def test_truncated_report_names_maximum_response_length_in_english() -> None:
    """The Issue #1660 sentence, read as the occupant reads it."""
    text = _get("fe:dashboard.analysis.rep.truncated_status", "en")
    assert '"Maximum Response Length"' in text and "AI settings" in text
    assert "Investigator" not in text


def test_every_pointing_english_message_is_listed() -> None:
    found = set()
    for area_dir in sorted((PKG / "frontend" / "i18n").iterdir()):
        if area_dir.is_dir():
            for key, value in json.loads((area_dir / "en.json").read_text(encoding="utf-8")).items():
                if _POINTS_SOMEWHERE.search(value):
                    found.add(f"fe:{key}")
    for key, value in _flat(json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))):
        if isinstance(value, str) and _POINTS_SOMEWHERE.search(value):
            found.add(f"ha:{key}")
    unlisted = sorted(found - set(REFERENCES) - set(REVIEWED) - set(COPIES))
    assert not unlisted, (
        "These English messages look like they send the occupant to a setting, menu, tab or button. Add each to "
        f"REFERENCES with the labels it names (or to REVIEWED with why it names none of ours): {unlisted}"
    )
    stale = sorted(
        (set(REFERENCES) | set(REVIEWED) | set(COPIES)) - found - {"fe:dashboard.analysis.days.tip.hvac_mode"}
    )
    assert not stale, f"Listed but no longer matching the pattern (re-check or remove): {stale}"


# The retired investigator setting (Issue #563). Nothing an occupant reads, and no log line, may name it.
_RETIRED = ("Investigator Max Response Length", "Investigator Max Response")


def test_no_text_names_the_retired_investigator_setting() -> None:
    hits = []
    for path in PKG.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".js", ".html", ".json"} and path.name != "fix_history.jsonl":
            text = path.read_text(encoding="utf-8", errors="ignore")
            hits += [f"{path.relative_to(ROOT)}: {name}" for name in _RETIRED if name in text]
    assert not hits, hits


def test_builtin_claude_limit_is_named_by_its_label_on_both_dashboards() -> None:
    """Mirror of Issue #1660: with built-in Claude (default Maximum Response Length 4096, below the 8192 a report
    needs) the pre-run warning and the cut-short notice said "max_tokens in climate_advisor"; they name the setting
    and the menu as the options flow shows them."""
    nd = (PKG / "frontend" / "nd_analysis.js").read_text(encoding="utf-8")
    assert "A('limits.builtin_setting')" in nd and "A('limits.builtin_place')" in nd
    assert "'climate_advisor'" not in nd
    classic = (PKG / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "_aiBuiltin(l) ? 'Maximum Response Length'" in classic
    assert "builtin_claude' ? 'climate_advisor'" not in classic


def _options_labels(lang: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for step in _catalog("ha", "", lang)["options"]["step"].values():
        labels.update(step.get("data") or {})
    return labels


@pytest.mark.parametrize("lang", LANGS)
def test_dashboard_settings_labels_are_the_options_flow_labels(lang: str) -> None:
    """Issue #1663: the dashboard Settings tab names each setting exactly as Configure does, so a person who reads
    it there finds the same name in the options flow. Units ride in the value (`time_unit`, temperature `unit`) or
    the description, never in only one of the two labels."""
    settings = _catalog("fe", "settings", lang)
    options = _options_labels(lang)
    keys = [m.group(1) for k in settings if (m := re.fullmatch(r"settings\.([a-z_0-9]+)\.label", k))]
    drift = {
        k: (settings[f"settings.{k}.label"], options[k])
        for k in keys
        if k in options and settings[f"settings.{k}.label"] != options[k]
    }
    assert not drift, f"[{lang}] dashboard label != options label: {drift}"


def test_every_dashboard_setting_has_an_options_flow_label() -> None:
    from custom_components.climate_advisor.const import CONFIG_METADATA

    options = _options_labels("en")
    assert not sorted(k for k in CONFIG_METADATA if k not in options), "a Settings-tab row with no options-flow label"
