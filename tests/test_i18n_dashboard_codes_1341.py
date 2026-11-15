"""The dashboard branches on the backend's real codes (Issue #1341, PR C).

The dashboard lanes were first written against guessed code names. These pins read the JS and fail when a code it
branches on is not one the backend emits (or when the catalog lacks the sentence for it).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor import coordinator, status_text  # noqa: E402

FE = Path(coordinator.__file__).parent / "frontend"
STATUS_JS = (FE / "nd_status.js").read_text(encoding="utf-8")
CHART_JS = (FE / "chart_v2.js").read_text(encoding="utf-8")
DASH = json.loads((FE / "i18n" / "dashboard" / "en.json").read_text(encoding="utf-8"))


def test_sensor_role_codes_are_the_backends():
    roles = set(coordinator._SENSOR_ROLE_KEYS)
    for src, name in ((STATUS_JS, "nd_status.js"), (CHART_JS, "chart_v2.js")):
        used = set(re.findall(r"role_code\s*===\s*'([a-z_]+)'", src))
        assert used and used <= roles | {"ca_reading"}, (name, used - roles)
    assert set(re.findall(r"KNOWN = \[([^\]]*)\]", CHART_JS)[0].replace("'", "").replace(" ", "").split(",")) <= roles


def test_fan_sentence_codes_are_the_backends_and_each_has_a_sentence():
    block = re.search(r"var FAN_SENTENCE = \{(.*?)\};", STATUS_JS, re.DOTALL).group(1)
    pairs = re.findall(r"(\w+): '(\w+)'", block)
    backend = {code for code, _key in status_text.FAN_STATUS_CODES.values()}
    assert {c for c, _ in pairs} == backend  # every fan token has a sentence, none is invented
    for _code, key in pairs:
        assert f"dashboard.status.{key}" in DASH, key


def test_automation_codes_are_the_backends():
    keys = coordinator._AUTO_STATUS_KEYS
    for code in ("disabled", "nat_vent", "nat_vent_soft_start"):
        assert code in keys, code
    assert {"active", "active_away", "active_vacation", "active_guest"} <= set(keys)
    assert "status_code" in STATUS_JS and "automation_status_msg" in STATUS_JS
