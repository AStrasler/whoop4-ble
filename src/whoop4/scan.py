#!/usr/bin/env python3
"""
Scan for nearby Whoop 4.0 bands and print their BLE addresses.

Usage:
    python3 -m whoop4.scan              # 8-second scan
    python3 -m whoop4.scan --duration 15

Set the address you find as the WHOOP_ADDRESS env var, e.g.:
    export WHOOP_ADDRESS="XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"   # macOS UUID
    export WHOOP_ADDRESS="AA:BB:CC:DD:EE:FF"                       # Linux MAC
"""
import argparse
import asyncio
from bleak import BleakScanner

# Whoop custom service UUID is the most reliable identifier
WHOOP_SERVICE_UUID = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=8.0,
                        help="Scan duration in seconds (default: 8)")
    parser.add_argument("--all", action="store_true",
                        help="Show all BLE devices, not just suspected Whoops")
    args = parser.parse_args()

    print(f"Scanning for {args.duration}s...")
    devices_with_adv = await BleakScanner.discover(
        timeout=args.duration, return_adv=True
    )

    whoops = []
    others = []
    for addr, (device, adv) in devices_with_adv.items():
        name = (device.name or "").strip()
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_whoop = (
            WHOOP_SERVICE_UUID.lower() in uuids
            or name.upper().startswith("WHOOP")
        )
        rssi = adv.rssi if adv else None
        entry = (rssi or -999, addr, name, is_whoop)
        if is_whoop:
            whoops.append(entry)
        else:
            others.append(entry)

    whoops.sort(reverse=True)
    others.sort(reverse=True)

    if whoops:
        print(f"\nFound {len(whoops)} suspected Whoop band(s):")
        for rssi, addr, name, _ in whoops:
            print(f"  {addr}  {rssi:>4} dBm  {name or '(no name)'}")
        print("\nSet env var:  export WHOOP_ADDRESS=\"<address>\"")
    else:
        print("\nNo Whoop bands found. Tips:")
        print("  - Make sure your band is awake (not in deep sleep)")
        print("  - Unpair from the official Whoop app first (it claims exclusive access)")
        print("  - On macOS the address is a UUID; on Linux it's a MAC address")

    if args.all and others:
        print(f"\nOther BLE devices ({len(others)}):")
        for rssi, addr, name, _ in others[:20]:
            print(f"  {addr}  {rssi:>4} dBm  {name or '(no name)'}")


if __name__ == "__main__":
    asyncio.run(main())
