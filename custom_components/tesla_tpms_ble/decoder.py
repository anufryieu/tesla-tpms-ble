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


# ---------------------------------------------------------------------------
# Connection path: TPDataRequest / TPData over the Tesla VCSEC GATT service.
#
# A car does not read pressure from the advertisement. It opens a GATT
# connection to the sensor, writes a ``TPDataRequest`` to characteristic 0212,
# and the sensor answers with a ``TPData`` message as an indication on 0213:
#
#     enum TPDataRequest_E { ... TP_DATAREQUEST_PRESSURE_TEMPERATURE = 1; ... }
#     message TPData       { int32 pressure = 1; sint32 temperature = 2; }
#
# Real replies from fitted sensors are ``TPData`` wrapped in two outer
# messages (``field 2 -> field 28 -> {pressure, temperature}``); the decoder
# searches in for the innermost message rather than assuming a flat layout.
# ``TPData.pressure`` is a different encoding from the advertisement -- raw
# units of about 0.08 psi, not the advertisement's ``raw - 100`` kPa -- so it
# is decoded with its own scale rather than through a :class:`DecodeProfile`.
#
# The exact VCSEC framing around the request is not public, so several
# documented candidate encodings are offered; a caller tries them in turn.
# ---------------------------------------------------------------------------

#: Inner TPDataRequest asking for pressure + temperature: field 1 (varint) = 1.
_TPDATA_REQUEST_INNER: Final[bytes] = bytes([0x08, 0x01])


def build_tpdata_requests() -> list[bytes]:
    """Return candidate TPDataRequest frames to write to 0212, simplest first.

    The inner message is almost certainly a single enum field set to 1
    (``08 01`` on the wire). What wraps it is unknown, so this offers it bare,
    nested as an outer length-delimited field 1/2/3, and each of those again
    behind a 2-byte big-endian length prefix. None enrol, bond, or carry a
    certificate; an unrecognised frame is simply ignored by the sensor.
    """
    inner = _TPDATA_REQUEST_INNER
    frames = [inner]
    for field_tag in (0x0A, 0x12, 0x1A):  # outer field 1 / 2 / 3, wiretype 2
        frames.append(bytes([field_tag, len(inner)]) + inner)
    return frames + [len(f).to_bytes(2, "big") + f for f in list(frames)]


def _read_varint(data: bytes, i: int) -> tuple[int, int] | None:
    """Read a base-128 varint at ``data[i]``; return (value, next_index)."""
    shift = 0
    value = 0
    while i < len(data):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:  # runaway: not a real varint
            return None
    return None


def _collect_protobuf(data: bytes) -> tuple[dict[int, int], list[bytes]]:
    """Walk one protobuf message: return its varint fields and sub-messages.

    Returns ``({field_number: varint_value}, [length_delimited_payloads])``.
    Unknown wire types abort the walk -- a half-parsed message is not trusted.
    """
    varints: dict[int, int] = {}
    submessages: list[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        key = _read_varint(data, i)
        if key is None:
            break
        tag, i = key
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            got = _read_varint(data, i)
            if got is None:
                break
            varints[field_number], i = got
        elif wire_type == 2:  # length-delimited
            got = _read_varint(data, i)
            if got is None:
                break
            length, i = got
            if i + length > n:
                break
            submessages.append(data[i : i + length])
            i += length
        elif wire_type == 5:  # 32-bit
            i += 4
        elif wire_type == 1:  # 64-bit
            i += 8
        else:  # groups / unknown -- give up rather than guess
            break
    return varints, submessages


def _unzigzag(value: int) -> int:
    """Decode a protobuf ``sint32`` zigzag varint to a signed int."""
    return (value >> 1) ^ -(value & 1)


def _find_tpdata_fields(data: bytes, depth: int = 0) -> tuple[int, int | None] | None:
    """Find the innermost ``{pressure, temperature}`` message, recursing in.

    A real reply from a fitted sensor is ``TPData`` wrapped in two outer
    messages: ``field 2 -> field 28 -> {field 1 = pressure, field 2 = temp}``.
    Rather than hard-code that nesting, search for the first message carrying a
    varint field 1 (pressure), descending through length-delimited fields.
    Returns ``(pressure_raw, temperature_raw_or_None)`` or ``None``.
    """
    if depth > 5:  # a sane bound; real nesting is two deep
        return None
    varints, submessages = _collect_protobuf(data)
    if 1 in varints:
        return varints[1], varints.get(2)
    for sub in submessages:
        found = _find_tpdata_fields(sub, depth + 1)
        if found is not None:
            return found
    return None


# TPData pressure scale. Four fitted sensors parked and cold reported raw
# pressures of 523-542 with the nesting above; raw * 0.08 psi lands them at
# 41.8-43.4 psi, i.e. right on a Model 3 / Y's 42 psi (2.9 bar) placard, and
# the paired temperatures decoded to a plausible 18-19 C. 0.08 psi == this
# many kPa per unit. Provisional until confirmed against a gauge; the raw
# value is always exposed so the scale can be corrected without guessing.
_GATT_KPA_PER_UNIT: Final[float] = 0.08 * _KPA_PER_PSI


def decode_tpdata(payload: bytes | bytearray) -> TeslaTpmsReading | None:
    """Decode a ``TPData`` indication received over the GATT connection.

    ``payload`` is the raw bytes of one indication on characteristic 0213,
    possibly wrapped in one or more outer messages. ``TPData`` carries pressure
    as ``int32`` field 1 and temperature as ``sint32`` (zigzag) field 2 in
    whole degrees C -- a different encoding from the advertisement. Returns
    ``None`` if no plausible pressure field is found.
    """
    found = _find_tpdata_fields(bytes(payload))
    if found is None:
        return None
    pressure_raw, temperature_raw = found
    # Guard against a mis-parse turning random bytes into a "reading".
    if not 0 <= pressure_raw <= 100_000:
        return None

    raw: dict[str, int] = {"gatt_pressure": pressure_raw}
    temperature_c: float | None = None
    if temperature_raw is not None:
        temperature_c = float(_unzigzag(temperature_raw))
        raw["gatt_temperature"] = temperature_raw

    return TeslaTpmsReading(
        awake=True,
        status=STATUS_AWAKE_MIN,
        pressure_bar=round(pressure_raw * _GATT_KPA_PER_UNIT / 100.0, 3),
        temperature_c=temperature_c,
        raw=raw,
        profile="connection",
    )
