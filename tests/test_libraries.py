"""Eigene Bibliotheken: Anlegen/Umbenennen/Löschen, Migration der External-Bibliothek aus 0.1.4,
Prefix-Zuordnung, Bibliotheks-Vorgaben, Ordnersuche je Bibliothek und Kommandozeile."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.content.installer import InstallError
from galaxy_linux.launch import compat
from galaxy_linux.settings import CustomLibrary, GlobalSettings


def exe(path: Path, size: int = 2_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * size)


def age(d: Path) -> None:
    past = time.time() - 600
    for p in [d, *d.rglob("*")]:
        os.utime(p, (past, past))


class LibraryTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.gog = self.base / "GOG" / "02.Games"
        self.gog.mkdir(parents=True)
        cfg, data, cache = self.base / "cfg", self.base / "data", self.base / "cache"
        self.cfg, self.data = cfg, data
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        self.jobs: list = []
        self._job_patches: list = []

    def tearDown(self):
        for p in self._job_patches:
            p.stop()
        self.patch.stop()
        self.td.cleanup()

    def backend(self, gog_dirs: bool = True):
        from galaxy_linux.backend import Backend
        b = Backend()
        if gog_dirs:
            b.settings.install_dir, b.settings.prefix_dir = str(self.gog), str(self.gog.parent / "01.Prefixes")
        p = mock.patch.object(b.jobs, "add", side_effect=lambda j: self.jobs.append(j) or j)
        p.start()
        self._job_patches.append(p)
        return b


# ----------------------------------------------------------------------------- Migration aus 0.1.4
class MigrationTest(LibraryTestBase):
    def setUp(self):
        super().setUp()
        self.ext = self.base / "Weitere_Spiele" / "02.Games"
        self.extpfx = self.base / "Weitere_Spiele" / "01.Prefixes"
        exe(self.ext / "Freelancer" / "EXE" / "Freelancer.exe")
        exe(self.ext / "Anno 2070 Complete Edition" / "Anno5.exe")
        exe(self.base / "Sonstwo" / "Handspiel" / "game.exe")      # 0.1.3: von Hand hinzugefügt, außerhalb des Ordners
        age(self.base)
        for name in ("Freelancer", "Anno 2070 Complete Edition", "Handspiel"):
            (self.extpfx / name / "pfx").mkdir(parents=True)
            (self.extpfx / name / "pfx" / "system.reg").write_text("")
        self.cfg.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.old_settings = {"install_dir": str(self.gog), "prefix_dir": str(self.gog.parent / "01.Prefixes"),
                             "external_games_dir": str(self.ext), "external_prefix_dir": str(self.extpfx),
                             "wayland_driver": True, "language": "de-DE", "use_keyring": True, "ignored_paths": []}
        (self.cfg / "settings.json").write_text(json.dumps(self.old_settings))
        old_games = {
            "ext-freelancer": {"product_id": "ext-freelancer", "title": "Freelancer", "platform": "windows",
                               "install_path": str(self.ext / "Freelancer"), "method": "external",
                               "executable": "EXE/Freelancer.exe", "install_size": 2000002, "installed_at": 1.0},
            "ext-anno-2070-complete-edition": {"product_id": "ext-anno-2070-complete-edition", "title": "Anno 2070 Complete Edition",
                                               "platform": "windows", "install_path": str(self.ext / "Anno 2070 Complete Edition"),
                                               "method": "external", "executable": "Anno5.exe", "installed_at": 2.0},
            "ext-handspiel": {"product_id": "ext-handspiel", "title": "Handspiel", "platform": "windows",
                              "install_path": str(self.base / "Sonstwo" / "Handspiel"), "method": "external",
                              "executable": "game.exe", "installed_at": 3.0},
        }
        (self.data / "installed.json").write_text(json.dumps(old_games))
        (self.cfg / "games.json").write_text(json.dumps({"ext-freelancer": {"launch_options": "-novid", "favorite": True}}))
        (self.data / "playtime.json").write_text(json.dumps({"ext-freelancer": {"minutes": 42, "last_played": 5.0}}))

    def test_external_becomes_first_library_without_data_loss(self):
        b = self.backend(gog_dirs=False)
        self.assertEqual([(l.id, l.name, l.games_dir, l.prefix_dir) for l in b.libraries()],
                         [("external", "External", str(self.ext), str(self.extpfx))])
        # Spiele, IDs, Prefixe und Pro-Spiel-Daten unverändert
        for pid in ("ext-freelancer", "ext-anno-2070-complete-edition", "ext-handspiel"):
            g = b.registry.get(pid)
            self.assertEqual(g.library, "external")
            self.assertTrue(b.launcher.prefix_exists(pid), pid)
        self.assertEqual(b.launcher.prefix_for("ext-freelancer"), self.extpfx / "Freelancer")
        self.assertEqual(b.launcher.prefix_for("ext-handspiel"), self.extpfx / "Handspiel")
        self.assertEqual(b.game_settings.get("ext-freelancer").launch_options, "-novid")
        self.assertEqual(b.playtime.get("ext-freelancer")[0], 42)
        # Sicherungen der alten Dateien, neue Struktur gespeichert, alte Schlüssel weg
        self.assertEqual(json.loads((self.cfg / "settings.json.vor-bibliotheken").read_text()), self.old_settings)
        self.assertTrue((self.data / "installed.json.vor-bibliotheken").exists())
        saved = json.loads((self.cfg / "settings.json").read_text())
        self.assertNotIn("external_games_dir", saved)
        self.assertEqual(saved["libraries"][0]["games_dir"], str(self.ext))
        self.assertEqual(saved["language"], "de-DE")
        self.assertTrue(saved["use_keyring"])
        # Ordnersuche danach: nichts neu, nichts ausgetragen, keine Prefix-Jobs
        rep = b.scan_install_dirs(wait=0.05)
        self.assertEqual((rep.imported, rep.removed, rep.prefix_jobs), ([], [], 0))
        self.assertEqual(len(b.library_games("external")), 3)

    def test_migration_runs_once(self):
        self.backend(gog_dirs=False)
        b2 = self.backend(gog_dirs=False)
        self.assertEqual(len(b2.libraries()), 1)
        (self.cfg / "settings.json.vor-bibliotheken").write_text("unverändert")
        b3 = self.backend(gog_dirs=False)
        b3.settings.save()
        self.assertEqual((self.cfg / "settings.json.vor-bibliotheken").read_text(), "unverändert")

    def test_fresh_install_gets_default_external_library_without_writing(self):
        for f in (self.cfg / "settings.json", self.data / "installed.json"):
            f.unlink()
        s = GlobalSettings.load()
        self.assertEqual([l.id for l in s.libraries], ["external"])
        self.assertFalse((self.cfg / "settings.json").exists())

    def test_broken_library_entries_are_skipped(self):
        (self.cfg / "settings.json").write_text(json.dumps({"libraries": [{"id": "a"}, "x", {"id": "b", "name": "B", "games_dir": "/m/b/g",
                                                                                              "prefix_dir": "/m/b/p", "unknown": 1}]}))
        s = GlobalSettings.load()
        self.assertEqual([l.id for l in s.libraries], ["b"])


# ----------------------------------------------------------------------------- Anlegen, Umbenennen, Löschen
class ManageTest(LibraryTestBase):
    def test_create_defaults_persist_and_validation(self):
        b = self.backend()
        b.settings.libraries = []
        games = self.base / "Weitere" / "Games"
        lib = b.create_library("Andere Stores", str(games))
        self.assertEqual((lib.id, lib.name, lib.games_dir, lib.prefix_dir), ("andere-stores", "Andere Stores", str(games), str(games / "prefixes")))
        self.assertTrue(games.is_dir())                                          # Ordner wird angelegt
        self.assertEqual([l.id for l in GlobalSettings.load().libraries], ["andere-stores"])   # gespeichert
        with self.assertRaises(InstallError):
            b.create_library("andere stores", str(self.base / "anders"))              # Name doppelt (ohne Groß/klein)
        with self.assertRaises(InstallError):
            b.create_library("Unterordner", str(games / "Sub"))                 # verschachtelt
        with self.assertRaises(InstallError):
            b.create_library("Oben", str(games.parent))                         # verschachtelt (umgekehrt)
        with self.assertRaises(InstallError):
            b.create_library("Heim", str(Path.home()))
        with self.assertRaises(InstallError):
            b.create_library("Gleich", str(self.base / "x"), str(self.base / "x"))
        with self.assertRaises(InstallError):
            b.create_library("", str(self.base / "y"))
        with self.assertRaises(InstallError):
            b.create_library("Wayland", str(self.base / "z"), wayland_driver="vielleicht")
        lib2 = b.create_library("Emulatoren", str(self.base / "Emu"), str(self.base / "Emu-Prefixe"), wayland_driver="off",
                                prefix_dpi=144, launch_options="gamemoderun %command%")
        self.assertEqual((lib2.wayland_driver, lib2.prefix_dpi, lib2.launch_options), ("off", 144, "gamemoderun %command%"))
        self.assertEqual(b._unique_library_id("Andere Stores"), "andere-stores-2")

    def test_rename_update_and_remove_keep_files(self):
        b = self.backend()
        games = self.base / "Andere Stores"
        exe(games / "Unreal" / "System64" / "Unreal.exe")
        age(games)
        lib = b.create_library("Andere Stores", str(games))
        b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual([g.product_id for g in b.library_games(lib.id)], ["ext-unreal"])
        b.rename_library(lib.id, "Alte Spiele")
        self.assertEqual((b.get_library("andere-stores").name, b.registry.get("ext-unreal").library), ("Alte Spiele", "andere-stores"))
        b.create_library("Neu", str(self.base / "Neu"))
        with self.assertRaises(InstallError):
            b.rename_library(lib.id, "neu")
        b.update_library(lib.id, prefix_dir=str(self.base / "Prefixe"), compat_tool="auto", watch="aus")
        self.assertEqual(b.launcher.prefix_for("ext-unreal"), self.base / "Prefixe" / "Unreal")
        self.assertEqual((lib.compat_tool, lib.watch), ("", False))
        with self.assertRaises(InstallError):
            b.update_library(lib.id, games_dir=str(self.base / "Neu"))          # gehört schon „Neu“
        pfx = b.launcher.prefix_for("ext-unreal")
        (pfx / "pfx").mkdir(parents=True)
        removed = b.remove_library(lib.id)
        self.assertEqual([g.product_id for g in removed], ["ext-unreal"])
        self.assertIsNone(b.get_library(lib.id))
        self.assertIsNone(b.registry.get("ext-unreal"))
        self.assertTrue((games / "Unreal" / "System64" / "Unreal.exe").exists())   # Dateien bleiben
        self.assertTrue(pfx.is_dir())                                             # Prefix bleibt
        self.assertEqual([l.name for l in GlobalSettings.load().libraries], ["External", "Neu"])

    def test_find_library(self):
        b = self.backend()
        b.settings.libraries = []
        b.create_library("Andere-Spiele", str(self.base / "R"))
        b.create_library("Emulatoren", str(self.base / "E"))
        self.assertEqual(b.find_library("andere-spiele").id, "andere-spiele")
        self.assertEqual(b.find_library("EMU").name, "Emulatoren")
        with self.assertRaises(InstallError):
            b.find_library("gibtsnicht")


# ----------------------------------------------------------------------------- Ordnersuche je Bibliothek
class ScanTest(LibraryTestBase):
    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.b.settings.libraries = []
        self.a_dir, self.b_dir = self.base / "A" / "Games", self.base / "B" / "Games"
        self.lib_a = self.b.create_library("Andere Stores", str(self.a_dir), str(self.base / "A" / "Prefixe"))
        self.lib_b = self.b.create_library("Klassiker", str(self.b_dir), str(self.base / "B" / "Prefixe"))
        exe(self.a_dir / "Freelancer" / "EXE" / "Freelancer.exe")
        exe(self.a_dir / "Anno 2070" / "Anno5.exe")
        exe(self.b_dir / "Freelancer" / "EXE" / "Freelancer.exe")       # gleicher Ordnername in zweiter Bibliothek
        exe(self.b_dir / "Unreal" / "System64" / "Unreal.exe")
        (self.b_dir / "Leer").mkdir()
        gogdir = self.gog / "Tropico"
        gogdir.mkdir()
        (gogdir / "goggame-1207663953.info").write_text(json.dumps(
            {"gameId": "1207663953", "rootGameId": "1207663953", "name": "Tropico", "playTasks": [{"isPrimary": True, "path": "Tropico.exe"}]}))
        exe(gogdir / "Tropico.exe")
        age(self.base)

    def test_games_land_in_their_library_with_own_prefix_root(self):
        rep = self.b.scan_install_dirs(wait=0.05)
        by_lib = {g.product_id: g.library for g in rep.imported}
        self.assertEqual(by_lib, {"ext-freelancer": "andere-stores", "ext-anno-2070": "andere-stores", "ext-freelancer-2": "klassiker",
                                  "ext-unreal": "klassiker", "1207663953": ""})
        self.assertEqual(self.b.launcher.prefix_for("ext-freelancer"), self.base / "A" / "Prefixe" / "Freelancer")
        self.assertEqual(self.b.launcher.prefix_for("ext-freelancer-2"), self.base / "B" / "Prefixe" / "Freelancer")
        self.assertEqual(self.b.launcher.prefix_for("1207663953"), self.gog.parent / "01.Prefixes" / "1207663953")
        self.assertIn(str(self.b_dir / "Leer"), rep.unknown)
        self.assertEqual(sorted(j.product_id for j in self.jobs), sorted(by_lib))    # Prefix-Jobs für alle Windows-Spiele
        self.assertEqual(sorted(g.product_id for g in self.b.library_games("klassiker")), ["ext-freelancer-2", "ext-unreal"])
        self.jobs.clear()
        self.assertEqual(self.b.queue_missing_prefixes(library="klassiker"), 2)
        self.assertEqual(sorted(j.product_id for j in self.jobs), ["ext-freelancer-2", "ext-unreal"])

    def test_copying_game_waits_until_stable(self):
        exe(self.a_dir / "Neues Spiel" / "neu.exe")                         # gerade kopiert: frische mtime
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertIn("Neues Spiel", rep.copying)
        self.assertIsNone(self.b.registry.get("ext-neues-spiel"))
        age(self.a_dir / "Neues Spiel")
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual([g.product_id for g in rep.imported], ["ext-neues-spiel"])
        self.assertEqual(self.b.registry.get("ext-neues-spiel").library, "andere-stores")

    def test_unwatched_library_skipped_by_folder_watch_only(self):
        self.b.update_library("klassiker", watch=False)
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False, auto=True)
        self.assertNotIn("ext-unreal", [g.product_id for g in rep.imported])
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertIn("ext-unreal", [g.product_id for g in rep.imported])

    def test_removed_folder_and_ignore_list_per_library(self):
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.b.remove_game("ext-unreal")                                        # Ordner bleibt, wird ignoriert
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual(rep.imported, [])
        import shutil
        shutil.rmtree(self.a_dir / "Anno 2070")
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual(rep.removed, ["Anno 2070"])
        self.assertEqual([g.product_id for g in self.b.library_games("andere-stores")], ["ext-freelancer"])

    def test_orphans_per_library(self):
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        for root, name in ((self.base / "A" / "Prefixe", "Altes Spiel"), (self.base / "B" / "Prefixe", "Weg")):
            (root / name / "pfx").mkdir(parents=True)
        self.assertEqual([d.name for d in self.b.orphan_prefixes("andere-stores")], ["Altes Spiel"])
        self.assertEqual(sorted(d.name for d in self.b.orphan_prefixes()), ["Altes Spiel", "Weg"])

    def test_manual_add_into_chosen_library(self):
        other = self.base / "USB" / "Mein Spiel"
        exe(other / "bin" / "game.exe")
        info = self.b.inspect_folder(other)
        self.assertEqual(info["library"], "")
        g = self.b.add_game_manually(other, exe="bin/game.exe", create_prefix=False, library="Klassiker")
        self.assertEqual(g.library, "klassiker")
        self.assertEqual(self.b.launcher.prefix_for(g.product_id), self.base / "B" / "Prefixe" / "Mein Spiel")
        inner = self.a_dir / "Von Hand"
        exe(inner / "x.exe")
        self.assertEqual(self.b.inspect_folder(inner)["library"], "andere-stores")    # Vorschlag nach Ordner
        self.b.settings.libraries = []
        with self.assertRaises(InstallError):
            self.b.add_game_manually(self.base / "B" / "Games" / "Unreal", create_prefix=False)


# ----------------------------------------------------------------------------- Vorgaben je Bibliothek
class DefaultsTest(LibraryTestBase):
    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.b.settings.libraries = []
        self.b.settings.prefer_umu = False
        self.b.settings.default_compat_tool = "auto"
        self.tools = [compat.CompatTool("proton:/t/A", "Proton A", "proton", str(self.base / "A"), source="System"),
                      compat.CompatTool("proton:/t/B", "Proton B", "proton", str(self.base / "B"), source="Steam")]
        mock.patch.object(self.b.launcher, "tools", return_value=self.tools).start()
        mock.patch.object(compat, "umu_available", return_value=False).start()
        self.addCleanup(mock.patch.stopall)
        games = self.base / "Lib"
        exe(games / "Spiel" / "spiel.exe")
        age(games)
        self.lib = self.b.create_library("Lib", str(games))
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.pid = "ext-spiel"

    def plan(self):
        return self.b.launcher.build_plan(self.pid)

    def test_compat_tool(self):
        self.assertEqual(self.plan().tool.name, "Proton A")                     # globale Vorgabe
        self.b.update_library("lib", compat_tool="proton:/t/B")
        self.assertEqual(self.plan().tool.name, "Proton B")                     # Bibliothek
        self.assertEqual(self.b.launcher.default_tool(self.pid)[1], "Vorgabe der Bibliothek „Lib“")
        self.b.game_settings.update(self.pid, compat_tool="proton:/t/A")
        self.assertEqual(self.plan().tool.name, "Proton A")                     # Spiel gewinnt

    def test_launch_options_and_wayland(self):
        self.b.update_library("lib", launch_options="LIBVAR=1 %command% -lib", wayland_driver="off")
        p = self.plan()
        self.assertEqual((p.env.get("LIBVAR"), p.argv[-1], p.env.get("PROTON_ENABLE_WAYLAND")), ("1", "-lib", "0"))
        self.b.game_settings.update(self.pid, launch_options="-spiel", wayland_driver="on")
        p = self.plan()
        self.assertEqual((p.env.get("LIBVAR"), p.argv[-1], p.env.get("PROTON_ENABLE_WAYLAND")), (None, "-spiel", "1"))
        self.b.update_library("lib", launch_options="", wayland_driver="")
        self.b.game_settings.update(self.pid, launch_options="", wayland_driver="")
        self.assertEqual(self.plan().env.get("PROTON_ENABLE_WAYLAND"), "1")    # global: Wayland an

    def test_dpi(self):
        seen = []
        with mock.patch.object(self.b.launcher, "run_in_prefix", side_effect=lambda pid, argv, **kw: seen.append(argv[-2]) or 0):
            self.b.update_library("lib", prefix_dpi=144)
            self.b.launcher.apply_dpi(self.pid)
            self.b.update_library("lib", prefix_dpi=0)
            self.b.settings.prefix_dpi = 120
            self.b.launcher.apply_dpi(self.pid)
        self.assertEqual(seen, ["144", "120"])

    def test_view_settings(self):
        st = self.b.settings
        self.assertTrue(st.view_settings()["sections"])
        v = st.view_settings(self.lib)
        self.assertFalse(v["sections"])                                         # eigene Bibliotheken: ein Abschnitt
        self.assertEqual(v["layout"], st.library_layout)
        self.b.update_library("lib", view={"layout": "list", "sort": 2, "unsinn": 1})
        self.assertEqual(self.lib.view, {"layout": "list", "sort": 2})
        self.assertEqual(st.view_settings(self.lib)["layout"], "list")
        self.assertEqual(st.view_settings()["layout"], "grid")


# ----------------------------------------------------------------------------- Kommandozeile
class CliTest(LibraryTestBase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        from galaxy_linux import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_library_commands(self):
        GlobalSettings().save()                                 # leere Konfiguration ohne Bibliotheken
        (self.cfg / "settings.json").write_text(json.dumps({"libraries": [], "install_dir": str(self.gog),
                                                            "prefix_dir": str(self.gog.parent / "01.Prefixes")}))
        games = self.base / "Andere Stores"
        exe(games / "Unreal" / "System64" / "Unreal.exe")
        age(games)
        rc, out = self.run_cli("library", "add", "Andere Stores", str(games), "--wayland", "off", "--dpi", "144", "--no-prefix")
        self.assertEqual(rc, 0, out)
        self.assertIn("übernommen: Unreal (ext-unreal)", out)
        rc, out = self.run_cli("library", "list")
        self.assertIn("Andere Stores  (ID andere-stores) · 1 Spiele", out)
        self.assertIn("Wayland-Treiber: aus (XWayland) · DPI: 144", out)
        rc, out = self.run_cli("library", "rename", "andere-stores", "Alte Spiele")
        self.assertIn("Andere Stores → Alte Spiele", out)
        rc, out = self.run_cli("library", "set", "Alte Spiele", "options", "-windowed")
        self.assertIn("Startoptionen: -windowed", out)
        other = self.base / "USB" / "Mein Spiel"
        exe(other / "game.exe")
        rc, out = self.run_cli("add", str(other), "--library", "alte", "--no-prefix")
        self.assertIn("Bibliothek Alte Spiele", out)
        rc, out = self.run_cli("list", "--library", "Alte Spiele")
        self.assertIn("Alte Spiele: 2 Spiele", out)
        self.assertIn("ext-mein-spiel", out)
        rc, out = self.run_cli("library", "remove", "Alte Spiele", "-y")
        self.assertIn("2 Spiele ausgetragen", out)
        self.assertTrue((games / "Unreal").is_dir())
        rc, out = self.run_cli("library", "list")
        self.assertIn("Keine eigenen Bibliotheken", out)
        rc, _ = self.run_cli("library", "rename", "gibtsnicht", "x")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
