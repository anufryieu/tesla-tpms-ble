#!/usr/bin/env python3
"""Scan for Tesla-protocol BLE TPMS sensors and decode them live.

Run this on any machine with a Bluetooth adapter (your laptop, a Raspberry Pi,
the Home Assistant host) to confirm your sensors are transmitting and that the
decoding is right, before or instead of installing the HA integration.

    python3 tools/tpms_scan.py                 # decode Tesla TPMS frames
    python3 tools/tpms_scan.py --all           # show every BLE advert seen
    python3 tools/tpms_scan.py --raw           # include raw payload hex
    python3 tools/tpms_scan.py --profile legacy
    python3 tools/tpms_scan.py --log caps.jsonl --duration 900

Every decoded frame can be appended to a JSONL file with --log; that file is
what you want when calibrating the formulas against a reference gauge (see
docs/CALIBRATION.md).

Note on sleepy sensors: a TPMS sensor that is not fitted, not pressurised and
not moving transmits rarely or not at all. See the README's "Waking the
sensors" section if nothing shows up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "tesla_tpms_ble")
)

from decoder import (  # noqa: E402
    PROFILES,
    TESLA_COMPANY_ID,
    decode,
    looks_like_tesla_tpms,
)

try:
    from bleak import BleakScanner
except ImportError:
    sys.exit(
        "bleak is not installed.\n"
        "    python3 -m venv .venv && .venv/bin/pip install bleak\n"
        "    .venv/bin/python tools/tpms_scan.py"
    )

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


class Tracker:
    """Accumulates per-sensor state so we can print a useful summary."""

    def __init__(self) -> None:
        self.seen: dict[str, dict] = {}
        self.frames = 0

    def record(self, address: str, name: str, reading) -> bool:
        """Record a frame. Returns True if this address is new."""
        self.frames += 1
        is_new = address not in self.seen
        entry = self.seen.setdefault(
            address, {"name": name, "frames": 0, "awake_frames": 0, "last": None}
        )
        entry["frames"] += 1
        if reading.awake:
            entry["awake_frames"] += 1
        entry["last"] = reading
        return is_new


def fmt_reading(address, name, rssi, reading, show_raw):
    ts = datetime.now().strftime("%H:%M:%S")
    head = f"{DIM}{ts}{RESET} {BOLD}{address}{RESET}"
    if name:
        head += f" {DIM}({name}){RESET}"
    head += f" {DIM}{rssi} dBm{RESET}"

    if not reading.awake:
        body = (
            f"  {YELLOW}asleep{RESET} (status=0x{reading.status:02X}) "
            f"no pressure/temperature in this frame"
        )
        if reading.battery_volts is not None:
            body += f" | batt {reading.battery_volts:.2f} V"
    else:
        body = (
            f"  {GREEN}{reading.pressure_bar:.2f} bar{RESET} "
            f"({reading.pressure_psi:.1f} psi / {reading.pressure_kpa:.0f} kPa)"
            f" | {CYAN}{reading.temperature_c:.1f} °C{RESET}"
            f" | batt {reading.battery_volts:.2f} V ({reading.battery_percent}%)"
            f" | status 0x{reading.status:02X}"
        )

    out = f"{head}\n{body}"
    if show_raw:
        raw = reading.raw
        out += (
            f"\n  {DIM}raw: b0=0x{raw['byte0']:02X} b1=0x{raw['byte1']:02X} "
            f"status={raw['status']} pressure={raw['pressure']} "
            f"temp={raw['temperature']} batt_mv={raw['battery_mv']}{RESET}"
        )
    return out


async def run(args) -> int:
    profile = PROFILES[args.profile]
    tracker = Tracker()
    log_fh = open(args.log, "a", encoding="utf-8") if args.log else None

    print(f"{BOLD}Tesla-protocol BLE TPMS scanner{RESET}")
    print(
        f"{DIM}company id 0x{TESLA_COMPANY_ID:04X} ({TESLA_COMPANY_ID}) "
        f"| profile: {profile.name} | Ctrl-C to stop{RESET}\n"
    )

    def callback(device, adv):
        mfr = adv.manufacturer_data or {}

        if args.all and TESLA_COMPANY_ID not in mfr:
            ids = ", ".join(f"0x{k:04X}" for k in mfr) or "none"
            print(
                f"{DIM}{datetime.now().strftime('%H:%M:%S')} "
                f"{device.address} rssi={adv.rssi} name={adv.local_name!r} "
                f"mfr_ids=[{ids}]{RESET}"
            )
            for cid, payload in mfr.items():
                print(f"{DIM}    0x{cid:04X}: {payload.hex(' ')}{RESET}")
            return

        payload = mfr.get(TESLA_COMPANY_ID)
        if payload is None:
            return

        if not args.no_filter and not looks_like_tesla_tpms(payload):
            print(
                f"{DIM}{device.address} has Tesla company id but does not look "
                f"like a TPMS frame (probably a vehicle): {payload.hex(' ')}{RESET}"
            )
            return

        reading = decode(payload, profile)
        if reading is None:
            return

        is_new = tracker.record(device.address, adv.local_name or "", reading)
        if is_new:
            print(f"{BOLD}{GREEN}>>> new sensor: {device.address}{RESET}")
        print(fmt_reading(device.address, adv.local_name, adv.rssi, reading, args.raw))

        if log_fh:
            log_fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "address": device.address,
                        "name": adv.local_name,
                        "rssi": adv.rssi,
                        "mfr_hex": payload.hex(),
                        "profile": profile.name,
                        "awake": reading.awake,
                        "status": reading.status,
                        "pressure_bar": reading.pressure_bar,
                        "temperature_c": reading.temperature_c,
                        "battery_volts": reading.battery_volts,
                        "raw": reading.raw,
                    }
                )
                + "\n"
            )
            log_fh.flush()

    scanner = BleakScanner(detection_callback=callback, scanning_mode="active")

    try:
        await scanner.start()
        if args.duration:
            await asyncio.sleep(args.duration)
        else:
            while True:
                await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass
        if log_fh:
            log_fh.close()

    print_summary(tracker, args)
    return 0 if tracker.seen else 1


def print_summary(tracker: Tracker, args) -> None:
    print(f"\n{BOLD}── summary ──{RESET}")
    if not tracker.seen:
        print(f"{RED}No Tesla-protocol TPMS sensors seen.{RESET}")
        print(
            "\nThese sensors sleep when the wheel is not turning. Try:\n"
            "  • fit and inflate the tyres, then drive above ~25 km/h\n"
            "  • or spin/shake the sensor hard to trip its accelerometer\n"
            "  • or wake it with a TPMS activation tool (125 kHz LF)\n"
            "  • run with --all to confirm the adapter sees any BLE traffic\n"
            "  • leave it running for 15-30 min: parked sensors still emit\n"
            "    occasional keep-alive frames"
        )
        return

    print(f"{tracker.frames} frames from {len(tracker.seen)} sensor(s)\n")
    for address, e in sorted(tracker.seen.items()):
        last = e["last"]
        line = f"  {BOLD}{address}{RESET}"
        if e["name"]:
            line += f" ({e['name']})"
        line += f"  frames={e['frames']} awake={e['awake_frames']}"
        print(line)
        if last and last.awake:
            print(
                f"      last: {last.pressure_bar:.2f} bar / "
                f"{last.temperature_c:.1f} °C / {last.battery_volts:.2f} V"
            )
        elif last:
            print("      last: asleep (no measurement in frame)")
    if args.log:
        print(f"\nLogged to {args.log}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Scan for and decode Tesla-protocol BLE TPMS sensors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--all", action="store_true",
                   help="also print every non-Tesla BLE advertisement seen")
    p.add_argument("--raw", action="store_true",
                   help="print raw decoded field values (for calibration)")
    p.add_argument("--no-filter", action="store_true",
                   help="do not apply the TPMS shape heuristic")
    p.add_argument("--profile", choices=sorted(PROFILES), default="default",
                   help="decoding profile (default: %(default)s)")
    p.add_argument("--duration", type=int, default=0,
                   help="stop after N seconds (default: run until Ctrl-C)")
    p.add_argument("--log", metavar="FILE",
                   help="append decoded frames to FILE as JSONL")
    args = p.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
