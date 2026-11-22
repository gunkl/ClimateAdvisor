**Version:** 0.8.60 (also checked main @ 0.9.34 — no equivalent option)
**Thermostat:** Nest Learning 3rd gen via unfr0zen (local MQTT), single-setpoint, `hvac_modes: [off, heat, cool, heat_cool]`
**Climate:** New England — cooling is never wanted from roughly November to April

### Problem
Climate Advisor determines what the thermostat can do solely from the climate entity's `hvac_modes`
(`parse_thermostat_capabilities()`). The comfort-edge selector (`decide_comfort_family`) then arms
cooling whenever indoor temperature enters the cool arming margin, or when the current family is
unknown and the fallback resolves to cooling. Outdoor temperature is never consulted, so CA arms
cooling on near-freezing days:

- **Nov 12, 00:20** — outdoor 7 °C. Bedtime band 18–22 °C, indoor 21.5 °C (≈0.5 °C under the sleep
  ceiling) → `thermostat_mode_set heat → cool` ("comfort range decision"). The Nest stayed in COOL
  until the 06:30 wake-up; `comfort_undertemp` incident at 06:35 (indoor 19.5 °C).
- **Nov 13, 09:54** — outdoor 3 °C, away band 17–26 °C, indoor 22 °C. The thermostat had just
  returned from an MQTT dropout (unavailable 09:50–10:01); family unknown → "in hysteresis gap —
  keeping current family" resolved to **cooling**. The Nest rejected it
  (`hvac_mode_not_applied: requested cool, actual heat`).

A heat→cool switch on a cold day with a single-setpoint thermostat means the house drifts down to
the cool setpoint unheated until something re-arms heat.

### Proposed solution
1. **Outdoor lockouts (options → setpoints/advanced):**
   - `cool_lockout_outdoor_temp` — never arm cooling while outdoor < X (default off; e.g. 16 °C)
   - `heat_lockout_outdoor_temp` — symmetric, never arm heating while outdoor > Y
   - Hysteresis (e.g. 2 °C) to avoid flapping around the threshold.
   Ideally applied as a capability mask inside `_get_thermostat_capabilities()`, so every path
   (selector, unknown-family fallback, ODE ceiling guard, restores, TOU) respects it the same way
   it already respects a genuinely heat-only thermostat.
2. **Optional "HVAC season" select entity** (Auto / Heat only / Cool only) exposed by CA, for users
   who prefer a manual seasonal switch.
3. Surface the lockout in Status and the briefing ("Cooling locked out — outdoor 3 °C"), and
   record an Activity Record event when it engages/releases.

