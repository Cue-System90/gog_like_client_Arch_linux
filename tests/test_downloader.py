"""Tests für den Chunk-Downloader mit einem lokalen Fake-CDN (ohne GOG-Konto)."""
from __future__ import annotations

import hashlib
import http.server
import os
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

import requests

from galaxy_linux.content.downloader import ChunkDownloader, DownloadError, FileJob, SfcSource, verify_existing_chunks
from galaxy_linux.content.manifest import Chunk, DepotFile, DepotLink


def make_chunk(data: bytes) -> tuple[Chunk, bytes]:
    comp = zlib.compress(data)
    return Chunk(hashlib.md5(data).hexdigest(), hashlib.md5(comp).hexdigest(), len(data), len(comp)), comp


class FakeCdn(http.server.ThreadingHTTPServer):
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.fail_once: set[str] = set()
        self.hits = 0
        super().__init__(("127.0.0.1", 0), FakeHandler)


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # still
        pass

    def do_GET(self):
        srv: FakeCdn = self.server  # type: ignore[assignment]
        srv.hits += 1
        md5 = self.path.rsplit("/", 1)[-1]
        if md5 in srv.fail_once:
            srv.fail_once.discard(md5)
            self.send_response(503)
            self.end_headers()
            return
        blob = srv.blobs.get(md5)
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeCdn()
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.srv.server_address[1]}/store"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def _file(self, path: str, parts: list[bytes], flags=()) -> DepotFile:
        chunks = []
        for p in parts:
            c, comp = make_chunk(p)
            self.srv.blobs[c.compressed_md5] = comp
            chunks.append(c)
        return DepotFile(path=path, chunks=chunks, flags=list(flags), md5=hashlib.md5(b"".join(parts)).hexdigest())

    def test_multi_chunk_download_resume_and_verify(self):
        data1 = os.urandom(300_000)
        data2 = os.urandom(200_000)
        f = self._file("Data/big.bin", [data1, data2])
        g = self._file("bin/game.exe", [b"MZ" + os.urandom(1000)], flags=("executable",))
        progress = []
        dl = ChunkDownloader(requests.Session, workers=3, progress_cb=lambda p: progress.append(p.done_bytes))
        dl.download([FileJob(f, self.endpoint), FileJob(g, self.endpoint)], self.root, directories=["empty/dir"],
                    links=[DepotLink("link_to_game", "bin/game.exe")])
        self.assertEqual((self.root / "Data/big.bin").read_bytes(), data1 + data2)
        self.assertTrue(os.access(self.root / "bin/game.exe", os.X_OK))
        self.assertTrue((self.root / "empty/dir").is_dir())
        self.assertEqual(os.readlink(self.root / "link_to_game"), "bin/game.exe")
        self.assertEqual(progress[-1], f.size + g.size)

        # Beschädigung: zweiter Chunk wird überschrieben → nur dieser Chunk wird neu geladen
        with open(self.root / "Data/big.bin", "r+b") as fh:
            fh.seek(len(data1) + 10)
            fh.write(b"XXXX")
        self.assertEqual(verify_existing_chunks(self.root / "Data/big.bin", f.chunks), [1])
        hits_before = self.srv.hits
        dl2 = ChunkDownloader(requests.Session, workers=2)
        dl2.download([FileJob(f, self.endpoint), FileJob(g, self.endpoint)], self.root)
        self.assertEqual(self.srv.hits - hits_before, 1)
        self.assertEqual((self.root / "Data/big.bin").read_bytes(), data1 + data2)

    def test_retry_on_server_error(self):
        f = self._file("a.txt", [b"hello world" * 100])
        self.srv.fail_once.add(f.chunks[0].compressed_md5)
        dl = ChunkDownloader(requests.Session, workers=1, retries=2)
        dl.download([FileJob(f, self.endpoint)], self.root)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"hello world" * 100)

    def test_bad_checksum_fails(self):
        f = self._file("b.txt", [b"abc" * 50])
        self.srv.blobs[f.chunks[0].compressed_md5] = zlib.compress(b"tampered")
        dl = ChunkDownloader(requests.Session, workers=1, retries=0)
        with self.assertRaises(DownloadError):
            dl.download([FileJob(f, self.endpoint)], self.root)

    def test_small_files_container(self):
        small1, small2 = b"installed\n", b'{"a":1}'
        container = b"\x00\x00\x00" + small1 + small2 + b"\xff"
        cchunk, ccomp = make_chunk(container)
        self.srv.blobs[cchunk.compressed_md5] = ccomp
        c1, _ = make_chunk(small1)
        c2, _ = make_chunk(small2)
        f1 = DepotFile("installed", [c1], sfc_ref=(3, len(small1)))
        f2 = DepotFile("cfg/x.json", [c2], sfc_ref=(3 + len(small1), len(small2)))
        dl = ChunkDownloader(requests.Session, workers=2)
        dl.download([FileJob(f1, self.endpoint, "depotA"), FileJob(f2, self.endpoint, "depotA")], self.root,
                    sfc_sources={"depotA": SfcSource(self.endpoint, [cchunk])})
        self.assertEqual((self.root / "installed").read_bytes(), small1)
        self.assertEqual((self.root / "cfg/x.json").read_bytes(), small2)

    def test_cancel(self):
        f = self._file("c.bin", [os.urandom(50_000) for _ in range(20)])
        ev = threading.Event()
        ev.set()
        dl = ChunkDownloader(requests.Session, workers=2, cancel=ev)
        from galaxy_linux.content.downloader import DownloadCancelled
        with self.assertRaises(DownloadCancelled):
            dl.download([FileJob(f, self.endpoint)], self.root)


if __name__ == "__main__":
    unittest.main()
