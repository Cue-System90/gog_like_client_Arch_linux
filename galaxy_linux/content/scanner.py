"""Erkennt Spiele, die von Hand in die Spiele-Ordner kopiert wurden (z. B. von einer Windows-Installation).

GOG-Spiele tragen in ihrem Ordner eine ``goggame-<ID>.info`` (JSON mit gameId, rootGameId, name,
playTasks …). DLCs haben eine eigene ``.info`` mit ``rootGameId`` = Basisspiel. Ordner ohne solche
Datei werden als „unbekannt“ gemeldet. Ein Ordner gilt erst als fertig kopiert, wenn sich Größe und
jüngste Änderungszeit eine Weile nicht mehr ändern.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

INFO_GLOB = "goggame-*.info"
SKIP_NAMES = {"prefixes", ".steam-client-stub", "__redist", "lost+found", "$RECYCLE.BIN", "System Volume Information"}


@dataclass
class FoundGame:
    product_id: str
    title: str
    platform: str                 # windows | linux
    path: Path
    build_id: str = ""
    language: str = ""
    dlc_ids: list[str] = field(default_factory=list)
    executable: str = ""          # nur Linux: start.sh
    info_file: Path | None = None


@dataclass
class ScanResult:
    found: list[FoundGame] = field(default_factory=list)       # neu erkannte Spiele
    known: list[str] = field(default_factory=list)              # bereits registrierte Produkt-IDs, die im Ordner liegen
    unknown: list[Path] = field(default_factory=list)          # Ordner ohne GOG-Spieldaten
    duplicates: list[Path] = field(default_factory=list)       # Spiel ist schon an anderem Ort registriert
    missing: list[str] = field(default_factory=list)            # registrierte Spiele, deren Ordner fehlt


def read_info(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _info_files(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.glob(INFO_GLOB) if p.is_file())
    except OSError:
        return []


def _build_id(d: Path, pid: str, info: dict) -> str:
    bid = str(info.get("buildId") or "")
    if bid:
        return bid
    id_file = d / f"goggame-{pid}.id"
    data = read_info(id_file) if id_file.exists() else None
    return str((data or {}).get("buildId") or "")


def _language(info: dict) -> str:
    langs = info.get("languages")
    if isinstance(langs, list) and langs:
        return str(langs[0])
    lang = info.get("language")
    return str(lang) if isinstance(lang, str) else ""


def _platform(d: Path, info: dict) -> tuple[str, str]:
    """windows oder linux (+ Startdatei für Linux)."""
    if (d / "start.sh").is_file():
        return "linux", "start.sh"
    os_bins = [str(x).lower() for x in info.get("osBins") or []]
    if os_bins and "windows" not in os_bins and "linux" in os_bins:
        return "linux", ""
    return "windows", ""


def game_in_dir(d: Path) -> FoundGame | None:
    """Liest das Basisspiel (und DLCs) aus einem Spielordner; sucht auch in ``game/`` (Linux-Layout)."""
    for sub in (d, d / "game"):
        infos = []
        for f in _info_files(sub):
            data = read_info(f)
            if data and data.get("gameId"):
                infos.append((f, data))
        if not infos:
            continue
        base = None
        for f, data in infos:
            gid, root = str(data.get("gameId")), str(data.get("rootGameId") or data.get("gameId"))
            if gid == root:
                base = (f, data)
                break
        if base is None:   # nur DLC-Dateien gefunden – deren rootGameId als Basisspiel nehmen
            f, data = next(((f, d2) for f, d2 in infos if d2.get("playTasks")), infos[0])
            base = (f, {**data, "gameId": data.get("rootGameId") or data.get("gameId")})
        f, data = base
        pid = str(data["gameId"])
        dlcs = sorted({str(d2.get("gameId")) for _, d2 in infos if str(d2.get("gameId")) != pid})
        platform, exe = _platform(d, data)
        return FoundGame(product_id=pid, title=str(data.get("name") or d.name), platform=platform, path=d,
                         build_id=_build_id(sub, pid, data), language=_language(data), dlc_ids=dlcs,
                         executable=exe, info_file=f)
    return None


def _is_relative_to(p: Path, other: Path) -> bool:
    try:
        p.resolve().relative_to(other.resolve())
        return True
    except (ValueError, OSError):
        return False


def scan(roots: Iterable[Path], registered: dict[str, Path], exclude: Iterable[Path] = ()) -> ScanResult:
    """Durchsucht die Basisordner (eine Ebene, bei Sammelordnern zwei Ebenen) nach Spielen.

    ``registered``: Produkt-ID → Installationsordner aller bekannten Spiele.
    """
    res = ScanResult()
    exclude = [Path(e) for e in exclude]
    reg_paths = {Path(p).resolve(): pid for pid, p in registered.items() if p}
    seen_dirs: set[Path] = set()
    roots = [Path(r) for r in roots]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".") and e.name not in SKIP_NAMES)
        except OSError as exc:
            log.warning("Spiele-Ordner %s nicht lesbar: %s", root, exc)
            continue
        for d in entries:
            if any(_is_relative_to(d, e) for e in exclude):
                continue
            rd = d.resolve()
            if rd in seen_dirs:
                continue
            seen_dirs.add(rd)
            if rd in reg_paths:
                res.known.append(reg_paths[rd])
                continue
            g = game_in_dir(d)
            if g is None:
                # Sammelordner (z. B. „GOG Games“ kopiert): eine Ebene tiefer suchen
                try:
                    subs = sorted(s for s in d.iterdir() if s.is_dir() and not s.name.startswith("."))
                except OSError:
                    subs = []
                inner = [x for x in (game_in_dir(s) for s in subs[:200]) if x]
                if not inner:
                    res.unknown.append(d)
                    continue
                for g2 in inner:
                    _classify(g2, registered, reg_paths, res)
                continue
            _classify(g, registered, reg_paths, res)
    # registrierte Spiele, deren Ordner in einem durchsuchten (vorhandenen, nicht leeren) Basisordner fehlt
    live_roots = [r for r in roots if r.is_dir() and _has_entries(r)]
    for pid, p in registered.items():
        path = Path(p)
        if path.exists():
            continue
        if any(_is_relative_to(path.parent, r) or path.parent == r for r in live_roots):
            res.missing.append(pid)
    return res


def _classify(g: FoundGame, registered: dict[str, Path], reg_paths: dict[Path, str], res: ScanResult) -> None:
    rp = g.path.resolve()
    if rp in reg_paths:
        res.known.append(reg_paths[rp])
    elif g.product_id in registered and Path(registered[g.product_id]).exists():
        res.duplicates.append(g.path)
    else:
        res.found.append(g)


def _has_entries(d: Path) -> bool:
    try:
        return any(True for _ in d.iterdir())
    except OSError:
        return False


def tree_signature(d: Path, limit: int = 400_000) -> tuple[int, float, int]:
    """(Gesamtgröße, jüngste mtime, Dateianzahl) – Grundlage für „Kopieren abgeschlossen?“."""
    size, newest, count = 0, 0.0, 0
    stack = [str(d)]
    while stack and count < limit:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
                    count += 1
        except OSError:
            continue
    return size, newest, count


def split_stable(games: list[FoundGame], wait: float = 4.0, quiet_for: float = 8.0) -> tuple[list[tuple[FoundGame, int]], list[FoundGame]]:
    """Trennt fertig kopierte von noch wachsenden Ordnern. Gibt (stabil+Größe, wird_kopiert) zurück."""
    if not games:
        return [], []
    first = {g.path: tree_signature(g.path) for g in games}
    time.sleep(wait)
    stable, copying = [], []
    now = time.time()
    for g in games:
        second = tree_signature(g.path)
        a = first[g.path]
        if second[:1] == a[:1] and second[2] == a[2] and now - second[1] >= quiet_for:
            stable.append((g, second[0]))
        else:
            copying.append(g)
    return stable, copying
