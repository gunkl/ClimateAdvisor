<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Climate Advisor — Home Assistant Automation UI Guide

This guide shows the manual Home Assistant automations that came before the integration. Use it to understand the logic, to test one behaviour on its own, or to build pieces without the full integration. It also shows how to use Climate Advisor's own entities and services in your automations.

For what the integration does, how to install it, its entities and its services, see the [README](../README.md).

> **Don't run both on one thermostat.** These manual automations and Climate Advisor would fight over the same thermostat. If you keep any of them, point them at a different thermostat, or turn the zone's **Automation** switch off. With the switch off, Climate Advisor still plans and reports but sends no commands.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| Is this the live implementation? | No. It documents the manual approach used before the integration existed. | [§Building automations in the HA UI](#building-automations-in-the-ha-ui) |
| How do I pause heating when a door or window opens? | State trigger on the contact sensor, open, with a "For" delay. Then set the HVAC mode to off. Resume when every sensor is closed. | [§Automation 3](#automation-3-doorwindow-open--pause-heating) |
| How do I use the forecast in a condition? | Run the `weather.get_forecasts` action with a response variable, then add a Template condition. The old `forecast` attribute no longer exists. | [§Forecast-based automations](#forecast-based-automations) |
| How do I react to Climate Advisor in my own automations? | Use the zone's entities, for example `sensor.living_room_day_type`, and call its services with the zone's `entry_id`. | [§Using Climate Advisor in your automations](#using-climate-advisor-in-your-automations) |
| Why did an automation run, or not run? | Open the automation, then **⋮ → Traces**. | [§UI tips](#ui-tips) |

## Building automations in the HA UI

Every automation has three parts:
- **Triggers:** what starts it. Any one trigger is enough.
- **Conditions:** what must be true for it to continue.
- **Actions:** what it does. The UI calls running a service **Perform action**.

Go to **Settings → Automations & scenes → + Create automation → Create new automation**.

The YAML examples below use current syntax (`triggers:` / `trigger:`, `conditions:` / `condition:`, `actions:` / `action:`). The old `service:` key is replaced by `action:`. To use an example, open the automation editor, choose **⋮ → Edit in YAML**, and paste it in. Replace the entity IDs with your own.

## Automation 1: Outdoor temp rising — turn off the heat

- **Trigger:** Numeric state → outdoor temperature sensor → above 65°F (5° below the comfort setpoint).
- **Condition:** State → thermostat is `heat`.
- **Action:** Perform action → `climate.set_hvac_mode` → `off`. Optionally send a notification.
- **Companion (evening restart):** when the outdoor temperature drops below the threshold after 5 pm, set the thermostat back to `heat`.

## Automation 2: Nobody home — setback

- **Trigger:** State → presence entity → to `not_home`, **For** 15–30 minutes.
- **Condition (optional):** the setpoint is above the setback temperature.
- **Action:** Perform action → `climate.set_temperature` → setback temperature (for example 60°F).
- **Companion (return):** State → `home`, with no delay → restore the comfort setpoint.

## Automation 3: Door/window open — pause heating

- **Trigger:** State → contact sensor(s) → to `on` (shown as "Open"), with a **For** delay. One State trigger can list several sensors; any of them starts the automation. Climate Advisor waits 10 minutes by default (its **Sensor Debounce Delay** option).
- **Condition:** State → thermostat is `heat`.
- **Action:** Perform action → `climate.set_hvac_mode` → `off`, then send a notification.
- **Companion (resume):** trigger when a sensor goes back to `off` ("Closed") for 30 seconds. Add a State condition listing **every** sensor as `off`; a State condition with several entities passes only when all of them match.

```yaml
alias: Heat — pause on open door or window
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.front_door
      - binary_sensor.kitchen_window
    to: "on"
    for:
      minutes: 10
conditions:
  - condition: state
    entity_id: climate.thermostat
    state: heat
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.thermostat
    data:
      hvac_mode: "off"
  - action: notify.notify
    data:
      message: Heating paused, a door or window is open.
mode: single
```

## Automation 4: Runaway runtime protection

- **Trigger:** State → thermostat → attribute `hvac_action` → to `heating`, **For** 2–3 hours.
- **Condition:** outdoor temperature above 40°F. In extreme cold, a long run can be normal.
- **Action:** Perform action → `climate.set_temperature` → 3–4°F lower, then send a notification.

## Automation 5: Bedtime setback

- **Trigger:** Time → 22:30.
- **Condition:** State → thermostat is `heat`.
- **Action:** Perform action → `climate.set_temperature` → 3–5°F lower.
- **Companion (morning warm-up):** Time → 30–60 minutes before waking. Skip it if the forecast high is above the setpoint.

## Forecast-based automations

Weather entities no longer have a `forecast` attribute, so `state_attr('weather.x', 'forecast')` returns nothing. Instead, run the `weather.get_forecasts` action with a response variable as the first action. Then add a Template condition as the next action. In a daily forecast, `forecast[0]` is today and `forecast[1]` is tomorrow.

### Warming trend — deeper setback tonight

- **Trigger:** Time → 21:00.
- **Condition:** tomorrow's high is more than 10°F above today's.
- **Action:** lower the setpoint 5–8°F instead of the usual 3–5°F.

```yaml
alias: Heat — deeper setback before a warm day
triggers:
  - trigger: time
    at: "21:00:00"
actions:
  - action: weather.get_forecasts
    target:
      entity_id: weather.home
    data:
      type: daily
    response_variable: fc
  - condition: template
    value_template: >
      {% set f = fc['weather.home'].forecast %}
      {{ f[1].temperature - f[0].temperature > 10 }}
  - action: climate.set_temperature
    target:
      entity_id: climate.thermostat
    data:
      temperature: 62
mode: single
```

### Cooling trend — pre-heat

- **Trigger:** Time → early evening.
- **Condition:** tomorrow's low (`f[1].templow`) is well below today's (`f[0].templow`).
- **Action:** raise the setpoint 2–3°F above comfort to store heat in the house.

### Master mode selector

- **Trigger:** Time → 06:00 and 15:00.
- **Action:** run `weather.get_forecasts`, then a **Choose** block:
  - if tomorrow's high is above 80°F and warming → `cool` and pre-cool;
  - else if tomorrow's low is below 40°F and cooling → `heat` and pre-heat;
  - else if tomorrow's high is 65–78°F → `off` and an "open windows" notification;
  - otherwise → leave the mode alone.

## Using Climate Advisor in your automations

Each zone's entity IDs start with the zone name, for example `sensor.living_room_day_type` and `switch.living_room_automation`. Check yours under **Settings → Devices & services → Climate Advisor → (zone device)**. The README lists [every entity](../README.md#entities) and [every service](../README.md#services).

Useful states:
- **Day Type:** `hot`, `warm`, `mild`, `cool` or `cold`.
- **Occupancy Mode:** `home`, `away`, `vacation` or `guest`.
- **Status** is descriptive text that can change between releases, so avoid exact matches on it.

Every Climate Advisor service needs the zone's `entry_id`. In the UI, pick the zone from the **Zone** dropdown and the editor fills it in.

```yaml
alias: Climate Advisor — hot day heads-up
triggers:
  - trigger: state
    entity_id: sensor.living_room_day_type
    to: hot
actions:
  - action: notify.notify
    data:
      message: >
        Hot day ahead. {{ states('sensor.living_room_your_next_action') }}
mode: single
```

```yaml
alias: Climate Advisor — reclassify after a weather alert
triggers:
  - trigger: state
    entity_id: binary_sensor.weather_alert
    to: "on"
actions:
  - action: climate_advisor.force_reclassify
    data:
      entry_id: 01JABCDEFGHJKMNPQRSTVWXYZ  # your zone's entry ID
mode: single
```

## UI tips

- **Names:** use clear names, for example "Heat — pause on open door".
- **Traces:** open the automation and choose **⋮ → Traces** to see why it ran or stopped.
- **Helpers:** create Number helpers for setpoints and thresholds under **Settings → Devices & services → Helpers**, and use them instead of fixed values.
- **Testing:** **⋮ → Run actions** runs the actions now and skips the triggers and conditions.
- **Templates:** test a template in **Developer tools → Template**. To test `weather.get_forecasts`, run it in **Developer tools → Actions** and check the response.
