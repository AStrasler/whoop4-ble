#!/usr/bin/env python3
"""
Example: Drain all historical HR records from the band to a local JSONL file.

The strap stores HR samples (and RR intervals + accelerometer) on internal
flash for ~14 days as a circular buffer. This script:

  1. Connects to the strap
  2. Sends SET_CLOCK defensively (so flash writes keep working)
  3. Streams HISTORICAL_DATA packets via SEND_HISTORICAL_DATA (0x16)
  4. ACKs each batch with HISTORICAL_DATA_RESULT (0x17), advancing the band's
     read pointer so the next chunk gets served
  5. Writes one JSONL row per record to ./data/hr.jsonl

Usage:
    export WHOOP_ADDRESS="<your-band-ble-address>"
    python3 examples/02_drain_to_jsonl.py

For a feature-complete drain (raw capture, dedup, multi-pass), see
`python3 -m whoop4.drain` in the parent package.
"""
import asyncio
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone

from bleak import BleakClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from whoop4 import (  # noqa: E402
    WhoopPacket, PacketType, CommandNumber, MetadataType,
    parse_historical_record,
)

ADDRESS   = os.environ.get("WHOOP_ADDRESS", "")
CMD_TO    = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM  = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA_FROM = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
OUT_FILE  = "./data/hr.jsonl"


async def main():
    if not ADDRESS:
        print("Set WHOOP_ADDRESS env var. Discover with: python3 -m whoop4.scan")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

    pending_trim: asyncio.Queue = asyncio.Queue()
    record_count = 0
    out = open(OUT_FILE, "a")

    def on_pkt(_char, raw: bytearray):
        nonlocal record_count
        pkt = WhoopPacket.from_data(bytes(raw))
        if not pkt:
            return
        if pkt.type == PacketType.HISTORICAL_DATA:
            rec = parse_historical_record(pkt.data)
            if rec:
                rec["ts_iso"] = datetime.fromtimestamp(
                    rec["timestamp"], tz=timezone.utc
                ).isoformat()
                out.write(json.dumps(rec) + "\n")
                record_count += 1
        elif pkt.type == PacketType.METADATA and pkt.cmd == MetadataType.HISTORY_END:
            pending_trim.put_nowait(pkt.data)

    async with BleakClient(ADDRESS, timeout=20.0) as client:
        await client.start_notify(CMD_FROM, on_pkt)
        await client.start_notify(DATA_FROM, on_pkt)

        # Defensive RTC sync — official Whoop app does this on every connect.
        set_clock = WhoopPacket(
            PacketType.COMMAND, 0, CommandNumber.SET_CLOCK,
            struct.pack("<I", int(time.time())) + b"\x01",
        ).framed_packet()
        await client.write_gatt_char(CMD_TO, set_clock)
        await asyncio.sleep(0.3)

        # Start drain
        drain = WhoopPacket(
            PacketType.COMMAND, 0, CommandNumber.SEND_HISTORICAL_DATA, b"\x00"
        ).framed_packet()
        await client.write_gatt_char(CMD_TO, drain)
        print("Drain started. Press Ctrl-C to stop early.")

        # ACK loop until 10s of silence (no new batches)
        last_activity = time.time()
        while time.time() - last_activity < 10:
            try:
                trim_data = await asyncio.wait_for(pending_trim.get(), timeout=3)
            except asyncio.TimeoutError:
                continue
            last_activity = time.time()
            # Trim offset lives at bytes 10-13 of the HISTORY_END payload (LE uint32)
            trim = struct.unpack_from("<I", trim_data, 10)[0]
            ack = WhoopPacket(
                PacketType.COMMAND, 0, CommandNumber.HISTORICAL_DATA_RESULT,
                struct.pack("<BII", 1, trim, 0),
            ).framed_packet()
            await client.write_gatt_char(CMD_TO, ack)
            print(f"  {record_count:>6,} records  trim=0x{trim:08X}")

    out.close()
    print(f"\nDone. {record_count} records → {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
