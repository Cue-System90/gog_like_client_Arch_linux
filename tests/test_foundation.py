"""Tests für das Fundament von 0.2.0: neue Einstellungsfelder (Vorgaben, alte Dateien, Kommandozeile),
Auftragsarten der Warteschlange, Update-Suche des Clients, Änderungsprotokoll und Store-Adressen."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from galaxy_linux import __version__, clientinfo, store_links
from galaxy_linux.settings import GameSettings, GameSettingsStore, GlobalSettings, coerce_value, resolve_switch

# Ausschnitt einer settings.json/games.json aus 0.1.6 (ohne die neuen Felder)
OLD_SETTINGS = {"install_dir": "/spiele", "prefix_dir": "/spiele/prefixes", "default_compat_tool": "auto",
                "sync_playtime": False, "libraries": [], "library_layout": "list", "external_games_dir": "/alt"}
OLD_GAME = {"compat_tool": "proton:/opt/p", "launch_options": "mangohud %command%", "redist_done": ["DirectX"],
            "wayland_driver": "off", "favorite": True}

NEW_GLOBAL = {"library_sync_interval": 10, "library_sync_on_focus": True, "library_new_ids": {}, "library_checksum": "",
              "feature_cloud_saves": True, "feature_galaxy_services": True, "feature_achievements": True,
              "feature_auto_update": False, "goverlay_register_folders": True, "goverlay_use_profiles": True,
              "goverlay_global_fallback": False, "goverlay_added_folders": [], "comet_path": "", "store_visible": True,
              "store_last_url": ""}
NEW_GAME = {"cloud_saves": "", "galaxy_services": "", "goverlay_profile": "", "cloud_timestamps": {}, "setup_done": []}


class TempPaths(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        cfg, data, cache = self.base / "cfg", self.base / "data", self.base / "cache"
        self.cfg = cfg
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        cfg.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()


# ----------------------------------------------------------------------------- Einstellungen
class SettingsFieldsTest(TempPaths):
    def test_defaults(self):
        s, g = GlobalSettings(), GameSettings()
        for k, v in NEW_GLOBAL.items():
            self.assertEqual(getattr(s, k), v, k)
        for k, v in NEW_GAME.items():
            self.assertEqual(getattr(g, k), v, k)
        # veränderliche Vorgaben werden nicht geteilt
        s.library_new_ids["1"] = 1.0
        self.assertEqual(GlobalSettings().library_new_ids, {})

    def test_load_old_settings(self):
        (self.cfg / "settings.json").write_text(json.dumps(OLD_SETTINGS))
        s = GlobalSettings.load()
        self.assertEqual(s.install_dir, "/spiele")
        self.assertFalse(s.sync_playtime)
        self.assertEqual(s.library_layout, "list")
        for k, v in NEW_GLOBAL.items():
            self.assertEqual(getattr(s, k), v, k)
        s.save()
        raw = json.loads((self.cfg / "settings.json").read_text())
        self.assertEqual(raw["library_sync_interval"], 10)
        self.assertNotIn("external_games_dir", raw)

    def test_wrong_types_fall_back(self):
        (self.cfg / "settings.json").write_text(json.dumps(dict(OLD_SETTINGS, library_new_ids=None, goverlay_added_folders="x")))
        s = GlobalSettings.load()
        self.assertEqual(s.library_new_ids, {})
        self.assertEqual(s.goverlay_added_folders, [])

    def test_game_settings_old_file(self):
        (self.cfg / "games.json").write_text(json.dumps({"1": OLD_GAME, "2": dict(OLD_GAME, cloud_timestamps=None, unbekannt=1)}))
        store = GameSettingsStore()
        g = store.get("1")
        self.assertEqual((g.compat_tool, g.redist_done, g.favorite), ("proton:/opt/p", ["DirectX"], True))
        for k, v in NEW_GAME.items():
            self.assertEqual(getattr(g, k), v, k)
        self.assertEqual(store.get("2").cloud_timestamps, {})
        store.update("1", cloud_saves="off", setup_done=["100:5555"])
        raw = json.loads((self.cfg / "games.json").read_text())
        self.assertEqual(raw["1"]["cloud_saves"], "off")
        self.assertEqual(raw["1"]["launch_options"], "mangohud %command%")

    def test_other_process_changes_survive(self):
        """Oberfläche mit altem Stand im Speicher und Kommandozeile schreiben dieselben Dateien."""
        gui_games = GameSettingsStore()
        cli_games = GameSettingsStore()                        # „anderer Prozess“
        cli_games.update("X", setup_done=["X:5"], cloud_timestamps={"saves": 3.0})
        gui_games.update("Y", favorite=True)                  # GUI kennt X noch nicht
        raw = json.loads((self.cfg / "games.json").read_text())
        self.assertEqual(raw["X"]["setup_done"], ["X:5"])
        self.assertTrue(raw["Y"]["favorite"])
        self.assertEqual(gui_games.get("X").cloud_timestamps, {"saves": 3.0})   # liest geänderte Datei neu
        gui = GlobalSettings.load()
        cli = GlobalSettings.load()
        cli.library_sync_interval = 0
        cli.save()
        gui.ui_language = "en"
        gui.save()                                             # darf das Intervall nicht zurücksetzen
        raw = json.loads((self.cfg / "settings.json").read_text())
        self.assertEqual((raw["library_sync_interval"], raw["ui_language"]), (0, "en"))
        gui.library_sync_interval = 30                         # eigene Änderung gewinnt
        gui.save()
        self.assertEqual(json.loads((self.cfg / "settings.json").read_text())["library_sync_interval"], 30)

    def test_auto_update_skips_running_and_busy_games(self):
        from galaxy_linux.backend import Backend
        queued = []
        fake = types.SimpleNamespace(
            launcher=types.SimpleNamespace(is_running=lambda pid: pid == "laeuft"),
            jobs=types.SimpleNamespace(active_for=lambda pid: object() if pid == "busy" else None),
            registry=types.SimpleNamespace(is_installed=lambda pid: pid != "weg"),
            update=lambda pid, build_id="": queued.append((pid, build_id)))
        found = {k: types.SimpleNamespace(build_id=f"b-{k}") for k in ("ok", "laeuft", "busy", "weg")}
        self.assertEqual(Backend.auto_update(fake, found), ["ok"])
        self.assertEqual(queued, [("ok", "b-ok")])

    def test_concurrent_updates_keep_all_fields(self):
        store = GameSettingsStore()
        fields = [("cloud_saves", "on"), ("galaxy_services", "off"), ("goverlay_profile", "none"), ("hidden", True),
                  ("favorite", True), ("pre_launch", "true"), ("umu_game_id", "umu-9"), ("language", "de-DE")]
        start = threading.Barrier(len(fields))

        def worker(k, v):
            start.wait()
            for _ in range(20):
                store.update("7", **{k: v})
        threads = [threading.Thread(target=worker, args=kv) for kv in fields]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        g = GameSettingsStore().get("7")
        for k, v in fields:
            self.assertEqual(getattr(g, k), v, k)

    def test_resolve_switch(self):
        self.assertTrue(resolve_switch("", True))
        self.assertFalse(resolve_switch("", False))
        self.assertTrue(resolve_switch("on", False))
        self.assertFalse(resolve_switch("off", True))

    def test_coerce_value(self):
        self.assertIs(coerce_value(True, "nein"), False)
        self.assertIs(coerce_value(False, "ja"), True)
        self.assertEqual(coerce_value(10, "15"), 15)
        self.assertEqual(coerce_value({}, '{"1": 2.5}'), {"1": 2.5})
        self.assertEqual(coerce_value([], '["/a"]'), ["/a"])
        self.assertEqual(coerce_value("", "x"), "x")
        with self.assertRaises(ValueError):
            coerce_value([], '{"a": 1}')
        with self.assertRaises(ValueError):
            coerce_value(10, "zehn")


class CliConfigTest(TempPaths):
    def run_cli(self, *argv):
        from galaxy_linux import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_global_new_fields(self):
        (self.cfg / "settings.json").write_text(json.dumps(OLD_SETTINGS))
        self.assertEqual(self.run_cli("config", "global", "library_sync_interval", "15")[0], 0)
        self.run_cli("config", "global", "feature_auto_update", "true")
        self.run_cli("config", "global", "goverlay_added_folders", '["/spiele"]')
        self.run_cli("config", "global", "library_new_ids", '{"100": 1700000000}')
        self.run_cli("config", "global", "comet_path", "/opt/comet")
        raw = json.loads((self.cfg / "settings.json").read_text())
        self.assertEqual(raw["library_sync_interval"], 15)
        self.assertTrue(raw["feature_auto_update"])
        self.assertEqual(raw["goverlay_added_folders"], ["/spiele"])
        self.assertEqual(raw["library_new_ids"], {"100": 1700000000})
        self.assertEqual(raw["comet_path"], "/opt/comet")
        rc, out = self.run_cli("config", "global", "library_sync_interval")
        self.assertEqual(out.strip(), "15")
        rc, out = self.run_cli("config", "global")
        self.assertIn('"store_visible": true', out)
        with self.assertRaises(SystemExit):
            self.run_cli("config", "global", "goverlay_added_folders", '{"a": 1}')

    def test_game_set_new_fields(self):
        (self.cfg / "settings.json").write_text(json.dumps(OLD_SETTINGS))
        self.run_cli("config", "set", "123", "cloud_saves", "off")
        self.run_cli("config", "set", "123", "cloud_timestamps", '{"saves": 12.5}')
        self.run_cli("config", "set", "123", "setup_done", '["123:5555"]')
        raw = json.loads((self.cfg / "games.json").read_text())["123"]
        self.assertEqual((raw["cloud_saves"], raw["cloud_timestamps"], raw["setup_done"]), ("off", {"saves": 12.5}, ["123:5555"]))


# ----------------------------------------------------------------------------- Auftragsarten
class JobKindTest(unittest.TestCase):
    def setUp(self):
        from galaxy_linux import backend
        self.backend_mod = backend
        self.p1 = mock.patch.dict(backend.JOB_KINDS, clear=True)
        self.p2 = mock.patch.object(backend, "_job_finished_hooks", [])
        self.p1.start()
        self.p2.start()
        self.fake = mock.Mock(updates={})
        with mock.patch.object(threading.Thread, "start"):            # kein Arbeits-Thread für den Test
            self.q = backend.JobQueue(self.fake)

    def tearDown(self):
        self.p2.stop()
        self.p1.stop()

    def test_custom_kind_runs_and_reports(self):
        b = self.backend_mod
        seen = []

        def run(backend, job, on_progress, status):
            status("Synchronisiere …")
            return {"files": 3, "pid": job.product_id, "x": job.data.get("x")}
        b.register_job_kind("cloud-sync", "Cloud-Abgleich", run, network=True)
        b.on_job_finished(lambda backend, job: seen.append((job.kind, job.state)))
        job = b.Job("cloud-sync", "1", "Demo", data={"x": 1})
        self.assertEqual(job.label, "Cloud-Abgleich")
        self.q._execute(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.result, {"files": 3, "pid": "1", "x": 1})
        self.assertEqual(seen, [("cloud-sync", "done")])
        self.assertTrue(b.is_network_job("cloud-sync"))
        self.assertTrue(b.is_network_job("install"))
        self.assertFalse(b.is_network_job("prefix"))

    def test_failures(self):
        b = self.backend_mod
        from galaxy_linux.content.installer import InstallError

        def fail(*_a):
            raise InstallError("kein Platz")
        b.register_job_kind("setup", "Einrichtung", fail)
        seen = []
        b.on_job_finished(lambda backend, job: seen.append(job.state))
        b.on_job_finished(lambda backend, job: 1 / 0)                 # fehlerhafter Rückruf stört nicht
        job = b.Job("setup", "1", "Demo")
        with self.assertLogs("galaxy_linux.backend", "WARNING"):
            self.q._execute(job)
        self.assertEqual((job.state, job.error), ("failed", "kein Platz"))
        self.assertEqual(seen, ["failed"])
        job = b.Job("gibt-es-nicht", "1", "Demo")
        with self.assertLogs("galaxy_linux.backend", "WARNING"):
            self.q._execute(job)
        self.assertEqual(job.state, "failed")


# ----------------------------------------------------------------------------- Client-Update, Änderungsprotokoll
class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._data


class ClientInfoTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(clientinfo.parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(clientinfo.parse_version("galaxy-linux 0.10-1"), (0, 10))
        self.assertEqual(clientinfo.parse_version("ohne"), ())
        self.assertTrue(clientinfo.is_newer("v0.2.1", "0.2.0"))
        self.assertTrue(clientinfo.is_newer("0.10", "0.9.9"))
        self.assertFalse(clientinfo.is_newer("0.2", "0.2.0"))
        self.assertFalse(clientinfo.is_newer("", "0.2.0"))

    def test_check_for_update(self):
        s = mock.Mock()
        s.get.return_value = FakeResponse(404)
        self.assertEqual(clientinfo.check_for_update("0.2.0", session=s).state, "none")
        url, kw = s.get.call_args.args[0], s.get.call_args.kwargs
        self.assertEqual(url, "https://api.github.com/repos/Cue-System90/gog_like_client_Arch_linux/releases/latest")
        self.assertNotIn("Authorization", kw["headers"])
        s.get.return_value = FakeResponse(200, {"tag_name": "v0.3.0", "html_url": "https://github.com/x/releases/tag/v0.3.0"})
        r = clientinfo.check_for_update("0.2.0", session=s)
        self.assertEqual((r.state, r.latest, r.url), ("newer", "0.3.0", "https://github.com/x/releases/tag/v0.3.0"))
        s.get.return_value = FakeResponse(200, {"tag_name": "v0.2.0"})
        self.assertEqual(clientinfo.check_for_update("0.2.0", session=s).state, "current")
        s.get.return_value = FakeResponse(500)
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.check_for_update("0.2.0", session=s)
        s.get.side_effect = requests.ConnectionError("offline")
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.check_for_update("0.2.0", session=s)

    def test_changelog_sections(self):
        text = "# Änderungen\n\n## 0.2.0 – Umbau\n\n- **neu**: `x`\n\n## 0.1.6 (19.10.2026)\n\n- Englisch\n"
        cur, older = clientinfo.changelog_section(text, "0.2.0")
        self.assertTrue(cur.startswith("## 0.2.0"))
        self.assertNotIn("0.1.6", cur)
        self.assertTrue(older.startswith("## 0.1.6"))
        self.assertEqual(clientinfo.changelog_section(text, "9.9")[0], "")
        self.assertEqual(clientinfo.markdown_to_plain("## 0.2.0\n- **neu**: `x`"), "0.2.0\n• neu: x")
        md = "## 0.2.0\n\nAbsatz eins\nweiter.\n\n- Punkt a\n  geht weiter\n- Punkt b\n"
        self.assertEqual(clientinfo.markdown_to_plain(md), "0.2.0\n\nAbsatz eins weiter.\n\n• Punkt a geht weiter\n• Punkt b")

    def test_shipped_changelog_has_current_version(self):
        path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        cur, _older = clientinfo.changelog_section(path.read_text(encoding="utf-8"), __version__)
        self.assertTrue(cur, f"CHANGELOG.md braucht einen Abschnitt „## {__version__}“")
        self.assertIn(path, clientinfo.changelog_candidates())
        self.assertTrue(clientinfo.changelog_text())


class StoreLinksTest(unittest.TestCase):
    def test_lang(self):
        self.assertEqual(store_links.store_lang("de"), "de")
        self.assertEqual(store_links.store_lang("de_DE"), "de")
        self.assertEqual(store_links.store_lang("en"), "en")
        self.assertEqual(store_links.store_lang("fr"), "en")
        self.assertEqual(store_links.store_lang(""), "en")

    def test_urls(self):
        u = store_links.store_url
        self.assertEqual(u("/redeem", "de"), "https://www.gog.com/de/redeem")
        self.assertEqual(u("/account/settings/orders", "en"), "https://www.gog.com/en/account/settings/orders")
        self.assertEqual(u("https://www.gog.com/en/game/the_witcher?x=1", "de"), "https://www.gog.com/de/game/the_witcher?x=1")
        self.assertEqual(u("https://www.gog.com/game/x", "en"), "https://www.gog.com/en/game/x")
        self.assertEqual(u("/de", "en"), "https://www.gog.com/en/")
        self.assertEqual(u("", "de"), "https://www.gog.com/de/")
        self.assertEqual(u("games", "de"), "https://www.gog.com/de/games")
        self.assertEqual(u("https://example.org/x", "de"), "https://example.org/x")        # fremde Hosts unverändert
        self.assertEqual(store_links.store_path("//www.gog.com/pl/promo#a"), "/promo#a")


if __name__ == "__main__":
    unittest.main()
