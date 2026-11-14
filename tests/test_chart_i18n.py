"""Issue #1341 (L9b): chart_v2.js keeps its English in the in-file ``EN`` map (classic index.html never loads
i18n.js); the catalog ``dashboard.chart.*`` keys must stay identical to it, params included."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.helpers.chart_i18n import CHART_JS, chart_en

CATALOG = CHART_JS.parent / "i18n" / "dashboard" / "en.json"
PREFIX = "dashboard.chart."


def _catalog_chart() -> dict[str, str]:
    cat = json.loads(Path(CATALOG).read_text(encoding="utf-8"))
    return {k[len(PREFIX) :]: v for k, v in cat.items() if k.startswith(PREFIX)}


def _params(msg: str) -> set[str]:
    return set(re.findall(r"\{\s*(\w+)\s*[,}]", msg))


def test_en_map_equals_catalog_keys_and_text():
    assert chart_en() == _catalog_chart()


def test_params_match_per_key():
    en, cat = chart_en(), _catalog_chart()
    for key, msg in en.items():
        assert _params(msg) == _params(cat[key]), key


def test_every_literal_t_call_has_a_message():
    src = CHART_JS.read_text(encoding="utf-8")
    used = set(re.findall(r"""\b_(?:t|tH|m)\(\s*'([\w.]+)'""", src))
    missing = {u for u in used if u not in chart_en()}
    # ids built at run time ('cap.' + name, 'group.' + id ...) end with a dot or underscore and are checked by prefix
    assert not {m for m in missing if not m.endswith((".", "_"))}, missing
    for prefix in {m for m in missing if m.endswith((".", "_"))}:
        assert any(k.startswith(prefix) for k in chart_en()), prefix
