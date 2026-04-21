# Climate Advisor Simulation Report

Generated: 2026-04-20

---

## Summary

| State | Pass | Fail | Skip |
|-------|------|------|------|
| pending | 10 | 0 | 0 |
| golden | 2 | 0 | 0 |
| synthetic | 7 | 0 | 0 |


---

## PENDING Scenarios

### 2026-03-28-overnight-door-open [#69] ✅ PASS

**Description:** Door open all day (87F high); 555 min comfort violations; outdoor dropped to 53F overnight

**Verdict:** positive — Directional guard blocks nat vent when outdoor >= indoor; threshold-based exit and comfort-floor exit still fire correctly

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-03-28T06:00:00 | `paused` | — | outdoor or indoor temp unknown — defaulting to pause |
| 2026-03-28T06:00:00 | `paused` | — | outdoor 75.0F >= indoor 74.0F — opening windows would add heat |
| 2026-03-28T20:00:00 | `natural_ventilation` | — | outdoor 74.0F <= target 72.0F + delta 3.0F |
| 2026-03-28T23:30:00 | `nat_vent_comfort_floor_exit` | — | indoor 70.0F ≤ comfort_heat 70.0F — fan stopped, heat restored |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-03-28T06:00:00 | `paused` | `paused` | ✅ | outdoor 75F >= indoor 74F â€” directional guard blocks nat vent; enters pause instead |
| 2026-03-28T14:00:00 | `paused` | `paused` | ✅ | outdoor 87F > threshold 75F with sensors open â€” must exit nat vent, enter pause |
| 2026-03-28T20:00:00 | `natural_ventilation` | `natural_ventilation` | ✅ | outdoor 74F dropped back to <= threshold 75F while paused â€” re-activate nat vent |
| 2026-03-28T23:30:00 | `nat_vent_comfort_floor_exit` | `nat_vent_comfort_floor_exit` | ✅ | indoor 70F hit comfort_heat floor â€” nat vent exits and heat restores (Issue #99 behavior) |

### away-mode-classification-cycle [#85] ✅ PASS

**Description:** User leaves home; away setback applied; 30-min classification cycle fires; setback must be maintained (not overwritten with comfort)

**Verdict:** positive — Classification cycle while away maintains setback â€” does not restore comfort

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T08:00:00 | `classification_applied` | 75.0°F | daily classification — warm day, hvac_mode=cool |
| 2026-04-05T09:30:00 | `setback_applied` | 80.0°F | occupancy away — cool setback (base 80 - modifier 0.0) |
| 2026-04-05T10:00:00 | `setback_applied` | 80.0°F | occupancy away — cool setback (base 80 - modifier 0.0) |
| 2026-04-05T10:30:00 | `setback_applied` | 80.0°F | occupancy away — cool setback (base 80 - modifier 0.0) |
| 2026-04-05T14:00:00 | `comfort_restored` | 75.0°F | occupancy home — restoring cool comfort |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T08:00:00 | `classification_applied` | `classification_applied` | ✅ | initial classification at home â€” cool mode applied (comfort_cool=75F) |
| 2026-04-05T09:45:00 | `setback_applied` | `setback_applied` | ✅ | occupancy_change to away â€” setback_cool (80F) applied |
| 2026-04-05T10:00:00 | `setback_applied` | `setback_applied` | ✅ | classification while away â€” setback reapplied (80F), comfort NOT restored |
| 2026-04-05T10:30:00 | `setback_applied` | `setback_applied` | ✅ | another classification while away â€” setback maintained (80F) |
| 2026-04-05T14:00:00 | `comfort_restored` | `comfort_restored` | ✅ | occupancy_change to home â€” comfort_cool (75F) restored |

### nat-vent-at-comfort-floor-no-activate [#115] ✅ PASS

**Description:** Indoor at comfort_heat floor (70F); sensor opens â€” floor guard prevents activation even though outdoor is cooler

**Verdict:** positive — When indoor is already at comfort_heat, nat vent must not activate regardless of outdoor temp

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-20T23:00:00 | `paused` | — | indoor 70.0F <= comfort_heat 70.0F — too cold to vent |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-20T23:00:00 | `paused` | `paused` | ✅ | indoor 70F == comfort_heat 70F â€” floor guard blocks nat vent activation; pulling in 65F air would immediately violate comfort floor |

### nat-vent-evening-activation [#115] ✅ PASS

**Description:** Evening cool-down: outdoor 70F < indoor 76F â€” nat vent activates for free cooling

**Verdict:** positive — When outdoor is cooler than indoor and below threshold, nat vent activates

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-20T18:00:00 | `natural_ventilation` | — | outdoor 70.0F <= target 72.0F + delta 3.0F |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-20T18:00:00 | `natural_ventilation` | `natural_ventilation` | ✅ | outdoor 70F < indoor 76F (beneficial direction); outdoor 70F < threshold 75F; indoor 76F > comfort_heat 70F â€” nat vent activates |

### nat-vent-indoor-hot-outdoor-near-comfort [#115] ✅ PASS

**Description:** Hot interior (78F) with outdoor near comfort (74F) â€” activates since outdoor < indoor and outdoor < threshold

**Verdict:** positive — When indoor is above comfort_cool and outdoor is below both indoor and threshold, nat vent activates

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-20T17:00:00 | `natural_ventilation` | — | outdoor 74.0F <= target 72.0F + delta 3.0F |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-20T17:00:00 | `natural_ventilation` | `natural_ventilation` | ✅ | outdoor 74F < indoor 78F (beneficial); outdoor 74F < threshold 75F (within ceiling); indoor 78F > comfort_heat 70F â€” nat vent activates for hot-day free cooling |

### nat-vent-outdoor-rises-above-indoor-exit [#115] ✅ PASS

**Description:** Nat vent active; outdoor rises above indoor â€” new directional exit fires before threshold is crossed

**Verdict:** positive — When outdoor warms above indoor during active nat vent, exits to pause even if still below threshold

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-20T18:00:00 | `natural_ventilation` | — | outdoor 70.0F <= target 72.0F + delta 3.0F |
| 2026-04-20T20:00:00 | `nat_vent_outdoor_rise_exit` | — | outdoor 74.5F >= indoor 74.0F — airflow would add heat |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-20T18:00:00 | `natural_ventilation` | `natural_ventilation` | ✅ | outdoor 70F < indoor 76F and below threshold 75F â€” nat vent activates |
| 2026-04-20T20:00:00 | `nat_vent_outdoor_rise_exit` | `nat_vent_outdoor_rise_exit` | ✅ | outdoor 74.5F >= indoor 74.0F â€” directional exit fires; outdoor still below threshold 75F but airflow no longer beneficial |

### nat-vent-outdoor-warmer-no-activate [#115] ✅ PASS

**Description:** Sensor opens; outdoor 75F >= indoor 74F â€” directional guard prevents nat vent activation

**Verdict:** positive — When outdoor temp equals or exceeds indoor, nat vent must not activate

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-20T06:00:00 | `paused` | — | outdoor 75.0F >= indoor 74.0F — opening windows would add heat |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-20T06:00:00 | `paused` | `paused` | ✅ | outdoor 75F >= indoor 74F â€” pulling in warmer air; directional guard blocks nat vent |

### warm-day-setback-cool-mode [#96] ✅ PASS

**Description:** Warm day + thermostat in cool mode: setback_cool applied instead of HVAC off (Issue #96)

**Verdict:** positive — Warm-day classifier respects thermostat cool mode â€” applies setback instead of shutoff

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-10T07:30:00 | `setback_applied` | 82.0°F | warm-day setback: thermostat in cool mode — setback_cool applied (82.0°F) |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-10T07:30:00 | `setback_applied` | `setback_applied` | ✅ | warm day + thermostat in cool mode â€” setback_cool (82F) applied instead of HVAC off |

### warm-day-setback-heat-cool-mode [#96] ✅ PASS

**Description:** Warm day + thermostat in heat_cool mode: dual setpoints (setback_heat/setback_cool) applied instead of HVAC off (Issue #96)

**Verdict:** positive — Warm-day classifier respects thermostat heat_cool mode â€” applies dual setpoints instead of shutoff

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-10T07:30:00 | `dual_setback_applied` | 60.0°F | warm-day setback: thermostat in heat_cool mode — dual setpoints 60.0°F heat / 82.0°F cool |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-10T07:30:00 | `dual_setback_applied` | `dual_setback_applied` | ✅ | warm day + thermostat in heat_cool mode â€” dual setback: setback_heat (60F) low / setback_cool (82F) high; HVAC mode unchanged |

### warm-day-setback-heat-mode [#96] ✅ PASS

**Description:** Warm day + thermostat in heat mode: setback_heat applied instead of HVAC off (Issue #96)

**Verdict:** positive — Warm-day classifier respects thermostat heat mode â€” applies setback instead of shutoff

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-10T07:30:00 | `setback_applied` | 60.0°F | warm-day setback: thermostat in heat mode — setback_heat applied (60.0°F) |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-10T07:30:00 | `setback_applied` | `setback_applied` | ✅ | warm day + thermostat in heat mode â€” setback_heat (60F) applied instead of HVAC off |


---

## GOLDEN Scenarios

### nat-vent-comfort-floor-exit-restores-heat [#99] ✅ PASS

**Description:** Natural ventilation fan shuts off and heat restores when indoor temp drops to comfort_heat floor (Issue #99)

**Verdict:** positive — When _natural_vent_active=True and indoor drops to comfort_heat, fan deactivates and HVAC restores to heat without entering pause

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-11T08:00:00 | `classification_applied` | 70.0°F | daily classification — mild day, hvac_mode=heat |
| 2026-04-11T09:05:00 | `natural_ventilation` | — | outdoor 62.0F <= target 75.0F + delta 3.0F |
| 2026-04-11T11:00:00 | `nat_vent_comfort_floor_exit` | — | indoor 70.0F ≤ comfort_heat 70.0F — fan stopped, heat restored |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-11T09:05:00 | `natural_ventilation` | `natural_ventilation` | ✅ | door opened; outdoor 62F <= threshold 78F â€” nat vent activated |
| 2026-04-11T11:00:00 | `nat_vent_comfort_floor_exit` | `nat_vent_comfort_floor_exit` | ✅ | indoor 70F <= comfort_heat 70F â€” fan stops, heat restores, paused_by_door stays False |

### nat-vent-fan-preserved-on-thermostat-event [#95] ✅ PASS

**Description:** Natural ventilation fan stays active when thermostat emits state_changed with hvac_mode=off (Issue #95)

**Verdict:** positive — Verifies the stale-clear guard: fan_active must NOT be cleared when natural_vent_active=True, even when thermostat reports hvac_mode=off

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-09T14:00:00 | `natural_ventilation` | — | outdoor 61.0F <= target 75.0F + delta 3.0F |
| 2026-04-09T14:01:00 | `nat_vent_fan_preserved` | — | hvac_mode=off + fan_active=True + natural_vent_active=True — fan NOT cleared |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-09T14:00:00 | `natural_ventilation` | `natural_ventilation` | ✅ | sensor opened; outdoor 61F <= comfort_cool 75F + delta 3F = 78F threshold â€” nat vent mode |
| 2026-04-09T14:01:00 | `nat_vent_fan_preserved` | `nat_vent_fan_preserved` | ✅ | thermostat_state_changed with hvac_mode=off while natural_vent_active=True â€” stale-clear guard must prevent fan_active from being cleared |


---

## SYNTHETIC Scenarios

### bedtime-heat-setback-fan-off [#86] ✅ PASS

**Description:** Bedtime deactivates running fan and applies heat setback (comfort 70 - depth 4 = 66)

**Verdict:** positive — Verifies bedtime stops the fan and applies the default-depth heat setback

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 70.0°F | daily classification — cool day, hvac_mode=heat |
| 2026-04-05T10:00:00 | `fan_cycle_on` | — | fan min-runtime cycle on (min_runtime=10 min/hr) |
| 2026-04-05T22:30:00 | `fan_off` | — | bedtime — fan off for night |
| 2026-04-05T22:30:00 | `setback_applied` | 66.0°F | bedtime — heat setback (default depth, no thermal model) |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T10:00:00 | `fan_cycle_on` | `fan_cycle_on` | ✅ | fan min-runtime cycle activates fan |
| 2026-04-05T22:30:00 | `setback_applied` | `setback_applied` | ✅ | bedtime: comfort_heat(70) - DEFAULT_SETBACK_DEPTH_F(4) + modifier(0) = 66, clamped above setback_heat(60) |

### classification-cool-day-heat-on [#86] ✅ PASS

**Description:** Cool day classification applies heat mode and sets target to comfort_heat

**Verdict:** positive — Verifies classification event sets hvac_mode and comfort temp target

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 70.0°F | daily classification — cool day, hvac_mode=heat |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | `classification_applied` | ✅ | cool day â†’ heat mode â†’ target = comfort_heat = 70 |

### economizer-cool-down-phase [#86] ✅ PASS

**Description:** Hot day economizer check during evening window with indoor above comfort â€” enters cool-down phase

**Verdict:** positive — Verifies economizer_check engages cool-down phase when eligible on a hot day

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 75.0°F | daily classification — hot day, hvac_mode=cool |
| 2026-04-05T18:00:00 | `economizer_engaged` | 75.0°F | economizer cool-down — indoor=79.0 > comfort=75.0, outdoor=77.0 assisting |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T18:00:00 | `economizer_engaged` | `economizer_engaged` | ✅ | hot day, outdoor=77 <= threshold(75+3=78), windows open, hour=18 in evening window, indoor=79 > comfort=75 â†’ cool-down phase, target=comfort_cool=75 |

### fan-cycle-on-off [#86] ✅ PASS

**Description:** Fan min-runtime cycle: activates fan then deactivates it after the cycle phase completes

**Verdict:** positive — Verifies fan_cycle_on activates the fan and fan_cycle_off deactivates it

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T09:00:00 | `fan_cycle_on` | — | fan min-runtime cycle on (min_runtime=10 min/hr) |
| 2026-04-05T09:10:00 | `fan_cycle_off` | — | fan min-runtime cycle complete — fan off |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T09:00:00 | `fan_cycle_on` | `fan_cycle_on` | ✅ | fan_min_runtime_per_hour=10, fan_mode=hvac_fan â†’ fan activates |
| 2026-04-05T09:10:00 | `fan_cycle_off` | `fan_cycle_off` | ✅ | 10 minutes elapsed â†’ fan_cycle_off deactivates fan |

### occupancy-away-setback-heat [#86] ✅ PASS

**Description:** Away setback on a heat day: target = setback_heat + setback_modifier

**Verdict:** positive — Verifies occupancy_away applies heat setback temperature without mode change

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 70.0°F | daily classification — cool day, hvac_mode=heat |
| 2026-04-05T08:30:00 | `setback_applied` | 60.0°F | occupancy away — heat setback (base 60 + modifier 0.0) |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | `classification_applied` | ✅ | cool day â†’ heat mode applied |
| 2026-04-05T08:30:00 | `setback_applied` | `setback_applied` | ✅ | away setback: setback_heat(60) + modifier(0) = 60 |

### occupancy-vacation-deeper-setback [#86] ✅ PASS

**Description:** Vacation setback on a heat day: deeper than away (setback_heat + modifier - VACATION_EXTRA)

**Verdict:** positive — Verifies occupancy_vacation applies a deeper setback than occupancy_away

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 70.0°F | daily classification — cool day, hvac_mode=heat |
| 2026-04-05T09:00:00 | `setback_applied` | 57.0°F | vacation — deep heat setback (base 60 + modifier 0.0 - vacation 3.0) |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T09:00:00 | `setback_applied` | `setback_applied` | ✅ | vacation: setback_heat(60) + modifier(0) - VACATION_SETBACK_EXTRA(3) = 57 |

### wakeup-restores-comfort [#86] ✅ PASS

**Description:** Morning wakeup after bedtime setback restores comfort_heat target

**Verdict:** positive — Verifies wakeup restores comfort temperature after a bedtime setback

**Decision timeline:**

| Time | Outcome | Temp | Reason |
|------|---------|------|--------|
| 2026-04-05T07:00:00 | `classification_applied` | 70.0°F | daily classification — cool day, hvac_mode=heat |
| 2026-04-05T22:30:00 | `setback_applied` | 66.0°F | bedtime — heat setback (default depth, no thermal model) |
| 2026-04-06T06:30:00 | `comfort_restored` | 70.0°F | morning wake-up — restoring heat comfort |

**Assertions:**

| Time | Expected | Actual | Result | Reason |
|------|----------|--------|--------|--------|
| 2026-04-05T22:30:00 | `setback_applied` | `setback_applied` | ✅ | bedtime setback: comfort_heat(70) - depth(4) = 66 |
| 2026-04-06T06:30:00 | `comfort_restored` | `comfort_restored` | ✅ | wakeup restores comfort_heat = 70 |
