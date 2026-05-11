# whoop4-ble

Direct BLE access to the **Whoop 4.0 strap** — pull your own HR / RR-intervals / accelerometer data, fix the firmware RTC when it gets stuck, and stream live HR. **No Whoop subscription required.**

```
┌───────────────┐   BLE   ┌─────────────┐   JSONL   ┌──────────────┐
│  Whoop 4.0    │◄───────►│  This repo  │◄─────────►│ Your storage │
│  (your strap) │         │   (Python)  │           │              │
└───────────────┘         └─────────────┘           └──────────────┘
```

## What this is, in one minute

Whoop 4.0 records HR, RR intervals, skin temperature, and accelerometer data to internal flash continuously, and broadcasts everything over Bluetooth Low Energy. The data is yours — the protocol is just undocumented. This repo is the cleaned-up output of reverse-engineering that protocol so you can:

- Pull every HR / RR / accel sample the band has stored on flash (~14 days of circular buffer, ~86,000 records per day) into a local JSONL file
- Stream live HR + RR over the standard BLE Heart Rate Service (works with no auth)
- See battery, skin temp, and wrist on/off events as the band records them
- Fix the band's internal clock when flash writes silently stop working (the most common, hardest-to-spot failure mode)
- Use a browser-based monitor to inspect every BLE packet in real time, decoded

What you **don't** get: Whoop's cloud-side recovery / strain / sleep scores. Those are computed in their cloud. But the raw data is here, and HRV (RMSSD over RR intervals) is a one-liner.

## Why this exists

Whoop's API became paywalled when consumer subscriptions ended for some users. The strap itself doesn't know or care — it keeps recording locally and broadcasting over BLE. You just need to speak its protocol.

This repo also documents a finding that **isn't in the existing community RE projects** ([gowhoop](https://github.com/cs-balazs/gowhoop), [whoomp](https://github.com/jogolden/whoomp)): `SET_CLOCK` requires a **5-byte payload**, not 4 bytes. Without the trailing commit byte, the firmware silently accepts the write but **does not update the RTC**, causing flash writes to fail invisibly until the next valid `SET_CLOCK`. The original 4-byte form is documented in those repos and works for the official Whoop app's frequent re-sync, but in any setup that doesn't apply SET_CLOCK on every connect (e.g. a self-hosted sync running once a day), the RTC drift trap is real. Full discovery write-up in [`docs/PROTOCOL.md`](docs/PROTOCOL.md#rtc-management).

## Show me real packets

```
[22:38:34.566] HR_SERVICE  (4 bytes)
  raw:     103e1904
  flags:   0x10        bit0 = HR size, bit4 = RR present
  hr_bpm:  62
  rr_ms:   [1024]      1/1024 sec ticks → milliseconds

[22:38:48.060] → SET_CLOCK  cmd=0x0A  (the 5-byte payload)
  payload: 1778539127 as LE uint32 + 0x01 commit byte

[22:38:48.612] ← COMMAND_RESPONSE  cmd=0x0B (GET_CLOCK)
  hw_rtc:      1778507944  → 2026-05-11T07:59:04+00:00  ✓ valid
  sub_second:  0.031       (32768 Hz hardware crystal)

[22:38:00.662] ← HISTORICAL_DATA  (104 bytes, one HR record from flash)
  timestamp:  1778513464 → 2026-05-11T09:31:04Z
  hr_bpm:     60
  rr_ms:      [993]
  accel xyz:  [0.5319, 0.3221, 0.806]  (g-force)
```

Full annotated walkthrough — every packet type, every byte, real captures from the monitor — in **[`docs/SAMPLE_SESSION.md`](docs/SAMPLE_SESSION.md)**.

## Documentation

Read these in order depending on what you want to do:

| If you want to… | Read |
|------------------|------|
| **Use it** — pull your own data, set up a sync | This README's [Quickstart](#quickstart) → run the [examples/](examples/) |
| **Understand what every packet means** | [`docs/SAMPLE_SESSION.md`](docs/SAMPLE_SESSION.md) — annotated real captures |
| **Look up the protocol** (frame format, commands, byte offsets) | [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the authoritative reference |
| **Know what's still unknown** | [`docs/STATUS.md`](docs/STATUS.md) — what's decoded, what's partially understood, what's a black box |
| **Contribute or extend the RE** | [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev loop, probing methodology, what kind of help is most useful |

## Quickstart

```bash
# 0. Install (Python 3.10+)
pip install -r requirements.txt

# 1. Unpair from the official Whoop app first — it claims exclusive BLE access.

# 2. Find your band's BLE address
python3 -m whoop4.scan
#   Scanning for 8s...
#   Found 1 suspected Whoop band(s):
#     XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX   -52 dBm   (no name)

# 3. Export it
export WHOOP_ADDRESS="XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"

# 4. Fix the RTC (if flash writes have been silently failing)
python3 examples/01_fix_rtc.py

# 5. Wear the band for 5–10 min, then drain the data
python3 examples/02_drain_to_jsonl.py
#   data/hr.jsonl ← one JSON record per HR sample

# 6. Or stream live HR right now
python3 examples/03_live_hr.py
```

## What you get

| What | How | Notes |
|------|-----|-------|
| **Live HR + RR** | Standard GATT `0x180D` service | Works without any custom protocol. ~1 Hz |
| **Historical HR** | Custom drain protocol | Each record = 93 bytes: ts, HR (bpm), up to 4 RR intervals (ms), accelerometer (g) |
| **Skin temperature** | EVENT packets during drain | `TEMPERATURE_LEVEL` (0x11), `BATTERY_LEVEL` (0x03) embeds it too |
| **Battery & wrist state** | EVENT packets during drain | `BATTERY_LEVEL` (0x03), `WRIST_ON`/`WRIST_OFF` (9, 10) |
| **Whoop's recovery / strain / sleep scores** | ❌ Not available | Those are computed in their cloud. Use the raw HR/RR/accel to compute your own (HRV via RMSSD on RR is one line). |

## Repo layout

```
whoop4-ble/
├── README.md                       This file — start here
├── CONTRIBUTING.md                 How to extend the RE / probe new commands safely
├── docs/
│   ├── PROTOCOL.md                 Authoritative protocol reference
│   ├── SAMPLE_SESSION.md           Real annotated packet captures, end-to-end
│   └── STATUS.md                   What's decoded, what's pending, what's a black box
├── examples/                       Minimal single-file scripts to copy-paste
│   ├── 01_fix_rtc.py               SET_CLOCK with the 5-byte payload
│   ├── 02_drain_to_jsonl.py        Historical data → ./data/hr.jsonl
│   └── 03_live_hr.py               Live HR via standard GATT service
├── src/whoop4/
│   ├── __init__.py                 Public API
│   ├── packet.py                   Frame builder + parser (CRC8/CRC32, all packet types, record decoder)
│   ├── scan.py                     `python3 -m whoop4.scan`
│   ├── set_clock.py                Standalone SET_CLOCK utility with diagnostics
│   ├── drain.py                    Multi-pass drain with raw capture, dedup, state file
│   ├── reader.py                   Live HR reader (loops, prints, logs)
│   └── monitor/                    Browser-based live debug monitor
│       ├── server.py               Python WS + HTTP backend
│       └── index.html              Dashboard: HR chart, HRV/RHR, hex viewer, command panel, calendar heat map
├── pyproject.toml                  Installable package metadata
├── requirements.txt
└── LICENSE                         MIT
```

## Key BLE primitives (cheat sheet)

```
Service:   61080001-8d6d-82b8-614a-1c8cb0f8dcc6
  CMD_TO    61080002-...   write     send commands
  CMD_FROM  61080003-...   notify    command responses
  DATA_FROM 61080005-...   notify    historical data + events + console logs

Frame:     [SOF=0xAA] [Len, 2 LE] [CRC8] [Type] [Seq] [Cmd] [Data...] [CRC32, 4 LE]
Length    = len(payload) + 4  (payload = Type|Seq|Cmd|Data, CRC32 trails)
CRC8 poly = 0x07 over the length field
CRC32     = standard (poly 0xEDB88320, init/xor 0xFFFFFFFF) — band accepts this even though its outbound frames use a non-standard variant
```

| Cmd | Name | Payload | Notes |
|-----|------|---------|-------|
| 0x0A | SET_CLOCK | **5 bytes**: `[LE u32 unix_ts][0x01]` | Fire-and-forget. **4-byte payload silently ignored — this is the trap.** |
| 0x0B | GET_CLOCK | 1 byte `0x00` | Only responds during drain context. Returns `[00 01][LE u32 hw_rtc][LE u16 subsec_32768Hz][6× 00]` |
| 0x14 | ABORT_HISTORICAL_TRANSMITS | – | Cancels an active drain |
| 0x16 | SEND_HISTORICAL_DATA | `0x00` | Starts the drain. Also activates the band's command processor for subsequent commands. |
| 0x17 | HISTORICAL_DATA_RESULT | `[01][LE u32 trim][00 00 00 00]` | ACK each METADATA HISTORY_END or the band replays the same batch |
| 0x21 | SET_READ_POINTER | – | Rewind without deleting data |
| 0x22 | GET_DATA_RANGE | – | Returns current trim offset and batch range |
| 0x4C | (GET_DEVICE_NAME) | – | Returns ASCII "WHOOP AGT" (Advanced Generation Tracker) |

Full table — including event types, the 93-byte historical record layout, and the historical drain state machine — in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## The RTC trap (this is the important bit)

If a drain returns `PullStats: Data: 0, Events: N` even though you've been wearing the band, the firmware RTC is probably stuck. The firmware emits this in CONSOLE_LOG packets during drain:

```
Flash: RTC timestamp 32154354 is invalid; not saving data to flash.
```

The value is the hardware RTC counter. The validity check rejects anything that isn't a plausible Unix timestamp (> year ~2020). When the RTC is in this state, flash writes silently no-op — **your data gap accumulates invisibly** until the next sync shows zero new records.

The fix is `SET_CLOCK` with a 5-byte payload. The 4-byte payload documented in [gowhoop](https://github.com/cs-balazs/gowhoop/blob/main/client/commands.go) and [whoomp](https://github.com/jogolden/whoomp) is accepted by the BLE stack but **never applied by the firmware**. We confirmed empirically that any byte value at position 4 works — the firmware appears to only check `payload_len >= 5`. We use `0x01` by convention.

```python
# Wrong (silently ignored)
payload = struct.pack("<I", int(time.time()))

# Right
payload = struct.pack("<I", int(time.time())) + b"\x01"
```

After applying it, `GET_CLOCK` (offset 2 of the response) flips from `~32M` (year 1971) to the current Unix timestamp. Records then accumulate normally.

## The browser-based monitor

`src/whoop4/monitor/` is a development tool that's been invaluable for further RE:

- Live HR chart + HRV (rolling 60s RMSSD) + RHR (5th percentile over 5min)
- Calendar heat map of data coverage (makes gaps visible at a glance)
- Hex viewer with hover-to-highlight on decoded fields
- Command panel with one-click "Fix RTC Clock" and "Check RTC" buttons
- Real-time console log from the band's firmware

```bash
python3 -m whoop4.monitor.server
# Open http://localhost:8765
```

## Related work

- [`cs-balazs/gowhoop`](https://github.com/cs-balazs/gowhoop) — Go implementation, commands & framing
- [`jogolden/whoomp`](https://github.com/jogolden/whoomp) — WebBluetooth in-browser RE
- [`bWanShiTong/reverse-engineering-whoop-strap`](https://github.com/bWanShiTong/reverse-engineering-whoop-strap) — original RE notes on packet format
- [`project-whoopsie/whoopsie-protocol`](https://github.com/project-whoopsie/whoopsie-protocol) — protocol documentation

This repo extends those with:
- The **5-byte SET_CLOCK payload** discovery
- The **two-clocks model** (event clock vs flash-write RTC are independent registers)
- A **fully decoded GET_CLOCK response** (subsec counter ticks at 32768 Hz)
- A **working multi-pass drain** with correct trim ACK offset (`<I` at byte 10 of HISTORY_END, not byte 8)
- The **GET_DEVICE_NAME (0x4C)** discovery
- A complete browser-based debug monitor

## Caveats

- macOS: the BLE address is a 128-bit UUID. Linux: it's a 48-bit MAC. They're not interchangeable.
- The official Whoop app holds an exclusive BLE pairing — **unpair from your phone first** or `bleak` will fail to connect.
- The official app sends `SET_CLOCK` on every connect. If you keep using the official app intermittently, you may never have hit the RTC trap.
- This is informational/educational. You're responsible for your own band. The 5-byte payload was confirmed across multiple sub-flag values (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x0F, 0x10, 0x7F, 0x80, 0xFF) without any observed adverse effects, but probe higher cmd bytes (>0x60) at your own risk — that range likely contains firmware-update / factory-reset commands.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome. See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for the dev loop, probing methodology (the same one that found the 5-byte SET_CLOCK fix), and what kind of help is most useful.

A short list of high-leverage open problems:
- Decode bytes 24-32 and 45-92 of the 93-byte HISTORICAL_DATA record (likely more sensor channels — see [STATUS.md](docs/STATUS.md#black-box))
- Decode EVENT types 11, 12, 16, 29, 32, 101, 102, 103
- Probe cmd bytes 0x60-0x7F carefully (a vibration trigger seems to live in 0x43-0x4D; firmware-update commands likely live above 0x80)
- Linux / Windows testing — most development happened on macOS
