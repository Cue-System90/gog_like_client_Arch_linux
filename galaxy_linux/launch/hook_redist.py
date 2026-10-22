"""Prefix-Einrichtung wie GOG Galaxy und Heroic: Installationsskripte und Redistributables (ersetzt die
Redistributables-Installation bis 0.2.0).

Vor dem ersten Start eines GOG-Windows-Spiels in einem Prefix (erneut nach Updates, nach dem Verschieben und auf
Wunsch – „Einrichtung erneut ausführen“, ``galaxy-linux prefix setup``):
  1. Installations-Datensatz besorgen (übernommene Spiele: ``content/build_records.ensure_record``; Datensätze bis
     0.1.6 einmal aus dem Build-Manifest ergänzen),
  2. fehlende globale Redistributables laden – Auftrag „Galaxy-Redistributables“ in der Download-Ansicht,
  3. fehlende Support-Dateien laden (``~/.local/share/galaxy-linux/gog-support/<id>``),
  4. die Schritte aus ``content/redist.build_steps`` im Prefix ausführen (``Launcher.prefix_command``, Verb ``run``):
     ISI-Skripte bzw. Support-Programme je Produkt, dann die Redistributables des Spiels.
Zustand je Prefix in ``<prefix>/galaxy-linux-setup.json`` (Exit-Codes, Fehlversuche), fertige Builds zusätzlich in
``GameSettings.setup_done``. Ein fehlgeschlagener Schritt läuft beim nächsten Start erneut (höchstens
``redist.MAX_RETRIES``-mal) und wird danach einmal gemeldet. Fehlt ein Programm (oder für ISI die Support-Dateien),
weil Netz oder Anmeldung fehlen, bleibt der Schritt offen, ohne als Fehlversuch zu zählen. ``install_redist`` (global
und pro Spiel) schaltet nur die Redistributables ab, die Installationsskripte laufen immer (wie in Galaxy).

Außerdem hier: Auftragsart ``redist`` und nach jeder Installation/Aktualisierung die Prüfung, ob Redistributables
fehlen; ``ui/ext_redist.py`` prüft beim Start der Oberfläche, ob der gemeinsame Ordner veraltet ist.
"""
from __future__ import annotations

from ..i18n import tr

import logging
import shlex
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable

from ..api.client import ApiError, NotLoggedIn
from ..backend import Job, on_job_finished, register_job_kind
from ..content import build_records, redist
from ..content.downloader import DownloadCancelled
from .hooks import LaunchContext, LaunchHook, register_hook
from .launcher import LaunchError

log = logging.getLogger(__name__)

STEP_TIMEOUT = 1800                 # Sekunden je Installer (wie bisher)
SETUP_ENV = {"PROTONFIXES_DISABLE": "1"}

_backend_ref: "weakref.ReferenceType | None" = None
_tried: set[tuple[str, str]] = set()      # (Art, Produkt-ID): Netzabfragen für Datensätze einmal je Programmlauf
_tried_lock = threading.Lock()


def pending_redists(record: dict, done: list[str]) -> list[dict]:
    """Einträge aus dem Datensatz, die noch nicht installiert wurden (Format bis 0.2.0, ``redist_done``)."""
    return [r for r in record.get("redist", []) if r.get("path") and r.get("id") not in done]


# --------------------------------------------------------------------------- Backend und Auftrag „Galaxy-Redistributables“
def attach_backend(backend: Any) -> None:
    """Backend merken (Oberfläche/Aufträge), damit der Hook Downloads als sichtbaren Auftrag ausführen kann."""
    global _backend_ref
    if backend is None:
        _backend_ref = None
        return
    try:
        _backend_ref = weakref.ref(backend)
    except TypeError:                    # Objekte ohne Weakref-Unterstützung (Tests) direkt halten
        _backend_ref = lambda b=backend: b  # noqa: E731


def attached_backend() -> Any:
    return _backend_ref() if _backend_ref is not None else None


def is_gog_windows(game: Any) -> bool:
    """Installiertes Windows-Spiel aus der GOG-Bibliothek (nur dafür gibt es Einrichtung und Redistributables)."""
    return (game is not None and game.platform == "windows" and game.method != "external"
            and not str(game.product_id).startswith("ext-"))


def installed_records(backend: Any) -> list[dict]:
    """Installations-Datensätze aller installierten GOG-Windows-Spiele (ohne Netz)."""
    out = []
    for g in backend.registry.all():
        if is_gog_windows(g):
            rec = build_records.load_record(g.product_id)
            if rec:
                out.append(rec)
    return out


def required_redists(backend: Any) -> list[str]:
    """ISI plus die globalen Abhängigkeiten aller installierten Spiele (wie Heroics ``getRequiredRedistList``)."""
    return redist.required_ids(installed_records(backend), redist.cached_repository())


def new_job(ids: list[str] | None = None, full: bool = True) -> Job:
    """Auftrag „Galaxy-Redistributables“; ``full`` = auch alle übrigen benötigten IDs (sonst nur ``ids``)."""
    return Job(kind=redist.JOB_KIND, product_id=redist.JOB_PRODUCT, title=tr('Galaxy-Redistributables'),
               data={"ids": [str(i) for i in ids or []], "full": bool(full)})


def run_job(backend: Any, job: Job, on_progress: Callable, status: Callable[[str], None]) -> list[str]:
    """Auftragsart ``redist`` (Job-Thread): gemeinsamen Ordner laden bzw. aktualisieren."""
    attach_backend(backend)
    ids = [str(i) for i in job.data.get("ids") or []]
    if job.data.get("full", True):
        ids = list(dict.fromkeys([*ids, *required_redists(backend)]))
    return redist.download_global_redists(backend.api, ids, workers=backend.settings.max_workers,
                                          progress_cb=on_progress, cancel=job.cancel, status=status)


def queue_redist_job(backend: Any, ids: list[str] | None = None) -> Job:
    """In die Warteschlange stellen (läuft nach den anderen Downloads); ein laufender/wartender Auftrag genügt."""
    for j in backend.jobs.active():
        if j.kind == redist.JOB_KIND:
            return j
    return backend.jobs.add(new_job(ids, full=True))


def redist_update_needed(backend: Any) -> bool:
    """Beim Start der Oberfläche (Arbeits-Thread): fehlen benötigte Redistributables oder hat GOG das Repository
    geändert? Nur wenn GOG erreichbar ist (das Repository ist öffentlich, Anmeldung unnötig) – offline wird kein
    Auftrag eingereiht, der nur fehlschlagen würde. Ohne installierte GOG-Windows-Spiele: nein."""
    if not backend.settings.install_redist:
        return False
    recs = installed_records(backend)
    if not recs:
        return False
    try:
        info = redist.repository_info(backend.api)
    except (ApiError, NotLoggedIn, OSError, ValueError) as exc:
        log.info("Abhängigkeits-Repository nicht erreichbar: %s", exc)
        return False
    if redist.missing_ids(redist.required_ids(recs, redist.cached_repository())):
        return True
    return redist.cache_outdated(str(info.get("build_id") or ""))


def _progress_text(job: Job) -> str:
    p = job.progress
    if p.total_bytes:
        return tr('Galaxy-Redistributables laden … {0} %').format(int(p.fraction * 100))
    return job.status


def fetch_with_job(backend: Any, ids: list[str], status: Callable[[str], None] | None = None,
                   poll: float = 0.25) -> Job:
    """Fehlende Redistributables vor einem Start laden – sichtbar als Auftrag. Läuft gerade einer, wird gewartet;
    sonst läuft ein eigener sofort in diesem Thread (nicht hinter einer Spiel-Installation in der Warteschlange)."""
    active = next((j for j in backend.jobs.active() if j.kind == redist.JOB_KIND and j.state == "running"), None)
    if active is not None:
        while active.active:
            if status:
                status(_progress_text(active))
            time.sleep(poll)
        if not redist.missing_ids(ids):
            return active
    job = new_job(ids, full=False)
    if status:
        job.on_progress = lambda j: status(_progress_text(j))
    backend.jobs.run_now(job)
    return job


@on_job_finished
def _after_job(backend: Any, job: Job) -> None:
    """Nach Installation/Update/Prüfung: fehlen Redistributables des Spiels, Auftrag einreihen (wie Heroic)."""
    attach_backend(backend)
    if job.kind not in ("install", "update", "verify") or job.state != "done" or not backend.settings.install_redist:
        return
    g = backend.registry.get(job.product_id)
    if not is_gog_windows(g):
        return
    rec = build_records.load_record(g.product_id)
    ids = [e["id"] for e in redist.record_global_entries(rec, redist.cached_repository())]
    if rec and rec.get("script_interpreter"):
        ids.insert(0, redist.ISI_ID)
    if ids and redist.missing_ids(ids):
        queue_redist_job(backend)


register_job_kind(redist.JOB_KIND, tr('Galaxy-Redistributables'), run_job, network=False)


# --------------------------------------------------------------------------- Einrichtung im Prefix
def _once(kind: str, pid: str) -> bool:
    with _tried_lock:
        if (kind, pid) in _tried:
            return False
        _tried.add((kind, pid))
        return True


def game_record(ctx: LaunchContext) -> dict | None:
    """Installations-Datensatz des Spiels; fehlt er (übernommene Spiele) oder stammt er von 0.1.6, wird er einmal je
    Programmlauf über das Content-System angelegt bzw. ergänzt."""
    pid = ctx.product_id
    rec = build_records.load_record(pid)
    if ctx.api is not None and not build_records.is_complete(rec) and _once("record", pid):
        ctx.status(tr('Installations-Datensatz anlegen …'))
        rec = build_records.ensure_record(ctx.api, ctx.game) or rec
    if ctx.api is not None and redist.needs_upgrade(rec) and _once("upgrade", pid):
        rec = redist.upgrade_record(ctx.api, ctx.game, rec) or rec
    return rec


def make_steps(ctx: LaunchContext, record: dict, with_redist: bool) -> list[redist.SetupStep]:
    entries = redist.record_global_entries(record, redist.cached_repository()) if with_redist else []
    language = ctx.game.language or str(record.get("language") or "") or ctx.settings.language
    return redist.build_steps(ctx.product_id, ctx.game.title, record, ctx.install_dir,
                              build_id=str(record.get("build_id") or ctx.game.build_id or ""),
                              version_name=str(record.get("version_name") or ctx.game.version_name or ""),
                              language=language, redist=entries)


def ensure_redists(ctx: LaunchContext, ids: list[str]) -> bool:
    """Globale Redistributables im gemeinsamen Ordner sicherstellen. True = alle da."""
    if not redist.missing_ids(ids):
        return True
    if ctx.api is None:
        return False
    ctx.status(tr('Galaxy-Redistributables laden …'))
    try:
        backend = attached_backend()
        if backend is not None:
            job = fetch_with_job(backend, ids, ctx.status)
            if job.state != "done":
                log.warning("Galaxy-Redistributables nicht geladen: %s", job.error or job.state)
        else:
            redist.download_global_redists(ctx.api, ids, workers=ctx.settings.max_workers, status=ctx.status)
    except (ApiError, NotLoggedIn, DownloadCancelled, OSError, ValueError) as exc:
        log.warning("Galaxy-Redistributables nicht geladen: %s", exc)
        ctx.log_line(f"--- Galaxy-Redistributables nicht geladen: {exc}")
    return not redist.missing_ids(ids)


def ensure_support(ctx: LaunchContext, record: dict, build_id: str) -> bool:
    """Support-Dateien des Builds sicherstellen (Nachladen braucht die GOG-Anmeldung). True = vorhanden."""
    if redist.support_ready(ctx.product_id, build_id):
        return True
    if ctx.api is None or not getattr(ctx.api, "logged_in", True):
        log.info("Support-Dateien für %s fehlen – ohne GOG-Anmeldung nicht ladbar", ctx.product_id)
        return False
    try:
        redist.download_support_files(ctx.api, ctx.product_id, record, workers=ctx.settings.max_workers,
                                      status=ctx.status)
    except (ApiError, NotLoggedIn, DownloadCancelled, OSError, ValueError) as exc:
        log.warning("Support-Dateien für %s nicht geladen: %s", ctx.product_id, exc)
        ctx.log_line(f"--- Support-Dateien nicht geladen: {exc}")
        return False
    return redist.support_ready(ctx.product_id, build_id)


def run_step(ctx: LaunchContext, step: redist.SetupStep, timeout: float | None = STEP_TIMEOUT) -> int:
    """Einen Schritt im Prefix ausführen (``prefix_command`` mit Verb ``run`` wie Heroic, Startordner wie dort).
    Ausgabe und Exit-Code ins Spielprotokoll. ``LaunchError`` (kein Proton/Wine) wird weitergereicht."""
    argv, env = ctx.launcher.prefix_command(ctx.product_id, list(step.argv), verb="run", extra_env=dict(SETUP_ENV))
    cwd = step.cwd if step.cwd and Path(step.cwd).is_dir() else None
    with open(ctx.log_file, "ab") as lf:
        lf.write(f"--- Einrichtung {step.key}: {shlex.join(argv)}\n".encode())
        lf.flush()
        try:
            rc = subprocess.run(argv, env=env, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = -1
            lf.write("--- Zeitüberschreitung\n".encode())
        except OSError as exc:
            rc = -2
            lf.write(f"--- Start fehlgeschlagen: {exc}\n".encode())
        lf.write(f"--- Einrichtung {step.key}: Exit-Code {rc}\n".encode())
    return rc


def _report_gave_up(ctx: LaunchContext, step: redist.SetupStep, rc: int | None) -> None:
    if rc is None:
        text = tr('„{0}“ fehlt und wird nicht mehr versucht.').format(step.title)
    else:
        text = tr('„{0}“ ist mehrmals fehlgeschlagen (Exit-Code {1}) und wird nicht mehr versucht – Details im '
                  'Spielprotokoll.').format(step.title, rc)
    log.warning("Einrichtung %s: %s nach %d Versuchen aufgegeben (Exit-Code %s)", ctx.product_id, step.key,
                redist.MAX_RETRIES + 1, rc)
    ctx.status(text)
    ctx.notify(text)


def _finish(ctx: LaunchContext, state: dict, build_id: str, dirty: bool) -> None:
    prod = state["products"].get(ctx.product_id) or {}
    if dirty or prod.get("build_id") != build_id or prod.get("install_dir") != str(ctx.install_dir):
        redist.mark_product(state, ctx.product_id, build_id, ctx.install_dir)
        redist.save_setup_state(ctx.prefix, state)
    done = redist.updated_setup_done(ctx.gs.setup_done, ctx.product_id, build_id)
    if done != list(ctx.gs.setup_done):
        ctx.update_game_settings(setup_done=done)


def setup_prefix(ctx: LaunchContext) -> dict[str, Any]:
    """Einrichtung des Prefix für dieses Spiel. Rückgabe für Protokoll/Tests:
    ``{mode, ran: [keys], failed: [keys], pending: [keys], finished: bool}``."""
    pid = ctx.product_id
    out: dict[str, Any] = {"mode": "", "ran": [], "failed": [], "pending": [], "finished": False}
    record = game_record(ctx)
    if not record or not record.get("build_id"):
        log.info("Einrichtung %s übersprungen: kein Installations-Datensatz mit Build", pid)
        return out
    build_id = str(record["build_id"])
    state = redist.load_setup_state(ctx.prefix)
    dirty = False
    if not redist.has_setup_state(ctx.prefix) and ctx.gs.redist_done and redist.prefix_initialized(ctx.prefix):
        n = redist.migrate_redist_done(state, ctx.gs.redist_done)
        dirty = dirty or bool(n)
        log.info("Einrichtung %s: %d Redistributables aus 0.1.x übernommen", pid, n)
    mode = redist.decide_force(state, pid, build_id, ctx.gs.setup_done, ctx.install_dir, ctx.prepare_only)
    out["mode"] = mode
    with_redist = bool(ctx.settings.install_redist and ctx.gs.install_redist)
    steps = make_steps(ctx, record, with_redist)
    keys = [s.key for s in steps]
    if mode == "all":
        redist.reset_steps(state, keys)
    elif mode == "scripts":
        redist.reset_steps(state, [s.key for s in steps if s.is_script])
    prod = state["products"].get(pid)
    if isinstance(prod, dict) and prod.pop("rerun", None):
        dirty = True
    dirty = dirty or bool(mode)
    due = [s for s in steps if redist.step_due(state, s.key)]
    if not due:
        _finish(ctx, state, build_id, dirty)
        out["finished"] = True
        return out
    redist.save_setup_state(ctx.prefix, state)
    log.info("Einrichtung %s (Build %s, %s): offen %s", pid, build_id, mode or "normal", ", ".join(s.key for s in due))

    # Programme besorgen: ISI/Redistributables (gemeinsamer Ordner), Support-Dateien
    need = [redist.ISI_ID] if any(s.kind == "isi" for s in due) else []
    need += [s.dep_id for s in due if s.kind == "redist" and not Path(s.exe).is_file()]
    cache_ok = ensure_redists(ctx, need) if need else True
    support_ok = True
    if any(s.kind in ("temp", "v1") and not Path(s.exe).is_file() for s in due) or (
            any(s.kind == "isi" for s in due) and not redist.support_ready(pid, build_id)):
        support_ok = ensure_support(ctx, record, build_id)

    steps = make_steps(ctx, record, with_redist)       # Pfade neu auflösen (jetzt im gemeinsamen Ordner)
    due = [s for s in steps if redist.step_due(state, s.key)]
    for i, step in enumerate(due, 1):
        if step.kind == "isi" and not support_ok:
            # ISI liest die Skripte aus /supportDir – ohne Support-Dateien bliebe es wirkungslos
            log.info("Einrichtung %s: %s bleibt offen (Support-Dateien fehlen)", pid, step.key)
            out["pending"].append(step.key)
            continue
        if not Path(step.exe).is_file():
            unavailable = not cache_ok if step.kind in ("isi", "redist") else not support_ok
            if unavailable:
                log.info("Einrichtung %s: %s bleibt offen (Programm nicht verfügbar: %s)", pid, step.key, step.exe)
                out["pending"].append(step.key)
                continue
            ctx.log_line(f"--- Einrichtung {step.key}: Programm fehlt: {step.exe}")
            rc, ok = None, False
        else:
            ctx.status(tr('Einrichtung {0}/{1}: {2} …').format(i, len(due), step.title))
            try:
                rc = run_step(ctx, step)
            except LaunchError as exc:
                log.warning("Einrichtung %s abgebrochen: %s", pid, exc)
                out["pending"].extend(s.key for s in due[i - 1:])
                break
            ok = rc in redist.OK_CODES
            out["ran"].append(step.key)
        log.info("Einrichtung %s: %s (%s) → %s", pid, step.key, step.title, "erledigt" if ok else f"Exit-Code {rc}")
        if not ok:
            out["failed"].append(step.key)
        if redist.record_result(state, step.key, ok, rc, step.title):
            _report_gave_up(ctx, step, rc)
        redist.save_setup_state(ctx.prefix, state)
    if redist.steps_finished(state, keys):
        _finish(ctx, state, build_id, True)
        out["finished"] = True
        log.info("Einrichtung %s abgeschlossen (Build %s)", pid, build_id)
    else:
        redist.save_setup_state(ctx.prefix, state)
        log.info("Einrichtung %s noch nicht vollständig – nächster Versuch beim nächsten Start", pid)
    return out


def prefix_status(launcher: Any, product_id: str) -> list[dict[str, Any]]:
    """Zeilen für „Status im Prefix“ (Eigenschaften, CLI) – ohne Netz."""
    pid = str(product_id)
    game = launcher.registry.get(pid)
    if not is_gog_windows(game):
        return []
    gs = launcher.game_settings.get(pid)
    prefix = launcher.prefix_for(pid, gs)
    state = redist.load_setup_state(prefix)
    record = build_records.load_record(pid) or {}
    steps: list[redist.SetupStep] = []
    if record.get("build_id"):
        entries = redist.record_global_entries(record, redist.cached_repository()) \
            if (launcher.settings.install_redist and gs.install_redist) else []
        steps = redist.build_steps(pid, game.title, record, game.install_path, build_id=str(record["build_id"]),
                                   version_name=str(record.get("version_name") or game.version_name or ""),
                                   language=game.language or str(record.get("language") or ""), redist=entries)
    return redist.status_rows(state, steps)


def request_rerun(launcher: Any, product_id: str) -> None:
    """„Einrichtung erneut ausführen“: Merker im Prefix, ``setup_done`` des Spiels leeren."""
    pid = str(product_id)
    gs = launcher.game_settings.get(pid)
    redist.request_rerun(launcher.prefix_for(pid, gs), pid)
    launcher.game_settings.update(pid, setup_done=redist.updated_setup_done(gs.setup_done, pid))
    log.info("Einrichtung %s wird beim nächsten Start wiederholt", pid)


class RedistHook(LaunchHook):
    name = "redist"
    order = 10
    setup = True

    def __init__(self) -> None:
        self.title = tr('Einrichtung im Prefix')

    def enabled(self, ctx: LaunchContext) -> bool:
        return ctx.is_windows and ctx.is_gog

    def prepare(self, ctx: LaunchContext) -> None:
        setup_prefix(ctx)


register_hook(RedistHook())
