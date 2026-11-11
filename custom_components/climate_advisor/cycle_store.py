"""Per-zone store of recorded heat/cool cycles (Issues #1512 / #1516): ``climate_advisor_cycles[_<entry_id>].json``.

Holds the raw cycles ``cycle_recorder.py`` produces so the pooled cycle model can refit from the stored cycles at any
time (a refit after a restart needs no in-flight state, and a better estimator can re-read the same evidence).

Persistence contract mirrors ``V4Archive`` / ``ChartStateLog``: ``load()`` / ``save()`` are blocking (the coordinator
MUST run them via its executor), ``save()`` is an atomic tempfile + ``os.replace`` with ``chmod 0o600`` on non-Windows,
retention is ``CYCLE_RETENTION_DAYS`` counted back from the newest cycle of the same mode (pruned at most hourly), plus
a hard cap (``CYCLE_MAX_STORED``, half of it per mode, each mode's oldest dropped first), a corrupt or unreadable file
starts empty and is logged ONCE, and no public method raises. Retention is anchored on the newest cycle, not on today
(owner decision 2026-11-11, "Fade only on new evidence"): a summer with no heating keeps last winter's heating cycles,
so the first refit in autumn starts from them instead of from nothing; old cycles leave only as newer ones of the same
mode arrive.

Occupant effect: none directly; this is the memory behind the heating/cooling rate the house is judged by.

Times are kept to the whole second. Indoor and outdoor readings are kept at their NATIVE resolution: each cycle
stores its values as integers with the fewest decimal places (0-3) that represent every reading of that series
exactly (``p`` / ``op``), so a whole-degree thermostat stores whole degrees and a 0.01 C sensor (0.018 F steps)
keeps every step (Issue #1512 verifier finding 6; log native resolution, never coarsen). Anything finer than 0.001 F
is float noise and is rounded to 0.001 F. ``add()`` applies the same rounding, so what ``cycles()`` returns is
exactly what a reload returns.

In memory each cycle is one ``_Cycle`` (``__slots__``) holding its series as ``array('i')`` offsets / scaled values
and its flags as ``bytes``: 9 bytes of payload per reading (4 time + 4 value + 1 flag), about 29 bytes per reading
measured including the per-cycle object and array overhead (a typical cycle), instead of ~175 for a list of three
Python objects.
``cycles()`` builds the public dicts on demand (call it in the executor for a full pool).

On disk (compact JSON, delta-encoded; ``v`` is ``CYCLE_STORE_VERSION``)::

    {"v": 2, "cycles": [{"i": "heat-1768000000", "m": "h", "a": 1768000000, "b": 1768000600, "x": null,
      "r": 0.1, "s": 70.0, "e": "sensor.hall", "p": 1,
      "t": [1768000000, 30, 31], "y": [685, 1, 1], "f": [[1, 20], [0, 5]],
      "op": 1, "ot": [1768000000, 600], "oy": [412, 1]}]}

``t`` is the first sample's epoch then the second-deltas, ``y`` the first scaled value (``value * 10**p``) then the
deltas, ``f`` the sample flags run-length encoded as ``[flags, count]``, ``ot`` / ``oy`` the outdoor series the same
way (``op`` places). A version-1 file (fixed centi-degree ``y``, deci-degree ``oy``, no ``p`` / ``op``) is read as
``p = 2`` / ``op = 1`` and written back as version 2. (Downgrades are not supported.)

Saving is periodic (``CYCLE_STORE_SAVE_INTERVAL_S``) plus at Home Assistant stop / unload: a crash may lose the most
recent cycles; the next refit works from the saved ones as a normal refit (the learning engine holds a fit only when
all the cycles it was fitted on are gone, so the survivors are not counted a second time through the prior). A
missing, partial or corrupt file loads as far as it is readable (unreadable rows dropped, duplicate ids kept once),
never raises, and nothing is counted twice: the learning engine counts a cycle as new evidence only when it is newer
than any an earlier fit saw (``t_seen``).
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from array import array
from pathlib import Path
from typing import Any

from .const import (
    CYCLE_MAX_STORED,
    CYCLE_RETENTION_DAYS,
    CYCLE_STORE_PRUNE_INTERVAL_S,
    CYCLE_STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_MODE_CODE = {"heat": "h", "cool": "c"}
_CODE_MODE = {v: k for k, v in _MODE_CODE.items()}
_MAX_PLACES = 3
_V1_PLACES = (2, 1)  # version-1 files: indoor centi-degrees, outdoor deci-degrees


def _places(vals: list[float]) -> int:
    """Fewest decimal places (0..3) that represent every value exactly (float noise below 1e-4 of a step ignored)."""
    for p in range(_MAX_PLACES):
        scale = 10**p
        if all(abs(v * scale - round(v * scale)) < 1e-4 for v in vals):
            return p
    return _MAX_PLACES


class _Cycle:
    """One stored cycle, compact (Issue #1512 verifier finding 6). Internal: ``cycles()`` returns public dicts."""

    __slots__ = (
        "cut",
        "end",
        "entity",
        "f",
        "id",
        "mode",
        "op",
        "ot",
        "oy",
        "p",
        "res",
        "setpoint_f",
        "t",
        "t_off",
        "t_on",
        "y",
    )

    def public(self) -> dict[str, Any]:
        t_on = self.t_on
        sc, osc = 10**self.p, 10**self.op
        return {
            "id": self.id,
            "mode": self.mode,
            "t_on": t_on,
            "t_off": self.t_off,
            "cut": self.cut,
            "res": self.res,
            "samples": [[t_on + dt, v / sc, f] for dt, v, f in zip(self.t, self.y, self.f, strict=True)],
            "outdoor": [[t_on + dt, v / osc] for dt, v in zip(self.ot, self.oy, strict=True)],
            "setpoint_f": self.setpoint_f,
            "entity": self.entity,
        }


def _compact(cycle: dict[str, Any]) -> _Cycle | None:
    """The cycle as the store keeps it (rounded, compact, native places per series), or None when it is unusable."""
    try:
        mode = cycle["mode"]
        if mode not in _MODE_CODE:
            return None
        raw = cycle.get("samples") or []
        rows = [(round(float(s[0])), float(s[1]), int(s[2])) for s in raw]
        orows = [(round(float(o[0])), float(o[1])) for o in cycle.get("outdoor") or []]
        if any(not 0 <= f <= 255 for _, _, f in rows):
            return None
        p = _places([v for _, v, _ in rows])
        op = _places([v for _, v in orows])
        t_on = float(round(cycle["t_on"]))
        t_off = cycle.get("t_off")
        res = cycle.get("res")
        sp = cycle.get("setpoint_f")
        cut = cycle.get("cut")
        entity = cycle.get("entity")
        c = _Cycle()
        c.id = str(cycle["id"])
        c.mode = mode
        c.t_on = t_on
        c.t_off = None if t_off is None else float(round(t_off))
        c.cut = None if cut is None else str(cut)
        c.res = None if res is None else round(float(res), 3)
        c.setpoint_f = None if sp is None else round(float(sp), 2)
        c.entity = None if entity is None else str(entity)
        c.p, c.op = p, op
        sc, osc = 10**p, 10**op
        c.t = array("i", (int(t - t_on) for t, _, _ in rows))
        c.y = array("i", (round(v * sc) for _, v, _ in rows))
        c.f = bytes(f for _, _, f in rows)
        c.ot = array("i", (int(t - t_on) for t, _ in orows))
        c.oy = array("i", (round(v * osc) for _, v in orows))
        c.end = (t_on + c.t[-1]) if rows else t_on
        return c
    except KeyError, TypeError, ValueError, IndexError, OverflowError:
        return None


def _deltas(vals: list[int]) -> list[int]:
    return [vals[0]] + [b - a for a, b in zip(vals, vals[1:], strict=False)] if vals else []


def _undelta(vals: list[int]) -> list[int]:
    out: list[int] = []
    acc = 0
    for i, v in enumerate(vals):
        acc = v if i == 0 else acc + v
        out.append(acc)
    return out


def _encode(c: _Cycle) -> dict[str, Any]:
    runs: list[list[int]] = []
    for f in c.f:
        if runs and runs[-1][0] == f:
            runs[-1][1] += 1
        else:
            runs.append([f, 1])
    t_on = int(c.t_on)
    return {
        "i": c.id,
        "m": _MODE_CODE[c.mode],
        "a": t_on,
        "b": None if c.t_off is None else int(c.t_off),
        "x": c.cut,
        "r": c.res,
        "s": c.setpoint_f,
        "e": c.entity,
        "p": c.p,
        "t": _deltas([t_on + dt for dt in c.t]),
        "y": _deltas(list(c.y)),
        "f": runs,
        "op": c.op,
        "ot": _deltas([t_on + dt for dt in c.ot]),
        "oy": _deltas(list(c.oy)),
    }


def _decode(d: Any, version: int) -> _Cycle | None:
    try:
        t = _undelta([int(x) for x in d["t"]])
        y = _undelta([int(x) for x in d["y"]])
        flags: list[int] = []
        for f, n in d["f"]:
            flags.extend([int(f)] * int(n))
        ot = _undelta([int(x) for x in d.get("ot") or []])
        oy = _undelta([int(x) for x in d.get("oy") or []])
        if not (len(t) == len(y) == len(flags)) or len(ot) != len(oy):
            return None
        mode = _CODE_MODE.get(d["m"])
        if mode is None:
            return None
        if version < 2:
            p, op = _V1_PLACES
        else:
            p, op = int(d["p"]), int(d["op"])
            if not (0 <= p <= _MAX_PLACES and 0 <= op <= _MAX_PLACES):
                return None
        sc, osc = 10**p, 10**op
        return _compact(
            {
                "id": d["i"],
                "mode": mode,
                "t_on": d["a"],
                "t_off": d.get("b"),
                "cut": d.get("x"),
                "res": d.get("r"),
                "setpoint_f": d.get("s"),
                "entity": d.get("e"),
                "samples": [[a, b / sc, f] for a, b, f in zip(t, y, flags, strict=True)],
                "outdoor": [[a, b / osc] for a, b in zip(ot, oy, strict=True)],
            }
        )
    except KeyError, TypeError, ValueError:
        return None


class CycleStore:
    """Rolling, persisted store of recorded cycles, ordered by ``t_on``."""

    def __init__(
        self,
        path: Path,
        *,
        max_days: int = CYCLE_RETENTION_DAYS,
        max_cycles: int = CYCLE_MAX_STORED,
    ) -> None:
        self._path = Path(path)
        self._file_prefix = self._path.stem + "_"
        self._max_days = max_days
        self._max_cycles = max_cycles
        self._cycles: list[_Cycle] = []
        self._keys: list[float] = []  # t_on of each cycle, parallel to _cycles (for bisect)
        self._by_id: dict[str, _Cycle] = {}  # id -> cycle: add() finds a replaced cycle in O(1) + its t_on bisect
        self._lock = threading.Lock()
        self._last_prune: float | None = None
        self._corrupt_logged = False
        self.dirty = False

    def __len__(self) -> int:
        return len(self._cycles)

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def _index_of(self, c: _Cycle) -> int:
        """Index of ``c`` in ``_cycles``: bisect on its t_on, then the few cycles sharing it. Caller holds the lock."""
        i = bisect.bisect_left(self._keys, c.t_on)
        while self._cycles[i] is not c:
            i += 1
        return i

    def _insert(self, c: _Cycle) -> None:
        """Insert in t_on order, replacing a cycle with the same id. Caller holds the lock."""
        old = self._by_id.get(c.id)
        if old is not None:
            i = self._index_of(old)
            del self._cycles[i]
            del self._keys[i]
        i = bisect.bisect_right(self._keys, c.t_on)
        self._cycles.insert(i, c)
        self._keys.insert(i, c.t_on)
        self._by_id[c.id] = c

    def _set_all(self, cycles: list[_Cycle]) -> None:
        """Replace the content (already in t_on order, unique ids). Caller holds the lock."""
        self._cycles = cycles
        self._keys = [c.t_on for c in cycles]
        self._by_id = {c.id: c for c in cycles}

    def _enforce_cap(self) -> int:
        """Each mode keeps at most half of ``max_cycles``, its own oldest dropped first (Issue #1512 final
        verification, the #1523 rule): a busy cooling summer can never push last winter's heating cycles out (it used
        to drop the oldest cycles whatever their mode: 0 of 3000 heat cycles left after a summer at 60 cycles a day).
        Caller holds the lock."""
        share = max(1, self._max_cycles // 2)
        counts: dict[str, int] = {}
        for c in self._cycles:
            counts[c.mode] = counts.get(c.mode, 0) + 1
        extra = {m: n - share for m, n in counts.items() if n > share}
        if not extra:
            return 0
        keep: list[_Cycle] = []
        for c in self._cycles:
            if extra.get(c.mode, 0) > 0:
                extra[c.mode] -= 1
                self._by_id.pop(c.id, None)
                continue
            keep.append(c)
        dropped = len(self._cycles) - len(keep)
        self._cycles = keep
        self._keys = [c.t_on for c in keep]
        return dropped

    def add(self, cycle: dict[str, Any], now: float | None = None) -> bool:
        """Keep a closed cycle (rounded as it will be stored). False when it was unusable. Never raises."""
        try:
            c = _compact(cycle)
            if c is None or not c.t:
                return False
            with self._lock:
                self._insert(c)
                self._enforce_cap()
                self.dirty = True
            self.prune(c.end if now is None else now)
            return True
        except Exception:  # noqa: BLE001 - recording must never break the thermostat path
            _LOGGER.warning("cycle_store: failed to add a cycle", exc_info=True)
            return False

    def cycles(self, mode: str | None = None, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """Stored cycles in ``t_on`` order for ``mode`` (None = both) that began at or after ``since_ts``.

        Fresh public dicts (the store keeps its own compact copies): building a full pool is a few hundred
        thousand list items, so a caller on the event loop should run it in the executor.
        """
        with self._lock:
            i = bisect.bisect_left(self._keys, since_ts)
            snap = self._cycles[i:]
        return [c.public() for c in snap if mode is None or c.mode == mode]

    def has_cycles(self, mode: str | None = None, since_ts: float = 0.0) -> bool:
        """True when a cycle of ``mode`` (None = any) began at or after ``since_ts`` (no dicts built)."""
        with self._lock:
            i = bisect.bisect_left(self._keys, since_ts)
            return any(mode is None or c.mode == mode for c in self._cycles[i:])

    def prune(self, now: float, *, force: bool = False) -> int:
        """Drop cycles over the retention and the cap. Hourly unless ``force``.

        A cycle is over the retention when its last sample is more than ``max_days`` older than the newest cycle of
        the same mode began (``now`` caps that anchor, so a clock jump into the future prunes nothing extra): a mode
        with no new cycles keeps all of its cycles, whatever the date."""
        try:
            if (
                not force
                and self._last_prune is not None
                and 0 <= now - self._last_prune < CYCLE_STORE_PRUNE_INTERVAL_S
            ):
                return 0
            self._last_prune = now
            span = self._max_days * 86400
            with self._lock:
                newest: dict[str, float] = {}
                for c in self._cycles:
                    newest[c.mode] = max(newest.get(c.mode, c.t_on), c.t_on)
                keep = [c for c in self._cycles if c.end >= min(newest[c.mode], now) - span]
                dropped = len(self._cycles) - len(keep)
                if dropped:
                    self._set_all(keep)
                dropped += self._enforce_cap()
                if dropped:
                    self.dirty = True
            if dropped:
                _LOGGER.debug("cycle_store: pruned %d cycles", dropped)
            return dropped
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cycle_store: prune failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # persistence (blocking: run in the executor)
    # ------------------------------------------------------------------

    def load(self, now: float | None = None) -> None:
        """Load from disk, dropping invalid or expired cycles. A missing/corrupt file starts empty. Never raises."""
        try:
            self._load(now)
        except Exception:  # noqa: BLE001 - the store must never break startup
            self._log_corrupt("unexpected error loading the cycle file, starting empty", exc_info=True)
            with self._lock:
                self._set_all([])

    def _log_corrupt(self, msg: str, *args: Any, exc_info: bool = False) -> None:
        if not self._corrupt_logged:
            self._corrupt_logged = True
            _LOGGER.warning("cycle_store: " + msg, *args, exc_info=exc_info)

    def _load(self, now: float | None) -> None:
        with self._lock:
            self._set_all([])
        self.dirty = False
        if not self._path.exists():
            _LOGGER.info("cycle_store: no cycle file yet, starting empty")
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            self._log_corrupt("failed to read the cycle file, starting empty: %s", err)
            return
        rows = raw.get("cycles") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            self._log_corrupt("the cycle file has an unexpected shape, starting empty")
            return
        version = raw.get("v")
        ver = version if isinstance(version, int) and not isinstance(version, bool) else 1
        decoded = [c for c in (_decode(r, ver) for r in rows) if c is not None and c.t]
        decoded.sort(key=lambda c: c.t_on)
        seen: set[str] = set()
        uniq = []
        for c in decoded:
            if c.id not in seen:
                seen.add(c.id)
                uniq.append(c)
        with self._lock:
            self._set_all(uniq)
        if len(uniq) != len(rows):
            self._log_corrupt("dropped %d unreadable cycles from the cycle file", len(rows) - len(uniq))
        if ver < CYCLE_STORE_VERSION and uniq:
            self.dirty = True  # migrated: the next save writes the current version
            _LOGGER.info(
                "cycle_store: migrating %d cycles from file version %s to %s", len(uniq), ver, CYCLE_STORE_VERSION
            )
        self.prune(time.time() if now is None else now, force=True)
        _LOGGER.info("cycle_store: loaded %d cycles", len(self._cycles))

    def save(self) -> bool:
        """Atomic write (tempfile + os.replace), 0o600 on non-Windows. True on success. Never raises."""
        try:
            with self._lock:
                snap = list(self._cycles)
            payload = {"v": CYCLE_STORE_VERSION, "cycles": [_encode(c) for c in snap]}
            serialized = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError, RuntimeError, KeyError) as err:
            _LOGGER.error("cycle_store: failed to serialize the cycles: %s", err)
            return False

        tmp_path_str: str | None = None
        try:
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self._path.parent, prefix=self._file_prefix, suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path_str, str(self._path))
            tmp_path_str = None
            if sys.platform != "win32":
                os.chmod(str(self._path), 0o600)
            self.dirty = False
            return True
        except OSError as err:
            _LOGGER.error("cycle_store: failed to save the cycle file: %s", err)
            if tmp_path_str is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path_str)
        return False
