"""The Tesla / Autel BLE TPMS integration."""
from __future__ import annotations

import logging

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant

from .connection import TeslaTpmsConnection
from .const import (
    CONF_CONNECT,
    CONF_PRESSURE_TRIM,
    CONF_PROFILE,
    CONF_TEMPERATURE_TRIM,
    CONNECT_POLL_INTERVAL,
    DEFAULT_CONNECT,
    DEFAULT_PRESSURE_TRIM,
    DEFAULT_PROFILE,
    DEFAULT_TEMPERATURE_TRIM,
    DOMAIN,
    WATCHER,
)
from .decoder import PROFILE_DEFAULT, PROFILES
from .parser import TeslaTPMSBluetoothDeviceData
from .watcher import UnknownAddressWatcher

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Tesla / Autel BLE TPMS sensor from a config entry."""
    address = entry.unique_id
    assert address is not None

    profile = PROFILES.get(
        entry.options.get(CONF_PROFILE, DEFAULT_PROFILE), PROFILE_DEFAULT
    )
    data = TeslaTPMSBluetoothDeviceData(
        profile=profile,
        pressure_trim=entry.options.get(CONF_PRESSURE_TRIM, DEFAULT_PRESSURE_TRIM),
        temperature_trim=entry.options.get(
            CONF_TEMPERATURE_TRIM, DEFAULT_TEMPERATURE_TRIM
        ),
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][f"{entry.entry_id}_data"] = data

    watcher: UnknownAddressWatcher = hass.data[DOMAIN].setdefault(
        WATCHER, UnknownAddressWatcher()
    )
    watcher.configured.add(address)
    watcher.start(hass)

    connection = TeslaTpmsConnection()
    hass.data[DOMAIN][f"{entry.entry_id}_connection"] = connection

    def _needs_poll(
        service_info: BluetoothServiceInfoBleak, last_poll: float | None
    ) -> bool:
        # Read the option live so the toggle takes effect without a reload.
        if not entry.options.get(CONF_CONNECT, DEFAULT_CONNECT):
            return False
        # Don't open connections while Home Assistant is still starting up.
        if hass.state is not CoreState.running:
            return False
        return last_poll is None or last_poll >= CONNECT_POLL_INTERVAL

    async def _poll(service_info: BluetoothServiceInfoBleak):
        reading = await connection.async_poll(hass, service_info)
        return data.update_from_connection(reading, service_info)

    coordinator = hass.data[DOMAIN][
        entry.entry_id
    ] = ActiveBluetoothProcessorCoordinator(
        hass,
        _LOGGER,
        address=address,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=data.update,
        needs_poll_method=_needs_poll,
        poll_method=_poll,
        connectable=False,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # Only start once every platform has had a chance to subscribe.
    entry.async_on_unload(coordinator.async_start())
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-read the decoding options without tearing down the entry."""
    data: TeslaTPMSBluetoothDeviceData | None = hass.data.get(DOMAIN, {}).get(
        f"{entry.entry_id}_data"
    )
    if data is None:
        return
    data.set_profile(
        entry.options.get(CONF_PROFILE, DEFAULT_PROFILE),
        entry.options.get(CONF_PRESSURE_TRIM, DEFAULT_PRESSURE_TRIM),
        entry.options.get(CONF_TEMPERATURE_TRIM, DEFAULT_TEMPERATURE_TRIM),
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_data", None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_connection", None)
        watcher: UnknownAddressWatcher | None = hass.data[DOMAIN].get(WATCHER)
        if watcher is not None:
            watcher.configured.discard(entry.unique_id)
            if not watcher.configured:
                watcher.stop()
                hass.data[DOMAIN].pop(WATCHER)
    return unload_ok
