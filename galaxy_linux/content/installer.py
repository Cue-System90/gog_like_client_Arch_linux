"""Installation, Update, Prüfung und Deinstallation von Spielen – wie Galaxy, nur für Linux.

Wege:
  * Content-System v2 (Standard für Windows-Builds; identisch zu Galaxy): Manifeste → Depots → Chunks
  * Linux-nativ: GOG-.sh-Installer laden und mit unzip entpacken (wie Minigalaxy/Heroic)
  * Offline-Installer + innoextract als Rückfall für Spiele ohne v2-Build
"""
from __future__ import annotations

from ..i18n import tr

import errno
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..api.client import ApiError, GogApi
from ..jsonstore import read_json, write_json
from ..settings import GlobalSettings, InstalledGame, InstalledRegistry
from . import redist as deps
from .downloader import ChunkDownloader, DownloadCancelled, DownloadError, FileJob, Progress, SfcSource
from .manifest import (Build, BuildManifest, DepotFile, DepotLink, DependencyDepot, ManifestError,
                       DepotManifest, parse_dependency_repository)

log = logging.getLogger(__name__)

ProgressCb = Callable[[Progress], None]

LANG_NAMES = {
    "en": ("english",), "de": ("deutsch", "german"), "fr": ("français", "french"), "es": ("español", "spanish"),
    "it": ("italiano", "italian"), "pl": ("polski", "polish"), "ru": ("русский", "russian"),
    "pt": ("português do brasil", "português", "portuguese"), "zh": ("中文", "chinese"), "ja": ("日本語", "japanese"),
    "cs": ("český", "czech"), "hu": ("magyar", "hungarian"), "ko": ("한국어", "korean"), "tr": ("türkçe", "turkish"),
    "nl": ("nederlands", "dutch"), "sv": ("svenska", "swedish"), "uk": ("українська", "ukrainian"),
}


class InstallError(Exception):
    pass


class NoGalaxyBuild(InstallError):
    """Für dieses Spiel gibt es keinen Content-System-v2-Build (nur Offline-Installer)."""


def safe_dirname(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return name or "Spiel"


@dataclass
class InstallPlan:
    product_id: str
    title: str
    platform: str
    language: str
    install_dir: Path
    build: Build
    manifest: BuildManifest
    jobs: list[FileJob] = field(default_factory=list)
    sfc_sources: dict[str, SfcSource] = field(default_factory=dict)
    directories: list[str] = field(default_factory=list)
    links: list[DepotLink] = field(default_factory=list)
    dlc_ids: list[str] = field(default_factory=list)
    redist: list[dict] = field(default_factory=list)            # globale Redistributables → Prefix (content/redist.py)
    dependency_names: list[str] = field(default_factory=list)
    game_dependencies: list[str] = field(default_factory=list)  # Spielordner-Pakete (DOSBox, ScummVM …)

    @property
    def support_count(self) -> int:
        return sum(1 for j in self.jobs if j.root is not None)

    @property
    def total_size(self) -> int:
        return sum(j.file.size for j in self.jobs)

    @property
    def download_size(self) -> int:
        return sum(j.file.compressed_size for j in self.jobs) + sum(
            sum(c.compressed_size for c in s.chunks) for s in self.sfc_sources.values())

    @property
    def file_count(self) -> int:
        return len(self.jobs)


class InstallManager:
    def __init__(self, api: GogApi, settings: GlobalSettings, registry: InstalledRegistry) -> None:
        self.api = api
        self.settings = settings
        self.registry = registry
        self._owned: set[str] | None = None
        self._dep_repo: dict[str, DependencyDepot] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ Hilfen
    def owned_ids(self) -> set[str] | None:
        with self._lock:
            if self._owned is None:
                try:
                    self._owned = self.api.owned_ids()
                except ApiError as exc:
                    log.warning("Besitzliste nicht abrufbar: %s", exc)
                    return None
            return self._owned

    def dependency_repo(self) -> dict[str, DependencyDepot]:
        with self._lock:
            if self._dep_repo is None:
                self._dep_repo = parse_dependency_repository(self.api.dependency_repository())
            return self._dep_repo

    def builds(self, product_id: str, platform: str = "windows") -> list[Build]:
        return [Build.from_api(b) for b in self.api.builds(product_id, platform)]

    def latest_build(self, product_id: str, platform: str = "windows", branch: str | None = None) -> Build | None:
        builds = [b for b in self.builds(product_id, platform) if b.is_v2 and (b.branch or None) == branch]
        return builds[0] if builds else None

    def check_update(self, game: InstalledGame) -> Build | None:
        if game.method not in ("content-system", "imported") or not game.build_id:
            return None
        try:
            latest = self.latest_build(game.product_id, game.platform, game.branch or None)
        except ApiError:
            return None
        if latest and latest.build_id != game.build_id:
            return latest
        return None

    def record_path(self, product_id: str) -> Path:
        return paths.MANIFEST_DIR / f"{product_id}.json"

    def install_root(self, override: str | Path | None = None) -> Path:
        return Path(os.path.expanduser(str(override or self.settings.install_dir)))

    # ------------------------------------------------------------------ Planung (Content-System v2)
    def plan(self, product_id: str, title: str, platform: str = "windows", language: str | None = None,
             build_id: str | None = None, install_root: str | Path | None = None, install_dir: Path | None = None,
             include_dlcs: bool = True, dlc_filter: set[str] | None = None, with_dependencies: bool | None = None,
             status: Callable[[str], None] | None = None) -> InstallPlan:
        product_id = str(product_id)
        language = language or self.settings.language
        if status:
            status(tr('Builds abrufen …'))
        builds = self.builds(product_id, platform)
        v2 = [b for b in builds if b.is_v2]
        log.debug("Builds für %s/%s: %s", product_id, platform, [(b.build_id, b.generation, b.version_name, b.branch) for b in builds[:8]])
        if not v2:
            raise NoGalaxyBuild(tr('Für {0} gibt es keinen Galaxy-Build ({1}).').format(title, platform))
        if build_id:
            build = next((b for b in v2 if b.build_id == build_id), None)
            if build is None:
                raise InstallError(tr('Build {0} nicht gefunden.').format(build_id))
        else:
            public = [b for b in v2 if not b.branch]
            build = (public or v2)[0]
        if status:
            status(tr('Manifest laden …'))
        manifest = BuildManifest.parse(self.api.zlib_json(build.link))
        owned = self.owned_ids()
        depots, lang = manifest.select_depots(language, owned, include_dlcs)
        if dlc_filter is not None:
            depots = [d for d in depots if d.product_id in ("", manifest.base_product_id) or d.product_id in dlc_filter]
        if install_dir is None:
            install_dir = self.install_root(install_root) / safe_dirname(manifest.install_directory or title)
        plan = InstallPlan(product_id=product_id, title=title, platform=platform, language=lang,
                           install_dir=Path(install_dir), build=build, manifest=manifest)
        endpoints: dict[str, str] = {}
        files_by_path: dict[str, FileJob] = {}
        support = deps.support_dir(product_id)
        for i, dep in enumerate(depots):
            if status:
                status(tr('Depot {0}/{1} laden …').format(i + 1, len(depots)))
            pid = dep.product_id or manifest.base_product_id
            if pid not in endpoints:
                endpoints[pid] = self.api.secure_link(pid)
            dm = DepotManifest.parse(self.api.depot_manifest(dep.manifest), pid, dep.manifest)
            if dm.small_files_container:
                plan.sfc_sources[dep.manifest] = SfcSource(endpoints[pid], dm.small_files_container)
            for f in dm.files:
                if f.is_support:     # wie gogdl: gog-support/<id>/<productId>/…, nicht in den Spielordner
                    files_by_path[f"\0support/{pid}/{f.path}"] = deps.support_file_job(f, pid, endpoints[pid], dep.manifest,
                                                                                       root=support)
                else:
                    files_by_path[f.path] = FileJob(f, endpoints[pid], dep.manifest if f.sfc_ref else "")
            plan.directories.extend(dm.directories)
            plan.links.extend(dm.links)
            if pid != manifest.base_product_id and pid not in plan.dlc_ids:
                plan.dlc_ids.append(pid)
        plan.jobs = list(files_by_path.values())
        log.info("Plan %s (%s): Build %s '%s' %s · Sprache %s (verfügbar: %s) · %d Depots · %d Dateien · %s entpackt / %s Download · DLCs %s · Abhängigkeiten %s · Ziel %s",
                 product_id, platform, build.build_id, build.version_name, build.date_published[:10], lang,
                 ",".join(manifest.available_languages) or "-", len(depots), len(plan.jobs), plan.total_size,
                 plan.download_size, plan.dlc_ids or "-", manifest.dependencies or "-", plan.install_dir)

        # Abhängigkeiten (Windows): Spielordner-Pakete gehören zum Spiel und werden immer mitgeladen; globale
        # Redistributables kommen in den gemeinsamen Ordner (Auftrag „Galaxy-Redistributables“) und werden nur
        # vermerkt. with_dependencies=False lässt beides weg.
        if with_dependencies is not False and manifest.dependencies and platform == "windows":
            self._plan_dependencies(plan, status)
        return plan

    def _plan_dependencies(self, plan: InstallPlan, status: Callable[[str], None] | None) -> None:
        """Wie gogdl (``DependenciesManager``): globale Pakete (``__redist/…``) nur vermerken, Spielordner-Pakete
        (DOSBox, ScummVM, nGlide, language_setup … – auch ``internal``) in den Spielordner laden."""
        try:
            repo = self.dependency_repo()
        except (ApiError, KeyError, ManifestError) as exc:
            log.warning("Abhängigkeiten nicht verfügbar: %s", exc)
            return
        glob, game, unknown = deps.split_dependencies(plan.manifest.dependencies, repo)
        for dep_id in unknown:
            log.warning("Abhängigkeit %s steht nicht im GOG-Repository", dep_id)
        plan.redist = deps.installer_entries(glob, repo)
        plan.dependency_names = [repo[i].readable_name for i in [*glob, *game] if i != deps.ISI_ID]
        if not game:
            return
        try:
            endpoint = self.api.dependency_link()
        except (ApiError, KeyError) as exc:
            log.warning("Spielordner-Abhängigkeiten nicht verfügbar: %s", exc)
            return
        for dep_id in game:
            dep = repo[dep_id]
            if status:
                status(tr('Abhängigkeit {0} …').format(dep.readable_name))
            try:
                dm = DepotManifest.parse(self.api.dependency_depot_manifest(dep.manifest), "", dep.manifest)
            except (ApiError, ManifestError) as exc:
                log.warning("Abhängigkeit %s übersprungen: %s", dep_id, exc)
                continue
            if dm.small_files_container:
                plan.sfc_sources[dep.manifest] = SfcSource(endpoint, dm.small_files_container)
            for f in dm.files:
                plan.jobs.append(FileJob(f, endpoint, dep.manifest if f.sfc_ref else ""))
            plan.directories.extend(dm.directories)
            plan.links.extend(dm.links)
            plan.game_dependencies.append(dep_id)
        log.info("Abhängigkeiten %s: Spielordner %s · global %s", plan.product_id, ",".join(plan.game_dependencies) or "-",
                 ",".join(glob) or "-")

    # ------------------------------------------------------------------ Ausführung
    def install(self, plan: InstallPlan, progress_cb: ProgressCb | None = None,
                cancel: threading.Event | None = None, verify: bool = True) -> InstalledGame:
        cancel = cancel or threading.Event()
        dl = ChunkDownloader(self.api.new_session, workers=self.settings.max_workers, progress_cb=progress_cb,
                             cancel=cancel)
        plan.install_dir.mkdir(parents=True, exist_ok=True)
        old = read_json(self.record_path(plan.product_id), {})
        try:
            dl.download(plan.jobs, plan.install_dir, plan.sfc_sources, plan.directories, plan.links, verify=verify)
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            raise InstallError(str(exc)) from exc

        # veraltete Dateien aus früherer Version entfernen (auch Support-Dateien und __redist-Kopien bis 0.1.6)
        new_paths = {j.file.path for j in plan.jobs if j.root is None}
        for p in old.get("files", []):
            if p not in new_paths:
                try:
                    (plan.install_dir / p).unlink()
                except OSError:
                    pass
        self._prune_empty_dirs(plan.install_dir)

        total = plan.total_size
        game = InstalledGame(
            product_id=plan.product_id, title=plan.title, platform=plan.platform,
            install_path=str(plan.install_dir), build_id=plan.build.build_id,
            version_name=plan.build.version_name, language=plan.language,
            installed_dlcs=list(plan.dlc_ids), install_size=total, method="content-system",
            branch=plan.build.branch or "",
        )
        write_json(self.record_path(plan.product_id), {
            "product_id": plan.product_id, "build_id": plan.build.build_id, "platform": plan.platform,
            "language": plan.language, "install_dir": str(plan.install_dir),
            "files": sorted(new_paths), "redist": plan.redist, "dependencies": plan.dependency_names,
            "client_id": plan.manifest.client_id, "client_secret": plan.manifest.client_secret,
            "dlcs": plan.dlc_ids, "updated_at": time.time(),
            # ab 0.2.0 (gleiches Format wie content/build_records.py)
            "dependency_ids": list(plan.manifest.dependencies), "script_interpreter": plan.manifest.script_interpreter,
            "products": [plan.product_id, *plan.dlc_ids], "support": list(plan.manifest.support),
            "version_name": plan.build.version_name, "manifest_link": plan.build.link, "origin": "installer",
            "game_dependencies": list(plan.game_dependencies),
        })
        if plan.platform == "windows":
            deps.write_support_marker(plan.product_id, plan.build.build_id, [plan.product_id, *plan.dlc_ids],
                                      plan.support_count)
        self.registry.set(game)
        log.info("Installiert: %s → %s (%s, Build %s, %s)", game.title, game.install_path, game.platform, game.build_id, total)
        return game

    def update(self, product_id: str, progress_cb: ProgressCb | None = None, cancel: threading.Event | None = None,
               status: Callable[[str], None] | None = None, build_id: str | None = None) -> InstalledGame:
        game = self.registry.get(product_id)
        if game is None:
            raise InstallError(tr('Spiel ist nicht installiert.'))
        if game.method not in ("content-system", "imported"):
            raise InstallError(tr('Updates sind nur für Galaxy-Installationen möglich (Spiel neu installieren).'))
        plan = self.plan(product_id, game.title, game.platform, game.language or None, build_id=build_id,
                         install_dir=Path(game.install_path), status=status)
        return self.install(plan, progress_cb, cancel)

    def verify(self, product_id: str, progress_cb: ProgressCb | None = None, cancel: threading.Event | None = None,
               status: Callable[[str], None] | None = None) -> InstalledGame:
        game = self.registry.get(product_id)
        if game is None:
            raise InstallError(tr('Spiel ist nicht installiert.'))
        if game.method not in ("content-system", "imported"):
            raise InstallError(tr('Prüfen ist nur für Galaxy-Installationen möglich.'))
        plan = self.plan(product_id, game.title, game.platform, game.language or None, build_id=game.build_id or None,
                         install_dir=Path(game.install_path), status=status)
        return self.install(plan, progress_cb, cancel, verify=True)

    def uninstall(self, product_id: str, remove_prefix: Path | None = None) -> None:
        game = self.registry.get(product_id)
        if game is not None:
            p = Path(game.install_path)
            if p.is_dir() and p != Path.home() and p != self.install_root():
                shutil.rmtree(p, ignore_errors=True)
        self.registry.remove(product_id)
        try:
            self.record_path(product_id).unlink()
        except OSError:
            pass
        deps.remove_support(product_id)
        if remove_prefix and remove_prefix.is_dir() and remove_prefix != Path.home():
            shutil.rmtree(remove_prefix, ignore_errors=True)

    # ------------------------------------------------------------------ Verschieben (wie „Installation verschieben“ im Windows-Client)
    def move(self, product_id: str, new_root: str | Path, progress_cb: ProgressCb | None = None,
             cancel: threading.Event | None = None, status: Callable[[str], None] | None = None) -> InstalledGame:
        game = self.registry.get(product_id)
        if game is None:
            raise InstallError(tr('Spiel ist nicht installiert.'))
        src = Path(game.install_path)
        if not src.is_dir():
            raise InstallError(tr('Installationsordner fehlt: {0}').format(src))
        root = Path(os.path.expanduser(str(new_root)))
        dst = root / src.name
        if dst.resolve() == src.resolve():
            raise InstallError(tr('Das Spiel liegt bereits in diesem Ordner.'))
        if dst.exists():
            raise InstallError(tr('Zielordner existiert bereits: {0}').format(dst))
        if str(dst.resolve()).startswith(str(src.resolve()) + os.sep):
            raise InstallError(tr('Der Zielordner darf nicht innerhalb des Spielordners liegen.'))
        root.mkdir(parents=True, exist_ok=True)
        if status:
            status(tr('Verschiebe nach {0} …').format(dst))
        log.info("Verschiebe %s: %s → %s", game.title, src, dst)
        try:
            os.rename(src, dst)          # gleiches Dateisystem: sofort
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES):
                raise InstallError(tr('Verschieben fehlgeschlagen: {0}').format(exc)) from exc
            log.info("Anderes Dateisystem – kopiere mit Fortschritt")
            self._copy_tree(src, dst, progress_cb, cancel)
            shutil.rmtree(src, ignore_errors=True)
        game.install_path = str(dst)
        self.registry.set(game)
        rec = read_json(self.record_path(product_id), {})
        if rec:
            rec["install_dir"] = str(dst)
            write_json(self.record_path(product_id), rec)
        if progress_cb:
            progress_cb(Progress(phase=tr('Fertig'), total_bytes=1, done_bytes=1))
        log.info("Verschoben: %s → %s", game.title, dst)
        return game

    @staticmethod
    def _copy_tree(src: Path, dst: Path, progress_cb: ProgressCb | None = None,
                   cancel: threading.Event | None = None) -> None:
        """Kopiert einen Spielordner mit Fortschritt (Symlinks, Rechte und Zeiten bleiben erhalten)."""
        files: list[Path] = []
        total = 0
        for p in src.rglob("*"):
            if p.is_symlink() or p.is_file():
                files.append(p)
                if not p.is_symlink():
                    total += p.stat().st_size
        prog = Progress(phase=tr('Verschieben'), total_bytes=total, files_total=len(files))
        last = [time.monotonic(), 0]
        try:
            for d in [src, *[p for p in src.rglob("*") if p.is_dir() and not p.is_symlink()]]:
                target = dst / d.relative_to(src)
                target.mkdir(parents=True, exist_ok=True)
                shutil.copystat(d, target, follow_symlinks=False)
            for f in files:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                target = dst / f.relative_to(src)
                prog.current = str(f.relative_to(src))
                if f.is_symlink():
                    os.symlink(os.readlink(f), target)
                else:
                    with open(f, "rb") as fin, open(target, "wb") as fout:
                        while True:
                            block = fin.read(4 << 20)
                            if not block:
                                break
                            fout.write(block)
                            prog.done_bytes += len(block)
                            now = time.monotonic()
                            if now - last[0] >= 0.3:
                                prog.write_speed = (prog.done_bytes - last[1]) / (now - last[0])
                                prog.speed = prog.write_speed
                                last[0], last[1] = now, prog.done_bytes
                                if progress_cb:
                                    progress_cb(prog)
                    shutil.copystat(f, target)
                prog.files_done += 1
            if progress_cb:
                progress_cb(prog)
        except BaseException:
            shutil.rmtree(dst, ignore_errors=True)
            raise

    @staticmethod
    def _prune_empty_dirs(root: Path) -> None:
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(str(p)), reverse=True):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------ Installer-Dateien (Linux .sh / Offline)
    def _installers(self, product_id: str, os_name: str, language: str) -> list[dict]:
        product = self.api.product(product_id, max_age=3600) or {}
        installers = [i for i in ((product.get("downloads") or {}).get("installers") or []) if i.get("os") == os_name]
        if not installers:
            return []
        code = language.split("-")[0].lower()
        by_lang = [i for i in installers if (i.get("language") or "").lower() == code]
        if not by_lang:
            names = LANG_NAMES.get(code, ())
            by_lang = [i for i in installers if (i.get("language_full") or "").lower() in names]
        if not by_lang:
            by_lang = [i for i in installers if (i.get("language") or "").lower() == "en"] or installers[:1]
        return by_lang

    def _download_installer_files(self, installer: dict, tmp: Path, progress_cb: ProgressCb | None,
                                  cancel: threading.Event | None, label: str) -> list[Path]:
        files = installer.get("files") or []
        total = sum(int(f.get("size", 0) or 0) for f in files)
        prog = Progress(phase=tr('{0} herunterladen').format(label), total_bytes=total, files_total=len(files))
        out: list[Path] = []
        done_before = 0
        last = [time.monotonic(), 0]
        for f in files:
            if cancel and cancel.is_set():
                raise DownloadCancelled()
            info = self.api.downlink(f["downlink"])
            url = info.get("downlink")
            if not url:
                raise InstallError(tr('Download-Link konnte nicht aufgelöst werden.'))
            name = url.split("?")[0].rstrip("/").split("/")[-1] or f["id"]
            dest = tmp / name
            prog.current = name

            def on_progress(have: int, _total: int, base=done_before) -> None:
                now = time.monotonic()
                prog.done_bytes = base + have
                prog.downloaded_bytes = prog.done_bytes
                if now - last[0] >= 0.5:
                    prog.speed = (prog.done_bytes - last[1]) / (now - last[0])
                    prog.write_speed = prog.speed
                    last[0], last[1] = now, prog.done_bytes
                if progress_cb:
                    progress_cb(prog)
            if dest.exists() and dest.stat().st_size == int(f.get("size", 0) or 0):
                on_progress(dest.stat().st_size, 0)
            else:
                self.api.stream_download(url, dest, on_progress, cancel)
            done_before += int(f.get("size", 0) or 0)
            prog.files_done += 1
            out.append(dest)
        return out

    def install_linux_native(self, product_id: str, title: str, language: str | None = None,
                             install_root: str | Path | None = None, progress_cb: ProgressCb | None = None,
                             cancel: threading.Event | None = None, status: Callable[[str], None] | None = None) -> InstalledGame:
        product_id = str(product_id)
        language = language or self.settings.language
        if status:
            status(tr('Installer suchen …'))
        installers = self._installers(product_id, "linux", language)
        if not installers:
            raise InstallError(tr('Kein Linux-Installer für dieses Spiel gefunden.'))
        installer = installers[0]
        install_dir = self.install_root(install_root) / safe_dirname(title)
        tmp = paths.CACHE_DIR / "installers" / product_id
        tmp.mkdir(parents=True, exist_ok=True)
        files = self._download_installer_files(installer, tmp, progress_cb, cancel, tr('Linux-Installer'))
        sh = next((f for f in files if f.suffix == ".sh"), files[0])
        if status:
            status(tr('Entpacken …'))
        if progress_cb:
            progress_cb(Progress(phase=tr('Entpacken'), total_bytes=1, done_bytes=0, current=sh.name))
        extract = tmp / "extract"
        shutil.rmtree(extract, ignore_errors=True)
        extract.mkdir(parents=True)
        # GOG-.sh-Installer = makeself + ZIP; unzip meldet Warnungen (Exit 1/2) wegen des Shell-Vorspanns.
        r = subprocess.run(["unzip", "-q", "-o", str(sh), "-d", str(extract)], capture_output=True, text=True)
        noarch = extract / "data" / "noarch"
        if not noarch.is_dir():
            raise InstallError(tr('Installer konnte nicht entpackt werden: {0}').format(r.stderr.strip()[:300]))
        if install_dir.exists():
            shutil.rmtree(install_dir, ignore_errors=True)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(noarch), str(install_dir))
        shutil.rmtree(extract, ignore_errors=True)
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        exe = "start.sh" if (install_dir / "start.sh").exists() else ""
        if exe:
            (install_dir / exe).chmod(0o755)
        size = sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file())
        game = InstalledGame(product_id=product_id, title=title, platform="linux", install_path=str(install_dir),
                             version_name=str(installer.get("version") or ""), language=language,
                             install_size=size, method="linux-installer", executable=exe)
        write_json(self.record_path(product_id), {"product_id": product_id, "platform": "linux",
                                                  "install_dir": str(install_dir), "files": [], "redist": []})
        self.registry.set(game)
        if progress_cb:
            progress_cb(Progress(phase=tr('Fertig'), total_bytes=1, done_bytes=1))
        return game

    def install_offline_windows(self, product_id: str, title: str, language: str | None = None,
                                install_root: str | Path | None = None, progress_cb: ProgressCb | None = None,
                                cancel: threading.Event | None = None, status: Callable[[str], None] | None = None) -> InstalledGame:
        """Rückfall für Spiele ohne Galaxy-Build: Offline-Installer + innoextract."""
        if not shutil.which("innoextract"):
            raise InstallError(tr('innoextract fehlt (sudo pacman -S innoextract) – nötig für Offline-Installer.'))
        product_id = str(product_id)
        language = language or self.settings.language
        installers = self._installers(product_id, "windows", language)
        if not installers:
            raise InstallError(tr('Kein Windows-Installer gefunden.'))
        installer = installers[0]
        install_dir = self.install_root(install_root) / safe_dirname(title)
        tmp = paths.CACHE_DIR / "installers" / product_id
        tmp.mkdir(parents=True, exist_ok=True)
        files = self._download_installer_files(installer, tmp, progress_cb, cancel, tr('Offline-Installer'))
        setup = next((f for f in files if f.suffix.lower() == ".exe"), files[0])
        if status:
            status(tr('Entpacken mit innoextract …'))
        extract = tmp / "extract"
        shutil.rmtree(extract, ignore_errors=True)
        extract.mkdir(parents=True)
        r = subprocess.run(["innoextract", "--gog", "--silent", "-d", str(extract), str(setup)], capture_output=True, text=True)
        if r.returncode != 0:
            raise InstallError(tr('innoextract fehlgeschlagen: {0}').format(r.stderr.strip()[:300]))
        src = extract / "app" if (extract / "app").is_dir() else extract
        if install_dir.exists():
            shutil.rmtree(install_dir, ignore_errors=True)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(install_dir))
        shutil.rmtree(extract, ignore_errors=True)
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        size = sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file())
        game = InstalledGame(product_id=product_id, title=title, platform="windows", install_path=str(install_dir),
                             version_name=str(installer.get("version") or ""), language=language,
                             install_size=size, method="offline-installer")
        write_json(self.record_path(product_id), {"product_id": product_id, "platform": "windows",
                                                  "install_dir": str(install_dir), "files": [], "redist": []})
        self.registry.set(game)
        if progress_cb:
            progress_cb(Progress(phase=tr('Fertig'), total_bytes=1, done_bytes=1))
        return game
