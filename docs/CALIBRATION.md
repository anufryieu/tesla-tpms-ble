# Verifying and calibrating the decode

The pressure and temperature formulas come from reverse engineering, not from a
datasheet. They are good to roughly 1.5% on pressure, and the temperature
encoding is inferred rather than measured. This page is how you check them
against your own sensors, and correct them if needed.

You do not have to do any of this. The defaults are reasonable and the
integration works out of the box. Do it if you care about the last few
hundredths of a bar, or if a reading looks obviously wrong.

## 1. Check pressure against a reference gauge

The cleanest check needs a decent tyre gauge and five minutes.

1. Enable the raw diagnostic entities. In Home Assistant go to the TPMS device →
   the `Raw pressure` entity → cog icon → **Enabled**. Do the same for
   `Raw temperature`. They are diagnostic entities, disabled by default so they
   do not clutter a normal install.
2. Get the sensor awake and transmitting (see the README's *Waking the sensors*).
3. Note `Raw pressure` and, at the same moment, the pressure your gauge reads.
4. Repeat at two or three different pressures — for example 2.0, 2.5 and 2.9 bar.
   Let the air settle for a few seconds after each change.

Now check the model. The default decode says:

```
pressure_bar = (raw_pressure - 100) / 100
```

so `raw_pressure` should equal `gauge_bar × 100 + 100`. At 2.90 bar you expect a
raw value near 390; at 2.00 bar, near 300.

| If you observe | It means | Do this |
|---|---|---|
| raw ≈ bar × 100 + 100 | the default profile is right | nothing |
| raw ≈ bar × 101.5 + 100 | the `legacy` divisor is right | switch profile to `legacy` |
| a constant offset, correct slope | sensor offset | set **Pressure correction** |
| something else entirely | new variant | please open an issue with the numbers |

Two points are enough to separate slope from offset. Plot them if you like:
slope tells you the divisor, intercept tells you the offset.

## 2. Check temperature

Harder to do well, because you need the sensor itself at a known temperature,
not the air near it. The sensor is inside the tyre, bolted to the rim.

The practical version: leave the car parked overnight, then compare
`Raw temperature` against outdoor air temperature first thing in the morning,
before the sun hits the wheels. Cold-soaked overnight, the rim is close to
ambient.

The default decode says:

```
temperature_c = raw_temperature - 50
```

so an overnight-soaked sensor at 8 °C should report a raw value near 58.

Do not calibrate against a tyre that has just been driven. Tyre temperature runs
well above ambient after even a short drive, and the whole point of a TPMS
temperature reading is that it tracks the tyre, not the weather.

## 3. Apply a correction

Two knobs, both in **Settings → Devices & Services → Tesla / Autel BLE TPMS →
Configure**, per sensor:

- **Decoding profile** — `default` or `legacy`. Changes the slope. See
  [PROTOCOL.md](PROTOCOL.md) for what each one assumes.
- **Pressure correction (bar)** and **Temperature correction (°C)** — additive
  trims applied after decoding. Changes the offset. Use these for a sensor that
  reads consistently high or low.

Changes take effect on the next advertisement; there is no need to reload the
integration or restart Home Assistant.

Set the trims per sensor, not globally — a 0.05 bar bias on one sensor is a
property of that sensor, and copying it to the other three will make them worse.

## 4. Collect data offline

To gather a batch of readings for analysis rather than eyeballing the UI:

```bash
python3 tools/tpms_scan.py --raw --log capture.jsonl --duration 1800
```

Each line is a JSON record with the raw fields, the decoded values and a
timestamp. Note the reference gauge pressure and the wall-clock time when you
take it, then match up against the log afterwards.

To compare both profiles on a single payload:

```bash
python3 tools/decode_hex.py 01020A860148EA0B --compare
```

```
  default     2.90 bar ( 42.1 psi /   290 kPa)    22.0 °C  3.05 V (97%)
  legacy      2.86 bar ( 41.4 psi /   286 kPa)    21.7 °C  3.05 V (97%)
```

## 5. If you find the real formula

If you establish the encoding properly — especially the temperature, or the
meaning of payload bytes 0 and 1 — that is worth contributing back to
[cunzulatu/Tesla_BLE_TPMS](https://github.com/cunzulatu/Tesla_BLE_TPMS), which
is where the original reverse engineering was done and which explicitly asks for
improvements. Adding a profile here is a one-line change in `decoder.py`:

```python
PROFILE_MINE = DecodeProfile(
    name="mine",
    pressure_offset=100.0,
    pressure_divisor=100.0,
    celsius_native=True,
    temperature_offset=50.0,
)
```

then add it to the `PROFILES` dict and it appears in the options flow.
