"""Tests für die nachträglich angelegten Installations-Datensätze (content/build_records.py) – ohne Netz,
mit Beispiel-Antworten des Content-Systems."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.api.client import ApiError
from galaxy_linux.content import build_records as br
from galaxy_linux.content.manifest import Build, BuildManifest, parse_dependency_repository
from galaxy_linux.settings import InstalledGame

SECRET = "geheim-0123456789abcdef"

BUILDS = [
    {"build_id": "9999", "product_id": "100", "os": "windows", "branch": None, "version_name": "2.0", "tags": [],
     "public": True, "date_published": "2026-09-01T10:00:00+0000", "generation": 2, "link": "https://cdn/meta/new"},
    {"build_id": "8888", "branch": "beta", "version_name": "2.1 beta", "generation": 2, "link": "https://cdn/meta/beta"},
    {"build_id": "5555", "branch": None, "version_name": "1.0", "generation": 2, "link": "https://cdn/meta/old",
     "legacy_build_id": "51234"},
    {"build_id": "1111", "branch": None, "version_name": "0.9", "generation": 1, "link": "https://cdn/v1"},
]

MANIFEST = {
    "version": 2, "buildId": "5555", "baseProductId": "100", "installDirectory": "Demo", "platform": "windows",
    "clientId": "52095557858766887", "clientSecret": SECRET, "scriptInterpreter": True,
    "dependencies": ["MSVC2017", "ISI", "DirectX", "Unbekannt"],
    "products": [{"productId": "100", "name": "Demo"},
                 {"productId": "200", "name": "Demo DLC", "temp_executable": "support/setup_dlc.exe", "temp_arguments": "/x"}],
    "depots": [{"manifest": "aa", "productId": "100", "languages": ["*"], "size": 1, "compressedSize": 1}],
}

REPOSITORY = {"depots": [
    {"dependencyId": "MSVC2017", "readableName": "MS Visual C++ 2017 Redistributable", "manifest": "m1",
     "executable": {"path": "__redist\\MSVC2017\\VC_redist.x86.exe", "arguments": "/install /quiet /norestart"}},
    {"dependencyId": "ISI", "readableName": "ISI", "manifest": "m2", "internal": True,
     "executable": {"path": "__redist/ISI/scriptinterpreter.exe", "arguments": ""}},
    {"dependencyId": "DirectX", "readableName": "DirectX Jun 2010", "manifest": "m3",
     "executable": {"path": "__redist/DirectX/DXSETUP.exe", "arguments": "/silent"}},
]}


class FakeApi:
    def __init__(self, builds=None, manifest=None, fail=False):
        self._builds = BUILDS if builds is None else builds
        self._manifest = manifest or MANIFEST
        self.fail = fail
        self.calls: list[tuple] = []

    def builds(self, pid, platform="windows"):
        self.calls.append(("builds", pid, platform))
        if self.fail:
            raise ApiError("offline")
        return list(self._builds)

    def zlib_json(self, url):
        self.calls.append(("zlib", url))
        return json.loads(json.dumps(self._manifest))

    def dependency_repository(self):
        self.calls.append(("repo",))
        return REPOSITORY


class BuildRecordTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.manifests = root / "manifests"
        self.patch = mock.patch.multiple("galaxy_linux.paths", MANIFEST_DIR=self.manifests)
        self.patch.start()
        self.repo_patch = mock.patch.object(br, "_repo_cache", None)
        self.repo_patch.start()
        self.dir = root / "Games" / "Demo"
        self.dir.mkdir(parents=True)
        (self.dir / "goggame-100.info").write_text(json.dumps({
            "gameId": "100", "rootGameId": "100", "name": "Demo", "buildId": "5555", "clientId": "52095557858766887",
            "languages": ["de-DE", "en-US"], "playTasks": []}), encoding="utf-8")
        (self.dir / "goggame-200.info").write_text(json.dumps({"gameId": "200", "rootGameId": "100", "name": "Demo DLC"}))
        (self.dir / "goggame-300.info").write_text(json.dumps({"gameId": "300", "rootGameId": "999", "name": "Fremd"}))
        self.game = InstalledGame("100", "Demo", "windows", str(self.dir), method="imported")

    def tearDown(self):
        self.repo_patch.stop()
        self.patch.stop()
        self.td.cleanup()

    def test_read_install_info(self):
        info = br.read_install_info(self.dir, "100")
        self.assertEqual(info["build_id"], "5555")
        self.assertEqual(info["client_id"], "52095557858766887")
        self.assertEqual(info["language"], "de-DE")
        self.assertEqual(info["products"], ["100", "200"])

    def test_id_file_and_game_subdir(self):
        other = Path(self.td.name) / "Alt"
        (other / "game").mkdir(parents=True)
        (other / "game" / "goggame-100.info").write_text(json.dumps({"gameId": "100", "language": "English"}))
        (other / "goggame-100.id").write_text(json.dumps({"buildId": "51234"}))
        info = br.read_install_info(other, "100")
        self.assertEqual((info["build_id"], info["language"], info["products"]), ("51234", "English", ["100"]))

    def test_pick_build(self):
        builds = [Build.from_api(b) for b in BUILDS]
        self.assertEqual(br.pick_build(builds, "5555").build_id, "5555")
        self.assertEqual(br.pick_build(builds, "51234").build_id, "5555")        # legacy_build_id (v1-Kennung)
        self.assertEqual(br.pick_build(builds, "4444").build_id, "9999")         # unbekannt → neuester öffentlicher
        self.assertEqual(br.pick_build(builds).build_id, "9999")
        self.assertEqual(br.pick_build(builds, "8888").build_id, "8888")
        self.assertIsNone(br.pick_build([Build.from_api(BUILDS[3])]))

    def test_redist_entries(self):
        repo = parse_dependency_repository(REPOSITORY)
        redist, names = br.redist_entries(MANIFEST["dependencies"], repo, {"ISI"})
        self.assertEqual([r["id"] for r in redist], ["MSVC2017", "DirectX"])
        self.assertEqual(redist[0], {"id": "MSVC2017", "name": "MS Visual C++ 2017 Redistributable",
                                     "path": "__redist/MSVC2017/VC_redist.x86.exe", "arguments": "/install /quiet /norestart"})
        self.assertEqual(names, ["MS Visual C++ 2017 Redistributable", "DirectX Jun 2010"])

    def test_manifest_support_entries(self):
        m = BuildManifest.parse(MANIFEST)
        self.assertEqual(m.support, [{"product_id": "200", "name": "Demo DLC", "temp_executable": "support/setup_dlc.exe",
                                      "temp_arguments": "/x"}])
        self.assertTrue(m.script_interpreter)

    def test_ensure_record_creates_installer_format(self):
        api = FakeApi()
        with self.assertLogs("galaxy_linux.content.build_records", "INFO") as logs:
            rec = br.ensure_record(api, self.game)
        self.assertNotIn(SECRET, "\n".join(logs.output))                       # Client-Secret nie im Protokoll
        self.assertEqual(api.calls[:2], [("builds", "100", "windows"), ("zlib", "https://cdn/meta/old")])
        stored = json.loads((self.manifests / "100.json").read_text())
        self.assertEqual(stored, rec)
        for key in ("product_id", "build_id", "platform", "language", "install_dir", "files", "redist", "dependencies",
                    "client_id", "client_secret", "dlcs", "updated_at"):     # Felder wie InstallManager.install()
            self.assertIn(key, rec)
        self.assertEqual(rec["build_id"], "5555")
        self.assertEqual(rec["client_id"], "52095557858766887")
        self.assertEqual(rec["client_secret"], SECRET)
        self.assertEqual([r["id"] for r in rec["redist"]], ["MSVC2017", "DirectX"])           # ISI ist intern
        self.assertEqual(rec["dependency_ids"], ["MSVC2017", "ISI", "DirectX", "Unbekannt"])
        self.assertEqual(rec["dependencies"], ["MS Visual C++ 2017 Redistributable", "DirectX Jun 2010"])
        self.assertTrue(rec["script_interpreter"])
        self.assertEqual(rec["products"], ["100", "200"])
        self.assertEqual(rec["dlcs"], ["200"])
        self.assertEqual(rec["support"][0]["temp_executable"], "support/setup_dlc.exe")
        self.assertEqual(rec["language"], "de-DE")
        self.assertEqual(rec["origin"], br.ORIGIN)
        self.assertEqual(rec["version_name"], "1.0")
        self.assertTrue(br.is_complete(rec))
        s = br.summary(self.game, rec)
        self.assertNotIn(SECRET, json.dumps(s))
        self.assertTrue(s["complete"])

    def test_existing_complete_record_is_reused(self):
        (self.manifests).mkdir(parents=True)
        old = {"product_id": "100", "build_id": "1", "client_id": "c", "client_secret": "s", "redist": [], "files": ["a"]}
        (self.manifests / "100.json").write_text(json.dumps(old))
        api = FakeApi()
        self.assertEqual(br.ensure_record(api, self.game), old)
        self.assertEqual(api.calls, [])
        rec = br.ensure_record(api, self.game, force=True)                  # neu anlegen, Dateiliste bleibt
        self.assertEqual(rec["build_id"], "5555")
        self.assertEqual(rec["files"], ["a"])

    def test_incomplete_record_is_completed(self):
        self.manifests.mkdir(parents=True)
        (self.manifests / "100.json").write_text(json.dumps({"product_id": "100", "build_id": "", "redist": [], "files": ["x"]}))
        rec = br.ensure_record(FakeApi(), self.game)
        self.assertTrue(br.is_complete(rec))
        self.assertEqual(rec["files"], ["x"])

    def test_unknown_build_uses_newest(self):
        (self.dir / "goggame-100.info").write_text(json.dumps({"gameId": "100", "buildId": "4444"}))
        api = FakeApi(manifest=dict(MANIFEST, buildId="9999"))
        with self.assertLogs("galaxy_linux.content.build_records", "INFO"):
            rec = br.ensure_record(api, self.game)
        self.assertEqual(rec["build_id"], "9999")
        self.assertIn(("zlib", "https://cdn/meta/new"), api.calls)

    def test_network_error_keeps_existing(self):
        with self.assertLogs("galaxy_linux.content.build_records", "WARNING"):
            self.assertIsNone(br.ensure_record(FakeApi(fail=True), self.game))
        self.assertFalse((self.manifests / "100.json").exists())

    def test_no_v2_build(self):
        with self.assertLogs("galaxy_linux.content.build_records", "INFO"):
            self.assertIsNone(br.ensure_record(FakeApi(builds=[BUILDS[3]]), self.game))

    def test_not_for_external_or_linux(self):
        api = FakeApi()
        self.assertIsNone(br.ensure_record(api, InstalledGame("ext-x", "X", "windows", str(self.dir), method="external")))
        self.assertIsNone(br.ensure_record(api, InstalledGame("100", "Demo", "linux", str(self.dir), method="imported")))
        self.assertEqual(api.calls, [])

    def test_repository_is_cached(self):
        api = FakeApi()
        br.ensure_record(api, self.game)
        br.ensure_record(api, self.game, force=True)
        self.assertEqual(api.calls.count(("repo",)), 1)


if __name__ == "__main__":
    unittest.main()
