"""Gemeinsame Fassade für UI und CLI: Einstellungen, API, Installer, Launcher, Job-Warteschlange."""
from __future__ import annotations

from .i18n import tr

import dataclasses
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import paths
from .jsonstore import read_json, write_json
from .api.auth import CredentialStore
from .api.client import ApiError, GogApi, NotLoggedIn
from .api.models import GameMeta, LibraryEntry
from .content.downloader import DownloadCancelled, Progress
from .content import external, scanner
from .content.installer import InstallError, InstallManager, InstallPlan, NoGalaxyBuild, safe_dirname
from .content.manifest import Build
from .launch import hooks as launch_hooks, shortcuts
from .launch.launcher import LaunchError, Launcher, PlayTask, RunningGame
from .settings import (EXTERNAL_LIBRARY_ID, VIEW_KEYS, CustomLibrary, GameSettings, GameSettingsStore, GlobalSettings,
                       InstalledGame, InstalledRegistry, PlaytimeStore)

log = logging.getLogger(__name__)


def _file_url(path: str) -> str:
    """Lokale Bilddatei als URL für den Bild-Cache (``GogApi.cached_image`` liefert sie direkt)."""
    return "file://" + str(path)


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
    „Abgebrochen“ (``job.cancel`` regelmäßig prüfen). ``pausable``: Pausieren bricht den Lauf ab und setzt ihn später
    neu auf (der Auftrag muss Fortsetzen können); ``step``: Phase während des Laufs (content/phases.py)."""
    kind: str
    label: str
    run: Callable[["Backend", "Job", Callable[[Progress], None], Callable[[str], None]], Any]
    network: bool = False         # braucht GOG-Zugang → „Ausloggen“ bricht ihn ab
    pausable: bool = False
    step: str = ""


JOB_KINDS: dict[str, JobKind] = {}
NETWORK_JOB_KINDS = {"install", "install-linux", "install-offline", "update", "verify"}
# Pausieren und Fortsetzen (Downloads sind fortsetzbar); diese Aufträge überstehen auch einen Neustart
PAUSABLE_JOB_KINDS = {"install", "install-linux", "install-offline", "update", "verify"}
# nach jedem beendeten Auftrag (fertig, fehlgeschlagen, abgebrochen) im Job-Thread: fn(backend, job)
_job_finished_hooks: list[Callable[["Backend", "Job"], None]] = []


def register_job_kind(kind: str, label: str, run: Callable[..., Any], network: bool = False, pausable: bool = False,
                      step: str = "") -> JobKind:
    jk = JobKind(kind, label, run, network, pausable, step)
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


def is_pausable(kind: str) -> bool:
    jk = JOB_KINDS.get(kind)
    return kind in PAUSABLE_JOB_KINDS or bool(jk and jk.pausable)


# vor Änderungen an den Spieldateien („verify“, „update“, „move“, „uninstall“): fn(backend, product_id, action, status)
# – z. B. GOverlay: bgmod-Änderungen im Spielordner zurücknehmen (launch/hook_goverlay.py)
_files_change_hooks: list[Callable[..., Any]] = []


def on_before_files_change(fn: Callable[..., Any]) -> Callable[..., Any]:
    if fn not in _files_change_hooks:
        _files_change_hooks.append(fn)
    return fn


# Felder, die ein Auftrag über einen Neustart behält (JobQueue.enable_persistence)
_PERSIST_FIELDS = ("kind", "product_id", "title", "platform", "language", "install_root", "build_id", "include_dlcs",
                   "dlc_filter", "shortcut_menu", "shortcut_desktop", "id", "data", "created")


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
    state: str = "queued"         # queued | running | paused | done | failed | cancelled
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
    dlc_filter: list[str] | None = None   # Konfigurieren → DLC: genau diese DLCs (None = alle im Besitz)
    step: str = "pending"         # Phase wie in Galaxy (content/phases.py)
    steps: list[str] = field(default_factory=list)       # bisher durchlaufene Phasen
    pause_reason: str = ""        # user | game („Downloads während des Spiels pausieren“)
    pause_requested: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    finished_at: float = 0.0
    ignore_play_hold: bool = False  # während des Spiels von Hand fortgesetzt (Galaxy: „… in Downloads fortsetzen“)

    @property
    def active(self) -> bool:
        """Noch nicht beendet (wartend, laufend oder pausiert)."""
        return self.state in ("queued", "running", "paused")

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @property
    def pausable(self) -> bool:
        return is_pausable(self.kind)

    @property
    def label(self) -> str:
        return {"install": tr('Installieren'), "update": tr('Aktualisieren'), "verify": tr('Dateien prüfen'), "move": tr('Verschieben'),
                "prefix": tr('Prefix anlegen'),
                "install-linux": tr('Installieren (Linux)'), "install-offline": tr('Installieren (Offline-Installer)')}.get(
                    self.kind, JOB_KINDS[self.kind].label if self.kind in JOB_KINDS else self.kind)

    def set_step(self, step: str) -> None:
        if step and step != self.step:
            if self.step and self.step != "pending" and self.step not in self.steps:
                self.steps.append(self.step)
            self.step = step

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in _PERSIST_FIELDS}
        d["state"] = "paused" if self.state == "paused" and self.pause_reason == "user" else "queued"
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job | None":
        if not isinstance(d, dict) or not d.get("kind") or not d.get("product_id"):
            return None
        kw = {k: d[k] for k in _PERSIST_FIELDS if k in d}
        if not isinstance(kw.get("data", {}), dict) or not (kw.get("dlc_filter") is None or isinstance(kw["dlc_filter"], list)):
            return None
        job = cls(**kw)
        if d.get("state") == "paused":
            job.state, job.status, job.pause_reason = "paused", tr('Pausiert'), "user"
        else:
            job.status = tr('In der Warteschlange')
        return job


def _game_options(backend: Any, product_id: str) -> tuple[str, set[str]]:
    """Passwort des privaten Beta-Kanals und abgewählte DLCs (Konfigurieren) für Installation und Update."""
    gs_store = getattr(backend, "game_settings", None)
    try:
        gs = gs_store.get(product_id) if gs_store is not None else None
        pw, excl = (gs.branch_password, gs.dlc_excluded) if gs is not None else ("", [])
        return (pw if isinstance(pw, str) else ""), {str(x) for x in excl} if isinstance(excl, list) else set()
    except Exception:  # pragma: no cover
        return "", set()


class JobQueue:
    """Führt Installationen nacheinander aus (wie Galaxy), parallelisiert innerhalb eines Jobs.

    Wie Galaxys Download-Ansicht: Aufträge pausieren und fortsetzen (einzeln oder alle), abbrechen, umsortieren
    („Nach oben verschieben“, Ziehen und Ablegen), während des Spielens pausieren. Pausieren bricht den laufenden
    Download ab – die Dateien bleiben liegen und werden beim Fortsetzen geprüft statt neu geladen. Mit
    ``enable_persistence`` (nur die Oberfläche) überstehen offene Aufträge einen Neustart."""

    WATCH_INTERVAL = 2.0

    def __init__(self, backend: "Backend") -> None:
        self.backend = backend
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._wake = threading.Event()
        self.listeners: list[Callable[[Job], None]] = []
        self.persist_path: Path | None = None
        self._play_hold = False
        self._thread = threading.Thread(target=self._run, name="gl-jobs", daemon=True)
        self._thread.start()
        self._watch = threading.Thread(target=self._watch_loop, name="gl-jobs-watch", daemon=True)
        self._watch.start()

    def add(self, job: Job) -> Job:
        if job.state == "queued":
            job.step = "pending"
        with self._lock:
            self.jobs.append(job)
        self._notify(job)
        self._save()
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

    def queue(self) -> list[Job]:
        """Offene Aufträge in Reihenfolge der Warteschlange (laufende zuerst)."""
        with self._lock:
            act = [j for j in self.jobs if j.active]
        return [j for j in act if j.state == "running"] + [j for j in act if j.state != "running"]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def cancel(self, job: Job) -> None:
        job.pause_requested.clear()
        job.cancel.set()
        if job.state in ("queued", "paused"):
            job.state = "cancelled"
            job.status = tr('Abgebrochen')
            job.finished_at = time.time()
            self._notify(job)
        self._save()

    def cancel_all(self) -> None:
        for j in self.active():
            self.cancel(j)

    def pause(self, job: Job, reason: str = "user") -> bool:
        """Auftrag pausieren. Ein laufender Download hält an (Dateien bleiben liegen)."""
        if not job.pausable or job.state not in ("queued", "running"):
            if job.state == "paused" and reason == "user":
                job.pause_reason = "user"        # während des Spiels pausiert → bleibt nach dem Spiel pausiert
                self._save()
                return True
            return False
        job.pause_reason = reason
        if job.state == "queued":
            job.state, job.status = "paused", tr('Pausiert')
            self._notify(job)
        else:
            job.pause_requested.set()
            job.cancel.set()
            job.status = tr('Wird pausiert …')
            self._notify(job)
        self._save()
        return True

    def resume(self, job: Job) -> bool:
        if job.state == "running" and job.pause_requested.is_set():
            # noch nicht angehalten: Pausieren zurücknehmen geht nicht mehr (der Download bricht gerade ab) – nach
            # dem Anhalten gleich wieder einreihen
            job.pause_reason = "resume"
            return True
        if job.state != "paused":
            return False
        job.cancel = threading.Event()
        job.pause_requested.clear()
        job.pause_reason = ""
        job.ignore_play_hold = self._play_hold          # während des Spiels von Hand fortgesetzt: läuft trotzdem
        job.state, job.status, job.step = "queued", tr('In der Warteschlange'), "pending"
        self._notify(job)
        self._save()
        self._wake.set()
        return True

    def pause_all(self) -> int:
        return sum(1 for j in self.queue() if j.state in ("queued", "running") and self.pause(j))

    def resume_all(self) -> int:
        return sum(1 for j in self.queue() if j.state == "paused" and self.resume(j))

    def move(self, job: Job, index: int) -> bool:
        """Auftrag an Position ``index`` der offenen Aufträge schieben (Ziehen und Ablegen)."""
        with self._lock:
            if job not in self.jobs or not job.active:
                return False
            act = [j for j in self.jobs if j.active]
            # Positionen wie in der Anzeige (queue(): laufende zuerst)
            ordered = [j for j in act if j.state == "running"] + [j for j in act if j.state != "running"]
            ordered.remove(job)
            index = max(0, min(index, len(ordered)))
            ordered.insert(index, job)
            self.jobs = [j for j in self.jobs if not j.active] + ordered
        self._notify(job)
        self._save()
        self._wake.set()
        return True

    def move_to_front(self, job: Job) -> bool:
        """„Nach oben verschieben“: als nächster Auftrag (ein laufender läuft weiter)."""
        return self.move(job, 0)

    def remove(self, job: Job) -> None:
        with self._lock:
            if job.active:
                return
            self.jobs = [j for j in self.jobs if j is not job]

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

    # ------------------------------------------------------------------ Neustart überstehen
    def enable_persistence(self, path: Path) -> list[Job]:
        """Offene Aufträge in ``path`` mitschreiben und die beim letzten Beenden offenen wieder einreihen
        (pausierte bleiben pausiert). Nur ein Prozess (die Oberfläche) darf das tun."""
        restored: list[Job] = []
        self.persist_path = Path(path)
        for d in read_json(self.persist_path, []) if isinstance(read_json(self.persist_path, []), list) else []:
            job = Job.from_dict(d)
            if job is None or not job.pausable or self.active_for(job.product_id):
                continue
            with self._lock:
                self.jobs.append(job)
            restored.append(job)
            self._notify(job)
        if restored:
            log.info("%d offene Aufträge vom letzten Mal wieder eingereiht: %s", len(restored),
                     ", ".join(f"{j.label} {j.title} ({j.state})" for j in restored))
        self._save()
        self._wake.set()
        return restored

    def _save(self) -> None:
        if self.persist_path is None:
            return
        with self._lock:
            data = [j.to_dict() for j in self.jobs if j.active and j.pausable]
        with self._save_lock:
            try:
                write_json(self.persist_path, data)
            except OSError as exc:  # pragma: no cover
                log.warning("Warteschlange konnte nicht gespeichert werden: %s", exc)

    # ------------------------------------------------------------------ Ablauf
    def _next(self) -> Job | None:
        with self._lock:
            for j in self.jobs:
                if j.state == "queued" and not (self._play_hold and j.pausable and not j.ignore_play_hold):
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

    def _watch_loop(self) -> None:
        while True:
            time.sleep(self.WATCH_INTERVAL)
            try:
                self.apply_play_pause()
            except Exception:  # pragma: no cover
                log.exception("Prüfung „Downloads während des Spiels pausieren“ fehlgeschlagen")

    def apply_play_pause(self) -> None:
        """„Downloads während des Spiels pausieren“: läuft ein Spiel, halten Downloads an und warten; danach geht es
        von selbst weiter (vom Nutzer pausierte bleiben pausiert)."""
        b = self.backend
        settings, launcher = getattr(b, "settings", None), getattr(b, "launcher", None)
        if settings is None or launcher is None:
            return
        hold = bool(getattr(settings, "pause_downloads_when_playing", False) and launcher.active_ids())
        if hold == self._play_hold:
            return
        self._play_hold = hold
        if hold:
            log.info("Spiel läuft – Downloads werden pausiert")
            for j in self.queue():
                if j.state == "running" and j.pausable and not j.ignore_play_hold:
                    self.pause(j, "game")
        else:
            log.info("Kein Spiel mehr aktiv – pausierte Downloads werden fortgesetzt")
            for j in self.queue():
                j.ignore_play_hold = False
                if j.state == "paused" and j.pause_reason == "game":
                    self.resume(j)
            self._wake.set()

    @property
    def play_hold(self) -> bool:
        return self._play_hold

    def _execute(self, job: Job) -> None:
        if job.cancel.is_set():
            job.state, job.status = "cancelled", tr('Abgebrochen')
            self._notify(job)
            return
        job.state, job.status = "running", tr('Vorbereiten …')
        job.steps = []
        job.step = ""
        jk = JOB_KINDS.get(job.kind)
        job.set_step({"install": "install_started", "install-linux": "install_started", "install-offline": "install_started",
                      "update": "file_list", "verify": "file_list", "move": "moving", "prefix": "setup"}.get(
                          job.kind, jk.step if jk else ""))
        log.info("Job %s gestartet: %s (%s) platform=%s lang=%s build=%s dlcs=%s", job.id, job.kind, job.title,
                 job.platform, job.language or "-", job.build_id or "latest",
                 job.include_dlcs if job.dlc_filter is None else ",".join(job.dlc_filter) or "keine")
        self._notify(job)
        self._save()
        t_start = time.monotonic()
        last = [0.0]

        def on_progress(p: Progress) -> None:
            job.progress = p
            if p.step:
                job.set_step(p.step)
            now = time.monotonic()
            if now - last[0] >= 0.2 or p.phase == tr('Fertig'):
                last[0] = now
                job.status = p.phase or job.status
                self._notify(job)

        def status(text: str) -> None:
            job.status = text
            if job.kind in ("install", "update", "verify") and job.step in ("install_started", ""):
                job.set_step("file_list")
            self._notify(job)

        inst = self.backend.installer
        password, dlc_exclude = _game_options(self.backend, job.product_id) if job.kind in ("install", "update", "verify") \
            else ("", set())
        try:
            if job.kind in ("update", "verify", "move"):
                self.backend.before_files_change(job.product_id, job.kind, status)
            if job.kind == "install":
                try:
                    plan = inst.plan(job.product_id, job.title, job.platform, job.language or None,
                                     build_id=job.build_id or None, install_root=job.install_root or None,
                                     include_dlcs=job.include_dlcs,
                                     dlc_filter=set(job.dlc_filter) if job.dlc_filter is not None else None,
                                     status=status, password=password, dlc_exclude=dlc_exclude)
                except NoGalaxyBuild:
                    status(tr('Kein Galaxy-Build – nutze Offline-Installer …'))
                    job.kind = "install-offline"
                    job.set_step("downloading")
                    job.result = inst.install_offline_windows(job.product_id, job.title, job.language or None,
                                                              job.install_root or None, on_progress, job.cancel, status)
                    plan = None
                if plan is not None:
                    job.plan = plan
                    status(tr('Herunterladen ({0} Dateien)').format(plan.file_count))
                    job.result = inst.install(plan, on_progress, job.cancel)
            elif job.kind == "install-linux":
                job.set_step("downloading")
                job.result = inst.install_linux_native(job.product_id, job.title, job.language or None,
                                                       job.install_root or None, on_progress, job.cancel, status)
            elif job.kind == "install-offline":
                job.set_step("downloading")
                job.result = inst.install_offline_windows(job.product_id, job.title, job.language or None,
                                                          job.install_root or None, on_progress, job.cancel, status)
            elif job.kind == "update":
                job.result = inst.update(job.product_id, on_progress, job.cancel, status, build_id=job.build_id or None,
                                         language=job.language or None,
                                         dlc_filter=set(job.dlc_filter) if job.dlc_filter is not None else None,
                                         password=password, dlc_exclude=dlc_exclude)
            elif job.kind == "verify":
                job.result = inst.verify(job.product_id, on_progress, job.cancel, status, password=password)
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
            if job.kind.startswith("install") or job.kind in ("update", "verify"):
                job.set_step("complete")
            self.backend.updates.pop(job.product_id, None)
            p = job.progress
            log.info("Job %s fertig nach %.0f s: %s · %s geschrieben, %s geladen, %d/%d Dateien", job.id,
                     time.monotonic() - t_start, job.title, p.done_bytes, p.downloaded_bytes, p.files_done, p.files_total)
        except DownloadCancelled:
            if job.pause_requested.is_set():
                again = job.pause_reason == "resume"
                job.state, job.status = "paused", tr('Pausiert')
                job.pause_requested.clear()
                log.info("Job %s pausiert nach %.0f s (%s)", job.id, time.monotonic() - t_start, job.pause_reason or "user")
                if again:
                    self.resume(job)
            else:
                job.state, job.status = "cancelled", tr('Abgebrochen')
                log.info("Job %s abgebrochen nach %.0f s", job.id, time.monotonic() - t_start)
        except (InstallError, ApiError, NotLoggedIn, OSError, ValueError) as exc:
            job.state, job.status, job.error = "failed", tr('Fehlgeschlagen'), str(exc)
            log.warning("Job %s fehlgeschlagen nach %.0f s: %s", job.id, time.monotonic() - t_start, exc, exc_info=True)
        except Exception as exc:  # pragma: no cover
            job.state, job.status, job.error = "failed", tr('Fehlgeschlagen'), f"{type(exc).__name__}: {exc}"
            log.exception("Job %s abgestürzt", job.id)
        if not job.active:
            job.finished_at = time.time()
        self._notify(job)
        self._save()
        if job.state == "paused":
            return                       # kein „beendet“ – der Auftrag läuft später weiter
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
        from .content import bandwidth
        # Bandbreitenbegrenzung (Einstellungen → Installieren/aktualisieren), für alle Downloads dieses Prozesses
        self.throttle = bandwidth.Throttle(lambda: bandwidth.current_rate(self.settings))
        self.installer = InstallManager(self.api, self.settings, self.registry, throttle=self.throttle)
        self.launcher = Launcher(self.settings, self.game_settings, self.registry, self.playtime, self.api)
        self.library: list[LibraryEntry] = self.api.cached_library() or []
        self.updates: dict[str, Build] = {}
        self._meta: dict[str, GameMeta] = {}
        self._covers: dict[str, str] = {}
        self._meta_lock = threading.Lock()
        self._playtime_checked: dict[str, float] = {}
        self.jobs = JobQueue(self)
        self._scan_lock = threading.Lock()
        self._identity_lock = threading.Lock()
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
        """Bibliothekseintrag (GOG oder installiert); ein eigener Titel („Bearbeiten …“) ersetzt den Titel."""
        pid = str(product_id)
        custom = self.game_settings.get(pid).custom_title
        for e in self.library:
            if e.id == pid:
                return dataclasses.replace(e, title=custom) if custom else e
        inst = self.registry.get(pid)
        if inst:
            return LibraryEntry(id=pid, title=custom or inst.title,
                                works_on={inst.platform if inst.platform != "osx" else "mac": True})
        return None

    def visible_library(self, include_hidden: bool | None = None) -> list[LibraryEntry]:
        """GOG-Bibliothek ohne ausgeblendete Spiele (hier oder bei GOG ausgeblendet), außer „Versteckte Spiele
        anzeigen“ ist an bzw. ``include_hidden``. Eigene Titel sind eingesetzt."""
        from .library_view import is_hidden
        show = self.settings.show_hidden if include_hidden is None else include_hidden
        all_gs = self.game_settings.items()
        default = GameSettings()
        out = []
        for e in self.library:
            gs = all_gs.get(e.id, default)
            if not show and is_hidden(e, gs):
                continue
            out.append(dataclasses.replace(e, title=gs.custom_title) if gs.custom_title else e)
        return out

    # ------------------------------------------------------------------ Bearbeiten … (eigener Titel und eigenes Cover)
    def default_title(self, product_id: str) -> str:
        """Titel ohne eigenen Titel: GOG-Titel bzw. erkannter Titel eines externen Spiels."""
        pid = str(product_id)
        inst = self.registry.get(pid)
        if inst is not None and inst.method == "external":
            return inst.title
        for e in self.library:
            if e.id == pid:
                return e.title
        return inst.title if inst is not None else pid

    def display_title(self, product_id: str) -> str:
        return self.game_settings.get(str(product_id)).custom_title or self.default_title(product_id)

    def set_custom_title(self, product_id: str, title: str) -> str:
        """Eigenen Titel setzen; leer oder gleich dem automatischen Titel = zurück auf automatisch."""
        pid = str(product_id)
        title = " ".join((title or "").split())
        if title == self.default_title(pid):
            title = ""
        self.game_settings.update(pid, custom_title=title)
        log.info("Eigener Titel für %s: %s", pid, title or "(automatisch)")
        return title or self.default_title(pid)

    def custom_cover_dir(self) -> Path:
        return paths.DATA_DIR / "custom-covers"

    def set_custom_cover(self, product_id: str, source: str | Path | None) -> str:
        """Eigenes Cover aus einer Bilddatei übernehmen (Kopie im Datenordner, damit es bleibt, auch wenn die Datei
        verschwindet); ``None`` = zurück auf automatisch. Gibt den gespeicherten Pfad zurück."""
        import hashlib
        import shutil
        pid = str(product_id)
        old = self.game_settings.get(pid).custom_cover
        dest = ""
        if source:
            src = Path(os.path.expanduser(str(source)))
            data = src.read_bytes()
            if not data:
                raise InstallError(tr('Die Bilddatei ist leer.'))
            ext = src.suffix.lower() if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".img"
            d = self.custom_cover_dir()
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pid)
            target = d / f"{safe}-{hashlib.sha1(data).hexdigest()[:10]}{ext}"
            if not target.exists():
                tmp = target.with_name(target.name + ".tmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, target)
            dest = str(target)
        self.game_settings.update(pid, custom_cover=dest)
        if old and old != dest:
            try:
                p = Path(old)
                if p.parent == self.custom_cover_dir():
                    p.unlink()
            except OSError:
                pass
        with self._meta_lock:
            self._covers.pop(pid, None)
        log.info("Eigenes Cover für %s: %s", pid, dest or "(automatisch)")
        return dest

    def copy_of(self, product_id: str) -> InstalledGame | None:
        """Original, wenn das Spiel eine Kopie (gleicher Inhalt) eines anderen externen Spiels ist."""
        g = self.registry.get(str(product_id))
        if g is None or not g.duplicate_of:
            return None
        return self.registry.get(g.duplicate_of)

    def meta(self, product_id: str) -> GameMeta | None:
        """Metadaten der Spielansicht; ein eigener Titel/ein eigenes Cover („Bearbeiten …“) wird eingesetzt."""
        m = self._meta_raw(str(product_id))
        if m is None:
            return None
        gs = self.game_settings.get(str(product_id))
        if gs.custom_title or gs.custom_cover:
            m = dataclasses.replace(m, title=gs.custom_title or m.title,
                                    vertical_cover=_file_url(gs.custom_cover) if gs.custom_cover else m.vertical_cover)
        return m

    def _meta_raw(self, pid: str) -> GameMeta | None:
        with self._meta_lock:
            if pid in self._meta:
                return self._meta[pid]
        g = self.registry.get(pid)
        if g is not None and g.method == "external":
            m = self._external_meta(g)
            with self._meta_lock:
                self._meta[pid] = m
            return m
        entry = None
        for e in self.library:
            if e.id == pid:
                entry = e
                break
        if entry is None and g is not None:
            entry = LibraryEntry(id=pid, title=g.title, works_on={g.platform if g.platform != "osx" else "mac": True})
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
        custom = self.game_settings.get(pid).custom_cover
        if custom and os.path.isfile(custom):
            return _file_url(custom)
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
        """Metadaten eines externen Spiels aus seiner Erkennung (content/identity.py): GOG-Spiel desselben Namens,
        sonst GOG-Katalog/Steam zum offiziellen Titel; Cover aus der Erkennung (auch Wikipedia)."""
        from .api import covers
        title = g.title
        base = g.base_title or title
        if g.gog_match and g.gog_match != g.product_id:
            src = self._meta_raw(g.gog_match)
            if src is not None:
                m = dataclasses.replace(src, id=g.product_id, title=title, os_list=["windows"], dlc_ids=[],
                                        logo="" if g.variant else src.logo)   # Variante: Titel mit Zusatz statt Logo
                m.vertical_cover = g.cover_url or m.vertical_cover
                return m
        queries = [external.search_title(base) or base]
        stem = Path(g.executable or "").stem
        if not g.identity and len(stem) >= 4 and external._norm(title).startswith(external._norm(stem)):
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
                m.vertical_cover = g.cover_url or m.vertical_cover or data.get("cover", "")
                if g.variant:
                    m.logo = ""
                return m
            except Exception as exc:  # pragma: no cover
                log.debug("GOG-Metadaten für %s fehlgeschlagen: %s", title, exc)
        m = GameMeta(id=g.product_id, title=title, os_list=["windows"])
        if data:
            m.vertical_cover, m.background, m.logo = data.get("cover", ""), data.get("background", ""), data.get("logo", "")
            m.summary, m.genres = data.get("summary", ""), data.get("genres", [])
            m.developers, m.publishers = data.get("developers", []), data.get("publishers", [])
            m.release_date = data.get("release_date", "")
        m.vertical_cover = g.cover_url or m.vertical_cover
        if g.variant:
            m.logo = ""                               # Variante: Titel mit Zusatz statt Logo des Grundspiels
        return m

    # ------------------------------------------------------------------ Externe Spiele erkennen (Kopien, Namen, Cover)
    def resolve_externals(self, network: bool = True, force: bool = False) -> list[str]:
        """Kopien (lokal, immer) und Namen/Cover (``identity``) der externen Spiele bestimmen und im Datensatz
        speichern. Erkannt wird nur, was noch nicht (oder in einer anderen Sprache) erkannt ist, außer ``force``.
        Läuft im Hintergrund; gibt die geänderten Produkt-IDs zurück."""
        if not self._identity_lock.acquire(blocking=False):
            return []
        try:
            return self._resolve_externals(network, force)
        finally:
            self._identity_lock.release()

    def _resolve_externals(self, network: bool, force: bool) -> list[str]:
        from . import i18n
        from .api import covers, wikidata
        from .content import identity
        games = [g for g in self.registry.all() if g.method == "external"]
        if not games:
            return []
        infos = [identity.FolderInfo(g.product_id, Path(g.install_path), g.installed_at) for g in games
                 if os.path.isdir(g.install_path)]
        cache = identity.FingerprintCache()
        prints = {f.pid: cache.get(f.path) for f in infos}
        cache.prune([f.path for f in infos])
        try:
            cache.save()
        except OSError as exc:
            log.debug("Fingerabdrücke nicht gespeichert: %s", exc)
        copies = identity.find_copies(infos, prints)
        installed = {g.product_id for g in self.registry.all()}
        gog_titles = [(g.product_id, g.title) for g in self.registry.all() if g.method != "external"]
        gog_titles += [(e.id, e.title) for e in self.library if e.id not in installed and e.is_game]
        # Grundnamen der Ordner: ein „… - Kopie“ mit anderem Inhalt ist eine Variante, wenn es das Original gibt
        plain = {identity.name_parts(Path(g.install_path).name)[:2] for g in games
                 if not identity.has_copy_marker(Path(g.install_path).name)}
        lang = i18n.current()
        session = self.api.session
        lookup = (lambda qs, company, errors: covers.lookup(session, qs, company, errors)) if network else None
        wiki = (lambda qs, errors: wikidata.find_game(session, qs, errors)) if network else None
        changed: list[str] = []
        for g in games:
            pid = g.product_id
            dup = copies.get(pid, "")
            upd: dict[str, object] = {}
            if g.duplicate_of != dup:
                upd["duplicate_of"] = dup
            if force or g.identity < identity.IDENTITY_VERSION or (g.identity_lang or lang) != lang:
                folder = Path(g.install_path).name
                if not g.identity:
                    self._keep_manual_title(g)
                clean, label, _marker = identity.name_parts(folder)
                exe = Path(g.install_path) / g.executable if g.executable else None
                try:
                    ident = identity.resolve(folder, exe, gog_titles, lookup, wiki, lang,
                                             copy_label=not dup and (clean, label) in plain)
                except Exception:
                    log.exception("Erkennung von %s fehlgeschlagen", g.install_path)
                    continue
                cover = ident.cover
                if ident.gog_match:
                    try:
                        cover = self.cover_url(ident.gog_match) or cover
                    except Exception as exc:  # pragma: no cover
                        log.debug("Cover von %s nicht ermittelbar: %s", ident.gog_match, exc)
                upd.update(title=ident.title, base_title=ident.base_title, variant=ident.label,
                           title_source=ident.source, gog_match=ident.gog_match, cover_url=cover, wikidata=ident.wikidata)
                if ident.complete and network:
                    upd.update(identity=identity.IDENTITY_VERSION, identity_lang=lang)
                if cover and network and not cover.startswith("file://"):
                    try:
                        self.api.cached_image(cover)          # für GOverlay-Cover (ohne Netz) schon im Bild-Cache
                    except Exception as exc:  # pragma: no cover
                        log.debug("Cover %s nicht geladen: %s", cover, exc)
                log.info("Externes Spiel %s: „%s“ (%s%s%s)", folder, ident.title, ident.source,
                         f", Kopie von {dup}" if dup else "", "" if ident.complete else ", unvollständig")
            upd = {k: v for k, v in upd.items() if getattr(g, k) != v}
            if upd:
                self.registry.update(pid, **upd)
                with self._meta_lock:
                    self._meta.pop(pid, None)
                changed.append(pid)
        return changed

    def _keep_manual_title(self, g: InstalledGame) -> None:
        """Vor 0.3.1 von Hand vergebener Name eines externen Spiels → eigener Titel (wird nicht überschrieben)."""
        folder = Path(g.install_path).name
        if g.title and g.title not in (external.display_title(folder), folder):
            gs = self.game_settings.get(g.product_id)
            if not gs.custom_title:
                self.game_settings.update(g.product_id, custom_title=g.title)
                log.info("Von Hand vergebener Name „%s“ bleibt als eigener Titel", g.title)

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
            pw = self.game_settings.get(g.product_id).branch_password
            b = self.installer.check_update(g, pw) if pw else self.installer.check_update(g)
            if b:
                found[g.product_id] = b
        self.updates = found
        return found

    # ------------------------------------------------------------------ Aktionen
    def install(self, product_id: str, platform: str = "windows", language: str = "", install_root: str = "",
                build_id: str = "", include_dlcs: bool = True, shortcut_menu: bool | None = None,
                shortcut_desktop: bool | None = None, dlc_filter: list[str] | None = None, **cbs) -> Job:
        entry = self.entry(product_id)
        title = entry.title if entry else str(product_id)
        kind = "install-linux" if platform == "linux" else "install"
        job = Job(kind=kind, product_id=str(product_id), title=title, platform=platform, language=language,
                  install_root=install_root, build_id=build_id, include_dlcs=include_dlcs, dlc_filter=dlc_filter,
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
                    v = {kk: (int(vv) if kk == "sort" else bool(vv) if kk in ("titles", "sections", "sort_desc") else str(vv))
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
            game = self._register_external(d, info["title"], exe, size, lib.id)
            if title.strip() and title.strip() != info["title"]:
                self.game_settings.update(game.product_id, custom_title=" ".join(title.split()))
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
        """Ältere Aufrufe: Name eines externen Spiels = eigener Titel („Bearbeiten …“), der Datensatz bleibt."""
        if self.registry.get(product_id) is not None:
            self.set_custom_title(product_id, title)

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

    def auto_update(self, found: dict[str, Build]) -> list[str]:
        """„Spiele automatisch aktualisieren“: Updates der gefundenen Spiele einreihen – nicht für laufende Spiele oder
        solche, die gerade bearbeitet werden (die kommen bei der nächsten Suche dran)."""
        queued = []
        for pid, build in found.items():
            if self.launcher.is_running(pid) or self.jobs.active_for(pid) or not self.registry.is_installed(pid):
                continue
            if not self.game_settings.get(pid).auto_update:    # Konfigurieren: Version selbst wählen (Rollback)
                continue
            self.update(pid, build_id=getattr(build, "build_id", "") or "")
            queued.append(pid)
        if queued:
            log.info("Automatische Updates eingereiht: %s", ", ".join(queued))
        return queued

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

    # ------------------------------------------------------------------ Konfigurieren (wie Galaxy 2.1)
    def _configurable(self, product_id: str) -> InstalledGame:
        g = self.registry.get(str(product_id))
        if g is None:
            raise InstallError(tr('Spiel ist nicht installiert.'))
        if g.method not in ("content-system", "imported") or g.platform == "linux":
            raise InstallError(tr('Nur für Spiele, die über das GOG-Content-System installiert sind.'))
        if self.jobs.active_for(g.product_id) or self.launcher.is_running(g.product_id):
            raise InstallError(tr('Das Spiel läuft oder wird gerade bearbeitet.'))
        return g

    def change_language(self, product_id: str, language: str) -> Job:
        """Konfigurieren → Sprache: Sprachdateien des installierten Builds laden, andere entfernen."""
        g = self._configurable(product_id)
        return self.jobs.add(Job(kind="update", product_id=g.product_id, title=g.title, language=language,
                                 build_id=g.build_id, data={"reason": "language"}))

    def owned_dlcs(self, product_id: str) -> list[tuple[str, str, bool]]:
        """DLCs des Spiels im Besitz: (ID, Titel, installiert) – DLCs aus api.gog.com (``expanded_dlcs``), Besitz aus
        GOGs Liste der eigenen Produkte (DLCs stehen nicht einzeln in der Bibliothek). Arbeits-Thread (Netz)."""
        pid = str(product_id)
        product = self.api.product(pid) or {}
        dlcs = [(str(d["id"]), str(d.get("title") or d["id"])) for d in product.get("expanded_dlcs") or []
                if isinstance(d, dict) and d.get("id")]
        owned = self.installer.owned_ids() if self.api.logged_in else None
        inst = self.registry.get(pid)
        have = set(inst.installed_dlcs) if inst else set()
        return [(d, t, d in have) for d, t in dlcs if d in have or (owned is not None and d in owned)]

    def change_dlcs(self, product_id: str, selected: list[str]) -> Job:
        """Konfigurieren → DLC: genau diese DLCs installieren; abgewählte bleiben auch bei Updates weg. Wartet schon
        ein DLC-Auftrag für das Spiel, übernimmt er die neue Auswahl (jedes Häkchen wirkt sofort)."""
        pid = str(product_id)
        sel = [str(d) for d in selected]
        waiting = self.jobs.active_for(pid)
        if waiting is not None and waiting.state in ("queued", "paused") and waiting.data.get("reason") == "dlc":
            g = self.registry.get(pid)
        else:
            g = self._configurable(pid)
            waiting = None
        owned = [d for d, _t, _i in self.owned_dlcs(pid)]
        self.game_settings.update(pid, dlc_excluded=sorted(d for d in owned if d not in sel))
        if waiting is not None:
            waiting.dlc_filter = sel
            self.jobs._save()
            return waiting
        return self.jobs.add(Job(kind="update", product_id=g.product_id, title=g.title, build_id=g.build_id,
                                 dlc_filter=sel, data={"reason": "dlc"}))

    def available_builds(self, product_id: str) -> list[Build]:
        """Alle Builds (Generation 2) inkl. der Beta-Kanäle, neueste zuerst; private Kanäle mit gespeichertem Passwort."""
        g = self.registry.get(str(product_id))
        platform = g.platform if g else "windows"
        pw = self.game_settings.get(str(product_id)).branch_password
        return [b for b in self.installer.builds(str(product_id), platform, pw) if b.is_v2]

    def change_build(self, product_id: str, build_id: str) -> Job:
        """Konfigurieren → Version (Rollback) oder Beta-Kanal: auf genau diesen Build wechseln."""
        g = self._configurable(product_id)
        return self.jobs.add(Job(kind="update", product_id=g.product_id, title=g.title, build_id=str(build_id),
                                 data={"reason": "build"}))

    def change_branch(self, product_id: str, branch: str | None) -> Job:
        """Beta-Kanal wechseln (None/"" = öffentliche Version): neuester Build des Kanals."""
        builds = [b for b in self.available_builds(product_id) if (b.branch or None) == (branch or None)]
        if not builds:
            raise InstallError(tr('Kanal nicht verfügbar.'))
        return self.change_build(product_id, builds[0].build_id)

    def add_private_channel(self, product_id: str, password: str) -> list[str]:
        """Passwort eines privaten Kanals prüfen und merken; gibt die damit sichtbaren neuen Kanäle zurück."""
        pid = str(product_id)
        g = self.registry.get(pid)
        platform = g.platform if g else "windows"
        public = {b.branch for b in self.installer.builds(pid, platform) if b.is_v2 and b.branch}
        found = sorted({b.branch for b in self.installer.builds(pid, platform, password) if b.is_v2 and b.branch} - public)
        if not found:
            raise InstallError(tr('Falsches Passwort für diesen Kanal!'))
        self.game_settings.update(pid, branch_password=password)
        log.info("Privater Kanal für %s freigeschaltet: %s", pid, ", ".join(found))
        return found

    def set_auto_update(self, product_id: str, on: bool) -> None:
        self.game_settings.update(str(product_id), auto_update=bool(on))

    # ------------------------------------------------------------------ Extras
    def extras(self, product_id: str) -> list:
        """Spielansicht → Extras: Offline-Installer, Patches, Sprachpakete und Extras des Spiels und seiner DLCs."""
        from .content import extras
        pid = str(product_id)
        product = self.api.product(pid, max_age=3600)
        owned = self.installer.owned_ids() if self.api.logged_in else None      # mit DLCs (nicht in der Bibliothek)
        return extras.list_extras(product, owned)

    def download_extras(self, product_id: str, items: list) -> Job:
        from .content import extras
        e = self.entry(product_id)
        return self.jobs.add(extras.new_job(self, str(product_id), e.title if e else str(product_id), list(items)))

    def backup_cloud_saves(self, product_id: str) -> Job:
        from .content import extras
        e = self.entry(product_id)
        return self.jobs.add(extras.new_cloud_backup_job(self, str(product_id), e.title if e else str(product_id)))

    # ------------------------------------------------------------------ Spielzeit von GOG
    PLAYTIME_MAX_AGE = 6 * 3600

    def pull_playtime(self, product_ids: list[str], max_age: float | None = None, limit: int = 0) -> int:
        """Spielzeit aller Geräte von GOG übernehmen (``time_sum``, höhere Werte gewinnen), je Spiel höchstens alle
        ``max_age`` Sekunden. Gibt die Zahl der Spiele mit neuem Wert zurück. Arbeits-Thread."""
        if not self.api.logged_in or not self.settings.sync_playtime:
            return 0
        max_age = self.PLAYTIME_MAX_AGE if max_age is None else max_age
        now, n, asked = time.time(), 0, 0
        for pid in product_ids:
            pid = str(pid)
            if pid.startswith("ext-") or not pid.isdigit() or now - self._playtime_checked.get(pid, 0) < max_age:
                continue
            if limit and asked >= limit:
                break
            asked += 1
            self._playtime_checked[pid] = now
            try:
                res = self.api.playtime(pid)
            except (ApiError, OSError) as exc:
                log.debug("Spielzeit von %s nicht abrufbar: %s", pid, exc)
                continue
            if not res:
                continue
            before = self.playtime.get(pid)
            self.playtime.set_remote(pid, res[0], res[1])
            if self.playtime.get(pid) != before:
                n += 1
        if n:
            log.info("Spielzeit von GOG übernommen: %d Spiel(e)", n)
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


def _register_builtin_kinds() -> None:
    from .content import extras
    register_job_kind(extras.JOB_KIND, tr('Extras herunterladen'), extras.run_job, network=True, pausable=True,
                      step="downloading")
    register_job_kind(extras.CLOUD_BACKUP_KIND, tr('Sicherungskopie deiner Cloud-Speicherstände'), extras.run_cloud_backup,
                      network=True, step="cloud")


_register_builtin_kinds()
