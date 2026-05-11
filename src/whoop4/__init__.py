"""
whoop4-ble — direct BLE access to the Whoop 4.0 strap.

No subscription required. Parses historical HR / RR-intervals / accelerometer,
fixes the firmware RTC, streams live HR over the standard GATT service.

See https://github.com/andyguzmaneth/whoop4-ble for docs.
"""
from .packet import (
    WhoopPacket,
    PacketType,
    CommandNumber,
    MetadataType,
    EventType,
    parse_historical_record,
    parse_event_packet,
    crc8,
    crc32,
)

__all__ = [
    "WhoopPacket",
    "PacketType",
    "CommandNumber",
    "MetadataType",
    "EventType",
    "parse_historical_record",
    "parse_event_packet",
    "crc8",
    "crc32",
]

__version__ = "0.1.0"
