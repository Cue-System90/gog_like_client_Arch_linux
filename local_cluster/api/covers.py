"""Cover und Metadaten für externe Spiele: zuerst GOG-Katalog (falls das Spiel auch auf GOG existiert),
sonst Steam-Shop. Beide Quellen sind öffentlich und brauchen keine Anmeldung. Treffer werden streng
geprüft – lieber kein Cover als ein falsches. Ergebnisse (auch „nichts gefunden“) werden gecacht.

Ein Treffer liefert zusätzlich ``base``: den offiziellen Titel in der Schreibweise des Shops, gekürzt auf den
gesuchten Teil („Soldier of Fortune“ aus „Soldier of Fortune: Platinum Edition“, „Anno 2070“ aus „Anno 2070™“).
Mit ``company`` (Hersteller aus der Versionsinfo der .exe) gilt im GOG-Katalog außerdem „Titel: Untertitel“, wenn
Entwickler oder Publisher dazu passen („Emperor“ von Sierra → „Emperor: Rise of the Middle Kingdom“).
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
# Firmenwörter ohne Aussagekraft beim Vergleich Hersteller ↔ Entwickler/Publisher
COMPANY_NOISE = {"inc", "ltd", "llc", "gmbh", "co", "corp", "corporation", "company", "games", "game", "entertainment",
                 "studios", "studio", "software", "interactive", "digital", "the", "and", "of", "publishing", "sa",
                 "ag", "limited", "productions", "media", "group", "international", "build", "by", "www", "com"}
CACHE_DAYS = 14


def _tokens(s: str) -> list[str]:
    s = re.sub(r"[™®©]", " ", s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("+", " and ")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def clean_title(title: str) -> str:
    """Shop-Titel ohne ™/®/© und doppelte Leerzeichen."""
    t = re.sub(r"[™®©]", "", title or "")
    return re.sub(r"\s{2,}", " ", t).strip()


def title_matches(query: str, candidate: str) -> bool:
    """Treffer muss mit dem Suchtitel beginnen; danach sind nur Editionswörter erlaubt.
    So passen „Unreal Gold“ und „Soldier of Fortune: Platinum Edition“, aber nicht „Soldier of Fortune II“,
    „Soldier of Fortune: Payback“ (anderes Spiel), „Freelancer Simulator“ oder DLCs."""
    q, c = _tokens(query), _tokens(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    if c[:len(q)] != q:
        return False
    return all(t in EDITION_WORDS for t in c[len(q):])


def official_prefix(candidate: str, query: str) -> str:
    """Der Teil des offiziellen Titels, der dem Suchtitel entspricht („Anno 2070™“ → „Anno 2070“,
    „Soldier of Fortune: Platinum Edition“ → „Soldier of Fortune“). Ohne Entsprechung: der ganze Titel."""
    c = clean_title(candidate)
    q = _tokens(query)
    if not q:
        return c
    for i in range(1, len(c) + 1):
        if _tokens(c[:i]) == q and (i == len(c) or not c[i].isalnum()):
            return c[:i].rstrip(" :-–_,.")
    return c


def remainder(text: str, base: str) -> str:
    """Was in ``text`` nach dem Anfang ``base`` folgt (Tokenvergleich): „Anno 2070 Complete Edition“ nach
    „Anno 2070“ → „Complete Edition“; "" wenn ``text`` nicht mit ``base`` beginnt."""
    b = _tokens(base)
    if not b or _tokens(text)[:len(b)] != b:
        return ""
    for i in range(1, len(text) + 1):
        if _tokens(text[:i]) == b and (i == len(text) or not text[i].isalnum()):
            return text[i:].strip(" :-–_,.")
    return ""


def subtitle_matches(query: str, candidate: str) -> bool:
    """„Emperor: Rise of the Middle Kingdom“ zu „Emperor“: Titel beginnt mit dem Suchtitel, danach ein Doppelpunkt
    oder Gedankenstrich und ein Untertitel."""
    q = _tokens(query)
    c = clean_title(candidate)
    m = re.match(r"^(.+?)\s*(?::|\s[-–]\s)\s*(\S.*)$", c)
    return bool(q and m and _tokens(m.group(1)) == q and _tokens(m.group(2)))


def company_matches(company: str, names: list[str]) -> bool:
    """Hersteller aus der .exe passt zu Entwickler/Publisher (gemeinsames aussagekräftiges Wort)."""
    mine = {t for t in _tokens(company) if t not in COMPANY_NOISE and len(t) > 2}
    if not mine:
        return False
    theirs = {t for n in names for t in _tokens(n) if t not in COMPANY_NOISE}
    return bool(mine & theirs)


def _cache_key(query: str, company: str = "") -> str:
    key = "external3-" + "-".join(_tokens(query))[:80]     # 3 = mit offiziellem Titel und Hersteller-Regel
    if company:
        key += "-c-" + "-".join(t for t in _tokens(company) if t not in COMPANY_NOISE)[:30]
    return key


def lookup(session, queries: list[str], company: str = "", errors: list | None = None) -> dict[str, Any] | None:
    """Sucht Metadaten zu den Suchbegriffen (der Reihe nach). Rückgabe: dict (mit ``query`` und ``base``) oder None.
    Netzfehler werden nicht als „nichts gefunden“ gespeichert, sondern in ``errors`` gesammelt."""
    for q in [x for x in dict.fromkeys(queries) if x and x.strip() and _tokens(x)]:
        f = paths.API_CACHE_DIR / f"{_cache_key(q, company)}.json"
        cached = read_json(f, None)
        if cached is not None and time.time() - cached.get("_ts", 0) < CACHE_DAYS * 86400:
            if cached.get("found"):
                return cached["data"]
            continue
        data = None
        try:
            data = _gog(session, q, company) or _steam(session, q)
        except Exception as exc:  # Netzfehler: nicht negativ cachen
            log.debug("Cover-Suche für %r fehlgeschlagen: %s", q, exc)
            if errors is not None:
                errors.append(exc)
            continue
        if data:
            data["query"] = q
        write_json(f, {"_ts": time.time(), "found": bool(data), "data": data})
        if data:
            log.info("Metadaten für %r: %s %s (%s)", q, data["source"], data["id"], data["title"])
            return data
    return None


def _gog_data(p: dict, q: str, base: str, rule: str) -> dict:
    return {"source": "gog", "id": str(p.get("id")), "title": clean_title(p.get("title", "")), "base": base,
            "rule": rule, "slug": p.get("slug") or "",
            "cover": p.get("coverVertical") or "", "background": p.get("galaxyBackgroundImage") or p.get("coverHorizontal") or "",
            "logo": p.get("logo") or "", "developers": [d.strip() for d in p.get("developers") or []],
            "publishers": [d.strip() for d in p.get("publishers") or []],
            "genres": [g.get("name", "") for g in p.get("genres") or []],
            "release_date": str(p.get("releaseDate") or "").replace(".", "-")[:10], "summary": ""}


def _gog(session, q: str, company: str = "") -> dict | None:
    r = session.get(GOG_SEARCH, params={"limit": 8, "query": f"like:{q}", "order": "desc:score",
                                        "productType": "in:game,pack", "locale": "de-DE"}, timeout=15)
    if not r.ok:
        return None
    products = r.json().get("products", [])
    for p in products:
        if title_matches(q, p.get("title", "")):
            return _gog_data(p, q, official_prefix(p.get("title", ""), q), "strict")
    if company:
        hits = [p for p in products if subtitle_matches(q, p.get("title", ""))
                and company_matches(company, [*(p.get("developers") or []), *(p.get("publishers") or [])])]
        if len(hits) == 1:                       # nur eindeutig: zwei passende Untertitel → lieber nichts
            return _gog_data(hits[0], q, clean_title(hits[0].get("title", "")), "subtitle")
    return None


def _steam(session, q: str) -> dict | None:
    r = session.get(STEAM_SEARCH, params={"term": q, "l": "german", "cc": "DE"}, timeout=15)
    if not r.ok:
        return None
    for it in r.json().get("items", []):
        if it.get("type") != "app" or not title_matches(q, clean_title(it.get("name", ""))):
            continue
        app = str(it.get("id"))
        name = clean_title(it.get("name", ""))
        data = {"source": "steam", "id": app, "title": name, "base": official_prefix(name, q), "rule": "strict",
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
