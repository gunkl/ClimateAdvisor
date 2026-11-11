"""Heat/cool cycle recorder (Issues #1512 / #1516): a pure state machine, no Home Assistant imports.

The pooled cycle model (``cycle_model.py``) fits heating and cooling rates from the raw shape of every run: the indoor
reading in seconds from the on edge through the coast after the off edge. This module only RECORDS those cycles; the
coordinator feeds it events and persists what it returns (``cycle_store.py``). Heat and cool are mirrored.

Occupant effect: every real run of the equipment becomes evidence about how fast THIS house heats or cools, including
the short pulses and the coast afterwards, so the rate the house is judged by sharpens with use.

One open cycle per mode (in practice at most one at a time: a mode switch closes the other). A cycle:

* opens at ``on_edge`` (the equipment starts heating/cooling),
* records every indoor reading change (``reading``, with seconds) and the outdoor value (``outdoor``) separately,
* gets its ``t_off`` at ``off_edge`` and keeps recording for ``CYCLE_POST_OFF_WINDOW_S`` (the coast),
* closes at the next on edge in the same mode, at off + the window, at a mode switch, at a setpoint change, or at a
  gap (Home Assistant restart / thermostat unavailable). A gap is NOT an off edge: the cycle is closed with ``t_off``
  unset (if it was still running) and ``cut`` naming the reason, and the next samples carry ``gap_before``.

A setpoint change keeps the run (Issue #1512 verifier finding 3): a commanded change normally reaches HA as the
setpoint first and the equipment's action a few seconds later, and CA's own moves arrive the same way. So a setpoint
change during a run ENDS the run at that moment and keeps the run portion; only what follows the change is left out:

* the off edge (or a mode switch) arrives within ``CYCLE_SETPOINT_OFF_WINDOW_S`` -> ``t_off`` is that action edge and
  the cycle closes there (``cut="setpoint_change"``; no coast after a commanded stop is recorded);
* the equipment keeps running past the window -> the cycle closes at the change with ``t_off`` unset (its run ends
  at its last sample, the change; no coast and no off-edge tail is invented) and a continuation cycle opens AT the
  change, already running, carrying the readings since (it chases the new setpoint);
* the change arrives while the cycle is coasting (after its off edge) -> the cycle closes at the change: run and
  coast so far kept, the post-change coast left out.

``cut`` is ``None`` for a natural close (next on edge, or the window after off), else ``"mode_switch"``,
``"setpoint_change"``, ``"restart"`` or ``"unavailable"``.

Closed cycle dict (the contract ``cycle_model.fit_cycles`` / ``cycle_store`` read)::

    {"id": "heat-1768000000", "mode": "heat", "t_on": 1768000000.0, "t_off": 1768000600.0 | None, "cut": None | str,
     "res": 0.1 | None,                      # most common non-zero reading step in this cycle
     "samples": [[t_epoch_s, T_f, flags]],   # flags: bit0 running, bit1 windows, bit2 fan, bit3 nat_vent,
                                             #        bit4 gap_before, bit5 unavailable (the gap was "unavailable")
     "outdoor": [[t_epoch_s, T_out_f]], "setpoint_f": 70.0 | None, "entity": "sensor.x" | None}

``running`` (bit0) is set while the equipment is on: the on-edge sample and every reading before the off edge. The
sample taken AT the off edge, and every later one, is not running. Samples are strictly ordered in time; a reading
older than the last kept one is dropped. Beyond ``CYCLE_MAX_SAMPLES`` the interior is thinned (every other sample),
keeping the first, the last, the off-edge sample and any sample flagged as after a gap.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .const import (
    CYCLE_FLAG_FAN,
    CYCLE_FLAG_GAP_BEFORE,
    CYCLE_FLAG_NAT_VENT,
    CYCLE_FLAG_RUNNING,
    CYCLE_FLAG_UNAVAILABLE,
    CYCLE_FLAG_WINDOWS,
    CYCLE_MAX_OUTDOOR,
    CYCLE_MAX_SAMPLES,
    CYCLE_POST_OFF_WINDOW_S,
    CYCLE_SETPOINT_OFF_WINDOW_S,
)

MODES: tuple[str, str] = ("heat", "cool")
CUT_MODE_SWITCH = "mode_switch"
CUT_SETPOINT_CHANGE = "setpoint_change"
CUT_RESTART = "restart"
CUT_UNAVAILABLE = "unavailable"


def _num(v: Any) -> float | None:
    """A finite float, or None (bool, None, NaN and strings are not readings)."""
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return float(v)


def _infer_step(temps: list[float]) -> float | None:
    """Most common non-zero |step| between consecutive readings (ties -> smaller); None when nothing moved."""
    counts: dict[float, int] = {}
    for a, b in zip(temps, temps[1:], strict=False):
        d = abs(b - a)
        if d > 1e-6:
            key = round(d, 2)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return min(k for k, n in counts.items() if n == best)


class CycleRecorder:
    """Records heat/cool cycles from edge and reading events. Never raises on odd input (it ignores it)."""

    def __init__(
        self,
        mode_capable: Iterable[str] | bool = True,
        *,
        window_s: float = CYCLE_POST_OFF_WINDOW_S,
        setpoint_window_s: float = CYCLE_SETPOINT_OFF_WINDOW_S,
        max_samples: int = CYCLE_MAX_SAMPLES,
        max_outdoor: int = CYCLE_MAX_OUTDOOR,
    ) -> None:
        """``mode_capable``: the modes the thermostat can run ("heat", "cool"); True = both, False = none."""
        if isinstance(mode_capable, bool):
            self._modes = set(MODES) if mode_capable else set()
        else:
            self._modes = {m for m in mode_capable if m in MODES}
        self._window_s = float(window_s)
        self._sp_window_s = float(setpoint_window_s)
        self._max_samples = max(8, int(max_samples))
        self._max_outdoor = max(2, int(max_outdoor))
        self._open: dict[str, dict[str, Any]] = {}
        self._bits = 0  # current windows/fan/nat_vent flag bits
        self._gap_bits = 0  # gap_before (+ unavailable) to stamp on the next sample
        self._last_entity: str | None = None

    # ------------------------------------------------------------------
    # introspection (diagnostics / tests)
    # ------------------------------------------------------------------

    @property
    def open_modes(self) -> list[str]:
        """Modes that currently have an open cycle."""
        return sorted(self._open)

    def is_running(self, mode: str | None = None) -> bool:
        """True when an open cycle has not yet seen its off edge."""
        return any(c["t_off"] is None for m, c in self._open.items() if mode in (None, m))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _close(self, mode: str, cut: str | None) -> dict[str, Any]:
        c = self._open.pop(mode)
        c.pop("_sp", None)
        c.pop("_sp_new", None)
        c["cut"] = cut
        c["res"] = _infer_step([s[1] for s in c["samples"]])
        return c

    def _close_at_setpoint(self, mode: str, *, keep_running: bool) -> dict[str, Any]:
        """A run whose setpoint changed and whose equipment did not stop within the window: close it AT the change.

        The run portion (samples up to the change) is kept with ``t_off`` unset; with ``keep_running`` a
        continuation cycle opens at the change carrying the readings since (the equipment is chasing the new target).
        """
        c = self._open[mode]
        t_sp = c["_sp"]
        new_sp = c.get("_sp_new")
        after = [s for s in c["samples"] if s[0] >= t_sp]
        c["samples"] = [s for s in c["samples"] if s[0] <= t_sp]
        out_after = [o for o in c["outdoor"] if o[0] >= t_sp]
        out_before = [o for o in c["outdoor"] if o[0] < t_sp]
        c["outdoor"] = [o for o in c["outdoor"] if o[0] <= t_sp]
        closed = self._close(mode, CUT_SETPOINT_CHANGE)
        if keep_running:
            outdoor = [list(o) for o in out_after]
            if out_before and (not outdoor or outdoor[0][0] > t_sp):
                outdoor.insert(0, [t_sp, out_before[-1][1]])  # the outdoor level at the change
            cont: dict[str, Any] = {
                "id": f"{mode}-{int(t_sp)}",
                "mode": mode,
                "t_on": t_sp,
                "t_off": None,
                "cut": None,
                "res": None,
                "samples": [list(s) for s in after],
                "outdoor": outdoor,
                "setpoint_f": new_sp if new_sp is not None else closed.get("setpoint_f"),
                "entity": closed.get("entity"),
            }
            if cont["id"] == closed["id"]:
                cont["id"] = f"{mode}-{int(t_sp)}-sp"
            self._open[mode] = cont
        return closed

    def _expire(self, ts: float) -> list[dict[str, Any]]:
        """Close every cycle whose post-off window has elapsed at ``ts`` (and settle a setpoint change's window)."""
        out = []
        for mode in list(self._open):
            c = self._open[mode]
            if c.get("_sp") is not None and c["t_off"] is None and ts > c["_sp"] + self._sp_window_s:
                out.append(self._close_at_setpoint(mode, keep_running=True))
        for mode in list(self._open):
            t_off = self._open[mode]["t_off"]
            if t_off is not None and ts >= t_off + self._window_s:
                out.append(self._close(mode, None))
        return out

    def _append(self, c: dict[str, Any], ts: float, temp: float, *, force: bool = False) -> None:
        samples = c["samples"]
        running = c["t_off"] is None
        flags = self._bits | (CYCLE_FLAG_RUNNING if running else 0)
        if self._gap_bits:
            flags |= self._gap_bits
            self._gap_bits = 0
        if samples:
            last = samples[-1]
            if ts < last[0]:
                return  # out of order: keep the series monotone
            if not force and last[1] == temp and last[2] == flags:
                return  # an attribute-only update, not a reading change
        samples.append([round(ts, 3), temp, flags])
        if len(samples) > self._max_samples:
            self._thin(c)

    @staticmethod
    def _thin(c: dict[str, Any]) -> None:
        samples = c["samples"]
        n = len(samples)
        t_off = c["t_off"]
        keep = [
            s
            for i, s in enumerate(samples)
            if i in (0, n - 1) or i % 2 == 0 or s[2] & CYCLE_FLAG_GAP_BEFORE or (t_off is not None and s[0] == t_off)
        ]
        c["samples"] = keep
        c["thinned"] = c.get("thinned", 0) + (n - len(keep))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def on_edge(
        self, ts: float, mode: str, T: float | None, out: float | None = None, setpoint: float | None = None
    ) -> list[dict[str, Any]]:
        """The equipment started ``mode`` at ``ts`` with indoor ``T`` / outdoor ``out``. Returns closed cycles."""
        ts = _num(ts)  # type: ignore[assignment]
        if ts is None or mode not in self._modes:
            return []
        closed = self._expire(ts)
        cur = self._open.get(mode)
        if cur is not None:
            if cur["t_off"] is None:
                return closed  # a repeated "on" for a run already recording
            closed.append(self._close(mode, None))  # the next on edge ends the previous cycle's coast
        for other in list(self._open):
            if other != mode:
                oc = self._open[other]
                if oc.get("_sp") is not None and oc["t_off"] is None:
                    # the setpoint change switched the mode: that switch is the run's end (kept, cut by the change)
                    oc["t_off"] = round(ts, 3)
                    temp_sw = _num(T)
                    if temp_sw is not None:
                        self._append(oc, ts, temp_sw, force=True)
                    closed.append(self._close(other, CUT_SETPOINT_CHANGE))
                else:
                    closed.append(self._close(other, CUT_MODE_SWITCH))
        c: dict[str, Any] = {
            "id": f"{mode}-{int(ts)}",
            "mode": mode,
            "t_on": round(ts, 3),
            "t_off": None,
            "cut": None,
            "res": None,
            "samples": [],
            "outdoor": [],
            "setpoint_f": _num(setpoint),
            "entity": self._last_entity,
        }
        self._open[mode] = c
        temp = _num(T)
        if temp is not None:
            self._append(c, ts, temp, force=True)
        o = _num(out)
        if o is not None:
            c["outdoor"].append([round(ts, 3), o])
        return closed

    def off_edge(self, ts: float, T: float | None = None) -> list[dict[str, Any]]:
        """The equipment stopped at ``ts``. Ignored when no cycle is running. Returns closed cycles."""
        ts = _num(ts)  # type: ignore[assignment]
        if ts is None:
            return []
        closed = self._expire(ts)
        for mode in list(self._open):
            c = self._open[mode]
            if c["t_off"] is None:
                c["t_off"] = round(ts, 3)
                temp = _num(T)
                if temp is not None:
                    self._append(c, ts, temp, force=True)
                if c.get("_sp") is not None:
                    # the stop a setpoint change commanded: the run is kept up to this edge, the coast is not
                    closed.append(self._close(mode, CUT_SETPOINT_CHANGE))
        return closed

    def reading(self, ts: float, T: float | None, entity: str | None = None) -> list[dict[str, Any]]:
        """An indoor band-sensor reading changed. Returns closed cycles (a coast window that has elapsed)."""
        ts = _num(ts)  # type: ignore[assignment]
        temp = _num(T)
        if ts is None:
            return []
        closed = self._expire(ts)
        if temp is None:
            return closed
        if entity:
            self._last_entity = entity
        for c in self._open.values():
            if entity:
                if c["entity"] is None:
                    c["entity"] = entity
                elif c["entity"] != entity:
                    continue  # one sensor per cycle: another entity's reading would mix two series
            self._append(c, ts, temp)
        return closed

    def outdoor(self, ts: float, T_out: float | None) -> list[dict[str, Any]]:
        """An outdoor temperature reading. Returns closed cycles."""
        ts = _num(ts)  # type: ignore[assignment]
        o = _num(T_out)
        if ts is None:
            return []
        closed = self._expire(ts)
        if o is None:
            return closed
        for c in self._open.values():
            pts = c["outdoor"]
            if pts and (ts < pts[-1][0] or pts[-1][1] == o):
                continue
            if len(pts) < self._max_outdoor:
                pts.append([round(ts, 3), o])
        return closed

    def flags(self, ts: float, windows: bool, fan: bool, nat_vent: bool) -> list[dict[str, Any]]:
        """The window / fan / nat-vent state now. A change is stamped on the open cycles at ``ts``."""
        ts = _num(ts)  # type: ignore[assignment]
        if ts is None:
            return []
        closed = self._expire(ts)
        bits = (
            (CYCLE_FLAG_WINDOWS if windows else 0)
            | (CYCLE_FLAG_FAN if fan else 0)
            | (CYCLE_FLAG_NAT_VENT if nat_vent else 0)
        )
        if bits != self._bits:
            self._bits = bits
            for c in self._open.values():
                if c["samples"]:  # re-state the last reading at ts so the flag change has a time
                    self._append(c, ts, c["samples"][-1][1], force=True)
        return closed

    def gap(self, ts: float, reason: str) -> list[dict[str, Any]]:
        """Home Assistant restarted or the thermostat became unavailable/unknown. NOT an off edge.

        Closes every open cycle with ``cut=reason`` and flags the next sample ``gap_before``.
        """
        ts = _num(ts)  # type: ignore[assignment]
        if ts is None:
            return []
        reason = CUT_RESTART if reason == CUT_RESTART else CUT_UNAVAILABLE
        closed = self._expire(ts)
        for mode in list(self._open):
            c = self._open[mode]
            if c.get("_sp") is not None and c["t_off"] is None:
                closed.append(self._close_at_setpoint(mode, keep_running=False))  # run kept up to the change
            else:
                closed.append(self._close(mode, reason))
        self._gap_bits = CYCLE_FLAG_GAP_BEFORE | (CYCLE_FLAG_UNAVAILABLE if reason == CUT_UNAVAILABLE else 0)
        return closed

    def setpoint_change(self, ts: float, setpoint: float | None = None) -> list[dict[str, Any]]:
        """The setpoint changed (``setpoint``: the new one, for a continuation cycle). Returns closed cycles.

        A running cycle is not closed yet: the change is remembered and the run ends at the off edge (or mode switch)
        if one arrives within ``CYCLE_SETPOINT_OFF_WINDOW_S``, else at the change itself (module docs). A coasting
        cycle closes now (``cut="setpoint_change"``): its run and the coast so far are kept, the rest is not recorded.
        A second change inside the window keeps the first change's time.
        """
        ts = _num(ts)  # type: ignore[assignment]
        if ts is None:
            return []
        closed = self._expire(ts)
        for mode in list(self._open):
            c = self._open[mode]
            if c["t_off"] is None:
                if c.get("_sp") is None:
                    c["_sp"] = round(ts, 3)
                    c["_sp_new"] = _num(setpoint)
                    if c["samples"]:  # the level at the change, so both halves of a split run have it
                        self._append(c, ts, c["samples"][-1][1], force=True)
            else:
                closed.append(self._close(mode, CUT_SETPOINT_CHANGE))
        return closed

    def tick(self, ts: float) -> list[dict[str, Any]]:
        """Periodic (5-minute) call: closes cycles whose coast window has elapsed."""
        ts = _num(ts)  # type: ignore[assignment]
        return [] if ts is None else self._expire(ts)
