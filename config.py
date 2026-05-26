# ═══════════════════════════════════════════════════════════════════════════════════
# DISCORD CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

DISCORD_USER_TOKEN = "YOUR_DISCORD_USER_TOKEN"  # Replace with actual token

# Target channel ID for monitoring
TARGET_CHANNEL_ID = 1315754807595761695

# Complaint/violation channel IDs for nickname cross-checking
COMPLAINT_CHANNEL_IDS = [
    1253763748603367464,  # LUST STATION
    920845668153700393,  # SS14/Corvax (complaints)
    921508655856234537,  # SS14/Corvax (appeals)
    1173186338753884220,  # SS14/Corvax (responses)
    1226163026210717696,  # Corvax Forge
    1241728803949252618,  # SS220
    1234367190040318053,  # Imperial
    1306191493530128454,  # Corvax 18+
    1175578453567864863,  # SUNRISE
    1157956566213992468,  # Fish Station
    1112658022859284500,  # Space Stories
    1291023511607054387,  # Adventure Time
    1241692667214168166,  # Space Stories - Marines
    1264636346610221068,  # FIRE STATION 2.0
    1105247551881498764,  # SS220 (SS13?)
    1132930484847005726,  # Backman
    921498847862214666,  # SS14/Corvax (bans)
]

MESSAGE_HISTORY_LIMIT = 70000

# ═══════════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════════

ADMIN_USERNAME = "USERNAME_HERE"  # Replace with actual username
ADMIN_PASSWORD = "PASSWORD_HERE"  # Replace with actual password

# ═══════════════════════════════════════════════════════════════════════════════════
# API CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

BASE_ADMIN_URL = "https://admin.deadspace14.net"
ACCOUNT_URL = "https://account.spacestation14.com"

OPERATION_TIMEOUT = 180
REQUEST_TIMEOUT = 90
SEARCH_TIMEOUT = 240
BATCH_TIMEOUT = 480
TERM_TIMEOUT = 240

MAX_CONCURRENT_REQUESTS = 5
LOGIN_RETRY_LIMIT = 3
COOLDOWN_DURATION = 25

# ═══════════════════════════════════════════════════════════════════════════════════
# LOAD OPTIMIZER CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

HIGH_LATENCY_THRESHOLD = 14.0
VERY_HIGH_LATENCY_THRESHOLD = 25.0
LOW_LATENCY_THRESHOLD = 4.0
TARGET_LATENCY = 10.0

# Adjustment settings
MIN_ADJUSTMENT_INTERVAL = 8
MAX_CONSECUTIVE_ADJUSTMENTS = 8

# ═══════════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

FAILURE_THRESHOLD = 30
RECOVERY_TIMEOUT = 60
HALF_OPEN_MAX_CALLS = 6

# ═══════════════════════════════════════════════════════════════════════════════════
# BACKOFF CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

INITIAL_DELAY = 1.0
MAX_DELAY = 30.0
MULTIPLIER = 1.4
JITTER = True

# ═══════════════════════════════════════════════════════════════════════════════════
# BATCH PROCESSING CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

CONSERVATIVE_BATCH_SIZE = 5
AGGRESSIVE_BATCH_SIZE = 8
BATCH_DELAY_BASE = 1.0
MAX_BATCH_RETRIES = 3
BATCH_RETRY_DELAY_MULTIPLIER = 2

# ═══════════════════════════════════════════════════════════════════════════════════
# SCAN CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

MESSAGE_LIMIT = 10
USERNAME = None
CHECK_BAN_BYPASS = False
BAN_BYPASS_PAGES = 3

MAX_TERMS_PER_SCAN = 5000

BYPASS_SEARCH_MAX_DEPTH = 2
SEARCH_MAX_DEPTH = 3
SEARCH_LIMIT_ROOT = 10
SEARCH_LIMIT_LEVEL1 = 5
SEARCH_LIMIT_LEVEL2 = 3
SEARCH_LIMIT_DEFAULT = 2

SEARCH_BATCH_SIZE = 3

# Cache settings
SEARCH_CACHE_MAX_SIZE = 12000
SEARCH_CACHE_TTL = 9000

# ═══════════════════════════════════════════════════════════════════════════════════
# TIMING THRESHOLDS
# ═══════════════════════════════════════════════════════════════════════════════════

CLOSE_TIME_THRESHOLD_MINUTES = 10
TIME_THRESHOLD_MINUTES = 30
SUSPICIOUS_TIME_THRESHOLD_MINUTES = 60
IP_MATCH_TIMEDELTA_MINUTES = 30

# ═══════════════════════════════════════════════════════════════════════════════════
# RETRY CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

MAX_RETRIES_PER_BATCH = 2
MAX_RETRIES_PER_TERM = 1
RETRY_DELAY_MULTIPLIER = 1.8
TIMEOUT_RECOVERY_DELAY = 2
CONSECUTIVE_TIMEOUT_LIMIT = 6

# ═══════════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

CHECK_INTERVAL = 300
MAX_ERROR_RATE = 0.3
MIN_SUCCESS_RATE = 0.7
MAX_RESPONSE_TIME = 30.0
MIN_THROUGHPUT = 10

# ═══════════════════════════════════════════════════════════════════════════════════
# EMERGENCY MODE CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

ENABLE_EMERGENCY_MODE = True
EMERGENCY_BATCH_SIZE = 3
EMERGENCY_DELAY = 5.0
EMERGENCY_TIMEOUT = 180
CONSECUTIVE_FAILURES_TRIGGER = 20
HIGH_LATENCY_TRIGGER = 35.0
ERROR_RATE_TRIGGER = 0.4

# ═══════════════════════════════════════════════════════════════════════════════════
# RESOURCE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════════

MAX_CONCURRENT_SEARCHES = 6
MAX_QUEUE_SIZE = 100
MEMORY_WARNING_THRESHOLD = 768
MEMORY_CRITICAL_THRESHOLD = 1536
AUTO_CLEANUP_ENABLED = True
CLEANUP_INTERVAL = 2700
CACHE_SIZE_LIMIT = 2000

# ═══════════════════════════════════════════════════════════════════════════════════
# PERFORMANCE MONITORING
# ═══════════════════════════════════════════════════════════════════════════════════

LATENCY_HISTORY_SIZE = 60
RECENT_LATENCY_SIZE = 12
CACHE_CLEANUP_INTERVAL = 1500
MAX_MEMORY_CACHE_SIZE = 120

PERFORMANCE_LOG_INTERVAL = 150
LOG_SLOW_OPERATIONS = True
SLOW_OPERATION_THRESHOLD = 15.0
LOG_ERROR_STATISTICS = True
ERROR_STATS_INTERVAL = 300

# ═══════════════════════════════════════════════════════════════════════════════════
# LOGGING CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

LOG_FILE = None
LOG_LEVEL = "INFO"
LOG_DIR = None
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5
USE_COLORS = True

# ═══════════════════════════════════════════════════════════════════════════════════
# REPORT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════════

HTML_REPORT_FILENAME = "ban_bypass_report.html"
JSON_REPORT_FILENAME = "scan_report.json"
REPORT_DIR = None

# ═══════════════════════════════════════════════════════════════════════════════════
# CONFIDENCE LEVELS
# ═══════════════════════════════════════════════════════════════════════════════════

HWID_MATCH = "HWID_MATCH"
IP_VERY_CLOSE_TIME = "IP_VERY_CLOSE_TIME"
IP_CLOSE_TIME = "IP_CLOSE_TIME"
IP_MODERATE_TIME = "IP_MODERATE_TIME"
IP_DISTANT_TIME = "IP_DISTANT_TIME"
IP_TIME_CLOSE_MATCH = "IP_TIME_CLOSE_MATCH"
IP_TIME_MATCH = "IP_TIME_MATCH"
IP_MATCH = "IP_MATCH"
NO_MATCH = "NO_MATCH"
