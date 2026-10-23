"""Angaben zum Client selbst: Projektseite, Änderungsprotokoll („Neu bei uns“), Suche nach neuen Versionen und
Selbst-Update wie Galaxy („Jetzt aktualisieren“/„Später“).

Die Update-Suche fragt die GitHub-API nach der neuesten Veröffentlichung (``releases/latest``); 404 heißt:
es gibt noch keine Veröffentlichung. Ist galaxy-linux als pacman-Paket installiert, lädt „Jetzt aktualisieren“ das
Paket ``galaxy-linux-<ver>-<rel>-any.pkg.tar.zst`` und ``SHA256SUMS`` der Veröffentlichung, prüft die Prüfsumme und
installiert mit ``pkexec pacman -U`` – das Passwort fragt der Polkit-Dialog des Systems ab, galaxy-linux sieht es nie.
Benutzerinstallationen (~/.local) und der Quellordner öffnen stattdessen die Veröffentlichung im Browser.
"""
from __future__ import annotations

from .i18n import tr

import hashlib
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import USER_AGENT, __version__

REPO = "Cue-System90/gog_like_client_Arch_linux"
REPO_URL = f"https://github.com/{REPO}"
ISSUES_URL = f"{REPO_URL}/issues"
RELEASES_URL = f"{REPO_URL}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"   # nur von hier werden Pakete geladen
PACKAGE_NAME = "galaxy-linux"
PKG_ASSET = re.compile(r"^galaxy-linux-(?P<ver>\d+(?:\.\d+)*)-(?P<rel>\d+)-any\.pkg\.tar\.zst$")
SUMS_ASSET = "SHA256SUMS"
CHECK_INTERVAL = 24 * 3600         # „Beim Start nach Client-Updates suchen“: höchstens einmal am Tag

PKG_DIR = Path(__file__).resolve().parent


class UpdateCheckError(Exception):
    pass


# --------------------------------------------------------------------------- Versionen
def parse_version(text: str) -> tuple[int, ...]:
    """``v0.2.0``, ``0.2.0-1`` oder ``galaxy-linux 0.2`` → (0, 2, 0) bzw. (0, 2); ohne Zahl → ()."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(remote: str, local: str) -> bool:
    r, l = parse_version(remote), parse_version(local)
    if not r:
        return False
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


@dataclass
class Release:
    """Eine Veröffentlichung auf GitHub (``releases/latest``)."""
    version: str
    tag: str = ""
    url: str = RELEASES_URL
    notes: str = ""                                              # Versionshinweise (Markdown)
    assets: dict[str, str] = field(default_factory=dict)         # Dateiname → Download-Adresse
    published: str = ""


@dataclass
class UpdateCheck:
    state: str          # "newer" | "current" | "none" (noch keine Veröffentlichung)
    latest: str = ""
    url: str = RELEASES_URL
    release: Release | None = None


def release_from_api(d: dict) -> Release:
    tag = str(d.get("tag_name") or d.get("name") or "")
    assets = {}
    for a in d.get("assets") or []:
        if isinstance(a, dict) and a.get("name") and a.get("browser_download_url"):
            assets[str(a["name"])] = str(a["browser_download_url"])
    return Release(".".join(str(x) for x in parse_version(tag)) or tag, tag, str(d.get("html_url") or RELEASES_URL),
                   str(d.get("body") or ""), assets, str(d.get("published_at") or ""))


def check_for_update(current: str = __version__, session=None, timeout: float = 15) -> UpdateCheck:
    import requests
    http = session or requests
    try:
        r = http.get(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
                     timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateCheckError(tr('GitHub nicht erreichbar: {0}').format(exc)) from exc
    if r.status_code == 404:
        return UpdateCheck("none")
    if not r.ok:
        raise UpdateCheckError(tr('GitHub antwortet mit HTTP {0}').format(r.status_code))
    rel = release_from_api(r.json() or {})
    return UpdateCheck("newer" if is_newer(rel.tag, current) else "current", rel.version, rel.url, rel)


def should_check(last: float, now: float, interval: float = CHECK_INTERVAL) -> bool:
    """Automatische Suche beim Start fällig? (nie gesucht, ein Tag vorbei oder die Uhr ging zurück)"""
    return not last or now - last >= interval or now < last


# --------------------------------------------------------------------------- Selbst-Update
def package_asset(release: Release) -> tuple[str, str] | None:
    """(Dateiname, Adresse) des pacman-Pakets dieser Version (höchstes pkgrel) – nur Adressen dieses Repositorys."""
    best: tuple[int, str, str] | None = None
    want = parse_version(release.version)
    for name, url in release.assets.items():
        m = PKG_ASSET.match(name)
        if not m or not url.startswith(DOWNLOAD_PREFIX) or parse_version(m.group("ver")) != want:
            continue
        rel = int(m.group("rel"))
        if best is None or rel > best[0]:
            best = (rel, name, url)
    return (best[1], best[2]) if best else None


def sums_asset(release: Release) -> tuple[str, str] | None:
    url = release.assets.get(SUMS_ASSET, "")
    return (SUMS_ASSET, url) if url.startswith(DOWNLOAD_PREFIX) else None


def parse_sha256sums(text: str) -> dict[str, str]:
    """``sha256sum``-Ausgabe → {Dateiname: Prüfsumme} (auch ``*Datei`` im Binärmodus)."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def update_method(pkg_dir: Path = PKG_DIR, run: Callable = subprocess.run) -> str:
    """Wie läuft galaxy-linux? ``pacman`` (Paket unter /usr, ``pacman -Qq galaxy-linux`` kennt es), ``source``
    (Quellordner mit pyproject.toml/.git) oder ``user`` (z. B. ``install.sh --user`` nach ~/.local)."""
    root = Path(pkg_dir).parent
    if (root / "pyproject.toml").is_file() or (root / ".git").exists():
        return "source"
    if str(pkg_dir).startswith("/usr/"):
        try:
            rc = run(["pacman", "-Qq", PACKAGE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, timeout=15).returncode
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc == 0:
            return "pacman"
    return "user"


def update_action(method: str, release: Release | None) -> str:
    """„Jetzt aktualisieren“: ``install`` (pacman-Paket mit Paket und SHA256SUMS in der Veröffentlichung) oder
    ``open`` (Veröffentlichung im Browser öffnen)."""
    if method == "pacman" and release is not None and package_asset(release) and sums_asset(release):
        return "install"
    return "open"


def _get(http, url: str, timeout: float, stream: bool = False):
    import requests
    try:
        r = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=stream)
    except requests.RequestException as exc:
        raise UpdateCheckError(tr('GitHub nicht erreichbar: {0}').format(exc)) from exc
    if not r.ok:
        raise UpdateCheckError(tr('GitHub antwortet mit HTTP {0}').format(r.status_code))
    return r


def download_update(release: Release, dest_dir: Path, session=None, progress: Callable[[int, int], None] | None = None,
                    cancel: threading.Event | None = None, timeout: float = 30) -> Path:
    """Paket der Veröffentlichung nach ``dest_dir`` laden und gegen ``SHA256SUMS`` prüfen. Rückgabe: Pfad des
    geprüften Pakets. Stimmt die Prüfsumme nicht, wird die Datei verworfen (``UpdateCheckError``)."""
    import requests
    http = session or requests
    pkg, sums = package_asset(release), sums_asset(release)
    if pkg is None:
        raise UpdateCheckError(tr('Die Veröffentlichung enthält kein pacman-Paket für {0}.').format(release.version))
    if sums is None:
        raise UpdateCheckError(tr('Die Veröffentlichung enthält keine Datei SHA256SUMS.'))
    name, url = pkg
    expected = parse_sha256sums(_get(http, sums[1], timeout).text).get(name)
    if not expected:
        raise UpdateCheckError(tr('SHA256SUMS enthält keine Prüfsumme für {0}.').format(name))
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("galaxy-linux-*.pkg.tar.zst*"):        # ältere Downloads aufräumen
        old.unlink(missing_ok=True)
    target = dest_dir / name
    part = dest_dir / (name + ".part")
    h = hashlib.sha256()
    r = _get(http, url, timeout, stream=True)
    try:
        total = int(r.headers.get("Content-Length") or 0) if hasattr(r, "headers") else 0
        done = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if cancel is not None and cancel.is_set():
                    raise UpdateCheckError(tr('Download abgebrochen.'))
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        raise UpdateCheckError(tr('Download fehlgeschlagen: {0}').format(exc)) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(r, "close", None)
        if close:
            close()
    if h.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise UpdateCheckError(tr('Die Prüfsumme des Pakets stimmt nicht – der Download wurde verworfen.'))
    part.replace(target)
    return target


def install_command(path: Path | str) -> list[str]:
    """Paket installieren; das Passwort fragt der Polkit-Dialog des Systems (pkexec), nie galaxy-linux."""
    return ["pkexec", "pacman", "-U", "--noconfirm", str(path)]


@dataclass
class InstallResult:
    ok: bool
    code: int
    message: str
    output: str = ""


def install_package(path: Path | str, run: Callable = subprocess.run, timeout: float = 900) -> InstallResult:
    try:
        p = run(install_command(path), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        return InstallResult(False, -2, tr('pkexec fehlt – bitte das Paket polkit installieren.'))
    except subprocess.TimeoutExpired:
        return InstallResult(False, -1, tr('Die Installation hat zu lange gedauert und wurde abgebrochen.'))
    except OSError as exc:
        return InstallResult(False, -2, str(exc))
    out = ((p.stdout or "") + (p.stderr or "")).strip()[-4000:]
    if p.returncode == 0:
        return InstallResult(True, 0, tr('Das Update ist installiert. Starte galaxy-linux neu, um die neue Version zu '
                                         'verwenden.'), out)
    if p.returncode == 126:
        return InstallResult(False, 126, tr('Abgebrochen – die Abfrage des Systems wurde nicht bestätigt.'), out)
    if p.returncode == 127:
        return InstallResult(False, 127, tr('Nicht berechtigt – das System hat die Installation abgelehnt.'), out)
    return InstallResult(False, p.returncode, tr('pacman meldet einen Fehler (Exit-Code {0}).').format(p.returncode), out)


def restart_command(which: Callable = shutil.which, executable: str = sys.executable) -> list[str]:
    """Befehl für „Neu starten“ nach dem Update (das installierte Startprogramm, sonst ``python -m galaxy_linux``)."""
    exe = which(PACKAGE_NAME)
    return [exe] if exe else [executable, "-m", "galaxy_linux"]


# --------------------------------------------------------------------------- Änderungsprotokoll
def changelog_candidates() -> list[Path]:
    """Paketkopie (PKGBUILD/--user), Quellordner, Dokumentation des Pakets."""
    return [PKG_DIR / "CHANGELOG.md", PKG_DIR.parent / "CHANGELOG.md", Path("/usr/share/doc/galaxy-linux/CHANGELOG.md")]


def changelog_text() -> str:
    for p in changelog_candidates():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


_HEAD = re.compile(r"^##\s+\[?v?(\d+(?:\.\d+)*)\]?", re.M)


def changelog_section(text: str, version: str) -> tuple[str, str]:
    """(Abschnitt ``## <version> …`` samt Überschrift, alle älteren Abschnitte). Fehlt die Version: ("", text)."""
    heads = list(_HEAD.finditer(text or ""))
    want = parse_version(version)
    for i, m in enumerate(heads):
        if parse_version(m.group(1)) == want:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.start():end].strip(), text[end:].strip()
    return "", (text or "").strip()


def markdown_to_plain(md: str) -> str:
    """Einfaches Markdown (Überschriften, Listen, Hervorhebungen) als lesbarer Text; umbrochene Zeilen eines
    Absatzes oder Listenpunkts werden zusammengefügt, damit der Dialog selbst umbricht."""
    out: list[str] = []
    joinable = False
    for line in (md or "").splitlines():
        s = line.strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            joinable = False
            continue
        heading = s.startswith("#")
        bullet = re.match(r"^[-*]\s+", s) is not None
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*]\s+", "• ", s)
        s = s.replace("**", "").replace("`", "")
        if joinable and not heading and not bullet:
            out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
        joinable = not heading
    return "\n".join(out).strip()
