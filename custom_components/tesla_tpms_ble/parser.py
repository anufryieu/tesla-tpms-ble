"""Home Assistant binding for the Tesla-protocol TPMS decoder.

All the protocol knowledge lives in :mod:`decoder`, which is deliberately free
of Home Assistant imports. This module only adapts it to the
``bluetooth_sensor_state_data`` interface that the passive Bluetooth
coordinator expects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data.enum import StrEnum

from .decoder import (
    MIN_PAYLOAD_LEN,
    PROFILE_DEFAULT,
    PROFILES,
    TESLA_COMPANY_ID,
    DecodeProfile,
    decode,
    looks_like_tesla_tpms,
)

_LOGGER = logging.getLogger(__name__)

# How many *distinct* payloads to remember per sensor. A well-behaved sensor
# only ever shows a handful; the cap stops a sensor that varies a counter byte
# on every frame from growing this without bound.
MAX_DISTINCT_PAYLOADS: Final[int] = 32


class PayloadHistory:
    """Remember every distinct manufacturer payload a sensor has sent.

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

    The history lives in memory only, so it starts empty after a Home Assistant
    restart. ``started`` records when, so a diagnostic is never read as covering
    more time than it does.
    """

    def __init__(self) -> None:
        """Start an empty history."""
        self._entries: dict[bytes, dict[str, Any]] = {}
        self.started = datetime.now(timezone.utc)
        self.total_frames = 0
        self.distinct_dropped = 0

    def record(
        self,
        payload: bytes,
        rssi: int | None,
        accepted: bool,
        note: str,
    ) -> None:
        """Record one advertisement payload and how the decoder judged it."""
        self.total_frames += 1
        now = datetime.now(timezone.utc)
        entry = self._entries.get(payload)

        if entry is None:
            if len(self._entries) >= MAX_DISTINCT_PAYLOADS:
                # Better to lose the newest oddity than to grow without bound;
                # the count still shows something was dropped.
                self.distinct_dropped += 1
                return
            entry = self._entries[payload] = {
                "hex": payload.hex(),
                "length": len(payload),
                "first_seen": now.isoformat(),
                "count": 0,
                "rssi_min": rssi,
                "rssi_max": rssi,
            }

        entry["count"] += 1
        entry["last_seen"] = now.isoformat()
        entry["accepted"] = accepted
        entry["note"] = note
        if rssi is not None:
            for key, better in (("rssi_min", min), ("rssi_max", max)):
                current = entry[key]
                entry[key] = rssi if current is None else better(current, rssi)

    @property
    def max_payload_len(self) -> int:
        """Longest payload seen. Above ``MIN_PAYLOAD_LEN`` means a reading."""
        return max((len(p) for p in self._entries), default=0)

    def as_dict(self) -> dict[str, Any]:
        """Render the history for a diagnostics download."""
        payloads = sorted(
            self._entries.values(), key=lambda e: e["count"], reverse=True
        )
        saw_reading = self.max_payload_len >= MIN_PAYLOAD_LEN
        return {
            "history_started": self.started.isoformat(),
            "total_frames": self.total_frames,
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
        manufacturer_data = service_info.manufacturer_data
        if not manufacturer_data:
            return

        payload = manufacturer_data.get(TESLA_COMPANY_ID)
        if payload is None:
            return

        # Record before judging: a frame rejected below is exactly the kind we
        # would otherwise never hear about.
        rssi = getattr(service_info, "rssi", None)

        if not looks_like_tesla_tpms(payload):
            # A Tesla *vehicle* advertises under the same company ID. Ignore it
            # rather than inventing a tyre out of the VCSEC beacon.
            self.history.record(payload, rssi, accepted=False, note="not TPMS-shaped")
            _LOGGER.debug(
                "Tesla company ID from %s but payload is not TPMS-shaped: %s",
                service_info.address,
                payload.hex(),
            )
            return

        reading = decode(payload, self._profile)
        if reading is None:
            self.history.record(payload, rssi, accepted=False, note="undecodable")
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
