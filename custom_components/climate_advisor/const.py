"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

# Integration version — MUST match manifest.json "version" field.
# A test in tests/test_version_sync.py enforces this.
VERSION = "0.6.82"

GITHUB_REPO = "gunkl/ClimateAdvisor"
GITHUB_REPO_URL = "https://github.com/gunkl/ClimateAdvisor"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_CONTEXT_TIMEOUT = 5.0  # seconds — skip if API is slow
GITHUB_ISSUES_LIMIT = 15  # max issues to include in context

CONF_GITHUB_TOKEN = "github_token"
CONF_GITHUB_REPO = "github_repo"
API_SUBMIT_GITHUB_ISSUE = "/api/climate_advisor/submit_github_issue"

# Default setpoints (°F) — reformatted to match a real, tuned installation's own
# configured values (architecture-reset session, user-requested) rather than
# arbitrary round numbers. Does NOT affect the version 14->15 migration in
# __init__.py, which intentionally preserves the OLD historical defaults
# (70/75/60/80) as its own literal fallbacks for backfilling PRE-EXISTING
# installs that upgrade through that specific version transition — that is
# backward-compatibility logic, not a "new install" default, and must not change.
DEFAULT_COMFORT_HEAT = 68
DEFAULT_COMFORT_COOL = 74
DEFAULT_SETBACK_HEAT = 63
DEFAULT_SETBACK_COOL = 79

# Day type classifications
DAY_TYPE_HOT = "hot"
DAY_TYPE_WARM = "warm"
DAY_TYPE_MILD = "mild"
DAY_TYPE_COOL = "cool"
DAY_TYPE_COLD = "cold"

# Day type thresholds (°F) — used as defaults when user has not customised them.
THRESHOLD_HOT = 85
THRESHOLD_WARM = 75
THRESHOLD_MILD = 60
THRESHOLD_COOL = 45
CLASSIFICATION_HYSTERESIS_F = 2  # °F dead zone to prevent threshold bouncing

# Configurable day-type threshold keys and defaults.
# These mirror the THRESHOLD_* constants above; existing installs receive the
# same values via the v15→v16 migration default, so behaviour is unchanged.
CONF_THRESHOLD_HOT = "threshold_hot"
CONF_THRESHOLD_WARM = "threshold_warm"
CONF_THRESHOLD_MILD = "threshold_mild"
CONF_THRESHOLD_COOL = "threshold_cool"
DEFAULT_THRESHOLD_HOT = THRESHOLD_HOT
DEFAULT_THRESHOLD_WARM = THRESHOLD_WARM
DEFAULT_THRESHOLD_MILD = THRESHOLD_MILD
DEFAULT_THRESHOLD_COOL = THRESHOLD_COOL

# Trend thresholds (°F difference to trigger predictive behavior)
TREND_THRESHOLD_SIGNIFICANT = 10
TREND_THRESHOLD_MODERATE = 5

# Timing
DOOR_WINDOW_PAUSE_SECONDS = 180  # deprecated — use CONF_SENSOR_DEBOUNCE instead

# Door/window sensor configuration
CONF_SENSOR_POLARITY_INVERTED = "sensor_polarity_inverted"

# Temperature unit preference (stored as canonical fahrenheit internally)
CONF_TEMP_UNIT = "temp_unit"
DEFAULT_TEMP_UNIT = "fahrenheit"

# Thermal learning feature toggles (Issue #61)
CONF_ADAPTIVE_PREHEAT = "adaptive_preheat_enabled"
CONF_ADAPTIVE_SETBACK = "adaptive_setback_enabled"
CONF_WEATHER_BIAS = "weather_bias_enabled"

# Thermal learning threshold config keys (Issue #62)
CONF_MIN_PREHEAT_MINUTES = "min_preheat_minutes"
CONF_MAX_PREHEAT_MINUTES = "max_preheat_minutes"
CONF_DEFAULT_PREHEAT_MINUTES = "default_preheat_minutes"
CONF_PREHEAT_SAFETY_MARGIN = "preheat_safety_margin"
CONF_MAX_SETBACK_DEPTH = "max_setback_depth_f"

# Debounce and grace period config keys
CONF_SENSOR_DEBOUNCE = "sensor_debounce_seconds"
CONF_MANUAL_GRACE_PERIOD = "manual_grace_seconds"
CONF_MANUAL_GRACE_NOTIFY = "manual_grace_notify"
CONF_AUTOMATION_GRACE_PERIOD = "automation_grace_seconds"
CONF_AUTOMATION_GRACE_NOTIFY = "automation_grace_notify"
CONF_WELCOME_HOME_DEBOUNCE = "welcome_home_debounce_seconds"
CONF_OVERRIDE_CONFIRM_PERIOD = "override_confirm_seconds"
CONF_EMAIL_NOTIFY = "email_notify"  # DEPRECATED — replaced by per-event toggles in v8

# Per-event push notification toggles (Issue #50)
CONF_PUSH_BRIEFING = "push_briefing"
CONF_PUSH_DOOR_WINDOW_PAUSE = "push_door_window_pause"
CONF_PUSH_OCCUPANCY_HOME = "push_occupancy_home"

# Per-event email notification toggles (Issue #50)
CONF_EMAIL_BRIEFING = "email_briefing"
CONF_EMAIL_DOOR_WINDOW_PAUSE = "email_door_window_pause"
CONF_EMAIL_GRACE_EXPIRED = "email_grace_expired"
CONF_EMAIL_GRACE_REPAUSE = "email_grace_repause"
CONF_EMAIL_OCCUPANCY_HOME = "email_occupancy_home"

# Startup coalescing window: suppress override detection for this many seconds after restart
STARTUP_COALESCE_SECONDS: int = 300  # 5 minutes (Issue #321)

# Debounce and grace period defaults (seconds)
DEFAULT_SENSOR_DEBOUNCE_SECONDS = 600  # 10 minutes (Issue #504 — was 5 min)
DEFAULT_MANUAL_GRACE_SECONDS = 1800  # 30 minutes
DEFAULT_AUTOMATION_GRACE_SECONDS = 300  # 5 minutes
DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS = 3600  # 60 minutes
DEFAULT_OVERRIDE_CONFIRM_SECONDS = 600  # 10 minutes
OCCUPANCY_SETBACK_MINUTES = 15
MAX_CONTINUOUS_RUNTIME_HOURS = 3

# Issue #444: _apply_comfort_band() has no source-of-truth "did the band actually
# change" check, so overlapping triggers (startup coalesce + its own follow-on
# Issue #444's original COMFORT_BAND_EVENT_DEDUP_SECONDS (10-minute time-windowed dedup)
# was replaced by Issue #591's shared, permanent (content-keyed) AutomationEngine.
# _recent_duplicate() helper — see automation.py._apply_comfort_band(). A real 11-minute
# production gap slipped past the old fixed window (Issue #591/#590 Finding D/Delta 1).

# Issue #530: an RF-remote-timer-linked manual grace period's software-tracked expiry and
# the timer's own hardware-side completion are the same physical event, but don't land at
# the exact same instant — confirmed live at an 11-second gap, with follow-on RF chatter
# settling within 60 seconds. This window (generous vs. that observed gap) marks how long
# after such a grace expires a fan-off report is still treated as the tail of that SAME
# timer boundary, not a fresh, independent event requiring its own new grace period.
TIMER_BOUNDARY_SETTLE_SECONDS = 120  # 2 minutes

# Economizer (window cooling) threshold
ECONOMIZER_TEMP_DELTA = 3  # °F — activate when outdoor temp within this delta of comfort_cool

# Economizer time boundaries for hot-day window cooling
ECONOMIZER_MORNING_START_HOUR = 6  # 6:00 AM
ECONOMIZER_MORNING_END_HOUR = 9  # 9:00 AM
ECONOMIZER_EVENING_START_HOUR = 17  # 5:00 PM
ECONOMIZER_EVENING_END_HOUR = 24  # midnight (end of day)

# Warm-day window timing — open early morning, close before outdoor temps climb
WARM_WINDOW_OPEN_HOUR = 6  # 6:00 AM
WARM_WINDOW_CLOSE_HOUR = 10  # 10:00 AM

# MILD-day window timing — open mid-morning, close late afternoon (Issue #147)
MILD_WINDOW_OPEN_HOUR = 10  # 10:00 AM fallback (was hardcoded in classifier.py)
MILD_WINDOW_CLOSE_HOUR = 17  # 5:00 PM fallback

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
CONF_FAN_STATE_ENTITY = "fan_state_entity"  # Issue #359: WHF Type 2 dual-entity support
CONF_FAN_STATE_FEEDBACK = "fan_state_feedback"  # Issue #361: command-only vs feedback mode
CONF_FAN_MODE = "fan_mode"
FAN_MODE_DISABLED = "disabled"
FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
FAN_MODE_HVAC = "hvac_fan"
FAN_MODE_BOTH = "both"
DEFAULT_FAN_MODE = FAN_MODE_DISABLED

# Minimum fan runtime per hour (Issue #77)
CONF_FAN_MIN_RUNTIME_PER_HOUR = "fan_min_runtime_per_hour"
DEFAULT_FAN_MIN_RUNTIME_PER_HOUR = 0  # minutes; 0 = disabled

# QuietCool RF remote timer events (Issue #486)
# Optional `event.*` entity from the gunkl/quietcool-house-fan ESPHome firmware.
# See docs/fan-remote-spec.md for the full firmware event contract.
CONF_FAN_REMOTE_ENTITY = "fan_remote_entity"
REMOTE_TIMER_EVENT_HOURS = {
    "timer_1h": 1.0,
    "timer_2h": 2.0,
    "timer_4h": 4.0,
    "timer_8h": 8.0,
    "timer_12h": 12.0,
    "timer_none": None,  # remote's default: use configured manual_grace_seconds
}

# QuietCool RF remote speed events (Issue #519). The firmware already emits these on an
# explicit speed-select press (0x1F/0x2F/0x3F); CA previously dropped them entirely. No
# CONFIG_METADATA entry -- this is not user-facing config, it's a fixed token set.
REMOTE_SPEED_TOKENS = frozenset({"low", "medium", "high"})

# Issue #519: window to combine a single physical multi-field remote interaction (a speed
# confirmation and a timer confirmation, transmitted as separate packets moments apart for
# ONE user action) into one decision instead of two. Grounded in the firmware's own documented
# protocol timing (docs/remote-capture-protocol.md in gunkl/quietcool-house-fan):
# SAME_BURST_TOLERANCE_MS=400ms per-value repeat spacing, CONFIRM_WINDOW_MS=1500ms per-field
# confirm cycle, multi-field bursts observed arriving within a similar few-second span.
# Internal-only, not user-configurable. Provisional pending live-hardware confirmation after
# the firmware change ships -- see the Verification step that tunes this against real capture
# data, same status as the firmware's own SELF_ECHO_WINDOW_MS.
REMOTE_BURST_WINDOW_SECONDS: float = 1.5

# Issue #519: object_id substring hint used to find the sibling ambient-speed text_sensor on
# the same ESPHome device as CONF_FAN_REMOTE_ENTITY, via the entity/device registry. Kept
# liberal (not an exact suffix match) since firmware naming could vary across forks/versions.
REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS: tuple[str, ...] = ("speed",)

# Natural ventilation mode (door/window open + outdoor air within comfort range)
CONF_NATURAL_VENT_DELTA = "natural_vent_delta"
# Ceiling tolerance above comfort_cool for nat vent.
# Outdoor must also be below current indoor temperature (see NAT_VENT_HYSTERESIS_F guard).
DEFAULT_NATURAL_VENT_DELTA = 3.0

# Nat vent re-activation guards (Philosopher-approved, Issue #115)
# After an outdoor-warm exit (outdoor ≥ indoor), outdoor must be this many °F
# below indoor before re-activation is allowed. Prevents oscillation at equilibrium.
NAT_VENT_HYSTERESIS_F = 1.0

# Minimum seconds between an outdoor-warm exit and the next re-activation check.
# 5 minutes prevents whiplash cycling when temps are near-equal.
NAT_VENT_REACTIVATION_LOCKOUT_S = 300

CONF_NAT_VENT_HYSTERESIS_F = "nat_vent_hysteresis_f"
CONF_NAT_VENT_REACTIVATION_LOCKOUT_S = "nat_vent_reactivation_lockout_s"

# Issue #685: shadow-engine diagnostic cascade-noise debounce. Real multi-step
# production transitions can fire several distinct top-level automation-engine
# method calls in a fast cascade (confirmed via live log evidence: a 2026-08-19
# 04:55:20-04:55:31 cascade hit 4 comparison axes, resolved in 11.71s). A WARNING
# is only logged once a comparison axis has continuously disagreed for this many
# wall-clock seconds — not a count of consecutive snapshots, since duplicate
# mirrored calls can fire 1-2ms apart during a real cascade.
SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S = 60  # Issue #685: cascade-noise debounce, see investigation evidence

# Issue #641: hard safety floor on CA-issued fan (WHF/HVAC-fan) toggle frequency —
# defense-in-depth against ANY future oscillation bug (not tied to one root cause),
# protecting the physical relay from rapid on/off/on cycling. A plain internal safety
# constant, not a CONF_* option (matching NAT_VENT_HYSTERESIS_F/MIN_VIABLE_NAT_VENT_HOURS
# precedent) — not something a user should be able to weaken below what the hardware needs.
FAN_MIN_TOGGLE_INTERVAL_S = 300

# Nat-vent soft-start sub-mode (Issue #540, scoped from #533): WHF-purge/comfort activation
# at outdoor/indoor parity once today's outdoor temp is confirmed past its peak and
# declining. Opt-out (default on) — users who want the old strict-delta-only behavior can
# disable it. No humidity/dew-point guard exists today; the comfort benefit itself is
# still subjective, but the project has chosen to default this on rather than opt-in.
CONF_NAT_VENT_SOFT_START_ENABLED = "nat_vent_soft_start_enabled"
DEFAULT_NAT_VENT_SOFT_START_ENABLED = True

# Degrees below today's observed outdoor peak required before soft-start considers the
# day "declining" — mirrors NAT_VENT_HYSTERESIS_F's role as a noise-margin buffer.
PEAK_DECLINE_MARGIN_F = 1.0

# Minimum viable nat vent window — skip activation (or exit proactively) if thermal
# model predicts indoor will hit comfort_heat floor within this many hours.
MIN_VIABLE_NAT_VENT_HOURS = 1.0

# State persistence
STATE_FILE = "climate_advisor_state.json"

# Chart state log
CHART_LOG_FILE = "climate_advisor_chart_log.json"
CHART_LOG_MAX_DAYS = 365  # 1-year rolling cap (~17,500 entries ≈ 2MB)
CHART_DOWNSAMPLE_HOURLY_DAYS = 3  # raw points for ≤3 days; hourly averages beyond
CHART_DOWNSAMPLE_DAILY_DAYS = 30  # daily summaries for >30 days

# Prediction archive — first-write-wins historical pred_indoor
PRED_ARCHIVE_HORIZON_HOURS = 4  # only archive ODE entries within this lookahead window

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
ATTR_BRIEFING_SHORT = "daily_briefing_short"
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
ATTR_FAN_STATUS = "fan_status"
ATTR_WHF_STATUS = "whf_status"
ATTR_HVAC_FAN_STATUS = "hvac_fan_status"
ATTR_FAN_RUNTIME = "fan_runtime_minutes"
ATTR_FAN_OVERRIDE_SINCE = "fan_override_since"
ATTR_FAN_RUNNING = "fan_running"
ATTR_CURRENT_SETPOINT = "current_setpoint"
ATTR_INDOOR_TEMP = "indoor_temp"
ATTR_OUTDOOR_TEMP = "outdoor_temp"
ATTR_FORECAST_HIGH = "forecast_high"
ATTR_FORECAST_LOW = "forecast_low"
ATTR_FORECAST_HIGH_TOMORROW = "forecast_high_tomorrow"
ATTR_FORECAST_LOW_TOMORROW = "forecast_low_tomorrow"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_HVAC_RUNTIME_TODAY = "hvac_runtime_today"
ATTR_CONTACT_STATUS = "contact_status"

# Revisit delay — follow-up check after any HVAC action (seconds)
REVISIT_DELAY_SECONDS = 300  # 5 minutes

# Event log ring buffer cap (Issue #76)
EVENT_LOG_CAP = 500  # keep last 500 events

# Real WARNING+/ERROR log-record ring buffer cap (Issue #578) — see log_capture.py
LOG_CAPTURE_CAP = 200

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
API_CANCEL_OVERRIDE = f"{API_BASE}/cancel_override"
API_CANCEL_FAN_OVERRIDE = f"{API_BASE}/cancel_fan_override"
API_RESUME_FROM_PAUSE = f"{API_BASE}/resume_from_pause"
API_TOGGLE_AUTOMATION = f"{API_BASE}/toggle_automation"
API_EVENT_LOG = f"{API_BASE}/event_log"
API_ENGINES = f"{API_BASE}/engines"

# Panel
PANEL_URL = "/climate_advisor/frontend"
PANEL_FRONTEND_PATH = "climate-advisor"

# Configuration metadata for the Settings tab.
# When adding new config options, update this dict so the Settings tab
# displays the new option with a proper description.
CONFIG_METADATA = {
    "weather_entity": {
        "label": "Weather Entity",
        "description": (
            "The weather integration used for forecast data."
            " Determines day type classification and all downstream automation decisions."
        ),
        "category": "core",
    },
    "climate_entity": {
        "label": "Thermostat Entity",
        "description": (
            "The climate entity Climate Advisor controls. All HVAC mode and temperature commands go to this entity."
        ),
        "category": "core",
    },
    "comfort_heat": {
        "label": "Comfort Heat",
        "description": (
            "Target temperature when heating is active. Lowering saves energy but may feel cooler."
            " Used for morning wake-up and occupancy-home restores."
        ),
        "category": "setpoints",
    },
    "comfort_cool": {
        "label": "Comfort Cool",
        "description": (
            "Target temperature when cooling is active. Raising saves energy but may feel warmer."
            " The economizer uses this as the threshold for window cooling decisions."
        ),
        "category": "setpoints",
    },
    "setback_heat": {
        "label": "Setback Heat",
        "description": (
            "Temperature when heating and away from home."
            " Lower values save more energy but take longer to recover when you return."
        ),
        "category": "setpoints",
    },
    "setback_cool": {
        "label": "Setback Cool",
        "description": (
            "Temperature when cooling and away from home."
            " Higher values save more energy but take longer to cool down when you return."
        ),
        "category": "setpoints",
    },
    "notify_service": {
        "label": "Notification Service",
        "description": "The HA notify service used for alerts and briefings (e.g., notify.mobile_app).",
        "category": "core",
    },
    CONF_TEMP_UNIT: {
        "label": "Temperature Unit",
        "description": (
            "Whether setpoints and displayed temperatures use Fahrenheit or Celsius. "
            "Setpoints are stored internally in Fahrenheit; changing this unit affects "
            "how they are displayed and entered in the UI."
        ),
        "category": "core",
    },
    "outdoor_temp_source": {
        "label": "Outdoor Temp Source",
        "description": (
            "Where outdoor temperature is read from:"
            " the weather service, a dedicated sensor, or an input_number helper."
        ),
        "category": "sensors",
    },
    "indoor_temp_source": {
        "label": "Indoor Temp Source",
        "description": (
            "Where indoor temperature is read from:"
            " the thermostat's built-in sensor, a dedicated sensor, or an input_number helper."
        ),
        "category": "sensors",
    },
    "door_window_sensors": {
        "label": "Door/Window Sensors",
        "description": (
            "Binary sensors that detect open doors and windows."
            " When open past the debounce period, HVAC pauses to avoid wasting energy."
        ),
        "category": "sensors",
    },
    "sensor_polarity_inverted": {
        "label": "Sensor Polarity Inverted",
        "description": (
            "Enable if your sensors report 'off' when open (some reed switches work this way)."
            " Incorrect polarity means HVAC pauses when doors are closed."
        ),
        "category": "sensors",
    },
    "sensor_debounce_seconds": {
        "label": "Sensor Debounce (minutes)",
        "description": (
            "How long a door/window sensor's state must hold steady before Climate Advisor acts on"
            " it — pausing/resuming HVAC, or engaging/exiting natural-ventilation fan control"
            " (whole-house fan or HVAC fan). Applies to every controlled device, not just HVAC."
            " Short values react faster but are more exposed to quick trips through a door or a"
            " flaky sensor bounce; longer values are steadier but slower to respond to a genuine change."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    },
    "manual_grace_seconds": {
        "label": "Manual Grace Period (minutes)",
        "description": (
            "After you manually change the thermostat — either the HVAC mode or the target temperature —"
            " CA waits this many minutes before resuming automated setpoint control."
            " Also prevents re-pausing if a door/window opens during this window. Default: 30 minutes."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_MANUAL_GRACE_SECONDS,
    },
    "manual_grace_notify": {
        "label": "Push: Manual Grace Expired",
        "description": "Push notification when manual grace expires and normal behavior resumes.",
        "category": "notifications",
    },
    "automation_grace_seconds": {
        "label": "Automation Grace Period (minutes)",
        "description": (
            "After Climate Advisor resumes HVAC (all doors/windows closed),"
            " this grace window prevents immediate re-pausing if a door opens briefly."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_AUTOMATION_GRACE_SECONDS,
    },
    "automation_grace_notify": {
        "label": "Push: Automation Grace Expired",
        "description": "Send a push notification when the automation grace period expires.",
        "category": "notifications",
    },
    "override_confirm_seconds": {
        "label": "Override Confirmation Delay (minutes)",
        "description": (
            "Time between system changes and confirmation of manual override."
            " When a change looks like a manual override, Climate Advisor waits this long before formally accepting it."
            " Transient events (thermostat restart, fan cycle) that resolve within the window are ignored."
            " Set to 0 to confirm overrides immediately."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    },
    "fan_mode": {
        "label": "Fan Control Mode",
        "description": (
            "Controls how fans assist ventilation. 'Whole house fan' controls a dedicated entity."
            " 'HVAC fan' uses the thermostat fan mode."
            " Fan activates during economizer maintain phase."
        ),
        "category": "fan",
    },
    "nat_vent_soft_start_enabled": {
        "label": "Nat-Vent Soft-Start (Purge Mode)",
        "description": (
            "When enabled, the whole-house fan may start at outdoor/indoor temperature parity"
            " (not waiting for outdoor to be measurably cooler) once today's outdoor temperature"
            " is confirmed past its peak and declining — for air movement and attic/thermal-mass"
            " purge, not bulk cooling. On by default; disable if you only want the fan to run"
            " once outdoor is measurably cooler than indoor. No humidity/dew-point sensor guards"
            " this today."
        ),
        "category": "fan",
    },
    "fan_entity": {
        "label": "Fan Entity",
        "description": (
            "The fan or switch entity to control for whole-house ventilation."
            " Only used when fan mode is 'whole_house_fan' or 'both'."
        ),
        "category": "fan",
    },
    "fan_remote_entity": {
        "label": "Fan RF Remote Event Entity",
        "description": (
            "Optional event entity (e.g. from the gunkl/quietcool-house-fan ESPHome firmware) that"
            " fires when the physical RF wall remote is pressed. When set, a remote timer selection"
            " (1/2/4/8/12 hours) sets the duration of the fan manual-override grace period, so CA"
            " honors the user's chosen runtime instead of the configured default. Leave blank to"
            " disable — no subscription is created and behavior is unchanged."
        ),
        "sensitive": False,
        "category": "fan",
    },
    "fan_state_entity": {
        "label": "Fan State Entity",
        "description": (
            "Optional separate sensor entity to read the actual physical state of the whole-house fan."
            " Use when the fan has a dedicated control entity and a separate state sensor (WHF dual-entity)."
            " If left blank, the Fan Entity is used for both control and state."
        ),
        "sensitive": False,
        "category": "fan",
    },
    "fan_state_feedback": {
        "label": "Fan state feedback reliable",
        "description": (
            "Turn ON if your fan entity or state sensor reports actual motor state "
            "(not just the last command sent). Leave OFF if you're not sure — CA will "
            "command the fan to the desired state on every cycle without reading back "
            "the entity state. Physical wall-switch overrides are undetectable when OFF."
        ),
        "category": "fan",
        "sensitive": False,
        "default": False,
    },
    "fan_min_runtime_per_hour": {
        "label": "Fan Minimum Runtime Per Hour",
        "description": (
            "Minutes of fan runtime per hour (0 = disabled, 60 = always on)."
            " Activates the fan for the specified duration each hour for air"
            " circulation. The cycle start time is offset from the clock hour"
            " based on when HA started."
        ),
        "category": "fan",
    },
    "home_toggle_entity": {
        "label": "Home/Away Toggle",
        "description": (
            "An entity that indicates whether someone is home. ON = home, OFF = away."
            " Climate Advisor applies setback temperatures when away."
        ),
        "category": "occupancy",
    },
    "home_toggle_invert": {
        "label": "Invert Home Toggle",
        "description": "Enable if your toggle reports ON when you're away and OFF when you're home.",
        "category": "occupancy",
    },
    "vacation_toggle_entity": {
        "label": "Vacation Mode Toggle",
        "description": (
            "An entity that indicates vacation mode."
            " When active, Climate Advisor applies a deeper temperature setback for extended energy savings."
        ),
        "category": "occupancy",
    },
    "vacation_toggle_invert": {
        "label": "Invert Vacation Toggle",
        "description": "Enable if your toggle reports ON when you're NOT on vacation.",
        "category": "occupancy",
    },
    "guest_toggle_entity": {
        "label": "Guest Mode Toggle",
        "description": (
            "An entity that indicates guests are present."
            " Overrides vacation and away modes — the house stays at comfort temperature while guests are visiting."
        ),
        "category": "occupancy",
    },
    "guest_toggle_invert": {
        "label": "Invert Guest Toggle",
        "description": "Enable if your toggle reports ON when guests are NOT present.",
        "category": "occupancy",
    },
    "welcome_home_debounce_seconds": {
        "label": "Welcome Home Quiet Period (minutes)",
        "description": (
            "Minimum time between welcome home notifications. If someone leaves and returns"
            " within this window, the notification is suppressed. Set to 0 to always notify."
        ),
        "category": "occupancy",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    },
    "wake_time": {
        "label": "Wake Time",
        "description": (
            "When morning comfort temperatures are restored."
            " Earlier times mean the house is comfortable when you get up but use more energy overnight."
        ),
        "category": "schedule",
    },
    "sleep_time": {
        "label": "Sleep Time",
        "description": (
            "When bedtime temperatures take effect. The system transitions to your sleep temperatures at this time."
        ),
        "category": "schedule",
    },
    "sleep_heat": {
        "label": "Sleep Temperature (Heat)",
        "description": (
            "Target temperature during sleep hours when you are home."
            " Independent from your away setback — use this to stay warmer at night"
            " than when you leave the house."
        ),
        "category": "setpoints",
    },
    "sleep_cool": {
        "label": "Sleep Temperature (Cool)",
        "description": (
            "Target temperature during sleep hours when you are home."
            " Independent from your away setback — use this to stay cooler at night"
            " than when you leave the house."
        ),
        "category": "setpoints",
    },
    "briefing_time": {
        "label": "Briefing Time",
        "description": (
            "When the daily climate briefing is generated and sent."
            " Should be before wake_time so you see it when you get up."
        ),
        "category": "schedule",
    },
    "learning_enabled": {
        "label": "Learning Engine",
        "description": (
            "When enabled, Climate Advisor tracks patterns"
            " (manual overrides, window compliance, runtime) and generates adaptive suggestions over time."
        ),
        "category": "advanced",
    },
    "adaptive_preheat_enabled": {
        "category": "advanced",
        "label": "Adaptive Pre-heat Timing",
        "description": "Use learned heating rate to compute pre-heat start time.",
    },
    "adaptive_setback_enabled": {
        "category": "advanced",
        "label": "Adaptive Bedtime Setback",
        "description": "Use learned heating/cooling rate to compute maximum safe setback depth.",
    },
    "weather_bias_enabled": {
        "category": "advanced",
        "label": "Weather Forecast Bias Correction",
        "description": (
            "Apply a location-specific correction to tomorrow's forecast based on observed forecast accuracy."
        ),
    },
    "min_preheat_minutes": {
        "label": "Minimum Pre-heat Time (min)",
        "description": "Shortest pre-heat window the system will ever schedule.",
        "category": "advanced",
    },
    "max_preheat_minutes": {
        "label": "Maximum Pre-heat Time (min)",
        "description": "Longest pre-heat window the system will ever schedule.",
        "category": "advanced",
    },
    "default_preheat_minutes": {
        "label": "Default Pre-heat Time (min)",
        "description": "Pre-heat duration used before enough observations are collected.",
        "category": "advanced",
    },
    "preheat_safety_margin": {
        "label": "Pre-heat Safety Margin",
        "description": ("Multiplier applied to model-computed pre-heat time as a buffer (e.g. 1.2 = 20% extra)."),
        "category": "advanced",
    },
    "max_setback_depth_f": {
        "label": "Maximum Setback Depth (°F)",
        "description": "Largest overnight setback the adaptive engine may compute.",
        "category": "advanced",
    },
    "aggressive_savings": {
        "label": "Prefer Savings Over Comfort",
        "description": (
            "When enabled, favors energy savings: the economizer skips AC-assisted cooling"
            " (ventilation only when windows open), and setbacks may be more aggressive."
            " When disabled, AC actively cools to comfort when outdoor temps drop."
        ),
        "category": "advanced",
    },
    "threshold_hot": {
        "label": "Hot Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature are classified as Hot. Default: 85°F / 29°C."
        ),
        "category": "advanced",
    },
    "threshold_warm": {
        "label": "Warm Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Hot) are"
            " classified as Warm. Default: 75°F / 24°C."
        ),
        "category": "advanced",
    },
    "threshold_mild": {
        "label": "Mild Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Warm) are"
            " classified as Mild. Default: 60°F / 16°C."
        ),
        "category": "advanced",
    },
    "threshold_cool": {
        "label": "Cool Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Mild) are"
            " classified as Cool; below is Cold. Default: 45°F / 7°C."
        ),
        "category": "advanced",
    },
    "push_briefing": {
        "label": "Push: Daily Briefing",
        "description": "Send a short TLDR briefing summary to your phone each morning.",
        "category": "notifications",
    },
    "push_door_window_pause": {
        "label": "Push: HVAC Paused",
        "description": "Send a push notification when HVAC is paused due to an open door or window.",
        "category": "notifications",
    },
    "push_occupancy_home": {
        "label": "Push: Welcome Home",
        "description": "Send a push notification when someone arrives home and comfort temperature is restored.",
        "category": "notifications",
    },
    "email_briefing": {
        "label": "Email: Full Daily Briefing",
        "description": "Send the full daily briefing via email with complete forecast and plan details.",
        "category": "notifications",
    },
    "email_door_window_pause": {
        "label": "Email: HVAC Paused",
        "description": "Send an email when HVAC is paused due to an open door or window.",
        "category": "notifications",
    },
    "email_grace_expired": {
        "label": "Email: Grace Period Expired",
        "description": "Send an email when a grace period expires and normal sensor behavior resumes.",
        "category": "notifications",
    },
    "email_grace_repause": {
        "label": "Email: Re-paused",
        "description": "Email when HVAC is re-paused because a door/window is still open after grace.",
        "category": "notifications",
    },
    "email_occupancy_home": {
        "label": "Email: Welcome Home",
        "description": "Send an email when someone arrives home and comfort temperature is restored.",
        "category": "notifications",
    },
    "ai_enabled": {
        "label": "Enable AI Features",
        "description": (
            "Master switch for all AI-powered features."
            " When disabled, Climate Advisor uses only its built-in coded logic."
        ),
        "category": "ai_settings",
    },
    "ai_api_key": {
        "label": "Claude API Key",
        "description": (
            "Your Anthropic API key. Stored securely in Home Assistant's config entry."
            " Never logged or exposed in sensor attributes."
        ),
        "category": "ai_settings",
        "sensitive": True,
    },
    "ai_model": {
        "label": "AI Model",
        "description": (
            "Which Claude model to use."
            " Sonnet is recommended for cost/quality balance."
            " Haiku is cheapest. Opus is most capable but expensive."
        ),
        "category": "ai_settings",
    },
    "ai_reasoning_effort": {
        "label": "Reasoning Effort",
        "description": (
            "How much reasoning effort Claude uses."
            " Higher effort produces better analysis but uses more tokens and costs more."
        ),
        "category": "ai_settings",
    },
    "ai_max_tokens": {
        "label": "Max Response Length (tokens)",
        "description": (
            "Maximum length of AI responses in tokens. Higher values allow more detailed analysis but cost more."
        ),
        "category": "ai_settings",
    },
    "ai_temperature": {
        "label": "Creativity (temperature)",
        "description": (
            "Controls randomness in AI responses. 0 = deterministic, 1.0 = most creative. 0.3 recommended for analysis."
        ),
        "category": "ai_settings",
    },
    "ai_monthly_budget": {
        "label": "Monthly Budget Cap ($)",
        "description": (
            "Maximum estimated monthly spend in USD. Set to 0 for no limit. AI features pause when budget is reached."
        ),
        "category": "ai_settings",
    },
    "ai_auto_requests_per_day": {
        "label": "Auto Requests Per Day",
        "description": (
            "Maximum automated/scheduled AI requests per day."
            " Limits unattended usage from features like daily plan generation."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
    "ai_manual_requests_per_day": {
        "label": "Manual Requests Per Day",
        "description": (
            "Maximum user-triggered AI requests per day."
            " Limits on-demand usage from features like the Investigative Analysis report."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_enabled": {
        "label": "Enable Investigative Agent",
        "description": (
            "Enable the investigative agent, which performs deep cross-source analysis"
            " to find incongruities, data quality issues, and system errors."
            " Requires AI to be enabled and configured. Default is off."
        ),
        "category": "ai_settings",
    },
    # ai_investigator_model / ai_investigator_reasoning_effort / ai_investigator_max_tokens
    # removed from the options UI (Issue #563) — the investigator now shares the single
    # `ai_model` config used everywhere else, instead of a separate persistent
    # model/reasoning/token-budget block. The CONF_AI_INVESTIGATOR_MODEL/_REASONING/
    # _MAX_TOKENS constants and their config-entry migration defaults are kept (not
    # deleted) purely so the historical v13->v14 config migration in __init__.py
    # doesn't break for very old installs — nothing reads these values anymore.
    "ai_investigator_requests_per_day": {
        "label": "Investigator Requests Per Day",
        "description": (
            "Maximum investigative analysis runs per day."
            " Each investigation uses extended thinking and is more expensive than other AI requests."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
}

# ---------------------------------------------------------------------------
# Thermal Model Learning (Issue #61)
# ---------------------------------------------------------------------------
MIN_THERMAL_SESSION_MINUTES = 5  # ignore sessions shorter than this (was 10; Ecobee cycles 7-9 min)
MIN_THERMAL_OBSERVATIONS = 5  # min obs before model is trusted
THERMAL_MODEL_MAX_OBS = 30  # use only most recent N observations
THERMAL_POST_HEAT_TIMEOUT_MINUTES = 45  # abandon post_heat phase after this long
THERMAL_STABILIZATION_THRESHOLD_F = 0.3  # |dT| < this over window → stabilized
THERMAL_STABILIZATION_WINDOW_MINUTES = 5  # window length for stabilization check
THERMAL_K_PASSIVE_MIN = -0.5  # reject k_passive outside this range (hr⁻¹)
THERMAL_K_PASSIVE_MAX = -0.001  # upper bound: near-zero decay (extremely well-insulated house)
THERMAL_K_ACTIVE_HEAT_MIN = 0.5  # reject k_active_heat outside this range (°F/hr)
THERMAL_K_ACTIVE_HEAT_MAX = 15.0  # upper bound: physically implausible heating rate
THERMAL_K_ACTIVE_COOL_MIN = -15.0  # reject k_active_cool outside this range (°F/hr)
THERMAL_K_ACTIVE_COOL_MAX = -0.5  # upper bound (least negative): minimal cooling effect
THERMAL_MIN_R_SQUARED = 0.2  # reject observation if R² below this
THERMAL_MIN_POST_HEAT_SAMPLES = 4  # min post-heat samples to commit (Issue #130: lowered from 10, enables short cycles)
THERMAL_PRE_HEAT_BUFFER_MINUTES = 15  # rolling pre-heat buffer length
THERMAL_SAMPLE_INTERVAL_SECONDS = 60  # sampling cadence during active/post_heat
THERMAL_MAX_ACTIVE_SAMPLES = 120  # cap on active_samples list per event
THERMAL_MAX_POST_HEAT_SAMPLES = 45  # cap on post_heat_samples list per event
DEFAULT_PREHEAT_MINUTES = 120  # fallback when no model data
MIN_PREHEAT_MINUTES = 30  # clamp floor
MAX_PREHEAT_MINUTES = 240  # clamp ceiling (4 hrs)
PREHEAT_SAFETY_MARGIN = 1.3  # multiply computed time by this
DEFAULT_SETBACK_DEPTH_F = 4.0  # preserved fallback (current heat setback)
DEFAULT_SETBACK_DEPTH_COOL_F = 3.0  # preserved fallback (current cool setback)

# Conservative heat setback on cold days (shallower than normal to aid morning recovery)
COLD_DAY_SETBACK_DEPTH_F: float = 3.0

# Window opportunity: today/tomorrow low must be at or below this to open windows on a hot day
WINDOW_OPPORTUNITY_MAX_LOW_F: float = 80.0

# Thermal factor bucket boundaries (outdoor temp in °F, internal representation)
THERMAL_COLD_BUCKET_LIMIT_F: float = 60.0  # below this → "cold" regime
THERMAL_MILD_BUCKET_LIMIT_F: float = 70.0  # below this (≥ cold limit) → "mild" regime

# Thermal factor interpolation zone half-width (°F either side of each bucket boundary)
# Eliminates hard jumps when outdoor temp crosses a threshold.
THERMAL_BUCKET_INTERP_HALF_F: float = 2.0

THERMAL_MIN_DECAY_F = 1.0  # min total post-heat decay required to commit (°F)

# --- v3 Observation Type string constants ---
OBS_TYPE_PASSIVE_DECAY = "passive_decay"
OBS_TYPE_FAN_ONLY_DECAY = "fan_only_decay"
OBS_TYPE_VENTILATED_DECAY = "ventilated_decay"
OBS_TYPE_SOLAR_GAIN = "solar_gain"
OBS_TYPE_HVAC_HEAT = "hvac_heat"
OBS_TYPE_HVAC_COOL = "hvac_cool"

# Thermal rejection reason codes (emitted in ThermalRejectionEvent)
REJECT_TOO_FEW_SAMPLES = "too_few_samples"
REJECT_SMALL_DELTA = "small_delta"
REJECT_OLS_BAD_FIT = "ols_bad_fit"
REJECT_OLS_WRONG_SIGN = "ols_wrong_sign"
REJECT_OLS_BOUNDS = "ols_bounds"
REJECT_ABANDONED = "abandoned"
REJECT_TOO_FEW_BLOCKS = "too_few_blocks"
REJECT_WINDOW_TOO_SHORT = "window_too_short"
REJECT_NO_INTERIOR_PEAK = "no_interior_peak"

# Reduced plateau guard (was THERMAL_MIN_DECAY_F = 1.0)
THERMAL_HVAC_MIN_DECAY_F = 0.3
# Minimum ΔT for single-point k_active estimate (filters sensor noise / no-effect cycles)
THERMAL_HVAC_MIN_SIGNAL_F: float = 0.5

# Thermostat swing (deadband half-amplitude) detection constants
THERMAL_SWING_DEFAULT_F: float = 1.5
THERMAL_SWING_MIN_F: float = 0.1
THERMAL_SWING_MAX_F: float = 5.0
THERMAL_SWING_CONF_LOW: int = 1
THERMAL_SWING_CONF_MEDIUM: int = 3
THERMAL_SWING_CONF_HIGH: int = 10

# Passive decay observation thresholds
THERMAL_PASSIVE_MIN_SAMPLES = 30
THERMAL_PASSIVE_MIN_DELTA_F = 3.0
THERMAL_PASSIVE_MIN_SIGNAL_F = 0.5

# Block-averaged OLS estimator for k_passive (dual-estimator framework, Issue #146)
THERMAL_BLOCK_OLS_BLOCK_MINUTES = 60  # width of each averaging block (minutes)
THERMAL_BLOCK_OLS_MIN_BLOCKS = 6  # minimum blocks required for OLS (≥6 → ≥6h window)
THERMAL_DUAL_AGREE_REL = 0.30  # max relative disagreement for endpoint+block to "agree"
THERMAL_DUAL_OLS_GOOD = 0.50  # block-OLS R² threshold for "good" quality
THERMAL_DUAL_OLS_OK = 0.20  # block-OLS R² threshold for "ok" quality

# Chart_log endpoint estimator thresholds (replaces passive_decay consecutive-pair OLS)
# Min window duration and temperature drop for passive-only and overnight ventilated windows.
THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES: int = 120  # 2h minimum window
THERMAL_CHART_LOG_PASSIVE_MIN_DT_F: float = 1.0  # at least 1°F sensor change
THERMAL_CHART_LOG_VENT_MIN_MINUTES: int = 120  # 2h minimum for overnight ventilated windows

# Fan-only decay observation thresholds
THERMAL_FAN_MIN_SAMPLES = 15
THERMAL_FAN_MIN_SIGNAL_F = 0.2

# Ventilated decay observation thresholds
THERMAL_VENT_MIN_SAMPLES = 20
THERMAL_VENT_MIN_SIGNAL_F = 0.3
# Lower trigger delta for ventilated_decay: k_vent_window is measurable at 1°F differential.
# passive_decay needs 3°F for sufficient envelope-decay SNR; vent obs measures a different
# phenomenon (air exchange rate) where smaller differentials still carry useful signal.
THERMAL_VENTILATED_MIN_DELTA_F: float = 1.0

# Solar gain observation thresholds
THERMAL_SOLAR_MIN_SAMPLES = 20
THERMAL_SOLAR_MIN_RATE_F_PER_HR = 0.5
THERMAL_SOLAR_DAYTIME_START_H = 8
THERMAL_SOLAR_DAYTIME_END_H = 18

# Solar phase offset (learning — Issue #147)
THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT = 2  # Prior before learning (peak at 3pm)
THERMAL_SOLAR_PHASE_OFFSET_MIN = 0  # Clamp lower bound
THERMAL_SOLAR_PHASE_OFFSET_MAX = 4  # Clamp upper bound (5pm max: offset=4 → peak at local hour 17)
THERMAL_SOLAR_PHASE_MIN_ENTRIES = 3  # Min chart_log entries in window
THERMAL_SOLAR_PHASE_MIN_WINDOW_H = 4  # Min window span (hours)
THERMAL_SOLAR_PHASE_MIN_DT_F = 1.5  # Min indoor ΔT for visible peak
THERMAL_SOLAR_PHASE_ALPHA = 0.10  # EWMA alpha (slow — stable building physics)
THERMAL_PARAM_STALE_DAYS = 90  # days — parameter older than this treated as None at resolver

# AC duty-cycle secondary solar phase estimator (Issue #312)
THERMAL_SOLAR_PHASE_AC_ALPHA = 0.07  # EWMA alpha (slower — less reliable signal)
THERMAL_SOLAR_PHASE_AC_MIN_OBS = 3  # Min observations before secondary is trusted
THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F = 68.0  # Setpoint range lower bound
THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F = 80.0  # Setpoint range upper bound
THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F = 1.5  # Max allowed setpoint spread (°F)
THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES = 4  # Min cool entries in 11:00-16:00 window
THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H = 11  # Peak window start (inclusive)
THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H = 16  # Peak window end (exclusive)
THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H = 18  # Setpoint stability check end (exclusive)
REJECT_AC_NO_COOL_SETPOINTS = "ac_no_cool_setpoints"
REJECT_AC_SETPOINT_UNSTABLE = "ac_setpoint_unstable"
REJECT_AC_SETPOINT_OUT_OF_RANGE = "ac_setpoint_out_of_range"
REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY = "ac_insufficient_midday_activity"
REJECT_AC_NO_SETPOINT_BREACH = "ac_no_setpoint_breach"

# Shared cap across all observation types
THERMAL_MAX_OBS_SAMPLES = 200

# v3 sampling redesign (Issue #122)
# THERMAL_DECAY_MAX_WINDOW_MINUTES is deprecated — subsumed by THERMAL_ROLLING_MAX_WINDOW_MINUTES (Issue #126).
# Kept here for backward compatibility; do not use in new code.
THERMAL_DECAY_MAX_WINDOW_MINUTES: int = 60  # wall-clock limit before vent/fan obs abandon
# Renamed from THERMAL_ROLLING_WINDOW_MINUTES — minimum window before first commit attempt.
THERMAL_ROLLING_MIN_WINDOW_MINUTES: int = 30
THERMAL_ROLLING_MAX_WINDOW_MINUTES: int = 240  # 4h hard cap; subsumes THERMAL_DECAY_MAX_WINDOW_MINUTES
THERMAL_ROLLING_WINDOW_MINUTES: int = THERMAL_ROLLING_MIN_WINDOW_MINUTES  # backward-compat alias
THERMAL_ROLLING_MIN_DELTA_T_F: float = 0.2  # min total indoor ΔT to commit a short window
# THERMAL_MIN_DECAY_SAMPLES is the single source of truth for OLS sample-pair floors.
# coordinator.py pre-gates on (THERMAL_MIN_DECAY_SAMPLES + 1) to guarantee at least
# THERMAL_MIN_DECAY_SAMPLES pairs are available for OLS.  Do not change either constant
# independently — the +1 offset is intentional and must be preserved.
THERMAL_MIN_DECAY_SAMPLES: int = 4  # min OLS pairs for rolling-window decay types (vs HVAC's 10)
THERMAL_SOLAR_FACTOR_MIN_RANGE: float = 0.30  # min solar_factor variance across samples for 2-param OLS
THERMAL_K_SOLAR_MAX_F_PER_HR: float = 8.0  # upper bound for k_solar (°F/hr); physical max ~6°F/hr on clear day
THERMAL_PASSIVE_SAMPLE_INTERVAL_S: int = 300  # 5 min — passive/vent slow decay
THERMAL_FAN_SAMPLE_INTERVAL_S: int = 120  # 2 min — fan-only (faster signal)
THERMAL_SOLAR_SAMPLE_INTERVAL_S: int = 300  # 5 min — solar gain slow trend
THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S: int = 300  # 5 min — post-heat is passive dynamics

# Per-type passive confidence count thresholds
THERMAL_PASSIVE_CONF_LOW = 5
THERMAL_PASSIVE_CONF_MEDIUM = 15
THERMAL_PASSIVE_CONF_HIGH = 30

# Sleep temperature config keys (Issue #101)
CONF_SLEEP_HEAT = "sleep_heat"
CONF_SLEEP_COOL = "sleep_cool"
DEFAULT_SLEEP_HEAT = 64.0  # comfort_heat(68) - DEFAULT_SETBACK_DEPTH_F(4) — still holds
DEFAULT_SLEEP_COOL = 72.0  # a real, tuned installation's own value — NOT derived from
# comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F (that formula assumes a warmer/looser
# overnight setback for economizing; this household's real preference is the opposite
# direction — cooler for sleep, not warmer — so this is now an independent flat default,
# matching the confirmed-correct P3 bedtime-application behavior (Issue #435/#436
# investigation found production already applies this flat value, not the formula).
MAX_SETBACK_DEPTH_F = 8.0  # never set back more than this
SETBACK_RECOVERY_BUFFER_MINUTES = 30  # pre-heat leads wake_time by this much

# ---------------------------------------------------------------------------
# Overnight Pre-Cool Phase (Issue #258)
# On warming-trend nights, CA applies a cooler ceiling mid-night to bank thermal mass.
# ---------------------------------------------------------------------------
PRE_COOL_POST_NAT_VENT_DELAY_MINUTES: int = 30  # delay after nat-vent window closes before AC pre-cool fires
PRE_COOL_WAKE_OFFSET_HOURS: float = 4.0  # fallback trigger: this many hours before wake_time
# Issue #558: fallback modifier used when overnight pre-cool is triggered by tomorrow's absolute
# hot-day classification rather than by a warming trend (setback_modifier stays 0 on a plateaued
# stretch of hot days, which would otherwise make compute_pre_cool_target() a no-op vs. the plain
# sleep_cool floor). Reuses the magnitude of the retired daytime hot-day catch-up offset, now
# applied only within this patient, nighttime-only mechanism.
HOT_DAY_PRE_COOL_MODIFIER: float = -2.0
# Pre-cool target floor is sleep_heat + nat_vent hysteresis (compute_pre_cool_target() in
# automation.py) — the same "+1 above the floor" convention nat_vent_temperature_check() uses
# for sleep-window fan cycling. Replaces the old comfort_heat + 2F floor (architecture-reset
# session), which left little to no headroom once DEFAULT_SLEEP_COOL was reformatted to a flat,
# cooler-than-daytime default (Issue #436).
THERMAL_OBS_CAP = 200  # max observations in LearningState

# ---------------------------------------------------------------------------
# ODE Ceiling Guard (Issue #136)
# ---------------------------------------------------------------------------
CEILING_PRECOOL_FALLBACK_MIN: int = 120  # fallback lead time when k_active_cool not learned
CEILING_BRIDGE_TOLERANCE_F: float = 1.0  # bridge homes: require breach > comfort_cool + this
# Issue #247: in aggressive_savings mode, tolerate this much overshoot above comfort_cool before
# the ceiling guard escalates nat-vent -> AC (savings homes accept a small overshoot before paying
# for cooling; normal mode escalates at comfort_cool).
CEILING_ESCALATION_SAVINGS_MARGIN_F: float = 2.0

# ---------------------------------------------------------------------------
# Grace-period adopt-on-match (Issue #483)
# ---------------------------------------------------------------------------
# How close the thermostat's live setpoint must be to the setpoint select_comfort_band()
# would arm right now for a manual mode-override to be considered "matching" automation's
# current decision and adopted early (see _override_matches_current_decision() in
# automation.py). Deliberately tight -- this only exists to catch minor floating-point/
# rounding noise, not to treat a genuinely different user-chosen temperature as a match.
OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F: float = 1.0

# Issue #664: the ONLY two `_start_grace_period(trigger=...)` values that mean "this grace
# exists to protect an active manual/fan override" — read by `_start_grace_period()` to set
# `_grace_protects_override`, which `coordinator._check_orphaned_grace()` uses to scope its
# self-heal to grace types that can actually BE orphaned (an override was cleared without its
# grace being cancelled alongside it). Every other grace trigger (fan-off cooldown, physical-
# drift correction, window-close resume, nat-vent-exit resume, dashboard resume) never sets
# `_manual_override_active`/`_fan_override_active` in the first place by design — treating
# their absence as "orphaned" was the root cause of #530's fan-off grace being killed within
# ~1ms of starting. A future grace-starting call site is automatically excluded here unless
# its trigger string is deliberately added to this set — if it's meant to protect a real
# override, add it; if not, leave it out. Single source of truth (Issue #664) — previously
# also hand-duplicated in override_grace_start.py's own module-level default, which risked
# silent drift since override_grace_fsm.py's call site never passed the real set explicitly.
GRACE_TRIGGERS_PROTECTING_OVERRIDE: frozenset[str] = frozenset({"fan_manual_override", "override_confirmed"})

# Issue #249 — thermostat capability detection. Home Assistant's
# ClimateEntityFeature.TARGET_TEMPERATURE_RANGE bit: when set in a climate entity's
# `supported_features`, the thermostat accepts target_temp_low/target_temp_high (dual-setpoint /
# heat_cool band). Defined locally as a stable HA flag value so automation.py need not import
# homeassistant.components.climate (which breaks the lightweight stub test environment).
CLIMATE_FEATURE_TARGET_TEMP_RANGE: int = 2

ATTR_THERMAL_HEATING_RATE = "thermal_heating_rate"
ATTR_THERMAL_COOLING_RATE = "thermal_cooling_rate"
ATTR_THERMAL_CONFIDENCE = "thermal_confidence"

# ---------------------------------------------------------------------------
# Weather Forecast Offset Learning (Issue #61)
# ---------------------------------------------------------------------------
MIN_WEATHER_BIAS_OBSERVATIONS = 7  # need a full week before applying bias
WEATHER_BIAS_MAX_OBS = 30  # use last 30 days of forecast comparisons
MIN_WEATHER_BIAS_APPLY_F = 0.5  # don't apply bias smaller than 0.5°F
MAX_WEATHER_BIAS_APPLY_F = 8.0  # cap correction at 8°F (sanity limit)
ATTR_FORECAST_HIGH_BIAS = "forecast_high_bias"
ATTR_FORECAST_LOW_BIAS = "forecast_low_bias"
ATTR_FORECAST_BIAS_CONFIDENCE = "forecast_bias_confidence"

# ---------------------------------------------------------------------------
# AI / Claude API Integration (Issue #68)
# ---------------------------------------------------------------------------

# Config keys
CONF_AI_ENABLED = "ai_enabled"
CONF_AI_API_KEY = "ai_api_key"
CONF_AI_MODEL = "ai_model"
CONF_AI_REASONING_EFFORT = "ai_reasoning_effort"
CONF_AI_MAX_TOKENS = "ai_max_tokens"
CONF_AI_TEMPERATURE = "ai_temperature"
CONF_AI_MONTHLY_BUDGET = "ai_monthly_budget"
CONF_AI_AUTO_REQUESTS_PER_DAY = "ai_auto_requests_per_day"
CONF_AI_MANUAL_REQUESTS_PER_DAY = "ai_manual_requests_per_day"
CONF_AI_INVESTIGATOR_ENABLED = "ai_investigator_enabled"
CONF_AI_INVESTIGATOR_MODEL = "ai_investigator_model"
CONF_AI_INVESTIGATOR_REASONING = "ai_investigator_reasoning_effort"
CONF_AI_INVESTIGATOR_MAX_TOKENS = "ai_investigator_max_tokens"
CONF_AI_INVESTIGATOR_RPD = "ai_investigator_requests_per_day"

# Defaults
DEFAULT_AI_ENABLED = False
DEFAULT_AI_MODEL = "claude-sonnet-5"
DEFAULT_AI_REASONING_EFFORT = "low"
DEFAULT_AI_MAX_TOKENS = 4096
DEFAULT_AI_TEMPERATURE = 0.3
DEFAULT_AI_MONTHLY_BUDGET = 0  # 0 = no cap
DEFAULT_AI_AUTO_REQUESTS_PER_DAY = 5
DEFAULT_AI_MANUAL_REQUESTS_PER_DAY = 20
DEFAULT_AI_INVESTIGATOR_ENABLED = False
DEFAULT_AI_INVESTIGATOR_MODEL = "claude-sonnet-4-6"
DEFAULT_AI_INVESTIGATOR_REASONING = "medium"
DEFAULT_AI_INVESTIGATOR_MAX_TOKENS = 8192  # must exceed MEDIUM reasoning budget (4096) + output buffer
DEFAULT_AI_INVESTIGATOR_RPD = 3

# Model options — Issue #563: these are the OFFLINE FALLBACK defaults, not "the" list.
# claude_api.py's fetch_available_models() fetches the live registry from Anthropic at
# runtime for both the config flow dropdown and capability-tier deprecation fallback;
# this static list is only used when that live fetch fails (no network, no API key yet,
# unsupported SDK version, etc.) — keep it reasonably current, but it is a safety net,
# not the source of truth for what models are actually available.
AI_MODEL_SONNET_5 = "claude-sonnet-5"
AI_MODEL_SONNET = "claude-sonnet-4-6"
AI_MODEL_OPUS = "claude-opus-4-6"
AI_MODEL_HAIKU = "claude-haiku-4-5-20251001"
AI_MODELS = [AI_MODEL_SONNET_5, AI_MODEL_SONNET, AI_MODEL_OPUS, AI_MODEL_HAIKU]

# Per-model request-shape capabilities (Issue #572) — replaces the reactive
# learn-from-a-live-failure approach (#563/#565/#568/#569), which guaranteed a silent,
# ~90s zero-output failure on a model's first-ever request before it could "learn" the
# correct shape, and whose learned state could be lost on a real HA restart (the
# #568/#569 persistence fix only covered the config-reload shutdown path, not
# EVENT_HOMEASSISTANT_STOP). This product supports a small, known set of Claude
# models, not an arbitrary universe of them, so the correct shape is hardcoded here
# instead of discovered live.
#
# Verified 2026-08-05 via direct calls to the Anthropic Messages API (not simulated):
#   - claude-sonnet-4-6 / claude-opus-4-6 / claude-haiku-4-5-20251001: accept
#     `temperature`; no thinking control needed at low/medium; legacy
#     `thinking:{type:enabled,budget_tokens:N}` shape used at "high" only — the
#     behavior this integration has always used for these models, unchanged.
#   - claude-sonnet-5 / claude-opus-5: reject `temperature` outright (400,
#     "`temperature` is deprecated for this model"); reject the legacy thinking shape
#     outright (400, "`thinking.type.enabled` is not supported for this model. Use
#     `thinking.type.adaptive`..."); confirmed producing real visible output with the
#     adaptive shape at both a small test prompt and full production scale
#     (max_tokens=8192) — claude-sonnet-5's silent zero-output failure without any
#     thinking control was also independently confirmed at production scale by
#     Issue #565's own live diagnostic and in live HA logs (2026-08-05).
#   - claude-fable-5: rejects `temperature` (same family pattern); adaptive shape
#     assumed by family consistency with sonnet-5/opus-5, not independently
#     re-confirmed this session.
#   - claude-haiku-5: does not exist (404 not_found_error as of 2026-08-05) — not a
#     real model ID, intentionally omitted.
#
# A model not in this table (e.g. a brand-new Anthropic release picked from the live
# model list before this table is updated) falls back to the "legacy" shape — the
# behavior proven safe for years prior to claude-sonnet-5 — and logs a WARNING naming
# the model, rather than silently guessing or reactively probing it live.
AI_THINKING_SHAPE_LEGACY = "legacy"
AI_THINKING_SHAPE_ADAPTIVE = "adaptive"

AI_MODEL_CAPABILITIES: dict[str, dict] = {
    AI_MODEL_SONNET: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_OPUS: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_HAIKU: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_SONNET_5: {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
    "claude-opus-5": {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
    "claude-fable-5": {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
}

# Default capabilities for a model not present in AI_MODEL_CAPABILITIES.
AI_MODEL_CAPABILITIES_DEFAULT: dict = {
    "thinking_shape": AI_THINKING_SHAPE_LEGACY,
    "supports_temperature": True,
}

# Reasoning effort options and budget_tokens mapping
AI_REASONING_LOW = "low"
AI_REASONING_MEDIUM = "medium"
AI_REASONING_HIGH = "high"
AI_REASONING_OPTIONS = [AI_REASONING_LOW, AI_REASONING_MEDIUM, AI_REASONING_HIGH]
AI_REASONING_BUDGET_TOKENS = {
    AI_REASONING_LOW: 1024,
    AI_REASONING_MEDIUM: 4096,
    AI_REASONING_HIGH: 16384,
}

# Circuit breaker
AI_CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures before tripping
AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300  # 5 min cooldown

# Retry
AI_MAX_RETRIES = 3
AI_RETRY_BASE_DELAY_SECONDS = 1.0  # exponential backoff: 1s, 2s, 4s

# Request history cap (metadata-only deque)
AI_REQUEST_HISTORY_CAP = 50

# Investigation report history (Issue #82)
INVESTIGATION_REPORT_HISTORY_CAP = 60
INVESTIGATION_REPORTS_FILE = "climate_advisor_investigation_reports.json"

# Sensor attributes for AI status
ATTR_AI_STATUS = "ai_status"

# API paths for AI endpoints
API_AI_STATUS = f"{API_BASE}/ai_status"
API_AI_INVESTIGATE = f"{API_BASE}/ai_investigate"
API_INVESTIGATION_REPORTS = f"{API_BASE}/investigation_reports"
API_DELETE_REPORT = f"{API_BASE}/delete_report"
