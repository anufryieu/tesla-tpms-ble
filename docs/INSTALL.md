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
Almost always a sleeping sensor. Confirm with `tools/tpms_scan.py` from a laptop
next to the wheel. If the laptop sees it and HA does not, it is range — add a
proxy.

**Discovered, but pressure and temperature are missing**
The sensor is asleep. Look at the `Awake` diagnostic entity. Sleep frames do not
refresh those bytes, so the integration deliberately publishes nothing rather
than a stale reading.

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
