"""Recorded-night replay: the evidence behind the night-strategy gate, run inside the integration (Issue #1230 R6).

Occupant view: the bedroom card only offers "open the windows" or "run the whole-house fan" when Climate Advisor has
checked, on this house's own recorded nights, that the prediction behind it is right. This module is that check. It
used to be a tool (``tools/night_strategy_replay.py``); the nightly job now runs it after the v4 fit so the answer
tracks the house, and the tool imports the same functions (one implementation, no drift).

For every recorded night (local bedtime to wake, an unbroken run of 15-minute readings) it fits v4 on the days BEFORE
that night's ISO week (walk-forward, ``train_days`` back: the live fit rule, ``V4_FIT_TRAIN_DAYS``), predicts the
night from the bedtime reading under the windows / fan / heat / AC that really happened, and compares with the sensor
that was recorded. The mean error per option class (``closed``, ``windows``, ``fan``) is what
``night_strategy.NightValidation`` carries into the gate (>= 3 nights, MAE <= 1.5 F, better than "nothing changes").

Pure and blocking CPU: no Home Assistant objects, no I/O, no clock. The coordinator runs :func:`run_nightly` on an
executor thread under the domain-wide v4 fit lock, right after the nightly fit.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from . import night_strategy as ns
from . import predictor_v4 as p4
from . import v4_shadow
from .const import V4_FIT_TRAIN_DAYS

CA = p4.CA_NODE
STEP_S = p4.V4_STEP_MINUTES * 60
SCHEMA_VERSION = 1
FAN_MIN_STEPS = 4  # >= 1 h of fan makes it a fan night
WINDOWS_MIN_SHARE = 0.6
CLOSED_MAX_SHARE = 0.1
MIN_AC_MIN = 30.0  # minutes of recorded cooling (heating) that make a night an AC (heat) night
NIGHT_REPLAY_NIGHTS = 60  # the integration replays the newest this-many nights (about nine weekly fits)
NIGHT_REPLAY_EXTRA_DAYS = 9  # history kept before the first replayed week so its first fit has its full window
SPAN_DAYS = 30.0  # evidence hours are counted over the same window the live fit trains on
# The hidden-mass node is a slow filter of the primary reading (time constant 1/c_mh, several hours). The live shadow
# advances it every cycle without a break, so a replayed night must start from a settled state too: a 6 h cold start
# (the first version of this replay) left the mass warm after a sunny day and read +1 to +1.8 F warm on autumn nights
# that the running house never had (0.5-1.2 F error once settled). Three days settles any plausible c_mh.
MASS_WARMUP_STEPS = 288


# --------------------------------------------------------------------------------------------- nights
def find_nights(
    samples: Sequence[p4.V4Sample],
    tz: tzinfo,
    bed: tuple[int, int],
    wake: tuple[int, int],
    primary: str = CA,
) -> list[tuple[datetime, datetime, list[p4.V4Sample]]]:
    """(start, end, step samples) per local date with an unbroken run of readings from bedtime to wake time."""
    by_epoch = {int(s.ts.timestamp()): s for s in samples}
    if not samples:
        return []
    first = samples[0].ts.astimezone(tz).date()
    last = samples[-1].ts.astimezone(tz).date()
    nights = []
    d = first
    while d <= last:
        start = datetime(d.year, d.month, d.day, bed[0], bed[1], tzinfo=tz)
        nxt = d + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, wake[0], wake[1], tzinfo=tz)
        n = int((end.timestamp() - start.timestamp()) // STEP_S)
        grid = [start] + p4.grid_steps(start, n)
        rows = [by_epoch.get(int(t.timestamp())) for t in grid]
        if all(r is not None for r in rows) and all(
            r.readings.get(primary) is not None and r.outdoor_f is not None for r in rows if r is not None
        ):
            nights.append((start, end, [r for r in rows if r is not None]))
        d = nxt
    return nights


def classify(rows: Sequence[p4.V4Sample]) -> tuple[str, str, dict[str, float]]:
    """(class, subtype, stats) of a night from its recorded steps (rows[0] is the bedtime state, not a step)."""
    steps = rows[1:]
    n = len(steps)
    fan_steps = sum(1 for r in steps if r.fan_running)
    win_steps = sum(1 for r in steps if r.windows_open)
    heat_min = sum((r.heat_duty or 0.0) for r in steps) * p4.V4_STEP_MINUTES
    cool_min = sum((r.cool_duty or 0.0) for r in steps) * p4.V4_STEP_MINUTES
    stats = {
        "fan_h": fan_steps * 0.25,
        "win_h": win_steps * 0.25,
        "heat_min": round(heat_min),
        "cool_min": round(cool_min),
    }
    if fan_steps >= FAN_MIN_STEPS:
        sub = "fan:long" if fan_steps * 0.25 > 3.0 else "fan:short"
        if cool_min >= MIN_AC_MIN:
            sub = "fan:then_ac"
        return ns.CLS_FAN, sub, stats
    if win_steps >= WINDOWS_MIN_SHARE * n:
        return ns.CLS_WINDOWS, "windows:all_night" if win_steps >= 0.9 * n else "windows:part", stats
    if win_steps <= CLOSED_MAX_SHARE * n:
        sub = "closed:ac" if cool_min >= MIN_AC_MIN else ("closed:heat" if heat_min >= MIN_AC_MIN else "closed:idle")
        return ns.CLS_CLOSED, sub, stats
    return "mixed", "mixed", stats


def mass_at(
    model: p4.V4Model, samples: Sequence[p4.V4Sample], idx_by_epoch: Mapping[int, int], start: datetime
) -> float | None:
    i = idx_by_epoch.get(int(start.timestamp()))
    if i is None:
        return None
    mass: float | None = None
    for s in samples[max(0, i - MASS_WARMUP_STEPS) : i + 1]:
        mass = p4.step_mass(model, mass_f=mass, readings=s.readings)
    return mass


# --------------------------------------------------------------------------------------------- one night
def replay_night(
    model: p4.V4Model,
    tz: tzinfo,
    start: datetime,
    end: datetime,
    rows: Sequence[p4.V4Sample],
    mass: float | None,
) -> dict[str, object]:
    """Variant A (the gate): recorded duty replayed through the v4 physics; variant C: the planner's own controller
    (the predictor's R1b thermostat emulator + the HVAC pause) from the recorded setpoints, when the night has them."""
    prim = model.primary
    steps = rows[1:]
    h0 = float(rows[0].readings[prim])
    house = [model.house[k] for k in p4.HOUSE_KEYS]
    out = [float(r.outdoor_f) for r in steps]
    win = [1.0 if r.windows_open else 0.0 for r in steps]
    fan = [1.0 if r.fan_running else 0.0 for r in steps]
    hrs = [p4._local_hour(r.ts, tz) for r in steps]
    uh = [
        float(r.heat_duty if r.heat_duty is not None else (1.0 if r.hvac_action == "heating" else 0.0)) for r in steps
    ]
    uc = [
        float(r.cool_duty if r.cool_duty is not None else (1.0 if r.hvac_action == "cooling" else 0.0)) for r in steps
    ]
    roll = p4.rollout(
        house,
        [],
        h0=h0,
        m0=mass if mass is not None else h0,
        b0=[],
        out=out,
        win=win,
        fan=fan,
        hours=hrs,
        heat_u=uh,
        cool_u=uc,
    )
    actual = [float(r.readings[prim]) for r in steps]
    err = [p - a for p, a in zip(roll.primary, actual, strict=True)]
    flat = [h0 - a for a in actual]
    res: dict[str, object] = {
        "mae_f": statistics.fmean(abs(e) for e in err),
        "bias_f": statistics.fmean(err),
        "flat_mae_f": statistics.fmean(abs(e) for e in flat),
        "end_err_f": err[-1],
        "max_abs_f": max(abs(e) for e in err),
        "actual_drop_f": actual[-1] - h0,
        "pred_drop_f": roll.primary[-1] - h0,
    }
    sps = [(r.heat_sp_f, r.cool_sp_f) for r in steps]
    if any(a is not None or b is not None for a, b in sps):
        inp = ns.NightInputs(
            model=model,
            start=start,
            end=end,
            tz=tz,
            fan_mode="whole_house_fan",
            outdoor=[(r.ts, float(r.outdoor_f)) for r in steps],
            readings={prim: h0},
            floor_f=66.0,
            ceiling_f=72.0,
            mass_f=mass,
            offset_loop=False,
        )
        sim = ns.replay_recorded(inp, list(zip(win, fan, strict=True)), sps)
        if sim is not None:
            cerr = [p - a for p, a in zip(sim.hall, actual, strict=True)]
            res["closed_loop_mae_f"] = statistics.fmean(abs(e) for e in cerr)
            res["closed_loop_bias_f"] = statistics.fmean(cerr)
            res["closed_loop_end_err_f"] = cerr[-1]
    return res


# --------------------------------------------------------------------------------------------- aggregation
def _agg(vals: Sequence[Mapping[str, object]], key: str = "mae_f") -> dict[str, object] | None:
    xs = [v for v in vals if key in v]
    if not xs:
        return None
    return {
        "n_nights": len(xs),
        "mae_f": round(statistics.fmean(float(v[key]) for v in xs), 3),  # type: ignore[arg-type]
        "flat_mae_f": round(statistics.fmean(float(v["flat_mae_f"]) for v in xs), 3)  # type: ignore[arg-type]
        if all("flat_mae_f" in v for v in xs)
        else None,
        "bias_f": round(
            statistics.fmean(float(v["bias_f" if key == "mae_f" else "closed_loop_bias_f"]) for v in xs),  # type: ignore[arg-type]
            3,
        ),
        "max_abs_f": round(max(float(v.get("max_abs_f", 0.0)) for v in xs), 2),  # type: ignore[arg-type]
    }


def run(
    samples: Sequence[p4.V4Sample],
    tz: tzinfo,
    *,
    primary: str = CA,
    sensors: Sequence[str] | None = None,
    bed: tuple[int, int] = (20, 30),
    wake: tuple[int, int] = (6, 30),
    train_days: int = 0,
    since: date | None = None,
    until: date | None = None,
    max_nights: int | None = None,
    log: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    """Walk forward by ISO week (fit before each Monday, replay that week's nights). Returns the report dict."""
    emit = log or (lambda _msg: None)
    nights = find_nights(samples, tz, bed, wake, primary)
    if since:
        nights = [n for n in nights if n[0].astimezone(tz).date() >= since]
    if until:
        nights = [n for n in nights if n[0].astimezone(tz).date() <= until]
    n_found = len(nights)
    if max_nights:
        nights = nights[-max_nights:]
    idx = {int(s.ts.timestamp()): i for i, s in enumerate(samples)}
    weeks: dict[date, list] = defaultdict(list)
    for n in nights:
        d = n[0].astimezone(tz).date()
        weeks[d - timedelta(days=d.weekday())].append(n)
    records: list[dict[str, object]] = []
    excluded: dict[str, int] = defaultdict(int)
    t0 = time.time()
    last_model: p4.V4Model | None = None
    for monday in sorted(weeks):
        wk_start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
        train = [
            s for s in samples if s.ts < wk_start and (not train_days or s.ts >= wk_start - timedelta(days=train_days))
        ]
        model = p4.fit(train, primary=primary, sensors=list(sensors or [primary]), tz=tz, now=wk_start)
        last_model = model
        frozen = ns._frozen_set(model)
        emit(f"  week {monday}: {len(weeks[monday])} nights, model {model.confidence} (frozen {sorted(frozen) or '-'})")
        for start, end, rows in weeks[monday]:
            cls, sub, stats = classify(rows)
            rec: dict[str, object] = {"night": start.date().isoformat(), "class": cls, "subtype": sub, **stats}
            if model.confidence == "none":
                excluded["model_none"] += 1
                continue
            if (cls == ns.CLS_FAN and "h/a_f" in frozen) or (cls in (ns.CLS_WINDOWS, ns.CLS_FAN) and "h/a_w" in frozen):
                excluded[f"{cls}_rate_frozen"] += 1
                rec["excluded"] = "rate frozen in the fit"
                records.append(rec)
                continue
            rec.update(replay_night(model, tz, start, end, rows, mass_at(model, samples, idx, start)))
            rec["model_confidence"] = model.confidence
            rec["a_f"], rec["a_w"], rec["a_o"] = (round(model.house[k], 3) for k in ("a_f", "a_w", "a_o"))
            records.append(rec)
    scored = [r for r in records if "mae_f" in r]
    classes: dict[str, object] = {}
    for cls in (ns.CLS_CLOSED, ns.CLS_WINDOWS, ns.CLS_FAN, ns.CLS_BLOWER):
        a = _agg([r for r in scored if r["class"] == cls])
        if a:
            classes[cls] = {k: a[k] for k in ("n_nights", "mae_f", "flat_mae_f", "bias_f")}
    subtypes: dict[str, object] = {}
    for sub in sorted({str(r["subtype"]) for r in scored}):
        a = _agg([r for r in scored if r["subtype"] == sub])
        if a:
            subtypes[sub] = a
    closed_loop: dict[str, object] = {}
    for cls in (ns.CLS_CLOSED, ns.CLS_WINDOWS, ns.CLS_FAN):
        a = _agg([r for r in scored if r["class"] == cls], "closed_loop_mae_f")
        if a:
            closed_loop[cls] = a
    ok_scored = [r for r in scored if r.get("model_confidence") == "ok"]
    classes_ok: dict[str, object] = {}
    for cls in (ns.CLS_CLOSED, ns.CLS_WINDOWS, ns.CLS_FAN):
        a = _agg([r for r in ok_scored if r["class"] == cls])
        if a:
            classes_ok[cls] = {k: a[k] for k in ("n_nights", "mae_f", "flat_mae_f", "bias_f")}
    emit(f"replayed {len(scored)} of {n_found} nights in {time.time() - t0:.0f} s")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "params": {
            "bed": f"{bed[0]:02d}:{bed[1]:02d}",
            "wake": f"{wake[0]:02d}:{wake[1]:02d}",
            "train_days": train_days,
            "primary": primary,
        },
        "n_nights_found": n_found,
        "n_nights_replayed": len(scored),
        "n_ok_confidence_fits": len(ok_scored),
        "last_model_confidence": last_model.confidence if last_model else None,
        "classes": classes,
        "classes_ok_fits_only": classes_ok,
        "subtypes": subtypes,
        "closed_loop_variant": closed_loop,
        "excluded": dict(excluded),
        "nights": records,
    }


def is_provisional(report: Mapping[str, Any]) -> bool:
    """A replay is FINAL (not provisional) only when it is the live rule end to end AND rests on trusted fits.

    * the fits were bounded to ``V4_FIT_TRAIN_DAYS`` (the live fit rule), not the all-history fit R1b showed is
      contaminated by months-old rows;
    * and EVERY option class that was replayed has at least ``VALIDATION_HIGH_NIGHTS`` nights predicted with a fit of
      confidence ``ok`` (the only fits the planner itself acts on). A class whose recorded nights mostly came from
      thin-history fits is evidence about a different, weaker model.

    Anything else caps every grade at ``medium`` (``night_strategy.validation_status``)."""
    params = report.get("params") or {}
    if int(params.get("train_days") or 0) != int(V4_FIT_TRAIN_DAYS):
        return True
    classes = report.get("classes") or {}
    if not classes:
        return True
    ok = report.get("classes_ok_fits_only") or {}
    return any(int((ok.get(cls) or {}).get("n_nights") or 0) < ns.VALIDATION_HIGH_NIGHTS for cls in classes)


def _carry_class(new: ns.ClassValidation | None, old: ns.ClassValidation | None) -> bool:
    """True when the previous record's class stands over this replay's (Issue #1525, owner rule "fade only on new
    evidence"): the replay only covers the newest ``NIGHT_REPLAY_NIGHTS`` nights, so a class whose nights left that
    span would otherwise drop to "no recorded nights" with time alone. The new replay's class replaces the old one
    once it has as many nights as the old one (up to ``VALIDATION_HIGH_NIGHTS``), or when its own
    ``VALIDATION_MIN_NIGHTS``+ nights grade it low (contradicting evidence speaks at once)."""
    if old is None or old.n_nights < ns.VALIDATION_MIN_NIGHTS:
        return False
    if new is None:
        return True
    if new.n_nights >= min(old.n_nights, ns.VALIDATION_HIGH_NIGHTS):
        return False
    if new.n_nights >= ns.VALIDATION_MIN_NIGHTS:
        probe = ns.NightValidation(classes={"x": new})
        if ns.validation_status(probe, "x")[0] == "low":
            return False
    return True


def _carry_evidence(new: ns.Evidence | None, old: ns.Evidence | None) -> ns.Evidence | None:
    """Evidence hours that never fall with time alone (Issue #1525): each count is the larger of this window's and
    the previous record's. The window (``SPAN_DAYS``) only loses hours as rows age out of it, never because a reading
    contradicted anything; a contradicted rate is graded by the replay's error, not by these counts."""
    if old is None:
        return new
    if new is None:
        return old
    sensors = dict(old.sensor_hours)
    for k, v in new.sensor_hours.items():
        sensors[k] = max(v, sensors.get(k, 0.0))
    return ns.Evidence(
        max(new.window_open_hours, old.window_open_hours),
        max(new.fan_hours, old.fan_hours),
        max(new.heat_hours, old.heat_hours),
        max(new.cool_hours, old.cool_hours),
        sensors,
    )


def validation_record(
    report: Mapping[str, Any],
    evidence: ns.Evidence | None,
    *,
    model_fitted_at: str | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The compact record stored next to the v4 model: ``NightValidation.to_dict()`` plus the evidence hours, the run
    parameters and the counts (no per-night rows: the file is read every start).

    ``previous`` (the stored record, Issue #1525): a class with no newer evidence keeps the previous record's result
    (``carried_classes``), and the evidence hours never fall below the previous record's."""
    val = ns.NightValidation.from_dict(
        {"schema_version": ns.NIGHT_STRATEGY_SCHEMA_VERSION, "classes": report.get("classes") or {}}
    )
    classes = dict(val.classes) if val else {}
    ok_report = report.get("classes_ok_fits_only") or {}
    # Issue #1525 verification: "final" is judged per class (its own nights on ok fits), so a class carried from an
    # older record carries its own count, and one provisional class cannot hold another for a season.
    ok_nights = {c: int((ok_report.get(c) or {}).get("n_nights") or 0) for c in classes}
    prev_val = ns.NightValidation.from_dict(previous) if isinstance(previous, Mapping) else None
    prev_ok = previous.get("ok_nights") if isinstance(previous, Mapping) else None
    carried: list[str] = []
    if prev_val is not None:
        for cls, old in prev_val.classes.items():
            if _carry_class(classes.get(cls), old):
                classes[cls] = old
                carried.append(cls)
                if isinstance(prev_ok, Mapping) and isinstance(prev_ok.get(cls), int):
                    ok_nights[cls] = int(prev_ok[cls])
                else:  # a record from before per-class counts: final only if that whole record was final
                    ok_nights[cls] = 0 if prev_val.provisional else ns.VALIDATION_HIGH_NIGHTS
    params = report.get("params") or {}
    provisional = (
        int(params.get("train_days") or 0) != int(V4_FIT_TRAIN_DAYS)
        or not classes
        or any(ok_nights.get(cls, 0) < ns.VALIDATION_HIGH_NIGHTS for cls in classes)
    )
    out = ns.NightValidation(
        classes=classes,
        provisional=provisional,
        generated=str(report.get("generated") or ""),
    ).to_dict()
    out["ok_nights"] = ok_nights
    ev = _carry_evidence(evidence, evidence_from_record(previous)) if previous is not None else evidence
    out["evidence"] = None if ev is None else _evidence_dict(ev)
    out["carried_classes"] = sorted(carried)
    out["model_fitted_at"] = model_fitted_at
    for key in ("params", "n_nights_found", "n_nights_replayed", "n_ok_confidence_fits", "excluded"):
        out[key] = report.get(key)
    return out


def _evidence_dict(ev: ns.Evidence) -> dict[str, Any]:
    return {
        "window_open_hours": ev.window_open_hours,
        "fan_hours": ev.fan_hours,
        "heat_hours": ev.heat_hours,
        "cool_hours": ev.cool_hours,
        "sensor_hours": dict(ev.sensor_hours),
    }


def evidence_from_record(rec: Any) -> ns.Evidence | None:
    """The ``Evidence`` stored in a validation record, or None when absent or malformed."""
    raw = rec.get("evidence") if isinstance(rec, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        return ns.Evidence(
            float(raw.get("window_open_hours", 0.0)),
            float(raw.get("fan_hours", 0.0)),
            float(raw.get("heat_hours", 0.0)),
            float(raw.get("cool_hours", 0.0)),
            {str(k): float(v) for k, v in (raw.get("sensor_hours") or {}).items()},
        )
    except TypeError, ValueError:
        return None


# --------------------------------------------------------------------------------------------- the nightly job
def run_nightly(
    *,
    chart_entries: Sequence[Mapping[str, Any]],
    snapshots_fn: Callable[[datetime, datetime], list[dict]] | None,
    tz: tzinfo,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    bed: tuple[int, int] = (22, 30),
    wake: tuple[int, int] = (6, 30),
    train_days: int = V4_FIT_TRAIN_DAYS,
    nights: int = NIGHT_REPLAY_NIGHTS,
    model_fitted_at: str | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The nightly validation: samples exactly as the live fit builds them, the newest ``nights`` replayed
    walk-forward under the live 30-day rule, the evidence hours of the live fit's own window. ``previous`` (the stored
    record) carries what the newer nights have not displaced (Issue #1525, :func:`validation_record`). Blocking CPU:
    run it on an executor thread, after the v4 fit, under the same domain lock. Raises on failure (the caller keeps
    the previous record)."""
    p = v4_shadow._predictor()
    horizon_days = float(train_days + nights * 1.1 + NIGHT_REPLAY_EXTRA_DAYS)
    entries = v4_shadow.rows_in_window(chart_entries, now, horizon_days)
    snapshots: list[dict] = []
    if snapshots_fn is not None:
        try:
            snapshots = snapshots_fn(now - timedelta(days=horizon_days + 1), now + timedelta(hours=1))
        except Exception:  # noqa: BLE001 - a bad forecast archive degrades to chart-log-only samples
            snapshots = []
    samples, prim, ordered = v4_shadow.build_samples(
        p,
        entries,
        snapshots=snapshots,
        tz=tz,
        primary=primary,
        sensors=sensors,
        aliases=tuple(primary_aliases),
        in_sleep=in_sleep,
    )
    report = run(
        samples,
        tz,
        primary=prim,
        sensors=ordered,
        bed=bed,
        wake=wake,
        train_days=train_days,
        max_nights=nights,
    )
    recent = [s for s in samples if s.ts >= now - timedelta(days=SPAN_DAYS)]
    evidence = ns.evidence_from_samples(recent)
    return validation_record(report, evidence, model_fitted_at=model_fitted_at, previous=previous)
