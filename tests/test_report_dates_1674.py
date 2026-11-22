"""Issue #1674: every multi-day section of the AI Investigator context names the day of each time.

Occupant experience: David reads a 7-day AI Investigator report and sees "1:34 AM", "6:15 AM", "10:46 AM to
11:21 AM" with no weekday or date, so events from different days blur together. The context printed bare ``HH:MM``
in ACTIVITY TIMELINE, ACTIVITY SESSIONS, SYSTEM LOG RECORDS, TIMING CORRELATIONS, KNOWN OVERRIDE FALSE POSITIVES,
FAN OWNERSHIP HISTORY and the current-override line, and the AI copied those times. Every test here fails if a
section drops the day again.

The stamp is one compact style: ``Sat 11-21 10:31`` (weekday, month-day, 24-hour local time). Tests run in a
Pacific zone (the install the report came from) so a UTC-printing regression shows up as a wrong hour/day.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import custom_components.climate_advisor.ai_skills_context as ctx
import custom_components.climate_advisor.ai_skills_investigator as inv

_LA = ZoneInfo("America/Los_Angeles")
_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_STAMP = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}-\d{2} \d{2}:\d{2}"


def _la(day: int, hour: int, minute: int, second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 11, day, hour, minute, second, tzinfo=_LA)


# Fri 11-20 and Sat 11-21 2026 (Pacific). "Now" is Sat 11-21 18:30.
_NOW = _la(21, 18, 30)


def _to_la(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(_LA)


def _local_zone():
    """dt_util.as_local -> the Pacific zone, dt_util.now -> _NOW (same module object the context uses)."""
    return (
        patch.object(ctx.dt_util, "as_local", side_effect=_to_la),
        patch.object(ctx.dt_util, "now", return_value=_NOW),
    )


def _coord(events, **extra):
    coord = MagicMock()
    coord._event_log = events
    coord.config = {"temp_unit": "fahrenheit"}
    for key, value in extra.items():
        setattr(coord, key, value)
    return coord


def _two_day_log() -> list[dict]:
    """A Friday-evening session, a run of closed-sensor events across midnight, and a Saturday-morning override."""
    return [
        {"type": "fan_activated", "time": _la(20, 19, 5).isoformat(), "reason": "natural ventilation"},
        {"type": "fan_deactivated", "time": _la(20, 19, 20).isoformat()},
        {"type": "sensor_all_closed", "time": _la(20, 23, 50).isoformat()},
        {"type": "sensor_all_closed", "time": _la(21, 0, 10).isoformat()},
        {"type": "override_detected", "time": _la(21, 6, 15).isoformat(), "source": "manual"},
    ]


class TestStampHelpers:
    def test_stamp_names_weekday_and_date_in_local_time(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            # 07:31 UTC on Sat 11-21 is 23:31 on Fri 11-20 in Pacific time: the local day, not UTC's.
            assert ctx.fmt_day_stamp(datetime.datetime(2026, 11, 21, 7, 31, tzinfo=datetime.UTC)) == "Fri 11-20 23:31"
            assert ctx.fmt_day_stamp(_la(21, 10, 31, 5), seconds=True) == "Sat 11-21 10:31:05"

    def test_span_keeps_one_date_on_one_day_and_names_the_second_day_across_midnight(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            assert ctx.fmt_day_span(_la(21, 10, 46), _la(21, 11, 21)) == "Sat 11-21 10:46-11:21"
            assert ctx.fmt_day_span(_la(21, 10, 46), _la(21, 10, 46, 30)) == "Sat 11-21 10:46"
            assert ctx.fmt_day_span(_la(20, 23, 50), _la(21, 0, 10)) == "Fri 11-20 23:50-Sat 11-21 00:10"
            assert ctx.fmt_clock_near(_la(21, 0, 10), _la(20, 23, 50)) == "Sat 11-21 00:10"

    def test_naive_time_is_read_as_utc_like_the_event_parser(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            assert ctx.fmt_day_stamp(datetime.datetime(2026, 11, 21, 18, 0)) == "Sat 11-21 10:00"

    def test_fmt_time_is_day_stamped_and_keeps_its_fallbacks(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            assert ctx._fmt_time(_la(21, 6, 15).isoformat()) == "Sat 11-21 06:15"
        assert ctx._fmt_time(None) == "??:??"
        assert ctx._fmt_time("not-a-time") == "not-a-time"


class TestActivitySections:
    def test_timeline_rows_carry_weekday_and_date(self):
        a, b = _local_zone()
        with a, b:
            text = asyncio.run(ctx.build_activity_timeline_context(None, _coord(_two_day_log()), hours=48))
        rows = [ln for ln in text.splitlines() if ln.startswith("| ") and not ln.startswith("| Time")]
        assert rows, text
        for row in rows:
            assert re.match(r"\| " + _STAMP + r" \|", row), row
        assert "| Fri 11-20 19:05 |" in text
        assert "| Sat 11-21 06:15 |" in text
        # A collapsed run crossing midnight names the second day.
        assert "(23:50-Sat 11-21 00:10)" in text

    def test_sessions_carry_weekday_and_date(self):
        a, b = _local_zone()
        with a, b:
            text = asyncio.run(ctx.build_activity_sessions_context(None, _coord(_two_day_log()), hours=48))
        heads = [ln for ln in text.splitlines() if ln.startswith("SESSION ")]
        assert heads == [
            "SESSION 1: Fri 11-20 19:05-19:20 (indoor —->—, outdoor —->—)",
            "SESSION 2: Fri 11-20 23:50-Sat 11-21 00:10 (indoor —->—, outdoor —->—)",
            "SESSION 3: Sat 11-21 06:15 (indoor —->—, outdoor —->—)",
        ]

    def test_no_ai_fallback_summary_carries_the_day(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            text = ctx.build_activity_summary_narrative(_two_day_log(), {"temp_unit": "fahrenheit"}, 48, _NOW)
        assert text.splitlines() == [
            "- Fri 11-20 19:05-19:20: 2 event(s).",
            "- Fri 11-20 23:50-Sat 11-21 00:10: 2 event(s).",
            "- Sat 11-21 06:15: 1 event(s).",
        ]

    def test_dashboard_rows_carry_the_instant_for_a_localized_date(self):
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            wire = ctx.build_event_timeline_wire(_two_day_log(), {"temp_unit": "fahrenheit"}, 48, _NOW)
        first = wire["rows"][0]
        assert first["time"] == "Fri 11-20 19:05"
        assert datetime.datetime.fromisoformat(first["ts"]) == _la(20, 19, 5)

    def test_session_with_no_readable_time_keeps_the_raw_text(self):
        events = [{"type": "fan_activated", "time": None}, {"type": "fan_deactivated", "time": None}]
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            sessions, _ = ctx._group_timeline_sessions(events, {"temp_unit": "fahrenheit"}, 48, _NOW)
        assert [s.time_range for s in sessions] == ["??:??"]

    def test_unparseable_row_time_still_renders_without_a_ts(self):
        rendered = ctx._RenderedEvent(None, "bad", "x", "X", "", "sensor", "—", "—")
        assert ctx._run_clock_range(rendered, rendered) == "bad"
        other = rendered._replace(time_str="bad2")
        assert ctx._run_clock_range(rendered, other) == "bad-bad2"


class TestEventLogSections:
    def test_timing_correlations_name_the_day(self):
        events = [
            {"type": "classification_applied", "time": _la(20, 23, 40).isoformat(), "source": "automation"},
            # 30 min after the automation event, across midnight: a matched cycle period.
            {"type": "override_detected", "time": _la(21, 0, 10).isoformat(), "source": "manual"},
            {"type": "override_detected", "time": _la(21, 6, 15).isoformat(), "source": "manual"},
        ]
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            text = ctx._build_timing_correlations(events, 48, _NOW)
        lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
        assert lines
        for line in lines:
            assert re.match(r"  \[[A-Z-]+\] " + _STAMP + " — ", line), line
        assert "[TIMING-COINCIDENT] Sat 11-21 00:10" in text
        assert "at Fri 11-20 23:40" in text  # the prior event names its own day across midnight

    def test_timing_correlation_with_no_prior_automation_names_the_day(self):
        events = [{"type": "override_detected", "time": _la(21, 6, 15).isoformat(), "source": "manual"}]
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            text = ctx._build_timing_correlations(events, 48, _NOW)
        assert "[OK] Sat 11-21 06:15 — override_detected: no prior automation event in window" in text

    def test_known_override_false_positives_name_the_day(self):
        events = [
            {"type": "nat_vent_manual_override_exit", "time": _la(20, 21, 54, 36).isoformat()},
            {"type": "override_detected", "time": _la(20, 21, 54, 36).isoformat()},
        ]
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            text = ctx._build_known_override_false_positives(events, 48, _NOW)
        assert "override_detected at Fri 11-20 21:54:36 is 0s from nat_vent_manual_override_exit at 21:54:36" in text

    def test_system_log_records_name_the_day(self):
        real_now = datetime.datetime.now(datetime.UTC)
        handler = SimpleNamespace(
            get_records=lambda: [
                {
                    "time": (real_now - datetime.timedelta(hours=30)).isoformat(),
                    "level": "WARNING",
                    "logger_name": "custom_components.climate_advisor.coordinator",
                    "message": "Weather entity not ready",
                    "zone": "z1",
                }
            ]
        )
        expected_local = (real_now - datetime.timedelta(hours=30)).astimezone(_LA)
        expected = f"{_WD[expected_local.weekday()]} {expected_local:%m-%d %H:%M}"
        with (
            patch.object(ctx.log_capture, "get_handler", return_value=handler),
            patch.object(ctx.zone_registry, "resolve_zone_display_name", return_value="Zone 1"),
            patch.object(ctx.dt_util, "as_local", side_effect=_to_la),
        ):
            text = asyncio.run(ctx.build_event_log_context(object(), _coord([], zone_label="z1"), hours=48))
        assert f"  {expected} WARNING [custom_components.climate_advisor.coordinator]" in text


class TestOverrideSections:
    def test_fan_ownership_and_current_override_name_the_day(self):
        real_now = datetime.datetime.now(datetime.UTC)
        fan_on = real_now - datetime.timedelta(hours=26)
        started = real_now - datetime.timedelta(hours=20)
        events = [{"type": "fan_activated", "time": fan_on.isoformat()}]
        ae = SimpleNamespace(
            _manual_override_active=True, _manual_override_time=started.isoformat(), _grace_active=True
        )
        coord = _coord(events, _today_record=None, automation_engine=ae)

        def stamp(dt):
            local = dt.astimezone(_LA)
            return f"{_WD[local.weekday()]} {local:%m-%d %H:%M}"

        with (
            patch.object(ctx.dt_util, "as_local", side_effect=_to_la),
            patch.object(ctx.dt_util, "now", return_value=real_now),
        ):
            text = asyncio.run(ctx.build_override_details_context(None, coord, hours=48))
        assert f"  Current override:  active since {stamp(started)}, " in text
        assert f"  {stamp(fan_on)}: CA owns fan (fan_activated)" in text


class TestInvestigatorPromptAndFallback:
    def test_prompt_tells_the_ai_to_keep_weekday_and_date(self):
        prompt = inv._SYSTEM_PROMPT
        assert "DAYS AND DATES" in prompt
        assert "keep its weekday and date with it" in prompt
        assert "Never write a bare clock time" in prompt
        assert "<weekday> <month-day> <start> to <end>: <one plain-English sentence>" in prompt
        example = prompt.split("Example shape", 1)[1].split("## INCONGRUITIES", 1)[0]
        dated = [ln for ln in example.splitlines() if re.match(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}-\d{2} ", ln)]
        assert len(dated) == 3, example

    def test_drift_check_flags_a_summary_line_without_its_day(self, caplog):
        raw = "## ACTIVITY SUMMARY\n6:15 AM: Automation restored the morning comfort range.\n"
        with caplog.at_level("WARNING"):
            inv.parse_investigation_response(raw)
        assert any("missing a leading day and time" in rec.message for rec in caplog.records)

    def test_drift_check_accepts_dated_lines(self, caplog):
        raw = (
            "## ACTIVITY SUMMARY\n"
            "Sat 11-21 6:15 AM: Automation restored the morning comfort range.\n"
            "Fri 11-20 11:50 PM to Sat 11-21 12:10 AM: Every door and window reported closed.\n"
            "Sat Nov 21, 10:46 AM to 11:21 AM: You raised the heat by a degree.\n"
        )
        with caplog.at_level("WARNING"):
            inv.parse_investigation_response(raw)
        assert not any("drift" in rec.message.lower() for rec in caplog.records)

    def test_fallback_cycling_note_names_the_day(self):
        real_now = datetime.datetime.now(datetime.UTC)
        base = real_now - datetime.timedelta(hours=10)
        events = [
            {"type": "nat_vent_outdoor_rise_exit", "time": (base + datetime.timedelta(minutes=m)).isoformat()}
            for m in (0, 10, 20)
        ]
        coord = _coord(events)
        coord.learning = MagicMock()
        coord.learning.get_compliance_summary.return_value = {}
        coord.learning.generate_suggestions.return_value = []
        local = base.astimezone(_LA)
        expected = f"between {_WD[local.weekday()]} {local:%m-%d %H:%M} and "
        with patch.object(ctx.dt_util, "as_local", side_effect=_to_la):
            incongruities = inv.investigation_fallback(coord)["incongruities"]
        assert "Rapid nat-vent cycling detected: 3 transitions " + expected in incongruities


def test_dashboard_activity_rows_show_the_localized_date():
    """The dashboard Activity record (up to 7 days) formats each row's ``ts`` in the viewer's language."""
    from pathlib import Path

    js = (Path(ctx.__file__).parent / "frontend" / "nd_analysis.js").read_text(encoding="utf-8")
    assert "esc(r.ts ? ND.fmtDateTime(r.ts) : r.time)" in js
