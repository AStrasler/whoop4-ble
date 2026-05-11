"""
Whoop 4.0 BLE Debug Monitor — Backend
--------------------------------------
Connects to the Whoop band, subscribes to all characteristics, parses every
packet via whoop_packet.py, and broadcasts JSON over WebSocket on port 8766.
Also serves the HTML frontend via HTTP on port 8765.

Usage:
    python ble_monitor.py

Dependencies:
    pip install bleak websockets
"""

import sys
import os
from pathlib import Path

# Ensure whoop_packet.py is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from bleak import BleakClient, BleakError, BleakScanner
except ImportError:
    print("ERROR: 'bleak' is not installed. Run: pip install bleak")
    sys.exit(1)

try:
    import websockets
    from websockets.server import serve as ws_serve
except ImportError:
    print("ERROR: 'websockets' is not installed. Run: pip install websockets")
    sys.exit(1)

try:
    # Import from sibling whoop4 package
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from packet import (  # type: ignore
        WhoopPacket, PacketType, CommandNumber, MetadataType,
        parse_historical_record, parse_event_packet
    )
except ImportError as e:
    print(f"ERROR: Could not import packet module: {e}")
    print("Ensure packet.py is in the whoop4 package directory.")
    sys.exit(1)

import asyncio
import json
import struct
import time
import datetime
import logging
from http.server import BaseHTTPRequestHandler
from threading import Thread

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WHOOP_ADDRESS = os.environ.get("WHOOP_ADDRESS", "")
target_address: str = WHOOP_ADDRESS  # mutable — updated by scan/connect commands
ble_reconnect_event: asyncio.Event | None = None  # signals ble_loop to reconnect

# Characteristics
CMD_TO_UUID    = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM_UUID  = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA_FROM_UUID = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
HR_CHAR_UUID   = "00002a37-0000-1000-8000-00805f9b34fb"  # standard HR measurement
BATT_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"  # standard Battery Level

HTTP_PORT = 8765
WS_PORT   = 8766

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE  = os.path.join(SCRIPT_DIR, "index.html")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

connected_ws_clients: set = set()
packet_counts: dict = {}
byte_counts: list = []   # (timestamp, byte_count) rolling window
console_buffer: str = ""  # accumulated console log text

# Last-known sensor values — populated by auto-info-pull, sent to each new browser client.
# This way Battery/Temp/RTC show immediately on page load instead of waiting for next drain.
last_known: dict = {
    "battery_pct": None,
    "skin_temp_c": None,
    "rtc_val": None,
    "rtc_iso": None,
    "wrist": None,
}

REPLAY_BUFFER_SIZE = 500
replay_buffer: list = []  # last N packets, replayed to new browser connections

# Disk log: one JSON line per packet, appended forever
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "monitor-logs")
os.makedirs(LOG_DIR, exist_ok=True)
_log_filename = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".jsonl"
PACKET_LOG_PATH = os.path.join(LOG_DIR, _log_filename)
_packet_log_file = open(PACKET_LOG_PATH, "a", buffering=1)  # line-buffered
log_startup = None  # set after logging is configured

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("ble_monitor")

# ---------------------------------------------------------------------------
# Packet decoding helpers
# ---------------------------------------------------------------------------

def decode_hr_service(raw: bytes) -> dict:
    """Parse BLE standard Heart Rate Measurement characteristic (0x2A37)."""
    result = {}
    if len(raw) < 2:
        return result
    flags = raw[0]
    result["flags"] = f"0x{flags:02X}"
    offset = 1
    # Bit 0: HR format — 0=uint8, 1=uint16
    if flags & 0x01:
        if len(raw) < offset + 2:
            return result
        result["hr_bpm"] = struct.unpack_from("<H", raw, offset)[0]
        offset += 2
    else:
        result["hr_bpm"] = raw[offset]
        offset += 1
    # Bit 4: RR intervals present
    if flags & 0x10:
        rr_intervals = []
        while offset + 1 < len(raw):
            rr_raw = struct.unpack_from("<H", raw, offset)[0]
            offset += 2
            # RR in 1/1024 sec units → convert to ms
            rr_ms = round(rr_raw * 1000 / 1024)
            if 200 <= rr_ms <= 2500:
                rr_intervals.append(rr_ms)
        if rr_intervals:
            result["rr_ms"] = rr_intervals
    return result


def decode_whoop_packet(pkt: WhoopPacket, source: str) -> dict:
    """Build the 'decoded' sub-object for a WhoopPacket."""
    decoded = {}

    # Attempt to name the packet type
    try:
        pt = PacketType(pkt.type)
        decoded["type_name"] = pt.name
    except ValueError:
        decoded["type_name"] = f"UNKNOWN_0x{pkt.type:02X}"

    # Attempt to name the cmd field
    try:
        cn = CommandNumber(pkt.cmd)
        decoded["cmd_name"] = cn.name
    except ValueError:
        decoded["cmd_name"] = f"0x{pkt.cmd:02X}"

    try:
        ptype = PacketType(pkt.type)
    except ValueError:
        return decoded

    # --- HISTORICAL_DATA ---
    if ptype == PacketType.HISTORICAL_DATA:
        rec = parse_historical_record(pkt.data)
        if rec:
            decoded.update(rec)
            if "timestamp" in rec:
                decoded["timestamp_iso"] = datetime.datetime.fromtimestamp(
                    rec["timestamp"]
                ).isoformat() + "Z"

    # --- EVENT ---
    elif ptype == PacketType.EVENT:
        evt = parse_event_packet(pkt)
        if evt:
            decoded.update(evt)
            # Cache last-known battery/temp/wrist so new browser clients see them immediately
            if evt.get("battery_pct") is not None:
                last_known["battery_pct"] = evt["battery_pct"]
            if evt.get("skin_temp_c") is not None:
                last_known["skin_temp_c"] = evt["skin_temp_c"]
            if evt.get("event") == "wrist_on":
                last_known["wrist"] = "on"
            elif evt.get("event") == "wrist_off":
                last_known["wrist"] = "off"

    # --- METADATA ---
    elif ptype == PacketType.METADATA:
        data = pkt.data
        try:
            mt = MetadataType(pkt.cmd)
            decoded["metadata_type"] = mt.name
        except ValueError:
            decoded["metadata_type"] = f"0x{pkt.cmd:02X}"

        if pkt.cmd == MetadataType.HISTORY_START and len(data) >= 12:
            decoded["start_offset"] = struct.unpack_from("<I", data, 4)[0]
            decoded["end_offset"]   = struct.unpack_from("<I", data, 8)[0]
        elif pkt.cmd == MetadataType.HISTORY_END and len(data) >= 14:
            decoded["trim_offset"] = struct.unpack_from("<I", data, 10)[0]

    # --- CONSOLE_LOGS ---
    elif ptype == PacketType.CONSOLE_LOGS:
        # Console log text starts at byte 11 of the raw data field per observed framing
        global console_buffer
        if len(pkt.data) >= 11:
            chunk = pkt.data[11:].decode("ascii", errors="replace").rstrip("\x00")
        else:
            chunk = pkt.data.decode("ascii", errors="replace").rstrip("\x00")
        if chunk:
            console_buffer += chunk
            decoded["console_text"] = chunk
            decoded["console_buffer_len"] = len(console_buffer)

    # --- COMMAND_RESPONSE ---
    elif ptype == PacketType.COMMAND_RESPONSE:
        decoded["response_data_hex"] = pkt.data.hex()
        if pkt.cmd == CommandNumber.GET_CLOCK and len(pkt.data) >= 8:
            # Response format (14 bytes):
            #   [0]   0x00 status
            #   [1]   0x01 flag
            #   [2:6] LE uint32 hw_rtc (Unix seconds, used for flash-write validity check)
            #   [6:8] LE uint16 sub_second counter (32768 Hz ticks within current second)
            #   [8:]  six zero bytes
            rtc_val = struct.unpack_from("<I", pkt.data, 2)[0]
            subsec_ticks = struct.unpack_from("<H", pkt.data, 6)[0]
            decoded["rtc_val"] = rtc_val
            decoded["rtc_subsec"] = round(subsec_ticks / 32768, 4)
            decoded["rtc_raw_hex"] = pkt.data.hex()
            last_known["rtc_val"] = rtc_val
            if rtc_val > 1_577_836_800:  # > year 2020 = valid Unix timestamp
                iso = datetime.datetime.fromtimestamp(
                    rtc_val + subsec_ticks / 32768, tz=datetime.timezone.utc
                ).isoformat()
                decoded["rtc_iso"] = iso
                last_known["rtc_iso"] = iso

    # --- REALTIME_DATA ---
    elif ptype in (PacketType.REALTIME_DATA, PacketType.REALTIME_RAW_DATA):
        decoded["data_hex"] = pkt.data.hex()

    return decoded


def build_broadcast(source: str, raw: bytes, pkt: "WhoopPacket | None", extra_decoded: dict | None = None) -> dict:
    """Build the JSON payload broadcast to all WebSocket clients."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if pkt is not None:
        decoded = decode_whoop_packet(pkt, source)
        if extra_decoded:
            decoded.update(extra_decoded)
        msg = {
            "ts": ts,
            "source": source,
            "raw": raw.hex(),
            "parsed": {
                "type": decoded.get("type_name", f"0x{pkt.type:02X}"),
                "type_hex": f"0x{pkt.type:02X}",
                "seq": pkt.seq,
                "cmd_hex": f"0x{pkt.cmd:02X}",
                "data_hex": pkt.data.hex(),
                "decoded": decoded,
            }
        }
    else:
        # HR service or unparseable raw
        decoded = extra_decoded or {}
        msg = {
            "ts": ts,
            "source": source,
            "raw": raw.hex(),
            "parsed": {
                "type": decoded.get("type_name", "RAW"),
                "type_hex": "N/A",
                "seq": None,
                "cmd_hex": "N/A",
                "data_hex": raw.hex(),
                "decoded": decoded,
            }
        }

    return msg


async def broadcast(msg: dict):
    """Send a JSON message to all connected WebSocket clients, buffer it, and log to disk."""
    payload = json.dumps(msg)

    # Skip buffering/logging pure system handshake messages
    msg_type = msg.get("parsed", {}).get("type", "")
    if msg_type not in ("CONNECTED", "STATS"):
        # Replay buffer
        replay_buffer.append(payload)
        if len(replay_buffer) > REPLAY_BUFFER_SIZE:
            replay_buffer.pop(0)
        # Disk log
        _packet_log_file.write(payload + "\n")

    if not connected_ws_clients:
        return

    dead = set()
    for ws in connected_ws_clients:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    connected_ws_clients.difference_update(dead)

    # Track bytes/sec
    byte_counts.append((time.monotonic(), len(payload)))
    cutoff = time.monotonic() - 10
    while byte_counts and byte_counts[0][0] < cutoff:
        byte_counts.pop(0)


def track_packet(source: str, byte_len: int):
    packet_counts[source] = packet_counts.get(source, 0) + 1


# ---------------------------------------------------------------------------
# BLE notification handlers
# ---------------------------------------------------------------------------

def make_whoop_handler(source: str, loop: asyncio.AbstractEventLoop):
    def handler(sender, raw: bytearray):
        raw = bytes(raw)
        track_packet(source, len(raw))
        try:
            pkt = WhoopPacket.from_data(raw)
            msg = build_broadcast(source, raw, pkt)
        except Exception as e:
            msg = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": source,
                "raw": raw.hex(),
                "parsed": {
                    "type": "PARSE_ERROR",
                    "type_hex": "N/A",
                    "seq": None,
                    "cmd_hex": "N/A",
                    "data_hex": raw.hex(),
                    "decoded": {"error": str(e)},
                }
            }
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
    return handler


def make_battery_handler(loop: asyncio.AbstractEventLoop):
    def handler(sender, raw: bytearray):
        raw = bytes(raw)
        track_packet("BATTERY_SERVICE", len(raw))
        pct = raw[0] if raw else None
        decoded = {"type_name": "BATTERY_SERVICE", "battery_pct": pct}
        msg = build_broadcast("BATTERY_SERVICE", raw, None, extra_decoded=decoded)
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
    return handler


def make_hr_handler(loop: asyncio.AbstractEventLoop):
    def handler(sender, raw: bytearray):
        raw = bytes(raw)
        track_packet("HR_SERVICE", len(raw))
        try:
            decoded = decode_hr_service(raw)
            decoded["type_name"] = "HR_SERVICE"
            msg = build_broadcast("HR_SERVICE", raw, None, extra_decoded=decoded)
        except Exception as e:
            msg = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "HR_SERVICE",
                "raw": raw.hex(),
                "parsed": {
                    "type": "HR_PARSE_ERROR",
                    "type_hex": "N/A",
                    "seq": None,
                    "cmd_hex": "N/A",
                    "data_hex": raw.hex(),
                    "decoded": {"error": str(e)},
                }
            }
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
    return handler


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

async def ws_handler(websocket):
    connected_ws_clients.add(websocket)
    log.info(f"WebSocket client connected ({len(connected_ws_clients)} total)")

    # Replay buffered packets so the browser catches up instantly
    if replay_buffer:
        for payload in replay_buffer:
            try:
                await websocket.send(payload)
            except Exception:
                break
        log.info(f"Replayed {len(replay_buffer)} buffered packets to new client")

    # Send greeting with current stats + last-known sensor values so the
    # topbar (battery/temp/RTC/wrist) populates immediately instead of waiting for next drain.
    await websocket.send(json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "SYSTEM",
        "raw": "",
        "parsed": {
            "type": "CONNECTED",
            "type_hex": "N/A",
            "seq": None,
            "cmd_hex": "N/A",
            "data_hex": "",
            "decoded": {
                "type_name": "CONNECTED",
                "message": "BLE monitor connected",
                "packet_counts": packet_counts,
                "last_known": last_known,
            }
        }
    }))
    try:
        async for message in websocket:
            await handle_ws_command(message)
    except Exception:
        pass
    finally:
        connected_ws_clients.discard(websocket)
        log.info(f"WebSocket client disconnected ({len(connected_ws_clients)} total)")


ble_client_ref: "BleakClient | None" = None


async def handle_ws_command(message: str):
    """Handle commands sent from the browser."""
    global ble_client_ref
    try:
        cmd = json.loads(message)
        action = cmd.get("action")
        if action == "send_command":
            cmd_name = cmd.get("cmd", "")
            payload_hex = cmd.get("payload_hex", "00")
            # Map command name to CommandNumber
            try:
                cmd_num = CommandNumber[cmd_name].value
            except KeyError:
                try:
                    cmd_num = int(cmd_name, 16)
                except ValueError:
                    log.warning(f"Unknown command: {cmd_name}")
                    return
            payload = bytes.fromhex(payload_hex)
            pkt = WhoopPacket(PacketType.COMMAND, 0, cmd_num, payload)
            if ble_client_ref and ble_client_ref.is_connected:
                await ble_client_ref.write_gatt_char(CMD_TO_UUID, pkt.framed_packet())
                log.info(f"Sent command: {cmd_name} payload={payload_hex}")

        elif action == "send_raw":
            hex_str = cmd.get("hex", "")
            raw = bytes.fromhex(hex_str)
            if ble_client_ref and ble_client_ref.is_connected:
                await ble_client_ref.write_gatt_char(CMD_TO_UUID, raw)
                log.info(f"Sent raw: {hex_str}")

        elif action == "scan":
            duration = float(cmd.get("duration", 5.0))
            await broadcast({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "SYSTEM", "raw": "",
                "parsed": {"type": "SCAN_STARTED", "type_hex": "N/A", "seq": None,
                           "cmd_hex": "N/A", "data_hex": "",
                           "decoded": {"type_name": "SCAN_STARTED", "duration": duration}}
            })
            # bleak 0.20+: use discovered_devices_and_advertisement_data for RSSI
            scanner = BleakScanner()
            await scanner.start()
            await asyncio.sleep(duration)
            await scanner.stop()
            seen = scanner.discovered_devices_and_advertisement_data  # {addr: (BLEDevice, AdvData)}
            results = []
            for addr, (device, adv) in seen.items():
                rssi = adv.rssi if adv and adv.rssi is not None else -999
                results.append({"name": device.name or "(unknown)", "address": addr, "rssi": rssi})
            results.sort(key=lambda x: -x["rssi"])
            await broadcast({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "SYSTEM", "raw": "",
                "parsed": {"type": "SCAN_RESULTS", "type_hex": "N/A", "seq": None,
                           "cmd_hex": "N/A", "data_hex": "",
                           "decoded": {"type_name": "SCAN_RESULTS", "devices": results}}
            })
            log.info(f"Scan found {len(results)} devices")

        elif action == "connect":
            global target_address, ble_reconnect_event
            new_addr = cmd.get("address", "").strip()
            if new_addr:
                target_address = new_addr
                log.info(f"Target address changed to {target_address}")
                if ble_reconnect_event:
                    ble_reconnect_event.set()

        elif action == "set_clock":
            now = int(time.time())
            # CRITICAL: 5-byte payload required. [LE uint32 ts][0x01 commit flag].
            # 4-byte payload is silently ignored by firmware.
            payload = struct.pack("<I", now) + b"\x01"
            pkt = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.SET_CLOCK, payload)
            if ble_client_ref and ble_client_ref.is_connected:
                await ble_client_ref.write_gatt_char(CMD_TO_UUID, pkt.framed_packet(), response=True)
                log.info(f"Sent SET_CLOCK: ts={now}")
                await broadcast({
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": "SYSTEM", "raw": "",
                    "parsed": {
                        "type": "SET_CLOCK_SENT", "type_hex": "0x0A", "seq": None,
                        "cmd_hex": "0x0A", "data_hex": payload.hex(),
                        "decoded": {
                            "type_name": "SET_CLOCK_SENT",
                            "unix_ts": now,
                            "iso": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat(),
                        }
                    }
                })

        elif action == "get_stats":
            cutoff = time.monotonic() - 10
            recent_bytes = sum(b for t, b in byte_counts if t >= cutoff)
            bps = recent_bytes / 10.0
            await broadcast({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "SYSTEM",
                "raw": "",
                "parsed": {
                    "type": "STATS",
                    "type_hex": "N/A",
                    "seq": None,
                    "cmd_hex": "N/A",
                    "data_hex": "",
                    "decoded": {
                        "type_name": "STATS",
                        "packet_counts": packet_counts,
                        "bytes_per_sec": round(bps, 1),
                        "console_buffer": console_buffer,
                    }
                }
            })
    except json.JSONDecodeError:
        log.warning(f"Invalid JSON from client: {message}")
    except Exception as e:
        log.error(f"Error handling WS command: {e}")


# ---------------------------------------------------------------------------
# HTTP server (serves the HTML file)
# ---------------------------------------------------------------------------

HR_JSONL_PATH = os.environ.get(
    "WHOOP_HR_JSONL",
    str(Path(os.environ.get("WHOOP_DATA_DIR", "./data")) / "hr.jsonl"),
)


def compute_coverage() -> list:
    """Return [{date: 'YYYY-MM-DD', count: N}] from hr.jsonl."""
    from collections import Counter
    counts: Counter = Counter()
    if not os.path.exists(HR_JSONL_PATH):
        return []
    try:
        with open(HR_JSONL_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or '"_schema"' in line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("ts", "")
                    if len(ts) >= 10:
                        counts[ts[:10]] += 1
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"compute_coverage failed: {e}")
    return [{"date": d, "count": c} for d, c in sorted(counts.items())]


class HTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/coverage.json":
            data = json.dumps({"days": compute_coverage()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            with open(HTML_FILE, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"ble_monitor.html not found in same directory")

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


def run_http_server():
    from http.server import HTTPServer
    server = HTTPServer(("127.0.0.1", HTTP_PORT), HTMLHandler)
    log.info(f"HTTP server: http://localhost:{HTTP_PORT}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Stats printer
# ---------------------------------------------------------------------------

async def stats_printer():
    while True:
        await asyncio.sleep(5)
        total = sum(packet_counts.values())
        counts_str = " | ".join(f"{k}={v}" for k, v in sorted(packet_counts.items()))
        log.info(f"Packets: {total} total | {counts_str}")


# ---------------------------------------------------------------------------
# BLE connection loop
# ---------------------------------------------------------------------------

async def ble_loop():
    global ble_client_ref, ble_reconnect_event
    ble_reconnect_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    while True:
        ble_reconnect_event.clear()
        log.info(f"Connecting to {target_address}...")
        try:
            async with BleakClient(target_address, timeout=20.0) as client:
                ble_client_ref = client
                log.info("Connected to Whoop band")

                # Broadcast connection event
                await broadcast({
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": "SYSTEM",
                    "raw": "",
                    "parsed": {
                        "type": "BLE_CONNECTED",
                        "type_hex": "N/A",
                        "seq": None,
                        "cmd_hex": "N/A",
                        "data_hex": "",
                        "decoded": {"type_name": "BLE_CONNECTED", "address": WHOOP_ADDRESS}
                    }
                })

                # Subscribe to CMD_FROM
                try:
                    await client.start_notify(CMD_FROM_UUID, make_whoop_handler("CMD_FROM", loop))
                    log.info("Subscribed to CMD_FROM")
                except Exception as e:
                    log.warning(f"Could not subscribe to CMD_FROM: {e}")

                # Subscribe to DATA_FROM
                try:
                    await client.start_notify(DATA_FROM_UUID, make_whoop_handler("DATA_FROM", loop))
                    log.info("Subscribed to DATA_FROM")
                except Exception as e:
                    log.warning(f"Could not subscribe to DATA_FROM: {e}")

                # Subscribe to HR characteristic
                try:
                    await client.start_notify(HR_CHAR_UUID, make_hr_handler(loop))
                    log.info("Subscribed to HR_SERVICE")
                except Exception as e:
                    log.warning(f"Could not subscribe to HR_SERVICE: {e}")

                # Try standard Battery Level characteristic
                try:
                    await client.start_notify(BATT_CHAR_UUID, make_battery_handler(loop))
                    log.info("Subscribed to BATTERY_SERVICE")
                except Exception as e:
                    # Not all devices support notify on battery; try a one-time read
                    try:
                        raw = await client.read_gatt_char(BATT_CHAR_UUID)
                        pct = raw[0] if raw else None
                        if pct is not None:
                            log.info(f"Battery (read): {pct}%")
                            msg = {
                                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "source": "BATTERY_SERVICE", "raw": raw.hex(),
                                "parsed": {"type": "BATTERY_SERVICE", "type_hex": "0x2A19",
                                           "seq": None, "cmd_hex": "N/A", "data_hex": raw.hex(),
                                           "decoded": {"type_name": "BATTERY_SERVICE", "battery_pct": pct}}
                            }
                            asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
                    except Exception:
                        log.warning(f"Battery characteristic not available: {e}")

                # Auto info-pull: brief drain after connect to populate Battery/Temp/RTC.
                # Runs in the background — drain flushes a few EVENT packets and a GET_CLOCK
                # response, which the existing broadcast pipeline forwards to the browser.
                async def info_pull():
                    await asyncio.sleep(1.5)  # let notifications settle
                    try:
                        # Defensive SET_CLOCK (5-byte payload, [LE ts][commit])
                        ts_payload = struct.pack("<I", int(time.time())) + b"\x01"
                        sc = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.SET_CLOCK, ts_payload).framed_packet()
                        await client.write_gatt_char(CMD_TO_UUID, sc)
                        await asyncio.sleep(0.3)
                        # Start drain to activate command processor + flush EVENT packets
                        drain = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.SEND_HISTORICAL_DATA, b"\x00").framed_packet()
                        await client.write_gatt_char(CMD_TO_UUID, drain)
                        await asyncio.sleep(0.8)
                        # GET_CLOCK (1-byte payload) to populate RTC display
                        gc = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.GET_CLOCK, b"\x00").framed_packet()
                        await client.write_gatt_char(CMD_TO_UUID, gc)
                        await asyncio.sleep(3.0)  # let EVENT packets flow through
                        # ABORT — we don't want to actually drain, just flush
                        abort = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"").framed_packet()
                        await client.write_gatt_char(CMD_TO_UUID, abort)
                        log.info(f"Auto info-pull complete. last_known={last_known}")
                    except Exception as e:
                        log.warning(f"Auto info-pull failed: {e}")

                asyncio.create_task(info_pull())

                # Keep alive until disconnected or reconnect requested
                while client.is_connected and not ble_reconnect_event.is_set():
                    await asyncio.sleep(1)
                if ble_reconnect_event.is_set():
                    log.info(f"Reconnecting to new target: {target_address}")
                    await client.disconnect()

                log.warning("Whoop disconnected")
                ble_client_ref = None

                await broadcast({
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": "SYSTEM",
                    "raw": "",
                    "parsed": {
                        "type": "BLE_DISCONNECTED",
                        "type_hex": "N/A",
                        "seq": None,
                        "cmd_hex": "N/A",
                        "data_hex": "",
                        "decoded": {"type_name": "BLE_DISCONNECTED", "address": WHOOP_ADDRESS}
                    }
                })

        except BleakError as e:
            log.error(f"BLE error: {e}")
        except Exception as e:
            log.error(f"Unexpected error in BLE loop: {e}")
        finally:
            ble_client_ref = None

        log.info("Retrying BLE connection in 5 seconds...")
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    # Start HTTP server in background thread
    http_thread = Thread(target=run_http_server, daemon=True)
    http_thread.start()

    log.info(f"Packet log: {PACKET_LOG_PATH}")
    # Start WebSocket server
    log.info(f"WebSocket server: ws://localhost:{WS_PORT}")
    ws_server = await ws_serve(ws_handler, "127.0.0.1", WS_PORT)

    # Run BLE loop and stats printer concurrently
    await asyncio.gather(
        ble_loop(),
        stats_printer(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
