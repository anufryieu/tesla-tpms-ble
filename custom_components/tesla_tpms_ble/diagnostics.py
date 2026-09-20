"""Diagnostics for the Tesla / Autel BLE TPMS integration.

The interesting part is the payload history. Home Assistant's own Bluetooth
diagnostics keep only the latest advertisement per device, so a download made
on the drive always shows a sleep frame no matter what the sensor did on the
road. This one keeps every distinct payload the sensor has sent since Home
Assistant started, with counts and timestamps, which is what you need to answer
"does this sensor ever broadcast a reading?".

Download it from the device page: **⋮ -> Download diagnostics**.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PRESSURE_TRIM,
    CONF_PROFILE,
    CONF_TEMPERATURE_TRIM,
    DEFAULT_PRESSURE_TRIM,
    DEFAULT_PROFILE,
    DEFAULT_TEMPERATURE_TRIM,
    DOMAIN,
)
from .parser import TeslaTPMSBluetoothDeviceData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one sensor."""
    data: TeslaTPMSBluetoothDeviceData | None = hass.data.get(DOMAIN, {}).get(
        f"{entry.entry_id}_data"
    )

    diagnostics: dict[str, Any] = {
        "address": entry.unique_id,
        "title": entry.title,
        "options": {
            "profile": entry.options.get(CONF_PROFILE, DEFAULT_PROFILE),
            "pressure_trim": entry.options.get(
                CONF_PRESSURE_TRIM, DEFAULT_PRESSURE_TRIM
            ),
            "temperature_trim": entry.options.get(
                CONF_TEMPERATURE_TRIM, DEFAULT_TEMPERATURE_TRIM
            ),
        },
    }

    if data is None:
        diagnostics["error"] = (
            "The config entry is not loaded, so no payloads have been recorded."
        )
        return diagnostics

    last_update = data.last_update_time
    diagnostics["last_decoded_advertisement"] = (
        last_update.isoformat() if last_update else None
    )
    diagnostics["payload_history"] = data.history.as_dict()
    return diagnostics
