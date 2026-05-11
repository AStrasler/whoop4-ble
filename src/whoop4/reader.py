#!/usr/bin/env python3
"""
Health Coach — BLE Reader for Whoop 4.0

Connects to Whoop band via Bluetooth Low Energy and logs:
- Heart rate (continuous, from standard HR service 0x180D)
- RR intervals (from HR characteristic, for local HRV calculation)
- Skin temperature + SpO2 (from custom DATA_FROM_STRAP characteristic)

Outputs to data/hr.jsonl (append-only).

Usage:
    python3 ble_reader.py              # Scan, connect, and log
    python3 ble_reader.py --scan-only  # Just scan for Whoop devices
    python3 ble_reader.py --device XX:XX:XX:XX:XX:XX  # Connect to specific address
"""

import asyncio
import json
import os
import signal
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

# --- Configuration ---
ROBLE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROBLE_DIR / "data"
HR_FILE = DATA_DIR / "hr.jsonl"
STATE_FILE = DATA_DIR / "_health_state.json"
LOG_FILE = Path.home() / ".health-coach-ble.log"

# BLE UUIDs
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Whoop custom service for sensor data (temp, SpO2, accelerometer)
WHOOP_DATA_SERVICE_UUID = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WHOOP_DATA_FROM_STRAP_UUID = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

# How often to flush buffered records to disk
FLUSH_INTERVAL_SEC = 10
# Max records to buffer before forcing a flush
FLUSH_BUFFER_MAX = 50

# Reconnect parameters
RECONNECT_DELAY_SEC = 5
MAX_RECONNECT_ATTEMPTS = 0  # 0 = infinite


# --- Logging ---
def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [ble] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --- Data buffer ---
class DataBuffer:
    """Buffers HR/RR records and flushes to JSONL periodically."""

    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.buffer: list[dict] = []
        self.last_flush = time.time()
        self.total_written = 0

    def add(self, record: dict):
        self.buffer.append(record)
        now = time.time()
        if len(self.buffer) >= FLUSH_BUFFER_MAX or (now - self.last_flush) >= FLUSH_INTERVAL_SEC:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        with open(self.output_path, "a") as f:
            for rec in self.buffer:
                f.write(json.dumps(rec) + "\n")
        self.total_written += len(self.buffer)
        self.buffer.clear()
        self.last_flush = time.time()


# --- HR characteristic parser ---
def parse_hr_measurement(data: bytearray) -> dict:
    """
    Parse the Heart Rate Measurement characteristic (0x2A37).

    Byte 0 = flags:
      bit 0: 0 = HR is uint8, 1 = HR is uint16
      bit 1-2: sensor contact status
      bit 3: energy expended present
      bit 4: RR-interval present

    Returns dict with hr_bpm and optional rr_intervals_ms list.
    """
    flags = data[0]
    hr_format_16bit = bool(flags & 0x01)
    rr_present = bool(flags & 0x10)
    energy_present = bool(flags & 0x08)

    offset = 1

    if hr_format_16bit:
        hr_bpm = struct.unpack_from("<H", data, offset)[0]
        offset += 2
    else:
        hr_bpm = data[offset]
        offset += 1

    if energy_present:
        offset += 2  # skip energy expended (uint16)

    rr_intervals_ms = []
    if rr_present:
        while offset + 1 < len(data):
            # RR intervals are in 1/1024 second units
            rr_raw = struct.unpack_from("<H", data, offset)[0]
            rr_ms = round(rr_raw * 1000.0 / 1024.0, 1)
            rr_intervals_ms.append(rr_ms)
            offset += 2

    return {"hr_bpm": hr_bpm, "rr_intervals_ms": rr_intervals_ms}


# --- Whoop custom data parser ---
def parse_whoop_data(data: bytearray) -> dict | None:
    """
    Parse Whoop DATA_FROM_STRAP characteristic.
    Format is not fully documented — extract what we can.
    Returns dict with available sensor values, or None if unparseable.
    """
    if len(data) < 4:
        return None

    result = {}

    # Attempt to extract known fields based on packet length patterns
    if len(data) >= 8:
        try:
            temp_raw = struct.unpack_from("<h", data, 4)[0]
            temp_c = temp_raw / 100.0
            if 25.0 <= temp_c <= 42.0:
                result["skin_temp_c"] = round(temp_c, 2)
        except (struct.error, IndexError):
            pass

    if len(data) >= 10:
        try:
            spo2 = data[8]
            if 70 <= spo2 <= 100:
                result["spo2_pct"] = spo2
        except IndexError:
            pass

    return result if result else None


# --- Scanner ---
async def scan_for_whoop(timeout: float = 15.0) -> list[dict]:
    """Scan for Whoop devices. Returns list of {name, address}."""
    log(f"Scanning for Whoop devices ({timeout}s)...")
    devices = await BleakScanner.discover(timeout=timeout)
    whoop_devices = []
    for d in devices:
        name = d.name or ""
        if "WHOOP" in name.upper():
            whoop_devices.append({"name": name, "address": d.address})
            log(f"  Found: {name} ({d.address})")
    if not whoop_devices:
        log("No Whoop devices found. Make sure HR broadcast is enabled on the band.")
    return whoop_devices


# --- Main BLE session ---
async def run_ble_session(address: str):
    """Connect to a Whoop device and stream HR data."""
    buffer = DataBuffer(HR_FILE)
    session_start = datetime.now(timezone.utc)
    sample_count = 0
    rr_count = 0

    def hr_callback(characteristic: BleakGATTCharacteristic, data: bytearray):
        nonlocal sample_count, rr_count
        parsed = parse_hr_measurement(data)
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "ts": now,
            "hr": parsed["hr_bpm"],
        }
        if parsed["rr_intervals_ms"]:
            record["rr_ms"] = parsed["rr_intervals_ms"]
            rr_count += len(parsed["rr_intervals_ms"])

        buffer.add(record)
        sample_count += 1

        if sample_count % 60 == 0:
            log(f"  HR: {parsed['hr_bpm']} bpm | RR intervals total: {rr_count} | Samples: {sample_count}")

    last_sensor_write = [0.0]  # mutable container for closure

    def whoop_data_callback(characteristic: BleakGATTCharacteristic, data: bytearray):
        parsed = parse_whoop_data(data)
        if not parsed:
            return
        # Throttle sensor writes to once per 60 seconds
        now_ts = time.time()
        if now_ts - last_sensor_write[0] < 60:
            return
        last_sensor_write[0] = now_ts

        now = datetime.now(timezone.utc).isoformat()
        record = {"ts": now, "type": "sensor"}
        if "skin_temp_c" in parsed:
            record["skin_temp_c"] = parsed["skin_temp_c"]
        if "spo2_pct" in parsed:
            record["spo2_pct"] = parsed["spo2_pct"]
        buffer.add(record)
        log(f"  Sensor: {record}")

    log(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        log(f"Connected: {client.is_connected}")

        # List services for debugging
        for service in client.services:
            log(f"  Service: {service.uuid} ({service.description})")

        # Subscribe to HR measurement
        try:
            await client.start_notify(HR_MEASUREMENT_UUID, hr_callback)
            log("Subscribed to Heart Rate Measurement")
        except Exception as e:
            log(f"Failed to subscribe to HR: {e}")
            return

        # Try subscribing to Whoop custom data
        try:
            await client.start_notify(WHOOP_DATA_FROM_STRAP_UUID, whoop_data_callback)
            log("Subscribed to Whoop DATA_FROM_STRAP")
        except Exception as e:
            log(f"Whoop custom data not available: {e} (normal if HR broadcast only)")

        log("Streaming data... (Ctrl+C to stop)")

        # Keep alive loop with periodic flush
        try:
            while client.is_connected:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                buffer.flush()
        except asyncio.CancelledError:
            pass

    # Final flush on disconnect
    buffer.flush()
    duration = (datetime.now(timezone.utc) - session_start).total_seconds()
    log(f"Session ended. Duration: {duration:.0f}s | Samples: {sample_count} | RR intervals: {rr_count} | Written: {buffer.total_written}")


async def run_with_reconnect(address: str):
    """Run BLE session with automatic reconnection."""
    attempt = 0
    while MAX_RECONNECT_ATTEMPTS == 0 or attempt < MAX_RECONNECT_ATTEMPTS:
        try:
            await run_ble_session(address)
            log("Disconnected cleanly.")
        except Exception as e:
            log(f"Connection error: {e}")

        attempt += 1
        log(f"Reconnecting in {RECONNECT_DELAY_SEC}s... (attempt {attempt})")
        await asyncio.sleep(RECONNECT_DELAY_SEC)


# --- JSONL schema init ---
def ensure_schema():
    """Write schema line if hr.jsonl doesn't exist yet."""
    if not HR_FILE.exists():
        HR_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HR_FILE, "w") as f:
            f.write(json.dumps({
                "_schema": "hr",
                "_version": 1,
                "_fields": "ts, hr, rr_ms (optional list), type (optional: sensor), skin_temp_c (optional), spo2_pct (optional)",
            }) + "\n")


# --- CLI ---
async def main():
    ensure_schema()

    scan_only = "--scan-only" in sys.argv
    device_arg = None
    if "--device" in sys.argv:
        idx = sys.argv.index("--device")
        if idx + 1 < len(sys.argv):
            device_arg = sys.argv[idx + 1]

    if scan_only:
        devices = await scan_for_whoop(timeout=15.0)
        if devices:
            print(f"\nFound {len(devices)} Whoop device(s):")
            for d in devices:
                print(f"  {d['name']} — {d['address']}")
            print(f"\nTo connect: python3 ble_reader.py --device {devices[0]['address']}")
        else:
            print("\nNo Whoop devices found.")
            print("Make sure HR Broadcast is enabled: Whoop app > Device Settings > Heart Rate Broadcast")
        return

    address = device_arg
    if not address:
        # Auto-scan and pick first Whoop
        devices = await scan_for_whoop(timeout=15.0)
        if not devices:
            log("No Whoop devices found. Enable HR Broadcast in the Whoop app.")
            log("Then retry: python3 ble_reader.py --scan-only")
            sys.exit(1)
        address = devices[0]["address"]
        log(f"Auto-selected: {devices[0]['name']} ({address})")

    await run_with_reconnect(address)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped by user.")
