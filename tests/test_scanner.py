"""Erkennung kopierter Spiele in den Spiele-Ordnern und automatische Prefix-Anlage."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.content import scanner


def make_game(d: Path, pid: str, name: str, *, root: str | None = None, exe: str = "game.exe", build: str = "", dlcs=(), old=True):
    d.mkdir(parents=True, exist_ok=True)
    info = {"gameId": pid, "rootGameId": root or pid, "name": name, "languages": ["de-DE"],
            "playTasks": [{"category": "game", "isPrimary": True, "name": name, "path": exe, "type": "FileTask"}]}
    if build:
        info["buildId"] = build
    (d / f"goggame-{pid}.info").write_text(json.dumps(info))
    (d / exe).write_bytes(b"MZ" + b"\0" * 2000)
    for dl in dlcs:
        (d / f"goggame-{dl}.info").write_text(json.dumps({"gameId": dl, "rootGameId": pid, "name": f"{name} DLC"}))
    if old:
        past = time.time() - 600
        for p in [d, *d.rglob("*")]:
            os.utime(p, (past, past))
    return d


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "Games"
        self.root.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_detects_games_dlcs_nested_linux_and_unknown(self):
        make_game(self.root / "The Witcher", "100", "The Witcher", build="555", dlcs=("101", "102"))
        make_game(self.root / "GOG Games" / "Stardew Valley", "200", "Stardew Valley")
        lin = self.root / "Linux Game"
        make_game(lin / "game", "300", "Linux Game")
        (lin / "start.sh").write_text("#!/bin/sh\n")
        (self.root / "Irgendwas").mkdir()
        (self.root / "Irgendwas" / "tool.exe").write_bytes(b"MZ")
        (self.root / "prefixes" / "100").mkdir(parents=True)
        res = scanner.scan([self.root], registered={}, exclude=[self.root / "prefixes"])
        found = {g.product_id: g for g in res.found}
        self.assertEqual(set(found), {"100", "200", "300"})
        self.assertEqual(found["100"].dlc_ids, ["101", "102"])
        self.assertEqual(found["100"].build_id, "555")
        self.assertEqual(found["100"].language, "de-DE")
        self.assertEqual(found["100"].platform, "windows")
        self.assertEqual(found["300"].platform, "linux")
        self.assertEqual(found["300"].executable, "start.sh")
        self.assertEqual([p.name for p in res.unknown], ["Irgendwas"])

    def test_known_duplicates_and_missing(self):
        a = make_game(self.root / "A", "1", "A")
        b = make_game(self.root / "B copy", "1", "A")
        res = scanner.scan([self.root], registered={"1": a, "9": self.root / "Gelöscht"})
        self.assertEqual(res.known, ["1"])
        self.assertEqual(res.duplicates, [b])
        self.assertEqual(res.missing, ["9"])
        self.assertEqual(res.found, [])

    def test_missing_not_reported_when_root_empty(self):
        # z. B. Laufwerk nicht eingehängt: leerer Basisordner → nichts austragen
        res = scanner.scan([self.root], registered={"9": self.root / "Spiel"})
        self.assertEqual(res.missing, [])

    def test_copy_in_progress_is_not_stable(self):
        done = make_game(self.root / "Fertig", "1", "Fertig")
        busy = make_game(self.root / "Kopiert", "2", "Kopiert", old=False)
        res = scanner.scan([self.root], registered={})
        stable, copying = scanner.split_stable(res.found, wait=0.1, quiet_for=8)
        self.assertEqual([g.path for g, _ in stable], [done])
        self.assertEqual([g.path for g in copying], [busy])
        self.assertGreater(stable[0][1], 2000)


class BackendScanTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        self.games = base / "Games"
        self.prefixes = base / "Prefixes"
        self.games.mkdir()
        cfg, data, cache = base / "cfg", base / "data", base / "cache"
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        from galaxy_linux.backend import Backend
        self.b = Backend()
        self.b.settings.install_dir = str(self.games)
        self.b.settings.prefix_dir = str(self.prefixes)
        self.b.settings.auto_create_prefixes = True

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_scan_imports_counts_and_queues_prefixes(self):
        make_game(self.games / "The Witcher", "100", "The Witcher", dlcs=("101",))
        make_game(self.games / "Cyberpunk 2077", "200", "Cyberpunk 2077")
        added = []
        with mock.patch.object(self.b.jobs, "add", side_effect=lambda j: added.append(j) or j):
            rep = self.b.scan_install_dirs(wait=0.05)
        self.assertEqual(sorted(g.product_id for g in rep.imported), ["100", "200"])
        self.assertEqual(len(self.b.registry.all()), 2)
        self.assertEqual(self.b.registry.get("100").method, "imported")
        self.assertEqual(self.b.registry.get("100").installed_dlcs, ["101"])
        self.assertEqual(sorted(j.product_id for j in added), ["100", "200"])
        self.assertTrue(all(j.kind == "prefix" for j in added))
        # zweiter Lauf: nichts Neues, Prefixe bereits eingereiht bzw. angelegt
        for pid in ("100", "200"):
            (self.prefixes / pid).mkdir(parents=True)
            (self.prefixes / pid / "system.reg").write_text("")
        with mock.patch.object(self.b.jobs, "add", side_effect=lambda j: added.append(j) or j):
            rep2 = self.b.scan_install_dirs(wait=0.05)
        self.assertEqual(rep2.imported, [])
        self.assertEqual(rep2.prefix_jobs, 0)

    def test_deleted_folder_is_removed_prefix_kept(self):
        g = make_game(self.games / "Weg", "5", "Weg")
        make_game(self.games / "Bleibt", "6", "Bleibt")
        with mock.patch.object(self.b.jobs, "add", side_effect=lambda j: j):
            self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        (self.prefixes / "5").mkdir(parents=True)
        import shutil
        shutil.rmtree(g)
        rep = self.b.scan_install_dirs(wait=0.05, create_prefixes=False)
        self.assertEqual(rep.removed, ["Weg"])
        self.assertIsNone(self.b.registry.get("5"))
        self.assertTrue((self.prefixes / "5").is_dir())

    def test_create_game_prefix_sets_dpi(self):
        make_game(self.games / "X", "7", "X")
        with mock.patch.object(self.b.jobs, "add", side_effect=lambda j: j):
            self.b.scan_install_dirs(wait=0.05, create_prefixes=False)

        def fake_create(pid):
            p = self.prefixes / pid
            p.mkdir(parents=True, exist_ok=True)
            (p / "system.reg").write_text("")
            return 0
        calls = []
        with mock.patch.object(self.b.launcher, "create_prefix", side_effect=fake_create), \
             mock.patch.object(self.b.launcher, "run_in_prefix", side_effect=lambda pid, argv, **kw: calls.append(argv) or 0), \
             mock.patch("galaxy_linux.launch.display.desktop_dpi", return_value=216):
            self.b.create_game_prefix("7")
        self.assertTrue(self.b.launcher.prefix_exists("7"))
        self.assertEqual(calls[0][:5], ["reg", "add", r"HKCU\Control Panel\Desktop", "/v", "LogPixels"])
        self.assertIn("216", calls[0])


if __name__ == "__main__":
    unittest.main()
