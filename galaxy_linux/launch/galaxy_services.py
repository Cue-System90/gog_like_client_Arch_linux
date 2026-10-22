"""Galaxy-Dienste für Spiele mit GOG Galaxy-SDK: comet und der GalaxyCommunication-Platzhalterdienst.

Spiele mit Galaxy-SDK (Galaxy.dll/Galaxy64.dll) sprechen mit dem „Communication Service“ von GOG Galaxy auf
``127.0.0.1:9977``: Erfolge, Statistiken, Bestenlisten, Spielzeit. Unter Linux ersetzt ihn **comet**
(https://github.com/imLinguin/comet, Apache-2.0) – genau wie in Heroic (heroic-gog.md §4/§5, gog-apis.md §7):

1. Platzhalterdienst im Prefix: ``GalaxyCommunication.exe`` nach ``C:\\ProgramData\\GOG.com\\Galaxy\\redists\\``,
   ``sc create GalaxyCommunication …`` und ``reg add HKLM\\SOFTWARE\\WOW6432Node\\GOG.com\\GalaxyClient\\paths``.
   Manche SDK-Versionen „wecken“ diesen Dienst, bevor sie sich mit comet verbinden.
2. comet läuft neben dem Spiel: ``comet --from-heroic --username <name> --quit``. Die Tokens stehen nie auf der
   Kommandozeile (``/proc/<pid>/cmdline``), sondern in einer Heroic-kompatiblen Datei in einem privaten Ordner
   (``~/.local/share/galaxy-linux/comet-auth/heroic/gog_store/auth.json``, Ordner 0700, Datei 0600), den comet über
   ``XDG_CONFIG_PATH`` findet (upstream ``src/import_parsers/heroic.rs``, v0.3.2). Nach dem Ende wird sie gelöscht.

Werkzeugsuche: Einstellung ``comet_path`` → ``comet`` im PATH → Heroic-Paket → eigener Download unter
``~/.local/share/galaxy-linux/tools/comet/<version>/`` (SHA-256-geprüft, atomar geschrieben). comet-Ausgaben landen
maskiert in ``logs/comet.log`` (comet protokolliert u. a. Client-Secrets der Spiele).
"""
from __future__ import annotations

from ..i18n import tr

import atexit
import dataclasses
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .. import USER_AGENT, paths
from ..clientinfo import is_newer
from ..jsonstore import read_json, write_json
from ..logsetup import mask_text

log = logging.getLogger(__name__)

COMET_VERSION = "0.3.2"
COMET_REPO_URL = "https://github.com/imLinguin/comet"
COMET_LICENSE = "Apache-2.0"
RELEASE_URL = COMET_REPO_URL + "/releases/download/v{version}/{asset}"
COMET_HOST = "127.0.0.1"
COMET_PORT = 9977
GALAXY_CLIENT_ID = "46899977096215655"     # Schlüssel in Heroics auth.json (= auth.CLIENT_ID)

HEROIC_BIN = Path("/usr/lib/heroic-games-launcher/app.asar.unpacked/build/bin/x64")
HEROIC_COMET = HEROIC_BIN / "linux" / "comet"
HEROIC_DUMMY = HEROIC_BIN / "win32" / "GalaxyCommunication.exe"

# Platzhalterdienst und Registry wie Heroic (prepareWineLaunch, main.js:34717–34760)
DUMMY_WIN_PATH = "C:\\ProgramData\\GOG.com\\Galaxy\\redists\\GalaxyCommunication.exe"
REG_ADD_CLIENT = ["reg", "add", "HKLM\\SOFTWARE\\WOW6432Node\\GOG.com\\GalaxyClient\\paths", "/v", "client",
                  "/t", "REG_SZ", "/d", "C:\\Program Files\\GOG Galaxy", "/f"]
SC_CREATE_SERVICE = ["sc", "create", "GalaxyCommunication", "binpath=" + DUMMY_WIN_PATH]

WATCH_INTERVAL = 15.0          # Sekunden zwischen zwei Prüfungen, ob noch ein Spiel comet braucht
RETRY_AFTER_FAILURE = 600.0    # nach einem fehlgeschlagenen automatischen Download so lange nicht erneut versuchen
LOG_ROTATE_BYTES = 2_000_000


class CometError(Exception):
    pass


@dataclass(frozen=True)
class Asset:
    key: str
    remote: str          # Dateiname im GitHub-Release
    filename: str        # Dateiname im Werkzeugordner
    sha256: str
    size: int
    mode: int = 0o755


ASSETS: dict[str, Asset] = {
    "comet": Asset("comet", "comet-x86_64-unknown-linux-gnu", "comet",
                   "2d6694d544fd3155d90d540e70bc1be767a6b9fdda130275f2b79616ff14e843", 12881904),
    "dummy": Asset("dummy", "GalaxyCommunication-dummy.exe", "GalaxyCommunication-dummy.exe",
                   "c7695267da363a861af99db95cafe68b732ae743e5830b4feea1bc7ee745f99d", 59684),
}

NOTICE_TEXT = (
    "comet {version} - {url}\n"
    "Copyright the comet authors (imLinguin and contributors)\n"
    "Licensed under the Apache License, Version 2.0: https://www.apache.org/licenses/LICENSE-2.0\n"
    "Includes GalaxyCommunication-dummy.exe from the same release (dummy-service).\n"
    "Downloaded unmodified from the official GitHub release by galaxy-linux; SHA-256 verified.\n"
)


# --------------------------------------------------------------------------- Pfade
def tools_root() -> Path:
    return paths.DATA_DIR / "tools" / "comet"


def download_dir(version: str = COMET_VERSION) -> Path:
    return tools_root() / version


def auth_root() -> Path:
    """Privater Ordner, den comet als ``XDG_CONFIG_PATH`` bekommt."""
    return paths.DATA_DIR / "comet-auth"


def auth_file(root: Path | None = None) -> Path:
    return (root or auth_root()) / "heroic" / "gog_store" / "auth.json"


def comet_log_file() -> Path:
    return paths.LOG_DIR / "comet.log"


# --------------------------------------------------------------------------- Prüfsummen und Versionen
_verified: dict[tuple[str, int, int, str], bool] = {}
_version_cache: dict[tuple[str, int, int], str] = {}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stat_key(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path), st.st_size, st.st_mtime_ns


def verified(path: Path, expected: str) -> bool:
    """Datei vorhanden und SHA-256 stimmt (pro Größe/Änderungszeit nur einmal berechnet)."""
    key = _stat_key(path)
    if key is None:
        return False
    ck = (*key, expected)
    if ck not in _verified:
        try:
            _verified[ck] = sha256_file(path) == expected
        except OSError:
            return False
        if not _verified[ck]:
            log.warning("Prüfsumme stimmt nicht: %s", path)
    return _verified[ck]


def parse_comet_version(text: str) -> str:
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", text or "")
    return m.group(1) if m else ""


def comet_version(path: Path, timeout: float = 10) -> str:
    """Version über ``comet --version`` (z. B. „comet 0.3.2“ → „0.3.2“), "" wenn nicht ermittelbar."""
    key = _stat_key(Path(path))
    if key is None:
        return ""
    if key in _version_cache:
        return _version_cache[key]
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
        version = parse_comet_version(out.stdout + " " + out.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("comet --version fehlgeschlagen (%s): %s", path, exc)
        version = ""
    _version_cache[key] = version
    return version


# --------------------------------------------------------------------------- Werkzeugsuche
@dataclass
class CometTool:
    path: Path
    source: str          # settings | path | heroic | download
    version: str = ""

    @property
    def outdated(self) -> bool:
        """Ältere Version als die, die galaxy-linux herunterladen würde."""
        return bool(self.version) and is_newer(COMET_VERSION, self.version)


def comet_candidates(comet_path: str = "") -> list[tuple[str, Path]]:
    """Suchreihenfolge: eigene Einstellung → PATH → Heroic-Paket → eigener Download."""
    out: list[tuple[str, Path]] = []
    if (comet_path or "").strip():
        out.append(("settings", Path(os.path.expanduser(comet_path.strip()))))
    found = shutil.which("comet")
    if found:
        out.append(("path", Path(found)))
    out.append(("heroic", HEROIC_COMET))
    out.append(("download", download_dir() / ASSETS["comet"].filename))
    return out


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_comet(comet_path: str = "", with_version: bool = True) -> CometTool | None:
    for source, p in comet_candidates(comet_path):
        ok = _executable(p) and (source != "download" or verified(p, ASSETS["comet"].sha256))
        if ok:
            return CometTool(p, source, comet_version(p) if with_version else "")
        if source == "settings":
            log.info("comet-Pfad aus den Einstellungen nicht nutzbar: %s", p)
    return None


def dummy_candidates() -> list[tuple[str, Path]]:
    return [("download", download_dir() / ASSETS["dummy"].filename), ("heroic", HEROIC_DUMMY)]


def find_dummy() -> Path | None:
    """GalaxyCommunication-Platzhalter: eigener (geprüfter) Download, sonst der aus dem Heroic-Paket."""
    for source, p in dummy_candidates():
        if p.is_file() and (source != "download" or verified(p, ASSETS["dummy"].sha256)):
            return p
    return None


# --------------------------------------------------------------------------- Download
_download_lock = threading.Lock()
_last_failure = 0.0


def asset_url(asset: Asset, version: str = COMET_VERSION) -> str:
    return RELEASE_URL.format(version=version, asset=asset.remote)


def _check_arch(asset: Asset) -> None:
    if asset.key == "comet" and platform.machine().lower() not in ("x86_64", "amd64"):
        raise CometError(tr('comet gibt es hier nur für x86_64 – bitte comet selbst installieren und den Pfad eintragen.'))


def download_asset(asset: Asset, dest_dir: Path, session: Any = None,
                   progress: Callable[[int, int], None] | None = None,
                   cancel: threading.Event | None = None, timeout: float = 60) -> Path:
    """Eine Release-Datei laden, SHA-256 prüfen, dann atomar ablegen (``mode`` 755). Nichts bleibt halb liegen."""
    _check_arch(asset)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / asset.filename
    url = asset_url(asset)
    http = session or requests
    log.info("Lade %s herunter: %s", asset.remote, url)
    try:
        r = http.get(url, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise CometError(tr('Download von {0} fehlgeschlagen: {1}').format(asset.remote, exc)) from exc
    fd, tmp = tempfile.mkstemp(prefix=f".{asset.filename}.", suffix=".part", dir=str(dest_dir))
    try:
        if r.status_code != 200:
            raise CometError(tr('Download von {0} fehlgeschlagen: HTTP {1}').format(asset.remote, r.status_code))
        total = int(r.headers.get("Content-Length") or 0) or asset.size
        limit = max(total, asset.size) * 2 + (1 << 20)
        h = hashlib.sha256()
        done = 0
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            for chunk in r.iter_content(1 << 16):
                if cancel is not None and cancel.is_set():
                    raise CometError(tr('Download abgebrochen.'))
                if not chunk:
                    continue
                done += len(chunk)
                if done > limit:
                    raise CometError(tr('Unerwartete Dateigröße bei {0}.').format(asset.remote))
                h.update(chunk)
                fh.write(chunk)
                if progress is not None:
                    progress(done, total)
            fh.flush()
            os.fsync(fh.fileno())
        if h.hexdigest() != asset.sha256:
            log.warning("Prüfsumme von %s stimmt nicht (erwartet %s, erhalten %s)", asset.remote, asset.sha256, h.hexdigest())
            raise CometError(tr('Prüfsumme von {0} stimmt nicht – Datei verworfen.').format(asset.remote))
        os.chmod(tmp, asset.mode)
        os.replace(tmp, final)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        close = getattr(r, "close", None)
        if close is not None:
            close()
    log.info("%s %s geprüft und abgelegt: %s", asset.remote, COMET_VERSION, final)
    return final


def download_tools(keys: Iterable[str] = ("comet", "dummy"), session: Any = None,
                   progress: Callable[[int, int], None] | None = None,
                   cancel: threading.Event | None = None) -> dict[str, Path]:
    """comet und/oder den Platzhalterdienst aus dem offiziellen Release laden (schon vorhandene, geprüfte bleiben)."""
    global _last_failure
    with _download_lock:
        dest = download_dir()
        assets = [ASSETS[k] for k in keys]
        todo = [a for a in assets if not verified(dest / a.filename, a.sha256)]
        grand = sum(a.size for a in todo) or 1
        base = 0
        out = {a.key: dest / a.filename for a in assets}
        try:
            for a in todo:
                cb = None
                if progress is not None:
                    cb = (lambda off: (lambda d, _t: progress(min(off + d, grand), grand)))(base)
                out[a.key] = download_asset(a, dest, session=session, progress=cb, cancel=cancel)
                base += a.size
        except CometError:
            _last_failure = time.monotonic()
            raise
        if todo:
            try:
                (dest / "NOTICE").write_text(NOTICE_TEXT.format(version=COMET_VERSION, url=COMET_REPO_URL), encoding="utf-8")
            except OSError:
                pass
            _remove_old_versions(dest)
        return out


def _remove_old_versions(keep: Path) -> None:
    """Ältere eigene comet-Downloads entfernen (nur Versionsordner unter tools/comet)."""
    try:
        for d in tools_root().iterdir():
            if d.is_dir() and d != keep and re.fullmatch(r"\d+(?:\.\d+)+", d.name):
                shutil.rmtree(d, ignore_errors=True)
                log.info("Alte comet-Version entfernt: %s", d)
    except OSError:
        pass


def download_and_select(comet_path: str = "", session: Any = None, progress: Callable[[int, int], None] | None = None,
                        cancel: threading.Event | None = None) -> tuple[CometTool | None, str]:
    """Ausdrücklicher Download („comet herunterladen“/„Aktualisieren“). Gewinnt danach noch eine ältere comet-Version
    aus PATH, Heroic oder dem eigenen Pfad, soll der neue Download als ``comet_path`` eingetragen werden.
    Rückgabe: (gefundenes Werkzeug, neuer Wert für ``comet_path`` oder "" = unverändert). Ändert keine Einstellungen
    selbst – läuft im Hintergrund, gespeichert wird im Hauptthread bzw. von der CLI."""
    files = download_tools(session=session, progress=progress, cancel=cancel)
    tool = find_comet(comet_path)
    if tool is not None and tool.source != "download" and tool.outdated:
        new_path = str(files["comet"])
        return find_comet(new_path), new_path
    return tool, ""


def _may_auto_download() -> bool:
    return not _last_failure or time.monotonic() - _last_failure > RETRY_AFTER_FAILURE


def ensure_comet(settings: Any, status: Callable[[str], None] | None = None) -> CometTool | None:
    """comet suchen; beim ersten Bedarf (Galaxy-Dienste an) einmalig herunterladen."""
    tool = find_comet(settings.comet_path)
    if tool is not None or not _may_auto_download():
        return tool
    if status is not None:
        status(tr('Lade comet herunter …'))
    try:
        download_tools(("comet", "dummy"))
    except CometError as exc:
        log.warning("comet-Download fehlgeschlagen: %s", exc)
        return None
    return find_comet(settings.comet_path)


def ensure_dummy(settings: Any, status: Callable[[str], None] | None = None) -> Path | None:
    """Platzhalterdienst suchen, sonst (zusammen mit comet, falls es auch fehlt) herunterladen."""
    found = find_dummy()
    if found is not None or not _may_auto_download():
        return found
    keys = ["dummy"] + ([] if find_comet(settings.comet_path, with_version=False) else ["comet"])
    if status is not None:
        status(tr('Lade Galaxy-Dienste herunter …'))
    try:
        return download_tools(keys)["dummy"]
    except CometError as exc:
        log.warning("Download des GalaxyCommunication-Dienstes fehlgeschlagen: %s", exc)
        return None


# --------------------------------------------------------------------------- Port
def port_in_use(port: int = COMET_PORT, host: str = COMET_HOST) -> bool:
    """Ist der Port belegt (z. B. comet von Heroic)? Prüft per bind() – eine Probe-Verbindung würde comet als
    Client zählen und mit ``--quit`` vorzeitig beenden. SO_REUSEADDR wie bei comet selbst (Rust/tokio)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


# --------------------------------------------------------------------------- Zugangsdaten für comet
def fresh_credentials(api: Any) -> Any:
    """Galaxy-Zugangsdaten frisch erneuern (comet bekommt ein Token mit voller Laufzeit); offline genügt ein noch
    gültiges. Gibt eine Kopie zurück."""
    from ..api.client import NotLoggedIn
    with api._lock:
        if not api.creds:
            raise NotLoggedIn()
        try:
            api._force_refresh()
        except Exception as exc:
            if api.creds is None or api.creds.expired():
                raise CometError(tr('Anmeldung bei GOG konnte nicht erneuert werden: {0}').format(exc)) from exc
            log.warning("Token-Erneuerung für comet fehlgeschlagen (%s) – nutze das noch gültige Token", exc)
        return dataclasses.replace(api.creds)


def heroic_auth_payload(creds: Any, now: float | None = None) -> dict[str, Any]:
    """Inhalt im Format von Heroics ``gog_store/auth.json``; comet liest access_token, refresh_token und user_id
    (alles Zeichenketten) unter der Galaxy-Client-ID. Sitzungs-ID u. Ä. bleiben draußen."""
    now = time.time() if now is None else now
    return {GALAXY_CLIENT_ID: {
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "user_id": str(creds.user_id),
        "expires_in": max(0, int(creds.expires_at - now)),
        "token_type": "bearer",
        "loginTime": int(now * 1000),
    }}


def _private_dirs(root: Path) -> None:
    for d in (root, root / "heroic", root / "heroic" / "gog_store"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def write_auth_file(creds: Any, root: Path | None = None, now: float | None = None) -> tuple[Path, dict[str, Any]]:
    root = root or auth_root()
    _private_dirs(root)
    path = auth_file(root)
    payload = heroic_auth_payload(creds, now)
    write_json(path, payload, mode=0o600)
    return path, payload


def remove_auth_file(root: Path | None = None) -> None:
    try:
        auth_file(root).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("comet-Zugangsdatei konnte nicht gelöscht werden: %s", exc)


def rotated_credentials(path: Path, written: dict[str, Any], now: float | None = None) -> Any:
    """Hat jemand (comet) die Tokens in der Datei erneuert? Dann neue ``Credentials``, sonst None."""
    from ..api.auth import Credentials
    data = read_json(path, None)
    old = written.get(GALAXY_CLIENT_ID) or {}
    entry = data.get(GALAXY_CLIENT_ID) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    access, refresh = entry.get("access_token"), entry.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
        return None
    if access == old.get("access_token") and refresh == old.get("refresh_token"):
        return None
    now = time.time() if now is None else now
    try:
        expires_in = int(entry.get("expires_in") or 3600)
        login = float(entry.get("loginTime") or 0) / 1000.0
    except (TypeError, ValueError):
        expires_in, login = 3600, 0.0
    expires_at = (login if login > 0 else now) + expires_in
    return Credentials(access_token=access, refresh_token=refresh, expires_at=expires_at,
                       user_id=str(entry.get("user_id") or old.get("user_id") or ""))


def import_rotated(api: Any, path: Path, written: dict[str, Any]) -> bool:
    """Von comet erneuerte Tokens in die eigene Anmeldung übernehmen – nur wenn sich die eigene Sitzung seitdem
    nicht selbst erneuert hat (dann sind die eigenen neuer)."""
    new = rotated_credentials(path, written)
    if new is None or api is None:
        return False
    old = written.get(GALAXY_CLIENT_ID) or {}
    with api._lock:
        cur = api.creds
        if cur is None:
            return False
        if cur.refresh_token != old.get("refresh_token"):
            log.info("Eigene Anmeldung wurde inzwischen erneuert – Tokens aus der comet-Datei verworfen")
            return False
        new.session_id = cur.session_id
        new.client_id = cur.client_id
        api.creds = new
        api.store.save(new)
    log.info("Von comet erneuerte GOG-Tokens übernommen")
    return True


def account_name(api: Any) -> str:
    """GOG-Benutzername für ``comet --username`` (notfalls die Benutzer-ID)."""
    name = getattr(api, "username", "") or ""
    if not name:
        try:
            api.user_data()
            name = getattr(api, "username", "") or ""
        except Exception as exc:
            log.info("Benutzername für comet nicht ermittelbar: %s", exc)
    return name or str(getattr(api, "user_id", "") or "")


# --------------------------------------------------------------------------- comet-Prozess
def comet_command(comet: Path | str, username: str) -> list[str]:
    """Aufruf wie Heroic – Tokens kommen aus der Datei, nie über die Kommandozeile."""
    return [str(comet), "--from-heroic", "--username", username, "--quit"]


def comet_env(root: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """comet liest ``$XDG_CONFIG_PATH/heroic/gog_store/auth.json`` (nicht XDG_CONFIG_HOME) und braucht HOME."""
    env = dict(os.environ if base is None else base)
    env.pop("LD_PRELOAD", None)
    env.setdefault("HOME", str(Path.home()))
    env["XDG_CONFIG_PATH"] = str(root)
    return env


_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{80,}(?:\.[A-Za-z0-9_\-]+)*")


def mask_line(text: str, secrets: Iterable[str] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return _LONG_TOKEN.sub("***", mask_text(text))


class _LogPump(threading.Thread):
    """Liest comets Rohausgabe (im privaten Ordner) und hängt sie maskiert an ``logs/comet.log`` an. comet schreibt
    in eine Datei statt in eine Pipe – so blockiert es nie, auch wenn galaxy-linux vor dem Spiel endet."""

    def __init__(self, raw: Path, out: Path, secrets: list[str], proc: subprocess.Popen | None) -> None:
        super().__init__(name="gl-comet-log", daemon=True)
        self.raw, self.out, self.secrets, self.proc = raw, out, secrets, proc
        self._finish = threading.Event()

    def run(self) -> None:
        buf = b""
        try:
            with open(self.raw, "rb") as src, open(self.out, "ab") as dst:
                while True:
                    data = src.read()
                    if data:
                        buf += data
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            dst.write((mask_line(line.decode("utf-8", "replace"), self.secrets) + "\n").encode())
                        dst.flush()
                        continue
                    if self._finish.is_set() or self.proc is None or self.proc.poll() is not None:
                        rest = src.read()
                        if rest:
                            buf += rest
                            continue
                        break
                    time.sleep(0.3)
                if buf:
                    dst.write((mask_line(buf.decode("utf-8", "replace"), self.secrets) + "\n").encode())
        except OSError as exc:
            log.warning("comet-Protokoll: %s", exc)
        try:
            self.raw.unlink()
        except OSError:
            pass

    def finish(self, timeout: float = 3.0) -> None:
        self._finish.set()
        if self.is_alive():
            self.join(timeout)


def _rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def terminate_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """SIGTERM, nach ``timeout`` Sekunden SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        log.info("comet reagiert nicht auf SIGTERM – wird beendet (SIGKILL)")
        try:
            proc.kill()
            proc.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


class CometService:
    """Ein comet für alle laufenden Spiele (Port 9977 gibt es nur einmal). Jedes Spiel meldet sich an
    (``acquire``) und ab (``release``); wenn keins mehr läuft, wird comet beendet und die Zugangsdatei gelöscht.
    Ein Wächter-Thread räumt auch dann auf, wenn ein Start nach ``prepare`` scheitert oder comet sich selbst beendet."""

    def __init__(self, port: int = COMET_PORT, host: str = COMET_HOST) -> None:
        self.port, self.host = port, host
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.holders: set[str] = set()
        self.root: Path | None = None
        self.written: dict[str, Any] | None = None
        self.pump: _LogPump | None = None
        self.api: Any = None
        self.is_running: Callable[[str], bool] | None = None
        self._stop_evt = threading.Event()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def acquire(self, product_id: str, comet: Path | str, creds: Any, username: str, api: Any = None,
                is_running: Callable[[str], bool] | None = None, wait: float = 5.0) -> str:
        """comet für ein Spiel bereitstellen: "started" (neu gestartet), "shared" (läuft schon für ein anderes
        Spiel) oder "foreign" (Port von einem fremden Dienst belegt, z. B. Heroics comet – der wird mitbenutzt);
        ohne ``creds`` wird nur mitbenutzt ("stopped", wenn comet nicht mehr läuft)."""
        product_id = str(product_id)
        with self._lock:
            if self.running():
                self.holders.add(product_id)
                return "shared"
            if self.proc is not None:
                self._cleanup_locked()
            if port_in_use(self.port, self.host):
                return "foreign"
            if creds is None:                       # nur Mitbenutzen angefragt, comet läuft aber nicht mehr
                return "stopped"
            root = auth_root()
            _private_dirs(root)
            raw = root / "comet-raw.log"
            out = comet_log_file()
            out.parent.mkdir(parents=True, exist_ok=True)
            if raw.exists():                       # Rest einer abgebrochenen Sitzung maskiert übernehmen
                _LogPump(raw, out, [], None).run()
            _rotate_log(out)
            self.root = root
            path, self.written = write_auth_file(creds, root)
            argv = comet_command(comet, username)
            with open(out, "ab") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} comet für {product_id}: {shlex.join(argv)} =====\n".encode())
            fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                self.proc = subprocess.Popen(argv, env=comet_env(root), cwd=str(root), stdout=fd, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self.proc = None
                remove_auth_file(root)
                self.written = None
                raise CometError(tr('comet konnte nicht gestartet werden: {0}').format(exc)) from exc
            finally:
                os.close(fd)
            log.info("comet gestartet (PID %d) für %s: %s", self.proc.pid, product_id, shlex.join(argv))
            self.holders = {product_id}
            self.api, self.is_running = api, is_running
            self.pump = _LogPump(raw, out, [creds.access_token, creds.refresh_token], self.proc)
            self.pump.start()
            self._stop_evt = threading.Event()
            threading.Thread(target=self._watchdog, args=(self.proc, self._stop_evt), name="gl-comet-watch",
                             daemon=True).start()
            proc = self.proc
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                code = proc.returncode
                log.warning("comet hat sich sofort beendet (Exit-Code %s) – Details in %s", code, comet_log_file())
                with self._lock:
                    if self.proc is proc:
                        self._cleanup_locked()
                raise CometError(tr('comet hat sich sofort beendet (Exit-Code {0}) – Details im comet-Protokoll.').format(code))
            if port_in_use(self.port, self.host):
                return "started"
            time.sleep(0.1)
        log.info("comet lauscht nach %.0f s noch nicht auf Port %d", wait, self.port)
        return "started"

    def release(self, product_id: str) -> None:
        """Spiel beendet: abmelden; ohne weitere Spiele comet beenden (SIGTERM, nach 5 s SIGKILL) und aufräumen."""
        with self._lock:
            self.holders.discard(str(product_id))
            if self.proc is None:
                return
            self._prune_locked()
            if not self.holders:
                self._stop_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _prune_locked(self) -> None:
        if self.is_running is None:
            return
        keep = set()
        for pid in self.holders:
            try:
                if self.is_running(pid):
                    keep.add(pid)
            except Exception:
                keep.add(pid)
        self.holders = keep

    def _stop_locked(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            log.info("Beende comet (PID %d)", proc.pid)
            terminate_process(proc)
        if proc is not None:
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        """Nach comets Ende: erneuerte Tokens übernehmen, Zugangsdatei löschen, Protokoll abschließen."""
        if self.root is not None and self.written is not None:
            try:
                import_rotated(self.api, auth_file(self.root), self.written)
            except Exception as exc:
                log.warning("Tokens aus der comet-Datei nicht übernommen: %s", exc)
            remove_auth_file(self.root)
        self.written = None
        if self.pump is not None:
            self.pump.finish()
            self.pump = None
        if self.proc is not None:
            log.info("comet beendet (Exit-Code %s)", self.proc.returncode)
        self.proc = None
        self.holders = set()
        self._stop_evt.set()

    def _watchdog(self, proc: subprocess.Popen, stop: threading.Event) -> None:
        while not stop.wait(WATCH_INTERVAL):
            with self._lock:
                if self.proc is not proc:
                    return
                if proc.poll() is not None:
                    self._cleanup_locked()
                    return
                self._prune_locked()
                if not self.holders:
                    log.info("Kein laufendes Spiel nutzt comet mehr")
                    self._stop_locked()
                    return

    def shutdown(self) -> None:
        """Beim Beenden von galaxy-linux: comet stoppen, wenn kein Spiel mehr läuft; sonst weiterlaufen lassen
        (es endet mit ``--quit`` nach dem Spiel). Die Zugangsdatei hat comet beim Start gelesen – sie wird gelöscht."""
        with self._lock:
            if self.proc is None:
                return
            self._prune_locked()
            if not self.holders or self.proc.poll() is not None:
                self._stop_locked()
                return
            if self.root is not None and self.written is not None:
                try:
                    import_rotated(self.api, auth_file(self.root), self.written)
                except Exception:
                    pass
                remove_auth_file(self.root)
                self.written = None
            log.info("comet läuft für %s weiter, bis das Spiel beendet ist", ", ".join(sorted(self.holders)))


COMET = CometService()
atexit.register(COMET.shutdown)


# --------------------------------------------------------------------------- Platzhalterdienst im Prefix
def drive_c(prefix: Path, runner: str) -> Path:
    """``drive_c`` des Prefixes: Proton/umu legen ihn unter ``<prefix>/pfx`` an, Wine direkt (oder in ``pfx``,
    wenn vorhanden – wie ``Launcher.prefix_command``)."""
    prefix = Path(prefix)
    if runner in ("umu", "proton"):
        return prefix / "pfx" / "drive_c"
    pfx = prefix / "pfx"
    return (pfx if pfx.is_dir() else prefix) / "drive_c"


def windows_to_prefix_path(drive: Path, win_path: str) -> Path:
    """``C:\\a\\b`` → ``<drive_c>/a/b``."""
    rest = re.sub(r"^[A-Za-z]:[\\/]*", "", win_path)
    return Path(drive).joinpath(*[p for p in re.split(r"[\\/]+", rest) if p])


_SERVICE_KEY = re.compile(r"^\[system\\\\(?:currentcontrolset|controlset\d+)\\\\services\\\\galaxycommunication\]", re.I)
_CLIENT_KEY = "[software\\\\wow6432node\\\\gog.com\\\\galaxyclient\\\\paths]"


def registry_state(prefix_root: Path) -> tuple[bool, bool]:
    """(Galaxy-Pfad gesetzt, Dienst GalaxyCommunication angelegt) aus ``system.reg`` des Prefixes."""
    try:
        text = (Path(prefix_root) / "system.reg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, False
    client = service = False
    section = ""
    for line in text.splitlines():
        if line.startswith("["):
            section = line.split("]", 1)[0].lower() + "]"
            if _SERVICE_KEY.match(line):
                service = True
        elif section == _CLIENT_KEY and line.lower().startswith('"client"='):
            client = True
    return client, service


def service_steps(exe_present: bool, client_set: bool, service_registered: bool) -> list[tuple[str, list[str]]]:
    """Schritte für den Platzhalterdienst in Ausführungsreihenfolge: ``reg add`` zuerst (legt einen neuen Prefix
    dabei an), dann die Datei kopieren, dann ``sc create``. Bereits Vorhandenes wird übersprungen."""
    steps: list[tuple[str, list[str]]] = []
    if not client_set:
        steps.append(("reg", list(REG_ADD_CLIENT)))
    if not exe_present:
        steps.append(("copy", []))
    if not service_registered or not exe_present:
        steps.append(("sc", list(SC_CREATE_SERVICE)))
    return steps


def run_prefix_command(ctx: Any, argv: list[str], timeout: float = 600) -> int:
    """Windows-Befehl im Prefix des Spiels (Proton-Verb ``run`` wie Heroic), Ausgabe ins Spielprotokoll."""
    from .launcher import LaunchError
    try:
        cmd, env = ctx.launcher.prefix_command(ctx.product_id, argv, verb="run",
                                               extra_env={"PROTONFIXES_DISABLE": "1", "WINEDEBUG": "-all"})
    except LaunchError as exc:
        raise CometError(str(exc)) from exc
    with open(ctx.log_file, "ab") as lf:
        lf.write(f"--- Galaxy-Dienste: {shlex.join(cmd)}\n".encode())
        lf.flush()
        try:
            return subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            lf.write("--- Zeitüberschreitung\n".encode())
            return -1


def setup_dummy_service(ctx: Any, run: Callable[[Any, list[str]], int] | None = None) -> list[str]:
    """Platzhalterdienst im Prefix einrichten. Gibt die ausgeführten Schritte zurück ("reg", "copy", "sc")."""
    run = run or run_prefix_command
    dc = drive_c(ctx.prefix, ctx.runner)
    dest = windows_to_prefix_path(dc, DUMMY_WIN_PATH)
    client_set, registered = registry_state(dc.parent)
    steps = service_steps(dest.is_file(), client_set, registered)
    if not steps:
        return []
    dummy = ensure_dummy(ctx.settings, ctx.status) if any(k == "copy" for k, _ in steps) else None
    if dummy is None and any(k == "copy" for k, _ in steps):
        log.info("GalaxyCommunication-Platzhalter nicht verfügbar – Dienst wird nicht eingerichtet")
        return []
    ctx.status(tr('Richte Galaxy-Dienste im Prefix ein …'))
    done: list[str] = []
    for kind, argv in steps:
        if kind != "reg" and not dc.is_dir():
            # Prefix (noch) nicht angelegt – nichts selbst anlegen, sonst entstünde z. B. ein echter pfx-Ordner,
            # wo umu einen Verweis erwartet. Beim nächsten Start wird es erneut versucht.
            log.warning("Prefix ohne drive_c (%s) – GalaxyCommunication-Dienst wird nicht eingerichtet", dc)
            break
        if kind == "copy":
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.copyfile(dummy, tmp)
            os.replace(tmp, dest)
            log.info("GalaxyCommunication.exe in den Prefix kopiert: %s", dest)
        else:
            code = run(ctx, argv)
            log.info("Galaxy-Dienste: %s beendet mit %s", " ".join(argv[:2]), code)
        done.append(kind)
    return done


# --------------------------------------------------------------------------- Ablauf beim Spielstart
def wanted(ctx: Any) -> bool:
    """Galaxy-Dienste für diesen Start? Windows-GOG-Spiel, Schalter (Spiel vor global) an, bei GOG angemeldet."""
    from ..settings import resolve_switch
    if ctx.preview or not (ctx.is_windows and ctx.is_gog):
        return False
    if not resolve_switch(ctx.gs.galaxy_services, ctx.settings.feature_galaxy_services):
        return False
    return ctx.api is not None and bool(getattr(ctx.api, "logged_in", False))


def start_comet(ctx: Any, service: CometService | None = None) -> str:
    """comet für das Spiel starten (oder einen laufenden mitbenutzen). Rückgabe wie ``CometService.acquire``
    bzw. "missing", wenn comet fehlt."""
    service = service or COMET
    if service.running():
        state = service.acquire(ctx.product_id, "", None, "")       # läuft schon für ein anderes Spiel
        if state == "shared":
            ctx.data["comet"] = {"state": state}
            return state
    if not service.running() and port_in_use(service.port, service.host):
        log.info("Port %d ist belegt (z. B. comet von Heroic) – nutze den laufenden Dienst", service.port)
        return "foreign"
    tool = ensure_comet(ctx.settings, ctx.status)
    if tool is None:
        log.info("comet nicht gefunden – Spiel startet ohne Galaxy-Dienste")
        return "missing"
    username = account_name(ctx.api)
    creds = fresh_credentials(ctx.api)
    ctx.status(tr('Starte Galaxy-Dienste (comet) …'))
    is_running = getattr(ctx.launcher, "is_running", None)
    state = service.acquire(ctx.product_id, tool.path, creds, username, api=ctx.api, is_running=is_running)
    if state in ("started", "shared"):
        ctx.data["comet"] = {"state": state, "path": str(tool.path), "version": tool.version}
    log.info("Galaxy-Dienste für %s: %s (%s %s)", ctx.product_id, state, tool.path, tool.version or "?")
    return state


def status_info(comet_path: str = "", enabled: bool = True) -> dict[str, Any]:
    """Zustand für Einstellungen und CLI (darf ``comet --version`` ausführen – nicht im GTK-Hauptthread)."""
    tool = find_comet(comet_path)
    dummy = find_dummy()
    return {
        "comet": None if tool is None else {"path": str(tool.path), "source": tool.source, "version": tool.version,
                                            "outdated": tool.outdated},
        "dummy": str(dummy) if dummy else None,
        "wanted_version": COMET_VERSION,
        "license": COMET_LICENSE,
        "port_in_use": port_in_use(),
        "running": COMET.running(),
        "enabled": bool(enabled),
    }


# Anzeige-Helfer (Einstellungen → Kompatibilität → Galaxy-Dienste), ohne GTK
def source_label(source: str) -> str:
    return {"settings": tr('eigener Pfad'), "path": tr('Systempfad'), "heroic": tr('aus Heroic'),
            "download": tr('von galaxy-linux geladen')}.get(source, source)


def status_text(info: dict) -> str:
    c = info.get("comet")
    if c:
        text = tr('Gefunden: {0} · Version {1} · {2}').format(c["path"], c["version"] or "?", source_label(c["source"]))
        if c.get("outdated"):
            text += tr(' · Version {0} verfügbar').format(info["wanted_version"])
    elif info.get("enabled"):
        text = tr('Nicht gefunden – wird beim ersten Start eines Spiels mit Galaxy-Diensten heruntergeladen.')
    else:
        text = tr('Nicht gefunden')
    if info.get("running"):
        text += tr(' · läuft gerade')
    elif info.get("port_in_use"):
        text += tr(' · Port {0} belegt (z. B. comet von Heroic) – dieser Dienst wird mitbenutzt').format(COMET_PORT)
    return text


def download_label(info: dict) -> str:
    """Beschriftung des Download-Knopfs; leer = kein Knopf nötig."""
    c = info.get("comet")
    if c is None or not info.get("dummy"):
        return tr('comet herunterladen')
    if c.get("outdated"):
        return tr('Aktualisieren')
    return ""


def cli_command(backend: Any, a: Any) -> int:
    """``galaxy-linux comet [--download] [--json]``."""
    import sys
    changed = False
    if a.download:
        def progress(done: int, total: int) -> None:
            sys.stderr.write("\r" + tr('Lade comet herunter … {0} %').format(int(done * 100 / max(total, 1))))
            sys.stderr.flush()
        try:
            _tool, new_path = download_and_select(backend.settings.comet_path, progress=progress)
        except CometError as exc:
            sys.stderr.write("\n")
            print(tr('Fehler: {0}').format(exc), file=sys.stderr)
            return 1
        sys.stderr.write("\n")
        if new_path:
            backend.settings.comet_path = new_path
            backend.save_settings()
            changed = True
    info = status_info(backend.settings.comet_path, backend.settings.feature_galaxy_services)
    if a.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    c = info["comet"]
    if c:
        print(tr('comet: {0} (Version {1}, Quelle {2})').format(c["path"], c["version"] or "?", c["source"]))
        if c["outdated"]:
            print(tr('Neuere Version {0} verfügbar: galaxy-linux comet --download').format(COMET_VERSION))
    else:
        print(tr('comet: nicht gefunden – galaxy-linux comet --download'))
    print(tr('GalaxyCommunication-Dienst: {0}').format(info["dummy"] or tr('nicht gefunden')))
    if changed:
        print(tr('Neuer Pfad eingetragen: {0}').format(backend.settings.comet_path))
    if info["port_in_use"] and not info["running"]:
        print(tr('Port {0} ist belegt – ein anderer Galaxy-Dienst (z. B. von Heroic) läuft.').format(COMET_PORT))
    return 0 if c else 1
