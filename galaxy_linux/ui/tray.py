"""Symbol im System-Tray wie Galaxy („Öffnen“ / „Beenden“) – über StatusNotifierItem (KDE Plasma, und überall, wo
ein StatusNotifierWatcher läuft) mit einem kleinen com.canonical.dbusmenu. GTK4 selbst kennt keinen Tray.

Nur Gio/GLib; ohne Watcher (z. B. GNOME ohne Erweiterung, unsichtbare Testsitzung) meldet ``available()`` False –
dann gilt „Im System-Tray minimieren“ wie „In Taskleiste minimieren“.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
   <arg type="as" name="propertyNames" direction="in"/>
   <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
   <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/><arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
   <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/></method>
  <method name="AboutToShowGroup">
   <arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/>
   <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/></signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
 </interface>
</node>"""


def item_props(label: str) -> dict:
    return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}


def menu_layout(labels: list[str], revision: int = 1) -> GLib.Variant:
    """Antwort auf GetLayout: Wurzel 0 mit den Einträgen 1…n."""
    children = [GLib.Variant("(ia{sv}av)", (i + 1, item_props(lbl), [])) for i, lbl in enumerate(labels)]
    return GLib.Variant("(u(ia{sv}av))", (revision, (0, {"children-display": GLib.Variant("s", "submenu")}, children)))


def watcher_available(bus: Gio.DBusConnection | None = None) -> bool:
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                            GLib.Variant("(s)", (WATCHER,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        return bool(res.unpack()[0])
    except GLib.Error:
        return False


class Tray:
    """Tray-Symbol mit „Öffnen“ (auch Linksklick) und „Beenden“. ``show()``/``hide()`` jederzeit (Hauptthread)."""

    def __init__(self, app_id: str, title: str, icon_name: str, on_activate: Callable[[], None],
                 on_quit: Callable[[], None], labels: tuple[str, str]) -> None:
        self.app_id, self.title, self.icon_name = app_id, title, icon_name
        self.on_activate, self.on_quit = on_activate, on_quit
        self.labels = list(labels)
        self.bus: Gio.DBusConnection | None = None
        self._regs: list[int] = []
        self._owner = 0
        self._watch = 0
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.shown = False

    def available(self) -> bool:
        return watcher_available(self.bus)

    def show(self) -> bool:
        if self.shown:
            return True
        try:
            self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            log.info("Kein Sitzungsbus für das Tray-Symbol: %s", exc)
            return False
        if not watcher_available(self.bus):
            log.info("Kein System-Tray (StatusNotifierWatcher) gefunden")
            return False
        try:
            sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self._regs = [self.bus.register_object(ITEM_PATH, sni, self._on_sni_call, self._on_sni_prop, None),
                          self.bus.register_object(MENU_PATH, menu, self._on_menu_call, self._on_menu_prop, None)]
        except GLib.Error as exc:
            log.warning("Tray-Symbol nicht registrierbar: %s", exc)
            self._unregister()
            return False
        self._owner = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self._register_with_watcher()
        # startet der Watcher neu (Plasma-Neustart), meldet sich das Symbol wieder an
        self._watch = Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       lambda *_: self._register_with_watcher(), None)
        self.shown = True
        log.info("Tray-Symbol angemeldet (%s)", self.name)
        return True

    def _register_with_watcher(self) -> None:
        if self.bus is None:
            return
        self.bus.call(WATCHER, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)),
                      None, Gio.DBusCallFlags.NONE, 2000, None, None, None)

    def _unregister(self) -> None:
        for r in self._regs:
            if r and self.bus is not None:
                self.bus.unregister_object(r)
        self._regs = []

    def hide(self) -> None:
        if not self.shown:
            return
        self._unregister()
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = 0
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        self.shown = False

    # ------------------------------------------------------------------ StatusNotifierItem
    def _on_sni_prop(self, _conn, _sender, _path, _iface, name):
        values = {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", self.app_id),
                  "Title": GLib.Variant("s", self.title), "Status": GLib.Variant("s", "Active"),
                  "WindowId": GLib.Variant("i", 0), "IconName": GLib.Variant("s", self.icon_name),
                  "IconThemePath": GLib.Variant("s", ""),
                  "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], self.title, "")),
                  "ItemIsMenu": GLib.Variant("b", False), "Menu": GLib.Variant("o", MENU_PATH)}
        return values.get(name)

    def _on_sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        invocation.return_value(None)

    # ------------------------------------------------------------------ dbusmenu
    def _on_menu_prop(self, _conn, _sender, _path, _iface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        if method == "GetLayout":
            invocation.return_value(menu_layout(self.labels))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            props = [(i, item_props(self.labels[i - 1])) for i in ids if 1 <= i <= len(self.labels)]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            props = item_props(self.labels[i - 1]) if 1 <= i <= len(self.labels) else {}
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event = params.unpack()[:2]
            if event == "clicked":
                self._clicked(i)
            invocation.return_value(None)
        elif method == "EventGroup":
            for i, event, _d, _t in params.unpack()[0]:
                if event == "clicked":
                    self._clicked(i)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, item_id: int) -> None:
        cb = self.on_activate if item_id == 1 else self.on_quit if item_id == 2 else None
        if cb is not None:
            GLib.idle_add(lambda: (cb(), False)[1])
