"""Shared date/timezone boundary hazard fixtures (Issue #906).

Issue #190 (pre-v0.3.44) shipped once already because daily-forecast UTC-midnight
timestamps were converted via ``dt_util.as_local()`` before extracting their
calendar date — which shifts the date backward a day in negative-UTC-offset
zones. Nothing forced every date-handling function added afterward to be
exercised against the same hazard set, so a fix to one function's convention
doesn't protect a different function written later.

This module defines that hazard set ONCE as a plain list of ``DateBoundaryCase``
so any current or future forecast-date function can parametrize against it and
get DST / UTC-rollover / calendar-boundary coverage "for free" — without having
to rediscover Issue #190's history first.

Two data-source conventions are exercised, both already independently correct
for their own source (see ``coordinator.py``'s ``_get_forecast()`` and
``_compute_day_hvac_modes()``/``_build_extended_hourly_forecast()``):

- **Daily-forecast entries** (day-labels): use the RAW date component of the
  parsed datetime, no ``dt_util.as_local()`` conversion. ``daily_expected_raw_date``
  is the date that convention must produce.
- **Hourly-forecast entries** (genuine specific-moment timestamps): convert via
  ``dt_util.as_local()`` first, then take the date. ``hourly_expected_local_date``
  is the date that convention must produce.

Each case supplies fixed-UTC-offset ``timezone`` instances (matching the
convention already used by ``tests/test_forecast_pipeline.py``'s ``_PDT``) rather
than a real IANA tzdata lookup, since the test-stub ``dt_util.as_local`` is a
``MagicMock`` unless a test explicitly patches it (see CLAUDE.md's "Testing
`_build_predicted_indoor_future` directly" section) — tests that want real
timezone-conversion semantics patch ``dt_util.as_local`` with
``side_effect=lambda dt: dt.astimezone(case.local_tz)`` using the case's own
``local_tz``, so the DST-offset difference between "before" and "after" cases is
genuinely exercised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Fixed-UTC-offset "zones" for the scenarios below. These are deliberately NOT
# real IANA zoneinfo objects (America/Los_Angeles, Europe/Berlin) — the offset
# itself is what matters for exercising the date-extraction conventions, and
# using a fixed offset keeps cases self-contained and independent of the host
# system's tzdata.
_PST = timezone(timedelta(hours=-8))  # Pacific Standard Time (winter)
_PDT = timezone(timedelta(hours=-7))  # Pacific Daylight Time (summer)
_CET = timezone(timedelta(hours=1))  # Central European Time (winter)

# Issue #906 verification round (Fix 4): a fixed-offset "zone" never
# transitions, so the DST cases above cannot distinguish a correct
# datetime.combine()+as_local() construction from a forbidden timedelta/
# now-relative one — both happen to produce identical output when the offset
# never changes. A real IANA zone can actually transition mid-scenario, which
# is what's needed to prove that hazard. Requires the `tzdata` package on
# platforms without a system tz database (see requirements_test.txt).
_LOS_ANGELES = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DateBoundaryCase:
    """One date/timezone boundary hazard scenario.

    Fields are raw ingredients, not a fixed call shape — different functions
    under test (``_build_extended_hourly_forecast()``, ``_synthesize_hourly_day()``,
    ``_get_forecast()``, ``_compute_day_hvac_modes()``) have different
    signatures, so each test builds its own call args from these fields rather
    than this module prescribing one.
    """

    label: str
    local_tz: timezone
    now: datetime  # tz-aware, already expressed in local_tz
    hourly_entry_utc: datetime  # a genuine hourly-forecast instant, UTC-aware
    hourly_expected_local_date: date  # dt_util.as_local(hourly_entry_utc).date()
    daily_entry_utc: datetime  # a daily-forecast UTC-midnight-style instant, UTC-aware
    daily_expected_raw_date: date  # daily_entry_utc.date() — raw, no conversion


DATE_BOUNDARY_CASES: list[DateBoundaryCase] = [
    # ── DST spring-forward (America/Los_Angeles, 2nd Sunday of March) ──────
    DateBoundaryCase(
        label="dst_spring_forward_before",
        local_tz=_PST,
        now=datetime(2026, 3, 8, 1, 30, 0, tzinfo=_PST),
        hourly_entry_utc=datetime(2026, 3, 8, 9, 30, 0, tzinfo=UTC),  # 01:30 PST
        hourly_expected_local_date=date(2026, 3, 8),
        daily_entry_utc=datetime(2026, 3, 9, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 3, 9),
    ),
    DateBoundaryCase(
        label="dst_spring_forward_after",
        local_tz=_PDT,
        now=datetime(2026, 3, 8, 3, 30, 0, tzinfo=_PDT),
        hourly_entry_utc=datetime(2026, 3, 8, 10, 30, 0, tzinfo=UTC),  # 03:30 PDT
        hourly_expected_local_date=date(2026, 3, 8),
        daily_entry_utc=datetime(2026, 3, 9, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 3, 9),
    ),
    # ── DST fall-back (America/Los_Angeles, 1st Sunday of November) ────────
    DateBoundaryCase(
        label="dst_fall_back_before",
        local_tz=_PDT,
        now=datetime(2026, 11, 1, 0, 30, 0, tzinfo=_PDT),
        hourly_entry_utc=datetime(2026, 11, 1, 7, 30, 0, tzinfo=UTC),  # 00:30 PDT
        hourly_expected_local_date=date(2026, 11, 1),
        daily_entry_utc=datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 11, 2),
    ),
    DateBoundaryCase(
        label="dst_fall_back_after",
        local_tz=_PST,
        now=datetime(2026, 11, 1, 3, 0, 0, tzinfo=_PST),
        hourly_entry_utc=datetime(2026, 11, 1, 11, 0, 0, tzinfo=UTC),  # 03:00 PST
        hourly_expected_local_date=date(2026, 11, 1),
        daily_entry_utc=datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 11, 2),
    ),
    # ── UTC-midnight rollover, negative-offset zone (PDT) — literal #190 ───
    DateBoundaryCase(
        label="utc_midnight_rollover_pdt_evening",
        local_tz=_PDT,
        now=datetime(2026, 5, 15, 19, 0, 0, tzinfo=_PDT),  # 7pm PDT; UTC already May 16
        hourly_entry_utc=datetime(2026, 5, 16, 2, 30, 0, tzinfo=UTC),  # 19:30 PDT May 15
        hourly_expected_local_date=date(2026, 5, 15),
        daily_entry_utc=datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 5, 16),
    ),
    DateBoundaryCase(
        label="utc_midnight_rollover_pdt_morning",
        local_tz=_PDT,
        now=datetime(2026, 5, 15, 6, 0, 0, tzinfo=_PDT),  # 6am PDT
        hourly_entry_utc=datetime(2026, 5, 15, 13, 30, 0, tzinfo=UTC),  # 06:30 PDT May 15
        hourly_expected_local_date=date(2026, 5, 15),
        daily_entry_utc=datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 5, 15),
    ),
    # ── Positive-offset zone (CET) — mirror-image check, not just "not PDT" ─
    # Local date has already rolled to May 16 while UTC is still May 15 —
    # the opposite lag direction from the PDT evening case above. The raw-date
    # convention for daily entries and the as_local() convention for hourly
    # entries must both still land on the correct (later) local day.
    DateBoundaryCase(
        label="utc_midnight_rollover_cet_mirror",
        local_tz=_CET,
        now=datetime(2026, 5, 16, 0, 30, 0, tzinfo=_CET),  # 00:30 CET May 16; UTC still May 15
        hourly_entry_utc=datetime(2026, 5, 15, 23, 30, 0, tzinfo=UTC),  # 00:30 CET May 16
        hourly_expected_local_date=date(2026, 5, 16),
        daily_entry_utc=datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 5, 16),
    ),
    # ── Year boundary (Dec 31 → Jan 1) ──────────────────────────────────────
    DateBoundaryCase(
        label="year_boundary",
        local_tz=_PST,
        now=datetime(2026, 12, 31, 20, 0, 0, tzinfo=_PST),
        hourly_entry_utc=datetime(2027, 1, 1, 5, 0, 0, tzinfo=UTC),  # 21:00 PST Dec 31
        hourly_expected_local_date=date(2026, 12, 31),
        daily_entry_utc=datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2027, 1, 1),
    ),
    # ── Month boundary (Jan 31 → Feb 1) ─────────────────────────────────────
    DateBoundaryCase(
        label="month_boundary",
        local_tz=_PST,
        now=datetime(2026, 1, 31, 22, 0, 0, tzinfo=_PST),
        hourly_entry_utc=datetime(2026, 2, 1, 5, 0, 0, tzinfo=UTC),  # 21:00 PST Jan 31
        hourly_expected_local_date=date(2026, 1, 31),
        daily_entry_utc=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 2, 1),
    ),
    # ── Leap day (Feb 29 of a leap year) ────────────────────────────────────
    DateBoundaryCase(
        label="leap_day",
        local_tz=_PST,
        now=datetime(2028, 2, 29, 12, 0, 0, tzinfo=_PST),
        hourly_entry_utc=datetime(2028, 2, 29, 20, 0, 0, tzinfo=UTC),  # 12:00 PST Feb 29
        hourly_expected_local_date=date(2028, 2, 29),
        daily_entry_utc=datetime(2028, 2, 29, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2028, 2, 29),
    ),
    # ── Real IANA zone spanning an actual DST transition (Fix 4) ───────────
    # America/Los_Angeles spring-forward, 2nd Sunday of March 2026 (Mar 8,
    # 2am -> 3am). Unlike the fixed-offset cases above, this zone's offset
    # genuinely changes mid-scenario, which is what makes it possible to
    # catch a `_synthesize_hourly_day()` reverted to timedelta/now-relative
    # arithmetic instead of a fresh `datetime.combine()+as_local()` call per
    # hour — see TestSynthesizeHourlyDay's dedicated DST test in
    # tests/test_forecast_extension.py for the actual mutation-proof check.
    DateBoundaryCase(
        label="dst_spring_forward_real_ianazone_los_angeles",
        local_tz=_LOS_ANGELES,
        now=datetime(2026, 3, 8, 1, 30, 0, tzinfo=_LOS_ANGELES),  # 01:30 PST, pre-transition
        hourly_entry_utc=datetime(2026, 3, 8, 9, 30, 0, tzinfo=UTC),  # 01:30 PST Mar 8
        hourly_expected_local_date=date(2026, 3, 8),
        daily_entry_utc=datetime(2026, 3, 9, 0, 0, 0, tzinfo=UTC),
        daily_expected_raw_date=date(2026, 3, 9),
    ),
]
