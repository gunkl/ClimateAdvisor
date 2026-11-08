"""Issue #1423: v4's past lines are continuous, between deploys too.

Occupant view: on the Prediction accuracy page and the Status chart, v4's past lines (what v4 said 1/4/12/24 h before)
were mostly dots or missing - about 24 points in 24 h, nearly all from the backtest published at the last deploy - and
they thinned out in the hours after each deploy. Two causes, both pinned here:

1. A restart (every deploy) never calls ``async_shutdown``, so the archive's hourly save was the only write: each
   restart dropped up to an hour of v4's predictions. Live Zone 1 on 2026-11-07 lost the 13:45 and 15:14 cycles that
   way (restarts at 14:38 and 15:28). The EVENT_HOMEASSISTANT_STOP listener now flushes the archive.
2. A cycle writes the ONE slot nearest ``now + lead``; a skipped, lost or moved cycle left the slots between two
   cycles' targets empty for good (no other cycle ever has them that far ahead). ``record()`` now fills them from the
   previous or the current curve, whichever was closer to that look-ahead, within ``fill_tolerance_s``; a longer outage
   stays an honest hole. First write still wins.

Plus the size budget for every slot of 400 days x every lead, and the backtest/archive join with no gap.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor.const import V4_ARCHIVE_LEADS, V4_ARCHIVE_MAX_DAYS
from custom_components.climate_advisor.v4_archive import V4Archive, fill_tolerance_s, lead_hours, slot_key

TZ = ZoneInfo("America/Los_Angeles")
CA = "_ca"
SLOT = 1800
# The look-aheads the deploy-time backtest publishes (the join with it is about these). The 2/3/5-day ones of Issue
# #1483 come from the live archive only: they start once the archive is older than the look-ahead (TestLongLeads).
BACKTEST_LEADS = ("1h", "4h", "12h", "24h")


def _curve(cycle: datetime) -> list[tuple[datetime, float]]:
    """A week-long curve every 15 min (Issue #1483: the display line reaches the 2/3/5-day look-aheads) whose value
    IS its look-ahead in hours (+60), so a test can read back how far ahead each archived value was made:
    ``value - 60``."""
    return [(cycle + timedelta(minutes=15 * k), 60.0 + 0.25 * k) for k in range(1, 4 * 168 + 1)]


def _run(arch: V4Archive, cycles: list[datetime]) -> None:
    for c in cycles:
        arch.record(c, {CA: _curve(c)}, CA)


def _slots(first: float, last: float) -> list[int]:
    lo = int(round(first / SLOT)) * SLOT
    return list(range(lo, int(last) + 1, SLOT))


def _assert_continuous(arch: V4Archive, start: float, end: float) -> None:
    """Every slot in [start, end] holds a value at every lead, made within the lead's tolerance and never after."""
    for lead in V4_ARCHIVE_LEADS:
        got = arch.series(CA, lead, float("-inf"), float("inf"))
        want = _slots(start + lead_hours(lead) * 3600, end + lead_hours(lead) * 3600 - SLOT)
        missing = [datetime.fromtimestamp(s, TZ).strftime("%m-%d %H:%M") for s in want if s not in got]
        assert not missing, f"{lead}: {len(missing)} empty slots, first {missing[:5]}"
        tol_h = (fill_tolerance_s(lead) + 450) / 3600  # + half a 15-min grid step: the value is the nearest point
        for s in want:
            ahead = got[s] - 60.0
            assert ahead > 0, f"{lead} slot {s}: a value made after the slot"
            assert abs(ahead - lead_hours(lead)) <= tol_h, f"{lead} slot {s}: made {ahead:.2f} h ahead"


# ---------------------------------------------------------------------------------------------- 1. the cadence
class TestEverySlotFills:
    START = datetime(2026, 11, 7, 12, 14, 30, tzinfo=TZ)

    def test_regular_cycles_fill_every_slot_at_every_lead_with_the_exact_lead(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="z")
        cycles = [self.START + timedelta(minutes=30 * k) for k in range(80)]
        _run(arch, cycles)
        _assert_continuous(arch, cycles[0].timestamp(), cycles[-1].timestamp())
        for lead in V4_ARCHIVE_LEADS:  # nothing was gap-filled: each slot is its own cycle's nearest point
            for v in arch.series(CA, lead, 0, 1e12).values():
                assert abs((v - 60.0) - lead_hours(lead)) <= 0.25

    def test_the_live_zone1_timeline_has_no_holes(self, tmp_path):
        """2026-11-07 12:23-15:44 PST, the cycles that reached the archive (13:45:27 and 15:14:15 were lost to the
        14:38 and 15:28 restarts). Before the fix the 1 h line had holes at 15:00 and 16:00 (4 h at 18:00 and 19:00)."""
        times = [
            *("12:23:12", "12:28:05", "12:31:30", "12:31:40", "12:33:47", "12:37:13", "12:37:23", "13:07:23"),
            *("13:37:23", "13:40:27", "14:40:39", "14:44:05", "14:44:15", "15:30:01", "15:33:23", "15:33:34"),
            *("15:41:27", "15:44:16"),
        ]
        cycles = [datetime(2026, 11, 7, *map(int, t.split(":")), tzinfo=TZ) for t in times]
        arch = V4Archive(tmp_path, entry_id="z")
        _run(arch, cycles)
        one_h = arch.series(CA, "1h", 0, 1e12)
        for hhmm in ("15:00", "16:00"):
            h, m = map(int, hhmm.split(":"))
            assert slot_key(datetime(2026, 11, 7, h, m, tzinfo=TZ)) in one_h, f"1h {hhmm} is still a hole"
        _assert_continuous(arch, cycles[0].timestamp(), cycles[-1].timestamp())

    def test_irregular_refreshes_fill_every_slot(self, tmp_path):
        """Any refresh (a service call, an override, a reload) moves HA's 30-minute timer; cycles land at random
        phases, sometimes seconds apart, never more than 30 min apart."""
        rng = random.Random(1423)
        t, cycles = self.START, []
        while t < self.START + timedelta(days=3):
            cycles.append(t)
            t += timedelta(seconds=rng.choice([10, 200, 400, 900, 1500, 1799, 1800, 1800, 1800]))
        arch = V4Archive(tmp_path, entry_id="z")
        _run(arch, cycles)
        _assert_continuous(arch, cycles[0].timestamp(), cycles[-1].timestamp())

    def test_one_skipped_cycle_is_filled_from_its_neighbours(self, tmp_path):
        """A cycle can be skipped (no fresh chart row, a failure caught by the wrapper): its slots are filled."""
        cycles = [self.START + timedelta(minutes=30 * k) for k in range(60) if k != 30]
        arch = V4Archive(tmp_path, entry_id="z")
        _run(arch, cycles)
        _assert_continuous(arch, cycles[0].timestamp(), cycles[-1].timestamp())

    def test_first_write_wins_is_kept(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="z")
        c0 = self.START
        arch.record(c0, {CA: _curve(c0)}, CA)
        before = {lead: arch.series(CA, lead, 0, 1e12) for lead in V4_ARCHIVE_LEADS}
        c1 = c0 + timedelta(minutes=40)  # 40 min: its targets AND the gap-fill touch the slots written above
        later = [(ts, v + 10.0) for ts, v in _curve(c1)]
        arch.record(c1, {CA: later}, CA)
        for lead in V4_ARCHIVE_LEADS:
            after = arch.series(CA, lead, 0, 1e12)
            assert all(after[s] == v for s, v in before[lead].items()), f"{lead}: a value was overwritten"

    def test_an_outage_longer_than_the_tolerance_stays_an_honest_hole(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="z")
        c0 = self.START
        c1 = c0 + timedelta(hours=3)
        _run(arch, [c0, c1])
        got = arch.series(CA, "1h", 0, 1e12)
        middle = slot_key(c0 + timedelta(hours=2, minutes=30))  # 2.5 h after c0, before c1: nobody said it 1 h ahead
        assert middle not in got
        for v in got.values():
            assert abs((v - 60.0) - 1.0) <= (fill_tolerance_s("1h") + 450) / 3600

    def test_the_tolerance_grows_with_the_lead(self):
        assert [fill_tolerance_s(lead) / 60 for lead in V4_ARCHIVE_LEADS] == [30, 30, 90, 180, 360, 540, 900]

    def test_a_node_model_fills_its_node_lines_and_still_derives_ca(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="z")
        hall = "climate.hall"
        cycles = [self.START + timedelta(minutes=30 * k) for k in range(60) if k != 20]
        for c in cycles:
            arch.record(c, {hall: _curve(c), CA: _curve(c)}, hall)
        assert CA not in arch._lines
        assert arch.series(CA, "4h", 0, 1e12) == arch.series(hall, "4h", 0, 1e12)
        lead_s = 4 * 3600
        want = _slots(cycles[0].timestamp() + lead_s, cycles[-1].timestamp() + lead_s - SLOT)
        assert set(want) <= set(arch.series(hall, "4h", 0, 1e12))


# ---------------------------------------------------------------------------------------------- 2. restart
class TestRestart:
    START = datetime(2026, 11, 7, 12, 14, 30, tzinfo=TZ)

    def test_the_cycle_after_a_restart_fills_the_gap_from_the_saved_curve(self, tmp_path):
        before = [self.START + timedelta(minutes=30 * k) for k in range(10)]
        arch = V4Archive(tmp_path, entry_id="z")
        _run(arch, before)
        assert arch.save()
        on_disk = json.loads(arch._path.read_text())
        assert on_disk["last"]["at"] == pytest.approx(before[-1].timestamp())
        restarted = V4Archive(tmp_path, entry_id="z")
        restarted.load()
        after = [before[-1] + timedelta(minutes=50) + timedelta(minutes=30 * k) for k in range(10)]  # 50-min outage
        _run(restarted, after)
        _assert_continuous(restarted, before[0].timestamp(), after[-1].timestamp())

    def test_restarts_every_few_hours_keep_the_last_day_continuous_without_a_deploy(self, tmp_path):
        """Two days of 30-min cycles with a restart (stop flush, then a fresh archive loading the file) every 5 h."""
        t, arch, last_restart = self.START, V4Archive(tmp_path, entry_id="z"), self.START
        cycles: list[datetime] = []
        while t < self.START + timedelta(days=2):
            if t - last_restart >= timedelta(hours=5):
                assert arch.save()  # EVENT_HOMEASSISTANT_STOP flush (TestStopFlush pins the wiring)
                arch = V4Archive(tmp_path, entry_id="z")
                arch.load()
                last_restart = t
                t += timedelta(minutes=3)  # the restart takes a few minutes; the timer restarts at a new phase
            arch.record(t, {CA: _curve(t)}, CA)
            cycles.append(t)
            t += timedelta(minutes=30)
        _assert_continuous(arch, cycles[0].timestamp(), cycles[-1].timestamp())

    def test_a_file_without_the_last_curve_still_loads(self, tmp_path):
        """A file from 0.9.7 or older has no "last" object; and an older release ignores the key (rollback safe)."""
        path = V4Archive(tmp_path, entry_id="z")._path
        slot = str(int(datetime.now(UTC).timestamp()) // SLOT * SLOT)  # recent: load drops slots past the retention
        old = {"v": 1, "leads": list(V4_ARCHIVE_LEADS), "sensors": {CA: {slot: [1, 2, None, None]}}}
        path.write_text(json.dumps(old))
        arch = V4Archive(tmp_path, entry_id="z")
        arch.load()
        assert arch._last is None and arch.slot_count() == {CA: 1}

    @pytest.mark.parametrize("last", [None, "x", {"at": "x"}, {"at": 1.0, "sensors": {"_ca": [["a", 1]]}}, {"at": 1.0}])
    def test_an_odd_last_curve_is_ignored(self, tmp_path, last):
        path = V4Archive(tmp_path, entry_id="z")._path
        path.write_text(json.dumps({"v": 1, "leads": list(V4_ARCHIVE_LEADS), "sensors": {}, "last": last}))
        arch = V4Archive(tmp_path, entry_id="z")
        arch.load()
        assert arch._last is None


class TestStopFlush:
    def test_ha_stop_writes_the_archive_a_restart_used_to_lose(self, monkeypatch):
        from custom_components.climate_advisor.coordinator import EVENT_HOMEASSISTANT_STOP  # noqa: PLC0415
        from tests.test_v4_shadow_wiring import (  # noqa: PLC0415
            _build,
            _cycle,
            _fake_model_dict,
            _seed_cycle_inputs,
            _tick,
        )
        from tests.v4_fake_predictor import install  # noqa: PLC0415

        install(monkeypatch)
        coord, fake_hass, scheduler = _build()
        coord._v4_store.v4_model = _fake_model_dict(coord, primary="_ca", sensors=["_ca"])
        path = coord._v4_archive._path
        _seed_cycle_inputs(coord, scheduler)
        _cycle(coord, scheduler)  # the first cycle after start writes the file at once
        _tick(scheduler, 1800)
        _seed_cycle_inputs(coord, scheduler, indoor=71.0)
        _cycle(coord, scheduler)  # within the hour: memory only
        assert coord._v4_archive.dirty is True
        held = coord._v4_archive.slot_count()
        assert json.loads(path.read_text())["sensors"][CA].__len__() < held[CA]
        with scheduler.installed():
            fake_hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            scheduler.advance_by(0)
        assert coord._v4_archive.dirty is False, "the stop listener flushed the archive"
        assert len(json.loads(path.read_text())["sensors"][CA]) == held[CA]


# ---------------------------------------------------------------------------------------------- 3. size budget
class TestSizeBudget:
    def test_every_slot_of_400_days_at_every_lead_fits_the_m4_budget_in_memory_and_on_disk(self, tmp_path):
        """Memory is unchanged by the fill (each line already reserves 4 bytes per slot and lead); the file grows to
        every slot written. Budget: 5 MB in memory (M4) and, since Issue #1483 added the 2/3/5-day look-aheads (7
        leads, was 4), 1.25 MB per line on disk (was 1 MB) for the worst case of four lines with a week-long saved
        curve each. Live Zone 1 has two lines (about 2.2 MB at 400 days)."""
        n = V4_ARCHIVE_MAX_DAYS * 48
        now = datetime.now(UTC)
        base = int(now.timestamp()) // SLOT * SLOT - (n - 1) * SLOT
        arch = V4Archive(tmp_path, entry_id="z")
        lines = (CA, "climate.hall", "sensor.bed", "sensor.b")
        for sensor in lines:
            for k in range(n):
                for j, lead in enumerate(V4_ARCHIVE_LEADS):
                    arch.put(sensor, base + k * SLOT, lead, 65.0 + (k % 1000) / 100 + j)  # realistic 2-dp values
        arch._last = (now.timestamp(), {s: _curve_pts(now) for s in lines})
        assert arch.memory_bytes() < 5 * 1024 * 1024
        assert arch.save()
        size = arch._path.stat().st_size
        assert size < 5 * 1024 * 1024, f"{size / 1e6:.2f} MB for 4 lines x {V4_ARCHIVE_MAX_DAYS} days x 7 leads"
        again = V4Archive(tmp_path, entry_id="z")
        again.load()
        assert again.slot_count() == {s: n for s in lines}


def _curve_pts(now: datetime) -> list[tuple[float, float]]:
    return [(now.timestamp() + 900 * k, 68.25) for k in range(1, 4 * 168 + 1)]


# ---------------------------------------------------------------------------------------------- 4. the join
class TestBacktestArchiveJoin:
    NOW = datetime(2026, 11, 9, 16, 0, tzinfo=TZ)

    def _setup(self, tmp_path, archive_start: datetime, bt_end: datetime):
        arch = V4Archive(tmp_path, entry_id="z")
        cycles, t = [], archive_start
        while t <= self.NOW:
            cycles.append(t)
            t += timedelta(minutes=30)
        _run(arch, cycles)
        points = {}
        for lead in BACKTEST_LEADS:  # the deploy-time backtest: hourly, ending at the deploy
            rows, t = [], bt_end - timedelta(days=3)
            while t <= bt_end:
                rows.append({"t": t.isoformat(), "actual": 68.0, "flat": 68.0, "v4": 61.0})
                t += timedelta(hours=1)
            points[lead] = rows
        return arch, {"points": {CA: points}}

    def _past(self, arch, backtest):
        out = pa.build_chart_v4(
            now=self.NOW,
            start=(self.NOW - timedelta(hours=24)).timestamp(),
            end=self.NOW.timestamp(),
            range_str="24h",
            unit="fahrenheit",
            historical=False,
            archive=arch,
            backtest=backtest,
            model={"has_model": True, "confidence": "ok", "primary": CA},
            last_future=None,
            sensor_ids=[CA],
        )
        return out["past"][CA]

    def test_after_a_deploy_the_archive_takes_over_from_the_backtest_with_no_gap(self, tmp_path):
        """The backtest ends at the deploy (2 days ago); the archive has run since before it, so for every lead the
        last 24 h are the archive's 30-minute points, with nothing missing - no new deploy needed."""
        deploy = self.NOW - timedelta(days=2)
        arch, bt = self._setup(tmp_path, archive_start=deploy - timedelta(days=2, minutes=-14), bt_end=deploy)
        for lead in BACKTEST_LEADS:
            ts = [datetime.fromisoformat(p["ts"]).timestamp() for p in self._past(arch, bt)[lead]]
            gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
            assert len(ts) == 48 and max(gaps) == SLOT, f"{lead}: {len(ts)} points, widest gap {max(gaps) / 60} min"

    def test_the_join_right_after_the_backtest_ends_has_no_gap(self, tmp_path):
        """The window holds the backtest's last hours AND the archive after them: hourly then 30-minute points,
        never a gap wider than the backtest's own hour."""
        deploy = self.NOW - timedelta(hours=10)
        arch, bt = self._setup(tmp_path, archive_start=deploy - timedelta(days=2, minutes=-14), bt_end=deploy)
        for lead in BACKTEST_LEADS:
            ts = [datetime.fromisoformat(p["ts"]).timestamp() for p in self._past(arch, bt)[lead]]
            gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
            assert max(gaps) <= SLOT, f"{lead}: widest gap {max(gaps) / 60} min"  # the archive covers both sides
            assert ts[-1] >= (self.NOW - timedelta(minutes=30)).timestamp()

    def test_the_accuracy_page_scores_every_slot_of_the_day(self, tmp_path):
        deploy = self.NOW - timedelta(days=2)
        arch, bt = self._setup(tmp_path, archive_start=deploy - timedelta(days=2, minutes=-14), bt_end=deploy)
        rows = []
        t = self.NOW - timedelta(days=3)
        while t < self.NOW:
            rows.append({"ts": t.isoformat(), "hvac": "idle", "indoor": 68.0, "outdoor": 50.0})
            t += timedelta(minutes=30)
        for lead in BACKTEST_LEADS:
            out = pa.build_prediction_accuracy(
                zone="z",
                sensor=CA,
                lead=lead,
                range_str="24h",
                before=None,
                now=self.NOW,
                unit="fahrenheit",
                chart_rows=rows,
                archive=arch,
                backtest=bt,
                model={"has_model": True, "confidence": "ok", "primary": CA},
                sensors_meta=[{"id": CA, "name": "Climate Advisor reading", "role": "x"}],
            )
            v4_points = [p for p in out["points"] if "v4" in p]
            assert len(v4_points) == 48, f"{lead}: {len(v4_points)} v4 points in 24 h"


# ---------------------------------------------------------------------------------------------- 5. the long leads
class TestLongLeads:
    """Issue #1483: the 2/3/5-day look-aheads are scored from the live archive alone (the backtest stops at 24 h)."""

    NOW = datetime(2026, 11, 16, 16, 0, tzinfo=TZ)

    def test_an_archive_older_than_the_lead_scores_every_slot_of_the_day(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id="z")
        cycles, t = [], self.NOW - timedelta(days=7, minutes=-14)
        while t <= self.NOW:
            cycles.append(t)
            t += timedelta(minutes=30)
        _run(arch, cycles)
        rows, t = [], self.NOW - timedelta(days=3)
        while t < self.NOW:
            rows.append({"ts": t.isoformat(), "hvac": "idle", "indoor": 68.0, "outdoor": 50.0})
            t += timedelta(minutes=30)
        for lead in ("48h", "72h", "120h"):
            out = pa.build_prediction_accuracy(
                zone="z",
                sensor=CA,
                lead=lead,
                range_str="24h",
                before=None,
                now=self.NOW,
                unit="fahrenheit",
                chart_rows=rows,
                archive=arch,
                backtest=None,
                model={"has_model": True, "confidence": "ok", "primary": CA},
                sensors_meta=[{"id": CA, "name": "Climate Advisor reading", "role": "x"}],
            )
            v4_points = [p for p in out["points"] if "v4" in p]
            assert len(v4_points) == 48, f"{lead}: {len(v4_points)} v4 points in 24 h"
            made = {round(p["v4"] - 60.0) for p in v4_points}  # the curve's value is how far ahead it was made
            assert made == {lead_hours(lead)}, (lead, made)

    def test_a_young_archive_has_no_long_lead_yet(self, tmp_path):
        """Two days of archive: nothing was made 5 days before any slot yet, so the 5-day line is honestly empty."""
        arch = V4Archive(tmp_path, entry_id="z")
        cycles = [self.NOW - timedelta(days=2) + timedelta(minutes=30 * k) for k in range(97)]
        _run(arch, cycles)
        assert arch.series(CA, "120h", 0, self.NOW.timestamp()) == {}
        assert arch.series(CA, "24h", 0, self.NOW.timestamp())

    def test_a_curve_that_stops_short_writes_no_long_lead(self, tmp_path):
        """A forecast that only reaches 2 days (a provider with a short daily forecast): the 3/5-day columns stay
        empty."""
        arch = V4Archive(tmp_path, entry_id="z")
        c = self.NOW
        arch.record(c, {CA: [(c + timedelta(minutes=15 * k), 60.0 + 0.25 * k) for k in range(1, 4 * 50)]}, CA)
        assert arch.series(CA, "48h", 0, 1e12)
        assert arch.series(CA, "72h", 0, 1e12) == {} and arch.series(CA, "120h", 0, 1e12) == {}
