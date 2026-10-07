"""Tests for the Tesla-protocol TPMS decoder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "tesla_tpms_ble"))

import pytest  # noqa: E402

from decoder import (  # noqa: E402
    PROFILE_DEFAULT,
    PROFILE_LEGACY,
    TESLA_COMPANY_ID,
    battery_percentage,
    build_tpdata_requests,
    decode,
    decode_full_advertisement,
    decode_tpdata,
    looks_like_tesla_tpms,
)


# Captured from a fitted Autel MX-Sensor BLE through Home Assistant's Bluetooth
# diagnostics: the whole advertisement, and its manufacturer data with the
# company ID stripped. A sleeping sensor sends just these three bytes.
REAL_SLEEP_ADVERTISEMENT = bytes.fromhex("020106" "06ff2b02" "01fe03")
REAL_SLEEP_FRAME = bytes.fromhex("01fe03")


def build_payload(status=0x0A, pressure=390, temperature=72, battery_mv=3050,
                  byte0=0x01, byte1=0x02):
    """Build a synthetic manufacturer-data payload (company ID stripped)."""
    return bytes([byte0, byte1, status]) \
        + pressure.to_bytes(2, "little") \
        + bytes([temperature]) \
        + battery_mv.to_bytes(2, "little")


def build_advertisement(mfr_payload, name=b"tsTPMS"):
    """Wrap a payload in a realistic AD structure stream."""
    flags = bytes([0x02, 0x01, 0x06])
    mfr_body = TESLA_COMPANY_ID.to_bytes(2, "little") + mfr_payload
    mfr = bytes([len(mfr_body) + 1, 0xFF]) + mfr_body
    name_ad = bytes([len(name) + 1, 0x09]) + name
    return flags + mfr + name_ad


class TestCompanyId:
    def test_tesla_company_id_is_0x022b(self):
        assert TESLA_COMPANY_ID == 0x022B == 555

    def test_little_endian_marker_matches_sketch(self):
        # The ESP32 sketch scans for the bytes 0x2B 0x02; that is exactly the
        # Tesla company ID encoded little-endian.
        assert TESLA_COMPANY_ID.to_bytes(2, "little") == b"\x2b\x02"


class TestDefaultProfile:
    def test_decodes_nominal_reading(self):
        r = decode(build_payload())
        assert r is not None
        assert r.awake is True
        # 390 raw kPa absolute - 100 kPa atmosphere = 290 kPa gauge = 2.90 bar
        assert r.pressure_bar == pytest.approx(2.90)
        assert r.pressure_kpa == pytest.approx(290.0)
        assert r.pressure_psi == pytest.approx(42.06, abs=0.01)
        assert r.temperature_c == pytest.approx(22.0)
        assert r.battery_volts == pytest.approx(3.05)

    def test_tesla_placard_pressure_round_trips(self):
        # Model 3/Y placard is 42 psi cold = 2.90 bar.
        r = decode(build_payload(pressure=390))
        assert r.pressure_psi == pytest.approx(42.06, abs=0.1)

    def test_zero_gauge_pressure(self):
        r = decode(build_payload(pressure=100))
        assert r.pressure_bar == pytest.approx(0.0)

    def test_slightly_negative_pressure_is_clamped(self):
        r = decode(build_payload(pressure=95))
        assert r.pressure_bar == 0.0

    def test_sub_zero_temperature(self):
        r = decode(build_payload(temperature=10))
        assert r.temperature_c == pytest.approx(-40.0)

    def test_operating_range_endpoints_representable(self):
        # Autel specs -40..+105 degC for this part; both must be encodable.
        assert decode(build_payload(temperature=10)).temperature_c == -40.0
        assert decode(build_payload(temperature=155)).temperature_c == 105.0


class TestLegacyProfile:
    def test_matches_published_sketch_pressure(self):
        r = decode(build_payload(pressure=390), PROFILE_LEGACY)
        # Sketch: psi = (raw - 100) / 7
        assert r.pressure_psi == pytest.approx((390 - 100) / 7, abs=0.02)

    def test_matches_published_sketch_temperature(self):
        r = decode(build_payload(temperature=72), PROFILE_LEGACY)
        # Sketch: degF = raw - 1
        assert r.temperature_c == pytest.approx((71 - 32) * 5 / 9, abs=0.05)

    def test_profiles_agree_at_room_temperature(self):
        # The two temperature models intersect at raw == 71.25, i.e. ~21 degC.
        # This is the evidence that the Fahrenheit fit was a bench calibration
        # linearised around room temperature.
        d = decode(build_payload(temperature=71), PROFILE_DEFAULT)
        legacy = decode(build_payload(temperature=71), PROFILE_LEGACY)
        assert d.temperature_c == pytest.approx(legacy.temperature_c, abs=0.3)

    def test_profiles_diverge_away_from_room_temperature(self):
        d = decode(build_payload(temperature=130), PROFILE_DEFAULT)
        legacy = decode(build_payload(temperature=130), PROFILE_LEGACY)
        assert abs(d.temperature_c - legacy.temperature_c) > 15

    def test_pressure_profiles_differ_by_about_1_5_percent(self):
        d = decode(build_payload(pressure=390), PROFILE_DEFAULT)
        legacy = decode(build_payload(pressure=390), PROFILE_LEGACY)
        ratio = d.pressure_bar / legacy.pressure_bar
        assert ratio == pytest.approx(7.0 / 6.89475729, abs=0.001)


class TestSleepMode:
    @pytest.mark.parametrize("status", [0x00, 0x01, 0x02, 0x03, 0x04])
    def test_status_below_5_reports_asleep(self, status):
        r = decode(build_payload(status=status))
        assert r.awake is False
        assert r.pressure_bar is None
        assert r.temperature_c is None

    def test_sleep_frame_still_reports_plausible_battery(self):
        r = decode(build_payload(status=0x02, battery_mv=3000))
        assert r.battery_volts == pytest.approx(3.0)

    def test_sleep_frame_rejects_implausible_battery(self):
        r = decode(build_payload(status=0x02, battery_mv=0))
        assert r.battery_volts is None

    @pytest.mark.parametrize("status", [0x05, 0x06, 0x0A, 0xFF])
    def test_status_5_and_above_is_awake(self, status):
        assert decode(build_payload(status=status)).awake is True

    def test_real_three_byte_sleep_frame_decodes(self):
        r = decode(REAL_SLEEP_FRAME)
        assert r is not None
        assert r.awake is False
        assert r.status == 0x03
        assert r.pressure_bar is None
        assert r.temperature_c is None
        assert r.battery_volts is None
        assert r.raw == {"byte0": 0x01, "byte1": 0xFE, "status": 0x03}

    def test_real_sleep_advertisement_decodes(self):
        r = decode_full_advertisement(REAL_SLEEP_ADVERTISEMENT)
        assert r is not None
        assert r.awake is False
        assert r.status == 0x03

    def test_truncated_awake_frame_returns_none(self):
        # Status says awake, but the measurement bytes are missing.
        assert decode(bytes([0x01, 0xFE, 0x0A])) is None


class TestGuards:
    def test_short_payload_returns_none(self):
        assert decode(b"\x01\x02\x0a\x86\x01\x48") is None

    def test_empty_payload_returns_none(self):
        assert decode(b"") is None

    def test_raw_fields_are_preserved_for_calibration(self):
        r = decode(build_payload(pressure=390, temperature=72, battery_mv=3050))
        assert r.raw["pressure"] == 390
        assert r.raw["temperature"] == 72
        assert r.raw["battery_mv"] == 3050


class TestShapeHeuristic:
    def test_accepts_plausible_tpms_frame(self):
        assert looks_like_tesla_tpms(build_payload()) is True

    def test_rejects_too_short(self):
        assert looks_like_tesla_tpms(b"\x01\x02") is False

    def test_rejects_none(self):
        assert looks_like_tesla_tpms(None) is False

    def test_accepts_real_three_byte_sleep_frame(self):
        assert looks_like_tesla_tpms(REAL_SLEEP_FRAME) is True

    def test_rejects_truncated_awake_frame(self):
        assert looks_like_tesla_tpms(bytes([0x01, 0xFE, 0x0A])) is False

    def test_rejects_vehicle_like_battery_field(self):
        # A Tesla *vehicle* also advertises under company ID 0x022B; its payload
        # will not have a plausible coin-cell voltage at offset 6..7.
        assert looks_like_tesla_tpms(build_payload(battery_mv=60000)) is False


class TestFullAdvertisement:
    def test_decodes_from_well_formed_ad_stream(self):
        adv = build_advertisement(build_payload())
        r = decode_full_advertisement(adv)
        assert r is not None
        assert r.pressure_bar == pytest.approx(2.90)
        assert r.temperature_c == pytest.approx(22.0)

    def test_falls_back_to_marker_scan_on_malformed_stream(self):
        raw = b"\xde\xad" + b"\x2b\x02" + build_payload()
        r = decode_full_advertisement(raw)
        assert r is not None
        assert r.pressure_bar == pytest.approx(2.90)

    def test_returns_none_without_tesla_company_id(self):
        adv = bytes([0x02, 0x01, 0x06]) + bytes([0x05, 0xFF, 0x4C, 0x00, 0x01, 0x02])
        assert decode_full_advertisement(adv) is None


class TestBatteryCurve:
    def test_full_battery(self):
        assert battery_percentage(3.4) == 100

    def test_flat_battery(self):
        assert battery_percentage(2.5) == 0

    def test_monotonic(self):
        volts = [2.5 + i * 0.02 for i in range(50)]
        pcts = [battery_percentage(v) for v in volts]
        assert pcts == sorted(pcts)


class TestTPDataRequests:
    """Candidate TPDataRequest frames written to 0212 over a connection."""

    def test_simplest_first_and_all_encode_the_enum(self):
        frames = build_tpdata_requests()
        assert frames[0] == bytes.fromhex("0801")  # bare: field 1 varint = 1
        assert bytes.fromhex("0a020801") in frames  # wrapped as outer field 1
        # Every candidate carries the 08 01 enum somewhere.
        assert all(b"\x08\x01" in f for f in frames)
        # The length-prefixed variants are the first set again, prefixed.
        assert bytes.fromhex("00020801") in frames

    def test_no_duplicates(self):
        frames = build_tpdata_requests()
        assert len(frames) == len(set(frames))


class TestDecodeTPData:
    """Decoding a TPData indication received on 0213."""

    # Real replies captured from the four fitted Autel sensors (parked, cold).
    # Nesting: field 2 -> field 28 -> {field 1 = pressure, field 2 = temp}.
    REAL_REPLIES = {
        "10F1": ("1208e20105089e041026", 542, 38, 19),
        "1104": ("1208e20105089a041024", 538, 36, 18),
        "10B2": ("1208e20105088f041026", 527, 38, 19),
        "142E": ("1208e20105088b041024", 523, 36, 18),
    }

    @pytest.mark.parametrize("name", list(REAL_REPLIES))
    def test_real_captured_replies_decode(self, name):
        hex_reply, raw_p, raw_t, temp_c = self.REAL_REPLIES[name]
        reading = decode_tpdata(bytes.fromhex(hex_reply))
        assert reading is not None
        assert reading.awake is True
        assert reading.profile == "connection"
        assert reading.raw["gatt_pressure"] == raw_p
        assert reading.raw["gatt_temperature"] == raw_t
        assert reading.temperature_c == temp_c
        # ~42 psi placard: raw * 0.08 psi -> bar.
        assert reading.pressure_bar == pytest.approx(raw_p * 0.08 / 14.50377, abs=0.01)

    def test_pressure_lands_near_the_42_psi_placard(self):
        # Sanity: a real capture should read ~2.9 bar, not absurdly off.
        reading = decode_tpdata(bytes.fromhex("1208e20105088f041026"))
        assert 2.5 < reading.pressure_bar < 3.3

    def test_negative_temperature_is_zigzag_decoded(self):
        # bare {pressure=527, temperature=-5 -> zigzag 9}
        reading = decode_tpdata(bytes.fromhex("088f041009"))
        assert reading.temperature_c == -5.0

    def test_bare_message_without_wrappers_decodes(self):
        reading = decode_tpdata(bytes.fromhex("088f041026"))
        assert reading.raw["gatt_pressure"] == 527
        assert reading.temperature_c == 19.0

    def test_pressure_only(self):
        reading = decode_tpdata(bytes.fromhex("088f04"))  # no temperature field
        assert reading.raw["gatt_pressure"] == 527
        assert reading.temperature_c is None

    def test_rejects_rubbish(self):
        assert decode_tpdata(b"") is None
        assert decode_tpdata(b"\xff\xff\xff") is None

    def test_rejects_an_implausible_pressure(self):
        # field 1 varint = 200000, far outside any real reading
        assert decode_tpdata(bytes.fromhex("08c09a0c")) is None
