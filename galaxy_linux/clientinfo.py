"""Angaben zum Client selbst: Projektseite, Änderungsprotokoll („Neu bei uns“) und Suche nach neuen Versionen.

Die Update-Suche fragt die GitHub-API nach der neuesten Veröffentlichung (``releases/latest``); 404 heißt:
es gibt noch keine Veröffentlichung.
"""
from __future__ import annotations

from .i18n import tr

import re
from dataclasses import dataclass
from pathlib import Path

from . import USER_AGENT, __version__

REPO = "Cue-System90/gog_like_client_Arch_linux"
REPO_URL = f"https://github.com/{REPO}"
ISSUES_URL = f"{REPO_URL}/issues"
RELEASES_URL = f"{REPO_URL}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"

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
class UpdateCheck:
    state: str          # "newer" | "current" | "none" (noch keine Veröffentlichung)
    latest: str = ""
    url: str = RELEASES_URL


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
    d = r.json() or {}
    tag = str(d.get("tag_name") or d.get("name") or "")
    latest = ".".join(str(x) for x in parse_version(tag)) or tag
    url = str(d.get("html_url") or RELEASES_URL)
    return UpdateCheck("newer" if is_newer(tag, current) else "current", latest, url)


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
