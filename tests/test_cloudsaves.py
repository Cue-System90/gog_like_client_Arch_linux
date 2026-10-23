"""Tests für die Cloud-Spielstände (content/cloudsaves.py, launch/hook_cloudsaves.py) – ohne Netz:
cloudstorage.gog.com und remote-config werden durch Attrappen ersetzt, alle Pfade liegen in Temp-Ordnern."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import tarfile
import tempfile
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from galaxy_linux.content import cloudsaves as cs
from galaxy_linux.i18n import tr
from galaxy_linux.launch import hooks
from galaxy_linux.launch.hook_cloudsaves import CloudSavesHook
from galaxy_linux.launch.launcher import Launcher
from galaxy_linux.settings import GameSettingsStore, GlobalSettings, InstalledGame, InstalledRegistry, PlaytimeStore

SECRET = "geheim-0123456789abcdef"
CONFIG = {"version": "1.2.37", "content": {
    "Windows": {"overlay": {"supported": True}, "cloudStorage": {"enabled": True, "locations": [
        {"name": "saves", "location": "<?APPLICATION_DATA_ROAMING?>/Studio/Game"}]}},
    "MacOS": {"cloudStorage": {"enabled": False}}, "cloudStorage": {"quota": 209715200}}, "bases": []}


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------- Attrappen
class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", js=None, headers: dict | None = None) -> None:
        self.status_code, self.ok = status, status < 400
        self._body, self._js, self.headers = body, js, headers or {}
        self.closed = False

    def json(self):
        if self._js is None:
            raise ValueError("kein JSON")
        return self._js

    def iter_content(self, _size):
        yield self._body

    def close(self) -> None:
        self.closed = True


class FakeCloud:
    """cloudstorage.gog.com v1 im Speicher – nur, was ``CloudStorage`` benutzt (Liste, GET, PUT, DELETE)."""

    def __init__(self, base: str = f"{cs.CLOUDSTORAGE}/v1/u1/c1", clock=time.time) -> None:
        self.base, self.clock = base, clock
        self.files: dict[str, dict] = {}
        self.headers: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.fail401 = 0

    def add(self, name: str, data: bytes, last_modified: float, local: float | None = None) -> None:
        gz = gzip.compress(data, 6, mtime=0)
        self.files[name] = {"data": data, "gz": gz, "hash": hashlib.md5(gz).hexdigest(), "last_modified": last_modified,
                            "local": cs.iso_time(local if local is not None else last_modified)}

    def request(self, method, url, headers=None, timeout=None, data=None, stream=False):
        self.calls.append((method, url, dict(headers or {}), data))
        if self.fail401:
            self.fail401 -= 1
            return FakeResponse(401)
        if url == self.base:
            if not self.files:
                return FakeResponse(404)
            return FakeResponse(200, js=[{"name": n, "hash": f["hash"], "bytes": len(f["gz"]),
                                          "last_modified": iso(f["last_modified"])} for n, f in self.files.items()])
        name = urllib.parse.unquote(url[len(self.base) + 1:])
        if method == "PUT":
            self.files[name] = {"data": gzip.decompress(data), "gz": data, "hash": hashlib.md5(data).hexdigest(),
                                "last_modified": self.clock(), "local": headers["X-Object-Meta-LocalLastModified"]}
            return FakeResponse(201)
        if method == "DELETE":
            return FakeResponse(204 if self.files.pop(name, None) else 404)
        f = self.files.get(name)
        if f is None:
            return FakeResponse(404)
        return FakeResponse(200, body=f["data"], headers={"X-Object-Meta-LocalLastModified": f["local"]})

    def puts(self) -> list[str]:
        return [urllib.parse.unquote(c[1][len(self.base) + 1:]) for c in self.calls if c[0] == "PUT"]


class FakeApi:
    def __init__(self, config=None, logged_in: bool = True) -> None:
        self.config = CONFIG if config is None else config
        self.logged_in = logged_in
        self.user_id = "u1"
        self._game_tokens: dict = {}
        self.get_calls: list[tuple] = []
        self.token_calls = 0

    def get(self, url, params=None, authenticated=True, timeout=30):
        self.get_calls.append((url, params, authenticated))
        return FakeResponse(200, js=self.config)

    def game_token(self, client_id, client_secret):
        self.token_calls += 1
        assert client_secret == SECRET
        return types.SimpleNamespace(access_token=f"tok-{client_id}", user_id="u1")


# --------------------------------------------------------------------------- Grundlage mit Temp-Ordnern
class CloudBase(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        root = self.root = Path(self.td.name)
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", DATA_DIR=root / "data", CACHE_DIR=root / "cache", API_CACHE_DIR=root / "cache" / "api",
            MANIFEST_DIR=root / "data" / "manifests", LOG_DIR=root / "logs", SETTINGS_FILE=root / "settings.json",
            GAME_SETTINGS_FILE=root / "games.json", INSTALLED_FILE=root / "installed.json",
            PLAYTIME_FILE=root / "playtime.json")
        self.patch.start()
        self.game_dir = root / "Games" / "Demo"
        self.game_dir.mkdir(parents=True)
        # Prefix wie von umu angelegt: drive_c direkt im Prefix, pfx → .
        self.prefix = root / "prefixes" / "1"
        (self.prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
        (self.prefix / "pfx").symlink_to(".")
        self.saves = self.prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "Studio" / "Game"
        self.settings = GlobalSettings(install_dir=str(root / "Games"), prefix_dir=str(root / "prefixes"))
        self.gs = GameSettingsStore()
        self.registry = InstalledRegistry()
        self.registry.set(InstalledGame("1", "Demo", "windows", str(self.game_dir)))
        self.registry.set(InstalledGame("2", "Lin", "linux", str(root / "Games" / "Lin")))
        self.api = FakeApi()
        self.launcher = Launcher(self.settings, self.gs, self.registry, PlaytimeStore(), self.api)
        self.rec_patch = mock.patch("galaxy_linux.content.build_records.ensure_record",
                                    return_value={"client_id": "c1", "client_secret": SECRET})
        self.rec_patch.start()
        self.cloud = FakeCloud()
        self.now = time.time()

    def tearDown(self) -> None:
        self.rec_patch.stop()
        self.patch.stop()
        self.td.cleanup()

    def write(self, rel: str, data: bytes, mtime: float) -> Path:
        p = self.saves / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
        return p

    def sync(self, mode: str, ask=None, **kw) -> cs.SyncResult:
        return cs.sync_game(self.launcher, self.api, "1", mode, ask=ask, prefix=self.prefix, runner="umu",
                            session=self.cloud, **kw)

    def stamp(self) -> float:
        return cs.stamp_of(self.gs.get("1").cloud_timestamps, "saves")


# --------------------------------------------------------------------------- Speicherorte
class LocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_wine_root_layouts(self):
        umu = self.root / "umu"
        (umu / "drive_c").mkdir(parents=True)
        (umu / "pfx").symlink_to(".")
        self.assertEqual(cs.wine_root(umu, "umu"), umu)
        proton = self.root / "proton"
        (proton / "pfx" / "drive_c").mkdir(parents=True)
        self.assertEqual(cs.wine_root(proton, "proton"), proton / "pfx")
        wine = self.root / "wine"
        (wine / "drive_c").mkdir(parents=True)
        self.assertEqual(cs.wine_root(wine, "wine"), wine)
        # noch nicht angelegt: wie umu/Proton/Wine ihn anlegen würden
        self.assertEqual(cs.wine_root(self.root / "neu", "proton"), self.root / "neu" / "pfx")
        self.assertEqual(cs.wine_root(self.root / "neu", "umu"), self.root / "neu")

    def test_wine_user_dir(self):
        p = self.root / "p"
        (p / "drive_c" / "users" / "steamuser").mkdir(parents=True)
        (p / "drive_c" / "users" / "anna").symlink_to("steamuser")
        self.assertEqual(cs.wine_user_dir(p, "umu", "anna"), p / "drive_c" / "users" / "steamuser")
        w = self.root / "w"
        (w / "drive_c" / "users" / "anna").mkdir(parents=True)
        self.assertEqual(cs.wine_user_dir(w, "wine", "anna"), w / "drive_c" / "users" / "anna")
        self.assertEqual(cs.wine_user_dir(self.root / "leer", "wine", "anna"), self.root / "leer" / "drive_c" / "users" / "anna")
        self.assertEqual(cs.wine_user_dir(self.root / "leer", "proton", "anna"),
                         self.root / "leer" / "pfx" / "drive_c" / "users" / "steamuser")

    def test_all_placeholders(self):
        inst, user = Path("/spiele/Demo"), Path("/pfx/drive_c/users/steamuser")
        cases = {"<?INSTALL?>/saves": inst / "saves",
                 "<?SAVED_GAMES?>/Demo": user / "Saved Games" / "Demo",
                 "<?APPLICATION_DATA_LOCAL?>/Demo": user / "AppData" / "Local" / "Demo",
                 "<?APPLICATION_DATA_LOCAL_LOW?>/Studio/Demo": user / "AppData" / "LocalLow" / "Studio" / "Demo",
                 "<?APPLICATION_DATA_ROAMING?>/11bitstudios/Frostpunk/Default": user / "AppData" / "Roaming" / "11bitstudios" / "Frostpunk" / "Default",
                 "<?DOCUMENTS?>/My Games/A Plague Tale Innocence": user / "Documents" / "My Games" / "A Plague Tale Innocence",
                 "<?DOCUMENTS?>\\My Games\\Back\\": user / "Documents" / "My Games" / "Back",
                 "<?DOCUMENTS?>": user / "Documents"}
        for tpl, want in cases.items():
            self.assertEqual(cs.resolve_location(tpl, inst, user), want, tpl)
        for bad in ("<?APPLICATION_SUPPORT?>/x", "C:/Saves", "<?DOCUMENTS?>/../../etc", "<?DOCUMENTS?>/<?INSTALL?>", ""):
            self.assertIsNone(cs.resolve_location(bad, inst, user), bad)

    def test_existing_folders_case_insensitive(self):
        user = self.root / "drive_c" / "users" / "steamuser"
        (user / "AppData" / "Roaming" / "Studio" / "GAME").mkdir(parents=True)
        got = cs.resolve_location("<?APPLICATION_DATA_ROAMING?>/studio/game/sub", self.root / "inst", user)
        self.assertEqual(got, user / "AppData" / "Roaming" / "Studio" / "GAME" / "sub")

    def test_parse_remote_config(self):
        cfg = cs.parse_remote_config(CONFIG, "c1")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.locations, [("saves", "<?APPLICATION_DATA_ROAMING?>/Studio/Game")])
        self.assertEqual(cfg.quota, 209715200)
        empty = cs.parse_remote_config({"content": {"Windows": {"cloudStorage": {"enabled": True, "locations": []}}}}, "c1")
        self.assertEqual(empty.locations, [("__default", "<?APPLICATION_DATA_LOCAL?>/GOG.com/Galaxy/Applications/c1/Storage/Shared/Files")])
        self.assertEqual(empty.quota, cs.DEFAULT_QUOTA)
        off = cs.parse_remote_config({"content": {"Windows": {"cloudStorage": {"enabled": False}}}}, "c1")
        self.assertFalse(off.enabled)
        self.assertEqual(off.locations, [])
        self.assertFalse(cs.parse_remote_config(None, "c1").enabled)
        dup = cs.parse_remote_config({"content": {"Windows": {"cloudStorage": {"enabled": True, "locations": [
            {"name": "a", "location": "<?DOCUMENTS?>/a"}, {"name": "a", "location": "<?DOCUMENTS?>/b"}, {"name": "b"}]}}}}, "c1")
        self.assertEqual(dup.locations, [("a", "<?DOCUMENTS?>/a")])


class GameCloudTest(CloudBase):
    def test_locations_in_prefix_and_cache(self):
        gc = cs.game_cloud(self.launcher, self.api, "1", self.prefix, "umu")
        self.assertEqual(gc.client_id, "c1")
        self.assertEqual([(loc.name, loc.path) for loc in gc.locations], [("saves", self.saves)])
        self.assertNotIn(SECRET, repr(gc))
        url, params, authenticated = self.api.get_calls[0]
        self.assertEqual(url, "https://remote-config.gog.com/components/galaxy_client/clients/c1")
        self.assertEqual(params, {"component_version": "2.0.51"})
        self.assertFalse(authenticated)
        cs.game_cloud(self.launcher, self.api, "1", self.prefix, "umu")
        self.assertEqual(len(self.api.get_calls), 1)          # eine Stunde aus dem Cache

    def test_default_location_and_too_broad(self):
        self.api.config = {"content": {"Windows": {"cloudStorage": {"enabled": True, "locations": [
            {"name": "alles", "location": "<?INSTALL?>"}, {"name": "home", "location": "<?DOCUMENTS?>/.."}]}}}}
        gc = cs.game_cloud(self.launcher, self.api, "1", self.prefix, "umu")
        self.assertEqual([loc.path for loc in gc.locations], [None, None])   # Spielordner selbst: abgelehnt
        (self.root / "cache" / "api" / "remote-config-c1.json").unlink()      # Cache verwerfen
        self.api.config = {"content": {"Windows": {"cloudStorage": {"enabled": True, "locations": []}}}}
        gc = cs.game_cloud(self.launcher, self.api, "1", self.prefix, "umu")
        user = self.prefix / "drive_c" / "users" / "steamuser"
        self.assertEqual(gc.locations[0].path, user / "AppData" / "Local" / "GOG.com" / "Galaxy" / "Applications" / "c1"
                         / "Storage" / "Shared" / "Files")

    def test_unsupported_games(self):
        with self.assertRaises(cs.CloudUnsupported):
            cs.game_cloud(self.launcher, self.api, "2")                    # Linux-Version
        with self.assertRaises(cs.CloudUnsupported):
            cs.game_cloud(self.launcher, self.api, "99")                   # nicht installiert
        self.api.config = {"content": {"Windows": {"cloudStorage": {"enabled": False}}}}
        with self.assertRaises(cs.CloudUnsupported):
            cs.game_cloud(self.launcher, self.api, "1", self.prefix, "umu")
        res = self.sync("auto")
        self.assertEqual(res.state, "unsupported")
        self.assertEqual(cs.load_state("1")["state"], "unsupported")


# --------------------------------------------------------------------------- Klassifizierung
def sf(rel: str, mtime: float, md5: str = "", path: Path | None = None) -> cs.SyncFile:
    return cs.SyncFile(rel, path, md5 or hashlib.md5(rel.encode() + str(mtime).encode()).hexdigest(), mtime, 10)


class ClassifyTest(unittest.TestCase):
    TS = 1000.0

    def test_both_empty_none(self):
        self.assertEqual(cs.classify([], [], self.TS).action, cs.NONE)

    def test_local_empty_download(self):
        c = cs.classify([], [sf("a", 10)], self.TS)
        self.assertEqual((c.action, [f.rel for f in c.updated_cloud]), (cs.DOWNLOAD, ["a"]))

    def test_cloud_empty_upload(self):
        c = cs.classify([sf("a", 10)], [], self.TS)
        self.assertEqual((c.action, [f.rel for f in c.updated_local]), (cs.UPLOAD, ["a"]))

    def test_only_local_newer_upload(self):
        c = cs.classify([sf("a", 2000), sf("b", 500)], [sf("a", 900), sf("c", 800)], self.TS)
        self.assertEqual(c.action, cs.UPLOAD)
        self.assertEqual([f.rel for f in c.updated_local], ["a"])
        self.assertEqual([f.rel for f in c.only_cloud], ["c"])           # wird in der Cloud gelöscht

    def test_only_cloud_newer_download(self):
        c = cs.classify([sf("a", 900), sf("b", 500)], [sf("a", 2000)], self.TS)
        self.assertEqual(c.action, cs.DOWNLOAD)
        self.assertEqual([f.rel for f in c.updated_cloud], ["a"])
        self.assertEqual([f.rel for f in c.only_local], ["b"])           # wird lokal gelöscht

    def test_both_newer_conflict(self):
        self.assertEqual(cs.classify([sf("a", 2000)], [sf("a", 2100)], self.TS).action, cs.CONFLICT)

    def test_nothing_newer_none(self):
        self.assertEqual(cs.classify([sf("a", 900)], [sf("a", 800)], self.TS).action, cs.NONE)

    def test_same_content_is_no_change(self):
        # gleicher Inhalt auf beiden Seiten (z. B. von Windows kopiert) ist kein Konflikt
        self.assertEqual(cs.classify([sf("A", 2000, "x")], [sf("a", 2100, "x")], 0).action, cs.NONE)

    def test_forced_and_decide(self):
        local, cloud = [sf("a", 1, "1"), sf("b", 1, "2")], [sf("a", 1, "1"), sf("c", 1, "3")]
        up = cs.forced(local, cloud, cs.UPLOAD)
        self.assertEqual(([f.rel for f in up.updated_local], [f.rel for f in up.only_cloud]), (["b"], ["c"]))
        down = cs.forced(local, cloud, cs.DOWNLOAD)
        self.assertEqual(([f.rel for f in down.updated_cloud], [f.rel for f in down.only_local]), (["c"], ["b"]))
        self.assertEqual(cs.forced([], cloud, cs.UPLOAD).only_cloud, [])     # leere Quelle löscht nichts
        self.assertEqual(cs.forced(local, [], cs.DOWNLOAD).only_local, [])
        self.assertEqual(cs.decide(cs.UPLOAD, "download"), cs.SKIP)
        self.assertEqual(cs.decide(cs.DOWNLOAD, "upload"), cs.SKIP)
        self.assertEqual(cs.decide(cs.CONFLICT, "upload"), cs.CONFLICT)
        self.assertEqual(cs.decide(cs.UPLOAD, "auto"), cs.UPLOAD)

    def test_cloud_files_filter(self):
        items = [{"name": "saves/a.sav", "hash": "AB", "bytes": 3, "last_modified": "2026-10-22T10:00:00Z"},
                 {"name": "saves/gone.sav", "hash": cs.DELETED_HASH, "bytes": 0, "last_modified": "2026-10-22T10:00:00Z"},
                 {"name": "saves/../x", "hash": "1"}, {"name": "other/b.sav", "hash": "2"}, "kaputt"]
        got = cs.cloud_files(items, "saves")
        self.assertEqual([(f.rel, f.md5, f.size) for f in got], [("a.sav", "ab", 3)])
        self.assertEqual(got[0].mtime, dt.datetime(2026, 10, 22, 10, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(cs.parse_time("2026-10-22T10:00:00"), got[0].mtime)    # ohne Zone = UTC
        self.assertEqual(cs.parse_time("unsinn"), 0.0)


# --------------------------------------------------------------------------- cloudstorage.gog.com
class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.cloud = FakeCloud()
        self.tokens: list[bool] = []
        self.store = cs.CloudStorage("u1", "c1", lambda force=False: (self.tokens.append(force), "tok")[1], self.cloud)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_upload_request_like_gogdl(self):
        content = b"savegame-" * 500
        p = self.root / "a b.sav"
        p.write_bytes(content)
        os.utime(p, (1700000000, 1700000000))
        self.store.upload("saves", cs.SyncFile("sub/a b.sav", p, "", 1700000000, len(content)))
        method, url, headers, data = self.cloud.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, f"{cs.CLOUDSTORAGE}/v1/u1/c1/saves/sub/a%20b.sav")
        self.assertEqual(data, gzip.compress(content, 6, mtime=0))
        self.assertEqual(gzip.decompress(data), content)
        self.assertEqual(headers["Etag"], hashlib.md5(data).hexdigest())
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["X-Object-Meta-LocalLastModified"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(self.cloud.headers["User-Agent"], cs.CLOUD_USER_AGENT)
        self.assertEqual(self.cloud.headers["X-Object-Meta-User-Agent"], cs.CLOUD_USER_AGENT)
        # der lokale Hash entspricht dem, den die Cloud danach meldet (kein Schein-Unterschied)
        self.assertEqual(cs.file_md5(p), self.cloud.files["saves/sub/a b.sav"]["hash"])

    def test_list_accept_404_and_token_refresh(self):
        self.assertEqual(self.store.list(), [])                       # 404 = leer
        self.assertEqual(self.cloud.calls[-1][2]["Accept"], "application/json")
        self.cloud.add("saves/a.sav", b"x", 100)
        self.cloud.fail401 = 1
        self.assertEqual([d["name"] for d in self.store.list()], ["saves/a.sav"])
        self.assertEqual(self.tokens[-2:], [False, True])             # nach 401 neues Spiel-Token

    def test_download_sets_mtime_and_delete(self):
        self.cloud.add("saves/x/y.sav", b"inhalt", 5000, local=1700000000)
        dest = self.root / "x" / "y.sav"
        self.store.download("saves", cs.SyncFile("x/y.sav", None, "", 5000), dest)
        self.assertEqual(dest.read_bytes(), b"inhalt")
        self.assertEqual(int(dest.stat().st_mtime), 1700000000)
        self.assertFalse(list(self.root.rglob("*.gl-part")))
        self.store.delete("saves", "x/y.sav")
        self.store.delete("saves", "x/y.sav")                         # 404 beim Löschen ist kein Fehler
        with self.assertRaises(cs.CloudError):
            self.store.download("saves", cs.SyncFile("fehlt", None), self.root / "fehlt")


# --------------------------------------------------------------------------- Abgleich, Zeitstempel, Rückfragen
class SyncTest(CloudBase):
    def test_upload_and_timestamp_bookkeeping(self):
        self.cloud.clock = lambda: time.time() + 50                  # Server-Uhr geht vor
        self.write("a.sav", b"A1", self.now - 1000)
        self.write("sub/b.sav", b"B1", self.now - 900)
        res = self.sync("upload")
        self.assertEqual(res.state, "synced")
        self.assertEqual(sorted(self.cloud.files), ["saves/a.sav", "saves/sub/b.sav"])
        self.assertEqual(res.locations[0].uploaded, 2)
        server = max(f["last_modified"] for f in self.cloud.files.values())
        self.assertGreaterEqual(self.stamp() + 0.001, server)        # nicht vor die Server-Zeit (ISO: µs)
        self.assertEqual(cs.load_state("1")["state"], "synced")
        kind, at, _ = cs.cloud_status("1", self.gs.get("1"), self.settings)
        self.assertEqual(kind, "synced")
        # zweiter Lauf: nichts zu tun, Zeitstempel bleibt erhalten oder wächst
        before = self.stamp()
        res = self.sync("auto")
        self.assertEqual(res.locations[0].done, cs.NONE)
        self.assertEqual(len(self.cloud.puts()), 2)
        self.assertGreaterEqual(self.stamp(), before)

    def test_download_mode_never_uploads(self):
        self.gs.update("1", cloud_timestamps={"saves": self.now - 500})
        self.cloud.add("saves/a.sav", b"alt", self.now - 800)
        self.write("a.sav", b"neu", self.now - 100)                  # lokal neuer
        res = self.sync("download")
        self.assertEqual(res.state, "pending")
        self.assertEqual(res.locations[0].done, cs.SKIP)
        self.assertEqual(self.cloud.puts(), [])
        self.assertEqual(self.stamp(), self.now - 500)               # bleibt stehen → Upload nach dem Spielen
        res = self.sync("upload")
        self.assertEqual((res.state, self.cloud.files["saves/a.sav"]["data"]), ("synced", b"neu"))

    def test_download_with_backup_and_local_delete(self):
        self.gs.update("1", cloud_timestamps={"saves": self.now - 500})
        self.write("a.sav", b"alt", self.now - 1000)
        self.write("b.sav", b"weg", self.now - 900)                   # in der Cloud gelöscht
        self.cloud.add("saves/a.sav", b"neu aus der Cloud", self.now - 100, local=1700000000)
        res = self.sync("download")
        self.assertEqual(res.state, "synced")
        self.assertEqual((self.saves / "a.sav").read_bytes(), b"neu aus der Cloud")
        self.assertEqual(int((self.saves / "a.sav").stat().st_mtime), 1700000000)
        self.assertFalse((self.saves / "b.sav").exists())
        self.assertGreater(self.stamp(), self.now - 500)
        backups = cs.list_backups("1")
        self.assertEqual(len(backups), 1)
        self.assertEqual(res.backup, str(backups[0]))
        with tarfile.open(backups[0]) as tar:
            self.assertEqual(tar.extractfile("saves/a.sav").read(), b"alt")
            self.assertEqual(tar.extractfile("saves/b.sav").read(), b"weg")

    def test_first_download_into_empty_prefix(self):
        self.cloud.add("saves/x/slot1.sav", b"S", self.now - 100)
        res = self.sync("download")
        self.assertEqual((self.saves / "x" / "slot1.sav").read_bytes(), b"S")
        self.assertEqual(res.backup, "")                             # nichts Lokales zu sichern
        self.assertEqual(cs.list_backups("1"), [])

    def conflict_setup(self) -> None:
        self.gs.update("1", cloud_timestamps={"saves": self.now - 500})
        self.write("a.sav", b"LOKAL", self.now - 100)
        self.cloud.add("saves/a.sav", b"CLOUD", self.now - 50)

    def test_conflict_ignore(self):
        self.conflict_setup()
        asked = []
        res = self.sync("download", ask=lambda t, b, o, d: (asked.append((t, b, o, d)), "ignore")[1])
        title, body, options, default = asked[0]
        self.assertEqual([o[0] for o in options], ["cloud", "local", "ignore"])
        self.assertEqual(default, "ignore")
        self.assertIn(tr('Dieser Computer – Zuletzt geändert: {0}').split("{")[0], body)
        self.assertEqual(res.state, "conflict")
        self.assertEqual(self.stamp(), self.now - 500)
        self.assertEqual((self.saves / "a.sav").read_bytes(), b"LOKAL")
        self.assertEqual(self.cloud.files["saves/a.sav"]["data"], b"CLOUD")
        self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings)[0], "conflict")

    def test_conflict_cloud_wins(self):
        self.conflict_setup()
        res = self.sync("download", ask=lambda *a: "cloud")
        self.assertEqual(res.state, "synced")
        self.assertEqual((self.saves / "a.sav").read_bytes(), b"CLOUD")
        self.assertEqual(len(cs.list_backups("1")), 1)

    def test_conflict_local_wins(self):
        self.conflict_setup()
        res = self.sync("upload", ask=lambda *a: "local")
        self.assertEqual(res.state, "synced")
        self.assertEqual(self.cloud.files["saves/a.sav"]["data"], b"LOKAL")

    def test_quota_exceeded(self):
        self.api.config = {"content": {"Windows": {"cloudStorage": {"enabled": True, "locations": [
            {"name": "saves", "location": "<?APPLICATION_DATA_ROAMING?>/Studio/Game"}]}}, "cloudStorage": {"quota": 10}}}
        self.write("big.sav", b"x" * 100, self.now - 10)
        asked = []
        res = self.sync("upload", ask=lambda *a: (asked.append(a), "cancel")[1])
        self.assertEqual(res.state, "quota")
        self.assertEqual(asked[0][2], [("cancel", tr('Synchronisation abbrechen'))])
        self.assertEqual(self.cloud.puts(), [])
        self.assertEqual(self.stamp(), 0.0)

    def test_force_upload_asks_before_replacing_newer_cloud(self):
        self.gs.update("1", cloud_timestamps={"saves": self.now - 500})
        self.write("a.sav", b"LOKAL", self.now - 1000)
        self.cloud.add("saves/a.sav", b"CLOUD", self.now - 50)        # anderer Rechner hat neuer hochgeladen
        self.cloud.add("saves/nur-cloud.sav", b"C2", self.now - 50)
        res = self.sync("force-upload", ask=lambda *a: "cancel", confirm=True)
        self.assertEqual(res.state, "cancelled")
        self.assertEqual(self.cloud.puts(), [])
        self.assertEqual(self.stamp(), self.now - 500)
        asked = []
        res = self.sync("force-upload", ask=lambda *a: (asked.append(a), "upload")[1], confirm=True)
        self.assertEqual([o[0] for o in asked[0][2]], ["cancel", "upload"])
        self.assertEqual(res.state, "synced")
        self.assertEqual(self.cloud.files["saves/a.sav"]["data"], b"LOKAL")
        self.assertNotIn("saves/nur-cloud.sav", self.cloud.files)

    def test_force_upload_without_newer_cloud_does_not_ask(self):
        self.gs.update("1", cloud_timestamps={"saves": self.now - 500})
        self.write("a.sav", b"LOKAL", self.now - 1000)
        self.cloud.add("saves/a.sav", b"ALT", self.now - 900)
        res = self.sync("force-upload", ask=lambda *a: self.fail("keine Rückfrage erwartet"), confirm=True)
        self.assertEqual(res.state, "synced")
        self.assertEqual(self.cloud.files["saves/a.sav"]["data"], b"LOKAL")

    def test_force_download_replaces_local(self):
        self.write("a.sav", b"LOKAL", self.now - 10)
        self.write("extra.sav", b"E", self.now - 10)
        self.cloud.add("saves/a.sav", b"CLOUD", self.now - 900)
        res = self.sync("force-download")
        self.assertEqual(res.state, "synced")
        self.assertEqual((self.saves / "a.sav").read_bytes(), b"CLOUD")
        self.assertFalse((self.saves / "extra.sav").exists())
        self.assertEqual(len(cs.list_backups("1")), 1)

    def test_login_busy_and_errors(self):
        with self.assertRaises(cs.CloudError):
            cs.sync_game(self.launcher, FakeApi(logged_in=False), "1", "auto")
        with self.assertRaises(ValueError):
            self.sync("seitwärts")
        lock = cs._game_lock("1")
        with lock:
            self.assertTrue(cs.is_syncing("1"))
            self.assertEqual(self.sync("auto").state, "busy")
            self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings)[0], "busy")
        self.cloud.request = lambda *a, **k: FakeResponse(500)
        with self.assertRaises(cs.CloudError):
            self.sync("auto")
        self.assertEqual(cs.load_state("1")["state"], "error")

    def test_secret_never_logged(self):
        self.write("a.sav", b"A", self.now - 10)
        with self.assertLogs("galaxy_linux.content.cloudsaves", level="DEBUG") as logs:
            self.sync("auto")
        self.assertFalse(any(SECRET in line or "tok-c1" in line for line in logs.output))


# --------------------------------------------------------------------------- Sicherungen
class BackupTest(CloudBase):
    def test_rotation_keeps_last_five(self):
        self.write("a.sav", b"A", self.now)
        made = [cs.make_backup("1", [("saves", self.saves)], now=self.now + i * 60) for i in range(7)]
        kept = cs.list_backups("1")
        self.assertEqual(len(kept), cs.BACKUP_KEEP)
        self.assertEqual(kept, made[2:])
        self.assertTrue(all(p.parent == self.root / "data" / "cloudsave-backups" / "1" for p in kept))
        with tarfile.open(kept[-1]) as tar:
            self.assertEqual(tar.getnames()[:2], ["saves", "saves/a.sav"])

    def test_same_second_and_nothing_to_save(self):
        self.write("a.sav", b"A", self.now)
        a = cs.make_backup("1", [("saves", self.saves)], now=self.now)
        b = cs.make_backup("1", [("saves", self.saves)], now=self.now)
        self.assertNotEqual(a, b)
        self.assertIsNone(cs.make_backup("1", [("leer", self.root / "gibt-es-nicht")]))
        self.assertEqual(cs.rotate_backups("1", keep=1), [a])


# --------------------------------------------------------------------------- Anzeige
class StatusTest(CloudBase):
    def test_status_kinds_and_labels(self):
        self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings)[0], "never")
        self.gs.update("1", cloud_timestamps={"saves": self.now - 300, "kaputt": "x"})
        kind, at, _ = cs.cloud_status("1", self.gs.get("1"), self.settings)
        self.assertEqual((kind, at), ("synced", self.now - 300))
        self.assertEqual(cs.status_label(kind, at, now=self.now), tr('Synchronisiert · {0}').format(tr('vor {0} Min.').format(5)))
        self.assertEqual(cs.ago(self.now - 30, self.now), tr('gerade eben'))
        self.assertEqual(cs.ago(self.now - 7200, self.now), tr('vor {0} Std.').format(2))
        self.assertEqual(cs.ago(self.now - 3 * 86400, self.now), tr('vor {0} Tagen').format(3))
        self.gs.update("1", cloud_saves="off")
        self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings)[0], "off")
        self.gs.update("1", cloud_saves="")
        self.settings.feature_cloud_saves = False
        self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings)[0], "off")
        self.gs.update("1", cloud_saves="on")
        self.assertTrue(cs.enabled_for(self.gs.get("1"), self.settings))
        cs.save_state("1", "quota", "zu groß")
        self.assertEqual(cs.cloud_status("1", self.gs.get("1"), self.settings), ("quota", mock.ANY, "zu groß"))
        self.assertIn("zu groß", cs.status_tooltip("quota", 0, "zu groß"))
        self.assertEqual(cs.status_label("conflict"), tr('Konflikt'))


# --------------------------------------------------------------------------- Launch-Hook
class HookTest(CloudBase):
    def setUp(self) -> None:
        super().setUp()
        self.hook = CloudSavesHook()
        self.statuses: list[str] = []
        self.ctx = self.launcher.make_context("1", None, status_cb=self.statuses.append)
        self.notes: list[str] = []
        self.launcher.ui_bridge = types.SimpleNamespace(ask=lambda *a: "ignore", notify=self.notes.append)

    def test_enabled_conditions(self):
        self.assertTrue(self.hook.enabled(self.ctx))
        self.assertFalse(self.hook.enabled(self.launcher.make_context("1", None, preview=True)))
        self.assertFalse(self.hook.enabled(self.launcher.make_context("1", None, prepare_only=True)))
        self.assertFalse(self.hook.enabled(self.launcher.make_context("2", None)))      # Linux-Version
        self.gs.update("1", cloud_saves="off")
        self.assertFalse(self.hook.enabled(self.launcher.make_context("1", None)))
        self.gs.update("1", cloud_saves="")
        self.api.logged_in = False
        self.assertFalse(self.hook.enabled(self.launcher.make_context("1", None)))

    def test_prepare_downloads_and_after_exit_uploads(self):
        calls = []

        def fake_sync(launcher, api, pid, mode, **kw):
            calls.append((pid, mode, kw["prefix"], kw["runner"]))
            kw["save_timestamps"]({"saves": 42.0})
            return cs.SyncResult(pid, "synced", locations=[cs.LocationResult("saves", uploaded=1)])
        with mock.patch.object(cs, "sync_game", side_effect=fake_sync):
            self.hook.prepare(self.ctx)
            self.hook.after_exit(self.ctx, 0, 5)
        self.assertEqual([c[1] for c in calls], ["download", "upload"])
        self.assertEqual(calls[0][2], self.ctx.prefix)
        self.assertEqual(self.ctx.gs.cloud_timestamps, {"saves": 42.0})
        self.assertEqual(self.gs.get("1").cloud_timestamps, {"saves": 42.0})
        self.assertEqual(self.notes, [tr('Synchronisiert in GOG Galaxy-Cloud')])
        self.assertIn("cloudsaves:upload", self.ctx.data)

    def test_failure_never_blocks_start(self):
        with mock.patch.object(cs, "sync_game", side_effect=cs.CloudError("offline")):
            self.hook.prepare(self.ctx)
        self.assertTrue(any("offline" in s for s in self.statuses))
        self.assertEqual(self.notes, [tr('Cloud-Synchronisation fehlgeschlagen')])

    def test_real_sync_through_hook(self):
        self.ctx.prefix, self.ctx.runner = self.prefix, "umu"
        self.cloud.add("saves/a.sav", b"AUS DER CLOUD", self.now - 100)
        with mock.patch.object(cs, "CloudStorage", lambda u, c, t, s=None: Real(u, c, t, self.cloud)):
            res = self.hook.sync(self.ctx, "download")
        self.assertEqual(res.state, "synced")
        self.assertEqual((self.saves / "a.sav").read_bytes(), b"AUS DER CLOUD")
        self.assertGreater(cs.stamp_of(self.ctx.gs.cloud_timestamps, "saves"), 0)
        self.assertTrue(any(tr('Synchronisiere Cloud-Spielstände …') == s for s in self.statuses))

    def test_registered(self):
        with mock.patch.dict(hooks._hooks, clear=True), mock.patch.object(hooks, "_loaded", False):
            import importlib
            import galaxy_linux.launch.hook_cloudsaves as mod
            importlib.reload(mod)
            self.assertIn("cloudsaves", [h.name for h in hooks.hooks()])
            self.assertEqual(hooks._hooks["cloudsaves"].order, 30)


class ExtensionPointTest(unittest.TestCase):
    def test_stats_column_and_module_loaded(self):
        from galaxy_linux.ui import prefs_registry, ui_registry
        ui_registry.register_game_section("t-cloud", lambda page: "kennzahl", column="stats", order=1)
        try:
            self.assertIn("t-cloud", [s.key for s in ui_registry.game_sections("stats")])
            self.assertEqual(ui_registry.game_sections("stats")[0].builder(None), "kennzahl")
        finally:
            ui_registry.unregister("t-cloud")
        self.assertIn("ext_cloudsaves", prefs_registry.extension_modules(["ext_cloudsaves", "util", "prefs_registry"]))


Real = cs.CloudStorage


if __name__ == "__main__":
    unittest.main()
