"""Umzug von galaxy-linux (bis 0.4.1) zu Milkyway: Ordner, Protokolle, Verknüpfungen, Schlüsselbund, Prefix-Zustand."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from milkyway import APP_ID, migrate, paths
from milkyway.api import auth
from milkyway.content import redist

OLD_ENTRY = """[Desktop Entry]
Type=Application
Version=1.5
Name=Lego City Undercover
Comment=Über galaxy-linux starten (inoffizieller GOG-Client)
Exec=/usr/bin/galaxy-linux galaxy-linux://launch/1986080547
Icon={data}/icons/1986080547.png
Terminal=false
Categories=Game;
StartupNotify=true
StartupWMClass=org.galaxylinux.Client
X-GOG-ProductId=1986080547
Actions=Page;

[Desktop Action Page]
Name=Open game page in the client
Exec=/usr/bin/galaxy-linux galaxy-linux://game/1986080547
"""


class DirTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_move_leaves_relative_link(self):
        old, new = self.root / "galaxy-linux", self.root / "milkyway"
        (old / "logs").mkdir(parents=True)
        (old / "settings.json").write_text("{}")
        self.assertIn("→", migrate.move_dir(old, new))
        self.assertTrue((new / "settings.json").is_file())
        self.assertTrue(old.is_symlink())
        self.assertEqual(os.readlink(old), "milkyway")
        self.assertTrue((old / "settings.json").is_file())          # alte Pfade gelten weiter
        self.assertIsNone(migrate.move_dir(old, new))                  # zweiter Start: nichts zu tun

    def test_existing_new_dir_is_kept(self):
        old, new = self.root / "galaxy-linux", self.root / "milkyway"
        old.mkdir()
        (old / "a").write_text("alt")
        new.mkdir()
        self.assertIn("bleibt", migrate.move_dir(old, new))
        self.assertEqual((old / "a").read_text(), "alt")
        self.assertFalse(old.is_symlink())

    def test_old_dir_was_a_link_elsewhere(self):
        target = self.root / "andere-platte" / "gl"
        target.mkdir(parents=True)
        old, new = self.root / "galaxy-linux", self.root / "milkyway"
        old.symlink_to(target)
        migrate.move_dir(old, new)
        self.assertEqual(Path(os.path.realpath(new)), target)
        self.assertTrue(old.is_symlink())

    def test_nothing_to_do(self):
        self.assertIsNone(migrate.move_dir(self.root / "galaxy-linux", self.root / "milkyway"))

    def test_logs(self):
        for n in ("galaxy-linux.log", "galaxy-linux.log.1", "1207658924.log"):
            (self.root / n).write_text(n)
        migrate.rename_logs(self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["1207658924.log", "milkyway.log", "milkyway.log.1"])
        self.assertEqual((self.root / "milkyway.log.1").read_text(), "galaxy-linux.log.1")

    def test_run_early(self):
        cfg, data, cache = (self.root / x / "milkyway" for x in ("config", "data", "cache"))
        for d in (cfg, data):
            legacy = migrate.legacy_dir(d)
            legacy.mkdir(parents=True)
        (migrate.legacy_dir(data) / "logs").mkdir()
        (migrate.legacy_dir(data) / "logs" / "galaxy-linux.log").write_text("x")
        with mock.patch.multiple(paths, CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs"):
            notes = migrate.run_early()
            self.assertEqual(len(notes), 3)                            # zwei Ordner, ein Protokoll; kein Cache
            self.assertTrue((data / "logs" / "milkyway.log").is_file())
            self.assertFalse(cache.exists())
            self.assertEqual(migrate.run_early(), [])


class EntryTest(unittest.TestCase):
    def test_rewrite_game_shortcut(self):
        old_data, new_data = "/home/u/.local/share/galaxy-linux", "/home/u/.local/share/milkyway"
        text = migrate.rewrite_entry(OLD_ENTRY.format(data=old_data), "/usr/bin/milkyway", old_data, new_data)
        self.assertIn("Exec=/usr/bin/milkyway milkyway://launch/1986080547\n", text)
        self.assertIn("Exec=/usr/bin/milkyway milkyway://game/1986080547\n", text)
        self.assertIn(f"Icon={new_data}/icons/1986080547.png\n", text)
        self.assertIn(f"StartupWMClass={APP_ID}\n", text)
        self.assertIn("Comment=Über Milkyway starten (inoffizieller GOG-Client)\n", text)
        self.assertIn("Name=Lego City Undercover\n", text)
        self.assertIn("Name=Open game page in the client\n", text)   # übersetzte Aktion bleibt
        self.assertNotIn("galaxy-linux", text)
        self.assertNotIn("galaxylinux", text)

    def test_rewrite_python_command_and_autostart(self):
        line = 'Exec=env "PYTHONPATH=/src" /usr/bin/python3 -m galaxy_linux galaxy-linux://launch/1'
        self.assertEqual(migrate.rewrite_entry(line, "milkyway", "", "").strip(), "Exec=milkyway milkyway://launch/1")
        auto = "[Desktop Entry]\nName=galaxy-linux\nExec=/usr/bin/galaxy-linux --gui --minimized\nIcon=org.galaxylinux.Client\n"
        out = migrate.rewrite_entry(auto, "/usr/bin/milkyway", "", "")
        self.assertIn("Name=Milkyway\n", out)
        self.assertIn("Exec=/usr/bin/milkyway --gui --minimized\n", out)
        self.assertIn(f"Icon={APP_ID}\n", out)

    def test_migrate_shortcuts_and_autostart(self):
        with tempfile.TemporaryDirectory() as d:
            apps, desk, auto = Path(d) / "apps", Path(d) / "Schreibtisch", Path(d) / "autostart"
            for x in (apps, desk, auto):
                x.mkdir()
            for x in (apps, desk):
                f = x / "galaxy-linux-42.desktop"
                f.write_text(OLD_ENTRY.replace("1986080547", "42").format(data="/d/galaxy-linux"))
                f.chmod(0o755)
            (apps / "galaxy-linux-7.desktop").write_text("alt")       # neue Datei gibt es schon: alte fällt weg
            (apps / "milkyway-7.desktop").write_text("neu")
            (apps / "andere.desktop").write_text("fremd")
            moved = migrate.migrate_shortcuts([apps, desk], "/usr/bin/milkyway", "/d/galaxy-linux", "/d/milkyway")
            self.assertEqual(moved, [apps / "milkyway-42.desktop", desk / "milkyway-42.desktop"])
            self.assertEqual(sorted(p.name for p in apps.iterdir()), ["andere.desktop", "milkyway-42.desktop",
                                                                      "milkyway-7.desktop"])
            self.assertEqual((apps / "milkyway-7.desktop").read_text(), "neu")
            self.assertEqual((desk / "milkyway-42.desktop").stat().st_mode & 0o777, 0o755)
            self.assertIn("Icon=/d/milkyway/icons/42.png", (desk / "milkyway-42.desktop").read_text())
            (auto / "org.galaxylinux.Client.desktop").write_text(
                "[Desktop Entry]\nExec=/usr/bin/galaxy-linux --gui --minimized\n")
            new = migrate.migrate_autostart(auto, "/usr/bin/milkyway --gui --minimized")
            self.assertEqual(new, auto / f"{APP_ID}.desktop")
            self.assertIn("Exec=/usr/bin/milkyway --gui --minimized", new.read_text())
            self.assertFalse((auto / "org.galaxylinux.Client.desktop").exists())


class LegacyNamesTest(unittest.TestCase):
    def test_env_fallback(self):
        with mock.patch.dict(os.environ, {"GALAXY_LINUX_LANG": "en"}, clear=False):
            os.environ.pop("MILKYWAY_LANG", None)
            self.assertEqual(paths.env("LANG"), "en")
            with mock.patch.dict(os.environ, {"MILKYWAY_LANG": "de"}):
                self.assertEqual(paths.env("LANG"), "de")
        self.assertEqual(paths.env("GIBTSNICHT"), "")

    def test_prefix_setup_state_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "galaxy-linux-setup.json").write_text('{"steps": []}')
            p = redist.setup_state_path(d)
            self.assertEqual(p.name, "milkyway-setup.json")
            self.assertTrue(p.is_file())
            self.assertFalse((Path(d) / "galaxy-linux-setup.json").exists())
            self.assertEqual(redist.setup_state_path(Path(d) / "neu").name, "milkyway-setup.json")


class FakeSecret:
    COLLECTION_DEFAULT = "default"

    def __init__(self):
        self.items: dict[str, str] = {}

    def password_lookup_sync(self, schema, attrs, cancel):
        return self.items.get(schema)

    def password_store_sync(self, schema, attrs, collection, label, secret, cancel):
        self.items[schema] = secret
        return True

    def password_clear_sync(self, schema, attrs, cancel):
        return self.items.pop(schema, None) is not None


class KeyringTest(unittest.TestCase):
    def store(self, fake):
        s = auth.CredentialStore(use_keyring=False)
        s.use_keyring, s._Secret, s._schema, s._legacy_schema = True, fake, "neu", "alt"
        return s

    def test_legacy_entry_is_taken_over(self):
        fake = FakeSecret()
        fake.items["alt"] = json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 1})
        creds = self.store(fake).load()
        self.assertEqual((creds.access_token, creds.refresh_token), ("a", "r"))
        self.assertEqual(list(fake.items), ["neu"])                  # übernommen, alter Eintrag gelöscht
        self.assertIsNotNone(self.store(fake).load())

    def test_legacy_entry_kept_when_store_fails(self):
        fake = FakeSecret()
        fake.items["alt"] = json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 1})
        fake.password_store_sync = lambda *a: False
        self.assertIsNotNone(self.store(fake).load())                # Anmeldung gilt trotzdem
        self.assertEqual(list(fake.items), ["alt"])                  # alter Eintrag bleibt

    def test_clear_removes_both(self):
        fake = FakeSecret()
        fake.items.update(neu="{}", alt="{}")
        self.store(fake).clear()
        self.assertEqual(fake.items, {})

    def test_schema_names(self):
        self.assertEqual(auth.CredentialStore.SCHEMA, APP_ID)
        self.assertEqual(auth.CredentialStore.LEGACY_SCHEMA, migrate.LEGACY_APP_ID)


if __name__ == "__main__":
    unittest.main()
