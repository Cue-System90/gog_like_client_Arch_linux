"""Externe Spiele (nicht von GOG): beliebige Windows-Spielordner in den eigenen Bibliotheken.

Jeder Unterordner des Spiele-Ordners einer Bibliothek ist ein Spiel. Die Startdatei wird per Bewertung
gefunden (Name passt zum Titel, übliche Spielordner wie EXE/, Binaries/Win64/, System64/; Deinstaller,
Laufzeitpakete, Editoren, Server und Crash-Reporter fallen heraus). In den Eigenschaften lässt sie sich
jederzeit umstellen.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ID_PREFIX = "ext-"
MAX_DEPTH = 4

SKIP_DIRS = re.compile(r"^(_?redist|_commonredist|commonredist|directx|dotnet|vcredist|support|install(er)?|setup|"
                       r"__installer|manuals?|docs?|acrobat|tools?|crashreport\w*|eac|easyanticheat|battleye|"
                       r"\$pluginsdir|redistributables?|prerequisites?|_?installer)$", re.I)
SKIP_EXE = re.compile(r"(unins\d*|uninst|setup|install|redist|vc_?redist|dxsetup|dxwebsetup|directx|dotnet|physx|"
                      r"crash|report|bugsplat|bssndrpt|quicksfv|sfv|wget|gamespy|acrobat|ar\d{3}|navbrowser|"
                      r"editor|edit$|ued|sdk|server|serv$|dedicated|tool|convert|config|settings|patch|update|"
                      r"helper|launcher_helper|cefprocess|webhelper|dw15|ucc|unitycrashhandler|vulkan|^ue\d)", re.I)
GOOD_DIRS = ("exe", "bin", "bin64", "binaries", "win64", "x64", "system64", "system", "game", "program")
RESOLUTION = re.compile(r"\s*\b\d{3,4}\s*[x×]\s*\d{3,4}\b\s*", re.I)
SEARCH_NOISE = re.compile(r"\b(complete|goty|game of the year|definitive|deluxe|gold|ultimate|enhanced|hd|"
                          r"remastere?d|edition|collection|anniversary|director'?s cut|kopie|original|copy)\b", re.I)


@dataclass
class ExeCandidate:
    rel: str
    score: float
    size: int


@dataclass
class FoundExternal:
    product_id: str
    title: str
    path: Path
    executable: str               # relativer Pfad der gewählten Startdatei
    candidates: list[str] = field(default_factory=list)
    library: str = ""             # ID der eigenen Bibliothek, in deren Ordner es liegt


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "spiel"


def external_id(folder_name: str) -> str:
    return ID_PREFIX + slug(folder_name)


def is_external_id(pid: str) -> bool:
    return str(pid).startswith(ID_PREFIX)


def display_title(folder_name: str) -> str:
    t = RESOLUTION.sub(" ", folder_name)
    return re.sub(r"\s{2,}", " ", t).strip(" -_") or folder_name


def search_title(title: str) -> str:
    t = display_title(title)
    t = re.sub(r"\s+-\s+(kopie|copy|original)$", "", t, flags=re.I)
    t = SEARCH_NOISE.sub(" ", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -_:")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def _acronym(title: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", title) if w and w.lower() not in ("the", "of", "a", "an", "der", "die", "das")]
    return "".join(w[0] for w in words).lower()


def _acronyms(title: str) -> list[str]:
    """Kürzel des ganzen Titels und jedes Teils ("The Operative - No One Lives Forever" → onolf, o, nolf)."""
    parts = [title, *re.split(r"\s+[-–:]\s+|:\s*", title)]
    return [a for a in dict.fromkeys(_acronym(x) for x in parts) if a]


def score_exe(rel: str, size: int, title: str) -> float:
    p = Path(rel)
    stem = _norm(p.stem)
    t = _norm(search_title(title))
    parts = [x.lower() for x in p.parts[:-1]]
    score = 0.0
    if stem and t:
        if stem == t:
            score += 60
        elif t.startswith(stem) or stem.startswith(t):
            score += 45
        elif len(stem) >= 4 and stem in t:
            score += 35
        elif len(t) >= 4 and t in stem:
            score += 35
        elif len(stem) >= 2 and any(a.startswith(stem.rstrip("0123456789")) for a in _acronyms(search_title(title))):
            score += 30
        elif any(len(w) >= 4 and _norm(w) in stem for w in re.split(r"\s+", search_title(title))):
            score += 20
    score -= 6 * len(parts)                      # tiefer = unwahrscheinlicher …
    score += sum(8 for x in parts if x in GOOD_DIRS)  # … außer in typischen Spielordnern
    if "system64" in parts or "win64" in parts or "x64" in parts or "bin64" in parts:
        score += 3                               # 64-Bit bevorzugen
    if "shipping" in stem:
        score += 10
    if stem.endswith("avx") or stem.endswith("dx12") or stem.endswith("vulkan"):
        score -= 4                               # Spezialbuilds nur nachrangig
    score += min(size / (8 << 20), 1.0) * 6      # größere EXE leicht bevorzugen
    if size < 64 << 10:
        score -= 15                              # winzige Starter/Stubs
    return score


def exe_candidates(d: Path, title: str, limit: int = 12) -> list[ExeCandidate]:
    out: list[ExeCandidate] = []
    stack: list[tuple[Path, int]] = [(d, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir() and not e.is_symlink():
                    if depth + 1 < MAX_DEPTH and not SKIP_DIRS.match(e.name) and not e.name.startswith("."):
                        stack.append((e, depth + 1))
                    continue
                if e.suffix.lower() != ".exe" or SKIP_EXE.search(e.stem):
                    continue
                size = e.stat().st_size
            except OSError:
                continue
            rel = str(e.relative_to(d))
            out.append(ExeCandidate(rel, score_exe(rel, size, title), size))
    out.sort(key=lambda c: (-c.score, len(c.rel)))
    return out[:limit]


def game_in_dir(d: Path) -> FoundExternal | None:
    title = display_title(d.name)
    cands = exe_candidates(d, title)
    if not cands:
        return None
    return FoundExternal(product_id=external_id(d.name), title=title, path=d, executable=cands[0].rel,
                         candidates=[c.rel for c in cands])
