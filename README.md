# Tesla / Autel BLE TPMS for Home Assistant

A Home Assistant custom integration for tyre pressure sensors that speak the
**Tesla BLE TPMS protocol** — including the **Autel MX-Sensor BLE**, the Tesla
OEM sensor, and the various aftermarket clones sold as "BLE TPMS for Tesla".

Pressure, temperature, battery and signal strength, over passive Bluetooth. No
cloud, no pairing, no ESP32 required (though one helps if your HA box is far
from the car).

---

## Why your sensors did not work with `tpms_ble`

[`bkbilly/tpms_ble`][tpms_ble] is the usual answer for BLE TPMS in Home
Assistant, and it is a good integration — it just cannot see these sensors, and
no amount of configuration will make it.

It matches sensors on Bluetooth company IDs `256`, `172`, `295` and `2088`, plus
service UUID `0x27A5`. Those cover the generic Chinese, Michelin and FOBO sensor
families.

Autel MX-Sensor BLE units are sold **pre-programmed for Tesla**. A TPMS sensor is
only useful if the car's TPMS ECU accepts it, and that ECU implements exactly one
protocol — so a sensor sold as drop-in for a Tesla must emit byte-identical
advertisements to the Tesla OEM part. Those advertisements use Bluetooth SIG
company ID **`0x022B`**, assigned to *Tesla, Inc.*

`tpms_ble` has no matcher for `0x022B`, so nothing is ever discovered. Your
sensors were transmitting fine the whole time; nothing was listening for them.

This integration decodes that protocol. Full details in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## Supported hardware

| Sensor | Status |
|---|---|
| Autel MX-Sensor BLE (`562-3560`, Tesla pre-programmed) | ✅ target of this work |
| Tesla OEM BLE TPMS (`1490701-01-B` / `-01-C`) | ✅ same protocol |
| Aftermarket "BLE TPMS for Tesla" clones | ✅ must be, to work in the car |
| Any 433 MHz TPMS sensor | ❌ not Bluetooth at all |
| Generic Chinese / Michelin / FOBO BLE TPMS | ➡️ use [`tpms_ble`][tpms_ble] |

You do **not** need a Tesla. These are just BLE sensors; the car is irrelevant to
Home Assistant. Fitted to any wheel, they will report.

---

## Before you install: wake the sensors

**This is the part that wastes people's afternoons.** A TPMS sensor that is not
fitted, not pressurised and not moving transmits almost nothing — that is how it
gets a 6+ year battery life. Sitting in a box on your desk, a sensor may emit
nothing at all for hours, and when it does emit it will be a sleep frame with no
pressure in it.

Nothing will be discovered until a sensor is awake. In rough order of ease:

1. **Fit them and drive.** Above roughly 25 km/h they transmit every few seconds.
   This is the real answer, and the only one that gives useful data anyway.
2. **Spin or shake one hard.** The wake-up is an accelerometer. A firm shake or
   spinning it on a table will often trip it for a short burst.
3. **Pressurise it.** Some sensors wake on a pressure change. Fitting to a wheel
   and inflating is usually enough.
4. **Use a TPMS activation tool.** Any 125 kHz LF activator — the Autel MX-Sensor
   range is designed for exactly this and your tyre shop has one.
5. **Just wait.** Even parked, sensors emit occasional keep-alive frames. Leave a
   scan running 15–30 minutes.

Verify with the bundled scanner *before* touching Home Assistant:

```bash
python3 -m venv .venv && .venv/bin/pip install bleak
.venv/bin/python tools/tpms_scan.py
```

```
>>> new sensor: E1:4C:2A:88:19:F3
14:22:07 E1:4C:2A:88:19:F3 (tsTPMS) -71 dBm
  2.90 bar (42.1 psi / 290 kPa) | 22.0 °C | batt 3.05 V (97%) | status 0x0A
```

If that prints nothing, the problem is the sensors being asleep or out of range —
not Home Assistant, and not this integration. Run `tools/tpms_scan.py --all` to
confirm your adapter sees *any* BLE traffic.

> On macOS you must grant Bluetooth permission to your terminal app
> (System Settings → Privacy & Security → Bluetooth), otherwise the process is
> killed with `SIGABRT` and no error message. Linux needs no special setup.

---

## Install

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories**
2. Repository `https://github.com/anufryieu/tesla-tpms-ble`, category **Integration**
3. Install **Tesla / Autel BLE TPMS**, then restart Home Assistant

### Manual

Copy `custom_components/tesla_tpms_ble/` into your Home Assistant
`config/custom_components/` directory and restart.

**[docs/INSTALL.md](docs/INSTALL.md) has the step-by-step**, including Samba,
Studio Code Server, SSH and Docker, plus troubleshooting.

### Then

With Bluetooth working and at least one sensor awake, Home Assistant discovers
each sensor on its own and offers it under **Settings → Devices & Services**.
Four sensors appear as four separate devices — name them `Front left`,
`Front right` and so on as you add them.

If nothing appears, re-read *Wake the sensors* above. Discovery cannot happen
until a sensor actually transmits.

## Entities

Per sensor:

| Entity | Category | Notes |
|---|---|---|
| Pressure | — | bar; HA converts to your preferred unit |
| Temperature | — | °C |
| Battery | diagnostic | % from a lithium discharge curve; coarse by nature |
| Voltage | diagnostic | cell volts, the honest version of the above |
| Awake | diagnostic | false while the sensor is in its low duty-cycle mode |
| Signal strength | diagnostic | disabled by default |
| Raw pressure / Raw temperature / Status | diagnostic | disabled by default; for [calibration](docs/CALIBRATION.md) |

Entities stay available when a sensor goes quiet, and are marked
`assumed_state`. This is deliberate — a parked car stops transmitting, and you
want yesterday's pressure rather than `unavailable` on your dashboard.

While a sensor is asleep it publishes **no** pressure or temperature. Sleep
frames do not refresh those bytes, so decoding them would give you a confident,
stale, wrong number.

---

## If the HA box is too far from the car

Bluetooth is short range and cars live outside. Flash any ESP32 as a Bluetooth
proxy and put it near where you park:

```bash
esphome run esphome/bluetooth-proxy.yaml
```

Home Assistant adopts it automatically and this integration keeps doing the
decoding, so you keep the profile switching and trim options.

There is also [`esphome/tesla-tpms.yaml`](esphome/tesla-tpms.yaml), a standalone
ESPHome config that decodes on the ESP32 itself and needs no custom integration
at all. Put your four MAC addresses in the substitutions block. Use it if you
would rather not run custom components — but the proxy is the better default.

## Tools

| Command | Purpose |
|---|---|
| `tools/tpms_scan.py` | live scan and decode; `--raw`, `--all`, `--log`, `--profile` |
| `tools/decode_hex.py` | decode a payload captured elsewhere; `--compare` shows both profiles |

`decode_hex.py` accepts either bare manufacturer data or a whole advertisement,
and works out which it is:

```bash
python3 tools/decode_hex.py 0201060BFF2B0201020A860148EA0B --compare
```

Useful for hex from the nRF Connect app, HA's Bluetooth diagnostics download, or
`btmon` on Linux.

## Accuracy

The decode is reverse engineered, not documented. Pressure is good to about
1.5%; the temperature encoding is inferred from indirect evidence rather than
measured. [docs/PROTOCOL.md](docs/PROTOCOL.md) sets out the reasoning and where
it is uncertain, and [docs/CALIBRATION.md](docs/CALIBRATION.md) shows how to
check it against a reference gauge and correct it if needed — there are two
decoding profiles and per-sensor trim offsets in the options flow.

If you establish better constants, please contribute them upstream to
[cunzulatu/Tesla_BLE_TPMS][upstream], which is where the original protocol work
was done.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

The decoder in `custom_components/tesla_tpms_ble/decoder.py` has no Home
Assistant or bleak imports, so the tests run anywhere.

## Credits

The protocol work is [cunzulatu/Tesla_BLE_TPMS][upstream] (MIT) — payload
offsets, the sleep-status threshold and the original empirical formulas all come
from there. This repository contributes the identification of `0x2B 0x02` as the
Tesla company ID, the kPa/Celsius reinterpretation of the constants, and the
Home Assistant and ESPHome integrations.

Integration structure follows [`bkbilly/tpms_ble`][tpms_ble].

[tpms_ble]: https://github.com/bkbilly/tpms_ble
[upstream]: https://github.com/cunzulatu/Tesla_BLE_TPMS
