"""Streaming, on-disk fix-history search for Climate Advisor (Issue #702).

`fix_history.jsonl` (shipped alongside this module, one JSON record per line) replaces
the old `RELEASE_NOTES`/`KNOWN_FIXES` dicts that used to live in `const.py` and made up
91% of that file — imported, parsed, and held resident in memory on every installation
regardless of whether anything ever read them.

This module never keeps the file's contents resident: every call streams the file
line-by-line from disk and discards it when done. There is no in-memory index, cache,
or database — relevance ranking is a fresh linear scan each time, which is the right
trade for a rare (opt-in AI Investigator, or ad-hoc dev search via `tools/fix_search.py`),
non-latency-critical read against a file that is at most a few hundred KB.

Record shape (fields are optional except `issue`/`version_fixed`):
    issue: int              — GitHub issue number
    version_fixed: str      — version the fix shipped in (may be a range, e.g. a
                               multi-phase internal migration collapsed into one record)
    title: str              — short dev-facing description
    scope_covered: str      — code paths the fix touched (dev/archaeology detail)
    user_summary: str       — occupant-outcome bullet, when one exists (not every
                               historical change was user-visible — do not assume
                               every record has this field)
    merged_from: list[int]  — present only on a consolidated record representing
                               several issue numbers that were phases of one internal
                               effort (e.g. the strangler-fig FSM migration)
    orphan: bool            — present only on records reconstructed from a release-note
                               bullet that had no matching structured fix record
"""

from __future__ import annotations

import functools
import heapq
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_FIX_HISTORY_FILE = Path(__file__).parent / "fix_history.jsonl"

_SEARCH_FIELDS = ("title", "user_summary", "scope_covered")


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string to a comparable tuple. Ranges use their first token."""
    try:
        first = str(version_str).split("-")[0].split("/")[0]
        return tuple(int(x) for x in first.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _iter_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Stream records one line at a time. Never materializes the full file in memory.

    Malformed lines are logged and skipped rather than aborting the whole scan —
    this file is hand-appended-to on every release (via `tools/add_fix_entry.py`),
    so a single bad line should not take down the entire fix-history feature.

    `path` defaults to the module-level `_FIX_HISTORY_FILE`, read at call time (not
    baked in as a default-argument value) so tests can patch it via
    `patch("custom_components.climate_advisor.fix_history._FIX_HISTORY_FILE", ...)`
    without needing to thread a path through every caller.
    """
    if path is None:
        path = _FIX_HISTORY_FILE
    if not path.exists():
        _LOGGER.warning("fix_history: %s not found, returning no records", path)
        return
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                _LOGGER.warning("fix_history: skipping malformed line %d in %s", lineno, path)


def _score(record: dict[str, Any], terms: list[str]) -> int:
    """Case-insensitive term-count relevance score across the searchable fields."""
    text = " ".join(str(record.get(f, "")) for f in _SEARCH_FIELDS).lower()
    return sum(text.count(term) for term in terms)


def search_records(
    query: str = "",
    limit: int = 15,
    current_version: tuple[int, ...] = (),
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return not-yet-deployed records plus the `limit` most relevant/recent others.

    Sync — callers from async code must offload via `async_search_fix_history`.

    Not-yet-deployed records (`version_fixed` parses later than `current_version`) are
    always included, matching the pre-#702 `_select_relevant_fixes` behavior: a known,
    still-open gap should always surface rather than "not fit in the recency window."

    For the remaining (already-deployed) records: if `query` has terms, rank by a
    simple case-insensitive term-count over title/user_summary/scope_covered and keep
    the top `limit` via `heapq.nlargest` (bounds working memory to O(limit), not
    O(file size)). If `query` is empty (e.g. a narration call with no specific focus),
    fall back to recency ordering by `version_fixed`, matching prior behavior.
    """
    not_yet_deployed: list[dict[str, Any]] = []
    deployed: list[dict[str, Any]] = []
    for record in _iter_records(path):
        if parse_version(record.get("version_fixed", "0")) > current_version:
            not_yet_deployed.append(record)
        else:
            deployed.append(record)

    terms = [t for t in query.lower().split() if t]
    if terms:
        scored = ((_score(r, terms), r) for r in deployed)
        top = heapq.nlargest(limit, scored, key=lambda pair: pair[0])
        relevant = [r for score, r in top if score > 0]
        # If nothing scored, fall back to recency so a query with no hits still
        # returns something rather than an empty, unhelpful section.
        if not relevant:
            relevant = _most_recent(deployed, limit)
    else:
        relevant = _most_recent(deployed, limit)

    return not_yet_deployed + relevant


def _most_recent(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return heapq.nlargest(limit, records, key=lambda r: parse_version(r.get("version_fixed", "0")))


async def async_search_fix_history(
    hass: HomeAssistant,
    query: str = "",
    limit: int = 15,
    current_version: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    """Async, executor-offloaded entry point — this performs blocking file I/O."""
    return await hass.async_add_executor_job(
        functools.partial(search_records, query=query, limit=limit, current_version=current_version)
    )


def _recent_release_notes(limit_versions: int, path: Path | None = None) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for record in _iter_records(path):
        summary = record.get("user_summary")
        if not summary:
            continue
        groups.setdefault(record.get("version_fixed", ""), []).append(summary)

    top_versions = heapq.nlargest(limit_versions, groups.keys(), key=parse_version)
    # Preserve descending-version order in the result.
    top_versions.sort(key=parse_version, reverse=True)
    return {v: groups[v] for v in top_versions}


async def async_recent_release_notes(hass: HomeAssistant, limit_versions: int = 5) -> dict[str, list[str]]:
    """Async, executor-offloaded — replaces the old `RELEASE_NOTES` dict-slice."""
    return await hass.async_add_executor_job(functools.partial(_recent_release_notes, limit_versions))
