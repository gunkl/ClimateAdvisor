# Setpoint Combination Warnings (Issue #1347)

## Anchors

| Question | Answer | Where |
|---|---|---|
| What limits does the Temperature Setpoints page put on a value? | Only a physical sanity range, the same for every field: 40–95 °F (4–35 °C, step 0.5). Typed into a number box. The old per-field ranges are gone, including the 65 °F / 18.5 °C cap on away heat. | `config_flow.setpoint_slider_ranges()`, `setpoint_combinations.SETPOINT_SANITY_*` |
| What happens when a combination is risky? | A warning appears next to the field and the form keeps what was typed. Submitting the identical values again saves them. Changing any value judges the form again. Nothing is blocked. | [Mechanism](#mechanism) |
| Which combinations warn? | See the [rules table](#rules). | `setpoint_combinations.setpoint_combination_findings()` |
| How is vacation derived? | The optional `vacation_heat`/`vacation_cool` (Options only) when set; otherwise away ∓ `VACATION_SETBACK_EXTRA` (3 °F). Each side falls back on its own. | `setpoint_combinations.vacation_edges()` (engine, scheduler, chart) |

## What the occupant experiences

A mini-split owner can keep the house warm while away (for example 20 °C, the #1347 report). They can also set away equal to home ("don't set back").

If a combination would make the heat and AC take turns, or make the house settle somewhere that doesn't make sense, the page says so in plain English. The occupant decides whether to keep it.

## Rules

All comparisons run in stored °F, with a tolerance of 0.05 °F. A °C value round-tripped through °F never reads as "warmer than" its own rounded display (#1049 history).

Each field shows one warning. When a field has several findings, the most severe wins, in this order:

1. inverted
2. narrow
3. outside the thermostat's limits
4. ordering
5. freeze

| Rule | Field warned | Key | Skipped when |
|---|---|---|---|
| Heat at or above cool in the comfort, away, sleep or (separate) vacation band | that band's heat field (`vacation_cool` if only it is set) | `setpoint_band_inverted_warning` | thermostat is heat-only or cool-only |
| Heat less than `MIN_HEAT_COOL_GAP_F` = 4 °F (2.5 °C in the text) below cool in a band | same | `setpoint_band_narrow_warning` | same |
| A setpoint outside the climate entity's `min_temp`/`max_temp` (read in HA's system unit) (Climate Advisor sends the thermostat's own limit instead, #1367) | that field | `setpoint_outside_thermostat_warning` | entity or attributes missing |
| Away heat above home heat | `setback_heat` | `away_heat_above_home_warning` | cool-only thermostat; equal is silent |
| Away cool below home cool | `setback_cool` | `away_cool_below_home_warning` | heat-only thermostat; equal is silent |
| Sleep heat below away heat | `sleep_heat` | `sleep_heat_below_away_warning` | cool-only |
| Sleep cool above away cool | `sleep_cool` | `sleep_cool_above_away_warning` | heat-only |
| Separate vacation heat above away heat | `vacation_heat` | `vacation_heat_above_away_warning` | cool-only; equal is silent |
| Separate vacation cool below away cool | `vacation_cool` | `vacation_cool_below_away_warning` | heat-only; equal is silent |
| Effective vacation heat below 45 °F / 7 °C (pipes) | `vacation_heat`, or `setback_heat` when vacation is derived | `vacation_freeze_warning` | cool-only |

**Deliberately silent** (controls):
- A sleep temperature warmer or cooler than home. A bedroom preference is not a fault.
- Every band rule on a heat-only or cool-only thermostat.
- An unavailable thermostat is judged as a two-mode system with no limits.

### Basis for the band thresholds

**Narrow, under 4 °F: owner decision, 2026-11-06.** The warning never blocks, so the threshold is deliberately conservative. It sits one degree above each of these:
- **The engine's own limit:** below 3 °F the comfort-family selector stops switching inside the band and only switches when the house goes past an edge (`effective_margin()` returns None under `3 × COMFORT_MIN_USABLE_MARGIN_F`).
- **Thermostat practice:** thermostats commonly enforce 2–3 °F between heat and cool in auto mode, with 3 °F a common default.
- **Real equipment:** compressors and furnaces run past their setpoint, and the learned swing defaults to 1.5 °F.

A test pins the threshold above the engine's limit.

**Inverted (heat at or above cool): shown in the harness.** The figures below come from the Tier-A harness ToyHouse, which models no equipment overshoot. They prove that inverted and equal bands fight, but they are **not** the basis for the narrow threshold: in the toy house, a 1 °F gap already looked normal.

Measured in the Tier-A generated-day harness (real production engine, ToyHouse), on 2026-11-06. Each setting ran on three seeds × two day types, with heat/AC flips counted per day.

| Comfort band | Heat↔cool flips per day |
|---|---|
| 68/74 (normal) | 0–3 |
| 71/72 (gap 1 °F) | 1–3 |
| 71.1/72 (gap 0.9 °F) | 3–6 |
| 71.5/72 (gap 0.5 °F) | 3–6 |
| 72/72 (equal) | 3–10 |
| 72.5/72 | 2–13 |
| 74/72 (inverted) | 8–40 |

The selector heats to the floor, which puts the house past the ceiling, so it cools, and the cycle repeats (`comfort_family_decision.py`). The settling guard only delays each switch.

No household promise flagged any of these days. That gap is tracked separately.

## Mechanism

This uses the #1144 two-pass pattern (the same one as `whf_no_sensors_warning`):
- `_setpoint_warnings()` reads the thermostat's `hvac_modes`, `min_temp` and `max_temp`, then calls the pure rules.
- If there are warnings, the step returns them in `errors` and remembers `{warnings, submitted values}` in `_setpoints_acknowledged`.
- An identical resubmit is accepted.

HA renders these in the error slot, so every text ends with "Submit again to keep this."

The two earlier blocking errors (`setback_must_be_lower`, `setback_must_be_higher`) are removed.

On the options page, a value left exactly as displayed keeps its stored °F, so re-saving a °C page never moves a setpoint by rounding.

The vacation fields use `suggested_value` (blank = derived) and are cleared through `clearable_keys`.

**Migration:** none. No new key is written for existing installs, and `VERSION` is unchanged.

## Code reference

- `custom_components/climate_advisor/setpoint_combinations.py`: rules, constants, `vacation_edges()`.
- `config_flow.py`: `setpoint_slider_ranges()`, `_setpoint_warnings()`, and both `async_step_setpoints()`.
- Consumers of `vacation_edges()`:
  - `automation.select_comfort_band()`
  - `scheduler.resolve_tou_away_vacation_phase()`
  - `coordinator._compute_target_band_schedule()`
- Tests:
  - `tests/test_setpoint_combinations_1347.py`
  - `tests_ha/test_setpoint_warnings_1347.py`
