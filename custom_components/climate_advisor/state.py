"""Operational state persistence for Climate Advisor.

Saves and restores runtime state (classification, weather, temp history,
automation state, daily record, briefing) across HA restarts.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .const import STATE_FILE

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 1


class StatePersistence:
    """Atomic JSON persistence for operational state."""

    def __init__(self, config_dir: Path) -> None:
        self._path = config_dir / STATE_FILE
        self._tmp_path = config_dir / f"{STATE_FILE}.tmp"

    def load(self) -> dict[str, Any]:
        """Load state from disk. Returns empty dict on missing/corrupt file."""
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
        """Write state to disk atomically (write .tmp, then rename)."""
        state["version"] = STATE_VERSION
        try:
            self._tmp_path.write_text(
                json.dumps(state, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(str(self._tmp_path), str(self._path))
        except OSError as err:
            _LOGGER.error("Failed to save state file: %s", err)

    def delete(self) -> None:
        """Remove the state file."""
        try:
            self._path.unlink(missing_ok=True)
            self._tmp_path.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Failed to delete state file: %s", err)
