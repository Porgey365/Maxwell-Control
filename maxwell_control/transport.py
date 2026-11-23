"""Finding and talking to the Maxwell's control interface through Linux hidraw.

Uses only the standard library: sysfs for discovery, os.write for output reports and the
HIDIOCGINPUT ioctl (a USB GET_REPORT) to poll input report 0x07, which is how HeadsetControl
reads replies (hid_get_input_report).
"""

from __future__ import annotations

import errno
import fcntl
import glob
import os
from dataclasses import dataclass
from typing import List, Optional

from .protocol import REPORT_ID_IN, REPORT_SIZE, ROUTE_HEADSET, ROUTE_LOCAL

AUDEZE_VID = 0x3329
CONTROL_USAGE_PAGE = 0xFF13  # vendor collection carrying reports 0x06/0x07

# Product IDs with their source. "reported" = a user report in another project's issue tracker,
# not confirmed by a maintainer with the hardware.
KNOWN_PRODUCTS = {
    0x4B18: ("Maxwell (Xbox dongle)", True),            # HeadsetControl
    0x4B19: ("Maxwell (PlayStation dongle)", True),     # HeadsetControl
    0x4B1A: ("Maxwell (USB-C, PlayStation)", False),    # audezeforlinux
    0x4B1E: ("Maxwell (USB-C, Xbox)", False),           # audezeforlinux
    0x4B28: ("Maxwell 2 (Xbox dongle)", True),          # HeadsetControl
    0x4B29: ("Maxwell 2 (PlayStation dongle)", True),   # HeadsetControl
    0x4B2A: ("Maxwell 2 (USB-C)", False),               # audezeforlinux
    0x4B2E: ("Maxwell 2 (USB-C)", False),               # audezeforlinux
    0x4BA8: ("Maxwell 2 ANC (Xbox dongle, reported)", True),         # audezeforlinux issue #3
    0x4BA9: ("Maxwell 2 ANC (PlayStation dongle, reported)", True),  # HeadsetControl #636, audezeforlinux #4
}

DEVICE_GONE_ERRNOS = (errno.ENODEV, errno.EIO, errno.EBADF, errno.ENOENT, errno.EPIPE, errno.ESHUTDOWN)


class TransportError(Exception):
    pass


class DeviceGone(TransportError):
    """The hidraw node disappeared (unplugged, or the headset was switched on/off and the
    dongle re-enumerated)."""


def _hidiocginput(length: int) -> int:
    # _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, len)
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x0A


def descriptor_usage_pages(descriptor: bytes) -> List[int]:
    """Usage pages declared (global Usage Page items) in a HID report descriptor."""
    pages = []
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        data = descriptor[i + 1:i + 1 + size]
        if prefix & 0xFC == 0x04:
            pages.append(int.from_bytes(data, "little"))
        i += 1 + size
    return pages


@dataclass
class DeviceInfo:
    path: str            # /dev/hidrawN
    vendor_id: int
    product_id: int
    name: str            # HID_NAME from sysfs
    interface: str       # e.g. "1-3:1.5"

    @property
    def model(self) -> str:
        known = KNOWN_PRODUCTS.get(self.product_id)
        return known[0] if known else f"Unknown Audeze device 0x{self.product_id:04X}"

    @property
    def is_known(self) -> bool:
        return self.product_id in KNOWN_PRODUCTS

    @property
    def default_route(self) -> int:
        known = KNOWN_PRODUCTS.get(self.product_id)
        if known:
            return ROUTE_HEADSET if known[1] else ROUTE_LOCAL
        # Unknown product id: guess from the name. Dongles report "... Dongle" in some cases;
        # otherwise assume a dongle, since that is how the Maxwell 2 ANC ships.
        return ROUTE_LOCAL if "usb-c" in self.name.lower() else ROUTE_HEADSET


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def list_devices(sysfs_root: str = "/sys/class/hidraw") -> List[DeviceInfo]:
    """All Audeze hidraw nodes that expose the control collection (usage page 0xFF13)."""
    found = []
    for node in sorted(glob.glob(os.path.join(sysfs_root, "hidraw*"))):
        uevent = _read(os.path.join(node, "device", "uevent"))
        if not uevent:
            continue
        fields = dict(
            line.split("=", 1) for line in uevent.decode(errors="replace").splitlines() if "=" in line
        )
        hid_id = fields.get("HID_ID", "")
        try:
            _bus, vid, pid = (int(x, 16) for x in hid_id.split(":"))
        except ValueError:
            continue
        if vid != AUDEZE_VID:
            continue
        descriptor = _read(os.path.join(node, "device", "report_descriptor")) or b""
        if CONTROL_USAGE_PAGE not in descriptor_usage_pages(descriptor):
            continue
        interface = os.path.basename(os.path.realpath(os.path.join(node, "device", "..")))
        found.append(DeviceInfo(
            path=os.path.join("/dev", os.path.basename(node)),
            vendor_id=vid,
            product_id=pid,
            name=fields.get("HID_NAME", ""),
            interface=interface,
        ))
    return found


def list_audeze_nodes(sysfs_root: str = "/sys/class/hidraw") -> List[str]:
    """Every Audeze hidraw node, with or without the control collection (for diagnostics)."""
    out = []
    for node in sorted(glob.glob(os.path.join(sysfs_root, "hidraw*"))):
        uevent = (_read(os.path.join(node, "device", "uevent")) or b"").decode(errors="replace")
        if f":0000{AUDEZE_VID:04X}:" in uevent.upper():
            descriptor = _read(os.path.join(node, "device", "report_descriptor")) or b""
            pages = ", ".join(f"0x{p:04X}" for p in descriptor_usage_pages(descriptor))
            out.append(f"/dev/{os.path.basename(node)}  usage pages: {pages or '?'}")
    return out


class HidrawTransport:
    """One open /dev/hidrawN node."""

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            self._fd = os.open(path, os.O_RDWR)
        except PermissionError as e:
            raise TransportError(
                f"No permission to open {path}. Install the udev rule (see README) and replug "
                "the dongle."
            ) from e
        except OSError as e:
            raise TransportError(f"Cannot open {path}: {e.strerror}") from e

    def write(self, report: bytes) -> None:
        try:
            os.write(self._fd, report)
        except OSError as e:
            if e.errno in DEVICE_GONE_ERRNOS:
                raise DeviceGone(str(e)) from e
            raise TransportError(f"write failed: {e.strerror}") from e

    def get_input_report(self) -> bytes:
        buf = bytearray(REPORT_SIZE)
        buf[0] = REPORT_ID_IN
        try:
            n = fcntl.ioctl(self._fd, _hidiocginput(REPORT_SIZE), buf, True)
        except OSError as e:
            if e.errno in DEVICE_GONE_ERRNOS:
                raise DeviceGone(str(e)) from e
            raise TransportError(f"GET_REPORT failed: {e.strerror}") from e
        return bytes(buf[:n]) if isinstance(n, int) and 0 < n <= REPORT_SIZE else bytes(buf)

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1
