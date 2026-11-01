"""Paralleler, fortsetzbarer Chunk-Downloader für das Content-System v2.

Jede Datei besteht aus zlib-komprimierten Chunks (≤10 MiB), die per MD5 geprüft und
an ihrem Offset in eine vorab angelegte Datei geschrieben werden. Bereits vorhandene,
korrekte Chunks werden erkannt und übersprungen (Fortsetzen, Update, Prüfen).
"""
from __future__ import annotations

from ..i18n import tr

import hashlib
import logging
import os
import threading
import time
import zlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import requests

from .bandwidth import BLOCK, Throttle
from .manifest import Chunk, DepotFile, DepotLink

log = logging.getLogger(__name__)


class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


@dataclass
class Progress:
    phase: str = ""
    total_bytes: int = 0
    done_bytes: int = 0
    downloaded_bytes: int = 0
    speed: float = 0.0            # Netz-Bytes/s
    write_speed: float = 0.0      # geschriebene Bytes/s
    files_total: int = 0
    files_done: int = 0
    current: str = ""
    step: str = ""                # Phase wie in Galaxy (content/phases.py), z. B. "scanning", "downloading"

    @property
    def fraction(self) -> float:
        return min(1.0, self.done_bytes / self.total_bytes) if self.total_bytes else 0.0

    @property
    def eta_seconds(self) -> float | None:
        if self.write_speed <= 0 or self.total_bytes <= 0:
            return None
        return max(0.0, (self.total_bytes - self.done_bytes) / self.write_speed)


ProgressCallback = Callable[[Progress], None]


@dataclass
class FileJob:
    file: DepotFile
    endpoint: str          # Basis-URL des CDN; Chunk = {endpoint}/{md5[:2]}/{md5[2:4]}/{md5}
    sfc_key: str = ""      # Schlüssel des SmallFilesContainers (Depot-Manifest-ID), falls sfc_ref
    root: Path | None = None   # eigener Zielordner statt dest_root (Support-Dateien → gog-support/<id>)

    def dest(self, dest_root: Path) -> Path:
        return (self.root or dest_root) / self.file.path


@dataclass
class SfcSource:
    endpoint: str
    chunks: list[Chunk]


def chunk_url(endpoint: str, compressed_md5: str) -> str:
    return f"{endpoint}/{compressed_md5[:2]}/{compressed_md5[2:4]}/{compressed_md5}"


def _offsets(chunks: Iterable[Chunk]) -> list[int]:
    out, pos = [], 0
    for c in chunks:
        out.append(pos)
        pos += c.size
    return out


def verify_existing_chunks(path: Path, chunks: list[Chunk]) -> list[int]:
    """Gibt die Indizes der Chunks zurück, die (noch) nicht korrekt in der Datei liegen."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return list(range(len(chunks)))
    missing: list[int] = []
    offsets = _offsets(chunks)
    try:
        with open(path, "rb") as fh:
            for i, (c, off) in enumerate(zip(chunks, offsets)):
                if off + c.size > st.st_size:
                    missing.append(i)
                    continue
                fh.seek(off)
                h = hashlib.md5()
                remaining = c.size
                while remaining > 0:
                    block = fh.read(min(1 << 20, remaining))
                    if not block:
                        break
                    h.update(block)
                    remaining -= len(block)
                if remaining > 0 or h.hexdigest() != c.md5:
                    missing.append(i)
    except OSError:
        return list(range(len(chunks)))
    return missing


class ChunkDownloader:
    def __init__(self, session_factory: Callable[[], requests.Session], workers: int = 6,
                 progress_cb: ProgressCallback | None = None, cancel: threading.Event | None = None,
                 retries: int = 4, throttle: Throttle | None = None) -> None:
        self.session_factory = session_factory
        self.throttle = throttle
        self.workers = max(1, workers)
        self.progress_cb = progress_cb
        self.cancel = cancel or threading.Event()
        self.retries = retries
        self._local = threading.local()
        self._lock = threading.Lock()
        self._progress = Progress()
        self._last_emit = 0.0
        self._net_window: deque[tuple[float, int]] = deque()
        self._write_window: deque[tuple[float, int]] = deque()
        self.errors: list[str] = []

    # ------------------------------------------------------------------ Hilfen
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = self.session_factory()
            self._local.session = s
        return s

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise DownloadCancelled()

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.25:
            return
        self._last_emit = now
        for win in (self._net_window, self._write_window):
            while win and now - win[0][0] > 5.0:
                win.popleft()
        span_n = (now - self._net_window[0][0]) if len(self._net_window) > 1 else 0
        span_w = (now - self._write_window[0][0]) if len(self._write_window) > 1 else 0
        self._progress.speed = sum(b for _, b in self._net_window) / span_n if span_n > 0.5 else 0.0
        self._progress.write_speed = sum(b for _, b in self._write_window) / span_w if span_w > 0.5 else 0.0
        if self.progress_cb:
            self.progress_cb(self._progress)

    def _account(self, net_bytes: int, written_bytes: int, current: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            self._progress.downloaded_bytes += net_bytes
            self._progress.done_bytes += written_bytes
            if net_bytes:
                self._net_window.append((now, net_bytes))
            if written_bytes:
                self._write_window.append((now, written_bytes))
            if current:
                self._progress.current = current
            self._emit()

    def set_phase(self, phase: str, current: str = "", step: str = "") -> None:
        with self._lock:
            self._progress.phase = phase
            self._progress.current = current
            if step:
                self._progress.step = step
            self._emit(force=True)

    # ------------------------------------------------------------------ Netz
    def fetch_chunk(self, endpoint: str, chunk: Chunk) -> bytes:
        url = chunk_url(endpoint, chunk.compressed_md5)
        last = ""
        for attempt in range(self.retries + 1):
            self._check_cancel()
            try:
                limited = self.throttle is not None and self.throttle.rate() > 0
                r = self.session.get(url, timeout=(15, 120), stream=limited)
                if r.status_code in (401, 403):
                    raise DownloadError(tr('Zugriff verweigert (HTTP {0}) – Link abgelaufen?').format(r.status_code))
                if r.status_code != 200:
                    last = tr('HTTP {0}').format(r.status_code)
                    raise requests.HTTPError(last)
                if limited:
                    # Bandbreitenbegrenzung: in kleinen Blöcken lesen und jeden Block über den Token-Eimer freigeben
                    buf = bytearray()
                    for block in r.iter_content(BLOCK):
                        self._check_cancel()
                        self.throttle.consume(len(block), self.cancel)
                        buf.extend(block)
                    raw = bytes(buf)
                else:
                    raw = r.content
                if chunk.compressed_md5 and hashlib.md5(raw).hexdigest() != chunk.compressed_md5:
                    last = tr('MD5 der komprimierten Daten falsch')
                    raise ValueError(last)
                data = zlib.decompress(raw)
                if hashlib.md5(data).hexdigest() != chunk.md5:
                    last = tr('MD5 der entpackten Daten falsch')
                    raise ValueError(last)
                self._account(len(raw), 0)
                return data
            except DownloadError:
                raise
            except (requests.RequestException, ValueError, zlib.error) as exc:
                last = last or str(exc)
                log.debug("Chunk %s Versuch %d fehlgeschlagen: %s", chunk.compressed_md5[:10], attempt + 1, last)
                if attempt >= self.retries:
                    break
                time.sleep(min(10.0, 0.5 * (2 ** attempt)))
        raise DownloadError(tr('Chunk {0}… konnte nicht geladen werden: {1}').format(chunk.md5[:8], last))

    # ------------------------------------------------------------------ Hauptablauf
    def download(self, jobs: list[FileJob], dest_root: Path, sfc_sources: dict[str, SfcSource] | None = None,
                 directories: Iterable[str] = (), links: Iterable[DepotLink] = (),
                 verify: bool = True) -> Progress:
        sfc_sources = sfc_sources or {}
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        self.errors.clear()
        self.cancel.clear() if not self.cancel.is_set() else None

        with self._lock:
            self._progress = Progress(phase=tr('Dateien prüfen'), total_bytes=sum(j.file.size for j in jobs),
                                      files_total=len(jobs), step="scanning")
            self._emit(force=True)

        for d in directories:
            if d:
                (dest_root / d).mkdir(parents=True, exist_ok=True)

        # 1) Planen: welche Chunks fehlen?
        chunk_jobs: list[tuple[FileJob, int, int]] = []   # (job, chunk-index, offset)
        sfc_jobs: list[FileJob] = []
        verified_bytes = 0
        for n, job in enumerate(jobs):
            self._check_cancel()
            f = job.file
            dest = job.dest(dest_root)
            if f.sfc_ref:
                if dest.exists() and verify and not verify_existing_chunks(dest, f.chunks):
                    verified_bytes += f.size
                    with self._lock:
                        self._progress.files_done += 1
                else:
                    sfc_jobs.append(job)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            missing = verify_existing_chunks(dest, f.chunks) if (verify and dest.exists()) else list(range(len(f.chunks)))
            if not f.chunks:
                dest.touch()
                with self._lock:
                    self._progress.files_done += 1
                continue
            offsets = _offsets(f.chunks)
            if missing:
                # Datei auf Zielgröße bringen (ohne vorhandene Daten zu verwerfen)
                flags = os.O_RDWR | os.O_CREAT
                fd = os.open(dest, flags, 0o644)
                try:
                    if os.fstat(fd).st_size != f.size:
                        os.ftruncate(fd, f.size)
                finally:
                    os.close(fd)
                for i in missing:
                    chunk_jobs.append((job, i, offsets[i]))
                verified_bytes += sum(c.size for i, c in enumerate(f.chunks) if i not in set(missing))
            else:
                verified_bytes += f.size
                with self._lock:
                    self._progress.files_done += 1
            if n % 50 == 0:
                with self._lock:
                    self._progress.current = f.path
                    self._emit()
        with self._lock:
            self._progress.done_bytes = verified_bytes
            self._progress.phase = tr('Herunterladen')
            self._progress.step = "downloading"
            self._emit(force=True)
        log.info("Download-Plan: %d Dateien, %d Chunks zu laden, %d Bytes bereits gültig, %d SFC-Dateien, %d Worker",
                 len(jobs), len(chunk_jobs), verified_bytes, len(sfc_jobs), self.workers)
        t_dl = time.monotonic()

        # 2) Chunks parallel laden
        pending_per_file: dict[str, int] = {}
        for job, _, _ in chunk_jobs:
            pending_per_file[job.file.path] = pending_per_file.get(job.file.path, 0) + 1

        def work(job: FileJob, idx: int, offset: int) -> None:
            self._check_cancel()
            chunk = job.file.chunks[idx]
            data = self.fetch_chunk(job.endpoint, chunk)
            dest = job.dest(dest_root)
            fd = os.open(dest, os.O_WRONLY)
            try:
                view = memoryview(data)
                pos = 0
                while pos < len(view):
                    pos += os.pwrite(fd, view[pos:], offset + pos)
            finally:
                os.close(fd)
            with self._lock:
                pending_per_file[job.file.path] -= 1
                done = pending_per_file[job.file.path] == 0
                if done:
                    self._progress.files_done += 1
            self._account(0, len(data), job.file.path)

        if chunk_jobs:
            self._run_parallel(work, chunk_jobs)

        # 3) SmallFilesContainer-Dateien
        if sfc_jobs:
            self._write_sfc_files(sfc_jobs, dest_root, sfc_sources)

        # 4) Rechte & Links
        for job in jobs:
            if job.file.executable:
                p = job.dest(dest_root)
                try:
                    p.chmod(p.stat().st_mode | 0o111)
                except OSError:
                    pass
        for link in links:
            p = dest_root / link.path
            try:
                if p.is_symlink() or p.exists():
                    p.unlink()
                p.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link.target, p)
            except OSError as exc:
                log.warning("Symlink %s konnte nicht angelegt werden: %s", p, exc)

        with self._lock:
            self._progress.phase = tr('Fertig')
            self._progress.step = "finalizing"
            self._progress.done_bytes = self._progress.total_bytes
            self._emit(force=True)
        dt = max(0.001, time.monotonic() - t_dl)
        log.info("Download fertig: %d B Netz in %.1f s (%.1f MB/s), Fehler: %d", self._progress.downloaded_bytes, dt,
                 self._progress.downloaded_bytes / dt / 1e6, len(self.errors))
        if self.errors:
            raise DownloadError("; ".join(self.errors[:3]))
        return self._progress

    def _run_parallel(self, fn: Callable, items: list[tuple]) -> None:
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="gl-dl") as ex:
            futures: set[Future] = set()
            it = iter(items)
            def submit_next() -> bool:
                try:
                    args = next(it)
                except StopIteration:
                    return False
                futures.add(ex.submit(fn, *args))
                return True
            for _ in range(self.workers * 2):
                if not submit_next():
                    break
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    exc = fut.exception()
                    if exc is not None:
                        if isinstance(exc, DownloadCancelled):
                            self.cancel.set()
                        else:
                            self.errors.append(str(exc))
                            self.cancel.set()
                if self.cancel.is_set():
                    for f in futures:
                        f.cancel()
                    futures = {f for f in futures if not f.cancelled()}
                    if not futures:
                        break
                    continue
                while len(futures) < self.workers * 2 and submit_next():
                    pass
        if self.cancel.is_set():
            if self.errors:
                raise DownloadError("; ".join(self.errors[:3]))
            raise DownloadCancelled()

    def _write_sfc_files(self, sfc_jobs: list[FileJob], dest_root: Path, sfc_sources: dict[str, SfcSource]) -> None:
        by_key: dict[str, list[FileJob]] = {}
        for j in sfc_jobs:
            by_key.setdefault(j.sfc_key, []).append(j)
        for key, group in by_key.items():
            self._check_cancel()
            src = sfc_sources.get(key)
            if src is None:
                self.errors.append(tr('SmallFilesContainer {0} fehlt im Manifest').format(key))
                continue
            self.set_phase(tr('Herunterladen'), tr('kleine Dateien'))
            blob = bytearray()
            for c in src.chunks:
                blob.extend(self.fetch_chunk(src.endpoint, c))
            for j in group:
                off, size = j.file.sfc_ref  # type: ignore[misc]
                data = bytes(blob[off:off + size])
                want = j.file.chunks[0].md5 if j.file.chunks else None
                if want and hashlib.md5(data).hexdigest() != want:
                    self.errors.append(tr('Prüfsumme falsch für {0}').format(j.file.path))
                    continue
                dest = j.dest(dest_root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                with self._lock:
                    self._progress.files_done += 1
                self._account(0, len(data), j.file.path)
