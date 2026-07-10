"""chart_log_driver — turn CA chart_log entries into replay-able input trajectories.

Per the architecture-reset Step-1 plan: the chart_log is a library of realistic
**input** trajectories ONLY (real indoor/outdoor/time sequences). It is never an
output oracle — any ``hvac``/``fan``/``windows_open`` fields recorded in the log were
produced by many different, buggy, since-deleted code versions and are deliberately
discarded here. We build ``temp_update`` events from ``indoor``/``outdoor``/``ts``
only, and hand them to the SAME production-scenario adapter (``run_production_scenario``)
that the golden suite and differential-replay harness already use — old-vs-old
divergence over this driver has the identical meaning it has over goldens: a hidden
input the harness failed to control.

Chart_log entry schema (see ``chart_log.py``): ``{"ts": ISO8601, "hvac": str,
"fan": bool, "indoor": float|None, "outdoor": float|None, ...}``. Entries with a
null indoor or outdoor reading are skipped (no viable temp_update).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_chart_log(path: str | Path) -> list[dict[str, Any]]:
    """Load raw chart_log entries from disk. Returns [] if the file is absent/invalid."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def build_scenario_from_chart_log(
    entries: list[dict[str, Any]],
    *,
    max_entries: int | None = None,
    stride: int = 1,
    config: dict[str, Any] | None = None,
    name: str = "chart_log_replay",
) -> dict[str, Any]:
    """Convert chart_log entries into a scenario dict driving ONLY temp_update events.

    Args:
        entries: raw chart_log entries (chronological order assumed, as persisted).
        max_entries: cap the number of *usable* (non-null indoor/outdoor) entries used,
                     taken from the end of the log (most recent) — keeps replay tractable.
        stride: use every Nth usable entry (subsample cadence).
        config: optional engine config overrides (merged over harness defaults).
        name: scenario name, for reporting.

    Returns:
        A scenario dict compatible with ``run_production_scenario`` — real physical
        input trajectories, zero recorded-decision fields carried over.
    """
    usable: list[dict[str, Any]] = []
    for e in entries:
        ts = e.get("ts")
        indoor = e.get("indoor")
        outdoor = e.get("outdoor")
        if ts is None or indoor is None or outdoor is None:
            continue
        if _parse_ts(ts) is None:
            continue
        usable.append({"ts": ts, "indoor": float(indoor), "outdoor": float(outdoor)})

    if max_entries is not None:
        usable = usable[-max_entries:]
    if stride > 1:
        usable = usable[::stride]

    events = [
        {
            "type": "temp_update",
            "time": e["ts"],
            "indoor_f": e["indoor"],
            "outdoor_f": e["outdoor"],
        }
        for e in usable
    ]

    return {
        "name": name,
        "description": f"Replay of {len(events)} real (indoor, outdoor) input pairs from chart_log — "
        "input trajectories only, recorded decisions discarded.",
        "config": config or {},
        "events": events,
    }
