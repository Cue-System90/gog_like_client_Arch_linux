"""Spielansicht → Extras wie in Galaxy 2.1: Offline-Installer (mit OS- und Sprachfilter), Patches, Sprachpakete,
Extras (Goodies), „Alle herunterladen“ und die Sicherungskopie der Cloud-Speicherstände.

Quelle ist ``api.gog.com/products/<id>?expand=downloads,expanded_dlcs`` (Basisspiel und DLCs im Besitz); jede Datei
wird über ihren ``downlink`` (mit Anmeldung) aufgelöst und fortsetzbar in den „Ordner für andere Downloads“ geladen
(Auftragsart ``extras`` – pausierbar, übersteht einen Neustart, hält die Bandbreitenbegrenzung ein).
"""
from __future__ import annotations

from ..i18n import N_, tr

import logging
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .downloader import DownloadCancelled, Progress

log = logging.getLogger(__name__)

JOB_KIND = "extras"
CLOUD_BACKUP_KIND = "cloud-backup"
KINDS = ("installer", "patch", "language_pack", "goodie")
KIND_NAMES = {"installer": N_("Offline-Versionen der Spiele-Installationsdateien"), "patch": N_("Patches"),
              "language_pack": N_("Sprachpakete"), "goodie": N_("Extras")}
OS_KEYS = ("windows", "linux", "mac")


@dataclass
class ExtraItem:
    key: str                      # eindeutig: <art>:<produkt>:<id>
    product_id: str
    product_title: str
    kind: str                     # installer | patch | language_pack | goodie
    name: str
    os: str = ""
    language: str = ""            # Kürzel wie „de“ ("" bei Extras)
    language_full: str = ""
    version: str = ""
    category: str = ""            # Art eines Extras (Handbuch, Soundtrack …)
    size: int = 0
    files: list[dict] = field(default_factory=list)   # [{id, size, downlink}]
    is_dlc: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExtraItem | None":
        try:
            return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        except (TypeError, KeyError):
            return None


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _files(raw: Any) -> list[dict]:
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict) and f.get("downlink"):
            out.append({"id": str(f.get("id") or ""), "size": _int(f.get("size")), "downlink": str(f["downlink"])})
    return out


def _items_of(product: dict, is_dlc: bool) -> list[ExtraItem]:
    pid = str(product.get("id") or "")
    title = str(product.get("title") or pid)
    dl = product.get("downloads") or {}
    out: list[ExtraItem] = []
    for kind, key in (("installer", "installers"), ("patch", "patches"), ("language_pack", "language_packs"),
                      ("goodie", "bonus_content")):
        for raw in dl.get(key) or []:
            if not isinstance(raw, dict):
                continue
            files = _files(raw.get("files"))
            if not files:
                continue
            size = _int(raw.get("total_size")) or sum(f["size"] for f in files)
            out.append(ExtraItem(
                key=f"{kind}:{pid}:{raw.get('id') or len(out)}", product_id=pid, product_title=title, kind=kind,
                name=str(raw.get("name") or title), os=str(raw.get("os") or "") if kind != "goodie" else "",
                language=str(raw.get("language") or "") if kind != "goodie" else "",
                language_full=str(raw.get("language_full") or "") if kind != "goodie" else "",
                version=str(raw.get("version") or ""), category=str(raw.get("type") or "") if kind == "goodie" else "",
                size=size, files=files, is_dlc=is_dlc))
    return out


def list_extras(product: dict | None, owned: set[str] | None = None) -> list[ExtraItem]:
    """Alle Dateien des Spiels und der DLCs im Besitz (``owned`` = None: alle DLCs)."""
    if not isinstance(product, dict):
        return []
    items = _items_of(product, False)
    for dlc in product.get("expanded_dlcs") or []:
        if isinstance(dlc, dict) and (owned is None or str(dlc.get("id")) in owned):
            items += _items_of(dlc, True)
    return items


def filter_items(items: Iterable[ExtraItem], os_name: str = "", language: str = "") -> list[ExtraItem]:
    """OS- und Sprachfilter der Extras-Ansicht; Extras (Goodies) gelten für jedes OS und jede Sprache."""
    out = []
    for it in items:
        if it.kind == "goodie":
            out.append(it)
            continue
        if os_name and it.os and it.os != os_name:
            continue
        if language and it.language and it.language != language:
            continue
        out.append(it)
    return out


def languages(items: Iterable[ExtraItem]) -> list[tuple[str, str]]:
    """(Kürzel, Name) aller Sprachen der Installer, nach Name sortiert."""
    seen: dict[str, str] = {}
    for it in items:
        if it.language and it.kind != "goodie":
            seen.setdefault(it.language, it.language_full or it.language)
    return sorted(seen.items(), key=lambda kv: kv[1].lower())


def default_extras_dir() -> Path:
    """Vorgabe für den „Ordner für andere Downloads“: <XDG-Download-Ordner>/GOG."""
    base = ""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                base = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
    except OSError:
        pass
    root = Path(base) if base and Path(base) != Path.home() else Path.home() / "Downloads"
    return root / "GOG"


def extras_dir(settings: Any) -> Path:
    raw = str(getattr(settings, "extras_dir", "") or "").strip()
    return Path(os.path.expanduser(raw)) if raw else default_extras_dir()


def game_dir(settings: Any, title: str) -> Path:
    from .installer import safe_dirname
    return extras_dir(settings) / safe_dirname(title)


def file_name(url: str, fallback: str) -> str:
    """Dateiname aus der Download-Adresse (ohne Pfadanteile); sonst ``fallback``."""
    from urllib.parse import unquote, urlsplit
    from ..store_links import safe_download_name
    name = unquote(urlsplit(url).path.rstrip("/").split("/")[-1])
    return safe_download_name(name) if name.strip(" ./") else safe_download_name(fallback)


def job_title(items: list[ExtraItem], game_title: str) -> str:
    """Bezeichnung in der Download-Ansicht wie Galaxy („… - Offline-Sicherungskopie …“)."""
    if items and all(i.kind == "installer" for i in items):
        return tr('{0} - Offline-Sicherungskopie der Spiele-Installationsdatei').format(game_title)
    if len(items) == 1:
        return f"{game_title} - {items[0].name}"
    return tr('{0} - Extras').format(game_title)


# --------------------------------------------------------------------------- Auftrag
def run_job(backend: Any, job: Any, on_progress: Callable[[Progress], None], status: Callable[[str], None]) -> list[str]:
    """Auftragsart ``extras``: alle Dateien der gewählten Einträge laden (vorhandene vollständige werden übersprungen,
    ``.part``-Dateien fortgesetzt). Rückgabe: die geladenen Dateien."""
    items = [i for i in (ExtraItem.from_dict(d) for d in job.data.get("items") or []) if i is not None]
    dest = Path(str(job.data.get("dest") or game_dir(backend.settings, job.title)))
    files = [(it, f) for it in items for f in it.files]
    total = sum(f["size"] for _it, f in files)
    prog = Progress(phase=tr('Wird heruntergeladen'), total_bytes=total, files_total=len(files), step="downloading")
    done_before = 0
    out: list[str] = []
    last = [time.monotonic(), 0]
    api = backend.api
    throttle = getattr(backend.installer, "throttle", None)
    for it, f in files:
        if job.cancel.is_set():
            raise DownloadCancelled()
        status(tr('{0} wird heruntergeladen …').format(it.name))
        info = api.downlink(f["downlink"])
        url = info.get("downlink") if isinstance(info, dict) else ""
        if not url:
            raise ValueError(tr('Download-Link konnte nicht aufgelöst werden.'))
        sub = dest / f"DLC - {file_name(it.product_title, it.product_id)}" if it.is_dlc else dest
        target = sub / file_name(url, f"{it.name}-{f['id']}")
        prog.current = target.name

        def cb(have: int, _total: int, base: int = done_before) -> None:
            now = time.monotonic()
            prog.done_bytes = prog.downloaded_bytes = base + have
            if now - last[0] >= 0.5:
                prog.speed = prog.write_speed = (prog.done_bytes - last[1]) / (now - last[0])
                last[0], last[1] = now, prog.done_bytes
            on_progress(prog)
        if target.is_file() and f["size"] and target.stat().st_size == f["size"]:
            cb(f["size"], f["size"])
        else:
            api.stream_download(url, target, cb, job.cancel, throttle=throttle)
        done_before += f["size"]
        prog.files_done += 1
        out.append(str(target))
        log.info("Extra geladen: %s → %s", it.name, target)
    prog.phase, prog.step, prog.done_bytes = tr('Fertig'), "finalizing", total
    on_progress(prog)
    return out


def new_job(backend: Any, product_id: str, game_title: str, items: list[ExtraItem]) -> Any:
    from ..backend import Job
    return Job(kind=JOB_KIND, product_id=str(product_id), title=job_title(items, game_title),
               data={"items": [i.to_dict() for i in items], "dest": str(game_dir(backend.settings, game_title)),
                     "game_title": game_title})


# --------------------------------------------------------------------------- Sicherungskopie der Cloud-Speicherstände
def cloud_backup(launcher: Any, api: Any, product_id: str, dest_dir: Path,
                 cancel: Any = None, status: Callable[[str], None] | None = None) -> Path:
    """Alle Cloud-Speicherstände des Spiels als ``<Titel> - Sicherungskopie … <Zeit>.tar.gz`` in ``dest_dir``."""
    from . import cloudsaves as cs
    gc = cs.game_cloud(launcher, api, product_id)
    tok = api.game_token(gc.client_id, gc.client_secret)
    store = cs.CloudStorage(str(getattr(tok, "user_id", "") or api.user_id), gc.client_id,
                            lambda force=False: api.game_token(gc.client_id, gc.client_secret).access_token)
    items = store.list()
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".cloud-backup-", dir=str(dest_dir)))
    try:
        n = 0
        for loc in gc.locations:
            for f in cs.cloud_files(items, loc.name):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                if status:
                    status(tr('{0} wird heruntergeladen …').format(f.rel))
                store.download(loc.name, f, (tmp / (loc.name.replace("/", "_") or "saves")).joinpath(*f.rel.split("/")))
                n += 1
        if not n:
            raise ValueError(tr('In der GOG Galaxy-Cloud liegen keine Spielstände für dieses Spiel.'))
        from .installer import safe_dirname
        stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        dest = dest_dir / f"{safe_dirname(tr('{0} - Sicherungskopie deiner Speicherstände').format(gc.title))} {stamp}.tar.gz"
        with tarfile.open(dest, "w:gz") as tar:
            for child in sorted(tmp.iterdir()):
                tar.add(str(child), arcname=child.name)
        log.info("Sicherungskopie der Cloud-Speicherstände von %s: %d Dateien → %s", product_id, n, dest)
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_cloud_backup(backend: Any, job: Any, on_progress: Callable[[Progress], None], status: Callable[[str], None]) -> str:
    on_progress(Progress(phase=tr('Cloud-Speicher wird synchronisiert'), step="cloud"))
    dest = Path(str(job.data.get("dest") or game_dir(backend.settings, job.title)))
    return str(cloud_backup(backend.launcher, backend.api, job.product_id, dest, job.cancel, status))


def new_cloud_backup_job(backend: Any, product_id: str, game_title: str) -> Any:
    from ..backend import Job
    return Job(kind=CLOUD_BACKUP_KIND, product_id=str(product_id),
               title=tr('{0} - Sicherungskopie deiner Speicherstände').format(game_title),
               data={"dest": str(game_dir(backend.settings, game_title)), "game_title": game_title})
