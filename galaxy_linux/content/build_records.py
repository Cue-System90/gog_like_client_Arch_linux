"""Installations-Datensätze ``~/.local/share/galaxy-linux/manifests/<id>.json`` für alle installierten GOG-Spiele.

Der Installer schreibt den Datensatz bei jeder Content-System-Installation. Für übernommene (kopierte) Spiele und
Offline-Installer-Spiele fehlt er – damit fehlen auch ``client_id``/``client_secret`` (Erfolge, Cloud-Speicherstände,
comet) und die Abhängigkeiten (Redistributables, ISI). ``ensure_record()`` legt ihn nach:

  1. ``goggame-<id>.info`` / ``goggame-<id>.id`` im Spielordner lesen (buildId, clientId, Sprache, vorhandene DLCs),
  2. Content-System-Builds (generation 2, windows) abrufen und den passenden Build wählen (sonst den neuesten),
  3. Build-Manifest laden (``content/manifest.py``) und den Datensatz im Format des Installers schreiben.

Felder (wie ``InstallManager.install()``, ergänzt): product_id, build_id, platform, language, install_dir, files,
redist [{id, name, path, arguments}], dependencies (lesbare Namen), dependency_ids, client_id, client_secret, dlcs,
products, script_interpreter, support [{product_id, name, temp_executable, temp_arguments}], version_name,
manifest_link, origin, updated_at.

Client-Secrets werden nie protokolliert.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .. import paths
from ..api.auth import AuthError
from ..api.client import ApiError
from ..jsonstore import read_json, write_json
from .manifest import Build, BuildManifest, DependencyDepot, ManifestError, parse_dependency_repository
from .redist import ISI_ID, is_global
from .scanner import read_info

log = logging.getLogger(__name__)

ORIGIN = "build-record"          # Kennzeichen nachträglich angelegter Datensätze
_repo_cache: dict[str, DependencyDepot] | None = None
_repo_lock = threading.Lock()


# --------------------------------------------------------------------------- Datensatz lesen
def record_path(product_id: str) -> Path:
    return paths.MANIFEST_DIR / f"{product_id}.json"


def load_record(product_id: str) -> dict | None:
    rec = read_json(record_path(str(product_id)), None)
    return rec if isinstance(rec, dict) else None


def is_complete(rec: dict | None) -> bool:
    """Datensatz mit Build und Client-Zugang (vom Installer oder von ensure_record)."""
    return bool(rec and rec.get("build_id") and rec.get("client_id"))


def summary(game, rec: dict | None) -> dict[str, Any]:
    """Kurzfassung für Anzeige/CLI – ohne Client-Secret."""
    rec = rec or {}
    return {"product_id": str(game.product_id), "title": game.title, "method": game.method, "exists": bool(rec),
            "complete": is_complete(rec), "build_id": str(rec.get("build_id") or ""),
            "dependencies": list(rec.get("dependency_ids") or rec.get("dependencies") or []),
            "redist": [r.get("id", "") for r in rec.get("redist") or []], "products": list(rec.get("products") or []),
            "script_interpreter": bool(rec.get("script_interpreter")), "origin": str(rec.get("origin") or "installer")}


# --------------------------------------------------------------------------- Spielordner
def _info_dirs(install_dir: Path) -> list[Path]:
    return [install_dir, install_dir / "game"]


def read_install_info(install_dir: Path, product_id: str) -> dict[str, Any]:
    """Angaben aus goggame-<id>.info/.id: build_id, client_id, language, products (Basisspiel zuerst, dann DLCs)."""
    pid = str(product_id)
    out: dict[str, Any] = {"build_id": "", "client_id": "", "language": "", "products": [pid]}
    base: dict | None = None
    dlcs: list[str] = []
    for d in _info_dirs(Path(install_dir)):
        try:
            files = sorted(d.glob("goggame-*.info"))
        except OSError:
            continue
        for f in files:
            info = read_info(f)
            if not info:
                continue
            gid = str(info.get("gameId") or f.stem.replace("goggame-", ""))
            root = str(info.get("rootGameId") or gid)
            if gid == pid:
                base = base or info
            elif root == pid and gid not in dlcs:
                dlcs.append(gid)
    for d in _info_dirs(Path(install_dir)):
        id_file = d / f"goggame-{pid}.id"
        if id_file.is_file():
            data = read_info(id_file) or {}
            if data.get("buildId"):
                out["build_id"] = str(data["buildId"])
                break
    if base:
        if base.get("buildId"):
            out["build_id"] = str(base["buildId"])
        out["client_id"] = str(base.get("clientId") or "")
        langs = base.get("languages")
        out["language"] = str(langs[0]) if isinstance(langs, list) and langs else str(base.get("language") or "")
    out["products"] = [pid, *sorted(dlcs)]
    return out


# --------------------------------------------------------------------------- Build und Manifest
def pick_build(builds: Iterable[Build], build_id: str = "") -> Build | None:
    """Build der Installation (build_id oder legacy_build_id), sonst der neueste öffentliche v2-Build."""
    v2 = [b for b in builds if b.is_v2]
    if build_id:
        for b in v2:
            if b.build_id == build_id or (b.legacy_build_id and b.legacy_build_id == build_id):
                return b
    public = [b for b in v2 if not b.branch]
    return (public or v2 or [None])[0]


def redist_entries(dependency_ids: Iterable[str], repo: dict[str, DependencyDepot],
                   skip: Iterable[str] = ()) -> tuple[list[dict[str, str]], list[str]]:
    """(redist-Liste wie im Installer, lesbare Namen). ``redist`` enthält nur globale Pakete (``__redist/…``, ohne
    ISI) – sie laufen im Prefix; Spielordner-Pakete (DOSBox, ScummVM …) stehen nur bei den Namen. ``internal`` zählt
    nicht (wie gogdl); übersprungene und unbekannte IDs fallen heraus."""
    skip = set(skip)
    redist: list[dict[str, str]] = []
    names: list[str] = []
    for dep_id in dependency_ids:
        dep = repo.get(dep_id)
        if dep is None or dep_id in skip:
            continue
        names.append(dep.readable_name)
        if dep_id != ISI_ID and is_global(dep) and all(r["id"] != dep_id for r in redist):
            redist.append({"id": dep_id, "name": dep.readable_name, "path": dep.executable_path,
                           "arguments": dep.executable_args})
    return redist, names


def make_record(product_id: str, install_dir: str, build: Build, manifest: BuildManifest,
                info: dict[str, Any], redist: list[dict], dependency_names: list[str], language: str = "",
                existing: dict | None = None) -> dict[str, Any]:
    """Datensatz im Format von ``InstallManager.install()`` (``files`` aus einem vorhandenen Datensatz bleiben)."""
    pid = str(product_id)
    products = [p for p in info.get("products") or [pid]]
    if pid not in products:
        products.insert(0, pid)
    return {
        "product_id": pid, "build_id": build.build_id, "platform": "windows",
        "language": language or info.get("language") or "", "install_dir": str(install_dir),
        "files": list((existing or {}).get("files") or []),
        "redist": redist, "dependencies": dependency_names, "dependency_ids": list(manifest.dependencies),
        "client_id": manifest.client_id or info.get("client_id") or "", "client_secret": manifest.client_secret,
        "dlcs": [p for p in products if p != pid], "products": products,
        "script_interpreter": manifest.script_interpreter, "support": list(manifest.support),
        "version_name": build.version_name, "manifest_link": build.link, "origin": ORIGIN,
        "updated_at": time.time(),
    }


def dependency_repo(api) -> dict[str, DependencyDepot]:
    """Abhängigkeits-Repository von GOG (einmal pro Programmlauf)."""
    global _repo_cache
    with _repo_lock:
        if _repo_cache is None:
            _repo_cache = parse_dependency_repository(api.dependency_repository())
        return _repo_cache


# --------------------------------------------------------------------------- Einstieg
def ensure_record(api, game, force: bool = False, repo: dict[str, DependencyDepot] | None = None) -> dict | None:
    """Datensatz des Spiels liefern und bei Bedarf anlegen (nur GOG-Windows-Spiele; sonst der vorhandene oder None).
    Netzwerkfehler werden protokolliert; dann kommt der vorhandene Datensatz (oder None) zurück."""
    pid = str(game.product_id)
    if pid.startswith("ext-") or game.method == "external":
        return None
    rec = load_record(pid)
    if not force and is_complete(rec):
        return rec
    if game.platform != "windows":
        return rec
    install_dir = Path(game.install_path)
    info = read_install_info(install_dir, pid)
    wanted = info["build_id"] or game.build_id or (rec or {}).get("build_id") or ""
    try:
        build = pick_build([Build.from_api(b) for b in api.builds(pid, "windows")], wanted)
        if build is None:
            log.info("Kein Galaxy-Build für %s – kein Installations-Datensatz", pid)
            return rec
        raw = api.zlib_json(build.link)
        manifest = BuildManifest.parse(raw)
        redist: list[dict] = []
        names: list[str] = []
        if manifest.dependencies:
            try:
                redist, names = redist_entries(manifest.dependencies, repo if repo is not None else dependency_repo(api),
                                               {ISI_ID})
            except (ApiError, KeyError) as exc:
                log.warning("Abhängigkeiten für %s nicht verfügbar: %s", pid, exc)
    except (ApiError, AuthError, ManifestError, ValueError, OSError) as exc:
        log.warning("Installations-Datensatz für %s nicht anlegbar: %s", pid, exc)
        return rec
    if wanted and build.build_id != wanted and build.legacy_build_id != wanted:
        log.info("Build %s von %s nicht mehr verfügbar – nutze %s", wanted, pid, build.build_id)
    record = make_record(pid, str(install_dir), build, manifest, info, redist, names,
                         language=game.language, existing=rec)
    write_json(record_path(pid), record)
    log.info("Installations-Datensatz für %s angelegt: Build %s, Abhängigkeiten %s, Produkte %s",
             pid, build.build_id, ",".join(manifest.dependencies) or "-", ",".join(record["products"]))
    return record
