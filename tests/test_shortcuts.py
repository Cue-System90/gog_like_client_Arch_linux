"""Verknüpfungen im Startmenü und auf dem Schreibtisch."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.launch import shortcuts


class ShortcutTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.apps, self.desk, self.icons = root / "apps", root / "Schreibtisch", root / "icons"
        self.patches = [mock.patch.object(shortcuts, "APPS_DIR", self.apps), mock.patch.object(shortcuts, "ICON_DIR", self.icons),
                        mock.patch.object(shortcuts, "desktop_dir", lambda: self.desk), mock.patch.object(shortcuts, "_refresh_menu", lambda: None),
                        mock.patch.object(shortcuts.shutil, "which", lambda n: "/usr/bin/galaxy-linux" if n == "galaxy-linux" else None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.cleanup()

    def test_create_and_remove(self):
        written = shortcuts.create("123", "Mein Spiel: Director's Cut", "/tmp/icon.png", menu=True, desktop=True)
        self.assertEqual(len(written), 2)
        content = (self.apps / "galaxy-linux-123.desktop").read_text()
        self.assertIn("Exec=/usr/bin/galaxy-linux galaxy-linux://launch/123", content)
        self.assertIn("Name=Mein Spiel: Director's Cut", content)
        self.assertIn("Icon=/tmp/icon.png", content)
        self.assertIn("Categories=Game;", content)
        self.assertIn("[Desktop Action Page]", content)
        self.assertTrue(os.access(self.desk / "galaxy-linux-123.desktop", os.X_OK))
        self.assertTrue(shortcuts.has_menu_shortcut("123") and shortcuts.has_desktop_shortcut("123"))
        removed = shortcuts.remove("123", menu=True, desktop=False)
        self.assertEqual(len(removed), 1)
        self.assertFalse(shortcuts.has_menu_shortcut("123"))
        self.assertTrue(shortcuts.has_desktop_shortcut("123"))
        shortcuts.remove("123")
        self.assertFalse(shortcuts.has_desktop_shortcut("123"))

    def test_icon_store_png(self):
        from PIL import Image
        src = Path(self.td.name) / "cover.jpg"
        Image.new("RGB", (400, 400), (120, 40, 200)).save(src)
        out = shortcuts.store_icon("5", src)
        self.assertTrue(out.endswith("5.png"))
        with Image.open(out) as im:
            self.assertLessEqual(max(im.size), 256)

    def test_exec_quoting_is_desktop_spec_compliant(self):
        import shutil as _sh, subprocess
        self.assertEqual(shortcuts.exec_quote("/usr/bin/galaxy-linux"), "/usr/bin/galaxy-linux")
        self.assertEqual(shortcuts.exec_quote('/mnt/spiele ordner/a"b$c'), '"/mnt/spiele ordner/a\\"b\\$c"')
        # Entwicklungsmodus: Pfad mit Leerzeichen → gültige Exec-Zeile
        with mock.patch.object(shortcuts.shutil, "which", lambda n: None), mock.patch.object(shortcuts.Path, "home", lambda: Path(self.td.name)):
            content = shortcuts.desktop_entry("9", "Spiel", None)
        self.assertIn('env "PYTHONPATH=', content)
        if _sh.which("desktop-file-validate"):
            f = Path(self.td.name) / "t.desktop"
            f.write_text(content)
            r = subprocess.run(["desktop-file-validate", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_fallback_icon_when_missing(self):
        content = shortcuts.desktop_entry("7", "Spiel", None)
        self.assertIn("Icon=org.galaxylinux.Client", content)


if __name__ == "__main__":
    unittest.main()
