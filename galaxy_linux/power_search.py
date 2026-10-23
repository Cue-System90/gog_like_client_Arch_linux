"""Power Search (Strg+F) wie in Galaxy 2.1 – ohne GTK: sucht in eigenen und installierten Spielen (Titel, Tags,
Genres, Entwickler), in Lesezeichen und im GOG-Katalog (catalog.gog.com, „Im Store kaufen“). Jedes Ergebnis hat eine
Hauptaktion (Spielen, Installieren, Spiel anzeigen, Im Store kaufen, Lesezeichen anzeigen)."""
from __future__ import annotations

from .i18n import N_, tr

from dataclasses import dataclass
from typing import Any

from . import library_view as lv

ACTIONS = {"play": N_("Spielen"), "install": N_("Installieren"), "show_game": N_("Spiel anzeigen"),
           "buy": N_("Im Store kaufen"), "show_bookmark": N_("Lesezeichen anzeigen")}
TYPES = {"installed": N_("Installiert"), "owned": N_("Spiel im Besitz"), "game": N_("Spiel"),
         "bookmark": N_("Lesezeichen"), "tag": N_("Tag"), "developer": N_("Entwickler")}


@dataclass
class Result:
    kind: str                 # game | store | bookmark
    title: str
    subtitle: str = ""
    action: str = "show_game"
    product_id: str = ""
    bookmark_id: str = ""
    url: str = ""
    image: str = ""
    score: float = 0.0

    @property
    def action_label(self) -> str:
        return tr(ACTIONS.get(self.action, self.action))


def _score(text: str, q: str) -> float:
    t = text.lower()
    for art in ("the ", "a ", "der ", "die ", "das "):
        if t.startswith(art) and not q.startswith(art):
            t = t[len(art):]
            break
    if t == q:
        return 4.0
    if t.startswith(q):
        return 3.0
    if any(w.startswith(q) for w in t.replace(":", " ").replace("-", " ").split()):
        return 2.0
    return 1.0 if q in t else 0.0


def local_results(backend: Any, query: str, limit: int = 12, entries: list | None = None) -> list[Result]:
    """Eigene Spiele (auch ausgeblendete und die der eigenen Bibliotheken) und Lesezeichen."""
    q = (query or "").strip().lower()
    if not q:
        return []
    if entries is None:
        by_id = {e.id: e for e in backend.visible_library(include_hidden=True)}
        for g in backend.registry.all():
            if g.product_id not in by_id:
                e = backend.entry(g.product_id)
                if e is not None:
                    by_id[e.id] = e
        entries = list(by_id.values())
    out: list[Result] = []
    for e in entries:
        f = lv.facts(backend, e)
        score = _score(f.title, q)
        why = ""
        if not score:
            for kind, values in (("tag", f.tags), ("developer", f.developers), ("genre", f.genres)):
                hit = next((v for v in values if q in v.lower()), "")
                if hit:
                    score, why = 0.5, f"{tr(TYPES.get(kind, N_('Genre')))}: {hit}"
                    break
        if not score:
            continue
        state = f.state
        action = "play" if state in ("installed", "update") else "install" if state == "not_installed" and \
            not f.id.startswith("ext-") else "show_game"
        typ = tr(TYPES["installed"]) if f.installed else tr(TYPES["owned"])
        out.append(Result("game", f.title, " · ".join(x for x in (typ, why) if x), action, product_id=f.id, score=score))
    for b in lv.bookmarks(backend.settings):
        s = _score(b.name, q)
        if s:
            out.append(Result("bookmark", b.name, tr(TYPES["bookmark"]), "show_bookmark", bookmark_id=b.id, score=s - 0.1))
    out.sort(key=lambda r: (-r.score, r.title.lower()))
    return out[:limit]


def price_text(p: dict) -> str:
    price = p.get("price") or {}
    if not isinstance(price, dict):
        return ""
    final = price.get("final") or ""
    disc = price.get("discount") or ""
    if not final:
        money = price.get("finalMoney") or {}
        if isinstance(money, dict) and money.get("amount") is not None:
            final = f"{money.get('amount')} {money.get('currency', '')}".strip()
    return f"{final} ({disc})" if final and disc and str(disc) not in ("0", "0%", "-0%") else str(final)


def store_results(products: list[dict], owned: set[str], limit: int = 8) -> list[Result]:
    """Treffer aus catalog.gog.com; Spiele im Besitz bekommen „Spiel anzeigen“ statt „Im Store kaufen“."""
    out = []
    for p in products[:limit]:
        pid = str(p.get("id") or "")
        title = str(p.get("title") or "")
        if not pid or not title:
            continue
        link = str(p.get("storeLink") or "")
        if pid in owned:
            out.append(Result("store", title, tr(TYPES["owned"]), "show_game", product_id=pid, url=link))
        else:
            out.append(Result("store", title, price_text(p), "buy", product_id=pid, url=link,
                              image=str(p.get("coverVertical") or p.get("coverHorizontal") or "")))
    return out
