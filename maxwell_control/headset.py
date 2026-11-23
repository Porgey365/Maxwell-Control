"""High-level headset API on top of the protocol and a transport."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

from . import protocol as p
from .protocol import Message, MessageStream, ProtocolError


class Transport(Protocol):
    def write(self, report: bytes) -> None: ...
    def get_input_report(self) -> bytes: ...
    def close(self) -> None: ...


class HeadsetTimeout(Exception):
    """No matching reply. Usually the headset is switched off or out of range."""


@dataclass
class SidetoneState:
    enabled: bool
    level: int
    max_level: int


@dataclass
class Status:
    """A snapshot. Any field is None when reading it failed."""
    battery: Optional[int] = None
    mic_muted: Optional[bool] = None
    eq_preset: Optional[int] = None
    noise_filter: Optional[int] = None
    sidetone: Optional[SidetoneState] = None
    voice_prompt_level: Optional[int] = None
    chatmix: Optional[int] = None
    auto_off_minutes: Optional[int] = None
    headset_firmware: Optional[str] = None
    dongle_firmware: Optional[str] = None
    errors: Dict[str, str] = field(default_factory=dict)

    @property
    def headset_reachable(self) -> bool:
        return self.battery is not None


class Headset:
    """Thread-safe: every request holds a lock, so a background poll can't interleave with a
    user action. Requests are paced like Audeze's app (per HeadsetControl): one at a time, 50 ms
    between polls, reads retried on timeout, writes sent exactly once."""

    POLL_INTERVAL = 0.05
    TIMEOUT = 1.25
    READ_ATTEMPTS = 3

    def __init__(self, transport: Transport, route: int = p.ROUTE_HEADSET,
                 on_unsolicited: Optional[Callable[[Message], None]] = None) -> None:
        self.transport = transport
        self.route = route
        self.on_unsolicited = on_unsolicited
        self._lock = threading.RLock()

    def close(self) -> None:
        self.transport.close()

    # -- core -----------------------------------------------------------------------------------

    def _await(self, accept: Callable[[Message], bool], timeout: float,
               collect: Optional[List[Message]] = None) -> Message:
        deadline = time.monotonic() + timeout
        stream = MessageStream()
        while True:
            for msg in stream.pop_all():
                if collect is not None:
                    collect.append(msg)
                if accept(msg):
                    return msg
                if self.on_unsolicited and msg.type in (p.TYPE_NOTIFICATION, p.TYPE_RESULT):
                    self.on_unsolicited(msg)
            if time.monotonic() >= deadline:
                raise HeadsetTimeout("no reply from the headset (is it switched on and in range?)")
            time.sleep(self.POLL_INTERVAL)
            stream.feed(p.report_fragment(self.transport.get_input_report()))

    def request(self, body: bytes, accept: Callable[[Message], bool], attempts: int = 1,
                timeout: Optional[float] = None, route: Optional[int] = None) -> Message:
        reports = p.build_reports(p.build_message(p.TYPE_REQUEST, body),
                                  self.route if route is None else route)
        with self._lock:
            last: Exception = HeadsetTimeout("no reply")
            for _ in range(max(1, attempts)):
                for report in reports:
                    self.transport.write(report)
                try:
                    return self._await(accept, self.TIMEOUT if timeout is None else timeout)
                except HeadsetTimeout as e:
                    last = e
            raise last

    def raw(self, body: bytes, listen: float = 1.0, route: Optional[int] = None) -> List[Message]:
        """Send an arbitrary request body once and return every message seen for `listen` s."""
        reports = p.build_reports(p.build_message(p.TYPE_REQUEST, body),
                                  self.route if route is None else route)
        seen: List[Message] = []
        with self._lock:
            for report in reports:
                self.transport.write(report)
            try:
                self._await(lambda m: False, listen, collect=seen)
            except HeadsetTimeout:
                pass
        return seen

    def listen(self, duration: float) -> List[Message]:
        """Collect whatever the device sends without asking (for discovering notifications)."""
        seen: List[Message] = []
        with self._lock:
            try:
                self._await(lambda m: False, duration, collect=seen)
            except HeadsetTimeout:
                pass
        return seen

    # -- key / parameter primitives -------------------------------------------------------------

    def read_key(self, key: int) -> int:
        msg = self.request(p.read_key_body(key), lambda m: p.is_read_key_reply(m, key),
                           attempts=self.READ_ATTEMPTS)
        return p.parse_read_key_reply(msg)

    def write_key(self, key: int, value: int) -> None:
        msg = self.request(p.write_key_body(key, value), lambda m: p.is_write_key_reply(m, key))
        p.parse_write_key_reply(msg)

    def read_param(self, param: int) -> bytes:
        msg = self.request(p.read_param_body(param), lambda m: p.is_read_param_reply(m, param),
                           attempts=self.READ_ATTEMPTS)
        return p.parse_read_param_reply(msg)

    def write_param(self, param: int, value: bytes) -> None:
        msg = self.request(p.write_param_body(param, value), lambda m: p.is_write_param_reply(m, param))
        p.parse_write_param_reply(msg)

    # -- features -------------------------------------------------------------------------------

    def battery(self) -> Optional[int]:
        """Battery percent, or None if the headset refused the request."""
        msg = self.request(p.battery_body(), p.is_battery_final, attempts=self.READ_ATTEMPTS)
        return p.parse_battery(msg)

    def firmware(self, route: Optional[int] = None) -> str:
        msg = self.request(p.firmware_body(), p.is_firmware_result, attempts=self.READ_ATTEMPTS,
                           route=route)
        return p.parse_firmware(msg)

    def mic_muted(self) -> bool:
        return self.read_key(p.KEY_MIC_MUTE) == p.MIC_MUTED_VALUE

    def eq_preset(self) -> int:
        return self.read_key(p.KEY_EQ_PRESET)

    def set_eq_preset(self, preset: int) -> None:
        if preset not in p.EQ_PRESETS:
            raise ValueError("preset must be 1..10")
        self.write_key(p.KEY_EQ_PRESET, preset)

    def noise_filter(self) -> int:
        return self.read_key(p.KEY_NOISE_FILTER)

    def set_noise_filter(self, level: int) -> None:
        if level not in p.NOISE_FILTER_LEVELS:
            raise ValueError("noise filter must be 0 (off), 1 (low) or 2 (high)")
        self.write_key(p.KEY_NOISE_FILTER, level)

    def voice_prompt_level(self) -> int:
        return self.read_key(p.KEY_VOICE_PROMPT_LEVEL)

    def set_voice_prompt_level(self, level: int) -> None:
        if not 0 <= level <= p.VOICE_PROMPT_MAX:
            raise ValueError(f"voice prompt level must be 0..{p.VOICE_PROMPT_MAX}")
        self.write_key(p.KEY_VOICE_PROMPT_LEVEL, level)

    def chatmix(self) -> int:
        """0..20; 10 is centre. Set by the wheel on the headset, so read-only here."""
        return min(self.read_param(p.PARAM_CHATMIX)[0], p.CHATMIX_MAX)

    def sidetone_max(self) -> int:
        try:
            value = self.read_key(p.KEY_SIDETONE_MAX_LEVEL)
        except (HeadsetTimeout, ProtocolError):
            return p.DEFAULT_SIDETONE_MAX
        return value or p.DEFAULT_SIDETONE_MAX

    def sidetone(self) -> SidetoneState:
        level = self.read_key(p.KEY_SIDETONE_LEVEL)
        enabled = self.read_param(p.PARAM_SIDETONE_ENABLED)[0] != 0
        return SidetoneState(enabled=enabled, level=level, max_level=max(self.sidetone_max(), level))

    def set_sidetone_enabled(self, on: bool) -> None:
        """Writing this parameter TOGGLES sidetone whatever value is sent (HeadsetControl,
        verified on an original Maxwell). So read first, write only if it differs, never retry."""
        with self._lock:
            current = self.read_param(p.PARAM_SIDETONE_ENABLED)[0] != 0
            if current != on:
                self.write_param(p.PARAM_SIDETONE_ENABLED, bytes((1 if on else 0,)))

    def set_sidetone_level(self, level: int) -> None:
        """Switches sidetone on first (Audeze's app order), then writes the level."""
        with self._lock:
            max_level = self.sidetone_max()
            if not 0 <= level <= max_level:
                raise ValueError(f"sidetone level must be 0..{max_level}")
            self.set_sidetone_enabled(True)
            self.write_key(p.KEY_SIDETONE_LEVEL, level)

    def auto_off(self) -> Optional[int]:
        return p.parse_auto_off(self.read_param(p.PARAM_AUTO_POWER_OFF))

    def set_auto_off(self, minutes: int) -> None:
        self.write_param(p.PARAM_AUTO_POWER_OFF, p.auto_off_value(minutes))

    # -- snapshot -------------------------------------------------------------------------------

    def status(self, include_firmware: bool = False) -> Status:
        s = Status()

        def attempt(name, fn):
            try:
                return fn()
            except (HeadsetTimeout, ProtocolError) as e:
                s.errors[name] = str(e)
                return None

        s.battery = attempt("battery", self.battery)
        if s.battery is None:
            # The headset doesn't answer while switched off; skip the rest instead of waiting
            # several seconds per field.
            return s
        s.mic_muted = attempt("mic", self.mic_muted)
        s.eq_preset = attempt("eq_preset", self.eq_preset)
        s.noise_filter = attempt("noise_filter", self.noise_filter)
        s.sidetone = attempt("sidetone", self.sidetone)
        s.voice_prompt_level = attempt("voice_prompts", self.voice_prompt_level)
        s.chatmix = attempt("chatmix", self.chatmix)
        s.auto_off_minutes = attempt("auto_off", self.auto_off)
        if include_firmware:
            s.headset_firmware = attempt("headset_firmware", self.firmware)
            if self.route == p.ROUTE_HEADSET:
                s.dongle_firmware = attempt("dongle_firmware", lambda: self.firmware(p.ROUTE_LOCAL))
        return s

    # -- discovery (read-only) ------------------------------------------------------------------

    def scan(self, keys=range(0x00, 0x40), params=range(0x00, 0x20),
             progress: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
        """Read every key/parameter id in range and return the ones that answer.

        EXPERIMENTAL. Only read requests are sent (01 09 / 83 2C), the same commands the
        documented features use, but reading undocumented ids has not been verified as harmless
        by any source. Used to look for the ANC setting: scan, change ANC with the headset
        button, scan again, compare.
        """
        out: Dict[str, str] = {}
        timeout = 0.4
        for key in keys:
            if progress:
                progress(f"key 0x{key:02X}")
            try:
                msg = self.request(p.read_key_body(key), lambda m, k=key: p.is_read_key_reply(m, k),
                                   timeout=timeout)
                out[f"key 0x{key:02X}"] = msg.body[4:].hex(" ").upper()
            except HeadsetTimeout:
                pass
        for param in params:
            if progress:
                progress(f"param 0x{param:02X}")
            try:
                msg = self.request(p.read_param_body(param),
                                   lambda m, q=param: p.is_read_param_reply(m, q), timeout=timeout)
                out[f"param 0x{param:02X}"] = (msg.body[2:3] + msg.body[5:]).hex(" ").upper()
            except HeadsetTimeout:
                pass
        return out
