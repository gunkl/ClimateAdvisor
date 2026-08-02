"""Tests for ClimateAdvisorBriefingSensor (Issue #555).

Issue #555: the sensor's native_value returned the TLDR text unconditionally with no
length guard, so HA rejected states over 255 chars and forced the sensor to `unknown`
on the exact days (away/vacation occupancy, dual window-opportunity) that had the most
to say. briefing.py now shrinks the TLDR content so this should rarely trigger, but the
truncation safety net (ClimateAdvisorBaseSensor._capped_state) must still behave
correctly when it does.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from custom_components.climate_advisor.const import ATTR_BRIEFING, ATTR_BRIEFING_SHORT
from custom_components.climate_advisor.sensor import ClimateAdvisorBriefingSensor


def _briefing_sensor(data: dict | None) -> ClimateAdvisorBriefingSensor:
    """Build a real ClimateAdvisorBriefingSensor over `data`."""
    coord = MagicMock()
    coord.data = data
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return ClimateAdvisorBriefingSensor(coord, entry)


class TestBriefingSensorNativeValue:
    def test_short_tldr_returned_unchanged(self, caplog):
        """TLDR under the limit is returned as-is, no truncation, no warning."""
        text = "  Day Type: Hot (88°F)\n  HVAC Mode: Cool at 75°F"
        sensor = _briefing_sensor({ATTR_BRIEFING_SHORT: text, ATTR_BRIEFING: text})
        with caplog.at_level(logging.WARNING):
            value = sensor.native_value
        assert value == text
        assert not any("truncated" in r.message for r in caplog.records)

    def test_long_tldr_truncated_with_warning(self, caplog):
        """TLDR over 250 chars is capped to 247 chars + '...', WARNING logged once."""
        long_text = "X" * 300
        sensor = _briefing_sensor({ATTR_BRIEFING_SHORT: long_text, ATTR_BRIEFING: long_text})
        with caplog.at_level(logging.WARNING):
            value1 = sensor.native_value
            value2 = sensor.native_value
        assert value1 == long_text[:247] + "..."
        assert len(value1) <= 255
        assert value2 == value1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "truncated" in r.message]
        assert len(warnings) == 1

    def test_falls_back_to_full_briefing_when_short_empty(self):
        """No TLDR yet — falls back to (possibly truncated) full briefing text."""
        full = "Full briefing text"
        sensor = _briefing_sensor({ATTR_BRIEFING_SHORT: "", ATTR_BRIEFING: full})
        assert sensor.native_value == full

    def test_falls_back_and_truncates_long_full_briefing(self):
        """No TLDR yet, and the full briefing itself exceeds the limit."""
        full = "Y" * 300
        sensor = _briefing_sensor({ATTR_BRIEFING_SHORT: "", ATTR_BRIEFING: full})
        assert sensor.native_value == full[:247] + "..."

    def test_empty_coordinator_data_returns_empty_string(self):
        """No coordinator data at all — existing empty-string behavior preserved."""
        sensor = _briefing_sensor(None)
        assert sensor.native_value == ""


class TestBriefingSensorAttributes:
    def test_attributes_expose_full_briefing_and_tldr(self):
        sensor = _briefing_sensor({ATTR_BRIEFING: "full text", ATTR_BRIEFING_SHORT: "short text"})
        attrs = sensor.extra_state_attributes
        assert attrs["full_briefing"] == "full text"
        assert attrs["tldr"] == "short text"

    def test_tldr_attribute_holds_untruncated_text_even_when_state_truncates(self):
        """The attribute must retain the full TLDR even when native_value truncates it."""
        long_text = "Z" * 300
        sensor = _briefing_sensor({ATTR_BRIEFING_SHORT: long_text, ATTR_BRIEFING: "full"})
        assert sensor.native_value == long_text[:247] + "..."
        assert sensor.extra_state_attributes["tldr"] == long_text

    def test_attributes_empty_when_no_coordinator_data(self):
        sensor = _briefing_sensor(None)
        attrs = sensor.extra_state_attributes
        assert attrs["full_briefing"] == ""
        assert attrs["tldr"] == ""
