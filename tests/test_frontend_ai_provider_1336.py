"""Issue #1336 (FE lane): the dashboard's AI-provider limit check, notices and neutral labels.

Occupant view: before an investigation runs, the occupant is warned (Run anyway / Cancel) when the chosen AI
provider's limits are too small; a report that fails at a provider limit or comes back cut short says which
setting to raise, in the occupant's language. These tests read the frontend sources and catalogs directly (no JS
harness in the unit tier) and pin the invariants that would let a regression slip through silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.climate_advisor import const

_FE = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend"
_LANGS = json.loads((_FE / "i18n" / "index.json").read_text(encoding="utf-8"))["languages"]
_ND = (_FE / "nd_analysis.js").read_text(encoding="utf-8")
_INDEX = (_FE / "index.html").read_text(encoding="utf-8")

_AI_CODES = [
    const.AI_ERROR_OUTPUT_LIMIT,
    const.AI_ERROR_CONTEXT_LIMIT,
    const.AI_ERROR_NO_ENTITY,
    const.AI_ERROR_PROVIDER,
    const.AI_CUT_SHORT,
]
_NEW_AI_KEYS = (
    ["ai.status_gathering", "ai.status_sending", "ai.status_provider"]
    + [f"ai.notice.{c}" for c in _AI_CODES]
    + [
        "ai.limits.checking",
        "ai.limits.title",
        "ai.limits.check",
        "ai.limits.advice",
        "ai.limits.unreadable",
        "ai.limits.run_anyway",
        "ai.limits.cancel",
        "ai.limits.cancelled",
    ]
)
_NEW_SETTINGS_KEYS = [
    "settings.ai_provider.label",
    "settings.ai_provider.description",
    "settings.ai_task_entity.label",
    "settings.ai_task_entity.description",
]
_CLAUDE_ONLY = [
    "settings.ai_api_key.description",
    "settings.ai_model.description",
    "settings.ai_reasoning_effort.description",
]


def _cat(area: str, lang: str) -> dict[str, str]:
    return json.loads((_FE / "i18n" / area / f"{lang}.json").read_text(encoding="utf-8"))


def _args(msg: str) -> set[str]:
    # ICU argument names: `{name}` and `{name, select|plural|number ...` (select option bodies are not names)
    return set(re.findall(r"\{\s*([a-z_]+)\s*[,}]", msg))


@pytest.mark.parametrize("lang", _LANGS)
def test_every_new_key_exists_in_every_language_with_the_english_arguments(lang):
    assert len(_LANGS) == 16
    for area, keys in (("ai", _NEW_AI_KEYS), ("settings", _NEW_SETTINGS_KEYS)):
        en, cat = _cat(area, "en"), _cat(area, lang)
        for key in keys:
            assert cat.get(key), (lang, key)
            assert _args(cat[key]) == _args(en[key]), (lang, key)


@pytest.mark.parametrize("lang", [x for x in _LANGS if x != "en"])
def test_translations_are_not_english_copies(lang):
    en, cat = _cat("ai", "en"), _cat("ai", lang)
    copies = [k for k in _NEW_AI_KEYS if cat[k] == en[k]]
    assert not copies, (lang, copies)


def test_every_error_code_has_a_notice_and_the_status_keys_match_the_contract():
    en = _cat("ai", "en")
    for code in _AI_CODES:
        assert f"ai.notice.{code}" in en, code
    # The backend's status events carry these keys (contract section 4); the dashboard renders them from the catalog.
    for key in ("ai.status_gathering", "ai.status_sending", "ai.status_provider"):
        assert key in en


def test_the_frontend_required_limits_match_the_backend_constants():
    for src in (_ND, _INDEX):
        assert f"output: {const.AI_REQUIRED_OUTPUT_TOKENS}" in src
        assert f"context: {const.AI_REQUIRED_CONTEXT_TOKENS}" in src
        assert f"combined: {const.AI_REQUIRED_COMBINED_TOKENS}" in src


def test_nd_checks_limits_before_streaming_and_only_warns():
    body = _ND[_ND.index("async function runInvestigation()") :]
    body = body[: body.index("function renderReport(")]
    assert body.index("preRunLimits(") < body.index("ND.stream('ai_investigate'")
    pre = _ND[_ND.index("async function preRunLimits(") : _ND.index("async function runInvestigation()")]
    # zone-scoped GET through ND.api (as ai_status); a failed request proceeds (never blocks)
    assert "ND.api('ai_limits')" in pre
    assert re.search(r"catch \(e\) \{ return \{ go: 'run'", pre)
    assert "l.readable === false" in pre and "limits.unreadable" in pre
    assert "limits.run_anyway" in pre and "limits.cancel" in pre


def test_nd_renders_status_keys_error_codes_and_cut_short():
    assert "loading.textContent = aiStatusText(ev)" in _ND
    assert "CAI18N.has(ev.key)" in _ND and "ev.text" in _ND  # falls back to the backend's text
    assert "e.cause && e.cause.error_code" in _ND  # SSE error event code
    assert "cut_short: !!ev.cut_short" in _ND and "cutShortCode(res)" in _ND
    # renderReport (stream done, history entries, the non-streaming JSON result) shows the notices
    render = _ND[_ND.index("function renderReport(") : _ND.index("function formatReport(")]
    assert "aiNotice(cutShortCode(res))" in render and "aiNotice(res.error_code)" in render


def test_every_ai_key_the_dashboard_uses_is_in_the_catalog():
    en = _cat("ai", "en")
    used = set(re.findall(r"\bA\('([a-z_.]+)'\s*[,)]", _ND))  # literal keys; `A('notice.' + code)` is the code list
    assert used, "no ai.* keys found in nd_analysis.js"
    missing = {f"ai.{k}" for k in used} - set(en)
    assert not missing, missing


def test_classic_checks_limits_before_streaming_and_shows_the_notices():
    body = _INDEX[_INDEX.index("async function _runAIInvestigation()") :]
    body = body[: body.index("// ---- Unified action buttons ----")]
    assert body.index("_aiPreRunLimits(content)") < body.index("apiFetchStream('ai_investigate'")
    assert "apiFetch('ai_limits')" in _INDEX
    assert "evErr.errorCode = event.error_code" in body
    for code in _AI_CODES:
        assert f"code === '{code}'" in _INDEX, code


def test_occupant_labels_are_provider_neutral():
    assert "AI Settings (Claude)" not in _INDEX
    assert "ai_settings: 'AI Settings'," in _INDEX


@pytest.mark.parametrize("lang", _LANGS)
def test_claude_only_settings_say_so_in_every_language(lang):
    en, cat = _cat("settings", "en"), _cat("settings", lang)
    for key in _CLAUDE_ONLY:
        assert en[key].startswith("Built-in Claude only. "), key
        assert "Claude" in cat[key].split(".")[0].split("。")[0], (lang, key)
