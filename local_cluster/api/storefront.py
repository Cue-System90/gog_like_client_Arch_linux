"""Kleine Store-Abfragen für die Oberfläche: Wunschlisten-Zähler (Bearer) und Produktseite eines Spiels.

Die Seiten selbst zeigt ``ui/store.py`` als eingebettete gog.com-Website; hier nur die nativen Ergänzungen.
"""
from __future__ import annotations

import logging
import threading

from ..store_links import parse_wishlist_count, store_href_from_v2
from .client import API, EMBED, ApiError

log = logging.getLogger(__name__)

_links: dict[tuple[str, str], str] = {}
_lock = threading.Lock()


def wishlist_count(api) -> int:
    """Anzahl der Spiele auf der Wunschliste (``embed.gog.com/user/wishlist.json`` mit Bearer-Token).
    Wirft NotLoggedIn/ApiError wie ``GogApi.get_json``."""
    return parse_wishlist_count(api.get_json(f"{EMBED}/user/wishlist.json", timeout=15, retries=1))


def product_store_url(api, product_id: str, locale: str = "en-US") -> str:
    """Lokalisierte Produktseite aus ``api.gog.com/v2/games/<id>`` (``_links.store.href``); "" wenn unbekannt.
    Ergebnisse werden pro Prozess zwischengespeichert."""
    key = (str(product_id), locale)
    with _lock:
        if key in _links:
            return _links[key]
    href = ""
    try:
        r = api.get(f"{API}/v2/games/{product_id}", params={"locale": locale}, authenticated=False, timeout=10, retries=0)
        if r.ok:
            href = store_href_from_v2(r.json())
        else:
            log.debug("Produktseite für %s nicht gefunden (HTTP %s)", product_id, r.status_code)
    except (ApiError, ValueError) as exc:
        log.debug("Produktseite für %s nicht abrufbar: %s", product_id, exc)
    if href:
        with _lock:
            _links[key] = href
    return href
