"""Read a Tesla-protocol TPMS sensor over a GATT connection.

The advertisement path only ever yields a sleep frame from the sensors tested
so far. A car instead opens a connection and asks for a reading: it writes a
``TPDataRequest`` to characteristic 0212 and the sensor answers with a
``TPData`` indication on 0213. This module does the same, read-only as far as
the sensor's state goes -- it requests a measurement, it never enrols, bonds,
or writes anything that changes the sensor.

The connection is kept open between polls. Synacktiv's teardown of the Tesla
side (*0-click RCE on Tesla Model 3 through TPMS Sensors*, Hexacon 2024) shows
why: a sensor stops advertising once a car connects to it and talks over the
connection instead. The connection, not the advertisement, is the data path --
so this holds one, listens on ``0213`` while idle, and only writes a request
when the sensor has pushed nothing since the last poll.

It is used by the optional active poll in :mod:`__init__`; the pure protocol
lives in :mod:`decoder` so it stays testable without Bluetooth.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
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

    def __init__(
        self, on_reading: Callable[[TeslaTpmsReading], None] | None = None
    ) -> None:
        """Start with no known-good request frame, no client and no history.

        ``on_reading`` is called with any reading the sensor sends unasked
        while the connection sits idle, so a push reaches the entities without
        waiting for the next poll.
        """
        self._good_frame: bytes | None = None
        self._on_reading = on_reading
        self._client: BleakClientWithServiceCache | None = None
        self._listening = False
        self._pushed: TeslaTpmsReading | None = None
        # Debugging surface for diagnostics: what the sensor last sent back and
        # why the last poll ended as it did. Populated on every poll attempt.
        self.last_replies: list[str] = []
        self.last_error: str | None = None
        #: Readings the sensor sent without being asked. Zero after a while
        #: means it only ever answers, and holding the connection buys nothing.
        self.pushed_count = 0

    def set_reading_callback(
        self, on_reading: Callable[[TeslaTpmsReading], None]
    ) -> None:
        """Say where pushed readings go, once there is something to feed."""
        self._on_reading = on_reading

    @property
    def connected(self) -> bool:
        """Return True while a connection to the sensor is being held open."""
        return self._client is not None and self._client.is_connected

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
        """Return a reading, over a connection that is held open afterwards.

        If the sensor pushed one since the last call, that is returned and
        nothing is written. Otherwise it is asked, over the held connection if
        there is one and a fresh one if not.

        Raises :class:`NoTPDataReply` if the sensor is reachable but says
        nothing decodable, or :class:`BleakError` if it cannot be connected --
        both of which the active coordinator treats as an ordinary failed poll.
        The failure reason is also kept in ``last_error`` for the diagnostics.
        """
        if (pushed := self._pushed) is not None:
            # The sensor spoke on its own since the last poll. Nothing to ask.
            self._pushed = None
            self.last_error = None
            return pushed

        client = await self._async_client(hass, service_info)
        # The request path installs its own handler on 0213, so stand down the
        # idle listener for the length of the exchange.
        await self._async_stop_listening()
        try:
            reading = await self._async_request(client)
        except BleakError as exc:
            self.last_error = str(exc)
            # A failed exchange says nothing good about the connection; drop it
            # so the next poll starts clean rather than inheriting the trouble.
            await self.async_close()
            raise
        await self._async_listen()
        self.last_error = None
        return reading

    async def _async_client(
        self, hass: HomeAssistant, service_info: BluetoothServiceInfoBleak
    ) -> BleakClientWithServiceCache:
        """Return the held connection, opening one if there is none."""
        if self.connected:
            assert self._client is not None
            return self._client

        address = service_info.address
        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            # No connectable adapter or proxy can reach it; a passive-only proxy
            # cannot open a connection. Nothing to do until that changes.
            self.last_error = f"no connectable path to {address}"
            raise NoTPDataReply(self.last_error)

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                service_info.name or address,
                disconnected_callback=self._on_disconnect,
            )
        except BleakError as exc:
            self.last_error = f"connect failed: {exc}"
            raise
        self._client = client
        self._listening = False
        return client

    async def _async_listen(self) -> None:
        """Subscribe to 0213 for whatever the sensor says while we sit idle."""
        if self._listening or not self.connected:
            return
        assert self._client is not None
        try:
            await self._client.start_notify(CHAR_INDICATE, self._on_push)
        except BleakError as exc:
            # Not fatal: the next poll still asks over the same connection.
            _LOGGER.debug("cannot listen on 0213: %s", exc)
            return
        self._listening = True

    async def _async_stop_listening(self) -> None:
        """Hand 0213 back, so a request can install its own handler."""
        if not self._listening:
            return
        self._listening = False
        if not self.connected:
            return
        assert self._client is not None
        try:
            await self._client.stop_notify(CHAR_INDICATE)
        except BleakError:
            pass

    def _on_push(self, _handle: int, data: bytearray) -> None:
        """Handle an indication the sensor sent without being asked."""
        payload = bytes(data)
        self.last_replies = [payload.hex()]
        reading = decode_tpdata(payload)
        if reading is None:
            _LOGGER.debug("unsolicited indication, undecodable: %s", payload.hex())
            return
        self.pushed_count += 1
        if self._on_reading is not None:
            self._on_reading(reading)
        else:
            # Nobody is listening yet; keep it for the next poll to return.
            self._pushed = reading

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Note that the sensor hung up, so the next poll reconnects."""
        self._listening = False
        self._client = None

    async def async_close(self) -> None:
        """Drop the held connection, on unload or after a failed exchange."""
        client, self._client = self._client, None
        self._listening = False
        if client is None:
            return
        try:
            await client.disconnect()
        except BleakError as exc:
            _LOGGER.debug("disconnect failed: %s", exc)

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
            # Already disconnecting, or never subscribed: nothing to salvage.
            with contextlib.suppress(BleakError):
                await client.stop_notify(CHAR_INDICATE)

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
