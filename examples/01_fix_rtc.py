#!/usr/bin/env python3
"""
Example: Fix the Whoop 4.0 RTC so flash writes resume.

The strap's RTC can reset to ~0 after a hard reset or deep discharge. When
it does, the firmware silently refuses to write HR/HRV records to flash with:

    Flash: RTC timestamp 32154354 is invalid; not saving data to flash.

This script sends SET_CLOCK (0x0A) with the **5-byte payload required by the
firmware** ([LE uint32 Unix timestamp][0x01 commit byte]). The 4-byte payload
documented elsewhere in the community is silently ignored — see the discovery
write-up in docs/PROTOCOL.md.

Usage:
    export WHOOP_ADDRESS="<your-band-ble-address>"
    python3 examples/01_fix_rtc.py
"""
import asyncio
import os
import struct
import sys
import time

from bleak import BleakClient

# Make the parent src/ importable so the example runs as-is from the repo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from whoop4 import WhoopPacket, PacketType, CommandNumber  # noqa: E402

ADDRESS  = os.environ.get("WHOOP_ADDRESS", "")
CMD_TO   = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"


async def main() -> None:
    if not ADDRESS:
        print("Set WHOOP_ADDRESS env var to your band's address.")
        print("Discover it with: python3 -m whoop4.scan")
        sys.exit(1)

    async with BleakClient(ADDRESS, timeout=20.0) as client:
        now = int(time.time())
        # The 5-byte payload — 4-byte LE Unix timestamp + 0x01 commit flag.
        # Without the trailing byte the firmware accepts the write but does
        # not update the RTC. Confirmed empirically: any 5th byte works,
        # the firmware appears to only check `len(payload) >= 5`.
        payload = struct.pack("<I", now) + b"\x01"
        pkt = WhoopPacket(
            PacketType.COMMAND, 0, CommandNumber.SET_CLOCK, payload
        ).framed_packet()
        await client.write_gatt_char(CMD_TO, pkt)
        print(f"Sent SET_CLOCK with ts={now}")
        print("Wear the band for 5–10 minutes, then run 02_drain_to_jsonl.py.")
        print("If HR records appear with valid 2026+ timestamps, the RTC is fixed.")


if __name__ == "__main__":
    asyncio.run(main())
