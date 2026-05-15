#!/usr/bin/env python3
"""
thermal_replay.py — Replay historical HA sensor data through the thermal learning OLS.

Fetches state history for indoor/outdoor temp and window sensors, reconstructs
ventilated_decay observation windows, runs the same OLS as the live learning
engine (2-param primary when sf_range >= 0.30, 1-param fallback), and merges
results into the HA learning DB.

Requirements:
  - tools/.env (or .env) with HA_URL and HA_TOKEN
  - tools/.deploy.env (or .deploy.env) with SSH config (for --write)

Usage:
  python tools/thermal_replay.py \\
      --climate-entity climate.ecobee \\
      --outdoor-entity sensor.outdoor_temp \\
      --window-entities binary_sensor.back_door \\
      --dry-run

  python tools/thermal_replay.py ... --days 14 --write

Entity hints (check HA → Developer Tools → States):
  --climate-entity   Thermostat (indoor temp from current_temperature attribute)
  --outdoor-entity   Outdoor sensor (numeric state, e.g. sensor.weather_temperature)
  --window-entities  Comma-separated binary_sensors: state on=open, off=closed
"""

import argparse
import json
import math
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_dotenv(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return result


def load_config() -> dict[str, str]:
    # env first (HA_URL, HA_TOKEN), deploy for SSH (HA_HOST, HA_SSH_*)
    env = _load_dotenv("tools/.env") or _load_dotenv(".env")
    deploy = _load_dotenv("tools/.deploy.env") or _load_dotenv(".deploy.env")
    merged = {**deploy, **env}
    # Fall back: derive HA_URL from HA_HOST if HA_URL not set
    if not merged.get("HA_URL") and merged.get("HA_HOST"):
        merged["HA_URL"] = f"http://{merged['HA_HOST']}:8123"
    # Fall back: HA_API_TOKEN → HA_TOKEN if HA_TOKEN not set
    if not merged.get("HA_TOKEN") and merged.get("HA_API_TOKEN"):
        merged["HA_TOKEN"] = merged["HA_API_TOKEN"]
    return merged


# ---------------------------------------------------------------------------
# HA REST API
# ---------------------------------------------------------------------------


def _ha_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_entity_history(base_url: str, token: str, entity_ids: list[str], days: int) -> dict[str, list[dict]]:
    """Return {entity_id: [state_dicts]} for the requested entities and window."""
    start = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    entity_param = ",".join(urllib.parse.quote(e) for e in entity_ids)
    url = (
        f"{base_url}/api/history/period/{urllib.parse.quote(start)}"
        f"?filter_entity_id={entity_param}&minimal_response=false&significant_changes_only=false"
    )
    req = urllib.request.Request(url, headers=_ha_headers(token))
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    result: dict[str, list[dict]] = {}
    for entity_states in data:
        if entity_states:
            eid = entity_states[0].get("entity_id", "")
            result[eid] = entity_states
    return result


def fetch_ha_config(base_url: str, token: str) -> dict:
    """Fetch HA system config (for unit_system)."""
    req = urllib.request.Request(f"{base_url}/api/config", headers=_ha_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# State interpolation
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def build_time_grid(start: datetime, end: datetime, interval_minutes: int = 5) -> list[datetime]:
    grid: list[datetime] = []
    t = start
    while t <= end:
        grid.append(t)
        t += timedelta(minutes=interval_minutes)
    return grid


def _advance_state_index(sorted_states: list[dict], j: int, ts: datetime) -> int:
    while j + 1 < len(sorted_states) and _parse_ts(sorted_states[j + 1]["last_changed"]) <= ts:
        j += 1
    return j


def interpolate_numeric(states: list[dict], timestamps: list[datetime], attr: str | None = None) -> list[float | None]:
    sorted_states = sorted(states, key=lambda s: s["last_changed"])
    result: list[float | None] = []
    j = 0
    first_ts = _parse_ts(sorted_states[0]["last_changed"])
    for ts in timestamps:
        j = _advance_state_index(sorted_states, j, ts)
        if first_ts > ts:
            result.append(None)
            continue
        state = sorted_states[j]
        try:
            raw = state["attributes"].get(attr) if attr else state["state"]
            result.append(float(raw))
        except (ValueError, TypeError, KeyError):
            result.append(None)
    return result


def interpolate_categorical(
    states: list[dict], timestamps: list[datetime], attr: str | None = None
) -> list[str | None]:
    sorted_states = sorted(states, key=lambda s: s["last_changed"])
    result: list[str | None] = []
    j = 0
    first_ts = _parse_ts(sorted_states[0]["last_changed"])
    for ts in timestamps:
        j = _advance_state_index(sorted_states, j, ts)
        if first_ts > ts:
            result.append(None)
            continue
        state = sorted_states[j]
        if attr:
            result.append(str(state["attributes"].get(attr, "")))
        else:
            result.append(str(state["state"]))
    return result


# ---------------------------------------------------------------------------
# Solar factor (mirrors learning.py _solar_factor)
# ---------------------------------------------------------------------------


def _solar_factor(hour: float) -> float:
    if hour < 8 or hour >= 18:
        return 0.0
    return round(math.sin(math.pi * (hour - 8) / 10.0), 4)


# ---------------------------------------------------------------------------
# OLS (mirrors learning.py — pure Python, no HA imports)
# ---------------------------------------------------------------------------

_K_PASSIVE_MIN = -0.5
_K_PASSIVE_MAX = -0.001
_MIN_R2 = 0.20
_MIN_SAMPLES = 4
_SF_RANGE_MIN = 0.30
_K_SOLAR_MAX = 8.0


def _smooth_temps(temps: list[float]) -> list[float]:
    if len(temps) < 3:
        return list(temps)
    smoothed = [temps[0]]
    for i in range(1, len(temps) - 1):
        smoothed.append((temps[i - 1] + temps[i] + temps[i + 1]) / 3.0)
    smoothed.append(temps[-1])
    return smoothed


def compute_k_passive(
    samples: list[dict],
    pre_samples: list[dict] | None = None,
    min_samples: int | None = None,
) -> tuple[float | None, float, str | None]:
    """Estimate k_passive from post (and optional pre) HVAC-off sample windows.

    Mirrors learning.py compute_k_passive() exactly, including:
    - Separate window processing (no rates computed across pre→post boundary)
    - min_samples override
    - Same OLS formula and rejection codes

    For the ventilated-decay (single-window) path, call with pre_samples=None.
    For the HVAC heat/cool path, pass post_samples + pre_samples separately.
    """
    _min_s = min_samples if min_samples is not None else _MIN_SAMPLES

    # Build list of windows; process each independently to avoid boundary spikes
    windows: list[list[dict]] = []
    if pre_samples:
        windows.append(list(pre_samples))
    windows.append(list(samples))

    total = sum(len(w) for w in windows)
    if total < _min_s + 1:
        return None, 0.0, "too_few_samples"

    rates: list[float] = []
    deltas: list[float] = []
    for window in windows:
        if len(window) < 2:
            continue
        indoor_raw = [s["indoor_temp_f"] for s in window]
        outdoor_w = [s["outdoor_temp_f"] for s in window]
        elapsed_w = [s["elapsed_minutes"] for s in window]
        indoor = _smooth_temps(indoor_raw)
        for i in range(len(indoor) - 1):
            dt_h = (elapsed_w[i + 1] - elapsed_w[i]) / 60.0
            if dt_h <= 0:
                continue
            rate = (indoor[i + 1] - indoor[i]) / dt_h
            delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor_w[i] + outdoor_w[i + 1]) / 2.0)
            rates.append(rate)
            deltas.append(delta)

    if len(rates) < _min_s:
        return None, 0.0, "too_few_samples"
    sum_rd = sum(r * d for r, d in zip(rates, deltas, strict=False))
    sum_d2 = sum(d * d for d in deltas)
    if sum_d2 == 0:
        return None, 0.0, "small_delta"
    k_p = sum_rd / sum_d2
    if k_p > 0:
        return None, 0.0, "ols_wrong_sign"
    if not (_K_PASSIVE_MIN <= k_p <= _K_PASSIVE_MAX):
        return None, 0.0, "ols_bounds"
    ss_res = sum((r - k_p * d) ** 2 for r, d in zip(rates, deltas, strict=False))
    ss_tot = sum(r * r for r in rates)
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    if r2 < _MIN_R2:
        return None, r2, "ols_bad_fit"
    return k_p, r2, None


def compute_k_env_solar(samples: list[dict]) -> tuple[float | None, float | None, float | None]:
    pairs: list[tuple[float, float, float]] = []
    for i in range(len(samples) - 1):
        s0, s1 = samples[i], samples[i + 1]
        dt_h = (s1["elapsed_minutes"] - s0["elapsed_minutes"]) / 60.0
        if dt_h <= 0:
            continue
        rate = (s1["indoor_temp_f"] - s0["indoor_temp_f"]) / dt_h
        delta = ((s0["indoor_temp_f"] - s0["outdoor_temp_f"]) + (s1["indoor_temp_f"] - s1["outdoor_temp_f"])) / 2.0
        sf = (s0["solar_factor"] + s1["solar_factor"]) / 2.0
        pairs.append((rate, delta, sf))
    if len(pairs) < 4:
        return None, None, None
    rates = [p[0] for p in pairs]
    deltas = [p[1] for p in pairs]
    sfs = [p[2] for p in pairs]
    sf_range = max(sfs) - min(sfs)
    if sf_range < _SF_RANGE_MIN:
        return None, None, None
    x1x1 = sum(d * d for d in deltas)
    x2x2 = sum(s * s for s in sfs)
    x1x2 = sum(d * s for d, s in zip(deltas, sfs, strict=False))
    x1y = sum(d * r for d, r in zip(deltas, rates, strict=False))
    x2y = sum(s * r for s, r in zip(sfs, rates, strict=False))
    det = x1x1 * x2x2 - x1x2 * x1x2
    if abs(det) < 1e-12:
        return None, None, None
    k_env = (x2x2 * x1y - x1x2 * x2y) / det
    k_sol = (x1x1 * x2y - x1x2 * x1y) / det
    mean_r = sum(rates) / len(rates)
    ss_res = sum((r - k_env * d - k_sol * s) ** 2 for r, d, s in zip(rates, deltas, sfs, strict=False))
    ss_tot = sum((r - mean_r) ** 2 for r in rates)
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return k_env, k_sol, r2


# ---------------------------------------------------------------------------
# Window detection
# ---------------------------------------------------------------------------

_IDLE_ACTIONS = {"idle", "off", ""}


def find_ventilated_windows(
    timestamps: list[datetime],
    indoor_temps: list[float | None],
    outdoor_temps: list[float | None],
    hvac_actions: list[str | None],
    window_open: list[bool | None],
    min_window_minutes: int = 60,
    max_window_minutes: int = 240,
    sample_interval: int = 5,
) -> list[list[dict]]:
    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush():
        if len(current) * sample_interval >= min_window_minutes:
            windows.append(list(current))
        current.clear()

    for i, ts in enumerate(timestamps):
        in_t = indoor_temps[i]
        out_t = outdoor_temps[i]
        hvac = hvac_actions[i]
        win = window_open[i]

        if in_t is None or out_t is None:
            _flush()
            continue

        # HVAC idle = not actively heating or cooling
        hvac_idle = (hvac in _IDLE_ACTIONS) or (hvac is not None and "heat" not in hvac and "cool" not in hvac)
        win_open = win is True

        if hvac_idle and win_open:
            elapsed = len(current) * sample_interval
            current.append(
                {
                    "indoor_temp_f": in_t,
                    "outdoor_temp_f": out_t,
                    "elapsed_minutes": elapsed,
                    "solar_factor": _solar_factor(ts.hour),
                    "timestamp": ts.isoformat(),
                }
            )
            if len(current) * sample_interval >= max_window_minutes:
                _flush()
        else:
            _flush()

    _flush()
    return windows


# ---------------------------------------------------------------------------
# OLS dispatch — mirrors Phase D 2-param primary path
# ---------------------------------------------------------------------------


def run_ols_on_window(window: list[dict]) -> dict | None:
    sf_vals = [s["solar_factor"] for s in window]
    sf_range = max(sf_vals) - min(sf_vals) if len(sf_vals) >= 2 else 0.0
    ts_str = window[-1]["timestamp"]
    date_str = ts_str[:10]

    # 2-param primary
    if sf_range >= _SF_RANGE_MIN:
        k_env, k_sol, r2 = compute_k_env_solar(window)
        if (
            k_env is not None
            and k_sol is not None
            and r2 is not None
            and _K_PASSIVE_MIN <= k_env <= 0.001
            and 0.0 <= k_sol <= _K_SOLAR_MAX
            and r2 >= _MIN_R2
        ):
            return {
                "event_id": str(uuid.uuid4()),
                "timestamp": ts_str,
                "date": date_str,
                "hvac_mode": "ventilated",
                "k_passive": round(k_env, 5),
                "k_solar": round(k_sol, 3),
                "k_active": None,
                "r_squared_passive": round(r2, 3),
                "r_squared_active": None,
                "sample_count_post": len(window),
                "confidence_grade": "low",
                "schema_version": 2,
                "two_param": True,
                "source": "replay",
            }

    # 1-param fallback
    k_p, r2_p, _ = compute_k_passive(window)
    if k_p is None:
        return None
    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": ts_str,
        "date": date_str,
        "hvac_mode": "ventilated",
        "k_passive": round(k_p, 5),
        "k_active": None,
        "r_squared_passive": round(r2_p, 3),
        "r_squared_active": None,
        "sample_count_post": len(window),
        "confidence_grade": "low",
        "schema_version": 2,
        "source": "replay",
    }


# ---------------------------------------------------------------------------
# EWMA model cache rebuild — mirrors learning.py _update_thermal_model_cache
# ---------------------------------------------------------------------------

_ALPHA_MAP = {"high": 0.3, "medium": 0.15, "low": 0.05}
_THERMAL_OBS_CAP = 500


def _ewma(old: float | None, new: float, alpha: float) -> float:
    return new if old is None else (1.0 - alpha) * old + alpha * new


def rebuild_model_cache(all_obs: list[dict]) -> dict:
    """Recompute thermal_model_cache EWMA from all obs in chronological order.

    Mirrors _update_thermal_model_cache logic exactly:
    - passive/heat/cool modes update k_passive
    - fan_only updates k_vent
    - ventilated updates k_vent_window (and k_solar when two_param=True)
    - solar updates k_solar
    """
    sorted_obs = sorted(all_obs, key=lambda o: o.get("timestamp", o.get("date", "")))
    cache: dict = {
        "k_passive": None,
        "k_active_heat": None,
        "k_active_cool": None,
        "k_vent": None,
        "k_vent_window": None,
        "k_solar": None,
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "observation_count_passive": 0,
        "observation_count_fan_only": 0,
        "observation_count_vent": 0,
        "observation_count_solar": 0,
        "swing_heat_f": None,
        "swing_cool_f": None,
        "observation_count_swing_heat": 0,
        "observation_count_swing_cool": 0,
        "avg_r_squared_passive": None,
        "last_observation_date": None,
        "confidence_k_passive": "none",
        "confidence_k_hvac": "none",
    }

    for obs in sorted_obs:
        grade = obs.get("confidence_grade", "low")
        alpha = _ALPHA_MAP.get(grade, 0.05)
        mode = obs.get("hvac_mode", "")
        k_p = obs.get("k_passive")
        k_a = obs.get("k_active")
        k_sol = obs.get("k_solar")
        r2_p = obs.get("r_squared_passive")

        envelope_mode = mode not in ("fan_only", "ventilated")

        if k_p is not None and envelope_mode:
            cache["k_passive"] = _ewma(cache["k_passive"], k_p, alpha)
        if r2_p is not None and envelope_mode:
            cache["avg_r_squared_passive"] = _ewma(cache["avg_r_squared_passive"], r2_p, alpha)

        if mode == "heat" and k_a is not None:
            cache["k_active_heat"] = _ewma(cache["k_active_heat"], k_a, alpha)
            cache["observation_count_heat"] += 1
        elif mode == "cool" and k_a is not None:
            cache["k_active_cool"] = _ewma(cache["k_active_cool"], k_a, alpha)
            cache["observation_count_cool"] += 1
        elif mode == "passive":
            cache["observation_count_passive"] += 1
        elif mode == "fan_only":
            if k_p is not None:
                cache["k_vent"] = _ewma(cache["k_vent"], k_p, alpha)
            cache["observation_count_fan_only"] += 1
        elif mode == "ventilated":
            if k_p is not None:
                cache["k_vent_window"] = _ewma(cache["k_vent_window"], k_p, alpha)
            if k_sol is not None and obs.get("two_param"):
                cache["k_solar"] = _ewma(cache["k_solar"], k_sol, alpha)
            cache["observation_count_vent"] += 1
        elif mode == "solar":
            if k_sol is not None:
                cache["k_solar"] = _ewma(cache["k_solar"], k_sol, alpha)
            cache["observation_count_solar"] += 1

        # Swing EWMA — update alongside the k_active EWMA for heat/cool obs
        swing_val = obs.get("swing_f")
        if swing_val is not None:
            if mode == "heat":
                cache["swing_heat_f"] = _ewma(cache["swing_heat_f"], swing_val, alpha)
                cache["observation_count_swing_heat"] += 1
            elif mode == "cool":
                cache["swing_cool_f"] = _ewma(cache["swing_cool_f"], swing_val, alpha)
                cache["observation_count_swing_cool"] += 1

        cache["last_observation_date"] = obs.get("date")

    # Confidence grades
    n_hvac = cache["observation_count_heat"] + cache["observation_count_cool"]
    cache["confidence_k_hvac"] = (
        "high" if n_hvac >= 20 else "medium" if n_hvac >= 10 else "low" if n_hvac >= 1 else "none"
    )
    n_passive = cache["observation_count_passive"]
    cache["confidence_k_passive"] = (
        "high" if n_passive >= 10 else "medium" if n_passive >= 5 else "low" if n_passive >= 1 else "none"
    )

    # Round for readability
    for k in (
        "k_passive",
        "k_active_heat",
        "k_active_cool",
        "k_vent",
        "k_vent_window",
        "k_solar",
        "swing_heat_f",
        "swing_cool_f",
        "avg_r_squared_passive",
    ):
        if cache[k] is not None:
            cache[k] = round(cache[k], 5)

    return cache


# ---------------------------------------------------------------------------
# SSH read/write
# ---------------------------------------------------------------------------

_LEARNING_DB_REMOTE = "/config/climate_advisor_learning.json"
_CHART_LOG_REMOTE = "/config/climate_advisor_chart_log.json"


def _ssh_args(config: dict) -> list[str]:
    host = config.get("HA_HOST", "homeassistant.local")
    user = config.get("HA_SSH_USER", "root")
    key = config.get("HA_SSH_KEY", "")
    port = str(config.get("HA_SSH_PORT", "22"))
    args = ["ssh", f"-p{port}", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        args += ["-i", key]
    args.append(f"{user}@{host}")
    return args


def fetch_learning_json_ssh(config: dict) -> dict:
    result = subprocess.run(
        [*_ssh_args(config), f"cat {_LEARNING_DB_REMOTE}"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read failed: {result.stderr.decode()}")
    return json.loads(result.stdout)


def write_learning_json_ssh(config: dict, data: dict) -> None:
    json_bytes = json.dumps(data, indent=2).encode()
    tmp = f"{_LEARNING_DB_REMOTE}.replay_tmp"
    cmd = f"cat > {tmp} && mv {tmp} {_LEARNING_DB_REMOTE}"
    result = subprocess.run([*_ssh_args(config), cmd], input=json_bytes, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SSH write failed: {result.stderr.decode()}")


def fetch_chart_log_ssh(config: dict) -> list[dict]:
    """SSH into HA and read the chart_log JSON. Returns the entries list."""
    result = subprocess.run(
        [*_ssh_args(config), f"cat {_CHART_LOG_REMOTE}"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read failed: {result.stderr.decode()}")
    data = json.loads(result.stdout)
    return data.get("entries", [])


def build_passive_only_windows(
    entries: list[dict],
    days: int = 30,
    min_window_minutes: int = 120,
    max_window_minutes: int = 720,
) -> list[list[dict]]:
    """Extract passive-only windows from chart_log entries.

    A passive-only window is a contiguous run where:
    - HVAC is idle/off (same check as build_windows_from_chart_log)
    - fan is False (not CA-driven fan or manual fan)
    - windows_open is False
    - Duration >= min_window_minutes (default 120 min)

    Each sample dict matches the format expected by compute_k_passive.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush() -> None:
        if current:
            elapsed_min = (len(current) - 1) * _avg_interval(current)
            if elapsed_min >= min_window_minutes:
                windows.append(list(current))
            current.clear()

    def _avg_interval(samples: list[dict]) -> float:
        if len(samples) < 2:
            return 30.0
        ts0 = datetime.fromisoformat(samples[0]["timestamp"])
        ts1 = datetime.fromisoformat(samples[-1]["timestamp"])
        return (ts1 - ts0).total_seconds() / 60.0 / max(len(samples) - 1, 1)

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "")
        fan = entry.get("fan")
        windows_open = entry.get("windows_open")

        if indoor is None or outdoor is None:
            _flush()
            continue

        hvac_idle = hvac in ("idle", "off", "", "fan") or ("heat" not in (hvac or "") and "cool" not in (hvac or ""))
        fan_off = fan is False or fan is None
        win_closed = not windows_open

        if not hvac_idle or not fan_off or not win_closed:
            _flush()
            continue

        elapsed = 0.0
        if current:
            ts0 = datetime.fromisoformat(current[0]["timestamp"])
            elapsed = (ts - ts0).total_seconds() / 60.0

        current.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed,
                "solar_factor": _solar_factor(ts.hour + ts.minute / 60.0),
                "timestamp": ts_str,
            }
        )

        if elapsed >= max_window_minutes:
            _flush()

    _flush()
    return windows


def _endpoint_k_passive(
    window: list[dict],
) -> tuple[float | None, str | None, str]:
    """Endpoint estimator for k_passive using Newton's law of cooling.

    k = ln((T_end - T_out_avg) / (T_start - T_out_avg)) / dt_hours

    Quality gates:
    - |T_end - T_start| >= 1.0 degF
    - dt_hours >= 2.0
    - k in [-0.5, -0.001]

    Confidence:
    - "medium": dt_hours >= 6 AND |deltaT| >= 2
    - "low": otherwise

    Returns (k, confidence, reject_reason).
    reject_reason is "" on success.
    """
    if len(window) < 2:
        return None, None, "too_few_samples"

    t_start = window[0]["indoor_temp_f"]
    t_end = window[-1]["indoor_temp_f"]
    t_out_avg = sum(s["outdoor_temp_f"] for s in window) / len(window)

    ts0 = datetime.fromisoformat(window[0]["timestamp"])
    ts1 = datetime.fromisoformat(window[-1]["timestamp"])
    dt_hours = (ts1 - ts0).total_seconds() / 3600.0

    delta_t = abs(t_end - t_start)
    if delta_t < 1.0:
        return None, None, "delta_too_small"
    if dt_hours < 2.0:
        return None, None, "window_too_short"

    denom = t_start - t_out_avg
    if abs(denom) < 0.01:
        return None, None, "small_delta_to_outdoor"

    numerator = t_end - t_out_avg
    # Avoid log of non-positive number: ratio must be positive and < 1 (cooling toward outdoor)
    ratio = numerator / denom
    if ratio <= 0 or ratio >= 1.0:
        return None, None, "ratio_out_of_range"

    k = math.log(ratio) / dt_hours
    if not (_K_PASSIVE_MIN <= k <= _K_PASSIVE_MAX):
        return None, None, "bounds"

    confidence = "medium" if (dt_hours >= 6.0 and delta_t >= 2.0) else "low"
    return k, confidence, ""


def run_passive_only_analysis(
    chart_entries: list[dict],
    learning_db: dict,
    days: int = 30,
) -> None:
    """Run passive-only analysis: extract windows, compare OLS vs endpoint estimator,
    simulate EWMA convergence, and compare ODE predictions.

    Prints results to stdout. Does not write to the learning DB.
    """
    print(f"\nExtracting passive-only windows (HVAC off, fan off, windows closed, min 120 min, last {days} days)...")
    windows = build_passive_only_windows(chart_entries, days=days, min_window_minutes=120)
    print(f"  Found {len(windows)} passive-only windows\n")

    if not windows:
        print("No passive-only windows found. Try increasing --days.")
        return

    # Current k_passive from learning DB
    cache = learning_db.get("thermal_model_cache", {})
    current_k_passive: float | None = cache.get("k_passive")
    print(f"Current k_passive from learning DB: {current_k_passive}")
    print()

    _ALPHA_LOW = 0.05
    _ALPHA_MEDIUM = 0.15

    ewma_k = current_k_passive
    endpoint_results: list[tuple[float, str]] = []  # (k, confidence)

    for _i, window in enumerate(windows):
        ts_start = window[0]["timestamp"]
        ts_end = window[-1]["timestamp"]
        ts0 = datetime.fromisoformat(ts_start)
        ts1 = datetime.fromisoformat(ts_end)
        dt_hours = (ts1 - ts0).total_seconds() / 3600.0

        t_start = window[0]["indoor_temp_f"]
        t_end = window[-1]["indoor_temp_f"]
        t_out_avg = sum(s["outdoor_temp_f"] for s in window) / len(window)
        delta_t = t_end - t_start
        n = len(window)

        # Format timestamps for display
        def _fmt_dt(ts_str: str) -> str:
            try:
                dt = datetime.fromisoformat(ts_str)
                return dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ts_str[:16]

        label_start = _fmt_dt(ts_start)
        label_end = _fmt_dt(ts_end)

        print(f"Passive window {label_start} -> {label_end} ({dt_hours:.1f}h):")
        print(
            f"  T_range: {t_start:.0f}F -> {t_end:.0f}F (deltaT={delta_t:+.1f}F)  "
            f"T_out_avg: {t_out_avg:.0f}F  n={n} entries"
        )

        # OLS (consecutive-pair)
        k_ols, r2_ols, reject_ols = compute_k_passive(window)
        if k_ols is not None:
            verdict_ols = "PASS" if r2_ols >= _MIN_R2 else f"WOULD REJECT (R2<{_MIN_R2})"
            print(f"  OLS (consecutive pairs): k={k_ols:+.4f} R2={r2_ols:.3f}  -> {verdict_ols}")
        else:
            print(f"  OLS (consecutive pairs): REJECTED ({reject_ols})")

        # Endpoint estimator
        k_ep, conf_ep, reject_ep = _endpoint_k_passive(window)
        if k_ep is not None:
            print(f"  Endpoint estimator:       k={k_ep:+.4f}          -> confidence={conf_ep}")
            endpoint_results.append((k_ep, conf_ep))  # type: ignore[arg-type]
        else:
            print(f"  Endpoint estimator:       REJECTED ({reject_ep})")

        print()

    # EWMA convergence simulation
    if endpoint_results:
        print("=" * 70)
        print("EWMA convergence simulation (endpoint estimator values):")
        print(f"  Starting k_passive: {current_k_passive}")
        print()

        ewma_k = current_k_passive
        for j, (k_ep, conf_ep) in enumerate(endpoint_results):
            alpha = _ALPHA_MEDIUM if conf_ep == "medium" else _ALPHA_LOW
            ewma_k_new = _ewma(ewma_k, k_ep, alpha)
            ewma_k_str = f"{ewma_k_new:+.5f}" if ewma_k_new is not None else "None"
            prev_str = f"{ewma_k:+.5f}" if ewma_k is not None else "None"
            print(
                f"  Window {j + 1}: k_ep={k_ep:+.4f} conf={conf_ep} alpha={alpha:.2f}  "
                f"k_passive: {prev_str} -> {ewma_k_str}"
            )
            ewma_k = ewma_k_new

        converged_k = ewma_k
        if converged_k is not None:
            print(f"\n  Final converged k_passive: {converged_k:+.5f}")
        else:
            print("\n  No convergence (no valid endpoint estimates)")
        print()

        # ODE prediction comparison: "last night" using last overnight window
        # Find the last window with dt >= 4h to use as overnight
        overnight_window = None
        for window in reversed(windows):
            ts0 = datetime.fromisoformat(window[0]["timestamp"])
            ts1 = datetime.fromisoformat(window[-1]["timestamp"])
            dt_h = (ts1 - ts0).total_seconds() / 3600.0
            if dt_h >= 4.0:
                overnight_window = window
                break

        if overnight_window is not None:
            print("=" * 70)
            print("ODE prediction comparison (last overnight passive window):")
            w = overnight_window
            t_in_0 = w[0]["indoor_temp_f"]
            t_out_avg_w = sum(s["outdoor_temp_f"] for s in w) / len(w)
            ts0_dt = datetime.fromisoformat(w[0]["timestamp"])
            ts1_dt = datetime.fromisoformat(w[-1]["timestamp"])
            dt_h_w = (ts1_dt - ts0_dt).total_seconds() / 3600.0
            t_actual = w[-1]["indoor_temp_f"]

            def _ode_predict(k_p: float | None, t_in: float, t_out: float, dt_h: float) -> float | None:
                if k_p is None:
                    return None
                # Simple passive decay: dT/dt = k_passive * (T_in - T_out)
                # Analytical: T(t) = T_out + (T_in_0 - T_out) * exp(k_passive * dt)
                return t_out + (t_in - t_out) * math.exp(k_p * dt_h)

            pred_current = _ode_predict(current_k_passive, t_in_0, t_out_avg_w, dt_h_w)
            pred_converged = _ode_predict(converged_k, t_in_0, t_out_avg_w, dt_h_w)

            def _fmt_ts(dt: datetime) -> str:
                return dt.strftime("%Y-%m-%d %H:%M")

            print(f"  Window: {_fmt_ts(ts0_dt)} -> {_fmt_ts(ts1_dt)} ({dt_h_w:.1f}h)")
            print(f"  T_start={t_in_0:.1f}F  T_out_avg={t_out_avg_w:.1f}F  T_actual_end={t_actual:.1f}F")
            print()

            if pred_current is not None:
                err_current = pred_current - t_actual
                print(f"  Current k_passive ({current_k_passive:+.5f}):")
                print(f"    Predicted end: {pred_current:.1f}F  Actual: {t_actual:.1f}F  Error: {err_current:+.1f}F")
            else:
                print("  Current k_passive: None (no prediction)")

            if pred_converged is not None and converged_k is not None:
                err_converged = pred_converged - t_actual
                print(f"  Converged k_passive ({converged_k:+.5f}):")
                print(
                    f"    Predicted end: {pred_converged:.1f}F  Actual: {t_actual:.1f}F  Error: {err_converged:+.1f}F"
                )
            else:
                print("  Converged k_passive: None (no prediction)")
            print()
        else:
            print("  (No overnight window >= 4h found for ODE comparison)")
    else:
        print("No valid endpoint estimates — EWMA simulation skipped.")


def build_ventilated_overnight_windows(
    entries: list[dict],
    days: int = 30,
    min_window_minutes: int = 120,
    max_window_minutes: int = 720,
) -> list[list[dict]]:
    """Extract ventilated windows that pass the natural overnight regime filter.

    A ventilated overnight window satisfies:
    - HVAC is idle/off
    - windows_open is True
    - ALL samples in the window have T_out < T_in (T_out stays below T_in throughout)
    - Duration >= min_window_minutes

    The natural regime filter (T_out < T_in throughout) auto-selects overnight conditions
    and rejects morning windows where T_out rises toward or past T_in. No time-of-day
    classification needed.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush() -> None:
        if len(current) < 2:
            current.clear()
            return
        ts0 = datetime.fromisoformat(current[0]["timestamp"])
        ts1 = datetime.fromisoformat(current[-1]["timestamp"])
        elapsed_min = (ts1 - ts0).total_seconds() / 60.0
        if elapsed_min < min_window_minutes:
            current.clear()
            return
        # Natural regime filter: reject windows where T_out ever meets or exceeds T_in
        if all(s["outdoor_temp_f"] < s["indoor_temp_f"] for s in current):
            windows.append(list(current))
        current.clear()

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "")
        windows_open = entry.get("windows_open")

        if indoor is None or outdoor is None:
            _flush()
            continue

        hvac_idle = hvac in ("idle", "off", "", "fan") or ("heat" not in (hvac or "") and "cool" not in (hvac or ""))
        win_open = bool(windows_open)

        if not hvac_idle or not win_open:
            _flush()
            continue

        elapsed = 0.0
        if current:
            ts0 = datetime.fromisoformat(current[0]["timestamp"])
            elapsed = (ts - ts0).total_seconds() / 60.0

        current.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed,
                "solar_factor": _solar_factor(ts.hour + ts.minute / 60.0),
                "timestamp": ts_str,
            }
        )

        if elapsed >= max_window_minutes:
            _flush()

    _flush()
    return windows


def run_vent_overnight_analysis(
    chart_entries: list[dict],
    learning_db: dict,
    days: int = 30,
) -> None:
    """Analyse overnight ventilated windows using endpoint estimator.

    Applies the natural regime filter (T_out < T_in throughout). Morning ventilated
    windows where T_out rises toward T_in are auto-rejected by this filter, confirming
    the mechanism. Prints results to stdout. Does not write to the learning DB.
    """
    print(
        f"\nExtracting overnight ventilated windows (HVAC off, windows open, T_out < T_in throughout, "
        f"min 120 min, last {days} days)..."
    )
    windows = build_ventilated_overnight_windows(chart_entries, days=days, min_window_minutes=120)

    raw_vent_count = _count_raw_ventilated_windows(chart_entries, days=days, min_window_minutes=120)
    rejected_by_filter = raw_vent_count - len(windows)

    print(f"  Raw ventilated windows found: {raw_vent_count}")
    print(f"  Overnight (T_out < T_in throughout): {len(windows)}")
    print(f"  Rejected by natural filter (morning/crossover): {rejected_by_filter}\n")

    if not windows:
        print("No overnight ventilated windows passed the natural filter. Try increasing --days.")
        return

    cache = learning_db.get("thermal_model_cache", {})
    current_k_vent: float | None = cache.get("k_vent_window")
    print(f"Current k_vent_window from learning DB: {current_k_vent}")
    print()

    _ALPHA_LOW = 0.05
    _ALPHA_MEDIUM = 0.15

    ewma_k = current_k_vent
    endpoint_results: list[tuple[float, str]] = []

    for _i, window in enumerate(windows):
        ts_start = window[0]["timestamp"]
        ts_end = window[-1]["timestamp"]
        ts0 = datetime.fromisoformat(ts_start)
        ts1 = datetime.fromisoformat(ts_end)
        dt_hours = (ts1 - ts0).total_seconds() / 3600.0

        t_start = window[0]["indoor_temp_f"]
        t_end = window[-1]["indoor_temp_f"]
        t_out_avg = sum(s["outdoor_temp_f"] for s in window) / len(window)
        delta_t = t_end - t_start
        n = len(window)

        def _fmt_dt(ts_str: str) -> str:
            try:
                dt = datetime.fromisoformat(ts_str)
                return dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                return ts_str[:16]

        label_start = _fmt_dt(ts_start)
        label_end = _fmt_dt(ts_end)

        print(f"Ventilated overnight window {label_start} -> {label_end} ({dt_hours:.1f}h):")
        print(
            f"  T_range: {t_start:.0f}F -> {t_end:.0f}F (deltaT={delta_t:+.1f}F)  "
            f"T_out_avg: {t_out_avg:.0f}F  n={n} entries"
        )

        # OLS (consecutive-pair) — expected to fail for slow overnight drift
        k_ols, r2_ols, reject_ols = compute_k_passive(window)
        if k_ols is not None:
            verdict_ols = "PASS" if r2_ols >= _MIN_R2 else f"WOULD REJECT (R2<{_MIN_R2})"
            print(f"  OLS (consecutive pairs): k={k_ols:+.4f} R2={r2_ols:.3f}  -> {verdict_ols}")
        else:
            print(f"  OLS (consecutive pairs): REJECTED ({reject_ols})")

        # Endpoint estimator
        k_ep, conf_ep, reject_ep = _endpoint_k_passive(window)
        if k_ep is not None:
            print(f"  Endpoint estimator:       k={k_ep:+.4f}          -> confidence={conf_ep}")
            endpoint_results.append((k_ep, conf_ep))  # type: ignore[arg-type]
        else:
            print(f"  Endpoint estimator:       REJECTED ({reject_ep})")

        print()

    # EWMA convergence simulation
    if endpoint_results:
        print("=" * 70)
        print("EWMA convergence simulation (overnight ventilated endpoint values):")
        print(f"  Starting k_vent_window: {current_k_vent}")
        print()

        ewma_k = current_k_vent
        for j, (k_ep, conf_ep) in enumerate(endpoint_results):
            alpha = _ALPHA_MEDIUM if conf_ep == "medium" else _ALPHA_LOW
            ewma_k_new = _ewma(ewma_k, k_ep, alpha)
            ewma_k_str = f"{ewma_k_new:+.5f}" if ewma_k_new is not None else "None"
            prev_str = f"{ewma_k:+.5f}" if ewma_k is not None else "None"
            print(
                f"  Window {j + 1}: k_ep={k_ep:+.4f} conf={conf_ep} alpha={alpha:.2f}  "
                f"k_vent_window: {prev_str} -> {ewma_k_str}"
            )
            ewma_k = ewma_k_new

        converged_k = ewma_k
        if converged_k is not None:
            print(f"\n  Final converged k_vent_window: {converged_k:+.5f}")
            if current_k_vent is not None:
                pct_change = (converged_k - current_k_vent) / abs(current_k_vent) * 100.0
                print(f"  Change from current: {pct_change:+.1f}%  ({current_k_vent:+.5f} -> {converged_k:+.5f})")
        else:
            print("\n  No convergence (no valid endpoint estimates)")
        print()

        # ODE prediction comparison using last overnight ventilated window
        overnight_window = None
        for window in reversed(windows):
            ts0 = datetime.fromisoformat(window[0]["timestamp"])
            ts1 = datetime.fromisoformat(window[-1]["timestamp"])
            dt_h = (ts1 - ts0).total_seconds() / 3600.0
            if dt_h >= 4.0:
                overnight_window = window
                break

        if overnight_window is not None:
            print("=" * 70)
            print("ODE prediction comparison (last overnight ventilated window):")
            w = overnight_window
            t_in_0 = w[0]["indoor_temp_f"]
            t_out_avg_w = sum(s["outdoor_temp_f"] for s in w) / len(w)
            ts0_dt = datetime.fromisoformat(w[0]["timestamp"])
            ts1_dt = datetime.fromisoformat(w[-1]["timestamp"])
            dt_h_w = (ts1_dt - ts0_dt).total_seconds() / 3600.0
            t_actual = w[-1]["indoor_temp_f"]

            def _ode_predict(k_p: float | None, t_in: float, t_out: float, dt_h: float) -> float | None:
                if k_p is None:
                    return None
                return t_out + (t_in - t_out) * math.exp(k_p * dt_h)

            pred_current = _ode_predict(current_k_vent, t_in_0, t_out_avg_w, dt_h_w)
            pred_converged = _ode_predict(converged_k, t_in_0, t_out_avg_w, dt_h_w)

            def _fmt_ts(dt: datetime) -> str:
                return dt.strftime("%Y-%m-%d %H:%M")

            print(f"  Window: {_fmt_ts(ts0_dt)} -> {_fmt_ts(ts1_dt)} ({dt_h_w:.1f}h)")
            print(f"  T_start={t_in_0:.1f}F  T_out_avg={t_out_avg_w:.1f}F  T_actual_end={t_actual:.1f}F")
            print()

            if pred_current is not None:
                err_current = pred_current - t_actual
                print(f"  Current k_vent_window ({current_k_vent:+.5f}):")
                print(f"    Predicted end: {pred_current:.1f}F  Actual: {t_actual:.1f}F  Error: {err_current:+.1f}F")
            else:
                print("  Current k_vent_window: None (no prediction)")

            if pred_converged is not None and converged_k is not None:
                err_converged = pred_converged - t_actual
                print(f"  Converged k_vent_window ({converged_k:+.5f}):")
                print(
                    f"    Predicted end: {pred_converged:.1f}F  Actual: {t_actual:.1f}F  Error: {err_converged:+.1f}F"
                )
            else:
                print("  Converged k_vent_window: None (no prediction)")
            print()
        else:
            print("  (No overnight ventilated window >= 4h found for ODE comparison)")
    else:
        print("No valid endpoint estimates — EWMA simulation skipped.")


def _count_raw_ventilated_windows(
    entries: list[dict],
    days: int = 30,
    min_window_minutes: int = 120,
) -> int:
    """Count ventilated windows (HVAC off, windows open) without natural filter."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    count = 0
    current: list[dict] = []

    def _flush_count() -> None:
        nonlocal count
        if len(current) >= 2:
            ts0 = datetime.fromisoformat(current[0]["timestamp"])
            ts1 = datetime.fromisoformat(current[-1]["timestamp"])
            if (ts1 - ts0).total_seconds() / 60.0 >= min_window_minutes:
                count += 1
        current.clear()

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "")
        windows_open = entry.get("windows_open")

        if indoor is None or outdoor is None:
            _flush_count()
            continue

        hvac_idle = hvac in ("idle", "off", "", "fan") or ("heat" not in (hvac or "") and "cool" not in (hvac or ""))

        if not hvac_idle or not windows_open:
            _flush_count()
            continue

        elapsed = 0.0
        if current:
            ts0_curr = datetime.fromisoformat(current[0]["timestamp"])
            if ts0_curr.tzinfo is None:
                ts0_curr = ts0_curr.replace(tzinfo=UTC)
            elapsed = (ts - ts0_curr).total_seconds() / 60.0

        current.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed,
                "timestamp": ts_str,
            }
        )

    _flush_count()
    return count


def build_windows_from_chart_log(
    entries: list[dict],
    days: int = 30,
    min_window_minutes: int = 60,
    max_window_minutes: int = 240,
) -> list[list[dict]]:
    """Convert chart_log entries into ventilated_decay sample windows.

    Chart_log entries have fields: ts, hvac, fan, indoor, outdoor, windows_open.
    A window is a contiguous run where HVAC is idle and windows_open is True.
    Each sample dict matches the format expected by compute_k_passive/compute_k_env_solar.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush() -> None:
        if current:
            elapsed_min = (len(current) - 1) * _avg_interval(current)
            if elapsed_min >= min_window_minutes:
                windows.append(list(current))
            current.clear()

    def _avg_interval(samples: list[dict]) -> float:
        if len(samples) < 2:
            return 30.0
        ts0 = datetime.fromisoformat(samples[0]["timestamp"])
        ts1 = datetime.fromisoformat(samples[-1]["timestamp"])
        return (ts1 - ts0).total_seconds() / 60.0 / max(len(samples) - 1, 1)

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "")
        windows_open = entry.get("windows_open")

        if indoor is None or outdoor is None:
            _flush()
            continue

        hvac_idle = hvac in ("idle", "off", "", "fan") or ("heat" not in (hvac or "") and "cool" not in (hvac or ""))
        # Exclude fan-only: fan=True but hvac=idle means CA-driven fan, which is ventilated
        # We include fan=True entries as long as HVAC isn't actively heating/cooling
        if not hvac_idle or not windows_open:
            _flush()
            continue

        elapsed = 0.0
        if current:
            ts0 = datetime.fromisoformat(current[0]["timestamp"])
            elapsed = (ts - ts0).total_seconds() / 60.0

        current.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed,
                "solar_factor": _solar_factor(ts.hour + ts.minute / 60.0),
                "timestamp": ts_str,
            }
        )

        if elapsed >= max_window_minutes:
            _flush()

    _flush()
    return windows


# ---------------------------------------------------------------------------
# HVAC cycle detection (--hvac mode, Issue #130 Phase C)
# ---------------------------------------------------------------------------

_HVAC_HEAT_VALS: frozenset[str] = frozenset({"heating", "heat"})
_HVAC_COOL_VALS: frozenset[str] = frozenset({"cooling", "cool"})
_HVAC_IDLE_VALS: frozenset[str | None] = frozenset({"idle", "off", "", None})

# Bounds mirroring const.py (standalone tool — no HA import)
_K_ACTIVE_HEAT_MIN = 0.5
_K_ACTIVE_HEAT_MAX = 15.0
_K_ACTIVE_COOL_MIN = -15.0
_K_ACTIVE_COOL_MAX = -0.5
_THERMAL_MIN_POST_HEAT_SAMPLES = 4  # mirrors THERMAL_MIN_POST_HEAT_SAMPLES (Phase B: was 10)
_THERMAL_HVAC_MIN_DECAY_F = 0.3  # mirrors THERMAL_HVAC_MIN_DECAY_F
_THERMAL_HVAC_MIN_SIGNAL_F = 0.5  # mirrors THERMAL_HVAC_MIN_SIGNAL_F — min ΔT for single-point
_SWING_DEFAULT_F = 1.5  # default thermostat swing when no observations available
_SWING_MIN_F = 0.1  # minimum plausible swing (°F)
_SWING_MAX_F = 5.0  # maximum plausible swing (°F)


def _hvac_category(hvac_str: str | None) -> str:
    """Return 'heat', 'cool', or 'idle' for a chart_log hvac field value."""
    val = (hvac_str or "").lower().strip()
    if val in _HVAC_HEAT_VALS:
        return "heat"
    if val in _HVAC_COOL_VALS:
        return "cool"
    return "idle"


def detect_hvac_cycles(
    entries: list[dict],
    pre_window_minutes: int = 60,
    post_window_minutes: int = 90,
    min_total_samples: int = 4,
) -> list[dict]:
    """Scan chart_log entries for HVAC heat/cool cycles.

    Returns a list of cycle dicts, each containing:
      mode: "heat" or "cool"
      pre_samples: chart_log entries within pre_window_minutes before HVAC start
      active_samples: chart_log entries where HVAC was active
      post_samples: chart_log entries within post_window_minutes after HVAC stop
      start_ts: ISO timestamp of first active entry

    Entries with outdoor=None are included — outdoor fallback is applied later
    at OLS time, not during detection.
    """
    cycles: list[dict] = []
    i = 0
    n = len(entries)

    while i < n:
        cat = _hvac_category(entries[i].get("hvac"))
        if cat not in ("heat", "cool"):
            i += 1
            continue

        mode = cat
        active: list[dict] = []
        while i < n and _hvac_category(entries[i].get("hvac")) in ("heat", "cool"):
            active.append(entries[i])
            i += 1

        if not active:
            continue

        active_start_ts = _parse_ts(active[0]["ts"])
        active_end_ts = _parse_ts(active[-1]["ts"])

        # post_samples: idle entries within post_window_minutes after last active
        post: list[dict] = []
        j = i
        while j < n:
            cat_j = _hvac_category(entries[j].get("hvac"))
            ts_j = _parse_ts(entries[j]["ts"])
            gap_min = (ts_j - active_end_ts).total_seconds() / 60.0
            if gap_min > post_window_minutes:
                break
            if cat_j == "idle":
                post.append(entries[j])
            j += 1

        # pre_samples: idle entries within pre_window_minutes before first active
        # Walk backwards from the entry just before the active block
        pre: list[dict] = []
        k = i - len(active) - 1
        while k >= 0:
            cat_k = _hvac_category(entries[k].get("hvac"))
            ts_k = _parse_ts(entries[k]["ts"])
            gap_min = (active_start_ts - ts_k).total_seconds() / 60.0
            if gap_min > pre_window_minutes:
                break
            if cat_k == "idle":
                pre.insert(0, entries[k])
            k -= 1

        total = len(pre) + len(active) + len(post)
        if total >= min_total_samples:
            cycles.append(
                {
                    "mode": mode,
                    "pre_samples": pre,
                    "active_samples": active,
                    "post_samples": post,
                    "start_ts": active[0]["ts"],
                }
            )

    return cycles


def _chart_entries_to_ols_samples(
    entries: list[dict],
    elapsed_start_ts: datetime,
) -> list[dict]:
    """Convert chart_log entries to OLS-compatible sample dicts.

    Applies outdoor fallback: when a sample has outdoor=None, propagate the
    last known valid outdoor value.  Entries where outdoor remains None after
    fallback (i.e., no prior outdoor ever seen in this sequence) are skipped.

    Returns a list of dicts with keys: indoor_temp_f, outdoor_temp_f,
    elapsed_minutes, solar_factor, timestamp.
    """
    result: list[dict] = []
    last_outdoor: float | None = None
    for entry in entries:
        indoor = entry.get("indoor")
        if indoor is None:
            continue
        outdoor = entry.get("outdoor")
        if outdoor is not None:
            last_outdoor = float(outdoor)
        elif last_outdoor is not None:
            outdoor = last_outdoor
        else:
            continue  # no outdoor at all yet — skip

        ts_str = entry.get("ts", "")
        entry_dt = _parse_ts(ts_str)
        elapsed_min = (entry_dt - elapsed_start_ts).total_seconds() / 60.0
        sf = _solar_factor(entry_dt.hour + entry_dt.minute / 60.0)
        result.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed_min,
                "solar_factor": sf,
                "timestamp": ts_str,
            }
        )
    return result


def compute_swing_from_hvac_events(
    on_entry: dict,
    off_entry: dict,
    mode: str,
) -> float | None:
    """Compute thermostat swing from a chart_log hvac_action_change event pair.

    on_entry: chart_log entry when HVAC turned ON (indoor = T_start)
    off_entry: chart_log entry when HVAC turned OFF (indoor = T_end)
    mode: "heat" or "cool"

    Returns swing in °F or None if signal insufficient or bounds fail.
    """
    t_start = on_entry.get("indoor")
    t_end = off_entry.get("indoor")
    if t_start is None or t_end is None:
        return None
    delta = abs(t_end - t_start)
    if delta < _THERMAL_HVAC_MIN_SIGNAL_F:
        return None
    swing = delta / 2.0
    if not (_SWING_MIN_F <= swing <= _SWING_MAX_F):
        return None
    return round(swing, 2)


def run_hvac_replay_ols(
    cycle: dict,
    k_vent_window_proxy: float | None = None,
) -> dict | None:
    """Run OLS on a detected HVAC cycle and return an observation dict or None.

    Arguments:
        cycle: dict from detect_hvac_cycles(), with keys:
               mode, pre_samples, active_samples, post_samples, start_ts
        k_vent_window_proxy: If not None and k_passive OLS fails, use this
               value as a proxy (bridge-home path, D17).  Forces grade="low".

    Returns the observation dict on success, None on rejection.
    The observation dict mirrors the schema used by existing ventilated_decay
    replay obs so it can be merged into the learning DB directly.
    """
    mode = cycle.get("mode", "heat")  # "heat" or "cool"

    # Establish a common elapsed origin from the earliest entry across all phases
    all_entries = cycle.get("pre_samples", []) + cycle.get("active_samples", []) + cycle.get("post_samples", [])
    if not all_entries:
        return None
    origin_ts = _parse_ts(all_entries[0]["ts"])

    pre_entries = cycle.get("pre_samples", [])
    active_entries = cycle.get("active_samples", [])
    post_entries = cycle.get("post_samples", [])

    pre_samples = _chart_entries_to_ols_samples(pre_entries, origin_ts)
    active_samples = _chart_entries_to_ols_samples(active_entries, origin_ts)
    post_samples = _chart_entries_to_ols_samples(post_entries, origin_ts)

    # Minimum post samples gate (mirrors THERMAL_MIN_POST_HEAT_SAMPLES).
    # When a k_vent_window proxy is available the post-heat OLS path is not used —
    # k_passive comes from the proxy and k_active from a single-point estimator that
    # only needs post[0] for the HVAC-off timestamp.  Drop the minimum to 1.
    _min_post = 1 if k_vent_window_proxy is not None else _THERMAL_MIN_POST_HEAT_SAMPLES
    if len(post_samples) < _min_post:
        return None

    # Plateau guard: reject if post-phase shows essentially no decay.
    # This guard validates post-heat decay quality for k_passive OLS and is
    # irrelevant when k_passive comes from the proxy (single-point path).
    if k_vent_window_proxy is None and post_samples:
        post_peak = max(s["indoor_temp_f"] for s in post_samples)
        post_end = post_samples[-1]["indoor_temp_f"]
        if (post_peak - post_end) < _THERMAL_HVAC_MIN_DECAY_F and (
            active_samples and post_samples[0]["indoor_temp_f"] >= active_samples[-1]["indoor_temp_f"]
        ):
            # No meaningful decay — skip (plateau guard)
            return None

    # Compute k_passive from post (+ pre) samples
    k_p, r2_p, _reject = compute_k_passive(post_samples, pre_samples if pre_samples else None)

    k_passive_from_proxy = False
    if k_p is None:
        # Bridge proxy: use k_vent_window if caller supplies it
        if k_vent_window_proxy is not None and k_vent_window_proxy < 0:
            k_p = k_vent_window_proxy
            k_passive_from_proxy = True
            force_grade = "low"
        else:
            return None
    else:
        force_grade = "low"  # all replay obs are low-confidence by policy

    # Compute k_active: OLS when ≥ 2 active samples, single-point fallback for n_act=1
    k_a: float | None = None
    if len(active_samples) >= 2:
        k_a, _r2_a = _compute_k_active_standalone(active_samples, k_p, mode)

    if k_a is None:
        # Try single-point estimator from timestamps (handles n_act=1)
        k_a = _compute_k_active_single_point_from_cycle(cycle, k_p)
        if k_a is not None:
            print(
                f"  [single-point] k_active={k_a:.3f} "
                f"(n_act={len(active_samples)}, k_passive={'proxy' if k_passive_from_proxy else 'ols'}={k_p:.4f})"
            )

    if k_a is None:
        return None

    # Build timestamp/date from start_ts
    ts_str = cycle.get("start_ts", all_entries[0]["ts"])
    date_str = ts_str[:10]

    # When k_passive came from the proxy (not OLS), emit k_passive=None in the obs so
    # rebuild_model_cache does NOT update the k_passive EWMA with the proxy value.
    # Only k_active EWMA is updated for these cycles.
    k_passive_out = None if k_passive_from_proxy else round(k_p, 5)

    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": ts_str,
        "date": date_str,
        "hvac_mode": mode,
        "k_passive": k_passive_out,
        "k_active": round(k_a, 3),
        "r_squared_passive": None if k_passive_from_proxy else (round(r2_p, 3) if r2_p else None),
        "r_squared_active": None,
        "sample_count_pre": len(pre_samples),
        "sample_count_active": len(active_samples),
        "sample_count_post": len(post_samples),
        "confidence_grade": force_grade,
        "schema_version": 2,
        "source": "replay",
    }


def _compute_k_active_single_point_from_cycle(
    cycle: dict,
    k_passive: float,
) -> float | None:
    """Single-point k_active from chart_log cycle timestamps.

    For n_act=1: uses active[0].ts (HVAC on) and post[0].ts (HVAC off)
    as the true start/end of the active phase.

    Physics: k_active = (T_peak - T_start) / elapsed_hours - k_passive * avg_delta
    where avg_delta = avg(T_in - T_out) across all available readings.
    """
    active = cycle["active_samples"]
    pre = cycle["pre_samples"]
    post = cycle["post_samples"]
    mode = cycle["mode"]

    if not active:
        return None

    # T_start: last pre-heat indoor (most recent reading before HVAC on)
    T_start = pre[-1].get("indoor") if pre else active[0].get("indoor")
    if T_start is None:
        return None

    # T_peak: max indoor across active + first two post entries (heat) / min (cool)
    candidates = active + post[:2]
    indoors = [e.get("indoor") for e in candidates if e.get("indoor") is not None]
    if not indoors:
        return None
    T_peak = max(indoors) if mode == "heat" else min(indoors)

    # elapsed: HVAC on → off, using exact state-change timestamps
    start_dt = _parse_ts(active[0]["ts"])
    end_dt = _parse_ts(post[0]["ts"]) if post else _parse_ts(active[-1]["ts"])
    if start_dt is None or end_dt is None:
        return None
    elapsed_hours = (end_dt - start_dt).total_seconds() / 3600.0
    if elapsed_hours <= 0 or elapsed_hours > 2.0:
        return None

    # avg delta_T from available readings (indoor - outdoor)
    all_entries = (pre[-3:] if pre else []) + active + (post[:2] if post else [])
    pairs = [
        (e["indoor"], e["outdoor"]) for e in all_entries if e.get("indoor") is not None and e.get("outdoor") is not None
    ]
    if not pairs:
        return None
    avg_delta = sum(i - o for i, o in pairs) / len(pairs)

    signal = T_peak - T_start
    if mode == "heat" and signal < _THERMAL_HVAC_MIN_SIGNAL_F:
        return None
    if mode == "cool" and signal > -_THERMAL_HVAC_MIN_SIGNAL_F:
        return None

    gross_rate = signal / elapsed_hours
    k_active = gross_rate - k_passive * avg_delta

    if mode == "heat" and not (_K_ACTIVE_HEAT_MIN <= k_active <= _K_ACTIVE_HEAT_MAX):
        return None
    if mode == "cool" and not (_K_ACTIVE_COOL_MIN <= k_active <= _K_ACTIVE_COOL_MAX):
        return None
    return k_active


def _compute_k_active_standalone(
    active_samples: list[dict],
    k_passive: float,
    session_mode: str,
) -> tuple[float | None, float]:
    """Pure-Python mirror of learning.py compute_k_active() — no HA imports."""
    if len(active_samples) < 2:
        return None, 0.0

    indoor_raw = [s["indoor_temp_f"] for s in active_samples]
    outdoor = [s["outdoor_temp_f"] for s in active_samples]
    elapsed = [s["elapsed_minutes"] for s in active_samples]
    indoor = _smooth_temps(indoor_raw)

    k_actives: list[float] = []
    for i in range(len(indoor) - 1):
        dt_hours = (elapsed[i + 1] - elapsed[i]) / 60.0
        if dt_hours <= 0:
            continue
        rate = (indoor[i + 1] - indoor[i]) / dt_hours
        delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor[i] + outdoor[i + 1]) / 2.0)
        k_actives.append(rate - k_passive * delta)

    if not k_actives:
        return None, 0.0

    k_active = sum(k_actives) / len(k_actives)

    if session_mode == "heat" and not (_K_ACTIVE_HEAT_MIN <= k_active <= _K_ACTIVE_HEAT_MAX):
        return None, 0.0
    if session_mode == "cool" and not (_K_ACTIVE_COOL_MIN <= k_active <= _K_ACTIVE_COOL_MAX):
        return None, 0.0

    var_res = sum((k_a - k_active) ** 2 for k_a in k_actives)
    var_tot = sum(k_a**2 for k_a in k_actives)
    r2 = max(0.0, 1.0 - var_res / var_tot) if var_tot > 0 else 0.0
    return k_active, r2


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical HA sensor data through the thermal learning OLS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--passive-only",
        action="store_true",
        help="Analyse passive-only windows (HVAC off, fan off, windows closed) from chart_log. "
        "Compares OLS vs endpoint estimator side-by-side, simulates EWMA convergence, and "
        "compares ODE predictions. Requires SSH access (same as --chart-log). "
        "Read-only — does not write to the learning DB.",
    )
    parser.add_argument(
        "--vent-overnight",
        action="store_true",
        help="Analyse overnight ventilated windows (HVAC off, windows open, T_out < T_in throughout) "
        "using the endpoint estimator. Applies the natural regime filter that auto-rejects morning "
        "windows where T_out rises toward T_in. Shows filter hit/miss counts, k_vent_window EWMA "
        "convergence, and ODE prediction comparison. Requires SSH access. Read-only.",
    )
    parser.add_argument(
        "--hvac",
        action="store_true",
        help="Detect and replay HVAC heat/cool cycles from chart_log (Issue #130 Phase C). "
        "Requires SSH access (same as --chart-log). Use with --days and --dry-run/--write.",
    )
    parser.add_argument(
        "--chart-log",
        action="store_true",
        help="Use CA chart_log JSON (via SSH) instead of HA state history. "
        "Recommended: chart_log has real indoor+outdoor temps at ~30min resolution. "
        "Does not require --climate-entity / --outdoor-entity / --window-entities.",
    )
    parser.add_argument(
        "--climate-entity",
        default=None,
        help="Thermostat entity ID (e.g. climate.ecobee_thermostat) — not needed with --chart-log",
    )
    parser.add_argument(
        "--outdoor-entity", default=None, help="Outdoor temperature sensor entity ID — not needed with --chart-log"
    )
    parser.add_argument(
        "--window-entities", default=None, help="Comma-separated binary_sensor entity IDs — not needed with --chart-log"
    )
    parser.add_argument("--days", type=int, default=30, help="Days of history to use (default: 30)")
    parser.add_argument(
        "--min-window-minutes", type=int, default=60, help="Min obs window length in minutes (default: 60)"
    )
    parser.add_argument(
        "--temp-unit",
        choices=["F", "C"],
        default=None,
        help="Force temperature unit (default: auto-detect from HA config)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing to HA")
    parser.add_argument("--write", action="store_true", help="Merge replay obs into HA learning DB via SSH")
    args = parser.parse_args()

    _read_only_mode = args.passive_only or args.vent_overnight
    if not _read_only_mode and not args.dry_run and not args.write:
        parser.error("Specify --dry-run to preview or --write to commit. Start with --dry-run.")

    if (
        not args.chart_log
        and not args.hvac
        and not args.passive_only
        and not args.vent_overnight
        and not (args.climate_entity and args.outdoor_entity and args.window_entities)
    ):
        parser.error(
            "Either --chart-log, --hvac, --passive-only, --vent-overnight, or all of "
            "--climate-entity, --outdoor-entity, --window-entities are required."
        )

    config = load_config()
    ha_url = config.get("HA_URL", "").rstrip("/")
    ha_token = config.get("HA_TOKEN") or config.get("HA_API_TOKEN", "")

    # Auto-detect temperature unit from HA
    temp_unit = args.temp_unit
    if temp_unit is None:
        ha_cfg = fetch_ha_config(ha_url, ha_token)
        temp_unit = ha_cfg.get("unit_system", {}).get("temperature", "°F")
        temp_unit = "C" if "C" in temp_unit else "F"
        print(f"HA temperature unit: {temp_unit}")

    if args.passive_only:
        # --- Passive-only analysis path (read-only, no --dry-run/--write needed) ---
        print(f"\nFetching chart_log from HA via SSH ({args.days} days) for passive-only analysis...")
        chart_entries = fetch_chart_log_ssh(config)
        print(f"  chart_log: {len(chart_entries)} total entries")

        print("\nFetching current learning DB from HA via SSH...")
        try:
            learning_db = fetch_learning_json_ssh(config)
        except Exception as exc:
            print(f"  WARNING: Could not fetch learning DB ({exc}) — k_passive will show as None")
            learning_db = {}

        run_passive_only_analysis(chart_entries, learning_db, days=args.days)
        return

    if args.vent_overnight:
        # --- Overnight ventilated analysis path (read-only, no --dry-run/--write needed) ---
        print(f"\nFetching chart_log from HA via SSH ({args.days} days) for overnight ventilated analysis...")
        chart_entries = fetch_chart_log_ssh(config)
        print(f"  chart_log: {len(chart_entries)} total entries")

        print("\nFetching current learning DB from HA via SSH...")
        try:
            learning_db = fetch_learning_json_ssh(config)
        except Exception as exc:
            print(f"  WARNING: Could not fetch learning DB ({exc}) — k_vent_window will show as None")
            learning_db = {}

        run_vent_overnight_analysis(chart_entries, learning_db, days=args.days)
        return

    if args.hvac:
        # --- HVAC heat/cool cycle replay path (Issue #130 Phase C) ---
        print(f"\nFetching chart_log from HA via SSH ({args.days} days) for HVAC replay...")
        chart_entries = fetch_chart_log_ssh(config)
        total = len(chart_entries)
        print(f"  chart_log: {total} total entries")

        # Apply days filter
        cutoff_dt = datetime.now(UTC) - timedelta(days=args.days)
        filtered = []
        for e in chart_entries:
            ts_str = e.get("ts", "")
            if not ts_str:
                continue
            ts = _parse_ts(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff_dt:
                filtered.append(e)
        print(f"  Entries in last {args.days} days: {len(filtered)}")

        print("\nDetecting HVAC heat/cool cycles...")
        cycles = detect_hvac_cycles(filtered)
        n_heat = sum(1 for c in cycles if c["mode"] == "heat")
        n_cool = sum(1 for c in cycles if c["mode"] == "cool")
        print(f"  Found {len(cycles)} cycles ({n_heat} heat, {n_cool} cool)")

        # Check for bridge proxy: use k_vent_window from existing DB if present.
        # Load regardless of --dry-run/--write — proxy is needed for single-point
        # k_active estimation in both modes (D22).
        k_vent_proxy: float | None = None
        try:
            db_check = fetch_learning_json_ssh(config)
            _cache = db_check.get("thermal_model_cache", {})
            _kv = _cache.get("k_vent_window")
            if isinstance(_kv, float) and _kv < 0:
                k_vent_proxy = _kv
                print(f"  Bridge proxy: k_vent_window={k_vent_proxy:.4f} available for fallback")
        except Exception:
            pass  # proxy unavailable; not fatal

        print("\nRunning HVAC OLS...")
        obs_list = []
        rejected = 0
        reject_reasons: dict[str, int] = {}
        for cycle in cycles:
            obs = run_hvac_replay_ols(cycle, k_vent_window_proxy=k_vent_proxy)
            mode_tag = cycle["mode"]
            ts_short = cycle.get("start_ts", "?")[:16].replace("T", " ")
            n_pre = len(cycle["pre_samples"])
            n_act = len(cycle["active_samples"])
            n_post = len(cycle["post_samples"])
            if obs:
                # Compute thermostat swing from the on/off event entries.
                # on_entry = first active sample (indoor at HVAC-on transition)
                # off_entry = first post sample (indoor at HVAC-off transition)
                _active = cycle["active_samples"]
                _post = cycle["post_samples"]
                if _active and _post:
                    swing_val = compute_swing_from_hvac_events(_active[0], _post[0], mode=mode_tag)
                    if swing_val is not None:
                        obs["swing_f"] = swing_val

                k_p_str = f"{obs['k_passive']:+.4f}" if obs["k_passive"] is not None else "proxy"
                k_a_str = f"{obs['k_active']:+.4f}" if obs["k_active"] is not None else "None"
                r2_p_val = obs.get("r_squared_passive")
                r2_str = f"{r2_p_val:.3f}" if r2_p_val is not None else "n/a"
                swing_str = f"{obs['swing_f']:.2f}°F" if obs.get("swing_f") is not None else "n/a"
                print(
                    f"  [{mode_tag}] {ts_short}  "
                    f"n_pre={n_pre} n_act={n_act} n_post={n_post}  "
                    f"k_passive={k_p_str} k_active={k_a_str} R²={r2_str}  "
                    f"swing={swing_str}  COMMITTED [low]"
                )
                obs_list.append(obs)
            else:
                rejected += 1
                reason = "rejected"
                print(f"  [{mode_tag}] {ts_short}  n_pre={n_pre} n_act={n_act} n_post={n_post}  rejected")
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

        print(f"\nSummary: {len(obs_list)} committed / {rejected} rejected")
        if reject_reasons:
            for reason, count in sorted(reject_reasons.items()):
                print(f"  {reason}: {count}")

        if not obs_list:
            print("\nNo HVAC obs to write — done.")
            return

        if args.dry_run:
            print("\n(dry-run) Use --write to merge into HA learning DB.")
            return

        # Write path
        print("\nFetching current learning DB from HA via SSH...")
        db = fetch_learning_json_ssh(config)
        existing_obs: list[dict] = db.get("thermal_observations", [])
        print(f"  Existing obs: {len(existing_obs)}")

        # Build index of swing_f values from chart_log-computed obs so we can patch DB
        # copies that were stored before swing detection was implemented.
        computed_swing: dict[tuple[str, str], float] = {
            (o["timestamp"], o["hvac_mode"]): o["swing_f"] for o in obs_list if o.get("swing_f") is not None
        }

        existing_replay_keys = {
            (o.get("timestamp"), o.get("hvac_mode")) for o in existing_obs if o.get("source") == "replay"
        }

        # Patch existing obs missing swing_f with values computed from chart_log.
        patched = 0
        for obs in existing_obs:
            key = (obs.get("timestamp", ""), obs.get("hvac_mode", ""))
            if key in computed_swing and obs.get("swing_f") is None:
                obs["swing_f"] = computed_swing[key]
                patched += 1
        if patched:
            print(f"  Patched {patched} existing obs with swing_f from chart_log")

        new_obs = [o for o in obs_list if (o["timestamp"], o["hvac_mode"]) not in existing_replay_keys]
        print(f"  New HVAC replay obs (after dedup): {len(new_obs)}")

        merged = existing_obs + new_obs
        merged.sort(key=lambda o: o.get("timestamp", ""))

        cutoff_str = (datetime.now().date() - timedelta(days=90)).isoformat()
        merged = [o for o in merged if o.get("date", "") >= cutoff_str]
        if len(merged) > _THERMAL_OBS_CAP:
            merged = merged[-_THERMAL_OBS_CAP:]

        print("\nRebuilding EWMA model cache from all obs...")
        new_model = rebuild_model_cache(merged)
        db["thermal_observations"] = merged
        db["thermal_model_cache"] = new_model

        print("\nNew model after rebuild:")
        _hvac_keys = (
            "k_passive",
            "k_active_heat",
            "k_active_cool",
            "k_vent_window",
            "confidence_k_passive",
            "confidence_k_hvac",
        )
        for k in _hvac_keys:
            print(f"  {k}: {new_model.get(k)}")

        print(f"\nWriting {len(merged)} obs to HA learning DB...")
        write_learning_json_ssh(config, db)
        print("Done. Restart Climate Advisor integration in HA to activate the updated model.")
        print("  (HA > Settings > Devices & Services > Climate Advisor > Reload)")
        return

    if args.chart_log:
        # --- Chart-log path (SSH) ---
        print(f"\nFetching chart_log from HA via SSH ({args.days} days)...")
        chart_entries = fetch_chart_log_ssh(config)
        total = len(chart_entries)
        print(f"  chart_log: {total} total entries")

        print(f"\nDetecting ventilated_decay windows (min {args.min_window_minutes} min)...")
        windows = build_windows_from_chart_log(
            chart_entries,
            days=args.days,
            min_window_minutes=args.min_window_minutes,
        )
        print(f"  Found {len(windows)} candidate windows")

    else:
        # --- Entity state history path (REST API) ---
        if not ha_url or not ha_token:
            sys.exit("ERROR: Set HA_URL and HA_TOKEN in tools/.env (not needed with --chart-log)")

        window_entities = [e.strip() for e in args.window_entities.split(",") if e.strip()]
        all_entities = [args.climate_entity, args.outdoor_entity] + window_entities

        print(f"\nFetching {args.days} days of history for {len(all_entities)} entities...")
        history = fetch_entity_history(ha_url, ha_token, all_entities, args.days)

        climate_states = history.get(args.climate_entity, [])
        outdoor_states = history.get(args.outdoor_entity, [])
        if not climate_states:
            sys.exit(f"ERROR: No history for climate entity '{args.climate_entity}'")
        if not outdoor_states:
            sys.exit(f"ERROR: No history for outdoor entity '{args.outdoor_entity}'")

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=args.days)
        timestamps = build_time_grid(start_time, end_time, interval_minutes=5)
        print(f"  Time grid: {len(timestamps)} points × 5 min ({args.days}d)")

        indoor_temps = interpolate_numeric(climate_states, timestamps, attr="current_temperature")
        outdoor_temps = interpolate_numeric(outdoor_states, timestamps)

        if temp_unit == "C":
            indoor_temps = [t * 9 / 5 + 32 if t is not None else None for t in indoor_temps]
            outdoor_temps = [t * 9 / 5 + 32 if t is not None else None for t in outdoor_temps]

        hvac_actions = interpolate_categorical(climate_states, timestamps, attr="hvac_action")

        window_series: list[bool | None] = [None] * len(timestamps)
        for ent in window_entities:
            ent_states = history.get(ent, [])
            if not ent_states:
                print(f"  WARNING: No history for window entity '{ent}' — skipping")
                continue
            vals = interpolate_categorical(ent_states, timestamps)
            for i, v in enumerate(vals):
                if v == "on":
                    window_series[i] = True
                elif v == "off" and window_series[i] is None:
                    window_series[i] = False

        print(f"\nDetecting ventilated_decay windows (min {args.min_window_minutes} min)...")
        windows = find_ventilated_windows(
            timestamps,
            indoor_temps,
            outdoor_temps,
            hvac_actions,
            window_series,
            min_window_minutes=args.min_window_minutes,
        )
        print(f"  Found {len(windows)} candidate windows")

    # Run OLS on each window
    print("\nRunning OLS...")
    obs_list = []
    rejected = 0
    for window in windows:
        obs = run_ols_on_window(window)
        if obs:
            sf_vals = [s["solar_factor"] for s in window]
            sf_range = max(sf_vals) - min(sf_vals) if sf_vals else 0.0
            path = "2p" if obs.get("two_param") else "1p"
            k_sol_str = f" k_solar={obs['k_solar']:+.3f}" if obs.get("k_solar") is not None else ""
            print(
                f"  [{path}] {obs['date']} {obs['timestamp'][11:16]} "
                f"k_env={obs['k_passive']:+.4f}{k_sol_str} "
                f"R²={obs['r_squared_passive']:.3f} n={obs['sample_count_post']} "
                f"sf_range={sf_range:.2f}"
            )
            obs_list.append(obs)
        else:
            rejected += 1

    two_param_count = sum(1 for o in obs_list if o.get("two_param"))
    print(f"\nSummary: {len(obs_list)} committed / {rejected} rejected")
    print(f"  2-param (k_solar learned): {two_param_count}")
    print(f"  1-param (k_env only):      {len(obs_list) - two_param_count}")

    if not obs_list:
        print("\nNo obs to write — done.")
        return

    if args.dry_run:
        print("\n(dry-run) Use --write to merge into HA learning DB.")
        return

    # Write path
    print("\nFetching current learning DB from HA via SSH...")
    db = fetch_learning_json_ssh(config)
    existing_obs: list[dict] = db.get("thermal_observations", [])
    print(f"  Existing obs: {len(existing_obs)}")

    # Deduplicate by source+timestamp (replay obs have source="replay")
    existing_replay_keys = {
        (o.get("timestamp"), o.get("hvac_mode")) for o in existing_obs if o.get("source") == "replay"
    }
    new_obs = [o for o in obs_list if (o["timestamp"], o["hvac_mode"]) not in existing_replay_keys]
    print(f"  New replay obs (after dedup): {len(new_obs)}")

    merged = existing_obs + new_obs
    merged.sort(key=lambda o: o.get("timestamp", ""))

    # 90-day rolling trim
    cutoff = (datetime.now().date() - timedelta(days=90)).isoformat()
    merged = [o for o in merged if o.get("date", "") >= cutoff]

    # Hard cap
    if len(merged) > _THERMAL_OBS_CAP:
        merged = merged[-_THERMAL_OBS_CAP:]

    # Rebuild EWMA model cache from all obs
    print("\nRebuilding EWMA model cache from all obs...")
    new_model = rebuild_model_cache(merged)
    db["thermal_observations"] = merged
    db["thermal_model_cache"] = new_model

    print("\nNew model after rebuild:")
    for k in ("k_passive", "k_vent_window", "k_solar", "confidence_k_passive", "confidence_k_hvac"):
        print(f"  {k}: {new_model.get(k)}")

    print(f"\nWriting {len(merged)} obs to HA learning DB...")
    write_learning_json_ssh(config, db)
    print("Done. Restart Climate Advisor integration in HA to activate the updated model.")
    print("  (HA > Settings > Devices & Services > Climate Advisor > Reload)")


if __name__ == "__main__":
    main()
