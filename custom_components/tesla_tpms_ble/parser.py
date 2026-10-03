"""Home Assistant binding for the Tesla-protocol TPMS decoder.

All the protocol knowledge lives in :mod:`decoder`, which is deliberately free
of Home Assistant imports. This module only adapts it to the
``bluetooth_sensor_state_data`` interface that the passive Bluetooth
coordinator expects.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Final

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data.enum import StrEnum

from sensor_state_data import SensorUpdate

from .decoder import (
    MIN_PAYLOAD_LEN,
    PROFILE_DEFAULT,
    PROFILES,
    TESLA_COMPANY_ID,
    DecodeProfile,
    TeslaTpmsReading,
    decode,
    looks_like_tesla_tpms,
)

_LOGGER = logging.getLogger(__name__)

# How many *distinct* payloads to remember per sensor. A well-behaved sensor
# only ever shows a handful; the cap stops a sensor that varies a counter byte
# on every frame from growing this without bound.
MAX_DISTINCT_PAYLOADS: Final[int] = 32

# How many forwarded advertisements to keep in time order. Home Assistant only
# forwards one when the content changes or the sensor comes back after being
# away, so this covers days of parking and driving, not seconds.
MAX_TIMELINE_EVENTS: Final[int] = 64

# Every sensor fitted so far has a Texas Instruments address in this block.
SENSOR_ADDRESS_PREFIX: Final[str] = "BC:6A:29"


def describe_advertisement(service_info: BluetoothServiceInfo) -> str:
    """Summarise an advertisement that carries no Tesla manufacturer data.

    An awake sensor that moved its reading somewhere else -- another company
    ID, service data -- would otherwise be dropped without a trace.
    """
    parts = [
        f"mfr {company:#06x}: {data.hex()}"
        for company, data in sorted(service_info.manufacturer_data.items())
    ]
    parts += [
        f"svc {uuid}: {data.hex()}"
        for uuid, data in sorted(service_info.service_data.items())
    ]
    parts += [f"uuid {uuid}" for uuid in sorted(service_info.service_uuids)]
    return "; ".join(parts) or "empty advertisement"


class PayloadHistory:
    """Remember every distinct advertisement payload a sensor has sent.

    Home Assistant's own Bluetooth diagnostics keep only the *latest*
    advertisement per device, which is close to useless for a TPMS sensor: it
    is awake for a few seconds while the wheel turns and asleep again long
    before anyone downloads a diagnostic. A snapshot taken on the drive will
    therefore always show a sleep frame, whatever happened on the road.

    Keeping one entry per distinct payload, with counts and timestamps,
    survives that transition -- a single awake frame during a drive is still
    listed hours later. ``max_payload_len`` is the field that answers the
    question this was written for: anything above
    :data:`~.decoder.MIN_PAYLOAD_LEN` means a real reading reached us.

    Home Assistant does not forward every radio frame. It drops an
    advertisement identical to the previous one from the same device, so a
    parked sensor repeating ``01 fe 03`` once a second is forwarded once, and
    again only after it has been out of range long enough to be forgotten.
    Counts are therefore *forwarded* advertisements -- roughly "times the
    sensor reappeared or changed" -- not frames on air. The ``timeline`` keeps
    them in order with the gap since the previous one, so a drive shows up as
    a long gap followed by whatever the sensor sent when it came back.

    The history lives in memory only, so it starts empty after a Home Assistant
    restart. ``started`` records when, so a diagnostic is never read as covering
    more time than it does.
    """

    def __init__(self) -> None:
        """Start an empty history."""
        self._entries: dict[bytes | str, dict[str, Any]] = {}
        self._timeline: deque[dict[str, Any]] = deque(maxlen=MAX_TIMELINE_EVENTS)
        self._last_seen: datetime | None = None
        self.started = datetime.now(timezone.utc)
        self.total_forwarded = 0
        self.distinct_dropped = 0

    def record(
        self,
        payload: bytes | str,
        rssi: int | None,
        accepted: bool,
        note: str,
        source: str | None = None,
    ) -> None:
        """Record one forwarded advertisement and how the decoder judged it.

        ``payload`` is the Tesla manufacturer data, or a text summary from
        :func:`describe_advertisement` when the advertisement had none.
        """
        self.total_forwarded += 1
        now = datetime.now(timezone.utc)
        shown = payload.hex() if isinstance(payload, bytes) else payload

        self._timeline.append(
            {
                "time": now.isoformat(),
                "gap_seconds": (
                    round((now - self._last_seen).total_seconds())
                    if self._last_seen
                    else None
                ),
                "payload": shown,
                "note": note,
                "rssi": rssi,
                "source": source,
            }
        )
        self._last_seen = now

        entry = self._entries.get(payload)
        if entry is None:
            if len(self._entries) >= MAX_DISTINCT_PAYLOADS:
                # Better to lose the newest oddity than to grow without bound;
                # the count still shows something was dropped.
                self.distinct_dropped += 1
                return
            entry = self._entries[payload] = {
                "hex": payload.hex() if isinstance(payload, bytes) else None,
                "length": len(payload) if isinstance(payload, bytes) else None,
                "first_seen": now.isoformat(),
                "count": 0,
                "rssi_min": rssi,
                "rssi_max": rssi,
                "sources": [],
            }
            if not isinstance(payload, bytes):
                entry["advertisement"] = payload

        entry["count"] += 1
        entry["last_seen"] = now.isoformat()
        entry["accepted"] = accepted
        entry["note"] = note
        if source is not None and source not in entry["sources"]:
            entry["sources"].append(source)
        if rssi is not None:
            for key, better in (("rssi_min", min), ("rssi_max", max)):
                current = entry[key]
                entry[key] = rssi if current is None else better(current, rssi)

    @property
    def max_payload_len(self) -> int:
        """Longest payload seen. Above ``MIN_PAYLOAD_LEN`` means a reading."""
        return max(
            (len(p) for p in self._entries if isinstance(p, bytes)), default=0
        )

    def as_dict(self) -> dict[str, Any]:
        """Render the history for a diagnostics download."""
        payloads = sorted(
            self._entries.values(), key=lambda e: e["count"], reverse=True
        )
        saw_reading = self.max_payload_len >= MIN_PAYLOAD_LEN
        return {
            "history_started": self.started.isoformat(),
            "advertisements_forwarded": self.total_forwarded,
            "counting_note": (
                "Home Assistant forwards an advertisement only when it differs "
                "from the previous one or the sensor reappears after being out "
                "of range, so counts are appearances, not frames on air."
            ),
            "distinct_payloads": len(self._entries),
            "distinct_dropped": self.distinct_dropped,
            "max_payload_len": self.max_payload_len,
            "bytes_needed_for_a_reading": MIN_PAYLOAD_LEN,
            "saw_a_full_length_frame": saw_reading,
            "verdict": (
                "A full-length frame arrived, so this sensor does broadcast "
                "readings -- check 'accepted' on it if no entities appeared."
                if saw_reading
                else "Only short frames so far. Either the sensor never woke "
                "while Home Assistant was listening, or it never puts a "
                "reading in its advertisement at all."
            ),
            "payloads": payloads,
            "timeline": list(self._timeline),
        }


class TPMSSensor(StrEnum):
    """Sensor keys exposed by this integration."""

    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    BATTERY = "battery"
    VOLTAGE = "voltage"
    SIGNAL_STRENGTH = "signal_strength"
    # Diagnostics -- the raw wire values, so the decode can be verified
    # against a reference gauge without a packet capture.
    RAW_PRESSURE = "raw_pressure"
    RAW_TEMPERATURE = "raw_temperature"
    STATUS = "status"


class TPMSBinarySensor(StrEnum):
    """Binary sensor keys exposed by this integration."""

    AWAKE = "awake"


class TeslaTPMSBluetoothDeviceData(BluetoothData):
    """Parse advertisements from Tesla-protocol BLE TPMS sensors.

    Covers the Tesla OEM sensor and every aftermarket sensor that emulates it,
    including the Autel MX-Sensor BLE, which must speak this protocol in order
    for a Tesla's TPMS ECU to accept it.
    """

    def __init__(
        self,
        profile: DecodeProfile = PROFILE_DEFAULT,
        pressure_trim: float = 0.0,
        temperature_trim: float = 0.0,
    ) -> None:
        """Initialise with the chosen decoding profile and trim offsets."""
        super().__init__()
        self._profile = profile
        self._pressure_trim = pressure_trim
        self._temperature_trim = temperature_trim
        self._last_update_time: datetime | None = None
        self.history = PayloadHistory()

    @property
    def last_update_time(self) -> datetime | None:
        """Timestamp of the most recent decoded advertisement."""
        return self._last_update_time

    def set_profile(
        self,
        profile_name: str,
        pressure_trim: float = 0.0,
        temperature_trim: float = 0.0,
    ) -> None:
        """Swap the decoding profile at runtime (from the options flow)."""
        self._profile = PROFILES.get(profile_name, PROFILE_DEFAULT)
        self._pressure_trim = pressure_trim
        self._temperature_trim = temperature_trim

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from a BLE advertisement."""
        # Record before judging: a frame rejected below is exactly the kind we
        # would otherwise never hear about.
        rssi = getattr(service_info, "rssi", None)
        source = getattr(service_info, "source", None)

        payload = service_info.manufacturer_data.get(TESLA_COMPANY_ID)
        if payload is None:
            # Only this sensor's address reaches us, so whatever it sent
            # instead is worth keeping -- it may be where an awake reading went.
            self.history.record(
                describe_advertisement(service_info),
                rssi,
                accepted=False,
                note="no Tesla manufacturer data",
                source=source,
            )
            return

        if not looks_like_tesla_tpms(payload):
            # A Tesla *vehicle* advertises under the same company ID. Ignore it
            # rather than inventing a tyre out of the VCSEC beacon.
            self.history.record(
                payload, rssi, accepted=False, note="not TPMS-shaped", source=source
            )
            _LOGGER.debug(
                "Tesla company ID from %s but payload is not TPMS-shaped: %s",
                service_info.address,
                payload.hex(),
            )
            return

        reading = decode(payload, self._profile)
        if reading is None:
            self.history.record(
                payload, rssi, accepted=False, note="undecodable", source=source
            )
            _LOGGER.debug(
                "Undecodable Tesla TPMS payload from %s: %s",
                service_info.address,
                payload.hex(),
            )
            return

        self.history.record(
            payload,
            rssi,
            accepted=True,
            note="awake" if reading.awake else "asleep",
            source=source,
        )
        _LOGGER.debug(
            "%s %s (status 0x%02X): %s",
            service_info.address,
            "awake" if reading.awake else "asleep",
            reading.status,
            payload.hex(),
        )
        self._last_update_time = datetime.now(timezone.utc)

        address = service_info.address
        name = f"TPMS {short_address(address)}"
        self.set_device_manufacturer("Tesla / Autel (BLE TPMS)")
        self.set_device_type("BLE TPMS")
        self.set_device_name(name)
        self.set_title(name)

        self.update_binary_sensor(
            key=str(TPMSBinarySensor.AWAKE),
            native_value=reading.awake,
            name="Awake",
        )
        self.update_sensor(
            key=str(TPMSSensor.STATUS),
            native_unit_of_measurement=None,
            native_value=reading.status,
            name="Status",
        )

        if reading.pressure_bar is not None:
            self.update_sensor(
                key=str(TPMSSensor.PRESSURE),
                native_unit_of_measurement=None,
                native_value=round(reading.pressure_bar + self._pressure_trim, 3),
                name="Pressure",
            )
            self.update_sensor(
                key=str(TPMSSensor.RAW_PRESSURE),
                native_unit_of_measurement=None,
                native_value=reading.raw["pressure"],
                name="Raw pressure",
            )

        if reading.temperature_c is not None:
            self.update_sensor(
                key=str(TPMSSensor.TEMPERATURE),
                native_unit_of_measurement=None,
                native_value=round(reading.temperature_c + self._temperature_trim, 1),
                name="Temperature",
            )
            self.update_sensor(
                key=str(TPMSSensor.RAW_TEMPERATURE),
                native_unit_of_measurement=None,
                native_value=reading.raw["temperature"],
                name="Raw temperature",
            )

        if reading.battery_volts is not None:
            self.update_sensor(
                key=str(TPMSSensor.VOLTAGE),
                native_unit_of_measurement=None,
                native_value=reading.battery_volts,
                name="Voltage",
            )
        if reading.battery_percent is not None:
            self.update_sensor(
                key=str(TPMSSensor.BATTERY),
                native_unit_of_measurement=None,
                native_value=reading.battery_percent,
                name="Battery",
            )

    def update_from_connection(
        self, reading: TeslaTpmsReading, service_info: BluetoothServiceInfo
    ) -> SensorUpdate:
        """Build a :class:`SensorUpdate` from a reading fetched over GATT.

        The advertisement path (``update``) is driven by the coordinator; this
        is its equivalent for the optional active poll. A connection reading is
        always awake and carries pressure and temperature in the ``TPData``
        encoding, which is *not* the advertisement's -- so ``RAW_PRESSURE`` /
        ``RAW_TEMPERATURE`` here hold the GATT integers, and ``STATUS`` (an
        advertisement-only byte) is left untouched.
        """
        self._events_updates.clear()
        address = service_info.address
        name = f"TPMS {short_address(address)}"
        self.set_device_manufacturer("Tesla / Autel (BLE TPMS)")
        self.set_device_type("BLE TPMS")
        self.set_device_name(name)
        self.set_title(name)

        self.update_binary_sensor(
            key=str(TPMSBinarySensor.AWAKE),
            native_value=True,
            name="Awake",
        )
        if reading.pressure_bar is not None:
            self.update_sensor(
                key=str(TPMSSensor.PRESSURE),
                native_unit_of_measurement=None,
                native_value=round(reading.pressure_bar + self._pressure_trim, 3),
                name="Pressure",
            )
            if "gatt_pressure" in reading.raw:
                self.update_sensor(
                    key=str(TPMSSensor.RAW_PRESSURE),
                    native_unit_of_measurement=None,
                    native_value=reading.raw["gatt_pressure"],
                    name="Raw pressure",
                )
        if reading.temperature_c is not None:
            self.update_sensor(
                key=str(TPMSSensor.TEMPERATURE),
                native_unit_of_measurement=None,
                native_value=round(reading.temperature_c + self._temperature_trim, 1),
                name="Temperature",
            )
            if "gatt_temperature" in reading.raw:
                self.update_sensor(
                    key=str(TPMSSensor.RAW_TEMPERATURE),
                    native_unit_of_measurement=None,
                    native_value=reading.raw["gatt_temperature"],
                    name="Raw temperature",
                )

        self._last_update_time = datetime.now(timezone.utc)
        self.update_signal_strength(service_info.rssi)
        return self._finish_update()
