"""System-Tray (StatusNotifierItem + dbusmenu) gegen einen eigenen dbus-daemon mit nachgebautem
StatusNotifierWatcher – berührt den Sitzungsbus des Desktops nicht."""
from __future__ import annotations

import shutil
import subprocess
import time
import unittest

try:
    from gi.repository import Gio, GLib
    from galaxy_linux.ui import tray
except Exception:  # pragma: no cover – ohne PyGObject
    tray = None

WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
 <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
</interface></node>"""


def spin(cond, timeout=3.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        ctx.iteration(False)
        time.sleep(0.01)
    return cond()


@unittest.skipIf(tray is None or not shutil.which("dbus-daemon"), "PyGObject oder dbus-daemon fehlt")
class TrayTest(unittest.TestCase):
    def setUp(self):
        self.daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                                       stdout=subprocess.PIPE, text=True)
        self.address = self.daemon.stdout.readline().strip()

        def conn():
            return Gio.DBusConnection.new_for_address_sync(
                self.address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        self.conn = conn
        self.registered: list[str] = []

    def tearDown(self):
        self.daemon.terminate()
        self.daemon.wait(5)

    def start_watcher(self):
        w = self.conn()
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def call(_c, _s, _p, _i, method, params, inv):
            self.registered.append(params.unpack()[0])
            inv.return_value(None)
        w.register_object(tray.WATCHER_PATH, info, call, None, None)
        res = w.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                          GLib.Variant("(su)", (tray.WATCHER, 0)), None, Gio.DBusCallFlags.NONE, 2000, None)
        self.assertEqual(res.unpack()[0], 1)
        return w

    def test_layout_variant(self):
        v = tray.menu_layout(["Öffnen", "Beenden"], revision=4)
        rev, (root, props, children) = v.unpack()
        self.assertEqual((rev, root, props["children-display"]), (4, 0, "submenu"))
        self.assertEqual([(c[0], c[1]["label"]) for c in children], [(1, "Öffnen"), (2, "Beenden")])

    def test_without_watcher_not_shown(self):
        t = tray.Tray("org.test.App", "Test", "icon", lambda: None, lambda: None, ("Öffnen", "Beenden"))
        t.bus = self.conn()
        self.assertFalse(t.available())
        self.assertFalse(t.show())

    def test_register_menu_and_clicks(self):
        self.watcher = self.start_watcher()          # Verbindung festhalten, sonst verschwindet der Name wieder
        calls = []
        t = tray.Tray("org.test.App", "Test", "org.test.App", lambda: calls.append("open"), lambda: calls.append("quit"),
                      ("Öffnen", "Beenden"))
        t.bus = self.conn()
        self.assertTrue(t.show())
        self.assertTrue(spin(lambda: self.registered == [t.name]))
        client = self.conn()

        def call(name, path, iface, method, args):
            """Asynchron rufen und die Hauptschleife drehen – der Tray antwortet im selben Thread."""
            box = []
            client.call(name, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 2000, None,
                        lambda c, res: box.append(c.call_finish(res)))
            self.assertTrue(spin(lambda: bool(box)))
            return box[0].unpack()

        self.assertTrue(spin(lambda: call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "NameHasOwner", GLib.Variant("(s)", (t.name,)))[0]))

        def get(path, iface, prop):
            return call(t.name, path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, prop)))[0]
        self.assertEqual(get(tray.ITEM_PATH, "org.kde.StatusNotifierItem", "IconName"), "org.test.App")
        self.assertEqual(get(tray.ITEM_PATH, "org.kde.StatusNotifierItem", "Menu"), tray.MENU_PATH)
        layout = call(t.name, tray.MENU_PATH, "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        self.assertEqual([c[1]["label"] for c in layout[1][2]], ["Öffnen", "Beenden"])
        call(t.name, tray.MENU_PATH, "com.canonical.dbusmenu", "Event",
             GLib.Variant("(isvu)", (2, "clicked", GLib.Variant("i", 0), 0)))
        call(t.name, tray.ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (0, 0)))
        self.assertTrue(spin(lambda: calls == ["quit", "open"]))
        t.hide()
        self.assertFalse(t.shown)


if __name__ == "__main__":
    unittest.main()
