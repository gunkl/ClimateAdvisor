# Configuration options

Every setting Climate Advisor asks for, in one place: what it does for the house, its default, the values it accepts, and when you might change it.

**Where they are.** The setup wizard asks for the basics when you add a zone. Everything is editable afterwards in **Settings → Devices & Services → Climate Advisor → Configure**. Configure opens a menu; each section below matches one entry in that menu, in the same order. Each zone (thermostat) has its own settings, except AI settings, which apply to every zone.

**Units.** Temperatures are shown and entered in the unit you choose (°F or °C). Defaults are given in °F with the °C value in brackets. A "degrees" value marked *difference* is a gap between two temperatures, not a reading (5°F of difference is about 2.8°C).

**Reading the tables.** *Setting* is the label you see in the form, with its internal name in `code` (the name in logs, diagnostics and the REST API). *Default* is what a new zone starts with. "Blank" means the field is empty and the feature is off or automatic, as the row says.

## Setup wizard

The wizard asks for these, in order. Each setting is explained in the Configure section named in brackets.

1. **Core:** Weather Service, Thermostat, Notification Service ([Core Settings](#core-settings)).
2. **Unit:** Temperature Unit ([Core Settings](#core-settings)).
3. **Setpoints:** comfort, away and sleep temperatures ([Temperature Setpoints](#temperature-setpoints)). The vacation temperatures are only in Configure.
4. **Temperature sources:** indoor and outdoor source. If you pick a sensor or an input helper, the next screen asks which entity ([Temperature Sources](#temperature-sources)).
5. **Doors/windows and fan:** sensors, polarity, fan mode, fan entity ([Sensors & Fan](#sensors--fan)). Timing and fan-feedback settings are only in Configure.
6. **Occupancy:** home/away, vacation and guest toggles ([Occupancy](#occupancy)).
7. **Schedule:** Zone Name, wake-up time, bedtime, briefing time and Daily Briefing Notifications ([Schedule](#schedule), [Notifications](#notifications)).

**Change zone setup** (**⋮ → Reconfigure** on the zone) changes only the Weather Service, Thermostat and Notification Service. The zone keeps its learning and settings.

**Repairs asking for a new key or token.** If Anthropic refuses the built-in Claude API key, or GitHub refuses the token, a Repairs card asks you for a new one (Claude API Key or GitHub Personal Access Token, see [AI settings](#ai-settings) and [GitHub Integration](#github-integration)). The new value is tested before it is saved.

## Core Settings

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Zone Name** `zone_name` | The zone's name in Settings and in the dashboard's zone selector. | Your thermostat's name | Text, up to 50 characters |
| **Weather Service** `weather_entity` | The weather entity whose forecast drives the day type and the plan. Change it if you switch weather integrations. | None (required) | A `weather` entity with a forecast |
| **Thermostat** `climate_entity` | The thermostat this zone controls. Each thermostat can belong to only one zone. | None (required) | A `climate` entity |
| **Temperature Unit** `temp_unit` | The unit for every temperature you see and type in Climate Advisor. Changing it needs a reload; Climate Advisor prompts you. Your settings are kept and shown in the new unit. | Fahrenheit | Fahrenheit (°F), Celsius (°C) |
| **Notification Service** `notify_service` | Where push notifications go, for example `notify.mobile_app_yourphone`. | `notify.notify` | A notify service, written `notify.<name>` |

## Temperature Setpoints

All setpoints accept 40–95°F (4–35°C), in steps of 1°F (0.5°C). If a combination would make the heat and AC fight, or settle somewhere odd, a warning appears next to the field. Press Submit again to keep it.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Comfortable Heating Temperature** `comfort_heat` | The house is heated up to this while someone is home and awake. | 68°F (20°C) | 40–95°F |
| **Comfortable Cooling Temperature** `comfort_cool` | The house is cooled down to this while someone is home and awake. | 74°F (23.5°C) | 40–95°F |
| **Away Heating Temperature** `setback_heat` | The lower heating target while nobody is home. Lower saves more but takes longer to recover. | 63°F (17°C) | 40–95°F |
| **Away Cooling Temperature** `setback_cool` | The higher cooling target while nobody is home. | 79°F (26°C) | 40–95°F |
| **Sleep Heating Temperature** `sleep_heat` | The heating target from bedtime to wake-up, when someone is home. | 64°F (18°C) | 40–95°F |
| **Sleep Cooling Temperature** `sleep_cool` | The cooling target from bedtime to wake-up, when someone is home. | 72°F (22°C) | 40–95°F |
| **Vacation Heating Temperature** `vacation_heat` | The heating target while the vacation toggle is on. Set it for a deeper setback on long trips. Configure only. | Blank: away heating minus 3°F (about 1.5°C) | 40–95°F, or blank |
| **Vacation Cooling Temperature** `vacation_cool` | The cooling target while the vacation toggle is on. Configure only. | Blank: away cooling plus 3°F (about 1.5°C) | 40–95°F, or blank |
| **No cooling below outdoor** `cool_lockout_outdoor_temp` | Issue #1699: no cooling while the outdoor temperature is below this; cooling is allowed again 2°C (3.6°F) above it. A house hotter than the ceiling by more than the day's overshoot tolerance still gets cooling. A missing outdoor reading keeps the last state; none since startup means no lockout. Configure only. | Blank (no lockout) | −20–110°F (−30–45°C), or blank |
| **No heating above outdoor** `heat_lockout_outdoor_temp` | Issue #1699: no heating while the outdoor temperature is above this; heating is allowed again 2°C (3.6°F) below it. A house colder than the floor by more than the day's overshoot tolerance still gets heating. Must be at least 2°C (3.6°F) above the cooling lockout, so both can never be engaged at once (the form rejects a closer pair). Configure only. | Blank (no lockout) | −20–110°F (−30–45°C), or blank |
| **Heating and cooling season** `hvac_season` | Issue #1699: Heat only or Cool only removes the other side for the season, as if the thermostat could not do it (a house far past that side's comfort edge still gets it). Configure only. | Auto | Auto (heat and cool), Heat only, Cool only |

## Temperature Sources

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Outdoor Temperature Source** `outdoor_temp_source` | Where the outside temperature comes from. Pick a sensor if you have one outside: it is usually closer to your house than the weather service. | Weather service | Weather service, Dedicated sensor, Input helper |
| **Outdoor Temperature Entity** `outdoor_temp_entity` | The outdoor sensor or input helper, when the source above is not the weather service. Picking one here also switches the source to match. | Blank | A temperature `sensor` or `input_number` |
| **Outdoor Humidity Sensor** `outdoor_humidity_entity` | Shown on the Status chart, with how warm or cold it feels outside. Chart only: no heating or cooling decision uses it. | Blank: found automatically | A humidity `sensor` |
| **Indoor Temperature Source** `indoor_temp_source` | Where the indoor temperature comes from. The thermostat's own sensor is fine for most homes; pick a room sensor if the thermostat sits somewhere unrepresentative. | Climate entity | Climate entity, Dedicated sensor, Input helper |
| **Indoor Temperature Entity** `indoor_temp_entity` | The indoor sensor or input helper, when the source above is not the thermostat. | Blank | A temperature `sensor` or `input_number` |
| **Indoor Humidity Sensor** `indoor_humidity_entity` | Shown on the Status chart, with how warm the room feels. Chart only. | Blank: found automatically | A humidity `sensor` |
| **Sleep Indoor Temp Sensor** `sleep_indoor_temp_entity` | A bedroom sensor used while the sleep schedule is active, so the bedroom (not the hallway) stays on target. Climate Advisor nudges the thermostat up to 4°F (2°C) past your sleep temperature to get there. If the sensor goes missing, the main sensor is used. | Blank: off | A temperature `sensor` |
| **Sleep Humidity Sensor** `sleep_indoor_humidity_entity` | The bedroom humidity, shown on the Status chart at night. Chart only. | Blank: found automatically | A humidity `sensor` |
| **Thermostat Finer Sensor** `thermostat_companion_sensor` | Many thermostats show whole degrees but have a finer sensor on the same device. Auto uses it once it proves to be the same reading (68.54°F instead of 69°F) and falls back to the thermostat if it goes missing or disagrees. Applies only when the indoor source is the thermostat. Choose Off if the finer reading looks wrong. | Auto | Auto, Off |
| **Recalibration Guard** `thermostat_recalibration_guard` | Some thermostats read several degrees too high for up to about 40 minutes after the furnace shuts itself off (its high-limit safety trips) while the room is still below the setpoint. On: Climate Advisor spots the jump, logs a warning, shows an ignorable Repairs notice, keeps those readings out of thermal learning, and reads the room from the sleep sensor or a dedicated indoor sensor until the thermostat settles. Applies only when the indoor source is the thermostat. Off ignores the jump. | On | On, Off |

## Sensors & Fan

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Door/Window Sensors** `door_window_sensors` | The doors and windows to watch. When one stays open, heating and cooling pause so you do not heat or cool the outdoors. A binary sensor group counts as one sensor. A whole-house fan needs at least one. | None | `binary_sensor` entities (several allowed) |
| **Invert Sensor Polarity** `sensor_polarity_inverted` | Turn on if your sensors report "on" when closed. Most sensors report "on" when open. | Off | On, Off |
| **Sensor Debounce Delay** `sensor_debounce_seconds` | How long a door or window must stay open (or closed) before Climate Advisor reacts: pausing or resuming the heat/AC, or starting or stopping fan ventilation. A shorter delay stops the heat/AC sooner when a door or window is left open; a longer one ignores brief openings but keeps the heat/AC running into an open door or window for longer. Configure only. | 10 minutes | 0–60 minutes |
| **Manual Change Pause** `manual_grace_seconds` | After you change the thermostat yourself, Climate Advisor leaves it alone this long before taking over again. Configure only. | 30 minutes | 0–240 minutes |
| **Automated Change Cooldown** `automation_grace_seconds` | After Climate Advisor changes the thermostat, the minimum wait before it makes another change. Prevents rapid back-and-forth. Configure only. | 5 minutes | 0–240 minutes |
| **Override Confirmation Delay** `override_confirm_seconds` | How long a change that did not come from Climate Advisor must last before it counts as your manual override. Short blips (a thermostat restart, a fan cycle) inside this window are ignored. 0 confirms at once. Configure only. | 10 minutes | 0–60 minutes |
| **Fan Control Mode** `fan_mode` | Which fan Climate Advisor may use to bring in outside air or move air around. | Disabled | Disabled, Whole house fan, HVAC fan |
| **HVAC Fan Restriction** `hvac_fan_restrict_mode` | With the HVAC fan mode: when the thermostat's blower may run on its own. Heat only skips it during cooling and for 2 hours after, so a wet AC coil does not blow humidity back into a humid house. Cool only does the same after heating. Configure only. | Both (no restriction) | Heat only, Cool only, Both |
| **Fan Entity** `fan_entity` | The whole-house fan to switch on and off. Needed only for the whole-house fan mode. | Blank | A `fan` or `switch` |
| **Fan State Entity** `fan_state_entity` | A separate entity that reports whether the fan is really running, for fans controlled by one entity and read by another. Configure only. | Blank: the Fan Entity is read | A `fan`, `binary_sensor`, `input_boolean` or `switch` |
| **Fan State Feedback Reliable** `fan_state_feedback` | Turn on if the fan entity (or the state entity) reports the motor's real state, not just the last command. Left off, Climate Advisor cannot see the fan and re-sends the wanted state every cycle. Configure only. | Off (on when a Fan State Entity is set) | On, Off |
| **Fan RF Remote Event Entity** `fan_remote_entity` | The event entity that fires when the fan's wall remote is pressed (QuietCool RF, with the `gunkl/quietcool-house-fan` firmware). A timer press of 1, 2, 4, 8 or 12 hours keeps Climate Advisor's hands off the fan for that long. Configure only. | Blank: off | An `event` entity |
| **Fan Minimum Runtime Per Hour** `fan_min_runtime_per_hour` | Runs the fan at least this many minutes each hour, for air circulation. 0 is off, 60 is always on. The hour is counted from when Home Assistant started, not from the clock. Configure only. | 0 (off) | 0–60 minutes |
| **Nat-Vent Soft-Start** `nat_vent_soft_start_enabled` | Lets the whole-house fan start when outside is about as warm as inside, once today's outdoor peak has passed and it is cooling down. This moves air and purges a hot attic early. Turn off to run the fan only when outside is clearly cooler. On humid nights the Humid-Air Guard keeps it from starting. Configure only. | On | On, Off |
| **Humid-Air Guard** `humid_air_guard_enabled` | Keeps humid outdoor air out (Issue #1691): while the outdoor dew point is at or above the ceiling below, or is 55 °F or more and at least 2 °F above the house's, the whole-house fan, the blower and the window-cooling economizer do not start their fan. A session already running is not stopped; a house with no fan is unaffected. Without an outdoor humidity reading (sensor or weather service) nothing changes. Configure only. | On | On, Off |
| **Humid-Air Dew Point Ceiling** `humid_air_guard_ceiling_f` | The outdoor dew point at or above which the Humid-Air Guard always blocks, even when the house is wetter. Lower blocks more nights. The guard lets go 1 °F below it. Shown and entered in your temperature unit, stored in °F. Configure only. | 65 °F (18.3 °C) | 55–70 °F (12.8–21.1 °C) |

## Occupancy

All toggles are optional. Leave one blank if you do not use that feature.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Home/Away Toggle** `home_toggle_entity` | Tells Climate Advisor whether anyone is home. On means home: comfort temperatures. Off means away: away temperatures. | Blank (always treated as home) | An `input_boolean`, `binary_sensor` or `switch` |
| **Invert Home Toggle** `home_toggle_invert` | Turn on if your toggle is on when you are away. | Off | On, Off |
| **Vacation Mode Toggle** `vacation_toggle_entity` | While on, the vacation temperatures apply: a deeper setback for longer trips. | Blank | An `input_boolean`, `binary_sensor` or `switch` |
| **Invert Vacation Toggle** `vacation_toggle_invert` | Turn on if your toggle is on when you are not on vacation. | Off | On, Off |
| **Guest Mode Toggle** `guest_toggle_entity` | While on, comfort temperatures are kept even when the house is marked away or on vacation. | Blank | An `input_boolean`, `binary_sensor` or `switch` |
| **Invert Guest Toggle** `guest_toggle_invert` | Turn on if your toggle is on when there are no guests. | Off | On, Off |
| **Welcome Home Quiet Period** `welcome_home_debounce_seconds` | The shortest time between two "welcome home" notifications. Coming back within this window sends none. 0 always notifies. Configure only. | 60 minutes | 0–480 minutes, in steps of 5 |

## Schedule

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Wake-up Time** `wake_time` | When the household gets up. Climate Advisor starts heating or cooling early so the house is at comfort temperature by then. | 06:30 | A time of day |
| **Bedtime** `sleep_time` | When the household goes to bed. The sleep temperatures apply from here until wake-up. | 22:30 | A time of day |
| **Daily Briefing Time** `briefing_time` | When the daily briefing (today's forecast, the plan and what to do with windows) is built and sent. | 06:00 | A time of day |

## TOU Scheduler

Time-of-use schedules mark when electricity is expensive. Before a high-cost period, Climate Advisor pre-cools or pre-heats within your comfort range, so the heat or AC runs less while power costs more. There are no temperatures to set. You can have up to 5 schedules.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Manage schedule** `manage` | Pick a schedule to edit, or add a new one. | None | One of your schedules, or Add new |
| **Name** `name` | A short name for the schedule, such as "Summer Peak". | Blank | Text |
| **Days** `days` | The days the schedule applies to. | None (pick at least one) | Mon–Sun, several allowed |
| **Starts** `start` | When the high-cost period starts. Pre-conditioning begins before this. | 16:00 | A time of day |
| **Ends** `end` | When the high-cost period ends. Must differ from the start. | 21:00 | A time of day |
| **TOU changes this setpoint** `applies_to` | Which setpoint the schedule may move: cooling on cooling days, heating on heating days. Choose Cooling only if your heating is not electric (a gas furnace), so heating is never changed. | Cooling and heating | Cooling and heating, Cooling only, Heating only |
| **Only while this is on** `condition_entity` | The schedule applies only while this entity is on, for example a "peak season" helper. If the entity is missing or unavailable, the schedule still applies. | Blank (always applies) | A `binary_sensor`, `input_boolean` or `switch` |
| **Delete this schedule** `delete_schedule` | Turn on and submit to delete the schedule you are editing. Shown only when editing. | Off | On, Off |

## Notifications

Push notifications go to the Notification Service. Emails go through Home Assistant's `send_email` service.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Daily Briefing Notifications** `briefing_notifications_enabled` | Whether this zone sends its scheduled daily briefing at all (push and email). Only the first zone starts with it on, so a home with several zones does not get one copy per zone. | On for the first zone, off for others | On, Off |
| **Push: Daily Briefing** `push_briefing` | A short briefing summary on your phone each morning. | On | On, Off |
| **Push: HVAC Paused** `push_door_window_pause` | A push when heating or cooling pauses because a door or window has been open too long. | On | On, Off |
| **Push: Welcome Home** `push_occupancy_home` | A push when someone arrives home and comfort temperature is restored. | On | On, Off |
| **Push: Manual Grace Expired** `manual_grace_notify` | A push when the pause after your own thermostat change ends and Climate Advisor takes over again. | Off | On, Off |
| **Push: Automation Grace Expired** `automation_grace_notify` | A push when the cooldown after Climate Advisor's own change ends. | On | On, Off |
| **Push: Entity Not Found** `push_entity_health` | A push when an entity you picked (thermostat, weather, a sensor, the fan, a toggle or the notification service) is removed or stops responding. | On | On, Off |
| **Email: Daily Briefing** `email_briefing` | The full daily briefing by email, with the complete forecast and plan. | On | On, Off |
| **Email: HVAC Paused** `email_door_window_pause` | An email when heating or cooling pauses for an open door or window. | On | On, Off |
| **Email: Grace Expired** `email_grace_expired` | An email when a pause or cooldown ends and normal door/window handling resumes. | On | On, Off |
| **Email: HVAC Re-paused** `email_grace_repause` | An email when heating or cooling pauses again because a door or window is still open after the pause ended. | On | On, Off |
| **Email: Welcome Home** `email_occupancy_home` | An email when someone arrives home. | On | On, Off |
| **Email: Entity Not Found** `email_entity_health` | An email when an entity you picked is removed or stops responding. | On | On, Off |

## Learning & Behavior

Learning only ever gives advice: suggestions never change your settings on their own.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Prediction engine** `primary_engine` | The engine that predicts the indoor temperature. It times pre-heating and pre-cooling and sets the plan and the chart. While it is still learning, Climate Advisor uses its default plan. See [prediction engines](prediction-engines-spec.md). | v4 | v4, v3 |
| **Comparison engine** `shadow_engine` | An optional second engine, drawn as a comparison line on the chart only. It must differ from the prediction engine. | None | None, v3, v4 |
| **Enable Learning & Suggestions** `learning_enabled` | Learns from your manual adjustments and suggests improvements over time. The three adaptive settings below need it. | On | On, Off |
| **Adaptive Pre-heat Timing** `adaptive_preheat_enabled` | Times morning pre-heating from how fast your house actually warms, instead of a fixed time. | On | On, Off |
| **Adaptive Bedtime Setback** `adaptive_setback_enabled` | Sets how far the temperature may drop (or rise) overnight from how fast your house recovers, so it is back on target by morning. | On | On, Off |
| **Weather Forecast Bias Correction** `weather_bias_enabled` | Corrects tomorrow's forecast for your location, from how far off past forecasts were. | On | On, Off |
| **Minimum Pre-heat Time** `min_preheat_minutes` | The shortest pre-heat Climate Advisor will ever plan. Must not exceed the maximum. | 30 minutes | 5–60 minutes, in steps of 5 |
| **Maximum Pre-heat Time** `max_preheat_minutes` | The longest pre-heat Climate Advisor will ever plan. Lower it if the heat comes on too early. | 240 minutes | 30–240 minutes, in steps of 15 |
| **Default Pre-heat Time** `default_preheat_minutes` | The pre-heat time used until enough has been learned about your house. Must not exceed the maximum. | 120 minutes | 10–120 minutes, in steps of 5 |
| **TOU Pre-conditioning Lead Time** `default_tou_lead_minutes` | How long before a high-cost period pre-conditioning starts, until your house's real heating and cooling rate is known. | 45 minutes | 30–240 minutes, in steps of 5 |
| **Pre-heat Safety Margin** `preheat_safety_margin` | Extra time added to the learned pre-heat time, as a multiplier (1.3 = 30% extra). Raise it if the house is still cool at wake-up. | 1.3 | 1.0–2.0, in steps of 0.05 |
| **Maximum Setback Depth** `max_setback_depth_f` | The largest overnight setback the adaptive setback may choose: how many degrees below (or above) comfort. A *difference*. | 8°F (about 4.4°C) | 1–10°F (0.5–5.6°C) |
| **Heat/Cool Switch Interval** `comfort_mode_switch_min_interval_s` | The shortest wait between routine switches from heating to cooling or back. A real breach (the room actually past a comfort edge) switches at once. | 600 seconds (10 minutes) | 60–1800 seconds, in steps of 30 |
| **Prefer savings over comfort** `aggressive_savings` | Favors saving energy: wider temperature swings are allowed before heating or cooling runs. | Off | On, Off |
| **Hot Day Overshoot Tolerance** `comfort_deadband_hot_f` | On a hot day, right after cooling ran: how far below the comfort band the house may drift before heating is switched on. Cooling is never delayed. A *difference*. | 5°F (about 2.8°C) | 2–8°F (1.1–4.5°C) |
| **Warm Day Overshoot Tolerance** `comfort_deadband_warm_f` | The same for a warm day: how far below the band before heating is switched on after cooling ran. | 2°F (about 1.1°C) | 1–5°F (0.5–2.8°C) |
| **Mild Day Overshoot Tolerance** `comfort_deadband_mild_f` | On a mild day, in both directions: how far past either edge before the opposite mode is switched on after the other one ran. | 2°F (about 1.1°C) | 1–5°F (0.5–2.8°C) |
| **Cool Day Overshoot Tolerance** `comfort_deadband_cool_f` | On a cool day, right after heating ran: how far above the band before cooling is switched on. Heating is never delayed. | 2°F (about 1.1°C) | 1–5°F (0.5–2.8°C) |
| **Cold Day Overshoot Tolerance** `comfort_deadband_cold_f` | The same for a cold day: how far above the band before cooling is switched on after heating ran. | 5°F (about 2.8°C) | 2–8°F (1.1–4.5°C) |
| **Recent Run Window** `comfort_family_recency_window_min` | How long heating or cooling counts as having "recently run" for the tolerances above, and for the predicted-overshoot guard, which waits instead of acting during this window. | 120 minutes | 15–360 minutes, in steps of 15 |
| **Energy Rate** `energy_rate` | What one kilowatt-hour costs you, in your currency. With the two power settings it lets the dashboard estimate each day's cost. Leave all three blank to show no cost. | Blank | 0.0001–10 per kWh |
| **Heating Power** `heat_power_kw` | The average electrical power your heating draws while it runs (nameplate or a smart plug). For the cost estimate only. | Blank | 0.1–50 kW |
| **Cooling Power** `cool_power_kw` | The average electrical power your cooling draws while it runs. For the cost estimate only. | Blank | 0.1–50 kW |

## Day-Type Thresholds

Each day is classified by its forecast high. The thresholds must go up from Cool to Hot. See [Day types](../README.md#day-types).

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Hot day threshold** `threshold_hot` | A forecast high at or above this is a Hot day: the AC runs and pre-cools when it helps. | 85°F (29°C) | 75–110°F (23.5–43.5°C) |
| **Warm day threshold** `threshold_warm` | At or above this (and below Hot) is a Warm day: windows are recommended, no AC planned. | 75°F (24°C) | 60–95°F (15–35°C) |
| **Mild day threshold** `threshold_mild` | At or above this (and below Warm) is a Mild day: comfortable, windows open. | 60°F (16°C) | 45–80°F (7–27°C) |
| **Cool day threshold** `threshold_cool` | At or above this (and below Mild) is a Cool day with light heating. Below it is a Cold day with full heating. | 45°F (7°C) | 30–65°F (−2–18.5°C) |

## AI settings

These apply to every zone: saving them in one zone sets them in all. See [AI Investigator](../README.md#ai-investigator) for setting up a provider. Settings marked *built-in Claude only* do nothing with a Home Assistant AI Task.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **Enable AI Features** `ai_enabled` | The master switch for AI features. Off, Climate Advisor uses only its own built-in rules. | Off | On, Off |
| **AI Provider** `ai_provider` | Home Assistant AI Task uses an AI integration you added to Home Assistant. Built-in Claude is deprecated and will be removed. | Home Assistant AI Task | Home Assistant AI Task, Built-in Claude |
| **AI Task Entity** `ai_task_entity` | The AI Task entity to use. | Blank: Home Assistant's preferred AI Task entity | An `ai_task` entity |
| **Claude API Key** `ai_api_key` | *Built-in Claude only.* Your Anthropic API key. It is tested before it is saved. Leave blank to keep the saved key. | Blank | Text (hidden) |
| **AI Model** `ai_model` | *Built-in Claude only.* Which Claude model to use. | `claude-sonnet-5` | The models your key can use |
| **Reasoning Effort** `ai_reasoning_effort` | *Built-in Claude only.* More effort gives better analysis at more cost. | Low | Low, Medium |
| **Maximum Response Length** `ai_max_tokens` | *Built-in Claude only.* The longest answer, in tokens. Too small cuts reports short. | 4096 tokens | 256–16384 tokens, in steps of 256 |
| **Creativity** `ai_temperature` | *Built-in Claude only.* 0 gives the same answer every time, 1 the most varied. | 0.3 | 0–1, in steps of 0.1 |
| **Monthly Budget Cap** `ai_monthly_budget` | *Built-in Claude only.* The most Climate Advisor may spend per month, estimated in US dollars. 0 means no cap. | 0 (no cap) | 0–100 USD |
| **Auto Requests Per Day** `ai_auto_requests_per_day` | The most AI requests Climate Advisor makes on its own per day. Resets at midnight. | 5 | 1–50 |
| **Manual Requests Per Day** `ai_manual_requests_per_day` | The most AI requests you can start per day. Resets at midnight. | 20 | 1–100 |
| **Enable Investigative Agent** `ai_investigator_enabled` | Turns on the AI Investigator, which reads what Climate Advisor did and why and writes a report. Needs AI features on. | Off | On, Off |
| **Investigator Requests Per Day** `ai_investigator_requests_per_day` | The most Investigator reports per day. Resets at midnight. | 3 | 1–20 |

## GitHub Integration

Lets you submit an Investigator report as a GitHub issue. Both are optional.

| Setting | What it does | Default | Values |
|---|---|---|---|
| **GitHub Personal Access Token** `github_token` | A token with `issues:write` access to the repository. Leave blank to keep the saved token. | Blank | Text (hidden) |
| **GitHub Repository** `github_repo` | The repository to submit issues to. | Blank | `owner/repo`, for example `gunkl/ClimateAdvisor` |
