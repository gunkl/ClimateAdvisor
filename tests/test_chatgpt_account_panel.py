"""Static checks on the panel (index.html) for the ChatGPT plan UI. The page itself is not run here."""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html").read_text(
    encoding="utf-8"
)


def test_required_plan_ui_text_present():
    for text in (
        "Using ChatGPT plan",
        "Manage usage",
        "Usage limit reached.",
        "You\\'re using your ChatGPT plan",
        "Got it",
        "Backups:",
        "Privacy:",
        "Disconnect",
    ):
        assert text in HTML, text


def test_not_connected_card_points_admins_to_the_options_flow():
    render = HTML[HTML.index("function _renderChatGPTAccount") : HTML.index("function _chatgptAccountMsg")]
    assert "Configure" in render and "AI Settings" in render
    assert "/config/integrations/integration/climate_advisor" in render


def test_disconnect_control_only_rendered_for_admins():
    render = HTML[HTML.index("function _renderChatGPTAccount") : HTML.index("function _chatgptAccountMsg")]
    admin_gate = render.index("if (!admin)")
    assert render.index("chatgptAccountDisconnect()") > admin_gate
    assert "a.is_admin === true" in render


def test_failed_load_shows_the_card_only_for_a_zone_known_to_be_on_an_account_provider():
    load = HTML[HTML.index("async function loadChatGPTAccount") : HTML.index("function _renderChatGPTAccount")]
    failure = load[load.index("} catch (e) {") :]
    assert "aiStatus && aiStatus.account_state != null" in failure
    assert "card.style.display = 'block'" not in failure


def test_panel_says_why_a_zone_that_could_not_apply_its_settings_is_stopped():
    reason = HTML[HTML.index("function _aiBlockedReason") : HTML.index("function _safeUsageUrl")]
    assert reason.index("_aiStatus.stop_reason") < reason.index("_aiStatus.account_state")


def test_monthly_cost_hidden_for_non_cost_providers():
    assert re.search(r"tracks_cost === false \? ''.*Monthly Cost", HTML)


def test_no_hardcoded_claude_label_left_in_ai_settings():
    # Only the Claude zone's title names Claude: any other provider's zone has an "AI Provider" row.
    assert "ai_settings: 'AI Settings (Claude)'" in HTML
    assert "items.some(s => s.key === 'ai_provider') ? 'AI Settings'" in HTML
    assert "Claude's reasoning" not in HTML


def test_usage_links_must_be_https_and_open_safely():
    assert "u.protocol === 'https:'" in HTML
    assert 'rel="noopener noreferrer"' in HTML


def test_dynamic_account_text_is_escaped():
    render = HTML[HTML.index("function _renderChatGPTAccount") : HTML.index("function _chatgptAccountMsg")]
    for needle in ("escapeHtml(String(a.email))", "escapeHtml(state.replace", "escapeHtml(String(s.provider_label"):
        assert needle in render, needle


# ---- Provider-neutral AI settings rows (config payload is built per zone from the provider spec) ----

import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from custom_components.climate_advisor.ai_backend import PROVIDERS  # noqa: E402
from custom_components.climate_advisor.ai_providers import ensure_providers_registered  # noqa: E402
from custom_components.climate_advisor.const import CHATGPT_ACCOUNT_HASS_KEY, CONFIG_METADATA  # noqa: E402

AI_KEYS = [k for k, m in CONFIG_METADATA.items() if m["category"] == "ai_settings"]
CLAUDE_WORDS = ("Claude", "Anthropic", "Sonnet", "Haiku", "Opus", "extended thinking", "more expensive")


def _ai_rows(config, account_state=None):
    from custom_components.climate_advisor.api import ClimateAdvisorConfigView
    from tests.test_api import _make_view_request

    coord = MagicMock()
    coord.config = config
    coord._outdoor_sensor_fallback_reason = None
    request = _make_view_request(coord)
    if account_state is not None:
        request.app["hass"].data[CHATGPT_ACCOUNT_HASS_KEY] = SimpleNamespace(state=account_state)
    resp = asyncio.run(ClimateAdvisorConfigView().get(request))
    return [s for s in resp.json_data["settings"] if s["category"] == "ai_settings"], resp.json_data["settings"]


def test_claude_zone_payload_unchanged():
    ensure_providers_registered()
    for cfg in ({}, {"ai_provider": "anthropic"}):
        rows, _ = _ai_rows(cfg)
        assert [r["key"] for r in rows] == AI_KEYS
        for r in rows:
            assert r["label"] == CONFIG_METADATA[r["key"]]["label"]
            assert r["description"] == CONFIG_METADATA[r["key"]]["description"]


def test_chatgpt_zone_shows_only_applicable_neutral_rows():
    ensure_providers_registered()
    rows, _ = _ai_rows(
        {"ai_provider": "chatgpt", "ai_chatgpt_model": "gpt-5", "ai_model": "claude-sonnet-5"},
        account_state="connected",
    )
    by_key = {r["key"]: r for r in rows}
    assert set(by_key) == {
        "ai_enabled",
        "ai_provider",
        "ai_chatgpt_model",
        "ai_manual_requests_per_day",
        "ai_investigator_enabled",
        "ai_investigator_requests_per_day",
    }
    assert by_key["ai_provider"]["value"] == "ChatGPT subscription (connected)"
    assert by_key["ai_chatgpt_model"]["value"] == "gpt-5"  # what is actually sent, not ai_model
    for r in rows:
        for word in CLAUDE_WORDS:
            assert word not in r["label"] + r["description"], (r["key"], word)


def test_chatgpt_unset_model_shows_not_set_and_a_disconnected_provider_row_without_a_manager():
    ensure_providers_registered()
    rows, _ = _ai_rows({"ai_provider": "chatgpt"})
    by_key = {r["key"]: r for r in rows}
    assert by_key["ai_chatgpt_model"]["value"] is None
    assert by_key["ai_provider"]["value"] == "ChatGPT subscription (disconnected)"


def test_fake_api_key_provider_gets_key_row_and_its_capabilities(monkeypatch):
    ensure_providers_registered()
    base = PROVIDERS["anthropic"]
    fake = dataclasses.replace(
        base,
        id="fakekey",
        label="Fake Router",
        display_name="Fake Router",
        api_name="Fake Router",
        tracks_cost=True,
        supports_tuning=False,
        plan_restricted=False,
        uses_claude_wording=False,
        model_setting="ai_fake_model",
    )
    monkeypatch.setitem(PROVIDERS, "fakekey", fake)
    rows, _ = _ai_rows({"ai_provider": "fakekey", "ai_api_key": "sk-secret", "ai_fake_model": "m1"})
    by_key = {r["key"]: r for r in rows}
    assert "ai_api_key" in by_key and by_key["ai_api_key"]["value"] == "configured"
    assert "ai_monthly_budget" in by_key and "ai_auto_requests_per_day" in by_key
    for gone in ("ai_reasoning_effort", "ai_max_tokens", "ai_temperature", "ai_model"):
        assert gone not in by_key
    assert by_key["ai_fake_model"]["value"] == "m1"
    for r in rows:
        for word in CLAUDE_WORDS:
            assert word not in r["label"] + r["description"], (r["key"], word)


def test_provider_with_its_own_key_field_shows_that_field_and_never_its_value(monkeypatch):
    ensure_providers_registered()
    fake = dataclasses.replace(
        PROVIDERS["anthropic"],
        id="ownkey",
        label="Own Key Router",
        display_name="Own Key Router",
        api_name="Own Key Router",
        uses_claude_wording=False,
        credential_setting="ai_ownkey_key",
        model_setting="ai_ownkey_model",
    )
    monkeypatch.setitem(PROVIDERS, "ownkey", fake)
    rows, _ = _ai_rows({"ai_provider": "ownkey", "ai_ownkey_key": "own-secret", "ai_api_key": "claude-secret"})
    by_key = {r["key"]: r for r in rows}
    assert "ai_api_key" not in by_key
    assert by_key["ai_ownkey_key"]["value"] == "configured"
    assert by_key["ai_ownkey_key"]["label"] == "API Key"
    assert "secret" not in json.dumps(rows)

    rows, _ = _ai_rows({"ai_provider": "ownkey", "ai_api_key": "claude-secret"})
    assert {r["key"]: r for r in rows}["ai_ownkey_key"]["value"] == "not set"


def test_status_label_keeps_underscores_only_for_claude_zones():
    assert "s.claude_wording === true ? statusValue : statusValue.replace(/_/g, ' ')" in HTML


def test_reauth_status_is_coloured_as_a_warning():
    assert "reauth_required: 'var(--warning)'" in HTML


def test_ai_status_model_is_what_is_sent_per_provider():
    from custom_components.climate_advisor.claude_api import ClaudeAPIClient

    ensure_providers_registered()
    claude = ClaudeAPIClient({"ai_enabled": True, "ai_model": "claude-x"}).get_status()
    assert claude["model"] == "claude-x"
    assert claude["plan_restricted"] is False and claude["claude_wording"] is True
    gpt = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_model": "claude-x"}, None)
    assert gpt.get_status()["model"] == ""
    assert gpt.get_status()["plan_restricted"] is True and gpt.get_status()["claude_wording"] is False
    gpt2 = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "gpt-5"}, None)
    assert gpt2.get_status()["model"] == "gpt-5"


def test_investigation_refuses_early_when_no_model_is_selected():
    from unittest.mock import AsyncMock

    from custom_components.climate_advisor.api import ClimateAdvisorInvestigateView
    from tests.test_api import _make_view_request

    coord = MagicMock()
    coord.config = {"ai_enabled": True, "ai_investigator_enabled": True}
    coord.claude_client.configured_model.return_value = ""
    request = _make_view_request(coord)
    request.headers = {}
    request.json = AsyncMock(return_value={"focus": "", "hours": 24})
    resp = asyncio.run(ClimateAdvisorInvestigateView().post(request))
    assert resp.status == 400
    assert "AI Settings" in str(resp.json_data)
    coord.claude_client.check_investigator_rate_limit.assert_not_called()


def test_panel_status_card_capability_driven():
    assert "s.plan_restricted === true ? ''" in HTML  # Auto Today hidden for manual-only providers
    assert "'not set'" in HTML and "s.provider_label" in HTML
    assert "Which Claude" not in HTML and "Anthropic" not in HTML


def test_manage_usage_links_use_theme_link_color():
    css_rule = re.search(r"#chatgpt-account-content a[^{]*\{[^}]*\}", HTML)
    assert css_rule and "var(--primary" in css_rule.group(0)
    assert re.search(r"#ai-plan-indicator a[^{]*\{[^}]*var\(--primary", HTML)
