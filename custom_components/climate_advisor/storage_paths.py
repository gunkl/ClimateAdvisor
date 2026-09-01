"""Entry-scoped storage path resolution for Climate Advisor.

Multi-zone support (Issue #796) means more than one config entry can be
loaded at once, each running its own `StatePersistence`, `ChartStateLog`,
and `LearningEngine`. Before this module existed, all three hand-rolled the
same `config_dir / <fixed filename>` join with no per-entry scoping, so a
second zone's writes collided with (clobbered) the first zone's file. This
module is the single source of truth for that scoping scheme so it cannot
drift across the three files the way the original bug already did (Gaps
1-3 in `docs/multi-zone-spec.md`).

This is a plain function module, not a mixin — same precedent as
`fan_status.py::resolve_untracked_fan_status()` for a "3+ places need the
same logic" problem.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def resolve_entry_scoped_path(config_dir: Path, base_filename: str, entry_id: str) -> Path:
    """Build an entry-scoped storage path.

    e.g. 'climate_advisor_learning.json' + entry_id -> 'climate_advisor_learning_<entry_id>.json'.

    Verified safe against all three actual storage filenames (`STATE_FILE`,
    `LEARNING_DB_FILE`, `_CHART_LOG_FILE`) — each has exactly one `.`, so
    `rsplit(".", 1)` splits correctly.

    Deviation from the spec's reference implementation: when `entry_id` is
    falsy (empty string), this returns the plain unscoped path
    (`config_dir / base_filename`) instead of appending a bare trailing
    underscore. Two real callers rely on this: (1) the simulation harness
    and ~90 existing unit tests construct `StatePersistence`/`ChartStateLog`/
    `LearningEngine` directly with no `entry_id`, asserting against the
    literal unscoped filename (e.g. `tmp_path / STATE_FILE`) as part of
    testing unrelated behavior (atomic-write, corruption-recovery, tmp-file
    cleanup) — always-scoping would silently rename their target file out
    from under them. (2) `ClimateAdvisorCoordinator.__init__` already treats
    `entry_id=""` as its own established "no resolvable config entry" case
    (see `coordinator.py`'s `self._entry_id` comment) — treating "" as "use
    the legacy unscoped path" here is consistent with that existing meaning
    rather than inventing a new one.
    """
    if not entry_id:
        return config_dir / base_filename
    stem, ext = base_filename.rsplit(".", 1)
    return config_dir / f"{stem}_{entry_id}.{ext}"


def migrate_legacy_storage_file(config_dir: Path, base_filename: str, entry_id: str) -> None:
    """One-time, idempotent migration of a pre-multi-zone unscoped storage file.

    A pre-existing single-zone install has `climate_advisor_state.json` (etc.)
    at the unscoped path. After this fix ships, that entry's coordinator looks
    for the entry-scoped path instead and would otherwise find nothing —
    silently losing learning/state/chart history on upgrade. This migrates
    the legacy file to the entry-scoped name the first time an entry with a
    real `entry_id` starts up and finds one.

    No-ops when: `entry_id` is falsy (harness/test contexts with no real
    config entry — `resolve_entry_scoped_path` already returns the unscoped
    path for these, so there is nothing to migrate away from), the
    entry-scoped file already exists (already migrated), or the legacy file
    doesn't exist (fresh install, or already migrated and cleaned up).
    Safe to call on every startup — after the first successful migration the
    legacy file is gone, so every later call is a no-op.

    If two zones exist from before this fix shipped (both already writing
    into the same colliding legacy file), whichever entry's coordinator
    starts up first claims that file; the second entry's migration call then
    finds the legacy file already gone and no-ops, starting fresh. That data
    was already being clobbered by the pre-existing collision bug this
    migration exists to fix going forward — this is not a new data-loss mode,
    just where the arbitrary "which entry gets it" question already implicit
    in the collision bug gets resolved.

    Blocking I/O — callers MUST run this via `hass.async_add_executor_job`
    (matches every other call in this codebase that touches `self._path`;
    see `StatePersistence.load`/`ChartStateLog.load`/`LearningEngine.load_state`,
    all already offloaded by `coordinator.async_restore_state()`).

    Atomicity: the legacy file is only unlinked *after* the migrated copy has
    been durably written via the existing write-tmp-then-os.replace pattern
    used elsewhere in this codebase (`state.py`/`chart_log.py`). If the
    process crashes before the `os.replace`, the legacy file is untouched and
    the next startup retries. If it crashes after `os.replace` but before the
    final `unlink`, both files exist — harmless duplication, not data loss —
    and the next startup no-ops (entry-scoped file already exists) leaving
    the stale legacy file in place. There is no window where neither file is
    readable.
    """
    if not entry_id:
        return

    legacy_path = config_dir / base_filename
    new_path = resolve_entry_scoped_path(config_dir, base_filename, entry_id)
    if new_path == legacy_path or new_path.exists() or not legacy_path.exists():
        return

    try:
        data = legacy_path.read_bytes()
    except OSError as err:
        _LOGGER.warning(
            "storage_paths: failed to read legacy file %s for migration: %s",
            legacy_path.name,
            err,
        )
        return

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=config_dir,
        prefix=f"{new_path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path_str, str(new_path))
        if sys.platform != "win32":
            os.chmod(str(new_path), 0o600)
    except OSError as err:
        _LOGGER.error(
            "storage_paths: failed to write migrated file %s: %s",
            new_path.name,
            err,
        )
        with contextlib.suppress(OSError):
            os.unlink(tmp_path_str)
        return

    try:
        legacy_path.unlink()
        _LOGGER.info(
            "storage_paths: migrated %s -> %s for entry %s",
            legacy_path.name,
            new_path.name,
            entry_id,
        )
    except OSError as err:
        _LOGGER.warning(
            "storage_paths: migrated to %s but failed to remove legacy file %s: %s",
            new_path.name,
            legacy_path.name,
            err,
        )
