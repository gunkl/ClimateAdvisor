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
import json
import logging
import math
import os
import sys
import tempfile
from array import array
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .const import V4_ARCHIVE_FILE, V4_ARCHIVE_LEADS, V4_ARCHIVE_MAX_DAYS, V4_MODEL_FILE
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

    def __init__(self) -> None:
        self.base: int | None = None
        self.cols: dict[str, array] = {lead: array("f") for lead in V4_ARCHIVE_LEADS}

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
        return None if v != v else round(float(v), 2)

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
            line = self._lines.setdefault(sensor, _Line())
            for epoch, vals in rows.items():
                for lead, v in vals.items():
                    line.put_if_absent(int(epoch), lead, float(v))

    def put(self, sensor: str, epoch: int, lead: str, value: float) -> bool:
        """Store one value (first write wins); True when new. For tools and tests (``record`` is the live path)."""
        added = self._lines.setdefault(sensor, _Line()).put_if_absent(int(epoch), lead, float(value))
        self.dirty = self.dirty or added
        return added

    def _rows(self, sensor: str, slots: list[int]) -> dict[int, dict[str, float]]:
        rows: dict[int, dict[str, float]] = {}
        for epoch in slots:
            vals = {}
            for lead in V4_ARCHIVE_LEADS:
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
            raw = json.loads(self._path.read_text(encoding="utf-8"))
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
            line = _Line()
            for key, vals in rows.items():
                try:
                    epoch = int(key)
                except TypeError, ValueError:
                    continue
                if epoch < cutoff or not isinstance(vals, list):
                    continue
                wrote = False
                for lead, v in zip(leads, vals, strict=False):
                    if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v):
                        wrote |= line.put_if_absent(epoch, str(lead), float(v))
                slots += 1 if wrote else 0
            if len(line):
                self._lines[sensor] = line
        self._trim_stored_ca()  # derivable slots: do not hold the duplicate (older _ca-only slots stay)
        _LOGGER.info("v4_archive: loaded %d slots across %d sensor lines", slots, len(self._lines))

    def save(self) -> bool:
        """Atomic write to disk (tempfile + os.replace), 0o600 on non-Windows. True on success. Never raises."""
        try:
            leads = list(V4_ARCHIVE_LEADS)
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
            serialized = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError, RuntimeError) as err:
            _LOGGER.error("v4_archive: failed to serialize archive: %s", err)
            return False

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
            for lead in V4_ARCHIVE_LEADS:
                lead_s = _lead_seconds(lead)
                target = now_ts + lead_s
                near = _nearest(curve, target, 600)
                if near is not None:
                    line = self._lines.setdefault(sensor, _Line())
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
                and self._lines.setdefault(sensor, _Line()).put_if_absent(slot, lead, best[2])
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

    def oldest_slot(self) -> int | None:
        """The earliest archived slot (Unix seconds) across every sensor, or None while the archive is empty."""
        firsts = [slots[0] for slots in (line.slots() for line in self._lines.values()) if slots]
        return min(firsts) if firsts else None

    def series(self, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        """``{slot_epoch: value}`` for one sensor and look-ahead with ``start <= slot < end`` (Unix seconds).

        Safe from an executor thread while the event loop records: it reads fixed-size columns by index.
        """
        if sensor == CA_LINE and self._ca_active():
            out: dict[int, float] = {}
            for epoch in self._ca_slots():
                if start <= epoch < end:
                    v = self._ca_value(epoch, lead)
                    if v is not None:
                        out[epoch] = v
            return out
        line = self._lines.get(sensor)
        if line is None:
            return {}
        out = {}
        for epoch in line.slots():
            if start <= epoch < end:
                v = line.get(epoch, lead)
                if v is not None:
                    out[epoch] = v
        return out

    def snapshot(self) -> dict[str, dict[int, dict[str, float]]]:
        """A copy of the archive in the legacy dict shape that is safe to read from another thread."""
        return self.data


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
         "night_validation": <night_replay.validation_record(...)> | null}

    ``night_validation`` (Issue #1230 R6) is the nightly recorded-night replay that gates the bedroom night strategy
    (``night_strategy.NightValidation`` + the evidence hours); an older file without it simply reads as ``null``.

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
        self.dirty = False

    def load(self) -> None:
        """Load from disk; missing, corrupt or odd content means no model. Never raises."""
        self.v4_model = self.v4_mass_f = self.v4_mass_at = self.last_fit = self.last_fit_error = None
        self.night_validation = None
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
