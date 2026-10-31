"""Snapshots of what the occupant reads, taken from the real surfaces (Step 2 Increment 4, Issue #945).

Occupant view: the daily briefing and the Status tab are the product. A wording regression (Issue #940: a
reopen-windows time printed before the close-windows time) could ship with CI green because nothing checked the
words. Here a real house is built for each reference day, the clock runs through the 06:00 briefing, and the
briefing, notifications, Status, Next User Action, Next Automation and fan cards are compared with the committed
snapshot. A change shows up as a diff in the pull request and a human decides whether it is right.

Update deliberately, then review the .ambr diff as prose:

    python scripts/test.py ha --snapshot-update tests_ha/test_snapshots.py

The Next Automation card is also checked against the Status Card Ontology word list, because a snapshot would
otherwise happily lock a violation in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests_ha.house.surfaces import NEXT_AUTOMATION_BANNED_WORDS, read_surfaces

pytestmark = [pytest.mark.house, pytest.mark.snapshot]

# 05:50 UTC start: the scheduled 06:00 briefing fires 10 minutes in. The house is scripted (physics off) so the
# indoor temperature, and therefore every sentence, is exactly what the test set.
_START = datetime(2026, 7, 15, 5, 50, tzinfo=UTC)

_DAYS = {
    "hot": {"outdoor_f": 80.0, "high_f": 96.0, "low_f": 68.0, "indoor": 75.0},
    "warm": {"outdoor_f": 70.0, "high_f": 84.0, "low_f": 62.0, "indoor": 72.0},
    "mild": {"outdoor_f": 62.0, "high_f": 72.0, "low_f": 55.0, "indoor": 71.0},
    "cool": {"outdoor_f": 50.0, "high_f": 62.0, "low_f": 44.0, "indoor": 70.0},
    "cold": {"outdoor_f": 30.0, "high_f": 41.0, "low_f": 24.0, "indoor": 68.0},
}


@pytest.fixture(autouse=True)
def _snapshot_file_is_readable(request):
    """syrupy treats a snapshot file it cannot find or parse as 'no snapshots' and reports every test as "does not
    exist". Fail with what syrupy itself resolves and reads instead, so a CI-only miss is diagnosable."""
    path = Path(__file__).resolve().parent / "snapshots" / "test_snapshots.ambr"
    assert path.is_file() and path.stat().st_size > 0, (
        f"snapshot file missing or empty: {path} (cwd={Path.cwd()}); syrupy would report 'does not exist'"
    )
    if "snapshot" not in request.fixturenames:
        return
    snap = request.getfixturevalue("snapshot")
    ext, loc = snap.extension, snap.test_location
    where = ext.get_location(test_location=loc, index=0)
    names = sorted(s.name for coll in ext.discover_snapshots(test_location=loc) for s in coll)
    assert Path(where).resolve() == path and names, (
        f"syrupy resolves {where!r} (test filepath {loc.filepath!r}) and parses {len(names)} snapshot(s) "
        f"{names[:3]}; expected file {path} (cwd={Path.cwd()}, header={path.read_bytes()[:40]!r})"
    )


def _assert_ontology(surfaces: dict) -> None:
    text = surfaces["next_automation"].lower()
    banned = [w for w in NEXT_AUTOMATION_BANNED_WORDS if w in text]
    assert not banned, f"Next Automation card uses banned word(s) {banned}: {surfaces['next_automation']!r}"


async def _day(make_house, name: str, **overrides):
    spec = {**_DAYS[name], **overrides.pop("spec", {})}
    house = await make_house(
        profile="scripted",
        start=_START,
        startup="cold",
        initial_indoor_f=spec["indoor"],
        outdoor_f=spec["outdoor_f"],
        high_f=spec["high_f"],
        low_f=spec["low_f"],
        ca_config={"push_briefing": True, **overrides.pop("ca_config", {})},
        **overrides,
    )
    house.set_indoor(spec["indoor"])
    return house


@pytest.mark.parametrize("day", list(_DAYS))
async def test_reference_day_surfaces(make_house, snapshot, day) -> None:
    """Briefing, notifications and the status cards for a plain day of each type (home, no fan)."""
    house = await _day(make_house, day)
    await house.advance(minutes=40)  # through the 06:00 briefing to 06:30
    surfaces = read_surfaces(house)
    _assert_ontology(surfaces)
    assert surfaces == snapshot


@pytest.mark.slow
async def test_warm_day_through_the_day(make_house, snapshot) -> None:
    """The warm day at three moments: the wording of Next User Action / Next Automation has regressed before."""
    house = await _day(make_house, "warm")
    seen = {}
    for label, minutes in (("06:30", 40), ("13:00", 390), ("20:00", 420)):
        await house.advance(minutes=minutes)
        seen[label] = read_surfaces(house)
        seen[label].pop("notifications")  # the briefing text is covered by test_reference_day_surfaces
        _assert_ontology(seen[label])
    assert seen == snapshot


async def test_mild_day_with_window_open_and_whole_house_fan(make_house, snapshot) -> None:
    """A mild day, window opened by the person, a whole-house fan available: Status, fan and next-step cards."""
    house = await _day(make_house, "mild", fan="whf", contacts=["window"], ca_config={"fan_min_runtime_per_hour": 0})
    await house.advance(minutes=40)
    await house.open("window")
    await house.advance(minutes=20)  # past the contact debounce
    surfaces = read_surfaces(house)
    surfaces.pop("notifications")
    _assert_ontology(surfaces)
    assert surfaces == snapshot


async def test_away_day(make_house, snapshot) -> None:
    """Occupant away (home toggle off): setback wording on the briefing and the cards."""
    house = await _day(
        make_house,
        "warm",
        extra_states={"input_boolean.home": "off"},
        ca_config={"home_toggle_entity": "input_boolean.home"},
    )
    await house.advance(minutes=40)
    surfaces = read_surfaces(house)
    _assert_ontology(surfaces)
    assert surfaces == snapshot


async def test_celsius_warm_day(make_house, snapshot) -> None:
    """A Celsius zone on a metric Home Assistant, same weather as the warm day: units and rounding differ only."""
    house = await _day(make_house, "warm", units="metric")
    await house.advance(minutes=40)
    surfaces = read_surfaces(house)
    _assert_ontology(surfaces)
    assert surfaces == snapshot
