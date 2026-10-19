"""Cover und Metadaten für externe Spiele: zuerst GOG-Katalog (falls das Spiel auch auf GOG existiert),
sonst Steam-Shop. Beide Quellen sind öffentlich und brauchen keine Anmeldung. Treffer werden streng
geprüft – lieber kein Cover als ein falsches. Ergebnisse (auch „nichts gefunden“) werden gecacht.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any

from .. import paths
from ..jsonstore import read_json, write_json

log = logging.getLogger(__name__)

GOG_SEARCH = "https://catalog.gog.com/v1/catalog"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_ASSET = "https://cdn.cloudflare.steamstatic.com/steam/apps/{id}/{name}"
EDITION_WORDS = {"gold", "goty", "complete", "edition", "definitive", "deluxe", "enhanced", "remastered", "hd",
                 "collection", "ultimate", "anniversary", "classic", "redux", "plus", "the", "platinum", "game",
                 "of", "year", "special", "standard", "digital", "directors", "cut", "director", "s"}
CACHE_DAYS = 14


def _tokens(s: str) -> list[str]:
    s = re.sub(r"[™®©]", " ", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def title_matches(query: str, candidate: str) -> bool:
    """Treffer muss mit dem Suchtitel beginnen; danach sind nur Editionswörter erlaubt.
    So passen „Unreal Gold“ und „Soldier of Fortune: Platinum Edition“, aber nicht „Soldier of Fortune II“,
    „Soldier of Fortune: Payback“ (anderes Spiel) oder DLCs."""
    q, c = _tokens(query), _tokens(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    if c[:len(q)] != q:
        return False
    return all(t in EDITION_WORDS for t in c[len(q):])


def _cache_key(query: str) -> str:
    return "external2-" + "-".join(_tokens(query))[:80]   # 2 = strengere Trefferregel


def lookup(session, queries: list[str]) -> dict[str, Any] | None:
    """Sucht Metadaten zu den Suchbegriffen (der Reihe nach). Rückgabe: dict oder None."""
    for q in [x for x in dict.fromkeys(queries) if x and x.strip()]:
        f = paths.API_CACHE_DIR / f"{_cache_key(q)}.json"
        cached = read_json(f, None)
        if cached is not None and time.time() - cached.get("_ts", 0) < CACHE_DAYS * 86400:
            if cached.get("found"):
                return cached["data"]
            continue
        data = None
        try:
            data = _gog(session, q) or _steam(session, q)
        except Exception as exc:  # Netzfehler: nicht negativ cachen
            log.debug("Cover-Suche für %r fehlgeschlagen: %s", q, exc)
            continue
        write_json(f, {"_ts": time.time(), "found": bool(data), "data": data})
        if data:
            log.info("Metadaten für %r: %s %s (%s)", q, data["source"], data["id"], data["title"])
            return data
    return None


def _gog(session, q: str) -> dict | None:
    r = session.get(GOG_SEARCH, params={"limit": 8, "query": f"like:{q}", "order": "desc:score",
                                        "productType": "in:game,pack", "locale": "de-DE"}, timeout=15)
    if not r.ok:
        return None
    for p in r.json().get("products", []):
        if title_matches(q, p.get("title", "")):
            return {"source": "gog", "id": str(p.get("id")), "title": p.get("title", ""),
                    "cover": p.get("coverVertical") or "", "background": p.get("galaxyBackgroundImage") or p.get("coverHorizontal") or "",
                    "logo": p.get("logo") or "", "developers": [d.strip() for d in p.get("developers") or []],
                    "publishers": [d.strip() for d in p.get("publishers") or []],
                    "genres": [g.get("name", "") for g in p.get("genres") or []],
                    "release_date": str(p.get("releaseDate") or "").replace(".", "-")[:10], "summary": ""}
    return None


def _steam(session, q: str) -> dict | None:
    r = session.get(STEAM_SEARCH, params={"term": q, "l": "german", "cc": "DE"}, timeout=15)
    if not r.ok:
        return None
    for it in r.json().get("items", []):
        if it.get("type") != "app" or not title_matches(q, re.sub(r"[™®©]", "", it.get("name", ""))):
            continue
        app = str(it.get("id"))
        data = {"source": "steam", "id": app, "title": re.sub(r"[™®©]", "", it.get("name", "")).strip(),
                "cover": STEAM_ASSET.format(id=app, name="library_600x900_2x.jpg"),
                "background": STEAM_ASSET.format(id=app, name="library_hero.jpg"),
                "logo": STEAM_ASSET.format(id=app, name="logo.png"),
                "developers": [], "publishers": [], "genres": [], "release_date": "", "summary": ""}
        try:
            d = session.get(STEAM_DETAILS, params={"appids": app, "l": "german"}, timeout=15).json().get(app, {}).get("data") or {}
            data.update(summary=re.sub(r"<[^>]+>", "", d.get("short_description", "")).strip(),
                        developers=d.get("developers") or [], publishers=d.get("publishers") or [],
                        genres=[g.get("description", "") for g in d.get("genres") or []],
                        release_date=(d.get("release_date") or {}).get("date", ""))
        except Exception:
            pass
        return data
    return None
