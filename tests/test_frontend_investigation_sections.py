"""Regression test for Issue #925's second live-render bug.

`frontend/index.html` has three independent places that must agree on the
investigation report's section names: `_INVESTIGATION_SECTION_DEFS` (render
order for the live-streaming preview), `_INVESTIGATION_HEADER_MAP` (used only
by the live-streaming parser, `_parseInvestigationSections()`, to recognize a
`## HEADER` line and attribute its content to a section key), and
`_formatInvestigationReport()`'s own `sections` list (used for the final,
already-complete report). When `## ACTIVITY SUMMARY` was restored as a real
section, only two of these three were updated -- `_INVESTIGATION_HEADER_MAP`
was missed, so every line streamed under that header was silently discarded
(mapped to a `null` key) until the next recognized header appeared. This test
reads index.html directly (no JS test harness exists in this repo) and
asserts every section key declared in `_INVESTIGATION_SECTION_DEFS` has a
corresponding header entry in `_INVESTIGATION_HEADER_MAP` -- the actual
invariant that broke.
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX_HTML = (
    Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html"
).read_text(encoding="utf-8")


def _extract_section_def_keys(text: str) -> set[str]:
    block_match = re.search(r"_INVESTIGATION_SECTION_DEFS\s*=\s*\[(.*?)\];", text, re.DOTALL)
    assert block_match, "_INVESTIGATION_SECTION_DEFS not found in index.html"
    return set(re.findall(r"key:\s*'([a-z_]+)'", block_match.group(1)))


def _extract_header_map_values(text: str) -> set[str]:
    block_match = re.search(r"_INVESTIGATION_HEADER_MAP\s*=\s*\{(.*?)\};", text, re.DOTALL)
    assert block_match, "_INVESTIGATION_HEADER_MAP not found in index.html"
    return set(re.findall(r":\s*'([a-z_]+)'", block_match.group(1)))


class TestInvestigationSectionMapsAgree:
    def test_every_section_def_key_has_a_header_map_entry(self):
        section_keys = _extract_section_def_keys(_INDEX_HTML)
        header_map_values = _extract_header_map_values(_INDEX_HTML)

        missing = section_keys - header_map_values
        assert not missing, (
            f"Section key(s) {missing} exist in _INVESTIGATION_SECTION_DEFS but have no "
            "entry in _INVESTIGATION_HEADER_MAP -- their content will be silently dropped "
            "during live streaming (see Issue #925 follow-up)."
        )

    def test_activity_summary_specifically_is_mapped(self):
        """Direct regression check for the exact bug reported: ACTIVITY SUMMARY
        content wasn't rendering live until the Incongruities header appeared."""
        assert "'ACTIVITY SUMMARY': 'activity_summary'" in _INDEX_HTML
