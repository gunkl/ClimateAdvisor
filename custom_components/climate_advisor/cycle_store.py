"""Per-zone store of recorded heat/cool cycles (Issues #1512 / #1516): ``climate_advisor_cycles[_<entry_id>].json``.

Holds the raw cycles ``cycle_recorder.py`` produces so the pooled cycle model can refit from the last 90 days at any
time (a refit after a restart needs no in-flight state, and a better estimator can re-read the same evidence).

Persistence contract mirrors ``V4Archive`` / ``ChartStateLog``: ``load()`` / ``save()`` are blocking (the coordinator
MUST run them via its executor), ``save()`` is an atomic tempfile + ``os.replace`` with ``chmod 0o600`` on non-Windows,
retention is time-based (``CYCLE_RETENTION_DAYS``, pruned at most hourly) plus a hard cap (``CYCLE_MAX_STORED``, oldest
dropped first), a corrupt or unreadable file starts empty and is logged ONCE, and no public method raises.

Occupant effect: none directly; this is the memory behind the heating/cooling rate the house is judged by.

Times are stored to the whole second, indoor readings as centi-degrees F, outdoor readings as deci-degrees F, so a
cycle read back differs from the recorded one by at most 0.005 F (indoor) / 0.05 F (outdoor) and 0.5 s. ``add()``
applies the same rounding, so what ``cycles()`` returns is exactly what a reload returns.

On disk (compact JSON, delta-encoded; ``v`` is ``CYCLE_STORE_VERSION``)::

    {"v": 1, "cycles": [{"i": "heat-1768000000", "m": "h", "a": 1768000000, "b": 1768000600, "x": null,
      "r": 0.1, "s": 70.0, "e": "sensor.hall",
      "t": [1768000000, 30, 31], "y": [6850, 2, 3], "f": [[1, 20], [0, 5]],
      "ot": [1768000000, 600], "oy": [412, 1]}]}

``t`` is the first sample's epoch then the second-deltas, ``y`` the first centi-degree value then deltas, ``f`` the
sample flags run-length encoded as ``[flags, count]``, ``ot`` / ``oy`` the outdoor series the same way (deci-degrees).
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


def _norm(cycle: dict[str, Any]) -> dict[str, Any] | None:
    """The cycle as the store keeps it (rounded), or None when it is unusable."""
    try:
        mode = cycle["mode"]
        if mode not in _MODE_CODE:
            return None
        samples = [[float(round(s[0])), round(float(s[1]) * 100) / 100, int(s[2])] for s in cycle.get("samples") or []]
        outdoor = [[float(round(o[0])), round(float(o[1]) * 10) / 10] for o in cycle.get("outdoor") or []]
        t_off = cycle.get("t_off")
        res = cycle.get("res")
        sp = cycle.get("setpoint_f")
        cut = cycle.get("cut")
        entity = cycle.get("entity")
        return {
            "id": str(cycle["id"]),
            "mode": mode,
            "t_on": float(round(cycle["t_on"])),
            "t_off": None if t_off is None else float(round(t_off)),
            "cut": None if cut is None else str(cut),
            "res": None if res is None else round(float(res), 3),
            "samples": samples,
            "outdoor": outdoor,
            "setpoint_f": None if sp is None else round(float(sp), 2),
            "entity": None if entity is None else str(entity),
        }
    except KeyError, TypeError, ValueError, IndexError:
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


def _encode(c: dict[str, Any]) -> dict[str, Any]:
    s, o = c["samples"], c["outdoor"]
    runs: list[list[int]] = []
    for _, _, f in s:
        if runs and runs[-1][0] == f:
            runs[-1][1] += 1
        else:
            runs.append([f, 1])
    return {
        "i": c["id"],
        "m": _MODE_CODE[c["mode"]],
        "a": int(c["t_on"]),
        "b": None if c["t_off"] is None else int(c["t_off"]),
        "x": c["cut"],
        "r": c["res"],
        "s": c["setpoint_f"],
        "e": c["entity"],
        "t": _deltas([int(x[0]) for x in s]),
        "y": _deltas([round(x[1] * 100) for x in s]),
        "f": runs,
        "ot": _deltas([int(x[0]) for x in o]),
        "oy": _deltas([round(x[1] * 10) for x in o]),
    }


def _decode(d: Any) -> dict[str, Any] | None:
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
        return _norm(
            {
                "id": d["i"],
                "mode": mode,
                "t_on": d["a"],
                "t_off": d.get("b"),
                "cut": d.get("x"),
                "res": d.get("r"),
                "setpoint_f": d.get("s"),
                "entity": d.get("e"),
                "samples": [[a, b / 100, f] for a, b, f in zip(t, y, flags, strict=True)],
                "outdoor": [[a, b / 10] for a, b in zip(ot, oy, strict=True)],
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
        self._cycles: list[dict[str, Any]] = []
        self._keys: list[float] = []  # t_on of each cycle, parallel to _cycles (for bisect)
        self._lock = threading.Lock()
        self._last_prune: float | None = None
        self._corrupt_logged = False
        self.dirty = False

    def __len__(self) -> int:
        return len(self._cycles)

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def _insert(self, c: dict[str, Any]) -> None:
        """Insert in t_on order, replacing a cycle with the same id. Caller holds the lock."""
        for i, old in enumerate(self._cycles):
            if old["id"] == c["id"]:
                del self._cycles[i]
                del self._keys[i]
                break
        i = bisect.bisect_right(self._keys, c["t_on"])
        self._cycles.insert(i, c)
        self._keys.insert(i, c["t_on"])

    def _enforce_cap(self) -> int:
        extra = len(self._cycles) - self._max_cycles
        if extra > 0:
            del self._cycles[:extra]
            del self._keys[:extra]
        return max(extra, 0)

    def add(self, cycle: dict[str, Any], now: float | None = None) -> bool:
        """Keep a closed cycle (rounded as it will be stored). False when it was unusable. Never raises."""
        try:
            c = _norm(cycle)
            if c is None or not c["samples"]:
                return False
            with self._lock:
                self._insert(c)
                self._enforce_cap()
                self.dirty = True
            end = c["samples"][-1][0]
            self.prune(end if now is None else now)
            return True
        except Exception:  # noqa: BLE001 - recording must never break the thermostat path
            _LOGGER.warning("cycle_store: failed to add a cycle", exc_info=True)
            return False

    def cycles(self, mode: str | None = None, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """Stored cycles in ``t_on`` order for ``mode`` (None = both) that began at or after ``since_ts``.

        The returned dicts are the store's own: callers must not mutate them.
        """
        with self._lock:
            snap = list(self._cycles)
        return [c for c in snap if (mode is None or c["mode"] == mode) and c["t_on"] >= since_ts]

    def prune(self, now: float, *, force: bool = False) -> int:
        """Drop cycles older than the retention (by their last sample) and over the cap. Hourly unless ``force``."""
        try:
            if (
                not force
                and self._last_prune is not None
                and 0 <= now - self._last_prune < CYCLE_STORE_PRUNE_INTERVAL_S
            ):
                return 0
            self._last_prune = now
            cutoff = now - self._max_days * 86400
            with self._lock:
                keep = [c for c in self._cycles if (c["samples"][-1][0] if c["samples"] else c["t_on"]) >= cutoff]
                dropped = len(self._cycles) - len(keep)
                if dropped:
                    self._cycles = keep
                    self._keys = [c["t_on"] for c in keep]
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
                self._cycles, self._keys = [], []

    def _log_corrupt(self, msg: str, *args: Any, exc_info: bool = False) -> None:
        if not self._corrupt_logged:
            self._corrupt_logged = True
            _LOGGER.warning("cycle_store: " + msg, *args, exc_info=exc_info)

    def _load(self, now: float | None) -> None:
        with self._lock:
            self._cycles, self._keys = [], []
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
        if isinstance(raw.get("v"), int) and raw["v"] > CYCLE_STORE_VERSION:
            self._log_corrupt("the cycle file is from a newer version (%s), starting empty", raw["v"])
            return
        decoded = [c for c in (_decode(r) for r in rows) if c is not None and c["samples"]]
        decoded.sort(key=lambda c: c["t_on"])
        seen: set[str] = set()
        uniq = []
        for c in decoded:
            if c["id"] not in seen:
                seen.add(c["id"])
                uniq.append(c)
        with self._lock:
            self._cycles = uniq
            self._keys = [c["t_on"] for c in uniq]
        if len(uniq) != len(rows):
            self._log_corrupt("dropped %d unreadable cycles from the cycle file", len(rows) - len(uniq))
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
