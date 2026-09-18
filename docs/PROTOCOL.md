# Tesla-protocol BLE TPMS advertisement format

## Why your Autel sensors speak Tesla

The Autel MX-Sensor BLE (Tiptopol index 562-3560) is sold "pre-programmed" for
Tesla Model 3 / S / X / Y. A TPMS sensor is only useful if the car's TPMS ECU
accepts it, and that ECU implements exactly one protocol. So a sensor sold as
drop-in for a Tesla **must** emit byte-identical advertisements to the Tesla OEM
sensor. That is what makes this integration work: it is a Tesla protocol
decoder, and your Autel sensors are Tesla-protocol emitters.

This is also why `bkbilly/tpms_ble` cannot see them. That integration matches on
company IDs 256, 172, 295 and 2088 plus service UUID `0x27A5` — the families of
cheap generic Chinese, Michelin and FOBO sensors. Tesla-protocol sensors use a
different company ID entirely, so no matcher fires and no device is ever
discovered.

## Transport

BLE advertisements. Everything this integration reads is in the advertisement
itself, so nothing ever connects to the sensor, and a passive scanner or
Bluetooth proxy is enough. That is good news for Home Assistant: listening costs
the sensor nothing.

## Advertisement structure

The data lives in a standard Manufacturer Specific Data AD element
(AD type `0xFF`) using Bluetooth SIG company identifier **`0x022B`**, which the
SIG assigns to **Tesla, Inc.**

```
 AD len  AD type   company id      manufacturer data
 ┌────┐  ┌────┐   ┌───────────┐   ┌──────────────────────────────┐
   0B      FF       2B    02       01 02 0A 86 01 48 EA 0B
                    └── little-endian 0x022B
```

That example is synthetic. The first real capture, from a fitted Autel
MX-Sensor BLE through Home Assistant's Bluetooth diagnostics, is a sleep frame:

```
 02 01 06                 flags
 06 FF 2B 02 01 FE 03     manufacturer data, company 0x022B: 01 FE 03
```

The local name `tsTPMS ` (with a trailing space) and a 16-bit service UUID
`0x1122` are not in that advertisement. BlueZ reported them anyway, so they come
from the scan response. The four sensors' addresses all start `BC:6A:29`, a
Texas Instruments prefix.

The ESP32 sketch this work is based on scans raw advertisement bytes for the
literal marker `0x2B 0x02`. That is not a magic number — it is the Tesla company
ID stored little-endian, which is why the same offsets work when you index from
`manufacturer_data[555]` in bleak or Home Assistant, both of which strip the two
company-ID bytes for you.

## Payload layout

Offsets are relative to the start of the manufacturer data, i.e. **after** the
company ID has been stripped:

| Offset | Size | Field | Notes |
|-------:|-----:|-------|-------|
| 0 | 1 | unknown | constant-ish; exposed as a raw diagnostic |
| 1 | 1 | unknown | possibly a counter or part of the sensor ID |
| 2 | 1 | status | `< 0x05` ⇒ asleep, measurement fields are stale |
| 3 | 2 | pressure | uint16 **little-endian**, absolute kPa |
| 5 | 1 | temperature | uint8, °C + 50 |
| 6 | 2 | battery | uint16 little-endian, millivolts |

A reading needs all 8 bytes. A sleep frame is usually just the first 3.

### Sleep frames

When the wheel is not turning the sensor sets `status < 0x05` and stops sending
measurements. The fitted Autel sensors send just `01 FE 03` — the three bytes up
to status — roughly once a second. Any sensor that does send the measurement
bytes in a sleep frame does **not refresh** them. Decoding them anyway is the
single easiest way to get a convincing but wrong tyre pressure in your
dashboard, so this integration publishes nothing but the battery from a sleep
frame, and only when the frame carries one. The `Awake` binary sensor shows
what is happening.

Integration version 1.0.0 required 8 bytes from every frame, so it rejected
these three-byte sleep frames. The sensors were heard but never offered.

### Distinguishing a sensor from a car

Tesla *vehicles* also advertise under company ID `0x022B`. Matching on the
company ID alone will therefore try to turn a passing Model 3 into a tyre. The
integration applies a shape check. A sleep frame must be 3 to 16 bytes. An awake
frame must be at least 8 bytes, with the value at offset 6..7 within 1500–4300 mV,
a plausible range for the primary lithium cell in a TPMS sensor. See
`looks_like_tesla_tpms()` in `decoder.py`.

## Decoding formulas, and how they were derived

The upstream reverse-engineering effort ([cunzulatu/Tesla_BLE_TPMS][upstream])
published two empirical fits, checked against a professional TPMS reader, and
explicitly flagged them as approximate:

```
psi  = (raw_pressure - 100) / 7
degF = raw_temperature - 1
```

This integration defaults to a slightly different reading of the same data.

### Pressure: the divisor is 6.89476, not 7

The psi→kPa conversion factor is 6.89476. Substituting it for the fitted `7`
makes the formula collapse to:

```
kPa_gauge = raw_pressure - 100
```

That is a physically meaningful encoding rather than a fitted curve. A MEMS
pressure die measures **absolute** pressure; subtracting 100 kPa is subtracting
one atmosphere (1 atm = 101.325 kPa) to get gauge pressure. A 1 kPa quantum also
matches the 0.1 bar accuracy Autel specifies for this part.

The two readings differ by 1.5% — about 0.6 psi at a Tesla's 42 psi placard
pressure, which is comfortably inside the error bars of the original fit.

### Temperature: °C + 50, not °F − 1

`degC = raw - 50` is a far more common encoding in TPMS silicon. Two independent
arguments favour it:

1. **The two models intersect at room temperature.** Setting
   `raw - 1 = (raw - 50) × 9/5 + 32` gives `raw = 71.25`, i.e. 21.2 °C / 70.2 °F
   — precisely where a bench calibration would have been performed. The
   Fahrenheit fit is a local linearisation of the Celsius encoding around the
   one point where it was measured. The two diverge sharply away from it: at
   `raw = 130` they disagree by 30 °C.
2. **Only one covers the specified range.** Autel specs −40 to +105 °C for this
   sensor. `raw - 50` spans −50…+205 °C. Read as Fahrenheit, `raw - 1` bottoms
   out at −18 °C and cannot represent the spec floor at all.

### If you disagree with either choice

Both are switchable. The integration ships two profiles — `default` (the above)
and `legacy` (a bit-for-bit reproduction of the published sketch) — selectable
from the options flow, plus additive trim offsets. The untouched wire values are
exposed as `Raw pressure` and `Raw temperature` diagnostic entities so you can
fit your own curve. See [CALIBRATION.md](CALIBRATION.md).

## Open questions

- What an awake frame from an Autel sensor looks like. Only sleep frames have
  been captured so far. The awake layout above still rests on the upstream
  sketch alone.
- Bytes 0 and 1 are not understood. In sleep frames they were `01 FE` on all
  four sensors, so they are not a sensor ID. They are exposed as raw
  diagnostics; if you see them vary in an interesting way, that is worth writing
  down.
- The full set of `status` values is unknown. Only the `< 0x05` sleep threshold
  is established. There is likely a fast-leak or over-pressure alarm bit in
  there, which would be worth surfacing as a binary sensor.
- Whether the Autel sensors carry any Autel-specific identifier in the payload
  is untested — that is what the `Raw` diagnostics are for.

[upstream]: https://github.com/cunzulatu/Tesla_BLE_TPMS
