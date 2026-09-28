# Climate Advisor Display — Rebuild Specification

**Purpose of this document:** a hardware- and framework-agnostic spec for recreating the
"Climate Advisor Display" wall-mounted touchscreen panel from scratch — what the device must
do and what it integrates with, not the current implementation's code. Hand it to a developer
or an AI assistant along with "rebuild this device using [stack of choice], satisfying every
requirement below" — it should work even if the display hardware, touch controller, or
firmware framework (ESPHome/LVGL today) are swapped out entirely.

This document intentionally does **not** include the current YAML configuration itself — that
file is specific to this physical unit and is kept out of version control, not reproduced
here.

---

## 1. Reference hardware (current build)

The current device is built on an **ESP32-2432S028R "Cheap Yellow Display" (CYD)**,
Sunton/JCZN variant:

| Subsystem | Component | Notes |
|---|---|---|
| MCU | ESP32 (no PSRAM), 4MB flash | Memory-constrained — display buffer + ~20 concurrent HA entity subscriptions + WiFi all compete for heap |
| Display | 320×240 TFT, ILI9341 driver, SPI | Mounted landscape, USB connector to the right |
| Touch | XPT2046 resistive touch controller, dedicated SPI bus | Requires per-unit calibration — see §5 |
| Backlight | PWM-dimmable | Software-controllable brightness, not just on/off |
| Ambient light | Onboard LDR on an ADC-capable GPIO | **Reliability unconfirmed on this board revision — see §7 open items. Do not assume this subsystem works without live verification on the actual unit.** |

**Pin map** (for this specific board; any replacement hardware just needs equivalent
capabilities — a display SPI bus, a separate touch SPI bus, a PWM-capable backlight pin,
and optionally an ADC-capable pin for ambient light):

- Display SPI: CLK, MOSI, MISO, CS, DC pins (5 GPIOs)
- Touch SPI: separate CLK, MOSI, MISO, CS, IRQ pins (5 GPIOs, does not share the display's SPI bus)
- Backlight: 1 PWM-capable GPIO
- Ambient light: 1 ADC-capable GPIO

A new build on different hardware should confirm its own pinout from that board's
documentation rather than reusing these pin numbers.

---

## 2. Functional requirements (hardware/framework-agnostic)

These must hold regardless of what display library, MCU, or firmware framework is chosen.

### 2.1 Main status screen (default view)
- Always shown on boot/idle; no navigation needed to see it.
- Shows, at minimum:
  - Today's forecast high temperature
  - Current thermostat mode, current indoor temperature, and active setpoint
  - HVAC status (compact, single-line/pill style — no scrollable inner containers, no visible scrollbars)
  - Whole-house-fan (WHF) status (same compact style as HVAC)
  - A visible affordance ("tap anywhere for controls" or equivalent) indicating the menu is reachable
- Must not require entering a sub-menu to see any of the above.

### 2.2 Tabbed control menu (Contacts / HVAC / WHF)
- Reachable by tapping the main screen.
- Three tabs plus a Home/exit control, all reachable without scrolling the tab bar itself.
- Tab bar must render as a horizontal strip, and tab switching must not require re-rendering
  the whole page (should feel instant).

**Contacts tab:**
- Lists door/window/contact sensors, but shows **only the ones currently open** — closed
  sensors are omitted entirely (not grayed out), so the list stays compact.
- Each open sensor's row must show its true assigned **Home Assistant Area**, not a name
  guessed from the entity ID — entity IDs are not reliable proxies for physical location and
  have been confirmed to diverge from the real Area in multiple cases on this system. Area
  assignment must be sourced from Home Assistant's Area Registry (e.g. a `area_name()`
  template query), never inferred.
- Where more than one sensor shares the same Area, each row needs a disambiguating detail
  label (which specific sensor within that area triggered).
- Must show correct state within a few seconds of boot — a single check right after connecting
  is not reliable. **Found on this build:** the framework's "state changed" event doesn't fire
  for the first value pushed after connecting (only real subsequent transitions), so a
  one-shot check can capture stale defaults (e.g. "all closed") indefinitely. Fix: poll every
  sensor's current value directly (not wait for an event), repeatedly, for ~30s after boot
  (every 2s, ~15 attempts) — the many concurrent subscriptions don't all deliver at once.
  Skip this only if the chosen framework guarantees a reliable initial-value event.
- If nothing is open, show an explicit "all clear" indicator rather than an empty list.

**HVAC tab:**
- Shows current mode, current temperature, and setpoint.
- Provides controls to change mode and adjust setpoint.
- Control actions must actually reach Home Assistant — confirm the device is authorized to
  call HA services (an HA-side per-device trust setting, not a firmware concern, but a
  prerequisite for this tab to work at all). **Found on this build:** every button press
  (HVAC and WHF alike) silently did nothing, no firmware-side error, until the HA core log
  showed an explicit "rejected; if you trust this device..." message. Fix was HA-side only:
  enable "allow this device to perform Home Assistant actions" for the device. Lesson: if a
  control silently no-ops despite the firmware clearly sending the command, check the HA core
  log for a rejection before suspecting the firmware.

**WHF (whole-house fan) tab:**
- Off / Low / High controls.
- Button presses must feel instant: update the displayed state optimistically on tap, rather
  than waiting for the real HVAC/fan hardware to confirm. Real whole-house fan units can have
  a mechanical damper that delays the power-sensing ground-truth signal by 10–20+ seconds
  after a speed change — the UI must not flicker back to a stale state during that window.
- After a bounded delay (long enough to clear the above physical settling time, e.g. 30–60s),
  reconcile the displayed state to the real ground-truth sensor in case the optimistic guess
  didn't match what actually happened.
- Ground truth (a real power-sensing signal) must always win over a "last commanded speed"
  value alone — if the fan is turned off at the physical unit/remote rather than through this
  display, the display must eventually reflect that, not show a stale commanded state forever.
- **A settle/guard window is required on every path that can push a ground-truth update, not
  just the delayed reconciliation timer.** Found on this build: the ground-truth sensor's own
  change-event fires immediately on any real transition, including transitional/stale readings
  during the fan's physical startup — this bypassed the delayed reconciliation and caused
  visible flicker (tapped state → flips back to old state after a couple seconds → settles
  correctly later). Fix: one shared "settle until" deadline, set on every button press, checked
  at the top of *every* path that could write a ground-truth value to the display, not just the
  delayed timer.
- **Found calibration values (this fan has a mechanical damper):** 6 seconds was too short —
  damper-open/motor-ramp can take 10–20+ seconds, so a 6s window let a transitional "off"-look
  through before the fan reached its commanded speed. 20s settle + 45s delayed reconciliation
  resolved it. Treat these as a starting point tied to *this* fan's mechanical delay, not a
  universal constant — increase the window if flicker is observed on different hardware.

### 2.3 Auto-brightness + idle dimming
- If an ambient light sensor is present and verified working: bright ambient conditions →
  display at its brightest; dark ambient conditions → display at its lowest usable brightness
  setting plus a small margin (not literally 0%, to remain readable/locatable in the dark).
  **Do not ship this behavior against unverified sensor hardware** — see §7.
- **Found on this build:** 10% PWM was visually indistinguishable from fully off on this
  panel — too small a margin is functionally the same failure as 0%. 20% was the working
  value. PWM-duty-cycle-to-perceived-brightness is nonlinear and panel-specific, so verify
  against the real hardware rather than picking a percentage that sounds reasonable on paper.
- If the touchscreen has not been touched for 2 minutes, dim to roughly half of whatever the
  current (ambient-appropriate) target brightness is.
- Any touch immediately wakes the display back to full (non-dimmed) brightness and resets the
  idle timer.

### 2.4 Boot-safety guards
- Any control that calls a Home Assistant service must be gated behind a "ready" flag that
  only becomes true a few seconds after boot, once the API connection has actually finished
  its handshake — an early tap right after flashing/reconnecting must not silently fail or
  crash.
- Any startup logic that manipulates on-screen widgets must not run before those widgets
  actually exist in memory — sequence UI-dependent startup logic after the UI framework
  confirms its widget tree is built, not just after a fixed delay guess.

---

## 3. Home Assistant integration surface

The device is a client of an existing Home Assistant instance running the **Climate
Advisor** custom integration plus standard HA entities. It needs read (and in some cases
write) access to:

| Role | Cardinality | Access needed | Notes |
|---|---|---|---|
| Primary thermostat | 1 `climate.*` entity | Read (mode, current temp, setpoint) + write (mode, setpoint) | |
| Forecast high temperature | 1 sensor | Read | |
| Door/window/contact sensors | Many (15 in the current build) | Read | Each needs its true HA Area resolved (Area Registry, not entity name) |
| WHF ground-truth power state | 1 sensor/binary_sensor | Read | Physical power-sensing plug — authoritative over commanded state |
| WHF commanded speed | 1 sensor | Read | Last-commanded value; secondary to ground truth |
| WHF off control | 1 switch/service | Write | |
| WHF low/high control | 1 service call (e.g. fan percentage) | Write | |

The device must be explicitly authorized (via whatever the chosen integration protocol's
device-trust mechanism is) to invoke HA services — without that, all write-side controls
silently do nothing.

---

## 4. Design decisions worth carrying forward (with rationale)

These are lessons learned building the current device — a rebuild should either keep them
or consciously re-evaluate them, not silently drop them:

- **Prefer manual UI composition over composite "smart" widgets** (e.g. a tabbed-view widget)
  if the framework's composite widgets show rendering anomalies under the rotation mode in use.
  A composite tab widget was suspected as the cause of a sideways-rendering bug here and
  replaced with manual buttons + show/hide panels — the bug later turned out to be the touch
  transform (§5), not the widget. Treat "avoid composite widgets" as a fallback technique, not
  a proven-necessary constraint; re-verify against the actual framework before assuming it's
  needed.
- **Optimistic UI + delayed reconciliation is the right pattern for any control whose real
  ground-truth confirmation is slow or physically delayed** (see WHF, §2.2) — don't gate
  visible feedback on round-trip confirmation from slow physical hardware.
- **Never guess a physical device's Area/location from its entity ID or friendly name** —
  resolve it from the authoritative registry data. **Found on this build:** HA's basic
  state-lookup endpoint doesn't expose Area Registry data at all; the correct source is the
  template-rendering endpoint's `area_name(entity_id)` function (see A.10). An empty result
  from the state-lookup endpoint means it's the wrong endpoint, not that the data is missing.
- **Container/panel widgets holding only absolutely-positioned children should have scrolling
  and default padding explicitly disabled** if the framework enables them by default — an
  unwanted inner scrollbar and asymmetric spacing (extra space above content but not below) on
  this build's status pills was caused by exactly this default; that asymmetry is a useful
  diagnostic signature for the same bug elsewhere.
- **On a memory-constrained MCU running both a graphics framework and many concurrent network
  subscriptions, add continuous free-heap logging from the start** (see A.12), not only once a
  crash is already suspected — it gives a baseline to distinguish a future memory-pressure
  regression from a logic bug without retrofitting instrumentation mid-investigation.

---

## 5. Touch/display calibration — must be re-derived per unit, not assumed

Whatever touch technology is used, its coordinate transform (axis swap, mirroring, rotation)
relative to the display's own rotation setting is **specific to that hardware/orientation
combination** and must be confirmed empirically:

1. Boot with a debug log line that prints raw and calibrated touch coordinates on every touch.
2. Physically tap all four corners of the mounted, oriented display.
3. Compare the logged coordinates against the corner actually tapped.
4. Only lock in the transform settings once all four corners confirm correctly — do not
   trust a datasheet's stated default orientation, and do not guess-and-check blindly (each
   blind guess on the current build made things worse, including one that inverted the whole
   display). If a first attempt is wrong, go back to raw coordinate logging rather than
   trying another guessed transform.

**Found on this build** (ILI9341 + XPT2046, landscape/USB-right mount, 90° hardware rotation):
the working transform was axis-swap disabled, X mirrored, Y not mirrored — the *opposite* of
what seemed intuitively correct, and only found by cross-referencing a community reference
config for the same controller/driver/color-order/SPI-speed combination after blind
trial-and-error made things worse. Treat this as reference data for this exact panel/mount
orientation, not a value to copy onto different hardware — re-derive via the corner-tap
process for any other combination. Also note: a "tab bar renders as a vertical strip instead
of horizontal" bug that looked like a display-rendering problem was actually this same
touch-transform bug — when a "wrong side of the screen" symptom appears, check the touch
transform before suspecting the graphics layer or a specific widget.

---

## 6. Security / operational constraints (non-negotiable)

- **The device's live configuration file is unit-specific and is never committed to git or
  reproduced in this or any other tracked document.** The current build enforces this
  structurally, not just by convention.
- No AI-attribution lines in any commits or PRs touching this repo, consistent with the rest
  of the project's git workflow rules.

---

## 7. Open items — do not treat as resolved

- **Touch transform values are unit-specific** (§5) — any rebuild on new physical hardware
  needs its own corner-tap calibration pass; values from a previous unit/orientation must not
  be copied over.

## 8. Resolved findings (kept for rebuild reference)

- **Ambient light sensor (§2.3) hardware fix, confirmed.** The onboard LDR's stock voltage
  divider (LDR to GND, 1MΩ pull-up labeled R15, plus a 1MΩ resistor labeled R19 in parallel to
  GND) was too high-impedance for the ESP32 ADC to sample accurately — flat, unchanging ~0.14V
  regardless of light. Fixed with a physical board mod: **removed R19**, **replaced R15 with
  47kΩ**. Live diagnostic logging (raw voltage per sample, no smoothing) is what confirmed both
  the failure and the fix — never trust a plausible-sounding theory about a symptom without
  observing the actual unit's response.

  Post-fix measurements: **0.33V bright room**, **0.80V dark room** (more light → lower
  voltage). Firmware threshold: **0.565V**, the midpoint — not a guess. A rebuild reusing an
  LDR-based sensor should expect to need the same 47kΩ mod and measure-first calibration; don't
  assume the stock 1MΩ/1MΩ values work without verification.

---

## Appendix A — Full Build Blueprint

Everything below is detailed enough to recreate nearly the entire current build — every
pin, timer, screen, widget, and piece of control logic — without being literal YAML, so it
stays usable even if the target framework/library is not ESPHome+LVGL. Tables and pseudocode
describe structure and behavior; adapt the syntax to whatever stack is chosen. Home Assistant
entity references use generic placeholders (`<domain>.<placeholder_name>`) that describe the
*role* an entity must fill, since real entity identifiers are specific to this live system
rather than to the device design itself.

### A.1 Peripheral / pin map (reference build)

| Function | Role | Pin (this build) | Notes |
|---|---|---|---|
| Display SPI clock | Shared display bus | GPIO14 | |
| Display SPI MOSI | Shared display bus | GPIO13 | |
| Display SPI MISO | Shared display bus | GPIO12 | |
| Display chip-select | Display bus | GPIO15 | |
| Display data/command | Display control | GPIO2 | |
| Touch SPI clock | Separate touch bus | GPIO25 | Must be its own bus, not shared with the display |
| Touch SPI MOSI | Separate touch bus | GPIO32 | |
| Touch SPI MISO | Separate touch bus | GPIO39 | |
| Touch chip-select | Touch bus | GPIO33 | |
| Touch interrupt | Touch bus | GPIO36 | |
| Backlight PWM output | LEDC/PWM channel | GPIO21 | Must support variable duty cycle, not just on/off |
| Ambient light ADC input | ADC1-capable pin | GPIO34 | See §8 hardware-mod note — stock divider on this board needed a 47kΩ pull-up swap to be usable |

A rebuild on different hardware should pull its own pinout from that board's documentation;
the roles in the "Role" column are the actual requirement, not these specific GPIO numbers.

### A.2 Display & touch driver configuration

| Setting | Value (this build) | Notes |
|---|---|---|
| Panel resolution | 320 × 240 | Landscape |
| Color depth | 16-bit | |
| Display controller | ILI9341-family SPI TFT | |
| Rotation mode | Hardware rotation via display driver, 90° | Mounted landscape, USB connector to the right |
| Graphics framebuffer/partial-buffer size | ~10% of full frame | Full framebuffer likely won't fit given the MCU's memory constraints (§4 heap note) |
| Touch controller | XPT2046 resistive, own SPI bus | |
| Touch raw calibration bounds | x: 150–3800, y: 150–3800 | Per-unit resistive-panel calibration; re-derive for a different physical touch panel |
| Touch transform (found working, this unit/orientation) | axis-swap: off, mirror X: on, mirror Y: off | See §5 — re-derive via 4-corner tap test for any other unit/orientation, do not copy |
| Idle screen-return timeout | 30 seconds | Any screen other than the main status screen auto-returns to it after 30s of no touch, with a ~300ms fade transition |

### A.3 Theme / color tokens

A small fixed palette, reused everywhere rather than hardcoding colors per-widget:

| Token | Approx. color | Used for |
|---|---|---|
| `bg` | very dark navy | Page backgrounds |
| `card_bg` | dark slate blue | Card/panel backgrounds, inactive button fill |
| `primary` | medium blue | Active/selected button fill, headings |
| `text` | light gray | Primary text |
| `text_dim` | mid gray | Secondary text |
| `text_faint` | darker gray | Tertiary/hint text |
| `open` | red | Contact-sensor "open" indication |
| `closed` | green | Contact-sensor "closed"/all-clear indication |
| `hvac_heat` | red-orange | Heat mode active-button fill |
| `hvac_cool` | blue | Cool mode active-button fill |
| `daytype_hot` … `daytype_cold` | red → orange → green → blue → indigo (5-step scale) | Day-type badge color, matched to the Climate Advisor dashboard's own hot/warm/mild/cool/cold badge colors for visual consistency across surfaces |

### A.4 Global state (persists across the running session, not across reboot)

Every variable below deliberately does **not** persist across a reboot/reflash — this is
intentional, not an oversight. Several are meaningless or actively dangerous if carried over:
a millis()-since-boot deadline like `whf_settle_until` refers to a clock that resets on reboot,
so persisting its old value would misbehave immediately; `ui_ready` must always start `false`
so the boot-safety gate (A.6) actually re-runs. If the chosen framework persists globals by
default, turn that off for all of these.

| Name | Type | Initial value | Purpose |
|---|---|---|---|
| `hvac_last_mode` | string | `"heat_cool"` | Remembers the mode to restore to when HVAC is turned back on from Off |
| `ui_ready` | bool | false | Gates every HA-service-calling control until the API connection has finished its handshake (see A.6, boot sequence) |
| `boot_refresh_count` | int | 0 | Counts repeated boot-time contact/WHF display refreshes (see A.6) |
| `hvac_setpoint_value` | float | a safe default in °F | Cache of the last-known real setpoint, used so +/- buttons have a value to increment from even before a fresh reading arrives |
| `whf_settle_until` | timestamp (ms since boot) | 0 | Deadline before which ground-truth WHF display updates are suppressed (optimistic-UI settle window) |
| `ambient_target_brightness` | float, 0–1 | 1.0 | Brightness level implied by the current ambient reading, before idle-dim is applied |
| `last_touch_millis` | timestamp (ms since boot) | 0 | Last time the touchscreen was touched; drives the idle-dim timeout |

### A.5 Timers / periodic logic

| Cadence | Guard condition | Action | Purpose |
|---|---|---|---|
| Once, ~5s after boot | `ui_ready` is still false | Set `ui_ready = true`; kick off the contacts auto-scroll loop | Delays enabling any HA-service-calling control until the API connection has had time to finish its handshake; also the safe point to start UI-dependent animation logic, since it's guaranteed the widget tree exists by then |
| Every 2s, up to 15 times | `boot_refresh_count < 15` | Increment counter; re-run the contacts-display refresh and the WHF-display refresh | Covers the ~30s window after boot during which not all subscribed entities have necessarily delivered their first real value yet (see A.10 and §2.2 findings) |
| Every 10s, indefinite | none | Re-run the backlight-apply logic | The only thing that re-checks the idle-dim timeout on its own; ambient-light updates and touches are event-driven, not polled, so without this the display would never transition into the dimmed state |

### A.6 Reusable behavior scripts

| Name | Triggered by | Behavior (pseudocode) |
|---|---|---|
| `apply_backlight` | Ambient-light sensor update, any touch, and the 10s poll (A.5) | `idle = (now - last_touch_millis) > 120000`; `level = ambient_target_brightness * (idle ? 0.5 : 1.0)`; set backlight to `level` with a short (~500ms) fade transition |
| `contacts_auto_scroll` | Once at boot-ready, then re-schedules itself | Animate the contacts list's scroll position to the bottom over ~4.5s → pause 5s → animate back to the top over ~4.5s → pause 5s → repeat indefinitely. Replaces a manual drag-scrollbar, which is fiddly on a small touch panel. Must no-op safely if the target widget doesn't exist yet (guards against running before the UI framework finishes building its widget tree) |
| `update_whf_display` | WHF ground-truth power sensor changing state, WHF commanded-speed sensor changing state, the boot-refresh timer, and the delayed-resync script below | If `now < whf_settle_until`, do nothing (this is the shared guard — see §2.2 finding). Otherwise: read the ground-truth power-detect signal and the last-commanded speed; derive a single "Off / Low / High / On" label prioritizing ground truth over the commanded-speed value; update the status label and the three WHF button highlight colors to match |
| `whf_resync_after_delay` | Every WHF button press (restarts on each press rather than stacking) | Wait 45 seconds, then re-run `update_whf_display` — the fallback correction if the optimistic guess didn't match what actually happened |
| `update_contacts_display` | Every contact sensor's state-change event, plus the boot-refresh timer | Walk the full list of contact-sensor rows; for each one currently open, reveal it, position it in the next vertical slot, and set its Area + disambiguating-detail text; for each one closed, hide its row entirely (not grayed out — removed from layout so the list stays compact with no gaps). If nothing is open, show an explicit "all clear" label instead |

### A.7 Screen 1 — Main status page (default view)

Full-canvas layout, top-left origin, in the panel's native 320×240 rotated coordinate space:

| Element | Position (x, y) | Size | Content / behavior |
|---|---|---|---|
| App title label | (10, 8) | auto | Static app name, large font, `primary` color |
| Day-type badge | (10, 36) | 85w, right-aligned | Dynamic: uppercased day-type word (e.g. hot/warm/mild/cool/cold), colored per A.3's day-type scale |
| Forecast-high label | (100, 36) | auto | Dynamic: "Today's High: NN°F" |
| HVAC status card (panel) | (10, 55) | 300×65 | Rounded-corner card, `card_bg` fill, **zero internal padding, scrolling disabled** (see §4 finding — a default-scrollable/padded container here caused an unwanted inner scrollbar) |
| — HVAC status line (inside card) | (12, 8) relative | auto | Dynamic: "HVAC: Off / Heat / Cool" |
| — HVAC current-temp line (inside card) | (12, 38) relative | auto | Dynamic: "NN°F" |
| — HVAC setpoint line (inside card) | (150, 38) relative | auto | Dynamic: "Set: NN°F" |
| WHF status card (panel) | (10, 130) | 300×50 | Same zero-padding/non-scrollable treatment as the HVAC card |
| — WHF status line (inside card) | (12, 14) relative | auto | Dynamic: "WHF: Off / Low / High / On" |
| Next-action label | (10, 188) | 300w | Dynamic: a single reformatted line summarizing the system's next recommended action (e.g. a window open/close time) — see A.10 for the text-cleanup behavior this needs |
| "Tap anywhere for controls" hint | (65, 212) | auto | Static, faint/tertiary text color |
| Full-screen invisible tap target | (0, 0) | 320×240, fully transparent | Must be the **last** widget added (so it sits on top of everything else); tapping anywhere navigates to the menu screen with a ~200ms fade |

### A.8 Screen 2 — Menu page (Contacts / HVAC / WHF tabs)

**Design note (see §4):** this is deliberately built as manually-managed buttons + manually
shown/hidden content panels, not a single composite "tabbed view" widget, because such a
widget mis-rendered under this build's rotation mode. Re-verify against the actual chosen
framework before assuming a composite tab widget needs to be avoided — it may render fine.

**Tab bar** (3 buttons + a fixed exit button, all in one horizontal row at y = 0, height 36):

| Button | Behavior on tap |
|---|---|
| "Contacts" | Show the Contacts panel, hide the other two; set this button's fill to `primary`, the other two tab buttons' fill to `card_bg` |
| "HVAC" | Same pattern, showing the HVAC panel |
| "WHF" | Same pattern, showing the WHF panel |
| Exit (rightmost, no label / icon-only) | Navigate back to the main status screen with a ~200ms fade — always visible regardless of which tab is active |

Only one of the three content panels is visible at a time; all three occupy the same
position/size region (full width minus the exit button's width, height = screen height minus
the tab bar's height), directly below the tab bar.

**A.8.1 Contacts panel**

- Static header label: "Contacts that are open".
- Below it, a vertically-scrollable inner container (scrollbar hidden — scrolling is done via
  the auto-scroll script in A.6, not manual drag) holding one row per known contact sensor.
- Row schema (repeated per sensor, generic — the real build has 15): two labels per row, one
  showing the sensor's true Home Assistant **Area** (left-aligned) and one showing a
  disambiguating **detail** string (right-aligned) for cases where more than one sensor shares
  an Area. Rows for closed sensors are hidden entirely; visible rows are packed top-to-bottom
  with no gaps (~22px row height) rather than reserving fixed slots for every sensor.
- Fallback "All contacts closed" label, shown only when zero rows are visible.

**A.8.2 HVAC panel**

| Element | Position (x, y) | Size | Behavior |
|---|---|---|---|
| "Mode" label | (10, 8) | auto | Static |
| Off / Heat / Cool buttons | y = 30, x staggered across 3 equal columns | 75×44 each | Tapping Off turns the mode off (remembering the prior mode to restore later); tapping Heat/Cool calls the mode-set service directly. Each button's fill highlights when it matches the live mode |
| "Temperature" label | (10, 90) | auto | Static |
| "−" button | (10, 115) | 60×50 | Decrements the cached setpoint by 1°F (clamped to a safe floor), sends the new setpoint |
| Setpoint display | (110, 128) | auto, large font | Dynamic: "NN°F" |
| "+" button | (210, 115) | 60×50 | Increments by 1°F (clamped to a safe ceiling), sends the new setpoint |

All three mode buttons and both temperature buttons must be gated behind the boot-ready flag
(A.5/A.6) — an early tap before the API handshake completes must be a no-op, not a crash or a
silently-dropped/erroring call.

**A.8.3 WHF panel**

| Element | Position (x, y) | Size | Behavior |
|---|---|---|---|
| "Whole House Fan" label | (10, 8) | auto | Static |
| Off / Low / High buttons | y = 30, x staggered across 3 equal columns | 75×44 each | See optimistic-UI pseudocode below |

Each button's `on_click`, gated behind the boot-ready flag, does the following in order:
1. Set the shared settle-window deadline to now + 20 seconds.
2. Immediately update the status label and all three buttons' highlight colors to reflect the
   tapped state (optimistic — before any real confirmation).
3. Send the real command (turn off / set to ~50% / set to ~100%, per button).
4. (Re)start the 45-second delayed-reconciliation script (A.6's `whf_resync_after_delay`),
   which re-syncs the display to ground truth once the physical fan has had time to settle.

**A.8.4 Layout constants** — the tab content width, the three tab-button x-positions, and the
exit button's width are all derived from one shared "content width" value, so retuning the
overall proportions only requires changing that single value rather than every widget's
position individually.

### A.9 Ambient light & backlight subsystem

| Setting | Value | Notes |
|---|---|---|
| Sensor input | ADC pin, ~12dB attenuation (full 0–~3.3V range) | See A.1/§8 for the hardware-mod prerequisite |
| Sampling | Every 5s, smoothed over a 6-sample moving average, emitting once per 6 samples | Reduces noise/flicker in the derived brightness target; use faster/unfiltered sampling only for one-off diagnostic calibration (§8), not steady-state operation |
| Dark/bright threshold | 0.565V (measured-midpoint value for this unit — see §8) | Voltage **above** this = dark, **below** = bright, for this specific divider topology (LDR pulling toward ground) — verify the sign/direction against whatever sensor circuit is actually used |
| Bright-condition target brightness | 100% | |
| Dark-condition target brightness | 20% (see §2.3 finding — 10% was indistinguishable from off on this panel) | |
| Idle timeout | 120 seconds since last touch | |
| Idle-dim factor | 50% of the current ambient-derived target | |
| Wake behavior | Any touch immediately resets the idle timer and re-applies the full (non-dimmed) target | |

### A.10 Home Assistant entity bindings (generic roles — see §3 for the full table)

| Placeholder | Real domain | Attribute path (if any) | Read/Write | Update behavior |
|---|---|---|---|---|
| `climate.<thermostat>` | climate | `state` (mode), `current_temperature`, `temperature` (setpoint) | Read + write | Mode changes and setpoint changes update on-screen labels/highlights immediately on receipt; setpoint is also cached into `hvac_setpoint_value` so +/- buttons have a base value even before another live reading arrives |
| `sensor.<forecast_high>` | sensor | numeric value | Read | Updates the forecast-high label; missing/NaN value must render a placeholder ("--°F"), never crash or show garbage |
| `sensor.<day_type>` | sensor (or derived from the thermostat integration) | string value | Read | Drives the day-type badge text (uppercased) and its color |
| `sensor.<next_action_text>` | sensor | string value | Read | Free-text "what to do next" sentence from the automation system; needs light text post-processing (extracting a short "Action: time" phrasing from a longer sentence, and replacing any non-ASCII punctuation like em/en dashes if the display font doesn't include those glyphs) |
| `binary_sensor.<contact_N>` (many) | binary_sensor | `state` (open/closed) | Read | Each one's real HA **Area** must be resolved via the Area Registry (not guessed from its entity name — see §4 finding) and baked into the display logic per sensor |
| `fan.<whole_house_fan>` | fan | `state`, plus a percentage-based turn_on call | Write (turn on/off, set percentage) | See A.8.3's optimistic-UI sequence |
| `<ground-truth power sensor for the fan>` | binary_sensor / input_boolean | `state` | Read | Authoritative over the fan's last-commanded speed — always wins in `update_whf_display` (A.6) |
| `sensor.<fan_commanded_speed>` | sensor | string value (e.g. "low"/"high") | Read | Secondary to the ground-truth power signal; used only to distinguish Low vs. High when the fan is confirmed powered |

Where an integration exposes a "device trust" or equivalent per-client authorization setting
for inbound service calls, it must be enabled for this device — see §2.2's WHF-tab finding for
the exact silent-failure symptom this produces if missed.

### A.11 Boot / restart / reflash state policy — do not let a reboot act on real hardware

Distinct from A.4's "don't persist local variables" — this is about **not issuing real
commands to external systems (thermostat, fan) just because the device itself restarted.**

| Component | Boot/restart behavior | Why |
|---|---|---|
| WHF on/off control (mirrors the real fan) | On boot, this control's state is read live from the fan's own ground-truth entity — it does **not** run its normal "turn off" action against the real fan just because the device is initializing in an off-like state. | The naive default for a boot-restorable on/off control is usually "restore last known state, and if unknown/off, actively command the underlying hardware off to match." Applied here, that would force the real whole-house fan off on every single device reflash/reboot, regardless of whether the house actually needs the fan off at that moment. This was deliberately disabled. |
| HVAC on/off control (mirrors the real thermostat) | Same pattern: no boot-time command sent to the real thermostat. State is read live, not restored-and-enforced. | Same reasoning — a boot-time "restore to off" default would force `climate.set_hvac_mode "off"` on the real thermostat every time this display reboots, which is a real, disruptive action against a shared piece of home hardware for a reason (this display restarting) that has nothing to do with whether the home should be heating/cooling right now. |
| Backlight | Defaults to **on** at boot (not "restore whatever brightness it was at when power was lost, which could be fully off"), then the ambient-light + idle logic (A.9) takes over within a few seconds of the first sensor reading. | A restart that leaves the physical panel looking powered-off/blank (because it "restored" a dimmed or off state from before a crash) is indistinguishable from a dead device — always come up visibly on, even if auto-brightness dims it moments later. |

**General rule for any rebuild:** for every control that mirrors a real, external, shared piece
of hardware (anything outside this device itself), boot/restart must default to **read-only
observation of the real system's current state**, never to actively re-asserting a locally
remembered or default state onto that real hardware. Only this device's own purely-local
concerns (its own backlight, its own display content) are safe to actively force to a fixed
value on boot.

### A.12 Logging & diagnostics

Logging was treated as a first-class feature, not an afterthought — most bugs found during
this build (§4, §7, §8) were only diagnosable because the relevant log line already existed.
Retrofitting logging during an active investigation is strictly worse than having it running
from the start.

| Log stream | Cadence | Content | Purpose |
|---|---|---|---|
| Free-heap / memory | Every 30 seconds, continuous | Current free heap | This MCU has no extra RAM and runs a full graphics framebuffer plus ~20 concurrent remote-entity subscriptions at once — a continuous heap baseline lets a future memory-pressure regression (e.g. sensors silently failing to update) be distinguished from a logic bug, without needing to add this instrumentation retroactively mid-investigation |
| Per-entity value changes (single shared tag, e.g. `"ha_data"`) | On every change, for every HA-sourced entity | `"<entity role> value -> <value>"` | Every one of the ~20 subscribed values logs through one common, grep-able tag rather than each having its own ad-hoc format — makes it possible to pull a complete timeline of everything the device received from Home Assistant in one log filter, which was essential for diagnosing timing/race issues (§2.2, §4) |
| Touch coordinates | On every physical touch | Both raw and calibrated (post-transform) x/y | Required for the corner-tap calibration process (§5) and for diagnosing any future touch-mapping regression — always-on, not something to add only when a touch bug is already suspected |
| Ambient light | Continuous, tied to the sensor's own sampling interval (see A.9) | Raw voltage, derived dark/bright classification, resulting target brightness | Same principle as free-heap: this is what actually caught both the original hardware defect (§8) and let the post-fix threshold be calibrated from real measured values instead of a guess |

**General rule for any rebuild:** any value that (a) drives a user-visible behavior and (b) is
read from an external or physical source (a sensor, a network-delivered entity value) should
have a log line on every update, tagged so it can be isolated from unrelated log traffic, from
the very first flash — not added later once something is already suspected to be wrong.

### A.13 Connectivity & update resilience

- **WiFi fallback access point.** If the device can't join the home network, it must broadcast
  its own AP with a captive portal for re-configuration, rather than becoming permanently
  unreachable — this is what lets a misconfigured/relocated unit recover without a physical
  reflash.
- **Encrypted remote firmware updates.** Updates must be deliverable over the network, and that
  channel must be encrypted/authenticated — this device shares the home network and shouldn't
  be a soft target for arbitrary firmware pushes.
- **Encrypted control-plane API.** Whatever protocol carries commands/state to the HA-equivalent
  controller must be encrypted, not sent in the clear on the local network.
- These are capability requirements, not something to document with real values — see §6.
