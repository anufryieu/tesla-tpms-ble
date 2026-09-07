# Upstream patch for `bkbilly/tpms_ble`

`tpms_ble-add-tesla-autel-typef.patch` adds Tesla-protocol support to
[bkbilly/tpms_ble][tpms_ble] as "Type F", following the same shape as the
existing Type A–E parsers: a company-ID branch in `_start_update`, a
`_process_tpms_f` method, and a matcher in `manifest.json`.

It is the same decode as this repository's integration, minus the profile
switching, trim offsets and raw diagnostic entities — those do not fit the
upstream design.

## Use it

Either to contribute upstream, or if you would rather run one TPMS integration
for a mixed set of sensors:

```bash
git clone https://github.com/bkbilly/tpms_ble.git
cd tpms_ble
git apply /path/to/contrib/tpms_ble-add-tesla-autel-typef.patch
```

Then copy `custom_components/tpms_ble/` into your Home Assistant config and
restart. Do not run both this patched `tpms_ble` and `tesla_tpms_ble`: they
would both match company ID 555 and race to claim the same sensors.

## Verified

The patch applies cleanly to `main` as of the clone date, and the patched parser
was exercised against a synthetic advertisement through the real
`bluetooth_sensor_state_data` pipeline:

```
awake  -> {'pressure': 2.9, 'temperature': 22, 'battery': 97, 'voltage': 3.05, ...}
asleep -> {'battery': 97, 'voltage': 3.05, ...}          # no stale pressure
vehicle rejected -> True                                  # a Tesla car is not a tyre
```

[tpms_ble]: https://github.com/bkbilly/tpms_ble
