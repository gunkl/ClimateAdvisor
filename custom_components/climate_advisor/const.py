"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

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

# Debounce and grace period defaults (seconds)
DEFAULT_SENSOR_DEBOUNCE_SECONDS = 300  # 5 minutes
DEFAULT_MANUAL_GRACE_SECONDS = 1800  # 30 minutes
DEFAULT_AUTOMATION_GRACE_SECONDS = 3600  # 60 minutes
OCCUPANCY_SETBACK_MINUTES = 15
MAX_CONTINUOUS_RUNTIME_HOURS = 3

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
