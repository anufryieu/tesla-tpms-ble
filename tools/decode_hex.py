#!/usr/bin/env python3
"""Decode Tesla-protocol TPMS payloads captured elsewhere -- offline.

Useful when you cannot run a live scan on the machine you are sitting at, but
you can get hex out of something else: the nRF Connect phone app, Home
Assistant's Bluetooth diagnostics download, `btmon`/`hcidump` on Linux, or the
ESP32 sketch's `debug on` output.

Accepts either form and figures out which it is:

  * manufacturer data with the company ID already stripped
        python3 tools/decode_hex.py 01020A8601 48EA0B
  * a whole advertisement PDU, company ID and AD headers included
        python3 tools/decode_hex.py 0201060BFF2B02 01020A860148EA0B 0709747354504D53

Separators (spaces, colons, dashes, 0x) are ignored. Pass several payloads to
decode them all; pass --compare to see every profile side by side, which is what
you want when checking the formulas against a reference gauge.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "tesla_tpms_ble")
)

from decoder import (  # noqa: E402
    PROFILES,
    TESLA_COMPANY_ID,
    decode,
    decode_full_advertisement,
)


def parse_hex(text: str) -> bytes:
    cleaned = re.sub(r"(?i)0x|[\s:,\-]", "", text)
    if len(cleaned) % 2:
        raise ValueError(f"odd number of hex digits in {text!r}")
    if not re.fullmatch(r"[0-9a-fA-F]*", cleaned):
        raise ValueError(f"not hex: {text!r}")
    return bytes.fromhex(cleaned)


def show(reading, label: str) -> None:
    if reading is None:
        print(f"  {label:<9} could not decode")
        return
    if not reading.awake:
        batt = (
            f"{reading.battery_volts:.2f} V"
            if reading.battery_volts is not None
            else "n/a"
        )
        print(
            f"  {label:<9} ASLEEP (status=0x{reading.status:02X}) — no measurement; "
            f"batt {batt}"
        )
        return
    print(
        f"  {label:<9} {reading.pressure_bar:6.2f} bar "
        f"({reading.pressure_psi:5.1f} psi / {reading.pressure_kpa:5.0f} kPa)  "
        f"{reading.temperature_c:6.1f} °C  "
        f"{reading.battery_volts:.2f} V ({reading.battery_percent}%)"
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Decode Tesla-protocol TPMS payloads from hex.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("hex", nargs="+", help="hex payload(s); separators ignored")
    p.add_argument("--compare", action="store_true",
                   help="decode with every profile side by side")
    p.add_argument("--profile", choices=sorted(PROFILES), default="default")
    args = p.parse_args()

    blob = parse_hex(" ".join(args.hex))
    if not blob:
        print("empty payload", file=sys.stderr)
        return 2

    print(f"input   {blob.hex(' ')}  ({len(blob)} bytes)")

    marker = TESLA_COMPANY_ID.to_bytes(2, "little")
    if marker in blob:
        idx = blob.index(marker)
        print(f"found   Tesla company id 0x{TESLA_COMPANY_ID:04X} at offset {idx}")
        reading_fn = decode_full_advertisement
        payload_desc = "full advertisement"
    else:
        print("assume  manufacturer data with company id already stripped")
        reading_fn = decode
        payload_desc = "manufacturer data"
    print(f"mode    {payload_desc}\n")

    profiles = sorted(PROFILES) if args.compare else [args.profile]
    any_ok = False
    for name in profiles:
        r = reading_fn(blob, PROFILES[name])
        if r is not None:
            any_ok = True
        show(r, name)

    if any_ok:
        r = reading_fn(blob, PROFILES[profiles[0]])
        raw = r.raw
        line = (
            f"\nraw     byte0=0x{raw['byte0']:02X} byte1=0x{raw['byte1']:02X} "
            f"status={raw['status']} (0x{raw['status']:02X})"
        )
        # A sleep frame usually ends at the status byte.
        if "pressure" in raw:
            line += (
                f" pressure={raw['pressure']} temp={raw['temperature']} "
                f"batt_mv={raw['battery_mv']}"
            )
        print(line)
        return 0

    print(
        "\nNothing decodable. A TPMS frame needs at least 3 bytes of "
        "manufacturer data after the company ID, and 8 to carry a reading.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
