#!/usr/bin/env python3
"""Read-only GATT probe for a Tesla-protocol BLE TPMS sensor.

The scanner (``tpms_scan.py``) only listens to advertisements. This tool
*connects* to one sensor and looks at its GATT table, because a Tesla car does
not read pressure from the advertisement — it opens a connection and exchanges
Protobuf messages over these characteristics:

    service 00000211-b2d1-43f0-9b88-960cebf8b91e   (Tesla VCSEC)
      0212  write     app/car -> sensor  (e.g. a TPDataRequest)
      0213  indicate  sensor  -> app/car  (e.g. TPData {pressure, temperature})
      0214  read      communication version

    service f000ffd0-0451-4000-b000-000000000000   (TI OAD firmware update)

This probe is deliberately **read-only**: it connects, enumerates services,
reads every readable characteristic, subscribes to the indicate characteristic,
waits, and prints whatever the sensor sends unprompted. It never writes, never
pairs/bonds, and never sends a request — so it cannot change the sensor or
impersonate a car. It exists to answer one question: *can this sensor be made
to give up a reading over a connection at all, without Tesla's enrolment?*

    python3 tools/tpms_gatt.py                 # pick the strongest tsTPMS sensor
    python3 tools/tpms_gatt.py --list          # just list tsTPMS sensors, don't connect
    python3 tools/tpms_gatt.py --address <id>  # target one sensor
    python3 tools/tpms_gatt.py --listen 60     # subscribe and watch for 60 s

On macOS the address is a CoreBluetooth UUID, not the BC:6A:29 MAC, so target a
sensor by scanning for the name instead (the default). Grant your terminal
Bluetooth permission (System Settings -> Privacy & Security -> Bluetooth) or the
process is killed with SIGABRT and no message.

While it listens, wake the sensor: spin or shake it hard, or drive. If a value
arrives on 0213, or 0214 reads back something other than empty, connection-based
reads are viable and worth building into the integration. If nothing ever comes
without a request being written first, the sensor only talks to something that
speaks Tesla's Protobuf request protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

# Tesla VCSEC communication service and its three characteristics.
TESLA_SERVICE = "00000211-b2d1-43f0-9b88-960cebf8b91e"
CHAR_WRITE = "00000212-b2d1-43f0-9b88-960cebf8b91e"
CHAR_INDICATE = "00000213-b2d1-43f0-9b88-960cebf8b91e"
CHAR_VERSION = "00000214-b2d1-43f0-9b88-960cebf8b91e"

# The name the sensors advertise. Note the trailing space that BlueZ/CoreBluetooth
# report; we match with a stripped comparison so either form works.
SENSOR_NAME = "tsTPMS"

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit(
        "bleak is not installed.\n"
        "    python3 -m venv .venv && .venv/bin/pip install bleak\n"
        "    .venv/bin/python tools/tpms_gatt.py"
    )

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def discover(duration: float):
    """Return {address: (name, rssi)} for every tsTPMS sensor heard."""
    print(f"{DIM}scanning {duration:.0f}s for '{SENSOR_NAME}' sensors...{RESET}")
    found: dict[str, tuple[str, int]] = {}

    def cb(device, adv):
        name = (adv.local_name or device.name or "").strip()
        if name == SENSOR_NAME:
            found[device.address] = (name, adv.rssi)

    scanner = BleakScanner(detection_callback=cb, scanning_mode="active")
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return found


async def probe(address: str, listen: float) -> int:
    print(f"{BOLD}connecting to {address}{RESET}")
    try:
        async with BleakClient(address, timeout=20.0) as client:
            print(f"{GREEN}connected{RESET} (services enumerated)\n")

            # 1. Full GATT table.
            print(f"{BOLD}GATT table{RESET}")
            for service in client.services:
                tag = " (Tesla VCSEC)" if service.uuid == TESLA_SERVICE else ""
                if service.uuid.startswith("f000ffd0"):
                    tag = " (TI OAD firmware update)"
                print(f"  {CYAN}service {service.uuid}{tag}{RESET}")
                for ch in service.characteristics:
                    props = ",".join(ch.properties)
                    print(f"    char {ch.uuid}  [{props}]")

            # 2. Read every readable characteristic.
            print(f"\n{BOLD}reads{RESET}")
            for service in client.services:
                for ch in service.characteristics:
                    if "read" not in ch.properties:
                        continue
                    try:
                        val = await client.read_gatt_char(ch)
                        label = "version" if ch.uuid == CHAR_VERSION else ch.uuid
                        pretty = val.hex(" ") if val else "(empty)"
                        print(f"  {label}: {pretty}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {ch.uuid}: {RED}read failed: {exc}{RESET}")

            # 3. Subscribe to the indicate characteristic and wait.
            got: list[bytes] = []

            def on_indicate(_handle, data: bytearray):
                got.append(bytes(data))
                print(
                    f"{GREEN}{ts()} indicate 0213: {bytes(data).hex(' ')}{RESET}"
                    f"  {DIM}({len(data)} bytes){RESET}"
                )

            print(
                f"\n{BOLD}listening on 0213 for {listen:.0f}s{RESET} "
                f"{DIM}— now spin/shake the sensor or drive to wake it{RESET}"
            )
            try:
                await client.start_notify(CHAR_INDICATE, on_indicate)
            except Exception as exc:  # noqa: BLE001
                print(f"{RED}could not subscribe to 0213: {exc}{RESET}")
            else:
                try:
                    await asyncio.sleep(listen)
                finally:
                    try:
                        await client.stop_notify(CHAR_INDICATE)
                    except Exception:  # noqa: BLE001
                        pass

            print(f"\n{BOLD}── result ──{RESET}")
            if got:
                print(
                    f"{GREEN}the sensor sent {len(got)} indication(s) without being "
                    f"asked.{RESET} Connection-based reads are viable."
                )
            else:
                print(
                    f"{YELLOW}no unsolicited indications.{RESET} Either the sensor was "
                    "asleep the whole time, or it only replies to a written request "
                    "(Tesla's Protobuf TPDataRequest). A read-only probe cannot tell "
                    "those apart — try again while the sensor is definitely awake."
                )
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}connection failed: {exc}{RESET}")
        print(
            f"{DIM}These sensors drop an idle connection after ~15 s and only accept "
            f"one central at a time — close nRF Connect / Home Assistant first, and "
            f"stay close.{RESET}"
        )
        return 1


async def run(args) -> int:
    if args.address and not args.list:
        return await probe(args.address, args.listen)

    found = await discover(args.scan)
    if not found:
        print(f"{RED}no '{SENSOR_NAME}' sensors heard.{RESET}")
        print(
            f"{DIM}They may be asleep or out of range. Get within a metre of a wheel, "
            f"or wake one, and retry.{RESET}"
        )
        return 1

    print(f"\n{BOLD}tsTPMS sensors heard:{RESET}")
    ranked = sorted(found.items(), key=lambda kv: kv[1][1], reverse=True)
    for addr, (name, rssi) in ranked:
        print(f"  {addr}  {rssi} dBm")

    if args.list:
        return 0

    target = ranked[0][0]
    print(f"\n{DIM}strongest is {target}; connecting to it{RESET}\n")
    return await probe(target, args.listen)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Read-only GATT probe for a Tesla-protocol TPMS sensor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--address", metavar="ID",
                   help="target this address/UUID instead of picking one")
    p.add_argument("--list", action="store_true",
                   help="list tsTPMS sensors and exit without connecting")
    p.add_argument("--scan", type=float, default=8.0, metavar="SECS",
                   help="how long to scan for sensors (default: %(default)ss)")
    p.add_argument("--listen", type=float, default=30.0, metavar="SECS",
                   help="how long to watch 0213 for indications (default: %(default)ss)")
    args = p.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
