"""Tests für Startoptionen, Kompatibilitätstool-Erkennung, Manifest-Parsing und Launch-Pläne."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.launch import compat, launch_options as lo
from galaxy_linux.launch.launcher import Launcher, PlayTask, split_windows_args, primary_task, play_tasks
from galaxy_linux.content.manifest import BuildManifest, DepotManifest, normalize_path, pick_language, ManifestError
from galaxy_linux.settings import GameSettings, GameSettingsStore, GlobalSettings, InstalledGame, InstalledRegistry, PlaytimeStore


class LaunchOptionsTest(unittest.TestCase):
    def test_goverlay_line(self):
        o = lo.parse("ENABLE_VKBASALT=1 mangohud %command%")
        self.assertEqual(o.env, {"ENABLE_VKBASALT": "1"})
        self.assertEqual(o.wrapper, ["mangohud"])
        self.assertEqual(o.apply(["game"]), ["mangohud", "game"])

    def test_args_only(self):
        o = lo.parse("-novid -w 1920")
        self.assertFalse(o.has_command_token)
        self.assertEqual(o.apply(["game"]), ["game", "-novid", "-w", "1920"])

    def test_wrapper_and_args(self):
        o = lo.parse('gamescope -W 1920 -- %command% --skip-intro "a b"')
        self.assertEqual(o.apply(["g"]), ["gamescope", "-W", "1920", "--", "g", "--skip-intro", "a b"])

    def test_env_with_spaces(self):
        o = lo.parse('WINEDLLOVERRIDES="d3d11=n,b" %command%')
        self.assertEqual(o.env["WINEDLLOVERRIDES"], "d3d11=n,b")

    def test_merge(self):
        self.assertEqual(lo.merge("MANGOHUD=1 %command%", "gamemoderun %command% -x"), "MANGOHUD=1 gamemoderun %command% -x")

    def test_invalid(self):
        with self.assertRaises(lo.LaunchOptionsError):
            lo.parse('"unterminated')


class VdfTest(unittest.TestCase):
    def test_minimal_parser(self):
        txt = '"libraryfolders"\n{\n "0" { "path" "/home/x/.local/share/Steam" "apps" { "1" "2" } }\n "1" { "path" "/mnt/games/SteamLibrary" }\n}'
        d = compat._parse_vdf_minimal(txt)
        self.assertEqual(d["libraryfolders"]["1"]["path"], "/mnt/games/SteamLibrary")
        self.assertEqual(d["libraryfolders"]["0"]["apps"]["1"], "2")


class CompatDiscoveryTest(unittest.TestCase):
    def test_discover_fake_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ge = root / "ct" / "GE-Proton9-20"
            ge.mkdir(parents=True)
            (ge / "proton").write_text("#!/usr/bin/env python3\n")
            (ge / "version").write_text("1730000000 GE-Proton9-20\n")
            (ge / "compatibilitytool.vdf").write_text('"compatibilitytools"{"compat_tools"{"GE-Proton9-20"{"display_name" "GE-Proton9-20"}}}')
            old = root / "ct" / "GE-Proton8-5"
            old.mkdir()
            (old / "proton").write_text("")
            with mock.patch.object(compat, "COMPAT_TOOLS_DIRS", [root / "ct"]), \
                 mock.patch.object(compat, "STEAM_ROOT_CANDIDATES", []), \
                 mock.patch.object(compat, "HEROIC_PROTON_DIRS", []), \
                 mock.patch.object(compat, "LUTRIS_WINE_DIRS", []), \
                 mock.patch.object(compat, "BOTTLES_RUNNER_DIRS", []):
                tools = compat.discover(include_umu_latest=False, include_wine=False)
            self.assertEqual([t.name for t in tools], ["GE-Proton9-20", "GE-Proton8-5"])
            self.assertEqual(tools[0].version, "GE-Proton9-20")
            self.assertEqual(tools[0].requires_runtime, "")
            self.assertIs(compat.resolve(tools[1].id, tools), tools[1])

    def test_system_tools_present(self):
        tools = compat.discover()
        self.assertTrue(tools, "Mindestens Wine/Proton sollten auf CachyOS gefunden werden")


class ManifestTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(normalize_path("Data\\sub\\file.bin"), "Data/sub/file.bin")
        self.assertEqual(normalize_path("./x/./y"), "x/y")
        with self.assertRaises(ManifestError):
            normalize_path("..\\..\\etc\\passwd")

    def test_language(self):
        self.assertEqual(pick_language(["en-US", "de-DE"], "de"), "de-DE")
        self.assertEqual(pick_language(["en-US", "fr-FR"], "de-DE"), "en-US")
        self.assertEqual(pick_language(["pl-PL"], "de-DE"), "pl-PL")

    def test_build_manifest_selection(self):
        m = BuildManifest.parse({
            "version": 2, "buildId": "1", "baseProductId": "100", "installDirectory": "Game", "platform": "windows",
            "depots": [
                {"manifest": "a", "productId": "100", "languages": ["*"]},
                {"manifest": "b", "productId": "100", "languages": ["en-US"]},
                {"manifest": "c", "productId": "100", "languages": ["de-DE"]},
                {"manifest": "d", "productId": "200", "languages": ["*"]},
                {"manifest": "e", "productId": "300", "languages": ["*"]},
            ],
            "dependencies": ["DirectX"], "products": [{"productId": "100", "name": "Game"}, {"productId": "200", "name": "DLC"}],
        })
        depots, lang = m.select_depots("de-DE", owned_ids={"100", "200"})
        self.assertEqual(lang, "de-DE")
        self.assertEqual([d.manifest for d in depots], ["a", "c", "d"])
        depots, lang = m.select_depots("it-IT", owned_ids=set())
        self.assertEqual(lang, "en-US")
        self.assertEqual([d.manifest for d in depots], ["a", "b"])

    def test_depot_manifest(self):
        dm = DepotManifest.parse({"depot": {"items": [
            {"path": "installed", "chunks": [{"md5": "x", "size": 11, "compressedMd5": "y", "compressedSize": 19}], "type": "DepotFile", "sfcRef": {"offset": 3, "size": 11}},
            {"path": "bin\\game.exe", "flags": ["executable"], "chunks": [{"md5": "m", "size": 5, "compressedMd5": "n", "compressedSize": 7}], "type": "DepotFile"},
            {"path": "saves", "type": "DepotDirectory"},
        ], "smallFilesContainer": {"chunks": [{"md5": "s", "size": 100, "compressedMd5": "t", "compressedSize": 50}]}}}, "100", "abc")
        self.assertEqual(dm.files[0].sfc_ref, (3, 11))
        self.assertEqual(dm.files[1].path, "bin/game.exe")
        self.assertTrue(dm.files[1].executable)
        self.assertEqual(dm.directories, ["saves"])
        self.assertEqual(dm.small_files_container[0].size, 100)


class LauncherPlanTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.patch = mock.patch.multiple("galaxy_linux.paths",
                                         SETTINGS_FILE=root / "settings.json", GAME_SETTINGS_FILE=root / "games.json",
                                         INSTALLED_FILE=root / "installed.json", PLAYTIME_FILE=root / "playtime.json",
                                         MANIFEST_DIR=root / "manifests", LOG_DIR=root / "logs")
        self.patch.start()
        self.game_dir = root / "Games" / "Demo"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "game.exe").write_bytes(b"MZ")
        (self.game_dir / "goggame-1.info").write_text(json.dumps({
            "gameId": "1", "playTasks": [
                {"category": "game", "isPrimary": True, "name": "Demo", "path": "game.exe", "arguments": "-fullscreen \"-x y\"", "type": "FileTask", "workingDir": "."},
                {"category": "tool", "name": "Editor", "path": "editor.exe", "type": "FileTask"},
                {"category": "document", "name": "Manual", "type": "URLTask", "link": "https://example"}]}))
        self.settings = GlobalSettings(install_dir=str(root / "Games"), prefix_dir=str(root / "prefixes"), prefer_umu=True)
        self.gs = GameSettingsStore()
        self.registry = InstalledRegistry()
        self.registry.set(InstalledGame("1", "Demo", "windows", str(self.game_dir)))
        self.launcher = Launcher(self.settings, self.gs, self.registry, PlaytimeStore())

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_play_tasks(self):
        tasks = self.launcher.tasks_for(self.registry.get("1"))
        self.assertEqual([t.name for t in tasks], ["Demo", "Editor"])
        self.assertEqual(primary_task(tasks).name, "Demo")
        self.assertEqual(split_windows_args('-fullscreen "-x y" C:\\a\\b'), ["-fullscreen", "-x y", "C:\\a\\b"])

    def test_plan_umu_with_goverlay_options(self):
        proton = compat.CompatTool("proton:/opt/p", "Proton X", "proton", "/opt/p", "x", "System", requires_runtime="1628350")
        self.gs.set("1", GameSettings(compat_tool=proton.id, launch_options="ENABLE_VKBASALT=1 mangohud %command% -skipintro", use_gamescope=True, gamescope_args="-f"))
        with mock.patch.object(compat, "resolve", return_value=proton), \
             mock.patch.object(compat, "umu_available", return_value=True), \
             mock.patch.object(compat, "umu_path", return_value="/usr/bin/umu-run"), \
             mock.patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"):
            plan = self.launcher.build_plan("1")
        self.assertEqual(plan.runner, "umu")
        self.assertEqual(plan.argv[:2], ["mangohud", "gamescope"])
        self.assertEqual(plan.argv[-5:], ["/usr/bin/umu-run", str(self.game_dir / "game.exe"), "-fullscreen", "-x y", "-skipintro"])
        self.assertIn("/usr/bin/umu-run", plan.argv)
        self.assertEqual(plan.argv[-1], "-skipintro")
        self.assertEqual(plan.env["PROTONPATH"], "/opt/p")
        self.assertEqual(plan.env["GAMEID"], "umu-1")
        self.assertEqual(plan.env["STORE"], "gog")
        self.assertEqual(plan.env["ENABLE_VKBASALT"], "1")
        self.assertTrue(plan.env["WINEPREFIX"].endswith("/prefixes/1"))
        self.assertEqual(plan.cwd, str(self.game_dir))

    def test_plan_proton_direct(self):
        proton = compat.CompatTool("proton:/opt/p", "Proton X", "proton", "/opt/p", "x", "Steam")
        self.settings.prefer_umu = False
        self.gs.set("1", GameSettings(compat_tool=proton.id, launch_options="PROTON_LOG=1 %command%"))
        with mock.patch.object(compat, "resolve", return_value=proton), \
             mock.patch.object(compat, "steam_root", return_value=None):
            plan = self.launcher.build_plan("1")
        self.assertEqual(plan.runner, "proton")
        self.assertEqual(plan.argv[:2], ["/opt/p/proton", "waitforexitandrun"])
        self.assertEqual(plan.env["PROTON_LOG"], "1")
        self.assertTrue(plan.env["STEAM_COMPAT_DATA_PATH"].endswith("/prefixes/1"))
        self.assertIn("STEAM_COMPAT_CLIENT_INSTALL_PATH", plan.env)

    def test_plan_wine(self):
        wine = compat.CompatTool("wine:/usr/bin/wine", "Wine", "wine", "/usr/bin/wine", "", "System")
        self.gs.set("1", GameSettings(compat_tool=wine.id))
        with mock.patch.object(compat, "resolve", return_value=wine):
            plan = self.launcher.build_plan("1")
        self.assertEqual(plan.runner, "wine")
        self.assertEqual(plan.argv[0], "/usr/bin/wine")
        self.assertTrue(plan.env["WINEPREFIX"].endswith("/prefixes/1"))

    def _umu_plan(self):
        proton = compat.CompatTool("proton:/opt/p", "Proton X", "proton", "/opt/p", "x", "System", requires_runtime="1628350")
        with mock.patch.object(compat, "resolve", return_value=proton), \
             mock.patch.object(compat, "umu_available", return_value=True), \
             mock.patch.object(compat, "umu_path", return_value="/usr/bin/umu-run"):
            return self.launcher.build_plan("1")

    @mock.patch.dict(os.environ, {"PROTON_ENABLE_WAYLAND": ""})
    def test_wayland_default_and_override(self):
        del os.environ["PROTON_ENABLE_WAYLAND"]
        self.gs.set("1", GameSettings(compat_tool="proton:/opt/p"))
        self.assertEqual(self._umu_plan().env.get("PROTON_ENABLE_WAYLAND"), "1")          # Vorgabe: KDE-Wayland
        self.gs.update("1", wayland_driver="off")
        self.assertEqual(self._umu_plan().env.get("PROTON_ENABLE_WAYLAND"), "0")          # pro Spiel XWayland
        self.settings.wayland_driver = False
        self.gs.update("1", wayland_driver="")
        self.assertNotIn("PROTON_ENABLE_WAYLAND", self._umu_plan().env)
        self.gs.update("1", wayland_driver="on")
        self.assertEqual(self._umu_plan().env.get("PROTON_ENABLE_WAYLAND"), "1")
        self.gs.update("1", wayland_driver="off", env={"PROTON_ENABLE_WAYLAND": "1"})   # eigene Variable gewinnt
        self.assertEqual(self._umu_plan().env.get("PROTON_ENABLE_WAYLAND"), "1")
        self.settings.wayland_driver = True                                              # reines Wine: kein Proton-Schalter
        wine = compat.CompatTool("wine:/usr/bin/wine", "Wine", "wine", "/usr/bin/wine", "", "System")
        self.gs.set("1", GameSettings(compat_tool=wine.id))
        with mock.patch.object(compat, "resolve", return_value=wine):
            self.assertNotIn("PROTON_ENABLE_WAYLAND", self.launcher.build_plan("1").env)

    def test_plan_linux_native(self):
        lin = Path(self.td.name) / "Games" / "Lin"
        lin.mkdir()
        (lin / "start.sh").write_text("#!/bin/sh\n")
        self.registry.set(InstalledGame("2", "Lin", "linux", str(lin), executable="start.sh"))
        plan = self.launcher.build_plan("2")
        self.assertEqual(plan.runner, "native")
        self.assertEqual(plan.argv, [str(lin / "start.sh")])


if __name__ == "__main__":
    unittest.main()
