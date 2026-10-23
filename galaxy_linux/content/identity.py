"""Identität externer Spiele: Kopien, Varianten und offizielle Namen (ab 0.3.1).

* **Kopien** erkennt der Inhalt, nicht der Name: Fingerabdruck eines Spielordners = Menge aus (relativer Pfad,
  Größe) aller Dateien, ohne Spielstände, Einstellungen und Protokolle (``*.sav``, ``*.ini``, ``*.cfg``, ``*.log``,
  ``*.bak``, Ordner ``save``/``saves``/``savegames``/``screenshots``). Stimmen zwei Ordner zu mindestens 98 % überein
  (Jaccard), ist es dieselbe Installation: Original ist der Ordner ohne Kopie-Kennzeichen im Namen („Kopie“, „Copy“,
  „(2)“, „_bck“, „bak“, „backup“, „Sicherung“), sonst der älteste; der andere bekommt ``duplicate_of``. Heißt ein
  Ordner „… - Kopie“, hat aber anderen Inhalt, ist er eine Variante. Fingerabdrücke werden pro Ordner zwischengespeichert
  (gültig, solange sich die Änderungszeiten des Ordners und seiner direkten Einträge nicht ändern).
* **Varianten**: Ordnername = Grundtitel + Zusatz (Auflösung „1360 x 768“, „HD“, „HD Edition“, „Mod“, „Modded“,
  „Widescreen“, „original“, „Fan-Patch“, „Remastered Mod“) → „<offizieller Titel> (<Zusatz>)“, außer der Zusatz gehört
  zu einem offiziellen Titel. Das Cover ist das des Grundspiels.
* **Offizieller Titel**, erster Treffer gilt: (1) eigener Titel (``GameSettings.custom_title``, nicht hier),
  (2) GOG-Spiel mit demselben Titel (installiert oder im Besitz) bzw. strenger Treffer im GOG-Katalog/Steam,
  (3) Versionsinfo der .exe (ProductName/FileDescription), (4) Wikidata (nur Videospiele), (5) bereinigter Ordnername.
  Ordner werden nie umbenannt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import external, peinfo
from ..api.covers import _tokens, official_prefix, remainder, title_matches

log = logging.getLogger(__name__)

IDENTITY_VERSION = 1
COPY_THRESHOLD = 0.98
FINGERPRINT_FILE = "fingerprints.json"
FINGERPRINT_VERSION = 1
MAX_FILES = 400_000
IGNORE_SUFFIXES = (".sav", ".ini", ".cfg", ".log", ".bak")
IGNORE_DIRS = frozenset({"save", "saves", "savegames", "screenshots"})

_MARK = r"kopie|copy|sicherungskopie|sicherung|backup|bak|bck"
COPY_TRAIL = re.compile(rf"(?:\s*[-–_.]\s*|\s+)({_MARK})(?:\s*\((\d{{1,2}})\)|\s+(\d{{1,2}}))?\s*$|\s*\((\d{{1,2}})\)\s*$",
                        re.I)
COPY_LEAD = re.compile(r"^\s*(kopie von|copy of)\s+", re.I)
_LABEL = r"\d{3,4}\s*[x×]\s*\d{3,4}|remastered\s+mod|fan[- ]?patch|hd\s+edition|widescreen|modded|mod|hd|original"
LABEL_TRAIL = re.compile(rf"(?:\s*[-–_]\s*|\s+|\s*[(\[]\s*)({_LABEL})\s*[)\]]?\s*$", re.I)


# --------------------------------------------------------------------------- Namen
def split_copy_marker(name: str) -> tuple[str, str]:
    """„Freelancer - Kopie“ → ("Freelancer", "Kopie"); „Spiel (2)“ → ("Spiel", "2"); ohne Kennzeichen → (name, "")."""
    base, marks = (name or "").strip(), []
    m = COPY_LEAD.match(base)
    if m:
        marks.append(m.group(1))
        base = base[m.end():]
    while True:
        m = COPY_TRAIL.search(base)
        if not m or m.start() == 0:
            break
        word = m.group(1) or ""
        num = m.group(2) or m.group(3) or m.group(4) or ""
        marks.insert(0, " ".join(x for x in (word, num) if x))
        base = base[:m.start()].rstrip()
    return (base or (name or "").strip()), " ".join(marks)


def has_copy_marker(name: str) -> bool:
    return bool(split_copy_marker(name)[1])


def split_variant(name: str) -> tuple[str, str]:
    """„Freelancer HD Edition“ → ("Freelancer", "HD Edition"); „EmperorDAdRdM 1360 x 768“ → ("EmperorDAdRdM",
    "1360 x 768"); ohne Zusatz → (name, "")."""
    base, labels = (name or "").strip(), []
    while True:
        m = LABEL_TRAIL.search(base)
        if not m:
            break
        rest = base[:m.start()].rstrip(" -–_([")
        if not _tokens(rest):
            break
        labels.insert(0, " ".join(m.group(1).split()))
        base = rest
    return base, " ".join(labels)


def name_parts(folder_name: str) -> tuple[str, str, str]:
    """(bereinigter Grundname, Varianten-Zusatz, Kopie-Kennzeichen) eines Ordnernamens."""
    base, marker = split_copy_marker(folder_name)
    base, label = split_variant(base)
    clean = external.display_title(base) or base
    return clean, label, marker


def variant_title(base: str, label: str) -> str:
    return f"{base} ({label})" if label else base


# --------------------------------------------------------------------------- Fingerabdrücke
@dataclass
class Fingerprint:
    hashes: frozenset[int] = frozenset()
    total: int = 0                         # Gesamtgröße der berücksichtigten Dateien

    @property
    def count(self) -> int:
        return len(self.hashes)


def _entry_hash(rel: str, size: int) -> int:
    h = hashlib.blake2b(f"{rel}\0{size}".encode("utf-8", "surrogateescape"), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def ignored_file(rel: str) -> bool:
    parts = rel.lower().split("/")
    return parts[-1].endswith(IGNORE_SUFFIXES) or any(p in IGNORE_DIRS for p in parts[:-1])


def fingerprint(root: str | Path) -> Fingerprint:
    """Fingerabdruck eines Spielordners (folgt keinen Symlinks)."""
    hashes: list[int] = []
    total = 0
    stack = [(str(root), "")]
    while stack and len(hashes) < MAX_FILES:
        cur, rel = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                r = f"{rel}/{e.name}" if rel else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in IGNORE_DIRS:
                            stack.append((e.path, r))
                        continue
                    if not e.is_file(follow_symlinks=False) or ignored_file(r):
                        continue
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                hashes.append(_entry_hash(r, size))
                total += size
    return Fingerprint(frozenset(hashes), total)


def folder_signature(root: str | Path) -> str:
    """Änderungsstand eines Ordners: mtime des Ordners und seiner direkten Einträge."""
    st = os.stat(root)
    parts = [str(FINGERPRINT_VERSION), str(st.st_mtime_ns)]
    with os.scandir(root) as it:
        for e in sorted(it, key=lambda x: x.name):
            try:
                s = e.stat(follow_symlinks=False)
                parts.append(f"{e.name}\0{s.st_mtime_ns}\0{s.st_size if e.is_file(follow_symlinks=False) else 0}")
            except OSError:
                parts.append(e.name)
    return hashlib.sha1("\n".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:20]


class FingerprintCache:
    """Fingerabdrücke je Ordner im Datenordner (``fingerprints.json``)."""

    def __init__(self, file: Path | None = None) -> None:
        if file is None:
            from .. import paths
            file = paths.DATA_DIR / FINGERPRINT_FILE
        self.file = Path(file)
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data: dict[str, dict] = data if isinstance(data, dict) else {}
        self.dirty = False

    def get(self, root: str | Path) -> Fingerprint | None:
        key = os.path.normpath(os.path.abspath(str(root)))
        try:
            sig = folder_signature(key)
        except OSError:
            return None
        hit = self.data.get(key)
        if isinstance(hit, dict) and hit.get("sig") == sig and isinstance(hit.get("h"), list):
            return Fingerprint(frozenset(int(x) for x in hit["h"]), int(hit.get("total", 0) or 0))
        fp = fingerprint(key)
        self.data[key] = {"sig": sig, "total": fp.total, "h": sorted(fp.hashes)}
        self.dirty = True
        return fp

    def prune(self, keep: Iterable[str | Path]) -> None:
        keys = {os.path.normpath(os.path.abspath(str(k))) for k in keep}
        for k in [k for k in self.data if k not in keys]:
            del self.data[k]
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.file)
        self.dirty = False


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class FolderInfo:
    pid: str
    path: Path
    installed_at: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name


def _age(f: FolderInfo) -> float:
    try:
        return os.stat(f.path).st_mtime
    except OSError:
        return float("inf")


def find_copies(folders: list[FolderInfo], prints: dict[str, Fingerprint | None],
                threshold: float = COPY_THRESHOLD) -> dict[str, str]:
    """Produkt-ID der Kopie → Produkt-ID des Originals. Verglichen werden nur Ordner mit ähnlich vielen Dateien."""
    ids = [f.pid for f in folders if prints.get(f.pid) is not None and prints[f.pid].count]
    parent = {pid: pid for pid in ids}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        pa = prints[a]
        for b in ids[i + 1:]:
            pb = prints[b]
            small, big = sorted((pa.count, pb.count))
            if small / big < threshold:
                continue
            if jaccard(pa.hashes, pb.hashes) >= threshold:
                parent[root(b)] = root(a)
    by_root: dict[str, list[FolderInfo]] = {}
    info = {f.pid: f for f in folders}
    for pid in ids:
        by_root.setdefault(root(pid), []).append(info[pid])
    out: dict[str, str] = {}
    for members in by_root.values():
        if len(members) < 2:
            continue
        canon = min(members, key=lambda f: (has_copy_marker(f.name), _age(f), f.installed_at, f.name.lower(), f.pid))
        for f in members:
            if f.pid != canon.pid:
                out[f.pid] = canon.pid
    return out


# --------------------------------------------------------------------------- Titel
@dataclass
class Identity:
    title: str                  # Anzeigetitel (mit Varianten-Zusatz)
    base_title: str             # offizieller Titel ohne Zusatz
    label: str = ""             # Varianten-Zusatz
    source: str = "folder"      # gog | catalog | pe | wikidata | folder
    gog_match: str = ""         # GOG-Produkt desselben Spiels (installiert oder im Besitz)
    cover: str = ""             # Cover-URL (Katalog, Steam, Wikipedia)
    wikidata: str = ""          # Q-ID
    complete: bool = True       # False: eine Quelle war nicht erreichbar → später erneut versuchen
    queries: list[str] = field(default_factory=list)


def match_gog(queries: list[str], gog_titles: Iterable[tuple[str, str]]) -> tuple[str, str, str] | None:
    """(Produkt-ID, Titel, Suchtitel) des ersten GOG-Spiels, dessen Titel einem Suchtitel entspricht."""
    titles = list(gog_titles)
    for q in queries:
        for pid, t in titles:
            if title_matches(q, t):
                return pid, t, q
    return None


Lookup = Callable[[list[str], str, list], "dict | None"]
WikiFind = Callable[[list[str], list], Any]


def resolve(folder_name: str, exe_path: str | Path | None = None, gog_titles: Iterable[tuple[str, str]] = (),
            lookup: Lookup | None = None, wiki: WikiFind | None = None, lang: str = "en",
            pe_info: dict[str, str] | None = None, copy_label: bool = False) -> Identity:
    """Offizieller Name eines externen Spielordners. ``lookup(queries, company, errors)`` = Katalog/Steam
    (``covers.lookup``), ``wiki(queries, errors)`` = ``wikidata.find_game`` – beide ohne Netz ``None``.
    ``copy_label``: das Kopie-Kennzeichen ist hier ein Varianten-Zusatz (Ordner „… - Kopie“ mit anderem Inhalt)."""
    gog_titles = list(gog_titles)
    errors: list = []
    clean, label, marker = name_parts(folder_name)
    if marker and copy_label:
        label = " ".join(x for x in (label, marker) if x)
    info = pe_info if pe_info is not None else (peinfo.version_strings(exe_path) if exe_path else {})
    pe = peinfo.game_name(info)
    company = (info.get("CompanyName") or "") if pe else ""

    def queries_for(text: str) -> list[str]:
        return [q for q in dict.fromkeys(x for x in (external.search_title(text) or text, pe) if x) if _tokens(q)]

    catalog = None
    if label:                                       # gehört der Zusatz zum offiziellen Titel („Age of X HD“)?
        full = f"{clean} {label}"
        hit = match_gog([full], gog_titles)
        if hit and _tokens(official_prefix(hit[1], full)) == _tokens(full):
            clean, label = full, ""
        elif lookup is not None:
            data = lookup([full], "", errors)
            if data and _tokens(data.get("base") or "") == _tokens(full):
                clean, label, catalog = full, "", data
    queries = queries_for(clean)
    ident = Identity(title=clean, base_title=clean, label=label, queries=queries)
    base = ""
    hit = match_gog(queries, gog_titles)
    if hit:
        base, ident.source, ident.gog_match = official_prefix(hit[1], hit[2]), "gog", hit[0]
    if not base and lookup is not None:
        catalog = catalog or lookup(queries, company, errors)
        if catalog:
            base = catalog.get("base") or official_prefix(catalog.get("title", ""), catalog.get("query", ""))
            ident.source, ident.cover = "catalog", catalog.get("cover", "") or ""
    if not base and pe:
        base, ident.source = pe, "pe"
    if wiki is not None and (not base or (not ident.cover and not ident.gog_match)):
        item = wiki([base] if base else queries, errors)
        if item is not None:
            ident.wikidata = getattr(item, "qid", "") or ""
            ident.cover = ident.cover or getattr(item, "image", "") or ""
            if not base:
                base, ident.source = item.label(lang), "wikidata"
    if not base:
        base, ident.source = clean, "folder"
    rest = remainder(clean, base) if ident.source != "folder" else ""
    ident.base_title = f"{base} {rest}" if rest else base
    ident.title = variant_title(ident.base_title, label)
    ident.complete = not errors
    return ident
