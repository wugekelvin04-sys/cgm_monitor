# Glucose thresholds (mg/dL)
VERY_LOW = 55
LOW = 70
HIGH = 180
VERY_HIGH = 250

# Glucose colors
COLOR_VERY_LOW = "#E74C3C"   # Critically low (red)
COLOR_LOW = "#F39C12"         # Low (orange)
COLOR_NORMAL = "#2ECC71"      # Normal (green)
COLOR_HIGH = "#F39C12"        # High (orange)
COLOR_VERY_HIGH = "#E74C3C"  # Critically high (red)

# Window configuration
MAIN_WINDOW_WIDTH = 300
MAIN_WINDOW_HEIGHT = 220
COMPACT_WINDOW_HEIGHT = 80
SETTINGS_WINDOW_WIDTH = 360
SETTINGS_WINDOW_HEIGHT = 380

# Initial position (top-right corner, below menu bar)
MAIN_WINDOW_X = 1200
MAIN_WINDOW_Y = 800

# Refresh interval (seconds)
REFRESH_INTERVAL_DEFAULT = 120  # 2 minutes
REFRESH_INTERVAL_OPTIONS = {
    "1 min": 60,
    "2 min": 120,
    "5 min": 300,
}

# AI analysis cache duration (seconds)
AI_CACHE_SECONDS = 300

# Keyring service name
KEYRING_SERVICE = "CGMMonitor"
KEYRING_USERNAME_KEY = "dexcom_username"
KEYRING_PASSWORD_KEY = "dexcom_password"
KEYRING_GEMINI_KEY = "gemini_api_key"
KEYRING_REFRESH_KEY = "refresh_interval"
KEYRING_REGION_KEY = "dexcom_region"
KEYRING_DISPLAY_MODE_KEY = "display_mode"
KEYRING_UNIT_KEY = "glucose_unit"

# Glucose unit constants
GLUCOSE_UNIT_MGDL = "mgdl"
GLUCOSE_UNIT_MMOL = "mmol"

# Display modes
DISPLAY_MODE_WINDOW = "window"  # Standard window mode (default)
DISPLAY_MODE_HOVER  = "hover"   # Floating ball mode: hover to expand, leave to collapse

# Glucose history query range (minutes, pydexcom maximum is 1440)
HISTORY_MINUTES = 1440  # 24h

# Maximum history data points (24h x 12 points/h = 288)
HISTORY_MAX_POINTS = 288
