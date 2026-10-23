"""Tests für die Erweiterungspunkte der Oberfläche (ohne GTK): Einstellungsgruppen, Spiel-Aktionen,
Spiel-Abschnitte, Fenster-Hooks."""
from __future__ import annotations

import unittest
from unittest import mock

from galaxy_linux.ui import prefs_registry as pr
from galaxy_linux.ui import ui_registry as ur


class PrefsRegistryTest(unittest.TestCase):
    def setUp(self):
        self.p1 = mock.patch.dict(pr._global, clear=True)
        self.p2 = mock.patch.dict(pr._game, clear=True)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p2.stop()
        self.p1.stop()

    def test_pages(self):
        self.assertEqual(pr.PAGES, ("general", "interface", "notifications", "downloads", "features", "libraries", "compat"))
        self.assertIn("features", pr.GAME_PAGES)
        with self.assertRaises(ValueError):
            pr.register("unbekannt", lambda d: None)
        with self.assertRaises(ValueError):
            pr.register_game("unbekannt", lambda d, p: None)

    def test_order_replace_and_build(self):
        pr.register("features", lambda d: ("b", d), order=20, key="b")
        pr.register("features", lambda d: ("a", d), order=10, key="a")
        pr.register("features", lambda d: None, order=5, key="leer")

        def broken(d):
            raise RuntimeError("kaputt")
        pr.register("features", broken, order=1, key="kaputt")
        with self.assertLogs("galaxy_linux.ui.prefs_registry", "ERROR"):
            groups = pr.build_groups("features", "DLG")
        self.assertEqual(groups, [("a", "DLG"), ("b", "DLG")])
        pr.register("features", lambda d: ("b2", d), order=30, key="b")       # gleicher Schlüssel ersetzt
        self.assertEqual(len(pr.builders("features")), 4)
        self.assertEqual(pr.build_groups("general", "DLG"), [])
        pr.unregister("kaputt")
        self.assertEqual(pr.build_groups("features", "X"), [("a", "X"), ("b2", "X")])

    def test_game_groups_get_product_id(self):
        pr.register_game("features", lambda d, pid: (d, pid))
        self.assertEqual(pr.build_game_groups("features", "DLG", 123), [("DLG", "123")])
        self.assertEqual(pr.build_game_groups("compat", "DLG", "1"), [])

    def test_default_key_from_function(self):
        def builder(d):
            return d
        pr.register("compat", builder)
        pr.register("compat", builder)
        self.assertEqual(len(pr.builders("compat")), 1)

    def test_extension_module_names(self):
        self.assertEqual(pr.extension_modules(["window", "prefs_registry", "prefs_cloud", "ext_store", "ext_a", "bridge"]),
                         ["ext_a", "ext_store", "prefs_cloud"])

    def test_load_extensions_once(self):
        mods = [mock.Mock(), mock.Mock(), mock.Mock()]
        for m, name in zip(mods, ("ext_demo", "prefs_registry", "window")):
            m.name = name
        with mock.patch.object(pr, "_loaded", False), mock.patch.object(pr.importlib, "import_module") as imp, \
             mock.patch.object(pr.pkgutil, "iter_modules", return_value=mods):
            pr.load_extensions()
            pr.load_extensions()
            self.assertTrue(pr._loaded)
        imp.assert_called_once_with("galaxy_linux.ui.ext_demo")


class UiRegistryTest(unittest.TestCase):
    def setUp(self):
        self.ps = [mock.patch.dict(d, clear=True) for d in (ur._actions, ur._sections, ur._window_hooks)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def test_game_actions(self):
        called = []
        ur.register_game_action("b", "B", lambda page, pid: called.append(("b", page, pid)), order=20)
        ur.register_game_action("a", "A", lambda page, pid: called.append(("a", page, pid)),
                                visible=lambda page, pid: pid == "1", order=10)
        acts = ur.game_actions()
        self.assertEqual([a.key for a in acts], ["a", "b"])
        self.assertTrue(ur.action_visible(acts[0], "P", "1"))
        self.assertFalse(ur.action_visible(acts[0], "P", "2"))
        self.assertFalse(ur.action_visible(acts[1], "P", ""))            # ohne Spiel nie sichtbar
        ur.run_action(acts[1], "P", "7")
        self.assertEqual(called, [("b", "P", "7")])

    def test_errors_are_isolated(self):
        def boom(*_a):
            raise RuntimeError("x")
        ur.register_game_action("x", "X", boom, visible=boom)
        act = ur.game_actions()[0]
        with self.assertLogs("galaxy_linux.ui.ui_registry", "ERROR"):
            self.assertFalse(ur.action_visible(act, "P", "1"))
            ur.run_action(act, "P", "1")

    def test_sections(self):
        class Box:
            def __init__(self, name):
                self.name, self.games, self.states = name, [], []

            def set_game(self, pid):
                self.games.append(pid)

            def refresh_state(self, pid):
                self.states.append(pid)
        ur.register_game_section("r", lambda page: Box("r"), column="right")
        ur.register_game_section("l2", lambda page: Box("l2"), order=20)
        ur.register_game_section("l1", lambda page: Box("l1"), order=10)
        ur.register_game_section("none", lambda page: None)
        with self.assertRaises(ValueError):
            ur.register_game_section("x", lambda page: None, column="mitte")
        left = ur.build_sections("left", "PAGE")
        self.assertEqual([b.name for b in left], ["l1", "l2"])
        self.assertEqual([b.name for b in ur.build_sections("right", "PAGE")], ["r"])
        ur.notify_sections(left + [object()], "set_game", "5")
        ur.notify_sections(left, "refresh_state", "5")
        self.assertEqual(left[0].games, ["5"])
        self.assertEqual(left[1].states, ["5"])

    def test_window_hooks(self):
        seen = []
        ur.register_window_hook("b", lambda w: seen.append(("b", w)), order=2)
        ur.register_window_hook("a", lambda w: seen.append(("a", w)), order=1)

        def boom(w):
            raise RuntimeError("x")
        ur.register_window_hook("c", boom, order=3)
        with self.assertLogs("galaxy_linux.ui.ui_registry", "ERROR"):
            self.assertEqual(ur.run_window_hooks("WIN"), ["a", "b"])
        self.assertEqual(seen, [("a", "WIN"), ("b", "WIN")])
        ur.unregister("a")
        self.assertEqual(sorted(ur._window_hooks), ["b", "c"])


class LaunchEventTest(unittest.TestCase):
    """Launch-Hooks melden Probleme (Cloud, Redistributables) an das Benachrichtigungszentrum – ohne Oberfläche nichts."""

    def ctx(self, bridge):
        from galaxy_linux.launch.hooks import LaunchContext
        return LaunchContext(product_id="7", game=mock.Mock(), gs=mock.Mock(), settings=mock.Mock(), plan=None,
                             install_dir=mock.Mock(), exe=None, prefix=mock.Mock(), runner="native", tool=None,
                             log_file=mock.Mock(), status=lambda t: None, ask=lambda *a: "",
                             launcher=mock.Mock(ui_bridge=bridge))

    def test_event_reaches_bridge(self):
        bridge = mock.Mock()
        self.ctx(bridge).event("cloud", "Titel", "Text")
        bridge.post.assert_called_once_with("cloud", "Titel", "Text", "7")

    def test_console_bridge_without_post(self):
        from galaxy_linux.launch.hooks import ConsoleBridge
        self.ctx(ConsoleBridge()).event("redist", "Titel")        # kein Fehler, keine Ausgabe nötig
        bridge = mock.Mock()
        bridge.post.side_effect = RuntimeError("weg")
        self.ctx(bridge).event("cloud", "Titel")                 # Fehler der Oberfläche halten den Start nicht auf


if __name__ == "__main__":
    unittest.main()
