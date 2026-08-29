"""Tests for fix_history.py (Issue #702).

fix_history.py replaces the old RELEASE_NOTES/KNOWN_FIXES dicts that used to live in
const.py (91% of that file, imported and held resident on every installation) with a
streaming search over fix_history.jsonl — never materializing the full file in memory,
never keeping anything resident between calls.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.fix_history import (
    _iter_records,
    async_recent_release_notes,
    async_search_fix_history,
    parse_version,
    search_records,
)


def _write_jsonl(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "fix_history.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


class TestParseVersion:
    def test_simple_version(self):
        assert parse_version("0.6.46") == (0, 6, 46)

    def test_range_uses_first_token(self):
        assert parse_version("0.6.66-0.6.69") == (0, 6, 66)

    def test_invalid_returns_zero_tuple(self):
        assert parse_version("not-a-version") == (0,)


class TestIterRecords:
    def test_round_trip(self, tmp_path):
        records = [{"issue": 1, "version_fixed": "0.1.0", "title": "a"}, {"issue": 2, "version_fixed": "0.1.1"}]
        path = _write_jsonl(tmp_path, records)
        assert list(_iter_records(path)) == records

    def test_missing_file_returns_no_records(self, tmp_path):
        assert list(_iter_records(tmp_path / "does_not_exist.jsonl")) == []

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "fix_history.jsonl"
        path.write_text('{"issue": 1, "version_fixed": "0.1.0"}\nnot json\n{"issue": 2, "version_fixed": "0.1.1"}\n')
        records = list(_iter_records(path))
        assert [r["issue"] for r in records] == [1, 2]

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "fix_history.jsonl"
        path.write_text('{"issue": 1, "version_fixed": "0.1.0"}\n\n\n')
        assert len(list(_iter_records(path))) == 1

    def test_is_a_generator_never_materializes_whole_file(self):
        # Streaming, not "read the whole file into a list and iterate that" —
        # the entire point of avoiding resident memory for a large corpus.
        assert inspect.isgeneratorfunction(_iter_records)

    def test_orphan_record_with_missing_fields_does_not_crash(self, tmp_path):
        # Orphan records (a release-note bullet with no matching structured fix)
        # have no title/scope_covered — callers must use .get(), not [].
        path = _write_jsonl(tmp_path, [{"issue": 144, "version_fixed": "0.3.44", "user_summary": "x", "orphan": True}])
        results = search_records(query="", limit=15, current_version=(99, 0, 0), path=path)
        assert results[0]["issue"] == 144
        assert results[0].get("title") is None


class TestSearchRecords:
    def test_not_yet_deployed_always_included(self, tmp_path):
        path = _write_jsonl(tmp_path, [{"issue": 1, "version_fixed": "0.9.0", "title": "future fix"}])
        results = search_records(query="", limit=15, current_version=parse_version("0.5.50"), path=path)
        assert any(r["issue"] == 1 for r in results)

    def test_older_fix_still_present_one_release_later(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [
                {"issue": 1, "version_fixed": "0.5.50", "title": "older fix"},
                {"issue": 2, "version_fixed": "0.5.51", "title": "newer fix"},
            ],
        )
        results = search_records(query="", limit=15, current_version=parse_version("0.5.51"), path=path)
        issues = {r["issue"] for r in results}
        assert issues == {1, 2}

    def test_empty_query_falls_back_to_recency(self, tmp_path):
        path = _write_jsonl(
            tmp_path, [{"issue": i, "version_fixed": f"0.{i}.0", "title": f"fix {i}"} for i in range(1, 21)]
        )
        results = search_records(query="", limit=15, current_version=parse_version("0.20.0"), path=path)
        assert len(results) == 15
        assert {r["issue"] for r in results} == set(range(6, 21))

    def test_targeted_query_ranks_by_term_match_not_recency(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [
                {"issue": 1, "version_fixed": "0.6.70", "title": "unrelated recent fix about fan status"},
                {
                    "issue": 2,
                    "version_fixed": "0.3.10",
                    "title": "nat vent grace period",
                    "user_summary": "nat vent grace period grace period",
                },
            ],
        )
        results = search_records(
            query="nat vent grace period", limit=1, current_version=parse_version("0.6.70"), path=path
        )
        assert results[0]["issue"] == 2

    def test_no_terms_match_falls_back_to_recency_not_empty(self, tmp_path):
        path = _write_jsonl(tmp_path, [{"issue": 1, "version_fixed": "0.1.0", "title": "something else entirely"}])
        results = search_records(
            query="completely unrelated nonsense query", limit=5, current_version=parse_version("0.1.0"), path=path
        )
        assert len(results) == 1

    def test_bounded_regardless_of_corpus_size(self, tmp_path):
        path = _write_jsonl(tmp_path, [{"issue": i, "version_fixed": f"{i // 50}.{i % 50}.0"} for i in range(1, 301)])
        results = search_records(query="", limit=15, current_version=parse_version("9.0.0"), path=path)
        assert len(results) <= 15


class TestRecentReleaseNotes:
    def test_groups_by_version_and_orders_descending(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [
                {"issue": 1, "version_fixed": "0.1.0", "user_summary": "Fix #1: old"},
                {"issue": 2, "version_fixed": "0.2.0", "user_summary": "Fix #2: newer"},
            ],
        )
        from custom_components.climate_advisor.fix_history import _recent_release_notes

        result = _recent_release_notes(limit_versions=5, path=path)
        assert list(result.keys()) == ["0.2.0", "0.1.0"]

    def test_skips_records_without_user_summary(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [
                {"issue": 1, "version_fixed": "0.1.0", "title": "internal only, no user_summary"},
                {"issue": 2, "version_fixed": "0.1.0", "user_summary": "Fix #2: user-facing"},
            ],
        )
        from custom_components.climate_advisor.fix_history import _recent_release_notes

        result = _recent_release_notes(limit_versions=5, path=path)
        assert result["0.1.0"] == ["Fix #2: user-facing"]

    def test_bounded_by_limit_versions(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [{"issue": i, "version_fixed": f"0.{i}.0", "user_summary": f"Fix #{i}: x"} for i in range(1, 11)],
        )
        from custom_components.climate_advisor.fix_history import _recent_release_notes

        result = _recent_release_notes(limit_versions=3, path=path)
        assert len(result) == 3


class TestAsyncWrappersOffloadToExecutor:
    def test_async_search_fix_history_uses_executor(self):
        hass = MagicMock()
        hass.async_add_executor_job = MagicMock(return_value=asyncio.sleep(0, result=[]))
        asyncio.run(async_search_fix_history(hass, query="x", limit=5, current_version=(0, 1)))
        assert hass.async_add_executor_job.called

    def test_async_recent_release_notes_uses_executor(self):
        hass = MagicMock()
        hass.async_add_executor_job = MagicMock(return_value=asyncio.sleep(0, result={}))
        asyncio.run(async_recent_release_notes(hass))
        assert hass.async_add_executor_job.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
