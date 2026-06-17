"""Chart state log for Climate Advisor.

Appends periodic HVAC state snapshots and event markers to a rolling JSON log.
Supports downsampled retrieval for charting over 6h–1y ranges.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

_CHART_LOG_FILE = "climate_advisor_chart_log.json"

# Downsampling thresholds (in days)
_RAW_THRESHOLD_DAYS = 3
_HOURLY_THRESHOLD_DAYS = 30


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string, returning None on failure."""
    with contextlib.suppress(ValueError, TypeError):
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    return None


class ChartStateLog:
    """Rolling log of HVAC state snapshots and event markers for charting."""

    def __init__(self, config_dir: Path, max_days: int = 365) -> None:
        self._path = config_dir / _CHART_LOG_FILE
        self._max_days = max_days
        self._entries: list[dict[str, Any]] = []
        # Prune on append at most once per hour to avoid O(n) work on every snapshot
        self._last_prune: datetime | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from disk, pruning entries older than max_days. Silent on missing file."""
        if not self._path.exists():
            self._entries = []
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("chart_log: failed to read log file, starting fresh: %s", err)
            self._entries = []
            return

        if not isinstance(data, dict):
            _LOGGER.warning("chart_log: log file is not a JSON object, starting fresh")
            self._entries = []
            return

        entries = data.get("entries")
        if not isinstance(entries, list):
            _LOGGER.warning("chart_log: 'entries' key missing or not a list, starting fresh")
            self._entries = []
            return

        cutoff = datetime.now(UTC) - timedelta(days=self._max_days)
        pruned: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ts = _parse_ts(entry.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            pruned.append(entry)

        removed = len(entries) - len(pruned)
        if removed:
            _LOGGER.debug("chart_log: pruned %d old entries on load", removed)

        self._entries = pruned

    def save(self) -> None:
        """Atomic write to disk (tempfile + os.replace). 0o600 on non-Windows."""
        try:
            serialized = json.dumps({"entries": self._entries}, separators=(",", ":"))
        except (TypeError, ValueError) as err:
            _LOGGER.error("chart_log: failed to serialize entries: %s", err)
            return

        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=self._path.parent,
            prefix="climate_advisor_chart_log_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
        except OSError as err:
            _LOGGER.error("chart_log: failed to save log file: %s", err)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path_str)

    def append(
        self,
        *,
        hvac: str,
        fan: bool,
        indoor: float | None,
        outdoor: float | None,
        windows_open: bool = False,
        windows_recommended: bool = False,
        pred_outdoor: float | None = None,
        pred_indoor: float | None = None,
        setpoint: float | None = None,
        event: str | None = None,
        ts: str | None = None,
        fan_running: bool = False,
        nat_vent_active: bool = False,
    ) -> None:
        """Append one entry. ts defaults to now (via dt_util).

        fan_running: fan is physically on right now (active, manual override, or
            untracked).  False when nat-vent is armed but the fan is between cycles,
            or when the fan is inactive/disabled.
        nat_vent_active: automation_engine._natural_vent_active — the nat-vent
            session is armed (fan may or may not be spinning).
        """
        if ts is None:
            ts = dt_util.now().isoformat()

        entry: dict[str, Any] = {
            "ts": ts,
            "hvac": hvac,
            "fan": fan,
            "indoor": indoor,
            "outdoor": outdoor,
            "windows_open": windows_open,
            "windows_recommended": windows_recommended,
            "pred_outdoor": pred_outdoor,
            "pred_indoor": pred_indoor,
            "setpoint": setpoint,
            "fan_running": fan_running,
            "nat_vent_active": nat_vent_active,
        }
        if event is not None:
            entry["event"] = event

        self._entries.append(entry)
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        """Prune entries older than max_days, at most once per hour."""
        now = dt_util.now()
        if self._last_prune is not None and (now - self._last_prune) < timedelta(hours=1):
            return
        self._last_prune = now
        cutoff = now - timedelta(days=self._max_days)
        before = len(self._entries)
        # Keep entries whose ts parses and is within window. Also keep entries
        # whose ts cannot be parsed — they shouldn't normally exist, but dropping
        # them silently here would hide bugs and surprise callers that just
        # appended them.
        kept: list[dict[str, Any]] = []
        for e in self._entries:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts >= cutoff:
                kept.append(e)
        self._entries = kept
        removed = before - len(self._entries)
        if removed:
            _LOGGER.debug("chart_log: pruned %d old entries on append", removed)

    def get_entries(self, range_str: str = "24h", before: datetime | None = None) -> list[dict[str, Any]]:
        """Return entries filtered to the requested range, with downsampling.

        range_str values: "6h", "12h", "24h", "3d", "7d", "30d", "1y"

        before: optional upper-bound anchor (timezone-aware datetime).  When
            provided, the window is [anchor - range_days, anchor) instead of
            [now - range_days, now).  Entries at or after *before* are excluded.
            This supports historical navigation: pass the right edge of the
            desired viewport as *before* to retrieve the corresponding data
            window from the past.

        Downsampling rules:
        - <= 3 days (6h/12h/24h/3d): return raw entries
        - 4–30 days (7d/30d): hourly averages
        - > 30 days (1y): daily summaries
        """
        range_days = self._range_str_to_days(range_str)
        anchor = before if before is not None else dt_util.now()
        cutoff = anchor - timedelta(days=range_days)

        filtered = [e for e in self._entries if self._entry_after(e, cutoff)]

        # When a before anchor is supplied, exclude entries at or after it so
        # the caller receives only the [cutoff, anchor) window.
        if before is not None:
            filtered = [e for e in filtered if not self._entry_after(e, before)]

        if range_days <= _RAW_THRESHOLD_DAYS:
            return filtered
        if range_days <= _HOURLY_THRESHOLD_DAYS:
            return self._bucket_hourly(filtered)
        return self._bucket_daily(filtered)

    @property
    def entry_count(self) -> int:
        """Return number of entries currently in memory."""
        return len(self._entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _range_str_to_days(range_str: str) -> float:
        _map = {
            "6h": 0.25,
            "12h": 0.5,
            "24h": 1.0,
            "3d": 3.0,
            "7d": 7.0,
            "30d": 30.0,
            "1y": 365.0,
        }
        return _map.get(range_str, 1.0)

    @staticmethod
    def _entry_after(entry: dict[str, Any], cutoff: datetime) -> bool:
        ts = _parse_ts(entry.get("ts", ""))
        return ts is not None and ts >= cutoff

    @staticmethod
    def _dominant_hvac(hvac_list: list[str]) -> str:
        counts: dict[str, int] = defaultdict(int)
        for h in hvac_list:
            if h:
                counts[h] += 1
        if not counts:
            return ""
        return max(counts, key=lambda k: counts[k])

    def _bucket_hourly(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bucket entries into 1-hour averages."""
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            ts = _parse_ts(entry.get("ts", ""))
            if ts is None:
                continue
            # Key: hour-truncated ISO string (UTC)
            bucket_key = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00+00:00")
            buckets[bucket_key].append(entry)

        result: list[dict[str, Any]] = []
        for bucket_key in sorted(buckets):
            group = buckets[bucket_key]
            indoor_vals = [e["indoor"] for e in group if e.get("indoor") is not None]
            outdoor_vals = [e["outdoor"] for e in group if e.get("outdoor") is not None]
            events = [e["event"] for e in group if "event" in e]

            pred_outdoor_vals = [e["pred_outdoor"] for e in group if e.get("pred_outdoor") is not None]
            pred_indoor_vals = [e["pred_indoor"] for e in group if e.get("pred_indoor") is not None]
            setpoint_vals = [e["setpoint"] for e in group if e.get("setpoint") is not None]

            summary: dict[str, Any] = {
                "ts": bucket_key,
                "hvac": self._dominant_hvac([e.get("hvac", "") for e in group]),
                "fan": any(e.get("fan", False) for e in group),
                "fan_running": any(e.get("fan_running", False) for e in group),
                "nat_vent_active": any(e.get("nat_vent_active", False) for e in group),
                "indoor": round(sum(indoor_vals) / len(indoor_vals), 1) if indoor_vals else None,
                "outdoor": (round(sum(outdoor_vals) / len(outdoor_vals), 1) if outdoor_vals else None),
                "windows_open": any(e.get("windows_open", False) for e in group),
                "windows_recommended": any(e.get("windows_recommended", False) for e in group),
                "pred_outdoor": (
                    round(sum(pred_outdoor_vals) / len(pred_outdoor_vals), 1) if pred_outdoor_vals else None
                ),
                "pred_indoor": (round(sum(pred_indoor_vals) / len(pred_indoor_vals), 1) if pred_indoor_vals else None),
                "setpoint": round(sum(setpoint_vals) / len(setpoint_vals), 1) if setpoint_vals else None,
            }
            if events:
                summary["event"] = events
            result.append(summary)

        return result

    def _bucket_daily(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bucket entries into daily summaries."""
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            ts = _parse_ts(entry.get("ts", ""))
            if ts is None:
                continue
            day_key = ts.astimezone(UTC).strftime("%Y-%m-%d")
            buckets[day_key].append(entry)

        result: list[dict[str, Any]] = []
        for day_key in sorted(buckets):
            group = buckets[day_key]
            indoor_vals = [e["indoor"] for e in group if e.get("indoor") is not None]
            outdoor_vals = [e["outdoor"] for e in group if e.get("outdoor") is not None]
            events = [e["event"] for e in group if "event" in e]
            fan_count = sum(1 for e in group if e.get("fan", False))

            pred_outdoor_vals = [e["pred_outdoor"] for e in group if e.get("pred_outdoor") is not None]
            pred_indoor_vals = [e["pred_indoor"] for e in group if e.get("pred_indoor") is not None]
            setpoint_vals = [e["setpoint"] for e in group if e.get("setpoint") is not None]

            summary: dict[str, Any] = {
                "ts": f"{day_key}T00:00:00+00:00",
                "hvac": self._dominant_hvac([e.get("hvac", "") for e in group]),
                "fan_minutes": fan_count * 30,
                "fan_running": any(e.get("fan_running", False) for e in group),
                "nat_vent_active": any(e.get("nat_vent_active", False) for e in group),
                "indoor_avg": round(sum(indoor_vals) / len(indoor_vals), 1) if indoor_vals else None,
                "indoor_min": min(indoor_vals) if indoor_vals else None,
                "indoor_max": max(indoor_vals) if indoor_vals else None,
                "outdoor_avg": (round(sum(outdoor_vals) / len(outdoor_vals), 1) if outdoor_vals else None),
                "outdoor_min": min(outdoor_vals) if outdoor_vals else None,
                "outdoor_max": max(outdoor_vals) if outdoor_vals else None,
                "windows_open": any(e.get("windows_open", False) for e in group),
                "windows_recommended": any(e.get("windows_recommended", False) for e in group),
                "pred_outdoor_avg": (
                    round(sum(pred_outdoor_vals) / len(pred_outdoor_vals), 1) if pred_outdoor_vals else None
                ),
                "pred_indoor_avg": (
                    round(sum(pred_indoor_vals) / len(pred_indoor_vals), 1) if pred_indoor_vals else None
                ),
                "setpoint": round(sum(setpoint_vals) / len(setpoint_vals), 1) if setpoint_vals else None,
            }
            if events:
                summary["events"] = events
            result.append(summary)

        return result
