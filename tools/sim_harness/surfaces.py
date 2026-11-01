"""surfaces - Promise 8 ``surfaces_agree``: what Climate Advisor SHOWS matches what the thermostat HOLDS.

Issue #1128 I6 (design ``plans/test-automation/03-step3-design.md`` section 8).

Occupant first: the person reads the Status card, the chart band, the briefing and the Next Action
line to learn what the house is being held to.  If the thermostat is armed to 66-72 F while the
chart draws 68-74 F, the person cannot trust anything on the page.  P8 asks, for each surface, "does
the range you quote equal the range the engine is armed with right now?".

DOCTRINE (design section 8): the READERS call the real production functions on the live FakeHass
coordinator session; this module never re-derives a band.

* armed reference ........ ``engine._resolved_comfort_band(classification)`` (what the thermostat gets)
* chart band ............. ``coordinator._build_target_band_for(...)`` + ``_target_band_lower_upper_now()``
* Status API ............. the real ``ClimateAdvisorStatusView.get()`` (``ca_target_heat/cool``)
* briefing ............... the real ``coordinator._build_briefing_text()`` HVAC Mode row
* Next Action ............ the real ``coordinator._compute_next_action()``, boundary-probed
* ODE ceiling guard ...... ``engine._ceiling_threshold(comfort_cool)``
* nat-vent target ........ ``coordinator._nat_vent_target_now()`` (must sit inside the armed band)
* thermostat hold ........ the live thermostat setpoint vs the active-family armed edge
* cards / fans ........... ``_compute_automation_status`` / ``_compute_next_automation_action`` /
  ``_compute_fan_status`` / ``_compute_whf_status`` / ``_compute_hvac_fan_status`` must answer, and the
  fan cards must agree with the physical fan

P8 is TEST-ONLY: it needs a live coordinator and engine, so it is not in the production ``PROMISES``
registry (which is a tuple of pure ``check(PromiseInput, PromiseConfig)`` functions that also run at
home).  Its severity is "hard" with a ratchet: ``tools/simulations/surface_divergences.json`` lists
divergences known on HEAD (tolerated); an unlisted one fails, a listed one that no longer diverges
also fails ("remove the stale entry").  Goldens are LOCKED: a known divergence is recorded in the
ledger, never by editing a scenario.

This module installs the Home Assistant stubs (``promise_probe`` does) - tests/tools only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tools.sim_harness._loop import run_coro
from tools.sim_harness.promise_probe import PromiseProbe

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN_DIR = REPO_ROOT / "tools" / "simulations" / "golden"
DIVERGENCES_PATH = REPO_ROOT / "tools" / "simulations" / "surface_divergences.json"

P8_KEY = "surfaces_agree"
P8_SEVERITY = "hard"  # ratcheted by the ledger above
P8_HOME_LEVEL = None  # never evaluated at home: it needs live coordinator objects

_ROUTINE_EVENT_TYPES = frozenset({"temp_update", "thermostat_state_changed"})
SURFACE_TOL_F = 0.6  # whole-degree display rounding (design section 3.1)
SURFACE_TOL_CELSIUS_F = 1.0  # a whole-degree Celsius label is up to 0.9 F off the internal value
SAMPLE_EVERY = timedelta(minutes=30)
UNIT_SAMPLE_SEEDS = (101, 102)  # the fixed generated sample the unit tier and the ledger use
UNIT_SAMPLE_DAYS = 10
GENERATED_SAMPLE_EVERY = timedelta(minutes=60)  # generated days are long; keep nightly cost bounded
NEXT_ACTION_SWEEP_F = 8.0
NEXT_ACTION_STEP_F = 0.5

SURFACES = (
    "chart_band",
    "status_api",
    "briefing",
    "next_action",
    "ode_ceiling",
    "nat_vent_target",
    "thermostat_hold",
    "fan_cards",
    "cards",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Pure half: values and the comparison (no engine, no Home Assistant)
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Armed:
    """The range the engine arms the thermostat with (internal degrees F)."""

    floor: float
    ceiling: float
    active: str  # "floor" | "ceiling"


@dataclass(frozen=True)
class Shown:
    """What one surface says.  ``kind`` selects the comparison:

    * ``edges``  - ``lower``/``upper`` (either may be None = not quoted) equal the armed edges
    * ``inside`` - ``lower`` is a single value that must sit inside the armed band
    * ``hold``   - a live setpoint (``lower`` = heat-mode, ``upper`` = cool-mode) must not be LOOSER
      than the armed edge; tighter is what pre-cool / pre-heat / TOU banking do on purpose
    """

    surface: str
    lower: float | None = None
    upper: float | None = None
    kind: str = "edges"
    note: str = ""


@dataclass(frozen=True)
class Divergence:
    surface: str
    scenario: str
    ts: str
    step: str
    detail: str
    armed: tuple[float, float]
    shown: tuple[float | None, float | None]
    replay: dict = field(default_factory=dict)
    signature: str = ""  # "<band>/<edge>", see divergence_signature()


def divergence_signature(band: str, detail: str) -> str:
    """Pure: the SHAPE of a divergence - ``<band>/<edge>``.

    ``band`` is "sleep" or "awake" (which comfort window the engine was in).  ``edge`` is which side of
    the range disagrees: "low", "high", "low+high", "reader_failed", or "text" (a card or fan-card
    sentence).  The ledger tolerates a known shape, not merely a known surface: a new daytime Next
    User Action divergence is a different shape from the tolerated sleep-window one and must fail."""
    if "reader failed" in detail:
        edge = "reader_failed"
    else:
        low = "low edge" in detail or "heat held" in detail
        high = "high edge" in detail or "cool held" in detail
        edge = "low+high" if low and high else "low" if low else "high" if high else "text"
    return f"{band}/{edge}"


def check_surfaces(armed: Armed, shown: list[Shown], tol: float = SURFACE_TOL_F) -> list[tuple[Shown, str]]:
    """Pure comparison: ``[(shown, plain-English detail)]`` for every surface that disagrees."""
    out: list[tuple[Shown, str]] = []
    for s in shown:
        if s.kind == "inside":
            v = s.lower
            if v is not None and not (armed.floor - tol <= v <= armed.ceiling + tol):
                out.append(
                    (
                        s,
                        f"{s.surface} aims at {v:.1f} F, outside the {armed.floor:.1f}-{armed.ceiling:.1f} F "
                        f"range the thermostat is held to",
                    )
                )
            continue
        bad: list[str] = []
        if s.kind == "hold":
            if s.lower is not None and s.lower < armed.floor - tol:
                bad.append(f"heat held at {s.lower:.1f} F, below the armed {armed.floor:.1f} F floor")
            if s.upper is not None and s.upper > armed.ceiling + tol:
                bad.append(f"cool held at {s.upper:.1f} F, above the armed {armed.ceiling:.1f} F ceiling")
            if bad:
                out.append((s, f"{s.surface} " + "; ".join(bad)))
            continue
        if s.lower is not None and abs(s.lower - armed.floor) > tol:
            bad.append(f"low edge {s.lower:.1f} F vs armed {armed.floor:.1f} F")
        if s.upper is not None and abs(s.upper - armed.ceiling) > tol:
            bad.append(f"high edge {s.upper:.1f} F vs armed {armed.ceiling:.1f} F")
        if bad:
            out.append((s, f"{s.surface} shows " + "; ".join(bad) + (f" ({s.note})" if s.note else "")))
    return out


def sweep_flip_points(texts: list[tuple[float, str]]) -> list[float]:
    """Pure: indoor values at which a (digit-normalised) message changes, midpoints of the step."""
    flips: list[float] = []
    prev: tuple[float, str] | None = None
    for x, msg in texts:
        norm = re.sub(r"\d+(\.\d+)?", "#", msg)
        if prev is not None and norm != prev[1]:
            flips.append((prev[0] + x) / 2.0)
        prev = (x, norm)
    return flips


# ═══════════════════════════════════════════════════════════════════════════════
# Known-divergence ledger (the ratchet)
# ═══════════════════════════════════════════════════════════════════════════════
def load_divergences(path: Path = DIVERGENCES_PATH) -> list[dict]:
    """``[{surface, scenario | context, issue, signature, evidence}]`` - type-validated, junk rows dropped.

    ``signature`` is the list of divergence shapes (``"<band>/<edge>"``) the row tolerates; a row
    without one is an error, because a surface-only match would hide every NEW shape on that surface."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = data.get("divergences", []) if isinstance(data, dict) else []
    out = []
    for r in rows:
        if not (isinstance(r, dict) and r.get("surface") in SURFACES and (r.get("scenario") or r.get("context"))):
            continue
        sig = r.get("signature")
        if not (isinstance(sig, list) and sig and all(isinstance(x, str) and "/" in x for x in sig)):
            raise ValueError(f"ledger row {r.get('surface')}/{r.get('scenario') or r.get('context')} needs signature")
        out.append(r)
    return out


def ledger_key(row_or_surface: Any, scenario: str | None = None, context: str | None = None) -> tuple[str, str]:
    if isinstance(row_or_surface, dict):
        return (row_or_surface["surface"], row_or_surface.get("scenario") or f"context:{row_or_surface['context']}")
    return (str(row_or_surface), scenario or f"context:{context}")


def judge(
    found: dict[tuple[str, str], list[Divergence]], ledger: list[dict], *, scope: set[str]
) -> tuple[list[tuple[tuple[str, str], list[Divergence]]], list[tuple[str, str]]]:
    """Ratchet verdict.  ``found`` maps (surface, scenario-or-context) to the divergences seen.

    Returns ``(unlisted, stale)``.  A divergence is tolerated only when its (surface, where) is listed
    AND its signature (band and edge) is one the row names; anything else is unlisted and fails.  A
    ledger row (or one of its signatures) inside ``scope`` - the scenarios/contexts this run covered -
    that did not diverge is stale and also fails.  Stale signature entries are reported as
    ``(surface, "where [signature]")``.
    """
    allowed: dict[tuple[str, str], set[str]] = {}
    for r in ledger:
        allowed.setdefault(ledger_key(r), set()).update(r["signature"])
    unlisted: list[tuple[tuple[str, str], list[Divergence]]] = []
    for k, divs in sorted(found.items()):
        bad = [d for d in divs if d.signature not in allowed.get(k, set())]
        if bad:
            unlisted.append((k, bad))
    stale: list[tuple[str, str]] = []
    for k in sorted(allowed):
        if k[1] not in scope:
            continue
        if k not in found:
            stale.append(k)
            continue
        seen = {d.signature for d in found[k]}
        stale.extend((k[0], f"{k[1]} [{sig}]") for sig in sorted(allowed[k] - seen))
    return unlisted, stale


# ═══════════════════════════════════════════════════════════════════════════════
# Live half: readers that call the REAL production surfaces
# ═══════════════════════════════════════════════════════════════════════════════
def _unit(coord: Any) -> str:
    return str(coord.config.get("temp_unit", "fahrenheit"))


def _tol(coord: Any) -> float:
    return SURFACE_TOL_F if _unit(coord) == "fahrenheit" else SURFACE_TOL_CELSIUS_F


def _dt_util() -> Any:
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    return dt_util


_SCHEDULE_KEYS = ("sleep_time", "wake_time", "sleep_heat", "sleep_cool")


def household_schedule_complete(config: dict) -> bool:
    """True when the sleep schedule and sleep temperatures are all configured.

    The config flow makes all four REQUIRED, so a real home always has them.  Several goldens omit
    them (they test other things), and then the engine and the chart fall back to DIFFERENT built-in
    defaults (engine 64/72 F from ``DEFAULT_SLEEP_*``; chart 66/77 F from comfort +/- setback depth)
    - an input no installation can produce.  Sleep-sensitive surfaces are therefore compared only for
    a complete household config; the disagreement is reported in the I6 handoff, not ledgered."""
    return all(config.get(k) not in (None, "") for k in _SCHEDULE_KEYS)


def read_armed(session: Any) -> Armed | None:
    """The engine's own resolved band for the current classification (None before one exists)."""
    c = session.engine._current_classification
    if c is None:
        return None
    band = session.engine._resolved_comfort_band(c)
    return Armed(floor=float(band.floor), ceiling=float(band.ceiling), active=str(band.active))


def read_chart_band(session: Any) -> Shown | None:
    """The chart's band at this instant, from the real builder fed a synthetic hourly forecast.

    The harness has no weather integration, so the hourly timestamp list the chart needs is supplied
    as the builder's own ``hourly_forecast_override`` argument (its documented input); the band maths
    is the unmodified production function.  Skips (None) while the chart is mid-ramp or deliberately
    differs from the armed band: the hourly schedule ramps toward sleep/wake and a held arrival
    restore draws the setback on purpose, so only a steady stretch (the bucket equals both neighbours)
    with no hold is comparable.
    """
    coord = session.coordinator
    engine = session.engine
    if not household_schedule_complete(coord.config):
        return None
    now = _dt_util().now()
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    outdoor = getattr(engine, "_last_outdoor_temp", None)
    out_f = float(outdoor) if isinstance(outdoor, (int, float)) else 60.0
    hourly = [{"datetime": (hour0 + timedelta(hours=h)).isoformat(), "temperature": out_f} for h in range(-24, 49)]
    held = coord._occupancy_restore_held()
    if held:
        return None
    sched = coord._build_target_band_for(now, engine._thermal_model, hourly_forecast_override=hourly)
    previous = getattr(coord, "_target_band_schedule", None)
    coord._target_band_schedule = sched
    try:
        lower, upper = coord._target_band_lower_upper_now()
    finally:
        coord._target_band_schedule = previous
    if lower is None or upper is None:
        return None
    # steady-stretch gate: the bucket in force and both neighbours carry the same band
    entries = sorted(sched, key=lambda e: str(e.get("ts")))
    idx = max((i for i, e in enumerate(entries) if datetime.fromisoformat(str(e["ts"])) <= now), default=None)
    if idx is None or idx == 0 or idx + 1 >= len(entries):
        return None
    trio = [(entries[j].get("lower"), entries[j].get("upper")) for j in (idx - 1, idx, idx + 1)]
    if not all(
        t[0] is not None and t[1] is not None and abs(t[0] - trio[1][0]) < 1e-6 and abs(t[1] - trio[1][1]) < 1e-6
        for t in trio
    ):
        return None
    return Shown("chart_band", float(lower), float(upper), note="steady chart bucket")


def read_status_api(session: Any, armed: Armed) -> Shown | None:
    """The real Status endpoint's ``ca_target_heat`` / ``ca_target_cool``, back to internal F."""
    from custom_components.climate_advisor.api import ClimateAdvisorStatusView  # noqa: PLC0415
    from custom_components.climate_advisor.const import DOMAIN  # noqa: PLC0415
    from custom_components.climate_advisor.temperature import to_fahrenheit  # noqa: PLC0415

    coord = session.coordinator
    hass = MagicMock()
    hass.data = {DOMAIN: {"p8": coord}}
    hass.states = session.fake_hass.states
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {}
    resp = run_coro(ClimateAdvisorStatusView().get(request))
    body = getattr(resp, "json_data", None)
    if not isinstance(body, dict) or "ca_target_heat" not in body:
        raise RuntimeError(f"status endpoint gave no band: {str(body)[:120]}")
    unit = _unit(coord)
    lo, hi = body.get("ca_target_heat"), body.get("ca_target_cool")
    return Shown(
        "status_api",
        None if lo is None else float(to_fahrenheit(float(lo), unit)),
        None if hi is None else float(to_fahrenheit(float(hi), unit)),
    )


_BRIEFING_ROW = re.compile(r"HVAC Mode:\s*(Heat|Cool) at (-?\d+(?:\.\d+)?)")


def read_briefing(session: Any) -> Shown | None:
    """The quoted edge of the real briefing's HVAC Mode row (``coordinator._build_briefing_text``), when it applies.

    The briefing is a morning snapshot built for the awake window, so it is compared only while the
    engine is itself awake (not in the sleep window) and the day has a heat or cool family.
    """
    from custom_components.climate_advisor.automation import _in_sleep_window  # noqa: PLC0415
    from custom_components.climate_advisor.temperature import to_fahrenheit  # noqa: PLC0415

    coord = session.coordinator
    c = coord._current_classification
    cfg = coord.config
    now = _dt_util().now()
    if c is None or c.hvac_mode not in ("heat", "cool") or _in_sleep_window(now, cfg):
        return None
    # The coordinator's own briefing builder (the one the morning send and the drift regeneration use),
    # not a re-assembled generate_briefing() call: the reader must see what the occupant is sent.
    _full, text = coord._build_briefing_text(c)
    m = _BRIEFING_ROW.search(text)
    if not m:
        return None
    value = float(to_fahrenheit(float(m.group(2)), _unit(coord)))
    return Shown("briefing", value if m.group(1) == "Heat" else None, value if m.group(1) == "Cool" else None)


def read_next_action_edges(session: Any, armed: Armed) -> tuple[Shown | None, list[float]]:
    """Boundary-probe the real ``_compute_next_action``: where does its guidance flip with indoor temp?

    It exposes no band, so sweep indoor around each armed edge and find where the (digit-normalised)
    message changes.  Returns the implied edges (None when the message never depends on indoor on that
    side, i.e. the surface makes no claim there) plus the raw flip points.
    """
    coord = session.coordinator
    engine = session.engine
    c = coord._current_classification
    lo_x = armed.floor - NEXT_ACTION_SWEEP_F
    hi_x = armed.ceiling + NEXT_ACTION_SWEEP_F
    n = int(round((hi_x - lo_x) / NEXT_ACTION_STEP_F))
    texts: list[tuple[float, str]] = []
    for i in range(n + 1):
        x = lo_x + i * NEXT_ACTION_STEP_F
        texts.append((x, str(coord._compute_next_action(c, indoor_temp=x, outdoor_temp=None, ae=engine))))
    flips = sweep_flip_points(texts)
    mid = (armed.floor + armed.ceiling) / 2.0
    upper = [f for f in flips if f > mid]
    lower = [f for f in flips if f < mid]

    def nearest(cands: list[float], edge: float) -> float | None:
        return min(cands, key=lambda f: abs(f - edge)) if cands else None

    return (
        Shown("next_action", nearest(lower, armed.floor), nearest(upper, armed.ceiling), note="boundary-probed"),
        flips,
    )


def read_ode_ceiling(session: Any, armed: Armed) -> Shown | None:
    """The ceiling the ODE guard hands off at, when the guard can apply (cooling-family day, occupied, awake)."""
    from custom_components.climate_advisor.automation import awake_comfort_edges  # noqa: PLC0415

    coord = session.coordinator
    engine = session.engine
    if coord._occupancy_mode not in ("home", "guest"):
        return None
    awake = awake_comfort_edges(coord.config, aggressive_savings=bool(coord.config.get("aggressive_savings", False)))
    if abs(armed.ceiling - awake[1]) > 1e-6 or abs(armed.floor - awake[0]) > 1e-6:
        return None  # not in the awake window
    # Issue #1093: the same sealed decision the live guard and the chart forecast make, so a sealed
    # whole-house-fan home's hand-off is probed (an unsealed one has none, by design).
    threshold = engine._ceiling_threshold(
        float(coord.config.get("comfort_cool", 75.0)), house_sealed=engine._whf_house_sealed_with_known_sensors()
    )
    if threshold is None:
        return None  # whole-house-fan mode with a window open / sensor unknown has no ceiling hand-off
    return Shown("ode_ceiling", None, float(threshold))


def read_nat_vent_target(session: Any) -> Shown | None:
    target = session.coordinator._nat_vent_target_now()
    return None if target is None else Shown("nat_vent_target", float(target), None, kind="inside")


def read_thermostat_hold(session: Any, armed: Armed) -> Shown | None:
    """The live thermostat setpoint vs the active-family armed edge, in a quiet, plain-comfort state.

    Skipped (None) when anything legitimately moves the thermostat off the comfort band: automation
    off, an override or grace, a door pause, nat-vent / economizer / fan sessions, an arrival restore
    hold, a TOU window, a recent CA command (the write may still be settling), or a mode that is not
    a single heat / cool family.
    """
    from custom_components.climate_advisor.temperature import read_state_temp_f  # noqa: PLC0415

    coord = session.coordinator
    e = session.engine
    if not household_schedule_complete(coord.config):
        return None
    if (
        not coord.automation_enabled
        or e._manual_override_active
        or e._grace_active
        or e.is_paused_by_door
        or e._natural_vent_active
        or e._economizer_active
        or getattr(e, "_fan_active", False)
        or coord._occupancy_restore_held()
        or getattr(e, "_tou_precondition_until", None) is not None
        or getattr(e, "_tou_av_precondition_target", None) is not None
        or coord._startup_coalesce_active
    ):
        return None
    now = session.now()
    for a in reversed(session.fake_hass.action_log[-3:]):
        ts = a.get("ts")
        if a.get("domain") == "climate" and ts is not None and now - ts < timedelta(minutes=10):
            return None
    st = session.fake_hass.states.get(session.climate_entity)
    if st is None:
        return None
    mode = str(st.state)
    sp = read_state_temp_f(st, "temperature", _unit(coord))
    if sp is None:
        return None
    # Only a setpoint CA itself wrote is "what CA arms": if the live value is not the last thing CA
    # sent, the occupant (or a quirk) moved it, which is override territory, not a surface question.
    sent = [
        a
        for a in session.fake_hass.action_log
        if a.get("domain") == "climate" and a.get("service") == "set_temperature"
    ]
    if not sent:
        return None
    last_t = (sent[-1].get("data") or {}).get("temperature")
    if last_t is None or abs(float(last_t) - float(st.attributes.get("temperature", last_t))) > 0.05:
        return None
    modes = [
        (a.get("data") or {}).get("hvac_mode")
        for a in session.fake_hass.action_log
        if a.get("domain") == "climate" and (a.get("data") or {}).get("hvac_mode")
    ]
    if not modes or modes[-1] != mode:
        return None  # the mode is not what CA last commanded: the occupant changed it
    if getattr(e, "_override_confirm_pending", False):
        return None
    if mode == "heat":
        return Shown("thermostat_hold", sp, None, kind="hold", note="live setpoint in heat mode")
    if mode == "cool":
        return Shown("thermostat_hold", None, sp, kind="hold", note="live setpoint in cool mode")
    return None


def read_cards(session: Any) -> list[str]:
    """Call the status-card functions; return problems (an exception or a non-string answer)."""
    coord = session.coordinator
    problems: list[str] = []
    c = coord._current_classification
    calls = {
        "automation_status": lambda: coord._compute_automation_status(),
        "next_automation_action": lambda: coord._compute_next_automation_action(c),
        "next_action": lambda: coord._compute_next_action(
            c, indoor_temp=coord._get_indoor_temp(), outdoor_temp=None, ae=session.engine
        ),
    }
    for name, fn in calls.items():
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001 - report, never crash the sampler
            problems.append(f"{name} raised {type(exc).__name__}: {exc}")
            continue
        text = out[0] if isinstance(out, tuple) else out
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{name} returned no text")
    return problems


def read_fan_cards(session: Any) -> list[str]:
    """The three fan cards must agree with the physical fan (ground truth wins, design section 8).

    Skips the 30 s settling window after CA's own fan command.  Returns problem sentences.
    """
    coord = session.coordinator
    fan_entity = coord.config.get("fan_entity")
    if not (isinstance(fan_entity, str) and fan_entity):
        return []
    st = session.fake_hass.states.get(fan_entity)
    if st is None or str(st.state).lower() in ("unavailable", "unknown", ""):
        return []
    now = session.now()
    for a in reversed(session.fake_hass.action_log[-4:]):
        ts = a.get("ts")
        if a.get("domain") in ("fan", "switch") and ts is not None and now - ts < timedelta(seconds=60):
            return []
    physically_on = str(st.state).lower() == "on"
    problems: list[str] = []
    try:
        whf = coord._compute_whf_status()
        fan = coord._compute_fan_status()
        coord._compute_hvac_fan_status()
    except Exception as exc:  # noqa: BLE001
        return [f"fan status raised {type(exc).__name__}: {exc}"]
    if whf is not None:
        if physically_on and str(whf).startswith("inactive"):
            problems.append(f"Fan (WHF) card says '{whf}' while the whole-house fan is physically on")
        if not physically_on and str(whf).startswith(("active", "running")):
            problems.append(f"Fan (WHF) card says '{whf}' while the whole-house fan is physically off")
    if physically_on and str(fan).startswith("inactive"):
        problems.append(f"Fan card says '{fan}' while the whole-house fan is physically on")
    return problems


@contextlib.contextmanager
def _coordinator_sees_engine_occupancy(session: Any):
    """Harness-gap shim (reads only, restored after): scenario ``occupancy_*`` events drive the ENGINE
    directly, so the coordinator's own ``_occupancy_mode`` (what the Status API, chart and briefing
    read) stays "home".  In production the coordinator changes mode first and the engine follows
    (``coordinator.py`` ``set_occupancy_mode`` call), so the two always agree.  Aligning them for the
    duration of one sample reproduces that relationship without altering the run."""
    coord, engine = session.coordinator, session.engine
    original = coord._occupancy_mode
    coord._occupancy_mode = engine._occupancy_mode
    try:
        yield
    finally:
        coord._occupancy_mode = original


# ═══════════════════════════════════════════════════════════════════════════════
# The probe
# ═══════════════════════════════════════════════════════════════════════════════
class SurfaceProbe(PromiseProbe):
    """A ``PromiseProbe`` that also samples every surface (so one run judges P1-P7 AND P8).

    Sampling: after each dispatched event and at most every 30 virtual minutes between events.
    A reader that raises is recorded as a divergence ("reader failed"), never swallowed: a surface
    that cannot answer is itself a defect the occupant would see as a blank card.
    """

    HOLD_SETTLE = timedelta(minutes=10)  # a thermostat write may still be settling right after a step

    def __init__(
        self,
        *,
        scenario_name: str = "",
        waivers: dict[str, str] | None = None,
        sample_every: timedelta = SAMPLE_EVERY,
        all_events: bool = True,
    ) -> None:
        super().__init__(scenario_name=scenario_name, waivers=waivers)
        self._sample_every = sample_every
        self._all_events = all_events
        self._hold_first: dict[str, datetime] = {}
        self._session: Any = None
        self._last_sample: datetime | None = None
        self.samples = 0
        self.compared: dict[str, int] = {}
        self.divergences: list[Divergence] = []
        self._seen: set[tuple[str, str, str]] = set()

    def attach(self, session: Any) -> None:
        super().attach(session)
        self._session = session

    def step(self, label: str):  # noqa: ANN201 - mirrors PromiseProbe.step
        new = super().step(label)
        s = self._session
        if s is None or s.coordinator is None:
            return new
        now = s.now()
        is_event = label.startswith("event[") and (
            self._all_events or label.split(":", 1)[-1] not in _ROUTINE_EVENT_TYPES
        )
        if not is_event and self._last_sample is not None and now - self._last_sample < self._sample_every:
            return new
        self._last_sample = now
        with contextlib.suppress(Exception):
            self._sample(label, now)
        return new

    # -- sampling ----------------------------------------------------------------
    def _hold_settle(self, coord: Any) -> timedelta:
        """How long a looser-than-armed hold may persist before it is a divergence: the arrival-home
        restore is DELIBERATELY deferred by the welcome-home debounce (default 60 min), so allow that
        plus five minutes of write settling."""
        from custom_components.climate_advisor.const import (  # noqa: PLC0415
            CONF_WELCOME_HOME_DEBOUNCE,
            DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
        )

        debounce = float(coord.config.get(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS))
        return max(self.HOLD_SETTLE, timedelta(seconds=debounce) + timedelta(minutes=5))

    def _time_band(self, now: datetime) -> str:
        """ "sleep" while the engine is in its sleep window, else "awake" (the signature's time band)."""
        from custom_components.climate_advisor.automation import _in_sleep_window  # noqa: PLC0415

        try:
            return "sleep" if _in_sleep_window(now, self._session.coordinator.config) else "awake"
        except Exception:  # noqa: BLE001 - an unreadable schedule must not hide the divergence
            return "awake"

    def _record(self, surface: str, step: str, now: datetime, detail: str, armed: Armed, shown: Shown) -> None:
        band = self._time_band(now)
        signature = divergence_signature(band, detail)
        key = (surface, signature, detail.split(" (")[0][:60])
        self.compared[surface] = self.compared.get(surface, 0)
        if key in self._seen:
            return
        self._seen.add(key)
        self.divergences.append(
            Divergence(
                surface=surface,
                scenario=self._scenario,
                ts=now.isoformat(),
                step=step,
                detail=detail,
                armed=(armed.floor, armed.ceiling),
                shown=(shown.lower, shown.upper),
                replay={"scenario": self._scenario, "virtual_ts": now.isoformat(), "step": step},
                signature=signature,
            )
        )

    def _sample(self, label: str, now: datetime) -> None:
        s = self._session
        armed = read_armed(s)
        if armed is None:
            return
        self.samples += 1
        with _coordinator_sees_engine_occupancy(s):
            self._sample_surfaces(label, now, armed)

    def _sample_surfaces(self, label: str, now: datetime, armed: Armed) -> None:
        s = self._session
        tol = _tol(s.coordinator)
        shown: list[Shown] = []
        readers: list[tuple[str, Any]] = [
            ("chart_band", lambda: read_chart_band(s)),
            ("status_api", lambda: read_status_api(s, armed)),
            ("briefing", lambda: read_briefing(s)),
            ("ode_ceiling", lambda: read_ode_ceiling(s, armed)),
            ("nat_vent_target", lambda: read_nat_vent_target(s)),
            ("thermostat_hold", lambda: read_thermostat_hold(s, armed)),
        ]
        for name, fn in readers:
            try:
                got = fn()
            except Exception as exc:  # noqa: BLE001 - a reader that cannot answer is a finding
                self._record(
                    name,
                    label,
                    now,
                    f"{name} reader failed: {type(exc).__name__}: {str(exc)[:100]}",
                    armed,
                    Shown(name),
                )
                continue
            if got is not None:
                shown.append(got)
                self.compared[name] = self.compared.get(name, 0) + 1
        try:
            na, _flips = read_next_action_edges(s, armed)
            if na is not None and (na.lower is not None or na.upper is not None):
                shown.append(na)
                self.compared["next_action"] = self.compared.get("next_action", 0) + 1
        except Exception as exc:  # noqa: BLE001
            self._record(
                "next_action",
                label,
                now,
                f"next_action reader failed: {type(exc).__name__}: {str(exc)[:100]}",
                armed,
                Shown("next_action"),
            )
        hold_now: set[str] = set()
        for sh, detail in check_surfaces(armed, shown, tol):
            if sh.surface == "thermostat_hold":
                # settling allowance: a looser-than-armed hold only counts once it has persisted
                first = self._hold_first.setdefault(sh.note, now)
                hold_now.add(sh.note)
                if now - first < self._hold_settle(s.coordinator):
                    continue
            self._record(sh.surface, label, now, detail, armed, sh)
        for gone in set(self._hold_first) - hold_now:
            del self._hold_first[gone]
        for problem in read_cards(s):
            self._record("cards", label, now, problem, armed, Shown("cards"))
        self.compared["cards"] = self.compared.get("cards", 0) + 1
        for problem in read_fan_cards(s):
            self._record("fan_cards", label, now, problem, armed, Shown("fan_cards"))
        self.compared["fan_cards"] = self.compared.get("fan_cards", 0) + 1


# ═══════════════════════════════════════════════════════════════════════════════
# Runners
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class SurfaceRun:
    name: str
    samples: int
    compared: dict[str, int]
    divergences: list[Divergence]
    callback_errors: int = 0
    error: str | None = None


def run_golden_surfaces(path: Path) -> SurfaceRun:
    """One scenario file through the real COORDINATOR session (forced; the surfaces need it)."""
    scenario = json.loads(path.read_text(encoding="utf-8"))
    return run_scenario_surfaces(scenario, path.stem)


def run_scenario_surfaces(scenario: dict, name: str) -> SurfaceRun:
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    sc = dict(scenario)
    sc["use_coordinator"] = True
    sc["skip_startup_coalesce"] = True
    probe = SurfaceProbe(scenario_name=name)
    try:
        result = run_production_scenario(sc, use_coordinator=True, probe=probe)
    except Exception as exc:  # noqa: BLE001 - a scenario the coordinator cannot host is reported, not fatal
        return SurfaceRun(name, probe.samples, probe.compared, probe.divergences, error=f"{type(exc).__name__}: {exc}")
    return SurfaceRun(
        name, probe.samples, probe.compared, probe.divergences, callback_errors=len(result.callback_errors)
    )


def run_generated_surfaces(
    master_seeds: list[int], days: int, profile: str = "coordinator"
) -> tuple[list[SurfaceRun], list[tuple[int, str]]]:
    """Generated days judged by P8.  The generator constructs its probe by name; swap in ours."""
    from unittest.mock import patch  # noqa: PLC0415

    from tools.sim_harness import generator  # noqa: PLC0415

    runs: list[SurfaceRun] = []
    errors: list[tuple[int, str]] = []
    holder: dict[str, SurfaceProbe] = {}

    def factory(**kw: Any) -> SurfaceProbe:
        p = SurfaceProbe(sample_every=GENERATED_SAMPLE_EVERY, all_events=False, **kw)
        holder["p"] = p
        return p

    with patch.object(generator, "PromiseProbe", factory):
        for ms in master_seeds:
            for index in range(days):
                ds = generator.day_seed_of(ms, index)
                holder.pop("p", None)
                try:
                    generator.run_day(ds, profile)
                except Exception as exc:  # noqa: BLE001
                    errors.append((ds, f"{type(exc).__name__}: {exc}"))
                    continue
                p = holder.get("p")
                if p is not None:
                    runs.append(SurfaceRun(f"generated:{ds}", p.samples, p.compared, p.divergences))
    return runs, errors


def collapse(runs: list[SurfaceRun], *, context: str | None = None) -> dict[tuple[str, str], list[Divergence]]:
    """Group divergences by ledger key: (surface, scenario) for goldens, (surface, context:X) for generated."""
    out: dict[tuple[str, str], list[Divergence]] = {}
    for r in runs:
        for d in r.divergences:
            key = ledger_key(d.surface, None if context else r.name, context)
            out.setdefault(key, []).append(d)
    return out


def format_divergence(d: Divergence) -> str:
    return f"  {d.surface} [{d.scenario}] {d.signature} {d.ts} ({d.step}): {d.detail}"


def cli_surfaces(seeds: list[int], days: int, profile: str) -> int:
    """``python tools/simulate.py --surfaces [--generate N --seeds ...]`` - P8 over the goldens + generated days."""
    logging.disable(logging.CRITICAL)
    goldens = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
    ledger = load_divergences()
    runs = [run_golden_surfaces(p) for p in goldens]
    found = collapse(runs)
    unlisted, stale = judge(found, ledger, scope={r.name for r in runs})
    print(f"P8 surfaces_agree: {len(runs)} goldens, {sum(r.samples for r in runs)} samples")
    for r in runs:
        if r.error:
            print(f"  [skipped] {r.name}: {r.error}")
    bad = 0
    if days:
        gruns, gerrors = run_generated_surfaces(seeds, days, profile)
        gfound = collapse(gruns, context="generated")
        # The ledger's generated rows are recorded against the FIXED unit-test sample; on any other
        # seeds a row legitimately may not recur, so staleness is only judged for that sample.
        fixed_sample = sorted(seeds) == sorted(UNIT_SAMPLE_SEEDS) and days == UNIT_SAMPLE_DAYS
        gunlisted, gstale = judge(gfound, ledger, scope={"context:generated"} if fixed_sample else set())
        unlisted += gunlisted
        stale += gstale
        print(f"  generated: {len(gruns)} days, {sum(r.samples for r in gruns)} samples, {len(gerrors)} errors")
    for (surface, where), rows in unlisted:
        bad += 1
        print(f"UNLISTED divergence: {surface} in {where}")
        for d in rows[:3]:
            print(format_divergence(d))
    for surface, where in stale:
        bad += 1
        print(f"STALE ledger entry (no longer diverges): {surface} in {where}")
    print("P8: " + ("FAIL" if bad else "ok") + f" ({len(ledger)} known divergences tolerated)")
    return 1 if bad else 0
