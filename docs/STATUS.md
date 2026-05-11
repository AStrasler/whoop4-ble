# Reverse Engineering Status

A frank ledger of what's solved, what's partially understood, and what's still a black box. Last updated **2026-05-11**.

## Decoded & verified ✅

### Frame format
- SOF, length, CRC8 header, payload framing, CRC32 trailer — all confirmed against [`bWanShiTong/reverse-engineering-whoop-strap`](https://github.com/bWanShiTong/reverse-engineering-whoop-strap)
- Payload header `[Type][Seq][Cmd][Data…]`
- Standard CRC32 (poly 0xEDB88320, init/xor 0xFFFFFFFF) is accepted on writes even though the band's outbound frames use a non-standard variant (init 0x54764834, xor 0x0FFA5F26) — leniency confirmed empirically

### Commands
| Cmd | Name | Payload | Verified by |
|-----|------|---------|-------------|
| 0x0A | **SET_CLOCK** | `[LE u32 unix_ts][0x01 commit]` (5 bytes) | This repo — see [PROTOCOL.md §RTC management](PROTOCOL.md#rtc-management). The 4-byte payload documented elsewhere is silently dropped. |
| 0x0B | GET_CLOCK | 1 byte (any value) | Returns 14-byte response, fully decoded |
| 0x14 | ABORT_HISTORICAL_TRANSMITS | – | Cancels active drain |
| 0x16 | SEND_HISTORICAL_DATA | `0x00` | Starts drain + activates command processor |
| 0x17 | HISTORICAL_DATA_RESULT | `[1][trim u32][4× 0]` | ACK each batch or band replays |
| 0x21 | SET_READ_POINTER | – | Rewind without deleting |
| 0x22 | GET_DATA_RANGE | – | Returns trim offset + batch range |
| 0x4C | (GET_DEVICE_NAME) | – | Returns ASCII "WHOOP AGT" |

### Packet types
| Type | Name | What we do with it |
|------|------|--------------------|
| 0x23 | COMMAND | Outbound only |
| 0x24 | COMMAND_RESPONSE | Decoded with per-cmd dispatch (GET_CLOCK fully parsed) |
| 0x28 | REALTIME_DATA | Pass-through hex (rarely seen in practice) |
| 0x2B | REALTIME_RAW_DATA | Pass-through hex |
| 0x2F | HISTORICAL_DATA | 93-byte record, partial decode (see below) |
| 0x30 | EVENT | Sub-typed (battery, temp, wrist, sensor snapshot, state markers) |
| 0x31 | METADATA | HISTORY_START / HISTORY_END / HISTORY_COMPLETE fully decoded |
| 0x32 | CONSOLE_LOGS | ASCII firmware log, treated as opaque text stream |

### EVENT types (`pkt.cmd` of an EVENT packet)
| Value | Name | Status |
|-------|------|--------|
| 3 | BATTERY_LEVEL | Fully decoded: `battery_pct`, embedded `skin_temp_c` |
| 9 | WRIST_ON | Boundary marker |
| 10 | WRIST_OFF | Boundary marker |
| 14 | DOUBLE_TAP | Boundary marker |
| 17 | TEMPERATURE_LEVEL | Fully decoded: int16 / 10.0 = °C |
| 63 | EXTENDED_BATTERY_INFORMATION | Named, partially decoded (voltage / current bytes still unverified) |

### HISTORICAL_DATA record (93 bytes)
| Offset | Field | Status |
|--------|-------|--------|
| 0-3   | Sequence counter (LE u32) | ✅ |
| 4-7   | Unix timestamp (LE u32) | ✅ |
| 8-9   | Sub-second tiebreaker (LE u16) | ✅ |
| 10-13 | Flags / state | ✅ |
| 14    | Heart rate (BPM) | ✅ |
| 15    | RR interval count (0-4) | ✅ |
| 16-23 | Up to 4 RR intervals (LE u16, ms each) | ✅ |
| 33-44 | Accelerometer X/Y/Z (3× LE float32, g-force) | ✅ confirmed (rest at ~1g, motion produces sane Δ) |

## Partially understood 🟡

### EVENT types we see but haven't decoded
| Value | Observed payload | Hypothesis |
|-------|------------------|------------|
| 29 (0x1D) | 12 bytes, contains battery_pct bits | Periodic sensor snapshot — bytes 6-7 match BATTERY_LEVEL.battery_pct |
| 32 (0x20) | 32 bytes, trailing float32 ≈ 0.06 | Possibly calibration or accelerometer summary |
| 11 (0x0B), 12 (0x0C), 16 (0x10) | Small (<20 bytes) | Activity/sleep state transitions? |
| 101, 102, 103 | 4 bytes — `01 00 00 00`, `01 01 00 00`, `01 01 01 00` | Look like sequential state markers (sleep stage transitions?) |

### GET_CLOCK response bytes 8-13
14-byte response is decoded for offsets 0-7 (status, flag, hw_rtc, sub_second). Bytes 8-13 are always six `0x00` in every capture so far. Could be reserved, could be a future field — unverified.

### SEND_HISTORICAL_DATA response (`00 02 0b 00 00`)
The `0x02` second byte is constant. The `0x0B` looks suspiciously like the GET_CLOCK opcode — possibly indicates "GET_CLOCK is available in this drain context". Trailing zeros unexplained.

### Cmd bytes 0x43-0x4D
Most respond with simple status-prefixed bytes (`0001…`). Probable get-info / device-state queries. The 0x4D response has a `0002` prefix (unusual) and may be a trigger command — vibration was observed during probing of this range without controls on which exact byte.

## Black box 🔴

### HISTORICAL_DATA bytes 24-32 and 45-92
- **Bytes 24-32 (9 bytes)** — varying byte patterns, possibly flags + sensor status bits
- **Bytes 45-48 (1 float)** — one sample saw value `11265.0` (out of range for any known sensor unit, possibly PPG raw or sensor temperature in unusual units)
- **Bytes 49-60 (3× float32)** — values often *mirror* bytes 33-44 exactly; could be a filtered/smoothed accel copy, or a second sample within the same second
- **Bytes 61-92 (~32 bytes)** — completely unexplored. Likely contains:
  - Skin temperature stream (per-record, not just events)
  - SpO2 (Whoop 4.0 has a pulse oximeter)
  - Gyroscope (Whoop 4.0 has one)
  - PPG signal quality metrics

### Cmd bytes 0x60-0xFF
Unprobed. This range likely contains:
- Firmware update commands (dangerous to probe blindly)
- Factory reset
- Pairing / authentication
- LED control
- Vibration / haptic control

### The two-clock architecture
We have two confirmed clocks (event clock vs flash-write RTC) but the firmware-level relationship between them is unclear. SET_CLOCK definitely updates the hardware RTC. Whether it also updates the event clock — or whether the event clock derives from the hardware RTC — is unverified.

### How the official Whoop app authenticates / pairs
The custom service appears to be accessible without authentication once paired at the OS level. There may be a higher-level auth handshake that unlocks additional commands (firmware update probably requires this). Out of scope for this repo.

### Cloud-side scores
Recovery, strain, sleep staging, and journal correlations are computed in Whoop's cloud — not derivable from the raw data alone. The community has [made progress on independent recovery models](https://github.com/jogolden/whoomp) (HRV-based) but the proprietary fusion is closed.

---

## High-leverage next steps for contributors

In rough order of impact-per-effort:

1. **Decode HISTORICAL_DATA bytes 61-92.** Even partial decoding (one more sensor channel) is valuable. Approach: capture data with the band in known states (high motion, hand under cold water, hold breath for SpO2 dip) and look for byte ranges that correlate with the expected physiological response.
2. **Decode EVENT types 29 / 101 / 102 / 103.** Watch for these around known activity boundaries (start sleep, start workout) — they're sparse enough that wall-clock correlation is feasible.
3. **Map cmd bytes 0x60-0x7F.** Probe one byte at a time with small (1-byte) payloads, watching for COMMAND_RESPONSE shape and side effects. Skip 0x80+ where firmware-update commands likely live.
4. **Linux & Windows testing.** Most development happened on macOS (Bleak). Bleak supports all three but BLE addressing differs (UUID vs MAC) and pairing semantics vary.
5. **HRV model.** RR intervals are decoded and timestamped. RMSSD/SDNN are trivial. A reasonable nightly-RHR proxy from this data would let people compute their own recovery score.

---

## Confidence levels

- **High** (production-safe): Frame format, all confirmed commands, HISTORICAL_DATA bytes 0-23 and 33-44, BATTERY/TEMPERATURE events, METADATA, the drain ACK loop.
- **Medium**: GET_CLOCK response layout (bytes 0-7 verified across many captures, bytes 8-13 observed-zero), accel bytes 49-60.
- **Low** (use at your own risk): cmd byte semantics for 0x43-0x4D, EVENT types 11/12/16/29/32/101/102/103.
- **None** (do not assume): everything in the "Black box" section.
