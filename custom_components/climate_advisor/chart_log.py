"""Chart state log for Climate Advisor.

Appends periodic HVAC state snapshots and event markers to a rolling JSON log.
Supports downsampled retrieval for charting over 6h–1y ranges.
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import os
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.util import dt as dt_util

from . import store_json
from .storage_paths import migrate_legacy_storage_file, resolve_entry_scoped_path

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


def _row_ts(entry: dict[str, Any]) -> datetime | None:
    """A row's parsed ``ts`` (``None`` when missing or unparseable)."""
    return _parse_ts(entry.get("ts", ""))


def _order_after(last: datetime | None, timestamps: Iterable[datetime | None]) -> tuple[bool, datetime | None]:
    """Issue #1598: whether ``timestamps`` continue a time-sorted run that ended at ``last``, and the run's newest.

    A missing/unparseable timestamp or a step backwards (a clock correction) breaks the run.
    """
    for ts in timestamps:
        if ts is None or (last is not None and ts < last):
            return False, None
        last = ts
    return True, last


class _NaiveRow(Exception):
    """A row whose ``ts`` has no UTC offset: its place in time depends on whose clock reads it (Issue #1730)."""


def _aware_epoch(entry: dict[str, Any]) -> float:
    """A row's ``ts`` as epoch seconds; raises for a missing/unparseable ``ts`` and for one without an offset."""
    dt = datetime.fromisoformat(entry["ts"])
    if dt.tzinfo is None:
        raise _NaiveRow
    return dt.timestamp()


def epoch_range(rows: list[dict[str, Any]], n: int, lo: float, hi: float) -> tuple[int, int] | None:
    """Issue #1730: ``(i, j)`` such that ``rows[i:j]`` of the time-sorted ``rows[:n]`` are the rows whose ``ts`` is
    within ``[lo, hi]`` (epoch seconds), found by bisection (~2 log2(n) timestamp parses instead of one per row), or
    ``None`` when a probed row cannot be read that way. Rows are written with an offset
    (``dt_util.now().isoformat()``); a row without one makes this answer ``None`` if a probe meets it, so a caller that
    reads a naive ``ts`` in its own zone keeps its own rule (the scan)."""
    try:
        i = bisect.bisect_left(rows, lo, 0, n, key=_aware_epoch)
        j = bisect.bisect_right(rows, hi, i, n, key=_aware_epoch)
    except _NaiveRow, KeyError, TypeError, ValueError, OverflowError, OSError:
        return None
    return i, j


def epoch_window(rows: list[dict[str, Any]], n: int, lo: float, hi: float) -> list[dict[str, Any]] | None:
    """The rows ``epoch_range`` selects, in log order, or ``None`` (the caller scans). A superset-provider: callers
    re-apply their own exact predicate to the slice."""
    got = epoch_range(rows, n, lo, hi)
    return rows[got[0] : got[1]] if got is not None else None


def _bucket_comfort_target(group: list[dict[str, Any]]) -> float | None:
    """The averaged room comfort target of a bucket, or None when no row in it carried ``comfort_target``.

    Issue #1194 (d): ``comfort_target`` exists only on rows where the remote-sensor outer loop held an offset; on
    every other row the room's target IS the thermostat ``setpoint``. Each row therefore contributes its comfort target
    when it has one and its setpoint otherwise -- the same preference ``_extract_historical_effective_target()``
    applies to a raw row -- so a 7d/30d/1y bucket that holds part of an offset night still charts the room's target,
    not the commanded value. Buckets without any offset row get no key (unchanged; the extractor uses setpoint).
    """
    if not any(e.get("comfort_target") is not None for e in group):
        return None
    vals = [
        e["comfort_target"] if e.get("comfort_target") is not None else e["setpoint"]
        for e in group
        if e.get("comfort_target") is not None or e.get("setpoint") is not None
    ]
    return round(sum(vals) / len(vals), 1)


def _bucket_indoor_sensors(group: list[dict[str, Any]]) -> dict[str, float | None] | None:
    """Per-entity mean (2 dp) of the rows' ``indoor_sensors``, or None when no row in the bucket carried the field.

    Issue #1230 R0. Same precedent as ``comfort_target``: the key appears only when at least one row has it, under the
    same name in the hourly and daily shapes. An entity whose every reading in the bucket is None stays in the mapping
    as None (the sensor was configured but silent). Legacy rows without the field contribute nothing.
    """
    maps = [e["indoor_sensors"] for e in group if isinstance(e.get("indoor_sensors"), dict)]
    if not maps:
        return None
    vals: dict[str, list[float]] = {}
    for m in maps:
        for entity_id, v in m.items():
            bucket = vals.setdefault(entity_id, [])
            if isinstance(v, int | float) and not isinstance(v, bool):
                bucket.append(v)
    return {k: (round(sum(v) / len(v), 2) if v else None) for k, v in vals.items()}


def _bucket_hold(group: list[dict[str, Any]]) -> dict[str, float]:
    """The bucket's ``hold_lower``/``hold_upper`` (Issue #1406), or {} when no row in it carried them.

    Same rule as ``lower``/``upper`` (the mean of the rows, 1 dp), with each row contributing the band the chart drew
    for it: its hold when it has one, else its ``lower``/``upper`` (on those rows the hold IS the band schedule). So an
    hour or a day that is part bank and part not averages the drawn band, never the hold rows alone. Buckets without
    any hold row get no keys (the ``comfort_target`` precedent), and the chart draws their ``lower``/``upper``.
    """
    if not any(e.get("hold_lower") is not None and e.get("hold_upper") is not None for e in group):
        return {}
    out: dict[str, float] = {}
    for key, base in (("hold_lower", "lower"), ("hold_upper", "upper")):
        vals = []
        for e in group:
            has_hold = e.get("hold_lower") is not None and e.get("hold_upper") is not None
            v = e.get(key) if has_hold else e.get(base)
            if v is not None:
                vals.append(v)
        if vals:
            out[key] = round(sum(vals) / len(vals), 1)
    return out if len(out) == 2 else {}


# Issue #1414: humidity and feels-like fields (chart display only). Written only when a value exists.
HUMIDITY_FEELSLIKE_KEYS: tuple[str, ...] = ("humidity", "feelslike", "outdoor_humidity", "outdoor_feelslike")


def _bucket_optional_means(group: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, float]:
    """Each key's mean over the rows that carry it (not rounded: native resolution); a key no row carries is absent.

    Issue #1414: one shared rule for the hourly and daily shapes, so a feels-like that applied for part of an hour
    averages only the rows where NWS defined it (a mild row has no feels-like, never a stand-in value).
    """
    out: dict[str, float] = {}
    for key in keys:
        vals = [e[key] for e in group if isinstance(e.get(key), int | float) and not isinstance(e.get(key), bool)]
        if vals:
            out[key] = sum(vals) / len(vals)
    return out


def _bucket_context(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket the Issue #1230 R2 context fields: dominant occupancy and band_sensor, override if any row had it.

    Each key appears only when at least one row in the bucket carried it (same precedent as ``comfort_target``).
    """
    out: dict[str, Any] = {}
    for key in ("occupancy", "band_sensor"):
        counts: dict[str, int] = defaultdict(int)
        for e in group:
            v = e.get(key)
            if isinstance(v, str) and v:
                counts[v] += 1
        if counts:
            out[key] = max(counts, key=lambda k: counts[k])
    if any(e.get("override") is True for e in group):
        out["override"] = True
    # Issue #1436: the plan's window state, when any row recorded it (true if the plan had the windows open in any row)
    planned = [e.get("windows_planned") for e in group if isinstance(e.get("windows_planned"), bool)]
    if planned:
        out["windows_planned"] = any(planned)
    return out


class ChartStateLog:
    """Rolling log of HVAC state snapshots and event markers for charting."""

    def __init__(self, config_dir: Path, max_days: int = 365, entry_id: str = "") -> None:
        self._config_dir = config_dir
        self._entry_id = entry_id
        self._path = resolve_entry_scoped_path(config_dir, _CHART_LOG_FILE, entry_id)
        self._max_days = max_days
        self._entries: list[dict[str, Any]] = []
        # Issue #1598: what is known about the time order of ``_entries``: (that list object, how many leading rows
        # have parseable, non-decreasing timestamps, the newest of them). ``-1`` = that list is not time-sorted.
        # One tuple, replaced whole, so an executor-thread reader never sees a half-updated record.
        self._order: tuple[list[dict[str, Any]], int, datetime | None] = (self._entries, 0, None)
        # Prune on append at most once per hour to avoid O(n) work on every snapshot
        self._last_prune: datetime | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from disk, pruning entries older than max_days. Silent on missing file."""
        migrate_legacy_storage_file(self._config_dir, _CHART_LOG_FILE, self._entry_id)
        if not self._path.exists():
            self._entries = []
            return
        try:
            data = store_json.loads(self._path.read_bytes())
        except (OSError, ValueError) as err:
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
        pruned_ts: list[datetime] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ts = _parse_ts(entry.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            pruned.append(entry)
            pruned_ts.append(ts)

        removed = len(entries) - len(pruned)
        if removed:
            _LOGGER.debug("chart_log: pruned %d old entries on load", removed)

        self._entries = pruned
        self._note_order(pruned, pruned_ts)

    def save(self) -> None:
        """Atomic write to disk (tempfile + os.replace). 0o600 on non-Windows."""
        try:
            # list() copy: one atomic snapshot, since the rows are now encoded one at a time while the loop appends
            serialized = store_json.dumps({"entries": list(self._entries)})  # one row per line (Issue #1730)
        except (TypeError, ValueError) as err:
            _LOGGER.error("chart_log: failed to serialize entries: %s", err)
            return

        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=self._path.parent,
            prefix="climate_advisor_chart_log_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
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
        lower: float | None = None,
        upper: float | None = None,
        nat_vent_target: float | None = None,
        indoor_primary: float | None = None,
        comfort_target: float | None = None,
        indoor_sensors: dict[str, float | None] | None = None,
        occupancy: str | None = None,
        override: bool | None = None,
        band_sensor: str | None = None,
        hold_lower: float | None = None,
        hold_upper: float | None = None,
        windows_planned: bool | None = None,
        humidity: float | None = None,
        feelslike: float | None = None,
        outdoor_humidity: float | None = None,
        outdoor_feelslike: float | None = None,
        recal_false: list[str] | None = None,
    ) -> None:
        """Append one entry. ts defaults to now (via dt_util).

        indoor_primary: the always-primary indoor reading (``IndoorTempReading.primary_value``,
            never the sleep-window swap that ``indoor`` may carry), written on
            ``hvac_action_change`` rows (Issue #1234) for the swing backfill. The key is present
            only when a value is given, so other rows and older logs are unchanged.

        fan_running: fan is physically on right now (active, manual override, or
            untracked).  False when nat-vent is armed but the fan is between cycles,
            or when the fan is inactive/disabled.
        nat_vent_active: automation_engine._natural_vent_active — the nat-vent
            session is armed (fan may or may not be spinning).
        lower/upper: the target-band schedule's resolved lower/upper bound for the
            current instant (Issue #514) — an immutable per-cycle snapshot of "what
            was the system's actual target at this timestamp," not a re-derivable
            prediction. The caller is responsible for resolving these once per cycle
            (see coordinator.py's ``_target_band_lower_upper_now()``) and passing the
            already-resolved values here; this method does not compute them.
        comfort_target: the room's comfort target (internal F) on a row where the remote-sensor outer loop holds an
            offset (Issue #1194 d), so ``setpoint`` (what the thermostat was told) and the target the room is
            supposed to reach are charted apart. The key is present only when an offset was in force, so every other
            row and older logs are unchanged.
        indoor_sensors: every configured indoor sensor's reading, ``{entity_id: temp_f_or_None}`` in internal F rounded
            to 2 dp (``indoor_temp.read_indoor_sensors_f()``; ``None`` = that sensor failed the resolver's
            parse/plausibility/stale rules). Read on every row regardless of the sleep window, so the v4 model can fit
            one line per sensor. The key is present only when a mapping is given (new rows from the coordinator always
            give one); older rows lack it. Informational only: nothing reads it for a decision or a display, and
            ``get_chart_data()`` strips it from ``state_log`` (Issue #1230 R0).
        occupancy: the zone occupancy mode on this row (home, away, vacation or guest; Issue #1230 R2),
            so the Status chart can draw an Away / Vacation strip. Written when given; absent on older rows.
        override: True while a manual override or its grace period is active on this row (Issue #1230 R2). Written only
            when True, so ordinary rows stay as small as before; absent means "no override".
        band_sensor: entity id of the sensor Climate Advisor was reading for comfort on this row, i.e. the sensor the
            Target Band applied to (the bedroom sensor in the sleep window; Issue #1230 R2). Written when known.
        hold_lower/hold_upper: the band the chart DREW at this instant when it differs from ``lower``/``upper``
            (Issue #1406): a time-of-use bank drawn as the hold the thermostat is told (an Away cool bank to 77 F in a
            60-80 F band: 60-77, where ``lower``/``upper`` keep the band schedule's 77-80), the schedule band while a
            bank is deferred, the Away/Vacation period widened. Display only; the caller passes the pair only when it
            differs, and it is written only as a pair, so ordinary rows and older logs are unchanged and
            ``lower``/``upper`` keep their meaning for the replay tools.
        windows_planned: whether the operation timeline expected the windows open at this instant (Issue #1436): the
            PAST "Win plan" strip, the same meaning as its faded part. ``windows_recommended`` is the classifier's live
            flag (true at 4 AM on a warm day), which is why it can no longer draw that strip. Written when given (True
            or False); absent on older rows and while there is no plan.
        humidity/feelslike/outdoor_humidity/outdoor_feelslike: Issue #1414, chart display only. ``humidity`` is the
            indoor RH % of the room whose temperature is ``indoor`` (the bedroom in the sleep window), ``feelslike`` its
            NWS heat index (internal F) when NWS defines one; ``outdoor_humidity`` the outdoor RH %,
            ``outdoor_feelslike`` the NWS heat index or wind chill (internal F). Each is written only when not None.
        recal_false: Issue #1676, the entities reading falsely on this row because the thermostat is recalibrating after
            a heating shutdown (the climate entity and the sensors on its device). Their readings are still logged as
            received; the v4 fit skips them on this row (``predictor_v4``). Written only when given and non-empty.
        nat_vent_target: the real thermostatic target the nat-vent fan was cycling
            around this cycle (Phase 3a), computed via
            ``nat_vent_cycling.compute_nat_vent_target()`` — only meaningful (non-None)
            when ``nat_vent_active`` is True for this cycle. Immutable per-cycle
            snapshot, same discipline as ``lower``/``upper``; this method does not
            compute it.
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
            "lower": lower,
            "upper": upper,
            "nat_vent_target": nat_vent_target,
        }
        if event is not None:
            entry["event"] = event
        if indoor_primary is not None:
            entry["indoor_primary"] = indoor_primary
        if comfort_target is not None:
            entry["comfort_target"] = comfort_target
        if indoor_sensors is not None:
            entry["indoor_sensors"] = dict(indoor_sensors)
        if occupancy is not None:
            entry["occupancy"] = occupancy
        if override:
            entry["override"] = True
        if band_sensor is not None:
            entry["band_sensor"] = band_sensor
        if hold_lower is not None and hold_upper is not None:
            entry["hold_lower"] = hold_lower
            entry["hold_upper"] = hold_upper
        if windows_planned is not None:
            entry["windows_planned"] = bool(windows_planned)
        for key, val in (
            ("humidity", humidity),
            ("feelslike", feelslike),
            ("outdoor_humidity", outdoor_humidity),
            ("outdoor_feelslike", outdoor_feelslike),
        ):
            if val is not None:
                entry[key] = val
        if recal_false:
            entry["recal_false"] = list(recal_false)

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
        sorted_rows = self._time_sorted_rows(check_unknown=False)
        if sorted_rows is not None:
            # Issue #1599: rows in time order (all parseable), so the rows to drop are a prefix found by bisection
            # (~log2(n) parses) instead of parsing every row of up to a year once an hour on the event loop. Keeps
            # exactly the suffix the walk below would keep.
            rows, n = sorted_rows
            lo = bisect.bisect_left(rows, cutoff, 0, n, key=_row_ts)
            if lo:
                kept_rows = rows[lo:]
                self._entries = kept_rows
                self._order = (kept_rows, len(kept_rows), _row_ts(kept_rows[-1]) if kept_rows else None)
                _LOGGER.debug("chart_log: pruned %d old entries on append", lo)
            return
        # Order unknown or broken: the full walk (which records the order for next time).
        # Keep entries whose ts parses and is within window. Also keep entries
        # whose ts cannot be parsed — they shouldn't normally exist, but dropping
        # them silently here would hide bugs and surprise callers that just
        # appended them.
        kept: list[dict[str, Any]] = []
        kept_ts: list[datetime | None] = []
        for e in self._entries:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts >= cutoff:
                kept.append(e)
                kept_ts.append(ts)
        self._entries = kept
        self._note_order(kept, kept_ts)
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

        sorted_rows = self._time_sorted_rows()
        if sorted_rows is not None:
            # Issue #1598: rows are in time order, so the window is found by bisection (~log2(n) timestamp parses)
            # instead of parsing every row of up to a year to keep the last few days. The [cutoff, before) slice is
            # exactly what the full scan below keeps.
            rows, n = sorted_rows
            lo = bisect.bisect_left(rows, cutoff, 0, n, key=_row_ts)
            hi = bisect.bisect_left(rows, before, lo, n, key=_row_ts) if before is not None else n
            filtered = rows[lo:hi]
        else:
            # Out of order (a clock stepped back) or a row without a usable ts: the full scan.
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

    def last_entry(self) -> dict[str, Any] | None:
        """The newest row appended (any kind), or None (Issue #1230 R1: the v4 shadow reads this cycle's row)."""
        return self._entries[-1] if self._entries else None

    def raw_entries(self) -> list[dict[str, Any]]:
        """A shallow copy of every retained row, undownsampled (Issue #1230 R1: v4 fit and accuracy page).

        Rows are never mutated after append, so the copy is safe to hand to an executor thread.
        """
        return list(self._entries)

    @property
    def entry_count(self) -> int:
        """Return number of entries currently in memory."""
        return len(self._entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _note_order(self, entries: list[dict[str, Any]], timestamps: list[datetime | None]) -> None:
        """Record the time order of ``entries`` from timestamps a full pass already parsed (load / prune)."""
        ordered, last = _order_after(None, timestamps)
        self._order = (entries, len(entries) if ordered else -1, last)

    def _time_sorted_rows(self, *, check_unknown: bool = True) -> tuple[list[dict[str, Any]], int] | None:
        """Issue #1598: ``(rows, n)`` when the first ``n`` rows (all of them, now) are time-sorted, else ``None``.

        Only rows appended since the last check are parsed (normally none or one); a list the log has not seen
        (assigned directly) is checked once in full. Rows are only ever appended, never edited, so a verified prefix
        stays valid. Safe from an executor thread: ``rows`` is a stable reference and ``n`` bounds the bisection.
        ``check_unknown=False`` (the prune, Issue #1599) returns ``None`` instead of checking a list the log has no
        record of, since the prune's own full walk then records it.
        """
        rows = self._entries
        n = len(rows)
        known, verified, last = self._order
        if known is not rows or verified > n:
            if not check_unknown:
                return None
            verified, last = 0, None
        if verified < 0:
            return None
        if verified < n:
            ordered, last = _order_after(last, (_row_ts(e) for e in rows[verified:n]))
            if not ordered:
                self._order = (rows, -1, None)
                return None
            self._order = (rows, n, last)
        return rows, n

    def sorted_rows_view(self) -> tuple[list[dict[str, Any]], int] | None:
        """``(rows, n)``: the first ``n`` rows, known to be time-sorted (see :meth:`_time_sorted_rows`), or ``None``."""
        return self._time_sorted_rows()

    def rows_between(self, lo: float, hi: float) -> list[dict[str, Any]] | None:
        """Issue #1730: the rows with ``lo <= ts <= hi`` (epoch seconds) by bisection of the time-sorted log, in log
        order; ``None`` when the log is out of order (or a probed row has no offset): the caller scans every row, as
        ``get_entries`` does. A superset-provider (see :func:`epoch_window`): the caller re-applies its own filter."""
        view = self._time_sorted_rows()
        return epoch_window(view[0], view[1], lo, hi) if view is not None else None

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
            lower_vals = [e["lower"] for e in group if e.get("lower") is not None]
            upper_vals = [e["upper"] for e in group if e.get("upper") is not None]
            nat_vent_target_vals = [e["nat_vent_target"] for e in group if e.get("nat_vent_target") is not None]
            comfort_target = _bucket_comfort_target(group)
            indoor_sensors = _bucket_indoor_sensors(group)

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
                "lower": round(sum(lower_vals) / len(lower_vals), 1) if lower_vals else None,
                "upper": round(sum(upper_vals) / len(upper_vals), 1) if upper_vals else None,
                "nat_vent_target": (
                    round(sum(nat_vent_target_vals) / len(nat_vent_target_vals), 1) if nat_vent_target_vals else None
                ),
            }
            if comfort_target is not None:
                summary["comfort_target"] = comfort_target
            if indoor_sensors is not None:
                summary["indoor_sensors"] = indoor_sensors
            summary.update(_bucket_hold(group))
            summary.update(_bucket_context(group))
            summary.update(_bucket_optional_means(group, HUMIDITY_FEELSLIKE_KEYS))
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
            lower_vals = [e["lower"] for e in group if e.get("lower") is not None]
            upper_vals = [e["upper"] for e in group if e.get("upper") is not None]
            nat_vent_target_vals = [e["nat_vent_target"] for e in group if e.get("nat_vent_target") is not None]
            comfort_target = _bucket_comfort_target(group)
            indoor_sensors = _bucket_indoor_sensors(group)

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
                "lower": round(sum(lower_vals) / len(lower_vals), 1) if lower_vals else None,
                "upper": round(sum(upper_vals) / len(upper_vals), 1) if upper_vals else None,
                "nat_vent_target": (
                    round(sum(nat_vent_target_vals) / len(nat_vent_target_vals), 1) if nat_vent_target_vals else None
                ),
            }
            if comfort_target is not None:
                summary["comfort_target"] = comfort_target
            if indoor_sensors is not None:
                summary["indoor_sensors"] = indoor_sensors
            summary.update(_bucket_hold(group))
            summary.update(_bucket_context(group))
            summary.update(_bucket_optional_means(group, HUMIDITY_FEELSLIKE_KEYS))
            if events:
                summary["events"] = events
            result.append(summary)

        return result
