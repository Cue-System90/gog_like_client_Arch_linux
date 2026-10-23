"""Bibliotheksansicht wie in GOG Galaxy 2.1 – ohne GTK: Sortieren, Gruppieren, Filtern, Tags, Bewertung, Lesezeichen.

* ``facts(backend, entry)`` sammelt alles, was Sortierung, Gruppierung und Filter brauchen (Status, Plattform, Tags,
  Bewertung, Spielzeit, Genres und Presse-Wertung aus dem GamesDB-Cache – ohne Netzwerk).
* ``sort_facts``/``group_facts`` ordnen eine Liste; Kriterien wie im Original (``LibrarySortingId``/``LibraryGroupingId``).
* ``LibraryFilter``: innerhalb einer Kategorie ODER, zwischen den Kategorien UND (wie Galaxys Filter-Knopf).
* Lesezeichen (``Bookmark``) legen die aktuelle Ansicht mit Filter, Suche, Sortierung und Gruppierung oder eine
  Spielansicht in die Seitenleiste; Import/Export als JSON.
"""
from __future__ import annotations

from .i18n import N_, tr

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .jsonstore import read_json

SORT_KEYS = ("title", "rating", "playtime", "critics", "release", "last_played", "tags", "platform", "genres", "size",
             "added")
SORT_NAMES = {"title": N_("Titel"), "rating": N_("Bewertung"), "playtime": N_("Spielzeit"),
              "critics": N_("Presse-Wertung"), "release": N_("Veröffentlichungsdatum"),
              "last_played": N_("Zuletzt gespielt"), "tags": N_("Tags"), "platform": N_("Plattform"),
              "genres": N_("Genres"), "size": N_("Größe auf der Festplatte"), "added": N_("Datum hinzugefügt")}
LEGACY_SORT = {0: "title", 1: "last_played", 2: "playtime", 3: "release"}
# ohne Angabe: absteigend sinnvoll (meiste Spielzeit, zuletzt gespielt, beste Bewertung …)
DESC_BY_DEFAULT = {"rating", "playtime", "critics", "release", "last_played", "size", "added"}

GROUP_KEYS = ("none", "tags", "platform", "genre", "rating", "release", "installed", "critics", "added")
GROUP_NAMES = {"none": N_("Nicht gruppieren"), "tags": N_("Tags"), "platform": N_("Plattform"),
               "genre": N_("Genre"), "rating": N_("Bewertung"), "release": N_("Veröffentlichungsdatum"),
               "installed": N_("Status"), "critics": N_("Presse-Wertung"), "added": N_("Datum hinzugefügt")}

STATUS_KEYS = ("installed", "not_installed", "update")
STATUS_NAMES = {"installed": N_("Installiert"), "not_installed": N_("Nicht installiert"), "update": N_("Update verfügbar")}
OS_NAMES = {"windows": "Windows", "linux": "Linux", "osx": "macOS"}
INSTALLED_STATES = ("installed", "update", "running", "busy")
MAX_RATING = 5


def stars(n: int) -> str:
    return "★" * max(0, min(MAX_RATING, int(n))) if n else tr("Keine Bewertung")


# --------------------------------------------------------------------------- Fakten je Spiel
@dataclass
class GameFacts:
    id: str
    title: str
    state: str = "not_installed"        # Backend.state()
    os: list[str] = field(default_factory=list)
    source: str = "gog"                 # gog | lib:<ID>
    source_name: str = "GOG.com"
    tags: list[str] = field(default_factory=list)
    rating: int = 0
    favorite: bool = False
    hidden: bool = False
    genres: list[str] = field(default_factory=list)
    critics: float | None = None        # 0–100
    release_date: str = ""              # JJJJ-MM-TT (kann kürzer sein)
    playtime: int = 0                   # Minuten
    last_played: float = 0.0
    size: int = 0
    added: float = 0.0
    developers: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.state in INSTALLED_STATES


_GAMESDB_CACHE: dict[str, tuple[float, dict]] = {}


def cached_gamesdb(product_id: str) -> dict:
    """GamesDB-Antwort aus dem API-Cache (``gamesdb-<id>.json``) ohne Netzwerk – das Laden der Cover füllt ihn."""
    f = paths.API_CACHE_DIR / f"gamesdb-{product_id}.json"
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    hit = _GAMESDB_CACHE.get(str(product_id))
    if hit and hit[0] == mtime:
        return hit[1]
    data = read_json(f, None)
    data = data if isinstance(data, dict) else {}
    _GAMESDB_CACHE[str(product_id)] = (mtime, data)
    return data


def _localized(v: Any) -> str:
    from .api.models import _localized as loc
    from . import i18n
    return loc(v, "de-DE" if i18n.current() == "de" else "en-US")


def gamesdb_facts(product_id: str) -> tuple[list[str], float | None, str, list[str]]:
    """Genres, Presse-Wertung, Erscheinungsdatum und Entwickler aus dem GamesDB-Cache."""
    d = cached_gamesdb(product_id)
    g = d.get("game") or {}
    genres = [x for x in (_localized(x.get("name")) for x in (g.get("genres") or []) if isinstance(x, dict)) if x]
    critics = None
    try:
        if g.get("aggregated_rating") is not None:
            critics = float(g["aggregated_rating"])
    except (TypeError, ValueError):
        critics = None
    release = str(d.get("first_release_date") or g.get("first_release_date") or "")[:10]
    devs = [str(x.get("name", "")) for x in (g.get("developers") or []) if isinstance(x, dict) and x.get("name")]
    return genres, critics, release, devs


def facts(backend: Any, entry: Any, state: str | None = None) -> GameFacts:
    pid = str(entry.id)
    gs = backend.game_settings.get(pid)
    inst = backend.registry.get(pid)
    lib = backend.library_of(pid) if inst is not None and inst.method == "external" else None
    minutes, last = backend.playtime.get(pid)
    genres, critics, release, devs = gamesdb_facts(pid) if not pid.startswith("ext-") else ([], None, "", [])
    meta = getattr(backend, "_meta", {}).get(pid)
    if meta is not None and not genres:
        genres, devs = list(meta.genres), list(meta.developers)
        critics = critics if critics is not None else meta.rating
    added = float((getattr(backend.settings, "library_new_ids", {}) or {}).get(pid, 0) or 0)
    if not added and inst is not None and inst.method == "external":
        added = float(inst.installed_at or 0)
    os_list = list(entry.platforms) if getattr(entry, "works_on", None) else ([inst.platform] if inst else [])
    return GameFacts(
        id=pid, title=entry.title, state=state or backend.state(pid), os=os_list,
        source=f"lib:{lib.id}" if lib is not None else "gog", source_name=lib.name if lib is not None else "GOG.com",
        tags=list(gs.tags), rating=int(gs.rating or 0), favorite=gs.favorite,
        hidden=is_hidden(entry, gs), genres=genres, critics=critics,
        release_date=release or str(getattr(entry, "release_date", "") or "")[:10],
        playtime=minutes, last_played=last, size=inst.install_size if inst else 0, added=added, developers=devs)


def is_hidden(entry: Any, gs: Any) -> bool:
    """Ausgeblendet: hier ausgeblendet oder bei GOG (dort nur wieder einblendbar, solange „unhidden“ gilt)."""
    return bool(gs.hidden or (getattr(entry, "is_hidden", False) and not gs.unhidden))


# --------------------------------------------------------------------------- Sortieren
def resolve_sort(view: dict) -> tuple[str, bool]:
    """Sortierkriterium und Richtung einer gespeicherten Ansicht (auch Werte aus 0.2.0)."""
    key = str(view.get("sort_by") or "")
    if key not in SORT_KEYS:
        try:
            key = LEGACY_SORT.get(int(view.get("sort", 0) or 0), "title")
        except (TypeError, ValueError):
            key = "title"
        desc = key in DESC_BY_DEFAULT
    else:
        desc = bool(view.get("sort_desc", False))
    return key, desc


def _title_key(f: GameFacts) -> tuple[str, str]:
    """Wie Galaxys Sortiertitel: führender Artikel zählt nicht („The Witcher“ unter W)."""
    t = f.title.lower()
    for art in ("the ", "a "):
        if t.startswith(art) and len(t) > len(art):
            return t[len(art):], t
    return t, t


def _sort_value(f: GameFacts, key: str) -> Any:
    if key == "rating":
        return f.rating or None
    if key == "playtime":
        return f.playtime or None
    if key == "critics":
        return f.critics
    if key == "release":
        return f.release_date or None
    if key == "last_played":
        return f.last_played or None
    if key == "tags":
        return ", ".join(sorted(t.lower() for t in f.tags)) or None
    if key == "platform":
        return f.source_name.lower()
    if key == "genres":
        return ", ".join(sorted(g.lower() for g in f.genres)) or None
    if key == "size":
        return f.size or None
    if key == "added":
        return f.added or None
    return _title_key(f)


def sort_facts(items: Iterable[GameFacts], key: str = "title", desc: bool = False) -> list[GameFacts]:
    """Sortieren wie Galaxy; Einträge ohne Wert (keine Bewertung, nie gespielt …) stehen immer am Ende,
    bei gleichem Wert entscheidet der Titel."""
    items = sorted(items, key=_title_key)
    if key == "title":
        return list(reversed(items)) if desc else items
    have = [f for f in items if _sort_value(f, key) is not None]
    none = [f for f in items if _sort_value(f, key) is None]
    have.sort(key=lambda f: _sort_value(f, key), reverse=desc)
    return have + none


# --------------------------------------------------------------------------- Gruppieren
def _release_group(date: str, now: float) -> tuple[int, str]:
    if not date:
        return 9999, tr("Unbekanntes Veröffentlichungsdatum")
    try:
        t = time.mktime(time.strptime(date[:10], "%Y-%m-%d")) if len(date) >= 10 else \
            time.mktime(time.strptime(date[:4] + "-01-01", "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return 9999, tr("Unbekanntes Veröffentlichungsdatum")
    today = time.localtime(now)
    if t > now + 86400:
        return 0, tr("Demnächst")
    if time.localtime(t)[:3] == today[:3] or now - t < 86400:
        return 1, tr("Heute")
    if now - t < 30 * 86400:
        return 2, tr("Letzten 30 Tage")
    year = time.localtime(t).tm_year
    if year == today.tm_year:
        return 3, tr("Dieses Jahr veröffentlicht")
    return 10 + (today.tm_year - year), str(year)


def _added_group(ts: float, now: float) -> tuple[int, str]:
    if not ts:
        return 9999, tr("Datum unbekannt")
    if now - ts < 86400:
        return 1, tr("Heute")
    if now - ts < 30 * 86400:
        return 2, tr("Letzten 30 Tage")
    year = time.localtime(ts).tm_year
    if year == time.localtime(now).tm_year:
        return 3, tr("Dieses Jahr")
    return 10 + (time.localtime(now).tm_year - year), str(year)


def _critics_group(v: float | None) -> tuple[int, str]:
    if v is None:
        return 99, tr("Keine Bewertung")
    lo = min(90, int(v) // 10 * 10)
    return 10 - lo // 10, f"{lo}–{100 if lo == 90 else lo + 9}"


def group_facts(items: list[GameFacts], key: str = "none", now: float | None = None) -> list[tuple[str, list[GameFacts]]]:
    """Gruppen in der Reihenfolge von Galaxy; ein Spiel mit mehreren Tags/Genres erscheint in jeder seiner Gruppen.
    Die Reihenfolge innerhalb einer Gruppe bleibt die der Eingabe (also die Sortierung)."""
    now = time.time() if now is None else now
    if key not in GROUP_KEYS or key == "none":
        return [("", list(items))]
    groups: dict[str, tuple[Any, list[GameFacts]]] = {}

    def put(order: Any, name: str, f: GameFacts) -> None:
        groups.setdefault(name, (order, []))[1].append(f)

    for f in items:
        if key == "tags":
            for t in f.tags or [""]:
                put((1, "") if not t else (0, t.lower()), t or tr("Keine Tags"), f)
        elif key == "genre":
            for g in f.genres or [""]:
                put((1, "") if not g else (0, g.lower()), g or tr("Kein Genre"), f)
        elif key == "platform":
            put((0 if f.source == "gog" else 1, f.source_name.lower()), f.source_name, f)
        elif key == "rating":
            put(-f.rating if f.rating else 1, stars(f.rating), f)
        elif key == "release":
            put(*_release_group(f.release_date, now), f)
        elif key == "added":
            put(*_added_group(f.added, now), f)
        elif key == "critics":
            put(*_critics_group(f.critics), f)
        elif key == "installed":
            put(0 if f.installed else 1, tr("Installiert") if f.installed else tr("Nicht installiert"), f)
    return [(name, lst) for name, (_o, lst) in sorted(groups.items(), key=lambda kv: (kv[1][0], kv[0].lower()))]


# --------------------------------------------------------------------------- Filter
@dataclass
class LibraryFilter:
    status: list[str] = field(default_factory=list)     # installed | not_installed | update
    os: list[str] = field(default_factory=list)         # windows | linux | osx
    sources: list[str] = field(default_factory=list)    # gog | lib:<ID>
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)       # "" = Keine Tags
    ratings: list[int] = field(default_factory=list)    # 0 = Keine Bewertung
    favorites: bool = False
    hidden: str = ""                                    # "" = wie „Versteckte Spiele anzeigen“, hidden, visible

    def is_empty(self) -> bool:
        return not (self.status or self.os or self.sources or self.genres or self.tags or self.ratings or self.favorites
                    or self.hidden)

    def count(self) -> int:
        return (len(self.status) + len(self.os) + len(self.sources) + len(self.genres) + len(self.tags)
                + len(self.ratings) + int(self.favorites) + int(bool(self.hidden)))

    def toggle(self, category: str, value: Any, on: bool) -> None:
        lst = getattr(self, category)
        if on and value not in lst:
            lst.append(value)
        elif not on and value in lst:
            lst.remove(value)

    def matches(self, f: GameFacts, show_hidden: bool = False) -> bool:
        if self.hidden == "hidden" and not f.hidden:
            return False
        if self.hidden == "visible" and f.hidden:
            return False
        if not self.hidden and f.hidden and not show_hidden:
            return False
        if self.status:
            ok = (("installed" in self.status and f.installed) or ("not_installed" in self.status and not f.installed)
                  or ("update" in self.status and f.state == "update"))
            if not ok:
                return False
        if self.os and not set(self.os) & set(f.os):
            return False
        if self.sources and f.source not in self.sources:
            return False
        if self.genres and not set(self.genres) & set(f.genres):
            return False
        if self.tags and not ((set(self.tags) & set(f.tags)) or ("" in self.tags and not f.tags)):
            return False
        if self.ratings and f.rating not in self.ratings:
            return False
        if self.favorites and not f.favorite:
            return False
        return True

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}

    @classmethod
    def from_dict(cls, d: dict | None) -> "LibraryFilter":
        d = d if isinstance(d, dict) else {}
        out = cls()
        for k in ("status", "os", "sources", "genres", "tags"):
            v = d.get(k)
            if isinstance(v, list):
                setattr(out, k, [str(x) for x in v])
        if isinstance(d.get("ratings"), list):
            out.ratings = [int(x) for x in d["ratings"] if str(x).lstrip("-").isdigit() and 0 <= int(x) <= MAX_RATING]
        out.favorites = bool(d.get("favorites", False))
        out.hidden = str(d.get("hidden") or "") if d.get("hidden") in ("hidden", "visible") else ""
        return out


def matches_search(f: GameFacts, query: str) -> bool:
    """Suche in der Bibliothek: Titel, eigene Tags, Genres, Entwickler (wie Galaxys Metadaten-Suche)."""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = [f.title.lower(), *(t.lower() for t in f.tags), *(g.lower() for g in f.genres), *(d.lower() for d in f.developers)]
    return any(q in h for h in hay)


# --------------------------------------------------------------------------- Tags und Bewertung
def all_tags(game_settings: Any) -> list[str]:
    """Alle vergebenen Tags (games.json), alphabetisch."""
    out: set[str] = set()
    for gs in game_settings.items().values():
        out.update(str(t) for t in gs.tags if str(t).strip())
    return sorted(out, key=str.lower)


def clean_tag(name: str) -> str:
    return " ".join(str(name or "").split())[:40]


def set_tag(game_settings: Any, product_id: str, tag: str, on: bool) -> list[str]:
    tag = clean_tag(tag)
    if not tag:
        return game_settings.get(product_id).tags
    tags = [t for t in game_settings.get(product_id).tags if t != tag]
    if on:
        tags.append(tag)
    return game_settings.update(product_id, tags=sorted(tags, key=str.lower)).tags


def rename_tag(game_settings: Any, old: str, new: str) -> int:
    """Tag in allen Spielen umbenennen („Alle Tags verwalten“); gibt die Zahl der geänderten Spiele zurück."""
    new = clean_tag(new)
    if not new or new == old:
        return 0
    n = 0
    for pid in games_with_tag(game_settings, old):
        tags = [new if t == old else t for t in game_settings.get(pid).tags]
        game_settings.update(pid, tags=sorted(dict.fromkeys(tags), key=str.lower))
        n += 1
    return n


def delete_tag(game_settings: Any, tag: str) -> int:
    n = 0
    for pid in games_with_tag(game_settings, tag):
        game_settings.update(pid, tags=[t for t in game_settings.get(pid).tags if t != tag])
        n += 1
    return n


def games_with_tag(game_settings: Any, tag: str) -> list[str]:
    return [pid for pid, gs in game_settings.items().items() if tag in gs.tags]


def set_rating(game_settings: Any, product_id: str, rating: int) -> int:
    rating = max(0, min(MAX_RATING, int(rating)))
    return game_settings.update(product_id, rating=rating).rating


# --------------------------------------------------------------------------- Lesezeichen
@dataclass
class Bookmark:
    id: str
    name: str
    view: str = "all"             # all | installed | recent | lib:<ID> | game
    pid: str = ""                 # bei view == "game"
    filter: dict = field(default_factory=dict)
    search: str = ""
    sort_by: str = ""
    sort_desc: bool = False
    group: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Bookmark | None":
        if not isinstance(d, dict) or not str(d.get("name") or "").strip():
            return None
        view = str(d.get("view") or "all")
        if view not in ("all", "installed", "recent", "game") and not view.startswith("lib:"):
            return None
        if view == "game" and not str(d.get("pid") or ""):
            return None
        return cls(id=str(d.get("id") or uuid.uuid4().hex[:8]), name=str(d["name"]).strip()[:60], view=view,
                   pid=str(d.get("pid") or ""), filter=LibraryFilter.from_dict(d.get("filter")).to_dict(),
                   search=str(d.get("search") or "")[:100],
                   sort_by=str(d.get("sort_by") or "") if d.get("sort_by") in SORT_KEYS else "",
                   sort_desc=bool(d.get("sort_desc", False)),
                   group=str(d.get("group") or "") if d.get("group") in GROUP_KEYS else "")

    def to_dict(self) -> dict:
        return asdict(self)

    def same_target(self, other: "Bookmark") -> bool:
        return (self.view, self.pid, self.filter, self.search.strip().lower(), self.sort_by, self.sort_desc, self.group) == \
            (other.view, other.pid, other.filter, other.search.strip().lower(), other.sort_by, other.sort_desc, other.group)


def bookmarks(settings: Any) -> list[Bookmark]:
    out = []
    for d in settings.bookmarks if isinstance(settings.bookmarks, list) else []:
        b = Bookmark.from_dict(d)
        if b is not None:
            out.append(b)
    return out


def _store(settings: Any, items: list[Bookmark]) -> None:
    settings.bookmarks = [b.to_dict() for b in items]
    settings.save()


def find_bookmark(settings: Any, candidate: Bookmark) -> Bookmark | None:
    return next((b for b in bookmarks(settings) if b.same_target(candidate)), None)


def add_bookmark(settings: Any, bm: Bookmark) -> Bookmark:
    items = bookmarks(settings)
    existing = next((b for b in items if b.same_target(bm)), None)
    if existing is not None:
        return existing
    if not bm.id or any(b.id == bm.id for b in items):
        bm.id = uuid.uuid4().hex[:8]
    items.append(bm)
    _store(settings, items)
    return bm


def rename_bookmark(settings: Any, bm_id: str, name: str) -> bool:
    name = " ".join(str(name or "").split())[:60]
    items = bookmarks(settings)
    for b in items:
        if b.id == bm_id and name:
            b.name = name
            _store(settings, items)
            return True
    return False


def move_bookmark(settings: Any, bm_id: str, delta: int) -> bool:
    items = bookmarks(settings)
    idx = next((i for i, b in enumerate(items) if b.id == bm_id), -1)
    new = idx + delta
    if idx < 0 or not 0 <= new < len(items):
        return False
    items.insert(new, items.pop(idx))
    _store(settings, items)
    return True


def remove_bookmark(settings: Any, bm_id: str) -> bool:
    items = bookmarks(settings)
    keep = [b for b in items if b.id != bm_id]
    if len(keep) == len(items):
        return False
    _store(settings, keep)
    return True


def export_bookmarks(settings: Any, path: str | Path) -> int:
    items = bookmarks(settings)
    Path(path).write_text(json.dumps({"galaxy_linux_bookmarks": 1, "bookmarks": [b.to_dict() for b in items]},
                                     indent=2, ensure_ascii=False), encoding="utf-8")
    return len(items)


def import_bookmarks(settings: Any, path: str | Path) -> int:
    """Lesezeichen aus einer exportierten Datei hinzufügen (gleiche Ziele werden nicht doppelt angelegt).
    Wirft ValueError, wenn die Datei kein gültiger Export ist."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    raw = data.get("bookmarks") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError(tr("Keine Lesezeichen in der Datei"))
    items = bookmarks(settings)
    n = 0
    for d in raw:
        b = Bookmark.from_dict(d)
        if b is None or any(x.same_target(b) for x in items):
            continue
        if any(x.id == b.id for x in items):
            b.id = uuid.uuid4().hex[:8]
        items.append(b)
        n += 1
    if n:
        _store(settings, items)
    return n
