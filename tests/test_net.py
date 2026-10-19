"""Netz-Tests gegen öffentliche GOG-Endpunkte (ohne Konto). Aktivieren mit GALAXY_LINUX_NET_TESTS=1."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from galaxy_linux.api.client import GogApi
from galaxy_linux.api.auth import CredentialStore
from galaxy_linux.content.downloader import ChunkDownloader, FileJob, SfcSource
from galaxy_linux.content.manifest import Build, BuildManifest, DepotManifest, parse_dependency_repository

NET = os.environ.get("GALAXY_LINUX_NET_TESTS") == "1"


@unittest.skipUnless(NET, "Netz-Tests deaktiviert")
class PublicEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.api = GogApi(CredentialStore())
        self.api.creds = None  # garantiert ohne Login

    def test_builds_and_manifest(self):
        builds = [Build.from_api(b) for b in self.api.builds("1207658924", "windows")]
        self.assertTrue(any(b.is_v2 for b in builds))
        b = next(b for b in builds if b.is_v2)
        m = BuildManifest.parse(self.api.zlib_json(b.link))
        self.assertEqual(m.base_product_id, "1207658924")
        self.assertTrue(m.depots)
        dm = DepotManifest.parse(self.api.depot_manifest(m.depots[0].manifest), m.base_product_id, m.depots[0].manifest)
        self.assertTrue(dm.files)

    def test_download_dependency_depot(self):
        """Lädt das kleine MSVC2005-Redistributable komplett über das Chunk-System – prüft URL-Schema und MD5."""
        repo = parse_dependency_repository(self.api.dependency_repository())
        dep = repo["MSVC2005"]
        endpoint = self.api.dependency_link()
        self.assertTrue(endpoint.startswith("http"))
        dm = DepotManifest.parse(self.api.dependency_depot_manifest(dep.manifest), "", dep.manifest)
        jobs = [FileJob(f, endpoint, dep.manifest if f.sfc_ref else "") for f in dm.files]
        sfc = {dep.manifest: SfcSource(endpoint, dm.small_files_container)} if dm.small_files_container else {}
        with tempfile.TemporaryDirectory() as td:
            dl = ChunkDownloader(self.api.new_session, workers=4)
            prog = dl.download(jobs, Path(td), sfc, dm.directories, dm.links)
            exe = Path(td) / dep.executable_path
            self.assertTrue(exe.exists(), f"{exe} fehlt")
            self.assertEqual(prog.files_done, len(jobs))
            self.assertGreater(prog.downloaded_bytes, 1_000_000)
            # zweiter Lauf: alles verifiziert, nichts geladen
            dl2 = ChunkDownloader(self.api.new_session, workers=2)
            prog2 = dl2.download(jobs, Path(td), sfc, verify=True)
            self.assertEqual(prog2.downloaded_bytes, 0)

    def test_gamesdb_and_product_meta(self):
        from galaxy_linux.api.models import GameMeta, LibraryEntry
        e = LibraryEntry(id="1207658924", title="The Witcher", image="//images-2.gog-statics.com/x")
        m = GameMeta.from_sources(e, self.api.gamesdb(e.id), self.api.product(e.id))
        self.assertIn("Witcher", m.title)
        self.assertTrue(m.vertical_cover.startswith("https://images.gog.com/"))
        self.assertTrue(m.background)
        self.assertTrue(m.developers)
        img = self.api.cached_image(m.square_icon)
        self.assertIsNotNone(img)
        self.assertGreater(img.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
