<!-- Nav: → [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Forecast Pipeline Spec

## Scope

Covers `_get_forecast()` in `coordinator.py`: how raw HA weather forecast data is fetched,
parsed, timezone-normalized, and returned as `(today_high, today_low, tomorrow_high, tomorrow_low)`
to the classifier and briefing.

## Anchors

| Question | Location |
|---|---|
| What datetime format does the HA weather API return? | [§ Datetime Format](#datetime-format) |
| How are today and tomorrow entries identified? | [§ Date-Keyed Matching](#date-keyed-matching) |
| What happens when today's forecast is missing from the API? | [§ Missing Today Handling](#missing-today-handling) |
| How is weather bias correction applied? | [§ Bias Correction](#bias-correction) |
| What timezone is used for date comparisons? | [§ Timezone Strategy](#timezone-strategy) |
| What is the fix history for this function? | [§ Known History](#known-history) |
| What happens when observed outdoor history overrides the provider's high/low by a lot? | [§ Observed-Extreme Override Warning](#observed-extreme-override-warning) |
| What if the provider's temperature unit differs from Climate Advisor's? | [§ Provider Unit Assumption](#provider-unit-assumption) |
| What happens while the weather entity is unavailable, and how is it retried? | [§ Weather Source Outage](#weather-source-outage) |
| Does the 30-minute cycle stop while the weather service is down (Issue #1337)? | No. The snapshot is built from the kept daily forecast and live readings, so the cycle runs as usual; with no snapshot at all the work that needs no forecast still runs. A failed retry changes nothing; outdoor is never the 65°F placeholder. [§ Weather Source Outage](#weather-source-outage) |
| Is every fetched hourly forecast kept for later error scoring (Issue #1230 R0)? | Yes, one snapshot per local clock hour, but only from a fetch that returned entries; the outage kept-forecast branch never records. [§ Forecast Archive](#forecast-archive-issue-1230-r0) |
| Is the forecast thrown away during an outage (Issue #1337)? | No. Only a fetch that returns entries replaces the hourly or daily forecast; an outage keeps the last good one (hourly: minus hours more than 1 h past) and every consumer keeps using it. The cosine model is used only for hours past its end. [§ Kept forecast](#kept-forecast-issue-1337) |

## Datetime Format

HA weather integrations vary in how they encode daily forecast datetimes:

- **UTC midnight**: `2026-05-16T00:00:00+00:00` — midnight UTC is 17:00 the *previous* local
  day (PDT = UTC-7). `dt_util.as_local()` shifts the date back by one calendar day.
- **Local noon or other offsets**: no shift problem; the date component is already correct.
- **Naive datetimes** (no `tzinfo`): treated as local time; `.date()` is used directly.

The integration does not control which format the configured weather provider uses. The
date-keyed dict approach (see below) handles all variants correctly.

## Date-Keyed Matching

`_get_forecast()` builds a `forecast_by_date: dict` using `setdefault()` — the first entry
for each raw date wins — then looks up today and tomorrow by local calendar date:

```python
today_fc = forecast_by_date.get(now_date)
tomorrow_fc = forecast_by_date.get(tomorrow_date)
```

`now_date = dt_util.now().date()` (local calendar date).
`tomorrow_date = now_date + timedelta(days=1)`.
Each entry's key: `fc_obj.date()` — raw date, no timezone conversion (see [§ Timezone Strategy](#timezone-strategy)).

If either lookup returns `None`, the corresponding temperature defaults to `current_outdoor`
(the value obtained from the live thermostat reading). The existing `if today_fc:` / `if
tomorrow_fc:` guards below the loop handle this gracefully.

No blind-index fallback exists. Array position carries no semantic meaning.

## Missing Today Handling

When `today_fc is None` after the dict build, a WARNING log records the available local dates:

```
_get_forecast: no entry for today (2026-05-15 local); available dates: [2026-05-16, 2026-05-17, ...]
```

This is normal for weather providers that exclude the current day from the daily forecast
once it is in progress. `today_high` / `today_low` fall back to `current_outdoor`, but
the `_outdoor_temp_history` override below can correct them once observed temperatures
accumulate during the day.

An INFO log also records the raw matched temperatures each cycle:

```
_get_forecast matched: today=2026-05-15 raw_temp=72, tomorrow=2026-05-16 raw_temp=79
```

A raw datetime snapshot (first 5 entries) is logged at DEBUG:

```
_get_forecast raw datetimes (first 5): ['2026-05-16T00:00:00+00:00', ...]
```

These three log lines are the primary diagnostic for any future date-matching problems.

## Bias Correction

After the forecast entries are extracted, weather bias (learned from comparing recent
forecast-vs-actual data) is applied to `tomorrow_high` and `tomorrow_low`:

- Applied only when `learning_enabled` and `weather_bias_enabled` are both `True`
- Bias is capped at `MAX_WEATHER_BIAS_APPLY_F` (8°F)
- Only applied when `|bias| >= MIN_WEATHER_BIAS_APPLY_F` (0.5°F)
- A positive `high_bias` means the forecast has been running high; the correction subtracts it

Bias is applied to **tomorrow** only, not today. Today's forecast is corrected by the
observed temperature history guard (`_outdoor_temp_history` max/min override).

## Observed-Extreme Override Warning

Today's high/low returned by `_get_forecast()` are overridden by the observed `_outdoor_temp_history` max/min when history is more extreme than the provider values. Since 0.7.83 (Issue #1015), when the override exceeds `_OBSERVED_EXTREME_DELTA_F` (20°F) a WARNING is logged:

```
Observed outdoor extreme overrides forecast: kind=<low|high> observed_ts=<ts> observed_value=<x.x> provider_value=<x.x> delta_f=<x.x>
```

It names the offending history sample (`observed_ts`, `observed_value`), the provider value and the delta. It is emitted once per distinct `(kind, observed_ts)` per day (the latch is cleared in `_async_end_of_day`), and only when the delta exceeds `_OBSERVED_EXTREME_DELTA_F = 20.0` °F. A legitimate large day/night swing can therefore trigger it once per new extreme sample. The sample and provider values in the line are internal °F (the history and the provider high/low are both held in °F at that point). **Returned values are unchanged** — this is observability only. A normal day or a large Fahrenheit swing under 20°F is silent. Typical causes: a unit mismatch/change polluting the history (see below), or a bad sensor reading. Use the sample ts to find the poll that introduced it.

## Provider Unit Assumption

`_get_forecast()` converts provider values using the *configured* `temp_unit`; it never reads the provider's unit for conversion. Right after reading the weather attributes it performs a one-shot check of the entity's `temperature_unit` attribute and logs `Weather unit mismatch: provider_unit=… configured_unit=…` on disagreement. See [temperature-conversion.md](temperature-conversion.md#provider-unit-must-equal-the-configured-unit-issue-1015).

## Weather Source Outage

While the weather entity is missing or `unavailable`/`unknown`, `_get_forecast()` builds the snapshot from the kept daily forecast and the live readings (`_kept_forecast_snapshot()`, `ForecastSnapshot.from_kept_forecast=True`; Issue #1337), so the 30-minute cycle runs as usual: day type, plan, prediction, ventilation checks, comfort tracking. It returns `None` only when no kept daily entry is for today or no real outdoor reading exists; the cycle then still runs everything that needs no forecast ([§ Cycle with no forecast](#cycle-with-no-forecast-issue-1337)). Only the "weather is back" bookkeeping waits for a real fetch: the retry reset, the "Weather entity now available" log and `_classification_fetched_at`.

**Retries.** The first failed cycle starts a backoff: 30 s, 60 s, 120 s, 240 s, 480 s (about 15 min), then the normal 30-minute cycle. Any successful cycle resets it, so every outage gets a fresh series. Issue #1329: only the scheduled retry consumes a retry. While a retry timer is pending (`_weather_retry_pending`), a refresh from anything else, such as a door/window event, neither uses one up nor schedules another timer. Before, a door opening 10 s into an outage turned "retry 5 in 30 s" into "retry 4 in 60 s", and the whole series collapsed into about 40 s. Retries keep running while the kept forecast covers the outage. Issue #1337: a retry refresh (`_weather_retry_refresh`, set by the retry timer) that still finds the service down runs nothing else. Re-running the plan on every 30-240 s retry re-sent Climate Advisor's commands so often that an occupant's thermostat change landing next to one read as its echo and was then undone.

**What stays live (Issue #1329).** Readings that do not come from the weather service are not blanked:
- **Dedicated outdoor sensor:** the dashboard's outdoor temperature and the chart log come from it (`outdoor_temp_source` sensor / input_number, `_live_dedicated_outdoor_temp()`), using a successful read only. They are never taken from the weather fallback or its placeholder.
- **Indoor:** the chart log's indoor value is the live thermostat/indoor reading.
- **Weather-service installs:** outdoor is interpolated from the kept hourly forecast (below), the same #511 interpolation used when the service is up (`_outage_outdoor_temp()`). It is unknown only once no kept entry is within 2 hours of now.
- **Never the 65°F placeholder (Issue #1337).** `_get_weather_outdoor_temp()` ends in `weather_attrs.get("temperature", 65)`, and an unavailable entity has no temperature, so every outdoor read during an outage used to get 65°F: the 5-minute refresh fed it to the engine (nat-vent and economizer decisions) and thermal learning sampled it. `_get_outdoor_temp(..., weather_down=True)` (from `_weather_attrs_and_down()`) never calls the weather fallback: a sensor install reads its sensor, then the kept forecast if the sensor fails too (recorded as `source_in_use="kept_forecast"`); a weather-service install reads the kept forecast. With neither, it returns None: the 5-minute refresh keeps the last value and learning skips the sample.
- **Outdoor history.** During an outage only a real sensor reading is recorded in `_outdoor_temp_history`; a value read off the kept forecast is not an observation and must not feed the observed high/low or weather-bias learning.

### Kept forecast (Issue #1337)

A failed or empty fetch never wipes forecast data Climate Advisor already has.

- **Hourly** (`_refresh_hourly_forecast()`, the single choke point for the 30-min cycle, the briefing and midnight): a fetch with entries replaces `_hourly_forecast_temps` (coordinator and engine copies). An empty or failed fetch keeps the previous list through `_unexpired_hourly_forecast()`, which drops entries more than 1 hour old (one point behind now stays for interpolation). The midnight reset no longer clears the list before refetching.
- **Daily** (`_get_forecast()` → `_build_forecast_snapshot()`): a daily response with entries replaces `_daily_forecast_full`. An empty one keeps the previous list and matches today/tomorrow from it by date, so a day it no longer covers falls back exactly as a missing day does ([§ Missing Today Handling](#missing-today-handling)). While the entity itself is down, the snapshot is built from the kept list (above).
- **Across restarts:** both lists are saved with the state (`kept_forecast`) and restored at start-up (hourly pruned of past hours, mirrored to the engine), so a restart in the middle of an outage still has them. Not restored after a unit change, like the other persisted readings.
- **Consumers** (outdoor interpolation, the ODE/ceiling-guard cache, nat-vent plan, Next Automation card, chart future series and multi-day extension) all read these lists, so they keep using real forecast hours. As the outage goes on the kept hourly list gets shorter, which each consumer handles the way it handles a provider with a short hourly horizon: the indoor prediction covers the hours that remain, and the chart's multi-day extension fills later days from the kept daily forecast (`_build_extended_hourly_forecast()`). The cosine model (`_cosine_outdoor_curve()`) is used only once no future hour is left.
- **Logs:** the outage start is one ERROR ("using the last forecast until it returns, the cosine model past its end"), then one INFO naming the kept forecast's end, then one WARNING if the outage outlasts it ("the last forecast has run out — using the cosine model"). A successful fetch logs the return at INFO and re-arms both.

### Cycle with no forecast (Issue #1337)

When there is no snapshot at all, `_run_weather_independent_cycle()` runs the cycle's work that needs none, on the saved day type and the live readings: first-cycle recovery after a restart (`_run_first_cycle_recovery()`), unit checks, today's `DailyRecord`, the startup coalesce (`_do_startup_coalesce()`, which ends the post-restart window that suppresses override detection, with or without a day type), the thermal-model refresh, time-of-use resolve/apply, stuck and orphaned grace clean-up, indoor history and Comfort Score minutes (`_track_indoor_comfort()`), and, with a real outdoor reading, the window-cooling and nat-vent re-checks; then it saves state. It does not recalculate the day type or re-apply the plan (the coalesce aside), since both need forecast data. Before #1337 all of this sat behind "did the weather service answer", although only the day type needs it: a restart during an outage kept override detection off until the weather returned, Comfort Score minutes were lost, and an outage across midnight left the new day without a record (#602's fix sat behind the same check).

Entity health announces the outage and its recovery: [entity-health-brief](entity-health-brief.md).

## Forecast Archive (Issue #1230 R0)

`_refresh_hourly_forecast()` is also the only place the hourly forecast is archived. After it installs a fetch that returned entries (the `if fetched:` branch) it calls `_record_forecast_archive(fetched)`, which drops any `synthetic` entries, parses the rest and records one snapshot of what the provider said (up to 72 h ahead, internal °F, no conversion) into `ForecastArchive`, saving through the executor only when the archive changed. The kept-forecast branch used during an outage never reaches the archive, so an outage leaves a gap rather than stale data stamped with a new issue time. The call is wrapped so an archive failure never breaks the refresh or the cycle; the forecast is installed on the coordinator and engine first. Contract, schema, retention and the R1 read path: [Forecast Archive Spec](forecast-archive-spec.md).

## Timezone Strategy

Date comparisons use **local calendar date for "now"** and **raw date from the forecast
datetime string (no timezone conversion) for each entry**:

- `dt_util.now().date()` — local calendar today (HA configured timezone)
- `fc_obj.date()` — raw date extracted from the API datetime string, no `astimezone()` conversion

**Why raw date, not local-converted or UTC-converted?**

Weather APIs that use UTC midnight timestamps (e.g. `2026-05-16T00:00:00+00:00`) intend the
date portion (`2026-05-16`) as the forecast date for calendar day May 16. Comparing that raw
date against the local calendar date is correct at all hours in all timezones.

Two problematic alternatives and why they fail:

1. **Local conversion** (`dt_util.as_local()`): `2026-05-16T00:00:00+00:00` → `2026-05-15T17:00-07:00`
   (PDT). Local date = May 15. The API's May 16 entry would be bucketed as today when the local
   date is May 15, producing an off-by-one (the pre-v0.3.44 bug).

2. **UTC calendar date for "now"** (`dt_util.utcnow().date()`): At 5pm PDT (= midnight UTC),
   UTC has already rolled to the next day. UTC "today" = local tomorrow. The API's local-tomorrow
   entry is labeled "today" and local day-after-tomorrow is labeled "tomorrow" (Issue #190 — the
   evening rollover bug introduced in v0.3.44).

The raw-date approach sidesteps both problems: the API's intended date is preserved exactly,
and "now" uses the local calendar date the user actually experiences.

## Known History

| Version | Change |
|---|---|
| v0.3.22 (Fix #107) | Changed forecast key from `'time'` to `'datetime'`; added `dt_util.as_local()` for timezone-aware parsing. Blind-index fallback retained. |
| v0.3.44 (Fix #143) | Replaced loop + fallback block with UTC-date-keyed dict. Removed all blind index assumptions. Switched from `dt_util.as_local()` to `astimezone(UTC)` for entry date extraction — fixes one-day off-by-one for UTC midnight forecast timestamps. Added WARNING logging for missing dates and INFO for matched raw temps. |
| v0.3.55 (Fix #190) | Replaced UTC-date approach with raw-date + local-now. `dt_util.utcnow()` → `dt_util.now()` for the reference date; `fc_obj.astimezone(UTC).date()` → `fc_obj.date()` for entry bucketing. Fixes evening UTC rollover: in UTC-7 timezones after 5pm, UTC had already rolled to the next day causing tomorrow's forecast to appear as today. |
| v0.7.83 (Fix #1015) | Added the observed-extreme override WARNING (>20°F) naming the history sample, and the one-shot weather-unit mismatch WARNING. Return values unchanged. |

### Why the fallback was wrong (Fix #143)

Before v0.3.44, the fallback block was:

```python
if today_fc is None and tomorrow_fc is None and len(forecast) >= 2:
    today_fc = forecast[0]
    tomorrow_fc = forecast[1]
elif today_fc is None and tomorrow_fc is not None and len(forecast) >= 1:
    today_fc = forecast[0]  # BUG: forecast[0] may be the same entry as tomorrow_fc
```

When the weather API returned a forecast array starting from tomorrow (no today entry), the
primary loop correctly set `tomorrow_fc = forecast[0]`. The second `elif` branch then blindly
set `today_fc = forecast[0]` — the identical object. Both `today_fc` and `tomorrow_fc` pointed
to the same entry (e.g., 2026-05-16 at 80°F). The real tomorrow's data (forecast[1], e.g.,
72°F) was unreachable. The briefing faithfully reported the corrupted value as "tomorrow's high."

The code comment at the top of the variable declarations already said *"never assume index ==
day"* — the fallback contradicted its own comment.
