"""The Tesla / Autel BLE TPMS integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_address_present,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

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


class TeslaTpmsCoordinator(ActiveBluetoothProcessorCoordinator):
    """An active coordinator that can also poll on a clock of its own.

    Home Assistant hangs the active poll off the Bluetooth event, and
    habluetooth returns before calling any listener when an advertisement
    matches the previous one. These sensors repeat one constant three-byte
    sleep frame about once a second, so that event fires only when the sensor
    drops out of range and comes back: measured on one drive, twice in three
    hours, with the pressure frozen in between. Hence a timer, in
    ``async_setup_entry``, and these two hooks for it.
    """

    @property
    def has_service_info(self) -> bool:
        """Return True once an advertisement has been seen.

        The poll needs one: it is what carries the address to connect to.
        """
        return self._last_service_info is not None

    @property
    def last_service_info(self) -> BluetoothServiceInfoBleak | None:
        """The most recent advertisement, or None if none has been seen."""
        return self._last_service_info

    @callback
    def async_push_update(self, update) -> None:
        """Send an update straight to the entities, outside the poll cycle.

        A held connection can produce a reading at any moment; this is how one
        reaches the same processors a poll result would.
        """
        for processor in self._processors:
            processor.async_handle_update(update)

    @callback
    def async_request_poll(self) -> None:
        """Queue a poll now, independently of advertisement traffic.

        Goes through the same debouncer as the advertisement-driven path, so
        the two cannot stack up on one sensor.
        """
        self._debounced_poll.async_schedule_call()


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

    coordinator = hass.data[DOMAIN][entry.entry_id] = TeslaTpmsCoordinator(
        hass,
        _LOGGER,
        address=address,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=data.update,
        needs_poll_method=_needs_poll,
        poll_method=_poll,
        connectable=False,
    )

    @callback
    def _async_timed_poll(_now: datetime) -> None:
        """Ask for a reading on our own clock. See :class:`TeslaTpmsCoordinator`."""
        if not entry.options.get(CONF_CONNECT, DEFAULT_CONNECT):
            return
        if hass.state is not CoreState.running:
            return
        if not coordinator.has_service_info:
            # Never heard from this sensor, so there is no address to connect to.
            return
        if connection.connected:
            # A connected sensor stops advertising, so it will look absent to
            # the check below. Having the connection is the better evidence.
            coordinator.async_request_poll()
            return
        if not async_address_present(hass, address, connectable=True):
            # Nothing that can open a connection is in range of it. Skip rather
            # than spend an attempt that can only fail -- but say so, because a
            # passive-only proxy looks exactly like this and never recovers.
            _LOGGER.debug("%s: no connectable path in range, skipping poll", address)
            return
        coordinator.async_request_poll()

    @callback
    def _async_pushed_reading(reading) -> None:
        """Feed a reading the sensor sent unasked to the entities."""
        service_info = coordinator.last_service_info
        if service_info is None:
            return
        coordinator.async_push_update(
            data.update_from_connection(reading, service_info)
        )

    connection.set_reading_callback(_async_pushed_reading)

    entry.async_on_unload(
        async_track_time_interval(
            hass, _async_timed_poll, timedelta(seconds=CONNECT_POLL_INTERVAL)
        )
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
    connection: TeslaTpmsConnection | None = hass.data.get(DOMAIN, {}).get(
        f"{entry.entry_id}_connection"
    )
    if connection is not None:
        # The connection outlives any one poll now, so it has to be let go here.
        await connection.async_close()
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
