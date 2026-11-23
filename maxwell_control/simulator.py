"""A fake headset implementing the documented replies, for tests and for trying the GUI without
hardware (MAXWELL_SIMULATE=1). It models what the sources describe, not verified device
behaviour beyond that."""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List

from . import protocol as p
from .protocol import MessageStream


class SimulatedHeadset:
    def __init__(self) -> None:
        self.powered_on = True
        self.battery = 73
        self.keys: Dict[int, int] = {
            p.KEY_EQ_PRESET: 1,
            p.KEY_VOICE_PROMPT_LEVEL: 7,
            p.KEY_MIC_MUTE: 0xFF,
            p.KEY_NOISE_FILTER: 1,
            p.KEY_SIDETONE_LEVEL: 12,
            p.KEY_SIDETONE_MAX_LEVEL: 31,
        }
        self.params: Dict[int, bytes] = {
            p.PARAM_AUTO_POWER_OFF: p.auto_off_value(30),
            p.PARAM_SIDETONE_ENABLED: b"\x00",
            p.PARAM_CHATMIX: b"\x0a",
        }
        self.firmware = {p.ROUTE_HEADSET: "1.0.5.6", p.ROUTE_LOCAL: "1.0.5.6-dongle"}
        self.requests: List[bytes] = []
        self._out: Deque[bytes] = deque()
        self._in = MessageStream()
        self._pending_route = p.ROUTE_HEADSET

    # Transport interface ------------------------------------------------------------------

    def write(self, report: bytes) -> None:
        assert len(report) == p.REPORT_SIZE and report[0] == p.REPORT_ID_OUT
        self._pending_route = report[2]
        self._in.feed(p.report_fragment(report))
        for msg in self._in.pop_all():
            assert msg.type == p.TYPE_REQUEST
            self.requests.append(msg.body)
            self._handle(msg.body, report[2])

    def get_input_report(self) -> bytes:
        report = bytearray(p.REPORT_SIZE)
        report[0] = p.REPORT_ID_IN
        if self._out:
            frag = self._out.popleft()
            report[1] = len(frag)
            report[3:3 + len(frag)] = frag
        else:
            # Like the real dongle: length 0 with stale bytes behind it.
            report[3:6] = b"\x05\x5b\xff"
        return bytes(report)

    def close(self) -> None:
        pass

    # Behaviour --------------------------------------------------------------------------------

    def _send(self, msg_type: int, body: bytes) -> None:
        data = p.build_message(msg_type, body)
        # Split oddly on purpose so reassembly is exercised.
        for i in range(0, len(data), 7):
            self._out.append(data[i:i + 7])

    def _handle(self, body: bytes, route: int) -> None:
        cmd = body[:2]
        if cmd == p.CMD_FIRMWARE:
            text = self.firmware.get(route, "").encode()
            if route == p.ROUTE_HEADSET and not self.powered_on:
                return
            self._send(p.TYPE_RESPONSE, cmd + b"\x00")
            self._send(p.TYPE_RESULT, cmd + bytes((0, 0, len(text))) + text)
            return
        if not self.powered_on:
            return  # the headset doesn't answer while off
        if cmd == p.CMD_BATTERY:
            self._send(p.TYPE_RESPONSE, cmd + b"\x00")
            self._send(p.TYPE_RESULT, cmd + bytes((0, 0, self.battery)))
        elif cmd == p.CMD_READ_KEY:
            key = body[2]
            if key in self.keys:
                self._send(p.TYPE_RESPONSE, cmd + bytes((key, 0, 0, self.keys[key])))
        elif cmd == p.CMD_WRITE_KEY:
            key, value = body[2], body[4]
            ok = key in self.keys and key not in (p.KEY_MIC_MUTE, p.KEY_SIDETONE_MAX_LEVEL)
            if ok:
                self.keys[key] = value
            self._send(p.TYPE_RESPONSE, cmd + bytes((key, 0, 0 if ok else 1)))
        elif cmd == p.CMD_READ_PARAM:
            param = body[2]
            if param in self.params:
                self._send(p.TYPE_RESPONSE, cmd + bytes((0, param, 0)) + self.params[param])
        elif cmd == p.CMD_WRITE_PARAM:
            param, value = body[2], body[4:]
            if param == p.PARAM_SIDETONE_ENABLED:
                # Toggles whatever value is sent (as documented by HeadsetControl).
                cur = self.params[param][0]
                self.params[param] = bytes((0 if cur else 1,))
            elif param in self.params and param != p.PARAM_CHATMIX:
                self.params[param] = bytes(value)
            self._send(p.TYPE_RESPONSE, cmd + bytes((0, param, 0)))
