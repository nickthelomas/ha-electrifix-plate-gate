"""Constants for ElectriFix Plate Gate."""

DOMAIN = "electrifix_plate_gate"
NAME = "ElectriFix Plate Gate"
HELP_URL = "https://fix.electrifixperth.com.au"

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CAMERA = "camera"
CONF_TOPIC_PREFIX = "topic_prefix"
CONF_PEOPLE_TEXT = "people_text"
CONF_NEAR_MISSES = "near_misses"
CONF_MATCH_DISTANCE = "match_distance"
CONF_CONFIRMED = "confirmed"
CONF_DEVICE_ENTITY = "device_entity"
CONF_OPEN_ON_ARRIVAL = "open_on_arrival"
CONF_CLOSE_ON_LEAVING = "close_on_leaving"
CONF_COOLDOWN = "cooldown_seconds"
CONF_REQUIRE_MOVING = "require_moving"
CONF_REPEAT_MODE = "repeat_mode"
CONF_LOCK_ACK = "lock_acknowledged"
CONF_AUTO_CLOSE = "auto_close_minutes"
CONF_ZONES = "zones"
CONF_ENABLED = "enabled"
CONF_DRY_RUN = "dry_run"
CONF_LAST_ACTION_AT = "last_action_at"  # written by the runtime, not the user
CONF_MODELS_BASE_URL = "models_base_url"  # advanced/testing: where model files are fetched from

DEFAULT_URL = "http://127.0.0.1:5000"
DEFAULT_TOPIC_PREFIX = "frigate"
DEFAULT_MATCH_DISTANCE = 0
DEFAULT_COOLDOWN = 180
DEFAULT_AUTO_CLOSE = 5
DEFAULT_OPEN_ON_ARRIVAL = True
DEFAULT_CLOSE_ON_LEAVING = True
DEFAULT_REQUIRE_MOVING = False
REPEAT_COOLDOWN = "cooldown"
REPEAT_COOLDOWN_AND_EVENT = "cooldown_and_event"
REPEAT_MODES = [REPEAT_COOLDOWN, REPEAT_COOLDOWN_AND_EVENT]
DEFAULT_REPEAT_MODE = REPEAT_COOLDOWN
DEFAULT_ENABLED = True
DEFAULT_DRY_RUN = True

DEVICE_DOMAINS = ["cover", "switch", "script", "lock"]
DEVICE_MOVE_WINDOW = 60  # seconds to wait for the device to change state after an action
EXAMPLE_NEAR_MISSES = "LO120, XO540, MO520"
