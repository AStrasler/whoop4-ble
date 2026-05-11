# Sample Session

Real packets captured from the browser monitor (`python3 -m whoop4.monitor.server`) talking to a Whoop 4.0 strap. Every example below was extracted from disk logs of `ble_monitor.py` — no synthetic or hand-crafted bytes.

Timestamps are local wall-clock. Hex is the raw BLE notification as delivered by the OS BLE stack.

---

## 1. Initial connection

After `BleakClient.connect()` succeeds, the monitor broadcasts a system event and subscribes to three characteristics:

```
[22:37:56.707] BLE_CONNECTED
  address: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX

  CMD_FROM  61080003-...  ← command responses (small, bursty)
  DATA_FROM 61080005-...  ← historical data + events + console (heavy traffic during drain)
  HR_SVC    00002A37-...  ← live HR @ ~1Hz, standard GATT Heart Rate Service
```

## 2. Live heart rate (always on, no auth needed)

The standard BLE Heart Rate Measurement characteristic streams every ~1 second. When the PPG signal is clean, RR intervals come embedded in the same notification:

```
[22:38:34.566] HR_SERVICE  (4 bytes)
  raw:     103e1904
  flags:   0x10        bit0 = HR size (0=uint8), bit4 = RR present
  hr_bpm:  62
  rr_ms:   [1024]        1/1024 sec ticks → milliseconds
```

## 3. Fixing the RTC (the 5-byte SET_CLOCK)

Sent fire-and-forget on every connect (defensive sync, like the official Whoop app). **This is the discovery this repo contributes** — 4-byte payloads are silently ignored:

```
[22:38:48.060] → SET_CLOCK  cmd=0x0A
  payload: 1778539127 as LE uint32 + 0x01 commit byte  (5 bytes total)
  iso:     2026-05-11T22:38:47+00:00
```
There is no `COMMAND_RESPONSE` for SET_CLOCK — it's fire-and-forget. Verify by sending GET_CLOCK afterward (see §5).

## 4. Starting a historical drain

`SEND_HISTORICAL_DATA` (0x16) tells the band to start streaming flash records. It produces an immediate `COMMAND_RESPONSE`, then `HISTORICAL_DATA` / `EVENT` / `METADATA` packets begin flowing over `DATA_FROM`:

```
[22:37:59.909] ← COMMAND_RESPONSE  cmd=0x16 (SEND_HISTORICAL_DATA ack)
  data: 00020b0000
```

## 5. GET_CLOCK during drain (RTC verification)

GET_CLOCK only responds while a drain is active. The response encodes the hardware RTC at offset 2 plus a 32768 Hz sub-second counter at offset 6:

```
[22:38:48.612] ← COMMAND_RESPONSE  cmd=0x0B (GET_CLOCK)
  raw data:    0001 a8e0016a 0100 050000000000      (14 bytes total)
               ──── ───────── ───── ────────────
               hdr  hw_rtc    sub   zeros (padding)

  hw_rtc:      1778507944  ← if this is ~32M, the RTC is broken (year 1971)
  iso:         2026-05-11T07:59:04+00:00  ✓ valid Unix time
  sub_second:  0.031       (from 32768 Hz hardware crystal)
```

If `hw_rtc` is ~32M instead of a 2026 Unix timestamp, the RTC is stuck. Repeat §3 (SET_CLOCK) until this returns a valid time.

## 6. The historical drain in flight

Each batch is bracketed by a `HISTORY_START` and `HISTORY_END` metadata packet. Between them, many `HISTORICAL_DATA` packets (93 bytes each, one HR sample per record) and occasional `EVENT` packets arrive:

```
[22:38:00.060] ← METADATA  HISTORY_START  (cmd=0x01)
  raw:           aa2c0052311501465a026a687e060000001000000002000000290000001100000006000000000000000806001e4e5107
  start_offset:  0x00067E68
  end_offset:    0x00100000
```

```
[22:38:00.662] ← HISTORICAL_DATA  (104 bytes)
  raw:        aa6400a12f18056da9fc0138f6016a785a805440013c01e10300000000000000207d05ff0019403c8527083f85eba43ef6584e3f0002e4468527083f85eba43ef6584e3f03025902a402590252015004010c020c000000000007000116da000000000000b009ccfa
  timestamp:  1778513464  (2026-05-11T09:31:04Z)
  subsec:     23160
  hr_bpm:     60
  rr_ms:      [993]
  accel xyz:  [0.5319, 0.3221, 0.806]  (g-force, bytes 33-44 as 3× LE float32)
```

```
[14:07:26.019] ← EVENT  type=BATTERY_LEVEL (3)
  raw:           aa2400fa307a03006262fd69303614000284030000cb1000000000311503001d00000000a4ca2c95…
  battery_pct:   90.0%
  skin_temp_c:   42.99°C
```

```
[22:38:02.610] ← METADATA  HISTORY_END  (cmd=0x02)
  raw:          aa1c00ab311602495a026ab0214b000000227300001a000000000000c1e8af02
  trim_offset:  0x00007322  ← MUST be ACK'd or band replays this batch
```

## 7. ACKing each batch (critical — easy to get wrong)

After every `HISTORY_END`, send `HISTORICAL_DATA_RESULT` (0x17) carrying the trim offset. Without this, the band keeps resending the same window forever (this was the most common bug in earlier community implementations — the trim offset lives at **byte 10** of HISTORY_END, not byte 8):

```python
trim = struct.unpack_from('<I', history_end.data, 10)[0]
ack_payload = struct.pack('<BII', 1, trim, 0)   # [status=1][trim u32][reserved 4 zeros]
pkt = WhoopPacket(PacketType.COMMAND, 0, CommandNumber.HISTORICAL_DATA_RESULT, ack_payload)
await client.write_gatt_char(CMD_TO, pkt.framed_packet())
```
```
[14:07:26.318] ← COMMAND_RESPONSE  cmd=0x17 (HISTORICAL_DATA_RESULT ack)
  data: 0001000000
```

## 8. Firmware console log (the diagnostic channel)

`CONSOLE_LOGS` packets carry the strap's internal log ring buffer. During a drain the band flushes what it has accumulated — these are the messages that reveal RTC failures, sensor errors, and drain state:

```
[22:38:00.062] : BLE: History burst success. Trim: 0x00000000:00
[22:38:00.062] 07316 (0:29462)
 18, 662800047: BLE: History burs
[22:38:00.066] 662800587: BLE: History burst success. Trim: 0x0
[22:38:00.066] 000000:0000731b (0:29467)
 18, 662801307: BLE: Hi
[22:38:00.210] 9469)
 18, 662801787: BLE: History burst success.
```
Useful patterns to grep for:

- `Flash: RTC timestamp X is invalid` — RTC is broken, run §3 (SET_CLOCK)
- `BLE: Historical Dump Complete` — drain finished cleanly
- `PullStats: Data: N, Events: M` — N=0 with the band on-wrist means flash writes are failing

## 9. End of drain

When the band has nothing more to send, you'll see `BLE: Historical Dump Complete` in console, then a `METADATA HISTORY_COMPLETE` (cmd=0x03) packet ends the stream. The final `PullStats` line summarizes what was sent:

```
BLE: Historical Dump Complete
PullStats: Data: 552, Events: 32, Bytes: 51736, Seconds: 4.812, Brate: 10755
```
| Field | Meaning |
|-------|---------|
| `Data` | HR records flushed in this drain (zero if RTC is broken — see [PROTOCOL.md](PROTOCOL.md#rtc-management)) |
| `Events` | EVENT records flushed (battery, temp, wrist transitions) |
| `Bytes` | Total bytes over BLE during the drain |
| `Seconds` | Drain duration |
| `Brate` | Effective bytes/sec over the BLE link |

---

## What this looks like in the monitor UI

Open http://localhost:8765 after `python3 -m whoop4.monitor.server`:

- **Top bar** updates live: HR, HRV (RMSSD/60s), RHR (5th percentile over 5min), Battery, Skin temp, RTC (green = valid Unix time), wrist on/off
- **Left panel**: scrolling packet stream — color-coded by type, click any row for hex + decoded view
- **Right panel**: detail view — hover a decoded field to highlight the exact bytes in the hex dump
- **Bottom panel**: live firmware console (same lines as §8 above, in real time, with a Copy button)
- **Commands**: one-click `Fix RTC Clock` (5-byte SET_CLOCK) and `Check RTC` (SEND_HIST → GET_CLOCK → decode)
- **Coverage**: GitHub-style calendar heat map of HR records per day so data gaps are immediately visible

