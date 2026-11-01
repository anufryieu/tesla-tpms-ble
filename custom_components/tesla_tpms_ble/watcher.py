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
        """Render for a diagnostics download, leaving out configured sensors.

        These addresses belong to vehicles and sensors that are not the user's,
        and a diagnostics download is written to be pasted into a bug report,
        so each one is cut back to its manufacturer prefix and its advertised
        name is left out. Both identify a specific vehicle; neither is needed
        to answer what this history is for, which is whether a reading ever
        arrives from an address no sensor is set up for.
        """
        return {
            "note": (
                "Advertisements with the Tesla company ID, or from the "
                f"{SENSOR_ADDRESS_PREFIX} address block, from addresses that "
                "are not set up as a sensor. An awake frame here means a "
                "sensor sends its reading from another address. Addresses are "
                "masked past the manufacturer prefix, and advertised names "
                "left out, because they are not this user's devices."
            ),
            "addresses_dropped": self.addresses_dropped,
            "addresses": {
                _mask_address(address, index): history.as_dict()
                for index, (address, history) in enumerate(
                    (
                        (address, history)
                        for address, history in self.histories.items()
                        if address not in self.configured
                    ),
                    start=1,
                )
            },
        }


def _mask_address(address: str, index: int) -> str:
    """Keep the manufacturer prefix of an address, drop the rest.

    The prefix is the part that carries diagnostic weight -- it is how a
    sensor is told apart from a car -- and the index keeps two masked
    addresses from collapsing into one key.
    """
    octets = address.split(":")
    kept = ":".join(octets[:3]) if len(octets) >= 3 else address
    return f"{kept}:xx:xx:xx #{index}"
