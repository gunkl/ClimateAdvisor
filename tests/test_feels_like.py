"""Issue #1414: the NWS feels-like rules behind the Status chart's feels-like lines (``feels_like.py``).

Occupant view: on a 90 F afternoon at 70 % humidity the chart says it feels like about 106 F (the NWS heat index
table's value), a 30 F morning in a 20 mph wind feels like about 17 F (the NWS wind chill chart), and a mild 75 F day
shows no feels-like line at all instead of a made-up number. Worked values below are the real functions' outputs and
agree with the published NWS tables to the table's whole degree.
"""

from __future__ import annotations

import math
import sys

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.feels_like import (  # noqa: E402
    feels_like_f,
    heat_index_f,
    wind_chill_f,
    wind_speed_to_mph,
)


class TestHeatIndex:
    @pytest.mark.parametrize(
        ("t", "rh", "expected", "nws_table"),
        [
            (90.0, 70.0, 105.92202, 106),  # NWS heat index chart: 90 F / 70 % -> 106 F
            (96.0, 65.0, 121.03163, 121),  # 96 F / 65 % -> 121 F
            (80.0, 50.0, 80.80290, 81),
        ],
    )
    def test_worked_values(self, t, rh, expected, nws_table):
        hi = heat_index_f(t, rh)
        assert hi == pytest.approx(expected, abs=1e-4)
        assert round(hi) == nws_table

    def test_averaged_switch_boundary(self):
        """The simple formula averaged with T decides: 80 F / 50 % averages 80.03 (regression), 79.9 F / 50 % 79.92."""
        assert heat_index_f(80.0, 50.0) is not None
        assert heat_index_f(79.9, 50.0) is None

    def test_switch_is_on_the_average_not_on_temperature(self):
        """81.1 F at 0 % averages just above 80 (regression applies); 80 F at 40 % averages below 80 (None)."""
        assert heat_index_f(81.1, 0.0) == pytest.approx(77.43445, abs=1e-4)
        assert heat_index_f(80.0, 40.0) is None

    def test_low_humidity_adjustment_subtracted(self):
        """RH < 13 % and 80-112 F: subtract [(13-RH)/4]*sqrt([17-|T-95|]/17) = 0.630 F at 100 F / 10 %."""
        assert heat_index_f(100.0, 10.0) == pytest.approx(94.12248, abs=1e-4)
        # at 113 F the adjustment's range ends (its square root would be of a negative number): no error
        assert heat_index_f(113.0, 10.0) == pytest.approx(107.81910, abs=1e-4)

    def test_high_humidity_adjustment_added(self):
        """RH > 85 % and 80-87 F: add [(RH-85)/10]*[(87-T)/5] = +0.2 F at 85 F / 90 %."""
        assert heat_index_f(85.0, 90.0) == pytest.approx(101.78080, abs=1e-4)
        assert heat_index_f(88.0, 90.0) == pytest.approx(113.24727, abs=1e-4)  # above 87 F: not adjusted

    @pytest.mark.parametrize(("t", "rh"), [(75.0, 90.0), (70.0, 50.0), (40.0, 99.0)])
    def test_none_below_the_regression(self, t, rh):
        assert heat_index_f(t, rh) is None

    @pytest.mark.parametrize("rh", [-1.0, 100.5, math.nan, None])
    def test_invalid_humidity_is_none(self, rh):
        assert heat_index_f(95.0, rh) is None


class TestWindChill:
    @pytest.mark.parametrize(
        ("t", "v", "expected", "nws_chart"),
        [(30.0, 20.0, 17.36178, 17), (0.0, 15.0, -19.39795, -19), (40.0, 10.0, 33.64255, 34)],
    )
    def test_worked_values(self, t, v, expected, nws_chart):
        wc = wind_chill_f(t, v)
        assert wc == pytest.approx(expected, abs=1e-4)
        assert round(wc) == nws_chart

    def test_boundaries(self):
        """Defined at or below 50 F and for wind ABOVE 3 mph only."""
        assert wind_chill_f(50.0, 3.01) is not None
        assert wind_chill_f(50.0, 3.0) is None
        assert wind_chill_f(50.01, 10.0) is None


class TestFeelsLike:
    def test_heat_index_first(self):
        assert feels_like_f(90.0, 70.0, wind_mph=20.0) == pytest.approx(105.92202, abs=1e-4)

    def test_wind_chill_when_given_wind(self):
        assert feels_like_f(30.0, 80.0, wind_mph=20.0) == pytest.approx(17.36178, abs=1e-4)
        assert feels_like_f(30.0, None, wind_mph=20.0) == pytest.approx(17.36178, abs=1e-4)

    def test_indoor_call_has_no_wind_chill(self):
        assert feels_like_f(45.0, 50.0) is None

    def test_mild_is_none(self):
        assert feels_like_f(72.0, 50.0, wind_mph=10.0) is None
        assert feels_like_f(None, 50.0, wind_mph=10.0) is None


class TestWindUnits:
    @pytest.mark.parametrize(
        ("value", "unit", "mph"),
        [(10.0, "mph", 10.0), (32.18688, "km/h", 20.0), (10.0, "m/s", 22.36936), (10.0, "kn", 11.50779)],
    )
    def test_conversions(self, value, unit, mph):
        assert wind_speed_to_mph(value, unit) == pytest.approx(mph, abs=1e-4)

    @pytest.mark.parametrize(("value", "unit"), [(5.0, None), (5.0, "Beaufort"), (None, "mph"), (-1.0, "mph")])
    def test_unknown_or_missing_is_none(self, value, unit):
        assert wind_speed_to_mph(value, unit) is None
