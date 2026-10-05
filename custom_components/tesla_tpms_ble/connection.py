"""Read a Tesla-protocol TPMS sensor over a GATT connection.

The advertisement path only ever yields a sleep frame from the sensors tested
so far. A car instead opens a connection and asks for a reading: it writes a
``TPDataRequest`` to characteristic 0212 and the sensor answers with a
``TPData`` indication on 0213. This module does the same, read-only as far as
the sensor's state goes -- it requests a measurement, it never enrols, bonds,
or writes anything that changes the sensor.

It is used by the optional active poll in :mod:`__init__`; the pure protocol
lives in :mod:`decoder` so it stays testable without Bluetooth.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Final

from bleak import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.core import HomeAssistant

from .decoder import TeslaTpmsReading, build_tpdata_requests, decode_tpdata

_LOGGER = logging.getLogger(__name__)

# Tesla VCSEC communication service characteristics.
CHAR_WRITE: Final[str] = "00000212-b2d1-43f0-9b88-960cebf8b91e"
CHAR_INDICATE: Final[str] = "00000213-b2d1-43f0-9b88-960cebf8b91e"

# How long to wait for a TPData indication after writing one request frame.
# The sensor drops an idle connection after ~15 s, so keep the whole exchange
# well under that even while trying every candidate frame.
_REPLY_TIMEOUT: Final[float] = 1.2
_TOTAL_BUDGET: Final[float] = 11.0


class NoTPDataReply(BleakError):
    """Connected, but the sensor sent no TPData -- treated as a poll failure.

    A subclass of :class:`BleakError` so the active coordinator logs it as a
    (recoverable) polling error rather than an unexpected crash.
    """


class TeslaTpmsConnection:
    """Connect to one sensor and ask it for a reading.

    Remembers which request framing the sensor answered, so after the first
    success every later poll is a single write.
    """

    def __init__(self) -> None:
        """Start with no known-good request frame and no history."""
        self._good_frame: bytes | None = None
        # Debugging surface for diagnostics: what the sensor last sent back and
        # why the last poll ended as it did. Populated on every poll attempt.
        self.last_replies: list[str] = []
        self.last_error: str | None = None

    @property
    def learned_request(self) -> str | None:
        """Hex of the request frame the sensor answered, once one has."""
        return self._good_frame.hex() if self._good_frame else None

    @property
    def last_reply(self) -> str | None:
        """Hex of the last raw indication the sensor sent, for diagnostics."""
        return self.last_replies[-1] if self.last_replies else None

    async def async_poll(
        self, hass: HomeAssistant, service_info: BluetoothServiceInfoBleak
    ) -> TeslaTpmsReading:
        """Connect, request a reading, and return it.

        Raises :class:`NoTPDataReply` if the sensor is reachable but says
        nothing decodable, or :class:`BleakError` if it cannot be connected --
        both of which the active coordinator treats as an ordinary failed poll.
        The failure reason is also kept in ``last_error`` for the diagnostics.
        """
        address = service_info.address
        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            # No connectable adapter or proxy can reach it; a passive-only proxy
            # cannot open a connection. Nothing to do until that changes.
            self.last_error = f"no connectable path to {address}"
            raise NoTPDataReply(self.last_error)

        try:
            client = await establish_connection(
                BleakClientWithServiceCache, ble_device, service_info.name or address
            )
        except BleakError as exc:
            self.last_error = f"connect failed: {exc}"
            raise
        try:
            reading = await self._async_request(client)
        except BleakError as exc:
            self.last_error = str(exc)
            raise
        finally:
            await client.disconnect()
        self.last_error = None
        return reading

    async def _async_request(self, client: BleakClientWithServiceCache) -> TeslaTpmsReading:
        """Subscribe to 0213, write request frame(s), parse the first reply."""
        write_char = client.services.get_characteristic(CHAR_WRITE)
        if write_char is None:
            raise NoTPDataReply("sensor has no 0212 write characteristic")
        with_response = "write" in write_char.properties
        if not with_response and "write-without-response" not in write_char.properties:
            raise NoTPDataReply("0212 is not writable")

        got: list[bytes] = []
        reply = asyncio.Event()

        def on_indicate(_handle: int, data: bytearray) -> None:
            got.append(bytes(data))
            reply.set()

        answering_frame: bytes | None = None
        await client.start_notify(CHAR_INDICATE, on_indicate)
        try:
            frames = [self._good_frame] if self._good_frame else build_tpdata_requests()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _TOTAL_BUDGET
            for frame in frames:
                if loop.time() >= deadline:
                    break
                before = len(got)
                reply.clear()
                await client.write_gatt_char(CHAR_WRITE, frame, response=with_response)
                try:
                    await asyncio.wait_for(reply.wait(), timeout=_REPLY_TIMEOUT)
                except TimeoutError:
                    continue
                # Only count an indication that arrived after this write, so an
                # unsolicited one on connect is not mis-credited to a frame.
                if len(got) > before:
                    answering_frame = frame
                    _LOGGER.debug(
                        "indication after request %s: %s", frame.hex(), got[-1].hex()
                    )
                    break
        finally:
            try:
                await client.stop_notify(CHAR_INDICATE)
            except BleakError:
                pass

        # Record what came back regardless of how it decodes, for diagnostics.
        self.last_replies = [b.hex() for b in got]

        if not got:
            raise NoTPDataReply("connected, but no indication on 0213")
        reading = decode_tpdata(got[-1])
        if reading is None:
            # The sensor talks over GATT -- we just cannot read this yet. Keep
            # the bytes (last_replies) so the format can be worked out.
            raise NoTPDataReply(f"indecipherable reply: {got[-1].hex()}")
        if answering_frame is not None:
            self._good_frame = answering_frame
        return reading
