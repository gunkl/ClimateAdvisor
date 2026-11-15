"""Issue #1341 S5: the scorecard's ``reasons`` keep their exact English; ``reasons_msg`` carries the same reasons as
catalog messages (``status.pa_reason.*``) for the dashboard to render in the viewer's language."""

from __future__ import annotations

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.prediction_accuracy import _score_row


@pytest.fixture(autouse=True)
def _catalogs():
    i18n.load_catalogs()


def _row(points, v3_running=True):
    return _score_row("z", "heating", "4h", points, v3_running)


def _pts(n, v4_off=1.0, v3=True):
    # (actual, flat, v4, v3): v4 is `v4_off` degrees high, v3 is exact
    return [(70.0, 70.0, 70.0 + v4_off, 70.0 if v3 else None) for _ in range(n)]


def _assert_wire_matches(row):
    assert len(row["reasons_msg"]) == len(row["reasons"])
    for wire, text in zip(row["reasons_msg"], row["reasons"], strict=True):
        assert wire["key"].startswith("status.pa_reason.")
        assert i18n.render_en(i18n.Msg.from_wire(wire)) == text


def test_not_enough_data_and_v3_collecting_english_unchanged():
    row = _row(_pts(10, v3=False))
    assert row["reasons"] == [
        "not enough data yet (n=10, need 72)",
        "v3 predictions at this look-ahead are still being collected",
    ]
    assert [w["key"] for w in row["reasons_msg"]] == [
        "status.pa_reason.not_enough_data",
        "status.pa_reason.v3_collecting",
    ]
    assert row["reasons_msg"][0]["params"] == {"n": 10, "need": 72}
    _assert_wire_matches(row)


def test_v3_off_english_unchanged():
    row = _row(_pts(80, v3=False), v3_running=False)
    assert "v3 is not running (neither the main nor the comparison engine)" in row["reasons"]
    _assert_wire_matches(row)


def test_scored_reasons_english_unchanged():
    # 80 points where every engine answered, plus 40 where only v3 did -> coverage 80/120
    pts = _pts(80) + [(70.0, 70.0, None, 70.0) for _ in range(40)]
    row = _row(pts)
    assert row["scored"] is True
    assert row["reasons"] == [
        "v4 not better than v3",
        "v4 not better than no-change",
        "|bias| 1.00 > 0.5",
        "coverage 0.67 < 0.8",
    ]
    _assert_wire_matches(row)


def test_clean_row_has_empty_reasons_msg():
    row = _row([(70.0, 71.0, 70.0, 70.5) for _ in range(80)])
    assert row["reasons"] == [] and row["reasons_msg"] == []


def test_reason_keys_exist_in_the_catalog():
    import json
    from pathlib import Path

    cat = json.loads(
        (Path(i18n.__file__).parent / "frontend" / "i18n" / "status" / "en.json").read_text(encoding="utf-8")
    )
    for key in (
        "not_enough_data",
        "v3_collecting",
        "v3_off",
        "v4_not_better_v3",
        "v4_not_better_flat",
        "bias_over",
        "coverage_low",
    ):
        assert f"status.pa_reason.{key}" in cat
