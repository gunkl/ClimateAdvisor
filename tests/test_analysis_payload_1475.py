"""Issue #1475: the learning payload says which days the scores cover and what holds today so far.

Occupant view: the Analysis tab labelled 14-day scores "91 days recorded" and showed today's window advice as
"yes" after the live outdoor check had said no. ``window_days`` and ``today_live`` let it say what each number covers.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import LearningEngine


def test_compliance_names_its_window(tmp_path):
    eng = LearningEngine(tmp_path)
    eng._state.records = [{"date": f"2026-08-{d:02d}", "hvac_runtime_minutes": 10} for d in range(1, 31)]
    s = eng.get_compliance_summary()
    assert s["days_recorded"] == 30 and s["window_days"] == 14
    eng._state.records = eng._state.records[:5]
    assert eng.get_compliance_summary()["window_days"] == 5


def _coord(classification, outdoor, indoor):
    c = object.__new__(ClimateAdvisorCoordinator)
    c._current_classification = classification
    c._outdoor_temp_history = outdoor
    c._indoor_temp_history = indoor
    c._surface_timeline = lambda **_: object()
    return c


def test_today_live_says_what_holds_now():
    now = datetime(2026, 11, 8, 9, 0)
    cls = SimpleNamespace(windows_recommended=False, forecast_windows_recommended=True, day_type="mild")
    c = _coord(cls, [(now, 49.0), (now, 58.3), (now, 52.0)], [(now, 66.9), (now, 67.3)])
    win = SimpleNamespace(periods=[(now.replace(hour=11, minute=30), now.replace(hour=16, minute=45)), (None, None)])
    with patch("custom_components.climate_advisor.coordinator.timeline_windows", return_value=win):
        out = c.analysis_today_live()
    assert out["windows_recommended_now"] is False and out["forecast_windows_recommended"] is True
    assert out["plan_windows"] == [["11:30:00", "16:45:00"]]
    assert (out["observed_high_f"], out["observed_low_f"], out["avg_indoor_f"]) == (58.3, 49.0, 67.1)


def test_today_live_without_history_or_timeline():
    c = object.__new__(ClimateAdvisorCoordinator)
    c._current_classification = None
    c._outdoor_temp_history = []
    c._indoor_temp_history = []
    c._surface_timeline = lambda **_: None
    out = c.analysis_today_live()
    assert out["windows_recommended_now"] is None and out["plan_windows"] == []
    assert out["observed_high_f"] is None and out["avg_indoor_f"] is None


def test_today_live_never_raises():
    c = object.__new__(ClimateAdvisorCoordinator)
    c._current_classification = None

    def boom(**_):
        raise RuntimeError

    c._surface_timeline = boom
    assert c.analysis_today_live() == {}
