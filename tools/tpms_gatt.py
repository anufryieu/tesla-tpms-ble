#!/usr/bin/env python3
"""GATT probe for a Tesla-protocol BLE TPMS sensor.

The scanner (``tpms_scan.py``) only listens to advertisements. This tool
*connects* to one sensor and looks at its GATT table, because a Tesla car does
not read pressure from the advertisement — it opens a connection and exchanges
Protobuf messages over these characteristics:

    service 00000211-b2d1-43f0-9b88-960cebf8b91e   (Tesla VCSEC)
      0212  write     app/car -> sensor  (e.g. a TPDataRequest)
      0213  indicate  sensor  -> app/car  (e.g. TPData {pressure, temperature})
      0214  read      communication version

    service f000ffd0-0451-4000-b000-000000000000   (TI OAD firmware update)

By default this probe is **read-only**: it connects, enumerates services, reads
every readable characteristic, subscribes to the indicate characteristic,
waits, and prints whatever the sensor sends unprompted. It never pairs or bonds.

With ``--request`` it also does what a car does on the *normal* pressure-read
path: it writes a ``TPDataRequest`` to 0212 and watches 0213 for the matching
``TPData``. That is a plain "tell me your pressure and temperature" message —
not enrolment, not the certificate exchange, nothing that changes the sensor.
The request bytes are built from the protobuf field numbers documented in
``docs/PROTOCOL.md``; because the outer VCSEC framing is not fully pinned down,
several documented encodings are tried in turn, and ``--request-hex`` lets you
write one exact frame if you already know it.

    python3 tools/tpms_gatt.py                 # read-only: pick the strongest sensor
    python3 tools/tpms_gatt.py --list          # just list tsTPMS sensors, don't connect
    python3 tools/tpms_gatt.py --address <id>  # target one sensor
    python3 tools/tpms_gatt.py --listen 60     # subscribe and watch for 60 s
    python3 tools/tpms_gatt.py --request       # also send a TPDataRequest and watch
    python3 tools/tpms_gatt.py --request-hex 0a020801   # send exactly these bytes

On macOS the address is a CoreBluetooth UUID, not the BC:6A:29 MAC, so target a
sensor by scanning for the name instead (the default). Grant your terminal
Bluetooth permission (System Settings -> Privacy & Security -> Bluetooth) or the
process is killed with SIGABRT and no message.

While it listens, wake the sensor: spin or shake it hard, or drive. If a value
arrives on 0213 — unprompted, or in reply to ``--request`` — connection-based
reads are viable and worth building into the integration. If nothing ever comes,
even after a request, the sensor talks only to something that speaks Tesla's
full enrolled protocol, which this tool deliberately does not attempt.
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


# Candidate TPDataRequest frames to write to 0212.
#
# docs/PROTOCOL.md records the reverse-engineered messages:
#     enum TPDataRequest_E { ... TP_DATAREQUEST_PRESSURE_TEMPERATURE = 1; ... }
# and notes the two sides speak "length-prefixed Protocol Buffers" over 0212/0213.
#
# The inner request is almost certainly a single enum field set to 1, which in
# protobuf wire form is tag 0x08 (field 1, varint) + value 0x01 -> "08 01".
# What wraps it is the open question: it may be sent bare, nested in an outer
# VCSEC message as a length-delimited field (tag 0x0a/0x12/0x1a = field 1/2/3),
# and/or prefixed with a 2-byte big-endian length. We try the documented
# possibilities from simplest outward and stop at the first that draws a reply.
# None of these enrol, bond, or carry a certificate; the worst case is that the
# sensor ignores an unrecognised frame.
def request_candidates() -> list[tuple[str, bytes]]:
    """Return (description, bytes) request frames to try, simplest first."""
    inner = bytes.fromhex("0801")  # TPDataRequest{ request = PRESSURE_TEMPERATURE }
    candidates: list[tuple[str, bytes]] = [("bare TPDataRequest", inner)]
    for field_tag in (0x0A, 0x12, 0x1A):  # wrapped as outer field 1 / 2 / 3
        wrapped = bytes([field_tag, len(inner)]) + inner
        candidates.append((f"wrapped as field {(field_tag >> 3)}", wrapped))
    # Same set again, each behind a 2-byte big-endian length prefix.
    prefixed = [
        (f"{desc}, 2-byte length prefix", len(body).to_bytes(2, "big") + body)
        for desc, body in list(candidates)
    ]
    return candidates + prefixed


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


def _frame_hex(frames: list[tuple[str, bytes]] | None, desc: str) -> str:
    """Hex of the frame with the given description, for the final hint."""
    for name, body in frames or []:
        if name == desc:
            return body.hex()
    return "?"


async def _send_requests(
    client, frames: list[tuple[str, bytes]], got: list[bytes]
) -> str | None:
    """Write each candidate request to 0212, waiting for a reply after each.

    Returns the description of the first frame that drew a new indication, or
    None if none did. Each write is a single short frame to the data-request
    characteristic; no bonding, no retries, no certificate exchange.
    """
    ch = client.services.get_characteristic(CHAR_WRITE)
    if ch is None:
        print(f"{RED}no 0212 write characteristic on this sensor{RESET}")
        return None
    # Prefer a with-response write so the sensor ACKs the frame; fall back to
    # write-without-response if that is all it advertises.
    with_response = "write" in ch.properties
    if not with_response and "write-without-response" not in ch.properties:
        print(f"{RED}0212 is not writable [{','.join(ch.properties)}]{RESET}")
        return None

    print(f"\n{BOLD}sending {len(frames)} candidate TPDataRequest frame(s){RESET}")
    for desc, body in frames:
        before = len(got)
        print(f"  {DIM}{ts()}{RESET} write 0212: {body.hex(' ')}  {DIM}({desc}){RESET}")
        try:
            await client.write_gatt_char(CHAR_WRITE, body, response=with_response)
        except Exception as exc:  # noqa: BLE001
            print(f"    {RED}write failed: {exc}{RESET}")
            continue
        # Give the sensor a moment to answer on 0213 before trying the next.
        for _ in range(12):
            await asyncio.sleep(0.25)
            if len(got) > before:
                print(f"    {GREEN}reply after '{desc}'{RESET}")
                return desc
    print(f"  {DIM}no reply to any candidate so far{RESET}")
    return None


async def probe(
    address: str,
    listen: float,
    request_frames: list[tuple[str, bytes]] | None = None,
) -> int:
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

            # 3. Subscribe to the indicate characteristic.
            got: list[bytes] = []

            def on_indicate(_handle, data: bytearray):
                got.append(bytes(data))
                print(
                    f"{GREEN}{ts()} indicate 0213: {bytes(data).hex(' ')}{RESET}"
                    f"  {DIM}({len(data)} bytes){RESET}"
                )

            unsolicited = 0
            replied_to: str | None = None
            try:
                await client.start_notify(CHAR_INDICATE, on_indicate)
            except Exception as exc:  # noqa: BLE001
                print(f"{RED}could not subscribe to 0213: {exc}{RESET}")
            else:
                try:
                    if request_frames:
                        # 4. Send each candidate TPDataRequest; first to draw a
                        # reply wins and the rest are skipped.
                        replied_to = await _send_requests(
                            client, request_frames, got
                        )
                    # Passive listen (the whole window when read-only, a short
                    # tail otherwise) in case a reading arrives late.
                    print(
                        f"\n{BOLD}listening on 0213 for {listen:.0f}s{RESET} "
                        f"{DIM}— now spin/shake the sensor or drive to wake it{RESET}"
                    )
                    before = len(got)
                    await asyncio.sleep(listen)
                    if not request_frames:
                        unsolicited = len(got)
                    elif len(got) > before and replied_to is None:
                        # Something arrived during the passive tail, after the
                        # requests had already gone unanswered.
                        replied_to = "(passive, after requests)"
                finally:
                    try:
                        await client.stop_notify(CHAR_INDICATE)
                    except Exception:  # noqa: BLE001
                        pass

            print(f"\n{BOLD}── result ──{RESET}")
            if replied_to and replied_to.startswith("("):
                print(
                    f"{GREEN}the sensor sent an indication during the listen "
                    f"window.{RESET} Connection-based reads are viable."
                )
            elif replied_to:
                print(
                    f"{GREEN}the sensor replied to a written TPDataRequest "
                    f"({replied_to}).{RESET} Connection-based reads are viable, and "
                    f"this is the frame to build into the integration:\n"
                    f"  {CYAN}{_frame_hex(request_frames, replied_to)}{RESET}"
                )
            elif request_frames:
                print(
                    f"{YELLOW}no reply to any candidate request, and nothing "
                    f"unsolicited.{RESET} Either the sensor was asleep throughout, or "
                    "none of the tried framings is the one it expects. Retry while it "
                    "is definitely awake; if it still never answers, it likely speaks "
                    "only to an enrolled car."
                )
            elif got:
                print(
                    f"{GREEN}the sensor sent {unsolicited} indication(s) without being "
                    f"asked.{RESET} Connection-based reads are viable."
                )
            else:
                print(
                    f"{YELLOW}no unsolicited indications.{RESET} Either the sensor was "
                    "asleep the whole time, or it only replies to a written request. "
                    "Re-run with --request while the sensor is definitely awake."
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


def _frames_from_args(args) -> list[tuple[str, bytes]] | None:
    """Build the request frames to send, or None for a read-only probe."""
    if args.request_hex:
        cleaned = args.request_hex.replace(" ", "")
        try:
            return [("--request-hex", bytes.fromhex(cleaned))]
        except ValueError:
            sys.exit(f"--request-hex is not valid hex: {args.request_hex!r}")
    if args.request:
        return request_candidates()
    return None


async def run(args) -> int:
    frames = _frames_from_args(args)

    if args.address and not args.list:
        return await probe(args.address, args.listen, frames)

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
    return await probe(target, args.listen, frames)


def main() -> int:
    p = argparse.ArgumentParser(
        description="GATT probe for a Tesla-protocol TPMS sensor.",
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
    p.add_argument("--request", action="store_true",
                   help="also write candidate TPDataRequest frames to 0212 and "
                        "watch 0213 for the TPData reply (the normal read path)")
    p.add_argument("--request-hex", metavar="HEX",
                   help="write exactly this hex frame to 0212 instead of the "
                        "built-in candidates (implies --request)")
    args = p.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
