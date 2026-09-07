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
    decode,
    decode_full_advertisement,
    looks_like_tesla_tpms,
)


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
