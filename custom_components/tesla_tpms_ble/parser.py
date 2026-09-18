"""Home Assistant binding for the Tesla-protocol TPMS decoder.

All the protocol knowledge lives in :mod:`decoder`, which is deliberately free
of Home Assistant imports. This module only adapts it to the
``bluetooth_sensor_state_data`` interface that the passive Bluetooth
coordinator expects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data.enum import StrEnum

from .decoder import (
    PROFILE_DEFAULT,
    PROFILES,
    TESLA_COMPANY_ID,
    DecodeProfile,
    decode,
    looks_like_tesla_tpms,
)

_LOGGER = logging.getLogger(__name__)


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

        if not looks_like_tesla_tpms(payload):
            # A Tesla *vehicle* advertises under the same company ID. Ignore it
            # rather than inventing a tyre out of the VCSEC beacon.
            _LOGGER.debug(
                "Tesla company ID from %s but payload is not TPMS-shaped: %s",
                service_info.address,
                payload.hex(),
            )
            return

        reading = decode(payload, self._profile)
        if reading is None:
            _LOGGER.debug(
                "Undecodable Tesla TPMS payload from %s: %s",
                service_info.address,
                payload.hex(),
            )
            return

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
