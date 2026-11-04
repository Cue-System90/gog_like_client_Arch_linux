"""Wikidata und Wikipedia als letzte Quelle für Namen und Cover externer Spiele.

* ``find_game``: ``wbsearchentities`` auf Deutsch und Englisch; angenommen wird nur ein Objekt, das eine Instanz von
  „Videospiel“ (Q7889) ist und dessen Bezeichnung oder Alias dem Suchtitel entspricht (Tokenvergleich wie bei den
  Covern – „Freelancer“ wird nie „Freelancer Simulator“). Ordnernamen enthalten statt „:“ oft „ - “; beide Formen
  werden gesucht.
* ``page_image``: Bild der englischen Wikipedia-Seite des Objekts (REST ``page/summary``, ``originalimage``) – bei
  Spielen meist das Packungsbild.

Ergebnisse (auch „nichts gefunden“) landen im API-Cache; Netzfehler werden nicht gespeichert.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import paths
from ..jsonstore import read_json, write_json
from .covers import _tokens

log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
VIDEO_GAME = "Q7889"
LANGS = ("de", "en")
CACHE_DAYS = 30
MAX_CANDIDATES = 4


@dataclass
class WikiGame:
    qid: str
    labels: dict[str, str] = field(default_factory=dict)       # Sprache → Bezeichnung
    enwiki: str = ""                                          # Titel der englischen Wikipedia-Seite
    image: str = ""                                           # Bild-URL (Wikipedia)

    def label(self, lang: str = "en") -> str:
        """Englische Bezeichnung, mit ``lang="de"`` die deutsche, falls vorhanden."""
        if lang == "de" and self.labels.get("de"):
            return self.labels["de"]
        return self.labels.get("en") or self.labels.get("de") or ""


# --------------------------------------------------------------------------- Antworten zerlegen (ohne Netz testbar)
def parse_search(data: Any) -> list[tuple[str, list[str]]]:
    """``wbsearchentities`` → [(Q-ID, [getroffener Text, Bezeichnung, Aliasse])] in der Reihenfolge der Suche."""
    out = []
    for r in (data or {}).get("search", []) if isinstance(data, dict) else []:
        if not isinstance(r, dict) or not str(r.get("id", "")).startswith("Q"):
            continue
        texts = [str((r.get("match") or {}).get("text") or ""), str(r.get("label") or "")]
        texts += [str(a) for a in r.get("aliases") or []]
        out.append((r["id"], [t for t in texts if t]))
    return out


def parse_instance_of(data: Any) -> set[str]:
    """``wbgetclaims`` (P31) → Menge der Klassen-IDs."""
    out = set()
    claims = (data or {}).get("claims", {}) if isinstance(data, dict) else {}
    for c in claims.get("P31", []) if isinstance(claims, dict) else []:
        try:
            out.add(c["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            continue
    return out


def parse_entity(data: Any, qid: str) -> WikiGame | None:
    """``wbgetentities`` (labels, aliases, sitelinks) → WikiGame ohne Bild."""
    ent = ((data or {}).get("entities") or {}).get(qid) if isinstance(data, dict) else None
    if not isinstance(ent, dict) or "missing" in ent:
        return None
    labels = {lang: v.get("value", "") for lang, v in (ent.get("labels") or {}).items()
              if lang in LANGS and isinstance(v, dict)}
    enwiki = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title", "")
    return WikiGame(qid=qid, labels={k: v for k, v in labels.items() if v}, enwiki=enwiki)


def entity_names(data: Any, qid: str) -> list[str]:
    """Alle Bezeichnungen und Aliasse (de/en) eines Objekts aus ``wbgetentities``."""
    ent = ((data or {}).get("entities") or {}).get(qid) if isinstance(data, dict) else None
    if not isinstance(ent, dict):
        return []
    names = [v.get("value", "") for v in (ent.get("labels") or {}).values() if isinstance(v, dict)]
    for lst in (ent.get("aliases") or {}).values():
        names += [v.get("value", "") for v in lst or [] if isinstance(v, dict)]
    return [n for n in names if n]


def parse_summary(data: Any) -> str:
    """Wikipedia ``page/summary`` → Bild-URL (Original, sonst Vorschaubild)."""
    if not isinstance(data, dict):
        return ""
    for key in ("originalimage", "thumbnail"):
        src = (data.get(key) or {}).get("source") if isinstance(data.get(key), dict) else ""
        if src:
            src = str(src)
            return src.split("?", 1)[0] if "utm_" in src else src     # Statistik-Parameter weg: gleiche Datei, gleicher Cache
    return ""


def query_variants(query: str) -> list[str]:
    """Suchformen: wie angegeben und mit „: “ statt „ - “ (Ordnernamen dürfen unter Windows keinen Doppelpunkt haben)."""
    q = " ".join((query or "").split())
    out = [q, q.replace(" - ", ": ").replace(" – ", ": ")]
    return [x for x in dict.fromkeys(out) if x]


def same_name(query: str, names: list[str]) -> bool:
    q = _tokens(query)
    return bool(q) and any(_tokens(n) == q for n in names)


# --------------------------------------------------------------------------- Netz
def _get(session, url: str, params: dict | None = None) -> Any:
    from .. import USER_AGENT
    from ..clientinfo import REPO_URL
    r = session.get(url, params=params, timeout=15, headers={"User-Agent": f"{USER_AGENT} (+{REPO_URL})"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _search(session, query: str, lang: str) -> list[tuple[str, list[str]]]:
    return parse_search(_get(session, API, {"action": "wbsearchentities", "search": query, "language": lang,
                                            "uselang": lang, "type": "item", "limit": 10, "format": "json"}))


def _is_video_game(session, qid: str) -> bool:
    return VIDEO_GAME in parse_instance_of(_get(session, API, {"action": "wbgetclaims", "entity": qid,
                                                                "property": "P31", "format": "json"}))


def _entity(session, qid: str) -> tuple[WikiGame | None, list[str]]:
    data = _get(session, API, {"action": "wbgetentities", "ids": qid, "props": "labels|aliases|sitelinks",
                               "languages": "|".join(LANGS), "sitefilter": "enwiki", "format": "json"})
    return parse_entity(data, qid), entity_names(data, qid)


def page_image(session, enwiki_title: str) -> str:
    if not enwiki_title:
        return ""
    title = urllib.parse.quote(enwiki_title.replace(" ", "_"), safe="")
    return parse_summary(_get(session, SUMMARY.format(lang="en", title=title)))


def _find(session, query: str) -> WikiGame | None:
    seen: list[str] = []
    for variant in query_variants(query):
        for lang in LANGS:
            for qid, texts in _search(session, variant, lang):
                if qid in seen or not same_name(variant, texts) and not same_name(query, texts):
                    continue
                seen.append(qid)
    for qid in seen[:MAX_CANDIDATES]:
        if not _is_video_game(session, qid):
            continue
        game, names = _entity(session, qid)
        if game is None or not any(same_name(v, names) for v in query_variants(query)):
            continue
        game.image = page_image(session, game.enwiki)
        return game
    return None


def _cache_file(query: str):
    return paths.API_CACHE_DIR / f"wikidata1-{'-'.join(_tokens(query))[:80]}.json"


def find_game(session, queries: list[str], errors: list | None = None) -> WikiGame | None:
    """Erstes Videospiel-Objekt zu den Suchbegriffen (der Reihe nach), mit Bild der englischen Wikipedia-Seite."""
    for q in [x for x in dict.fromkeys(queries) if x and _tokens(x)]:
        f = _cache_file(q)
        cached = read_json(f, None)
        if isinstance(cached, dict) and time.time() - cached.get("_ts", 0) < CACHE_DAYS * 86400:
            if cached.get("found"):
                d = cached.get("data") or {}
                return WikiGame(qid=d.get("qid", ""), labels=d.get("labels") or {}, enwiki=d.get("enwiki", ""),
                                image=d.get("image", ""))
            continue
        try:
            game = _find(session, q)
        except Exception as exc:  # Netzfehler: nicht negativ cachen
            log.debug("Wikidata-Suche für %r fehlgeschlagen: %s", q, exc)
            if errors is not None:
                errors.append(exc)
            continue
        write_json(f, {"_ts": time.time(), "found": game is not None, "data": asdict(game) if game else None})
        if game is not None:
            log.info("Wikidata für %r: %s (%s)", q, game.qid, game.label())
            return game
    return None
