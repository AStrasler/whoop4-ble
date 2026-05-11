#!/usr/bin/env python3
"""
Example: Stream live HR from the band over the standard BLE GATT
Heart Rate Service (0x180D / 0x2A37).

This service is the same one any fitness BLE device exposes — it works
without authentication and is independent of the Whoop custom protocol.
You'll see HR every ~1 second when the band is on-wrist, plus RR intervals
when the PPG signal is clean.

Usage:
    export WHOOP_ADDRESS="<your-band-ble-address>"
    python3 examples/03_live_hr.py
"""
import asyncio
import os
import struct
import sys
import time
from datetime import datetime

from bleak import BleakClient

ADDRESS      = os.environ.get("WHOOP_ADDRESS", "")
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def decode_hr_measurement(raw: bytes) -> dict:
    """Standard BLE Heart Rate Measurement characteristic decode."""
    if not raw:
        return {}
    flags = raw[0]
    out: dict = {"flags": f"0x{flags:02X}"}
    offset = 1
    # bit 0: HR value format (0 = uint8, 1 = uint16)
    if flags & 0x01:
        out["hr_bpm"] = struct.unpack_from("<H", raw, offset)[0]
        offset += 2
    else:
        out["hr_bpm"] = raw[offset]
        offset += 1
    # bit 4: RR intervals present
    if flags & 0x10:
        rrs = []
        while offset + 1 < len(raw):
            rr_raw = struct.unpack_from("<H", raw, offset)[0]
            offset += 2
            rr_ms = round(rr_raw * 1000 / 1024)  # 1/1024 sec ticks → ms
            if 200 <= rr_ms <= 2500:
                rrs.append(rr_ms)
        if rrs:
            out["rr_ms"] = rrs
    return out


async def main():
    if not ADDRESS:
        print("Set WHOOP_ADDRESS env var. Discover with: python3 -m whoop4.scan")
        sys.exit(1)

    def on_hr(_char, raw: bytearray):
        d = decode_hr_measurement(bytes(raw))
        ts = datetime.now().strftime("%H:%M:%S")
        rr = " rr=" + str(d["rr_ms"]) if "rr_ms" in d else ""
        print(f"{ts}  HR={d.get('hr_bpm','--'):>3} bpm{rr}")

    async with BleakClient(ADDRESS, timeout=20.0) as client:
        await client.start_notify(HR_CHAR_UUID, on_hr)
        print("Streaming live HR. Press Ctrl-C to stop.")
        while True:
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
