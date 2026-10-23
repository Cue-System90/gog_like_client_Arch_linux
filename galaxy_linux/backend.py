"""Gemeinsame Fassade für UI und CLI: Einstellungen, API, Installer, Launcher, Job-Warteschlange."""
from __future__ import annotations

from .i18n import tr

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import paths
from .api.auth import CredentialStore
from .api.client import ApiError, GogApi, NotLoggedIn
from .api.models import GameMeta, LibraryEntry
from .content.downloader import DownloadCancelled, Progress
from .content import external, scanner
from .content.installer import InstallError, InstallManager, InstallPlan, NoGalaxyBuild, safe_dirname
from .content.manifest import Build
from .launch import hooks as launch_hooks, shortcuts
from .launch.launcher import LaunchError, Launcher, PlayTask, RunningGame
from .settings import (EXTERNAL_LIBRARY_ID, VIEW_KEYS, CustomLibrary, GameSettingsStore, GlobalSettings, InstalledGame,
                       InstalledRegistry, PlaytimeStore)

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    imported: list[InstalledGame] = field(default_factory=list)   # GOG- und externe Spiele
    copying: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    prefix_jobs: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.imported or self.removed)


# --------------------------------------------------------------------------- Erweiterungspunkt Auftragsarten
@dataclass
class JobKind:
    """Eigene Auftragsart für die Download-/Job-Warteschlange (z. B. Cloud-Abgleich, Prefix-Einrichtung).
    ``run(backend, job, on_progress, status)`` läuft im Job-Thread; Rückgabe landet in ``job.result``;
    ``InstallError``/``ApiError``/``OSError``/``ValueError`` gelten als „Fehlgeschlagen“, ``DownloadCancelled`` als
    „Abgebrochen“ (``job.cancel`` regelmäßig prüfen)."""
    kind: str
    label: str
    run: Callable[["Backend", "Job", Callable[[Progress], None], Callable[[str], None]], Any]
    network: bool = False         # braucht GOG-Zugang → „Ausloggen“ bricht ihn ab


JOB_KINDS: dict[str, JobKind] = {}
NETWORK_JOB_KINDS = {"install", "install-linux", "install-offline", "update", "verify"}
# nach jedem beendeten Auftrag (fertig, fehlgeschlagen, abgebrochen) im Job-Thread: fn(backend, job)
_job_finished_hooks: list[Callable[["Backend", "Job"], None]] = []


def register_job_kind(kind: str, label: str, run: Callable[..., Any], network: bool = False) -> JobKind:
    jk = JobKind(kind, label, run, network)
    JOB_KINDS[kind] = jk
    return jk


def on_job_finished(fn: Callable[["Backend", "Job"], None]) -> Callable[["Backend", "Job"], None]:
    """Rückruf nach jedem beendeten Auftrag anmelden (z. B. Installations-Datensatz, Cloud-Download nach Install)."""
    if fn not in _job_finished_hooks:
        _job_finished_hooks.append(fn)
    return fn


def is_network_job(kind: str) -> bool:
    jk = JOB_KINDS.get(kind)
    return kind in NETWORK_JOB_KINDS or bool(jk and jk.network)


# vor Änderungen an den Spieldateien („verify“, „update“, „move“, „uninstall“): fn(backend, product_id, action, status)
# – z. B. GOverlay: bgmod-Änderungen im Spielordner zurücknehmen (launch/hook_goverlay.py)
_files_change_hooks: list[Callable[..., Any]] = []


def on_before_files_change(fn: Callable[..., Any]) -> Callable[..., Any]:
    if fn not in _files_change_hooks:
        _files_change_hooks.append(fn)
    return fn


@dataclass
class Job:
    kind: str                     # install | update | verify | install-linux | install-offline | move | prefix | JOB_KINDS
    product_id: str
    title: str
    platform: str = "windows"
    language: str = ""
    install_root: str = ""
    build_id: str = ""
    include_dlcs: bool = True
    shortcut_menu: bool = False
    shortcut_desktop: bool = False
    state: str = "queued"         # queued | running | done | failed | cancelled
    status: str = tr('Wartet …')
    progress: Progress = field(default_factory=Progress)
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    on_progress: Callable[["Job"], None] | None = None
    on_done: Callable[["Job"], None] | None = None
    result: Any = None            # InstalledGame bei Installationen, sonst Rückgabe von JobKind.run
    plan: InstallPlan | None = None
    data: dict[str, Any] = field(default_factory=dict)   # Angaben für eigene Auftragsarten

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    @property
    def label(self) -> str:
        return {"install": tr('Installieren'), "update": tr('Aktualisieren'), "verify": tr('Dateien prüfen'), "move": tr('Verschieben'),
                "prefix": tr('Prefix anlegen'),
                "install-linux": tr('Installieren (Linux)'), "install-offline": tr('Installieren (Offline-Installer)')}.get(
                    self.kind, JOB_KINDS[self.kind].label if self.kind in JOB_KINDS else self.kind)


class JobQueue:
    """Führt Installationen nacheinander aus (wie Galaxy), parallelisiert innerhalb eines Jobs."""

    def __init__(self, backend: "Backend") -> None:
        self.backend = backend
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.listeners: list[Callable[[Job], None]] = []
        self._thread = threading.Thread(target=self._run, name="gl-jobs", daemon=True)
        self._thread.start()

    def add(self, job: Job) -> Job:
        with self._lock:
            self.jobs.append(job)
        self._notify(job)
        self._wake.set()
        return job

    def run_now(self, job: Job) -> Job:
        """Auftrag sofort im aufrufenden (Arbeits-)Thread ausführen – an der Warteschlange vorbei, aber in der
        Download-Ansicht sichtbar (z. B. Galaxy-Redistributables vor einem Spielstart, während ein Download läuft)."""
        job.state = "running"            # die Warteschlange nimmt nur „queued“-Aufträge
        with self._lock:
            self.jobs.append(job)
        self._execute(job)
        return job

    def active_for(self, product_id: str) -> Job | None:
        with self._lock:
            for j in self.jobs:
                if j.product_id == str(product_id) and j.active:
                    return j
        return None

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self.jobs if j.active]

    def cancel(self, job: Job) -> None:
        job.cancel.set()
        if job.state == "queued":
            job.state = "cancelled"
            job.status = tr('Abgebrochen')
            self._notify(job)

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.active]

    def _notify(self, job: Job) -> None:
        for cb in list(self.listeners):
            try:
                cb(job)
            except Exception:  # pragma: no cover
                log.exception("Job-Listener fehlgeschlagen")
        if job.on_progress:
            try:
                job.on_progress(job)
            except Exception:  # pragma: no cover
                log.exception("on_progress fehlgeschlagen")

    def _next(self) -> Job | None:
        with self._lock:
            for j in self.jobs:
                if j.state == "queued":
                    return j
        return None

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._execute(job)

    def _execute(self, job: Job) -> None:
        if job.cancel.is_set():
            job.state, job.status = "cancelled", tr('Abgebrochen')
            self._notify(job)
            return
        job.state, job.status = "running", tr('Vorbereiten …')
        log.info("Job %s gestartet: %s (%s) platform=%s lang=%s build=%s dlcs=%s", job.id, job.kind, job.title,
                 job.platform, job.language or "-", job.build_id or "latest", job.include_dlcs)
        self._notify(job)
        t_start = time.monotonic()
        last = [0.0]

        def on_progress(p: Progress) -> None:
            job.progress = p
            now = time.monotonic()
            if now - last[0] >= 0.2 or p.phase == tr('Fertig'):
                last[0] = now
                job.status = p.phase or job.status
                self._notify(job)

        def status(text: str) -> None:
            job.status = text
            self._notify(job)

        inst = self.backend.installer
        try:
            if job.kind in ("update", "verify", "move"):
                self.backend.before_files_change(job.product_id, job.kind, status)
            if job.kind == "install":
                try:
                    plan = inst.plan(job.product_id, job.title, job.platform, job.language or None,
                                     build_id=job.build_id or None, install_root=job.install_root or None,
                                     include_dlcs=job.include_dlcs, status=status)
                except NoGalaxyBuild:
                    status(tr('Kein Galaxy-Build – nutze Offline-Installer …'))
                    job.kind = "install-offline"
                    job.result = inst.install_offline_windows(job.product_id, job.title, job.language or None,
                                                              job.install_root or None, on_progress, job.cancel, status)
                    plan = None
                if plan is not None:
                    job.plan = plan
                    status(tr('Herunterladen ({0} Dateien)').format(plan.file_count))
                    job.result = inst.install(plan, on_progress, job.cancel)
            elif job.kind == "install-linux":
                job.result = inst.install_linux_native(job.product_id, job.title, job.language or None,
                                                       job.install_root or None, on_progress, job.cancel, status)
            elif job.kind == "install-offline":
                job.result = inst.install_offline_windows(job.product_id, job.title, job.language or None,
                                                          job.install_root or None, on_progress, job.cancel, status)
            elif job.kind == "update":
                job.result = inst.update(job.product_id, on_progress, job.cancel, status, build_id=job.build_id or None)
            elif job.kind == "verify":
                job.result = inst.verify(job.product_id, on_progress, job.cancel, status)
            elif job.kind == "move":
                job.result = inst.move(job.product_id, job.install_root, on_progress, job.cancel, status)
            elif job.kind == "prefix":
                self.backend.create_game_prefix(job.product_id, status)
            elif job.kind in JOB_KINDS:
                job.result = JOB_KINDS[job.kind].run(self.backend, job, on_progress, status)
            else:
                raise InstallError(tr('Unbekannter Job: {0}').format(job.kind))
            if job.kind.startswith("install") and (job.shortcut_menu or job.shortcut_desktop):
                status(tr('Verknüpfungen anlegen …'))
                try:
                    self.backend.create_shortcuts(job.product_id, job.shortcut_menu, job.shortcut_desktop)
                except Exception as exc:  # pragma: no cover
                    log.warning("Verknüpfungen konnten nicht angelegt werden: %s", exc)
            job.state, job.status = "done", tr('Fertig')
            self.backend.updates.pop(job.product_id, None)
            p = job.progress
            log.info("Job %s fertig nach %.0f s: %s · %s geschrieben, %s geladen, %d/%d Dateien", job.id,
                     time.monotonic() - t_start, job.title, p.done_bytes, p.downloaded_bytes, p.files_done, p.files_total)
        except DownloadCancelled:
            job.state, job.status = "cancelled", tr('Abgebrochen')
            log.info("Job %s abgebrochen nach %.0f s", job.id, time.monotonic() - t_start)
        except (InstallError, ApiError, NotLoggedIn, OSError, ValueError) as exc:
            job.state, job.status, job.error = "failed", tr('Fehlgeschlagen'), str(exc)
            log.warning("Job %s fehlgeschlagen nach %.0f s: %s", job.id, time.monotonic() - t_start, exc, exc_info=True)
        except Exception as exc:  # pragma: no cover
            job.state, job.status, job.error = "failed", tr('Fehlgeschlagen'), f"{type(exc).__name__}: {exc}"
            log.exception("Job %s abgestürzt", job.id)
        self._notify(job)
        for fn in list(_job_finished_hooks):
            try:
                fn(self.backend, job)
            except Exception:
                log.exception("Rückruf nach Auftrag %s fehlgeschlagen", job.id)
        if job.on_done:
            try:
                job.on_done(job)
            except Exception:  # pragma: no cover
                log.exception("on_done fehlgeschlagen")


class Backend:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self.settings = GlobalSettings.load()
        self.api = GogApi(CredentialStore(self.settings.use_keyring))
        self.game_settings = GameSettingsStore()
        self.registry = InstalledRegistry()
        self.playtime = PlaytimeStore()
        self.installer = InstallManager(self.api, self.settings, self.registry)
        self.launcher = Launcher(self.settings, self.game_settings, self.registry, self.playtime, self.api)
        self.library: list[LibraryEntry] = self.api.cached_library() or []
        self.updates: dict[str, Build] = {}
        self._meta: dict[str, GameMeta] = {}
        self._covers: dict[str, str] = {}
        self._meta_lock = threading.Lock()
        self.jobs = JobQueue(self)
        self._scan_lock = threading.Lock()
        self._assign_libraries()
        # Hook-Module (launch/hook_*.py) früh laden: sie melden auch Auftragsarten und Job-Rückrufe an
        launch_hooks.load_builtin_hooks()
        log.info("Einstellungen: install_dir=%s prefix_dir=%s compat=%s prefer_umu=%s lang=%s workers=%s redist=%s",
                 self.settings.install_dir, self.settings.prefix_dir, self.settings.default_compat_tool,
                 self.settings.prefer_umu, self.settings.language, self.settings.max_workers, self.settings.install_redist)
        for lib in self.settings.libraries:
            log.info("Bibliothek %s (%s): Spiele %s · Prefixe %s · %d Spiele", lib.name, lib.id, lib.games_dir, lib.prefix_dir,
                     len(self.library_games(lib.id)))
        log.info("Angemeldet: %s · Bibliothek im Cache: %d · installiert: %d", self.api.logged_in, len(self.library),
                 len(self.registry.all()))
        for t in self.launcher.tools():
            log.info("Kompatibilitätstool: %s [%s, %s, runtime=%s]", t.name, t.kind, t.source, t.requires_runtime or "-")

    # ------------------------------------------------------------------ Bibliothek
    def refresh_library(self, progress: Callable[[int, int], None] | None = None) -> list[LibraryEntry]:
        """Bibliothek vollständig neu von GOG laden (erzwungener Abgleich)."""
        self.sync_library("manual", force=True, progress=progress)
        return self.library

    def sync_library(self, reason: str = "manual", force: bool = False,
                     progress: Callable[[int, int], None] | None = None):
        """Bibliothek mit GOG abgleichen: userData.json, Prüfsummen-Vorfilter, versteckte Spiele, neue IDs (NEU)
        – siehe api/library_sync.py. Liefert ein ``SyncResult``; wirft NotLoggedIn/ApiError."""
        from .api import library_sync
        return library_sync.sync(self, reason, force, progress)

    def entry(self, product_id: str) -> LibraryEntry | None:
        pid = str(product_id)
        for e in self.library:
            if e.id == pid:
                return e
        inst = self.registry.get(pid)
        if inst:
            return LibraryEntry(id=pid, title=inst.title, works_on={inst.platform if inst.platform != "osx" else "mac": True})
        return None

    def visible_library(self) -> list[LibraryEntry]:
        out = []
        for e in self.library:
            gs = self.game_settings.get(e.id)
            if (e.is_hidden or gs.hidden) and not self.settings.show_hidden:
                continue
            out.append(e)
        return out

    def meta(self, product_id: str) -> GameMeta | None:
        pid = str(product_id)
        with self._meta_lock:
            if pid in self._meta:
                return self._meta[pid]
        g = self.registry.get(pid)
        if g is not None and g.method == "external":
            m = self._external_meta(g)
            with self._meta_lock:
                self._meta[pid] = m
            return m
        entry = self.entry(pid)
        if entry is None:
            return None
        try:
            m = self.api.game_meta(entry)
        except ApiError as exc:
            log.warning("Metadaten für %s nicht verfügbar: %s", pid, exc)
            m = GameMeta(id=pid, title=entry.title, vertical_cover=entry.image_url("_glx_vertical_cover.jpg"), os_list=entry.platforms)
        with self._meta_lock:
            self._meta[pid] = m
        return m

    # ------------------------------------------------------------------ Zustand
    def cover_url(self, product_id: str) -> str:
        """Echte Hochkant-Boxart. Die Bibliotheksbilder von GOG sind Querformat-Kacheln; deren
        „_glx_vertical_cover“-Variante ist nur ein Ausschnitt (Titel abgeschnitten). Deshalb GamesDB."""
        pid = str(product_id)
        if self.is_external(pid):
            m = self.meta(pid)
            return m.vertical_cover if m else ""
        with self._meta_lock:
            if pid in self._covers:
                return self._covers[pid]
        url = ""
        try:
            gdb = self.api.gamesdb(pid)
            if gdb:
                from .api.models import _img
                url = _img(gdb.get("vertical_cover") or (gdb.get("game") or {}).get("vertical_cover"), "_glx_vertical_cover")
        except Exception as exc:  # pragma: no cover
            log.debug("GamesDB-Cover für %s nicht verfügbar: %s", pid, exc)
        with self._meta_lock:
            self._covers[pid] = url
        return url

    def _external_meta(self, g: InstalledGame) -> GameMeta:
        from .api import covers
        title = g.title
        queries = [external.search_title(title)]
        stem = Path(g.executable or "").stem
        if len(stem) >= 4 and external._norm(title).startswith(external._norm(stem)):
            queries.append(stem)
        data = None
        try:
            data = covers.lookup(self.api.session, queries)
        except Exception as exc:  # pragma: no cover
            log.debug("Metadaten für %s nicht verfügbar: %s", title, exc)
        if data and data.get("source") == "gog":
            try:
                stub = LibraryEntry(id=data["id"], title=data["title"])
                m = GameMeta.from_sources(stub, self.api.gamesdb(data["id"]), self.api.product(data["id"]))
                m.id, m.title, m.os_list, m.dlc_ids = g.product_id, title, ["windows"], []
                m.vertical_cover = m.vertical_cover or data.get("cover", "")
                return m
            except Exception as exc:  # pragma: no cover
                log.debug("GOG-Metadaten für %s fehlgeschlagen: %s", title, exc)
        m = GameMeta(id=g.product_id, title=title, os_list=["windows"])
        if data:
            m.vertical_cover, m.background, m.logo = data.get("cover", ""), data.get("background", ""), data.get("logo", "")
            m.summary, m.genres = data.get("summary", ""), data.get("genres", [])
            m.developers, m.publishers = data.get("developers", []), data.get("publishers", [])
            m.release_date = data.get("release_date", "")
        return m

    def state(self, product_id: str) -> str:
        pid = str(product_id)
        if self.launcher.is_running(pid):
            return "running"
        if self.jobs.active_for(pid):
            return "busy"
        if self.registry.is_installed(pid):
            return "update" if pid in self.updates else "installed"
        return "not_installed"

    def check_updates(self) -> dict[str, Build]:
        found: dict[str, Build] = {}
        for g in self.registry.all():
            b = self.installer.check_update(g)
            if b:
                found[g.product_id] = b
        self.updates = found
        return found

    # ------------------------------------------------------------------ Aktionen
    def install(self, product_id: str, platform: str = "windows", language: str = "", install_root: str = "",
                build_id: str = "", include_dlcs: bool = True, shortcut_menu: bool | None = None,
                shortcut_desktop: bool | None = None, **cbs) -> Job:
        entry = self.entry(product_id)
        title = entry.title if entry else str(product_id)
        kind = "install-linux" if platform == "linux" else "install"
        job = Job(kind=kind, product_id=str(product_id), title=title, platform=platform, language=language,
                  install_root=install_root, build_id=build_id, include_dlcs=include_dlcs,
                  shortcut_menu=self.settings.shortcut_menu if shortcut_menu is None else shortcut_menu,
                  shortcut_desktop=self.settings.shortcut_desktop if shortcut_desktop is None else shortcut_desktop, **cbs)
        return self.jobs.add(job)

    # ------------------------------------------------------------------ Kopierte Spiele erkennen
    def install_roots(self) -> list[Path]:
        roots: list[Path] = []
        for r in [self.settings.install_dir, *self.settings.install_locations]:
            p = Path(os.path.expanduser(str(r)))
            if p not in roots:
                roots.append(p)
        return roots

    def _busy_dirs(self) -> list[Path]:
        """Zielordner laufender Installationen – die dürfen nicht als „kopiertes Spiel“ gelten."""
        out: list[Path] = []
        for j in self.jobs.active():
            if j.plan is not None:
                out.append(Path(j.plan.install_dir))
            elif j.kind.startswith("install"):
                out.append(self.installer.install_root(j.install_root or None) / safe_dirname(j.title))
        return out

    def is_external(self, product_id: str) -> bool:
        g = self.registry.get(str(product_id))
        return bool(g and g.method == "external") or external.is_external_id(str(product_id))

    # ------------------------------------------------------------------ Eigene Bibliotheken
    LIBRARY_KEYS = ("name", "games_dir", "prefix_dir", "compat_tool", "launch_options", "wayland_driver", "prefix_dpi",
                    "watch", "view")

    def libraries(self) -> list[CustomLibrary]:
        return list(self.settings.libraries)

    def get_library(self, lib_id: str) -> CustomLibrary | None:
        return self.settings.library(lib_id)

    def find_library(self, text: str) -> CustomLibrary:
        """Bibliothek nach ID oder Name (auch eindeutiger Namensteil) – für die Kommandozeile."""
        t = text.strip().lower()
        for lib in self.settings.libraries:
            if t in (lib.id.lower(), lib.name.lower()):
                return lib
        hits = [lib for lib in self.settings.libraries if t and t in lib.name.lower()]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(lib.name for lib in self.settings.libraries) or tr("keine")
        raise InstallError(tr('Bibliothek „{0}“ nicht gefunden{1} – vorhanden: {2}').format(text, tr(' (mehrdeutig)') if hits else '', names))

    def library_of(self, product_id: str) -> CustomLibrary | None:
        return self.launcher.library_of(str(product_id))

    def library_games(self, lib_id: str) -> list[InstalledGame]:
        out = []
        for g in self.registry.all():
            lib = self.launcher.library_of(g)
            if lib is not None and lib.id == lib_id:
                out.append(g)
        return out

    def _assign_libraries(self) -> None:
        """Externe Spiele aus 0.1.3/0.1.4 kennen noch keine Bibliothek: nach Ordner zuordnen, sonst der
        External-Bibliothek. IDs, Prefixe und Pro-Spiel-Einstellungen bleiben unverändert."""
        todo = [g for g in self.registry.all() if g.method == "external" and not g.library]
        if not todo or not self.settings.libraries:
            return
        backup = paths.INSTALLED_FILE.with_name(paths.INSTALLED_FILE.name + ".vor-bibliotheken")
        try:
            if paths.INSTALLED_FILE.exists() and not backup.exists():
                import shutil
                shutil.copy2(paths.INSTALLED_FILE, backup)
        except OSError:
            pass
        fallback = self.settings.library(EXTERNAL_LIBRARY_ID) or self.settings.libraries[0]
        for g in todo:
            g.library = (self.settings.library_for_path(g.install_path) or fallback).id
            self.registry.set(g)
        log.info("%d externe Spiele den Bibliotheken zugeordnet", len(todo))

    def _library_path(self, text: str) -> Path:
        return Path(os.path.expanduser(text.strip())).absolute()

    def _check_library_dirs(self, games: Path, prefixes: Path, skip_id: str = "") -> None:
        g, p = games.resolve(), prefixes.resolve()
        if g in (Path("/").resolve(), Path.home().resolve()) or len(g.parts) < 3:
            raise InstallError(tr('„{0}“ ist als Spiele-Ordner zu allgemein – bitte einen eigenen Ordner wählen.').format(games))
        if p == g:
            raise InstallError(tr('Spiele- und Prefix-Ordner müssen verschieden sein.'))
        if p in g.parents:
            raise InstallError(tr('Der Spiele-Ordner darf nicht im Prefix-Ordner liegen.'))
        if g == Path(os.path.expanduser(self.settings.prefix_dir)).resolve():
            raise InstallError(tr('Das ist der Prefix-Ordner der GOG-Spiele.'))
        for lib in self.settings.libraries:
            if lib.id == skip_id:
                continue
            other = lib.games_root.resolve()
            if g == other or other in g.parents or g in other.parents:
                raise InstallError(tr('Der Ordner überschneidet sich mit der Bibliothek „{0}“ ({1}).').format(lib.name, lib.games_dir))
            if g == lib.prefix_root.resolve() or lib.prefix_root.resolve() in g.parents:
                raise InstallError(tr('Der Ordner liegt im Prefix-Ordner der Bibliothek „{0}“.').format(lib.name))

    def _unique_library_id(self, name: str) -> str:
        base = external.slug(name)
        lid, n = base, 2
        while self.settings.library(lid) is not None:
            lid, n = f"{base}-{n}", n + 1
        return lid

    def _check_library_values(self, changes: dict) -> dict:
        out = {}
        for k, v in changes.items():
            if k not in self.LIBRARY_KEYS:
                raise InstallError(tr('Unbekannte Bibliothekseinstellung: {0}').format(k))
            if k == "wayland_driver":
                v = {"an": "on", "aus": "off", "default": "", "global": ""}.get(str(v).lower(), str(v).lower())
                if v not in ("", "on", "off"):
                    raise InstallError(tr('Wayland-Treiber: on, off oder leer (globale Vorgabe)'))
            elif k == "prefix_dpi":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise InstallError(tr('DPI muss eine Zahl sein')) from None
                if v < 0 or v > 960:
                    raise InstallError(tr('DPI zwischen 0 (globale Vorgabe) und 960'))
            elif k == "watch":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "ja", "yes", "on", "an")
            elif k == "view":
                if isinstance(v, str):
                    import json
                    try:
                        v = json.loads(v)
                    except ValueError:
                        v = None
                if not isinstance(v, dict):
                    raise InstallError(tr('Ansicht als JSON angeben, z. B. {"layout": "list", "sort": 1}'))
                try:
                    v = {kk: (int(vv) if kk == "sort" else bool(vv) if kk in ("titles", "sections") else str(vv))
                         for kk, vv in v.items() if kk in VIEW_KEYS}
                except (TypeError, ValueError):
                    raise InstallError(tr('Sortierung muss eine Zahl sein (0–3)')) from None
            elif k == "compat_tool":
                v = "" if str(v) in ("", "auto", "default") else str(v)
            elif k == "launch_options":
                from .launch import launch_options
                launch_options.parse(str(v))          # Syntax früh prüfen
                v = str(v).strip()
            else:
                v = str(v).strip()
            out[k] = v
        return out

    def create_library(self, name: str, games_dir: str, prefix_dir: str = "", **defaults) -> CustomLibrary:
        """Neue Bibliothek: jeder Unterordner von games_dir wird ein Spiel, Prefixe liegen in prefix_dir
        (leer = <Spiele-Ordner>/prefixes). Der Spiele-Ordner wird bei Bedarf angelegt."""
        name = (name or "").strip()
        if not name:
            raise InstallError(tr('Bitte einen Namen für die Bibliothek angeben.'))
        if any(lib.name.lower() == name.lower() for lib in self.settings.libraries):
            raise InstallError(tr('Eine Bibliothek „{0}“ gibt es schon.').format(name))
        if not (games_dir or "").strip():
            raise InstallError(tr('Bitte den Spiele-Ordner der Bibliothek angeben.'))
        games = self._library_path(games_dir)
        prefixes = self._library_path(prefix_dir) if (prefix_dir or "").strip() else games / "prefixes"
        self._check_library_dirs(games, prefixes)
        values = self._check_library_values(defaults)
        try:
            games.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InstallError(tr('Spiele-Ordner kann nicht angelegt werden: {0}').format(exc)) from exc
        lib = CustomLibrary(id=self._unique_library_id(name), name=name, games_dir=str(games), prefix_dir=str(prefixes))
        for k, v in values.items():
            setattr(lib, k, v)
        self.settings.libraries.append(lib)
        self.settings.save()
        log.info("Bibliothek angelegt: %s (%s) · Spiele %s · Prefixe %s", lib.name, lib.id, lib.games_dir, lib.prefix_dir)
        return lib

    def update_library(self, lib_id: str, **changes) -> CustomLibrary:
        lib = self.get_library(lib_id)
        if lib is None:
            raise InstallError(tr('Bibliothek nicht gefunden.'))
        values = self._check_library_values(changes)
        if "name" in values:
            if not values["name"]:
                raise InstallError(tr('Der Name darf nicht leer sein.'))
            if any(o.id != lib.id and o.name.lower() == values["name"].lower() for o in self.settings.libraries):
                raise InstallError(tr('Eine Bibliothek „{0}“ gibt es schon.').format(values['name']))
        if "games_dir" in values or "prefix_dir" in values:
            games = self._library_path(values.get("games_dir") or lib.games_dir)
            prefixes = self._library_path(values.get("prefix_dir") or lib.prefix_dir)
            self._check_library_dirs(games, prefixes, skip_id=lib.id)
            if "games_dir" in values:
                values["games_dir"] = str(games)
            if "prefix_dir" in values:
                values["prefix_dir"] = str(prefixes)
        for k, v in values.items():
            setattr(lib, k, v)
        self.settings.save()
        with self._meta_lock:
            for g in self.library_games(lib.id):
                self._meta.pop(g.product_id, None)
        log.info("Bibliothek %s geändert: %s", lib.id, ", ".join(f"{k}={v}" for k, v in values.items()))
        return lib

    def rename_library(self, lib_id: str, name: str) -> CustomLibrary:
        return self.update_library(lib_id, name=name)

    def remove_library(self, lib_id: str) -> list[InstalledGame]:
        """Bibliothek löschen: ihre Spiele verschwinden aus dem Client, Spielordner und Prefixe bleiben liegen."""
        lib = self.get_library(lib_id)
        if lib is None:
            raise InstallError(tr('Bibliothek nicht gefunden.'))
        games = self.library_games(lib.id)
        busy = [g.title for g in games if self.launcher.is_running(g.product_id) or self.jobs.active_for(g.product_id)]
        if busy:
            raise InstallError(tr('Erst beenden bzw. abwarten: {0}').format(', '.join(busy[:3])))
        for g in games:
            self._forget(g.product_id)
        self.settings.libraries = [x for x in self.settings.libraries if x.id != lib.id]
        self.settings.save()
        log.info("Bibliothek gelöscht: %s (%s) – %d Spiele ausgetragen, Dateien und Prefixe bleiben", lib.name, lib.id, len(games))
        return games

    def _ignored(self) -> list[Path]:
        return [Path(os.path.expanduser(p)) for p in self.settings.ignored_paths]

    def _unique_external_id(self, folder_name: str, path: Path) -> str:
        base = external.external_id(folder_name)
        pid, n = base, 2
        while True:
            g = self.registry.get(pid)
            if g is None or Path(g.install_path).resolve() == path.resolve():
                return pid
            pid, n = f"{base}-{n}", n + 1

    def _register_gog(self, g: scanner.FoundGame, size: int) -> InstalledGame:
        game = InstalledGame(product_id=g.product_id, title=g.title, platform=g.platform, install_path=str(g.path),
                             build_id=g.build_id, language=g.language, installed_dlcs=list(g.dlc_ids),
                             install_size=size, method="imported", executable=g.executable)
        self.registry.set(game)
        log.info("Spiel übernommen: %s (%s, %s) aus %s, Build %s, DLCs %s", g.title, g.product_id, g.platform,
                 g.path, g.build_id or "-", g.dlc_ids or "-")
        return game

    def _register_external(self, path: Path, title: str, exe: str, size: int, library: str) -> InstalledGame:
        pid = self._unique_external_id(path.name, path)
        game = InstalledGame(product_id=pid, title=title, platform="windows", install_path=str(path),
                             install_size=size, method="external", executable=exe, library=library)
        self.registry.set(game)
        with self._meta_lock:
            self._meta.pop(pid, None)
        log.info("Externes Spiel übernommen: %s (%s) aus %s in Bibliothek %s, Startdatei %s", title, pid, path, library, exe)
        return game

    def _scan_library(self, lib: CustomLibrary, registered: dict[str, Path], exclude: list[Path]):
        """Wie scanner.scan, aber jeder Ordner ohne GOG-Daten mit startbarer .exe wird ein Spiel der Bibliothek."""
        gog_found, ext_found, unknown, missing = [], [], [], []
        root = lib.games_root
        reg_paths = {Path(p).resolve(): pid for pid, p in registered.items()}
        if root.is_dir():
            try:
                dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")
                              and d.name not in scanner.SKIP_NAMES)
            except OSError:
                dirs = []
            for d in dirs:
                if any(scanner._is_relative_to(d, e) for e in exclude) or d.resolve() in reg_paths:
                    continue
                g = scanner.game_in_dir(d)
                if g is not None:
                    if g.product_id not in registered:
                        gog_found.append(g)
                    continue
                e = external.game_in_dir(d)
                if e is not None:
                    e.library = lib.id
                (ext_found if e else unknown).append(e or d)
            if scanner._has_entries(root):
                for pid, pth in registered.items():
                    if not Path(pth).exists() and scanner._is_relative_to(Path(pth).parent, root):
                        missing.append(pid)
        # von Hand hinzugefügte Spiele außerhalb des Ordners: austragen, wenn ihr Ordner fehlt, der Elternordner aber da ist
        for g in self.library_games(lib.id):
            p = Path(g.install_path)
            if g.product_id not in missing and not p.exists() and scanner._has_entries(p.parent):
                missing.append(g.product_id)
        return gog_found, ext_found, unknown, missing

    def scan_install_dirs(self, create_prefixes: bool | None = None, wait: float = 4.0, auto: bool = False) -> ScanReport | None:
        """Spiele-Ordner (GOG) und die Ordner der eigenen Bibliotheken durchsuchen: neue Spiele übernehmen, gelöschte
        austragen, fehlende Prefixe anlegen. Von Hand entfernte Ordner (ignored_paths) bleiben außen vor.
        auto=True (Ordnerüberwachung): Bibliotheken mit abgeschalteter Überwachung werden übersprungen."""
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            report = ScanReport()
            registered = {g.product_id: Path(g.install_path) for g in self.registry.all()}
            libs = self.settings.libraries
            lib_roots = [lib.games_root for lib in libs]
            exclude = [Path(os.path.expanduser(self.settings.prefix_dir)), *[lib.prefix_root for lib in libs], paths.CACHE_DIR,
                       *self._busy_dirs(), *self._ignored()]
            lib_resolved = {r.resolve() for r in lib_roots}
            gog_roots = [r for r in self.install_roots() if r.resolve() not in lib_resolved]
            res = scanner.scan(gog_roots, registered, exclude + lib_roots)
            found, unknown, missing = list(res.found), list(res.unknown), list(res.missing)
            for lib in libs:
                if auto and not lib.watch:
                    continue
                gog_ext, ext_found, ext_unknown, ext_missing = self._scan_library(lib, registered, exclude)
                seen = {g.product_id for g in found if not isinstance(g, external.FoundExternal)}
                found += [g for g in gog_ext if g.product_id not in seen] + ext_found
                unknown += ext_unknown
                missing += [pid for pid in ext_missing if pid not in missing]
            stable, copying = scanner.split_stable(found, wait=wait)
            for g, size in stable:
                if isinstance(g, external.FoundExternal):
                    report.imported.append(self._register_external(g.path, g.title, g.executable, size, g.library))
                else:
                    report.imported.append(self._register_gog(g, size))
            report.copying = [g.title for g in copying]
            report.unknown = [str(p) for p in unknown]
            report.duplicates = [str(p) for p in res.duplicates]
            for pid in missing:
                g = self.registry.get(pid)
                if g is None or self.launcher.is_running(pid) or self.jobs.active_for(pid):
                    continue
                self._forget(pid)
                report.removed.append(g.title)
                log.info("Spiel ausgetragen (Ordner fehlt): %s – %s; Prefix bleibt erhalten", g.title, g.install_path)
            if self.settings.auto_create_prefixes if create_prefixes is None else create_prefixes:
                report.prefix_jobs = self.queue_missing_prefixes()
            if report.copying:
                log.info("Wird noch kopiert: %s", ", ".join(report.copying))
            return report
        finally:
            self._scan_lock.release()

    # ------------------------------------------------------------------ Spiele von Hand hinzufügen / entfernen
    def inspect_folder(self, path: str | Path) -> dict:
        """Vorschau für „Spiel hinzufügen“: GOG oder extern, Titel, Startdatei-Kandidaten, passende Bibliothek."""
        d = Path(os.path.expanduser(str(path)))
        if not d.is_dir():
            raise InstallError(tr('Ordner nicht gefunden: {0}').format(d))
        for g in self.registry.all():
            if Path(g.install_path).resolve() == d.resolve():
                raise InstallError(tr('„{0}“ ist bereits in der Bibliothek.').format(g.title))
        gog = scanner.game_in_dir(d)
        if gog is not None:
            return {"kind": "gog", "path": str(d), "title": gog.title, "product_id": gog.product_id, "candidates": [],
                    "library": ""}
        title = external.display_title(d.name)
        cands = [c.rel for c in external.exe_candidates(d, title, limit=30)]
        lib = self.settings.library_for_path(d)
        return {"kind": "external", "path": str(d), "title": title, "product_id": "", "candidates": cands,
                "library": lib.id if lib else ""}

    def add_game_manually(self, path: str | Path, title: str = "", exe: str = "",
                          create_prefix: bool | None = None, library: str = "") -> InstalledGame:
        """Spiel aus einem beliebigen Ordner aufnehmen. Externe Spiele kommen in die angegebene Bibliothek (sonst die,
        in deren Ordner sie liegen, sonst die erste) und bekommen ihren Prefix in deren Prefix-Ordner."""
        info = self.inspect_folder(path)
        d = Path(info["path"])
        size = scanner.tree_signature(d)[0]
        if info["kind"] == "gog":
            g = scanner.game_in_dir(d)
            if g.product_id in {x.product_id for x in self.registry.all()}:
                raise InstallError(tr('„{0}“ ist bereits an anderer Stelle installiert.').format(g.title))
            game = self._register_gog(g, size)
        else:
            lib = (self.find_library(library) if library else None) or self.get_library(info["library"]) \
                or (self.settings.libraries[0] if self.settings.libraries else None)
            if lib is None:
                raise InstallError(tr('Keine eigene Bibliothek vorhanden – bitte zuerst eine anlegen (Einstellungen → Bibliotheken).'))
            exe = exe or (info["candidates"][0] if info["candidates"] else "")
            if not exe or not (d / exe).is_file():
                raise InstallError(tr('Keine startbare .exe gefunden – bitte die Startdatei angeben.'))
            game = self._register_external(d, (title or info["title"]).strip(), exe, size, lib.id)
        self._unignore(d)
        if (self.settings.auto_create_prefixes if create_prefix is None else create_prefix) and game.platform == "windows":
            self.queue_prefix(game.product_id)
        return game

    def _unignore(self, d: Path) -> None:
        keep = [p for p in self.settings.ignored_paths if Path(os.path.expanduser(p)).resolve() != d.resolve()]
        if keep != self.settings.ignored_paths:
            self.settings.ignored_paths = keep
            self.settings.save()

    def _forget(self, pid: str) -> None:
        self.registry.remove(pid)
        self.updates.pop(pid, None)
        with self._meta_lock:
            self._meta.pop(pid, None)
        try:
            self.remove_shortcuts(pid)
        except Exception:  # pragma: no cover
            pass

    def _safe_to_delete(self, p: Path) -> bool:
        p = p.resolve()
        forbidden = {Path("/").resolve(), Path.home().resolve(), Path(os.path.expanduser(self.settings.prefix_dir)).resolve(),
                     *[r.resolve() for r in self.install_roots()],
                     *[lib.games_root.resolve() for lib in self.settings.libraries],
                     *[lib.prefix_root.resolve() for lib in self.settings.libraries]}
        return p not in forbidden and len(p.parts) >= 3

    def remove_game(self, product_id: str, delete_files: bool = False, delete_prefix: bool = False) -> None:
        """Spiel aus der Bibliothek entfernen. Ohne delete_files bleibt der Ordner liegen und wird bei der
        Ordnersuche nicht wieder aufgenommen (Ausschlussliste in den Einstellungen)."""
        pid = str(product_id)
        g = self.registry.get(pid)
        if g is None:
            raise InstallError(tr('Spiel ist nicht in der Bibliothek.'))
        if self.launcher.is_running(pid) or self.jobs.active_for(pid):
            raise InstallError(tr('Das Spiel läuft oder wird gerade bearbeitet.'))
        if delete_prefix:
            self.delete_prefix(pid)
        path = Path(g.install_path)
        if delete_files:
            if path.is_dir():
                if not self._safe_to_delete(path):
                    raise InstallError(tr('Ordner wird aus Sicherheitsgründen nicht gelöscht: {0}').format(path))
                self.before_files_change(pid, "uninstall")
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            try:
                self.installer.record_path(pid).unlink()
            except OSError:
                pass
            from .content.redist import remove_support
            remove_support(pid)
            if self.game_settings.get(pid).cloud_timestamps:     # Spielstände im Spielordner sind mit weg
                self.game_settings.update(pid, cloud_timestamps={})
        elif str(path) not in self.settings.ignored_paths:
            self.settings.ignored_paths.append(str(path))
            self.settings.save()
        self._forget(pid)
        log.info("Spiel entfernt: %s (Dateien %s, Prefix %s)", g.title, "gelöscht" if delete_files else "behalten",
                 "gelöscht" if delete_prefix else "behalten")

    def before_files_change(self, product_id: str, action: str, status: Callable[[str], None] | None = None) -> None:
        """Rückrufe vor Änderungen an den Spieldateien (``on_before_files_change``); Fehler werden nur protokolliert."""
        for fn in list(_files_change_hooks):
            try:
                fn(self, str(product_id), action, status)
            except Exception:
                log.exception("Rückruf vor „%s“ für %s fehlgeschlagen", action, product_id)

    def rename_external(self, product_id: str, title: str) -> None:
        g = self.registry.get(product_id)
        if g is None or not title.strip():
            return
        g.title = title.strip()
        self.registry.set(g)
        with self._meta_lock:
            self._meta.pop(product_id, None)

    def set_external_exe(self, product_id: str, exe: str) -> None:
        g = self.registry.get(product_id)
        if g is not None and g.method == "external":
            g.executable = exe
            self.registry.set(g)

    # ------------------------------------------------------------------ Prefixe verwalten
    def queue_prefix(self, product_id: str) -> Job | None:
        g = self.registry.get(product_id)
        if g is None or g.platform != "windows" or self.jobs.active_for(product_id):
            return None
        return self.jobs.add(Job(kind="prefix", product_id=g.product_id, title=g.title))

    def delete_prefix(self, product_id: str) -> Path | None:
        p = self.launcher.prefix_for(product_id)
        if self.launcher.is_running(product_id):
            raise InstallError(tr('Das Spiel läuft – Prefix kann nicht gelöscht werden.'))
        if not p.exists():
            return None
        if not self._safe_to_delete(p):
            raise InstallError(tr('Prefix-Ordner wird aus Sicherheitsgründen nicht gelöscht: {0}').format(p))
        import shutil
        shutil.rmtree(p, ignore_errors=True)
        gs = self.game_settings.get(product_id)
        # Einrichtung (Skripte, Redistributables) läuft im neuen Prefix neu; die Spielstände im Prefix sind weg – der
        # letzte Cloud-Abgleich gilt nicht mehr (sonst sähe ein Hochladen die Cloud-Dateien als lokal gelöscht an)
        if gs.redist_done or gs.setup_done or gs.cloud_timestamps:
            self.game_settings.update(product_id, redist_done=[], setup_done=[], cloud_timestamps={})
        log.info("Prefix gelöscht: %s", p)
        return p

    def reset_prefix(self, product_id: str) -> Job | None:
        self.delete_prefix(product_id)
        return self.queue_prefix(product_id)

    def prefix_roots(self, library: str | None = None) -> list[Path]:
        """GOG-Prefix-Ordner und die Prefix-Ordner aller eigenen Bibliotheken (oder nur der einen)."""
        if library is not None:
            lib = self.get_library(library)
            return [lib.prefix_root] if lib else []
        out = []
        for r in (Path(os.path.expanduser(self.settings.prefix_dir)), *[lib.prefix_root for lib in self.settings.libraries]):
            if r not in out:
                out.append(r)
        return out

    def orphan_prefixes(self, library: str | None = None) -> list[Path]:
        """Prefix-Ordner in den Prefix-Ordnern, die zu keinem Spiel der Bibliothek gehören."""
        known = {self.launcher.prefix_for(g.product_id).resolve() for g in self.registry.all()}
        out = []
        for root in self.prefix_roots(library):
            try:
                dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
            except OSError:
                continue
            for d in dirs:
                if d.resolve() in known:
                    continue
                if (d / "pfx").exists() or (d / "system.reg").exists() or (d / "drive_c").exists():
                    out.append(d)
        return out

    def delete_orphan_prefixes(self, dirs: list[Path]) -> list[Path]:
        import shutil
        current = {p.resolve() for p in self.orphan_prefixes()}
        done = []
        for d in dirs:
            if d.resolve() in current and self._safe_to_delete(d):
                shutil.rmtree(d, ignore_errors=True)
                done.append(d)
                log.info("Verwaister Prefix gelöscht: %s", d)
        return done

    def queue_missing_prefixes(self, library: str | None = None) -> int:
        n = 0
        games = self.registry.all() if library is None else self.library_games(library)
        for g in games:
            if g.platform != "windows" or self.jobs.active_for(g.product_id):
                continue
            try:
                if self.launcher.prefix_exists(g.product_id):
                    continue
            except Exception:
                continue
            if self.queue_prefix(g.product_id):
                n += 1
        return n

    def create_game_prefix(self, product_id: str, status: Callable[[str], None] | None = None) -> None:
        prefix = self.launcher.prefix_for(product_id)
        if not self.launcher.prefix_exists(product_id):
            if status:
                status(tr('Prefix wird angelegt: {0}').format(prefix))
            rc = self.launcher.create_prefix(product_id)
            if not self.launcher.prefix_exists(product_id):
                raise InstallError(tr('Prefix konnte nicht angelegt werden (Code {0}) – Protokoll: {1}').format(rc, paths.game_log_file(product_id)))
        if status:
            status(tr('Bildschirm-Skalierung (DPI) einstellen …'))
        try:
            self.launcher.apply_dpi(product_id)
        except LaunchError as exc:
            log.warning("DPI konnte nicht gesetzt werden: %s", exc)
        log.info("Prefix bereit: %s → %s", product_id, prefix)

    # ------------------------------------------------------------------ Verknüpfungen
    def create_shortcuts(self, product_id: str, menu: bool = True, desktop: bool = False) -> list:
        pid = str(product_id)
        entry = self.entry(pid)
        title = entry.title if entry else pid
        icon = None
        try:
            m = self.meta(pid)
            if m:
                title = m.title or title
                for url in (m.square_icon, m.vertical_cover):
                    path = self.api.cached_image(url) if url else None
                    if path:
                        icon = shortcuts.store_icon(pid, path)
                        break
        except Exception as exc:
            log.debug("Icon für Verknüpfung nicht verfügbar: %s", exc)
        comment = tr('Über galaxy-linux starten (inoffizieller GOG-Client)')
        return shortcuts.create(pid, title, icon, menu=menu, desktop=desktop, comment=comment)

    def remove_shortcuts(self, product_id: str, menu: bool = True, desktop: bool = True) -> list:
        return shortcuts.remove(str(product_id), menu=menu, desktop=desktop)

    def shortcut_state(self, product_id: str) -> tuple[bool, bool]:
        return shortcuts.has_menu_shortcut(str(product_id)), shortcuts.has_desktop_shortcut(str(product_id))

    def update(self, product_id: str, build_id: str = "", **cbs) -> Job:
        entry = self.entry(product_id)
        return self.jobs.add(Job(kind="update", product_id=str(product_id), title=entry.title if entry else str(product_id),
                                 build_id=build_id, **cbs))

    def verify(self, product_id: str, **cbs) -> Job:
        entry = self.entry(product_id)
        return self.jobs.add(Job(kind="verify", product_id=str(product_id), title=entry.title if entry else str(product_id), **cbs))

    def move(self, product_id: str, new_root: str, **cbs) -> Job:
        entry = self.entry(product_id)
        return self.jobs.add(Job(kind="move", product_id=str(product_id), title=entry.title if entry else str(product_id),
                                 install_root=str(new_root), **cbs))

    def uninstall(self, product_id: str, remove_prefix: bool = False) -> None:
        """Spiel deinstallieren: Dateien löschen, optional auch den Prefix."""
        self.remove_game(product_id, delete_files=True, delete_prefix=remove_prefix)

    def launch(self, product_id: str, task: PlayTask | None = None, extra_args: list[str] | None = None,
               on_exit=None, status_cb=None) -> RunningGame:
        return self.launcher.launch(product_id, task, extra_args, on_exit, status_cb)

    def ensure_build_records(self, force: bool = False) -> int:
        """Installations-Datensätze (Build, Client-ID, Abhängigkeiten) für alle installierten GOG-Windows-Spiele
        anlegen, denen einer fehlt – z. B. übernommene Spiele. Gibt die Zahl neu angelegter Datensätze zurück."""
        from .content import build_records
        n = 0
        for g in self.registry.all():
            if g.method == "external" or g.platform != "windows":
                continue
            if not force and build_records.is_complete(build_records.load_record(g.product_id)):
                continue
            if build_records.is_complete(build_records.ensure_record(self.api, g, force=force)):
                n += 1
        return n

    def logout(self) -> None:
        """Abmelden: Zugangsdaten und Cache weg, dazu comet (läuft mit den Tokens) beenden und dessen Zugangsdatei
        löschen."""
        self.api.logout()
        try:
            from .launch import galaxy_services
            galaxy_services.on_logout()
        except Exception:  # pragma: no cover
            log.exception("comet nach dem Abmelden nicht aufgeräumt")

    def save_settings(self) -> None:
        self.settings.save()
        self.api.store = CredentialStore(self.settings.use_keyring)
