#!/usr/bin/env python3
"""Replay recorded nights against the v4 model: what "validated" means for the night-strategy gate (Issue #1230 R4).

WHY: the night-strategy engine (``night_strategy.py``) offers an option (windows, whole-house fan, blower) only when
Climate Advisor has evidence for the rates behind it. This tool is that evidence: for every recorded night it fits v4
on data BEFORE the night, predicts the night under the windows / fan / HVAC that actually happened, and compares
the prediction with the sensor that was recorded. The mean error per option class is what
``night_strategy.NightValidation`` carries into the gate (>= 3 nights, MAE <= 1.5 F, better than "nothing changes").

HOW (walk-forward, no future leakage)
  * Nights: local ``--bed`` (default 20:30) to ``--wake`` (default 06:30), only when every 15-minute step has a
    reading and an outdoor temperature (a gap in the log drops the night).
  * Model: fitted once per ISO week on chart_log rows strictly BEFORE that Monday 00:00 (``--train-days`` back,
    0 = all), like ``tools/predict_backtest.py``. Weeks whose model has confidence "none" are skipped.
  * Variant A (the gate): physics with RECORDED heat/cool duty replayed (``predictor_v4.rollout``, ``thermostat=None``),
    recorded outdoor, recorded window and fan flags. It isolates what the gate is about: the window, fan and
    envelope rates. Error = predicted - recorded hallway (``_ca``) at every 15-minute step.
  * Variant C (informational): the same night through ``night_strategy.replay_recorded`` - the controller the planner
    uses (thermostat emulation from the recorded setpoints, HVAC paused while windows / fan are open). Only nights
    with a recorded setpoint (chart_log ``setpoint`` is null before 2026-09-07).
  * A night is excluded from its class when the fit it uses had the class's rate frozen (``a_f`` for fan nights,
    ``a_w`` for window nights): that is "no evidence", not "bad evidence". Counted under ``excluded``.
  * Option class of a night: ``fan`` (fan ran >= 1 h), else ``windows`` (windows open >= 60 % of the night), else
    ``closed`` (open <= 10 %), else ``mixed`` (reported, never gated). Sub-types: fan short/long/then-AC, windows
    all-night/part, closed idle/heat/AC.

LIMITS (printed in the report): the chart_log has no bedroom history before 2026-11-06, so only the hallway is
validated; the bedroom node needs its own hours of readings (gate: 72 h). Forecast error is excluded on purpose
(recorded outdoor). A replay is PROVISIONAL (grades capped at medium) unless its fits followed the live 30-day rule
(``--train-days 30``, the default) AND every replayed class has at least 10 nights predicted with an "ok" fit
(``night_replay.is_provisional``).

The replay itself is ``custom_components/climate_advisor/night_replay.py``: the integration runs the same code every
night after the v4 fit and stores the result next to the model, so this tool reproduces what the gate reads.

Usage:
    python tools/night_strategy_replay.py --chart-log chart.json --json out.json
    python tools/night_strategy_replay.py --chart-log chart.json --forecast-archive fa.json --tonight \
        --validation out.json --bedroom-sensor sensor.primary_bedroom_temperature_temperature
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from custom_components.climate_advisor import night_replay as nr  # noqa: E402
from custom_components.climate_advisor import night_strategy as ns  # noqa: E402
from custom_components.climate_advisor import predictor_v4 as p4  # noqa: E402

CA = "_ca"
STEP_S = nr.STEP_S


# The replay itself lives in the integration (``night_replay``): the nightly job runs the SAME functions, so this tool
# and the stored validation record cannot drift apart.
find_nights = nr.find_nights
classify = nr.classify
replay_night = nr.replay_night
_mass_at = nr.mass_at


def run(
    samples: list[p4.V4Sample],
    tz: tzinfo,
    *,
    bed: tuple[int, int] = (20, 30),
    wake: tuple[int, int] = (6, 30),
    train_days: int = 0,
    since: date | None = None,
    until: date | None = None,
    log=print,
) -> dict[str, object]:
    rep = nr.run(samples, tz, primary=CA, bed=bed, wake=wake, train_days=train_days, since=since, until=until, log=log)
    rep["provisional"] = nr.is_provisional(rep)
    rep["provisional_reason"] = (
        None
        if not rep["provisional"]
        else "fits were not the live 30-day rule, or a replayed class has fewer than "
        f"{ns.VALIDATION_HIGH_NIGHTS} nights predicted with an 'ok' fit"
    )
    rep["limits"] = [
        "hallway only: the chart_log has no bedroom sensor before 2026-11-06",
        "recorded outdoor temperature (forecast error excluded on purpose)",
    ]
    return rep


def print_report(rep: dict[str, object], log=print) -> None:
    log("")
    log("Option class        nights   MAE F  'no change' F   bias F   gate")
    classes = rep["classes"]
    assert isinstance(classes, dict)
    val = ns.NightValidation.from_dict({"schema_version": 1, "classes": classes, "provisional": True})
    for cls, v in classes.items():
        grade, why = ns.validation_status(val, cls)
        flat = v["flat_mae_f"] if v["flat_mae_f"] is not None else float("nan")
        log(f"{cls:<18} {v['n_nights']:>6} {v['mae_f']:>7.2f} {flat:>11.2f} {v['bias_f']:>9.2f}   {grade} {why}")
    log("")
    log("Sub-type                nights   MAE F  'no change' F   bias F  worst-night F")
    subs = rep["subtypes"]
    assert isinstance(subs, dict)
    for sub, v in subs.items():
        flat = v["flat_mae_f"] if v["flat_mae_f"] is not None else float("nan")
        log(
            f"{sub:<22} {v['n_nights']:>6} {v['mae_f']:>7.2f} {flat:>11.2f} {v['bias_f']:>9.2f} {v['max_abs_f']:>10.1f}"
        )
    cl = rep["closed_loop_variant"]
    assert isinstance(cl, dict)
    if cl:
        log("")
        log("Closed-loop variant (planner controller from recorded setpoints; informational)")
        for cls, v in cl.items():
            log(f"  {cls:<10} nights {v['n_nights']:>3}  MAE {v['mae_f']:.2f} F  bias {v['bias_f']:+.2f} F")
    ok = rep.get("classes_ok_fits_only") or {}
    if ok:
        log("\nOnly nights whose fit was confidence 'ok' (the fits the planner itself would trust):")
        for cls, v in ok.items():
            log(f"  {cls:<10} nights {v['n_nights']:>3}  MAE {v['mae_f']:.2f} F  'no change' {v['flat_mae_f']:.2f} F")
    log(f"\nprovisional: {rep.get('provisional')} ({rep.get('provisional_reason') or 'final'})")
    log(f"excluded: {rep['excluded']}")
    for lim in rep["limits"]:  # type: ignore[attr-defined]
        log(f"LIMIT: {lim}")


# --------------------------------------------------------------------------------------------- tonight
def tonight(
    samples: list[p4.V4Sample],
    forecast_snapshot: dict,
    tz: tzinfo,
    *,
    validation: ns.NightValidation | None,
    bedroom: str | None,
    fan_mode: str,
    floor_f: float,
    ceiling_f: float,
    bed: tuple[int, int],
    wake: tuple[int, int],
    train_days: int,
    hold_f: float,
) -> tuple[ns.NightStrategy, p4.V4Model]:
    """Plan tonight from the latest data: roll the model from the last reading to bedtime under the daytime plan
    (hold the awake band, windows closed), then run the strategy."""
    now = samples[-1].ts
    sensors = [CA] + ([bedroom] if bedroom and any(bedroom in s.readings for s in samples) else [])
    train = [s for s in samples if not train_days or s.ts >= now - timedelta(days=train_days)]
    model = p4.fit(train, primary=CA, sensors=sensors, tz=tz, now=now)
    h0 = datetime.fromisoformat(forecast_snapshot["h0"]).astimezone(tz)
    fc = [(h0 + timedelta(hours=i), float(v)) for i, v in enumerate(forecast_snapshot["t"])]
    start = now.replace(hour=bed[0], minute=bed[1], second=0, microsecond=0)
    if start <= now:
        start = now
    end = (start + timedelta(days=1)).replace(hour=wake[0], minute=wake[1])
    if end - start > timedelta(hours=16):
        end = start.replace(hour=wake[0], minute=wake[1]) + (
            timedelta(days=1) if start.hour >= wake[0] else timedelta()
        )
    pts = [(t, v) for t, v in fc if now - timedelta(hours=1) <= t <= end + timedelta(hours=2)]
    corrected = p4._corrected_outdoor(
        model.forecast,
        raw=[v for _, v in pts],
        hours=[p4._local_hour(t, tz) for t, _ in pts],
        lead_h=[(t - now).total_seconds() / 3600 for t, _ in pts],
        now_hour=p4._local_hour(now, tz),
        outdoor_now=samples[-1].outdoor_f,
        forecast_now=samples[-1].forecast_outdoor_f,
    )
    out = [(t, o) for (t, _), o in zip(pts, corrected, strict=True)]
    mass: float | None = None
    for s in samples[-32:]:
        mass = p4.step_mass(model, mass_f=mass, readings=s.readings)
    n_to_bed = int((start - now).total_seconds() // STEP_S)
    readings = dict(samples[-1].readings)
    if n_to_bed > 0:
        steps = p4.grid_steps(now, n_to_bed)
        raw = [(t.timestamp(), v) for t, v in fc]
        inputs = [
            p4.V4Input(ts=t, outdoor_f=ns._interp(raw, t.timestamp()), heat_sp_f=hold_f, cool_sp_f=None) for t in steps
        ]
        pred = p4.predict(
            model,
            now=now,
            readings=samples[-1].readings,
            mass_f=mass,
            inputs=inputs,
            tz=tz,
            outdoor_now_f=samples[-1].outdoor_f,
            forecast_now_f=samples[-1].forecast_outdoor_f,
        )
        if pred:
            readings = {k: v[-1][1] for k, v in pred.items()}
    inp = ns.NightInputs(
        model=model,
        start=start,
        end=end,
        tz=tz,
        fan_mode=fan_mode,
        outdoor=out,
        readings=readings,
        floor_f=floor_f,
        ceiling_f=ceiling_f,
        target_sensor=bedroom,
        mass_f=mass,
        evidence=ns.evidence_from_samples(samples),
        validation=validation,
    )
    return ns.plan_night(inp), model


def print_strategy(st: ns.NightStrategy, log=print) -> None:
    log(
        f"Night {st.start:%a %I:%M %p} -> {st.end:%a %I:%M %p}, sleep band {st.floor_f:.0f}-{st.ceiling_f:.0f} F, "
        f"target {st.target_sensor}, fan mode {st.fan_mode}, show_numbers={st.show_numbers}"
    )
    log(f"RECOMMENDED: {ns.headline(st)}")
    for o in st.options:
        mark = "*" if o.key == st.recommended else " "
        log(
            f" {mark} {o.label:<72} bed {o.bed_min_f:5.1f}-{o.bed_max_f:5.1f}  heat {o.heat_min:3d}  "
            f"AC {o.cool_min:3d}  "
            f"outside {o.discomfort_fh:4.2f}  score {o.score:4.2f}  {o.grade}  actions {o.n_actions} {o.note}"
        )
    for t in ns.tradeoffs(st):
        log(f"   trade-off: {t['text']}")
    for h in st.hidden:
        log(f" HIDDEN (debug only) {h['key']}: {'; '.join(h['reasons'])}")


def _load_samples(path: Path, tz: tzinfo, archive: Path | None) -> list[p4.V4Sample]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries = entries["entries"] if isinstance(entries, dict) else entries
    snaps = None
    if archive and archive.exists():
        raw = json.loads(archive.read_text(encoding="utf-8"))
        snaps = [
            {
                "issued": s["i"],
                "source": s.get("s"),
                "hours": [
                    [(datetime.fromisoformat(s["h0"]) + timedelta(hours=k)).isoformat(), v]
                    for k, v in enumerate(s["t"])
                ],
            }
            for s in raw.get("snapshots", [])
        ]
    return p4.samples_from_chart_log(entries, tz=tz, archive_snapshots=snaps)


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chart-log", type=Path, required=True)
    ap.add_argument("--forecast-archive", type=Path, help="forecast archive file (needed for --tonight)")
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--bed", default="20:30")
    ap.add_argument("--wake", default="06:30")
    ap.add_argument(
        "--train-days", type=int, default=30, help="training look-back per week (the live rule: 30; 0 = all history)"
    )
    ap.add_argument("--since", type=date.fromisoformat)
    ap.add_argument("--until", type=date.fromisoformat)
    ap.add_argument("--json", type=Path, help="write the replay result (also the NightValidation input)")
    ap.add_argument("--tonight", action="store_true", help="plan tonight from the latest data instead of replaying")
    ap.add_argument("--validation", type=Path, help="replay json used as the gate's validation record (--tonight)")
    ap.add_argument("--bedroom-sensor")
    ap.add_argument("--fan-mode", default="whole_house_fan", choices=["whole_house_fan", "hvac_fan", "disabled"])
    ap.add_argument("--floor", type=float, default=66.0)
    ap.add_argument("--ceiling", type=float, default=72.0)
    ap.add_argument(
        "--hold", type=float, default=68.0, help="daytime heat setpoint used to roll to bedtime (--tonight)"
    )
    a = ap.parse_args(argv)
    tz = ZoneInfo(a.tz)
    samples = _load_samples(a.chart_log, tz, a.forecast_archive)
    if not samples:
        print("no samples in the chart log", file=sys.stderr)
        return 2
    if a.tonight:
        if not a.forecast_archive:
            print("--tonight needs --forecast-archive", file=sys.stderr)
            return 2
        snap = json.loads(a.forecast_archive.read_text(encoding="utf-8"))["snapshots"][-1]
        validation = None
        if a.validation:
            validation = ns.NightValidation.from_dict(json.loads(a.validation.read_text(encoding="utf-8")))
        st, model = tonight(
            samples,
            snap,
            tz,
            validation=validation,
            bedroom=a.bedroom_sensor,
            fan_mode=a.fan_mode,
            floor_f=a.floor,
            ceiling_f=a.ceiling,
            bed=_hm(a.bed),
            wake=_hm(a.wake),
            train_days=a.train_days,
            hold_f=a.hold,
        )
        print(f"model: {p4.summary_line(model)}")
        print_strategy(st)
        if a.json:
            a.json.write_text(json.dumps(st.to_dict(include_debug=True), indent=1), encoding="utf-8")
        return 0
    rep = run(samples, tz, bed=_hm(a.bed), wake=_hm(a.wake), train_days=a.train_days, since=a.since, until=a.until)
    print_report(rep)
    if a.json:
        a.json.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
