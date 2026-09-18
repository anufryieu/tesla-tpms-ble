"""Pure-Python decoder for Tesla-protocol BLE TPMS advertisements.

This module has **no Home Assistant or bleak dependencies** so that it can be
reused by the standalone capture tool in ``tools/`` and by the unit tests.

Background
----------
Autel MX-Sensor BLE sensors sold "pre-programmed for Tesla" must speak the same
BLE protocol as the Tesla OEM sensor, because the car's TPMS ECU only
understands one format. The sensor broadcasts a connectionless BLE
advertisement containing a Manufacturer Specific Data (AD type 0xFF) element
with Bluetooth SIG company identifier ``0x022B`` -- "Tesla, Inc.".

Payload layout, relative to the start of the manufacturer data (i.e. *after*
the two company-ID bytes have been stripped, which is what both bleak and Home
Assistant hand you in ``manufacturer_data[555]``)::

    offset  size  field
    ------  ----  -----------------------------------------------------
    0       1     unknown / protocol sub-type       (exposed as raw)
    1       1     unknown / counter                 (exposed as raw)
    2       1     status. < 0x05 means the sensor is asleep: the
                  remaining fields are stale or zero and must be ignored.
    3       2     pressure, uint16 little-endian    (see PRESSURE below)
    4
    5       1     temperature, uint8                (see TEMPERATURE below)
    6       2     battery millivolts, uint16 little-endian
    7

A sleeping sensor usually stops after the status byte: a fitted Autel sensor
was captured sending just ``01 fe 03``.

PRESSURE
--------
The upstream ESP32 reverse-engineering effort (cunzulatu/Tesla_BLE_TPMS) fitted
``psi = (raw - 100) / 7`` against a professional TPMS reader, and explicitly
flagged it as approximate.

That constant is almost certainly a rounded stand-in for the psi->kPa factor
6.89476. Reading it that way, the formula collapses to ``kPa_gauge = raw - 100``
-- i.e. the sensor reports *absolute* pressure in whole kPa, and the offset of
100 is the subtraction of one atmosphere (1 atm = 101.325 kPa ~= 100 kPa) to
convert absolute to gauge pressure. That is exactly what a MEMS pressure die
does, and 1 kPa resolution matches the 0.1 bar accuracy Autel specs for this
part. We therefore default to the kPa interpretation, which differs from the
published fit by only 1.5%.

TEMPERATURE
-----------
The same project fitted ``degF = raw - 1``. A plain ``degC = raw - 50`` encoding
is far more common in TPMS silicon, and the two models intersect at
raw = 71.25 -> 21.2 degC / 70.2 degF, i.e. room temperature, which is precisely
where a bench calibration would have been performed. The Fahrenheit fit is a
local linearisation around that point. ``raw - 50`` also spans -50..+205 degC,
covering the -40..+105 degC operating range Autel specifies, whereas the
Fahrenheit reading bottoms out at -18 degC and cannot represent the spec floor.
We therefore default to ``degC = raw - 50``.

Both choices are switchable via :class:`DecodeProfile` and every raw field is
preserved on the result so the formulas can be verified against a reference
gauge. See ``docs/CALIBRATION.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Bluetooth SIG company identifier 0x022B == 555 == "Tesla, Inc."
TESLA_COMPANY_ID: Final[int] = 0x022B

# Minimum manufacturer-data length of any frame: up to and including status.
# This is all a sleeping sensor sends.
MIN_SLEEP_PAYLOAD_LEN: Final[int] = 3

# Minimum manufacturer-data length we need to decode a full reading.
MIN_PAYLOAD_LEN: Final[int] = 8

# status values below this mean "asleep / no fresh measurement".
STATUS_AWAKE_MIN: Final[int] = 0x05

_PSI_PER_BAR: Final[float] = 14.5037737730
_KPA_PER_PSI: Final[float] = 6.89475729


@dataclass(frozen=True)
class DecodeProfile:
    """Selectable decoding constants.

    ``kpa_absolute``/``celsius_offset`` is the default and is what you want
    unless you have measured otherwise. ``legacy`` reproduces the published
    ESP32 sketch bit-for-bit and exists so you can A/B the two against a
    reference gauge.
    """

    name: str = "default"
    # pressure: bar = (raw - pressure_offset) / pressure_divisor
    pressure_offset: float = 100.0
    pressure_divisor: float = 100.0
    # temperature: degC = raw - temperature_offset  (when celsius_native)
    celsius_native: bool = True
    temperature_offset: float = 50.0


#: Default profile: raw pressure is absolute kPa, raw temperature is degC + 50.
PROFILE_DEFAULT: Final[DecodeProfile] = DecodeProfile()

#: Bit-for-bit reproduction of the published ESP32 sketch (psi = (raw-100)/7,
#: degF = raw - 1). Kept for comparison during calibration.
PROFILE_LEGACY: Final[DecodeProfile] = DecodeProfile(
    name="legacy",
    pressure_offset=100.0,
    pressure_divisor=7.0 * _PSI_PER_BAR,  # -> bar
    celsius_native=False,
    temperature_offset=1.0,
)

PROFILES: Final[dict[str, DecodeProfile]] = {
    PROFILE_DEFAULT.name: PROFILE_DEFAULT,
    PROFILE_LEGACY.name: PROFILE_LEGACY,
}


@dataclass
class TeslaTpmsReading:
    """A decoded reading from one Tesla-protocol TPMS sensor."""

    #: False when the sensor is in sleep mode; pressure/temperature are None.
    awake: bool
    status: int
    pressure_bar: float | None = None
    temperature_c: float | None = None
    battery_volts: float | None = None
    battery_percent: int | None = None
    #: Raw, undecoded field values -- surfaced as diagnostics for calibration.
    raw: dict[str, int] = field(default_factory=dict)
    profile: str = PROFILE_DEFAULT.name

    @property
    def pressure_psi(self) -> float | None:
        """Pressure in psi, for cross-checking against a US gauge."""
        if self.pressure_bar is None:
            return None
        return round(self.pressure_bar * _PSI_PER_BAR, 2)

    @property
    def pressure_kpa(self) -> float | None:
        """Pressure in kPa."""
        if self.pressure_bar is None:
            return None
        return round(self.pressure_bar * 100.0, 1)


def looks_like_tesla_tpms(mfr_data: bytes | bytearray | None) -> bool:
    """Cheap shape check used to tell a TPMS sensor from a Tesla *vehicle*.

    Tesla cars also advertise under company ID 0x022B, so matching the company
    ID alone is not enough. A TPMS frame is a short fixed-size record; the
    vehicle's VCSEC advertisement is a different shape. We accept a short sleep
    frame, or anything long enough to decode whose battery voltage lands in a
    physically plausible range for a coin-cell/primary-lithium TPMS sensor.
    """
    if mfr_data is None or len(mfr_data) < MIN_SLEEP_PAYLOAD_LEN:
        return False
    status = mfr_data[2]
    if status < STATUS_AWAKE_MIN:
        # Sleeping sensor: only the status byte is meaningful, so all we can do
        # is a length check. Accept it -- a vehicle advert is not this short.
        return len(mfr_data) <= 16
    if len(mfr_data) < MIN_PAYLOAD_LEN:
        return False
    battery_mv = int.from_bytes(mfr_data[6:8], "little")
    # 1.8 V .. 4.2 V covers every lithium chemistry used in TPMS sensors.
    return 1500 <= battery_mv <= 4300


def battery_percentage(volts: float) -> int:
    """Map cell voltage to a percentage using a lithium discharge curve.

    TPMS sensors use a primary lithium cell with a very flat discharge
    plateau, so this is intentionally coarse -- it is a fuel gauge, not a
    measurement.
    """
    curve: list[tuple[float, int]] = [
        (3.30, 100),
        (3.05, 97),
        (2.94, 91),
        (2.90, 75),
        (2.85, 25),
        (2.80, 17),
        (2.60, 0),
    ]
    if volts >= curve[0][0]:
        return 100
    if volts < curve[-1][0]:
        return 0
    for i in range(len(curve) - 1):
        v_hi, p_hi = curve[i]
        v_lo, p_lo = curve[i + 1]
        if v_lo < volts <= v_hi:
            return int(round(p_lo + ((volts - v_lo) / (v_hi - v_lo)) * (p_hi - p_lo)))
    return 0


def decode(
    mfr_data: bytes | bytearray,
    profile: DecodeProfile = PROFILE_DEFAULT,
) -> TeslaTpmsReading | None:
    """Decode one Tesla-protocol TPMS manufacturer-data payload.

    ``mfr_data`` is the manufacturer data *with the two company-ID bytes
    already stripped* -- exactly what ``manufacturer_data[555]`` gives you in
    both bleak and Home Assistant.

    Returns ``None`` if the payload is too short: under 3 bytes, or an awake
    frame without the measurement bytes.
    """
    if mfr_data is None or len(mfr_data) < MIN_SLEEP_PAYLOAD_LEN:
        return None

    data = bytes(mfr_data)
    status = data[2]
    raw = {"byte0": data[0], "byte1": data[1], "status": status}
    if len(data) >= MIN_PAYLOAD_LEN:
        raw["pressure"] = int.from_bytes(data[3:5], "little")
        raw["temperature"] = data[5]
        raw["battery_mv"] = int.from_bytes(data[6:8], "little")

    if status < STATUS_AWAKE_MIN:
        # Sleep frame: usually just the three bytes up to status, and any
        # measurement fields after it are not refreshed. Report the battery only
        # if it is there and plausible, and nothing else.
        volts = raw["battery_mv"] / 1000.0 if "battery_mv" in raw else None
        plausible = volts is not None and 1.5 <= volts <= 4.3
        return TeslaTpmsReading(
            awake=False,
            status=status,
            battery_volts=round(volts, 3) if plausible else None,
            battery_percent=battery_percentage(volts) if plausible else None,
            raw=raw,
            profile=profile.name,
        )

    if len(data) < MIN_PAYLOAD_LEN:
        return None

    raw_pressure = raw["pressure"]
    raw_temperature = raw["temperature"]
    raw_battery_mv = raw["battery_mv"]

    pressure_bar = (raw_pressure - profile.pressure_offset) / profile.pressure_divisor
    # A deflated tyre reads slightly negative because of sensor offset; clamp.
    if -0.2 < pressure_bar < 0.0:
        pressure_bar = 0.0

    if profile.celsius_native:
        temperature_c = raw_temperature - profile.temperature_offset
    else:
        temperature_f = raw_temperature - profile.temperature_offset
        temperature_c = (temperature_f - 32.0) * 5.0 / 9.0

    volts = raw_battery_mv / 1000.0

    return TeslaTpmsReading(
        awake=True,
        status=status,
        pressure_bar=round(pressure_bar, 3),
        temperature_c=round(temperature_c, 1),
        battery_volts=round(volts, 3),
        battery_percent=battery_percentage(volts),
        raw=raw,
        profile=profile.name,
    )


def decode_full_advertisement(
    payload: bytes | bytearray,
    profile: DecodeProfile = PROFILE_DEFAULT,
) -> TeslaTpmsReading | None:
    """Decode from a *complete* raw advertisement (AD structures included).

    Scans for the little-endian Tesla company ID ``2B 02`` inside a Manufacturer
    Specific Data element and decodes from there. Useful for offline analysis of
    captures taken with an ESP32, ``btmon`` or a sniffer, where you have the
    whole PDU rather than a parsed dict.
    """
    data = bytes(payload)
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        if length == 0 or i + 1 + length > n:
            break
        ad_type = data[i + 1]
        if ad_type == 0xFF and length >= 3:
            body = data[i + 2 : i + 1 + length]
            if len(body) >= 2 and int.from_bytes(body[0:2], "little") == TESLA_COMPANY_ID:
                return decode(body[2:], profile)
        i += 1 + length

    # Fallback: not a well-formed AD stream, just hunt for the marker.
    idx = data.find(b"\x2b\x02")
    if idx != -1:
        return decode(data[idx + 2 :], profile)
    return None
