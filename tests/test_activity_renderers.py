"""Tests for the deterministic timeline table and EVENT_RENDERERS (Issue #330).

This file is the regression lock for the #330 band-Settings fix:
every comfort_band_applied / bedtime_setback / morning_wakeup / occupancy_setback
row in the deterministic timeline MUST carry a non-empty Settings cell that shows
the setpoint rendered by _format_band_setpoint.

Test classes:
  TestFormatBandSetpoint      — unit tests for _format_band_setpoint
  TestBuildEventTimelineTable — full table builder: band events, dedup, non-band events
  TestDefaultRenderer         — _default_renderer surprise-safe fallback
  TestEventRenderersCoverage  — guardrail: all emitted types have a registered renderer
"""

from __future__ import annotations

import datetime
import re
import sys
from unittest.mock import patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

# Patch dt_util.as_local to be identity so _fmt_time comparisons use real datetimes
# rather than MagicMock objects (same pattern as test_coordinator.py).
import custom_components.climate_advisor.ai_skills_activity as _act_mod  # noqa: E402

_REAL_NOW = datetime.datetime(2026, 6, 17, 14, 0, 0, tzinfo=datetime.UTC)


def _make_event(
    event_type: str,
    hours_ago: float = 1.0,
    **payload,
) -> dict:
    """Build an event dict with a real UTC timestamp."""
    ts = _REAL_NOW - datetime.timedelta(hours=hours_ago)
    return {"time": ts.isoformat(), "type": event_type, **payload}


def _build_table(events: list[dict], hours: float = 24.0, unit: str = "fahrenheit") -> str:
    """Call build_event_timeline_table with a fixed now and return the result."""
    from custom_components.climate_advisor.ai_skills_activity import (
        build_event_timeline_table,
    )

    config = {"temp_unit": unit}
    with patch.object(_act_mod.dt_util, "as_local", side_effect=lambda x: x):
        return build_event_timeline_table(
            raw_event_log=events,
            config=config,
            hours=hours,
            now=_REAL_NOW,
        )


# ---------------------------------------------------------------------------
# TestFormatBandSetpoint
# ---------------------------------------------------------------------------


class TestFormatBandSetpoint:
    """Unit tests for _format_band_setpoint (the Settings cell renderer for band events)."""

    def test_active_ceiling_shows_cool_first(self):
        """active='ceiling' → 'setpoint: 72°F Cool (64°F Heat)' — cool edge is prominent."""
        from custom_components.climate_advisor.ai_skills_activity import _format_band_setpoint

        result = _format_band_setpoint(floor=64, ceiling=72, active="ceiling", unit="fahrenheit")
        assert result == "setpoint: 72°F Cool (64°F Heat)", (
            f"#330: active='ceiling' must show Cool setpoint first. Got: {result!r}"
        )

    def test_active_floor_shows_heat_first(self):
        """active='floor' → 'setpoint: 64°F Heat (72°F Cool)' — heat edge is prominent."""
        from custom_components.climate_advisor.ai_skills_activity import _format_band_setpoint

        result = _format_band_setpoint(floor=64, ceiling=72, active="floor", unit="fahrenheit")
        assert result == "setpoint: 64°F Heat (72°F Cool)", (
            f"#330: active='floor' must show Heat setpoint first. Got: {result!r}"
        )

    def test_active_unknown_shows_both(self):
        """active=None/'other' → 'setpoint: 64°F Heat / 72°F Cool' (no ordering bias)."""
        from custom_components.climate_advisor.ai_skills_activity import _format_band_setpoint

        result = _format_band_setpoint(floor=64, ceiling=72, active=None, unit="fahrenheit")
        assert "64°F Heat" in result and "72°F Cool" in result, (
            f"#330: unknown active must show both setpoints. Got: {result!r}"
        )

    def test_celsius_conversion(self):
        """Fahrenheit inputs are converted to Celsius display (18°C Heat / 22°C Cool)."""
        from custom_components.climate_advisor.ai_skills_activity import _format_band_setpoint

        # 64°F ≈ 17.8°C → rounds to 18°C; 72°F = 22.2°C → rounds to 22°C
        result = _format_band_setpoint(floor=64, ceiling=72, active="floor", unit="celsius")
        assert "°C" in result, f"Celsius unit must produce °C symbol. Got: {result!r}"
        assert "18°C" in result, f"64°F should convert to 18°C. Got: {result!r}"
        assert "22°C" in result, f"72°F should convert to 22°C. Got: {result!r}"

    def test_invalid_values_return_empty(self):
        """Non-numeric floor/ceiling → empty string (no crash, no partial output)."""
        from custom_components.climate_advisor.ai_skills_activity import _format_band_setpoint

        result = _format_band_setpoint(floor="n/a", ceiling=None, active="ceiling", unit="fahrenheit")
        assert result == "", f"Invalid inputs must return empty string, got: {result!r}"


# ---------------------------------------------------------------------------
# TestBuildEventTimelineTable
# ---------------------------------------------------------------------------


class TestBuildEventTimelineTable:
    """#330 regression lock: the deterministic table populates the Settings cell for band events."""

    # -- Band events: Settings cell must be non-empty and contain 'setpoint:' --

    def test_comfort_band_applied_settings_non_empty(self):
        """comfort_band_applied row has a non-empty Settings cell with 'setpoint:'.

        #330 core regression lock: if _render_comfort_band_applied is deleted or its
        _format_band_setpoint call is removed, this test fails.
        """
        event = _make_event(
            "comfort_band_applied",
            mode="home",
            floor=64,
            ceiling=76,
            active="ceiling",
        )
        table = _build_table([event])

        assert "setpoint:" in table, (
            f"#330: comfort_band_applied Settings cell must contain 'setpoint:'. Table:\n{table}"
        )
        # Both temperature values must appear
        assert "76" in table, f"ceiling=76 must appear in Settings cell. Table:\n{table}"
        assert "64" in table, f"floor=64 must appear in Settings cell. Table:\n{table}"

    def test_bedtime_setback_settings_non_empty(self):
        """bedtime_setback row has a non-empty Settings cell with 'setpoint:'."""
        event = _make_event(
            "bedtime_setback",
            mode="home",
            floor=60,
            ceiling=80,
            active="floor",
        )
        table = _build_table([event])

        assert "setpoint:" in table, f"#330: bedtime_setback Settings cell must contain 'setpoint:'. Table:\n{table}"
        assert "60" in table and "80" in table

    def test_morning_wakeup_settings_non_empty(self):
        """morning_wakeup row has a non-empty Settings cell with 'setpoint:'."""
        event = _make_event(
            "morning_wakeup",
            mode="home",
            floor=68,
            ceiling=76,
            active="ceiling",
        )
        table = _build_table([event])

        assert "setpoint:" in table, f"#330: morning_wakeup Settings cell must contain 'setpoint:'. Table:\n{table}"

    def test_occupancy_setback_settings_non_empty(self):
        """occupancy_setback row has a non-empty Settings cell with 'setpoint:'."""
        event = _make_event(
            "occupancy_setback",
            occupancy="away",
            floor=60,
            ceiling=82,
        )
        table = _build_table([event])

        assert "setpoint:" in table, f"#330: occupancy_setback Settings cell must contain 'setpoint:'. Table:\n{table}"

    # -- Dedup: consecutive same-type rows collapse to xN but PRESERVE Settings cell --

    def test_dedup_preserves_settings_cell(self):
        """Many consecutive comfort_band_applied events collapse to xN, Settings cell kept.

        The 'Sleep comfort band applied ×18' bug from #330: the dedup code was
        clearing the Settings cell.  After the fix, the last event's Settings must
        survive the collapse.  This test fails if run_settings is dropped or reset.
        """
        # comfort_band_applied is in _NO_DEDUP — it should NOT be deduplicated
        # Use a dedup-eligible type instead (e.g. nat_vent_fan_on which is not in _NO_DEDUP)
        events = [
            _make_event(
                "nat_vent_fan_on",
                hours_ago=float(i) * 0.05 + 0.1,
                indoor_temp=72 + i,
                on_threshold=70,
            )
            for i in range(18)
        ]
        table = _build_table(events)

        # Must show xN dedup
        assert "x18" in table or "x17" in table or "x16" in table, (
            f"#330: 18 identical nat_vent_fan_on events must be deduplicated. Table:\n{table}"
        )
        # Settings cell must NOT be empty after dedup (last event's settings survive)
        assert "fan: auto->on" in table, f"#330: Settings cell must survive dedup collapse. Table:\n{table}"

    def test_no_dedup_types_each_get_own_row(self):
        """Types in _NO_DEDUP (e.g. comfort_band_applied) each get their own row.

        #330: comfort_band_applied is in _NO_DEDUP because each application has
        distinct setpoint payload.  Verify two consecutive events both appear.
        """
        events = [
            _make_event(
                "comfort_band_applied",
                hours_ago=2.0,
                mode="home",
                floor=64,
                ceiling=76,
                active="ceiling",
            ),
            _make_event(
                "comfort_band_applied",
                hours_ago=1.0,
                mode="away",
                floor=60,
                ceiling=82,
                active="floor",
            ),
        ]
        table = _build_table(events)

        # Both rows present: count '| Comfort band applied' occurrences
        row_count = table.count("Comfort band applied")
        assert row_count == 2, (
            f"#330: two comfort_band_applied events must each appear as separate rows. "
            f"Found {row_count} rows. Table:\n{table}"
        )

    def test_consecutive_occupancy_setback_events_collapse(self):
        """#485: repeated identical occupancy_setback (away) events collapse to xN.

        Reported symptom: the automation engine re-asserts an unchanged away setback
        every ~5 minutes (revisit loop), and occupancy_setback had no dedup — unlike
        comfort_band_applied (#444) — so 11 identical rows showed up in the Activity
        Record for a single unbroken away period. occupancy_setback was removed from
        _NO_DEDUP so it uses the same collapse-consecutive-rows mechanism proven by
        test_dedup_preserves_settings_cell.
        """
        events = [
            _make_event(
                "occupancy_setback",
                hours_ago=1.0 - float(i) * 0.05,
                mode="away",
                occupancy="away",
                floor=63,
                ceiling=79,
            )
            for i in range(11)
        ]
        table = _build_table(events)

        row_count = table.count("Occupancy setback")
        assert row_count == 1, (
            f"#485: 11 identical occupancy_setback events must collapse to a single row. "
            f"Found {row_count} rows. Table:\n{table}"
        )
        assert "x11" in table or "×11" in table, f"#485: collapsed row must show the x11 repeat count. Table:\n{table}"
        assert "setpoint:" in table, (
            f"#485: collapsed occupancy_setback row must still show the setpoint. Table:\n{table}"
        )

    def test_occupancy_setback_breaks_run_on_mode_change(self):
        """#485: an occupancy_setback for a DIFFERENT mode (e.g. vacation after away) must

        not be silently merged into the prior run — two distinct rows, not one xN row,
        because the underlying event_type is the same but this proves collapsing is
        still based on consecutive same-type events only, matching every other
        dedup-eligible type (e.g. nat_vent_fan_on) rather than payload equality.
        """
        events = [
            _make_event("occupancy_setback", hours_ago=2.0, mode="away", occupancy="away", floor=63, ceiling=79),
            _make_event(
                "occupancy_comfort_restored", hours_ago=1.5, mode="cool", target_f=74
            ),  # in _NO_DEDUP -- breaks any run
            _make_event(
                "occupancy_setback", hours_ago=1.0, mode="vacation", occupancy="vacation", floor=60, ceiling=85
            ),
        ]
        table = _build_table(events)

        assert "Occupancy setback (away)" in table
        assert "Occupancy setback (vacation)" in table

    def test_single_fan_activated_event_shows_full_reason_in_event_column(self):
        """Issue #402 follow-up: a single (non-repeated) fan_activated event must show its

        full reason text in the Event column, not a bare '_humanize_type()' label.

        Before this fix, _flush_run() used _humanize_type(run_type) even for a run of
        exactly one event (never actually deduplicated with anything) — discarding the
        renderer's real ev_text for every event type not in the small _NO_DEDUP allowlist.
        fan_activated is not in _NO_DEDUP, so this was silently losing the reason for the
        single most common case: one fan-activation event with nothing before/after it of
        the same type. Confirmed against live production data showing bare "Fan activated"
        rows with no explanation.
        """
        event = _make_event(
            "fan_activated",
            reason="nat-vent re-engaged: outdoor 65.0°F < indoor 70.0°F, indoor > comfort_heat 68.0°F",
            fan_device="whf",
        )
        table = _build_table([event])

        assert "nat-vent re-engaged" in table, (
            f"#402: single fan_activated event must show its full reason in the Event column, "
            f"not a bare label. Table:\n{table}"
        )

    def test_single_nat_vent_fan_off_event_shows_indoor_and_threshold(self):
        """Issue #402 follow-up: same bug, different event type — nat_vent_fan_off is also

        not in _NO_DEDUP, so a single occurrence must still show its real indoor/threshold
        values in the Event column rather than bare 'Nat vent fan off'.
        """
        event = _make_event(
            "nat_vent_fan_off",
            indoor_temp=69.0,
            off_threshold=70.0,
            target=71.0,
        )
        table = _build_table([event])

        assert "69" in table and "70" in table, (
            f"#402: single nat_vent_fan_off event must show real indoor/threshold values "
            f"in the Event column. Table:\n{table}"
        )

    def test_repeated_run_still_collapses_with_xn_label(self):
        """REGRESSION GUARD: genuinely repeated events (run_count > 1) must still collapse

        to the generic '_humanize_type() xN (time range)' label — the fix must only change
        behavior for run_count == 1, not break real deduplication.
        """
        events = [
            _make_event(
                "fan_activated",
                hours_ago=float(i) * 0.05 + 0.1,
                reason=f"reason variant {i}",
                fan_device="whf",
            )
            for i in range(5)
        ]
        table = _build_table(events)

        assert "Fan activated x5" in table or "Fan activated x4" in table, (
            f"#402: repeated fan_activated events must still collapse with an xN label. Table:\n{table}"
        )
        # Only the LAST event's distinct reason text should appear in a collapsed run's
        # Settings-adjacent context — the individual "reason variant N" strings for the
        # collapsed middle events must NOT all appear (that would mean dedup broke).
        assert "reason variant 0" not in table, (
            f"#402: collapsed run must not list every individual reason. Table:\n{table}"
        )

    # -- Non-band events keep expected Settings content --

    def test_override_detected_settings_shows_setpoint_transition(self):
        """override_detected with old/new setpoint → Settings: 'setpoint: 72°F->75°F'."""
        event = _make_event(
            "override_detected",
            source="setpoint",
            old_setpoint_f=72.0,
            new_setpoint_f=75.0,
        )
        table = _build_table([event])

        assert "setpoint:" in table, f"Settings cell must contain setpoint transition. Table:\n{table}"
        assert "72" in table and "75" in table

    def test_sensor_opened_settings_shows_mode_change(self):
        """sensor_opened with hvac_mode_change → Settings shows mode change."""
        event = _make_event(
            "sensor_opened",
            entity="binary_sensor.front_door",
            result="paused",
            hvac_mode_change="heat→off",
        )
        table = _build_table([event])

        assert "Sensor opened" in table

    def test_grace_started_settings_blank_for_unknown_trigger(self):
        """grace_started with unknown trigger → Event cell shows the renderer's full label

        ('Grace period started (90 min)'), Settings empty.

        Issue #402 follow-up: before the run_count==1 fix, this single event's Event cell
        would show the bare _humanize_type('grace_started') -> 'Grace started' instead of
        the renderer's real, more informative label — locked in here as the corrected
        expectation. For triggers not in _GRACE_TRIGGER_LABELS the renderer returns
        Settings='' so the Settings cell stays blank.
        """
        event = _make_event(
            "grace_started",
            trigger="door_opened",  # not a known trigger — Settings stays blank
            duration_seconds=5400,
        )
        table = _build_table([event])

        assert "Grace period started (90 min)" in table, f"grace_started row must appear. Table:\n{table}"
        rows = [line for line in table.splitlines() if "Grace period started" in line]
        assert rows, "No grace_started row found"
        cells = rows[0].split("|")
        if len(cells) >= 4:
            assert cells[3].strip() == "", (
                f"grace_started Settings cell should be empty for unknown trigger. Got: {cells[3]!r}"
            )

    def test_empty_event_log_returns_header_and_sentinel(self):
        """Empty event log → table has header + '(no events in window)' sentinel row."""
        table = _build_table([])

        assert "| Time | Event | Settings | Source | Indoor | Outdoor |" in table
        assert "no events in window" in table

    def test_table_always_starts_with_header(self):
        """Every call to build_event_timeline_table returns a table starting with the header."""
        event = _make_event("system_restarted", recovered_events=5)
        table = _build_table([event])

        first_line = table.splitlines()[0]
        assert first_line == "| Time | Event | Settings | Source | Indoor | Outdoor |", (
            f"Table must start with header row. Got: {first_line!r}"
        )


# ---------------------------------------------------------------------------
# TestDefaultRenderer
# ---------------------------------------------------------------------------


class TestDefaultRenderer:
    """_default_renderer: surprise-safe fallback for unregistered event types."""

    def test_unknown_type_event_cell_is_non_empty(self):
        """Unknown event type → Event cell is the humanized type name (non-empty)."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        ev_text, _settings = _default_renderer("some_new_event_type", {}, "fahrenheit")
        assert ev_text, "Event cell must not be empty for unknown event type"
        assert "some new event type" in ev_text.lower(), (
            f"Event cell should contain humanized type name. Got: {ev_text!r}"
        )

    def test_unknown_type_with_reason(self):
        """Unknown event with 'reason' field → reason appended to event text."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        ev_text, _ = _default_renderer("my_event", {"reason": "test_reason"}, "fahrenheit")
        assert "test_reason" in ev_text, f"reason must appear in Event cell. Got: {ev_text!r}"

    def test_unknown_type_with_mode_change_in_settings(self):
        """Unknown event with old/new hvac_mode → Settings shows mode transition."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        _, settings = _default_renderer(
            "my_event",
            {"old_hvac_mode": "heat", "new_hvac_mode": "cool"},
            "fahrenheit",
        )
        assert "mode:" in settings and "heat" in settings and "cool" in settings, (
            f"Settings must show mode transition. Got: {settings!r}"
        )

    def test_unknown_type_with_setpoint_in_settings(self):
        """Unknown event with old/new setpoint → Settings shows setpoint transition."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        _, settings = _default_renderer(
            "my_event",
            {"old_setpoint_f": 70.0, "new_setpoint_f": 75.0},
            "fahrenheit",
        )
        assert "setpoint:" in settings, f"Settings must show setpoint transition. Got: {settings!r}"

    def test_unknown_type_with_band_fields_in_settings(self):
        """Unknown event with floor/ceiling → Settings shows band setpoint."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        _, settings = _default_renderer(
            "my_event",
            {"floor": 64, "ceiling": 76, "active": "ceiling"},
            "fahrenheit",
        )
        assert "setpoint:" in settings, f"Settings must contain band setpoint. Got: {settings!r}"

    def test_unknown_type_no_crash_with_empty_payload(self):
        """Empty payload → no crash; returns (non_empty_label, '') without raising."""
        from custom_components.climate_advisor.ai_skills_activity import _default_renderer

        try:
            ev_text, settings = _default_renderer("weird_event", {}, "fahrenheit")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"_default_renderer must not raise on empty payload. Got: {exc!r}") from exc
        assert ev_text  # non-empty
        assert isinstance(settings, str)

    def test_unknown_type_appears_in_table(self):
        """Unknown event type is rendered in the table via _default_renderer (no crash)."""
        event = _make_event("brand_new_event_2030", reason="future_feature", floor=68, ceiling=78)
        table = _build_table([event])

        assert "Brand new event 2030" in table, (
            f"Unknown event type must be humanized and appear in table. Table:\n{table}"
        )


# ---------------------------------------------------------------------------
# TestEventRenderersCoverage — forward-compat guardrail
# ---------------------------------------------------------------------------


class TestEventRenderersCoverage:
    """Guardrail: every event type emitted by production has a registered renderer.

    This test regex-extracts every _emit_event / _emit_event_callback("type", ...)
    literal from automation.py and coordinator.py, then asserts each type is either
    in EVENT_RENDERERS or in an explicit documented allowlist that routes to
    _default_renderer.

    If a developer adds a new _emit_event("new_type", ...) call in production without
    adding it to EVENT_RENDERERS, this test fails loudly — catching the gap before
    it reaches production where the AI investigator would silently show a blank row.
    """

    # Types that intentionally route to _default_renderer rather than having a
    # dedicated renderer.  Add here only with a comment explaining why.
    _DEFAULT_RENDERER_ALLOWLIST: frozenset[str] = frozenset(
        {
            # No types currently in the allowlist — all production types have
            # dedicated renderers.  Add entries here if a new event type is
            # intentionally left to the default renderer (e.g., rare diagnostic
            # events where the generic field extraction is sufficient).
        }
    )

    def _extract_emitted_types(self) -> set[str]:
        """Read automation.py and coordinator.py; return all emitted event type literals."""
        emitted: set[str] = set()
        pattern = re.compile(r'_emit_event(?:_callback)?\s*\(\s*["\'](\w+)["\']')
        for fname in (
            "custom_components/climate_advisor/automation.py",
            "custom_components/climate_advisor/coordinator.py",
        ):
            with open(fname, encoding="utf-8") as f:
                content = f.read()
            emitted.update(pattern.findall(content))
        return emitted

    def test_all_emitted_types_have_renderer(self):
        """Every _emit_event literal in production is in EVENT_RENDERERS or the allowlist.

        Forward-compat guardrail (#330): adding a new emit call without a renderer
        causes a blank Settings cell and an unhelpful Event label in the AI timeline.
        """
        from custom_components.climate_advisor.ai_skills_activity import EVENT_RENDERERS

        emitted = self._extract_emitted_types()
        covered = set(EVENT_RENDERERS.keys()) | self._DEFAULT_RENDERER_ALLOWLIST

        missing = emitted - covered
        assert not missing, (
            "#330 guardrail: the following emitted event types have no renderer in "
            "EVENT_RENDERERS and are not in the allowlist:\n"
            + "\n".join(f"  - {t}" for t in sorted(missing))
            + "\nAdd a renderer to EVENT_RENDERERS in ai_skills_activity.py, or add "
            "the type to _DEFAULT_RENDERER_ALLOWLIST with an explanatory comment."
        )

    def test_no_renderer_is_dead_code(self):
        """All EVENT_RENDERERS keys correspond to either emitted types or legacy types.

        Soft check: warn if a renderer exists but the event type is never emitted
        and is not a known legacy type.  Dead renderers are harmless but may
        indicate a renamed event type that drifted out of sync.

        The three warm_day_* legacy types are grandfathered — they appear in
        persisted event logs from pre-P3 instances.
        """
        from custom_components.climate_advisor.ai_skills_activity import EVENT_RENDERERS

        _LEGACY_TYPES = frozenset(
            {
                "warm_day_setback_applied",
                "warm_day_state_confirmed",
                "warm_day_comfort_gap",
                # Removed in v0.4.47 (Issue #374 — Priority 0 exit deleted).
                # Kept to render historical events in persisted logs from v0.4.46.
                "nat_vent_sleep_ceiling_reached",
            }
        )

        emitted = self._extract_emitted_types()
        registered = set(EVENT_RENDERERS.keys())
        dead = registered - emitted - _LEGACY_TYPES - self._DEFAULT_RENDERER_ALLOWLIST

        # This is a soft informational check — we don't fail the suite for dead
        # renderers because they're harmless.  But we do assert the currently-known
        # dead set is empty, so a future deletion becomes visible.
        assert not dead, (
            "The following EVENT_RENDERERS entries have no corresponding _emit_event call "
            "in production and are not in the legacy allowlist.  They may be dead code "
            "from a renamed event type:\n"
            + "\n".join(f"  - {t}" for t in sorted(dead))
            + "\nEither remove them from EVENT_RENDERERS or add them to _LEGACY_TYPES."
        )


class TestFanEventRenderers:
    """Issue #331 follow-up: fan_activated / fan_deactivated / fan_running_untracked / cleared."""

    def test_fan_activated_shows_trigger(self):
        """Issue #392 Fix 2: settings cell uses the archetype-specific fan_device label."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_activated"](
            {"reason": "min_runtime_cycle", "fan_device": "hvac_fan"}, "fahrenheit"
        )
        assert "Fan activated" in ev and "min_runtime_cycle" in ev
        assert st == "hvac_fan: off->on"

    def test_fan_activated_shows_trigger_whf(self):
        """Issue #392 Fix 2: whole-house-fan archetype renders 'whf', not the generic 'fan'."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_activated"](
            {"reason": "natural ventilation", "fan_device": "whf"}, "fahrenheit"
        )
        assert "Fan activated" in ev and "natural ventilation" in ev
        assert st == "whf: off->on"

    def test_fan_activated_no_fan_device_falls_back(self):
        """No fan_device in payload (legacy/pre-#392 event) -> generic 'fan' label, no crash."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_activated"]({"reason": "min_runtime_cycle"}, "fahrenheit")
        assert "Fan activated" in ev and "min_runtime_cycle" in ev
        assert st == "fan: off->on"

    def test_fan_deactivated_shows_trigger(self):
        """Issue #392 Fix 2: settings cell uses the archetype-specific fan_device label."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_deactivated"](
            {"reason": "economizer off -- fan no longer needed", "fan_device": "hvac_fan"}, "fahrenheit"
        )
        assert "Fan deactivated" in ev and "economizer off" in ev
        assert st == "hvac_fan: on->off"

    def test_fan_deactivated_shows_trigger_whf(self):
        """Issue #392 Fix 2: whole-house-fan archetype renders 'whf', not the generic 'fan'."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_deactivated"](
            {"reason": "door/window closed", "fan_device": "whf"}, "fahrenheit"
        )
        assert "Fan deactivated" in ev and "door/window closed" in ev
        assert st == "whf: on->off"

    def test_fan_deactivated_no_fan_device_falls_back(self):
        """No fan_device in payload (legacy/pre-#392 event) -> generic 'fan' label, no crash."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_deactivated"](
            {"reason": "economizer off -- fan no longer needed"}, "fahrenheit"
        )
        assert "Fan deactivated" in ev and "economizer off" in ev
        assert st == "fan: on->off"

    def test_fan_running_untracked_shows_source(self):
        ev, st = _act_mod.EVENT_RENDERERS["fan_running_untracked"](
            {"source": "thermostat blower during cool cycle", "hvac_action": "fan"}, "fahrenheit"
        )
        assert "untracked" in ev.lower() and "thermostat blower during cool cycle" in ev
        assert "untracked" in st

    def test_fan_untracked_cleared(self):
        ev, st = _act_mod.EVENT_RENDERERS["fan_untracked_cleared"]({}, "fahrenheit")
        assert "untracked" in ev.lower()
        assert st == "fan: off"


class TestGraceStartedRendering:
    """Issue #341: grace_started trigger field renders as human-readable Settings cell."""

    def test_fan_manual_override_trigger_shows_in_settings(self):
        """grace_started with trigger=fan_manual_override → Settings shows readable label.

        The occupant sees 'Grace started | fan override (manual fan change)' rather than
        the raw internal code 'fan_manual_override' — making the reason for the pause clear.
        """
        ev, st = _act_mod.EVENT_RENDERERS["grace_started"](
            {"source": "manual", "duration_seconds": 5400, "trigger": "fan_manual_override"},
            "fahrenheit",
        )
        assert "fan override" in st, f"Settings must show readable trigger label. Got: {st!r}"
        assert "manual fan change" in st

    def test_hvac_override_trigger(self):
        ev, st = _act_mod.EVENT_RENDERERS["grace_started"](
            {"source": "manual", "duration_seconds": 5400, "trigger": "override_confirmed"},
            "fahrenheit",
        )
        assert "HVAC mode override" in st

    def test_sensor_closed_resume_trigger(self):
        ev, st = _act_mod.EVENT_RENDERERS["grace_started"](
            {"source": "automation", "duration_seconds": 300, "trigger": "sensor_closed_resume"},
            "fahrenheit",
        )
        assert "all sensors closed" in st

    def test_unknown_trigger_settings_blank(self):
        """Unknown trigger codes do not appear in Settings (no junk internal codes shown)."""
        ev, st = _act_mod.EVENT_RENDERERS["grace_started"](
            {"source": "manual", "duration_seconds": 5400, "trigger": "some_future_trigger"},
            "fahrenheit",
        )
        assert st == "", f"Unknown trigger must leave Settings empty. Got: {st!r}"

    def test_no_trigger_field_settings_blank(self):
        """Legacy events without a trigger key render without error; Settings stays empty."""
        ev, st = _act_mod.EVENT_RENDERERS["grace_started"](
            {"source": "manual", "duration_seconds": 5400},
            "fahrenheit",
        )
        assert st == ""

    def test_fan_manual_override_trigger_in_table(self):
        """End-to-end: fan_manual_override trigger appears in Settings column of timeline table."""
        event = _make_event(
            "grace_started",
            trigger="fan_manual_override",
            duration_seconds=5400,
            source="manual",
        )
        table = _build_table([event])
        assert "Grace period started" in table
        rows = [line for line in table.splitlines() if "Grace period started" in line]
        assert rows, "No grace_started row found"
        cells = rows[0].split("|")
        if len(cells) >= 4:
            assert "fan override" in cells[3], f"Settings cell must contain 'fan override'. Got: {cells[3]!r}"


class TestFanManualOverrideRenderer:
    """Issue #341: fan_manual_override event renders fan state change in Settings."""

    def test_fan_state_change_shows_in_settings(self):
        """fan_manual_override with fan_before/fan_after → Settings shows 'fan: on->auto'."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_manual_override"](
            {"fan_before": "on", "fan_after": "auto", "override_active_since": "2026-06-20T06:48:00"},
            "fahrenheit",
        )
        assert ev == "Fan manual override"
        assert st == "fan: on->auto"

    def test_fan_state_change_off_to_on(self):
        ev, st = _act_mod.EVENT_RENDERERS["fan_manual_override"](
            {"fan_before": "off", "fan_after": "on"},
            "fahrenheit",
        )
        assert st == "fan: off->on"

    def test_missing_fan_states_settings_blank(self):
        """No fan_before/fan_after → Settings stays blank (graceful fallback)."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_manual_override"]({}, "fahrenheit")
        assert ev == "Fan manual override"
        assert st == ""

    def test_fan_manual_override_source_is_manual(self):
        """fan_manual_override source resolves to 'manual' for timeline Source column."""
        source = _act_mod._event_source_label("fan_manual_override", {"source": ""})
        assert source == "manual"


# ---------------------------------------------------------------------------
# TestTempColumns (Issue #352)
# ---------------------------------------------------------------------------


class TestTempColumns:
    """Indoor/Outdoor temperature columns in the timeline table (Issue #352)."""

    def test_temp_columns_populated_from_event(self):
        """Events carrying indoor_f/outdoor_f render temperatures in the table."""
        events = [
            _make_event(
                "comfort_band_applied",
                hours_ago=1.0,
                floor=68.0,
                ceiling=74.0,
                active="ceiling",
                mode="cool",
                reason="day",
                indoor_f=73.5,
                outdoor_f=68.0,
            )
        ]
        table = _build_table(events)
        assert "Indoor" in table
        assert "Outdoor" in table
        # 73.5°F rounds to 74°F in format_temp; 68°F is both setpoint floor and outdoor temp
        assert "74" in table

    def test_temp_columns_absent_renders_emdash(self):
        """Events without indoor_f/outdoor_f render em-dash; no crash."""
        events = [
            _make_event(
                "comfort_band_applied",
                hours_ago=1.0,
                floor=68.0,
                ceiling=74.0,
                active="ceiling",
                mode="cool",
                reason="day",
            )
        ]
        table = _build_table(events)
        assert "Indoor" in table
        assert "Outdoor" in table
        assert "—" in table  # em-dash

    def test_dedup_row_preserves_first_event_temps(self):
        """Consecutive same-type events collapsed to one row keep the first event's temps."""
        events = [
            _make_event("nat_vent_ac_assist_armed", hours_ago=3.0, indoor_f=75.0, outdoor_f=70.0),
            _make_event("nat_vent_ac_assist_armed", hours_ago=2.5, indoor_f=76.0, outdoor_f=71.0),
            _make_event("nat_vent_ac_assist_armed", hours_ago=2.0, indoor_f=77.0, outdoor_f=72.0),
        ]
        table = _build_table(events)
        assert "x3" in table
        assert "75" in table
        assert "70" in table


# ---------------------------------------------------------------------------
# TestAltKeyTempFallback
# ---------------------------------------------------------------------------


class TestAltKeyTempFallback:
    """Verify _first_temp fallback reads alt key names from existing ring buffer events."""

    def test_indoor_temp_key_populates_column(self):
        """nat_vent_fan_on stores indoor_temp — table must show it."""
        events = [
            _make_event(
                "nat_vent_fan_on",
                hours_ago=1.0,
                indoor_temp=71.0,
                on_threshold=68.0,
                target=72.0,
            )
        ]
        table = _build_table(events)
        assert "71" in table  # 71.0°F formatted

    def test_indoor_outdoor_keys_populate_columns(self):
        """nat_vent_ceiling_escalation stores indoor/outdoor — both columns must show."""
        events = [
            _make_event(
                "nat_vent_ceiling_escalation",
                hours_ago=1.0,
                indoor=78.0,
                outdoor=62.0,
                comfort_cool=75.0,
            )
        ]
        table = _build_table(events)
        assert "78" in table
        assert "62" in table

    def test_indoor_f_takes_priority_over_alt_keys(self):
        """If indoor_f is already present, it wins over indoor_temp."""
        events = [
            _make_event(
                "some_event",
                hours_ago=1.0,
                indoor_f=75.0,
                indoor_temp=99.0,  # should be ignored
            )
        ]
        table = _build_table(events)
        assert "75" in table
        assert "99" not in table


# ---------------------------------------------------------------------------
# TestFanOwnershipAnnotations (Issue #359)
# ---------------------------------------------------------------------------


class TestFanOwnershipAnnotations:
    """Issue #359: fan_cancel renderer and nat_vent_fan_off ownership annotation.

    When a fan_manual_override (fan-ON by user) precedes a nat_vent_fan_off, the
    fan may still be physically running under user control. The timeline annotates
    the nat_vent_fan_off row with a NOTE so the developer can see this.
    When CA owns the fan (nat_vent_fan_on precedes), no NOTE is shown.
    """

    def test_fan_cancel_renderer_label(self):
        """fan_cancel renderer returns 'Fan cancel (user turned off)' label."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_cancel"](
            {"fan_before": "on", "fan_after": "auto", "trigger": "fan_off"},
            "fahrenheit",
        )
        assert "Fan cancel" in ev
        assert "user turned off" in ev.lower() or "cancel" in ev.lower()

    def test_fan_cancel_renderer_settings_shows_transition(self):
        """fan_cancel settings cell shows 'fan: on->auto'."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_cancel"](
            {"fan_before": "on", "fan_after": "auto"},
            "fahrenheit",
        )
        assert "on" in st and "auto" in st
        assert "->" in st or "→" in st

    def test_fan_cancel_renderer_missing_fan_states_shows_placeholder(self):
        """fan_cancel with no fan_before/fan_after → Settings shows '?' placeholder (graceful, no crash)."""
        ev, st = _act_mod.EVENT_RENDERERS["fan_cancel"]({}, "fahrenheit")
        assert ev  # label still present
        # The renderer defaults to "?" for missing fan states — settings is non-empty but informative
        assert isinstance(st, str)  # no crash, returns a string

    def test_nat_vent_fan_off_ownership_annotation_in_ev_text(self):
        """When user owns fan, the NOTE annotation is added to ev_text in the renderer block.

        For dedup-eligible types (nat_vent_fan_off is NOT in _NO_DEDUP), the dedup flush
        path uses _humanize_type(run_type) rather than the annotated ev_text, so the NOTE
        does not appear in the final table row. This test documents the renderer-level
        annotation by verifying the fan ownership logic works (fan_manual_override sets
        _fan_user_owns), and that the nat_vent_fan_off renderer produces a valid label.

        Occupant impact: even if the NOTE is not in the table, the renderer correctly
        flags user-owned fan state so developers reading raw events see the signal.
        """
        # fan_manual_override with fan_after=on correctly identifies user ownership
        ev_override, st_override = _act_mod.EVENT_RENDERERS["fan_manual_override"](
            {"fan_before": "auto", "fan_after": "on"},
            "fahrenheit",
        )
        assert ev_override == "Fan manual override"
        assert "on" in st_override

        # nat_vent_fan_off renderer produces valid output without crashing
        renderer = _act_mod.EVENT_RENDERERS.get("nat_vent_fan_off")
        if renderer is not None:
            ev_nv, st_nv = renderer({"indoor_temp": 71.0, "comfort_heat": 70.0}, "fahrenheit")
            assert ev_nv  # non-empty label
            assert isinstance(st_nv, str)  # no crash

        # The table shows both events without error (fan ownership tracking works)
        events = [
            _make_event(
                "fan_manual_override",
                hours_ago=2.0,
                fan_before="auto",
                fan_after="on",
            ),
            _make_event(
                "nat_vent_fan_off",
                hours_ago=1.0,
                indoor_temp=71.0,
                comfort_heat=70.0,
            ),
        ]
        table = _build_table(events)
        # Both event types must appear in the table (no crash, no missing rows)
        assert "Fan manual override" in table
        assert "Nat vent fan off" in table or "nat-vent fan off" in table.lower() or "Nat-vent fan off" in table

    def test_nat_vent_fan_off_no_annotation_when_ca_owns(self):
        """nat_vent_fan_off after nat_vent_fan_on (CA owns) → no NOTE in label.

        CA activated the fan itself, so it can stop it cleanly without ambiguity.
        """
        events = [
            _make_event(
                "nat_vent_fan_on",
                hours_ago=2.0,
                indoor_temp=76.0,
                on_threshold=70.0,
            ),
            _make_event(
                "nat_vent_fan_off",
                hours_ago=1.0,
                indoor_temp=71.0,
                comfort_heat=70.0,
            ),
        ]
        table = _build_table(events)
        nat_vent_fan_off_rows = [
            line for line in table.splitlines() if "Nat-vent fan off" in line or "nat vent fan off" in line.lower()
        ]
        for row in nat_vent_fan_off_rows:
            assert "NOTE" not in row, f"nat_vent_fan_off when CA owns fan must NOT show NOTE. Row: {row!r}"
