"""Tests for the KNOWN-FIXED ISSUES context section (Issue #563, migrated for #702).

Issue #563 fixed a context-bloat bug (every KNOWN_FIXES entry matched a version-scoping
rule that was supposed to bound the section). Issue #702 then moved the underlying data
out of const.py's `RELEASE_NOTES`/`KNOWN_FIXES` dicts entirely, into
`fix_history.jsonl` read via `fix_history.py`'s streaming search
(`tests/test_fix_history.py` covers that module's own selection/relevance logic
directly). These tests now verify `build_known_fixes_context`/`build_version_context`
render fix_history's output correctly, seeding a temp `fix_history.jsonl` rather than
patching `const.KNOWN_FIXES`/`const.RELEASE_NOTES`, which no longer exist.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.ai_skills_context import (
    _KNOWN_FIXES_RECENT_COUNT,
    _trim_issue_fields,
    build_known_fixes_context,
    build_version_context,
)


def _write_fix_history(tmp_path, records):
    path = tmp_path / "fix_history.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def _make_hass() -> MagicMock:
    """A hass mock whose async_add_executor_job actually runs the partial.

    fix_history.py's async wrappers offload real work via
    `hass.async_add_executor_job(functools.partial(...))` — a bare MagicMock returns
    a MagicMock, which isn't awaitable, so these tests need the executor call to
    synchronously execute and return the wrapped function's result.
    """
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return hass


class TestBuildKnownFixesContext:
    def _run(self, tmp_path, records, version):
        coord = MagicMock()
        hass = _make_hass()
        path = _write_fix_history(tmp_path, records)
        with (
            patch("custom_components.climate_advisor.fix_history._FIX_HISTORY_FILE", path),
            patch("custom_components.climate_advisor.const.VERSION", version),
        ):
            return asyncio.run(build_known_fixes_context(hass, coord))

    def test_uses_user_summary_not_scope_covered(self, tmp_path):
        records = [
            {
                "issue": 561,
                "version_fixed": "0.5.50",
                "title": "A very long internal engineering title mentioning function names.",
                "scope_covered": "Added AutomationEngine._any_monitored_sensor_open() as the choke point...",
                "user_summary": "Fix #561: the whole-house fan could turn itself on with windows closed.",
            }
        ]
        ctx = self._run(tmp_path, records, "0.5.50")
        assert "Fix #561: the whole-house fan could turn itself on with windows closed." in ctx
        assert "_any_monitored_sensor_open" not in ctx
        assert "A very long internal engineering title" not in ctx

    def test_falls_back_to_title_when_no_user_summary(self, tmp_path):
        records = [{"issue": 700, "version_fixed": "0.5.50", "title": "Some fix with no user summary."}]
        ctx = self._run(tmp_path, records, "0.5.50")
        assert "Some fix with no user summary." in ctx

    def test_older_fix_still_present_one_release_later(self, tmp_path):
        records = [
            {"issue": 1, "version_fixed": "0.5.50", "title": "older fix"},
            {"issue": 2, "version_fixed": "0.5.51", "title": "newer fix"},
        ]
        ctx = self._run(tmp_path, records, "0.5.51")
        assert "newer fix" in ctx
        assert "older fix" in ctx

    def test_bounded_beyond_recent_count(self, tmp_path):
        records = [{"issue": i, "version_fixed": f"0.{i}.0", "title": f"fix {i}"} for i in range(1, 30)]
        ctx = self._run(tmp_path, records, "0.29.0")
        assert f"({_KNOWN_FIXES_RECENT_COUNT} relevant/recent entries)" in ctx
        assert "Issue #1 " not in ctx
        assert "Issue #29 " in ctx

    def test_empty_fix_history_returns_empty_string(self, tmp_path):
        assert self._run(tmp_path, [], "0.5.50") == ""

    def test_focus_kwarg_ranks_by_relevance(self, tmp_path):
        records = [
            {"issue": 1, "version_fixed": "0.6.70", "title": "unrelated recent fix about fan status"},
            {
                "issue": 2,
                "version_fixed": "0.3.10",
                "title": "nat vent grace period fix",
                "user_summary": "Fix #2: nat vent grace period grace period grace period",
            },
        ]
        coord = MagicMock()
        hass = _make_hass()
        path = _write_fix_history(tmp_path, records)
        with (
            patch("custom_components.climate_advisor.fix_history._FIX_HISTORY_FILE", path),
            patch("custom_components.climate_advisor.const.VERSION", "0.6.70"),
        ):
            ctx = asyncio.run(build_known_fixes_context(hass, coord, focus="nat vent grace period"))
        assert "Issue #2" in ctx


class TestBuildVersionContext:
    def test_renders_recent_release_notes(self, tmp_path):
        records = [
            {"issue": 1, "version_fixed": "0.5.50", "user_summary": "Fix #1: older"},
            {"issue": 2, "version_fixed": "0.5.51", "user_summary": "Fix #2: newer"},
        ]
        path = _write_fix_history(tmp_path, records)
        coord = MagicMock()
        hass = _make_hass()
        with (
            patch("custom_components.climate_advisor.fix_history._FIX_HISTORY_FILE", path),
            patch("custom_components.climate_advisor.const.VERSION", "0.5.51"),
        ):
            ctx = asyncio.run(build_version_context(hass, coord))
        assert "0.5.51" in ctx
        assert "Fix #2: newer" in ctx
        assert "Fix #1: older" in ctx


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
