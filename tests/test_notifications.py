"""Benachrichtigungszentrum: Speicher mit Grenzen (200 Meldungen, 30 Tage), Doppelte über Schlüssel, gelesen,
„Alle löschen“, Weiterleitung je Typ (Desktop und Zentrum, nur Zentrum, aus) und Fensterfokus – ohne GTK."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from galaxy_linux import notifications as nt

DAY = 86400


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "notifications.json"
        self.now = [1_800_000_000.0]

    def tearDown(self):
        self.td.cleanup()

    def store(self, **kw):
        return nt.NotificationStore(self.path, clock=lambda: self.now[0], **kw)

    def test_add_newest_first_and_persist(self):
        s = self.store()
        changes = []
        s.listeners.append(lambda: changes.append(1))
        a = s.add("download", "Spiel A", "Installation abgeschlossen", action="game:1", product_id=1)
        self.now[0] += 5
        b = s.add("wishlist", "Spiel B", "-50%", action="store:/game/b")
        self.assertEqual([n.id for n in s.items()], [b.id, a.id])
        self.assertEqual(s.unread_count(), 2)
        self.assertEqual(a.product_id, "1")
        self.assertEqual(len(changes), 2)
        again = self.store()
        self.assertEqual([(n.title, n.action) for n in again.items()], [("Spiel B", "store:/game/b"),
                                                                         ("Spiel A", "game:1")])

    def test_limit_count(self):
        s = self.store(max_items=5)
        for i in range(8):
            self.now[0] += 1
            s.add("download", f"N{i}")
        self.assertEqual([n.title for n in s.items()], ["N7", "N6", "N5", "N4", "N3"])
        self.assertEqual(nt.MAX_ITEMS, 200)
        self.assertEqual(len(json.loads(self.path.read_text())["items"]), 5)

    def test_limit_age(self):
        s = self.store()
        s.add("download", "alt", key="k-alt")
        self.now[0] += 10 * DAY
        s.add("download", "mittel")
        self.now[0] += 21 * DAY                 # „alt“ ist jetzt 31 Tage alt
        self.assertEqual([n.title for n in s.items()], ["mittel"])
        self.assertIsNotNone(s.add("download", "alt wieder", key="k-alt"))      # Schlüssel ist mit abgelaufen
        self.now[0] += 30 * DAY + 1
        self.assertEqual([n.title for n in self.store().items()], [])

    def test_keys_prevent_duplicates_even_after_clear(self):
        s = self.store()
        self.assertIsNotNone(s.add("update", "Updates", key="updates:1@b1"))
        self.assertIsNone(s.add("update", "Updates", key="updates:1@b1"))
        self.assertEqual(s.clear(), 1)
        self.assertEqual(s.items(), [])
        self.assertIsNone(self.store().add("update", "Updates", key="updates:1@b1"))
        self.assertIsNotNone(s.add("update", "Updates", key="updates:1@b2"))
        self.assertIsNotNone(s.add("cloud", "ohne Schlüssel"))
        self.assertIsNotNone(s.add("cloud", "ohne Schlüssel"))

    def test_mark_read_and_remove(self):
        s = self.store()
        a = s.add("download", "A")
        b = s.add("download", "B")
        self.assertEqual(s.mark_read(a.id), 1)
        self.assertEqual(s.mark_read(a.id), 0)
        self.assertEqual(s.unread_count(), 1)
        self.assertEqual(s.mark_read(), 1)
        self.assertEqual(s.unread_count(), 0)
        self.assertTrue(self.store().get(b.id).read)
        self.assertTrue(s.remove(a.id))
        self.assertFalse(s.remove(a.id))
        self.assertEqual([n.id for n in s.items()], [b.id])

    def test_broken_file(self):
        self.path.write_text('{"items": [{"id": "x"}, {"id": "y", "title": "Gut", "created": "kaputt"}, 5,'
                             ' {"id": "z", "title": "Ok", "created": 1800000000}], "keys": {"a": "x", "b": 1800000000}}')
        s = self.store()
        self.assertEqual([n.id for n in s.items()], ["z"])
        self.assertIsNone(s.add("x", "doppelt", key="b"))
        self.path.write_text("kein json")
        self.assertEqual(self.store().items(), [])


class RouteTest(unittest.TestCase):
    def test_types_registered(self):
        keys = [t.key for t in nt.types()]
        self.assertEqual(keys[:4], ["downloads", "wishlist", "library", "cloud"])
        self.assertEqual(nt.type_for_kind("update").key, "downloads")
        self.assertEqual(nt.type_for_kind("redist").key, "downloads")
        self.assertEqual(nt.type_for_kind("available").key, "wishlist")
        self.assertIsNone(nt.type_for_kind("unbekannt"))

    def test_mode_for(self):
        s = SimpleNamespace(notify_download_status="none", notify_wishlist="center", notify_new_games="kaputt",
                            notify_cloud_saves="desktop")
        self.assertEqual(nt.mode_for(s, "download"), "none")
        self.assertEqual(nt.mode_for(s, "wishlist"), "center")
        self.assertEqual(nt.mode_for(s, "library"), "desktop")        # unbekannter Wert → Vorgabe
        self.assertEqual(nt.mode_for(s, "cloud"), "desktop")
        self.assertEqual(nt.mode_for(s, "ganz-neu"), "desktop")
        self.assertEqual(nt.mode_for(SimpleNamespace(), "wishlist"), "desktop")

    def test_route(self):
        self.assertEqual(nt.route("desktop", window_focused=False), (True, True))
        self.assertEqual(nt.route("desktop", window_focused=True), (True, False))
        self.assertEqual(nt.route("desktop", window_focused=True, focus_desktop=True), (True, True))
        self.assertEqual(nt.route("center", window_focused=False), (True, False))
        self.assertEqual(nt.route("none", window_focused=False), (False, False))

    def test_register_type(self):
        # eigener Testtyp – die echten Freunde-Typen (galaxy_linux/friends.py) bleiben unberührt
        try:
            nt.register_type(nt.NotificationType("testtyp", "Test", "notify_testtyp", order=999, focus_desktop=True))
            nt.register_kind("test-kind", "testtyp")
            self.assertEqual(nt.type_for_kind("test-kind").setting, "notify_testtyp")
            self.assertEqual([t.key for t in nt.types()][-1], "testtyp")
            self.assertEqual(nt.mode_for(SimpleNamespace(notify_testtyp="center"), "test-kind"), "center")
        finally:
            nt._types.pop("testtyp", None)
            nt.KIND_TYPE.pop("test-kind", None)


if __name__ == "__main__":
    unittest.main()
