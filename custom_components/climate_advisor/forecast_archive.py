"""Forecast archive for Climate Advisor (Issue #1230 R0).

Persists what the hourly weather forecast SAID at each point in time, so the forecast error at every
lead time (1 h, 4 h, 12 h, 24 h ahead...) can be scored later against what the weather then did.
Today only the *current hour's* forecast value is logged (chart_log ``pred_outdoor``), which cannot
answer "how wrong was the 12-hour-ahead forecast?".

Record-only: no automation reads this, nothing is displayed, no REST/API exposure. The only reader is
``get_snapshots()`` for the offline backtest tool.

Persistence contract mirrors ``ChartStateLog`` (docs/chart-log-spec.md): ``load()`` / ``save()`` are
blocking (callers MUST run them via the executor), ``save()`` is an atomic tempfile + ``os.replace``
with ``chmod 0o600`` on non-Windows, retention is time-based, pruning runs at most once per hour, and
no public method raises.

Recording rules (anchors for the Tier 3 spec):

* Called by the coordinator only after a *successful fetch that returned entries* at the single choke
  point ``_refresh_hourly_forecast()``. A kept-forecast reuse during an outage is never recorded -
  it would duplicate old data under a new issue time and corrupt error scoring (Issue #1337).
* At most one snapshot per local clock hour: a later fetch in the same hour REPLACES that hour's
  snapshot (the latest issue in the hour wins - it is the freshest statement of what the provider
  expected, and the 30-minute cycle plus the briefing/midnight refreshes can fetch several times
  an hour). A refetch whose content (source, grid start, temps) is unchanged is not a change: the
  earlier issue time is kept and nothing is written.
* Temperatures are internal degrees F, exactly as given. ``_get_hourly_forecast_data()`` already
  normalised the list to F at its source (Issue #1015); this module NEVER converts.
* Only real provider hourly entries are recorded. The chart's synthetic extended days
  (``_build_extended_hourly_forecast``) are filtered out by the caller and never reach here.
* Horizon is capped at ``FORECAST_ARCHIVE_HORIZON_HOURS`` ahead of the issue time.

On-disk schema (compact; one hourly grid per snapshot rather than a timestamp per hour, which keeps a
365-day archive near 3-4 MB)::

    {"snapshots": [
        {"i": "2026-11-06T14:05-07:00",      # issued: when the fetch happened (minute precision)
         "s": "weather.home",                # source weather entity id
         "h0": "2026-11-06T14:00-07:00",     # first forecast hour of the grid
         "t": [54.3, 53.1, null, 51.8, ...]} # temp_F (1 decimal) for h0 + n hours; null = no entry
    ]}

``get_snapshots()`` decodes this to the documented read shape
``{"issued": iso, "source": str, "hours": [[iso_hour, temp_f], ...]}``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .const import FORECAST_ARCHIVE_FILE, FORECAST_ARCHIVE_HORIZON_HOURS, FORECAST_ARCHIVE_MAX_DAYS
from .storage_paths import resolve_entry_scoped_path

_LOGGER = logging.getLogger(__name__)

_GRID_TOLERANCE_S = 60  # an entry further than this from an exact hour boundary of the grid is skipped


def _aware(dt: datetime, default_tz: Any = UTC) -> datetime:
    """Return ``dt`` as an aware datetime; a naive value is read in ``default_tz``."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=default_tz)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to an aware datetime (naive read as UTC); None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


class ForecastArchive:
    """Rolling, persisted archive of hourly-forecast snapshots (one per local clock hour)."""

    def __init__(
        self,
        config_dir: Path,
        max_days: int = FORECAST_ARCHIVE_MAX_DAYS,
        horizon_hours: int = FORECAST_ARCHIVE_HORIZON_HOURS,
        entry_id: str = "",
    ) -> None:
        self._config_dir = config_dir
        self._entry_id = entry_id
        # Entry-scoped like chart_log / state / learning (Issue #796). A brand-new file, so there is no
        # legacy unscoped file to migrate.
        self._path = resolve_entry_scoped_path(config_dir, FORECAST_ARCHIVE_FILE, entry_id)
        self._max_days = max_days
        self._horizon_hours = horizon_hours
        # Encoded snapshots (on-disk shape), ascending by issue time.
        self._snapshots: list[dict[str, Any]] = []
        self._last_prune: datetime | None = None

    # ------------------------------------------------------------------
    # Persistence (blocking - run via the executor)
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from disk, dropping invalid or expired snapshots. Never raises."""
        try:
            self._load()
        except Exception:  # noqa: BLE001 - the archive must never break startup
            _LOGGER.warning("forecast_archive: unexpected error loading archive, starting fresh", exc_info=True)
            self._snapshots = []

    def _load(self) -> None:
        if not self._path.exists():
            self._snapshots = []
            _LOGGER.info("forecast_archive: no archive file yet, starting empty")
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("forecast_archive: failed to read archive file, starting fresh: %s", err)
            self._snapshots = []
            return
        if not isinstance(data, dict):
            _LOGGER.warning("forecast_archive: archive file is not a JSON object, starting fresh")
            self._snapshots = []
            return
        raw = data.get("snapshots")
        if not isinstance(raw, list):
            _LOGGER.warning("forecast_archive: 'snapshots' key missing or not a list, starting fresh")
            self._snapshots = []
            return

        cutoff = datetime.now(UTC) - timedelta(days=self._max_days)
        kept: list[tuple[datetime, dict[str, Any]]] = []
        for snap in raw:
            issued = self._valid_issued(snap)
            if issued is None or issued < cutoff:
                continue
            kept.append((issued, snap))
        kept.sort(key=lambda pair: pair[0])
        self._snapshots = [snap for _, snap in kept]
        dropped = len(raw) - len(self._snapshots)
        if dropped:
            _LOGGER.debug("forecast_archive: dropped %d invalid or expired snapshots on load", dropped)
        if self._snapshots:
            _LOGGER.info(
                "forecast_archive: loaded %d snapshots spanning %s .. %s",
                len(self._snapshots),
                self._snapshots[0]["i"],
                self._snapshots[-1]["i"],
            )
        else:
            _LOGGER.info("forecast_archive: loaded 0 snapshots")

    @staticmethod
    def _valid_issued(snap: Any) -> datetime | None:
        """Issue time of a structurally valid on-disk snapshot, else None."""
        if not isinstance(snap, dict):
            return None
        if not isinstance(snap.get("s"), str) or not isinstance(snap.get("t"), list):
            return None
        if _parse_iso(snap.get("h0")) is None:
            return None
        return _parse_iso(snap.get("i"))

    def save(self) -> None:
        """Atomic write to disk (tempfile + os.replace), 0o600 on non-Windows. Never raises."""
        try:
            # list() copy: the executor thread must not iterate the list while the event loop appends.
            serialized = json.dumps({"snapshots": list(self._snapshots)}, separators=(",", ":"))
        except (TypeError, ValueError) as err:
            _LOGGER.error("forecast_archive: failed to serialize snapshots: %s", err)
            return

        tmp_path_str: str | None = None
        try:
            tmp_fd, tmp_path_str = tempfile.mkstemp(
                dir=self._path.parent, prefix="climate_advisor_forecast_archive_", suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            tmp_path_str = None
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
        except OSError as err:
            _LOGGER.error("forecast_archive: failed to save archive file: %s", err)
            if tmp_path_str is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path_str)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, issued: datetime, source: str, hourly: list[tuple[datetime, float]]) -> bool:
        """Record one snapshot in memory. Returns True when the archive changed (caller then saves).

        ``hourly`` is the real hourly forecast as ``(forecast_hour, temp_F)`` pairs, already in internal
        degrees F (never converted here). A naive forecast hour is read in the issue time's timezone.
        Replaces the snapshot already held for the same local clock hour. Does not touch disk. Never raises.
        """
        try:
            return self._record(issued, source, hourly)
        except Exception:  # noqa: BLE001 - the archive must never break a coordinator cycle
            _LOGGER.warning("forecast_archive: failed to record snapshot", exc_info=True)
            return False

    def _record(self, issued: datetime, source: str, hourly: list[tuple[datetime, float]]) -> bool:
        issued = _aware(issued)
        tz = issued.tzinfo
        # Lead 0 is the hour containing the issue time; earlier hours are history, not forecast.
        first_allowed = _floor_hour(issued)
        last_allowed = first_allowed + timedelta(hours=self._horizon_hours)  # exclusive: exactly N hourly points
        points: dict[datetime, float] = {}
        for hour, temp in hourly:
            hour = _aware(hour, tz)
            if not math.isfinite(temp) or hour < first_allowed or hour >= last_allowed:
                continue
            points[hour] = temp
        if not points:
            return False

        h0 = min(points)
        h0_floor = h0.replace(second=0, microsecond=0)
        grid: dict[int, float] = {}
        skipped = 0
        for hour, temp in points.items():
            offset_s = (hour - h0_floor).total_seconds()
            idx = round(offset_s / 3600)
            if abs(offset_s - idx * 3600) > _GRID_TOLERANCE_S:
                skipped += 1  # not on the hourly grid (e.g. a 90-minute entry); never invent a value
                continue
            grid[idx] = round(temp, 1)
        if not grid:
            return False
        temps: list[float | None] = [grid.get(i) for i in range(max(grid) + 1)]

        encoded = {
            "i": issued.isoformat(timespec="minutes"),
            "s": str(source),
            "h0": h0_floor.isoformat(timespec="minutes"),
            "t": temps,
        }

        # Same local clock hour -> replace (aware-datetime equality keeps a DST fall-back repeat hour distinct).
        issue_hour = _floor_hour(issued)
        replaced = False
        if self._snapshots:
            last_issued = _parse_iso(self._snapshots[-1]["i"])
            if last_issued is not None and _floor_hour(last_issued.astimezone(tz)) == issue_hour:
                # Identical CONTENT (source, grid start, temps) -> no change and no file rewrite, whatever the
                # minute of the refetch: the 30-min cycle usually refetches an unchanged provider forecast, and
                # comparing "i" too would rewrite the whole year-long file on every such fetch. The earlier issue
                # time is kept: the provider already said exactly this then.
                prev = self._snapshots[-1]
                if all(prev.get(k) == encoded[k] for k in ("s", "h0", "t")):
                    return False
                self._snapshots[-1] = encoded
                replaced = True
        if not replaced:
            self._snapshots.append(encoded)
        self._maybe_prune()
        _LOGGER.debug(
            "forecast_archive: recorded snapshot issued=%s source=%s hours=%d%s%s total=%d",
            encoded["i"],
            source,
            sum(1 for t in temps if t is not None),
            " (replaced same-hour snapshot)" if replaced else "",
            f" skipped_off_grid={skipped}" if skipped else "",
            len(self._snapshots),
        )
        return True

    def _maybe_prune(self) -> None:
        """Evict snapshots older than max_days; at most once per hour (like ChartStateLog)."""
        now = datetime.now(UTC)
        if self._last_prune is not None and now - self._last_prune < timedelta(hours=1):
            return
        self._last_prune = now
        cutoff = now - timedelta(days=self._max_days)
        before = len(self._snapshots)
        self._snapshots = [s for s in self._snapshots if (_parse_iso(s["i"]) or now) >= cutoff]
        if before != len(self._snapshots):
            _LOGGER.debug("forecast_archive: pruned %d expired snapshots", before - len(self._snapshots))

    # ------------------------------------------------------------------
    # Read (for the R1 backtest tool only)
    # ------------------------------------------------------------------

    def get_snapshots(self, start: datetime, end: datetime) -> list[dict]:
        """Snapshots issued in ``[start, end)``, ascending by issue time. Never raises (returns [] on error).

        Each item is ``{"issued": iso, "source": str, "hours": [[iso_hour, temp_f], ...]}``; hours with no
        provider entry are omitted. Temperatures are internal degrees F. Naive ``start``/``end`` are read as UTC.
        """
        try:
            start_a, end_a = _aware(start), _aware(end)
            out: list[dict] = []
            for snap in list(self._snapshots):
                issued = _parse_iso(snap["i"])
                if issued is None or not (start_a <= issued < end_a):
                    continue
                out.append(self._decode(snap))
            return out
        except Exception:  # noqa: BLE001
            _LOGGER.warning("forecast_archive: get_snapshots failed", exc_info=True)
            return []

    @staticmethod
    def _decode(snap: dict[str, Any]) -> dict:
        h0 = _parse_iso(snap["h0"])
        hours: list[list[Any]] = []
        if h0 is not None:
            for idx, temp in enumerate(snap["t"]):
                if isinstance(temp, (int, float)) and not isinstance(temp, bool):
                    hours.append([(h0 + timedelta(hours=idx)).isoformat(timespec="minutes"), temp])
        return {"issued": snap["i"], "source": snap["s"], "hours": hours}
