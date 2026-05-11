# Contributing & Development Guide

This is a reverse-engineering project, not a polished SDK. The goal of this guide is to lower the time-to-first-useful-experiment to about 10 minutes.

## What kind of contributions are most useful

In order of leverage:

1. **Decoding currently-unknown bytes.** See [`docs/STATUS.md`](docs/STATUS.md#black-box) for the precise byte ranges that need decoding. If you have a Whoop 4.0 and can capture data while doing something specific (cold exposure, breath-hold, intense workout), you can often pin down a sensor channel in one session.
2. **Probing safe cmd bytes.** The 0x60-0x7F range is unprobed. Use the patterns in `roble_research/ble_probe_hw_rtc.py` (the methodology that found the 5-byte SET_CLOCK fix) — short payloads, watch for responses, never blast random bytes at the band.
3. **Cross-platform testing.** Most development was on macOS. Bleak works on Linux/Windows but addressing semantics differ.
4. **Independent computation of derived metrics.** HRV (RMSSD), RHR, recovery proxies — the raw data is there, the math is documented in the [HRV literature](https://www.heart.org).
5. **Bug fixes & docs improvements.** Always welcome.

## Quick development loop

```bash
# 1. Clone & install
git clone https://github.com/andyguzmaneth/whoop4-ble.git
cd whoop4-ble
pip install -r requirements.txt

# 2. Find your band
python3 -m whoop4.scan
export WHOOP_ADDRESS="<your-address>"

# 3. Launch the live monitor (your main development tool)
python3 -m whoop4.monitor.server
# Open http://localhost:8765

# 4. From here you can:
#    - See every packet in real time
#    - Click "Commands → Fix RTC Clock" / "Check RTC"
#    - Trigger drains and watch the trim ACK loop
#    - See the calendar coverage heat map
```

## How the monitor helps RE

The monitor is the most important tool in this repo for decoding new bytes:

- **Color-coded packet stream**: at a glance you can see when EVENT packets appear during a drain
- **Hover-to-highlight**: in the detail panel, hovering a decoded field highlights the exact bytes in the hex dump. Inverse direction (hovering hex bytes to see what field they map to) is on the roadmap.
- **Filter chips**: temporarily hide HR_SERVICE / CONSOLE_LOGS to focus on the rare packet types you're trying to decode
- **`?UNDECODED only` filter**: shows only packets where the parser couldn't make sense of the payload — fast way to find the next decoding target
- **Console copy button**: copy the firmware log buffer for offline analysis
- **Send raw frames**: the Commands panel has a raw-hex field for sending arbitrary BLE writes — use it to test hypotheses about command formats

## Safe probing methodology

The hardware-RTC discovery came from a methodology that's worth following for any new command probing:

1. **One change at a time.** Vary either the cmd byte or the payload, never both.
2. **Read state before and after.** GET_CLOCK (offset 2) is your "barometer" for any RTC-related command. For other state, capture a baseline drain output and diff after the probe.
3. **Use short payloads first.** A 1-byte 0x00 payload triggers the command's response shape without supplying parameters that might activate a destructive code path.
4. **ABORT between probes.** After each probe, send `ABORT_HISTORICAL_TRANSMITS` (0x14) to clean up any drain state.
5. **Skip dangerous ranges.** Cmd bytes ≥ 0x80 are very likely to contain firmware-update / factory-reset commands. Don't blast these without specific reason.
6. **Avoid cmd=0x00.** Causes the firmware's BLE_SPI subsystem to spam `GENERIC CMD CRC FAILURE 4` every 600ms for hours — it pollutes the console log buffer and makes everything else harder to read.

## Code layout

```
src/whoop4/
├── __init__.py      Re-exports the public API: WhoopPacket, parse_*, CommandNumber, etc.
├── packet.py        The pure data-layer module. No I/O. Easy to unit-test.
├── scan.py          BLE discovery. `python3 -m whoop4.scan`
├── set_clock.py     Standalone diagnostic — sends SET_CLOCK and reports verification
├── drain.py         The "real" drain: multi-pass, raw capture, dedup, state file, RTC check
├── reader.py        Live HR streaming (standard GATT service)
└── monitor/
    ├── server.py    asyncio + websockets + bleak. Subscribes to all characteristics, broadcasts JSON.
    └── index.html   Single-file vanilla JS UI. No build step.
```

Add new decoders in `packet.py`. Add new tools as separate modules. The monitor exists to support RE — keep it simple and uncoupled from your specific decoding work.

## When you find something new

1. **Verify by inversion.** If you think bytes X-Y mean "skin temperature in centi-celsius", set the band on a known surface (you know your skin temp, you know room temp) and confirm the value matches.
2. **Capture multiple samples.** A single packet doesn't tell you which bytes vary by sample vs. which are constant. Capture at least 100 records under varied conditions before claiming a decode.
3. **Update [`docs/STATUS.md`](docs/STATUS.md).** Move the entry from "Black box" to "Partially understood" or "Decoded & verified" with the evidence.
4. **Update [`docs/PROTOCOL.md`](docs/PROTOCOL.md).** This is the authoritative reference. Aim for the level of detail you'd want to find if you were starting from scratch.
5. **PR with a sample**. Attach the raw packet bytes (anonymize timestamps if you want) and the decoded interpretation. Make it easy to reproduce.

## Testing

There is no automated test suite yet. The packet layer (`packet.py`) is pure — adding pytest fixtures with known-good raw bytes and expected decodes is a great first contribution.

## Code style

- Python 3.10+ (uses `dict | None`, `list[int]` etc.)
- No dependencies beyond `bleak` and `websockets`
- One short comment per non-obvious line. The WHY belongs in code; the WHAT belongs in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).
- Avoid abstraction. This is a research codebase. Inline > class hierarchy.

## Ethical notes

- This is for **your own band, on your own data**. The protocol is reverse-engineered from publicly-observable BLE traffic.
- Don't ship anything that automatically scrapes other people's bands without consent.
- Don't ship anything that could be used to brick a band — keep firmware-update commands out of automated workflows.
- The Whoop brand and product names belong to Whoop, Inc. This project is not affiliated with or endorsed by Whoop.

## Where to ask questions

Open a GitHub Discussion or an Issue. For RE coordination across the community, also check:
- [`cs-balazs/gowhoop`](https://github.com/cs-balazs/gowhoop) — most active alt implementation
- [`jogolden/whoomp`](https://github.com/jogolden/whoomp) — WebBluetooth approach
- [`project-whoopsie/whoopsie-protocol`](https://github.com/project-whoopsie/whoopsie-protocol) — protocol reference
