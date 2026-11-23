"""Wire format of the Audeze Maxwell control channel.

Nothing in this module touches hardware; it only builds and parses bytes, so it is fully
unit-testable.

Sources (see README "Where the protocol comes from"):
  * HeadsetControl, lib/devices/audeze_maxwell_device.hpp (Maxwell + Maxwell 2), which states it
    was recovered from Audeze.exe and a USB capture of the original Maxwell dongle.
  * audezeforlinux, docs/PROTOCOL.md, which marks each item CONFIRMED or HYPOTHESIS.

Only items that both sources agree on, or that one marks as confirmed on Maxwell 2 hardware,
are used here.

HID level
---------
Reports are 62 bytes. Host->device is output report 0x06, device->host is input report 0x07:

    <report id> <fragment length 0..59> <route> <fragment ...> <zero padding>

route 0x80 reaches the headset through the wireless dongle; 0x00 talks to the device the
report is sent to (the dongle itself, or the headset when it is connected by USB-C).

Message level (Airoha "RACE" framing)
-------------------------------------
Fragments are concatenated into a stream of messages. One report can carry several messages
and one message can span several reports:

    05 <type> <body length, LE16> <body ...>

type: 5A request, 5B response, 5C notification, 5D asynchronous result.
The first two body bytes identify the command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

REPORT_SIZE = 62
REPORT_ID_OUT = 0x06
REPORT_ID_IN = 0x07
FRAGMENT_OFFSET = 3
MAX_FRAGMENT = REPORT_SIZE - FRAGMENT_OFFSET  # 59

ROUTE_LOCAL = 0x00
ROUTE_HEADSET = 0x80

MARKER = 0x05
TYPE_REQUEST = 0x5A
TYPE_RESPONSE = 0x5B
TYPE_NOTIFICATION = 0x5C
TYPE_RESULT = 0x5D
HEADER_SIZE = 4
MAX_MESSAGE_SIZE = 1024

TYPE_NAMES = {
    TYPE_REQUEST: "request",
    TYPE_RESPONSE: "response",
    TYPE_NOTIFICATION: "notification",
    TYPE_RESULT: "result",
}

STATUS_OK = 0x00

# Command identities: the first two body bytes.
CMD_WRITE_KEY = bytes((0x00, 0x09))
CMD_READ_KEY = bytes((0x01, 0x09))
CMD_WRITE_PARAM = bytes((0x82, 0x2C))
CMD_READ_PARAM = bytes((0x83, 0x2C))
CMD_BATTERY = bytes((0xD6, 0x0C))
CMD_FIRMWARE = bytes((0x07, 0x1C))

# Keys of the 01 09 (read) / 00 09 (write) family.
KEY_EQ_PRESET = 0x00
KEY_VOICE_PROMPT_LEVEL = 0x20
KEY_MIC_MUTE = 0x22
KEY_NOISE_FILTER = 0x24
KEY_SIDETONE_LEVEL = 0x2C
KEY_SIDETONE_MAX_LEVEL = 0x2D

# Parameters of the 83 2C (read) / 82 2C (write) family.
PARAM_AUTO_POWER_OFF = 0x01
PARAM_SIDETONE_ENABLED = 0x07
PARAM_CHATMIX = 0x0B


class ProtocolError(Exception):
    """A reply was malformed or reported a failure status."""


@dataclass(frozen=True)
class Message:
    type: int
    body: bytes

    @property
    def command(self) -> bytes:
        return self.body[:2]

    def to_bytes(self) -> bytes:
        return build_message(self.type, self.body)

    def describe(self) -> str:
        name = TYPE_NAMES.get(self.type, f"type 0x{self.type:02X}")
        return f"{name:<12} {self.body.hex(' ').upper()}"


def build_message(msg_type: int, body: bytes) -> bytes:
    if len(body) > 0xFFFF:
        raise ValueError("message body too long")
    return bytes((MARKER, msg_type, len(body) & 0xFF, len(body) >> 8)) + bytes(body)


def build_reports(message: bytes, route: int) -> List[bytes]:
    """Split one message into zero-padded 62-byte output reports."""
    reports = []
    for offset in range(0, len(message), MAX_FRAGMENT):
        chunk = message[offset:offset + MAX_FRAGMENT]
        report = bytearray(REPORT_SIZE)
        report[0] = REPORT_ID_OUT
        report[1] = len(chunk)
        report[2] = route
        report[FRAGMENT_OFFSET:FRAGMENT_OFFSET + len(chunk)] = chunk
        reports.append(bytes(report))
    return reports


def report_fragment(report: bytes) -> bytes:
    """The valid payload of an input report. Empty means "nothing new yet".

    Bytes after the declared length are padding or stale data from older reports and must be
    ignored.
    """
    if len(report) < FRAGMENT_OFFSET:
        return b""
    length = report[1]
    if length > MAX_FRAGMENT or FRAGMENT_OFFSET + length > len(report):
        return b""
    return bytes(report[FRAGMENT_OFFSET:FRAGMENT_OFFSET + length])


class MessageStream:
    """Reassembles messages from input report fragments."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, fragment: bytes) -> None:
        self._buf.extend(fragment)

    def pop(self) -> Optional[Message]:
        buf = self._buf
        while buf:
            if buf[0] != MARKER:
                del buf[0]
                continue
            if len(buf) < HEADER_SIZE:
                return None
            size = HEADER_SIZE + (buf[2] | (buf[3] << 8))
            if size > MAX_MESSAGE_SIZE:
                del buf[0]
                continue
            if len(buf) < size:
                return None
            msg = Message(buf[1], bytes(buf[HEADER_SIZE:size]))
            del buf[:size]
            return msg
        return None

    def pop_all(self) -> List[Message]:
        out = []
        while (m := self.pop()) is not None:
            out.append(m)
        return out


# ---------------------------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------------------------

def read_key_body(key: int) -> bytes:
    return CMD_READ_KEY + bytes((key, 0x00))


def write_key_body(key: int, value: int) -> bytes:
    return CMD_WRITE_KEY + bytes((key, 0x00, value & 0xFF))


def read_param_body(param: int) -> bytes:
    return CMD_READ_PARAM + bytes((param, 0x00))


def write_param_body(param: int, value: Iterable[int]) -> bytes:
    return CMD_WRITE_PARAM + bytes((param, 0x00)) + bytes(value)


def battery_body() -> bytes:
    return CMD_BATTERY + b"\x00"


def firmware_body() -> bytes:
    return CMD_FIRMWARE + b"\x00"


# ---------------------------------------------------------------------------------------------
# Reply matching / parsing. Replies carry no sequence number, so they are matched on command
# identity plus the key/parameter id.
# ---------------------------------------------------------------------------------------------

def is_read_key_reply(msg: Message, key: int) -> bool:
    # body: 01 09 <key> 00 <status> <value>
    return msg.type == TYPE_RESPONSE and msg.command == CMD_READ_KEY and msg.body[2:4] == bytes((key, 0))


def parse_read_key_reply(msg: Message) -> int:
    if len(msg.body) < 6:
        raise ProtocolError("short key reply")
    if msg.body[4] != STATUS_OK:
        raise ProtocolError(f"headset refused reading key 0x{msg.body[2]:02X} (status 0x{msg.body[4]:02X})")
    return msg.body[5]


def is_write_key_reply(msg: Message, key: int) -> bool:
    # body: 00 09 <key> 00 <status>
    return msg.type == TYPE_RESPONSE and msg.command == CMD_WRITE_KEY and msg.body[2:4] == bytes((key, 0))


def parse_write_key_reply(msg: Message) -> None:
    # audezeforlinux observed status 0x01 for a refused write (their issue #9).
    if len(msg.body) >= 5 and msg.body[4] != STATUS_OK:
        raise ProtocolError(f"headset refused writing key 0x{msg.body[2]:02X} (status 0x{msg.body[4]:02X})")


def is_read_param_reply(msg: Message, param: int) -> bool:
    # body: 83 2C <status> <param> 00 <value ...>
    return msg.type == TYPE_RESPONSE and msg.command == CMD_READ_PARAM and msg.body[3:5] == bytes((param, 0))


def parse_read_param_reply(msg: Message) -> bytes:
    if len(msg.body) < 6:
        raise ProtocolError("short parameter reply")
    if msg.body[2] != STATUS_OK:
        raise ProtocolError(f"headset refused reading parameter 0x{msg.body[3]:02X} (status 0x{msg.body[2]:02X})")
    return msg.body[5:]


def is_write_param_reply(msg: Message, param: int) -> bool:
    # body: 82 2C <status> <param> 00
    return msg.type == TYPE_RESPONSE and msg.command == CMD_WRITE_PARAM and msg.body[3:5] == bytes((param, 0))


def parse_write_param_reply(msg: Message) -> None:
    if len(msg.body) < 3 or msg.body[2] != STATUS_OK:
        raise ProtocolError(f"headset refused writing parameter 0x{msg.body[3] if len(msg.body) > 3 else 0:02X}")


def is_battery_final(msg: Message) -> bool:
    """The battery level result (5D), or a refusing acknowledgement (5B with non-zero status)."""
    if msg.command != CMD_BATTERY:
        return False
    if msg.type == TYPE_RESULT and len(msg.body) >= 5:
        return True
    return msg.type == TYPE_RESPONSE and len(msg.body) >= 3 and msg.body[2] != STATUS_OK


def parse_battery(msg: Message) -> Optional[int]:
    # 5D body: D6 0C <status> <role> <percent>
    if msg.type != TYPE_RESULT:
        return None
    return min(msg.body[4], 100)


def is_firmware_result(msg: Message) -> bool:
    return msg.type == TYPE_RESULT and msg.command == CMD_FIRMWARE


def parse_firmware(msg: Message) -> str:
    # 5D body: 07 1C <status> <?> <string length> <ASCII ...> per audezeforlinux's parser
    # (payload[2] is the length there, payload being the body after the command bytes).
    payload = msg.body[2:]
    if len(payload) >= 3:
        length = payload[2]
        text = payload[3:3 + length]
    else:
        text = b""
    return text.decode("ascii", errors="replace").rstrip("\x00").strip()


# ---------------------------------------------------------------------------------------------
# Auto power-off values
# ---------------------------------------------------------------------------------------------

# Minutes the headset accepts, per HeadsetControl (recovered from Audeze.exe). 0 = never.
AUTO_OFF_MINUTES = (0, 5, 10, 15, 30, 45, 60, 90, 120, 240, 360)


def auto_off_value(minutes: int) -> bytes:
    """Value for PARAM_AUTO_POWER_OFF: enable flag, 00, seconds LE16, repeated twice."""
    if minutes not in AUTO_OFF_MINUTES:
        raise ValueError(f"unsupported auto-off time {minutes}; use one of {AUTO_OFF_MINUTES}")
    seconds = minutes * 60
    enabled = 1 if seconds else 0
    one = bytes((enabled, 0, seconds & 0xFF, seconds >> 8))
    return one + one


def parse_auto_off(value: bytes) -> Optional[int]:
    """Best-effort decode of a PARAM_AUTO_POWER_OFF read. Returns minutes, or None if the reply
    doesn't look like the layout that is written. UNVERIFIED: no source documents the read
    layout byte-for-byte, so callers must treat None as "unknown"."""
    if len(value) < 4 or value[1] != 0 or value[0] not in (0, 1):
        return None
    seconds = value[2] | (value[3] << 8)
    if value[0] == 0:
        return 0
    if seconds % 60:
        return None
    return seconds // 60


# ---------------------------------------------------------------------------------------------
# Value tables
# ---------------------------------------------------------------------------------------------

# Device preset index (1..10) -> name, per HeadsetControl and audezeforlinux (they agree).
EQ_PRESETS = {
    1: "Audeze",
    2: "Treble Boost",
    3: "Bass Boost",
    4: "Immersive",
    5: "Competition",
    6: "Footsteps",
    7: "Custom 1",
    8: "Custom 2",
    9: "Custom 3",
    10: "Custom 4",
}

NOISE_FILTER_LEVELS = {0: "Off", 1: "Low", 2: "High"}

MIC_MUTED_VALUE = 0x00  # KEY_MIC_MUTE reads 00 while muted, FF otherwise

VOICE_PROMPT_MAX = 15  # 0 (off) .. 15 (100%), default 7, per HeadsetControl
DEFAULT_SIDETONE_MAX = 31
CHATMIX_MAX = 20  # 0..20, 10 = centre


def parse_hex(text: str) -> bytes:
    """Parse "01 09 22 00" / "0109 2200" / "0x01,0x09" into bytes."""
    cleaned = text.replace(",", " ").replace("0x", " ").replace("0X", " ")
    cleaned = "".join(cleaned.split())
    if len(cleaned) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(cleaned)
