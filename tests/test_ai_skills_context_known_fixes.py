"""Tests for the KNOWN_FIXES context-bloat fix (Issue #563).

Two bugs, found and fixed in sequence:

1. `_fix_is_relevant()` (now replaced by `_select_relevant_fixes()`) matched almost
   every KNOWN_FIXES entry because its first rule ("scope_not_covered is non-empty")
   was satisfied by all 169 real entries (the release checklist made the field
   mandatory on every entry), so the intended version-scoping never actually bounded
   anything. `scope_not_covered` has been removed from the schema entirely.

2. The first fix for (1) — bounding by "version_fixed >= current version" — turned
   out to be too narrow to be useful: on a real running install, that only ever
   matches the single most-recent release's fixes (verified: one release after a fix
   ships, its own entry already drops out of context), defeating the "was this
   already fixed" cross-check for anyone even one release behind. `_select_relevant_fixes()`
   replaces it with a count-bounded window (most recent N fixes, plus anything not
   yet deployed) — bounded regardless of KNOWN_FIXES size or release cadence, and
   actually useful across more than a single release.

Rendering also now uses the matching RELEASE_NOTES bullet instead of the long
`title`/`scope_covered` engineering prose.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.ai_skills_context import (
    _KNOWN_FIXES_RECENT_COUNT,
    _parse_version,
    _release_note_bullet,
    _select_relevant_fixes,
    _trim_issue_fields,
    build_known_fixes_context,
)


class TestSelectRelevantFixes:
    def test_includes_most_recent_fix(self):
        known_fixes = {1: {"version_fixed": "0.5.50"}}
        relevant = _select_relevant_fixes(known_fixes, _parse_version("0.5.50"))
        assert 1 in relevant

    def test_still_includes_fix_from_one_release_earlier(self):
        # This is the exact scenario that was broken by the version-equality rule:
        # a fix from the immediately prior release must not vanish from context
        # the moment the next release ships.
        known_fixes = {
            1: {"version_fixed": "0.5.50"},
            2: {"version_fixed": "0.5.51"},
        }
        relevant = _select_relevant_fixes(known_fixes, _parse_version("0.5.51"))
        assert 1 in relevant
        assert 2 in relevant

    def test_not_yet_deployed_entry_always_included(self):
        known_fixes = {1: {"version_fixed": "0.9.0"}}
        relevant = _select_relevant_fixes(known_fixes, _parse_version("0.5.50"))
        assert 1 in relevant

    def test_scope_not_covered_field_has_no_special_effect(self):
        # Regression guard: this field used to force inclusion regardless of version.
        # It has been removed from the schema; even if a stray entry still carried
        # it (e.g. from an unmerged branch), it must not resurrect the old behavior —
        # a very old fix should only be included via the recency window, same as any
        # other old entry, not because this field is present.
        known_fixes = {
            1: {"version_fixed": "0.1.0", "scope_not_covered": "some old gap"},
            **{i: {"version_fixed": f"0.{i}.0"} for i in range(2, 2 + _KNOWN_FIXES_RECENT_COUNT)},
        }
        relevant = _select_relevant_fixes(known_fixes, _parse_version("99.0.0"))
        assert 1 not in relevant

    def test_bounded_under_large_synthetic_history(self):
        """The core regression this fix targets: a large, ever-growing KNOWN_FIXES
        must not make every entry relevant forever, regardless of release cadence."""
        current = _parse_version("9.0.0")
        synthetic = {i: {"version_fixed": f"{i // 50}.{i % 50}.0"} for i in range(1, 301)}
        relevant = _select_relevant_fixes(synthetic, current)
        assert len(relevant) <= _KNOWN_FIXES_RECENT_COUNT

    def test_selects_the_n_most_recent_not_arbitrary_n(self):
        known_fixes = {i: {"version_fixed": f"0.{i}.0"} for i in range(1, 21)}
        relevant = _select_relevant_fixes(known_fixes, _parse_version("0.20.0"))
        assert len(relevant) == _KNOWN_FIXES_RECENT_COUNT
        # the most recent N (20 down to 20 - N + 1) must be the ones kept
        expected = set(range(21 - _KNOWN_FIXES_RECENT_COUNT, 21))
        assert set(relevant.keys()) == expected


class TestReleaseNoteBullet:
    def test_finds_fix_prefixed_bullet(self):
        release_notes = {"0.5.50": ["Fix #561: the whole-house fan could turn itself on with windows closed."]}
        result = _release_note_bullet(release_notes, "0.5.50", 561)
        assert result == "Fix #561: the whole-house fan could turn itself on with windows closed."

    def test_finds_feat_prefixed_bullet(self):
        release_notes = {"0.5.19": ["Feat #519: added speed-aware remote support."]}
        result = _release_note_bullet(release_notes, "0.5.19", 519)
        assert result.startswith("Feat #519:")

    def test_no_match_returns_empty_string(self):
        release_notes = {"0.5.50": ["Fix #561: something else entirely."]}
        assert _release_note_bullet(release_notes, "0.5.50", 999) == ""

    def test_missing_version_returns_empty_string(self):
        release_notes = {"0.5.50": ["Fix #561: something."]}
        assert _release_note_bullet(release_notes, "0.9.9", 561) == ""

    def test_does_not_match_substring_issue_number(self):
        # Fix #5 must not match a bullet for issue #561, etc.
        release_notes = {"0.5.50": ["Fix #561: some fix."]}
        assert _release_note_bullet(release_notes, "0.5.50", 56) == ""


class TestBuildKnownFixesContext:
    def _run(self, known_fixes, release_notes, version):
        coord = MagicMock()
        hass = MagicMock()
        with (
            patch("custom_components.climate_advisor.const.KNOWN_FIXES", known_fixes),
            patch("custom_components.climate_advisor.const.RELEASE_NOTES", release_notes),
            patch("custom_components.climate_advisor.const.VERSION", version),
        ):
            return asyncio.run(build_known_fixes_context(hass, coord))

    def test_uses_release_notes_bullet_not_scope_covered(self):
        known_fixes = {
            561: {
                "version_fixed": "0.5.50",
                "title": "A very long internal engineering title mentioning function names.",
                "scope_covered": "Added AutomationEngine._any_monitored_sensor_open() as the choke point...",
            }
        }
        release_notes = {"0.5.50": ["Fix #561: the whole-house fan could turn itself on with windows closed."]}
        ctx = self._run(known_fixes, release_notes, "0.5.50")
        assert "Fix #561: the whole-house fan could turn itself on with windows closed." in ctx
        assert "_any_monitored_sensor_open" not in ctx
        assert "A very long internal engineering title" not in ctx

    def test_falls_back_to_title_when_no_release_notes_match(self):
        known_fixes = {700: {"version_fixed": "0.5.50", "title": "Some fix with no matching bullet."}}
        ctx = self._run(known_fixes, {}, "0.5.50")
        assert "Some fix with no matching bullet." in ctx

    def test_older_fix_still_present_one_release_later(self):
        known_fixes = {
            1: {"version_fixed": "0.5.50", "title": "older fix"},
            2: {"version_fixed": "0.5.51", "title": "newer fix"},
        }
        ctx = self._run(known_fixes, {}, "0.5.51")
        assert "newer fix" in ctx
        assert "older fix" in ctx

    def test_bounded_beyond_recent_count(self):
        known_fixes = {i: {"version_fixed": f"0.{i}.0", "title": f"fix {i}"} for i in range(1, 30)}
        ctx = self._run(known_fixes, {}, "0.29.0")
        assert f"most recent {_KNOWN_FIXES_RECENT_COUNT} of 29 entries" in ctx
        assert "Issue #1 " not in ctx
        assert "Issue #29 " in ctx

    def test_empty_known_fixes_returns_empty_string(self):
        assert self._run({}, {}, "0.5.50") == ""


class TestTrimIssueFields:
    def test_keeps_only_rendered_fields(self):
        raw = [
            {
                "number": 42,
                "title": "Something broke",
                "state": "open",
                "labels": [{"name": "bug", "color": "ff0000"}],
                "body": "a very long description " * 200,
                "user": {"login": "someone"},
                "assignees": [],
                "milestone": None,
                "reactions": {"+1": 3},
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        trimmed = _trim_issue_fields(raw)
        assert trimmed == [{"number": 42, "title": "Something broke", "state": "open", "labels": [{"name": "bug"}]}]

    def test_empty_list_returns_empty_list(self):
        assert _trim_issue_fields([]) == []

    def test_none_returns_empty_list(self):
        assert _trim_issue_fields(None) == []
