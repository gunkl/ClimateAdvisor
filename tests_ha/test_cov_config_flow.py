"""Coverage for config_flow.py helper edge paths (Silver test-coverage, #1644).

Occupant view: a settings form always opens and reads sensibly even when a translated phrase is missing or has
a broken placeholder; a slider never rejects the owner's own stored setpoint because of float noise at its
bound, while a genuinely out-of-range value is shown unchanged so the owner can fix it; an input_number
temperature source offers input_number entities; and a GitHub token check reports a blank token or an
unreachable GitHub as a clear error instead of raising.

Real Home Assistant: real selectors and the real aiohttp client session helper (patched to fail).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.climate_advisor import config_flow as cf
from custom_components.climate_advisor.const import TEMP_SOURCE_INPUT_NUMBER, TEMP_SOURCE_SENSOR


def test_text_falls_back_to_key_and_raw_template() -> None:
    assert cf._text({}, "missing.key") == "missing.key"
    assert cf._text({"k": "Hello {name}"}, "k") == "Hello {name}"  # placeholder not supplied
    assert cf._text({"k": "Hello {name}"}, "k", name="Ann") == "Hello Ann"


def test_display_default_snaps_float_noise_but_keeps_real_out_of_range() -> None:
    assert cf._display_default(59.97, False, 60.0, 80.0) == 60.0
    assert cf._display_default(80.03, False, 60.0, 80.0) == 80.0
    assert cf._display_default(85.0, False, 60.0, 80.0) == 85.0
    assert cf._display_default(70.0, False, 60.0, 80.0) == 70.0


def test_entity_selector_follows_the_source_type() -> None:
    assert cf._entity_selector_for_source(TEMP_SOURCE_INPUT_NUMBER).config["domain"] == ["input_number"]
    sensor_cfg = cf._entity_selector_for_source(TEMP_SOURCE_SENSOR).config
    assert sensor_cfg["domain"] == ["sensor"]
    assert sensor_cfg["device_class"] == ["temperature"]


async def test_github_token_check_blank_token(hass: HomeAssistant) -> None:
    assert await cf._async_check_github_token(hass, "", "owner/repo") == "github_token_invalid"


async def test_github_token_check_no_answer(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get.side_effect = OSError("network down")
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session):
        assert await cf._async_check_github_token(hass, "tok", "") == "github_unreachable"
    assert session.get.call_args.args[0].endswith("/user")  # no repository: the token's own user is read
