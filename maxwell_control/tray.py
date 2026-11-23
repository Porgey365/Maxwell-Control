"""System tray icon via the StatusNotifierItem D-Bus protocol (KDE/freedesktop "AppIndicator").

GTK 4 has no tray API, so this speaks the protocol directly with Gio. It needs a tray host:
Noctalia, Waybar, KDE Plasma, etc. GNOME needs the AppIndicator extension.

Two interfaces are exported on the session bus:
  * org.kde.StatusNotifierItem at /StatusNotifierItem (icon, tooltip, click handling)
  * com.canonical.dbusmenu at /MenuBar (the right-click menu)
and the item is registered with org.kde.StatusNotifierWatcher, again whenever a watcher
(re)appears, e.g. after the shell restarts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from gi.repository import Gio, GLib

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    id: int
    label: str = ""
    enabled: bool = True
    visible: bool = True
    separator: bool = False
    toggle: str = ""          # "", "checkmark" or "radio"
    checked: bool = False
    on_click: Optional[Callable[[], None]] = None
    children: List["MenuItem"] = field(default_factory=list)

    def props(self) -> Dict[str, GLib.Variant]:
        if self.separator:
            return {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", self.visible)}
        d = {
            "label": GLib.Variant("s", self.label),
            "enabled": GLib.Variant("b", self.enabled),
            "visible": GLib.Variant("b", self.visible),
        }
        if self.toggle:
            d["toggle-type"] = GLib.Variant("s", self.toggle)
            d["toggle-state"] = GLib.Variant("i", 1 if self.checked else 0)
        if self.children:
            d["children-display"] = GLib.Variant("s", "submenu")
        return d


def battery_icon(percent: Optional[int]) -> str:
    """Icon name for the tray. Standard freedesktop/Adwaita names only."""
    if percent is None:
        return "audio-headset-symbolic"
    if percent <= 15:
        return "battery-caution-symbolic"
    return "audio-headset-symbolic"


class TrayIcon:
    def __init__(self, title: str, on_activate: Callable[[], None]) -> None:
        self.title = title
        self.on_activate = on_activate
        self.icon_name = "audio-headset-symbolic"
        self.tooltip_title = title
        self.tooltip_body = ""
        self.root = MenuItem(0, children=[])
        self._index: Dict[int, MenuItem] = {}
        self._revision = 1
        self._bus: Optional[Gio.DBusConnection] = None
        self._reg_ids: List[int] = []
        self._own_id = 0
        self._watch_id = 0
        self.registered = False
        self.service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

    # -- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        self._own_id = Gio.bus_own_name(
            Gio.BusType.SESSION, self.service_name, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, self._on_name_acquired, None)

    def stop(self) -> None:
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._bus:
            for rid in self._reg_ids:
                self._bus.unregister_object(rid)
        self._reg_ids = []
        if self._own_id:
            Gio.bus_unown_name(self._own_id)
            self._own_id = 0

    def _on_bus_acquired(self, bus, _name) -> None:
        self._bus = bus
        self._reg_ids.append(bus.register_object(
            ITEM_PATH, self._sni_info, self._sni_call, self._sni_get, None))
        self._reg_ids.append(bus.register_object(
            MENU_PATH, self._menu_info, self._menu_call, self._menu_get, None))

    def _on_name_acquired(self, _bus, _name) -> None:
        self._watch_id = Gio.bus_watch_name(
            Gio.BusType.SESSION, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared, self._on_watcher_vanished)

    def _on_watcher_appeared(self, bus, _name, _owner) -> None:
        bus.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (self.service_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                 self._on_registered)

    def _on_registered(self, bus, result) -> None:
        try:
            bus.call_finish(result)
            self.registered = True
        except GLib.Error as e:
            self.registered = False
            print(f"maxwell-control: tray registration failed: {e.message}")

    def _on_watcher_vanished(self, _bus, _name) -> None:
        self.registered = False

    # -- public updates -------------------------------------------------------------------------

    def set_icon(self, icon_name: str) -> None:
        if icon_name != self.icon_name:
            self.icon_name = icon_name
            self._emit_sni("NewIcon")

    def set_tooltip(self, title: str, body: str) -> None:
        if (title, body) != (self.tooltip_title, self.tooltip_body):
            self.tooltip_title, self.tooltip_body = title, body
            self._emit_sni("NewToolTip")
            self._emit_sni("NewTitle")

    def set_menu(self, items: List[MenuItem]) -> None:
        self.root = MenuItem(0, children=items)
        self._index = {}

        def walk(item):
            self._index[item.id] = item
            for c in item.children:
                walk(c)

        walk(self.root)
        self._revision += 1
        if self._bus:
            self._bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                  GLib.Variant("(ui)", (self._revision, 0)))

    # -- StatusNotifierItem ---------------------------------------------------------------------

    def _emit_sni(self, signal: str) -> None:
        if self._bus:
            self._bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, None)

    def _sni_get(self, _bus, _sender, _path, _iface, prop):
        empty_pixmaps = GLib.Variant("a(iiay)", [])
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "maxwell-control"),
            "Title": GLib.Variant("s", self.tooltip_title),
            # Always Active: Noctalia hides Passive items by default (hide_passive).
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconThemePath": GLib.Variant("s", ""),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconPixmap": empty_pixmaps,
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": empty_pixmaps,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": empty_pixmaps,
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], self.tooltip_title, self.tooltip_body)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, _bus, _sender, _path, _iface, method, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        # ContextMenu: hosts normally show the dbusmenu themselves. Scroll: nothing to do.
        invocation.return_value(None)

    # -- dbusmenu -------------------------------------------------------------------------------

    def _layout(self, item: MenuItem, depth: int) -> GLib.Variant:
        children = []
        if depth != 0:
            children = [GLib.Variant("v", self._layout(c, depth - 1)) for c in item.children]
        return GLib.Variant("(ia{sv}av)", (item.id, item.props(), children))

    def _menu_get(self, _bus, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _menu_call(self, _bus, _sender, _path, _iface, method, params, invocation) -> None:
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, _names = args
            item = self._index.get(parent, self.root)
            layout = self._layout(item, depth)
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self._revision), layout))
        elif method == "GetGroupProperties":
            ids, _names = args
            ids = ids or list(self._index)
            out = [(i, self._index[i].props()) for i in ids if i in self._index]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            item_id, name = args
            item = self._index.get(item_id)
            value = item.props().get(name) if item else None
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", "no such property")
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event, _data, _ts = args
            self._event(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            errors = []
            for item_id, event, _data, _ts in args[0]:
                if item_id in self._index:
                    self._event(item_id, event)
                else:
                    errors.append(item_id)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _event(self, item_id: int, event: str) -> None:
        item = self._index.get(item_id)
        if event == "clicked" and item and item.on_click and item.enabled:
            GLib.idle_add(lambda: (item.on_click(), False)[1])
