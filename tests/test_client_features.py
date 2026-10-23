"""0.3.0: Spielzeit von GOG, Online-Status, Autostart, Power Search, eigene Dateiverknüpfungen und Konfigurieren."""
from __future__ import annotations

import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux import autostart, power_search, presence
from galaxy_linux.api.client import parse_playtime
from galaxy_linux.api.models import LibraryEntry
from galaxy_linux.content.installer import InstallError
from galaxy_linux.content.manifest import Build
from galaxy_linux.settings import GlobalSettings, InstalledGame


class PlaytimeParseTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_playtime({"time_sum": 125, "last_session_date": 1700000000}), (125, 1700000000.0))
        self.assertEqual(parse_playtime({"time_sum": "7", "last_session_date": "1700000000"}), (7, 1700000000.0))
        from datetime import datetime, timezone
        self.assertEqual(parse_playtime({"time_sum": 3, "last_session_date": "2026-10-01T10:00:00+00:00"})[1],
                         datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parse_playtime({"time_sum": 3, "last_session_date": "gestern"}), (3, 0.0))
        self.assertIsNone(parse_playtime({"items": []}))
        self.assertIsNone(parse_playtime([]))


class PresenceTest(unittest.TestCase):
    def api(self, logged_in=True):
        calls = []
        return calls, types.SimpleNamespace(logged_in=logged_in, set_presence=lambda g="": calls.append(("on", g)) or True,
                                            clear_presence=lambda: calls.append(("off",)) or True)

    def test_online_game_invisible_and_stop(self):
        calls, api = self.api()
        s = types.SimpleNamespace(presence_status="online")
        p = presence.Presence(api, s)
        self.assertEqual(p.tick(), "online")
        p.set_game("1207658924")
        self.assertEqual(p.tick(), "online:1207658924")
        s.presence_status = "invisible"
        self.assertEqual(p.tick(), "offline")
        self.assertEqual(p.tick(), "")                     # nur einmal löschen
        s.presence_status = "online"
        p.set_game("")
        p.tick()
        p.stop()
        self.assertEqual(calls, [("on", ""), ("on", "1207658924"), ("off",), ("on", ""), ("off",)])

    def test_logged_out_and_errors(self):
        calls, api = self.api(logged_in=False)
        self.assertEqual(presence.Presence(api, types.SimpleNamespace()).tick(), "")
        api = types.SimpleNamespace(logged_in=True, set_presence=mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(presence.Presence(api, types.SimpleNamespace()).tick(), "")


class AutostartTest(unittest.TestCase):
    def test_enable_disable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertFalse(autostart.is_enabled())
            f = autostart.set_enabled(True)
            self.assertEqual(f, Path(d) / "autostart" / "org.galaxylinux.Client.desktop")
            text = f.read_text()
            self.assertIn("--gui --minimized", text)
            self.assertIn("Type=Application", text)
            self.assertTrue(autostart.is_enabled())
            f.write_text(text + "Hidden=true\n")
            self.assertFalse(autostart.is_enabled())
            autostart.set_enabled(False)
            self.assertFalse(f.exists())
            autostart.set_enabled(False)                  # zweimal aus ist kein Fehler


class PowerSearchTest(unittest.TestCase):
    def backend(self):
        from galaxy_linux import library_view as lv
        settings = GlobalSettings()
        settings.bookmarks = [{"id": "b1", "name": "Witcher-Sammlung", "view": "all", "search": "witcher"}]
        states = {"1": "installed", "2": "not_installed", "ext-x": "installed"}
        tags = {"2": ["Rollenspiel-Klassiker"]}
        gs = types.SimpleNamespace(get=lambda pid: types.SimpleNamespace(tags=tags.get(pid, []), rating=0, favorite=False,
                                                                         hidden=False, unhidden=False))
        b = types.SimpleNamespace(settings=settings, game_settings=gs, registry=types.SimpleNamespace(get=lambda p: None),
                                  library_of=lambda p: None, playtime=types.SimpleNamespace(get=lambda p: (0, 0.0)),
                                  state=lambda p: states[p], _meta={})
        entries = [LibraryEntry(id="1", title="The Witcher"), LibraryEntry(id="2", title="Albion"),
                   LibraryEntry(id="ext-x", title="Witcher Mod")]
        self.assertTrue(lv)
        return b, entries

    def test_local(self):
        b, entries = self.backend()
        res = power_search.local_results(b, "witcher", entries=entries)
        self.assertEqual([(r.kind, r.title, r.action) for r in res],
                         [("game", "The Witcher", "play"), ("game", "Witcher Mod", "play"),
                          ("bookmark", "Witcher-Sammlung", "show_bookmark")])
        res = power_search.local_results(b, "klassiker", entries=entries)
        self.assertEqual([(r.title, r.action) for r in res], [("Albion", "install")])
        self.assertIn("Rollenspiel-Klassiker", res[0].subtitle)
        self.assertEqual(power_search.local_results(b, "  ", entries=entries), [])
        self.assertEqual(res[0].action_label, "Installieren")

    def test_store(self):
        prods = [{"id": 1, "title": "Owned", "storeLink": "https://www.gog.com/game/o"},
                 {"id": 2, "title": "Neu", "price": {"final": "9,99 €", "discount": "-50%"}, "coverVertical": "c.jpg"},
                 {"id": 3, "title": "Gratis", "price": {"finalMoney": {"amount": "0.00", "currency": "EUR"}}},
                 {"title": "ohne ID"}]
        res = power_search.store_results(prods, owned={"1"})
        self.assertEqual([(r.title, r.action, r.subtitle) for r in res],
                         [("Owned", "show_game", "Spiel im Besitz"), ("Neu", "buy", "9,99 € (-50%)"),
                          ("Gratis", "buy", "0.00 EUR")])


class TasksTest(unittest.TestCase):
    def test_custom_and_additional_tasks(self):
        from galaxy_linux.launch.launcher import Launcher
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / "Demo"
            inst.mkdir()
            (inst / "game.exe").write_bytes(b"MZ")
            (inst / "editor.exe").write_bytes(b"MZ")
            (inst / "manual.pdf").write_bytes(b"%PDF")
            (inst / "goggame-1.info").write_text(json.dumps({"gameId": "1", "playTasks": [
                {"name": "Spiel", "path": "game.exe", "isPrimary": True, "type": "FileTask", "category": "game"},
                {"name": "Editor", "path": "editor.exe", "type": "FileTask", "category": "tool"},
                {"name": "Handbuch", "path": "manual.pdf", "type": "FileTask", "category": "document"},
                {"name": "Forum", "link": "https://www.gog.com/forum", "type": "URLTask", "category": "document"}]}))
            game = InstalledGame("1", "Demo", "windows", str(inst))
            gs = types.SimpleNamespace(custom_tasks=[{"name": "Mit Konsole", "path": "game.exe", "arguments": "-console"},
                                                     {"name": "Fremd", "path": "/opt/tool.exe"}, {"name": "leer"}],
                                       play_task="")
            store = types.SimpleNamespace(get=lambda pid: gs)
            launcher = Launcher.__new__(Launcher)
            launcher.game_settings = store
            tasks = launcher.tasks_for(game)
            self.assertEqual([t.name for t in tasks], ["Spiel", "Editor", "Handbuch", "Mit Konsole", "Fremd"])
            extra = launcher.additional_tasks(game)
            self.assertEqual([t.name for t in extra], ["Editor", "Handbuch", "Forum", "Mit Konsole", "Fremd"])
            self.assertEqual([Launcher.opens_with_desktop(t) for t in extra], [False, True, True, False, False])
            self.assertEqual(launcher.task_path(game, extra[4]), Path("/opt/tool.exe"))
            self.assertEqual(launcher.task_path(game, extra[3]), inst / "game.exe")
            gs.play_task = "Mit Konsole"
            self.assertEqual(launcher.select_task(game, gs).arguments, "-console")
            self.assertIn("Spiel", [t.name for t in launcher.additional_tasks(game)])


class ConfigureBackendTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        cfg, data, cache = base / "cfg", base / "data", base / "cache"
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        from galaxy_linux.backend import Backend
        with mock.patch("galaxy_linux.launch.launcher.Launcher.tools", return_value=[]):
            self.b = Backend()
        self.jobs = []
        self.p2 = mock.patch.object(self.b.jobs, "add", side_effect=lambda j: self.jobs.append(j) or j)
        self.p2.start()
        self.b.registry.set(InstalledGame("1", "Demo", "windows", str(base / "Demo"), build_id="5",
                                          installed_dlcs=["7", "8"], method="content-system"))
        self.b.library = [LibraryEntry(id="1", title="Demo"), LibraryEntry(id="7", title="DLC A"),
                          LibraryEntry(id="8", title="DLC B"), LibraryEntry(id="9", title="DLC C")]
        product = {"id": 1, "expanded_dlcs": [{"id": 7, "title": "DLC A"}, {"id": 8, "title": "DLC B"},
                                              {"id": 9, "title": "DLC C"}, {"id": 10, "title": "Nicht im Besitz"}]}
        self.p3 = mock.patch.object(self.b.api, "product", return_value=product)
        self.p3.start()
        self.b.installer._owned = {"1", "7", "8", "9"}
        self.b.api.creds = object()

    def tearDown(self):
        self.b.api.creds = None
        self.p3.stop()
        self.p2.stop()
        self.patch.stop()
        self.td.cleanup()

    def test_dlcs_language_build(self):
        self.assertEqual(self.b.owned_dlcs("1"), [("7", "DLC A", True), ("8", "DLC B", True), ("9", "DLC C", False)])
        job = self.b.change_dlcs("1", ["7", "9"])
        self.assertEqual((job.kind, job.dlc_filter, job.build_id), ("update", ["7", "9"], "5"))
        self.assertEqual(self.b.game_settings.get("1").dlc_excluded, ["8"])
        with mock.patch.object(self.b.jobs, "active_for", return_value=job):
            self.assertIs(self.b.change_dlcs("1", ["8"]), job)              # wartender Auftrag übernimmt die Auswahl
        self.assertEqual((job.dlc_filter, len(self.jobs)), (["8"], 1))
        self.assertEqual(self.b.game_settings.get("1").dlc_excluded, ["7", "9"])
        job = self.b.change_language("1", "pl-PL")
        self.assertEqual((job.language, job.build_id, job.data["reason"]), ("pl-PL", "5", "language"))
        builds = [Build("9", 2), Build("8", 2, branch="beta"), Build("3", 1)]
        with mock.patch.object(self.b.installer, "builds", return_value=builds):
            self.assertEqual([b.build_id for b in self.b.available_builds("1")], ["9", "8"])
            self.assertEqual(self.b.change_branch("1", "beta").build_id, "8")
            self.assertEqual(self.b.change_branch("1", None).build_id, "9")
            with self.assertRaises(InstallError):
                self.b.change_branch("1", "alpha")

    def test_private_channel(self):
        def builds(pid, plat, pw=""):
            out = [Build("9", 2), Build("8", 2, branch="beta")]
            return out + ([Build("7", 2, branch="intern")] if pw == "richtig" else [])
        with mock.patch.object(self.b.installer, "builds", side_effect=builds):
            with self.assertRaises(InstallError):
                self.b.add_private_channel("1", "falsch")
            self.assertEqual(self.b.add_private_channel("1", "richtig"), ["intern"])
        self.assertEqual(self.b.game_settings.get("1").branch_password, "richtig")

    def test_not_configurable(self):
        self.b.registry.set(InstalledGame("2", "Linux", "linux", "/x", method="linux-installer"))
        with self.assertRaises(InstallError):
            self.b.change_language("2", "de-DE")
        with self.assertRaises(InstallError):
            self.b.change_language("404", "de-DE")

    def test_auto_update_respects_game_choice(self):
        self.b.game_settings.update("1", auto_update=False)
        with mock.patch.object(self.b, "update") as upd:
            self.assertEqual(self.b.auto_update({"1": Build("9", 2)}), [])
            self.b.game_settings.update("1", auto_update=True)
            self.assertEqual(self.b.auto_update({"1": Build("9", 2)}), ["1"])
        self.assertEqual(upd.call_count, 1)

    def test_pull_playtime(self):
        self.b.api.user = {"userId": "42"}
        answers = {"1": (300, 1700000000.0), "7": None}
        with mock.patch.object(self.b.api, "playtime", side_effect=lambda pid: answers.get(pid)) as pt:
            self.assertEqual(self.b.pull_playtime(["1", "7", "ext-a", "1"]), 1)
            self.assertEqual(self.b.playtime.get("1"), (300, 1700000000.0))
            self.assertEqual(self.b.pull_playtime(["1"]), 0)              # gerade erst gefragt
            self.assertEqual(pt.call_count, 2)
            self.b.settings.sync_playtime = False
            self.assertEqual(self.b.pull_playtime(["1"], max_age=0), 0)


if __name__ == "__main__":
    unittest.main()
