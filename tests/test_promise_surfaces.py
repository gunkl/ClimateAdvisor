"""Promise 8 (surfaces_agree): what Climate Advisor shows matches the band it arms (Issue #1128 I6).

Occupant first: the person trusts the Status card, the chart, the briefing and the Next Action line to
say what the house is held to.  P8 reads the REAL production surfaces on the live FakeHass
coordinator session (``tools/sim_harness/surfaces.py``) and compares them with the engine's resolved
band; the pure comparison is tested here with hand-built inputs, the readers against real goldens.

Known divergences on HEAD are a ratchet (``tools/simulations/surface_divergences.json``): an unlisted
one fails, a listed one that no longer diverges also fails.  Goldens are LOCKED.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tools.sim_harness import surfaces as S

REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_FILES = sorted(p for p in S.GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")
_LEDGER = S.load_divergences()
_ARMED = S.Armed(floor=68.0, ceiling=74.0, active="ceiling")


@pytest.fixture(scope="module", autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ── pure comparison (the P8 verdict itself) ───────────────────────────────────────────────────
def test_matching_surfaces_agree() -> None:
    shown = [S.Shown("chart_band", 68.0, 74.0), S.Shown("status_api", 68.4, 73.6), S.Shown("briefing", None, 74.0)]
    assert S.check_surfaces(_ARMED, shown) == []


def test_a_wrong_edge_is_reported_in_plain_english() -> None:
    out = S.check_surfaces(_ARMED, [S.Shown("chart_band", 66.0, 77.0)])
    assert len(out) == 1
    assert "low edge 66.0 F vs armed 68.0 F" in out[0][1] and "high edge 77.0 F vs armed 74.0 F" in out[0][1]


def test_tolerance_is_display_rounding_not_a_free_pass() -> None:
    assert S.check_surfaces(_ARMED, [S.Shown("status_api", 68.0, 74.5)]) == []  # inside 0.6 F
    assert S.check_surfaces(_ARMED, [S.Shown("status_api", 68.0, 74.7)])  # outside


def test_a_surface_that_does_not_quote_an_edge_is_not_compared() -> None:
    assert S.check_surfaces(_ARMED, [S.Shown("next_action", None, None)]) == []


def test_nat_vent_target_must_sit_inside_the_band() -> None:
    assert S.check_surfaces(_ARMED, [S.Shown("nat_vent_target", 71.0, None, kind="inside")]) == []
    assert S.check_surfaces(_ARMED, [S.Shown("nat_vent_target", 79.0, None, kind="inside")])


def test_thermostat_hold_flags_only_a_looser_hold() -> None:
    """Tighter than armed is pre-cool / pre-heat / TOU banking on purpose; looser is the occupant's harm."""
    tighter = S.Shown("thermostat_hold", None, 70.0, kind="hold")
    looser = S.Shown("thermostat_hold", None, 78.0, kind="hold")
    assert S.check_surfaces(_ARMED, [tighter]) == []
    assert S.check_surfaces(_ARMED, [looser])
    assert S.check_surfaces(_ARMED, [S.Shown("thermostat_hold", 64.0, None, kind="hold")])


def test_flip_points_find_where_guidance_changes_and_ignore_the_digits_in_the_text() -> None:
    texts = [
        (70.0, "Comfortable"),
        (72.0, "Comfortable"),
        (74.0, "Open windows (indoor 74.0F)"),
        (76.0, "Open windows (indoor 76.0F)"),
    ]
    assert S.sweep_flip_points(texts) == [73.0]


# ── the ratchet ─────────────────────────────────────────────────────────────────────────────
def _div(surface: str, scenario: str, signature: str = "sleep/high") -> S.Divergence:
    return S.Divergence(surface, scenario, "t", "step", "d", (68.0, 74.0), (66.0, None), signature=signature)


def test_ratchet_unlisted_divergence_fails_and_listed_is_tolerated() -> None:
    ledger = [{"surface": "chart_band", "scenario": "alpha", "issue": 1, "signature": ["sleep/high"]}]
    unlisted, stale = S.judge({("chart_band", "alpha"): [_div("chart_band", "alpha")]}, ledger, scope={"alpha"})
    assert unlisted == [] and stale == []
    unlisted, _ = S.judge({("status_api", "alpha"): [_div("status_api", "alpha")]}, ledger, scope={"alpha"})
    assert [k for k, _ in unlisted] == [("status_api", "alpha")]


def test_ratchet_a_new_signature_on_an_already_ledgered_surface_fails() -> None:
    """The hole this closes: a ledger row matched by (surface, scenario) alone tolerated EVERY future
    divergence on that surface.  A different time band or edge is a different defect and must fail."""
    ledger = [{"surface": "next_action", "context": "generated", "issue": 1136, "signature": ["sleep/high"]}]
    key = ("next_action", "context:generated")
    ok, _ = S.judge({key: [_div("next_action", "g", "sleep/high")]}, ledger, scope=set())
    assert ok == []
    daytime, _ = S.judge(
        {key: [_div("next_action", "g", "sleep/high"), _div("next_action", "g", "awake/high")]}, ledger, scope=set()
    )
    assert [(k, [d.signature for d in rows]) for k, rows in daytime] == [(key, ["awake/high"])]
    other_edge, _ = S.judge({key: [_div("next_action", "g", "sleep/low")]}, ledger, scope=set())
    assert other_edge


def test_ratchet_a_signature_that_stopped_happening_is_stale() -> None:
    ledger = [{"surface": "next_action", "scenario": "alpha", "issue": 1136, "signature": ["sleep/high", "sleep/low"]}]
    _, stale = S.judge(
        {("next_action", "alpha"): [_div("next_action", "alpha", "sleep/high")]}, ledger, scope={"alpha"}
    )
    assert stale == [("next_action", "alpha [sleep/low]")]


def test_signature_names_band_and_edge() -> None:
    assert S.divergence_signature("awake", "next_action shows high edge 79.2 F vs armed 77.0 F") == "awake/high"
    assert (
        S.divergence_signature("sleep", "next_action shows low edge 1 F vs armed 2 F; high edge 3 F vs armed 4 F")
        == "sleep/low+high"
    )
    assert (
        S.divergence_signature("awake", "thermostat_hold heat held at 60.0 F, below the armed 64.0 F floor")
        == "awake/low"
    )
    assert S.divergence_signature("awake", "chart_band reader failed: KeyError: x") == "awake/reader_failed"
    assert (
        S.divergence_signature("awake", "Fan card says 'inactive' while the whole-house fan is physically on")
        == "awake/text"
    )


def test_ratchet_stale_entry_inside_the_covered_scope_fails_but_outside_it_does_not() -> None:
    """Proves the stale-entry rule: a listed divergence that no longer happens must be removed."""
    ledger = [{"surface": "chart_band", "scenario": "alpha", "issue": 1, "signature": ["sleep/high"]}]
    _, stale = S.judge({}, ledger, scope={"alpha"})
    assert stale == [("chart_band", "alpha")]
    _, stale = S.judge({}, ledger, scope={"beta"})  # alpha was not run: not provably stale
    assert stale == []


def test_a_ledger_row_without_a_signature_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "d.json"
    bad.write_text(
        json.dumps({"divergences": [{"surface": "chart_band", "scenario": "a", "issue": 1}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="signature"):
        S.load_divergences(bad)


def test_ledger_rows_are_well_formed_and_name_real_goldens() -> None:
    names = {p.stem for p in _GOLDEN_FILES}
    raw = json.loads(S.DIVERGENCES_PATH.read_text(encoding="utf-8"))["divergences"]
    assert len(raw) == len(_LEDGER), "a ledger row has an unknown surface or neither scenario nor context"
    for row in _LEDGER:
        assert row.get("issue") is not None and row.get("evidence"), f"ledger row needs an issue and evidence: {row}"
        if row["issue"] == "unfiled":  # only a harness-premise row may be unfiled (it is not a production claim)
            assert row["evidence"].startswith("HARNESS PREMISE"), f"ledger row must name an issue number: {row}"
        assert row["signature"], f"ledger row must pin the divergence signature(s): {row}"
        if row.get("scenario"):
            assert row["scenario"] in names, f"ledger row for unknown golden {row['scenario']}"
        else:
            assert row["context"] == "generated"


# ── readers on real goldens: every reader answers, nothing unexplained diverges ──────────────────────
_RUNS: dict[str, S.SurfaceRun] = {}


def _run(path: Path) -> S.SurfaceRun:
    if path.stem not in _RUNS:
        _RUNS[path.stem] = S.run_golden_surfaces(path)
    return _RUNS[path.stem]


@pytest.mark.parametrize("path", _GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden_surfaces_agree_with_the_armed_band(path: Path) -> None:
    run = _run(path)
    assert run.error is None, f"{path.stem}: the coordinator session could not host this scenario: {run.error}"
    found = S.collapse([run])
    unlisted, stale = S.judge(found, _LEDGER, scope={path.stem})
    assert not unlisted, "unlisted surface divergence:\n" + "\n".join(
        S.format_divergence(d) for _, rows in unlisted for d in rows[:3]
    )
    assert not stale, f"{path.stem}: stale surface_divergences.json row(s) {stale} - it no longer diverges; remove it"


def test_every_reader_returned_a_value_somewhere_on_the_goldens() -> None:
    """Effectiveness: a reader that never answers would let P8 pass vacuously."""
    totals: dict[str, int] = {}
    for p in _GOLDEN_FILES:
        for surface, n in _run(p).compared.items():
            totals[surface] = totals.get(surface, 0) + n
    for surface in (
        "chart_band",
        "status_api",
        "briefing",
        "next_action",
        "ode_ceiling",
        "thermostat_hold",
        "cards",
        "fan_cards",
    ):
        assert totals.get(surface, 0) > 0, f"reader '{surface}' never produced a value on any golden"


def test_every_golden_reached_a_classified_sample() -> None:
    """A run that never had a classification samples nothing and would pass for the wrong reason."""
    unsampled = [p.stem for p in _GOLDEN_FILES if _run(p).samples == 0]
    assert len(unsampled) <= 25, f"too many goldens never sampled (no classification): {unsampled}"


# ── effectiveness: a wrong surface IS caught on a real engine ──────────────────────────────────────────
def test_p8_catches_a_deliberately_wrong_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break the chart band reader's source (the real production builder returns a shifted band) and the
    probe must report it: the verdict is not vacuous."""
    path = next(p for p in _GOLDEN_FILES if p.stem == "cold_day_heat_all_day_with_override")
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario.setdefault("config", {}).update(
        {"sleep_time": "22:30", "wake_time": "06:30", "sleep_heat": 64.0, "sleep_cool": 72.0}
    )
    from custom_components.climate_advisor import coordinator as coord_mod

    real = coord_mod.ClimateAdvisorCoordinator._build_target_band_for

    def shifted(self, *a, **kw):
        return [{**e, "lower": e["lower"] - 5.0, "upper": e["upper"] + 5.0} for e in real(self, *a, **kw)]

    monkeypatch.setattr(coord_mod.ClimateAdvisorCoordinator, "_build_target_band_for", shifted)
    run = S.run_scenario_surfaces(scenario, "p8_negative_control")
    assert any(d.surface == "chart_band" for d in run.divergences)


def test_p8_is_not_in_the_home_registry() -> None:
    """P8 needs live coordinator objects; the production registry holds only pure PromiseInput checks."""
    from custom_components.climate_advisor.invariant_watchdog import PROMISES

    assert S.P8_KEY not in {s.key for s in PROMISES}
    assert S.P8_HOME_LEVEL is None and S.P8_SEVERITY == "hard"


# ── generated days ───────────────────────────────────────────────────────────────────────────────
def test_generated_days_surfaces_agree_with_the_ratchet() -> None:
    """The fixed generated sample (seeds 101/102 x 10 days): same verdict rules as the goldens."""
    runs, errors = S.run_generated_surfaces(list(S.UNIT_SAMPLE_SEEDS), S.UNIT_SAMPLE_DAYS)
    assert not errors, errors[:3]
    assert sum(r.samples for r in runs) > 0
    found = S.collapse(runs, context="generated")
    unlisted, stale = S.judge(found, _LEDGER, scope={"context:generated"})
    assert not unlisted, "unlisted surface divergence on generated days:\n" + "\n".join(
        S.format_divergence(d) for _, rows in unlisted for d in rows[:3]
    )
    assert not stale, f"stale generated-context ledger row(s) {stale}"
