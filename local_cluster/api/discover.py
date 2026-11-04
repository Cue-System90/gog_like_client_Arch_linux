"""„Entdecken“ wie in GOG Galaxy 2.1 – Regale aus ``catalog.gog.com`` (gog-apis.md §3.2), ohne GTK.

Regale: „Spiele für dich“ (Trends, Spiele im Besitz ausgeblendet, wenn angemeldet), „Neuerscheinungen“
(``releaseStatuses=in:new-arrival``), „Angebote“ (``discounted=eq:true``), „Aus deiner Wunschliste“ (wishlist.json
+ Batch-Preise), „Zusätzliche Inhalte für deine Spiele“ (``onlyDlcForOwned=true`` mit Bearer) und „Good Old Games“
(Katalog-Tag ``good-old-game``). Land und Währung kommen aus ``embed.gog.com/userData.json``, die Sprache aus der
Oberfläche. Antworten bleiben kurz (10 min) im Speicher; schlägt ein Abruf fehl, gilt der letzte Stand weiter.

Der Katalog nimmt Filter, die er nicht kennt, stillschweigend hin (gog-apis.md §3.2: unbekannte Parameter liefern die
volle Liste). Ob ``onlyDlcForOwned`` mit dem Token wirkt, prüft deshalb ein Vergleich mit der Gesamtzahl aller DLCs
(``dlc_filter_applied``); wirkt der Filter nicht, entfällt das Regal.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..i18n import N_, tr
from ..store_links import STORE_SECTIONS, store_lang, store_locale
from . import wishlist as wl
from .auth import AuthError
from .client import CATALOG, EMBED, ApiError, NotLoggedIn
from .prices import Price, catalog_price

log = logging.getLogger(__name__)

CATALOG_URL = f"{CATALOG}/v1/catalog"
SHELF_LIMIT = 40           # abgefragt – Reserve, damit nach dem Entfernen von Doppelten genug bleiben
SHELF_SHOW = 20            # gezeigt je Regal
CACHE_TTL = 600            # s: Regale
REGION_TTL = 6 * 3600      # s: Land und Währung
WISHLIST_SHELF_MAX = 30
GAMES = "in:game,pack"


@dataclass(frozen=True)
class Region:
    country: str = ""
    currency: str = ""
    lang: str = "en"           # Sprache der Oberfläche (Preise, Store-Adressen)

    @property
    def locale(self) -> str:
        return store_locale(self.lang)


def region_from_userdata(data: Any, ui_lang: str) -> Region:
    """Land und Währung aus userData.json (Gast oder angemeldet), Sprache aus der Oberfläche."""
    d = data if isinstance(data, dict) else {}
    cur = d.get("selectedCurrency")
    code = cur.get("code") if isinstance(cur, dict) else cur
    return Region(str(d.get("country") or "").upper(), str(code or "").upper(), store_lang(ui_lang))


@dataclass
class Product:
    id: str
    title: str
    slug: str = ""
    product_type: str = "game"
    cover: str = ""            # Hochkant (coverVertical)
    image: str = ""            # Querformat (coverHorizontal bzw. logo2x)
    store_link: str = ""
    state: str = "default"     # default | coming-soon | early-access …
    price: Price | None = None
    price_text: str = ""       # Preistext des Katalogs, falls kein Betrag lesbar ist

    @property
    def store_path(self) -> str:
        """Produktseite für den eingebetteten Store."""
        return self.store_link or (f"/game/{self.slug}" if self.slug else "/")


def parse_product(p: Any) -> Product | None:
    if not isinstance(p, dict) or not p.get("id") or not p.get("title"):
        return None
    price = p.get("price") if isinstance(p.get("price"), dict) else None
    return Product(id=str(p["id"]), title=str(p["title"]), slug=str(p.get("slug") or ""),
                   product_type=str(p.get("productType") or "game"), cover=str(p.get("coverVertical") or ""),
                   image=str(p.get("coverHorizontal") or ""), store_link=str(p.get("storeLink") or ""),
                   state=str(p.get("productState") or "default"), price=catalog_price(price),
                   price_text=str((price or {}).get("final") or ""))


def parse_catalog(data: Any) -> list[Product]:
    """``products[]`` einer Katalog-Antwort (ohne doppelte und unlesbare Einträge)."""
    out, seen = [], set()
    for p in (data.get("products") if isinstance(data, dict) else None) or []:
        prod = parse_product(p)
        if prod is not None and prod.id not in seen:
            seen.add(prod.id)
            out.append(prod)
    return out


def product_count(data: Any) -> int:
    try:
        return int((data or {}).get("productCount") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def dlc_filter_applied(filtered: int, total: int) -> bool:
    """Wirkt ``onlyDlcForOwned``? Nur wenn weniger DLCs kommen als mit sonst gleicher Abfrage ohne den Filter
    (ignorierte Filter liefern dieselbe Liste)."""
    return total > 0 and 0 <= filtered < total


# --------------------------------------------------------------------------- Regale
@dataclass(frozen=True)
class Shelf:
    key: str
    title: str                       # N_-Text
    kind: str = "catalog"            # catalog | wishlist | dlc
    params: dict = field(default_factory=dict)
    more: str = ""                   # „Alle anzeigen“ im Store
    login: bool = False              # nur angemeldet

    @property
    def label(self) -> str:
        return tr(self.title)


SHELVES: tuple[Shelf, ...] = (
    Shelf("for_you", N_("Spiele für dich"), params={"order": "desc:trending", "productType": GAMES}, more="/games"),
    Shelf("new", N_("Neuerscheinungen"),
          params={"order": "desc:trending", "productType": GAMES, "releaseStatuses": "in:new-arrival"},
          more="/games?releaseStatuses=new-arrival&order=desc:trending"),
    Shelf("deals", N_("Angebote"), params={"order": "desc:trending", "productType": GAMES, "discounted": "eq:true"},
          more=STORE_SECTIONS["deals"]),
    Shelf("wishlist", N_("Aus deiner Wunschliste"), kind="wishlist", more=STORE_SECTIONS["wishlist"], login=True),
    Shelf("dlc", N_("Zusätzliche Inhalte für deine Spiele"), kind="dlc",
          params={"order": "desc:trending", "productType": "in:dlc", "onlyDlcForOwned": "true"}, login=True),
    Shelf("classics", N_("Good Old Games"),
          params={"order": "desc:trending", "productType": GAMES, "tags": "in:good-old-game"},
          more="/games?tags=good-old-game&order=desc:trending"),
)


def shelf(key: str) -> Shelf:
    return next(s for s in SHELVES if s.key == key)


def catalog_params(sh: Shelf, region: Region, logged_in: bool, limit: int = SHELF_LIMIT) -> dict[str, Any]:
    """Abfrage eines Katalog-Regals; angemeldet ohne Spiele im Besitz (``hideOwned``, braucht den Bearer)."""
    params: dict[str, Any] = {"limit": limit, **sh.params, "locale": region.locale}
    if region.country:
        params["countryCode"] = region.country
    if region.currency:
        params["currencyCode"] = region.currency
    if logged_in:
        params["hideOwned"] = "true"
    else:
        params.pop("onlyDlcForOwned", None)
    return params


def dedupe_shelves(raw: dict[str, list[Product]], order: Iterable[str], show: int = SHELF_SHOW,
                   keep: Iterable[str] = ("wishlist",)) -> dict[str, list[Product]]:
    """Was jedes Regal zeigt: ein Spiel erscheint nur im ersten Regal (in ``order``), das es enthält – „Angebote“
    wiederholt also nicht „Spiele für dich“. Regale in ``keep`` (die Wunschliste) zeigen alles und zählen nicht mit.
    Noch nicht geladene Regale (fehlen in ``raw``) blockieren nichts."""
    keep = set(keep)
    seen: set[str] = set()
    out: dict[str, list[Product]] = {}
    for key in order:
        if key not in raw:
            continue
        if key in keep:
            out[key] = list(raw[key])
            continue
        items = [p for p in raw[key] if p.id not in seen][:show]
        seen.update(p.id for p in items)
        out[key] = items
    return out


def sort_wishlist(products: list[Product]) -> list[Product]:
    """Wunschlisten-Regal: Rabatte zuerst (größter vorn), dann erhältliche, zuletzt „Demnächst“; sonst nach Titel."""
    def key(p: Product) -> tuple:
        return (-(p.price.discount if p.price else -1), p.price is None, p.title.lower())
    return sorted(products, key=key)


class DiscoverService:
    """Abrufe für die Seite „Entdecken“ (Arbeits-Thread). ``shelf()`` liefert Produkte, ``[]`` = Regal entfällt;
    ohne Netz und ohne früheren Stand wirft es ApiError."""

    def __init__(self, api: Any, clock: Callable[[], float] = time.time) -> None:
        self.api = api
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, Any]] = {}

    # Zwischenspeicher
    def _get(self, key: tuple, ttl: float) -> tuple[bool, Any]:
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and self.clock() - hit[0] < ttl:
            return True, hit[1]
        return False, hit[1] if hit is not None else None

    def _put(self, key: tuple, value: Any) -> Any:
        with self._lock:
            self._cache[key] = (self.clock(), value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # Region
    def region(self, ui_lang: str) -> Region:
        """Land/Währung: angemeldet aus den zwischengespeicherten Nutzerdaten, sonst Gast-userData.json."""
        user = getattr(self.api, "user", None)
        if getattr(self.api, "logged_in", False) and isinstance(user, dict) and user.get("country"):
            return region_from_userdata(user, ui_lang)
        fresh, cached = self._get(("region",), REGION_TTL)
        if not fresh:
            try:
                r = self.api.get(f"{EMBED}/userData.json", authenticated=False, timeout=10, retries=0)
                data = r.json() if r.ok else None
                if isinstance(data, dict):
                    cached = self._put(("region",), data)
            except (ApiError, ValueError) as exc:
                log.debug("userData.json (Gast) nicht abrufbar: %s", exc)
        return region_from_userdata(cached or {}, ui_lang)

    # Katalog
    def _catalog(self, params: dict[str, Any], auth: bool) -> dict:
        r = self.api.get(CATALOG_URL, params=params, authenticated=auth, timeout=15, retries=1)
        if not r.ok:
            raise ApiError(tr('HTTP {0} für {1}').format(r.status_code, "catalog.gog.com"))
        try:
            data = r.json()
        except ValueError as exc:
            raise ApiError(tr('Ungültige JSON-Antwort von {0}').format("catalog.gog.com")) from exc
        if not isinstance(data, dict):
            raise ApiError(tr('Ungültige JSON-Antwort von {0}').format("catalog.gog.com"))
        return data

    def _catalog_shelf(self, sh: Shelf, region: Region, logged_in: bool) -> list[Product]:
        if logged_in:
            try:
                return parse_catalog(self._catalog(catalog_params(sh, region, True), True))
            except NotLoggedIn:
                pass
            except (ApiError, AuthError) as exc:   # Token abgelaufen o. Ä.: Gast-Abfrage (mit Spielen im Besitz)
                log.debug("Katalog mit Anmeldung fehlgeschlagen (%s) – ohne", exc)
        return parse_catalog(self._catalog(catalog_params(sh, region, False), False))

    def _dlc_shelf(self, sh: Shelf, region: Region) -> list[Product]:
        try:
            data = self._catalog(catalog_params(sh, region, True), True)
        except NotLoggedIn:
            return []
        # Vergleich unter gleichen Bedingungen (angemeldet, hideOwned), nur ohne onlyDlcForOwned
        ck = ("dlc-total", region, getattr(self.api, "user_id", ""))
        fresh, total = self._get(ck, 86400)
        if not fresh:
            plain = catalog_params(sh, region, True, limit=1)
            plain.pop("onlyDlcForOwned", None)
            total = self._put(ck, product_count(self._catalog(plain, True)))
        filtered = product_count(data)
        if not dlc_filter_applied(filtered, int(total or 0)):
            log.info("Katalog: onlyDlcForOwned wirkt nicht (%d von %d DLCs) – Regal entfällt", filtered, total or 0)
            return []
        return parse_catalog(data)

    def _wishlist_shelf(self, region: Region) -> list[Product]:
        try:
            ids = wl.fetch_ids(self.api)
        except NotLoggedIn:
            return []
        ids = ids[:WISHLIST_SHELF_MAX * 2]
        if not ids:
            return []
        prices = wl.fetch_prices(self.api, ids, region.country, region.currency)
        info = wl.fetch_products(self.api, ids, region.locale)
        out = []
        for pid in ids:
            i = info.get(pid) or {}
            if not i.get("title"):
                continue
            out.append(Product(id=pid, title=i["title"], slug=i.get("slug", ""), image=i.get("image", ""),
                               store_link=i.get("link", ""), price=prices.get(pid),
                               state="default" if pid in prices else "coming-soon"))
        return sort_wishlist(out)[:WISHLIST_SHELF_MAX]

    def shelf(self, key: str, ui_lang: str, force: bool = False) -> list[Product]:
        sh = shelf(key)
        logged_in = bool(getattr(self.api, "logged_in", False))
        if sh.login and not logged_in:
            return []
        region = self.region(ui_lang)
        ck = ("shelf", key, region, logged_in)
        fresh, cached = self._get(ck, CACHE_TTL)
        if fresh and not force:
            return cached
        try:
            if sh.kind == "wishlist":
                items = self._wishlist_shelf(region)
            elif sh.kind == "dlc":
                items = self._dlc_shelf(sh, region)
            else:
                items = self._catalog_shelf(sh, region, logged_in)
        except (ApiError, AuthError) as exc:
            if cached is not None:           # offline: letzter Stand bleibt stehen
                return cached
            if isinstance(exc, ApiError):
                raise
            raise ApiError(str(exc)) from exc
        return self._put(ck, items)
