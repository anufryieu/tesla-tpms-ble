"""End-to-end tests through the real Home Assistant parser pipeline.

Skipped automatically if Home Assistant is not installed, so the pure decoder
tests still run anywhere.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ha = pytest.importorskip("homeassistant.const")
bsd = pytest.importorskip("bluetooth_sensor_state_data")

from home_assistant_bluetooth import BluetoothServiceInfo  # noqa: E402

from custom_components.tesla_tpms_ble.parser import (  # noqa: E402
    TeslaTPMSBluetoothDeviceData,
    TPMSBinarySensor,
    TPMSSensor,
)
from custom_components.tesla_tpms_ble.decoder import (  # noqa: E402
    PROFILE_LEGACY,
    TESLA_COMPANY_ID,
)

ADDRESS = "E1:4C:2A:88:19:F3"

# Manufacturer data from a fitted Autel MX-Sensor BLE, company ID stripped,
# captured through Home Assistant's Bluetooth diagnostics.
REAL_SLEEP_FRAME = bytes.fromhex("01fe03")

MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_tpms_ble"
    / "manifest.json"
)


def service_info(payload: bytes, company_id: int = TESLA_COMPANY_ID, name="tsTPMS"):
    return BluetoothServiceInfo(
        name=name,
        address=ADDRESS,
        rssi=-71,
        manufacturer_data={company_id: payload},
        service_uuids=[],
        service_data={},
        source="local",
    )


def bleak_service_info(payload: bytes, connectable: bool):
    """What HA's Bluetooth manager hands the discovery matchers for one advert."""
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    mfr = {TESLA_COMPANY_ID: payload}
    return BluetoothServiceInfoBleak(
        name="tsTPMS",
        address=ADDRESS,
        rssi=-71,
        manufacturer_data=mfr,
        service_data={},
        service_uuids=[],
        source="proxy",
        device=BLEDevice(ADDRESS, "tsTPMS", None),
        advertisement=AdvertisementData(
            local_name="tsTPMS",
            manufacturer_data=mfr,
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=-71,
            platform_data=(),
        ),
        connectable=connectable,
        time=0.0,
        tx_power=None,
    )


def payload(status=0x0A, pressure=390, temperature=72, battery_mv=3050):
    return bytes([0x01, 0x02, status]) + pressure.to_bytes(2, "little") \
        + bytes([temperature]) + battery_mv.to_bytes(2, "little")


def values(update):
    return {k.key: v.native_value for k, v in update.entity_values.items()}


def binary_values(update):
    return {k.key: v.native_value for k, v in update.binary_entity_values.items()}


class TestParserPipeline:
    def test_awake_frame_produces_expected_entities(self):
        update = TeslaTPMSBluetoothDeviceData().update(service_info(payload()))
        v = values(update)
        assert v[TPMSSensor.PRESSURE] == pytest.approx(2.90)
        assert v[TPMSSensor.TEMPERATURE] == pytest.approx(22.0)
        assert v[TPMSSensor.VOLTAGE] == pytest.approx(3.05)
        assert v[TPMSSensor.BATTERY] == 97
        assert binary_values(update)[TPMSBinarySensor.AWAKE] is True

    def test_raw_diagnostics_are_published(self):
        v = values(TeslaTPMSBluetoothDeviceData().update(service_info(payload())))
        assert v[TPMSSensor.RAW_PRESSURE] == 390
        assert v[TPMSSensor.RAW_TEMPERATURE] == 72
        assert v[TPMSSensor.STATUS] == 0x0A

    def test_device_is_named_and_titled(self):
        data = TeslaTPMSBluetoothDeviceData()
        data.update(service_info(payload()))
        assert "TPMS" in (data.title or "")

    def test_sleep_frame_publishes_no_pressure_or_temperature(self):
        update = TeslaTPMSBluetoothDeviceData().update(
            service_info(payload(status=0x02))
        )
        v = values(update)
        # Stale bytes must not be turned into a confident wrong tyre pressure.
        assert TPMSSensor.PRESSURE not in v
        assert TPMSSensor.TEMPERATURE not in v
        assert binary_values(update)[TPMSBinarySensor.AWAKE] is False

    def test_supported_accepts_a_tpms_frame(self):
        assert TeslaTPMSBluetoothDeviceData().supported(service_info(payload())) is True

    def test_supported_accepts_the_real_sleep_frame(self):
        # What a fitted Autel sensor actually sends while the wheel is still.
        # Rejecting it aborts the discovery flow, and HA does not offer the
        # sensor again while it keeps transmitting.
        info = service_info(REAL_SLEEP_FRAME, name="tsTPMS ")
        assert TeslaTPMSBluetoothDeviceData().supported(info) is True

    def test_real_sleep_frame_publishes_no_measurements(self):
        update = TeslaTPMSBluetoothDeviceData().update(
            service_info(REAL_SLEEP_FRAME, name="tsTPMS ")
        )
        v = values(update)
        assert set(v) == {TPMSSensor.STATUS, TPMSSensor.SIGNAL_STRENGTH}
        assert v[TPMSSensor.STATUS] == 0x03
        assert binary_values(update) == {TPMSBinarySensor.AWAKE: False}

    def test_supported_rejects_other_manufacturers(self):
        # Apple, company id 0x004C -- must not be claimed by this integration.
        info = service_info(payload(), company_id=0x004C)
        assert TeslaTPMSBluetoothDeviceData().supported(info) is False

    def test_supported_rejects_a_tesla_vehicle_beacon(self):
        # Same company ID, but the field at offset 6..7 is not a cell voltage.
        info = service_info(payload(battery_mv=51000))
        assert TeslaTPMSBluetoothDeviceData().supported(info) is False

    def test_trim_offsets_are_applied(self):
        data = TeslaTPMSBluetoothDeviceData(
            pressure_trim=0.05, temperature_trim=-1.5
        )
        v = values(data.update(service_info(payload())))
        assert v[TPMSSensor.PRESSURE] == pytest.approx(2.95)
        assert v[TPMSSensor.TEMPERATURE] == pytest.approx(20.5)

    def test_profile_can_be_switched_at_runtime(self):
        data = TeslaTPMSBluetoothDeviceData()
        assert values(data.update(service_info(payload())))[
            TPMSSensor.PRESSURE
        ] == pytest.approx(2.90)
        data.set_profile("legacy")
        assert values(data.update(service_info(payload())))[
            TPMSSensor.PRESSURE
        ] == pytest.approx(2.856, abs=0.002)

    def test_legacy_profile_via_constructor(self):
        data = TeslaTPMSBluetoothDeviceData(profile=PROFILE_LEGACY)
        v = values(data.update(service_info(payload())))
        assert v[TPMSSensor.PRESSURE] == pytest.approx(2.856, abs=0.002)

    def test_last_update_time_is_tracked(self):
        data = TeslaTPMSBluetoothDeviceData()
        assert data.last_update_time is None
        data.update(service_info(payload()))
        assert data.last_update_time is not None


class TestPlatformConversion:
    def test_sensor_update_converts_to_bluetooth_data_update(self):
        from custom_components.tesla_tpms_ble.sensor import (
            SENSOR_DESCRIPTIONS,
            sensor_update_to_bluetooth_data_update,
        )

        update = TeslaTPMSBluetoothDeviceData().update(service_info(payload()))
        converted = sensor_update_to_bluetooth_data_update(update)
        assert converted.entity_data
        for key in converted.entity_descriptions:
            assert key.key in SENSOR_DESCRIPTIONS

    def test_binary_sensor_update_converts(self):
        from custom_components.tesla_tpms_ble.binary_sensor import (
            BINARY_SENSOR_DESCRIPTIONS,
            sensor_update_to_bluetooth_data_update,
        )

        update = TeslaTPMSBluetoothDeviceData().update(service_info(payload()))
        converted = sensor_update_to_bluetooth_data_update(update)
        for key in converted.entity_descriptions:
            assert key.key in BINARY_SENSOR_DESCRIPTIONS

    def test_manifest_matcher_covers_the_tesla_company_id(self):
        manifest = json.loads(MANIFEST.read_text())
        ids = [m.get("manufacturer_id") for m in manifest["bluetooth"]]
        assert TESLA_COMPANY_ID in ids, "manifest would never trigger discovery"


class TestDiscoveryMatcher:
    """The manifest matchers, run through Home Assistant's own matching code."""

    @pytest.mark.parametrize(
        "connectable", [True, False], ids=["local-adapter", "passive-proxy"]
    )
    def test_tpms_advert_triggers_discovery(self, connectable):
        # A local adapter reports every advert as connectable. A passive
        # scanner -- an ESPHome proxy without active connections, a Shelly --
        # reports every advert as non-connectable, and HA only runs a matcher
        # against those when it says "connectable": false.
        from homeassistant.components.bluetooth.match import ble_device_matches

        matchers = json.loads(MANIFEST.read_text())["bluetooth"]
        info = bleak_service_info(payload(), connectable=connectable)
        assert any(ble_device_matches(m, info) for m in matchers)
