"""Umzug von Milkyway (0.5.x) und galaxy-linux (bis 0.4.1) zu local:cluster: Ordner, Protokolle, Verknüpfungen,
Schlüsselbund, Prefix-Zustand."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_cluster import APP_ID, migrate, paths
from local_cluster.api import auth
from local_cluster.content import redist

MILKYWAY_ENTRY = """[Desktop Entry]
Type=Application
Version=1.5
Name=Lego City Undercover
Comment=Über Milkyway starten (inoffizieller GOG-Client)
Exec=/usr/bin/milkyway milkyway://launch/1986080547
Icon={data}/icons/1986080547.png
Terminal=false
Categories=Game;
StartupNotify=true
StartupWMClass=io.github.cue_system90.Milkyway
X-GOG-ProductId=1986080547
Actions=Page;

[Desktop Action Page]
Name=Open game page in the client
Exec=/usr/bin/milkyway milkyway://game/1986080547
"""
GALAXY_ENTRY = (MILKYWAY_ENTRY.replace("milkyway", "galaxy-linux").replace("Milkyway", "galaxy-linux")
                .replace("io.github.cue_system90.galaxy-linux", "org.galaxylinux.Client"))


class DirTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.new = self.root / "local-cluster"
        self.mw, self.gl = self.root / "milkyway", self.root / "galaxy-linux"

    def tearDown(self):
        self.td.cleanup()

    def test_from_milkyway_with_galaxy_link(self):
        """Stand nach 0.5.0: echter Ordner milkyway, Verweis galaxy-linux → milkyway."""
        (self.mw / "logs").mkdir(parents=True)
        (self.mw / "settings.json").write_text("{}")
        self.gl.symlink_to("milkyway")
        notes = migrate.migrate_dir(self.new)
        self.assertEqual(len(notes), 2)
        self.assertTrue((self.new / "settings.json").is_file() and not self.new.is_symlink())
        self.assertEqual(os.readlink(self.mw), "local-cluster")
        self.assertEqual(os.readlink(self.gl), "local-cluster")           # direkt, nicht über milkyway
        self.assertTrue((self.gl / "settings.json").is_file())           # alte Pfade gelten weiter
        self.assertEqual(migrate.migrate_dir(self.new), [])               # zweiter Start: nichts zu tun

    def test_from_galaxy_linux(self):
        """Sprung von 0.4.x: nur ein echter Ordner galaxy-linux."""
        self.gl.mkdir()
        (self.gl / "a").write_text("alt")
        migrate.migrate_dir(self.new)
        self.assertEqual((self.new / "a").read_text(), "alt")
        self.assertEqual(os.readlink(self.gl), "local-cluster")
        self.assertFalse(self.mw.exists() or self.mw.is_symlink())

    def test_existing_new_dir_is_kept(self):
        self.mw.mkdir()
        (self.mw / "a").write_text("alt")
        self.new.mkdir()
        self.assertIn("bleibt", migrate.move_dir(self.mw, self.new))
        self.assertEqual((self.mw / "a").read_text(), "alt")
        self.assertFalse(self.mw.is_symlink())

    def test_old_dir_was_a_link_elsewhere(self):
        target = self.root / "andere-platte" / "gl"
        target.mkdir(parents=True)
        self.gl.symlink_to(target)
        self.mw.symlink_to(target)                                      # so hat 0.5.0 es angelegt
        migrate.migrate_dir(self.new)
        self.assertEqual(Path(os.path.realpath(self.new)), target)
        self.assertEqual(Path(os.readlink(self.gl)), target)            # fremde Verweise bleiben, wie sie sind

    def test_nothing_to_do(self):
        self.assertEqual(migrate.migrate_dir(self.new), [])

    def test_logs(self):
        for n in ("milkyway.log", "galaxy-linux.log.1", "galaxy-linux.log", "1207658924.log"):
            (self.root / n).write_text(n)
        migrate.rename_logs(self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["1207658924.log", "galaxy-linux.log", "local-cluster.log", "local-cluster.log.1"])
        self.assertEqual((self.root / "local-cluster.log").read_text(), "milkyway.log")     # neuester Name zuerst
        self.assertEqual((self.root / "local-cluster.log.1").read_text(), "galaxy-linux.log.1")

    def test_run_early(self):
        cfg, data, cache = (self.root / x / "local-cluster" for x in ("config", "data", "cache"))
        for d in (cfg, data):
            d.parent.mkdir(parents=True)
        (data.with_name("milkyway") / "logs").mkdir(parents=True)
        (data.with_name("milkyway") / "logs" / "milkyway.log").write_text("x")
        data.with_name("galaxy-linux").symlink_to("milkyway")
        cfg.with_name("galaxy-linux").mkdir()                            # Konfiguration noch aus 0.4.x
        with mock.patch.multiple(paths, CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs"):
            notes = migrate.run_early()
            self.assertEqual(len(notes), 4)       # Konfiguration, Daten, Verweis galaxy-linux, Protokoll; kein Cache
            self.assertTrue((data / "logs" / "local-cluster.log").is_file())
            self.assertTrue(cfg.is_dir())
            self.assertFalse(cache.exists())
            self.assertEqual(migrate.run_early(), [])


class EntryTest(unittest.TestCase):
    OLD_DATA = ["/home/u/.local/share/milkyway", "/home/u/.local/share/galaxy-linux"]
    NEW_DATA = "/home/u/.local/share/local-cluster"

    def check(self, text):
        self.assertIn("Exec=/usr/bin/local-cluster local-cluster://launch/1986080547\n", text)
        self.assertIn("Exec=/usr/bin/local-cluster local-cluster://game/1986080547\n", text)
        self.assertIn(f"Icon={self.NEW_DATA}/icons/1986080547.png\n", text)
        self.assertIn(f"StartupWMClass={APP_ID}\n", text)
        self.assertIn("Comment=Über local:cluster starten (inoffizieller GOG-Client)\n", text)
        self.assertIn("Name=Lego City Undercover\n", text)
        self.assertIn("Name=Open game page in the client\n", text)   # übersetzte Aktion bleibt
        for old in ("milkyway", "Milkyway", "galaxy-linux", "galaxylinux"):
            self.assertNotIn(old, text)

    def test_rewrite_milkyway_and_galaxy_shortcuts(self):
        for entry, data in ((MILKYWAY_ENTRY, self.OLD_DATA[0]), (GALAXY_ENTRY, self.OLD_DATA[1])):
            self.check(migrate.rewrite_entry(entry.format(data=data), "/usr/bin/local-cluster", self.OLD_DATA,
                                             self.NEW_DATA))

    def test_rewrite_python_command_autostart_and_titles(self):
        line = 'Exec=env "PYTHONPATH=/src" /usr/bin/python3 -m milkyway milkyway://launch/1'
        self.assertEqual(migrate.rewrite_entry(line, "local-cluster", [], "").strip(),
                         "Exec=local-cluster local-cluster://launch/1")
        for name, app_id in (("Milkyway", "io.github.cue_system90.Milkyway"), ("galaxy-linux", "org.galaxylinux.Client")):
            auto = f"[Desktop Entry]\nName={name}\nExec=/usr/bin/x --gui --minimized\nIcon={app_id}\n"
            out = migrate.rewrite_entry(auto, "/usr/bin/local-cluster", [], "")
            self.assertIn("Name=local:cluster\n", out)
            self.assertIn("Exec=/usr/bin/local-cluster --gui --minimized\n", out)
            self.assertIn(f"Icon={APP_ID}\n", out)
        title = migrate.rewrite_entry("Name=Milkyway Odyssey\n", "x", [], "")
        self.assertEqual(title, "Name=Milkyway Odyssey\n")                 # Spieltitel bleiben

    def test_migrate_shortcuts_and_autostart(self):
        with tempfile.TemporaryDirectory() as d:
            apps, desk, auto = Path(d) / "apps", Path(d) / "Schreibtisch", Path(d) / "autostart"
            for x in (apps, desk, auto):
                x.mkdir()
            f = apps / "milkyway-42.desktop"
            f.write_text(MILKYWAY_ENTRY.replace("1986080547", "42").format(data="/d/milkyway"))
            f = desk / "galaxy-linux-43.desktop"
            f.write_text(GALAXY_ENTRY.replace("1986080547", "43").format(data="/d/galaxy-linux"))
            f.chmod(0o755)
            (apps / "milkyway-7.desktop").write_text("alt")             # neue Datei gibt es schon: alte fällt weg
            (apps / "local-cluster-7.desktop").write_text("neu")
            (apps / "andere.desktop").write_text("fremd")
            moved = migrate.migrate_shortcuts([apps, desk], "/usr/bin/local-cluster", ["/d/milkyway", "/d/galaxy-linux"],
                                              "/d/local-cluster")
            self.assertEqual(moved, [apps / "local-cluster-42.desktop", desk / "local-cluster-43.desktop"])
            self.assertEqual(sorted(p.name for p in apps.iterdir()),
                             ["andere.desktop", "local-cluster-42.desktop", "local-cluster-7.desktop"])
            self.assertEqual((apps / "local-cluster-7.desktop").read_text(), "neu")
            self.assertEqual((desk / "local-cluster-43.desktop").stat().st_mode & 0o777, 0o755)
            self.assertIn("Icon=/d/local-cluster/icons/43.png", (desk / "local-cluster-43.desktop").read_text())
            self.assertIn("Icon=/d/local-cluster/icons/42.png", (apps / "local-cluster-42.desktop").read_text())
            (auto / "io.github.cue_system90.Milkyway.desktop").write_text(
                "[Desktop Entry]\nExec=/usr/bin/milkyway --gui --minimized\n")
            new = migrate.migrate_autostart(auto, "/usr/bin/local-cluster --gui --minimized")
            self.assertEqual(new, auto / f"{APP_ID}.desktop")
            self.assertIn("Exec=/usr/bin/local-cluster --gui --minimized", new.read_text())
            self.assertEqual([p.name for p in auto.iterdir()], [f"{APP_ID}.desktop"])


class LegacyNamesTest(unittest.TestCase):
    def test_env_fallback(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("LOCAL_CLUSTER_LANG", "MILKYWAY_LANG", "GALAXY_LINUX_LANG")}
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["GALAXY_LINUX_LANG"] = "en"
            self.assertEqual(paths.env("LANG"), "en")
            os.environ["MILKYWAY_LANG"] = "de"
            self.assertEqual(paths.env("LANG"), "de")
            os.environ["LOCAL_CLUSTER_LANG"] = "en"
            self.assertEqual(paths.env("LANG"), "en")
        self.assertEqual(paths.env("GIBTSNICHT"), "")

    def test_prefix_setup_state_renamed(self):
        for old in ("milkyway-setup.json", "galaxy-linux-setup.json"):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / old).write_text('{"steps": []}')
                p = redist.setup_state_path(d)
                self.assertEqual(p.name, "local-cluster-setup.json")
                self.assertTrue(p.is_file())
                self.assertFalse((Path(d) / old).exists())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "milkyway-setup.json").write_text("neuer")
            (Path(d) / "galaxy-linux-setup.json").write_text("älter")
            self.assertEqual(redist.setup_state_path(d).read_text(), "neuer")
            self.assertEqual(redist.setup_state_path(Path(d) / "neu").name, "local-cluster-setup.json")


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


CREDS = json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 1})


class KeyringTest(unittest.TestCase):
    def store(self, fake):
        s = auth.CredentialStore(use_keyring=False)
        s.use_keyring, s._Secret, s._schema = True, fake, "neu"
        s._legacy_schemas = [("Milkyway", "mw"), ("galaxy-linux", "gl")]
        return s

    def test_legacy_entries_are_taken_over(self):
        for old in ("mw", "gl"):
            fake = FakeSecret()
            fake.items[old] = CREDS
            creds = self.store(fake).load()
            self.assertEqual((creds.access_token, creds.refresh_token), ("a", "r"))
            self.assertEqual(list(fake.items), ["neu"])              # übernommen, alter Eintrag gelöscht
            self.assertIsNotNone(self.store(fake).load())

    def test_legacy_entry_kept_when_store_fails(self):
        fake = FakeSecret()
        fake.items["mw"] = CREDS
        fake.password_store_sync = lambda *a: False
        self.assertIsNotNone(self.store(fake).load())                # Anmeldung gilt trotzdem
        self.assertEqual(list(fake.items), ["mw"])                   # alter Eintrag bleibt

    def test_clear_removes_all(self):
        fake = FakeSecret()
        fake.items.update(neu="{}", mw="{}", gl="{}")
        self.store(fake).clear()
        self.assertEqual(fake.items, {})

    def test_schema_names(self):
        self.assertEqual(auth.CredentialStore.SCHEMA, APP_ID)
        self.assertEqual(auth.CredentialStore.LEGACY_SCHEMAS, migrate.LEGACY_APP_IDS)


if __name__ == "__main__":
    unittest.main()
