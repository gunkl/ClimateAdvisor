Thanks for the review. Here is the information from the form, the data, and a narrowed request.

### Versions
- Climate Advisor **0.8.60** at the time of the incidents, **0.9.41** now
- Home Assistant **2026.11.2** (HA OS)
- Thermostat: Nest Learning 3rd gen via unfr0zen (local MQTT), single setpoint. hvac_modes advertised at the time: `[off, heat, cool, heat_cool]`
- Time zone: America/New_York (EST, UTC−05:00). All times below are local.

### What I experienced at home
- **Nov 12 (outdoor 6–8 °C all night):** the Nest switched to **cool** at 00:20 and stayed in cool, with no heating at all, until the 06:30 wake-up. We woke to **19.5 °C** against a 22 °C day comfort setpoint, about 2.5 °C cold. Once heat came back it took the furnace until **07:18** (48 min) to bring the house back to 22 °C. The 18 °C sleep floor wasn't breached that night, but only by luck: in cool mode nothing was protecting it.
- **Nov 13 (outdoor 3 °C, house set to Away):** no comfort impact. CA requested cool while the Nest was offline, and the Nest stayed in heat.
- **Nov 14 (outdoor 0 °C), not in the original post:** the Nest reconnected at 02:09:18.790 after an MQTT dropout, and CA switched it to **cool 41 ms later** (02:09:18.831). Indoor was 20.0 °C, mid-band in the 18–22 °C sleep band. It stayed in cool until 02:35, when my HA workaround automation (below) put it back to heat.

### What I expected
Heating stays armed whenever it is cold outside. On a single-setpoint thermostat, a switch to cool on a 0–7 °C night should not happen, whatever the indoor temperature is inside the band.

### Evidence or hypothesis
**Evidence:**
- HA recorder history for the Nest climate entity on the three nights, and hourly outdoor statistics for Nov 12 (attached CSVs).
- CA Activity Record entries quoted in the original post: Nov 12 00:20 `thermostat_mode_set heat → cool ("comfort range decision")`; Nov 13 09:54 `hvac_mode_not_applied: requested cool, actual heat`; Nov 12 06:35 `comfort_undertemp`.
- Current diagnostics (0.9.41) attached.

The recorder keeps 10 days, so Nov 12 before 04:20 has already been purged; the switch time comes from the entity's `last_changed`. CA's event log keeps 7 days, so the Nov 12–13 Activity Record rows are no longer in the diagnostics. `climate_advisor_chart_log.json` still covers these nights, and I can attach it if useful.


**Hypothesis (from reading the code, not confirmed by logs):** the code path. `decide_comfort_family` arms cooling at the cool arming margin (Nov 12). After the thermostat goes unavailable → available, the family is unknown, and the "in hysteresis gap — keeping current family" fallback resolves to cooling (Nov 13, Nov 14). Outdoor temperature is never consulted.

### Bug or feature
Both. Split as follows:
1. **Bug A (Nov 12):** cooling was armed 0.5 °C under the sleep ceiling during a heating night (outdoor 7 °C). I'd expect this to be wrong even without any lockout option.
2. **Bug B (Nov 13, Nov 14):** after an unavailable → available transition, the unknown-family fallback picks cooling. I'd expect it to keep the family that was active before the dropout, or to prefer heating when outdoor is below the comfort range.
3. **Feature, narrowed to one piece:** `cool_lockout_outdoor_temp` (default off) with about 2 °C hysteresis, applied as a capability mask in `_get_thermostat_capabilities()`, so every path treats cooling as unavailable. If outdoor is unavailable, keep the last lockout state; if it has never been known since startup, no lockout (today's behaviour). I'm dropping the HVAC season select and the Status/briefing/Activity Record surfacing from this request.

I can open Bugs A and B as separate issues if you'd prefer this one to be the feature only.

### Current workaround (relevant to the approach)
Since Nov 14, an HA automation republishes the Nest's MQTT discovery config with `modes: [off, heat]` when outdoor is below 16 °C (full list above 18 °C), then calls `climate_advisor.force_reclassify`. CA then treats the Nest as heat-only. Since then there has been **no heat → cool switch** (Nov 14 02:35 → today), so masking capabilities does work as a lockout. The catch is that it depends on rewriting the thermostat's discovery config, which unfr0zen overwrites on reconnect.

### What would confirm the fix
- With the workaround disabled and the lockout at 16 °C: from now through April, no `thermostat_mode_set → cool` in the Activity Record while outdoor < 16 °C. That includes after Nest MQTT dropouts, which are frequent here because there is no C-wire.
- A replay of Nov 12 00:20 (outdoor 7 °C, indoor 21.5 °C, sleep band 18–22 °C) keeps heat.
- A replay of Nov 14 02:09 (reconnect, outdoor 0 °C, indoor 20.0 °C mid-band) keeps heat.

[issue-1560-outdoor-hourly.csv](https://github.com/user-attachments/files/35243950/issue-1560-outdoor-hourly.csv)
[issue-1560-thermostat-history.csv](https://github.com/user-attachments/files/35243951/issue-1560-thermostat-history.csv)

[config_entry-climate_advisor-01M76M739ZS0YSJCGEGDTX85N4.json](https://github.com/user-attachments/files/35243974/config_entry-climate_advisor-01M76M739ZS0YSJCGEGDTX85N4.json)
