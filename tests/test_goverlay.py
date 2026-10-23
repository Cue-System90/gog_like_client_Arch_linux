"""Tests für GOverlay pro Spiel (launch/goverlay_profiles.py, launch/hook_goverlay.py): SanitizeFileName-Nachbau,
nonsteam_folders.txt, Cover, bgmod.conf, Profilauflösung, Startbefehl mit bgmod, Vorlage kopieren, bgmod-uninstaller.
Alle Pfade liegen in Temp-Ordnern (HOME, XDG_*), kein Netz."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.launch import compat, goverlay, goverlay_profiles as gp, hooks
from galaxy_linux.launch.hook_goverlay import GOverlayHook
from galaxy_linux.launch.launcher import Launcher, LaunchPlan
from galaxy_linux.settings import (CustomLibrary, GameSettings, GameSettingsStore, GlobalSettings, InstalledGame,
                                   InstalledRegistry, PlaytimeStore)

GAME = "Sid Meier's Alpha Centauri Planetary Pack"
PROFILE = "Sid Meier_s Alpha Centauri Planetary Pack"


def make_exe(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class SanitizeTest(unittest.TestCase):
    def test_vectors(self):
        for raw, want in (("LEGO® 2K Drive", "LEGO_ 2K Drive"),
                          (GAME, PROFILE),
                          ("Theos: Cities Of Myth Demo", "Theos_ Cities Of Myth Demo"),
                          ("GRID (2019)", "GRID (2019)"), ("Tropico 7 - Demo", "Tropico 7 - Demo"),
                          ("DEATHLOOP", "DEATHLOOP"), ("__redist", "_redist"), ("a//b", "a_b"), ("x\ty", "x_y"),
                          ("Ä", "_"), ("®", "_"), ("[x]+.y", "[x]+.y"), ("  Spiel  ", "Spiel"),
                          ("", "game"), ("   ", "game"), (None, "game")):
            self.assertEqual(gp.goverlay_sanitize(raw), want, raw)


class TempEnv(unittest.TestCase):
    """HOME, XDG_CONFIG_HOME und XDG_DATA_HOME in einem Temp-Ordner; galaxy-linux-Pfade ebenso."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = root = Path(self.td.name)
        self.home = root / "home"
        self.cfg, self.data = self.home / ".config", self.home / ".local" / "share"
        self.cfg.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.cfg),
                                                "XDG_DATA_HOME": str(self.data), "HOST_XDG_DATA_HOME": ""})
        self.env.start()
        gl = root / "gl"
        self.paths = mock.patch.multiple(
            "galaxy_linux.paths", SETTINGS_FILE=gl / "settings.json", GAME_SETTINGS_FILE=gl / "games.json",
            INSTALLED_FILE=gl / "installed.json", PLAYTIME_FILE=gl / "playtime.json", MANIFEST_DIR=gl / "manifests",
            LOG_DIR=gl / "logs", IMAGE_CACHE_DIR=gl / "images", API_CACHE_DIR=gl / "api")
        self.paths.start()
        for d in ("logs", "images", "api", "manifests"):
            (gl / d).mkdir(parents=True)
        self.sys_dirs = mock.patch.object(gp, "SYSTEM_BGMOD_DIRS", [])
        self.sys_dirs.start()
        self.games = root / "GOG" / "02.Games"
        self.game_dir = self.games / GAME
        make_exe(self.game_dir / "game.exe", "MZ")
        (self.game_dir / f"goggame-1.info").write_text(json.dumps({"gameId": "1", "playTasks": [
            {"category": "game", "isPrimary": True, "name": "Spiel", "path": "game.exe", "arguments": "-x", "type": "FileTask"}]}))
        self.settings = GlobalSettings(install_dir=str(self.games), prefix_dir=str(root / "GOG" / "01.Prefixes"),
                                       sync_playtime=False)

    def tearDown(self):
        self.sys_dirs.stop()
        self.paths.stop()
        self.env.stop()
        self.td.cleanup()

    def install_goverlay(self) -> Path:
        d = self.cfg / "goverlay"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def profile(self, name: str = PROFILE, conf: str = "[Config]\nGOVERLAY_MANGOHUD=1\n", bgmod: bool = True) -> Path:
        pdir = self.data / "goverlay" / "gameconfig" / name
        pdir.mkdir(parents=True, exist_ok=True)
        if conf is not None:
            (pdir / "bgmod.conf").write_text(conf)
        if bgmod:
            make_exe(pdir / "bgmod")
        return pdir

    def template(self) -> Path:
        t = self.data / "goverlay" / "bgmod"
        make_exe(t / "bgmod", "#!/bin/sh\n# vorlage\n")
        make_exe(t / "bgmod-uninstaller", "#!/bin/sh\n# uninstaller\n")
        return t


# ----------------------------------------------------------------------------- nonsteam_folders.txt
class FolderPlanTest(unittest.TestCase):
    def test_add_keep_user_lines_and_idempotent(self):
        exists = lambda p: True  # noqa: E731
        p = gp.plan_folder_lines(["/user/games", ""], ["/a/games/", "/b"], [], exists=exists)
        self.assertEqual(p.lines, ["/user/games", "", "/a/games", "/b"])
        self.assertEqual((p.added, p.removed, p.ours), (["/a/games", "/b"], [], ["/a/games", "/b"]))
        again = gp.plan_folder_lines(p.lines, ["/a/games", "/b"], p.ours, exists=exists)
        self.assertFalse(again.changed)
        self.assertEqual(again.lines, p.lines)
        self.assertEqual(again.ours, ["/a/games", "/b"])

    def test_user_line_is_not_ours(self):
        p = gp.plan_folder_lines(["  /a/games/  "], ["/a/games"], [], exists=lambda _p: True)
        self.assertFalse(p.changed)
        self.assertEqual(p.ours, [])
        off = gp.plan_folder_lines(p.lines, ["/a/games"], p.ours, enabled=False, exists=lambda _p: True)
        self.assertEqual(off.lines, ["  /a/games/  "])               # Zeile des Nutzers bleibt

    def test_remove_only_ours(self):
        p = gp.plan_folder_lines(["/user", "/a", "/b"], ["/a"], ["/a", "/b"], exists=lambda _p: True)
        self.assertEqual(p.lines, ["/user", "/a"])
        self.assertEqual(p.removed, ["/b"])
        off = gp.plan_folder_lines(p.lines, ["/a"], p.ours, enabled=False, exists=lambda _p: True)
        self.assertEqual((off.lines, off.removed, off.ours), (["/user"], ["/a"], []))

    def test_missing_root_not_added(self):
        p = gp.plan_folder_lines([], ["/gibt/es/nicht"], [], exists=lambda _p: False)
        self.assertEqual((p.lines, p.ours), ([], []))


class FolderSyncTest(TempEnv):
    def test_not_installed_writes_nothing(self):
        self.assertIsNone(gp.sync_folders(self.settings))
        self.assertFalse((self.cfg / "goverlay").exists())

    def test_roots_and_file(self):
        extra = self.root / "Extra"
        lib_games = self.root / "Lib" / "games"
        extra.mkdir()
        lib_games.mkdir(parents=True)
        self.settings.install_locations = [str(extra) + "/", str(self.home), "/"]
        self.settings.libraries = [CustomLibrary(id="x", name="X", games_dir=str(lib_games), prefix_dir=str(self.root / "p"))]
        self.assertEqual(gp.configured_roots(self.settings), [str(self.games), str(extra), str(lib_games)])
        d = self.install_goverlay()
        (d / gp.FOLDERS_FILE).write_text("/eigene/Spiele\n")
        plan = gp.sync_folders(self.settings)
        text = (d / gp.FOLDERS_FILE).read_text()
        self.assertEqual(text, f"/eigene/Spiele\n{self.games}\n{extra}\n{lib_games}\n")
        self.assertEqual(plan.ours, [str(self.games), str(extra), str(lib_games)])
        self.assertEqual(sorted(p.name for p in d.iterdir()), [gp.FOLDERS_FILE])   # keine Temp-Datei übrig
        mtime = (d / gp.FOLDERS_FILE).stat().st_mtime_ns
        self.settings.goverlay_added_folders = plan.ours
        self.assertFalse(gp.sync_folders(self.settings).changed)
        self.assertEqual((d / gp.FOLDERS_FILE).stat().st_mtime_ns, mtime)      # unverändert → nicht geschrieben
        self.assertTrue(gp.folder_registered(str(self.game_dir)))
        self.assertFalse(gp.folder_registered(str(self.games / "prefixes")))
        # abschalten: nur die eigenen Zeilen verschwinden
        self.settings.goverlay_register_folders = False
        plan = gp.sync_folders(self.settings)
        self.assertEqual((d / gp.FOLDERS_FILE).read_text(), "/eigene/Spiele\n")
        self.assertEqual(plan.ours, [])

    def test_symlinked_folder_file_stays_a_symlink(self):
        self.install_goverlay()
        self.games.mkdir(parents=True, exist_ok=True)
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / gp.FOLDERS_FILE
        target.write_text("/eigene/Spiele\n")
        link = self.cfg / "goverlay" / gp.FOLDERS_FILE
        link.symlink_to(target)
        gp.sync_folders(self.settings)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), f"/eigene/Spiele\n{self.games}\n")

    def test_sync_all_saves_added_folders(self):
        self.install_goverlay()
        backend = types.SimpleNamespace(settings=self.settings, registry=InstalledRegistry(), _covers={}, _meta={})
        rep = gp.sync_all(backend)
        self.assertEqual(rep.folders.added, [str(self.games)])
        self.assertEqual(self.settings.goverlay_added_folders, [str(self.games)])
        self.assertEqual(GlobalSettings.load().goverlay_added_folders, [str(self.games)])
        (self.cfg / "goverlay" / gp.FOLDERS_FILE).unlink()
        (self.cfg / "goverlay").rmdir()
        self.assertFalse(gp.sync_all(backend).installed)                # GOverlay weg → nichts tun

    def test_auto_sync_runs_on_change(self):
        self.install_goverlay()
        backend = types.SimpleNamespace(settings=self.settings, registry=InstalledRegistry(), _covers={}, _meta={})
        sync = gp.AutoSync(backend)

        def wait(n):
            end = time.time() + 5
            while (sync.runs < n or sync._running) and time.time() < end:
                time.sleep(0.01)
            self.assertEqual(sync.runs, n)
        self.assertTrue(sync.request())
        wait(1)
        self.assertFalse(sync.request())                     # nichts geändert
        extra = self.root / "Neu"
        extra.mkdir()
        self.settings.install_locations.append(str(extra))
        self.assertTrue(sync.request())
        wait(2)
        self.assertIn(str(extra), (self.cfg / "goverlay" / gp.FOLDERS_FILE).read_text())
        self.assertIs(gp.auto_sync(backend), gp.auto_sync(backend))
        self.assertIsNone(gp.auto_sync(types.SimpleNamespace(), create=False))


# ----------------------------------------------------------------------------- Cover
class CoverTest(TempEnv):
    def png(self) -> Path:
        from PIL import Image
        p = self.root / "cover.png"
        Image.new("RGB", (1200, 1800), (120, 60, 200)).save(p)
        return p

    def test_seed_real_jpeg_with_pillow(self):
        self.assertTrue(gp.seed_cover(GAME, self.png()))
        dest = self.home / ".cache" / "goverlay" / "nonsteam_covers" / f"{PROFILE}.jpg"
        data = dest.read_bytes()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        from PIL import Image
        with Image.open(dest) as im:
            self.assertEqual(im.size, (600, 900))
        self.assertFalse(dest.with_name(dest.name + ".fallback").exists())
        self.assertFalse(gp.seed_cover(GAME, self.png()))            # vorhandenes echtes Cover bleibt

    def test_fallback_is_replaced_and_marker_removed(self):
        dest = gp.cover_path(GAME)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"platzhalter")
        marker = dest.with_name(dest.name + ".fallback")
        marker.write_text("")
        self.assertTrue(gp.seed_cover(GAME, self.png()))
        self.assertEqual(dest.read_bytes()[:3], b"\xff\xd8\xff")
        self.assertFalse(marker.exists())

    def test_without_pillow_only_jpeg_is_copied(self):
        jpg = self.root / "c.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0echtes-jpeg")
        png = self.png()
        with mock.patch.dict(sys.modules, {"PIL": None}):
            self.assertFalse(gp.seed_cover("A", png))
            self.assertFalse(gp.cover_path("A").exists())
            self.assertTrue(gp.seed_cover("B", jpg))
        self.assertEqual(gp.cover_path("B").read_bytes(), jpg.read_bytes())

    def test_sync_seeds_from_image_cache(self):
        from galaxy_linux import paths
        import hashlib
        self.install_goverlay()
        url = "https://images.gog.com/abc_glx_vertical_cover.jpg"
        (paths.API_CACHE_DIR / "gamesdb-1.json").write_text(json.dumps(
            {"game": {"vertical_cover": {"url_format": "https://images.gog.com/abc{formatter}.{ext}"}}}))
        cached = paths.IMAGE_CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")
        cached.write_bytes(self.png().read_bytes())
        reg = InstalledRegistry()
        reg.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))
        backend = types.SimpleNamespace(settings=self.settings, registry=reg, _covers={}, _meta={})
        self.assertEqual(gp.cached_cover_url(backend, "1"), url)
        rep = gp.sync_all(backend)
        self.assertEqual(rep.covers, 1)
        self.assertTrue(gp.cover_path(GAME).is_file())
        self.assertEqual(gp.sync_all(backend).covers, 0)


# ----------------------------------------------------------------------------- bgmod.conf
class BgmodConfTest(unittest.TestCase):
    SAMPLE = """[Config]
SHOW_LAUNCH_SPLASH=1
GOVERLAY_TWEAKS=1
gamemode=1
winedetectionenable=0
TARGET_EXE=Tropico6-Win64-Shipping.exe
; Kommentar
[Launchers]
Cyberpunk 2077=REDprelauncher.exe|bin/x64/Cyberpunk2077.exe
[Env]
MANGOHUD_CONFIGFILE=/home/x/.local/share/goverlay/gameconfig/Tropico 6/MangoHud.conf
PROTON_ENABLE_WAYLAND=1
=game-performance
[Args]
"""

    def test_parse(self):
        c = gp.parse_bgmod_conf(self.SAMPLE)
        self.assertEqual(c.get("config", "GAMEMODE"), "1")
        self.assertEqual(c.get("Config", "winedetectionenable"), "0")
        self.assertEqual(c.get("launchers", "Cyberpunk 2077"), "REDprelauncher.exe|bin/x64/Cyberpunk2077.exe")
        self.assertTrue(c.get("env", "MANGOHUD_CONFIGFILE").endswith("Tropico 6/MangoHud.conf"))
        self.assertEqual(c.broken_flags["env"], ["game-performance"])
        self.assertTrue(c.has_flag("env", "game-performance"))
        self.assertEqual(c.get("config", "fehlt", "x"), "x")
        self.assertIn("args", c.sections)

    def test_flag_forms(self):
        for line in ("game-performance", "=game-performance", "game-performance=1"):
            self.assertTrue(gp.parse_bgmod_conf(f"[Env]\n{line}\n").has_flag("env", "game-performance"), line)
        self.assertEqual(gp.parse_bgmod_conf("[Env]\ngame-performance\n").flags["env"], ["game-performance"])
        self.assertFalse(gp.parse_bgmod_conf("").has_flag("env", "game-performance"))


# ----------------------------------------------------------------------------- Profil und Startbefehl
class ProfileTest(TempEnv):
    def test_auto_profile_and_choices(self):
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings).kind, "none")
        pdir = self.profile()
        prof = gp.resolve_profile(str(self.game_dir), "", self.settings)
        self.assertEqual((prof.kind, prof.name, prof.bgmod), ("game", PROFILE, Path(os.path.realpath(pdir / "bgmod"))))
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "none", self.settings).kind, "off")
        self.settings.goverlay_use_profiles = False
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings).kind, "off")
        self.profile("Anderes")
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "Anderes", self.settings).name, "Anderes")   # ausdrücklich
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "../x", self.settings).kind, "none")
        self.assertEqual(gp.list_profiles(), ["Anderes", PROFILE])

    def test_global_fallback(self):
        self.profile(gp.GLOBAL_PROFILE)
        self.assertFalse(GlobalSettings().goverlay_global_fallback)            # nur auf Wunsch (goverlay.md §5.6)
        self.settings.goverlay_global_fallback = False
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings).kind, "none")
        self.settings.goverlay_global_fallback = True
        prof = gp.resolve_profile(str(self.game_dir), "", self.settings)
        self.assertEqual(prof.kind, "global")
        self.profile()                                               # eigenes Profil geht vor
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings).kind, "game")
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "global", self.settings).kind, "global")

    def test_symlink_outside_and_long_path(self):
        pdir = self.profile(conf=None, bgmod=False)
        outside = make_exe(self.root / "usr" / "lib" / "bgmod")
        (pdir / "bgmod").symlink_to(outside)
        prof = gp.resolve_profile(str(self.game_dir), "", self.settings)
        self.assertEqual(prof.kind, "none")
        self.assertIn("Symlink", prof.reason)
        (pdir / "bgmod.conf").write_text("[Config]\n")
        self.template()
        prof = gp.resolve_profile(str(self.game_dir), "", self.settings)
        self.assertTrue(prof.needs_copy)                             # Symlink wird durch eine echte Kopie ersetzt
        long_name = "x" * 250
        self.profile(long_name)
        self.assertIn("255", gp.resolve_profile("/spiele/" + long_name, "", self.settings).reason)

    def test_describe_and_status(self):
        backend = types.SimpleNamespace(settings=self.settings, registry=InstalledRegistry(), game_settings=GameSettingsStore())
        backend.registry.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))
        self.assertIsNone(gp.status_for(backend, "1"))               # GOverlay nicht installiert
        self.install_goverlay()
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('kein Profil – Spiele-Ordner ist nicht in GOverlay eingetragen'))
        (self.cfg / "goverlay" / gp.FOLDERS_FILE).write_text(f"{self.games}\n")
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('kein Profil – in GOverlay das Spiel anklicken'))
        self.profile()
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('GOverlay-Profil: {0} (pro Spiel)').format(PROFILE))
        backend.game_settings.update("1", goverlay_profile="none")
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('GOverlay-Profil: aus'))
        self.assertEqual(gp.describe(gp.Profile("global")), gp.tr('globales GOverlay-Profil'))
        self.assertIsNone(gp.status_for(backend, "2"))


def plan(**kw) -> LaunchPlan:
    p = LaunchPlan(env={"ENABLE_VKBASALT": "1"}, cwd="/g", tool=None, prefix="/p", runner="umu", description="umu",
                   base=["/usr/bin/umu-run", "/g/game.exe", "-x"], exe="/g/game.exe")
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class ApplyProfileTest(unittest.TestCase):
    def prof(self, conf: str = "") -> gp.Profile:
        return gp.Profile("game", "Spiel", Path("/cfg/Spiel"), bgmod=Path("/cfg/Spiel/bgmod"), conf=gp.parse_bgmod_conf(conf))

    def test_windows_with_gamemode_and_wine_detection(self):
        p = plan(wrappers=["gamemoderun", "mangohud"], compositor=["gamescope", "-f", "--"],
                 user_wrappers=["/h/goverlay/gameconfig/global/bgmod", "--", "strangle", "60"], args=["-skip"])
        notes = gp.apply_profile(p, self.prof("[Config]\ngamemode=1\nwinedetectionenable=0\n"), True,
                                 own_vkbasalt=True, which=lambda n: f"/usr/bin/{n}")
        self.assertEqual(p.argv, ["strangle", "60", "gamescope", "-f", "--", "/cfg/Spiel/bgmod", "--", "env", "gamemoderun",
                                  "/usr/bin/umu-run", "/g/game.exe", "-x", "-skip", "/WineDetectionEnabled:False"])
        self.assertNotIn("ENABLE_VKBASALT", p.env)
        self.assertTrue(any("MangoHud" in n for n in notes) and any("vkBasalt" in n for n in notes))
        self.assertTrue(any("global/bgmod" in n for n in notes))

    def test_native_keeps_mangohud_no_wine_flag(self):
        p = plan(wrappers=["mangohud"], base=["/g/start.sh"], runner="native")
        gp.apply_profile(p, self.prof("[Config]\nwinedetectionenable=0\ngamemode=1\n"), False, which=lambda n: None)
        self.assertEqual(p.argv, ["/cfg/Spiel/bgmod", "mangohud", "/g/start.sh"])     # gamemoderun fehlt → nur Hinweis
        self.assertEqual(p.env["ENABLE_VKBASALT"], "1")                                  # nicht unser Schalter

    def test_broken_game_performance_line(self):
        p = plan()
        gp.apply_profile(p, self.prof("[Config]\nGOVERLAY_TWEAKS=1\n[Env]\n=game-performance\n"), True, which=lambda n: "/x")
        self.assertEqual(p.tool_wrappers, ["/cfg/Spiel/bgmod", "game-performance"])
        p = plan()
        gp.apply_profile(p, self.prof("[Config]\nGOVERLAY_TWEAKS=1\n[Env]\ngame-performance\n"), True, which=lambda n: "/x")
        self.assertEqual(p.tool_wrappers, ["/cfg/Spiel/bgmod"])                          # das macht bgmod selbst

    def test_inactive_profile_changes_nothing(self):
        p = plan(wrappers=["mangohud"])
        self.assertEqual(gp.apply_profile(p, gp.Profile("none"), True), [])
        self.assertEqual(p.argv, ["mangohud", "/usr/bin/umu-run", "/g/game.exe", "-x"])


class LaunchIntegrationTest(TempEnv):
    def setUp(self):
        super().setUp()
        self.gs = GameSettingsStore()
        self.registry = InstalledRegistry()
        self.registry.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))
        lin = self.games / "Lin Spiel"
        make_exe(lin / "start.sh")
        self.registry.set(InstalledGame("2", "Lin Spiel", "linux", str(lin), executable="start.sh"))
        self.launcher = Launcher(self.settings, self.gs, self.registry, PlaytimeStore())
        self.hp = mock.patch.dict(hooks._hooks, clear=True)
        self.hp.start()
        self.lp = mock.patch.object(hooks, "_loaded", True)
        self.lp.start()
        hooks.register_hook(GOverlayHook())
        self.proton = compat.CompatTool("proton:/opt/p", "Proton X", "proton", "/opt/p", "x", "System", requires_runtime="1628350")
        self.gs.set("1", GameSettings(compat_tool=self.proton.id, use_mangohud=True))

    def tearDown(self):
        self.lp.stop()
        self.hp.stop()
        super().tearDown()

    def mocks(self):
        return (mock.patch.object(compat, "resolve", return_value=self.proton),
                mock.patch.object(compat, "umu_available", return_value=True),
                mock.patch.object(compat, "umu_path", return_value="/usr/bin/umu-run"),
                mock.patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"))

    def preview(self, pid="1"):
        a, b, c, d = self.mocks()
        with a, b, c, d:
            return self.launcher.preview_plan(pid)

    def test_without_profile_only_install_path(self):
        p = self.preview()
        self.assertEqual(p.env["STEAM_COMPAT_INSTALL_PATH"], str(self.game_dir))
        self.assertEqual(p.argv[0], "mangohud")
        self.assertEqual(p.tool_wrappers, [])
        self.gs.update("1", env={"STEAM_COMPAT_INSTALL_PATH": "/eigen"})
        self.assertEqual(self.preview().env["STEAM_COMPAT_INSTALL_PATH"], "/eigen")

    def test_profile_in_preview(self):
        pdir = self.profile(conf="[Config]\ngamemode=1\nwinedetectionenable=0\n")
        p = self.preview()
        self.assertEqual(p.argv[:4], [os.path.realpath(pdir / "bgmod"), "--", "env", "gamemoderun"])
        self.assertNotIn("mangohud", p.argv)
        self.assertEqual(p.argv[-1], "/WineDetectionEnabled:False")
        self.assertEqual(p.argv[4:6], ["/usr/bin/umu-run", str(self.game_dir / "game.exe")])
        lin = self.preview("2")
        self.assertEqual(lin.tool_wrappers, [])                       # kein Profil „Lin Spiel“, kein globales

    def test_template_copied_on_launch(self):
        pdir = self.profile(bgmod=False)
        self.template()
        p = self.preview()                                            # Vorschau zeigt bgmod, kopiert aber nichts
        self.assertEqual(p.tool_wrappers, [str(Path(os.path.realpath(pdir)) / "bgmod")])
        self.assertFalse((pdir / "bgmod").exists())

        class Proc:
            pid = 99

            def wait(self):
                return 0

            def poll(self):
                return 0
        a, b, c, d = self.mocks()
        with a, b, c, d, mock.patch("subprocess.Popen", return_value=Proc()) as popen:
            done = threading.Event()
            self.launcher.launch("1", on_exit=lambda *_: done.set())
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertTrue(done.wait(5))
        self.assertEqual(argv[0], os.path.realpath(pdir / "bgmod"))
        self.assertEqual(env["STEAM_COMPAT_INSTALL_PATH"], str(self.game_dir))
        for name in ("bgmod", "bgmod-uninstaller"):
            f = pdir / name
            self.assertTrue(f.is_file() and not f.is_symlink())
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o755)
        self.assertIn("vorlage", (pdir / "bgmod").read_text())
        self.assertEqual(os.readlink(pdir / "fgmod"), "bgmod")
        log_text = (self.root / "gl" / "logs" / "1.log").read_text()
        self.assertIn(f"GOverlay-Profil {PROFILE}", log_text)

    def test_bgmod_from_launch_options_used_once(self):
        """Bekannter Fehler: global/bgmod in den Startoptionen + globaler Rückfall → bgmod wickelte bgmod ein."""
        gdir = self.profile(gp.GLOBAL_PROFILE)
        self.settings.goverlay_global_fallback = True
        self.gs.update("1", launch_options=f'"{gdir}/bgmod" %command%')
        p = self.preview()
        bg = [a for a in p.argv if os.path.basename(a) in ("bgmod", "fgmod")]
        self.assertEqual(bg, [os.path.realpath(gdir / "bgmod")])
        self.settings.goverlay_global_fallback = False                  # auch ohne Rückfall: Profil aus den Startoptionen
        p = self.preview()
        self.assertEqual(p.tool_wrappers, [os.path.realpath(gdir / "bgmod")])
        self.assertEqual(p.user_wrappers, [])
        backend = types.SimpleNamespace(settings=self.settings, registry=self.registry, game_settings=self.gs)
        prof = gp.profile_for_game(backend, "1")
        self.assertEqual((prof.kind, prof.from_options), ("global", True))
        self.assertEqual(gp.describe(prof), gp.tr('GOverlay-Profil: {0} (aus den Startoptionen, wird einmal vorangestellt)')
                         .format(gp.tr('globales GOverlay-Profil')))
        self.gs.update("1", goverlay_profile="none")                    # Profil aus: Startoptionen bleiben, wie sie sind
        self.assertEqual(self.preview().user_wrappers, [str(gdir / "bgmod")])

    def test_nonsteam_command_is_a_wrapper(self):
        pdir = self.profile(conf="[Config]\ngamemode=1\nwinedetectionenable=0\n")
        self.gs.update("1", launch_options=f'"{pdir}/bgmod" -- env gamemoderun /WineDetectionEnabled:False')
        p = self.preview()
        self.assertEqual(p.argv[:4], [os.path.realpath(pdir / "bgmod"), "--", "env", "gamemoderun"])
        self.assertEqual(p.argv.count("gamemoderun"), 1)
        self.assertEqual(p.argv[-1], "/WineDetectionEnabled:False")
        self.assertEqual(p.argv.count("/WineDetectionEnabled:False"), 1)
        self.assertEqual(p.args, ["/WineDetectionEnabled:False"])
        self.gs.update("1", goverlay_profile="none")
        p = self.preview()
        self.assertEqual(p.user_wrappers, [str(pdir / "bgmod"), "--", "env", "gamemoderun"])
        self.assertEqual(p.args, ["/WineDetectionEnabled:False"])

    def test_legacy_launch_options_from_0_1_6(self):
        self.profile(conf="[Config]\nGOVERLAY_MANGOHUD=0\n")
        self.gs.update("1", use_mangohud=False, launch_options="ENABLE_VKBASALT=1 MANGOHUD=1 mangohud %command% -x2")
        p = self.preview()
        self.assertNotIn("mangohud", p.argv)
        self.assertNotIn("ENABLE_VKBASALT", p.env)
        self.assertNotIn("MANGOHUD", p.env)
        self.assertEqual(p.args, ["-x2"])
        self.gs.update("1", env={"ENABLE_VKBASALT": "1"})                # eigene Umgebung bleibt
        self.assertEqual(self.preview().env["ENABLE_VKBASALT"], "1")

    def test_explicit_env_wins_over_profile_env(self):
        pdir = self.profile(conf="[Config]\ngamemode=1\n[Env]\nPROTON_ENABLE_WAYLAND=1\nPROTON_USE_WOW64=1\n")
        self.gs.update("1", wayland_driver="off")
        p = self.preview()
        self.assertEqual(p.env["PROTON_ENABLE_WAYLAND"], "0")
        self.assertEqual(p.argv[:5], [os.path.realpath(pdir / "bgmod"), "--", "env", "PROTON_ENABLE_WAYLAND=0",
                                      "gamemoderun"])
        self.gs.update("1", wayland_driver="", launch_options="PROTON_USE_WOW64=0 %command%")
        p = self.preview()
        self.assertEqual(p.argv[:4], [os.path.realpath(pdir / "bgmod"), "--", "env", "PROTON_USE_WOW64=0"])
        self.gs.update("1", launch_options="")                           # globale Vorgabe: Profil bestimmt
        self.assertEqual(self.preview().argv[:4], [os.path.realpath(pdir / "bgmod"), "--", "env", "gamemoderun"])

    def test_registered_hook(self):
        import importlib
        from galaxy_linux import backend
        sys.modules.pop("galaxy_linux.launch.hook_goverlay", None)
        importlib.import_module("galaxy_linux.launch.hook_goverlay")
        h = next(h for h in hooks.hooks() if h.name == "goverlay")
        self.assertEqual((h.order, h.setup), (50, False))
        self.assertTrue(any(getattr(f, "__name__", "") == "undo_bgmod_changes" for f in backend._files_change_hooks))


# ----------------------------------------------------------------------------- bgmod-uninstaller
class UninstallerTest(TempEnv):
    def setUp(self):
        super().setUp()
        self.out = self.root / "uninstaller.txt"
        self.backend = types.SimpleNamespace(settings=self.settings, registry=InstalledRegistry(), game_settings=GameSettingsStore())
        self.backend.registry.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))

    def make_profile(self, name: str, game_dir: str) -> Path:
        pdir = self.profile(name, conf=f"[Config]\nGAME_DIR={game_dir}\n")
        make_exe(pdir / "bgmod-uninstaller",
                 f'#!/bin/sh\necho "$BGMOD_CONFIG_DIR|$BGMOD_BACKUPS_DIR|$STEAM_COMPAT_INSTALL_PATH|$1|$(pwd)" >> "{self.out}"\n')
        return pdir

    def test_runs_for_matching_profile(self):
        pdir = self.make_profile(PROFILE, f"{self.game_dir}/bin/")
        self.assertEqual(gp.uninstall_candidates(str(self.game_dir), ""), [pdir])
        status: list[str] = []
        self.assertEqual(gp.undo_bgmod_changes(self.backend, "1", "verify", status.append), 1)
        line = self.out.read_text().strip()
        self.assertEqual(line, f"{pdir}/|{pdir}/backups|{self.game_dir}|--|{self.game_dir}")
        self.assertEqual(status, [gp.tr('GOverlay-Änderungen werden zurückgenommen …')])
        self.assertIn("bgmod-uninstaller", (self.root / "gl" / "logs" / "1.log").read_text())

    def test_skips_other_game_and_global(self):
        self.make_profile(PROFILE, "/ganz/anderes/Spiel")
        self.make_profile(gp.GLOBAL_PROFILE, str(self.game_dir))
        self.backend.game_settings.update("1", goverlay_profile="global")
        self.assertEqual(gp.undo_bgmod_changes(self.backend, "1", "uninstall"), 0)
        self.assertFalse(self.out.exists())

    def test_backend_hook_isolation(self):
        from galaxy_linux import backend
        calls = []

        def boom(b, pid, action, status):
            raise RuntimeError("x")
        with mock.patch.object(backend, "_files_change_hooks", [boom, lambda *a: calls.append(a[1:3])]), \
             self.assertLogs("galaxy_linux.backend", "ERROR"):
            backend.Backend.before_files_change(types.SimpleNamespace(), "1", "move")
        self.assertEqual(calls, [("1", "move")])

    def test_job_queue_calls_hook_point(self):
        from galaxy_linux import backend
        fake = mock.Mock(updates={})
        with mock.patch.object(threading.Thread, "start"), mock.patch.object(backend, "_job_finished_hooks", []):
            q = backend.JobQueue(fake)
            for kind in ("verify", "move", "update", "prefix"):
                fake.reset_mock()
                q._execute(backend.Job(kind, "1", "Demo", install_root="/neu"))
                if kind == "prefix":
                    fake.before_files_change.assert_not_called()
                else:
                    self.assertEqual(fake.before_files_change.call_args.args[:2], ("1", kind))


# ----------------------------------------------------------------------------- „Aus GOverlay übernehmen“
class ImportTest(unittest.TestCase):
    def test_strip_profile_parts(self):
        for raw, want in (("ENABLE_VKBASALT=1 mangohud %command% -skipintro", "-skipintro"),
                          ('"/h/.local/share/goverlay/gameconfig/global/bgmod" %command%', ""),
                          ("DISABLE_VULKAN_LAYER_LS_frame_generation=1 %command%", "DISABLE_VULKAN_LAYER_LS_frame_generation=1 %command%"),
                          ("PROTON_LOG=1 /p/bgmod -- env gamemoderun %command% -x", "PROTON_LOG=1 %command% -x"),
                          ("/p/bgmod -- strangle 60 %command%", "strangle 60 %command%"),
                          ("mangohud %command%", ""), ("", "")):
            self.assertEqual(goverlay.strip_profile_parts(raw), want, raw)

    def test_no_mangohud_line_with_profile(self):
        self.assertEqual(goverlay.import_from_goverlay("mangohud %command% -w", profile_active=True), "-w")
        st = goverlay.ToolStatus(goverlay=True, mangohud=True, mangohud32=False, vkbasalt=False, gamemode=False,
                                 gamescope=False, mangohud_config=True, vkbasalt_config=False)
        with mock.patch.object(goverlay, "status", return_value=st):
            self.assertEqual(goverlay.import_from_goverlay("egal"), "mangohud %command%")        # ohne Profil wie bisher
        self.assertEqual(goverlay.build_launch_options(mangohud=False, gamescope_args="-f "), "gamescope -f -- %command%")


if __name__ == "__main__":
    unittest.main()
