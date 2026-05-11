#!/usr/bin/env python3
"""
Apply SET_CLOCK (cmd=0x0A) to fix the Whoop 4.0 RTC.

Discovered via cross-referencing:
- cs-balazs/gowhoop: SetTime() uses CommandCodeSetTime = 0x0A
- jogolden/whoomp: SET_CLOCK = 10 (0x0A)
- project-whoopsie: protocol table documents 0x0A as SET_CLOCK

Command: 0x0A (SET_CLOCK)
Payload: 4-byte LE uint32 = current Unix timestamp in seconds
Response: fire-and-forget (no COMMAND_RESPONSE expected)
  — band may emit EVENT type 16 (SET_RTC) after accepting
Works: standalone (no drain context required)

Also: 0x0B = GET_CLOCK (query current RTC, returns 8-byte response)

Verification: After SET_CLOCK, the "Flash: RTC timestamp X is invalid"
console errors should STOP. We listen for 30s after sending.

Usage:
    python3 ble_set_clock.py            # set clock, verify
    python3 ble_set_clock.py --dry-run  # just GET_CLOCK, don't set
    python3 ble_set_clock.py --repeat 3 # send 3 times for reliability
"""
import asyncio, os, struct, sys, time, argparse
from datetime import datetime, timezone
from pathlib import Path
from bleak import BleakClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from .packet import WhoopPacket, PacketType, CommandNumber  # type: ignore
except ImportError:
    from packet import WhoopPacket, PacketType, CommandNumber  # type: ignore

ADDRESS   = os.environ.get("WHOOP_ADDRESS", "")
CMD_TO    = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM  = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA_FROM = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

SET_CLOCK = 0x0A
GET_CLOCK = 0x0B


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Only GET_CLOCK, don't send SET_CLOCK")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Send SET_CLOCK this many times (default 1)")
    parser.add_argument("--address", default=ADDRESS)
    args = parser.parse_args()

    rtc_errors_before: list[int] = []
    rtc_errors_after:  list[int] = []
    clock_responses: list[bytes] = []
    set_clock_sent_at: list[float] = []
    all_cmd_responses: list[tuple[int, bytes]] = []
    event_packets: list[bytes] = []

    def on_pkt(_src: str, raw: bytearray):
        r = bytes(raw)
        if len(r) < 7 or r[0] != 0xAA:
            return
        ptype, seq, cmd = r[4], r[5], r[6]
        data = r[7:-4]

        if ptype == 0x32:  # CONSOLE_LOG
            text = data.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
            if "RTC timestamp" in text and "invalid" in text:
                try:
                    val = int(text.split("RTC timestamp")[1].split("is")[0].strip())
                    if set_clock_sent_at:
                        rtc_errors_after.append(val)
                        log(f"  [RTC AFTER] val={val}  ← STILL INVALID (bad)")
                    else:
                        rtc_errors_before.append(val)
                        log(f"  [RTC before] val={val}")
                except Exception:
                    pass
            elif "RTC" in text or "Flash" in text or "saving" in text.lower():
                log(f"  [console] {text[:120]}")
            elif any(k in text for k in ["BLE:", "MFLT"]):
                if "History burst" not in text:
                    log(f"  [console] {text[:120]}")

        elif ptype == 0x24:  # COMMAND_RESPONSE
            all_cmd_responses.append((cmd, data))
            if cmd == GET_CLOCK:
                clock_responses.append(data)
                if len(data) >= 4:
                    rtc_val = struct.unpack_from("<I", data, 0)[0]
                    log(f"  *** GET_CLOCK response: RTC={rtc_val} ({datetime.fromtimestamp(rtc_val, tz=timezone.utc).isoformat() if rtc_val > 1e9 else 'INVALID'}) ***")
                else:
                    log(f"  *** GET_CLOCK response raw: {data.hex()} ***")
            elif cmd == SET_CLOCK:
                log(f"  *** SET_CLOCK ACK: seq=0x{seq:02X} data={data.hex()} ***")
            else:
                log(f"  [CMD_RESP] cmd=0x{cmd:02X} seq=0x{seq:02X} data={data.hex()}")

        elif ptype == 0x30:  # EVENT
            event_packets.append(r)
            event_type = cmd
            if event_type == 16:  # SET_RTC event
                log(f"  *** EVENT SET_RTC (type=16): {data.hex()} ***")
            else:
                pass  # suppress noisy events

    log(f"Connecting to {args.address}...")
    async with BleakClient(args.address, timeout=20.0) as client:
        log("Connected.")
        await client.start_notify(CMD_FROM,  lambda _, d: on_pkt("CMD", d))
        await client.start_notify(DATA_FROM, lambda _, d: on_pkt("DAT", d))

        # Step 1: GET_CLOCK (0x0B) — query current RTC
        log("Sending GET_CLOCK (0x0B)...")
        get_pkt = WhoopPacket(PacketType.COMMAND, 0, GET_CLOCK, b"").framed_packet()
        log(f"  Packet: {get_pkt.hex()}")
        await client.write_gatt_char(CMD_TO, get_pkt, response=True)
        await asyncio.sleep(2.0)

        # Step 2: Collect baseline RTC errors
        log("Listening for RTC errors (5s baseline)...")
        await asyncio.sleep(5.0)
        log(f"Baseline: {len(rtc_errors_before)} RTC errors, last val: {rtc_errors_before[-1] if rtc_errors_before else 'none'}")

        if args.dry_run:
            log("Dry-run mode: not sending SET_CLOCK.")
        else:
            # Step 3: SET_CLOCK
            now = int(time.time())
            dt_now = datetime.fromtimestamp(now, tz=timezone.utc)
            log(f"Sending SET_CLOCK (0x{SET_CLOCK:02X}) with ts={now} ({dt_now.isoformat()})...")
            for i in range(args.repeat):
                # CRITICAL: 5-byte payload = [LE uint32 ts][0x01 commit flag]
                # Without the 0x01 suffix, firmware silently ignores the command.
                set_pkt = WhoopPacket(PacketType.COMMAND, 0, SET_CLOCK,
                                      struct.pack("<I", now) + b"\x01").framed_packet()
                log(f"  Packet ({i+1}/{args.repeat}): {set_pkt.hex()}")
                await client.write_gatt_char(CMD_TO, set_pkt, response=True)
                if i < args.repeat - 1:
                    await asyncio.sleep(0.5)
            set_clock_sent_at.append(time.time())
            log("SET_CLOCK sent. Watching for 30s...")

            # Step 4: Watch for RTC errors to stop
            await asyncio.sleep(30.0)

            # Step 5: GET_CLOCK again to confirm
            log("Sending GET_CLOCK again to verify new RTC value...")
            now2 = int(time.time())
            get_pkt2 = WhoopPacket(PacketType.COMMAND, 0, GET_CLOCK, b"").framed_packet()
            await client.write_gatt_char(CMD_TO, get_pkt2, response=True)
            await asyncio.sleep(2.0)

    # Report
    log("\n=== RESULTS ===")
    log(f"GET_CLOCK responses received: {len(clock_responses)}")
    log(f"RTC errors BEFORE SET_CLOCK: {len(rtc_errors_before)}")
    log(f"RTC errors AFTER  SET_CLOCK: {len(rtc_errors_after)}")
    log(f"All CMD_RESPs: {[(f'0x{c:02X}', d.hex()) for c, d in all_cmd_responses]}")
    log(f"EVENT packets: {len(event_packets)}")

    if not args.dry_run:
        if rtc_errors_before and not rtc_errors_after:
            log("")
            log("SUCCESS: RTC errors STOPPED after SET_CLOCK!")
            log("The band's clock is now correct.")
            log("Next: wear the band for 5-10 minutes, then run:")
            log("  python3 ble_drain.py")
            log("You should see new records with 2026 timestamps.")
        elif not rtc_errors_before and not rtc_errors_after:
            log("")
            log("INFO: No RTC errors seen at all during this session.")
            log("This could mean the band needs to be on-wrist to trigger flash writes.")
            log("Try: wear band for 5-10 min, then run ble_drain.py.")
            log("If records appear with 2026 timestamps, SET_CLOCK worked.")
        elif rtc_errors_after:
            log("")
            log(f"PARTIAL: RTC errors still appear after SET_CLOCK.")
            log(f"  Last BEFORE val: {rtc_errors_before[-1] if rtc_errors_before else 'N/A'}")
            log(f"  Last AFTER val:  {rtc_errors_after[-1]}")
            log("  Try: python3 ble_set_clock.py --repeat 3")
            log("  Or check if GET_CLOCK showed the new timestamp.")


if __name__ == "__main__":
    asyncio.run(main())
