"""Tests für die Galaxy-Dienste: comet-Suche, Download mit SHA-256-Prüfung, Zugangsdatei, Aufruf und Umgebung,
Port-Erkennung, Platzhalterdienst im Prefix, comet-Prozess (mit einem Fake-comet) und die Launch-Hooks.
Kein Netz: Downloads laufen über eine Fake-Session."""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.api.auth import AuthError, Credentials
from galaxy_linux.launch import galaxy_services as gsvc
from galaxy_linux.launch import hooks
from galaxy_linux.settings import GameSettings, GlobalSettings


_LEVEL = gsvc.log.level


def setUpModule():
    gsvc.log.setLevel(logging.ERROR)            # erwartete Warnungen (Prüfsumme, offline …) nicht ausgeben


def tearDownModule():
    gsvc.log.setLevel(_LEVEL)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def version_script(version: str) -> str:
    return f"#!/bin/sh\necho \"comet {version}\"\n"


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self.data, self.status_code = data, status
        self.headers = {"Content-Length": str(len(data))}
        self.closed = False

    def iter_content(self, size: int):
        for i in range(0, len(self.data), 7):
            yield self.data[i:i + 7]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, files: dict[str, bytes], status: int = 200) -> None:
        self.files, self.status, self.urls = files, status, []

    def get(self, url, stream=False, timeout=None, headers=None):
        self.urls.append(url)
        return FakeResponse(self.files[url.rsplit("/", 1)[-1]], self.status)


class FakeApi:
    def __init__(self, creds: Credentials | None, refresh_error: Exception | None = None, username: str = "spieler") -> None:
        self._lock = threading.RLock()
        self.creds = creds
        self.refresh_error = refresh_error
        self.store = mock.Mock()
        self.username = username
        self.user_id = creds.user_id if creds else ""
        self.logged_in = creds is not None
        self.refreshed = 0

    def _force_refresh(self) -> str:
        if self.refresh_error:
            raise self.refresh_error
        self.refreshed += 1
        self.creds = Credentials(f"acc{self.refreshed}-{'a' * 40}", f"ref{self.refreshed}-{'r' * 40}",
                                 time.time() + 3600, self.creds.user_id)
        return self.creds.access_token


class TempDataBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.patch = mock.patch.multiple("galaxy_linux.paths", DATA_DIR=self.root / "data", LOG_DIR=self.root / "logs",
                                         CONFIG_DIR=self.root / "config")
        self.patch.start()
        self.heroic = self.root / "heroic" / "comet"
        self.heroic_dummy = self.root / "heroic" / "GalaxyCommunication.exe"
        self.hpatch = mock.patch.multiple(gsvc, HEROIC_COMET=self.heroic, HEROIC_DUMMY=self.heroic_dummy, _last_failure=0.0)
        self.hpatch.start()
        gsvc._verified.clear()
        gsvc._version_cache.clear()

    def tearDown(self):
        self.hpatch.stop()
        self.patch.stop()
        self.td.cleanup()


# ----------------------------------------------------------------------------- Werkzeugsuche
class DiscoveryTest(TempDataBase):
    def setUp(self):
        super().setUp()
        self.mine = version_script("0.3.2").encode()
        self.assets = mock.patch.dict(gsvc.ASSETS, {"comet": gsvc.Asset("comet", "comet-x86_64-unknown-linux-gnu", "comet",
                                                                       sha(self.mine), len(self.mine))})
        self.assets.start()

    def tearDown(self):
        self.assets.stop()
        super().tearDown()

    def test_order_settings_path_heroic_download(self):
        own = script(self.root / "own" / "comet", version_script("0.4.0"))
        inpath = script(self.root / "bin" / "comet", version_script("0.3.0"))
        script(self.heroic, version_script("0.2.0"))
        dl = gsvc.download_dir() / "comet"
        dl.parent.mkdir(parents=True)
        dl.write_bytes(self.mine)
        dl.chmod(0o755)
        with mock.patch("shutil.which", return_value=str(inpath)):
            t = gsvc.find_comet(str(own))
            self.assertEqual((t.source, t.path, t.version), ("settings", own, "0.4.0"))
            self.assertFalse(t.outdated)
            t = gsvc.find_comet("")
            self.assertEqual((t.source, t.version), ("path", "0.3.0"))
            self.assertTrue(t.outdated)
            t = gsvc.find_comet(str(self.root / "fehlt" / "comet"))     # unbrauchbarer eigener Pfad → weiter
            self.assertEqual(t.source, "path")
        with mock.patch("shutil.which", return_value=None):
            t = gsvc.find_comet("")
            self.assertEqual((t.source, t.path, t.version), ("heroic", self.heroic, "0.2.0"))
            self.heroic.unlink()
            t = gsvc.find_comet("")
            self.assertEqual((t.source, t.path, t.version), ("download", dl, "0.3.2"))

    def test_download_needs_valid_checksum_and_exec_bit(self):
        dl = gsvc.download_dir() / "comet"
        dl.parent.mkdir(parents=True)
        dl.write_bytes(b"#!/bin/sh\necho manipuliert\n")
        dl.chmod(0o755)
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(gsvc.find_comet(""))
            dl.write_bytes(self.mine)
            dl.chmod(0o644)
            self.assertIsNone(gsvc.find_comet(""))
            dl.chmod(0o755)
            self.assertEqual(gsvc.find_comet("").source, "download")

    def test_dummy_prefers_own_download(self):
        self.assertIsNone(gsvc.find_dummy())
        self.heroic_dummy.parent.mkdir(parents=True, exist_ok=True)
        self.heroic_dummy.write_bytes(b"MZ heroic")
        self.assertEqual(gsvc.find_dummy(), self.heroic_dummy)
        data = b"MZ dummy"
        with mock.patch.dict(gsvc.ASSETS, {"dummy": gsvc.Asset("dummy", "GalaxyCommunication-dummy.exe",
                                                               "GalaxyCommunication-dummy.exe", sha(data), len(data))}):
            own = gsvc.download_dir() / "GalaxyCommunication-dummy.exe"
            own.parent.mkdir(parents=True)
            own.write_bytes(data)
            self.assertEqual(gsvc.find_dummy(), own)

    def test_version_parsing(self):
        self.assertEqual(gsvc.parse_comet_version("comet 0.3.2\n"), "0.3.2")
        self.assertEqual(gsvc.parse_comet_version("kaputt"), "")
        self.assertEqual(gsvc.comet_version(self.root / "fehlt"), "")


# ----------------------------------------------------------------------------- Download
class PinnedReleaseTest(unittest.TestCase):
    def test_release_v032_pinned(self):
        self.assertEqual(gsvc.COMET_VERSION, "0.3.2")
        c, d = gsvc.ASSETS["comet"], gsvc.ASSETS["dummy"]
        self.assertEqual(gsvc.asset_url(c),
                         "https://github.com/imLinguin/comet/releases/download/v0.3.2/comet-x86_64-unknown-linux-gnu")
        self.assertEqual(c.sha256, "2d6694d544fd3155d90d540e70bc1be767a6b9fdda130275f2b79616ff14e843")
        self.assertEqual(gsvc.asset_url(d), "https://github.com/imLinguin/comet/releases/download/v0.3.2/GalaxyCommunication-dummy.exe")
        self.assertEqual(d.sha256, "c7695267da363a861af99db95cafe68b732ae743e5830b4feea1bc7ee745f99d")
        self.assertEqual((c.mode, d.mode), (0o755, 0o755))


class DownloadTest(TempDataBase):
    def setUp(self):
        super().setUp()
        self.comet = version_script("0.3.2").encode()
        self.dummy = b"MZ" + b"\0" * 100
        self.assets = mock.patch.dict(gsvc.ASSETS, {
            "comet": gsvc.Asset("comet", "comet-x86_64-unknown-linux-gnu", "comet", sha(self.comet), len(self.comet)),
            "dummy": gsvc.Asset("dummy", "GalaxyCommunication-dummy.exe", "GalaxyCommunication-dummy.exe", sha(self.dummy), len(self.dummy)),
        })
        self.assets.start()
        self.arch = mock.patch.object(gsvc.platform, "machine", return_value="x86_64")
        self.arch.start()
        self.session = FakeSession({"comet-x86_64-unknown-linux-gnu": self.comet, "GalaxyCommunication-dummy.exe": self.dummy})

    def tearDown(self):
        self.arch.stop()
        self.assets.stop()
        super().tearDown()

    def test_download_verifies_and_writes_atomically(self):
        seen = []
        out = gsvc.download_tools(session=self.session, progress=lambda d, t: seen.append((d, t)))
        comet = out["comet"]
        self.assertEqual(comet, gsvc.download_dir() / "comet")
        self.assertEqual(comet.read_bytes(), self.comet)
        self.assertEqual(stat.S_IMODE(comet.stat().st_mode), 0o755)
        self.assertEqual(out["dummy"].read_bytes(), self.dummy)
        self.assertEqual([p.name for p in comet.parent.iterdir() if p.name.endswith(".part")], [])
        self.assertIn("Apache License", (comet.parent / "NOTICE").read_text())
        self.assertEqual(seen[-1], (len(self.comet) + len(self.dummy),) * 2)
        self.assertTrue(all(u.startswith("https://github.com/imLinguin/comet/releases/download/v0.3.2/") for u in self.session.urls))
        # vorhanden und geprüft → kein zweiter Download
        self.session.urls.clear()
        gsvc.download_tools(session=self.session)
        self.assertEqual(self.session.urls, [])

    def test_checksum_mismatch_discards_file(self):
        bad = FakeSession({"comet-x86_64-unknown-linux-gnu": self.comet + b"x", "GalaxyCommunication-dummy.exe": self.dummy})
        with self.assertRaises(gsvc.CometError):
            gsvc.download_tools(("comet",), session=bad)
        d = gsvc.download_dir()
        self.assertFalse((d / "comet").exists())
        self.assertEqual(list(d.iterdir()), [])
        self.assertGreater(gsvc._last_failure, 0)

    def test_http_error_and_cancel(self):
        with self.assertRaises(gsvc.CometError):
            gsvc.download_tools(("dummy",), session=FakeSession(self.session.files, status=404))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(gsvc.CometError):
            gsvc.download_tools(("comet",), session=self.session, cancel=ev)
        self.assertEqual(list(gsvc.download_dir().iterdir()), [])

    def test_explicit_update_overrides_older_heroic(self):
        script(self.heroic, version_script("0.2.0"))
        old = gsvc.tools_root() / "0.2.9"
        old.mkdir(parents=True)
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(gsvc.find_comet("").source, "heroic")
            tool, new_path = gsvc.download_and_select("", session=self.session)
            self.assertEqual(new_path, str(gsvc.download_dir() / "comet"))      # neuer Download vor älterem Heroic
            self.assertEqual((tool.source, tool.version), ("settings", "0.3.2"))
            self.assertFalse(old.exists())
            # ohne ältere Konkurrenz bleibt der Pfad leer (automatische Suche findet den Download)
            self.heroic.unlink()
            tool, new_path = gsvc.download_and_select("", session=self.session)
            self.assertEqual((tool.source, new_path), ("download", ""))

    def test_status_info_and_labels(self):
        with mock.patch("shutil.which", return_value=None), mock.patch.object(gsvc, "port_in_use", return_value=False):
            info = gsvc.status_info("", enabled=True)
            self.assertIsNone(info["comet"])
            self.assertEqual(gsvc.download_label(info), "comet herunterladen")
            self.assertIn("ersten Start", gsvc.status_text(info))
            self.assertEqual(gsvc.status_text(gsvc.status_info("", enabled=False)), "Nicht gefunden")
            script(self.heroic, version_script("0.2.0"))
            info = gsvc.status_info("", enabled=True)
            self.assertEqual((info["comet"]["source"], info["comet"]["outdated"]), ("heroic", True))
            self.assertEqual(gsvc.download_label(info), "comet herunterladen")     # Platzhalterdienst fehlt noch
            gsvc.download_tools(session=self.session)
            info = gsvc.status_info("", enabled=True)
            self.assertEqual(gsvc.download_label(info), "Aktualisieren")           # Heroic-comet ist älter
            self.assertIn(str(self.heroic), gsvc.status_text(info))
            self.assertIn("0.3.2", gsvc.status_text(info))
            self.heroic.unlink()
            info = gsvc.status_info("", enabled=True)
            self.assertEqual((info["comet"]["source"], gsvc.download_label(info)), ("download", ""))
        with mock.patch.object(gsvc, "port_in_use", return_value=True):
            self.assertIn("9977", gsvc.status_text(gsvc.status_info("", enabled=True)))

    def test_auto_download_on_first_need_only_once_after_failure(self):
        settings = GlobalSettings()
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(gsvc, "download_tools", side_effect=gsvc.CometError("offline")) as dl:
            with mock.patch.object(gsvc, "_last_failure", 0.0):
                self.assertIsNone(gsvc.ensure_comet(settings))
            with mock.patch.object(gsvc, "_last_failure", time.monotonic()):
                self.assertIsNone(gsvc.ensure_comet(settings))
        self.assertEqual(dl.call_count, 1)


# ----------------------------------------------------------------------------- Zugangsdatei, Aufruf, Umgebung
class AuthFileTest(TempDataBase):
    def creds(self):
        return Credentials("ACCESS-" + "a" * 60, "REFRESH-" + "r" * 60, 1000.0 + 3600, "4711", session_id="SID")

    def test_content_and_permissions(self):
        path, payload = gsvc.write_auth_file(self.creds(), now=1000.0)
        root = gsvc.auth_root()
        self.assertEqual(path, root / "heroic" / "gog_store" / "auth.json")
        self.assertEqual(path, gsvc.auth_file())
        data = json.loads(path.read_text())
        self.assertEqual(data, payload)
        entry = data["46899977096215655"]
        self.assertEqual(entry["access_token"], self.creds().access_token)
        self.assertEqual(entry["refresh_token"], self.creds().refresh_token)
        self.assertEqual(entry["user_id"], "4711")
        self.assertIsInstance(entry["user_id"], str)       # comet: as_str().unwrap()
        self.assertEqual(entry["expires_in"], 3600)
        self.assertNotIn("session_id", entry)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for d in (root, root / "heroic", root / "heroic" / "gog_store"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)
        gsvc.remove_auth_file()
        self.assertFalse(path.exists())
        gsvc.remove_auth_file()                             # zweimal ist kein Fehler

    def test_command_and_env_without_tokens(self):
        argv = gsvc.comet_command(Path("/opt/comet"), "spieler")
        self.assertEqual(argv, ["/opt/comet", "--from-heroic", "--username", "spieler", "--quit"])
        self.assertFalse(any("ACCESS" in a or "REFRESH" in a or "token" in a for a in argv))
        env = gsvc.comet_env(Path("/priv"), base={"HOME": "/h", "LD_PRELOAD": "x.so", "PATH": "/bin", "XDG_CONFIG_HOME": "/c"})
        self.assertEqual(env["XDG_CONFIG_PATH"], "/priv")
        self.assertEqual(env["HOME"], "/h")
        self.assertNotIn("LD_PRELOAD", env)
        self.assertEqual(env["XDG_CONFIG_HOME"], "/c")
        self.assertIn("HOME", gsvc.comet_env(Path("/p"), base={}))

    def test_rotated_tokens_imported_only_if_own_session_unchanged(self):
        c = self.creds()
        path, written = gsvc.write_auth_file(c, now=1000.0)
        api = FakeApi(Credentials(c.access_token, c.refresh_token, c.expires_at, c.user_id, session_id="SID"))
        self.assertFalse(gsvc.import_rotated(api, path, written))          # unverändert
        data = json.loads(path.read_text())
        data["46899977096215655"].update(access_token="NEU-A", refresh_token="NEU-R", expires_in=3600, loginTime=2_000_000)
        path.write_text(json.dumps(data))
        self.assertTrue(gsvc.import_rotated(api, path, written))
        self.assertEqual((api.creds.access_token, api.creds.refresh_token, api.creds.user_id), ("NEU-A", "NEU-R", "4711"))
        self.assertEqual(api.creds.expires_at, 2000 + 3600)
        self.assertEqual(api.creds.session_id, "SID")
        api.store.save.assert_called_once()
        # eigene Sitzung inzwischen selbst erneuert → eigene Tokens sind neuer
        api2 = FakeApi(Credentials("x", "eigenes-neues", 9e9, "4711"))
        self.assertFalse(gsvc.import_rotated(api2, path, written))
        self.assertEqual(api2.creds.refresh_token, "eigenes-neues")
        self.assertFalse(gsvc.import_rotated(None, path, written))

    def test_fresh_credentials(self):
        api = FakeApi(Credentials("alt", "alt-r", time.time() + 3000, "4711"))
        c = gsvc.fresh_credentials(api)
        self.assertEqual(api.refreshed, 1)
        self.assertEqual(c.access_token, api.creds.access_token)
        self.assertIsNot(c, api.creds)
        ok = FakeApi(Credentials("alt", "alt-r", time.time() + 3000, "4711"), refresh_error=AuthError("offline"))
        self.assertEqual(gsvc.fresh_credentials(ok).access_token, "alt")
        expired = FakeApi(Credentials("alt", "alt-r", time.time() - 10, "4711"), refresh_error=AuthError("offline"))
        with self.assertRaises(gsvc.CometError):
            gsvc.fresh_credentials(expired)
        from galaxy_linux.api.client import NotLoggedIn
        with self.assertRaises(NotLoggedIn):
            gsvc.fresh_credentials(FakeApi(None))

    def test_account_name_fallbacks(self):
        api = FakeApi(Credentials("a", "r", 9e9, "4711"), username="")
        api.user_data = mock.Mock(side_effect=RuntimeError("offline"))
        self.assertEqual(gsvc.account_name(api), "4711")
        self.assertEqual(gsvc.account_name(FakeApi(Credentials("a", "r", 9e9, "1"), username="Gerd")), "Gerd")

    def test_mask_line(self):
        line = 'Client identified as 123 ' + "f" * 64 + " token ACCESS-xyz Bearer abc.def " + "Z" * 90
        out = gsvc.mask_line(line, ["ACCESS-xyz"])
        self.assertNotIn("ACCESS-xyz", out)
        self.assertNotIn("f" * 64, out)
        self.assertNotIn("Z" * 90, out)
        self.assertNotIn("abc.def", out)
        self.assertIn("Client identified as 123", out)


# ----------------------------------------------------------------------------- Port
class PortTest(unittest.TestCase):
    def test_port_in_use_detection(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertTrue(gsvc.port_in_use(port))
        finally:
            s.close()
        self.assertFalse(gsvc.port_in_use(port))


# ----------------------------------------------------------------------------- Platzhalterdienst
class DummyServiceTest(TempDataBase):
    def test_drive_c_mapping(self):
        p = self.root / "pfxroot"
        self.assertEqual(gsvc.drive_c(p, "umu"), p / "pfx" / "drive_c")
        self.assertEqual(gsvc.drive_c(p, "proton"), p / "pfx" / "drive_c")
        self.assertEqual(gsvc.drive_c(p, "wine"), p / "drive_c")
        (p / "pfx").mkdir(parents=True)
        self.assertEqual(gsvc.drive_c(p, "wine"), p / "pfx" / "drive_c")
        dc = Path("/x/pfx/drive_c")
        self.assertEqual(gsvc.windows_to_prefix_path(dc, gsvc.DUMMY_WIN_PATH),
                         dc / "ProgramData" / "GOG.com" / "Galaxy" / "redists" / "GalaxyCommunication.exe")

    def test_commands_like_heroic(self):
        self.assertEqual(gsvc.SC_CREATE_SERVICE, ["sc", "create", "GalaxyCommunication",
                                                  "binpath=C:\\ProgramData\\GOG.com\\Galaxy\\redists\\GalaxyCommunication.exe"])
        self.assertEqual(gsvc.REG_ADD_CLIENT, ["reg", "add", "HKLM\\SOFTWARE\\WOW6432Node\\GOG.com\\GalaxyClient\\paths",
                                               "/v", "client", "/t", "REG_SZ", "/d", "C:\\Program Files\\GOG Galaxy", "/f"])
        kinds = lambda steps: [k for k, _ in steps]
        self.assertEqual(kinds(gsvc.service_steps(False, False, False)), ["reg", "copy", "sc"])
        self.assertEqual(gsvc.service_steps(False, False, False)[0][1], gsvc.REG_ADD_CLIENT)
        self.assertEqual(gsvc.service_steps(False, False, False)[2][1], gsvc.SC_CREATE_SERVICE)
        self.assertEqual(kinds(gsvc.service_steps(True, True, True)), [])
        self.assertEqual(kinds(gsvc.service_steps(True, False, True)), ["reg"])
        self.assertEqual(kinds(gsvc.service_steps(True, True, False)), ["sc"])
        self.assertEqual(kinds(gsvc.service_steps(False, True, True)), ["copy", "sc"])   # Datei fehlte → wie Heroic

    def test_registry_state(self):
        p = self.root / "p"
        p.mkdir()
        self.assertEqual(gsvc.registry_state(p), (False, False))
        (p / "system.reg").write_text(
            "WINE REGISTRY Version 2\n\n"
            "[Software\\\\Wow6432Node\\\\GOG.com\\\\GalaxyClient\\\\paths] 1791146050\n#time=1db\n"
            "\"client\"=\"C:\\\\Program Files\\\\GOG Galaxy\"\n\n"
            "[System\\\\ControlSet001\\\\Services\\\\GalaxyCommunication] 1791146050\n\"Start\"=dword:00000003\n")
        self.assertEqual(gsvc.registry_state(p), (True, True))
        (p / "system.reg").write_text("[Software\\\\Wow6432Node\\\\GOG.com\\\\GalaxyClient\\\\paths] 1\n\"other\"=\"x\"\n"
                                      "[System\\\\CurrentControlSet\\\\Services\\\\GalaxyCommunication] 1\n")
        self.assertEqual(gsvc.registry_state(p), (False, True))

    def make_ctx(self, runner="umu"):
        prefix = self.root / "prefixes" / "1"
        prefix.mkdir(parents=True, exist_ok=True)
        status = []
        return types.SimpleNamespace(prefix=prefix, runner=runner, settings=GlobalSettings(), status=status.append,
                                     product_id="1", statuses=status)

    def test_setup_runs_reg_copy_sc_then_nothing(self):
        dummy = self.root / "dummy.exe"
        dummy.write_bytes(b"MZ dummy")
        ctx = self.make_ctx("proton")
        calls = []

        def fake_run(c, argv):
            calls.append(argv)
            (c.prefix / "pfx" / "drive_c").mkdir(parents=True, exist_ok=True)     # Proton legt den Prefix an
            return 0
        with mock.patch.object(gsvc, "find_dummy", return_value=dummy):
            done = gsvc.setup_dummy_service(ctx, run=fake_run)
        self.assertEqual(done, ["reg", "copy", "sc"])
        self.assertEqual(calls, [gsvc.REG_ADD_CLIENT, gsvc.SC_CREATE_SERVICE])
        dest = ctx.prefix / "pfx" / "drive_c" / "ProgramData" / "GOG.com" / "Galaxy" / "redists" / "GalaxyCommunication.exe"
        self.assertEqual(dest.read_bytes(), b"MZ dummy")
        self.assertTrue(ctx.statuses)
        # Prefix hat jetzt alles (Wine schreibt die Registry) → beim nächsten Start nichts zu tun
        (ctx.prefix / "pfx" / "system.reg").write_text(
            "[Software\\\\Wow6432Node\\\\GOG.com\\\\GalaxyClient\\\\paths] 1\n\"client\"=\"C:\\\\Program Files\\\\GOG Galaxy\"\n"
            "[System\\\\ControlSet001\\\\Services\\\\GalaxyCommunication] 1\n")
        calls.clear()
        with mock.patch.object(gsvc, "find_dummy", side_effect=AssertionError("nicht nötig")):
            self.assertEqual(gsvc.setup_dummy_service(ctx, run=fake_run), [])
        self.assertEqual(calls, [])

    def test_setup_stops_if_prefix_was_not_created(self):
        dummy = self.root / "dummy.exe"
        dummy.write_bytes(b"MZ dummy")
        ctx = self.make_ctx("umu")
        calls = []
        with mock.patch.object(gsvc, "find_dummy", return_value=dummy):
            done = gsvc.setup_dummy_service(ctx, run=lambda c, argv: calls.append(argv) or 1)   # reg add scheitert
        self.assertEqual(done, ["reg"])
        self.assertEqual(calls, [gsvc.REG_ADD_CLIENT])
        self.assertFalse((ctx.prefix / "pfx").exists())                   # kein eigener pfx-Ordner (umu: Verweis)

    def test_setup_wine_prefix_without_pfx(self):
        dummy = self.root / "dummy.exe"
        dummy.write_bytes(b"MZ dummy")
        ctx = self.make_ctx("wine")
        (ctx.prefix / "drive_c").mkdir()
        (ctx.prefix / "system.reg").write_text(
            "[Software\\\\Wow6432Node\\\\GOG.com\\\\GalaxyClient\\\\paths] 1\n\"client\"=\"C:\\\\Program Files\\\\GOG Galaxy\"\n")
        calls = []
        with mock.patch.object(gsvc, "find_dummy", return_value=dummy):
            done = gsvc.setup_dummy_service(ctx, run=lambda c, argv: calls.append(argv) or 0)
        self.assertEqual(done, ["copy", "sc"])
        self.assertEqual(calls, [gsvc.SC_CREATE_SERVICE])
        self.assertTrue((ctx.prefix / "drive_c" / "ProgramData" / "GOG.com" / "Galaxy" / "redists" / "GalaxyCommunication.exe").is_file())

    def test_setup_skips_without_dummy(self):
        ctx = self.make_ctx("wine")
        with mock.patch.object(gsvc, "find_dummy", return_value=None), \
             mock.patch.object(gsvc, "download_tools", side_effect=gsvc.CometError("offline")), \
             mock.patch("shutil.which", return_value=None):
            self.assertEqual(gsvc.setup_dummy_service(ctx, run=lambda c, a: self.fail("kein Befehl")), [])

    def test_run_prefix_command_uses_verb_run(self):
        ctx = self.make_ctx()
        ctx.log_file = self.root / "game.log"
        ctx.launcher = mock.Mock()
        ctx.launcher.prefix_command.return_value = (["/bin/true"], dict(os.environ))
        self.assertEqual(gsvc.run_prefix_command(ctx, gsvc.REG_ADD_CLIENT), 0)
        args, kw = ctx.launcher.prefix_command.call_args
        self.assertEqual(args, ("1", gsvc.REG_ADD_CLIENT))
        self.assertEqual(kw["verb"], "run")
        self.assertIn("Galaxy-Dienste", ctx.log_file.read_text())


# ----------------------------------------------------------------------------- Bedingungen und Hooks
def ctx_ns(**kw):
    base = dict(preview=False, is_windows=True, is_gog=True, gs=GameSettings(), settings=GlobalSettings(),
                api=types.SimpleNamespace(logged_in=True), prepare_only=False, data={}, product_id="1")
    base.update(kw)
    return types.SimpleNamespace(**base)


class WantedAndHookTest(unittest.TestCase):
    def test_wanted(self):
        self.assertTrue(gsvc.wanted(ctx_ns()))
        self.assertFalse(gsvc.wanted(ctx_ns(is_windows=False)))
        self.assertFalse(gsvc.wanted(ctx_ns(is_gog=False)))
        self.assertFalse(gsvc.wanted(ctx_ns(preview=True)))
        self.assertFalse(gsvc.wanted(ctx_ns(api=None)))
        self.assertFalse(gsvc.wanted(ctx_ns(api=types.SimpleNamespace(logged_in=False))))
        self.assertFalse(gsvc.wanted(ctx_ns(gs=GameSettings(galaxy_services="off"))))
        off = GlobalSettings(feature_galaxy_services=False)
        self.assertFalse(gsvc.wanted(ctx_ns(settings=off)))
        self.assertTrue(gsvc.wanted(ctx_ns(settings=off, gs=GameSettings(galaxy_services="on"))))

    def test_hooks_registered(self):
        with mock.patch.dict(hooks._hooks, clear=True):
            sys.modules.pop("galaxy_linux.launch.hook_galaxy_services", None)
            mod = importlib.import_module("galaxy_linux.launch.hook_galaxy_services")
            by_name = {h.name: h for h in hooks.hooks()}
            self.assertEqual((by_name["galaxy_service"].order, by_name["galaxy_service"].setup), (20, True))
            self.assertEqual((by_name["comet"].order, by_name["comet"].setup), (25, False))
            comet = by_name["comet"]
            self.assertFalse(comet.enabled(ctx_ns(prepare_only=True)))
            self.assertTrue(comet.enabled(ctx_ns()))
            ctx = ctx_ns(gs=GameSettings(galaxy_services="off"), data={"comet": {"state": "started"}})
            self.assertTrue(comet.enabled(ctx))              # gestartet → after_exit läuft auch nach Umschalten
            with mock.patch.object(mod.gsvc, "COMET") as svc:
                comet.after_exit(ctx, 0, 5)
                svc.release.assert_called_once_with("1")
                svc.reset_mock()
                comet.after_exit(ctx_ns(), 0, 5)
                svc.release.assert_not_called()

    def test_start_comet_flow(self):
        status = []
        launcher = types.SimpleNamespace(is_running=lambda pid: True)
        api = FakeApi(Credentials("a" * 50, "r" * 50, time.time() + 3000, "4711"))
        ctx = ctx_ns(api=api, status=status.append, launcher=launcher)
        svc = mock.Mock(port=1, host="127.0.0.1")
        svc.running.return_value = False
        svc.acquire.return_value = "started"
        tool = gsvc.CometTool(Path("/opt/comet"), "heroic", "0.2.0")
        with mock.patch.object(gsvc, "port_in_use", return_value=False), \
             mock.patch.object(gsvc, "ensure_comet", return_value=tool):
            self.assertEqual(gsvc.start_comet(ctx, svc), "started")
        args, kw = svc.acquire.call_args
        self.assertEqual(args[:2], ("1", Path("/opt/comet")))
        self.assertEqual(args[2].refresh_token, api.creds.refresh_token)       # frisch erneuert
        self.assertEqual(args[3], "spieler")
        self.assertIs(kw["api"], api)
        self.assertEqual(ctx.data["comet"]["state"], "started")
        # Port fremd belegt → nichts starten
        ctx2 = ctx_ns(api=api, status=status.append, launcher=launcher)
        svc.reset_mock()
        with mock.patch.object(gsvc, "port_in_use", return_value=True), \
             mock.patch.object(gsvc, "ensure_comet", side_effect=AssertionError("kein Download")):
            self.assertEqual(gsvc.start_comet(ctx2, svc), "foreign")
        svc.acquire.assert_not_called()
        self.assertNotIn("comet", ctx2.data)
        # comet fehlt und lässt sich nicht laden → Spiel startet trotzdem
        with mock.patch.object(gsvc, "port_in_use", return_value=False), \
             mock.patch.object(gsvc, "ensure_comet", return_value=None):
            self.assertEqual(gsvc.start_comet(ctx_ns(api=api, status=status.append, launcher=launcher), svc), "missing")


# ----------------------------------------------------------------------------- comet-Prozess (Fake)
FAKE_COMET = """#!{python}
import json, os, signal, socket, sys, time
root = os.environ["XDG_CONFIG_PATH"]
path = os.path.join(root, "heroic", "gog_store", "auth.json")
data = json.load(open(path))
entry = data["46899977096215655"]
print("Loading Heroic credentials", entry["access_token"], "refresh", entry["refresh_token"], flush=True)
print("Client identified as 99 " + "e" * 64, flush=True)
print("args", " ".join(sys.argv[1:]), flush=True)
if os.environ.get("FAKE_ROTATE"):
    entry["access_token"], entry["refresh_token"] = "ROTATED-A", "ROTATED-R"
    json.dump(data, open(path, "w"))
if os.environ.get("FAKE_EXIT"):
    sys.exit(3)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(os.environ["FAKE_COMET_PORT"]))); s.listen(5)
print("Listening on port", os.environ["FAKE_COMET_PORT"], flush=True)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(0.1)
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CometServiceTest(TempDataBase):
    def setUp(self):
        super().setUp()
        self.fake = script(self.root / "fake" / "comet", FAKE_COMET.format(python=sys.executable))
        self.port = free_port()
        self.env = mock.patch.dict(os.environ, {"FAKE_COMET_PORT": str(self.port)})
        self.env.start()
        self.creds = Credentials("SECRET-ACCESS-" + "q" * 30, "SECRET-REFRESH-" + "w" * 30, time.time() + 3600, "4711")
        self.svc = gsvc.CometService(port=self.port)

    def tearDown(self):
        self.svc.stop()
        self.env.stop()
        super().tearDown()

    def test_start_share_release_and_cleanup(self):
        running = {"1": True, "2": True}
        api = FakeApi(Credentials(self.creds.access_token, self.creds.refresh_token, self.creds.expires_at, "4711"))
        state = self.svc.acquire("1", self.fake, self.creds, "spieler", api=api, is_running=lambda p: running.get(p, False))
        self.assertEqual(state, "started")
        self.assertTrue(self.svc.running())
        self.assertTrue(gsvc.port_in_use(self.port))
        auth = gsvc.auth_file()
        self.assertTrue(auth.exists())
        self.assertNotIn(self.creds.access_token, " ".join(self.svc.proc.args))
        self.assertEqual(self.svc.acquire("2", self.fake, None, ""), "shared")
        self.svc.release("1")
        self.assertTrue(self.svc.running())               # Spiel 2 läuft noch
        self.svc.release("2")
        self.assertFalse(self.svc.running())
        self.assertFalse(auth.exists())
        self.assertFalse((gsvc.auth_root() / "comet-raw.log").exists())
        log_text = gsvc.comet_log_file().read_text()
        self.assertIn("Loading Heroic credentials", log_text)
        self.assertIn("--from-heroic --username spieler --quit", log_text)
        self.assertNotIn(self.creds.access_token, log_text)
        self.assertNotIn(self.creds.refresh_token, log_text)
        self.assertNotIn("e" * 64, log_text)
        self.assertEqual(api.store.save.call_count, 0)      # comet hat nichts erneuert

    def test_foreign_port_and_rotation_import(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.port))
        blocker.listen(1)
        try:
            self.assertEqual(self.svc.acquire("1", self.fake, self.creds, "spieler"), "foreign")
            self.assertFalse(gsvc.auth_file().exists())
        finally:
            blocker.close()
        api = FakeApi(Credentials(self.creds.access_token, self.creds.refresh_token, self.creds.expires_at, "4711"))
        with mock.patch.dict(os.environ, {"FAKE_ROTATE": "1"}):
            self.assertEqual(self.svc.acquire("1", self.fake, self.creds, "spieler", api=api), "started")
        self.svc.release("1")
        self.assertEqual((api.creds.access_token, api.creds.refresh_token), ("ROTATED-A", "ROTATED-R"))
        api.store.save.assert_called_once()
        self.assertFalse(gsvc.auth_file().exists())

    def test_immediate_exit_raises_and_cleans_up(self):
        with mock.patch.dict(os.environ, {"FAKE_EXIT": "1"}):
            with self.assertRaises(gsvc.CometError):
                self.svc.acquire("1", self.fake, self.creds, "spieler")
        self.assertFalse(self.svc.running())
        self.assertFalse(gsvc.auth_file().exists())

    def test_watchdog_stops_when_no_game_runs(self):
        running = {"1": True}
        with mock.patch.object(gsvc, "WATCH_INTERVAL", 0.2):
            self.assertEqual(self.svc.acquire("1", self.fake, self.creds, "spieler", is_running=lambda p: running.get(p, False)), "started")
            running["1"] = False                           # Start gescheitert, after_exit kommt nie
            deadline = time.monotonic() + 10
            while self.svc.running() and time.monotonic() < deadline:
                time.sleep(0.1)
        self.assertFalse(self.svc.running())
        self.assertFalse(gsvc.auth_file().exists())

    def test_shutdown_keeps_comet_for_running_game_but_removes_auth(self):
        self.assertEqual(self.svc.acquire("1", self.fake, self.creds, "spieler", is_running=lambda p: True), "started")
        self.svc.shutdown()
        self.assertTrue(self.svc.running())
        self.assertFalse(gsvc.auth_file().exists())
        idle = gsvc.CometService(port=free_port())
        idle.shutdown()                                     # nichts gestartet → nichts anfassen


if __name__ == "__main__":
    unittest.main()
