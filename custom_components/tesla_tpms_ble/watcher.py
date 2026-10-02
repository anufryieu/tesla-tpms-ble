"""Record Tesla-looking advertisements from addresses nobody has configured.

Each config entry only hears its own sensor's address. If a sensor advertises
from a different address while it is awake -- a private address, or a second
identity for the reading -- those frames never reach any entry, and the
per-sensor payload history shows nothing but sleep frames. This watcher listens
to everything, keeps what looks like it came from a TPMS sensor or a Tesla, and
shows it in every sensor's diagnostics.
"""
from __future__ import annotations

from typing import Any, Final

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .decoder import TESLA_COMPANY_ID, decode, looks_like_tesla_tpms
from .parser import SENSOR_ADDRESS_PREFIX, PayloadHistory, describe_advertisement

# More than a car's worth of sensors plus a few passing Teslas; past this, a
# car park full of them is not going to tell us anything new.
MAX_WATCHED_ADDRESSES: Final[int] = 16


class UnknownAddressWatcher:
    """Keep a payload history for every unconfigured Tesla-looking address."""

    def __init__(self) -> None:
        """Start with nothing seen and no addresses configured."""
        self.configured: set[str] = set()
        self.histories: dict[str, PayloadHistory] = {}
        self.names: dict[str, str] = {}
        self.addresses_dropped = 0
        self._unsub: CALLBACK_TYPE | None = None

    def start(self, hass: HomeAssistant) -> None:
        """Listen to every advertisement, connectable or not."""
        if self._unsub is None:
            self._unsub = async_register_callback(
                hass,
                self._async_advertisement,
                BluetoothCallbackMatcher(connectable=False),
                BluetoothScanningMode.PASSIVE,
            )

    def stop(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _async_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Record one advertisement if it could be a sensor we are missing."""
        address = service_info.address
        if address in self.configured:
            return
        payload = service_info.manufacturer_data.get(TESLA_COMPANY_ID)
        if payload is None and not address.upper().startswith(SENSOR_ADDRESS_PREFIX):
            return
        self.record(address, service_info, payload)

    def record(
        self,
        address: str,
        service_info: BluetoothServiceInfoBleak,
        payload: bytes | None,
    ) -> None:
        """Add one advertisement to the history for ``address``."""
        history = self.histories.get(address)
        if history is None:
            if len(self.histories) >= MAX_WATCHED_ADDRESSES:
                self.addresses_dropped += 1
                return
            history = self.histories[address] = PayloadHistory()
        if service_info.name and service_info.name != address:
            self.names[address] = service_info.name

        if payload is None:
            key: bytes | str = describe_advertisement(service_info)
            accepted, note = False, "no Tesla manufacturer data"
        elif not looks_like_tesla_tpms(payload):
            key, accepted, note = payload, False, "not TPMS-shaped"
        elif (reading := decode(payload)) is None:
            key, accepted, note = payload, False, "undecodable"
        else:
            key, accepted = payload, True
            note = "awake" if reading.awake else "asleep"

        history.record(
            key,
            service_info.rssi,
            accepted=accepted,
            note=note,
            source=service_info.source,
        )

    def as_dict(self) -> dict[str, Any]:
        """Render for a diagnostics download, leaving out configured sensors."""
        return {
            "note": (
                "Advertisements with the Tesla company ID, or from the "
                f"{SENSOR_ADDRESS_PREFIX} address block, from addresses that "
                "are not set up as a sensor. An awake frame here means a "
                "sensor sends its reading from another address."
            ),
            "addresses_dropped": self.addresses_dropped,
            "addresses": {
                address: {"name": self.names.get(address), **history.as_dict()}
                for address, history in self.histories.items()
                if address not in self.configured
            },
        }
