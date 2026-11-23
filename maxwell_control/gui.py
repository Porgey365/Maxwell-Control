"""GTK 4 / libadwaita window for Maxwell Control."""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import NoDeviceError, __version__, open_headset  # noqa: E402
from . import protocol as p  # noqa: E402
from .headset import Headset, HeadsetTimeout, Status  # noqa: E402
from .protocol import ProtocolError  # noqa: E402
from .transport import DeviceGone, DeviceInfo, TransportError  # noqa: E402

APP_ID = "io.github.maxwellcontrol.MaxwellControl"
POLL_SECONDS = 15
RECONNECT_SECONDS = 3


class Worker:
    """Runs device I/O off the GTK main loop, one job at a time."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def submit(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def stop(self) -> None:
        self._q.put(None)

    def _run(self) -> None:
        while (fn := self._q.get()) is not None:
            try:
                fn()
            except Exception:  # never kill the worker
                traceback.print_exc()


class MaxwellWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Maxwell Control")
        self.set_default_size(480, 760)
        self.worker = Worker()
        self.headset: Optional[Headset] = None
        self.info: Optional[DeviceInfo] = None
        self._syncing = False
        self._sidetone_timer = 0
        self._voice_timer = 0
        self._busy = False

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh(full=True))
        header.pack_end(self.refresh_btn)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner()
        toolbar.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = Adw.StatusPage(
            icon_name="audio-headset-symbolic",
            title="Looking for your Maxwell…",
            description="Plug in the wireless dongle or a USB-C cable.",
        )
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self._build_page(), "main")
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close)
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)
        self.connect_device()

    # -- layout -----------------------------------------------------------------------------

    def _build_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        status = Adw.PreferencesGroup(title="Status")
        self.battery_row = Adw.ActionRow(title="Battery")
        self.battery_bar = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER, hexpand=True)
        self.battery_bar.set_size_request(120, -1)
        self.battery_bar.add_offset_value("low", 20)
        self.battery_bar.add_offset_value("high", 50)
        self.battery_label = Gtk.Label(width_chars=5, xalign=1)
        self.battery_row.add_suffix(self.battery_bar)
        self.battery_row.add_suffix(self.battery_label)
        status.add(self.battery_row)

        self.mic_row = Adw.ActionRow(title="Microphone", subtitle="Mute state as reported by the headset")
        self.mic_label = Gtk.Label()
        self.mic_row.add_suffix(self.mic_label)
        status.add(self.mic_row)

        self.chatmix_row = Adw.ActionRow(title="Game / chat mix", subtitle="Set with the wheel on the headset")
        self.chatmix_bar = Gtk.LevelBar(min_value=0, max_value=p.CHATMIX_MAX, valign=Gtk.Align.CENTER)
        self.chatmix_bar.set_size_request(120, -1)
        self.chatmix_bar.remove_offset_value("low")
        self.chatmix_bar.remove_offset_value("high")
        self.chatmix_bar.remove_offset_value("full")
        self.chatmix_label = Gtk.Label(width_chars=10, xalign=1)
        self.chatmix_row.add_suffix(self.chatmix_bar)
        self.chatmix_row.add_suffix(self.chatmix_label)
        status.add(self.chatmix_row)

        self.device_row = Adw.ActionRow(title="Device", subtitle_selectable=True)
        status.add(self.device_row)
        self.fw_row = Adw.ActionRow(title="Firmware", subtitle="—", subtitle_selectable=True)
        status.add(self.fw_row)
        page.add(status)

        sound = Adw.PreferencesGroup(title="Sound")
        self.eq_row = Adw.ComboRow(title="EQ preset",
                                   model=Gtk.StringList.new(list(p.EQ_PRESETS.values())))
        self.eq_row.connect("notify::selected", self._on_eq)
        sound.add(self.eq_row)
        anc = Adw.ActionRow(
            title="Noise cancelling / transparency",
            subtitle="Not available yet: the command for this hasn't been documented by anyone. "
                     "Use the button on the headset. See the README to help find it.",
        )
        anc.set_sensitive(False)
        sound.add(anc)
        page.add(sound)

        mic = Adw.PreferencesGroup(title="Microphone")
        self.nf_row = Adw.ComboRow(title="AI noise filter",
                                   model=Gtk.StringList.new(list(p.NOISE_FILTER_LEVELS.values())))
        self.nf_row.connect("notify::selected", self._on_noise_filter)
        mic.add(self.nf_row)
        self.sidetone_switch = Adw.SwitchRow(title="Sidetone", subtitle="Hear your own voice")
        self.sidetone_switch.connect("notify::active", self._on_sidetone_switch)
        mic.add(self.sidetone_switch)
        self.sidetone_row = Adw.ActionRow(title="Sidetone level")
        self.sidetone_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, p.DEFAULT_SIDETONE_MAX, 1)
        self.sidetone_scale.set_hexpand(True)
        self.sidetone_scale.set_draw_value(True)
        self.sidetone_scale.set_digits(0)
        self.sidetone_scale.set_size_request(200, -1)
        self.sidetone_scale.connect("value-changed", self._on_sidetone_level)
        self.sidetone_row.add_suffix(self.sidetone_scale)
        mic.add(self.sidetone_row)
        page.add(mic)

        other = Adw.PreferencesGroup(title="Headset")
        self.voice_row = Adw.ActionRow(title="Voice prompt volume", subtitle="0 turns prompts off")
        self.voice_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, p.VOICE_PROMPT_MAX, 1)
        self.voice_scale.set_hexpand(True)
        self.voice_scale.set_digits(0)
        self.voice_scale.set_size_request(200, -1)
        self.voice_scale.connect("value-changed", self._on_voice)
        self.voice_row.add_suffix(self.voice_scale)
        other.add(self.voice_row)
        labels = ["Never" if m == 0 else f"{m} minutes" if m < 60 else
                  f"{m // 60} hour{'s' if m >= 120 else ''}" + (" 30 min" if m % 60 else "")
                  for m in p.AUTO_OFF_MINUTES]
        self.autooff_row = Adw.ComboRow(title="Auto power-off", model=Gtk.StringList.new(labels))
        self.autooff_row.connect("notify::selected", self._on_auto_off)
        other.add(self.autooff_row)
        page.add(other)

        self.controls = [self.eq_row, self.nf_row, self.sidetone_switch, self.sidetone_row,
                         self.voice_row, self.autooff_row]
        return page

    # -- connection & polling -----------------------------------------------------------------

    def connect_device(self) -> None:
        def job():
            try:
                headset, info = open_headset()
            except NoDeviceError:
                GLib.idle_add(self._show_disconnected, None)
                return
            except TransportError as e:
                GLib.idle_add(self._show_disconnected, str(e))
                return
            self.headset, self.info = headset, info
            GLib.idle_add(self._on_connected)
            self._refresh_job(full=True)

        self.worker.submit(job)

    def _show_disconnected(self, error: Optional[str]) -> bool:
        self.stack.set_visible_child_name("empty")
        if error:
            self.empty.set_title("Can't open the headset")
            self.empty.set_description(error)
        else:
            self.empty.set_title("Looking for your Maxwell…")
            self.empty.set_description("Plug in the wireless dongle or a USB-C cable.")
        GLib.timeout_add_seconds(RECONNECT_SECONDS, self._retry_connect)
        return False

    def _retry_connect(self) -> bool:
        if self.headset is None:
            self.connect_device()
        return False

    def _on_connected(self) -> bool:
        # Controls stay disabled until the first status read has filled them with real values.
        for c in self.controls:
            c.set_sensitive(False)
        self.stack.set_visible_child_name("main")
        self.device_row.set_subtitle(f"{self.info.model}\n{self.info.path} · {self.info.vendor_id:04x}:"
                                     f"{self.info.product_id:04x}")
        return False

    def _lost(self) -> None:
        if self.headset:
            try:
                self.headset.close()
            except Exception:
                pass
        self.headset = None
        GLib.idle_add(self._show_disconnected, None)

    def _tick(self) -> bool:
        if self.headset is not None and not self._busy:
            self.refresh(full=False)
        return True

    def refresh(self, full: bool) -> None:
        if self.headset is None:
            self.connect_device()
            return
        self.worker.submit(lambda: self._refresh_job(full))

    def _refresh_job(self, full: bool) -> None:
        h = self.headset
        if h is None:
            return
        self._busy = True
        try:
            status = h.status(include_firmware=full)
        except DeviceGone:
            self._lost()
            return
        except TransportError as e:
            GLib.idle_add(self._toast, str(e))
            return
        finally:
            self._busy = False
        GLib.idle_add(self._apply_status, status, full)

    def _apply_status(self, s: Status, full: bool) -> bool:
        self._syncing = True
        try:
            reachable = s.headset_reachable
            if reachable:
                self.banner.set_revealed(False)
            else:
                self.banner.set_title("Headset not responding. Is it switched on and in range?")
                self.banner.set_revealed(True)
            for c in self.controls:
                c.set_sensitive(reachable)

            if s.battery is not None:
                self.battery_bar.set_value(s.battery)
                self.battery_label.set_label(f"{s.battery}%")
            else:
                self.battery_bar.set_value(0)
                self.battery_label.set_label("—")
            self.mic_label.set_label("—" if s.mic_muted is None else "Muted" if s.mic_muted else "Live")
            if s.chatmix is not None:
                self.chatmix_bar.set_value(s.chatmix)
                self.chatmix_label.set_label("Balanced" if s.chatmix == 10 else
                                             "More game" if s.chatmix < 10 else "More chat")
            if s.eq_preset in p.EQ_PRESETS:
                self.eq_row.set_selected(list(p.EQ_PRESETS).index(s.eq_preset))
            if s.noise_filter in p.NOISE_FILTER_LEVELS:
                self.nf_row.set_selected(s.noise_filter)
            if s.sidetone is not None:
                self.sidetone_switch.set_active(s.sidetone.enabled)
                self.sidetone_scale.set_range(0, s.sidetone.max_level)
                self.sidetone_scale.set_value(s.sidetone.level)
                self.sidetone_row.set_sensitive(s.sidetone.enabled)
            if s.voice_prompt_level is not None:
                self.voice_scale.set_value(s.voice_prompt_level)
            if s.auto_off_minutes in p.AUTO_OFF_MINUTES:
                self.autooff_row.set_selected(p.AUTO_OFF_MINUTES.index(s.auto_off_minutes))
            if full and (s.headset_firmware or s.dongle_firmware):
                parts = []
                if s.headset_firmware:
                    parts.append(f"Headset {s.headset_firmware}")
                if s.dongle_firmware:
                    parts.append(f"Dongle {s.dongle_firmware}")
                self.fw_row.set_subtitle(" · ".join(parts))
            failed = [k for k in s.errors if k != "battery" or reachable]
            if reachable and failed:
                self._toast("Couldn't read: " + ", ".join(failed))
        finally:
            self._syncing = False
        return False

    # -- writes -------------------------------------------------------------------------------

    def _write(self, description: str, fn: Callable[[Headset], None]) -> None:
        if self._syncing or self.headset is None:
            return

        def job():
            h = self.headset
            if h is None:
                return
            try:
                fn(h)
            except DeviceGone:
                self._lost()
                return
            except (HeadsetTimeout, ProtocolError, TransportError, ValueError) as e:
                GLib.idle_add(self._toast, f"{description} failed: {e}")
            self._refresh_job(full=False)

        self.worker.submit(job)

    def _on_eq(self, row, _pspec) -> None:
        preset = list(p.EQ_PRESETS)[row.get_selected()]
        self._write("Changing EQ preset", lambda h: h.set_eq_preset(preset))

    def _on_noise_filter(self, row, _pspec) -> None:
        level = row.get_selected()
        self._write("Changing noise filter", lambda h: h.set_noise_filter(level))

    def _on_sidetone_switch(self, row, _pspec) -> None:
        on = row.get_active()
        self.sidetone_row.set_sensitive(on)
        self._write("Switching sidetone", lambda h: h.set_sidetone_enabled(on))

    def _debounced(self, attr: str, fn: Callable[[], None]) -> None:
        if getattr(self, attr):
            GLib.source_remove(getattr(self, attr))

        def fire():
            setattr(self, attr, 0)
            fn()
            return False

        setattr(self, attr, GLib.timeout_add(350, fire))

    def _on_sidetone_level(self, scale) -> None:
        if self._syncing:
            return
        level = int(round(scale.get_value()))
        self._debounced("_sidetone_timer",
                        lambda: self._write("Setting sidetone level", lambda h: h.set_sidetone_level(level)))

    def _on_voice(self, scale) -> None:
        if self._syncing:
            return
        level = int(round(scale.get_value()))
        self._debounced("_voice_timer",
                        lambda: self._write("Setting voice prompts", lambda h: h.set_voice_prompt_level(level)))

    def _on_auto_off(self, row, _pspec) -> None:
        minutes = p.AUTO_OFF_MINUTES[row.get_selected()]
        self._write("Setting auto power-off", lambda h: h.set_auto_off(minutes))

    # -- misc ---------------------------------------------------------------------------------

    def _toast(self, text: str) -> bool:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))
        return False

    def _on_close(self, *_):
        self.worker.stop()
        if self.headset:
            self.headset.close()
        return False


class MaxwellApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        GLib.set_application_name("Maxwell Control")

    def do_activate(self) -> None:
        win = self.props.active_window or MaxwellWindow(self)
        win.present()


def main() -> int:
    return MaxwellApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
