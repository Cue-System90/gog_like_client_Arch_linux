"""HTTP-Client für die GOG-Dienste, die auch der offizielle Galaxy-Client nutzt."""
from __future__ import annotations

from ..i18n import tr

import hashlib
import json
import logging
import os
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable

import requests

from .. import USER_AGENT, paths
from ..jsonstore import read_json, write_json
from . import auth
from ..logsetup import mask_url
from .models import GameMeta, LibraryEntry

log = logging.getLogger(__name__)

EMBED = "https://embed.gog.com"
API = "https://api.gog.com"
GAMESDB = "https://gamesdb.gog.com"
CONTENT_SYSTEM = "https://content-system.gog.com"
CDN_META = "https://gog-cdn-fastly.gog.com/content-system/v2/meta"
CDN_DEP_META = "https://gog-cdn-fastly.gog.com/content-system/v2/dependencies/meta"
GAMEPLAY = "https://gameplay.gog.com"
USERS = "https://users.gog.com"
PRESENCE = "https://presence.gog.com"
CATALOG = "https://catalog.gog.com"

ProgressCb = Callable[[int, int], None]


class ApiError(Exception):
    pass


class NotLoggedIn(ApiError):
    def __init__(self) -> None:
        super().__init__(tr('Nicht bei GOG angemeldet.'))


class GogApi:
    def __init__(self, store: auth.CredentialStore | None = None) -> None:
        self.store = store or auth.CredentialStore()
        self.creds: auth.Credentials | None = self.store.load()
        self.user: dict[str, Any] | None = read_json(paths.API_CACHE_DIR / "user.json", None)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._game_tokens: dict[str, auth.Credentials] = {}
        self._img_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ Sitzungen
    def new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        return s

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = self.new_session()
            self._local.session = s
        return s

    # ------------------------------------------------------------------ Login
    @property
    def logged_in(self) -> bool:
        return self.creds is not None

    @property
    def user_id(self) -> str:
        if self.user and self.user.get("userId"):
            return str(self.user["userId"])
        return self.creds.user_id if self.creds else ""

    @property
    def username(self) -> str:
        return (self.user or {}).get("username", "") if self.user else ""

    def login_with_code(self, code: str) -> dict[str, Any]:
        log.info("Login über GOG-Autorisierungscode")
        creds = auth.exchange_code(code)
        with self._lock:
            self.creds = creds
            self.store.save(creds)
        return self.user_data()

    def logout(self) -> None:
        log.info("Abmeldung, Cache wird geleert")
        with self._lock:
            self.creds = None
            self.user = None
            self._game_tokens.clear()
            self.store.clear()
            for f in paths.API_CACHE_DIR.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _ensure_token(self) -> str:
        with self._lock:
            if not self.creds:
                raise NotLoggedIn()
            if self.creds.expired():
                self.creds = auth.refresh(self.creds)
                self.store.save(self.creds)
            return self.creds.access_token

    def _force_refresh(self) -> str:
        with self._lock:
            if not self.creds:
                raise NotLoggedIn()
            self.creds = auth.refresh(self.creds)
            self.store.save(self.creds)
            return self.creds.access_token

    # ------------------------------------------------------------------ HTTP
    def request(self, method: str, url: str, *, authenticated: bool = True, params: dict | None = None,
                timeout: float = 30, retries: int = 2, **kw: Any) -> requests.Response:
        headers = dict(kw.pop("headers", None) or {})
        if authenticated:
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
        last_exc: Exception | None = None
        r: requests.Response | None = None
        shown = mask_url(url + (("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())) if params else ""))
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                r = self.session.request(method, url, params=params, headers=headers, timeout=timeout, **kw)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s %s fehlgeschlagen (Versuch %d): %s", method, shown, attempt + 1, exc)
                if attempt == retries:
                    break
                time.sleep(1 + attempt)
                continue
            size = "stream" if kw.get("stream") else f"{len(r.content)} B"
            log.debug("%s %s -> %s (%s, %.0f ms)", method, shown, r.status_code, size, (time.monotonic() - t0) * 1000)
            if r.status_code == 401 and authenticated and attempt == 0:
                log.info("401 bei %s – Token wird erneuert", shown)
                headers["Authorization"] = f"Bearer {self._force_refresh()}"
                continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(1 + attempt)
                continue
            return r
        if r is not None:
            return r
        raise ApiError(tr('Netzwerkfehler bei {0}: {1}').format(mask_url(url), last_exc))

    def get(self, url: str, **kw: Any) -> requests.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        if r.status_code == 401:
            raise NotLoggedIn()
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, url))
        try:
            return r.json()
        except ValueError as exc:
            raise ApiError(tr('Ungültige JSON-Antwort von {0}').format(url)) from exc

    def zlib_json(self, url: str) -> Any:
        r = self.get(url, authenticated=False, timeout=60)
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, url))
        data = r.content
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
        return json.loads(data)

    # ------------------------------------------------------------------ Nutzer & Bibliothek
    def user_data(self) -> dict[str, Any]:
        self.user = self.get_json(f"{EMBED}/userData.json")
        write_json(paths.API_CACHE_DIR / "user.json", self.user)
        return self.user

    def owned_ids(self) -> set[str]:
        d = self.get_json(f"{EMBED}/user/data/games")
        return {str(i) for i in d.get("owned", [])}

    def _filtered_products(self, hidden: bool, progress: ProgressCb | None = None,
                           offset: int = 0) -> tuple[list[LibraryEntry], dict]:
        """Alle Seiten von getFilteredProducts mit hiddenFlag 0 bzw. 1; liefert Einträge und die erste Antwort."""
        page, entries, first = 1, [], {}
        while True:
            d = self.get_json(f"{EMBED}/account/getFilteredProducts",
                              params={"mediaType": 1, "page": page, "sortBy": "title", "hiddenFlag": int(hidden)})
            if not isinstance(d, dict):
                raise ApiError(tr('Ungültige JSON-Antwort von {0}').format(f"{EMBED}/account/getFilteredProducts"))
            first = first or d
            entries.extend(LibraryEntry.from_embed(p) for p in d.get("products") or [])
            total = int(d.get("totalPages", 1) or 1)
            if progress:
                progress(offset + page, offset + total)
            if page >= total:
                break
            page += 1
        return entries, first

    def library(self, progress: ProgressCb | None = None, include_hidden: bool = True) -> list[LibraryEntry]:
        """Ganze Bibliothek: sichtbare und (zweiter Durchlauf) versteckte Produkte, wie gog-apis.md §4."""
        from .library_sync import merge_hidden
        visible, first = self._filtered_products(False, progress)
        hidden: list[LibraryEntry] = []
        if include_hidden and first.get("hasHiddenProducts", True) is not False:
            try:
                hidden, _ = self._filtered_products(True, progress, offset=int(first.get("totalPages", 1) or 1))
            except NotLoggedIn:
                raise
            except ApiError as exc:     # versteckte Spiele aus dem Cache behalten, statt sie als „entfernt“ zu verlieren
                log.warning("Versteckte Spiele konnten nicht geladen werden: %s", exc)
                hidden = [e for e in (self.cached_library() or []) if e.is_hidden]
        entries = merge_hidden(visible, hidden)
        write_json(paths.API_CACHE_DIR / "library.json", [e.to_dict() for e in entries])
        return entries

    def cached_library(self) -> list[LibraryEntry] | None:
        data = read_json(paths.API_CACHE_DIR / "library.json", None)
        if not isinstance(data, list):
            return None
        return [LibraryEntry.from_dict(d) for d in data]

    # ------------------------------------------------------------------ Metadaten
    def _cached(self, key: str, fetch: Callable[[], Any], max_age: float = 86400, allow_none: bool = True) -> Any:
        f = paths.API_CACHE_DIR / f"{key}.json"
        try:
            if f.exists() and time.time() - f.stat().st_mtime < max_age:
                return read_json(f, None)
        except OSError:
            pass
        try:
            data = fetch()
        except ApiError as exc:
            cached = read_json(f, None)
            if cached is not None or allow_none:
                log.debug("Nutze Cache für %s (%s)", key, exc)
                return cached
            raise
        write_json(f, data)
        return data

    def product(self, product_id: str, max_age: float = 86400) -> dict | None:
        def fetch() -> dict | None:
            r = self.get(f"{API}/products/{product_id}",
                         params={"expand": "downloads,expanded_dlcs,description,screenshots,changelog"},
                         authenticated=False)
            if r.status_code == 404:
                return None
            if not r.ok:
                raise ApiError(tr('HTTP {0} für Produkt {1}').format(r.status_code, product_id))
            return r.json()
        return self._cached(f"product-{product_id}", fetch, max_age)

    def gamesdb(self, product_id: str, max_age: float = 7 * 86400) -> dict | None:
        def fetch() -> dict | None:
            r = self.get(f"{GAMESDB}/platforms/gog/external_releases/{product_id}", authenticated=False)
            if r.status_code == 404:
                return None
            if not r.ok:
                raise ApiError(tr('HTTP {0} für GamesDB {1}').format(r.status_code, product_id))
            return r.json()
        return self._cached(f"gamesdb-{product_id}", fetch, max_age)

    def game_meta(self, entry: LibraryEntry) -> GameMeta:
        return GameMeta.from_sources(entry, self.gamesdb(entry.id), self.product(entry.id))

    def game_details(self, product_id: str) -> dict:
        return self.get_json(f"{EMBED}/account/gameDetails/{product_id}.json")

    # ------------------------------------------------------------------ Content-System (Galaxy-Downloads)
    def builds(self, product_id: str, platform: str = "windows", password: str = "") -> list[dict]:
        """Builds aller Kanäle; mit ``password`` zusätzlich die privaten Beta-Kanäle (wie gogdl ``--password``)."""
        params: dict[str, Any] = {"generation": 2}
        if password:
            params["password"] = password
        d = self.get_json(f"{CONTENT_SYSTEM}/products/{product_id}/os/{platform}/builds",
                          params=params, authenticated=self.logged_in)
        return list(d.get("items") or [])

    def depot_manifest(self, md5: str) -> dict:
        return self.zlib_json(f"{CDN_META}/{md5[:2]}/{md5[2:4]}/{md5}")

    def dependency_repository(self) -> dict:
        d = self.get_json(f"{CONTENT_SYSTEM}/dependencies/repository", params={"generation": 2}, authenticated=False)
        return self.zlib_json(d["repository_manifest"])

    def dependency_depot_manifest(self, md5: str) -> dict:
        return self.zlib_json(f"{CDN_DEP_META}/{md5[:2]}/{md5[2:4]}/{md5}")

    @staticmethod
    def pick_endpoint(urls: list[dict], generation: int = 2) -> str:
        cands = [u for u in urls if generation in (u.get("supports_generation") or [generation])]
        if not cands:
            cands = list(urls)
        cands.sort(key=lambda u: (bool(u.get("fallback_only")), int(u.get("priority", 999))))
        if not cands:
            raise ApiError(tr('Kein CDN-Endpunkt verfügbar'))
        ep = cands[0]
        url = ep["url_format"]
        for k, v in (ep.get("parameters") or {}).items():
            url = url.replace("{" + k + "}", str(v))
        return url.rstrip("/")

    def secure_link(self, product_id: str, path: str = "/") -> str:
        d = self.get_json(f"{CONTENT_SYSTEM}/products/{product_id}/secure_link",
                          params={"_version": 2, "generation": 2, "path": path})
        return self.pick_endpoint(d.get("urls") or [])

    def dependency_link(self) -> str:
        d = self.get_json(f"{CONTENT_SYSTEM}/open_link",
                          params={"_version": 2, "generation": 2, "path": "/dependencies/store/"},
                          authenticated=False)
        return self.pick_endpoint(d.get("urls") or [])

    # ------------------------------------------------------------------ Installer-Downloads (Offline-Installer, Linux-.sh)
    def downlink(self, url: str) -> dict:
        if url.startswith("/"):
            url = EMBED + url
        return self.get_json(url)

    def stream_download(self, url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
                        cancel: threading.Event | None = None, authenticated: bool = False,
                        chunk_size: int = 1 << 20, throttle: Any = None) -> Path:
        """Lädt eine (große) Datei mit Fortsetzung herunter; schreibt nach dest. Abbrechen (auch Pausieren) wirft
        ``DownloadCancelled`` und lässt die ``.part``-Datei für das Fortsetzen liegen; ``throttle`` begrenzt die
        Bandbreite (content/bandwidth.Throttle)."""
        from ..content.downloader import DownloadCancelled
        if throttle is not None and throttle.rate() > 0:
            chunk_size = min(chunk_size, 64 * 1024)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        r = self.request("GET", url, authenticated=authenticated, headers=headers, stream=True, timeout=60, allow_redirects=True)
        if r.status_code == 416:  # schon vollständig
            part.rename(dest)
            return dest
        if not r.ok:
            raise ApiError(tr('Download fehlgeschlagen (HTTP {0}) für {1}').format(r.status_code, url))
        if r.status_code != 206:
            have = 0
        total = int(r.headers.get("Content-Length", 0) or 0) + have
        mode = "ab" if have else "wb"
        with open(part, mode) as fh:
            for block in r.iter_content(chunk_size):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                if throttle is not None:
                    throttle.consume(len(block), cancel)
                fh.write(block)
                have += len(block)
                if progress:
                    progress(have, total)
        part.rename(dest)
        return dest

    # ------------------------------------------------------------------ Gameplay: Spielzeit, Erfolge
    def post_playtime(self, product_id: str, minutes: int, session_date: float | None = None) -> bool:
        if not self.logged_in or minutes <= 0:
            return False
        uid = self.user_id
        if not uid:
            return False
        try:
            r = self.request("POST", f"{GAMEPLAY}/games/{product_id}/users/{uid}/sessions",
                             json={"session_date": int(session_date or time.time()), "time": int(minutes)})
            return r.ok
        except ApiError as exc:
            log.warning("Spielzeit konnte nicht übertragen werden: %s", exc)
            return False

    def playtime(self, product_id: str) -> tuple[int, float] | None:
        """Spielzeit bei GOG (Summe aller Geräte, wie Galaxys „Periodic Game Times Sync“): (Minuten, letzte Sitzung)
        oder None, wenn GOG nichts liefert."""
        uid = self.user_id
        if not self.logged_in or not uid:
            return None
        r = self.request("GET", f"{GAMEPLAY}/games/{product_id}/users/{uid}/sessions", timeout=20, retries=1)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, "gameplay sessions"))
        try:
            d = r.json()
        except ValueError:
            return None
        return parse_playtime(d)

    def set_presence(self, game_id: str = "") -> bool:
        """Online-Status bei GOG setzen (Freunde sehen „online“ bzw. das laufende Spiel) – wie Galaxy alle 5 min."""
        uid = self.user_id
        if not self.logged_in or not uid:
            return False
        from .. import APP_NAME, __version__
        body: dict[str, Any] = {"application_type": APP_NAME, "force_update": False, "presence": "online",
                                "version": __version__}
        if game_id:
            body["game_id"] = str(game_id)
        r = self.request("POST", f"{PRESENCE}/users/{uid}/status", json=body, timeout=15, retries=1)
        return r.ok

    def clear_presence(self) -> bool:
        """Offline bzw. „Unsichtbar“: Status bei GOG löschen."""
        uid = self.user_id
        if not self.logged_in or not uid:
            return False
        r = self.request("DELETE", f"{PRESENCE}/users/{uid}/status", timeout=4, retries=0)
        return r.ok or r.status_code == 404

    def catalog_search(self, query: str, limit: int = 8, locale: str = "de-DE", country: str = "") -> list[dict]:
        """Store-Suche über catalog.gog.com (Power Search „Im Store kaufen“): Spiele und Pakete."""
        params: dict[str, Any] = {"limit": limit, "order": "desc:score", "productType": "in:game,pack",
                                  "query": f"like:{query}", "locale": locale}
        if country:
            params["countryCode"] = country
        r = self.request("GET", f"{CATALOG}/v1/catalog", params=params, authenticated=False, timeout=15, retries=1)
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, "catalog.gog.com"))
        try:
            d = r.json()
        except ValueError as exc:
            raise ApiError(tr('Ungültige JSON-Antwort von {0}').format("catalog.gog.com")) from exc
        return [p for p in (d.get("products") or []) if isinstance(p, dict)]

    def game_token(self, client_id: str, client_secret: str) -> auth.Credentials:
        with self._lock:
            tok = self._game_tokens.get(client_id)
            if tok and not tok.expired():
                return tok
            if not self.creds:
                raise NotLoggedIn()
            tok = auth.game_token(self.creds.refresh_token, client_id, client_secret)
            self._game_tokens[client_id] = tok
            return tok

    def achievements(self, client_id: str, client_secret: str) -> dict:
        tok = self.game_token(client_id, client_secret)
        uid = tok.user_id or self.user_id
        r = self.session.get(f"{GAMEPLAY}/clients/{client_id}/users/{uid}/achievements",
                             headers={"Authorization": f"Bearer {tok.access_token}"}, timeout=30)
        if not r.ok:
            raise ApiError(tr('Erfolge nicht abrufbar (HTTP {0})').format(r.status_code))
        return r.json()

    # ------------------------------------------------------------------ Bilder
    def cached_image(self, url: str) -> Path | None:
        if not url:
            return None
        if url.startswith("file://"):              # eigenes Cover („Bearbeiten …“): Datei direkt
            p = Path(url[7:])
            return p if p.is_file() and p.stat().st_size > 0 else None
        name = hashlib.sha1(url.encode()).hexdigest()
        ext = os.path.splitext(url.split("?")[0])[1] or ".img"
        dest = paths.IMAGE_CACHE_DIR / (name + ext)
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        with self._lock:
            lock = self._img_locks.setdefault(name, threading.Lock())
        with lock:
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            try:
                r = self.session.get(url, timeout=30)
            except requests.RequestException:
                return None
            if not r.ok or not r.content:
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(dest)
            return dest


def parse_playtime(d: Any) -> tuple[int, float] | None:
    """Antwort von gameplay …/sessions: ``time_sum`` (Minuten) und ``last_session_date`` (Unixzeit oder ISO)."""
    if not isinstance(d, dict) or d.get("time_sum") is None:
        return None
    try:
        minutes = int(d.get("time_sum") or 0)
    except (TypeError, ValueError):
        return None
    last = d.get("last_session_date")
    ts = 0.0
    if isinstance(last, (int, float)):
        ts = float(last)
    elif isinstance(last, str) and last:
        if last.isdigit():
            ts = float(last)
        else:
            from datetime import datetime
            try:
                ts = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0.0
    return max(0, minutes), ts
