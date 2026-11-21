"""status_text: payload siblings and fan-status tokens render through the catalog (Issue #1341, Silver coverage).

Occupant view: the dashboard shows the fan line and status lines in the viewer's language; a token Climate Advisor
does not know still shows its own text instead of disappearing, and a code the guards compare stays stable.
"""

from __future__ import annotations

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.status_text import (
    FAN_STATUS_CODES,
    CodedText,
    fan_status_text,
    render_text,
    wire_of,
    with_siblings,
)


class TestWithSiblings:
    def test_coded_text_renders_in_language_with_msg_and_code_siblings(self):
        value = fan_status_text("active")
        payload: dict = {}
        with_siblings(payload, "fan_status", value, "fr", code=True)
        assert payload["fan_status"] == "actif"
        assert payload["fan_status_msg"] == i18n.Msg("status.fan_status.active").to_wire()
        assert payload["fan_status_code"] == "active"

    def test_without_code_flag_no_code_sibling(self):
        payload: dict = {}
        with_siblings(payload, "fan_status", fan_status_text("inactive"), "en")
        assert payload == {
            "fan_status": "inactive",
            "fan_status_msg": i18n.Msg("status.fan_status.inactive").to_wire(),
        }

    def test_non_string_value_passes_through_untouched(self):
        payload: dict = {}
        with_siblings(payload, "count", 5, "fr", code=False)
        assert payload == {"count": 5, "count_msg": None}

    def test_plain_prose_keeps_its_text_and_has_no_msg(self):
        payload: dict = {}
        with_siblings(payload, "detail", "plain words", "fr")
        assert payload == {"detail": "plain words", "detail_msg": None}


class TestFanStatusText:
    def test_coded_text_is_returned_as_is(self):
        original = CodedText("custom", None, text="something")
        assert fan_status_text(original) is original

    def test_unknown_token_keeps_text_with_unknown_code(self):
        out = fan_status_text("spinning sideways")
        assert str(out) == "spinning sideways"
        assert out.code == "unknown"
        assert out.msg is None
        assert wire_of(out) is None
        assert render_text(out, "fr") == "spinning sideways"

    def test_every_enum_token_keeps_english_text_and_catalog_code(self):
        for token, (code, key) in FAN_STATUS_CODES.items():
            out = fan_status_text(token)
            assert str(out) == token
            assert out.code == code
            assert out.msg == i18n.Msg(key)

    def test_render_text_none_is_empty(self):
        assert render_text(None, "fr") == ""
