#!/usr/bin/env python3
"""
Whoop 4.0 BLE Historical Data Drain — Multi-pass.

Connects to the strap, requests historical data, ACKs each batch with the
trim offset (so the band frees those records and serves the next chunk),
and loops until HISTORY_COMPLETE or the band stops sending.

Writes:
    data/hr.jsonl                  — one parsed record per line (ts/hr/rr_ms/accel)
    data/raw/<run_id>/historical.bin   — every raw HISTORICAL_DATA packet (length-prefixed)
    data/raw/<run_id>/events.jsonl     — all decoded EVENT packets (sleep boundaries, temp, battery)
    data/_health_state.json        — last sync timestamp + counts

Raw packet capture lets us re-decode old data later without re-pulling from the band.

Usage:
    python3 ble_drain.py                # normal drain (ACKs trims, advances band's read pointer)
    python3 ble_drain.py --no-trim      # passive drain (do NOT ACK — band may resend)
    python3 ble_drain.py --max-passes N # cap passes (default 30)
"""
import asyncio
import json
import os
import struct
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient

# Allow running from src/whoop4/ or as installed module
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from .packet import (  # type: ignore
        WhoopPacket, PacketType, CommandNumber, MetadataType,
        parse_historical_record, parse_event_packet,
    )
except ImportError:
    from packet import (  # type: ignore
        WhoopPacket, PacketType, CommandNumber, MetadataType,
        parse_historical_record, parse_event_packet,
    )

# --- Configuration ---
# BLE address of your band. Set via env var WHOOP_ADDRESS or --address CLI arg.
# Find it with: `python3 -m whoop4.scan` or via your OS Bluetooth menu.
ADDRESS = os.environ.get("WHOOP_ADDRESS", "")
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA_FROM = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

# Data output paths (override with WHOOP_DATA_DIR env var; default ./data)
DATA_DIR = Path(os.environ.get("WHOOP_DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
HR_FILE = DATA_DIR / "hr.jsonl"
STATE_FILE = DATA_DIR / "_state.json"
RAW_DIR = DATA_DIR / "raw"

# --- Globals ---
existing_ts = set()
records_by_day = Counter()
ts_range = [None, None]
total_new = 0
last_ts_unix = 0
seq_in_sec = 0


def load_existing():
    global existing_ts
    existing_ts = set()
    if HR_FILE.exists():
        with open(HR_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or '"_schema"' in line:
                    continue
                try:
                    existing_ts.add(json.loads(line).get("ts", ""))
                except json.JSONDecodeError:
                    continue


async def do_one_pass(no_trim: bool, raw_hist_fp, events_fp):
    """Single connect→drain cycle. Returns (new_written, history_complete, last_trim, batch_count)."""
    global total_new, last_ts_unix, seq_in_sec

    hist_done = asyncio.Event()
    pending_trim = asyncio.Queue()
    record_count = 0
    new_written = 0
    last_trim = 0
    batch_records = 0
    get_clock_result = {"hw_rtc": None}

    hr_file = open(HR_FILE, "a")

    def write_record(rec):
        global total_new, last_ts_unix, seq_in_sec
        nonlocal new_written
        ts_unix = rec["timestamp"]
        if ts_unix < 1577836800:
            return
        if ts_unix == last_ts_unix:
            seq_in_sec += 1
        else:
            last_ts_unix = ts_unix
            seq_in_sec = 0
        dt = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{seq_in_sec:03d}+00:00"
        if ts in existing_ts:
            return
        entry = {"ts": ts, "hr": rec["hr"]}
        if "rr_ms" in rec:
            entry["rr_ms"] = rec["rr_ms"]
        if "accel" in rec:
            entry["accel"] = rec["accel"]
        if "skin_temp_c" in rec:
            entry["skin_temp_c"] = rec["skin_temp_c"]
        hr_file.write(json.dumps(entry) + "\n")
        existing_ts.add(ts)
        new_written += 1
        total_new += 1

    def handle_packet(raw: bytearray):
        """Common packet dispatcher (called from both CMD_FROM and DATA_FROM notifications)."""
        nonlocal record_count, batch_records
        pkt = WhoopPacket.from_data(bytes(raw))
        if not pkt:
            return

        if pkt.type == PacketType.HISTORICAL_DATA:
            # Save raw bytes (length-prefixed) for retroactive re-decoding
            raw_hist_fp.write(struct.pack("<H", len(raw)) + bytes(raw))
            record_count += 1
            batch_records += 1
            rec = parse_historical_record(pkt.data)
            if rec:
                ts = rec["timestamp"]
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                records_by_day[day] += 1
                if ts_range[0] is None or ts < ts_range[0]:
                    ts_range[0] = ts
                if ts_range[1] is None or ts > ts_range[1]:
                    ts_range[1] = ts
                write_record(rec)

        elif pkt.type == PacketType.METADATA:
            try:
                mt = MetadataType(pkt.cmd)
                if mt == MetadataType.HISTORY_COMPLETE:
                    hist_done.set()
                elif mt == MetadataType.HISTORY_END:
                    pending_trim.put_nowait(pkt.data)
            except ValueError:
                pass

        elif pkt.type == PacketType.COMMAND_RESPONSE and pkt.cmd == CommandNumber.GET_CLOCK:
            # Response: [00][01][LE uint32 hw_rtc at offset 2][...]
            if len(pkt.data) >= 6:
                get_clock_result["hw_rtc"] = struct.unpack_from("<I", pkt.data, 2)[0]

        elif pkt.type == PacketType.EVENT:
            evt = parse_event_packet(pkt)
            if evt:
                evt["received_at"] = datetime.now(timezone.utc).isoformat()
                events_fp.write(json.dumps(evt) + "\n")
                events_fp.flush()

    def on_data(char, raw: bytearray):
        handle_packet(raw)

    def on_cmd(char, raw: bytearray):
        handle_packet(raw)

    client = BleakClient(ADDRESS)
    await client.connect()

    await client.start_notify(CMD_FROM, on_cmd)
    await client.start_notify(DATA_FROM, on_data)

    # Defensive RTC sync — official Whoop app does this on every connect.
    # Without the 0x01 commit flag, firmware silently ignores SET_CLOCK and flash
    # writes fail with "RTC timestamp X is invalid" until the next valid SET_CLOCK.
    set_clock_ts = int(time.time())
    set_clock = WhoopPacket(
        PacketType.COMMAND, 0, CommandNumber.SET_CLOCK,
        struct.pack("<I", set_clock_ts) + b"\x01"
    ).framed_packet()
    await client.write_gatt_char(CMD_TO, set_clock)
    await asyncio.sleep(0.3)  # let firmware apply before drain starts

    cmd = WhoopPacket(
        PacketType.COMMAND, 0, CommandNumber.SEND_HISTORICAL_DATA, b"\x00"
    ).framed_packet()
    await client.write_gatt_char(CMD_TO, cmd)

    # GET_CLOCK during drain to verify the RTC sync took effect and detect reboots
    await asyncio.sleep(0.8)
    get_clock = WhoopPacket(
        PacketType.COMMAND, 0, CommandNumber.GET_CLOCK, b"\x00"
    ).framed_packet()
    await client.write_gatt_char(CMD_TO, get_clock)

    start = time.time()
    last_count = 0
    last_time = time.time()
    batch_num = 0

    while not hist_done.is_set():
        try:
            end_data = pending_trim.get_nowait()
            batch_num += 1
            batch_records = 0
            if len(end_data) >= 14:
                trim = struct.unpack_from("<I", end_data, 10)[0]
                last_trim = trim
                if not no_trim:
                    response = struct.pack("<BII", 1, trim, 0)
                    ack = WhoopPacket(
                        PacketType.COMMAND, 0,
                        CommandNumber.HISTORICAL_DATA_RESULT, response,
                    ).framed_packet()
                    try:
                        await client.write_gatt_char(CMD_TO, ack)
                    except Exception:
                        break
        except asyncio.QueueEmpty:
            pass

        if record_count > last_count:
            last_count = record_count
            last_time = time.time()

        if time.time() - last_time > 20 and record_count > 0:
            break
        if time.time() - start > 600:
            break
        await asyncio.sleep(0.05)

    got_complete = hist_done.is_set()
    hr_file.flush()
    hr_file.close()

    try:
        await client.disconnect()
    except Exception:
        pass

    return new_written, got_complete, last_trim, batch_num, get_clock_result["hw_rtc"]


async def main():
    args = sys.argv[1:]
    no_trim = "--no-trim" in args
    max_passes = 30
    for i, a in enumerate(args):
        if a == "--max-passes" and i + 1 < len(args):
            max_passes = int(args[i + 1])

    load_existing()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RAW_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_hist_path = run_dir / "historical.bin"
    events_path = run_dir / "events.jsonl"

    print(f"Drain start. Existing records: {len(existing_ts):,}")
    print(f"Mode: {'PASSIVE (no trim)' if no_trim else 'NORMAL (acks trims)'}")
    print(f"Raw capture: {run_dir}\n")

    raw_hist_fp = open(raw_hist_path, "ab")
    events_fp = open(events_path, "a")

    pass_num = 0
    consecutive_empty = 0
    last_hw_rtc = None

    # Load previous hw_rtc from state to detect reboots
    prev_state = {}
    if STATE_FILE.exists():
        try: prev_state = json.loads(STATE_FILE.read_text())
        except Exception: pass
    prev_hw_rtc = prev_state.get("ble_last_hw_rtc")

    try:
        while pass_num < max_passes:
            pass_num += 1
            print(f"--- Pass {pass_num} ---")
            new, complete, trim, batches, hw_rtc = await do_one_pass(no_trim, raw_hist_fp, events_fp)
            if hw_rtc is not None:
                last_hw_rtc = hw_rtc
                wall = int(time.time())
                drift = hw_rtc - wall
                valid = hw_rtc > 1_577_836_800  # year 2020+
                tag = "VALID" if valid else "INVALID (still ~32M — RTC FIX FAILED)"
                print(f"  RTC check: hw_rtc={hw_rtc} drift={drift:+d}s {tag}")
                if prev_hw_rtc and hw_rtc < prev_hw_rtc - 60:
                    print(f"  ⚠ REBOOT DETECTED: hw_rtc went backwards from {prev_hw_rtc} → {hw_rtc}")
                if not valid:
                    print(f"  ⚠ Flash writes will silently fail until RTC is fixed.")
            raw_hist_fp.flush()
            latest = ""
            if ts_range[1]:
                latest = datetime.fromtimestamp(ts_range[1], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"  +{new:,} new records, {batches} batches, trim=0x{trim:08X}, latest={latest}")

            if complete:
                print("\n  HISTORY_COMPLETE received.")
                break
            if new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    print("\n  3 consecutive empty passes — done.")
                    break
            else:
                consecutive_empty = 0

            await asyncio.sleep(2)
    finally:
        raw_hist_fp.close()
        events_fp.close()

    print(f"\n{'=' * 60}")
    print("FINAL RESULTS")
    print(f"  Passes:               {pass_num}")
    print(f"  New records written:  {total_new:,}")
    print(f"  Raw historical bytes: {raw_hist_path.stat().st_size:,}")
    print(f"  Event records:        {events_path.stat().st_size and sum(1 for _ in open(events_path))}")

    if ts_range[0] and ts_range[1]:
        e = datetime.fromtimestamp(ts_range[0], tz=timezone.utc)
        l = datetime.fromtimestamp(ts_range[1], tz=timezone.utc)
        span = ts_range[1] - ts_range[0]
        print(f"  Time range:           {e} → {l}")
        print(f"  Span:                 {span / 3600:.1f}h ({span / 86400:.1f}d)")

    if records_by_day:
        print(f"\n  Daily coverage ({len(records_by_day)} days):")
        for d in sorted(records_by_day):
            print(f"    {d}: {records_by_day[d]:>7,}")

    if total_new > 0 or last_hw_rtc is not None:
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
        if total_new > 0:
            state["ble_last_sync"] = datetime.now(timezone.utc).isoformat()
            state["ble_last_records"] = total_new
            state["ble_last_run_id"] = run_id
        if last_hw_rtc is not None:
            state["ble_last_hw_rtc"] = last_hw_rtc
            state["ble_last_hw_rtc_at"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2))



if __name__ == "__main__":
    if not ADDRESS:
        print("ERROR: BLE address not set.")
        print("  Set WHOOP_ADDRESS env var to your band's BLE address.")
        print("  Find it with: python3 -m whoop4.scan")
        sys.exit(1)
    asyncio.run(main())
