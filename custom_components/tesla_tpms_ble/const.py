"""Constants for the Tesla / Autel BLE TPMS integration."""

DOMAIN = "tesla_tpms_ble"

# hass.data[DOMAIN] key for the one watcher shared by every config entry.
WATCHER = "unknown_address_watcher"

CONF_PROFILE = "profile"
DEFAULT_PROFILE = "default"

CONF_PRESSURE_TRIM = "pressure_trim"
DEFAULT_PRESSURE_TRIM = 0.0

CONF_TEMPERATURE_TRIM = "temperature_trim"
DEFAULT_TEMPERATURE_TRIM = 0.0

# Opt-in: actively connect to the sensor and ask for a reading over GATT, for
# sensors that never put one in their advertisement. Off by default -- it opens
# a connection (which costs the sensor a little battery) and needs a connectable
# adapter or proxy, and the request framing is still experimental.
CONF_CONNECT = "connect"
DEFAULT_CONNECT = False

# How often to ask a sensor for a reading, in seconds. This is the integration's
# own clock: the advertisement cannot drive the poll, because Home Assistant
# drops an advertisement identical to the previous one and these sensors repeat
# one constant sleep frame forever. See TeslaTpmsCoordinator in __init__.py.
#
# It is also the floor for the advertisement-driven poll, for the rare occasions
# when an advertisement does get through.
#
# Every poll is a GATT connection, so this trades sensor battery for resolution.
CONNECT_POLL_INTERVAL = 30.0

# How long a reading keeps the sensor counted as awake. An awake sensor answers
# or volunteers something every poll, so three missed intervals means it has
# stopped talking; the slack is there so one dropped connection does not flap
# the Awake state. Nothing else can clear it -- a reading fetched over GATT is
# awake by definition, and a sleeping sensor sends nothing to say otherwise.
AWAKE_TIMEOUT = 90.0

# How often to try once the sensor has gone quiet. It sleeps when the wheel
# stops and answers nothing until it moves again, so the 30-second clock above
# buys nothing and costs a failed connection attempt every half minute, around
# the clock. Waking is not missed: a sensor that starts advertising again is an
# advertisement Home Assistant forwards, which polls through the usual path.
CONNECT_IDLE_POLL_INTERVAL = 300.0

# Connection attempts per poll. bleak-retry-connector's own default is right for
# a device that is there and busy, and wrong for one that is asleep and will not
# answer any of them -- so a quiet sensor gets fewer.
CONNECT_ATTEMPTS = 4
CONNECT_QUIET_ATTEMPTS = 2
