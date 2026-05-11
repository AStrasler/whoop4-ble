"""
Whoop 4.0 BLE Packet Protocol

Binary framing for the Whoop proprietary BLE service.
Based on reverse engineering by bWanShiTong and jogolden/whoomp.

Frame format:
    [SOF(1)] [Length(2, LE)] [CRC8(1)] [Payload(N)] [CRC32(4, LE)]
     0xAA     payload+4       CRC8      Type|Seq|Cmd|Data   CRC32
"""

import struct
from enum import IntEnum

SOF = 0xAA

# CRC8 table (polynomial 0x07)
_CRC8_TABLE = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = ((_crc << 1) ^ 0x07) & 0xFF if _crc & 0x80 else (_crc << 1) & 0xFF
    _CRC8_TABLE.append(_crc)


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


# CRC32 (standard, polynomial 0xEDB88320, reflect in/out, init/xor 0xFFFFFFFF)
_CRC32_TABLE = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = ((_crc >> 1) ^ 0xEDB88320) if _crc & 1 else _crc >> 1
    _CRC32_TABLE.append(_crc)


def crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


class PacketType(IntEnum):
    COMMAND = 0x23
    COMMAND_RESPONSE = 0x24
    REALTIME_DATA = 0x28
    REALTIME_RAW_DATA = 0x2B
    HISTORICAL_DATA = 0x2F
    EVENT = 0x30
    METADATA = 0x31
    CONSOLE_LOGS = 0x32


class CommandNumber(IntEnum):
    # RTC management (standalone — no drain required, fire-and-forget)
    SET_CLOCK = 0x0A   # payload: 5 bytes = [LE uint32 Unix ts][0x01 commit flag]. Fire-and-forget.
                       # CRITICAL: 4-byte payload (without commit flag) is SILENTLY IGNORED by firmware.
    GET_CLOCK = 0x0B   # payload: 1 byte (any value, e.g. 0x00). Returns response only during drain context.
                       # Response: [00][01][LE uint32 hw_rtc at offset 2][...]
    # Historical drain
    ABORT_HISTORICAL_TRANSMITS = 0x14
    SEND_HISTORICAL_DATA = 0x16
    HISTORICAL_DATA_RESULT = 0x17
    SET_READ_POINTER = 0x21
    GET_DATA_RANGE = 0x22


class MetadataType(IntEnum):
    HISTORY_START = 1
    HISTORY_END = 2
    HISTORY_COMPLETE = 3


class WhoopPacket:
    """Represents a single Whoop BLE packet."""

    def __init__(self, ptype: int, seq: int, cmd: int, data: bytes = b""):
        self.type = ptype
        self.seq = seq
        self.cmd = cmd
        self.data = data

    def framed_packet(self) -> bytes:
        """Build a framed packet ready to write to CMD_TO_STRAP."""
        payload = bytes([self.type, self.seq, self.cmd]) + self.data
        length = len(payload) + 4  # payload + CRC32
        length_bytes = struct.pack("<H", length)
        c8 = crc8(length_bytes)
        c32 = crc32(payload)
        return bytes([SOF]) + length_bytes + bytes([c8]) + payload + struct.pack("<I", c32)

    @classmethod
    def from_data(cls, raw: bytes) -> "WhoopPacket | None":
        """Parse a raw notification into a WhoopPacket. Returns None if invalid."""
        if len(raw) < 8 or raw[0] != SOF:
            return None

        length = struct.unpack_from("<H", raw, 1)[0]
        # c8_expected = raw[3]
        payload = raw[4:4 + length - 4]  # strip CRC32 from end

        if len(payload) < 3:
            return None

        ptype = payload[0]
        seq = payload[1]
        cmd = payload[2]
        data = payload[3:]

        return cls(ptype, seq, cmd, data)

    def __repr__(self):
        return f"WhoopPacket(type=0x{self.type:02X}, seq={self.seq}, cmd=0x{self.cmd:02X}, data={self.data.hex()})"


def parse_historical_record(data: bytes) -> dict | None:
    """
    Parse a historical data packet payload (93-byte sync batch record).

    Byte layout (cross-referenced from jogolden/whoomp + cs-balazs/gowhoop + our RE 2026-05-11):
    - Bytes 0-3:   Sequence counter (LE uint32)
    - Bytes 4-7:   Unix timestamp (LE uint32)
    - Bytes 8-9:   Sub-second tiebreaker (LE uint16)
    - Bytes 10-13: Flags / state
    - Byte 14:     Heart rate (BPM)
    - Byte 15:     RR interval count (0–4)
    - Bytes 16-23: Up to 4 RR intervals (uint16 LE, ms each)
    - Bytes 24-32: UNDECODED (9 bytes) — looks like flags + sensor status bits
    - Bytes 33-44: Accelerometer X/Y/Z sample 1 (3× float32 LE, g-force) — CONFIRMED
    - Bytes 45-48: UNDECODED float32 (1 sample saw value 11265.0 — possibly PPG raw or sensor temp)
    - Bytes 49-60: Accelerometer X/Y/Z sample 2 (3× float32 LE, g-force) — often mirrors bytes 33-44
                   suggesting filtered/smoothed copy or 2nd sample within the same second
    - Bytes 61-92: UNDECODED (~32 bytes) — varying byte patterns suggest more sensor channels
                   (gyro? PPG raw? skin temp updates within record?)

    NOTE: skin temperature is NOT in these packets — it arrives in EVENT (0x30) packets,
    type=17 (TEMPERATURE_LEVEL) or embedded in type=3 BATTERY_LEVEL events.
    """
    if len(data) < 15:
        return None

    try:
        timestamp = struct.unpack_from("<I", data, 4)[0]
        subsec = struct.unpack_from("<H", data, 8)[0]
        hr = data[14]

        # Validate
        if timestamp < 1577836800 or timestamp > 1893456000:  # 2020-2030
            return None
        if hr == 0 or hr > 220:
            return None

        record = {
            "timestamp": timestamp,
            "subsec": subsec,
            "hr": hr,
        }

        # RR intervals (HRV-grade beat-to-beat data)
        if len(data) >= 16:
            rr_count = data[15]
            if 0 < rr_count <= 4 and len(data) >= 16 + rr_count * 2:
                rr = []
                for i in range(rr_count):
                    rr_ms = struct.unpack_from("<H", data, 16 + i * 2)[0]
                    # Sanity check: 200–2500 ms covers HR 24–300 bpm
                    if 200 <= rr_ms <= 2500:
                        rr.append(rr_ms)
                if rr:
                    record["rr_ms"] = rr

        # Accelerometer (tentative — gowhoop labels these "maybe accelerometer")
        if len(data) >= 45:
            try:
                ax = struct.unpack_from("<f", data, 33)[0]
                ay = struct.unpack_from("<f", data, 37)[0]
                az = struct.unpack_from("<f", data, 41)[0]
                # Sanity check: each axis should be in roughly [-32, 32] for ±g range
                if all(-100.0 < v < 100.0 for v in (ax, ay, az)):
                    record["accel"] = [round(ax, 4), round(ay, 4), round(az, 4)]
            except struct.error:
                pass

        return record
    except (struct.error, IndexError):
        return None


# EVENT packet types (from project-whoopsie/whoopsie-protocol)
class EventType(IntEnum):
    BATTERY_LEVEL = 3            # uint32 LE / 10.0 = percent
    WRIST_ON = 9                 # sleep boundary marker
    WRIST_OFF = 10               # sleep boundary marker
    DOUBLE_TAP = 14
    TEMPERATURE_LEVEL = 17       # int16 LE / 10.0 = °C (skin temp)
    SENSOR_SNAPSHOT = 29         # plen=12. Contains battery (bytes 6-7 LE u16 = battery raw)
    UNKNOWN_32 = 32              # plen=32. Has trailing float32 (purpose unclear)
    EXTENDED_BATTERY_INFORMATION = 63
    # Small 4-byte state markers — purpose unclear (sleep phase? activity phase?)
    STATE_MARKER_A = 101         # payload always 01 00 00 00
    STATE_MARKER_B = 102         # payload always 01 01 00 00 (most common)
    STATE_MARKER_C = 103         # payload always 01 01 01 00


def parse_event_packet(pkt) -> dict | None:
    """Parse an EVENT (0x30) packet. Returns dict with event_type + decoded payload.

    Empirical packet layout for pkt.data (verified across 318 events from drain 20260413T185228Z):
        byte 0:       leading flag (always 0x00 in observed data)
        bytes 1-4:    device clock counter (LE uint32, ~70 ticks/sec, not unix time)
        bytes 5-6:    sub-counter (LE uint16, also clock-like)
        bytes 7-8:    payload length (LE uint16) — verified matches len(pkt.data) - 9
        bytes 9+:     payload (length bytes), starts with 1-byte sub-opcode then values

    BATTERY_LEVEL payload layout: [0x02][battery_pct uint32 LE / 10][temp uint16 LE / 100][...]
        Verified: 5 successive battery events monotonically decreased 31.3 → 30.4% over ~30s.
        Embedded temp in same packet was 38.0–38.1°C (plausible skin temp).
    """
    if pkt.type != PacketType.EVENT:
        return None
    try:
        evt_type = pkt.cmd
        raw = pkt.data
        result = {"event_type": evt_type, "_raw": raw.hex()}

        # Parse common header (length is verifiable so we trust it).
        if len(raw) >= 9:
            payload_len = struct.unpack_from("<H", raw, 7)[0]
            payload = raw[9:9 + payload_len] if payload_len else b""
        else:
            payload = b""

        if evt_type == EventType.WRIST_ON:
            result["event"] = "wrist_on"
        elif evt_type == EventType.WRIST_OFF:
            result["event"] = "wrist_off"
        elif evt_type == EventType.DOUBLE_TAP:
            result["event"] = "double_tap"
        elif evt_type == EventType.BATTERY_LEVEL and len(payload) >= 7:
            # Sub-opcode at payload[0] (observed: 0x02). Value at offset 1.
            battery_raw = struct.unpack_from("<I", payload, 1)[0]
            battery_pct = battery_raw / 10.0
            if 0.0 <= battery_pct <= 100.0:
                result["event"] = "battery"
                result["battery_pct"] = round(battery_pct, 1)
                # Bytes 5-6 of payload look like skin temperature (uint16 LE / 100)
                temp_raw = struct.unpack_from("<H", payload, 5)[0]
                temp_c = temp_raw / 100.0
                if 20.0 <= temp_c <= 45.0:
                    result["skin_temp_c"] = round(temp_c, 2)
            else:
                result["event"] = "battery_unparsed"
        elif evt_type == EventType.TEMPERATURE_LEVEL and len(payload) >= 2:
            temp_raw = struct.unpack_from("<h", payload, 0)[0]
            temp_c = temp_raw / 10.0
            if 20.0 <= temp_c <= 45.0:
                result["event"] = "temperature"
                result["skin_temp_c"] = round(temp_c, 2)
        elif evt_type == EventType.SENSOR_SNAPSHOT and len(payload) >= 12:
            # plen=12. Bytes 6-7 (LE u16) match BATTERY_LEVEL.battery_pct*10
            battery_raw = struct.unpack_from("<H", payload, 6)[0]
            battery_pct = battery_raw / 10.0
            result["event"] = "sensor_snapshot"
            if 0.0 <= battery_pct <= 100.0:
                result["battery_pct"] = round(battery_pct, 1)
        elif evt_type in (EventType.STATE_MARKER_A, EventType.STATE_MARKER_B, EventType.STATE_MARKER_C):
            result["event"] = f"state_marker_{evt_type}"
            result["payload_bits"] = payload.hex() if payload else ""
        else:
            result["event"] = f"unknown_{evt_type}"

        return result
    except (struct.error, IndexError):
        return None
