#!/usr/bin/env python3
"""Thermal learning database diagnostic tool for Climate Advisor.

Reads climate_advisor_learning.json directly from HA via SSH and prints
a structured report of the rejection log, committed observations, and
model summary. No HA_URL or HA_TOKEN required.

Usage:
    python3 tools/learning_db.py                # full report
    python3 tools/learning_db.py --rejections   # rejection log only
    python3 tools/learning_db.py --committed    # committed obs only
    python3 tools/learning_db.py --model        # model summary only
    python3 tools/learning_db.py --thermal      # chart_log endpoint observations only
    python3 tools/learning_db.py --last N       # last N rejections per type (default 5)
    python3 tools/learning_db.py --swing        # how the swing was learned, coast (Issue #1233)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _zone_storage import (  # noqa: E402
    LEARNING_DB_FILE,
    ZoneSelectionRequired,
    entry_id_from_remote_path,
    format_zone_choices,
    resolve_api_zone,
    resolve_remote_path_with_discovery,
    with_zone,
)
from ha_logs import load_config, ssh_args, ssh_target  # noqa: E402

# Kept for backward compatibility with anything importing this name directly;
# fetch_learning_db() now resolves the real (possibly entry-scoped) path itself.
LEARNING_DB_PATH = "/config/climate_advisor_learning.json"

OBS_TYPES = [
    "passive_decay",
    "vent_window_decay",
    "vent_fan_decay",
    "solar_gain",
    "hvac_heat",
    "hvac_cool",
]

# Issue #587: old-format obs_type strings, retired but still possibly present in
# pre-upgrade persisted history — --check-587 flags any post-upgrade-dated entry
# still carrying these as a FAIL (see the #133 broken-state-trap check).
LEGACY_OBS_TYPES = ["fan_only_decay", "ventilated_decay"]

DEBUG_STATE_URL_PATH = "/api/climate_advisor/automation_state"


# ---------------------------------------------------------------------------
# .env loader (same logic as thermal_health.py)
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load HA_URL and HA_TOKEN from .env file if not already set in environment."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k in ("HA_URL", "HA_TOKEN") and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# SSH fetch
# ---------------------------------------------------------------------------


def fetch_learning_db(config: dict[str, str], entry_id: str | None = None, resolved: dict | None = None) -> dict:
    """SSH into HA and read the learning JSON file. Returns parsed dict.

    Since Issue #796, storage is entry-scoped in a multi-zone install. If
    entry_id isn't given, auto-discovers a single matching zone file, or
    falls back to the legacy unscoped path for single-zone installs.
    ``resolved`` (optional) receives ``entry_id``: the zone whose file was read
    (None for the legacy unscoped file), so the live REST section asks for the
    same zone (Issue #1418).
    """
    remote_path = resolve_remote_path_with_discovery(
        config,
        LEARNING_DB_FILE,
        entry_id,
        lambda cfg, remote_cmd: ssh_args(cfg) + [ssh_target(cfg), remote_cmd],
    )
    if resolved is not None:
        resolved["entry_id"] = entry_id or entry_id_from_remote_path(LEARNING_DB_FILE, remote_path)
    cmd = ssh_args(config) + [ssh_target(config), f"cat {remote_path}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("ERROR: SSH timed out reading learning DB.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0 and not result.stdout.strip():
        stderr = result.stderr.strip()
        if stderr:
            print(f"SSH error: {stderr}", file=sys.stderr)
        else:
            print(f"ERROR: Could not read {remote_path} (file may not exist yet).", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse learning DB JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("ERROR: Learning DB has unexpected structure (expected dict).", file=sys.stderr)
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Optional REST fetch for live pending observations
# ---------------------------------------------------------------------------


def _fetch_live_pipeline(ha_url: str, ha_token: str, entry_id: str | None = None) -> dict | None:
    """Fetch live pipeline state from the debug API for ``entry_id``'s zone. Returns dict or None."""
    import urllib.error
    import urllib.request

    url = with_zone(f"{ha_url}{DEBUG_STATE_URL_PATH}", entry_id)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError, urllib.error.URLError, OSError:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "thermal_pipeline" not in data:
        return None

    return data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _val(v: Any, suffix: str = "", decimals: int = 3) -> str:
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.{decimals}f}{suffix}"
    return str(v)


def _pad(text: str, width: int) -> str:
    return str(text).ljust(width)


# ---------------------------------------------------------------------------
# Section A: Model Summary
# ---------------------------------------------------------------------------


def _load_ca_module(name: str):
    """``custom_components/climate_advisor/<name>.py`` loaded by path (the package needs Home Assistant).

    Same approach as ``tools/thermal_replay.py``: one implementation of the engine's pure rules, reused here.
    Only for the dependency-free modules that support standalone loading (``swing``, ``thermal_confidence``).
    """
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_ca_{name}_standalone", path)
    if spec.name in sys.modules:
        return sys.modules[spec.name]
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def _load_swing_module():
    """The engine's pure swing rules (``swing.py``)."""
    return _load_ca_module("swing")


def _load_confidence_module():
    """The engine's live confidence grades and physics-eligibility rule (``thermal_confidence.py``, Issue #1277)."""
    return _load_ca_module("thermal_confidence")


def _print_model_summary(db: dict) -> None:
    cache = db.get("thermal_model_cache")
    if not isinstance(cache, dict):
        print("Thermal Model Summary")
        print("=====================")
        print("(no thermal_model_cache found in learning DB)")
        print()
        return

    def _model_field(key: str, suffix: str, obs_key: str | None = None) -> str:
        val = cache.get(key)
        if val is None:
            return "None"
        result = f"{val:.4f} {suffix}"
        if obs_key:
            n = cache.get(obs_key)
            if n is not None:
                result += f"  ({n} obs)"
        r2_key = key.replace("k_", "r2_") if key.startswith("k_") else None
        if r2_key and r2_key in cache:
            result += f"  avg_R2={cache[r2_key]:.3f}"
        return result

    # Issue #1277: graded live from the counts, as the engine does; stored copies were never refreshed.
    _conf = _load_confidence_module()
    conf_passive = _conf.grade_passive_confidence(cache)
    conf_hvac = _conf.grade_hvac_confidence(cache)
    last_obs = cache.get("last_observation_date") or cache.get("last_obs") or "none"

    print("Thermal Model Summary")
    print("=====================")
    print(f"k_passive:     {_model_field('k_passive', 'hr^-1', 'n_passive')}")
    print(f"k_active_heat: {_model_field('k_active_heat', 'F/hr', 'n_hvac_heat')}")
    print(f"k_active_cool: {_model_field('k_active_cool', 'F/hr', 'n_hvac_cool')}")
    print(f"k_vent_window: {_model_field('k_vent_window', 'hr^-1', 'observation_count_vent_window')}")
    print(f"k_vent_fan:    {_model_field('k_vent_fan', 'hr^-1', 'observation_count_vent_fan')}")
    print(f"k_solar:       {_model_field('k_solar', 'F/hr', 'n_solar')}")
    if "k_vent" in cache or "observation_count_fan_only" in cache:
        print("WARNING: legacy k_vent/observation_count_fan_only key(s) present in cache (Issue #587)")
    if _load_swing_module().needs_swing_migration(cache):
        print(
            "WARNING: swing values below are from the RETIRED definition (half the rise per run) - no"
            " swing_definition_version yet; Climate Advisor >= 0.8.34 nulls them on its next load (Issue #1234)"
        )

    def _swing_field(mode: str) -> str:
        # Issue #1234: swing = the thermostat's own one-sided hysteresis (heat: F below setpoint at
        # heat-on; cool: F above at cool-on), median of recent real starts; +/- resolution/2 on
        # whole-degree readings. The stop point is kept separate and never mixed into the swing.
        val = cache.get(f"swing_{mode}_f")
        cnt = cache.get(f"observation_count_swing_{mode}", 0)
        if val is None:
            return f"None  ({cnt} samples)"
        res = cache.get(f"swing_{mode}_resolution_f") or 0.0
        off = cache.get(f"swing_{mode}_off_point_f")
        interval = f" +/-{res / 2:.2f}" if res else ""
        off_txt = f", stops {abs(off):.2f} F {'short of' if off >= 0 else 'past'} setpoint" if off is not None else ""
        return f"{val:.2f} F{interval} {'below' if mode == 'heat' else 'above'} setpoint  ({cnt} samples{off_txt})"

    print(f"swing_heat:    {_swing_field('heat')}")
    print(f"swing_cool:    {_swing_field('cool')}")

    def _coast_field(mode: str) -> str:
        # Issue #1233: coast overshoot is kept apart from the swing (heat: peak above the off reading).
        val = cache.get(f"coast_{mode}_f")
        cnt = cache.get(f"observation_count_coast_{mode}", 0)
        if val is None:
            return f"None  ({cnt} windows)"
        past = cache.get(f"coast_{mode}_past_setpoint_f")
        past_txt = f", {past:+.2f} F past setpoint" if past is not None else ""
        return f"{val:.2f} F beyond the off reading  ({cnt} windows{past_txt})"

    print(f"coast_heat:    {_coast_field('heat')}")
    print(f"coast_cool:    {_coast_field('cool')}")
    print("(--swing: per-cycle inputs, reject reasons, recomputed median, coast windows)")
    print(f"conf_passive:  {conf_passive}")
    print(f"conf_hvac:     {conf_hvac}")
    print(f"last_obs:      {last_obs}")
    print()


def _fmt_num(v: Any, decimals: int = 2) -> str:
    return "-" if v is None else f"{float(v):.{decimals}f}"


def _fmt_age(seconds: Any) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def format_swing_debug(db: dict, last_n: int = 15) -> str:
    """Per-sample swing table, reject histogram, recomputed median and coast (Issue #1233).

    Occupant framing: a homeowner checks the learned swing against what their own thermostat does.
    Every judged cycle (accepted or rejected) is listed with the inputs that decided it, the stored
    median is recomputed from the retained samples with the SAME function production uses, and the
    coast overshoot is shown in its own section because it is NOT part of the swing.
    """
    swing = _load_swing_module()
    cache = db.get("thermal_model_cache") if isinstance(db, dict) else None
    out: list[str] = [
        "Thermostat swing: how it was learned",
        "=====================================",
        swing.SWING_DEFINITION_TEXT,
        "",
    ]
    has_swing = isinstance(cache, dict) and any(
        cache.get(f"swing_{m}_audit") or cache.get(f"swing_{m}_samples") or cache.get(f"swing_{m}_f") is not None
        for m in ("heat", "cool")
    )
    if not has_swing:
        out.append("(no swing samples recorded yet)")
        if isinstance(cache, dict) and any(cache.get(f"coast_{m}_samples") for m in ("heat", "cool")):
            out.append("")
            out += _format_coast_section(cache, last_n)
        return "\n".join(out) + "\n"
    if swing.needs_swing_migration(cache):
        out.append("WARNING: swing values are from the RETIRED definition (no swing_definition_version).")
        out.append("  They are NOT the thermostat's hysteresis; the integration resets them on its next load.")
        out.append("")
    for mode in ("heat", "cool"):
        out += _format_swing_mode_section(swing, cache, mode, last_n)
        out.append("")
    out += _format_coast_section(cache, last_n)
    return "\n".join(out) + "\n"


def _format_swing_mode_section(swing: Any, cache: dict, mode: str, last_n: int) -> list[str]:
    title = (
        "Heat swing (degrees below setpoint at heat-on)"
        if mode == "heat"
        else "Cool swing (degrees above setpoint at cool-on)"
    )
    p = f"swing_{mode}"
    samples = [s for s in (cache.get(f"{p}_samples") or []) if isinstance(s, dict)]
    audit = [r for r in (cache.get(f"{p}_audit") or []) if isinstance(r, dict)]
    counts = cache.get(f"{p}_reject_counts") or {}
    summary = swing.summarize_swing_samples(samples)
    stored = cache.get(f"{p}_f")
    lines = [title, "-" * len(title)]
    verdict = ""
    if summary["value_f"] is not None and stored is not None and abs(summary["value_f"] - float(stored)) > 0.005:
        verdict = "  MISMATCH: the stored value disagrees with the retained samples"
    res = summary["resolution_f"] or 0.0
    interval = f" +/-{res / 2:.2f}" if res else ""
    lines.append(
        f"stored {_fmt_num(stored)}  |  recomputed median {_fmt_num(summary['value_f'])}{interval}"
        f" of the last {len(samples)} accepted sample(s)  |  resolution {res:.2f} F"
        f"  |  off-point {_fmt_num(summary['off_point_f'])}{verdict}"
    )
    lines.append(f"lifetime accepted samples: {cache.get(f'observation_count_{p}', 0)}")
    rows = audit[-last_n:] if last_n > 0 else []
    if rows:
        lines.append("")
        lines.append(f"Last {len(rows)} judged cycle(s), newest last:")
        lines.append(
            f"  {'start (local ts)':<26} {'verdict':<18} {'value':>6} {'setpt':>6} {'sp age':>7} {'on':>7} {'off':>7}"
            f" {'res':>5} {'unit':<10} {'trigger':<11} source"
        )
        for r in rows:
            verdict_txt = "accepted" if r.get("accepted") else f"rejected:{r.get('reason')}"
            lines.append(
                f"  {str(r.get('ts'))[:26]:<26} {verdict_txt:<18} {_fmt_num(r.get('value_f')):>6}"
                f" {_fmt_num(r.get('setpoint_f'), 1):>6} {_fmt_age(r.get('setpoint_age_s')):>7}"
                f" {_fmt_num(r.get('on_indoor_f')):>7} {_fmt_num(r.get('off_indoor_f')):>7}"
                f" {_fmt_num(r.get('resolution_f'), 1):>5} {str(r.get('reading_unit') or '-'):<10}"
                f" {str(r.get('trigger') or '-'):<11} {r.get('indoor_entity') or '-'}"
            )
    elif samples:
        lines.append("(no audit rows - samples predate the per-cycle audit)")
    elif stored is not None:
        lines.append("(a stored value with no retained samples or audit rows - learned before Issue #1234/#1233)")
    window_rej: dict[str, int] = {}
    for r in audit:
        if not r.get("accepted"):
            window_rej[str(r.get("reason"))] = window_rej.get(str(r.get("reason")), 0) + 1
    lines.append("")
    lines.append("Reject histogram (lifetime / in the retained window):")
    reasons = list(swing.SWING_REJECT_REASONS)
    reasons += [k for k in counts if k not in reasons]
    if not any(counts.get(k) or window_rej.get(k) for k in reasons):
        lines.append("  (none)")
    for k in reasons:
        if counts.get(k) or window_rej.get(k):
            lines.append(
                f"  {k:<18} {int(counts.get(k, 0)):>5} / {window_rej.get(k, 0):<3}"
                f" {'#' * min(int(counts.get(k, 0)), 40)}"
            )
    return lines


def _format_coast_section(cache: dict, last_n: int) -> list[str]:
    title = "Coast overshoot (NOT part of the swing)"
    lines = [
        title,
        "-" * len(title),
        "After the equipment stops the room keeps going: heat -> peak above the off reading, cool -> trough below it.",
        "A window is a LOWER bound when something (the next run, a setpoint change) cut it short.",
    ]
    any_rows = False
    for mode, word in (("heat", "peak"), ("cool", "trough")):
        samples = [s for s in (cache.get(f"coast_{mode}_samples") or []) if isinstance(s, dict)]
        if not samples:
            lines.append(f"{mode}: no coast windows recorded yet")
            continue
        any_rows = True
        lines.append(
            f"{mode}: median coast {_fmt_num(cache.get(f'coast_{mode}_f'))} F ({word} past the off reading),"
            f" {_fmt_num(cache.get(f'coast_{mode}_past_setpoint_f'))} F past the setpoint"
            f"  ({cache.get(f'observation_count_coast_{mode}', 0)} windows)"
        )
        ended: dict[str, int] = {}
        for s in samples:
            ended[str(s.get("ended_by"))] = ended.get(str(s.get("ended_by")), 0) + 1
        lines.append("  ended by: " + ", ".join(f"{k} x{v}" for k, v in sorted(ended.items())))
        for s in samples[-last_n:] if last_n > 0 else []:
            lines.append(
                f"  {str(s.get('ts'))[:26]:<26} off {_fmt_num(s.get('off_indoor_f')):>6}"
                f" {word} {_fmt_num(s.get('extreme_f')):>6}"
                f" coast {_fmt_num(s.get('coast_f')):>5} past-sp {_fmt_num(s.get('past_setpoint_f')):>5}"
                f" n={s.get('n_post')} window {_fmt_age(s.get('window_s'))} ended_by {s.get('ended_by')}"
            )
    if not any_rows:
        lines.append("(coast needs readings after a thermostat stop; none recorded yet)")
    return lines


def _print_swing_debug(db: dict, last_n: int) -> None:
    print(format_swing_debug(db, last_n))


# ---------------------------------------------------------------------------
# Section A2: Solar Model Detail
# ---------------------------------------------------------------------------


def _print_solar_model_section(db: dict) -> None:
    """Print a focused Solar Model section: phase offset, confidence, and rejection summary."""
    cache = db.get("thermal_model_cache") or {}
    rejection_log = db.get("rejection_log") or {}

    print("Solar Model")
    print("-----------")

    # --- Phase offset ---
    phase_offset = cache.get("solar_phase_offset_h")
    if phase_offset is None:
        print("solar_phase_offset_h:    None  (no phase observations yet)")
    else:
        print(f"solar_phase_offset_h:    {phase_offset:.3f} h")

    # --- Committed solar observations + confidence ---
    n_solar = cache.get("observation_count_solar", 0)

    # Derive confidence_k_solar directly (mirrors learning.py logic)
    if n_solar >= 100:
        conf_solar = "high"
    elif n_solar >= 50:
        conf_solar = "medium"
    elif n_solar >= 20:
        conf_solar = "low"
    else:
        conf_solar = "none"

    print(f"observation_count_solar: {n_solar}")
    print(f"confidence_k_solar:      {conf_solar}")

    # --- Solar rejection summary ---
    solar_rejections = rejection_log.get("solar_gain")
    if not isinstance(solar_rejections, list):
        solar_rejections = []

    total_attempted = n_solar + len(solar_rejections)
    print()
    print(
        f"Solar rejection summary: {len(solar_rejections)} rejected / {total_attempted} attempted / {n_solar} committed"
    )

    if solar_rejections:
        # Dominant rejection reason
        reason_counts: dict[str, int] = {}
        for entry in solar_rejections:
            if not isinstance(entry, dict):
                continue
            rc = str(entry.get("reason_code", entry.get("reason", "unknown")))
            reason_counts[rc] = reason_counts.get(rc, 0) + 1
        if reason_counts:
            top_reason, top_count = max(reason_counts.items(), key=lambda x: x[1])
            all_reasons = ", ".join(f"{rc} ({n})" for rc, n in sorted(reason_counts.items(), key=lambda x: -x[1]))
            print(f"Dominant rejection:      {top_reason} ({top_count}/{len(solar_rejections)}) — all: {all_reasons}")

        # Last 3 rejection events (most recent first)
        print("Last 3 solar_gain rejections:")
        last_3 = list(reversed(solar_rejections))[:3]
        for entry in last_3:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "?")
            reason = entry.get("reason_code", entry.get("reason", "?"))
            elapsed_raw = entry.get("elapsed_minutes")
            elapsed = f"{elapsed_raw}min" if elapsed_raw is not None else "?min"
            n_samples = entry.get("n_samples", entry.get("sample_count", "-"))
            print(f"  {ts}  {str(reason):<20} elapsed={elapsed:<7} n={n_samples}")
    else:
        print("  (no solar_gain rejections recorded)")

    print()


# ---------------------------------------------------------------------------
# Section B: Rejection Log
# ---------------------------------------------------------------------------


def _print_rejection_log(db: dict, last_n: int, filter_type: str | None = None) -> None:
    rejection_log = db.get("rejection_log")
    if not isinstance(rejection_log, dict):
        print("Rejection Log")
        print("-------------")
        print("(no rejection_log found in learning DB)")
        print()
        return

    print("Rejection Log")
    print("-------------")

    # Summary: top reason codes across all types (or filtered type)
    _reason_totals: dict[str, int] = {}
    _types_to_scan = [filter_type] if filter_type else list(rejection_log.keys())
    for _ot in _types_to_scan:
        _entries = rejection_log.get(_ot)
        if not isinstance(_entries, list):
            continue
        for _e in _entries:
            if not isinstance(_e, dict):
                continue
            _rc = str(_e.get("reason_code", _e.get("reason", "unknown")))
            _reason_totals[_rc] = _reason_totals.get(_rc, 0) + 1
    if _reason_totals:
        _top = sorted(_reason_totals.items(), key=lambda x: -x[1])[:5]
        _summary = ", ".join(f"{rc} ({n})" for rc, n in _top)
        print(f"Top rejection reasons: {_summary}")
        print()

    any_printed = False

    obs_types_to_show = [filter_type] if filter_type else OBS_TYPES
    for obs_type in obs_types_to_show:
        entries = rejection_log.get(obs_type)
        if not entries:
            if not filter_type:
                print(f"{obs_type}: no rejections")
            continue

        if not isinstance(entries, list):
            print(f"{obs_type}: (unexpected format)")
            continue

        # Stored oldest-first; show newest first
        reversed_entries = list(reversed(entries))
        total = len(reversed_entries)
        shown = reversed_entries[:last_n]

        print(f"{obs_type} ({total} total, showing last {min(last_n, total)}):")
        for entry in shown:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "?")
            reason = entry.get("reason_code", entry.get("reason", "?"))
            n_samples = entry.get("n_samples", entry.get("sample_count", "-"))
            elapsed_raw = entry.get("elapsed_minutes")
            elapsed = f"{elapsed_raw}min" if elapsed_raw is not None else "?min"
            delta_t = entry.get("delta_t_f", entry.get("indoor_delta_f", 0.0))
            r2 = entry.get("r_squared", entry.get("r2"))
            r2_str = f"{r2:.3f}" if isinstance(r2, float) else "None"
            sf_range_raw = entry.get("sf_range")
            sf_range_str = f"{sf_range_raw:.2f}" if isinstance(sf_range_raw, (int, float)) else "n/a"
            indoor_dir = entry.get("indoor_direction", "n/a")
            print(
                f"  {ts}  {str(reason):<16} n={str(n_samples):<3} "
                f"elapsed={elapsed:<7} delta_t={float(delta_t) if delta_t is not None else 0.0:.2f}F  "
                f"r2={r2_str}  sf_range={sf_range_str}  indoor={indoor_dir}"
            )
        any_printed = True

    # Also print any obs_types in the log that aren't in our canonical list (when not filtered)
    if not filter_type:
        for obs_type, entries in rejection_log.items():
            if obs_type not in OBS_TYPES and entries:
                reversed_entries = list(reversed(entries)) if isinstance(entries, list) else []
                total = len(reversed_entries)
                shown = reversed_entries[:last_n]
                print(f"{obs_type} [{total} total, showing last {min(last_n, total)}] (unknown type):")
                for entry in shown:
                    if isinstance(entry, dict):
                        print(f"  {entry}")
                any_printed = True

    if not any_printed and not filter_type:
        print("(no rejections recorded)")
    elif not any_printed and filter_type:
        print(f"(no rejections recorded for type '{filter_type}')")

    print()


# ---------------------------------------------------------------------------
# Section C: Committed Observations
# ---------------------------------------------------------------------------


def _print_committed(db: dict) -> None:
    observations = db.get("thermal_observations")
    if not isinstance(observations, list):
        print("Committed Observations")
        print("----------------------")
        print("(no thermal_observations found in learning DB)")
        print()
        return

    last_10 = list(reversed(observations))[:10]
    total = len(observations)

    print(f"Committed Observations (last 10 of {total})")
    print("-" * 60)

    header = _pad("date", 12) + _pad("obs_type", 20) + _pad("k_passive", 12) + _pad("R2", 7) + _pad("grade", 8) + "n"
    print(header)
    print("-" * 60)

    for obs in last_10:
        if not isinstance(obs, dict):
            continue
        date = obs.get("date", "?")
        obs_type = obs.get("hvac_mode", obs.get("obs_type", "?"))
        k_passive = obs.get("k_passive")
        k_passive_str = f"{k_passive:.4f}" if isinstance(k_passive, float) else "-"
        r2 = obs.get("r_squared_passive", obs.get("r_squared", obs.get("r2")))
        r2_str = f"{r2:.3f}" if isinstance(r2, float) else "-"
        grade = obs.get("confidence_grade", "-")
        n = obs.get("sample_count_post", obs.get("sample_count_pre", obs.get("sample_count", "-")))

        row = (
            _pad(str(date), 12)
            + _pad(str(obs_type), 20)
            + _pad(k_passive_str, 12)
            + _pad(r2_str, 7)
            + _pad(str(grade), 8)
            + str(n)
        )
        print(row)

    print()


# ---------------------------------------------------------------------------
# Section C2: Issue #587 post-deploy validation check
# ---------------------------------------------------------------------------


def _print_check_587(db: dict) -> None:
    """PASS/WARN/FAIL summary for the Issue #587 k_vent/fan_only_decay retirement.

    Checks (per docs/08-COMPUTATION-REFERENCE.md's post-deploy monitoring table):
      1. "k_vent" key absent from thermal_model_cache (PASS) / present (FAIL)
      2. "observation_count_fan_only" key absent (PASS) / present (FAIL)
      3. k_vent_window / k_vent_fan values + bounds sanity + observation counts
      4. No post-upgrade thermal_observations entry still carries hvac_mode
         "ventilated" or "fan_only" (the old strings) — FAIL if so (#133 broken-
         state-trap check, runnable in production, not just at code review)
      5. REJECT_NO_K_PASSIVE count from the rejection log (Defect B, #852 — live).
         A single snapshot can't detect a "climbing" trend on its own — that's
         the daily-check procedure's job (#854): compare today's count against
         yesterday's. This check flags the one thing a single snapshot CAN say
         for certain: if k_passive is already populated in the cache, a fresh
         no_k_passive rejection should not still be happening going forward.
    """
    print("Issue #587 Post-Deploy Check")
    print("=============================")

    cache = db.get("thermal_model_cache")
    overall = "PASS"
    lines: list[str] = []

    if not isinstance(cache, dict):
        print("WARN: no thermal_model_cache found in learning DB (fresh install — nothing to check yet)")
        print()
        return

    # Check 1/2: legacy keys must be absent
    if "k_vent" in cache:
        overall = "FAIL"
        lines.append("FAIL: 'k_vent' key still present in thermal_model_cache — old code/cache never cleared")
    else:
        lines.append("PASS: 'k_vent' key absent from thermal_model_cache")

    if "observation_count_fan_only" in cache:
        overall = "FAIL"
        lines.append("FAIL: 'observation_count_fan_only' key still present in thermal_model_cache")
    else:
        lines.append("PASS: 'observation_count_fan_only' key absent from thermal_model_cache")

    # Check 3: k_vent_window / k_vent_fan values + counts
    k_vw = cache.get("k_vent_window")
    k_vf = cache.get("k_vent_fan")
    n_vw = cache.get("observation_count_vent_window", 0)
    n_vf = cache.get("observation_count_vent_fan", 0)
    lines.append(f"INFO: k_vent_window={k_vw} (observation_count_vent_window={n_vw})")
    if k_vw is not None and k_vw >= 0:
        overall = "FAIL" if overall != "FAIL" else overall
        lines.append(f"FAIL: k_vent_window={k_vw} has wrong sign (expected < 0)")
    lines.append(
        f"INFO: k_vent_fan={k_vf} (observation_count_vent_fan={n_vf})"
        + (" — not yet exercised (WHF hasn't run with windows open); not itself a failure" if k_vf is None else "")
    )
    if k_vf is not None and k_vf >= 0:
        overall = "FAIL" if overall != "FAIL" else overall
        lines.append(f"FAIL: k_vent_fan={k_vf} has wrong sign (expected < 0)")

    # Check 4: no post-upgrade entry with the old hvac_mode strings
    observations = db.get("thermal_observations")
    legacy_hits = 0
    if isinstance(observations, list):
        legacy_hits = sum(
            1 for o in observations if isinstance(o, dict) and o.get("hvac_mode") in ("ventilated", "fan_only")
        )
    if legacy_hits:
        overall = "FAIL"
        lines.append(
            f"FAIL: {legacy_hits} thermal_observations entr{'y' if legacy_hits == 1 else 'ies'} still "
            f"carr{'ies' if legacy_hits == 1 else 'y'} the old hvac_mode 'ventilated'/'fan_only' — the old "
            "commit path may still be live somewhere (a #133-class parallel-path bug)"
        )
    else:
        lines.append("PASS: no thermal_observations entries carry the old 'ventilated'/'fan_only' hvac_mode")

    # Check 5: REJECT_NO_K_PASSIVE (Defect B, #852)
    rejection_log = db.get("rejection_log") or {}
    solar_rejections = rejection_log.get("solar_gain") or []
    no_k_passive_events = [
        e for e in solar_rejections if isinstance(e, dict) and e.get("reason_code") == "no_k_passive"
    ]
    no_k_passive_count = len(no_k_passive_events)
    k_passive_now = cache.get("k_passive")
    if no_k_passive_count and k_passive_now is not None:
        # k_passive is populated right now, yet the rejection log still carries
        # no_k_passive events — expected only if they all predate k_passive's
        # first commit (fresh-install ramp-up); can't disambiguate from a single
        # snapshot, so WARN (not FAIL) and point at the daily-check comparison.
        overall = "FAIL" if overall == "FAIL" else "WARN"
        lines.append(
            f"WARN: REJECT_NO_K_PASSIVE count={no_k_passive_count} while k_passive={k_passive_now:.4f} is "
            "already populated — expected to be zero going forward once k_passive exists; if this count "
            "is still increasing day over day (compare against yesterday's #854 check), that's a FAIL, "
            "not a WARN — the cache read in the solar_gain commit path may be broken"
        )
    else:
        lines.append(
            f"INFO: REJECT_NO_K_PASSIVE count={no_k_passive_count} "
            f"(k_passive currently {'populated' if k_passive_now is not None else 'not yet populated'} — "
            "expect this count flat/zero within a day of install once k_passive exists; a single run here "
            "can't detect a climbing trend, compare against yesterday's #854 check for that)"
        )

    for line in lines:
        print(line)
    print()
    print(f"Overall: {overall}")
    print()


# ---------------------------------------------------------------------------
# Section D: Chart-log endpoint observations
# ---------------------------------------------------------------------------


def _print_chart_log_endpoint_obs(db: dict) -> None:
    """Print committed observations that came from the chart_log endpoint estimator."""
    observations = db.get("thermal_observations")
    if not isinstance(observations, list):
        print("Chart-Log Endpoint Observations")
        print("--------------------------------")
        print("(no thermal_observations found in learning DB)")
        print()
        return

    endpoint_obs = [o for o in observations if isinstance(o, dict) and o.get("source") == "chart_log_endpoint"]
    total = len(endpoint_obs)
    # Show most-recent first
    recent = list(reversed(endpoint_obs))[:20]

    print(f"Chart-Log Endpoint Observations (last {min(len(recent), total)} of {total})")
    print("-" * 68)

    header = (
        _pad("date", 12)
        + _pad("hvac_mode", 12)
        + _pad("k", 10)
        + _pad("dt_h", 8)
        + _pad("delta_F", 9)
        + _pad("grade", 8)
        + "ratio"
    )
    print(header)
    print("-" * 68)

    for obs in recent:
        date = obs.get("date", "?")
        hvac_mode = obs.get("hvac_mode", "?")
        k = obs.get("k_passive")
        k_str = f"{k:.4f}" if isinstance(k, float) else "-"
        dt_h = obs.get("elapsed_hours")
        dt_str = f"{dt_h:.1f}" if isinstance(dt_h, float) else "-"
        delta = obs.get("delta_t_f")
        delta_str = f"{delta:.1f}" if isinstance(delta, float) else "-"
        grade = obs.get("confidence_grade", "-")
        ratio = obs.get("ratio")
        ratio_str = f"{ratio:.3f}" if isinstance(ratio, float) else "-"

        row = (
            _pad(str(date), 12)
            + _pad(str(hvac_mode), 12)
            + _pad(k_str, 10)
            + _pad(dt_str, 8)
            + _pad(delta_str, 9)
            + _pad(str(grade), 8)
            + ratio_str
        )
        print(row)

    if total == 0:
        print("  (none yet — backfill fires on next HA restart if not already done)")

    print()


# ---------------------------------------------------------------------------
# Section E: Live pending observations (optional, requires HA_URL + HA_TOKEN)
# ---------------------------------------------------------------------------

COL_PL_TYPE = 20
COL_PL_STATUS = 12
COL_PL_ELAPSED = 11
COL_PL_SAMPLES = 9
COL_PL_LAST = 11
COL_PL_INDOOR = 17
COL_PL_OUTDOOR = 11


def _fmt_optional(value: float | None, suffix: str, decimals: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f} {suffix}"


def _print_live_pipeline(pipeline: dict) -> None:
    pending = pipeline.get("pending") or []
    rejection_counts = pipeline.get("rejection_log_counts") or {}

    print("Current Observations (live)")
    print("-" * 28)

    header = (
        _pad("obs_type", COL_PL_TYPE)
        + _pad("status", COL_PL_STATUS)
        + _pad("elapsed", COL_PL_ELAPSED)
        + _pad("samples", COL_PL_SAMPLES)
        + _pad("last_smp", COL_PL_LAST)
        + _pad("indoor", COL_PL_INDOOR)
        + _pad("outdoor", COL_PL_OUTDOOR)
        + "delta"
    )
    print(header)

    if not pending:
        print("  (no active observations)")
    else:
        for obs in pending:
            obs_type = obs.get("obs_type", "?")
            status = obs.get("status", "?")
            elapsed = _fmt_optional(obs.get("elapsed_minutes"), "min")
            samples = str(obs.get("sample_count", "-"))
            last_smp = _fmt_optional(obs.get("last_sample_age_minutes"), "min")
            indoor_range = obs.get("indoor_range_f")
            if indoor_range and len(indoor_range) == 2:
                indoor = f"{indoor_range[0]:.1f}-{indoor_range[1]:.1f} F"
            else:
                indoor = "-"
            outdoor = _fmt_optional(obs.get("outdoor_f"), "F")
            delta = _fmt_optional(obs.get("indoor_delta_f"), "F", decimals=2)

            row = (
                _pad(obs_type, COL_PL_TYPE)
                + _pad(status, COL_PL_STATUS)
                + _pad(elapsed, COL_PL_ELAPSED)
                + _pad(samples, COL_PL_SAMPLES)
                + _pad(last_smp, COL_PL_LAST)
                + _pad(indoor, COL_PL_INDOOR)
                + _pad(outdoor, COL_PL_OUTDOOR)
                + delta
            )
            print(row)

    if rejection_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(rejection_counts.items()))
        print(f"\n(Rejection log entries: {parts})")

    print()


# ---------------------------------------------------------------------------
# Section F: Nightly setback records
# ---------------------------------------------------------------------------


def _print_daily_records(db: dict, n: int = 30) -> None:
    """Print last N nightly setback records."""
    records = db.get("records")
    if not isinstance(records, list):
        print("Nightly Setback Records")
        print("-----------------------")
        print("(no records found in learning DB)")
        print()
        return

    last = records[-n:] if len(records) >= n else records
    print(f"Nightly Setback Records (last {len(last)} of {len(records)})")
    print("-" * 70)
    header = (
        _pad("Date", 12)
        + _pad("DayType", 9)
        + _pad("Mode", 6)
        + _pad("Applied", 9)
        + _pad("Depth", 8)
        + _pad("Adaptive", 10)
        + "Skipped"
    )
    print(header)
    print("-" * 70)

    for rec in last:
        if not isinstance(rec, dict):
            continue
        date = rec.get("date", "?")
        day_type = rec.get("day_type", "?")
        mode = rec.get("hvac_mode_recommended", "?")
        heat = rec.get("setback_heat_applied_f")
        cool = rec.get("setback_cool_applied_f")
        applied = f"{heat:.1f}°F" if heat is not None else (f"{cool:.1f}°F" if cool is not None else "--")
        depth = rec.get("setback_depth_f")
        depth_str = f"{depth:.1f}°F" if depth is not None else "--"
        adaptive = rec.get("setback_was_adaptive")
        adaptive_str = "yes" if adaptive is True else ("no" if adaptive is False else "--")
        skipped = rec.get("setback_skipped_reason") or "--"

        row = (
            _pad(str(date), 12)
            + _pad(str(day_type), 9)
            + _pad(str(mode), 6)
            + _pad(applied, 9)
            + _pad(depth_str, 8)
            + _pad(adaptive_str, 10)
            + skipped
        )
        print(row)

    if not last:
        print("  (no records yet)")
    print()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Climate Advisor thermal learning DB diagnostic")
    parser.add_argument("--rejections", action="store_true", help="Show rejection log only")
    parser.add_argument("--committed", action="store_true", help="Show committed observations only")
    parser.add_argument("--model", action="store_true", help="Show model summary only")
    parser.add_argument("--thermal", action="store_true", help="Show chart_log endpoint observations only")
    parser.add_argument(
        "--pending",
        action="store_true",
        help=(
            "Show only the live in-flight observations (needs HA_URL/HA_TOKEN). They are never persisted: "
            "a run in progress at a restart is learned from only after it (Issue #1437)."
        ),
    )
    parser.add_argument(
        "--swing",
        action="store_true",
        help=(
            "Show how the swing was learned: per-cycle inputs and verdicts, reject histogram, recomputed "
            "median, and coast overshoot (Issue #1233). Rows follow --last (default 15)."
        ),
    )
    parser.add_argument(
        "--check-587",
        action="store_true",
        dest="check_587",
        help=(
            "PASS/WARN/FAIL summary for the Issue #587 k_vent/fan_only_decay retirement + "
            "vent_window_decay/vent_fan_decay split (post-deploy validation)"
        ),
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="Last N rejections per type (default 5) or, with --swing, last N cycles per mode (default 15)",
    )
    parser.add_argument(
        "--type",
        dest="obs_type",
        metavar="TYPE",
        help="Filter --rejections to a specific obs_type (e.g. hvac_cool)",
    )
    parser.add_argument(
        "--daily",
        type=int,
        nargs="?",
        const=30,
        default=None,
        metavar="N",
        help="Show last N nightly setback records (default 30)",
    )
    parser.add_argument(
        "--entry-id",
        default=None,
        help="Zone config entry_id to target (multi-zone installs). Auto-detected when only one zone's file exists.",
    )
    return parser


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Determine which sections to show
    show_daily = args.daily is not None
    daily_n = args.daily if args.daily is not None else 30
    show_pending = getattr(args, "pending", False)
    show_check_587 = getattr(args, "check_587", False)
    filter_type = getattr(args, "obs_type", None)
    show_swing = getattr(args, "swing", False)
    last_n = args.last if args.last is not None else (15 if show_swing else 5)
    section_flag = (
        args.rejections
        or args.committed
        or args.model
        or args.thermal
        or show_daily
        or show_pending
        or show_check_587
        or show_swing
    )
    show_model = args.model or args.thermal or not section_flag
    show_rejections = args.rejections or not section_flag
    show_committed = args.committed or not section_flag
    show_thermal = args.thermal or not section_flag

    config = load_config()

    print(f"Reading {LEARNING_DB_FILE} from {config['HA_HOST']} ...")
    resolved: dict = {}
    db = fetch_learning_db(config, entry_id=getattr(args, "entry_id", None), resolved=resolved)
    print()

    if show_check_587:
        _print_check_587(db)

    if show_model:
        _print_model_summary(db)
        _print_solar_model_section(db)

    if show_swing:
        _print_swing_debug(db, last_n)

    if show_rejections:
        _print_rejection_log(db, last_n=last_n, filter_type=filter_type)

    if show_committed:
        _print_committed(db)

    if show_thermal:
        _print_chart_log_endpoint_obs(db)

    if show_daily:
        _print_daily_records(db, n=daily_n)

    # Live pending observations via REST API (optional)
    _load_dotenv()
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")

    if ha_url and ha_token:
        # Issue #1418: the live section must describe the same zone as the file above, never the first zone.
        try:
            live_zone = resolve_api_zone(ha_url, ha_token, resolved.get("entry_id"))
        except ZoneSelectionRequired as exc:
            print("(live pending observations skipped: several zones are loaded — pass --entry-id, one of:)")
            print(format_zone_choices(exc.zones))
            return
        live_data = _fetch_live_pipeline(ha_url, ha_token, live_zone)
        if live_data is not None:
            _print_live_pipeline(live_data["thermal_pipeline"])
        else:
            print("(could not fetch live pending observations — is the integration running?)")
    else:
        print("(set HA_URL and HA_TOKEN in .env to also see live pending observations)")


if __name__ == "__main__":
    main()
