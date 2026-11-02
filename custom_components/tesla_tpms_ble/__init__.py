"""The Tesla / Autel BLE TPMS integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_address_present,
)
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .connection import TeslaTpmsConnection
from .const import (
    AWAKE_TIMEOUT,
    CONF_CONNECT,
    CONF_PRESSURE_TRIM,
    CONF_PROFILE,
    CONF_TEMPERATURE_TRIM,
    CONNECT_ATTEMPTS,
    CONNECT_POLL_INTERVAL,
    CONNECT_QUIET_ATTEMPTS,
    DEFAULT_CONNECT,
    DEFAULT_PRESSURE_TRIM,
    DEFAULT_PROFILE,
    DEFAULT_TEMPERATURE_TRIM,
    DOMAIN,
    WATCHER,
)
from .coordinator import (
    TeslaTpmsConfigEntry,
    TeslaTpmsCoordinator,
    TeslaTpmsRuntimeData,
    poll_decision,
)
from .decoder import PROFILE_DEFAULT, PROFILES
from .parser import TeslaTPMSBluetoothDeviceData
from .watcher import UnknownAddressWatcher

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: TeslaTpmsConfigEntry
) -> bool:
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

    # One watcher serves every entry, so it outlives any of them and stays in
    # hass.data rather than on an entry's runtime data.
    watcher: UnknownAddressWatcher = hass.data.setdefault(DOMAIN, {}).setdefault(
        WATCHER, UnknownAddressWatcher()
    )
    watcher.configured.add(address)
    watcher.start(hass)

    connection = TeslaTpmsConnection()

    # The timer below ticks at CONNECT_POLL_INTERVAL, but only polls that often
    # while the sensor is still talking; see _async_timed_poll. Setup time is
    # the starting point for "how long has it been quiet", so a sensor that has
    # never answered is treated as quiet rather than polled flat out forever.
    setup_time = dt_util.utcnow()
    last_attempt: datetime | None = None
    quiet = False

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
        # Judged here rather than taken from the timer: a sensor that has just
        # started turning is heard first by the advertisement path, which polls
        # straight away, and that poll deserves the full set of attempts.
        silent_for = (
            dt_util.utcnow() - (data.last_update_time or setup_time)
        ).total_seconds()
        attempts = (
            CONNECT_QUIET_ATTEMPTS if silent_for >= AWAKE_TIMEOUT else CONNECT_ATTEMPTS
        )
        reading = await connection.async_poll(
            hass, service_info, max_attempts=attempts
        )
        return data.update_from_connection(reading, service_info)

    coordinator = TeslaTpmsCoordinator(
        hass,
        _LOGGER,
        address=address,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=data.update,
        needs_poll_method=_needs_poll,
        poll_method=_poll,
        connectable=False,
    )
    entry.runtime_data = TeslaTpmsRuntimeData(
        coordinator=coordinator, data=data, connection=connection
    )

    @callback
    def _async_timed_poll(now: datetime) -> None:
        """Ask for a reading on our own clock. See :class:`TeslaTpmsCoordinator`."""
        nonlocal last_attempt, quiet

        if not entry.options.get(CONF_CONNECT, DEFAULT_CONNECT):
            return
        if hass.state is not CoreState.running:
            return
        if not coordinator.has_service_info:
            # Never heard from this sensor, so there is no address to connect to.
            return

        # Waking is never missed by backing off: a sensor that starts turning
        # advertises again, and the first advertisement after an absence is one
        # Home Assistant does forward, which polls through the usual path.
        silent_for = (now - (data.last_update_time or setup_time)).total_seconds()
        now_quiet, due = poll_decision(
            silent_for=silent_for,
            connected=connection.connected,
            seconds_since_attempt=(
                None if last_attempt is None else (now - last_attempt).total_seconds()
            ),
        )
        if now_quiet != quiet:
            quiet = now_quiet
            if now_quiet:
                _LOGGER.debug(
                    "%s: silent for %.0f s, reporting it asleep", address, silent_for
                )
                coordinator.async_push_update(data.update_quiet())
        if not due:
            return

        if connection.connected:
            # A connected sensor stops advertising, so it will look absent to
            # the check below. Having the connection is the better evidence.
            last_attempt = now
            coordinator.async_request_poll()
            return
        if not async_address_present(hass, address, connectable=True):
            # Nothing that can open a connection is in range of it. Skip rather
            # than spend an attempt that can only fail -- but say so, because a
            # passive-only proxy looks exactly like this and never recovers.
            _LOGGER.debug("%s: no connectable path in range, skipping poll", address)
            return
        last_attempt = now
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


async def _async_options_updated(
    hass: HomeAssistant, entry: TeslaTpmsConfigEntry
) -> None:
    """Re-read the decoding options without tearing down the entry."""
    entry.runtime_data.data.set_profile(
        entry.options.get(CONF_PROFILE, DEFAULT_PROFILE),
        entry.options.get(CONF_PRESSURE_TRIM, DEFAULT_PRESSURE_TRIM),
        entry.options.get(CONF_TEMPERATURE_TRIM, DEFAULT_TEMPERATURE_TRIM),
    )


async def async_unload_entry(
    hass: HomeAssistant, entry: TeslaTpmsConfigEntry
) -> bool:
    """Unload a config entry."""
    # The connection outlives any one poll now, so it has to be let go here.
    await entry.runtime_data.connection.async_close()
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        watcher: UnknownAddressWatcher | None = hass.data[DOMAIN].get(WATCHER)
        if watcher is not None:
            watcher.configured.discard(entry.unique_id)
            if not watcher.configured:
                watcher.stop()
                hass.data[DOMAIN].pop(WATCHER)
    return unload_ok
