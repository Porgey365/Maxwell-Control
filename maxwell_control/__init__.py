"""Maxwell Control: status and settings for Audeze Maxwell headsets on Linux."""

from __future__ import annotations

import os
from typing import Optional, Tuple

from .headset import Headset
from .transport import DeviceInfo, HidrawTransport, TransportError, list_devices

__version__ = "0.1.0"


class NoDeviceError(TransportError):
    pass


def open_headset(path: Optional[str] = None, route: Optional[int] = None) -> Tuple[Headset, DeviceInfo]:
    """Open the first Maxwell found (or `path`) and return (headset, info).

    With MAXWELL_SIMULATE=1 in the environment a simulated headset is returned instead, for
    trying the app without hardware."""
    if os.environ.get("MAXWELL_SIMULATE"):
        from .simulator import SimulatedHeadset
        return Headset(SimulatedHeadset()), DeviceInfo("(simulated)", 0x3329, 0x4BA9, "Simulator", "-")
    devices = list_devices()
    if path:
        matches = [d for d in devices if d.path == path]
        info = matches[0] if matches else DeviceInfo(path, 0x3329, 0, "", "")
    else:
        if not devices:
            raise NoDeviceError(
                "No Audeze Maxwell control interface found. Is the dongle or USB-C cable plugged "
                "in? Run 'maxwellctl list' for details."
            )
        info = devices[0]
    transport = HidrawTransport(info.path)
    return Headset(transport, route=info.default_route if route is None else route), info
