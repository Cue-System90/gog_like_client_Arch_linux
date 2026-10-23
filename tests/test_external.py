"""Externe Spiele (nicht von GOG) sowie Hinzufügen/Entfernen von Spielen und Prefixen."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.content import external
from galaxy_linux.settings import CustomLibrary


def exe(path: Path, size: int = 2_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * size)


def age(d: Path) -> None:
    past = time.time() - 600
    for p in [d, *d.rglob("*")]:
        os.utime(p, (past, past))


class ExeScoringTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_picks_main_exe_over_helpers(self):
        g = self.root / "Freelancer HD Edition"
        exe(g / "unins000.exe", 4_000_000)
        exe(g / "EXE" / "Freelancer.exe")
        exe(g / "EXE" / "FLServer.exe", 200_000)
        exe(g / "_Redist" / "vcredist_x86.exe", 9_000_000)
        self.assertEqual(external.game_in_dir(g).executable, "EXE/Freelancer.exe")

    def test_unreal_engine_and_acronyms(self):
        g = self.root / "The Sinking City 2"
        exe(g / "PlayGround" / "Binaries" / "Win64" / "TheSinkingCity2.exe", 9_000_000)
        exe(g / "Engine" / "Binaries" / "Win64" / "CrashReportClient.exe", 9_000_000)
        self.assertEqual(external.game_in_dir(g).executable, "PlayGround/Binaries/Win64/TheSinkingCity2.exe")
        n = self.root / "The Operative - No One Lives Forever GOTY Edition"
        exe(n / "lithtech.exe", 800_000)
        exe(n / "NOLF.exe", 600_000)
        exe(n / "NOLFTools001.exe", 5_000_000)
        self.assertEqual(external.game_in_dir(n).executable, "NOLF.exe")
        s = self.root / "Soldier of Fortune"
        exe(s / "SoF.exe", 1_400_000)
        exe(s / "unins000.exe", 1_100_000)
        self.assertEqual(external.game_in_dir(s).executable, "SoF.exe")

    def test_64bit_preferred_and_no_exe(self):
        u = self.root / "Unreal"
        exe(u / "SYSTEM" / "Unreal.exe", 260_000)
        exe(u / "System64" / "Unreal.exe", 300_000)
        exe(u / "System64" / "UnrealEd.exe", 1_200_000)
        self.assertEqual(external.game_in_dir(u).executable, "System64/Unreal.exe")
        (self.root / "Leer").mkdir()
        self.assertIsNone(external.game_in_dir(self.root / "Leer"))

    def test_titles_and_ids(self):
        self.assertEqual(external.display_title("EmperorDAdRdM 1360 x 768"), "EmperorDAdRdM")
        self.assertEqual(external.search_title("Anno 2070 Complete Edition"), "Anno 2070")
        self.assertEqual(external.search_title("Freelancer HD Edition - Kopie"), "Freelancer")
        self.assertEqual(external.external_id("Herrscher des Olymp - Zeus"), "ext-herrscher-des-olymp-zeus")


class ExternalBackendTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        self.gog = base / "GOG"
        self.ext = base / "Weitere" / "02.Games"
        self.extpfx = base / "Weitere" / "01.Prefixes"
        for d in (self.gog, self.ext, self.extpfx):
            d.mkdir(parents=True)
        cfg, data, cache = base / "cfg", base / "data", base / "cache"
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        from galaxy_linux.backend import Backend
        self.b = Backend()
        s = self.b.settings
        s.install_dir, s.prefix_dir = str(self.gog), str(self.gog / "prefixes")
        s.libraries = [CustomLibrary(id="external", name="External", games_dir=str(self.ext), prefix_dir=str(self.extpfx))]
        self.jobs = []
        self.add_patch = mock.patch.object(self.b.jobs, "add", side_effect=lambda j: self.jobs.append(j) or j)
        self.add_patch.start()
        exe(self.ext / "Anno 2070 Complete Edition" / "Anno5.exe")
        exe(self.ext / "Freelancer" / "EXE" / "Freelancer.exe")
        age(self.ext)

    def tearDown(self):
        self.add_patch.stop()
        self.patch.stop()
        self.td.cleanup()

    def test_scan_registers_external_with_own_prefix_dir(self):
        rep = self.b.scan_install_dirs(wait=0.05)
        ids = sorted(g.product_id for g in rep.imported)
        self.assertEqual(ids, ["ext-anno-2070-complete-edition", "ext-freelancer"])
        g = self.b.registry.get("ext-freelancer")
        self.assertEqual((g.method, g.executable, g.platform, g.library), ("external", "EXE/Freelancer.exe", "windows", "external"))
        self.assertEqual(self.b.launcher.prefix_for("ext-freelancer"), self.extpfx / "Freelancer")
        self.assertEqual(sorted(j.product_id for j in self.jobs), ids)          # Prefixe werden angelegt
        plan = self.b.launcher.build_plan("ext-freelancer")
        self.assertTrue(plan.cwd.endswith("Freelancer/EXE"))
        self.assertIn(str(self.ext / "Freelancer" / "EXE" / "Freelancer.exe"), plan.argv)

    def test_remove_keeps_files_and_is_not_reimported(self):
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.b.remove_game("ext-freelancer")
        self.assertTrue((self.ext / "Freelancer").is_dir())
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual(rep.imported, [])
        self.assertIsNone(self.b.registry.get("ext-freelancer"))
        game = self.b.add_game_manually(self.ext / "Freelancer", title="Freelancer (Original)", create_prefix=False)
        self.assertEqual(game.title, "Freelancer")                                     # erkannter Titel
        self.assertEqual(self.b.display_title(game.product_id), "Freelancer (Original)")   # eigener Titel
        self.assertEqual(self.b.settings.ignored_paths, [])

    def test_manual_add_outside_folders_and_delete_files(self):
        other = Path(self.td.name) / "Sonstwo" / "Mein Spiel"
        exe(other / "bin" / "game.exe")
        info = self.b.inspect_folder(other)
        self.assertEqual(info["kind"], "external")
        self.assertIn("bin/game.exe", info["candidates"])
        g = self.b.add_game_manually(other, exe="bin/game.exe", create_prefix=True)
        self.assertEqual(self.jobs[-1].product_id, g.product_id)
        with self.assertRaises(Exception):
            self.b.inspect_folder(other)       # schon in der Bibliothek
        self.b.remove_game(g.product_id, delete_files=True)
        self.assertFalse(other.exists())

    def test_prefix_delete_reset_and_orphans(self):
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        p = self.b.launcher.prefix_for("ext-freelancer")
        (p / "pfx").mkdir(parents=True)
        (p / "pfx" / "system.reg").write_text("")
        orphan = self.extpfx / "Altes Spiel"
        (orphan / "pfx").mkdir(parents=True)
        self.assertEqual(self.b.orphan_prefixes(), [orphan])
        self.assertEqual(self.b.delete_orphan_prefixes([orphan, p]), [orphan])   # p gehört einem Spiel → bleibt
        self.assertTrue(p.exists())
        self.b.game_settings.update("ext-freelancer", cloud_timestamps={"saves": 1.0}, setup_done=["x:1"])
        self.b.reset_prefix("ext-freelancer")
        self.assertFalse(p.exists())
        gs = self.b.game_settings.get("ext-freelancer")
        self.assertEqual((gs.cloud_timestamps, gs.setup_done), ({}, []))      # alter Abgleich gilt nicht mehr
        self.assertEqual(self.jobs[-1].kind, "prefix")
        # Sicherheitsnetz: Wurzelordner werden nie gelöscht
        self.b.game_settings.update("ext-freelancer", prefix=str(self.extpfx))
        with self.assertRaises(Exception):
            (self.extpfx / "x").mkdir()
            self.b.delete_prefix("ext-freelancer")
        self.assertTrue(self.extpfx.is_dir())

    def test_rename_exe_and_no_gog_playtime(self):
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.b.rename_external("ext-freelancer", "Freelancer Classic")
        self.assertEqual(self.b.display_title("ext-freelancer"), "Freelancer Classic")      # = eigener Titel
        self.assertEqual(self.b.game_settings.get("ext-freelancer").custom_title, "Freelancer Classic")
        exe(self.ext / "Freelancer" / "EXE" / "FLServerTool.exe", 100_000)
        self.b.set_external_exe("ext-freelancer", "EXE/FLServerTool.exe")
        tasks = self.b.launcher.tasks_for(self.b.registry.get("ext-freelancer"))
        self.assertEqual([t.name for t in tasks if t.is_primary], ["EXE/FLServerTool.exe"])
        self.assertTrue(self.b.is_external("ext-freelancer"))


if __name__ == "__main__":
    unittest.main()
