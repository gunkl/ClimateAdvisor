"""Archive of what prediction engine v4 said, per sensor and look-ahead (Issue #1230 R1).

Shadow data only: nothing in a decision path reads it. It exists so v4's forecasts can later be scored against
what the house then did (the "Prediction accuracy" preview page, ``prediction_accuracy.py``).

Same first-write-wins rule as the v3 ``_pred_archive`` (docs/chart-log-spec.md): the value kept for a 30-minute slot
and look-ahead is the one written when the slot was that far ahead, and a later prediction for the same slot never
overwrites it. Every slot is filled at every look-ahead (Issue #1423): a slot no cycle had within 15 minutes of the
look-ahead (two cycles more than 30 minutes apart) takes the closer of the two neighbouring cycles' values, within
``fill_tolerance_s``; the newest cycle's curves are kept (and persisted as ``"last"``) for that.

In memory (R1b, M4) each sensor line is columnar: a base epoch plus one ``array('f')`` per look-ahead, indexed by
``(epoch - base) / 30 min`` with NaN for "not written". A line costs 4 bytes per slot and look-ahead (a 400-day line
is about 300 KB; the former dict-per-slot layout cost ~500 bytes a slot). The ``data`` property still presents the
legacy shape ``sensor -> epoch_30min -> {"1h": v, ...}`` (a materialised copy) for tools and tests::

    data: dict[str, dict[int, dict[str, float]]]

``sensor`` is an indoor entity id, or ``"_ca"`` for the reading Climate Advisor itself uses. While the model has a
primary NODE line, ``"_ca"`` is NOT stored: it is derived on read from the node lines - the sleep-sensor node's value
for a slot inside the sleep window when that node exists, else the primary node's - using the ``ca`` source the
coordinator configures (``set_ca_source``). A ``_ca``-only model (``primary == "_ca"``, what the fit picks when no
entity sensor has enough history; Issue #1400) has no node line to derive from, so its ``"_ca"`` predictions ARE
stored as their own line - otherwise nothing would ever be archived for it. When a later model gains a primary node,
the stored ``"_ca"`` slots the node line also covers are dropped (derivable again) and the older ones are kept, so the
accuracy history is not lost. Temperatures are internal degrees F, rounded to 2 dp. ``epoch_30min`` is the slot's Unix
time rounded to 30 minutes.

On disk (``climate_advisor_v4_archive_<entry_id>.json``, compact: ~45 bytes a slot; the format is unchanged, a
``"_ca"`` line from an older file is read and dropped once the source is known, the optional ``"ca"`` object records
the derivation)::

    {"v": 1, "leads": ["1h", "4h", "12h", "24h"],
     "sensors": {"sensor.hall": {"1768000000": [68.1, 68.4, null, null]}},
     "ca": {"primary": "sensor.hall", "sleep": "sensor.bed", "sleep_time": "22:30", "wake_time": "07:00", "tz": "..."}}

Persistence contract mirrors ``ForecastArchive`` / ``ChartStateLog``: ``load()`` / ``save()`` are blocking (callers
MUST run them via the executor), ``save()`` is an atomic tempfile + ``os.replace`` with ``chmod 0o600`` on non-Windows,
retention is time-based (``V4_ARCHIVE_MAX_DAYS``), pruning runs at most once per hour, and no public method raises.
"""

from __future__ import annotations

import bisect
import contextlib
import heapq
import json
import logging
import math
import os
import sys
import tempfile
from array import array
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import store_json
from .const import (
    V3_ARCHIVE_FILE,
    V3_ARCHIVE_LEADS,
    V4_ARCHIVE_FILE,
    V4_ARCHIVE_LEADS,
    V4_ARCHIVE_MAX_DAYS,
    V4_MODEL_FILE,
)
from .storage_paths import resolve_entry_scoped_path

_LOGGER = logging.getLogger(__name__)

_SLOT_S = 1800
_FORMAT_VERSION = 1
_NAN = float("nan")
CA_LINE = "_ca"
_FILL_MAX_GAP_S = 24 * 3600  # a previous curve older than this cannot fill anything (it ends 24 h after it was made)


def slot_key(dt: datetime) -> int:
    """Unix time of the 30-minute slot nearest to ``dt`` (a cycle that fires a few seconds early still lands right)."""
    return int(round(dt.timestamp() / _SLOT_S)) * _SLOT_S


class _Line:
    """One sensor's predictions: ``base`` epoch + a float32 column per look-ahead (NaN = never written)."""

    __slots__ = ("base", "cols")

    def __init__(self, leads: tuple[str, ...] = V4_ARCHIVE_LEADS) -> None:
        self.base: int | None = None
        self.cols: dict[str, array] = {lead: array("f") for lead in leads}

    def __len__(self) -> int:
        return len(next(iter(self.cols.values()))) if self.cols else 0

    def _index(self, epoch: int, grow: bool) -> int | None:
        if self.base is None:
            if not grow:
                return None
            self.base = epoch
        i = (epoch - self.base) // _SLOT_S
        if i < 0:
            if not grow:
                return None
            for col in self.cols.values():
                col[:0] = array("f", [_NAN]) * (-i)
            self.base = epoch
            i = 0
        n = len(self)
        if i >= n:
            if not grow:
                return None
            for col in self.cols.values():
                col.extend(array("f", [_NAN]) * (i + 1 - n))
        return i

    def get(self, epoch: int, lead: str) -> float | None:
        i = self._index(epoch, False)
        col = self.cols.get(lead)
        if i is None or col is None:
            return None
        v = col[i]
        return None if v != v else round(v, 2)

    def put_if_absent(self, epoch: int, lead: str, value: float) -> bool:
        if lead not in self.cols:
            return False
        i = self._index(epoch, True)
        assert i is not None
        col = self.cols[lead]
        if col[i] == col[i]:  # already written
            return False
        col[i] = value
        return True

    def slots(self) -> list[int]:
        """Epochs with at least one value, ascending."""
        if self.base is None:
            return []
        n = len(self)
        cols = list(self.cols.values())
        return [self.base + i * _SLOT_S for i in range(n) if any(c[i] == c[i] for c in cols)]

    def _window(self, start: float, end: float) -> tuple[int, int]:
        """Index range ``[i0, i1)`` of the slots with ``start <= epoch < end``, from base and step (no scan)."""
        if self.base is None:
            return 0, 0
        n = len(self)
        top = self.base + n * _SLOT_S
        if not (start < top and end > self.base and end > start):  # also false for NaN
            return 0, 0
        i0 = 0 if start <= self.base else math.ceil((start - self.base) / _SLOT_S)
        i1 = n if end >= top else math.ceil((end - self.base) / _SLOT_S)
        return i0, max(i0, i1)

    def iter_slots(self, start: float = -math.inf, end: float = math.inf, *, reverse: bool = False) -> Iterator[int]:
        """Epochs in ``[start, end)`` with at least one value, ascending (or descending), produced lazily."""
        if self.base is None:
            return
        i0, i1 = self._window(start, end)
        cols = list(self.cols.values())
        for i in range(i1 - 1, i0 - 1, -1) if reverse else range(i0, i1):
            if any(c[i] == c[i] for c in cols):
                yield self.base + i * _SLOT_S

    def series(self, lead: str, start: float, end: float) -> dict[int, float]:
        """``{epoch: value}`` for one look-ahead with ``start <= epoch < end``; only the window is visited."""
        col = self.cols.get(lead)
        if col is None or self.base is None:
            return {}
        i0, i1 = self._window(start, end)
        base = self.base
        return {base + i * _SLOT_S: round(v, 2) for i, v in enumerate(col[i0:i1], i0) if v == v}

    def span(self, lead: str) -> tuple[int, int] | None:
        """``(first, last)`` epoch holding a value for ``lead``; scans only the leading/trailing NaN run."""
        col = self.cols.get(lead)
        if col is None or self.base is None:
            return None
        first = next((i for i, v in enumerate(col) if v == v), None)
        if first is None:
            return None
        last = next(i for i in range(len(col) - 1, first - 1, -1) if col[i] == col[i])
        return self.base + first * _SLOT_S, self.base + last * _SLOT_S

    def drop_before(self, cutoff: float) -> int:
        """Remove slots older than ``cutoff``; returns how many slot positions were dropped."""
        if self.base is None:
            return 0
        k = min(len(self), max(0, int(math.ceil((cutoff - self.base) / _SLOT_S))))
        if k <= 0:
            return 0
        for col in self.cols.values():
            del col[:k]
        self.base += k * _SLOT_S
        return k

    def drop_from(self, cutoff: float) -> int:
        """Remove slots at or after ``cutoff``; returns how many slot positions were dropped."""
        if self.base is None:
            return 0
        n = len(self)
        keep = min(n, max(0, int(math.ceil((cutoff - self.base) / _SLOT_S))))
        if keep >= n:
            return 0
        for col in self.cols.values():
            del col[keep:]
        if keep == 0:
            self.base = None
        return n - keep

    def nbytes(self) -> int:
        return sum(c.buffer_info()[1] * c.itemsize for c in self.cols.values())


class V4Archive:
    """Rolling, persisted, first-write-wins archive of v4 predictions.

    ``leads`` / ``path`` let another engine keep the same kind of archive in its own file (``V3Archive``, Issue
    #1492); the defaults are v4's.
    """

    def __init__(
        self,
        config_dir: Path,
        max_days: int = V4_ARCHIVE_MAX_DAYS,
        entry_id: str = "",
        *,
        leads: tuple[str, ...] = V4_ARCHIVE_LEADS,
        path: Path | None = None,
    ) -> None:
        self._entry_id = entry_id
        self._leads = tuple(leads)
        # Entry-scoped like the other zone files (Issue #796); a brand-new file, so no legacy file to migrate.
        self._path = path if path is not None else resolve_entry_scoped_path(config_dir, V4_ARCHIVE_FILE, entry_id)
        self._file_prefix = self._path.stem + "_"
        self._max_days = max_days
        self._lines: dict[str, _Line] = {}
        self.ca: dict[str, str | None] = {}  # how "_ca" is derived (see module docstring)
        self._last_prune: float | None = None
        # The newest cycle's stored curves, (epoch, {sensor: [(epoch, temp_f)]}), so the next cycle can fill the slots
        # between their look-ahead targets (Issue #1423). Persisted as the file's optional "last" object.
        self._last: tuple[float, dict[str, list[tuple[float, float]]]] | None = None
        self.dirty = False

    # ------------------------------------------------------------------
    # The "_ca" derivation
    # ------------------------------------------------------------------

    def set_ca_source(
        self,
        primary: str | None,
        *,
        sleep: str | None = None,
        sleep_time: str | None = None,
        wake_time: str | None = None,
        tz: str | None = None,
    ) -> None:
        """Say which node lines make up ``"_ca"``; a line named ``"_ca"`` kept from an older file is then dropped."""
        new = {"primary": primary, "sleep": sleep, "sleep_time": sleep_time, "wake_time": wake_time, "tz": tz}
        if new != self.ca:
            self.ca = new
            self.dirty = True
        if self._trim_stored_ca():
            self.dirty = True

    def _trim_stored_ca(self) -> bool:
        """Drop the stored ``"_ca"`` slots the primary node line also covers (derivable). True when anything changed.

        Slots older than the node line (written while the model was ``_ca``-only, Issue #1400) are kept: deriving cannot
        recreate them, and they are the live accuracy history of that time.
        """
        primary = self.ca.get("primary")
        stored = self._lines.get(CA_LINE)
        node = self._lines.get(primary or "")
        if stored is None or node is None or primary == CA_LINE or node.base is None:
            return False
        dropped = stored.drop_from(node.base)
        if not stored.slots():
            del self._lines[CA_LINE]
            return True
        return dropped > 0

    def _ca_active(self) -> bool:
        p = self.ca.get("primary")
        return bool(p) and p != CA_LINE and p in self._lines

    def _ca_value(self, epoch: int, lead: str) -> float | None:
        primary = self.ca.get("primary")
        sleep = self.ca.get("sleep")
        if sleep and sleep != primary and self._in_sleep(epoch):
            v = self._lines[sleep].get(epoch, lead) if sleep in self._lines else None
            if v is not None:
                return v
            # Issue #1432: a sleep slot the bedroom line cannot answer is never the thermostat's room (a room-node
            # primary); a stored ``_ca`` slot (a stitched model's own CA reading) still answers it.
            if primary and primary != CA_LINE:
                stored = self._lines.get(CA_LINE)
                return stored.get(epoch, lead) if stored is not None else None
        line = self._lines.get(primary or "")
        v = line.get(epoch, lead) if line is not None else None
        if v is None and CA_LINE in self._lines:  # a slot from a _ca-only model (Issue #1400)
            v = self._lines[CA_LINE].get(epoch, lead)
        return v

    def _ca_slots(self) -> list[int]:
        """Slots the derived ``"_ca"`` can answer: the primary node's plus any stored ``"_ca"`` line's (ascending)."""
        slots = set(self._lines[str(self.ca["primary"])].slots())
        if CA_LINE in self._lines:
            slots.update(self._lines[CA_LINE].slots())
        return sorted(slots)

    def _in_sleep(self, epoch: int) -> bool:
        st, wt, tzname = self.ca.get("sleep_time"), self.ca.get("wake_time"), self.ca.get("tz")
        if not st or not wt or not tzname:
            return False
        try:
            local = datetime.fromtimestamp(epoch, ZoneInfo(tzname))
        except Exception:  # noqa: BLE001 - an odd zone name just disables the composite
            return False
        try:
            from . import predictor_v4  # noqa: PLC0415 - lazy: a broken engine must not break the archive

            return predictor_v4.in_sleep_window(local, st, wt)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Legacy view
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict[str, dict[int, dict[str, float]]]:
        """Materialised ``sensor -> epoch -> {lead: value}`` incl. derived ``"_ca"``; a slow copy for tools/tests."""
        out: dict[str, dict[int, dict[str, float]]] = {}
        for sensor, line in list(self._lines.items()):
            rows = self._rows(sensor, line.slots())
            if rows:
                out[sensor] = rows
        if self._ca_active():
            out.pop(CA_LINE, None)
            rows = self._rows(CA_LINE, self._ca_slots())
            if rows:
                out[CA_LINE] = rows
        return out

    @data.setter
    def data(self, value: dict[str, dict[int, dict[str, float]]]) -> None:
        self._lines = {}
        for sensor, rows in value.items():
            line = self._lines.setdefault(sensor, _Line(self._leads))
            for epoch, vals in rows.items():
                for lead, v in vals.items():
                    line.put_if_absent(int(epoch), lead, float(v))

    def put(self, sensor: str, epoch: int, lead: str, value: float) -> bool:
        """Store one value (first write wins); True when new. For tools and tests (``record`` is the live path)."""
        added = self._lines.setdefault(sensor, _Line(self._leads)).put_if_absent(int(epoch), lead, float(value))
        self.dirty = self.dirty or added
        return added

    def _rows(self, sensor: str, slots: list[int]) -> dict[int, dict[str, float]]:
        rows: dict[int, dict[str, float]] = {}
        for epoch in slots:
            vals = {}
            for lead in self._leads:
                derived = sensor == CA_LINE and self._ca_active()
                v = self._ca_value(epoch, lead) if derived else self._lines[sensor].get(epoch, lead)
                if v is not None:
                    vals[lead] = v
            if vals:
                rows[epoch] = vals
        return rows

    # ------------------------------------------------------------------
    # Persistence (blocking - run via the executor)
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from disk, dropping invalid or expired slots. A missing or corrupt file starts empty. Never raises."""
        try:
            self._load()
        except Exception:  # noqa: BLE001 - the archive must never break startup
            _LOGGER.warning("v4_archive: unexpected error loading archive, starting fresh", exc_info=True)
            self._lines = {}

    def _load(self) -> None:
        self._lines = {}
        self._last = None
        self.dirty = False
        if not self._path.exists():
            _LOGGER.info("v4_archive: no archive file yet, starting empty")
            return
        try:
            raw = store_json.loads(self._path.read_bytes())
        except (OSError, ValueError) as err:
            _LOGGER.warning("v4_archive: failed to read archive file, starting fresh: %s", err)
            return
        sensors = raw.get("sensors") if isinstance(raw, dict) else None
        leads = raw.get("leads") if isinstance(raw, dict) else None
        if not isinstance(sensors, dict) or not isinstance(leads, list):
            _LOGGER.warning("v4_archive: archive file has an unexpected shape, starting fresh")
            return
        ca = raw.get("ca")
        if isinstance(ca, dict):
            self.ca = {k: (v if isinstance(v, str) else None) for k, v in ca.items()}
        self._last = _parse_last(raw.get("last"))
        cutoff = datetime.now().timestamp() - self._max_days * 86400
        slots = 0
        for sensor, rows in sensors.items():
            if not isinstance(sensor, str) or not isinstance(rows, dict):
                continue
            line, wrote = _parse_line(rows, leads, self._leads, cutoff)
            slots += wrote
            if line is not None:
                self._lines[sensor] = line
        self._trim_stored_ca()  # derivable slots: do not hold the duplicate (older _ca-only slots stay)
        _LOGGER.info("v4_archive: loaded %d slots across %d sensor lines", slots, len(self._lines))

    def save(self) -> bool:
        """Atomic write to disk (tempfile + os.replace), 0o600 on non-Windows. True on success. Never raises."""
        try:
            leads = list(self._leads)
            sensors = {}
            for sensor, line in list(self._lines.items()):
                sensors[sensor] = {str(epoch): [line.get(epoch, lead) for lead in leads] for epoch in line.slots()}
            payload: dict = {"v": _FORMAT_VERSION, "leads": leads, "sensors": sensors}
            if self.ca:
                payload["ca"] = dict(self.ca)
            last = self._last
            if last is not None:  # the newest cycle's curves, for the gap fill after a restart (Issue #1423)
                payload["last"] = {
                    "at": round(last[0], 3),
                    "sensors": {sid: [[round(t, 3), v] for t, v in pts] for sid, pts in list(last[1].items())},
                }
            serialized = store_json.dumps(payload, depth=3)  # one slot per line: no long single GIL hold (Issue #1730)
        except (TypeError, ValueError, RuntimeError) as err:
            _LOGGER.error("v4_archive: failed to serialize archive: %s", err)
            return False

        tmp_path_str: str | None = None
        try:
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self._path.parent, prefix=self._file_prefix, suffix=".tmp")
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            tmp_path_str = None
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
            self.dirty = False
            return True
        except OSError as err:
            _LOGGER.error("v4_archive: failed to save archive file: %s", err)
            if tmp_path_str is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path_str)
        return False

    # ------------------------------------------------------------------
    # Recording (memory only)
    # ------------------------------------------------------------------

    def record(self, now: datetime, predictions: dict[str, list[tuple[datetime, float]]], primary: str | None) -> int:
        """Archive the 1/4/12/24 h values of one cycle's predictions, first write wins. Returns values newly added.

        ``predictions`` is ``predict()``'s result ``{sensor: [(ts, temp_f), ...]}``. For each look-ahead the
        point nearest ``now + lead`` (within 10 minutes) is stored under the slot nearest its time, and the empty slots
        between the previous cycle's target and this one's are filled (``_fill``, Issue #1423). ``"_ca"`` is not
        stored while it is derivable (``primary`` is a node this cycle predicted); for a ``_ca``-only model
        (``primary == "_ca"``, or no primary node line predicted) it is stored as its own line (Issue #1400).
        ``primary`` seeds the ``"_ca"`` source when none was configured. Never raises.
        """
        try:
            return self._record(now, predictions, primary)
        except Exception:  # noqa: BLE001 - shadow data must never break a coordinator cycle
            _LOGGER.warning("v4_archive: failed to record predictions", exc_info=True)
            return 0

    def _record(self, now: datetime, predictions: dict[str, list[tuple[datetime, float]]], primary: str | None) -> int:
        if primary is not None and not self.ca.get("primary"):
            self.set_ca_source(primary)
        now_ts = now.timestamp()
        added = 0
        # "_ca" is derivable only from a primary NODE line this cycle also predicts; otherwise store it (Issue #1400).
        ca_derivable = bool(primary) and primary != CA_LINE and bool(predictions.get(primary or ""))
        curves: dict[str, list[tuple[float, float]]] = {}
        for sensor, points in predictions.items():
            if not points or (sensor == CA_LINE and ca_derivable):
                continue
            curve = sorted(
                (ts.timestamp(), round(float(temp), 2))
                for ts, temp in points
                if isinstance(temp, int | float) and not isinstance(temp, bool) and math.isfinite(temp)
            )
            if curve:
                curves[sensor] = curve
        prev = self._last
        prev_ok = prev is not None and 0 < now_ts - prev[0] <= _FILL_MAX_GAP_S
        for sensor, curve in curves.items():
            prev_curve = prev[1].get(sensor) if prev_ok and prev is not None else None
            for lead in self._leads:
                lead_s = _lead_seconds(lead)
                target = now_ts + lead_s
                near = _nearest(curve, target, 600)
                if near is not None:
                    line = self._lines.setdefault(sensor, _Line(self._leads))
                    if line.put_if_absent(_slot_of(near[0]), lead, near[1]):
                        added += 1
                if prev_curve:
                    assert prev is not None
                    added += self._fill(sensor, lead, lead_s, prev[0], prev_curve, now_ts, curve)
        if curves:
            self._last = (now_ts, curves)
            self.dirty = True
        if primary is not None and primary != self.ca.get("primary") and primary in self._lines:
            self.set_ca_source(primary, **{k: v for k, v in self.ca.items() if k != "primary"})
        if added:
            self.dirty = True
            self._maybe_prune(now_ts)
        return added

    def _fill(
        self,
        sensor: str,
        lead: str,
        lead_s: float,
        prev_at: float,
        prev_curve: list[tuple[float, float]],
        now_ts: float,
        curve: list[tuple[float, float]],
    ) -> int:
        """Fill the slots no cycle had exactly ``lead`` ahead between the previous cycle and this one (Issue #1423).

        A cycle writes the one slot nearest ``now + lead``. When two cycles are more than 30 minutes apart (a skipped
        or lost cycle, a restart, a refresh that moved the 30-minute timer), the slots between their targets were never
        that far ahead of ANY cycle and stayed empty for good. Each such empty slot gets the value from whichever of the
        two curves (the previous cycle's, this one's) was closest to ``lead`` ahead of it, if that is within
        ``fill_tolerance_s(lead)``; an outage longer than that stays an honest hole. Never overwrites (first write
        wins). Returns values added."""
        tol = fill_tolerance_s(lead)
        added = 0
        for slot in range(_slot_of(prev_at + lead_s), _slot_of(now_ts + lead_s) + 1, _SLOT_S):
            line = self._lines.get(sensor)
            if line is not None and line.get(slot, lead) is not None:
                continue
            best: tuple[float, int, float] | None = None
            for order, (at, pts) in enumerate(((prev_at, prev_curve), (now_ts, curve))):
                near = _nearest(pts, slot, 600)
                if near is None:
                    continue
                err = abs((near[0] - at) - lead_s)  # how far ahead this value really was
                if best is None or (err, order) < best[:2]:
                    best = (err, order, near[1])
            if (
                best is not None
                and best[0] <= tol
                and self._lines.setdefault(sensor, _Line(self._leads)).put_if_absent(slot, lead, best[2])
            ):
                added += 1
        return added

    def _maybe_prune(self, now_ts: float) -> None:
        """Evict slots older than the retention; at most once per hour."""
        if self._last_prune is not None and now_ts - self._last_prune < 3600:
            return
        self._last_prune = now_ts
        cutoff = now_ts - self._max_days * 86400
        dropped = 0
        for sensor in list(self._lines):
            line = self._lines[sensor]
            dropped += line.drop_before(cutoff)
            if not line.slots():
                del self._lines[sensor]
        if dropped:
            self.dirty = True
            _LOGGER.debug("v4_archive: pruned %d expired slots", dropped)

    def sensor_ids(self) -> list[str]:
        """Sensor lines held (``"_ca"`` first when derivable, then the node entity ids)."""
        has_ca = self._ca_active() or CA_LINE in self._lines  # derived, or stored by a _ca-only model (Issue #1400)
        return ([CA_LINE] if has_ca else []) + [s for s in self._lines if s != CA_LINE]

    def memory_bytes(self) -> int:
        """Bytes held by the sensor columns (not counting object overhead)."""
        return sum(line.nbytes() for line in self._lines.values())

    def slot_count(self) -> dict[str, int]:
        """``{sensor: slots}`` (``"_ca"`` included when derivable) for the Debug tab."""
        out = {s: len(line.slots()) for s, line in self._lines.items()}
        if self._ca_active():
            out[CA_LINE] = len(self._ca_slots())
        return out

    def sensors_with_slots(self) -> set[str]:
        """Sensor ids holding at least one slot (``"_ca"`` when derivable): ``slot_count()``'s non-zero keys, found by
        looking for one slot per line instead of listing them all (Issue #1730)."""
        out = {s for s, line in list(self._lines.items()) if next(line.iter_slots(), None) is not None}
        if self._ca_active():
            if next(self._ca_candidates(-math.inf, math.inf), None) is not None:
                out.add(CA_LINE)
            else:
                out.discard(CA_LINE)
        return out

    def oldest_slot(self) -> int | None:
        """The earliest archived slot (Unix seconds) across every sensor, or None while the archive is empty."""
        firsts = [next(line.iter_slots(), None) for line in list(self._lines.values())]
        return min((f for f in firsts if f is not None), default=None)

    def _ca_candidates(self, start: float, end: float, *, reverse: bool = False) -> Iterator[int]:
        """Slots in ``[start, end)`` the derived ``"_ca"`` can answer: the primary node's and a stored ``"_ca"``'s."""
        feeds = [self._lines[str(self.ca["primary"])].iter_slots(start, end, reverse=reverse)]
        if CA_LINE in self._lines:
            feeds.append(self._lines[CA_LINE].iter_slots(start, end, reverse=reverse))
        last = None
        for epoch in heapq.merge(*feeds, reverse=reverse):
            if epoch != last:
                last = epoch
                yield epoch

    def series(self, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        """``{slot_epoch: value}`` for one sensor and look-ahead with ``start <= slot < end`` (Unix seconds).

        Only the window is visited (the index range comes from the line's base and the 30-minute step), so a short
        window of a year-long line costs the window, not the line; ``-inf`` / ``inf`` give the whole line.
        Safe from an executor thread while the event loop records: it reads fixed-size columns by index.
        """
        if sensor == CA_LINE and self._ca_active():
            out: dict[int, float] = {}
            for epoch in self._ca_candidates(start, end):
                v = self._ca_value(epoch, lead)
                if v is not None:
                    out[epoch] = v
            return out
        line = self._lines.get(sensor)
        return line.series(lead, start, end) if line is not None else {}

    def series_leads(self, sensor: str, leads: Sequence[str], start: float, end: float) -> dict[str, dict[int, float]]:
        """``{lead: series(sensor, lead, start, end)}`` for several look-aheads in one pass (Issue #1730).

        The derived ``"_ca"`` picks, once per slot, the line each look-ahead reads (the bedroom line in the sleep
        window, else the primary node's; a stored ``"_ca"`` line answers what that one cannot), not once per look-ahead.
        """
        if not (sensor == CA_LINE and self._ca_active()):
            return {lead: self.series(sensor, lead, start, end) for lead in leads}
        out: dict[str, dict[int, float]] = {lead: {} for lead in leads}
        primary, sleep = self.ca.get("primary"), self.ca.get("sleep")
        by_sleep = bool(sleep) and sleep != primary
        views = [  # (base, slots, columns) per line, read once: what _Line.get does per value
            (line.base, len(line), line.cols) if line is not None and line.base is not None else None
            for line in (
                self._lines.get(str(primary)),
                self._lines.get(str(sleep)) if by_sleep else None,
                self._lines.get(CA_LINE),
            )
        ]
        node, sleep_line, stored = views
        wanted = [(lead, out[lead]) for lead in leads]
        for epoch in self._ca_candidates(start, end):
            first = sleep_line if by_sleep and self._in_sleep(epoch) else node
            i1 = i2 = None
            if first is not None:
                i = (epoch - first[0]) // _SLOT_S
                i1 = i if 0 <= i < first[1] else None
            if stored is not None:
                i = (epoch - stored[0]) // _SLOT_S
                i2 = i if 0 <= i < stored[1] else None
            first_cols = first[2] if first is not None else {}
            stored_cols = stored[2] if stored is not None else {}
            for lead, dest in wanted:
                v = None
                col = first_cols.get(lead)
                if i1 is not None and col is not None and col[i1] == col[i1]:
                    v = col[i1]
                else:
                    col = stored_cols.get(lead)
                    if i2 is not None and col is not None and col[i2] == col[i2]:
                        v = col[i2]
                if v is not None:
                    dest[epoch] = round(v, 2)
        return out

    def span(self, sensor: str, lead: str) -> tuple[int, int] | None:
        """``(first, last)`` slot holding a value for ``sensor`` at ``lead``, or None; no whole-line scan."""
        if sensor == CA_LINE and self._ca_active():
            ends = [
                next(
                    (
                        e
                        for e in self._ca_candidates(-math.inf, math.inf, reverse=rev)
                        if self._ca_value(e, lead) is not None
                    ),
                    None,
                )
                for rev in (False, True)
            ]
            return (ends[0], ends[1]) if ends[0] is not None and ends[1] is not None else None
        line = self._lines.get(sensor)
        return line.span(lead) if line is not None else None

    def snapshot(self) -> dict[str, dict[int, dict[str, float]]]:
        """A copy of the archive in the legacy dict shape that is safe to read from another thread."""
        return self.data


class V3Archive(V4Archive):
    """What v3 (today's ODE prediction) said 1/4/12/24 h ahead for the reading Climate Advisor uses (Issue #1492).

    Occupant view: lets the Prediction accuracy scorecard compare v4 with v3 at every look-ahead; before, only v3's 4 h
    value was kept (chart-log ``pred_indoor``). Same format, first-write-wins rule, gap fill, retention and
    persistence contract as ``V4Archive``, in its own entry-scoped ``V3_ARCHIVE_FILE``; one stored line, ``"_ca"``.
    Record-only: no decision reads it.
    """

    def __init__(self, config_dir: Path, max_days: int = V4_ARCHIVE_MAX_DAYS, entry_id: str = "") -> None:
        super().__init__(
            config_dir,
            max_days,
            entry_id,
            leads=V3_ARCHIVE_LEADS,
            path=resolve_entry_scoped_path(config_dir, V3_ARCHIVE_FILE, entry_id),
        )


def _lead_seconds(lead: str) -> float:
    return float(lead.rstrip("h")) * 3600.0


def _finite(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _parse_last(raw: object) -> tuple[float, dict[str, list[tuple[float, float]]]] | None:
    """The file's optional ``"last"`` object -> ``(epoch, {sensor: sorted [(epoch, temp_f)]})``; odd -> None."""
    if not isinstance(raw, dict) or not _finite(raw.get("at")) or not isinstance(raw.get("sensors"), dict):
        return None
    curves: dict[str, list[tuple[float, float]]] = {}
    for sid, pts in raw["sensors"].items():
        if not isinstance(sid, str) or not isinstance(pts, list):
            continue
        curve = sorted(
            (float(p[0]), float(p[1]))
            for p in pts
            if isinstance(p, list) and len(p) == 2 and _finite(p[0]) and _finite(p[1])
        )
        if curve:
            curves[sid] = curve
    return (float(raw["at"]), curves) if curves else None


def _parse_line(rows: dict, file_leads: list, leads: tuple[str, ...], cutoff: float) -> tuple[_Line | None, int]:
    """One sensor's stored rows -> ``(line | None, slots_written)``, as ``put_if_absent`` per value would build it.

    Columnar (Issue #1730): the rows are written straight into float32 columns, lead by lead, instead of one method call
    (with a bounds check and a possible resize) per value. Same rules: a row needs an integer-like key at or after
    ``cutoff`` and a list value; only a finite non-bool number in a lead this archive keeps is written; the first value
    for a slot and look-ahead wins; the line starts at the earliest slot that took a value and ends at the latest, so a
    row with no usable value never grows it. Epochs that do not sit on one 30-minute grid (a hand-edited file) take the
    per-value path, whose result depends on the order of the rows.
    """
    ep: list[int] = []
    vl: list[list] = []
    for key, vals in rows.items():
        try:
            epoch = int(key)
        except TypeError, ValueError:
            continue
        if epoch < cutoff or not isinstance(vals, list):
            continue
        ep.append(epoch)
        vl.append(vals)
    if not ep:
        return None, 0
    base0 = min(ep)
    if any((e - base0) % _SLOT_S for e in ep):
        return _parse_line_by_value(ep, vl, file_leads, leads)
    idx = [(e - base0) // _SLOT_S for e in ep]
    n = max(idx) + 1
    cols: dict[str, array] = {}
    wrote = bytearray(len(ep))
    for j, lead in enumerate(file_leads):
        lead = str(lead)
        if lead not in leads:
            continue
        col = cols.get(lead)
        if col is None:
            col = cols[lead] = array("f", [_NAN]) * n
        for r, (i, vals) in enumerate(zip(idx, vl, strict=True)):
            if j < len(vals):
                v = vals[j]
                t = type(v)
                if (t is float or t is int) and v - v == 0 and col[i] != col[i]:  # finite, not a bool, first write wins
                    col[i] = v
                    wrote[r] = 1
    written = sum(wrote)
    if not written:
        return None, 0
    used = [idx[r] for r in range(len(ep)) if wrote[r]]
    lo, hi = min(used), max(used) + 1
    line = _Line(leads)
    line.base = base0 + lo * _SLOT_S
    for lead in leads:
        col = cols.get(lead)
        line.cols[lead] = col[lo:hi] if col is not None else array("f", [_NAN]) * (hi - lo)
    return line, written


def _parse_line_by_value(
    ep: list[int], vl: list[list], file_leads: list, leads: tuple[str, ...]
) -> tuple[_Line | None, int]:
    """The per-value build (``put_if_absent`` for each value, rows in file order): what a load always did."""
    line = _Line(leads)
    slots = 0
    for epoch, vals in zip(ep, vl, strict=True):
        wrote = False
        for lead, v in zip(file_leads, vals, strict=False):
            if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v):
                wrote |= line.put_if_absent(epoch, str(lead), float(v))
        slots += 1 if wrote else 0
    return (line, slots) if len(line) else (None, slots)


def fill_tolerance_s(lead: str) -> float:
    """How far from ``lead`` a gap-filled slot's real look-ahead may be (Issue #1423): 30 min, or 1/8 of the lead."""
    return max(float(_SLOT_S), _lead_seconds(lead) / 8.0)


def _slot_of(epoch: float) -> int:
    return int(round(epoch / _SLOT_S)) * _SLOT_S


def _nearest(curve: list[tuple[float, float]], t: float, within: float) -> tuple[float, float] | None:
    """The point of a time-sorted ``[(epoch, temp)]`` curve nearest ``t``, if no further than ``within`` seconds."""
    i = bisect.bisect_left(curve, (t,))
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if (
            0 <= j < len(curve)
            and abs(curve[j][0] - t) <= within
            and (best is None or abs(curve[j][0] - t) < abs(best[0] - t))
        ):
            best = curve[j]
    return best


def lead_hours(lead: str) -> float:
    """Hours in a look-ahead label such as ``"4h"``."""
    return float(lead.rstrip("h"))


class V4ModelStore:
    """The fitted v4 model and its live hidden-node state, in their own entry-scoped file (Issue #1230 R1).

    Deliberately NOT part of the learning file: a rollback to a release that does not know the v4 fields would make
    ``LearningState(**data)`` reject the file and reset the owner's whole learning history. A separate file is simply
    ignored by the older release. A missing or corrupt file means "no model": the first fit runs.

    On disk (``climate_advisor_v4_model_<entry_id>.json``)::

        {"v": 1, "model": <predictor_v4.to_dict(...)> | null, "mass_f": 66.2 | null, "mass_at": iso | null,
         "last_fit": {"at", "reason", "seconds", "ok"} | null, "last_fit_error": str | null,
         "night_validation": <night_replay.validation_record(...)> | null,
         "unusable_fit": {"count", "at", "reason", "fit_reason", "fitted_at", "kept_fitted_at"} | null,
         "fit_attempt_at": iso | null}

    ``night_validation`` (Issue #1230 R6) is the nightly recorded-night replay that gates the bedroom night strategy
    (``night_strategy.NightValidation`` + the evidence hours); an older file without it simply reads as ``null``.
    ``unusable_fit`` (Issue #1529) records the last refit that came back unusable (confidence ``none``) while a usable
    ``model`` was kept for the decisions; ``null`` once a usable fit replaces the model (and in older files).
    ``fit_attempt_at`` (Issue #1722) is the wall-clock ISO time the last fit or night replay STARTED, saved before the
    work begins: a restart inside ``V4_FIT_RETRY_MIN_INTERVAL_S`` of it does not refit at once (a fit that the restart
    killed writes no ``last_fit``). Additive, so the format version stays 1: ``load`` reads every key with ``.get``
    and never checks ``v``, and an older release simply ignores the key (and drops it on its next save).

    Same persistence contract as ``V4Archive``: blocking load/save via the executor, atomic, 0o600, never raise.
    """

    def __init__(self, config_dir: Path, entry_id: str = "") -> None:
        self._path = resolve_entry_scoped_path(config_dir, V4_MODEL_FILE, entry_id)
        self.v4_model: dict | None = None
        self.v4_mass_f: float | None = None
        self.v4_mass_at: str | None = None
        self.last_fit: dict | None = None
        self.last_fit_error: str | None = None
        self.night_validation: dict | None = None
        self.unusable_fit: dict | None = None
        self.fit_attempt_at: str | None = None
        self.dirty = False

    def load(self) -> None:
        """Load from disk; missing, corrupt or odd content means no model. Never raises."""
        self.v4_model = self.v4_mass_f = self.v4_mass_at = self.last_fit = self.last_fit_error = None
        self.night_validation = None
        self.unusable_fit = None
        self.fit_attempt_at = None
        self.dirty = False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("v4_model: could not read the model file, a new fit will run: %s", err)
            return
        if not isinstance(raw, dict):
            _LOGGER.warning("v4_model: model file has an unexpected shape, a new fit will run")
            return
        model = raw.get("model")
        self.v4_model = model if isinstance(model, dict) else None
        mass = raw.get("mass_f")
        self.v4_mass_f = (
            float(mass)
            if isinstance(mass, int | float) and not isinstance(mass, bool) and math.isfinite(mass)
            else None
        )
        at = raw.get("mass_at")
        self.v4_mass_at = at if isinstance(at, str) else None
        last = raw.get("last_fit")
        self.last_fit = last if isinstance(last, dict) else None
        err = raw.get("last_fit_error")
        self.last_fit_error = err if isinstance(err, str) else None
        night = raw.get("night_validation")
        self.night_validation = night if isinstance(night, dict) else None
        unusable = raw.get("unusable_fit")
        self.unusable_fit = unusable if isinstance(unusable, dict) else None
        attempt = raw.get("fit_attempt_at")
        self.fit_attempt_at = attempt if isinstance(attempt, str) else None

    def save(self) -> bool:
        """Atomic write (tempfile + os.replace), 0o600 on non-Windows. True on success. Never raises."""
        tmp_path_str: str | None = None
        try:
            serialized = json.dumps(
                {
                    "v": _FORMAT_VERSION,
                    "model": self.v4_model,
                    "mass_f": self.v4_mass_f,
                    "mass_at": self.v4_mass_at,
                    "last_fit": self.last_fit,
                    "last_fit_error": self.last_fit_error,
                    "night_validation": self.night_validation,
                    "unusable_fit": self.unusable_fit,
                    "fit_attempt_at": self.fit_attempt_at,
                },
                separators=(",", ":"),
                allow_nan=False,
            )
            tmp_fd, tmp_path_str = tempfile.mkstemp(
                dir=self._path.parent, prefix="climate_advisor_v4_model_", suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            tmp_path_str = None
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
            self.dirty = False
            return True
        except (OSError, TypeError, ValueError) as err:
            _LOGGER.error("v4_model: failed to save the model file: %s", err)
            if tmp_path_str is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path_str)
            return False
