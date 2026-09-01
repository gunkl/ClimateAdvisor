"""Operational state persistence for Climate Advisor.

Saves and restores runtime state (classification, weather, temp history,
automation state, daily record, briefing) across HA restarts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .const import STATE_FILE
from .storage_paths import migrate_legacy_storage_file, resolve_entry_scoped_path

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 1


class StatePersistence:
    """Atomic JSON persistence for operational state."""

    def __init__(self, config_dir: Path, entry_id: str = "") -> None:
        self._config_dir = config_dir
        self._entry_id = entry_id
        self._path = resolve_entry_scoped_path(config_dir, STATE_FILE, entry_id)

    def load(self) -> dict[str, Any]:
        """Load state from disk. Returns empty dict on missing/corrupt file."""
        migrate_legacy_storage_file(self._config_dir, STATE_FILE, self._entry_id)
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                _LOGGER.warning("State file is not a JSON object, starting fresh")
                return {}
            if data.get("version") != STATE_VERSION:
                _LOGGER.warning(
                    "State file version %s != expected %s, starting fresh",
                    data.get("version"),
                    STATE_VERSION,
                )
                return {}
            return data
        except (json.JSONDecodeError, OSError) as err:
            _LOGGER.warning("Failed to load state file, starting fresh: %s", err)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        """Write state to disk atomically (write unique .tmp, then rename)."""
        state["version"] = STATE_VERSION
        try:
            serialized = json.dumps(state, indent=2, default=str)
        except (TypeError, ValueError) as err:
            _LOGGER.error("Failed to serialize state: %s", err)
            return

        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=self._path.parent,
            prefix="climate_advisor_state_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            if hasattr(os, "chmod"):
                os.chmod(str(self._path), 0o600)
        except OSError as err:
            _LOGGER.error("Failed to save state file: %s", err)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path_str)

    def delete(self) -> None:
        """Remove the state file and any leftover temp files."""
        try:
            self._path.unlink(missing_ok=True)
            for tmp in self._path.parent.glob("climate_advisor_state_*.tmp"):
                tmp.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Failed to delete state file: %s", err)
