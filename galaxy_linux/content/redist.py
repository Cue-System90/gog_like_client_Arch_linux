"""GOG-Abhängigkeiten wie in GOG Galaxy und Heroic (gogdl): Redistributables, Spielordner-Pakete, Support-Dateien
und die Einrichtung im Prefix.

GOGs Abhängigkeits-Repository (``content-system.gog.com/dependencies/repository?generation=2``) beschreibt jedes Paket
mit ``executable.path``. Daran teilt gogdl die Pakete in zwei Klassen – genauso hier:

* **globale Redistributables** – ``executable.path`` beginnt mit ``__redist`` (ISI, MSVC*, DirectX, dotNet*, PhysX,
  XNA …): ein gemeinsamer Ordner ``~/.local/share/galaxy-linux/redist/gog/__redist/<ID>/…`` mit ``.redist-manifest``
  (Repository samt ``build_id``, installierte IDs, Dateiliste). Geladen als Auftrag „Galaxy-Redistributables“, im
  Prefix ausgeführt vor dem ersten Start. ISI (Install Script Interpreter) ist immer dabei.
* **Spielordner-Abhängigkeiten** – leerer oder anderer Pfad (DOSBox*, ScummVM, nGlide*, language_setup,
  SuspendLauncher, DOSBOXConfigurator …, auch mit ``internal: true``): gehören zum Spiel und landen beim
  Installieren, Aktualisieren und Prüfen im Spielordner (``content/installer.py``).

Dateien mit dem Manifest-Flag ``support`` (Installationsskripte, ``temp_executable``) liegen nicht im Spielordner,
sondern unter ``~/.local/share/galaxy-linux/gog-support/<id>/<productId>/…`` (wie ``gogdl --support``).

Einrichtung im Prefix (``launch/hook_redist.py``, Ablauf wie Heroics ``setup``):
  1. v2 mit ``scriptInterpreter``: ``__redist/ISI/scriptinterpreter.exe`` je Produkt (Basisspiel + installierte DLCs),
  2. v2 ohne: ``temp_executable`` des Produkts aus dem Support-Ordner,
  3. v1: ``support_commands``,
  4. die globalen Redistributables des Spiels (MSI-Pakete wie ``PHYSXLEGACY`` über ``msiexec /i …``).
Windows-Pfade sind ``Z:`` + Unix-Pfad (``Z:`` ist ``/`` in jedem Wine-/Proton-Prefix). Zustand je Prefix in
``<prefix>/galaxy-linux-setup.json``: Schritte mit Exit-Code und Fehlversuchen; ein fehlgeschlagener Schritt läuft
beim nächsten Start erneut, höchstens ``MAX_RETRIES``-mal, dann wird er einmal gemeldet.
"""
from __future__ import annotations

from ..i18n import tr

import dataclasses
import logging
import os
import shlex
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import paths
from ..api.client import CONTENT_SYSTEM, ApiError
from ..jsonstore import read_json, write_json
from .downloader import ChunkDownloader, DownloadCancelled, DownloadError, FileJob, Progress, SfcSource
from .manifest import (Build, BuildManifest, DepotFile, DepotManifest, DependencyDepot, ManifestError,
                       parse_dependency_repository)

log = logging.getLogger(__name__)

REDIST_PREFIX = "__redist"
ISI_ID = "ISI"
ISI_EXE = "__redist/ISI/scriptinterpreter.exe"
PHYSX_LEGACY = "PHYSXLEGACY"
CACHE_MANIFEST = ".redist-manifest"
SETUP_STATE = "galaxy-linux-setup.json"
SUPPORT_MARKER = ".galaxy-linux-support.json"
MAX_RETRIES = 3                                  # Erstversuch + höchstens 3 Wiederholungen bei späteren Starts
OK_CODES = frozenset({0, 1638, 1641, 3010})      # 1638: neuere Version schon da, 1641/3010: Neustart nötig
JOB_KIND = "redist"
JOB_PRODUCT = "gog-redist"                       # Pseudo-ID des Auftrags (wie Heroics „gog-redist“)
SCRIPT_KINDS = ("isi", "temp", "v1")

_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- Pfade
def redist_root() -> Path:
    """Gemeinsamer Ordner der globalen Redistributables (darin ``__redist/<ID>/…``)."""
    return paths.DATA_DIR / "redist" / "gog"


def cache_manifest_path() -> Path:
    return redist_root() / CACHE_MANIFEST


def support_root() -> Path:
    return paths.DATA_DIR / "gog-support"


def support_dir(product_id: str) -> Path:
    """Support-Ordner eines Spiels (darin ``<productId>/…``) – ``/supportDir`` für ISI."""
    return support_root() / str(product_id)


def setup_state_path(prefix: Path | str) -> Path:
    return Path(prefix) / SETUP_STATE


def to_posix(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("/")


def prefix_initialized(prefix: Path | str) -> bool:
    """Gibt es den Prefix schon (Wine: ``system.reg``, Proton/umu: ``pfx/system.reg``)?"""
    p = Path(prefix)
    return (p / "system.reg").is_file() or (p / "pfx" / "system.reg").is_file()


# --------------------------------------------------------------------------- Einteilung der Abhängigkeiten
def is_global_path(path: str) -> bool:
    """Wie gogdl: Pakete, deren ``executable.path`` mit ``__redist`` beginnt, sind global."""
    return to_posix(path).startswith(REDIST_PREFIX)


def is_global(dep: DependencyDepot) -> bool:
    return is_global_path(dep.executable_path)


def split_dependencies(ids: Iterable[str], repo: dict[str, DependencyDepot]) -> tuple[list[str], list[str], list[str]]:
    """(globale, Spielordner-, unbekannte) Abhängigkeiten in der Reihenfolge des Manifests, ohne Doppelte.
    ``internal`` spielt keine Rolle (gogdl wertet es nicht aus)."""
    glob: list[str] = []
    game: list[str] = []
    unknown: list[str] = []
    for i in ids:
        i = str(i)
        dep = repo.get(i)
        target = unknown if dep is None else glob if is_global(dep) else game
        if i not in target:
            target.append(i)
    return glob, game, unknown


def installer_entries(ids: Iterable[str], repo: dict[str, DependencyDepot]) -> list[dict[str, str]]:
    """Einträge ``{id, name, path, arguments}`` der globalen Pakete, die im Prefix installiert werden (ohne ISI –
    das ist das Werkzeug für die Installationsskripte, kein Installer)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i in ids:
        dep = repo.get(str(i))
        if dep is None or dep.dependency_id == ISI_ID or not is_global(dep) or dep.dependency_id in seen:
            continue
        seen.add(dep.dependency_id)
        out.append({"id": dep.dependency_id, "name": dep.readable_name, "path": dep.executable_path,
                    "arguments": dep.executable_args})
    return out


def record_products(record: dict, product_id: str) -> list[str]:
    """Basisspiel zuerst, dann die installierten DLCs."""
    pid = str(product_id)
    products = [str(p) for p in (record.get("products") or [pid, *(record.get("dlcs") or [])]) if p]
    if pid in products:
        products.remove(pid)
    return [pid, *dict.fromkeys(products)]


def record_dependency_ids(record: dict | None) -> list[str]:
    """Abhängigkeiten laut Datensatz; ältere Datensätze (bis 0.1.6) kennen nur ``redist``."""
    record = record or {}
    ids = record.get("dependency_ids")
    if isinstance(ids, list):
        return [str(i) for i in ids]
    return [str(r.get("id")) for r in record.get("redist") or [] if isinstance(r, dict) and r.get("id")]


def record_global_entries(record: dict | None, repo: dict[str, DependencyDepot] | None = None) -> list[dict[str, str]]:
    """Globale Redistributables, die für dieses Spiel im Prefix installiert werden. Mit Repository aus
    ``dependency_ids``, sonst aus der Liste ``redist`` des Datensatzes (auch Datensätze bis 0.1.6)."""
    record = record or {}
    if repo and isinstance(record.get("dependency_ids"), list):
        entries = installer_entries(record["dependency_ids"], repo)
        if entries or not record.get("redist"):
            return entries
    out = []
    for r in record.get("redist") or []:
        if isinstance(r, dict) and r.get("id") and r.get("id") != ISI_ID and is_global_path(str(r.get("path") or "")):
            out.append({"id": str(r["id"]), "name": str(r.get("name") or r["id"]), "path": to_posix(str(r["path"])),
                        "arguments": str(r.get("arguments") or "")})
    return out


def required_ids(records: Iterable[dict | None], repo: dict[str, DependencyDepot] | None = None) -> list[str]:
    """Wie Heroics ``getRequiredRedistList``: ISI plus die globalen Abhängigkeiten aller Datensätze."""
    out = [ISI_ID]
    for rec in records:
        if not rec:
            continue
        if repo:
            ids = split_dependencies(record_dependency_ids(rec), repo)[0]
        else:
            ids = [e["id"] for e in record_global_entries(rec)]
        for i in ids:
            if i not in out:
                out.append(i)
    return out


# --------------------------------------------------------------------------- gemeinsamer Ordner (Cache)
def load_cache_manifest() -> dict:
    data = read_json(cache_manifest_path(), {})
    return data if isinstance(data, dict) else {}


def cached_repository() -> dict[str, DependencyDepot]:
    """Repository aus dem ``.redist-manifest`` (ohne Netz; leer, solange nichts geladen wurde)."""
    try:
        return parse_dependency_repository(load_cache_manifest())
    except (ManifestError, TypeError, ValueError, AttributeError):
        return {}


def installed_ids() -> list[str]:
    ids = load_cache_manifest().get("installed")
    return [str(i) for i in ids] if isinstance(ids, list) else []


def missing_ids(ids: Iterable[str], repo: dict[str, DependencyDepot] | None = None) -> list[str]:
    """IDs, die im gemeinsamen Ordner fehlen (nicht geladen oder Programmdatei weg)."""
    manifest = load_cache_manifest()
    have = {str(i) for i in manifest.get("installed") or []} if isinstance(manifest.get("installed"), list) else set()
    if repo is None:
        try:
            repo = parse_dependency_repository(manifest)
        except (ManifestError, TypeError, ValueError, AttributeError):
            repo = {}
    root = redist_root()
    out = []
    for i in ids:
        i = str(i)
        dep = repo.get(i)
        if i not in have or (dep is not None and dep.executable_path and not (root / dep.executable_path).is_file()):
            if i not in out:
                out.append(i)
    return out


def cache_outdated(build_id: str) -> bool:
    """Hat sich das Repository seit dem letzten Download geändert?"""
    old = str(load_cache_manifest().get("build_id") or "")
    return bool(old and build_id and old != str(build_id))


def cache_info() -> dict[str, Any]:
    """Für Anzeige und CLI: Ordner, Repository-Build, installierte IDs, Größe."""
    root = redist_root()
    m = load_cache_manifest()
    size = 0
    base = root / REDIST_PREFIX
    if base.is_dir():
        for p in base.rglob("*"):
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                pass
    return {"path": str(root), "build_id": str(m.get("build_id") or ""), "installed": installed_ids(), "size": size,
            "updated_at": float(m.get("updated_at") or 0)}


def repository_info(api) -> dict:
    """Kleine Antwort des Content-Systems: ``build_id`` und Link zum Repository-Manifest (öffentlich)."""
    d = api.get_json(f"{CONTENT_SYSTEM}/dependencies/repository", params={"generation": 2}, authenticated=False)
    if not isinstance(d, dict) or not d.get("repository_manifest"):
        raise ApiError(tr('Abhängigkeits-Repository nicht verfügbar'))
    return d


def fetch_repository(api) -> tuple[str, dict]:
    """(build_id, Repository-Manifest) von GOG – öffentlich, ohne Anmeldung."""
    d = repository_info(api)
    raw = api.zlib_json(d["repository_manifest"])
    if not isinstance(raw, dict):
        raise ApiError(tr('Abhängigkeits-Repository nicht verfügbar'))
    return str(d.get("build_id") or raw.get("build_id") or ""), raw


def _prune_empty(base: Path) -> None:
    if not base.is_dir():
        return
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(str(p)), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def download_global_redists(api, ids: Iterable[str], workers: int = 6, progress_cb: Callable[[Progress], None] | None = None,
                            cancel: threading.Event | None = None, status: Callable[[str], None] | None = None) -> list[str]:
    """Globale Redistributables (immer mit ISI) in den gemeinsamen Ordner laden bzw. aktualisieren – wie
    ``gogdl redist --ids … --path …``. Schon geladene IDs bleiben erhalten (andere Spiele brauchen sie).
    Gibt die installierten IDs zurück. ``ApiError`` bei Netz-/Downloadfehlern, ``DownloadCancelled`` bei Abbruch."""
    say = status or (lambda _t: None)
    requested = [str(i) for i in ids]
    with _cache_lock:
        say(tr('Abhängigkeits-Repository abrufen …'))
        build_id, raw = fetch_repository(api)
        try:
            repo = parse_dependency_repository(raw)
        except (ManifestError, TypeError, ValueError, AttributeError) as exc:
            raise ApiError(tr('Abhängigkeits-Repository nicht verfügbar')) from exc
        old = load_cache_manifest()
        wanted: list[str] = []
        for i in [ISI_ID, *requested, *[str(x) for x in old.get("installed") or []]]:
            dep = repo.get(i)
            if dep is None:
                if i in requested:
                    log.warning("Abhängigkeit %s steht nicht im GOG-Repository", i)
                continue
            if is_global(dep) and i not in wanted:
                wanted.append(i)
        root = redist_root()
        if str(old.get("build_id") or "") == build_id and not missing_ids(wanted, repo):
            log.info("Galaxy-Redistributables aktuell (Repository %s): %s", build_id, ", ".join(wanted))
            return wanted
        endpoint = api.dependency_link()
        jobs: list[FileJob] = []
        sfc: dict[str, SfcSource] = {}
        dirs: list[str] = []
        links = []
        for i in wanted:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            dep = repo[i]
            say(tr('Manifest {0} laden …').format(dep.readable_name))
            try:
                dm = DepotManifest.parse(api.dependency_depot_manifest(dep.manifest), "", dep.manifest)
            except ManifestError as exc:
                raise ApiError(str(exc)) from exc
            if dm.small_files_container:
                sfc[dep.manifest] = SfcSource(endpoint, dm.small_files_container)
            jobs.extend(FileJob(f, endpoint, dep.manifest if f.sfc_ref else "") for f in dm.files)
            dirs.extend(dm.directories)
            links.extend(dm.links)
        dl = ChunkDownloader(api.new_session, workers=workers, progress_cb=progress_cb, cancel=cancel)
        try:
            dl.download(jobs, root, sfc, dirs, links, verify=True)
        except DownloadError as exc:
            raise ApiError(str(exc)) from exc
        new_files = sorted({j.file.path for j in jobs})
        keep = set(new_files)
        for p in old.get("files") or []:
            p = to_posix(str(p))
            if p in keep or not p.startswith(REDIST_PREFIX + "/") or ".." in p.split("/"):
                continue
            try:
                (root / p).unlink()
            except OSError:
                pass
        _prune_empty(root / REDIST_PREFIX)
        write_json(cache_manifest_path(), {**raw, "build_id": build_id, "installed": wanted, "files": new_files,
                                           "updated_at": time.time()})
        log.info("Galaxy-Redistributables geladen (Repository %s): %s – %d Dateien", build_id, ", ".join(wanted), len(jobs))
        return wanted


# --------------------------------------------------------------------------- Support-Dateien
def support_file_job(f: DepotFile, product_id: str, endpoint: str, sfc_key: str = "", root: Path | None = None) -> FileJob:
    """Support-Datei unter ``<productId>/<Pfad>`` (relativ zum Support-Ordner des Spiels), wie gogdl. Mit ``root``
    landet sie dort statt im Zielordner des Downloads (Installation: Spielordner)."""
    moved = dataclasses.replace(f, path=f"{product_id}/{f.path}")
    return FileJob(moved, endpoint, sfc_key if f.sfc_ref else "", root)


def write_support_marker(product_id: str, build_id: str, products: Iterable[str], files: int) -> None:
    write_json(support_dir(product_id) / SUPPORT_MARKER, {"build_id": str(build_id), "products": list(products),
                                                         "files": int(files), "updated_at": time.time()})


def support_ready(product_id: str, build_id: str) -> bool:
    """Liegen die Support-Dateien zu diesem Build vor (Installation oder Nachladen)?"""
    m = read_json(support_dir(product_id) / SUPPORT_MARKER, None)
    return isinstance(m, dict) and str(m.get("build_id") or "") == str(build_id)


def remove_support(product_id: str) -> None:
    d = support_dir(product_id)
    if str(product_id) and d.is_dir() and d.parent == support_root():
        shutil.rmtree(d, ignore_errors=True)


def _record_build(api, product_id: str, record: dict) -> Build | None:
    from .build_records import pick_build
    return pick_build([Build.from_api(b) for b in api.builds(product_id, "windows")], str(record.get("build_id") or ""))


def download_support_files(api, product_id: str, record: dict, workers: int = 6,
                           progress_cb: Callable[[Progress], None] | None = None, cancel: threading.Event | None = None,
                           status: Callable[[str], None] | None = None) -> int:
    """Nur die Support-Dateien eines installierten Spiels nachladen (übernommene Spiele, Installationen vor 0.2.0).
    Braucht die GOG-Anmeldung (secure_link). Gibt die Zahl der Dateien zurück."""
    pid = str(product_id)
    say = status or (lambda _t: None)
    say(tr('Support-Dateien laden …'))
    link = str(record.get("manifest_link") or "")
    if not link:
        build = _record_build(api, pid, record)
        if build is None:
            raise ApiError(tr('Kein Galaxy-Build für {0}').format(pid))
        link = build.link
    try:
        manifest = BuildManifest.parse(api.zlib_json(link))
    except ManifestError as exc:
        raise ApiError(str(exc)) from exc
    products = record_products(record, pid)
    base = manifest.base_product_id or pid
    depots, _lang = manifest.select_depots(str(record.get("language") or "en-US"), set(products), include_dlcs=True)
    jobs: list[FileJob] = []
    sfc: dict[str, SfcSource] = {}
    endpoints: dict[str, str] = {}
    for d in depots:
        owner = d.product_id or base
        if owner not in products and owner != base:
            continue
        try:
            dm = DepotManifest.parse(api.depot_manifest(d.manifest), owner, d.manifest)
        except ManifestError as exc:
            raise ApiError(str(exc)) from exc
        files = [f for f in dm.files if f.is_support]
        if not files:
            continue
        if owner not in endpoints:
            endpoints[owner] = api.secure_link(owner)
        if any(f.sfc_ref for f in files) and dm.small_files_container:
            sfc[d.manifest] = SfcSource(endpoints[owner], dm.small_files_container)
        jobs.extend(support_file_job(f, owner, endpoints[owner], d.manifest) for f in files)
    if jobs:
        dl = ChunkDownloader(api.new_session, workers=workers, progress_cb=progress_cb, cancel=cancel)
        try:
            dl.download(jobs, support_dir(pid), sfc, verify=True)
        except DownloadError as exc:
            raise ApiError(str(exc)) from exc
    write_support_marker(pid, str(record.get("build_id") or manifest.build_id), products, len(jobs))
    log.info("Support-Dateien für %s: %d Dateien in %s", pid, len(jobs), support_dir(pid))
    return len(jobs)


def needs_upgrade(record: dict | None) -> bool:
    """Datensatz bis 0.1.6 (ohne ``script_interpreter``/``support``) – einmal aus dem Build-Manifest ergänzen."""
    return bool(record and record.get("build_id") and "script_interpreter" not in record)


def upgrade_record(api, game, record: dict) -> dict | None:
    """Datensätze bis 0.1.6 kennen weder ``script_interpreter`` noch ``support``/``products``: einmal aus dem
    Build-Manifest ergänzen (Herkunft und Dateiliste bleiben). None bei Netzwerkfehlern."""
    from .build_records import record_path
    pid = str(game.product_id)
    try:
        build = _record_build(api, pid, record)
        if build is None:
            return None
        manifest = BuildManifest.parse(api.zlib_json(build.link))
    except (ApiError, ManifestError, ValueError, KeyError, OSError) as exc:
        log.warning("Installations-Datensatz %s nicht ergänzbar: %s", pid, exc)
        return None
    rec = dict(record)
    rec.setdefault("dependency_ids", list(manifest.dependencies))
    rec["script_interpreter"] = manifest.script_interpreter
    rec["support"] = list(manifest.support)
    rec.setdefault("products", [pid, *[str(x) for x in rec.get("dlcs") or []]])
    rec.setdefault("version_name", build.version_name or getattr(game, "version_name", ""))
    rec.setdefault("manifest_link", build.link)
    write_json(record_path(pid), rec)
    log.info("Installations-Datensatz %s ergänzt: Skript-Interpreter %s, Support-Programme %d", pid,
             manifest.script_interpreter, len(manifest.support))
    return rec


# --------------------------------------------------------------------------- Windows-Pfade, Sprache
def to_windows_path(path: Path | str) -> str:
    """Unix-Pfad → Windows-Pfad im Prefix (``Z:`` ist ``/`` in Wine/Proton), z. B. ``Z:\\mnt\\Spiele\\No Man's Sky``.
    Leerzeichen und Apostrophe bleiben unverändert – die Befehle laufen ohne Shell (argv-Liste)."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    return "Z:" + p.replace("/", "\\")


# Englische Sprachnamen wie Intl.DisplayNames(["en"], {type: "language"}) in Heroic, dazu der übliche GOG-Code
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "en-US"), "de": ("German", "de-DE"), "fr": ("French", "fr-FR"), "es": ("Spanish", "es-ES"),
    "it": ("Italian", "it-IT"), "pl": ("Polish", "pl-PL"), "ru": ("Russian", "ru-RU"), "pt": ("Portuguese", "pt-BR"),
    "zh": ("Chinese", "zh-Hans"), "ja": ("Japanese", "ja-JP"), "ko": ("Korean", "ko-KR"), "cs": ("Czech", "cs-CZ"),
    "hu": ("Hungarian", "hu-HU"), "tr": ("Turkish", "tr-TR"), "nl": ("Dutch", "nl-NL"), "sv": ("Swedish", "sv-SE"),
    "uk": ("Ukrainian", "uk-UA"), "ar": ("Arabic", "ar"), "da": ("Danish", "da-DK"), "fi": ("Finnish", "fi-FI"),
    "nb": ("Norwegian Bokmål", "nb-NO"), "no": ("Norwegian", "no-NO"), "el": ("Greek", "el-GR"),
    "ro": ("Romanian", "ro-RO"), "th": ("Thai", "th-TH"), "bg": ("Bulgarian", "bg-BG"), "sk": ("Slovak", "sk-SK"),
    "et": ("Estonian", "et-EE"), "he": ("Hebrew", "he-IL"), "be": ("Belarusian", "be-BY"), "sr": ("Serbian", "sr-RS"),
    "hr": ("Croatian", "hr-HR"), "lt": ("Lithuanian", "lt-LT"), "lv": ("Latvian", "lv-LV"),
    "vi": ("Vietnamese", "vi-VN"), "id": ("Indonesian", "id-ID"), "ca": ("Catalan", "ca-ES"),
    "sl": ("Slovenian", "sl-SI"), "is": ("Icelandic", "is-IS"), "fa": ("Persian", "fa-IR"), "hi": ("Hindi", "hi-IN"),
}
_NATIVE = {"deutsch": "de", "français": "fr", "francais": "fr", "español": "es", "espanol": "es", "italiano": "it",
           "polski": "pl", "русский": "ru", "português": "pt", "portugues": "pt", "português do brasil": "pt",
           "brazilian": "pt", "brazilian portuguese": "pt", "中文": "zh", "日本語": "ja", "한국어": "ko", "český": "cs",
           "magyar": "hu", "türkçe": "tr", "nederlands": "nl", "svenska": "sv", "українська": "uk",
           "simplified chinese": "zh", "traditional chinese": "zh"}


def _lang_key(lang: str) -> str:
    t = (lang or "").strip()
    if not t:
        return "en"
    low = t.lower()
    base = low.replace("_", "-").split("-")[0]
    if base in LANGUAGES and (len(low) <= 3 or "-" in low or "_" in low):
        return base
    for code, (name, _full) in LANGUAGES.items():
        if low == name.lower():
            return code
    return _NATIVE.get(low, base if base in LANGUAGES else "")


def language_name(lang: str) -> str:
    """Englischer Sprachname für ``/Language=`` (``de-DE`` → ``German``); unbekannt → ``English``."""
    key = _lang_key(lang)
    return LANGUAGES[key][0] if key in LANGUAGES else "English"


def language_code(lang: str) -> str:
    """Code für ``/lang-code=`` (``de-DE`` bleibt, ``German`` → ``de-DE``); unbekannt → ``en-US``."""
    t = (lang or "").strip()
    if t and "-" in t and t.split("-")[0].lower() in LANGUAGES:
        return t
    key = _lang_key(t)
    return LANGUAGES[key][1] if key in LANGUAGES else "en-US"


# --------------------------------------------------------------------------- Befehle der Einrichtung
def isi_argv(isi_exe: str, game_dir_win: str, language: str, product_id: str, build_id: str, version_name: str,
             lang_code: str, support_dir_win: str) -> list[str]:
    """``scriptinterpreter.exe`` mit genau Heroics Schaltern (inkl. GOGs Tippfehler ``/nodesktopshorctut``)."""
    return [isi_exe, "/VERYSILENT", f"/DIR={game_dir_win}", f"/Language={language}", f"/LANG={language}",
            f"/ProductId={product_id}", "/galaxyclient", f"/buildId={build_id}", f"/versionName={version_name}",
            f"/lang-code={lang_code}", f"/supportDir={support_dir_win}", "/nodesktopshorctut", "/nodesktopshortcut"]


def temp_executable_argv(exe: str, game_dir_win: str, language: str, lang_code: str, product_id: str, build_id: str,
                         version_name: str) -> list[str]:
    """``temp_executable`` (v2 ohne ScriptInterpreter) wie Heroic: Sprache klein, ohne ``/supportDir``."""
    lang = language.lower()
    return [exe, "/VERYSILENT", f"/DIR={game_dir_win}", f"/Language={lang}", f"/LANG={lang}", f"/lang-code={lang_code}",
            f"/ProductId={product_id}", "/galaxyclient", f"/buildId={build_id}", f"/versionName={version_name}",
            "/nodesktopshorctut", "/nodesktopshortcut"]


def v1_support_argv(exe: str, game_dir_win: str, language: str, product_id: str, build_id: str,
                    version_name: str) -> list[str]:
    """v1-``support_commands`` wie Heroic – aber mit der echten Produkt-ID (Heroic übergibt dort die Sprache)."""
    lang = language.lower()
    return [exe, "/VERYSILENT", f"/DIR={game_dir_win}", f"/Language={lang}", f"/LANG={lang}", f"/ProductId={product_id}",
            "/galaxyclient", f"/buildId={build_id}", f"/versionName={version_name}", "/nodesktopshorctut",
            "/nodesktopshortcut"]


def split_args(arguments: str) -> list[str]:
    """Argumente eines Redistributables wie Heroic (``shlex.split``); bei Fehlern Windows-Zerlegung."""
    try:
        return shlex.split(arguments or "")
    except ValueError:
        from ..launch.launcher import split_windows_args
        return split_windows_args(arguments)


def redist_argv(dep_id: str, exe: str, arguments: str) -> list[str]:
    """Installer im Prefix. MSI-Pakete laufen über ``msiexec /i`` – ``PHYSXLEGACY`` wie bei Heroic mit ``/qb`` am
    Ende (XNA ebenso über msiexec, ein MSI lässt sich nicht direkt starten)."""
    if dep_id == PHYSX_LEGACY:
        return ["msiexec", "/i", to_windows_path(exe), *split_args(arguments), "/qb"]
    if str(exe).lower().endswith(".msi"):
        return ["msiexec", "/i", to_windows_path(exe), *split_args(arguments)]
    return [exe, *split_args(arguments)]


def resolve_redist_exe(path: str, install_dir: Path | str) -> Path:
    """Installer im gemeinsamen Ordner; Installationen bis 0.1.6 haben ihn noch im Spielordner (``__redist/…``)."""
    rel = to_posix(path)
    cached = redist_root() / rel
    if cached.is_file():
        return cached
    old = Path(install_dir) / rel
    return old if old.is_file() else cached


def resolve_support_exe(product_id: str, owner: str, path: str, install_dir: Path | str) -> Path:
    """Support-Programm im Support-Ordner; Installationen bis 0.1.6 haben es noch im Spielordner."""
    rel = to_posix(path)
    new = support_dir(product_id) / str(owner) / rel
    if new.is_file():
        return new
    old = Path(install_dir) / rel
    return old if old.is_file() else new


@dataclass
class SetupStep:
    key: str            # isi:<produkt>:<build> | temp:… | v1:… | redist:<ID>
    kind: str           # isi | temp | v1 | redist
    title: str
    argv: list[str]
    cwd: str
    exe: str
    product_id: str = ""
    dep_id: str = ""

    @property
    def is_script(self) -> bool:
        return self.kind in SCRIPT_KINDS


def build_steps(product_id: str, title: str, record: dict, install_dir: Path | str, *, build_id: str,
                version_name: str, language: str, redist: list[dict] | None = None,
                product_names: dict[str, str] | None = None) -> list[SetupStep]:
    """Alle Einrichtungsschritte eines Spiels in Heroics Reihenfolge: Skripte je Produkt, dann Redistributables."""
    pid = str(product_id)
    install_dir = Path(install_dir)
    game_win = to_windows_path(install_dir)
    names = dict(product_names or {})
    names.setdefault(pid, title)
    for s in record.get("support") or []:
        if isinstance(s, dict) and s.get("product_id") and s.get("name"):
            names.setdefault(str(s["product_id"]), str(s["name"]))
    lang_name, code = language_name(language), language_code(language)
    support = support_dir(pid)
    products = record_products(record, pid)
    steps: list[SetupStep] = []
    if record.get("script_interpreter"):
        isi = str(redist_root() / ISI_EXE)
        for prod in products:
            steps.append(SetupStep(
                f"isi:{prod}:{build_id}", "isi", tr('Installationsskript (ISI) für {0}').format(names.get(prod, prod)),
                isi_argv(isi, game_win, lang_name, prod, build_id, version_name, code, to_windows_path(support)),
                str(install_dir), isi, product_id=prod))
    else:
        for s in record.get("support") or []:
            prod = str((s or {}).get("product_id") or "")
            if prod not in products or not (s or {}).get("temp_executable"):
                continue
            exe = str(resolve_support_exe(pid, prod, str(s["temp_executable"]), install_dir))
            steps.append(SetupStep(
                f"temp:{prod}:{build_id}", "temp", tr('Setup-Programm für {0}').format(names.get(prod, prod)),
                temp_executable_argv(exe, game_win, lang_name, code, prod, build_id, version_name),
                str(install_dir), exe, product_id=prod))
        wanted = {lang_name.lower(), str(language or "").lower(), "neutral"}
        for cmd in record.get("support_commands") or []:
            if not isinstance(cmd, dict) or not cmd.get("executable"):
                continue
            if not wanted & {str(x).lower() for x in cmd.get("languages") or []}:
                continue
            prod = str(cmd.get("gameID") or pid)
            exe = str(resolve_support_exe(pid, prod, str(cmd["executable"]), install_dir))
            steps.append(SetupStep(
                f"v1:{prod}:{build_id}", "v1", tr('Setup-Programm für {0}').format(names.get(prod, prod)),
                v1_support_argv(exe, game_win, lang_name, prod, build_id, version_name),
                str(install_dir), exe, product_id=prod))
    for e in redist or []:
        exe = resolve_redist_exe(e["path"], install_dir)
        cwd = str(redist_root()) if exe.is_relative_to(redist_root()) else str(install_dir)
        steps.append(SetupStep(f"redist:{e['id']}", "redist", str(e.get("name") or e["id"]),
                               redist_argv(e["id"], str(exe), str(e.get("arguments") or "")), cwd, str(exe),
                               dep_id=str(e["id"])))
    return steps


# --------------------------------------------------------------------------- Zustand je Prefix
def load_setup_state(prefix: Path | str) -> dict:
    raw = read_json(setup_state_path(prefix), None)
    st = raw if isinstance(raw, dict) else {}
    for key, kind in (("steps", dict), ("products", dict), ("reported", list)):
        if not isinstance(st.get(key), kind):
            st[key] = kind()
    st["version"] = 1
    return st


def has_setup_state(prefix: Path | str) -> bool:
    return setup_state_path(prefix).is_file()


def save_setup_state(prefix: Path | str, state: dict) -> None:
    write_json(setup_state_path(prefix), state)


def _tries(entry: dict | None) -> int:
    try:
        return int((entry or {}).get("tries") or 0)
    except (TypeError, ValueError):
        return 0


def step_due(state: dict, key: str) -> bool:
    """Noch nicht erledigt und nicht aufgegeben (Erstversuch + höchstens MAX_RETRIES Wiederholungen)?"""
    s = state["steps"].get(key)
    if not s:
        return True
    if s.get("ok"):
        return False
    return _tries(s) <= MAX_RETRIES


def step_gave_up(state: dict, key: str) -> bool:
    s = state["steps"].get(key)
    return bool(s) and not s.get("ok") and _tries(s) > MAX_RETRIES


def record_result(state: dict, key: str, ok: bool, rc: int | None, title: str = "") -> bool:
    """Ergebnis eines Schritts festhalten. True genau dann, wenn der Schritt damit endgültig aufgegeben ist und das
    noch nicht gemeldet wurde (→ einmal anzeigen)."""
    s = state["steps"].setdefault(key, {})
    s.update(ok=bool(ok), rc=rc, at=time.time(), title=title or s.get("title", ""))
    s["tries"] = 0 if ok else _tries(s) + 1
    if not ok and s["tries"] > MAX_RETRIES and key not in state["reported"]:
        state["reported"].append(key)
        return True
    return False


def reset_steps(state: dict, keys: Iterable[str]) -> None:
    for k in keys:
        state["steps"].pop(k, None)
        if k in state["reported"]:
            state["reported"].remove(k)


def steps_finished(state: dict, keys: Iterable[str]) -> bool:
    """Alle Schritte erledigt oder aufgegeben?"""
    return all(not step_due(state, k) for k in keys)


def mark_product(state: dict, product_id: str, build_id: str, install_dir: Path | str) -> None:
    state["products"][str(product_id)] = {"build_id": str(build_id), "install_dir": str(install_dir), "at": time.time()}


def migrate_redist_done(state: dict, ids: Iterable[str]) -> int:
    """Bis 0.2.0 installierte Redistributables (``GameSettings.redist_done``) gelten im Prefix als erledigt."""
    n = 0
    for i in ids:
        key = f"redist:{i}"
        if key not in state["steps"]:
            state["steps"][key] = {"ok": True, "rc": None, "tries": 0, "at": 0, "title": str(i), "migrated": True}
            n += 1
    return n


def setup_key(product_id: str, build_id: str) -> str:
    """Eintrag in ``GameSettings.setup_done``."""
    return f"{product_id}:{build_id}"


def updated_setup_done(setup_done: Iterable[str], product_id: str, build_id: str = "") -> list[str]:
    """``setup_done`` ohne alte Einträge des Spiels, mit ``<id>:<build>`` (leerer Build = nur entfernen)."""
    pid = str(product_id)
    out = [k for k in setup_done if not str(k).startswith(pid + ":")]
    if build_id:
        out.append(setup_key(pid, build_id))
    return out


def decide_force(state: dict, product_id: str, build_id: str, setup_done: Iterable[str], install_dir: Path | str,
                 prepare_only: bool = False) -> str:
    """Was muss neu laufen? ``"all"`` / ``"scripts"`` / ``""``:
    * ``run_prepare_only`` (CLI ``prefix setup``), „Einrichtung erneut ausführen“ (Merker im Prefix) oder ein
      gelöschter ``setup_done``-Eintrag bei fertiger Einrichtung → alles;
    * Spielordner verschoben → nur die Skripte (sie schreiben den Pfad in die Registry, wie Heroic nach dem
      Verschieben). Nach einem Update laufen die Skripte ohnehin neu – ihre Schlüssel enthalten den Build."""
    prod = state["products"].get(str(product_id)) or {}
    if prepare_only or prod.get("rerun"):
        return "all"
    if prod.get("build_id") == str(build_id) and setup_key(product_id, build_id) not in set(setup_done):
        return "all"
    if prod.get("install_dir") and prod.get("install_dir") != str(install_dir):
        return "scripts"
    return ""


def request_rerun(prefix: Path | str, product_id: str) -> None:
    """„Einrichtung erneut ausführen“: beim nächsten Start alle Schritte des Spiels wiederholen."""
    state = load_setup_state(prefix)
    state["products"].setdefault(str(product_id), {})["rerun"] = True
    save_setup_state(prefix, state)


def status_rows(state: dict, steps: list[SetupStep]) -> list[dict[str, Any]]:
    """Zeilen für „Status im Prefix“: die Schritte des Spiels, dann Redistributables anderer Spiele im Prefix."""
    rows = []
    own = set()
    for s in steps:
        own.add(s.key)
        e = state["steps"].get(s.key) or {}
        st = "ok" if e.get("ok") else "gave_up" if step_gave_up(state, s.key) else "failed" if e else "pending"
        rows.append({"key": s.key, "title": s.title, "state": st, "rc": e.get("rc"), "tries": _tries(e),
                     "at": float(e.get("at") or 0), "kind": s.kind})
    for key, e in sorted(state["steps"].items()):
        if key in own or not key.startswith("redist:") or not e.get("ok"):
            continue
        rows.append({"key": key, "title": str(e.get("title") or key[7:]), "state": "other", "rc": e.get("rc"),
                     "tries": 0, "at": float(e.get("at") or 0), "kind": "redist"})
    return rows


def state_text(row: dict[str, Any]) -> str:
    """Zustand einer Zeile aus ``status_rows`` als Text (Eigenschaften, CLI)."""
    st = row.get("state")
    rc = "–" if row.get("rc") is None else str(row.get("rc"))
    if st == "ok":
        return tr('Ausgeführt') if row.get("kind") in SCRIPT_KINDS else tr('Installiert')
    if st == "other":
        return tr('Installiert (für ein anderes Spiel in diesem Prefix)')
    if st == "pending":
        return tr('Ausstehend – läuft beim nächsten Start')
    if st == "gave_up":
        return tr('Fehlgeschlagen (Exit-Code {0}) – wird nicht mehr versucht').format(rc)
    return tr('Fehlgeschlagen (Exit-Code {0}) – neuer Versuch beim nächsten Start ({1}/{2})').format(
        rc, row.get("tries", 0), MAX_RETRIES + 1)


def summary_text(rows: list[dict[str, Any]]) -> str:
    """Kurzfassung „x von y erledigt“ (nur die Schritte des Spiels)."""
    own = [r for r in rows if r.get("state") != "other"]
    if not own:
        return tr('Nichts einzurichten')
    done = sum(1 for r in own if r.get("state") == "ok")
    return tr('{0} von {1} erledigt').format(done, len(own))
