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
