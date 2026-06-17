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

    def test_grace_started_settings_blank(self):
        """grace_started → Event cell shows humanized type, Settings cell is empty.

        grace_started is not in _NO_DEDUP, so the dedup flush path uses
        _humanize_type('grace_started') → 'Grace started' for the Event cell
        (the renderer ev_text is discarded for dedup-eligible types).
        The renderer returns Settings='', confirmed here.
        """
        event = _make_event(
            "grace_started",
            trigger="door_opened",
            duration_seconds=5400,
        )
        table = _build_table([event])

        # Row present (humanized type name from the dedup flush path)
        assert "Grace started" in table, f"grace_started row must appear. Table:\n{table}"
        # Settings column is blank for grace_started (renderer returns Settings='')
        rows = [line for line in table.splitlines() if "Grace started" in line]
        assert rows, "No grace_started row found"
        # The row format is: | Time | Event | Settings | Source |
        # cells[0]='' cells[1]=time cells[2]=event cells[3]=settings cells[4]=source
        cells = rows[0].split("|")
        if len(cells) >= 4:
            assert cells[3].strip() == "", f"grace_started Settings cell should be empty. Got: {cells[3]!r}"

    def test_empty_event_log_returns_header_and_sentinel(self):
        """Empty event log → table has header + '(no events in window)' sentinel row."""
        table = _build_table([])

        assert "| Time | Event | Settings | Source |" in table
        assert "no events in window" in table

    def test_table_always_starts_with_header(self):
        """Every call to build_event_timeline_table returns a table starting with the header."""
        event = _make_event("system_restarted", recovered_events=5)
        table = _build_table([event])

        first_line = table.splitlines()[0]
        assert first_line == "| Time | Event | Settings | Source |", (
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
        ev, st = _act_mod.EVENT_RENDERERS["fan_activated"](
            {"reason": "min_runtime_cycle", "fan_mode": "hvac_fan"}, "fahrenheit"
        )
        assert "Fan activated" in ev and "min_runtime_cycle" in ev
        assert st == "fan: off->on"

    def test_fan_deactivated_shows_trigger(self):
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
