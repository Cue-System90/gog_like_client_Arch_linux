"""Installation verschieben – Umbenennen auf demselben Dateisystem und Kopieren mit Fortschritt."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.content.installer import InstallError, InstallManager
from galaxy_linux.settings import GlobalSettings, InstalledGame, InstalledRegistry


class MoveTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.patch = mock.patch.multiple("galaxy_linux.paths", INSTALLED_FILE=root / "installed.json", MANIFEST_DIR=root / "manifests")
        self.patch.start()
        self.src = root / "games" / "Demo Game"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "game.exe").write_bytes(os.urandom(3_000_000))
        (self.src / "sub" / "data.bin").write_bytes(b"x" * 10_000)
        os.symlink("game.exe", self.src / "link")
        self.registry = InstalledRegistry()
        self.registry.set(InstalledGame("1", "Demo Game", "windows", str(self.src)))
        self.mgr = InstallManager(api=mock.Mock(), settings=GlobalSettings(install_dir=str(root / "games")), registry=self.registry)

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_move_same_filesystem(self):
        new_root = Path(self.td.name) / "other"
        game = self.mgr.move("1", new_root)
        self.assertEqual(game.install_path, str(new_root / "Demo Game"))
        self.assertFalse(self.src.exists())
        self.assertTrue((new_root / "Demo Game" / "sub" / "data.bin").exists())
        self.assertEqual(self.registry.get("1").install_path, str(new_root / "Demo Game"))

    def test_copy_tree_with_progress_and_symlink(self):
        dst = Path(self.td.name) / "copy" / "Demo Game"
        seen = []
        InstallManager._copy_tree(self.src, dst, lambda p: seen.append((p.done_bytes, p.files_done)))
        self.assertEqual((dst / "game.exe").stat().st_size, 3_000_000)
        self.assertEqual(os.readlink(dst / "link"), "game.exe")
        self.assertEqual(seen[-1][1], 3)
        self.assertEqual(seen[-1][0], 3_010_000)

    def test_copy_fallback_when_rename_fails(self):
        new_root = Path(self.td.name) / "xdev"
        real_rename = os.rename

        def fake_rename(a, b):
            raise OSError(18, "Invalid cross-device link")
        with mock.patch("galaxy_linux.content.installer.os.rename", side_effect=fake_rename):
            game = self.mgr.move("1", new_root)
        self.assertTrue((new_root / "Demo Game" / "game.exe").exists())
        self.assertFalse(self.src.exists())
        self.assertEqual(game.install_path, str(new_root / "Demo Game"))

    def test_move_rejects_same_and_nested(self):
        with self.assertRaises(InstallError):
            self.mgr.move("1", self.src.parent)
        with self.assertRaises(InstallError):
            self.mgr.move("1", self.src / "inside")

    def test_cancel_cleans_up(self):
        dst = Path(self.td.name) / "cancel" / "Demo Game"
        ev = threading.Event()
        ev.set()
        from galaxy_linux.content.downloader import DownloadCancelled
        with self.assertRaises(DownloadCancelled):
            InstallManager._copy_tree(self.src, dst, None, ev)
        self.assertFalse(dst.exists())


if __name__ == "__main__":
    unittest.main()
