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

# Don't poll more often than this many seconds, even though an advertisement
# (the poll trigger) may arrive more frequently.
CONNECT_POLL_INTERVAL = 300.0
