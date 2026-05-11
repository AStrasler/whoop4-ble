---
title: "Whoop 4.0 BLE Protocol — Reference & Learnings"
type: technical-reference
date: 2026-04-12
status: active
tags: [health-coach, whoop, ble, reverse-engineering]
---

# Whoop 4.0 BLE Protocol — Reference & Learnings

Direct-from-band data extraction via Bluetooth LE. Replaces the Whoop API path after subscription ended 2026-04-11.

*Last updated: 2026-05-10*

## Why this exists

Andy's Whoop subscription was cancelled 2026-04-11. The API-based `whoop-sync.py` no longer works (cloud data is paywalled). But the band itself still records HR, RR intervals, and skin temp locally and broadcasts them over BLE. This doc captures what we reverse-engineered so future syncs stay reliable.

Primary references:
- [jogolden/whoomp](https://github.com/jogolden/whoomp) — WebBluetooth reverse engineering
- [bWanShiTong research](https://github.com/bWanShiTong/reverse-engineering-whoop-strap) — packet format

## How it fits into Health Coach

```
Whoop 4.0 band (on wrist, stores data locally)
      │ BLE
      ▼
scripts/health/ble_sync.py  ─── full historical dump (run 1-2x/week)
scripts/health/ble_reader.py ── live HR streaming (standard 0x180D service)
      │
      ▼
data/hr.jsonl (append-only, deduped by timestamp)
      │
      ▼
your data store
      │
      ▼
apps/health-pwa + health_coach.py processing → HRV, RHR, strain, recovery
```

Related projects:
- **Medicine 3.0** (`personal/health/medicine-3.0/`) — the knowledge/protocol layer this data feeds
- **Health Coach** (`personal/health/HEALTH-COACH-ARCHITECTURE.md`) — the umbrella system (6 layers)
- **Roble** — your DB = the LDL Supabase instance where cloud HR lives in the `hc` schema

## Capacity findings (measured 2026-04-12)

The band is a much better offline logger than we assumed.

- **Sample rate:** ~1 Hz for HR (≈86,000 records/day)
- **Observed storage:** 8+ full days pulled in one drain session (Apr 4 partial → Apr 12)
- **Likely max:** 14+ days based on community reports, possibly up to 3 weeks
- **Behavior:** circular buffer on internal flash. Data accumulates until a BLE client ACKs the trim pointer (see protocol below). Without ACKs the buffer keeps growing.

**Practical rule:** run a BLE sync every 1-2 weeks and no data is lost. The band is effectively self-sufficient storage — the phone app (or our script) is just a drain mechanism.

**What we lose vs the subscription:**
| Lost | Kept (raw from band) |
|------|----------------------|
| Whoop's recovery score | Raw HR, RR intervals (compute our own HRV/RHR) |
| Sleep staging (REM/SWS/light) | Skin temp at sleep boundaries |
| Strain score | Workout HR zones (derive from raw) |
| Journal correlations | Everything we log ourselves |
| Coaching prompts | — |

We keep the substrate, lose the interpretation layer. `scripts/health/health_coach.py` rebuilds interpretation from raw.

## BLE characteristics

Whoop custom service: `61080001-8d6d-82b8-614a-1c8cb0f8dcc6`

| UUID | Direction | Purpose |
|------|-----------|---------|
| `61080002-...` | write | `CMD_TO_STRAP` — send commands to band |
| `61080003-...` | notify | `CMD_FROM_STRAP` — command responses & metadata |
| `61080005-...` | notify | `DATA_FROM_STRAP` — historical data packets |

Standard services also exposed:
- `0x180D` Heart Rate Service — works **without subscription**, live HR stream (used by `ble_reader.py`)

## Frame format

```
[SOF(1)=0xAA] [Length(2, LE)] [CRC8(1)] [Payload(N)] [CRC32(4, LE)]
```

- `Length` = payload length + 4 (CRC32)
- `CRC8` = polynomial `0x07`, over the length field
- `CRC32` = standard (polynomial `0xEDB88320`, init/xor `0xFFFFFFFF`, reflected)

Payload header: `[Type(1)] [Seq(1)] [Cmd(1)] [Data...]`

### Packet types

| Value | Name |
|-------|------|
| `0x23` | COMMAND |
| `0x24` | COMMAND_RESPONSE |
| `0x28` | REALTIME_DATA |
| `0x2B` | REALTIME_RAW_DATA |
| `0x2F` | HISTORICAL_DATA |
| `0x30` | EVENT |
| `0x31` | METADATA |
| `0x32` | CONSOLE_LOGS |

### Command numbers

| Value | Name | Context | Notes |
|-------|------|---------|-------|
| `0x0A` | SET_CLOCK | Standalone | **5-byte payload: `[LE uint32 Unix ts][0x01 commit flag]`**. Fire-and-forget. 4-byte payload is silently ignored. |
| `0x0B` | GET_CLOCK | Drain only | **1-byte payload required** (any value, e.g. `0x00`). Empty payload yields no response. Returns 14-byte data with hw RTC at offset 2 (LE uint32). |
| `0x14` | ABORT_HISTORICAL_TRANSMITS | Standalone | Stop an active drain. |
| `0x16` | SEND_HISTORICAL_DATA | — | Starts historical drain. Also activates the band's command dispatcher for subsequent commands. |
| `0x17` | HISTORICAL_DATA_RESULT | Drain | ACK each batch. Required to advance trim pointer. |
| `0x21` | SET_READ_POINTER | Drain | Rewind read pointer without deleting data. |
| `0x22` | GET_DATA_RANGE | Drain | Returns current trim offset and batch range. |
| `0x43` | unknown | Drain | Responds. Data `00010100000000000000040020`. May trigger haptic. |
| `0x44` | unknown | Drain | Responds. Data `000101000000000000`. |
| `0x46-0x48` | unknown | Drain | Respond with `0001000000` (status-only get-info commands). |
| `0x49` | unknown | Drain | Responds. Data `0001090020`. |
| `0x4A` | unknown | Drain | Responds. Data `0001000f20`. |
| `0x4B` | unknown | Drain | Responds. Data `0001010000`. |
| `0x4C` | GET_DEVICE_NAME | Drain | Returns device model name as ASCII. Data: `0001 0101 0a 57484f4f502041475400000000000000` → "WHOOP AGT" |
| `0x4D` | unknown (trigger?) | Drain | Status prefix `0002` (unusual — usually `0001`). Suspected vibration trigger when probed without parameters. |

**Command routing:** The band only sends `COMMAND_RESPONSE` packets after `SEND_HISTORICAL_DATA` has been sent in the current session. Standalone writes (before drain) go unacknowledged — but fire-and-forget commands like `SET_CLOCK` still execute silently. Do not use `COMMAND_RESPONSE` as the sole success signal.

### Metadata sub-types

| Value | Name |
|-------|------|
| `1` | HISTORY_START |
| `2` | HISTORY_END (batch boundary — carries trim value) |
| `3` | HISTORY_COMPLETE (full dump finished) |

## Historical data flow

1. Client writes `COMMAND SEND_HISTORICAL_DATA` (`0x23 00 0x16 0x00`) to `CMD_TO_STRAP`.
2. Band starts streaming `HISTORICAL_DATA` (`0x2F`) packets over `DATA_FROM_STRAP`.
3. Every few hundred packets, band sends `METADATA HISTORY_END` with a **trim value**.
4. Client MUST ACK with `COMMAND HISTORICAL_DATA_RESULT` (`0x17`) carrying that trim, or the band **replays the same batch**.
5. Repeat until `METADATA HISTORY_COMPLETE`.

### ACK format

9-byte payload: `[status(1)=1] [trim(4, LE)] [reserved(4)=0]` — built with `struct.pack("<BII", 1, trim, 0)`.

## Historical record layout (93-byte packets)

| Offset | Size | Field |
|--------|------|-------|
| 0-3 | 4 | Sequence counter (LE) |
| 4-7 | 4 | **Unix timestamp (LE)** |
| 8-9 | 2 | Sub-second counter (LE) |
| 10-13 | 4 | Flags/state |
| 14 | 1 | **Heart rate (BPM)** |
| 15 | 1 | Flags |
| 16+ | N | Float-packed sensor data (accel / gyro / temp) |

Skin temperature appears as a float in the 25-42°C range, typically at offsets 46/50/54 — scan these and take the first plausible value.

## Packet field naming: Python vs Swift

Two RE implementations use different names for the same bytes. Mapping:

| Wire byte | Python (`whoop_packet.py`) | Swift (`WhoopPacket.swift`) |
|-----------|---------------------------|------------------------------|
| `r[4]` payload[0] | `type` (e.g. 0x23 = COMMAND) | `counter_lo` |
| `r[5]` payload[1] | `seq` | `command` (sub-operation) |
| `r[6]` payload[2] | `cmd` (e.g. 0x16 = SEND_HISTORICAL_DATA) | `category` |

The band echoes `cmd`/`category` in `COMMAND_RESPONSE` (not `seq`/`command`). So `cmd=0x16` in a response means "responding to category 0x16". The `seq`/`command` byte is a sub-operation within the category — the band appears not to validate it for known categories.

**CRC32 variant:** Python uses standard CRC32 (init=XOR=`0xFFFFFFFF`). The band's own packets use a non-standard variant (init=`0x54764834`, xor=`0x0FFA5F26`) derived via crcbeagle. The band appears to accept standard CRC32 on inbound writes (lenient validation or no validation on receive).

## RTC management

### The 5-byte payload discovery (2026-05-11)

**Critical finding:** SET_CLOCK (0x0A) requires a **5-byte payload**, not 4 bytes as previously documented in community sources (gowhoop, whoomp). The correct format is:

```
[byte 0..3] LE uint32  Unix timestamp (seconds since epoch)
[byte 4]    uint8      0x01 = commit flag (REQUIRED)
```

With a 4-byte payload (only the timestamp, no commit flag), the firmware silently accepts the BLE write but **does not update the RTC**. No COMMAND_RESPONSE either way (fire-and-forget). We spent significant time chasing alternative commands before discovering this payload requirement empirically.

```python
# CORRECT
payload = struct.pack("<I", int(time.time())) + b"\x01"

# WRONG (silently ignored)
payload = struct.pack("<I", int(time.time()))
```

Verified by: applying SET_CLOCK with 5-byte payload jumped GET_CLOCK response from `32,193,226` (~year 1971, invalid) to `1,778,507,869` (= 2026-05-11T13:57 UTC, valid Unix time).

### SET_CLOCK byte 4 is a size guard, not a commit flag

Empirically tested with byte 4 values `0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x0F, 0x10, 0x7F, 0x80, 0xFF` — **all of them** make SET_CLOCK take effect. The firmware appears to only check `payload_len >= 5`, not the content of byte 4. So:
- 4-byte payload → silently rejected (size check fails)
- 5+ byte payload → accepted, bytes 0-3 read as LE uint32 Unix timestamp

We use `0x01` for the suffix by convention.

### GET_CLOCK response format (fully decoded)

GET_CLOCK (0x0B) requires a **1-byte payload** (any value, conventionally `0x00`) and only responds when a drain is active (`SEND_HISTORICAL_DATA` must have been sent first). Empty `b""` payload yields no response.

Response data (14 bytes):
| Offset | Size | Field |
|--------|------|-------|
| 0 | 1 | `0x00` status |
| 1 | 1 | `0x01` flag (always) |
| 2-5 | 4 | **`hw_rtc`** — LE uint32 Unix seconds. The flash-write validity check uses this. |
| 6-7 | 2 | **`sub_second`** — LE uint16, 32768 Hz ticks within current second (range 0-32767, maps to 0.0-1.0s) |
| 8-13 | 6 | Six zero bytes (padding) |

The 32768 Hz tick rate matches a standard hardware RTC crystal frequency, confirming this comes directly from the band's RTC chip.

### Background

The band's HR flash write RTC can reset to 0 after a hard reset or deep discharge. With a low counter (~32M ≈ ~1 year of ticks from epoch 0), the firmware validity check fails:

```
Flash: RTC timestamp 32154354 is invalid; not saving data to flash.
```

This silently stops all HR/HRV flash writes. The data gap accumulates invisibly until a drain shows `PullStats: Data: 0`.

**Verification signal:** Run a drain (`SEND_HISTORICAL_DATA`) and watch CONSOLE_LOG packets. If RTC is broken, `Flash: RTC timestamp X is invalid` errors appear within 3 seconds (errors fire at ~1 Hz when band is on wrist). If absent, RTC is correct.

**False-negative trap:** If the band is NOT on wrist during verification, flash writes are never attempted, so no errors appear — giving a false "all clear". Always verify with band on wrist.

### Fix

```python
# SET_CLOCK (0x0A): send current Unix timestamp, fire-and-forget
pkt = WhoopPacket(PacketType.COMMAND, 0, 0x0A, struct.pack("<I", int(time.time()))).framed_packet()
await client.write_gatt_char(CMD_TO, pkt, response=True)
```

Or from the monitor UI: open **Commands → Fix RTC Clock** (sends SET_CLOCK with server-side timestamp).

Or via script:
```bash
python3 examples/ble_set_clock.py
```

**After sending:** Immediately run a drain while wearing the band. Watch console logs — if `Flash: RTC timestamp X is invalid` errors have stopped, SET_CLOCK worked. Then use **Check RTC** button (SEND_HISTORICAL_DATA → GET_CLOCK) to see the new RTC value.

### GET_CLOCK verification

GET_CLOCK (0x0B) returns the current RTC value. It only produces a `COMMAND_RESPONSE` after `SEND_HISTORICAL_DATA` has been sent. The monitor's **Check RTC** button automates this sequence and displays the result in the topbar RTC field.

Response format: 4-byte LE uint32 Unix timestamp. If value > 1,577,836,800 (year 2020), the RTC is valid.

### Why the official app never has this problem

The Whoop app sends `SET_CLOCK` on every connect (before drain). Our scripts should do the same.

## ⚠️ Critical bug we fixed

**Symptom:** BLE sync pulls ~300 records and then loops forever receiving the same batch.

**Root cause:** wrong offset for the trim value inside the `HISTORY_END` metadata payload.

- ❌ Old code: `struct.unpack_from("<IHHI", pkt.data, 0)` → reads trim at byte offset 8
- ✅ Correct: `struct.unpack_from("<I", pkt.data, 10)` → trim is at byte offset **10**

Confirmed against `whoomp.js`: `dataView.getUint32(10, true)`.

If trim is wrong, the band keeps sending the same 47-second window and we write the same records repeatedly (dedup silently absorbs them → looks like "no new data").

**Files fixed:** `/tmp/ble_drain_all.py` uses the correct offset. `ble_sync.py` still has the buggy code as of 2026-04-12 and needs the same fix applied.

## Console log messages reference

These appear in `CONSOLE_LOG` (ptype=0x32) packets on `DATA_FROM_STRAP`. Useful for diagnosing band state during RE.

| Message | Meaning |
|---------|---------|
| `Flash: RTC timestamp X is invalid; not saving data to flash.` | RTC is broken (X ≪ Unix epoch). Fix with SET_CLOCK. Appears within ~3s of connecting if broken. |
| `BLE: Command Send Historical Data` | Band received SEND_HISTORICAL_DATA. |
| `BLE: Enabled Entry.` | Normal initialization on every SEND_HISTORICAL_DATA. Not command-specific. |
| `BLE: Codemem OK` | Normal. Part of drain init. |
| `BLE: R10+R11 data packet transmission default` | Normal. Part of drain init. |
| `BLE: Historical Dump Complete` | Drain finished (all batches sent). |
| `BLE: Hist pull too short for valid stats` | Drain had too few records to compute stats. Normal when band has little data. |
| `BLE SPI: GENERIC CMD CRC FAILURE 4` | **Danger.** Caused by sending cmd=0x00. Loops every 600ms, floods console. Self-clears after a few minutes. Do not probe cmd=0x00. |
| `BLE: Phone connected: 1` | Band sees our connection. |
| `BLE: MFLT_DATA_SEND_SIG retry X but did not send` | Memfault crash telemetry failing — expected without official Whoop app. |
| `BLE: Unknown payload contents type (0x00)` | Firmware rejects historical records with type=0x00 (empty slots from failed flash writes). |
| `PullStats: Data: 0, Events: N, Bytes: B, Seconds: S` | End-of-drain summary. `Data: 0` means no HR records (usually RTC broken during accumulation). |
| `IQS7223B: Error reported, error code 0x05` | Capacitive touch sensor (CAPSENSE) error. Normal if band not on wrist. |

Firmware log line format: `<level>, <ms_since_boot>: <module>: <message>`

## Scripts in this directory

| Script | Purpose | When to run |
|--------|---------|-------------|
| `ble_drain.py` | Historical dump with trim ACK (advances trim pointer) | Weekly / bi-weekly |
| `ble_set_clock.py` | Apply SET_CLOCK (0x0A) to fix broken RTC | When "RTC invalid" errors appear in drain |
| `ble_drain_verbose.py` | Drain showing all packet types + CONSOLE_LOGS | Debugging firmware state |
| `ble_reader.py` | Live HR streaming via standard 0x180D service | When actively training |
| `ble_monitor.py` | Real-time band monitor feeding ble_monitor.html | Live debugging in browser |
| `ble_diag.py` | Packet-level diagnostics | Debugging protocol issues |
| `ble_raw_dump.py` | Raw byte capture for reverse engineering | New firmware / unknown packets |
| `ble_probe_during_drain.py` | Drain + blind cmd probe, watches for COMMAND_RESPONSE | RE new commands |
| `ble_probe_subcmd.py` | Probe sub-commands within categories (0x0e/0x03) | RE device control commands |
| `whoop_packet.py` | Frame builder + parser (CRC8/CRC32, packet types, record layout) | Imported by all ble_* scripts |
| `ble_sync.py` | **Older drain** — needs trim-offset fix (use ble_drain.py instead) | — |
| `_archive/whoop-sync.py` | **DEPRECATED** — Whoop cloud API path | Dead since 2026-04-11 |
| `health_coach.py` | Process hr.jsonl into HRV/RHR/strain/recovery | After each sync |

## Sync operational notes

- macOS: if connection fails with "Peer removed pairing information", unpair the band from the Whoop phone app first.
- A full multi-day drain takes ~3-4 hours at ~3,700 records/min when band is actively streaming.
- The drain may pause at day boundaries while the band transitions internal segments — multi-pass with reconnection (as `/tmp/ble_drain_all.py` does) handles this cleanly.
- Dedup is by `ts` string in `data/hr.jsonl` — safe to re-run without duplicates.

## Cloud push

Target: `hc.hr_readings` in Supabase (any Postgres / Supabase).

Schema:
```sql
user_id UUID   -- <your-user-id>
ts      TIMESTAMPTZ
hr      INT    -- nullable (sensor-only rows exist)
rr_ms   NUMERIC(7,2)[]
```

Dedup on `(user_id, ts)`. Use batch insert with `ON CONFLICT DO NOTHING`.

**Do NOT create a new schema.** All cloud health data lives in the existing `hc` schema alongside `hc.sleep_entries`, `hc.workouts`, `hc.body_metrics`, `hc.lab_results`, `hc.supplement_log`, `hc.recovery_entries`.
