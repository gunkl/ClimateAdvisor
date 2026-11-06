"""Archive of what prediction engine v4 said, per sensor and look-ahead (Issue #1230 R1).

Shadow data only: nothing in a decision path reads it. It exists so v4's forecasts can later be scored against
what the house then did (the "Prediction accuracy" preview page, ``prediction_accuracy.py``).

Same first-write-wins rule as the v3 ``_pred_archive`` (docs/chart-log-spec.md): the value kept for a 30-minute slot
and look-ahead is the one written when the slot was exactly that far ahead, and a later prediction for the same slot
never overwrites it. In memory::

    data: dict[str, dict[int, dict[str, float]]]    # sensor -> epoch_30min -> {"1h": v, "4h": v, "12h": v, "24h": v}

``sensor`` is an indoor entity id, or ``"_ca"`` for the reading Climate Advisor itself uses. Temperatures are
internal degrees F. ``epoch_30min`` is the slot's Unix time rounded to the nearest 30 minutes.

On disk (``climate_advisor_v4_archive_<entry_id>.json``, compact: ~45 bytes a slot)::

    {"v": 1, "leads": ["1h", "4h", "12h", "24h"],
     "sensors": {"_ca": {"1768000000": [68.1, 68.4, null, null]}}}

Persistence contract mirrors ``ForecastArchive`` / ``ChartStateLog``: ``load()`` / ``save()`` are blocking (callers
MUST run them via the executor), ``save()`` is an atomic tempfile + ``os.replace`` with ``chmod 0o600`` on non-Windows,
retention is time-based (``V4_ARCHIVE_MAX_DAYS``), pruning runs at most once per hour, and no public method raises.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .const import V4_ARCHIVE_FILE, V4_ARCHIVE_LEADS, V4_ARCHIVE_MAX_DAYS
from .storage_paths import resolve_entry_scoped_path

_LOGGER = logging.getLogger(__name__)

_SLOT_S = 1800
_FORMAT_VERSION = 1


def slot_key(dt: datetime) -> int:
    """Unix time of the 30-minute slot nearest to ``dt`` (a cycle that fires a few seconds early still lands right)."""
    return int(round(dt.timestamp() / _SLOT_S)) * _SLOT_S


class V4Archive:
    """Rolling, persisted, first-write-wins archive of v4 predictions."""

    def __init__(
        self,
        config_dir: Path,
        max_days: int = V4_ARCHIVE_MAX_DAYS,
        entry_id: str = "",
    ) -> None:
        self._entry_id = entry_id
        # Entry-scoped like the other zone files (Issue #796); a brand-new file, so no legacy file to migrate.
        self._path = resolve_entry_scoped_path(config_dir, V4_ARCHIVE_FILE, entry_id)
        self._max_days = max_days
        self.data: dict[str, dict[int, dict[str, float]]] = {}
        self._last_prune: float | None = None
        self.dirty = False

    # ------------------------------------------------------------------
    # Persistence (blocking - run via the executor)
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from disk, dropping invalid or expired slots. A missing or corrupt file starts empty. Never raises."""
        try:
            self._load()
        except Exception:  # noqa: BLE001 - the archive must never break startup
            _LOGGER.warning("v4_archive: unexpected error loading archive, starting fresh", exc_info=True)
            self.data = {}

    def _load(self) -> None:
        self.data = {}
        self.dirty = False
        if not self._path.exists():
            _LOGGER.info("v4_archive: no archive file yet, starting empty")
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("v4_archive: failed to read archive file, starting fresh: %s", err)
            return
        sensors = raw.get("sensors") if isinstance(raw, dict) else None
        leads = raw.get("leads") if isinstance(raw, dict) else None
        if not isinstance(sensors, dict) or not isinstance(leads, list):
            _LOGGER.warning("v4_archive: archive file has an unexpected shape, starting fresh")
            return
        cutoff = datetime.now().timestamp() - self._max_days * 86400
        slots = 0
        for sensor, rows in sensors.items():
            if not isinstance(sensor, str) or not isinstance(rows, dict):
                continue
            kept: dict[int, dict[str, float]] = {}
            for key, vals in rows.items():
                try:
                    epoch = int(key)
                except TypeError, ValueError:
                    continue
                if epoch < cutoff or not isinstance(vals, list):
                    continue
                entry = {
                    str(lead): float(v)
                    for lead, v in zip(leads, vals, strict=False)
                    if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
                }
                if entry:
                    kept[epoch] = entry
            if kept:
                self.data[sensor] = kept
                slots += len(kept)
        _LOGGER.info("v4_archive: loaded %d slots across %d sensor lines", slots, len(self.data))

    def save(self) -> None:
        """Atomic write to disk (tempfile + os.replace), 0o600 on non-Windows. Never raises."""
        try:
            leads = list(V4_ARCHIVE_LEADS)
            # list() copies: the executor thread must not iterate a dict the event loop is adding to.
            sensors = {
                sensor: {str(epoch): [vals.get(lead) for lead in leads] for epoch, vals in list(rows.items())}
                for sensor, rows in list(self.data.items())
            }
            serialized = json.dumps({"v": _FORMAT_VERSION, "leads": leads, "sensors": sensors}, separators=(",", ":"))
        except (TypeError, ValueError, RuntimeError) as err:
            _LOGGER.error("v4_archive: failed to serialize archive: %s", err)
            return

        tmp_path_str: str | None = None
        try:
            tmp_fd, tmp_path_str = tempfile.mkstemp(
                dir=self._path.parent, prefix="climate_advisor_v4_archive_", suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            tmp_path_str = None
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
            self.dirty = False
        except OSError as err:
            _LOGGER.error("v4_archive: failed to save archive file: %s", err)
            if tmp_path_str is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path_str)

    # ------------------------------------------------------------------
    # Recording (memory only)
    # ------------------------------------------------------------------

    def record(self, now: datetime, predictions: dict[str, list[tuple[datetime, float]]], primary: str | None) -> int:
        """Archive the 1/4/12/24 h values of one cycle's predictions, first write wins. Returns values newly added.

        ``predictions`` is ``predict()``'s result ``{sensor: [(ts, temp_f), ...]}``. For each look-ahead the
        point nearest ``now + lead`` (within 10 minutes) is stored under the slot nearest its time. The line of
        ``primary`` is also stored as ``"_ca"``, the reading Climate Advisor itself uses. Never raises.
        """
        try:
            return self._record(now, predictions, primary)
        except Exception:  # noqa: BLE001 - shadow data must never break a coordinator cycle
            _LOGGER.warning("v4_archive: failed to record predictions", exc_info=True)
            return 0

    def _record(self, now: datetime, predictions: dict[str, list[tuple[datetime, float]]], primary: str | None) -> int:
        lines = dict(predictions)
        if primary is not None and primary in predictions:
            lines["_ca"] = predictions[primary]
        now_ts = now.timestamp()
        added = 0
        for sensor, points in lines.items():
            if not points:
                continue
            for lead in V4_ARCHIVE_LEADS:
                target = now_ts + _lead_seconds(lead)
                best: tuple[float, datetime, float] | None = None
                for ts, temp in points:
                    if not isinstance(temp, int | float) or not math.isfinite(temp):
                        continue
                    gap = abs(ts.timestamp() - target)
                    if best is None or gap < best[0]:
                        best = (gap, ts, float(temp))
                if best is None or best[0] > 600:
                    continue
                slot = self.data.setdefault(sensor, {}).setdefault(slot_key(best[1]), {})
                if lead not in slot:
                    slot[lead] = round(best[2], 2)
                    added += 1
        if added:
            self.dirty = True
            self._maybe_prune(now_ts)
        return added

    def _maybe_prune(self, now_ts: float) -> None:
        """Evict slots older than the retention; at most once per hour."""
        if self._last_prune is not None and now_ts - self._last_prune < 3600:
            return
        self._last_prune = now_ts
        cutoff = now_ts - self._max_days * 86400
        dropped = 0
        for sensor in list(self.data):
            rows = self.data[sensor]
            old = [epoch for epoch in rows if epoch < cutoff]
            for epoch in old:
                del rows[epoch]
            dropped += len(old)
            if not rows:
                del self.data[sensor]
        if dropped:
            self.dirty = True
            _LOGGER.debug("v4_archive: pruned %d expired slots", dropped)

    def sensor_ids(self) -> list[str]:
        """Sensor lines held (``"_ca"`` plus entity ids)."""
        return list(self.data)

    def series(self, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        """``{slot_epoch: value}`` for one sensor and look-ahead with ``start <= slot < end`` (Unix seconds).

        Safe from an executor thread while the event loop records: it iterates a copy.
        """
        rows = self.data.get(sensor) or {}
        return {epoch: vals[lead] for epoch, vals in list(rows.items()) if start <= epoch < end and lead in vals}

    def snapshot(self) -> dict[str, dict[int, dict[str, float]]]:
        """A copy of the archive that is safe to read from another thread."""
        return {
            sensor: {epoch: dict(vals) for epoch, vals in list(rows.items())}
            for sensor, rows in list(self.data.items())
        }


def _lead_seconds(lead: str) -> float:
    return float(lead.rstrip("h")) * 3600.0


def lead_hours(lead: str) -> float:
    """Hours in a look-ahead label such as ``"4h"``."""
    return float(lead.rstrip("h"))
