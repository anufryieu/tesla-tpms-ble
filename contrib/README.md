# Upstream patch for `bkbilly/tpms_ble`

`tpms_ble-add-tesla-autel-type-i.patch` adds Tesla-protocol support to
[bkbilly/tpms_ble][tpms_ble] as "Type I", following the same shape as the
existing Type A–H parsers: a company-ID branch in `_start_update`, a
`_process_tpms_i` method, a matcher in `manifest.json`, and a test in
`tests/test_parser.py`.

It is the same decode as this repository's integration, minus the profile
switching, trim offsets, raw diagnostic entities and the connection path —
those do not fit the upstream design.

## Use it

Either to contribute upstream, or if you would rather run one TPMS integration
for a mixed set of sensors:

```bash
git clone https://github.com/bkbilly/tpms_ble.git
cd tpms_ble
git apply /path/to/contrib/tpms_ble-add-tesla-autel-type-i.patch
```

Then copy `custom_components/tpms_ble/` into your Home Assistant config and
restart. Do not run both this patched `tpms_ble` and `tesla_tpms_ble`: they
would both match company ID 555 and race to claim the same sensors.

## Verified

Generated against upstream `master` at commit `1998488` (2026-10-18), where the
letters up to H are already taken — F is HRTPMS, G and H are Ai TPMS and
TC.TPMS, which is why this is Type I. What was checked here:

- it applies cleanly to a fresh clone of `master`, and the patched parser and
  test file compile
- the patched parser's arithmetic was compared against this repository's
  `decoder.decode()` over 723 payloads — every status, pressure, temperature
  and battery combination that matters — with no divergence
- the test it adds covers an awake frame, the real `01 fe 03` sleep frame from
  a fitted Autel sensor, and a Tesla *vehicle* beacon that must not be turned
  into a tyre

The upstream suite itself was not run from this repository. If upstream moves
on again, regenerate rather than fixing the hunks by hand — this patch has gone
stale once already.

[tpms_ble]: https://github.com/bkbilly/tpms_ble
