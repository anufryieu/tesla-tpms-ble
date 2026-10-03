# Installing into Home Assistant

The integration is 13 files in one folder. Installing it means getting that
folder to `<config>/custom_components/tesla_tpms_ble/` on your HA machine and
restarting. Everything below is just different ways of moving a folder.

`<config>` is wherever your `configuration.yaml` lives — `/config` on HA OS and
in the container images, `~/.homeassistant` for a `pip` install.

---

## Step 0 — check Bluetooth works first

Do this before installing anything. If HA has no Bluetooth, the integration
cannot possibly find your sensors, and you will spend the evening debugging the
wrong layer.

**Settings → Devices & Services** should show a **Bluetooth** integration. If it
does not, click **+ Add integration → Bluetooth**. HA needs either:

- a Bluetooth adapter on the HA host (built into a Raspberry Pi, or a USB
  dongle), or
- an ESPHome Bluetooth proxy — see [`esphome/bluetooth-proxy.yaml`](../esphome/bluetooth-proxy.yaml).

Range matters more than you would think. Bluetooth is a few metres through
walls, and cars are usually parked outside. If HA lives in a cupboard on the
other side of the house, a proxy near the driveway is not optional.

---

## Step 1 — copy the folder

Pick whichever matches how you access your HA box.

### If you have the Samba add-on

Easiest on a Mac.

1. In Finder: **Go → Connect to Server** (`⌘K`), enter `smb://homeassistant.local`
   (or your HA box's IP), connect, and mount the `config` share.
2. Open the mounted share. If there is no `custom_components` folder, create one.
3. Copy `custom_components/tesla_tpms_ble/` from this repo into it.

You should end up with `config/custom_components/tesla_tpms_ble/manifest.json`.

### If you have the Studio Code Server or File Editor add-on

1. Open the add-on.
2. Create `custom_components/tesla_tpms_ble/` if it does not exist.
3. Upload the 13 files, keeping `translations/en.json` in its subfolder.

Studio Code Server can drag-and-drop a whole folder into its file tree, which is
much less tedious than uploading one file at a time.

### If you have SSH

From this repo's directory:

```bash
scp -r custom_components/tesla_tpms_ble \
    root@homeassistant.local:/config/custom_components/
```

Port 22222 and user `root` for the *Advanced SSH & Web Terminal* add-on; port 22
and whatever user you configured for a supervised or container install. Adjust
the hostname if `homeassistant.local` does not resolve for you — use the IP.

### If you run HA in Docker

Copy into the directory you bind-mounted as `/config`:

```bash
cp -r custom_components/tesla_tpms_ble /path/to/ha-config/custom_components/
```

### Using a downloaded zip

GitHub's green **Code → Download ZIP** gives you an archive containing
`custom_components/tesla_tpms_ble/…` at the right relative path. Unzip it and
copy that one folder across.

---

## Step 2 — restart Home Assistant

**Settings → System → top-right power icon → Restart Home Assistant.**

A config reload is not enough. Custom integrations are only picked up on a full
restart.

---

## Step 3 — wake a sensor

**Nothing will appear until a sensor actually transmits.** These sleep when the
wheel is not turning — that is how they last six years on one cell.

Fit them and drive above ~25 km/h, or shake one hard to trip its accelerometer,
or use a 125 kHz TPMS activation tool. See *Wake the sensors* in the
[README](../README.md).

Sanity-check from your laptop first, so you know whether you are debugging the
sensors or Home Assistant:

```bash
.venv/bin/python tools/tpms_scan.py
```

If that shows nothing, HA will show nothing either, and the problem is not the
integration.

---

## Step 4 — add the devices

With a sensor awake and in range, HA discovers each one and shows it under
**Settings → Devices & Services** as a discovered device. Click **Configure** on
each and confirm.

Four sensors appear as four separate devices. Name them as you add them —
`Front left`, `Front right`, `Rear left`, `Rear right` — because a bare MAC
address tells you nothing at 07:00 when a tyre is flat.

If discovery does not fire, **+ Add integration → Tesla / Autel BLE TPMS** lets
you pick from sensors already seen.

---

## Troubleshooting

**"Integration not found" / nothing in the add-integration list**
The folder is in the wrong place or you did not do a full restart. Check
`<config>/custom_components/tesla_tpms_ble/manifest.json` exists, then restart
again. A nested folder (`tesla_tpms_ble/tesla_tpms_ble/`) is the usual cause.

**No devices discovered**
First find out whether HA hears the sensors at all. Straight after a drive,
with the car parked, open the Bluetooth advertisement monitor —
[my.home-assistant.io/redirect/bluetooth_advertisement_monitor](https://my.home-assistant.io/redirect/bluetooth_advertisement_monitor/)
— and look for a device named `tsTPMS`, or with manufacturer ID `555`
(`0x022B`).

- **Not listed** — HA never heard them. Either the sensors had gone quiet by
  the time the car was in range, or the car is simply out of range: a sensor
  inside a tyre inside a metal wheel is weak, and a car on the drive is rarely
  within reach of an adapter indoors. Put a proxy where you park, and confirm
  with `tools/tpms_scan.py` from a laptop next to the wheel.
- **Listed, but nothing discovered** — update to 1.0.1 or later and restart.
  Version 1.0.0 rejected the three-byte frames a sleeping sensor sends, and
  never auto-discovered through a passive scanner (an ESPHome proxy without
  active connections, a Shelly). If a sensor is still not offered after that,
  add it by hand while it is listed: **+ Add integration → Tesla / Autel BLE
  TPMS**. That list only holds sensors heard in the last few minutes.
- **Listed, but the add-integration list says nothing was found** — the
  advertisement is not the shape this integration expects. Open an issue with
  the manufacturer data shown in the monitor.

**Discovered, but pressure and temperature are missing**
The sensor is asleep. Look at the `Awake` diagnostic entity. Sleep frames do not
refresh those bytes, so the integration deliberately publishes nothing rather
than a stale reading.

To find out whether it is *ever* awake, download this integration's diagnostics:
the device page → **⋮ → Download diagnostics**. Under `payload_history` you get
every distinct payload the sensor has sent since Home Assistant started, with
counts and timestamps:

```json
"max_payload_len": 8,
"saw_a_full_length_frame": true,
"payloads": [
  { "hex": "01fe03",           "count": 4,   "note": "asleep" },
  { "hex": "01020a860148ea0b", "count": 1,   "note": "awake"  }
]
```

This is the download to take **after a drive**. Home Assistant's own Bluetooth
diagnostics keep only the *latest* advertisement per device, so they always show
a sleep frame once you have parked — a three-second awake burst on the road is
long gone. The payload history keeps it.

- `saw_a_full_length_frame: false` — the sensor never sent a reading while HA
  was listening. Either it never woke, or it does not put readings in its
  advertisement at all.
- a full-length frame present but `"accepted": false` — it reached us and the
  integration rejected it. That is a bug worth an issue; include the `hex`.

The counts are small, and that is expected. Home Assistant only forwards an
advertisement when it differs from the previous one from that device, or when
the device comes back after being out of range long enough to be forgotten. A
parked sensor repeating `01 fe 03` every second is forwarded once. So a `count`
of 4 means "reappeared four times", not "four frames on air".

`timeline` lists the last 64 forwarded advertisements in order, with
`gap_seconds` since the previous one and the adapter or proxy that heard it
(`source`). A drive shows up as a long gap followed by whatever the sensor sent
when the car came back into range. Compare those times with when you actually
left and arrived: if the first thing heard on arrival is already `01fe03`, the
sensor was not advertising a reading even while it was still rolling.

`other_addresses` is shared by all sensors. It keeps every advertisement with
the Tesla company ID, or from the `BC:6A:29` address block, from an address that
is *not* set up as a sensor. If a sensor sends its reading from a different
address while it is awake, that is the only place it will appear.

**Pressure is missing because the sensor never advertises a reading**
Some sensors — the fitted Autel units tested so far among them — only ever
broadcast the sleep frame, even on the road, and hand pressure out only over a
connection, the way a car reads them. For those, enable **Read over a
connection** under **Configure** on the device. Home Assistant then connects
periodically and asks the sensor for pressure and temperature (a `TPDataRequest`
on the Tesla service), exactly what a car does on the normal read path — it does
not enrol, bond, or change the sensor.

This is **experimental and off by default**:

- It needs a *connectable* path to the sensor: a local Bluetooth adapter, or an
  ESPHome proxy with active connections enabled. A passive-only proxy cannot
  open a connection, and the poll will do nothing.
- The exact request framing is not yet confirmed, so the integration tries a few
  documented encodings on the first poll and remembers whichever the sensor
  answers. `connection.learned_request` in the diagnostics shows the winner (and
  `last_poll_successful` whether the most recent poll worked). If no framing ever
  works, find one with `tools/tpms_gatt.py --request` and nothing passive will
  help.
- Pressure from a connection is decoded as whole kPa — a different encoding from
  the advertisement — so trim it against a gauge if needed.
- Connecting costs the sensor a little battery, so it polls at most every few
  minutes, only when the sensor is in range.

**Readings look wrong**
See [CALIBRATION.md](CALIBRATION.md). There are two decoding profiles and
per-sensor trim offsets under **Configure** on each device. Do not reinstall —
this is a calibration issue, not an installation one.

**Turn on debug logging**

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.tesla_tpms_ble: debug
```

This logs every Tesla-company-ID advertisement seen, including ones rejected as
not TPMS-shaped — which tells you whether the sensor is being heard at all.

---

## Installing via HACS instead

HACS handles the copy and the updates for you, which is why it is worth the two
extra clicks.

1. **HACS → ⋮ (top right) → Custom repositories**
2. Repository: `https://github.com/anufryieu/tesla-tpms-ble` — Type: **Integration** — **Add**
3. Find **Tesla / Autel BLE TPMS** in the HACS list and **Download**
4. Restart Home Assistant

Then carry on from *Step 3 — wake a sensor* above. HACS will notify you when a
new release is published, which manual copying will not.
