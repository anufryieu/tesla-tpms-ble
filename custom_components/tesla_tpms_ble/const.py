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
