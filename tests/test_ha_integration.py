"""End-to-end tests through the real Home Assistant parser pipeline.

Skipped automatically if Home Assistant is not installed, so the pure decoder
tests still run anywhere.
"""
import asyncio
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


def service_info(
    payload: bytes,
    company_id: int = TESLA_COMPANY_ID,
    name="tsTPMS",
    address=ADDRESS,
    service_data=None,
):
    return BluetoothServiceInfo(
        name=name,
        address=address,
        rssi=-71,
        manufacturer_data={company_id: payload} if payload is not None else {},
        service_uuids=[],
        service_data=service_data or {},
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


class TestPayloadHistory:
    """The recorder that survives a sensor falling asleep.

    A diagnostic downloaded on the drive always shows a sleep frame, so the
    history has to keep the awake frame that arrived minutes earlier on the
    road.
    """

    def test_sleep_frames_are_recorded_and_deduplicated(self):
        device = TeslaTPMSBluetoothDeviceData()
        for _ in range(5):
            device.update(service_info(REAL_SLEEP_FRAME))

        history = device.history.as_dict()
        assert history["advertisements_forwarded"] == 5
        assert history["distinct_payloads"] == 1
        assert history["payloads"][0]["hex"] == "01fe03"
        assert history["payloads"][0]["count"] == 5
        assert history["payloads"][0]["note"] == "asleep"
        assert history["payloads"][0]["accepted"] is True

    def test_an_awake_frame_is_still_there_after_the_sensor_sleeps_again(self):
        device = TeslaTPMSBluetoothDeviceData()
        device.update(service_info(REAL_SLEEP_FRAME))
        device.update(service_info(payload()))  # the wheel turns, briefly
        for _ in range(50):  # parked again, sleep frames for hours
            device.update(service_info(REAL_SLEEP_FRAME))

        history = device.history.as_dict()
        assert history["saw_a_full_length_frame"] is True
        assert history["max_payload_len"] == len(payload())
        awake = [p for p in history["payloads"] if p["note"] == "awake"]
        assert len(awake) == 1, "the one awake frame must survive the sleep frames"
        assert awake[0]["count"] == 1

    def test_only_sleep_frames_reports_no_reading(self):
        device = TeslaTPMSBluetoothDeviceData()
        device.update(service_info(REAL_SLEEP_FRAME))

        history = device.history.as_dict()
        assert history["saw_a_full_length_frame"] is False
        assert history["max_payload_len"] == 3

    def test_rejected_frames_are_recorded_too(self):
        # The whole point: a frame the shape check throws away is exactly the
        # one we would otherwise never learn about.
        device = TeslaTPMSBluetoothDeviceData()
        device.update(service_info(bytes.fromhex("01fe0a"))) # awake but truncated

        history = device.history.as_dict()
        assert history["distinct_payloads"] == 1
        entry = history["payloads"][0]
        assert entry["accepted"] is False
        assert entry["note"] == "not TPMS-shaped"

    def test_distinct_payloads_are_capped(self):
        from custom_components.tesla_tpms_ble.parser import MAX_DISTINCT_PAYLOADS

        device = TeslaTPMSBluetoothDeviceData()
        for i in range(MAX_DISTINCT_PAYLOADS + 10):
            device.update(service_info(bytes([1, 0xFE, 3, i & 0xFF])))

        history = device.history.as_dict()
        assert history["distinct_payloads"] == MAX_DISTINCT_PAYLOADS
        assert history["distinct_dropped"] == 10

    def test_an_advert_without_tesla_data_is_recorded(self):
        # If an awake sensor put its reading anywhere but company 0x022B, the
        # parser used to return without a trace.
        device = TeslaTPMSBluetoothDeviceData()
        device.update(
            service_info(
                None, service_data={"00001122-0000-1000-8000-00805f9b34fb": b"\x01\x02"}
            )
        )

        history = device.history.as_dict()
        entry = history["payloads"][0]
        assert entry["note"] == "no Tesla manufacturer data"
        assert entry["hex"] is None
        assert "00001122" in entry["advertisement"]
        assert history["max_payload_len"] == 0

    def test_timeline_keeps_order_gaps_and_source(self):
        device = TeslaTPMSBluetoothDeviceData()
        device.update(service_info(REAL_SLEEP_FRAME))
        device.update(service_info(payload()))
        device.update(service_info(REAL_SLEEP_FRAME))

        timeline = device.history.as_dict()["timeline"]
        assert [e["note"] for e in timeline] == ["asleep", "awake", "asleep"]
        assert timeline[0]["gap_seconds"] is None
        assert timeline[1]["gap_seconds"] is not None
        assert timeline[1]["source"] == "local"
        assert device.history.as_dict()["payloads"][0]["sources"] == ["local"]

    def test_timeline_is_capped(self):
        from custom_components.tesla_tpms_ble.parser import MAX_TIMELINE_EVENTS

        device = TeslaTPMSBluetoothDeviceData()
        for _ in range(MAX_TIMELINE_EVENTS + 5):
            device.update(service_info(REAL_SLEEP_FRAME))
        assert len(device.history.as_dict()["timeline"]) == MAX_TIMELINE_EVENTS


class TestUnknownAddressWatcher:
    """Tesla-looking adverts from addresses no sensor is set up for."""

    def watcher(self):
        from custom_components.tesla_tpms_ble.watcher import UnknownAddressWatcher

        w = UnknownAddressWatcher()
        w.configured.add(ADDRESS)
        return w

    def test_configured_address_is_left_to_its_entry(self):
        w = self.watcher()
        w._async_advertisement(service_info(payload()), None)
        assert w.as_dict()["addresses"] == {}

    def test_awake_frame_from_another_address_is_kept(self):
        w = self.watcher()
        other = "5A:11:22:33:44:55"
        w._async_advertisement(service_info(payload(), address=other), None)

        seen = w.as_dict()["addresses"][other]
        assert seen["name"] == "tsTPMS"
        assert seen["saw_a_full_length_frame"] is True
        assert seen["payloads"][0]["note"] == "awake"

    def test_sensor_address_block_is_kept_without_tesla_data(self):
        w = self.watcher()
        other = "BC:6A:29:00:00:01"
        w._async_advertisement(service_info(None, address=other), None)
        assert w.as_dict()["addresses"][other]["payloads"][0]["note"] == (
            "no Tesla manufacturer data"
        )

    def test_unrelated_devices_are_ignored(self):
        w = self.watcher()
        w._async_advertisement(
            service_info(payload(), company_id=0x004C, address="11:22:33:44:55:66"),
            None,
        )
        assert w.as_dict()["addresses"] == {}

    def test_addresses_are_capped(self):
        from custom_components.tesla_tpms_ble.watcher import MAX_WATCHED_ADDRESSES

        w = self.watcher()
        for i in range(MAX_WATCHED_ADDRESSES + 3):
            w._async_advertisement(
                service_info(REAL_SLEEP_FRAME, address=f"5A:00:00:00:00:{i:02X}"),
                None,
            )
        d = w.as_dict()
        assert len(d["addresses"]) == MAX_WATCHED_ADDRESSES
        assert d["addresses_dropped"] == 3


class TestConnectionUpdate:
    """update_from_connection() turns a GATT reading into entities."""

    # A real reply from sensor 10B2: raw pressure 527 (~2.91 bar), temp 19 C.
    REAL_REPLY = bytes.fromhex("1208e20105088f041026")

    def _reading(self):
        from custom_components.tesla_tpms_ble.decoder import decode_tpdata

        return decode_tpdata(self.REAL_REPLY)

    def test_connection_reading_publishes_pressure_and_temperature(self):
        data = TeslaTPMSBluetoothDeviceData()
        update = data.update_from_connection(self._reading(), service_info(b"\x01\x02"))
        v = values(update)
        assert v[TPMSSensor.PRESSURE] == pytest.approx(4.26, abs=0.01)
        assert v[TPMSSensor.TEMPERATURE] == pytest.approx(19.0)
        assert v[TPMSSensor.RAW_PRESSURE] == 527
        assert v[TPMSSensor.RAW_TEMPERATURE] == 38
        assert binary_values(update)[TPMSBinarySensor.AWAKE] is True
        # STATUS is an advertisement-only byte; a connection update must not set it.
        assert TPMSSensor.STATUS not in v

    def test_connection_reading_honours_trim(self):
        data = TeslaTPMSBluetoothDeviceData(pressure_trim=0.1, temperature_trim=-2.0)
        v = values(data.update_from_connection(self._reading(), service_info(b"\x01")))
        assert v[TPMSSensor.PRESSURE] == pytest.approx(4.36, abs=0.01)
        assert v[TPMSSensor.TEMPERATURE] == pytest.approx(17.0)


class _FakeChar:
    def __init__(self, props):
        self.properties = props


class _FakeServices:
    def __init__(self, char):
        self._char = char

    def get_characteristic(self, uuid):
        return self._char


class _FakeClient:
    """Minimal BleakClient stand-in that replies to one known request frame."""

    def __init__(self, answers_to: bytes | None, reply: bytes, props=("write",)):
        self._answers_to = answers_to
        self._reply = reply
        self.services = _FakeServices(_FakeChar(list(props)))
        self._cb = None
        self.writes: list[bytes] = []

    async def start_notify(self, uuid, cb):
        self._cb = cb

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append(bytes(data))
        if self._answers_to is not None and bytes(data) == self._answers_to:
            self._cb(0, bytearray(self._reply))

    async def stop_notify(self, uuid):
        pass


class TestConnectionRequest:
    """TeslaTpmsConnection._async_request: write candidates, parse the reply."""

    def test_learns_the_frame_that_gets_a_reply(self):
        from custom_components.tesla_tpms_ble.connection import TeslaTpmsConnection

        # Sensor answers only the "wrapped as field 1" framing, with a real
        # nested TPData reply (raw pressure 527 ~ 2.91 bar).
        reply = bytes.fromhex("1208e20105088f041026")
        client = _FakeClient(answers_to=bytes.fromhex("0a020801"), reply=reply)
        conn = TeslaTpmsConnection()
        reading = asyncio.run(conn._async_request(client))

        assert reading.pressure_bar == pytest.approx(4.26, abs=0.01)
        assert conn.learned_request == "0a020801"
        # Once learned, a second poll writes only that one frame.
        client2 = _FakeClient(answers_to=bytes.fromhex("0a020801"), reply=reply)
        asyncio.run(conn._async_request(client2))
        assert client2.writes == [bytes.fromhex("0a020801")]

    def test_no_reply_raises(self):
        from custom_components.tesla_tpms_ble.connection import (
            NoTPDataReply,
            TeslaTpmsConnection,
        )

        client = _FakeClient(answers_to=None, reply=b"")
        with pytest.raises(NoTPDataReply):
            asyncio.run(TeslaTpmsConnection()._async_request(client))

    def test_undecodable_reply_is_kept_but_not_learned(self):
        from custom_components.tesla_tpms_ble.connection import (
            NoTPDataReply,
            TeslaTpmsConnection,
        )

        # Sensor answers the first frame, but with bytes we cannot decode.
        client = _FakeClient(answers_to=bytes.fromhex("0801"), reply=b"\xff\xff\xff")
        conn = TeslaTpmsConnection()
        with pytest.raises(NoTPDataReply):
            asyncio.run(conn._async_request(client))
        # The raw reply is kept for diagnostics, but no frame is "learned".
        assert conn.last_reply == "ffffff"
        assert conn.last_replies == ["ffffff"]
        assert conn.learned_request is None

    def test_unwritable_characteristic_raises(self):
        from custom_components.tesla_tpms_ble.connection import (
            NoTPDataReply,
            TeslaTpmsConnection,
        )

        client = _FakeClient(answers_to=None, reply=b"", props=("read",))
        with pytest.raises(NoTPDataReply):
            asyncio.run(TeslaTpmsConnection()._async_request(client))
