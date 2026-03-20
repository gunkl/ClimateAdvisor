"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

# Integration version — MUST match manifest.json "version" field.
# A test in tests/test_version_sync.py enforces this.
VERSION = "0.1.0"

# Default setpoints (°F)
DEFAULT_COMFORT_HEAT = 70
DEFAULT_COMFORT_COOL = 75
DEFAULT_SETBACK_HEAT = 60
DEFAULT_SETBACK_COOL = 80

# Day type classifications
DAY_TYPE_HOT = "hot"
DAY_TYPE_WARM = "warm"
DAY_TYPE_MILD = "mild"
DAY_TYPE_COOL = "cool"
DAY_TYPE_COLD = "cold"

# Day type thresholds (°F)
THRESHOLD_HOT = 85
THRESHOLD_WARM = 75
THRESHOLD_MILD = 60
THRESHOLD_COOL = 45

# Trend thresholds (°F difference to trigger predictive behavior)
TREND_THRESHOLD_SIGNIFICANT = 10
TREND_THRESHOLD_MODERATE = 5

# Timing
DOOR_WINDOW_PAUSE_SECONDS = 180  # deprecated — use CONF_SENSOR_DEBOUNCE instead

# Door/window sensor configuration
CONF_SENSOR_POLARITY_INVERTED = "sensor_polarity_inverted"

# Debounce and grace period config keys
CONF_SENSOR_DEBOUNCE = "sensor_debounce_seconds"
CONF_MANUAL_GRACE_PERIOD = "manual_grace_seconds"
CONF_MANUAL_GRACE_NOTIFY = "manual_grace_notify"
CONF_AUTOMATION_GRACE_PERIOD = "automation_grace_seconds"
CONF_AUTOMATION_GRACE_NOTIFY = "automation_grace_notify"
CONF_EMAIL_NOTIFY = "email_notify"

# Debounce and grace period defaults (seconds)
DEFAULT_SENSOR_DEBOUNCE_SECONDS = 300  # 5 minutes
DEFAULT_MANUAL_GRACE_SECONDS = 1800  # 30 minutes
DEFAULT_AUTOMATION_GRACE_SECONDS = 3600  # 60 minutes
OCCUPANCY_SETBACK_MINUTES = 15
MAX_CONTINUOUS_RUNTIME_HOURS = 3

# Economizer (window cooling) threshold
ECONOMIZER_TEMP_DELTA = 3  # °F — activate when outdoor temp within this delta of comfort_cool

# Economizer time boundaries for hot-day window cooling
ECONOMIZER_MORNING_START_HOUR = 6   # 6:00 AM
ECONOMIZER_MORNING_END_HOUR = 9     # 9:00 AM
ECONOMIZER_EVENING_START_HOUR = 17  # 5:00 PM
ECONOMIZER_EVENING_END_HOUR = 24    # midnight (end of day)

# Warm-day window timing — open early morning, close before outdoor temps climb
WARM_WINDOW_OPEN_HOUR = 6    # 6:00 AM
WARM_WINDOW_CLOSE_HOUR = 10  # 10:00 AM

# Occupancy toggle configuration
CONF_HOME_TOGGLE = "home_toggle_entity"
CONF_HOME_TOGGLE_INVERT = "home_toggle_invert"
CONF_VACATION_TOGGLE = "vacation_toggle_entity"
CONF_VACATION_TOGGLE_INVERT = "vacation_toggle_invert"
CONF_GUEST_TOGGLE = "guest_toggle_entity"
CONF_GUEST_TOGGLE_INVERT = "guest_toggle_invert"

# Occupancy mode values
OCCUPANCY_HOME = "home"
OCCUPANCY_AWAY = "away"
OCCUPANCY_VACATION = "vacation"
OCCUPANCY_GUEST = "guest"

# Vacation deeper setback (degrees beyond normal setback)
VACATION_SETBACK_EXTRA = 3

# Fan control configuration
CONF_FAN_ENTITY = "fan_entity"
CONF_FAN_MODE = "fan_mode"
FAN_MODE_DISABLED = "disabled"
FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
FAN_MODE_HVAC = "hvac_fan"
FAN_MODE_BOTH = "both"
DEFAULT_FAN_MODE = FAN_MODE_DISABLED

# State persistence
STATE_FILE = "climate_advisor_state.json"

# Learning system
LEARNING_DB_FILE = "climate_advisor_learning.json"
SUGGESTION_COOLDOWN_DAYS = 7  # Don't repeat the same suggestion within a week
MIN_DATA_POINTS_FOR_SUGGESTION = 14  # Need 2 weeks of data before suggesting changes
COMPLIANCE_THRESHOLD_LOW = 0.3  # Below 30% compliance triggers a suggestion
COMPLIANCE_THRESHOLD_HIGH = 0.8  # Above 80% means the advice is working

# Temperature source types
TEMP_SOURCE_SENSOR = "sensor"
TEMP_SOURCE_INPUT_NUMBER = "input_number"
TEMP_SOURCE_WEATHER_SERVICE = "weather_service"
TEMP_SOURCE_CLIMATE_FALLBACK = "climate_fallback"

# Sensor attributes
ATTR_DAY_TYPE = "day_type"
ATTR_TREND = "trend_direction"
ATTR_TREND_MAGNITUDE = "trend_magnitude"
ATTR_BRIEFING = "daily_briefing"
ATTR_NEXT_ACTION = "next_human_action"
ATTR_AUTOMATION_STATUS = "automation_status"
ATTR_LEARNING_SUGGESTIONS = "pending_suggestions"
ATTR_COMPLIANCE_SCORE = "compliance_score"
ATTR_ESTIMATED_SAVINGS = "estimated_savings"
ATTR_AUTOMATION_ENABLED = "automation_enabled"
ATTR_NEXT_AUTOMATION_ACTION = "next_automation_action"
ATTR_NEXT_AUTOMATION_TIME = "next_automation_time"
ATTR_OCCUPANCY_MODE = "occupancy_mode"
ATTR_LAST_ACTION_TIME = "last_action_time"
ATTR_LAST_ACTION_REASON = "last_action_reason"

# Revisit delay — follow-up check after any HVAC action (seconds)
REVISIT_DELAY_SECONDS = 300  # 5 minutes

# API paths for dashboard panel
API_BASE = "/api/climate_advisor"
API_STATUS = f"{API_BASE}/status"
API_BRIEFING = f"{API_BASE}/briefing"
API_CHART_DATA = f"{API_BASE}/chart_data"
API_AUTOMATION_STATE = f"{API_BASE}/automation_state"
API_LEARNING = f"{API_BASE}/learning"
API_FORCE_RECLASSIFY = f"{API_BASE}/force_reclassify"
API_SEND_BRIEFING = f"{API_BASE}/send_briefing"
API_RESPOND_SUGGESTION = f"{API_BASE}/respond_suggestion"
API_CONFIG = f"{API_BASE}/config"

# Panel
PANEL_URL = "/climate_advisor/frontend"
PANEL_FRONTEND_PATH = "climate-advisor"

# Configuration metadata for the Settings tab.
# When adding new config options, update this dict so the Settings tab
# displays the new option with a proper description.
CONFIG_METADATA = {
    "weather_entity": {
        "label": "Weather Entity",
        "description": "The weather integration used for forecast data. Determines day type classification and all downstream automation decisions.",
        "category": "core",
    },
    "climate_entity": {
        "label": "Thermostat Entity",
        "description": "The climate entity Climate Advisor controls. All HVAC mode and temperature commands go to this entity.",
        "category": "core",
    },
    "comfort_heat": {
        "label": "Comfort Heat (°F)",
        "description": "Target temperature when heating is active. Lowering saves energy but may feel cooler. Used for morning wake-up and occupancy-home restores.",
        "category": "core",
    },
    "comfort_cool": {
        "label": "Comfort Cool (°F)",
        "description": "Target temperature when cooling is active. Raising saves energy but may feel warmer. The economizer uses this as the threshold for window cooling decisions.",
        "category": "core",
    },
    "setback_heat": {
        "label": "Setback Heat (°F)",
        "description": "Temperature when heating and away from home. Lower values save more energy but take longer to recover when you return.",
        "category": "core",
    },
    "setback_cool": {
        "label": "Setback Cool (°F)",
        "description": "Temperature when cooling and away from home. Higher values save more energy but take longer to cool down when you return.",
        "category": "core",
    },
    "notify_service": {
        "label": "Notification Service",
        "description": "The HA notify service used for alerts and briefings (e.g., notify.mobile_app).",
        "category": "core",
    },
    "email_notify": {
        "label": "Email Notifications",
        "description": "When enabled, briefings and alerts are also sent via the send_email notify service.",
        "category": "core",
    },
    "outdoor_temp_source": {
        "label": "Outdoor Temp Source",
        "description": "Where outdoor temperature is read from: the weather service, a dedicated sensor, or an input_number helper.",
        "category": "sensors",
    },
    "indoor_temp_source": {
        "label": "Indoor Temp Source",
        "description": "Where indoor temperature is read from: the thermostat's built-in sensor, a dedicated sensor, or an input_number helper.",
        "category": "sensors",
    },
    "door_window_sensors": {
        "label": "Door/Window Sensors",
        "description": "Binary sensors that detect open doors and windows. When open past the debounce period, HVAC pauses to avoid wasting energy.",
        "category": "sensors",
    },
    "sensor_polarity_inverted": {
        "label": "Sensor Polarity Inverted",
        "description": "Enable if your sensors report 'off' when open (some reed switches work this way). Incorrect polarity means HVAC pauses when doors are closed.",
        "category": "sensors",
    },
    "sensor_debounce_seconds": {
        "label": "Sensor Debounce (minutes)",
        "description": "How long a door/window must stay open before HVAC pauses. Short values react faster but may cause unnecessary pauses for quick trips through a door.",
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
    },
    "manual_grace_seconds": {
        "label": "Manual Grace Period (minutes)",
        "description": "After you manually turn HVAC back on during a sensor pause, this grace window prevents re-pausing. Gives you time to close up without the system cycling.",
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
    },
    "manual_grace_notify": {
        "label": "Manual Grace Notifications",
        "description": "Send a notification when the manual grace period expires and normal sensor behavior resumes.",
        "category": "sensors",
    },
    "automation_grace_seconds": {
        "label": "Automation Grace Period (minutes)",
        "description": "After Climate Advisor resumes HVAC (all doors/windows closed), this grace window prevents immediate re-pausing if a door opens briefly.",
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
    },
    "automation_grace_notify": {
        "label": "Automation Grace Notifications",
        "description": "Send a notification when the automation grace period expires.",
        "category": "sensors",
    },
    "fan_mode": {
        "label": "Fan Control Mode",
        "description": "Controls how fans assist ventilation. 'Whole house fan' controls a dedicated entity. 'HVAC fan' uses the thermostat fan mode. 'Both' uses both. Fan activates during economizer maintain phase.",
        "category": "fan",
    },
    "fan_entity": {
        "label": "Fan Entity",
        "description": "The fan or switch entity to control for whole-house ventilation. Only used when fan mode is 'whole_house_fan' or 'both'.",
        "category": "fan",
    },
    "home_toggle_entity": {
        "label": "Home/Away Toggle",
        "description": "An entity that indicates whether someone is home. ON = home, OFF = away. Climate Advisor applies setback temperatures when away.",
        "category": "occupancy",
    },
    "home_toggle_invert": {
        "label": "Invert Home Toggle",
        "description": "Enable if your toggle reports ON when you're away and OFF when you're home.",
        "category": "occupancy",
    },
    "vacation_toggle_entity": {
        "label": "Vacation Mode Toggle",
        "description": "An entity that indicates vacation mode. When active, Climate Advisor applies a deeper temperature setback for extended energy savings.",
        "category": "occupancy",
    },
    "vacation_toggle_invert": {
        "label": "Invert Vacation Toggle",
        "description": "Enable if your toggle reports ON when you're NOT on vacation.",
        "category": "occupancy",
    },
    "guest_toggle_entity": {
        "label": "Guest Mode Toggle",
        "description": "An entity that indicates guests are present. Overrides vacation and away modes — the house stays at comfort temperature while guests are visiting.",
        "category": "occupancy",
    },
    "guest_toggle_invert": {
        "label": "Invert Guest Toggle",
        "description": "Enable if your toggle reports ON when guests are NOT present.",
        "category": "occupancy",
    },
    "wake_time": {
        "label": "Wake Time",
        "description": "When morning comfort temperatures are restored. Earlier times mean the house is comfortable when you get up but use more energy overnight.",
        "category": "schedule",
    },
    "sleep_time": {
        "label": "Sleep Time",
        "description": "When bedtime setbacks begin. The system sets back 4°F for heating or +3°F for cooling to save energy while you sleep.",
        "category": "schedule",
    },
    "briefing_time": {
        "label": "Briefing Time",
        "description": "When the daily climate briefing is generated and sent. Should be before wake_time so you see it when you get up.",
        "category": "schedule",
    },
    "learning_enabled": {
        "label": "Learning Engine",
        "description": "When enabled, Climate Advisor tracks patterns (manual overrides, window compliance, runtime) and generates adaptive suggestions over time.",
        "category": "advanced",
    },
    "aggressive_savings": {
        "label": "Prefer Savings Over Comfort",
        "description": "When enabled, favors energy savings: the economizer skips AC-assisted cooling (ventilation only when windows open), and setbacks may be more aggressive. When disabled, AC actively cools to comfort when outdoor temps drop.",
        "category": "advanced",
    },
}
