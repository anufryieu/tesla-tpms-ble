"""The active Bluetooth coordinator, and what one config entry carries.

Split out of :mod:`__init__` so the coordinator lives where Home Assistant
expects to find it. ``TeslaTpmsRuntimeData`` is what hangs off
:attr:`ConfigEntry.runtime_data`: the three objects an entry needs while it is
loaded, in one place instead of three string-suffixed ``hass.data`` keys.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from .connection import TeslaTpmsConnection
from .parser import TeslaTPMSBluetoothDeviceData

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


@dataclass(slots=True)
class TeslaTpmsRuntimeData:
    """Everything one sensor's config entry holds while it is loaded."""

    coordinator: TeslaTpmsCoordinator
    data: TeslaTPMSBluetoothDeviceData
    connection: TeslaTpmsConnection


#: A config entry for this integration, with its runtime data typed.
TeslaTpmsConfigEntry = ConfigEntry[TeslaTpmsRuntimeData]
