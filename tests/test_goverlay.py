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
            LOG_DIR=gl / "logs", IMAGE_CACHE_DIR=gl / "images", API_CACHE_DIR=gl / "api", DATA_DIR=gl / "data",
            CONFIG_DIR=gl / "cfg", CACHE_DIR=gl / "cache")
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


class ViewPlanTest(unittest.TestCase):
    def test_card_names(self):
        for raw, want in (("LEGO® City Undercover", "LEGO City Undercover"), ("AC/DC: Rock", "AC-DC: Rock"),
                          ("  .Versteckt  ", "Versteckt"), ("Star Trek Voyager™: Elite Force", "Star Trek Voyager: Elite Force"),
                          ("a\tb\nc", "abc"), ("", "game"), ("x" * 200, "x" * gp.NAME_MAX)):
            self.assertEqual(gp.card_name(raw), want, raw)

    def test_unique_only_for_different_games(self):
        games = [gp.ViewGame("1", "Freelancer", "/g/Freelancer"), gp.ViewGame("2", "Freelancer", "/e/Freelancer"),
                 gp.ViewGame("3", "Metro: X", "/g/MetroX"), gp.ViewGame("4", "Metro_ X", "/g/Metro_X"),
                 gp.ViewGame("5", "Downloads", "/g/dl"), gp.ViewGame("6", "Zeus and Poseidon", "/g/Zeus and Poseidon_bck")]
        plan = {g.pid: g.name for g in gp.plan_view(games)}
        self.assertEqual(plan["1"], "Freelancer")
        self.assertEqual(plan["2"], "Freelancer (2)")
        self.assertEqual({plan["3"], plan["4"]}, {"Metro: X", "Metro_ X (2)"})   # gleicher Profilname in GOverlay
        self.assertEqual(plan["5"], "Downloads (2)")                            # GOverlay überspringt „downloads“
        self.assertEqual(plan["6"], "Zeus and Poseidon")                         # Titel, nie der Ordnername

    def test_existing_names_stay_stable(self):
        games = [gp.ViewGame("1", "Freelancer", "/g/a"), gp.ViewGame("2", "Freelancer", "/g/b")]
        plan = {g.pid: g.name for g in gp.plan_view(games, {"Freelancer (2)": "/g/a", "Freelancer": "/g/b"})}
        self.assertEqual(plan, {"1": "Freelancer (2)", "2": "Freelancer"})
        plan = {g.pid: g.name for g in gp.plan_view([gp.ViewGame("1", "Neuer Titel", "/g/a")], {"Freelancer": "/g/a"})}
        self.assertEqual(plan, {"1": "Neuer Titel"})                             # Titel geändert → neuer Name
        plan = {g.pid: g.name for g in gp.plan_view([gp.ViewGame("1", "Spiel", "/g/a")], {}, reserved=["Spiel"])}
        self.assertEqual(plan, {"1": "Spiel (2)"})                               # fremde Datei im Ordner


class ViewSyncTest(TempEnv):
    def setUp(self):
        super().setUp()
        self.ext = self.root / "Repack" / "02.Games"
        self.reg = InstalledRegistry()
        self.gs = GameSettingsStore()
        self.reg.set(InstalledGame("1", "LEGO® City Undercover", "windows", str(self.game_dir)))
        for pid, folder, extra in (("ext-freelancer", "Freelancer", {}),
                                   ("ext-freelancer-kopie", "Freelancer - Kopie", {"duplicate_of": "ext-freelancer"}),
                                   ("ext-freelancer-hd", "Freelancer HD Edition", {}),
                                   ("ext-hidden", "Verstecktes Spiel", {})):
            (self.ext / folder).mkdir(parents=True)
            g = InstalledGame(pid, {"ext-freelancer-hd": "Freelancer (HD Edition)"}.get(pid, folder), "windows",
                              str(self.ext / folder), method="external", library="external")
            for k, v in extra.items():
                setattr(g, k, v)
            self.reg.set(g)
        self.reg.set(InstalledGame("2", "Weg", "windows", str(self.root / "fehlt")))
        self.gs.update("ext-hidden", hidden=True)
        self.backend = types.SimpleNamespace(settings=self.settings, registry=self.reg, game_settings=self.gs,
                                             _covers={}, _meta={})
        self.view = self.data_dir() / gp.VIEW_DIR

    def data_dir(self) -> Path:
        from galaxy_linux import paths
        return paths.DATA_DIR

    def links(self) -> dict[str, str]:
        return {p.name: os.readlink(p) for p in self.view.iterdir() if p.is_symlink()}

    def test_not_installed_writes_nothing(self):
        self.assertIsNone(gp.sync_folders(self.settings))
        self.assertFalse(gp.sync_all(self.backend).installed)
        self.assertFalse((self.cfg / "goverlay").exists())
        self.assertFalse(self.view.exists())

    def test_view_links_and_folder_migration(self):
        d = self.install_goverlay()
        old_roots = [str(self.games), str(self.ext)]
        (d / gp.FOLDERS_FILE).write_text(f"/eigene/Spiele\n{old_roots[0]}\n\n{old_roots[1]}\n")
        self.settings.goverlay_added_folders = list(old_roots)
        rep = gp.sync_all(self.backend, covers=False)
        self.assertEqual(self.links(), {"LEGO City Undercover": str(self.game_dir), "Freelancer": str(self.ext / "Freelancer"),
                                        "Freelancer (HD Edition)": str(self.ext / "Freelancer HD Edition")})
        self.assertEqual((d / gp.FOLDERS_FILE).read_text(), f"/eigene/Spiele\n\n{self.view}\n")
        self.assertEqual(sorted(rep.folders.removed), sorted(old_roots))
        self.assertEqual(self.settings.goverlay_added_folders, [str(self.view)])
        self.assertEqual(GlobalSettings.load().goverlay_added_folders, [str(self.view)])
        self.assertEqual(sorted(p.name for p in d.iterdir()), [gp.FOLDERS_FILE])   # keine Temp-Datei übrig
        self.assertEqual(gp.read_state()["ext-freelancer-hd"], "Freelancer (HD Edition)")
        self.assertTrue(gp.in_view("1"))
        self.assertFalse(gp.in_view("ext-freelancer-kopie"))
        # GOverlay folgt dem Symlink: ein Spielordner pro Karte
        self.assertTrue((self.view / "Freelancer").is_dir())
        mtime = (d / gp.FOLDERS_FILE).stat().st_mtime_ns
        again = gp.sync_all(self.backend, covers=False)
        self.assertFalse(again.view.changed or again.folders.changed)
        self.assertEqual((d / gp.FOLDERS_FILE).stat().st_mtime_ns, mtime)          # unverändert → nicht geschrieben
        # Titel geändert, Spiel entfernt, kaputter Link, fremde Datei: nur eigene Links werden angepasst
        self.gs.update("1", custom_title="LEGO City")
        self.reg.remove("ext-freelancer")
        os.symlink(str(self.root / "gibt-es-nicht"), self.view / "Kaputt")
        (self.view / "notiz.txt").write_text("vom Nutzer")
        rep = gp.sync_all(self.backend, covers=False)
        self.assertEqual(sorted(rep.view.removed), ["Freelancer", "Kaputt", "LEGO City Undercover"])
        self.assertEqual(sorted(self.links()), ["Freelancer (HD Edition)", "LEGO City"])
        self.assertTrue((self.view / "notiz.txt").exists())
        # abschalten: Links weg, nur die eigene Zeile verschwindet
        self.settings.goverlay_register_folders = False
        gp.sync_all(self.backend)
        self.assertEqual((d / gp.FOLDERS_FILE).read_text(), "/eigene/Spiele\n\n")
        self.assertEqual(self.links(), {})
        self.assertEqual(self.settings.goverlay_added_folders, [])

    def test_user_line_for_view_is_not_ours(self):
        d = self.install_goverlay()
        self.view.mkdir(parents=True)
        (d / gp.FOLDERS_FILE).write_text(f"{self.view}\n")
        rep = gp.sync_all(self.backend, covers=False)
        self.assertEqual(rep.folders.ours, [])
        self.settings.goverlay_register_folders = False
        gp.sync_all(self.backend, covers=False)
        self.assertEqual((d / gp.FOLDERS_FILE).read_text(), f"{self.view}\n")       # Zeile des Nutzers bleibt

    def test_symlinked_folder_file_stays_a_symlink(self):
        self.install_goverlay()
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / gp.FOLDERS_FILE
        target.write_text("/eigene/Spiele\n")
        link = self.cfg / "goverlay" / gp.FOLDERS_FILE
        link.symlink_to(target)
        gp.sync_all(self.backend, covers=False)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), f"/eigene/Spiele\n{self.view}\n")

    def test_dry_run_writes_nothing(self):
        names = [v.name for v in gp.dry_run(self.backend)]
        self.assertEqual(names, ["Freelancer", "Freelancer (HD Edition)", "LEGO City Undercover"])
        self.assertFalse(self.view.exists())
        self.assertFalse(self.data_dir().exists() and any(self.data_dir().iterdir()))

    def test_auto_sync_runs_on_change(self):
        self.install_goverlay()
        sync = gp.AutoSync(self.backend)

        def wait(n):
            end = time.time() + 5
            while (sync.runs < n or sync._running) and time.time() < end:
                time.sleep(0.01)
            self.assertEqual(sync.runs, n)
        self.assertTrue(sync.request())
        wait(1)
        self.assertFalse(sync.request())                     # nichts geändert
        (self.ext / "Neu").mkdir()
        self.reg.set(InstalledGame("ext-neu", "Neues Spiel", "windows", str(self.ext / "Neu"), method="external"))
        self.assertTrue(sync.request())
        wait(2)
        self.assertIn("Neues Spiel", self.links())
        self.assertIs(gp.auto_sync(self.backend), gp.auto_sync(self.backend))
        self.assertIsNone(gp.auto_sync(types.SimpleNamespace(), create=False))


# ----------------------------------------------------------------------------- Cover
def jpeg(size=(600, 900), color=(120, 60, 200)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


class CoverRulesTest(unittest.TestCase):
    def test_jpeg_size_and_placeholder(self):
        self.assertEqual(gp.jpeg_size(jpeg((150, 215))), (150, 215))
        self.assertTrue(gp.is_placeholder(jpeg((150, 215))))
        self.assertFalse(gp.is_placeholder(jpeg((600, 900))))
        self.assertIsNone(gp.jpeg_size(b"kein jpeg"))

    def test_decision(self):
        ours = jpeg()
        entry = {"sha1": gp._sha1(ours), "src": "/a|1|2"}
        self.assertEqual(gp.cover_decision(None, False, None, "x"), "write")               # fehlt
        self.assertEqual(gp.cover_decision(b"", False, entry, "x"), "write")
        self.assertEqual(gp.cover_decision(jpeg(color=(1, 2, 3)), False, None, "x"), "write")  # GOverlays Steam/Bing-Treffer
        self.assertEqual(gp.cover_decision(ours, True, entry, "/a|1|2"), "write")          # .fallback
        self.assertEqual(gp.cover_decision(jpeg((150, 215)), False, entry, "/a|1|2"), "write")  # Platzhalter ohne Marker
        self.assertEqual(gp.cover_decision(ours, False, entry, "/a|1|2"), "same")
        self.assertEqual(gp.cover_decision(ours, False, entry, "/b|1|2"), "write")         # eigenes Cover, neue Quelle
        self.assertEqual(gp.cover_decision(jpeg(color=(9, 9, 9)), False, entry, "/b|1|2"), "keep")  # Wahl des Nutzers


class CoverSyncTest(TempEnv):
    def setUp(self):
        super().setUp()
        from galaxy_linux import paths
        import hashlib
        self.install_goverlay()
        self.url = "https://images.gog.com/abc_glx_vertical_cover.jpg"
        (paths.API_CACHE_DIR / "gamesdb-1.json").write_text(json.dumps(
            {"game": {"vertical_cover": {"url_format": "https://images.gog.com/abc{formatter}.{ext}"}}}))
        self.cached = paths.IMAGE_CACHE_DIR / (hashlib.sha1(self.url.encode()).hexdigest() + ".jpg")
        from PIL import Image
        Image.new("RGB", (1200, 1800), (120, 60, 200)).save(self.cached, "JPEG")
        self.reg = InstalledRegistry()
        self.reg.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))
        ext = self.root / "ext" / "Ohne Cover"
        ext.mkdir(parents=True)
        self.reg.set(InstalledGame("ext-x", "Ohne Cover", "windows", str(ext), method="external"))
        self.gs = GameSettingsStore()
        self.backend = types.SimpleNamespace(settings=self.settings, registry=self.reg, game_settings=self.gs,
                                             _covers={}, _meta={})

    def test_covers_from_cache_and_title_card(self):
        self.assertEqual(gp.cached_cover_url(self.backend, "1"), self.url)
        dest = gp.cover_path(GAME)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(jpeg(color=(0, 255, 0)))                     # GOverlays falscher Steam-Treffer
        card = gp.cover_path("Ohne Cover")
        rep = gp.sync_all(self.backend)
        self.assertEqual(rep.covers, 2)
        from PIL import Image
        with Image.open(dest) as im:
            self.assertEqual(im.size, (600, 900))
            self.assertEqual(im.getpixel((300, 450))[0] > 100, True)   # unser Cover (violett), nicht grün
        with Image.open(card) as im:
            self.assertEqual(im.size, (600, 900))                      # Titelkarte – GOverlay sucht nicht bei Steam/Bing
        self.assertFalse(card.with_name(card.name + ".fallback").exists())
        self.assertEqual(gp.sync_all(self.backend).covers, 0)          # aktuell → nichts geschrieben
        manifest = gp.read_manifest()
        self.assertEqual(set(manifest), {gp.goverlay_sanitize(GAME), "Ohne Cover"})

    def test_user_cover_kept_fallback_replaced(self):
        gp.sync_all(self.backend)
        dest = gp.cover_path(GAME)
        mine = jpeg(color=(9, 9, 9))
        dest.write_bytes(mine)                                         # in GOverlay selbst gesetzt
        self.assertEqual(gp.sync_all(self.backend).covers, 0)
        self.assertEqual(dest.read_bytes(), mine)
        dest.write_bytes(jpeg((150, 215)))                             # GOverlays Platzhalter
        dest.with_name(dest.name + ".fallback").write_text("")
        self.assertEqual(gp.sync_all(self.backend).covers, 1)
        self.assertFalse(dest.with_name(dest.name + ".fallback").exists())
        self.assertNotEqual(gp.jpeg_size(dest.read_bytes()), (150, 215))

    def test_custom_cover_and_stale_cleanup(self):
        gp.sync_all(self.backend)
        custom = self.root / "eigen.png"
        from PIL import Image
        Image.new("RGB", (342, 482), (200, 10, 10)).save(custom)
        self.gs.update("ext-x", custom_cover=str(custom))
        self.assertEqual(gp.sync_all(self.backend).covers, 1)          # unsere Titelkarte → eigenes Cover
        with Image.open(gp.cover_path("Ohne Cover")) as im:
            self.assertGreater(im.getpixel((10, 10))[0], 150)
        foreign = gp.cover_path("Fremd")
        foreign.write_bytes(jpeg())
        self.gs.update("ext-x", hidden=True)                           # Karte verschwindet
        gp.sync_all(self.backend)
        self.assertFalse(gp.cover_path("Ohne Cover").exists())         # eigenes Cover der Karte weg
        self.assertTrue(foreign.exists())                              # fremdes bleibt
        self.assertNotIn("Ohne Cover", gp.read_manifest())

    def test_without_pillow(self):
        jpg = self.root / "c.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0echtes-jpeg")
        self.gs.update("1", custom_cover=str(jpg))
        with mock.patch.dict(sys.modules, {"PIL": None}):
            self.assertIsNone(gp.title_card("x"))
            rep = gp.sync_all(self.backend)
        self.assertEqual(rep.covers, 1)                                # JPEG kopiert, Titelkarte übersprungen
        self.assertEqual(gp.cover_path(GAME).read_bytes(), jpg.read_bytes())
        self.assertFalse(gp.cover_path("Ohne Cover").exists())


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

    def test_lookup_order_card_name_then_folder(self):
        """Profil aus den Eigenschaften → Kartenname (Titel) → Spielordner (Profile aus 0.3.0)."""
        game = InstalledGame("1", "LEGO® City Undercover", "windows", str(self.game_dir))
        gs = GameSettings()
        self.assertEqual(gp.auto_profile_names(game, gs), ["LEGO City Undercover", PROFILE])
        names = gp.auto_profile_names(game, gs)
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings, names).name, "LEGO City Undercover")
        self.profile()                                               # nur das alte Ordner-Profil (0.3.0)
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings, names).name, PROFILE)
        self.profile("LEGO City Undercover")                         # Profil der neuen Karte geht vor
        self.assertEqual(gp.resolve_profile(str(self.game_dir), "", self.settings, names).name, "LEGO City Undercover")
        gs.custom_title = "Eigener Name"
        self.assertEqual(gp.auto_profile_names(game, gs)[0], "Eigener Name")
        self.assertEqual(gp.resolve_profile(str(self.game_dir), PROFILE, self.settings, names).name, PROFILE)   # ausdrücklich

    def test_global_profile_only_on_request(self):
        """Das globale Profil gilt nie von selbst – auch nicht, wenn kein Spielprofil existiert."""
        self.profile(gp.GLOBAL_PROFILE)
        game = InstalledGame("1", GAME, "windows", str(self.game_dir))
        names = gp.auto_profile_names(game)
        self.assertFalse(self.settings.goverlay_global_fallback)
        prof = gp.resolve_profile(str(self.game_dir), "", self.settings, names)
        self.assertEqual(prof.kind, "none")
        self.assertNotEqual(prof.name, gp.GLOBAL_PROFILE)

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
        backend = types.SimpleNamespace(settings=self.settings, registry=InstalledRegistry(), game_settings=GameSettingsStore(),
                                        _covers={}, _meta={})
        backend.registry.set(InstalledGame("1", GAME, "windows", str(self.game_dir)))
        self.assertIsNone(gp.status_for(backend, "1"))               # GOverlay nicht installiert
        self.install_goverlay()
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('kein Profil – das Spiel erscheint nicht in GOverlay (Einstellungen → Kompatibilität → GOverlay)'))
        gp.sync_all(backend, covers=False)
        self.assertEqual(gp.status_for(backend, "1"), gp.tr('kein Profil – Spiel in GOverlay anklicken, um sein Profil anzulegen'))
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
