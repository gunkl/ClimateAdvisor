"""Issue #1341: chart_v2.js reads its English text from the in-file ``EN`` map (``_t('id')``).

The static tests that pin the English wording read the module source; ``resolve_en`` puts the English back in place
of every parameter-less ``_t('id')`` call so those assertions keep pinning the same words (and keep failing if a word
changes). ``chart_en`` returns the map itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CHART_JS = Path(__file__).resolve().parents[2] / "custom_components" / "climate_advisor" / "frontend" / "chart_v2.js"


def chart_en(src: str | None = None) -> dict[str, str]:
    src = src if src is not None else CHART_JS.read_text(encoding="utf-8")
    m = re.search(r"/\* EN-BEGIN \*/\s*const EN = (\{.*?\});\s*/\* EN-END \*/", src, re.DOTALL)
    assert m, "EN map markers not found in chart_v2.js"
    return json.loads(m.group(1))


def resolve_en(src: str) -> str:
    en = chart_en()

    def sub(m: re.Match) -> str:
        text = en[m.group(1)].replace("\\", "\\\\").replace("'", "\\'")
        return "'" + text + "'"

    return re.sub(r"_t\('([\w.]+)'\)", sub, src)


def read_chart_js() -> str:
    return resolve_en(CHART_JS.read_text(encoding="utf-8"))
