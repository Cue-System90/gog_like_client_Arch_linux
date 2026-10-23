"""Cloud-Spielstände wie in GOG Galaxy (cloudstorage.gog.com, Protokoll wie gogdl/Heroic).

Ablauf eines Abgleichs (``sync_game``):
  1. ``client_id``/``client_secret`` aus dem Installations-Datensatz (``build_records.ensure_record``), Spiel-Token
     über ``GogApi.game_token`` (Galaxy-Refresh-Token gegen Spiel-Zugang),
  2. Speicherorte aus ``remote-config.gog.com/components/galaxy_client/clients/<clientId>?component_version=2.0.51``
     (eine Stunde im API-Cache): ``content.Windows.cloudStorage {enabled, locations[{name, location}]}``, Kontingent
     ``content.cloudStorage.quota`` (Vorgabe 200 MB),
  3. Platzhalter wie ``<?DOCUMENTS?>`` auf den Wine-/Proton-Prefix abbilden (``wine_user_dir``, ``resolve_location``),
  4. Dateiliste der Cloud (``GET /v1/<userId>/<clientId>``) je Ort mit den lokalen Dateien vergleichen (``classify``
     wie gogdls SyncClassifier; Zeitpunkt der letzten Synchronisation pro Ort in ``GameSettings.cloud_timestamps``):
     lokal leer → herunterladen, Cloud leer → hochladen, nur lokal neuer → hochladen, nur Cloud neuer →
     herunterladen, beides → Konflikt (Rückfrage wie Galaxy), sonst nichts. Inhaltsgleiche Dateien (gleicher Hash)
     zählen nicht als geändert,
  5. vor dem Überschreiben oder Löschen lokaler Dateien eine Sicherung
     ``~/.local/share/galaxy-linux/cloudsave-backups/<id>/<zeit>.tar.gz`` (die letzten fünf bleiben).

Betriebsarten: ``auto`` (beide Richtungen), ``download`` (vor dem Start, wie Heroic ``--skip-upload``), ``upload``
(nach dem Beenden, ``--skip-download``), ``force-download``/``force-upload`` (eine Seite ersetzt die andere, wie
gogdl ``forcedownload``/``forceupload``). Mit ``confirm=True`` (Knopf „Jetzt hochladen“) wird vor ``force-upload``
nachgefragt, wenn sich die Cloud seit dem letzten Abgleich geändert hat – das wäre sonst nicht wiederherstellbar
(lokale Dateien werden vor dem Überschreiben ohnehin gesichert).

Zustand für die Anzeige (``cloud_status``/``status_label``): ``cloudsaves.json`` im Datenordner, Zeitpunkte der
letzten Synchronisation in ``GameSettings.cloud_timestamps``.

Nicht live geprüft (reference/overhaul-2026-10-22/gog-apis.md §5 und §12.6):
  * Standardordner bei leerer Ortsliste: Heroic nutzt ``%LOCALAPPDATA%/GOG.com/Galaxy/Applications/<clientId>/
    Storage/Shared/Files``, lgogdownloader ``…/Storage`` – wir folgen Heroic (Ortsname ``__default``).
  * Bedeutung des Hashes ``aadd86936a80ee8a369579c3926f1b3c``: gogdl überspringt solche Einträge (vermutlich
    Löschmarken), hier gelten sie als nicht vorhanden.
  * Benutzerordner im Prefix: umu und Proton ``drive_c/users/steamuser``, Wine ``drive_c/users/<$USER>``; vorhandene
    Ordner haben Vorrang. Ordnernamen werden ohne Rücksicht auf Groß-/Kleinschreibung gefunden (wie unter Windows).
Tokens und Client-Secrets werden nie protokolliert.
"""
from __future__ import annotations

from ..i18n import tr

import datetime as _dt
import getpass
import gzip
import hashlib
import logging
import os
import re
import sys
import tarfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .. import paths
from ..api.auth import AuthError
from ..api.client import ApiError
from ..jsonstore import read_json, write_json
from ..settings import format_bytes, resolve_switch

log = logging.getLogger(__name__)

REMOTE_CONFIG_URL = "https://remote-config.gog.com/components/galaxy_client/clients/{0}"
COMPONENT_VERSION = "2.0.51"
CLOUDSTORAGE = "https://cloudstorage.gog.com"
# wie gogdl/comet (GOGs Kommunikationsdienst); ob der Server ihn verlangt, ist nicht geprüft
CLOUD_USER_AGENT = "GOGGalaxyCommunicationService/2.0.13.27 (Windows_32bit) dont_sync_marker/true installation_source/gog"
DELETED_HASH = "aadd86936a80ee8a369579c3926f1b3c"
DEFAULT_QUOTA = 200 * 1024 * 1024
DEFAULT_LOCATION_NAME = "__default"
DEFAULT_LOCATION = "<?APPLICATION_DATA_LOCAL?>/GOG.com/Galaxy/Applications/{0}/Storage/Shared/Files"
CONFIG_MAX_AGE = 3600
BACKUP_KEEP = 5

DOWNLOAD, UPLOAD, CONFLICT, NONE, SKIP = "download", "upload", "conflict", "none", "skip"
MODES = ("auto", "download", "upload", "force-download", "force-upload")

# Platzhalter → Ordner relativ zum Windows-Benutzerordner im Prefix (None = Installationsordner)
PLACEHOLDERS: dict[str, str | None] = {
    "INSTALL": None,
    "SAVED_GAMES": "Saved Games",
    "APPLICATION_DATA_LOCAL": "AppData/Local",
    "APPLICATION_DATA_LOCAL_LOW": "AppData/LocalLow",
    "APPLICATION_DATA_ROAMING": "AppData/Roaming",
    "DOCUMENTS": "Documents",
}
_PLACEHOLDER_RE = re.compile(r"<\?([A-Z_]+)\?>")

AskFn = Callable[[str, str, "list[tuple[str, str]]", str], str]


class CloudError(Exception):
    """Abgleich nicht möglich (Netz, Zugang, Dateien)."""


class CloudUnsupported(CloudError):
    """Spiel ohne Cloud-Spielstände (Linux-Version, extern, kein Galaxy-Zugang, von GOG nicht freigeschaltet)."""


# --------------------------------------------------------------------------- Speicherorte
@dataclass
class CloudConfig:
    enabled: bool
    locations: list[tuple[str, str]]          # (Name = Ordner in der Cloud, Vorlage mit Platzhaltern)
    quota: int = DEFAULT_QUOTA


@dataclass
class Location:
    name: str
    template: str
    path: Path | None                          # None = nicht auflösbar oder abgelehnt


def parse_remote_config(data: Any, client_id: str = "") -> CloudConfig:
    """Antwort von remote-config → Speicherorte für Windows; leere Liste → Galaxy-Standardordner ``__default``."""
    content = data.get("content") if isinstance(data, dict) else None
    content = content if isinstance(content, dict) else {}
    win = content.get("Windows") if isinstance(content.get("Windows"), dict) else {}
    cs = win.get("cloudStorage") if isinstance(win.get("cloudStorage"), dict) else {}
    enabled = bool(cs.get("enabled"))
    locs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in cs.get("locations") or []:
        if not isinstance(item, dict) or not item.get("location"):
            continue
        name = str(item.get("name") or DEFAULT_LOCATION_NAME).strip() or DEFAULT_LOCATION_NAME
        if name in seen:
            continue
        seen.add(name)
        locs.append((name, str(item["location"])))
    if enabled and not locs and client_id:
        locs = [(DEFAULT_LOCATION_NAME, DEFAULT_LOCATION.format(client_id))]
    quota_box = content.get("cloudStorage") if isinstance(content.get("cloudStorage"), dict) else {}
    try:
        quota = int(quota_box.get("quota") or DEFAULT_QUOTA)
    except (TypeError, ValueError):
        quota = DEFAULT_QUOTA
    return CloudConfig(enabled, locs, quota if quota > 0 else DEFAULT_QUOTA)


def remote_config(api, client_id: str, max_age: float = CONFIG_MAX_AGE) -> Any:
    """Spielkonfiguration von remote-config.gog.com (ohne Anmeldung, eine Stunde im Cache; bei Netzfehlern der
    alte Cache). 404 = keine Konfiguration (leeres Objekt)."""
    f = paths.API_CACHE_DIR / f"remote-config-{client_id}.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            cached = read_json(f, None)
            if cached is not None:
                return cached
    except OSError:
        pass
    try:
        r = api.get(REMOTE_CONFIG_URL.format(client_id), params={"component_version": COMPONENT_VERSION},
                    authenticated=False, timeout=20)
        if r.status_code == 404:
            data: Any = {}
        elif not r.ok:
            raise CloudError(tr('Speicherorte nicht abrufbar (HTTP {0})').format(r.status_code))
        else:
            data = r.json()
    except (ApiError, CloudError, ValueError) as exc:
        cached = read_json(f, None)
        if cached is not None:
            log.info("remote-config für %s nicht erreichbar (%s) – nutze Cache", client_id, exc)
            return cached
        if isinstance(exc, CloudError):
            raise
        raise CloudError(tr('Speicherorte nicht abrufbar: {0}').format(exc)) from exc
    write_json(f, data)
    return data


def _username() -> str:
    try:
        return getpass.getuser() or "user"
    except Exception:
        return os.environ.get("USER") or "user"


def wine_root(prefix: Path, runner: str) -> Path:
    """Ordner mit ``drive_c``: umu legt den Prefix direkt an (``pfx`` → ``.``), Proton unter ``pfx/``, Wine wie
    ``Launcher.build_plan`` (``pfx/``, falls vorhanden)."""
    prefix = Path(prefix)
    pfx = prefix / "pfx"
    order = (prefix, pfx) if pfx.is_symlink() else (pfx, prefix)
    for cand in order:
        if (cand / "drive_c").is_dir():
            return cand
    return pfx if runner == "proton" else prefix


def wine_user_dir(prefix: Path, runner: str, username: str | None = None) -> Path:
    """Windows-Benutzerordner (``%USERPROFILE%``) im Prefix: vorhandener ``steamuser``- bzw. Benutzerordner, sonst
    ``steamuser`` für umu/Proton und der Linux-Benutzername für Wine."""
    users = wine_root(prefix, runner) / "drive_c" / "users"
    username = username or _username()
    for name in ("steamuser", username):
        if (users / name).is_dir():
            return users / name
    return users / ("steamuser" if runner in ("umu", "proton") else username)


def ci_join(base: Path, rel: str) -> Path:
    """``base/rel`` – vorhandene Ordner und Dateien werden ohne Rücksicht auf Groß-/Kleinschreibung gefunden."""
    cur = Path(base)
    for part in [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]:
        cand = cur / part
        if not cand.exists() and cur.is_dir():
            want = part.casefold()
            try:
                match = next((e for e in sorted(cur.iterdir()) if e.name.casefold() == want), None)
            except OSError:
                match = None
            if match is not None:
                cand = match
        cur = cand
    return cur


def resolve_location(template: str, install_dir: Path, user_dir: Path) -> Path | None:
    """Vorlage aus remote-config (``<?DOCUMENTS?>/My Games/X``) → Pfad im Prefix bzw. Spielordner.
    None bei unbekanntem Platzhalter, fehlendem Platzhalter am Anfang oder ``..``."""
    text = (template or "").strip().replace("\\", "/")
    m = _PLACEHOLDER_RE.match(text)
    if not m or m.group(1) not in PLACEHOLDERS:
        return None
    rest = text[m.end():]
    if _PLACEHOLDER_RE.search(rest):
        return None
    parts = [p for p in rest.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    sub = PLACEHOLDERS[m.group(1)]
    base = Path(install_dir) if sub is None else ci_join(Path(user_dir), sub)
    return ci_join(base, "/".join(parts)) if parts else base


def _too_broad(path: Path, protected: list[Path]) -> bool:
    """Ort wäre der Spielordner, der Prefix oder der Benutzerordner selbst (oder ein Ordner darüber) – dort
    würde ein Abgleich das ganze Spiel hoch- oder weglöschen."""
    p = Path(os.path.abspath(path))
    for q in protected:
        q = Path(os.path.abspath(q))
        if p == q or p in q.parents:
            return True
    return False


@dataclass
class GameCloud:
    """Alles, was ein Abgleich über das Spiel wissen muss (``client_secret`` wird nie ausgegeben)."""
    product_id: str
    title: str
    client_id: str
    client_secret: str = field(repr=False)
    config: CloudConfig = field(default_factory=lambda: CloudConfig(False, []))
    locations: list[Location] = field(default_factory=list)
    install_dir: Path = Path()
    user_dir: Path = Path()


def enabled_for(gs, settings) -> bool:
    """Automatischer Abgleich für das Spiel an? (pro Spiel an/aus, sonst „Einstellungen → Spielfunktionen“)"""
    return resolve_switch(getattr(gs, "cloud_saves", ""), getattr(settings, "feature_cloud_saves", True))


def game_cloud(launcher, api, product_id: str, prefix: Path | None = None, runner: str = "",
               need_secret: bool = True) -> GameCloud:
    """Speicherorte eines Spiels ermitteln (Netz: Installations-Datensatz, remote-config).
    ``CloudUnsupported``, wenn das Spiel keine Cloud-Spielstände haben kann."""
    from . import build_records
    pid = str(product_id)
    game = launcher.registry.get(pid)
    if game is None:
        raise CloudUnsupported(tr('Spiel ist nicht installiert.'))
    if game.method == "external" or pid.startswith("ext-"):
        raise CloudUnsupported(tr('Cloud-Spielstände gibt es nur für GOG-Spiele.'))
    if game.platform != "windows":
        raise CloudUnsupported(tr('Linux-Versionen von GOG-Spielen haben keine Cloud-Spielstände.'))
    rec = build_records.ensure_record(api, game) or {}
    cid, secret = str(rec.get("client_id") or ""), str(rec.get("client_secret") or "")
    if not cid:
        cid = str(build_records.read_install_info(Path(game.install_path), pid).get("client_id") or "")
    if not cid or (need_secret and not secret):
        raise CloudUnsupported(tr('Kein Galaxy-Zugang für dieses Spiel (Installations-Datensatz ohne Client-ID).'))
    cfg = parse_remote_config(remote_config(api, cid), cid)
    if not cfg.enabled or not cfg.locations:
        raise CloudUnsupported(tr('Das Spiel unterstützt keine Cloud-Spielstände.'))
    gs = launcher.game_settings.get(pid)
    if prefix is None:
        prefix = launcher.prefix_for(pid, gs)
    if not runner:
        tool = launcher.resolve_tool(gs, "windows", pid)
        runner = launcher.runner_for(tool) if tool is not None else "umu"
    install_dir = Path(game.install_path)
    user_dir = wine_user_dir(Path(prefix), runner)
    protected = [install_dir, Path(prefix), user_dir]
    locs: list[Location] = []
    for name, tpl in cfg.locations:
        path = resolve_location(tpl, install_dir, user_dir)
        if path is None:
            log.warning("Speicherort %s von %s nicht auflösbar: %s", name, pid, tpl)
        elif _too_broad(path, protected):
            log.warning("Speicherort %s von %s abgelehnt (umfasst Spiel- oder Prefix-Ordner): %s", name, pid, path)
            path = None
        locs.append(Location(name, tpl, path))
    return GameCloud(pid, game.title, cid, secret, cfg, locs, install_dir, user_dir)


# --------------------------------------------------------------------------- Dateien
@dataclass
class SyncFile:
    rel: str                     # relativ zum Ort, „/“ als Trenner (Cloud-Name ohne „<ort>/“)
    path: Path | None = None     # lokale Datei (None bei reinen Cloud-Einträgen)
    md5: str = ""                # md5 der gzip-Daten (Stufe 6, mtime 0) wie Galaxy/gogdl
    mtime: float = 0.0           # lokal st_mtime, Cloud last_modified
    size: int = 0

    @property
    def key(self) -> str:
        return self.rel.casefold()


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, 6, mtime=0)


def file_md5(path: Path) -> str:
    return hashlib.md5(gzip_bytes(Path(path).read_bytes())).hexdigest()


def safe_rel(rel: str) -> bool:
    """Relativer Pfad ohne ``..``/absolute Teile (Namen aus der Cloud dürfen den Ort nie verlassen)."""
    parts = rel.split("/")
    return bool(rel) and not rel.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def scan_local(root: Path | None) -> list[SyncFile]:
    """Alle Dateien eines Speicherorts (Unterordner eingeschlossen, Ordner-Symlinks nicht verfolgt)."""
    out: list[SyncFile] = []
    if root is None or not Path(root).is_dir():
        return out
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if fn.endswith(".gl-part"):
                continue
            try:
                if not p.is_file():
                    continue
                st = p.stat()
                md5 = file_md5(p)
            except OSError as exc:
                log.debug("Spielstand %s nicht lesbar: %s", p, exc)
                continue
            out.append(SyncFile(p.relative_to(root).as_posix(), p, md5, st.st_mtime, st.st_size))
    return out


def dir_stats(root: Path | None) -> tuple[int, int, float]:
    """(Dateien, Bytes, jüngste Änderung) ohne Hashes – für Anzeigen."""
    n = size = 0
    latest = 0.0
    if root is None or not Path(root).is_dir():
        return 0, 0, 0.0
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            try:
                st = (Path(dirpath) / fn).stat()
            except OSError:
                continue
            n += 1
            size += st.st_size
            latest = max(latest, st.st_mtime)
    return n, size, latest


def parse_time(text: Any) -> float:
    """ISO-Zeit der Cloud (``…Z``/``+00:00``; ohne Zone = UTC) → Unix-Zeit, 0 bei Unlesbarem."""
    if not text:
        return 0.0
    try:
        d = _dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def iso_time(ts: float) -> str:
    """Unix-Zeit → ISO in UTC auf Sekunden, wie gogdl für ``X-Object-Meta-LocalLastModified``."""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def cloud_files(items: list, dirname: str) -> list[SyncFile]:
    """Einträge der Cloud-Liste, die zum Ort ``dirname`` gehören (ohne Löschmarken und unsichere Namen)."""
    prefix = dirname + "/"
    out: list[SyncFile] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").replace("\\", "/")
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not safe_rel(rel):
            log.warning("Cloud-Eintrag mit unzulässigem Namen übersprungen: %s", name)
            continue
        h = str(it.get("hash") or "").lower()
        if h == DELETED_HASH:
            continue
        try:
            size = int(it.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append(SyncFile(rel, None, h, parse_time(it.get("last_modified")), size))
    return out


# --------------------------------------------------------------------------- Abgleich
@dataclass
class Classification:
    action: str = NONE
    updated_local: list[SyncFile] = field(default_factory=list)    # → hochladen
    updated_cloud: list[SyncFile] = field(default_factory=list)    # → herunterladen
    only_cloud: list[SyncFile] = field(default_factory=list)       # lokal nicht vorhanden (beim Hochladen löschen)
    only_local: list[SyncFile] = field(default_factory=list)       # in der Cloud nicht vorhanden (beim Herunterladen löschen)


def _maps(local: list[SyncFile], cloud: list[SyncFile]) -> tuple[dict, dict, set]:
    lmap = {f.key: f for f in local}
    cmap = {f.key: f for f in cloud}
    same = {k for k, f in lmap.items() if k in cmap and f.md5 and f.md5 == cmap[k].md5}
    return lmap, cmap, same


def classify(local: list[SyncFile], cloud: list[SyncFile], ts: float) -> Classification:
    """Wie gogdls SyncClassifier: lokal leer → DOWNLOAD, Cloud leer → UPLOAD, sonst nach Änderungen seit ``ts``.
    Dateien mit gleichem Hash auf beiden Seiten gelten als unverändert (vermeidet Schein-Konflikte)."""
    lmap, cmap, same = _maps(local, cloud)
    c = Classification(only_cloud=[f for f in cloud if f.key not in lmap],
                       only_local=[f for f in local if f.key not in cmap])
    if not local and not cloud:
        return c
    if not local:
        c.action, c.updated_cloud = DOWNLOAD, list(cloud)
        return c
    if not cloud:
        c.action, c.updated_local = UPLOAD, list(local)
        return c
    c.updated_local = [f for f in local if f.mtime > ts and f.key not in same]
    c.updated_cloud = [f for f in cloud if f.mtime > ts and f.key not in same]
    if c.updated_local and c.updated_cloud:
        c.action = CONFLICT
    elif c.updated_local:
        c.action = UPLOAD
    elif c.updated_cloud:
        c.action = DOWNLOAD
    return c


def forced(local: list[SyncFile], cloud: list[SyncFile], direction: str) -> Classification:
    """Eine Seite ersetzt die andere (Konflikt gelöst, „Jetzt hochladen/herunterladen“): alles Abweichende wird
    übertragen, was auf der Quellseite fehlt, auf der Zielseite gelöscht – außer die Quellseite ist ganz leer
    (dann wird nichts gelöscht, wie bei gogdl, das leere Seiten vor dem Erzwingen behandelt)."""
    lmap, cmap, same = _maps(local, cloud)
    c = Classification(action=direction, only_cloud=[f for f in cloud if f.key not in lmap],
                       only_local=[f for f in local if f.key not in cmap])
    if direction == DOWNLOAD:
        c.updated_cloud = [f for f in cloud if f.key not in same]
        if not cloud:
            c.only_local = []
    else:
        c.updated_local = [f for f in local if f.key not in same]
        if not local:
            c.only_cloud = []
    return c


def decide(action: str, mode: str) -> str:
    """Was die Betriebsart aus dem Ergebnis macht: vor dem Start wird nie hochgeladen, nach dem Ende nie
    heruntergeladen (``SKIP``, der Zeitstempel bleibt dann stehen)."""
    if mode == "download" and action == UPLOAD:
        return SKIP
    if mode == "upload" and action == DOWNLOAD:
        return SKIP
    return action


# --------------------------------------------------------------------------- cloudstorage.gog.com
class CloudStorage:
    """cloudstorage.gog.com v1 wie gogdl (saves.py): Liste, Herunterladen, Hochladen (gzip), Löschen."""

    def __init__(self, user_id: str, client_id: str, token: Callable[[bool], str],
                 session: requests.Session | None = None, timeout: float = 60) -> None:
        self.base = f"{CLOUDSTORAGE}/v1/{user_id}/{client_id}"
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": CLOUD_USER_AGENT, "X-Object-Meta-User-Agent": CLOUD_USER_AGENT})
        self._token = token
        self.timeout = timeout

    def url(self, dirname: str, rel: str) -> str:
        return f"{self.base}/{urllib.parse.quote(dirname, safe='')}/{urllib.parse.quote(rel)}"

    def _request(self, method: str, url: str, **kw: Any) -> requests.Response:
        headers = dict(kw.pop("headers", None) or {})
        r: requests.Response | None = None
        for attempt in (0, 1):
            try:
                headers["Authorization"] = f"Bearer {self._token(attempt == 1)}"
            except (ApiError, AuthError) as exc:
                raise CloudError(tr('Kein Zugang zu den Cloud-Spielständen: {0}').format(exc)) from exc
            try:
                r = self.session.request(method, url, headers=headers, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                raise CloudError(tr('Verbindung zu cloudstorage.gog.com fehlgeschlagen: {0}').format(exc)) from exc
            if r.status_code == 401 and attempt == 0:
                log.info("Cloud-Speicher: 401 – Spiel-Token wird erneuert")
                continue
            break
        return r  # type: ignore[return-value]

    def list(self) -> list[dict]:
        r = self._request("GET", self.base, headers={"Accept": "application/json"})
        if r.status_code == 404:
            return []
        if not r.ok:
            raise CloudError(tr('Cloud-Dateiliste nicht abrufbar (HTTP {0})').format(r.status_code))
        try:
            data = r.json()
        except ValueError as exc:
            raise CloudError(tr('Ungültige Antwort von cloudstorage.gog.com')) from exc
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def upload(self, dirname: str, f: SyncFile) -> None:
        data = gzip_bytes(Path(f.path).read_bytes())
        headers = {"X-Object-Meta-LocalLastModified": iso_time(f.mtime), "Etag": hashlib.md5(data).hexdigest(),
                   "Content-Encoding": "gzip"}
        r = self._request("PUT", self.url(dirname, f.rel), data=data, headers=headers)
        if not r.ok:
            raise CloudError(tr('Hochladen von {0} fehlgeschlagen (HTTP {1})').format(f.rel, r.status_code))

    def download(self, dirname: str, f: SyncFile, dest: Path) -> None:
        r = self._request("GET", self.url(dirname, f.rel), stream=True)
        try:
            if not r.ok:
                raise CloudError(tr('Herunterladen von {0} fehlgeschlagen (HTTP {1})').format(f.rel, r.status_code))
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".gl-part")
            try:
                with open(tmp, "wb") as fh:
                    for block in r.iter_content(1 << 20):      # requests entpackt Content-Encoding: gzip
                        if block:
                            fh.write(block)
                os.replace(tmp, dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            ts = parse_time(r.headers.get("X-Object-Meta-LocalLastModified")) or f.mtime
            if ts:
                os.utime(dest, (ts, ts))
        finally:
            r.close()

    def delete(self, dirname: str, rel: str) -> None:
        r = self._request("DELETE", self.url(dirname, rel))
        if not r.ok and r.status_code != 404:
            raise CloudError(tr('Löschen von {0} in der Cloud fehlgeschlagen (HTTP {1})').format(rel, r.status_code))


# --------------------------------------------------------------------------- Sicherungen
def backup_dir(product_id: str) -> Path:
    return paths.DATA_DIR / "cloudsave-backups" / str(product_id)


def list_backups(product_id: str) -> list[Path]:
    d = backup_dir(product_id)
    try:
        files = [p for p in d.glob("*.tar.gz") if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def rotate_backups(product_id: str, keep: int = BACKUP_KEEP) -> list[Path]:
    """Nur die neuesten ``keep`` Sicherungen behalten; gibt die gelöschten zurück."""
    files = list_backups(product_id)
    old = files[:-keep] if keep > 0 else files
    for p in old:
        try:
            p.unlink()
        except OSError as exc:
            log.warning("Alte Spielstand-Sicherung %s nicht löschbar: %s", p, exc)
    return old


def make_backup(product_id: str, dirs: list[tuple[str, Path]], keep: int = BACKUP_KEEP,
                now: float | None = None) -> Path | None:
    """Speicherorte als ``<zeit>.tar.gz`` sichern (Ordnername im Archiv = Ortsname); None, wenn nichts da ist."""
    todo = []
    for name, p in dirs:
        try:
            if p is not None and Path(p).is_dir() and any(Path(p).iterdir()):
                todo.append((name, Path(p)))
        except OSError:
            continue
    if not todo:
        return None
    d = backup_dir(product_id)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    dest, i = d / f"{stamp}.tar.gz", 1
    while dest.exists():
        dest, i = d / f"{stamp}-{i}.tar.gz", i + 1
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for name, p in todo:
                tar.add(str(p), arcname=name.replace("/", "_") or DEFAULT_LOCATION_NAME)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("Spielstände von %s gesichert: %s", product_id, dest)
    rotate_backups(product_id, keep)
    return dest


# --------------------------------------------------------------------------- Zustand für die Anzeige
_state_lock = threading.Lock()


def state_file() -> Path:
    return paths.DATA_DIR / "cloudsaves.json"


def load_state(product_id: str) -> dict:
    data = read_json(state_file(), {})
    v = data.get(str(product_id)) if isinstance(data, dict) else None
    return v if isinstance(v, dict) else {}


def save_state(product_id: str, state: str, message: str = "", now: float | None = None) -> dict:
    entry = {"state": state, "at": now if now is not None else time.time(), "message": message}
    with _state_lock:
        data = read_json(state_file(), {})
        data = data if isinstance(data, dict) else {}
        data[str(product_id)] = entry
        write_json(state_file(), data)
    return entry


_sync_locks: dict[str, threading.Lock] = {}
_sync_guard = threading.Lock()


def _game_lock(product_id: str) -> threading.Lock:
    with _sync_guard:
        return _sync_locks.setdefault(str(product_id), threading.Lock())


def is_syncing(product_id: str) -> bool:
    return _game_lock(product_id).locked()


def cloud_status(product_id: str, gs, settings) -> tuple[str, float, str]:
    """(Art, Zeitpunkt, Meldung) für die Spielansicht: off | busy | never | synced | pending | conflict | quota |
    error | unsupported."""
    if is_syncing(product_id):
        return "busy", time.time(), ""
    if not enabled_for(gs, settings):
        return "off", 0.0, ""
    st = load_state(product_id)
    kind = str(st.get("state") or "")
    at = float(st.get("at") or 0)
    if kind in ("pending", "conflict", "quota", "error", "unsupported"):
        return kind, at, str(st.get("message") or "")
    last = last_sync(gs)
    if kind == "synced" or last:
        return "synced", max(last, at if kind == "synced" else 0.0), ""
    return "never", 0.0, ""


def stamp_of(stamps: dict, name: str) -> float:
    """Zeitpunkt der letzten Synchronisation eines Orts (unlesbare Werte = nie)."""
    v = (stamps or {}).get(name, 0)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def last_sync(gs) -> float:
    """Jüngster erfolgreicher Abgleich über alle Speicherorte (0 = nie)."""
    stamps = getattr(gs, "cloud_timestamps", {}) or {}
    return max([stamp_of(stamps, k) for k in stamps] or [0.0])


def ago(ts: float, now: float | None = None) -> str:
    """„vor 5 Min.“ – kurze relative Zeit für die Spielansicht."""
    d = max(0.0, (now if now is not None else time.time()) - ts)
    if d < 120:
        return tr('gerade eben')
    if d < 3600:
        return tr('vor {0} Min.').format(int(d // 60))
    if d < 86400:
        return tr('vor {0} Std.').format(int(d // 3600))
    if d < 2 * 86400:
        return tr('gestern')
    return tr('vor {0} Tagen').format(int(d // 86400))


def status_label(kind: str, at: float = 0.0, now: float | None = None) -> str:
    """Kurzer Wert für die Kennzahl „Cloud-Spielstände“ der Spielansicht."""
    if kind == "synced":
        return tr('Synchronisiert · {0}').format(ago(at, now)) if at else tr('Synchronisiert')
    return {"off": tr('Aus'), "busy": tr('Synchronisiere …'), "never": tr('Noch nicht synchronisiert'),
            "pending": tr('Nicht synchronisiert'), "conflict": tr('Konflikt'), "quota": tr('Kontingent überschritten'),
            "error": tr('Fehler'), "unsupported": tr('Nicht verfügbar')}.get(kind, kind)


def status_tooltip(kind: str, at: float = 0.0, message: str = "") -> str:
    """Ausführlicher Text (Galaxy-Bezeichnungen) für Tooltip und Eigenschaften."""
    if kind == "synced":
        head = tr('Cloud-Spielstände synchronisiert') + (f" ({_fmt_time(at)})" if at else "")
    elif kind == "off":
        head = tr('Lokale Spielstände werden nicht mit der GOG Galaxy-Cloud synchronisiert.')
    elif kind == "busy":
        head = tr('Synchronisiere Cloud-Spielstände …')
    elif kind == "error":
        head = tr('Beim Synchronisieren deiner Cloud-Spielstände ist ein Problem aufgetreten.')
    elif kind == "unsupported":
        head = tr('Für dieses Spiel sind keine Cloud-Spielstände verfügbar.')
    else:
        head = tr('Cloud-Spielstände nicht synchronisiert')
    return head + ("\n" + message if message and message != head else "")


# --------------------------------------------------------------------------- Ergebnis
@dataclass
class LocationResult:
    name: str
    path: str = ""
    action: str = NONE           # Ergebnis des Vergleichs
    done: str = NONE             # ausgeführt: download | upload | none | skip | conflict | quota | missing
    downloaded: int = 0
    uploaded: int = 0
    deleted_local: int = 0
    deleted_cloud: int = 0
    local_latest: float = 0.0
    cloud_latest: float = 0.0


@dataclass
class SyncResult:
    product_id: str
    state: str                   # synced | pending | conflict | quota | unsupported | busy | cancelled
    message: str = ""
    locations: list[LocationResult] = field(default_factory=list)
    backup: str = ""

    @property
    def changed(self) -> bool:
        return any(r.downloaded or r.uploaded or r.deleted_local or r.deleted_cloud for r in self.locations)

    def summary(self) -> str:
        up = sum(r.uploaded for r in self.locations)
        down = sum(r.downloaded for r in self.locations)
        if self.state == "synced" and (up or down):
            return tr('Synchronisiert in GOG Galaxy-Cloud ({0} hochgeladen, {1} heruntergeladen)').format(up, down)
        if self.state == "synced":
            return tr('Cloud-Spielstände synchronisiert')
        return self.message or tr('Cloud-Spielstände nicht synchronisiert')


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "–"


def conflict_question(title: str, local_latest: float, cloud_latest: float) -> tuple[str, str, list[tuple[str, str]], str]:
    """Rückfrage wie Galaxys Konfliktdialog: (Titel, Text, Optionen, Vorgabe)."""
    body = "\n\n".join((
        title,
        tr('Einige Dateien aus der GOG Galaxy-Cloud stehen im Konflikt mit den Dateien auf diesem Computer. An welchem Ort befinden sich die zu verwendenden Dateien?'),
        tr('GOG Galaxy-Cloud – Zuletzt geändert: {0}').format(_fmt_time(cloud_latest)) + "\n"
        + tr('Dieser Computer – Zuletzt geändert: {0}').format(_fmt_time(local_latest)),
    ))
    options = [("cloud", tr('GOG Galaxy-Cloud')), ("local", tr('Dieser Computer')), ("ignore", tr('Ignorieren'))]
    return tr('Benutzerdaten können für dieses Spiel nicht synchronisiert werden'), body, options, "ignore"


def quota_message(total: int, quota: int, last: float = 0.0) -> str:
    """Text wie Galaxys Dialog „Kontingent überschritten“ (beim Starten und Beenden)."""
    return (tr('Die Benutzerdaten auf diesem Computer übersteigen deinen GOG Galaxy Cloud-Speicherplatz. Starte bitte das Spiel und entferne ältere Spielstände.')
            + "\n\n" + tr('GOG Galaxy Cloud-Speicherplatz: {0}').format(format_bytes(quota))
            + "\n" + tr('Benutzerdaten auf diesem Computer: {0}').format(format_bytes(total))
            + "\n" + tr('Letzte erfolgreiche Synchronisierung: {0}').format(_fmt_time(last)))


def overwrite_question(title: str, cloud_latest: float, local_latest: float) -> tuple[str, str, list[tuple[str, str]], str]:
    """Rückfrage vor „Jetzt hochladen“, wenn die Cloud seit dem letzten Abgleich neuere Dateien hat."""
    body = "\n\n".join((
        title,
        tr('In der GOG Galaxy-Cloud liegen Spielstände, die seit dem letzten Abgleich geändert wurden. Beim Hochladen werden sie durch die Dateien auf diesem Computer ersetzt; Cloud-Dateien, die es hier nicht gibt, werden gelöscht.'),
        tr('GOG Galaxy-Cloud – Zuletzt geändert: {0}').format(_fmt_time(cloud_latest)) + "\n"
        + tr('Dieser Computer – Zuletzt geändert: {0}').format(_fmt_time(local_latest)),
    ))
    return (tr('Spielstände in der Cloud ersetzen?'), body,
            [("cancel", tr('Abbrechen')), ("upload", tr('Hochladen'))], "cancel")


# --------------------------------------------------------------------------- Einstieg
def sync_game(launcher, api, product_id: str, mode: str = "auto", ask: AskFn | None = None,
              status: Callable[[str], None] | None = None, prefix: Path | None = None, runner: str = "",
              save_timestamps: Callable[[dict[str, float]], Any] | None = None,
              session: requests.Session | None = None, confirm: bool = False) -> SyncResult:
    """Spielstände eines Spiels abgleichen (läuft im Arbeits-Thread; ``ask`` darf blockieren).
    ``confirm``: vor ``force-upload`` nachfragen, wenn dabei neuere Cloud-Dateien verloren gingen.
    ``CloudError`` bei Netz-/Zugangsfehlern (Zustand „error“ wird vorher gespeichert)."""
    pid = str(product_id)
    if mode not in MODES:
        raise ValueError(f"unbekannte Betriebsart: {mode}")
    if not getattr(api, "logged_in", False):
        raise CloudError(tr('Nicht angemeldet – Cloud-Spielstände brauchen eine GOG-Anmeldung.'))
    lock = _game_lock(pid)
    if not lock.acquire(blocking=False):
        return SyncResult(pid, "busy", tr('Synchronisation läuft bereits.'))
    try:
        try:
            return _sync(launcher, api, pid, mode, ask, status or (lambda _t: None), prefix, runner,
                         save_timestamps, session, confirm)
        except CloudUnsupported as exc:
            save_state(pid, "unsupported", str(exc))
            return SyncResult(pid, "unsupported", str(exc))
        except (CloudError, ApiError, AuthError, OSError, tarfile.TarError) as exc:
            msg = str(exc) or type(exc).__name__
            save_state(pid, "error", msg)
            if isinstance(exc, CloudError):
                raise
            raise CloudError(msg) from exc
    finally:
        lock.release()


def _sync(launcher, api, pid: str, mode: str, ask: AskFn | None, status: Callable[[str], None],
          prefix: Path | None, runner: str, save_timestamps, session, confirm: bool = False) -> SyncResult:
    status(tr('Synchronisiere Cloud-Spielstände …'))
    gc = game_cloud(launcher, api, pid, prefix, runner, need_secret=True)
    if not any(loc.path is not None for loc in gc.locations):
        raise CloudUnsupported(tr('Kein Speicherort des Spiels ließ sich im Prefix auflösen.'))

    def token(force: bool = False) -> str:
        if force:
            getattr(api, "_game_tokens", {}).pop(gc.client_id, None)
        return api.game_token(gc.client_id, gc.client_secret).access_token

    try:
        tok = api.game_token(gc.client_id, gc.client_secret)
    except (ApiError, AuthError) as exc:
        raise CloudError(tr('Kein Zugang zu den Cloud-Spielständen: {0}').format(exc)) from exc
    user_id = str(getattr(tok, "user_id", "") or api.user_id)
    if not user_id:
        raise CloudError(tr('GOG-Benutzerkennung unbekannt – bitte neu anmelden.'))
    store = CloudStorage(user_id, gc.client_id, token, session)
    items = store.list()
    old = launcher.game_settings.get(pid).cloud_timestamps or {}
    stamps = {k: stamp_of(old, k) for k in old if stamp_of(old, k)}
    result = SyncResult(pid, "synced")

    # 1. Vergleichen
    plans: list[dict[str, Any]] = []
    for loc in gc.locations:
        lr = LocationResult(loc.name, str(loc.path or ""))
        result.locations.append(lr)
        if loc.path is None:
            lr.done = "missing"
            continue
        local = scan_local(loc.path)
        cloud = cloud_files(items, loc.name)
        lr.local_latest = max([f.mtime for f in local] or [0.0])
        lr.cloud_latest = max([f.mtime for f in cloud] or [0.0])
        normal = classify(local, cloud, stamp_of(stamps, loc.name))
        if mode == "force-download":
            cl = forced(local, cloud, DOWNLOAD)
        elif mode == "force-upload":
            cl = forced(local, cloud, UPLOAD)
        else:
            cl = normal
        lr.action = cl.action
        plans.append({"loc": loc, "local": local, "cloud": cloud, "cl": cl, "do": decide(cl.action, mode), "lr": lr,
                      "normal": normal.action})

    # Rückfrage vor dem Ersetzen neuerer Cloud-Stände (nur „Jetzt hochladen“ mit confirm)
    if mode == "force-upload" and confirm and ask is not None:
        risky = [p for p in plans if p["normal"] in (DOWNLOAD, CONFLICT)
                 and (p["cl"].updated_local or p["cl"].only_cloud)]
        if risky:
            heading, body, options, default = overwrite_question(
                gc.title, max(p["lr"].cloud_latest for p in risky), max(p["lr"].local_latest for p in risky))
            if ask(heading, body, options, default) != "upload":
                log.info("Hochladen der Spielstände von %s abgebrochen", pid)
                for p in plans:
                    p["lr"].done = "cancelled"
                result.state, result.message = "cancelled", tr('Hochladen abgebrochen.')
                return result

    # 2. Konflikte: eine Rückfrage für das ganze Spiel (wie Galaxy)
    conflicts = [p for p in plans if p["do"] == CONFLICT]
    if conflicts:
        choice = "ignore"
        if ask is not None:
            heading, body, options, default = conflict_question(
                gc.title, max(p["lr"].local_latest for p in conflicts), max(p["lr"].cloud_latest for p in conflicts))
            choice = ask(heading, body, options, default)
        log.info("Cloud-Konflikt bei %s (%s): %s", pid, ", ".join(p["loc"].name for p in conflicts), choice)
        for p in conflicts:
            if choice == "cloud":
                p["cl"], p["do"] = forced(p["local"], p["cloud"], DOWNLOAD), DOWNLOAD
            elif choice == "local":
                p["cl"], p["do"] = forced(p["local"], p["cloud"], UPLOAD), UPLOAD

    # 3. Kontingent (gilt für alle Orte des Spiels zusammen)
    if any(p["do"] == UPLOAD for p in plans):
        total = sum(f.size for p in plans for f in p["local"])
        if total > gc.config.quota:
            msg = quota_message(total, gc.config.quota, max([stamp_of(stamps, k) for k in stamps] or [0.0]))
            log.warning("Cloud-Kontingent von %s überschritten: %d > %d Bytes", pid, total, gc.config.quota)
            if ask is not None:
                ask(tr('Benutzerdaten können für dieses Spiel nicht synchronisiert werden'), gc.title + "\n\n" + msg,
                    [("cancel", tr('Synchronisation abbrechen'))], "cancel")
            for p in plans:
                if p["do"] == UPLOAD:
                    p["do"] = "quota"
            result.state, result.message = "quota", msg

    # 4. Sicherung, bevor lokale Dateien überschrieben oder gelöscht werden
    local_writes = [p for p in plans if p["do"] == DOWNLOAD and (p["cl"].updated_cloud or p["cl"].only_local)]
    if local_writes:
        try:
            backup = make_backup(pid, [(p["loc"].name, p["loc"].path) for p in local_writes])
        except (OSError, tarfile.TarError) as exc:
            raise CloudError(tr('Sicherung der lokalen Spielstände fehlgeschlagen: {0}').format(exc)) from exc
        result.backup = str(backup or "")

    # 5. Ausführen
    uploaded_locs: list[str] = []
    for p in plans:
        loc, cl, lr, do = p["loc"], p["cl"], p["lr"], p["do"]
        lr.done = do
        if do == DOWNLOAD:
            for f in cl.updated_cloud:
                local_match = next((x for x in p["local"] if x.key == f.key), None)
                dest = local_match.path if local_match is not None else ci_join(loc.path, f.rel)
                store.download(loc.name, f, dest)
                lr.downloaded += 1
            for f in cl.only_local:
                try:
                    Path(f.path).unlink()
                    lr.deleted_local += 1
                except FileNotFoundError:
                    pass
        elif do == UPLOAD:
            for f in cl.updated_local:
                store.upload(loc.name, f)
                lr.uploaded += 1
            for f in cl.only_cloud:
                store.delete(loc.name, f.rel)
                lr.deleted_cloud += 1
            uploaded_locs.append(loc.name)
        if do in (DOWNLOAD, UPLOAD, NONE):
            # nicht vor die Server-Zeit der schon abgeglichenen Cloud-Dateien (Uhr des Servers kann vorgehen)
            stamps[loc.name] = max(time.time(), lr.cloud_latest)

    # Zeitstempel nach dem Hochladen nicht vor die Server-Zeit der hochgeladenen Dateien legen (Uhrenabweichung)
    if any(r.uploaded for r in result.locations):
        try:
            after = store.list()
        except CloudError as exc:
            log.debug("Cloud-Liste nach dem Hochladen nicht abrufbar: %s", exc)
            after = []
        for name in uploaded_locs:
            latest = max([f.mtime for f in cloud_files(after, name)] or [0.0])
            stamps[name] = max(stamp_of(stamps, name), latest)

    if save_timestamps is not None:
        save_timestamps(stamps)
    else:
        launcher.game_settings.update(pid, cloud_timestamps=stamps)

    dos = [p["do"] for p in plans]
    if result.state != "quota":
        if CONFLICT in dos:
            result.state, result.message = "conflict", tr('Konflikt zwischen Cloud und diesem Computer – nicht synchronisiert.')
        elif SKIP in dos:
            result.state = "pending"
            result.message = (tr('Lokale Spielstände sind neuer – sie werden nach dem Spielen hochgeladen.') if mode == "download"
                              else tr('In der Cloud liegen neuere Spielstände – sie werden vor dem nächsten Start heruntergeladen.'))
    save_state(pid, result.state, result.message)
    log.info("Cloud-Abgleich %s (%s): %s", pid, mode, "; ".join(
        f"{r.name}={r.done} hoch={r.uploaded} runter={r.downloaded} geloescht={r.deleted_local}/{r.deleted_cloud}"
        for r in result.locations))
    return result


def action_label(action: str) -> str:
    """Lesbarer Name eines Abgleich-Schritts (CLI, Anzeige)."""
    return {DOWNLOAD: tr('herunterladen'), UPLOAD: tr('hochladen'), CONFLICT: tr('Konflikt'), NONE: tr('nichts zu tun'),
            SKIP: tr('übersprungen'), "quota": tr('Kontingent überschritten'), "cancelled": tr('abgebrochen'),
            "missing": tr('nicht auflösbar')}.get(action, action)


def inspect_game(launcher, api, product_id: str, with_cloud: bool = True,
                 session: requests.Session | None = None) -> dict[str, Any]:
    """Übersicht für Anzeige und ``galaxy-linux cloud --status`` (ohne Secret); ``with_cloud`` vergleicht zusätzlich
    mit der Cloud-Liste (braucht Anmeldung), ohne etwas zu übertragen."""
    pid = str(product_id)
    gc = game_cloud(launcher, api, pid, need_secret=with_cloud)
    gs = launcher.game_settings.get(pid)
    items: list[dict] | None = None
    if with_cloud and getattr(api, "logged_in", False):
        tok = api.game_token(gc.client_id, gc.client_secret)
        store = CloudStorage(str(getattr(tok, "user_id", "") or api.user_id), gc.client_id,
                             lambda force=False: api.game_token(gc.client_id, gc.client_secret).access_token, session)
        items = store.list()
    locs = []
    for loc in gc.locations:
        n, size, latest = dir_stats(loc.path)
        row: dict[str, Any] = {"name": loc.name, "template": loc.template, "path": str(loc.path or ""),
                               "exists": bool(loc.path and loc.path.is_dir()), "local_files": n, "local_bytes": size,
                               "local_latest": latest, "last_sync": stamp_of(gs.cloud_timestamps, loc.name)}
        if items is not None:
            cloud = cloud_files(items, loc.name)
            row.update(cloud_files=len(cloud), cloud_bytes=sum(f.size for f in cloud),
                       cloud_latest=max([f.mtime for f in cloud] or [0.0]),
                       action=classify(scan_local(loc.path), cloud, row["last_sync"]).action)
        locs.append(row)
    return {"product_id": pid, "title": gc.title, "client_id": gc.client_id, "quota": gc.config.quota,
            "user_dir": str(gc.user_dir), "locations": locs, "state": load_state(pid),
            "backups": [str(p) for p in list_backups(pid)], "backup_dir": str(backup_dir(pid))}


# --------------------------------------------------------------------------- Kommandozeile
def run_cli(backend, product_id: str, args) -> int:
    """``galaxy-linux cloud <spiel> [--upload|--download|--status] [--force] [--json]``"""
    try:
        return _run_cli(backend, str(product_id), args)
    except CloudError as exc:
        print(tr('Fehler: {0}').format(exc), file=sys.stderr)
        return 1


def _run_cli(backend, pid: str, args) -> int:
    import json
    if getattr(args, "status", False):
        info = inspect_game(backend.launcher, backend.api, pid, with_cloud=backend.api.logged_in)
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return 0
        gs = backend.game_settings.get(pid)
        print(tr('{0} (Client-ID {1}) · Kontingent {2}').format(info["title"], info["client_id"], format_bytes(info["quota"])))
        print(tr('Automatischer Abgleich: {0}').format(tr('an') if enabled_for(gs, backend.settings) else tr('aus')))
        for row in info["locations"]:
            print(f"  {row['name']}: {row['path'] or row['template'] + ' ' + tr('(nicht auflösbar)')}")
            print("    " + tr('lokal: {0} Dateien, {1} · zuletzt synchronisiert: {2}').format(
                row["local_files"], format_bytes(row["local_bytes"]), _fmt_time(row["last_sync"])))
            if "cloud_files" in row:
                print("    " + tr('Cloud: {0} Dateien, {1} · nächster Schritt: {2}').format(
                    row["cloud_files"], format_bytes(row["cloud_bytes"]), action_label(row["action"])))
        kind, at, message = cloud_status(pid, gs, backend.settings)
        print(tr('Zustand: {0}').format(status_tooltip(kind, at, message)))
        print(tr('Sicherungen: {0} ({1})').format(info["backup_dir"], len(info["backups"])))
        return 0
    if backend.launcher.is_running(pid):
        print(tr('Das Spiel läuft – die Spielstände werden nach dem Beenden synchronisiert.'), file=sys.stderr)
        return 1
    force = getattr(args, "force", False)
    if getattr(args, "upload", False):
        mode = "force-upload" if force else "upload"
    elif getattr(args, "download", False):
        mode = "force-download" if force else "download"
    else:
        mode = "auto"
    res = sync_game(backend.launcher, backend.api, pid, mode, ask=backend.launcher.ui_bridge.ask,
                    status=lambda t: print(t, file=sys.stderr))
    for r in res.locations:
        print(f"  {r.name}: " + tr('{0} · hochgeladen {1}, heruntergeladen {2}, gelöscht lokal {3}, in der Cloud {4}').format(
            action_label(r.done), r.uploaded, r.downloaded, r.deleted_local, r.deleted_cloud))
    if res.backup:
        print(tr('Sicherung: {0}').format(res.backup))
    print(res.summary())
    return 0 if res.state in ("synced", "pending") else 1
