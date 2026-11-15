"""Thermostat profile (Issues #1556, #1566-#1569): learn what each fan mode does; never latch the blower on.

Occupant view: on a Honeywell T6 Pro (``["Auto low", "Low", "Circulation"]``) Climate Advisor sent ``Low`` (fan ON)
every time it turned its fan "off", so the blower ran for days and no thermal observation ever committed (#1556).
"""

from __future__ import annotations

import json
import random
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.fan_mode_resolver import (
    is_thermostat_fan_physically_active,
    is_thermostat_schedule_signal,
    resolve_fan_mode_command,
)
from custom_components.climate_advisor.thermostat_profile import (
    EVIDENCE_CAP_MINUTES,
    PROFILE_ARCHIVE_MAX,
    FanModeEvidence,
    FanRole,
    ThermostatIdentity,
    ThermostatProfileStore,
    build_fan_profile,
    classify_fan_mode_name,
    evidence_kind,
    fan_command_starts_blower,
    live_fan_profile,
)

T6 = ["Auto low", "Low", "Circulation"]
_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"
_SIM = Path(__file__).resolve().parent.parent / "tools" / "simulations"


def ev(fan: float, idle: float) -> FanModeEvidence:
    return FanModeEvidence(fan_min=fan, idle_min=idle)


# ── #1556 reproduction: the reporter's thermostat ─────────────────────────────────────────────────────────────
class TestT6ProNameGuess:
    def test_fan_off_sends_auto_low_not_low(self):
        """Before: off -> 'Low' (fan ON on this thermostat). After: the auto variant."""
        assert resolve_fan_mode_command(T6, "off") == "Auto low"

    def test_fan_on_sends_low(self):
        assert resolve_fan_mode_command(T6, "on") == "Low"

    def test_on_and_off_differ(self):
        assert resolve_fan_mode_command(T6, "on") != resolve_fan_mode_command(T6, "off")

    def test_auto_low_while_idle_reads_not_running(self):
        """Before: True -- every heat/cool observation was abandoned as fan interference."""
        assert is_thermostat_fan_physically_active("Auto low", "idle") is False

    def test_auto_low_with_a_reported_blower_reads_running(self):
        assert is_thermostat_fan_physically_active("Auto low", "fan") is True

    def test_auto_low_blower_is_the_thermostat_schedule(self):
        assert is_thermostat_schedule_signal("Auto low", "fan") is True

    def test_low_reads_running_even_when_idle(self):
        assert is_thermostat_fan_physically_active("Low", "idle") is True

    def test_circulation_follows_the_reported_action(self):
        assert is_thermostat_fan_physically_active("Circulation", "idle") is False
        assert is_thermostat_fan_physically_active("Circulation", "fan") is True

    def test_circulation_without_a_reported_action_reads_running(self):
        """No hvac_action: the blower may run any time in circulate, so the conservative read stands."""
        assert is_thermostat_fan_physically_active("Circulation", "") is True

    @pytest.mark.parametrize("action", ["idle", "heating", "cooling", "off", ""])
    def test_auto_low_never_reads_running_without_a_reported_blower(self, action):
        assert is_thermostat_fan_physically_active("Auto low", action) is False


# ── Unchanged behaviour for thermostats that already worked ───────────────────────────────────────────────────
class TestUnchangedVocabularies:
    @pytest.mark.parametrize(
        ("modes", "off", "on"),
        [
            (["auto", "on"], "auto", "on"),
            (["Auto", "On"], "Auto", "On"),
            (["auto", "low", "medium", "high"], "auto", "high"),
            (["on", "off"], "off", "on"),
            (["auto", "on", "circulate"], "auto", "on"),
            (["auto", "diffuse", "focus"], "auto", None),
        ],
    )
    def test_existing_lists_pick_the_same_values(self, modes, off, on):
        assert resolve_fan_mode_command(modes, "off") == off
        assert resolve_fan_mode_command(modes, "on") == on

    def test_no_idle_mode_keeps_the_lowest_speed_fallback_and_says_so(self):
        p = build_fan_profile(["low", "medium", "high"])
        assert p.off_value == "low"
        assert any("lowest speed" in x for x in p.problems)

    def test_no_safe_off_value_sends_nothing(self):
        p = build_fan_profile(["Smart", "Boost"])
        assert p.off_value is None
        assert p.status == "blocked"
        assert resolve_fan_mode_command(["Smart", "Boost"], "off") is None

    @pytest.mark.parametrize("value", ["", "auto", "off", "AUTO ", "Off"])
    def test_idle_values_unchanged(self, value):
        assert is_thermostat_fan_physically_active(value, "idle") is False
        assert is_thermostat_fan_physically_active(value, "fan") is True

    @pytest.mark.parametrize("value", ["on", "low", "medium", "high", "Smart"])
    def test_running_values_unchanged(self, value):
        assert is_thermostat_fan_physically_active(value, "idle") is True

    def test_empty_list(self):
        assert resolve_fan_mode_command([], "off") is None
        assert resolve_fan_mode_command(None, "on") is None


class TestNameGuess:
    @pytest.mark.parametrize(
        ("name", "role"),
        [
            ("auto", FanRole.AUTO),
            ("Auto low", FanRole.AUTO),
            ("Auto High", FanRole.AUTO),
            ("automatic", FanRole.AUTO),
            ("off", FanRole.OFF),
            ("", FanRole.OFF),
            ("Circulation", FanRole.CIRCULATE),
            ("circulate", FanRole.CIRCULATE),
            ("on", FanRole.ON),
            ("On low", FanRole.ON),
            ("Continuous", FanRole.ON),
            ("Low", FanRole.ON),
            ("high", FanRole.ON),
            ("Smart", FanRole.UNKNOWN),
            ("diffuse", FanRole.UNKNOWN),
        ],
    )
    def test_classify(self, name, role):
        assert classify_fan_mode_name(name) is role


# ── #1567: evidence from the thermostat's own reports ────────────────────────────────────────────────────────
class TestEvidence:
    def test_evidence_kind(self):
        assert evidence_kind("fan") is True
        assert evidence_kind("idle") is False
        for action in ("heating", "cooling", "off", "", None, "defrosting"):
            assert evidence_kind(action) is None

    def test_too_little_evidence_keeps_the_name_guess(self):
        p = build_fan_profile(T6, {"Low": ev(10, 0)})
        assert p.info["Low"].source == "name"

    def test_low_observed_continuous_is_verified(self):
        p = build_fan_profile(T6, {"Low": ev(60, 0), "Auto low": ev(5, 55)})
        assert p.info["Low"].role is FanRole.ON and p.info["Low"].source == "observed"
        assert p.off_value == "Auto low" and p.on_value == "Low"
        assert p.status == "verified"

    def test_a_speed_observed_idle_is_never_the_on_value(self):
        """A 'low' the thermostat reports idle in does not run the blower: CA cannot express fan-on with it."""
        p = build_fan_profile(["auto", "low"], {"low": ev(0, 60)}, reports_fan=True)
        assert p.info["low"].role is FanRole.AUTO
        assert p.on_value is None

    def test_an_auto_observed_continuous_is_never_the_off_value(self):
        """Evidence vetoes the name: an 'Auto low' that keeps the blower running is flagged and never sent as off,
        and the lowest-speed fallback does not step in (on a T6 that would be 'Low' = fan ON)."""
        p = build_fan_profile(T6, {"Auto low": ev(120, 0)})
        assert p.off_value is None
        assert any("named like an idle mode" in x for x in p.problems)

    def test_an_unnamed_mode_observed_idle_becomes_the_off_value(self):
        p = build_fan_profile(["Smart", "Boost"], {"Smart": ev(5, 55)})
        assert p.off_value == "Smart"

    def test_observed_continuous_lowest_speed_blocks_the_fallback(self):
        """The fallback is only the LOWEST speed; it never steps up to a higher one."""
        p = build_fan_profile(["low", "high"], {"low": ev(60, 0)})
        assert p.off_value is None

    def test_fallback_never_equals_the_on_value(self):
        p = build_fan_profile(["Circulation", "low"])
        assert p.on_value == "low" and p.off_value is None

    def test_ecobee_fan_minimum_runtime_stays_auto(self):
        """An auto mode with a fan-min-on-time (blower most of each hour) is in between: the name guess stands."""
        p = build_fan_profile(["auto", "on"], {"auto": ev(45, 3)})
        assert p.info["auto"].role is FanRole.AUTO
        assert p.off_value == "auto"

    def test_old_evidence_ages_out_by_new_evidence(self):
        e = ev(0, EVIDENCE_CAP_MINUTES)  # a full day of "idle"
        for _ in range(int(EVIDENCE_CAP_MINUTES / 5) * 4):  # ~4 days of new evidence: the old fades by about e^-4
            e.add(fan=True, minutes=5)
        assert e.total_min <= EVIDENCE_CAP_MINUTES + 1e-6
        assert e.observed_runs_blower() is True

    def test_evidence_never_ages_out_by_time_alone(self):
        e = ev(60, 0)
        assert e.observed_runs_blower() is True  # nothing in the model is time-based

    def test_evidence_round_trip_and_malformed(self):
        assert FanModeEvidence.from_dict(ev(1.5, 2).to_dict()) == ev(1.5, 2)
        assert FanModeEvidence.from_dict({"fan_min": "x"}) == FanModeEvidence()
        assert FanModeEvidence.from_dict(None) == FanModeEvidence()


class TestConsistencyRules:
    """For any list and any evidence, the values CA sends obey the rules that #1556 broke."""

    VOCAB = ["auto", "Auto low", "off", "on", "Low", "low", "high", "Circulation", "Smart", "Continuous"]

    def test_property(self):
        rng = random.Random(1566)
        for _ in range(3000):
            modes = rng.sample(self.VOCAB, rng.randint(1, 5))
            evidence = {
                m: ev(rng.choice([0, 10, 60, 120]), rng.choice([0, 2, 30, 90])) for m in modes if rng.random() < 0.6
            }
            p = build_fan_profile(modes, evidence)
            if p.on_value is not None:
                assert p.info[p.on_value].role is FanRole.ON
            if p.off_value is not None:
                i = p.info[p.off_value]
                assert i.role is not FanRole.CIRCULATE and i.role is not FanRole.UNKNOWN
                if i.role is FanRole.ON:  # only the no-idle-mode fallback, never an observed-continuous mode
                    assert i.source == "name"
                    assert not any(p.info[m].name_role in (FanRole.AUTO, FanRole.OFF) for m in modes)
            assert p.on_value is None or p.on_value != p.off_value


# ── #1568: fingerprint, mode-list change, swap ────────────────────────────────────────────────────────────────
class TestStore:
    A = ThermostatIdentity("device:a", "Honeywell", "T6 Pro")
    B = ThermostatIdentity("device:b", "ecobee", "Smart Thermostat")

    def _learned(self) -> ThermostatProfileStore:
        s = ThermostatProfileStore()
        assert s.select(self.A, T6, "2026-11-14T00:00").kind == "new"
        s.record("Low", fan=True, minutes=60)
        s.record("Auto low", fan=False, minutes=60)
        return s

    def test_learned_profile_survives_a_save_and_restore(self):
        back = ThermostatProfileStore.from_dict(self._learned().to_dict())
        assert back.select(self.A, T6, "2026-11-14T01:00").kind == "none"
        assert back.fan_profile(T6).status == "verified"

    def test_same_device_changed_mode_list_keeps_unchanged_evidence(self):
        s = self._learned()
        change = s.select(self.A, ["Auto low", "Low", "Circulate"], "t")
        assert change.kind == "modes_changed"
        assert change.added == ("Circulate",) and change.removed == ("Circulation",)
        assert s.fan_profile(["Auto low", "Low", "Circulate"]).info["Low"].source == "observed"

    def test_swap_starts_fresh_and_swap_back_restores(self):
        s = self._learned()
        swap = s.select(self.B, ["auto", "on"], "t2")
        assert swap.kind == "swapped" and swap.previous.fingerprint == "device:a"
        assert s.fan_profile(["auto", "on"]).status == "unverified"
        assert [p.fingerprint for p in s.archived()] == ["device:a"]
        assert s.select(self.A, T6, "t3").kind == "restored"
        assert s.fan_profile(T6).status == "verified"

    def test_archive_is_bounded(self):
        s = ThermostatProfileStore()
        for n in range(PROFILE_ARCHIVE_MAX + 4):
            s.select(ThermostatIdentity(f"device:{n}"), ["auto", "on"], f"2026-11-{n + 1:02d}")
        assert len(s.archived()) == PROFILE_ARCHIVE_MAX
        assert "device:0" not in {p.fingerprint for p in s.archived()}

    def test_evidence_outside_the_list_is_ignored(self):
        s = self._learned()
        s.record("Turbo", fan=True, minutes=60)
        assert "Turbo" not in s.active.evidence

    @pytest.mark.parametrize(
        "bad", [None, [], {"profiles": "x"}, {"profiles": [1, {"x": 1}]}, {"active": "missing", "profiles": []}]
    )
    def test_load_tolerates_malformed_payloads(self, bad):
        assert ThermostatProfileStore.from_dict(bad).active is None

    def test_live_profile_helper(self):
        s = self._learned()
        state = types.SimpleNamespace(attributes={"fan_modes": T6})
        assert live_fan_profile(s, state).status == "verified"
        assert live_fan_profile(None, state).status == "unverified"
        assert live_fan_profile(s, None).modes == ()
        assert live_fan_profile(None, types.SimpleNamespace(attributes=MagicMock())).modes == ()


def test_fan_command_starts_blower():
    assert fan_command_starts_blower("Low") is True
    assert fan_command_starts_blower("on") is True
    assert fan_command_starts_blower("Auto low") is False
    p = build_fan_profile(["auto", "low"], {"low": ev(0, 60)}, reports_fan=True)
    assert fan_command_starts_blower("low", p) is False


def test_resolver_uses_a_learned_profile_only_for_its_own_list():
    p = build_fan_profile(["Smart", "Boost"], {"Smart": ev(5, 55)})
    assert resolve_fan_mode_command(["Smart", "Boost"], "off", profile=p) == "Smart"
    assert resolve_fan_mode_command(["Smart", "Turbo"], "off", profile=p) is None  # a stale profile is ignored
    assert resolve_fan_mode_command(T6, "off", profile=MagicMock()) == "Auto low"  # a non-profile is ignored


# ── Coordinator: the 5-minute tick, logging, persistence, diagnostics ─────────────────────────────────────────
def _coord(attrs: dict, state: str = "heat"):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    c = object.__new__(ClimateAdvisorCoordinator)
    c.hass = MagicMock()
    c._st = types.SimpleNamespace(state=state, attributes=dict(attrs))
    c.hass.states.get.side_effect = lambda eid: c._st if eid == "climate.t" else None
    c.config = {"climate_entity": "climate.t"}
    c._thermostat_profiles = ThermostatProfileStore()
    c._thermostat_profile_last_ts = None
    c._thermostat_profile_logged = None
    c._hvac_report_watch = {}
    c.automation_engine = MagicMock()
    c.automation_engine._get_thermostat_capabilities.return_value = types.SimpleNamespace(
        modes=("heat", "cool", "off"),
        supports_heat=True,
        supports_cool=True,
        supports_heat_cool=False,
        supports_dual_setpoint=False,
    )
    c._identity = ThermostatIdentity("device:a", "Honeywell", "T6 Pro")
    c._thermostat_identity = lambda entity: c._identity
    return c


def _ticks(c, n: int, *, start: datetime, step_min: float = 5.0):
    for k in range(n):
        with patch(_NOW, return_value=start + timedelta(minutes=step_min * k)):
            c._tick_thermostat_profile()


class TestCoordinatorTick:
    T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def test_t6_learns_low_is_continuous_from_its_own_reports(self):
        c = _coord({"fan_modes": T6, "fan_mode": "Low", "hvac_action": "fan"})
        _ticks(c, 13, start=self.T0)  # 60 minutes of "fan" while Low
        c._st.attributes.update(fan_mode="Auto low", hvac_action="idle")
        _ticks(c, 13, start=self.T0 + timedelta(hours=2))
        p = c._fan_profile()
        assert p.info["Low"].source == "observed" and p.info["Low"].role is FanRole.ON
        assert p.info["Auto low"].source == "observed"
        assert p.status == "verified"
        assert p.reports_hvac_action is True

    def test_heating_minutes_are_not_fan_evidence(self):
        c = _coord({"fan_modes": T6, "fan_mode": "Auto low", "hvac_action": "heating"})
        _ticks(c, 30, start=self.T0)
        assert c._thermostat_profiles.active.evidence == {}

    def test_a_thermostat_without_hvac_action_stays_unverified(self):
        c = _coord({"fan_modes": T6, "fan_mode": "Low"})
        _ticks(c, 30, start=self.T0)
        p = c._fan_profile()
        assert p.status == "unverified" and p.reports_hvac_action is False
        assert c._thermostat_profiles.active.evidence == {}

    def test_unavailable_gap_is_not_credited(self):
        c = _coord({"fan_modes": T6, "fan_mode": "Low", "hvac_action": "fan"})
        _ticks(c, 1, start=self.T0)
        c._st.state = "unavailable"
        _ticks(c, 1, start=self.T0 + timedelta(minutes=5))
        c._st.state = "heat"
        _ticks(c, 1, start=self.T0 + timedelta(hours=3))
        assert c._thermostat_profiles.active.evidence.get("Low") is None

    def test_profile_logged_once_per_change_and_a_warning_when_blocked(self):
        c = _coord({"fan_modes": ["Smart", "Boost"], "fan_mode": "Smart", "hvac_action": "heating"})
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as log:
            _ticks(c, 5, start=self.T0)
        profile_lines = [a for a in log.info.call_args_list if a.args[0].startswith("Thermostat profile:")]
        assert len(profile_lines) == 1
        assert "device=Honeywell T6 Pro" in profile_lines[0].args[1]
        assert "status=blocked" in profile_lines[0].args[1]
        assert any("Fan control held back" in a.args[0] for a in log.warning.call_args_list)

    def test_device_swap_logs_a_warning_and_starts_fresh(self):
        c = _coord({"fan_modes": T6, "fan_mode": "Low", "hvac_action": "fan"})
        _ticks(c, 13, start=self.T0)
        c._identity = ThermostatIdentity("device:b", "ecobee", "Smart Thermostat")
        c._st.attributes.update(fan_modes=["auto", "on"], fan_mode="auto")
        with patch("custom_components.climate_advisor.coordinator._LOGGER") as log:
            _ticks(c, 1, start=self.T0 + timedelta(hours=2))
        assert any(a.args[0].startswith("Thermostat changed") for a in log.warning.call_args_list)
        assert c._fan_profile().status == "unverified"
        assert c.thermostat_profile_diagnostics()["archived_devices"][0]["device"] == "Honeywell T6 Pro"

    def test_diagnostics_payload_and_text(self):
        from custom_components.climate_advisor.diagnostic_snapshot import render_diagnostic_snapshot_text

        c = _coord({"fan_modes": T6, "fan_mode": "Low", "hvac_action": "fan"})
        _ticks(c, 13, start=self.T0)
        d = c.thermostat_profile_diagnostics()
        assert d["device"] == "Honeywell T6 Pro"
        assert d["fan"]["off_value"] == "Auto low" and d["fan"]["on_value"] == "Low"
        assert d["capabilities"]["supports_heat"] is True
        text = render_diagnostic_snapshot_text({"version": "x", "climate_entity": {}, "thermostat_profile": d})
        assert "fan off -> 'Auto low'" in text and "'Low': on (observed" in text

    def test_identity_from_the_device_registry(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        c = object.__new__(ClimateAdvisorCoordinator)
        c.hass = MagicMock()
        entry = types.SimpleNamespace(device_id="dev1")
        device = types.SimpleNamespace(manufacturer="Honeywell", model="T6 Pro")
        with (
            patch("custom_components.climate_advisor.coordinator.er.async_get") as er_get,
            patch("custom_components.climate_advisor.coordinator.dr.async_get") as dr_get,
        ):
            er_get.return_value.async_get.return_value = entry
            dr_get.return_value.async_get.return_value = device
            ident = ClimateAdvisorCoordinator._thermostat_identity(c, "climate.t")
            assert ident == ThermostatIdentity("device:dev1", "Honeywell", "T6 Pro")
            er_get.return_value.async_get.return_value = None
            assert ClimateAdvisorCoordinator._thermostat_identity(c, "climate.t").fingerprint == "entity:climate.t"
            er_get.side_effect = RuntimeError("registry not ready")
            assert ClimateAdvisorCoordinator._thermostat_identity(c, "climate.t").fingerprint == "entity:climate.t"

    def test_promise_journal_stamps_a_t6_low_as_a_fan_start(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        c = _coord({"fan_modes": T6, "fan_mode": "Auto low"})
        c._promise_journal = MagicMock()
        c._automation_enabled = True
        c._get_indoor_temp = lambda: None
        c.config["temp_unit"] = "fahrenheit"
        ClimateAdvisorCoordinator._promise_observe_command(c, "climate", "set_fan_mode", {"fan_mode": "Low"}, "x")
        ClimateAdvisorCoordinator._promise_observe_command(c, "climate", "set_fan_mode", {"fan_mode": "Auto low"}, "x")
        cmds = [call.args[0] for call in c._promise_journal.record_command.call_args_list]
        assert [cm.fan_on for cm in cmds] == [True, False]


def test_engine_reads_the_coordinators_profile_through_its_provider():
    from custom_components.climate_advisor.automation import AutomationEngine

    ae = object.__new__(AutomationEngine)
    sentinel = build_fan_profile(T6, {"Low": ev(60, 0)})
    ae._fan_profile_provider = lambda: sentinel
    assert AutomationEngine._fan_profile(ae) is sentinel
    ae._fan_profile_provider = None
    ae.hass = MagicMock()
    ae.climate_entity = "climate.t"
    ae.hass.states.get.return_value = types.SimpleNamespace(attributes={"fan_modes": T6})
    assert AutomationEngine._fan_profile(ae).off_value == "Auto low"


def test_learned_profile_survives_the_state_file_round_trip():
    """#1568: a restart (or a release) never re-learns from scratch -- the real save and restore code paths."""
    import asyncio

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
    from tests.test_coordinator_health import _make_restore_coord_stub
    from tests.test_promise_wiring import TestPersistence

    learned = ThermostatProfileStore()
    learned.select(ThermostatIdentity("device:a", "Honeywell", "T6 Pro"), T6, "2026-11-14T00:00")
    learned.record("Low", fan=True, minutes=60)
    learned.record("Auto low", fan=False, minutes=60)
    with (
        patch.object(ClimateAdvisorCoordinator, "_thermostat_profiles", learned, create=True),
        patch(_NOW, return_value=datetime(2026, 11, 14, tzinfo=UTC)),
    ):
        saved = TestPersistence()._state({})
    assert saved["thermostat_profiles"]["active"] == "device:a"

    coord = _make_restore_coord_stub(saved)
    asyncio.run(coord.async_restore_state())
    restored = coord._thermostat_profiles
    assert isinstance(restored, ThermostatProfileStore)
    assert restored.fan_profile(T6).status == "verified"


# ── Real engine, real scenarios: the T6 Pro's vocabulary through whole fan sessions (Tier A harness) ─────────────
@pytest.mark.parametrize(
    "name",
    [
        "nat_vent_thermostat_cycling",
        "issue-359-fan-state-machine",
        "nat-vent-outdoor-rises-above-indoor-exit__hvac_fan",
    ],
)
def test_t6_fan_sessions_end_on_auto_low_through_the_real_engine(name):
    """Occupant view (#1556): every fan session CA ends puts the T6 Pro back on Auto, never leaves it on 'Low'."""
    for p in (str(_SIM.parent.parent), str(_SIM.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from sim_harness.run_production import run_production_scenario

    path = next(_SIM.glob(f"*/{name}.json"))
    sc = json.loads(path.read_text())
    sc["config"]["thermostat_fan_modes"] = T6
    result = run_production_scenario(sc, use_coordinator=bool(sc.get("use_coordinator")))
    sent = [a["data"].get("fan_mode") for a in result.action_log if a["service"] == "set_fan_mode"]
    assert sent, "the scenario must run at least one CA fan session"
    assert set(sent) <= {"Auto low", "Low"}, sent
    assert "Auto low" in sent, f"fan-off never sent the auto variant: {sent}"
    assert sent[-1] == "Auto low" and result.engine_state.get("_fan_active") is False


# ── Independent-review fixes (H1, H2, M1, M2, L1, L3, L4) ────────────────────────────────────────────────────
def test_h1_ca_never_starts_a_blower_it_cannot_stop_through_the_real_engine():
    """Mirror of #1556: with an on value but no off value, the real engine refuses to start the HVAC fan."""
    for p in (str(_SIM.parent.parent), str(_SIM.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from sim_harness.run_production import run_production_scenario

    from custom_components.climate_advisor.automation import AutomationEngine

    blocked = build_fan_profile(T6, {"Auto low": ev(120, 0)}, reports_fan=True)
    assert blocked.on_value == "Low" and blocked.off_value is None
    sc = json.loads(next(_SIM.glob("*/nat_vent_thermostat_cycling.json")).read_text())
    sc["config"]["thermostat_fan_modes"] = T6
    with patch.object(AutomationEngine, "_fan_profile", lambda self: blocked):
        result = run_production_scenario(sc, use_coordinator=bool(sc.get("use_coordinator")))
    assert [a for a in result.action_log if a["service"] == "set_fan_mode"] == []
    assert result.engine_state.get("_fan_active") is False


def test_h2_fan_only_time_teaches_nothing():
    c = _coord({"fan_modes": ["auto", "on"], "fan_mode": "auto", "hvac_action": "fan"}, state="fan_only")
    _ticks(c, 30, start=TestCoordinatorTick.T0)
    assert c._thermostat_profiles.active.evidence == {}
    assert c._fan_profile().off_value == "auto"


def test_m1_idle_reports_cannot_demote_an_on_mode_on_a_thermostat_that_never_reports_its_blower():
    p = build_fan_profile(["auto", "on"], {"on": ev(0, 120)})
    assert p.info["on"].role is FanRole.ON and p.on_value == "on"
    p = build_fan_profile(["auto", "on"], {"on": ev(0, 120)}, reports_fan=True)
    assert p.info["on"].role is FanRole.AUTO


def test_m1_through_the_tick_idle_with_fan_on_keeps_on():
    """An integration that says 'idle' while its continuous fan runs (and never says 'fan') keeps 'on' working."""
    c = _coord({"fan_modes": ["auto", "on"], "fan_mode": "on", "hvac_action": "idle"})
    _ticks(c, 30, start=TestCoordinatorTick.T0)
    p = c._fan_profile()
    assert p.on_value == "on" and p.reports_fan is False
    assert is_thermostat_fan_physically_active("on", "idle", profile=p) is True


def test_m2_a_temporarily_shorter_list_keeps_the_evidence():
    s = ThermostatProfileStore()
    a = ThermostatIdentity("device:a")
    s.select(a, T6, "t0")
    s.record("Low", fan=True, minutes=60)
    s.select(a, ["Auto low", "Circulation"], "t1")  # e.g. a re-interview reports a partial list
    s.select(a, T6, "t2")
    assert s.fan_profile(T6).info["Low"].source == "observed"


def test_l1_a_missing_fan_mode_value_reads_as_unknown_running():
    assert classify_fan_mode_name(None) is FanRole.UNKNOWN
    assert is_thermostat_fan_physically_active(None, "idle") is True
    assert is_thermostat_schedule_signal(None, "fan") is False


def test_l3_a_non_string_device_id_falls_back_to_the_entity():
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    c = object.__new__(ClimateAdvisorCoordinator)
    c.hass = MagicMock()
    with patch("custom_components.climate_advisor.coordinator.er.async_get") as er_get:
        er_get.return_value.async_get.return_value = types.SimpleNamespace(device_id=MagicMock())
        assert ClimateAdvisorCoordinator._thermostat_identity(c, "climate.t").fingerprint == "entity:climate.t"


def test_reports_fan_survives_a_restart():
    s = ThermostatProfileStore()
    s.select(ThermostatIdentity("device:a"), T6, "t0")
    s.note_reports_fan()
    assert ThermostatProfileStore.from_dict(s.to_dict()).fan_profile(T6).reports_fan is True
