"""Wunschliste: IDs, Batch-Preise, Titel und die Erkennung neuer Rabatte (gog-apis.md §6) – ohne GTK.

* ``embed.gog.com/user/wishlist.json`` (Bearer) liefert die IDs,
* ``api.gog.com/products/prices?ids=…&countryCode=…&currency=…`` die Preise in Cent (je Aufruf höchstens 50 IDs;
  Produkte ohne Preis – „Demnächst“ – fehlen in der Antwort),
* ``api.gog.com/products?ids=…`` Titel, Slug und Bilder (24 h zwischengespeichert).

``check()`` vergleicht mit dem Stand der letzten Prüfung (``wishlist_state.json`` im Datenordner): Ein Spiel, dessen
Rabatt begonnen hat oder gewachsen ist, ergibt eine Änderung „discount“; ein Spiel, das vorher keinen Preis hatte und
jetzt einen hat, „available“ (Neu erhältlich). Die erste Prüfung und neu auf die Wunschliste gesetzte Spiele melden
nichts. Schlägt ein Abruf fehl, bleibt der alte Stand stehen (kein falsches „Neu erhältlich“ nach einem Netzfehler).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..i18n import tr
from ..jsonstore import read_json, write_json
from .client import API, EMBED, ApiError
from .prices import Price, format_amount, parse_cents

log = logging.getLogger(__name__)

BATCH = 50
CHECK_INTERVAL = 6 * 3600       # s: Abstand der Rabatt-Prüfungen
START_DELAY = 60                # s: erste Prüfung nach dem Start
PRODUCTS_TTL = 86400            # s: Titel/Bilder aus api.gog.com/products

_products: dict[tuple[str, str], tuple[float, dict]] = {}
_products_lock = threading.Lock()


@dataclass
class Change:
    kind: str           # discount | available
    product_id: str
    price: Price
    title: str = ""
    link: str = ""      # Produktseite

    @property
    def discount(self) -> int:
        return self.price.discount


# --------------------------------------------------------------------------- Abrufe
def parse_ids(data: Any) -> list[str]:
    """IDs aus ``wishlist.json`` (``{"wishlist": {"<id>": true, …}}``), in der Reihenfolge der Antwort."""
    if not isinstance(data, dict):
        return []
    wl = data.get("wishlist", data)
    if isinstance(wl, dict):
        return [str(k) for k, v in wl.items() if v and str(k).isdigit()]
    if isinstance(wl, list):
        return [str(v) for v in wl if str(v).isdigit()]
    return []


def fetch_ids(api: Any) -> list[str]:
    """IDs der Wunschliste (braucht die Anmeldung; wirft NotLoggedIn/ApiError)."""
    return parse_ids(api.get_json(f"{EMBED}/user/wishlist.json", timeout=15, retries=1))


def chunks(ids: Iterable[str], size: int = BATCH) -> list[list[str]]:
    items = list(dict.fromkeys(str(i) for i in ids))
    return [items[i:i + size] for i in range(0, len(items), size)]


def parse_batch_prices(data: Any, currency: str = "") -> dict[str, Price]:
    """Antwort von ``/products/prices`` → {ID: Price} in der gewünschten Währung (sonst der ersten genannten)."""
    out: dict[str, Price] = {}
    items = ((data or {}).get("_embedded") or {}).get("items") if isinstance(data, dict) else None
    for item in items or []:
        emb = item.get("_embedded") if isinstance(item, dict) else None
        if not isinstance(emb, dict):
            continue
        pid = str((emb.get("product") or {}).get("id") or "")
        prices = [p for p in emb.get("prices") or [] if isinstance(p, dict)]
        if not pid or not prices:
            continue
        chosen = next((p for p in prices if str((p.get("currency") or {}).get("code") or "").upper() == currency.upper()),
                      prices[0]) if currency else prices[0]
        final, base = parse_cents(chosen.get("finalPrice")), parse_cents(chosen.get("basePrice"))
        if final is None:
            continue
        code = str((chosen.get("currency") or {}).get("code") or final[1]).upper()
        out[pid] = Price(final[0], base[0] if base and base[0] >= final[0] else final[0], code)
    return out


def fetch_prices(api: Any, ids: Iterable[str], country: str = "", currency: str = "") -> dict[str, Price]:
    """Preise für alle IDs (in Paketen zu 50, ohne Anmeldung). Wirft ApiError, wenn ein Paket fehlschlägt."""
    out: dict[str, Price] = {}
    for part in chunks(ids):
        params: dict[str, str] = {"ids": ",".join(part)}
        if country:
            params["countryCode"] = country
        if currency:
            params["currency"] = currency
        r = api.get(f"{API}/products/prices", params=params, authenticated=False, timeout=15, retries=1)
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, "products/prices"))
        try:
            out.update(parse_batch_prices(r.json(), currency))
        except ValueError as exc:
            raise ApiError(tr('Ungültige JSON-Antwort von {0}').format("products/prices")) from exc
    return out


def _https(url: Any) -> str:
    url = str(url or "")
    return "https:" + url if url.startswith("//") else url


def parse_products(data: Any) -> dict[str, dict]:
    """``api.gog.com/products?ids=…`` → {ID: {title, slug, link, image}}."""
    out: dict[str, dict] = {}
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        imgs = p.get("images") if isinstance(p.get("images"), dict) else {}
        links = p.get("links") if isinstance(p.get("links"), dict) else {}
        out[str(p["id"])] = {"title": str(p.get("title") or ""), "slug": str(p.get("slug") or ""),
                             "link": str(links.get("product_card") or ""),
                             "image": _https(imgs.get("logo2x") or imgs.get("logo") or "")}
    return out


def fetch_products(api: Any, ids: Iterable[str], locale: str = "en-US", now: float | None = None) -> dict[str, dict]:
    """Titel und Bilder (24 h im Speicher). Fehlschläge einzelner Pakete werden übergangen."""
    now = time.time() if now is None else now
    ids = list(dict.fromkeys(str(i) for i in ids))
    out: dict[str, dict] = {}
    missing = []
    with _products_lock:
        for pid in ids:
            hit = _products.get((pid, locale))
            if hit and now - hit[0] < PRODUCTS_TTL:
                out[pid] = hit[1]
            else:
                missing.append(pid)
    for part in chunks(missing):
        try:
            r = api.get(f"{API}/products", params={"ids": ",".join(part), "locale": locale}, authenticated=False,
                        timeout=15, retries=1)
            data = r.json() if r.ok else None
        except (ApiError, ValueError) as exc:
            log.debug("Produktdaten der Wunschliste nicht abrufbar: %s", exc)
            continue
        got = parse_products(data)
        with _products_lock:
            for pid, info in got.items():
                _products[(pid, locale)] = (now, info)
        out.update(got)
    return out


# --------------------------------------------------------------------------- Vergleich
def snapshot(ids: Iterable[str], prices: dict[str, Price]) -> dict[str, dict]:
    """Stand je ID: Preis (oder keiner = nicht erhältlich)."""
    out: dict[str, dict] = {}
    for pid in dict.fromkeys(str(i) for i in ids):
        p = prices.get(pid)
        out[pid] = {"available": p is not None, "discount": p.discount if p else 0, **(p.to_dict() if p else {})}
    return out


def detect_changes(old: Any, new: dict[str, dict]) -> list[tuple[str, str]]:
    """(Art, ID) für begonnene/gewachsene Rabatte und neu erhältliche Spiele; größter Rabatt zuerst. IDs ohne alten
    Stand (neu auf der Wunschliste) melden nichts."""
    old = old if isinstance(old, dict) else {}
    hits: list[tuple[int, str, str]] = []
    for pid, n in new.items():
        o = old.get(pid)
        if not isinstance(o, dict) or not n.get("available"):
            continue
        disc = int(n.get("discount") or 0)
        if not o.get("available"):
            hits.append((disc, "available", pid))
        elif disc > 0 and disc > int(o.get("discount") or 0):
            hits.append((disc, "discount", pid))
    hits.sort(key=lambda h: (-h[0], h[2]))
    return [(kind, pid) for _d, kind, pid in hits]


def load_state(path: Path) -> dict:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def check(api: Any, path: Path, country: str = "", currency: str = "", locale: str = "en-US",
          now: float | None = None) -> list[Change]:
    """Wunschliste prüfen und den Stand speichern; liefert die zu meldenden Änderungen. Wirft NotLoggedIn/ApiError."""
    now = time.time() if now is None else now
    ids = fetch_ids(api)
    prices = fetch_prices(api, ids, country, currency)
    new = snapshot(ids, prices)
    state = load_state(path)
    first = not state.get("checked")
    found = [] if first else detect_changes(state.get("items"), new)
    info = fetch_products(api, [pid for _k, pid in found], locale, now) if found else {}
    write_json(path, {"checked": now, "country": country, "currency": currency, "items": new})
    log.info("Wunschliste geprüft: %d Spiele, %d mit Preis, %d Änderung(en)%s", len(ids), len(prices), len(found),
             " (erste Prüfung)" if first else "")
    return [Change(kind, pid, prices[pid], info.get(pid, {}).get("title", ""), info.get(pid, {}).get("link", ""))
            for kind, pid in found]


def notification_text(change: Change, lang: str) -> tuple[str, str]:
    """(Art, Text) einer Änderung für das Benachrichtigungszentrum, Preis in der Sprache der Oberfläche."""
    title = change.title or change.product_id
    price = tr('Kostenlos') if change.price.free else format_amount(change.price.final, change.price.currency, lang)
    if change.kind == "available":
        return "available", tr('Neu erhältlich: „{0}“ – {1}').format(title, price)
    return "wishlist", tr('„{0}“ ist jetzt um {1} % reduziert – {2}').format(title, change.discount, price)


def notification_key(change: Change) -> str:
    """Schlüssel gegen doppelte Meldungen: gleicher Rabatt zum gleichen Preis wird nur einmal gemeldet."""
    return f"{change.kind}:{change.product_id}:{change.discount}:{change.price.final}"


def due(path: Path, now: float | None = None, interval: float = CHECK_INTERVAL) -> bool:
    """Ist die nächste Prüfung fällig (auch über Neustarts hinweg)?"""
    now = time.time() if now is None else now
    try:
        last = float(load_state(path).get("checked") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return not last or now - last >= interval
