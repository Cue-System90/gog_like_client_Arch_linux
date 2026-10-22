"""Adressen des GOG-Stores: Pfade wie ``/game/<slug>`` oder ``/redeem`` in eine lokalisierte gog.com-Adresse wandeln.

Dazu die reinen Regeln des eingebetteten Stores (``ui/store.py``): Seitenleisten-Einträge, Kaufabschluss-Erkennung,
Einordnung neuer Fenster und Links, Wunschlisten-Zähler, Download-Dateinamen. Alles ohne GTK und ohne Netz.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STORE_HOST = "https://www.gog.com"
STORE_HOSTS = {"www.gog.com", "gog.com", "embed.gog.com"}
STORE_LANGS = {"en", "de", "fr", "pl", "ru", "zh", "es", "it", "pt", "ja", "ko", "tr", "cs", "hu", "nl", "sv", "uk"}

# Seitenleiste „GOG STORE“ wie im Original (Schlüssel → Pfad); Beschriftungen setzt ui/store.py
STORE_SECTIONS: dict[str, str] = {
    "home": "/",
    "games": "/games",
    "deals": "/games?order=desc:discount&discounted=true&hideOwned=true",
    "wishlist": "/account/wishlist",
    "cart": "/checkout",
    "orders": "/account/settings/orders",
}
# Abschlussseite des Checkouts, auch mit Sprachsegment: /de/checkout/<publicOrderId>/complete
_COMPLETE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?checkout/([A-Za-z0-9._-]+)/complete/?$", re.IGNORECASE)
_SECRET_QUERY = {"code", "token", "access_token", "refresh_token", "auth", "session"}


def store_lang(ui_lang: str) -> str:
    """Sprache der Store-Seiten passend zur Oberfläche: Deutsch → de, sonst en."""
    return "de" if (ui_lang or "").lower().startswith("de") else "en"


def store_locale(lang: str) -> str:
    """Locale für api.gog.com (``de`` → ``de-DE``, sonst ``en-US``)."""
    return "de-DE" if lang == "de" else "en-US"


def is_store_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in ("http", "https") and parts.hostname in STORE_HOSTS


def store_path(url_or_path: str) -> str:
    """Pfad ohne Host und Sprachpräfix: ``https://www.gog.com/en/game/x?a=1`` → ``/game/x?a=1``, ``""`` → ``/``.
    Adressen fremder Hosts bleiben unverändert."""
    text = (url_or_path or "").strip()
    if text.startswith(("http://", "https://", "//")):
        if text.startswith("//"):
            text = "https:" + text
        if not is_store_url(text):
            return text
        parts = urlsplit(text)
        text = parts.path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    if not text.startswith("/"):
        text = "/" + text
    seg = text[1:].split("/", 1)
    head = seg[0].split("?", 1)[0].split("#", 1)[0]
    if head.lower() in STORE_LANGS:
        rest = text[1 + len(head):]
        text = rest if rest.startswith("/") else "/" + rest.lstrip("/")
    return text or "/"


def store_url(url_or_path: str, lang: str) -> str:
    """Vollständige Store-Adresse in der gewünschten Sprache (fremde Hosts unverändert)."""
    path = store_path(url_or_path)
    if not path.startswith("/"):
        return path
    return f"{STORE_HOST}/{lang}{path}"


def _split(url_or_path: str) -> tuple[str, dict[str, list[str]]] | None:
    """(Pfad ohne Sprache, Query) einer Store-Adresse; None für fremde Hosts."""
    text = store_path(url_or_path)
    if not text.startswith("/"):
        return None
    parts = urlsplit(text)
    return (parts.path.rstrip("/") or "/"), parse_qs(parts.query)


def section_for_path(url_or_path: str) -> str | None:
    """Seitenleisten-Eintrag zu einer Store-Adresse (``home``, ``games``, ``deals``, ``wishlist``, ``cart``, ``orders``)."""
    sp = _split(url_or_path)
    if sp is None:
        return None
    path, query = sp
    if path == "/":
        return "home"
    if path == "/games" or path.startswith("/games/"):
        disc = [v.lower() for v in query.get("discounted", [])]
        return "deals" if any(v in ("true", "eq:true", "1") for v in disc) else "games"
    if path == "/account/wishlist" or path.startswith("/account/wishlist/"):
        return "wishlist"
    if path == "/checkout" or path.startswith("/checkout/"):
        return "cart"
    if path.startswith(("/account/settings/orders", "/galaxy/v2/account/settings/orders")):
        return "orders"
    return None


def checkout_order_id(url: str) -> str | None:
    """``publicOrderId`` einer Kaufabschluss-Seite (``/[<sprache>/]checkout/<id>/complete``), sonst None.
    Volle Adressen zählen nur auf den Store-Hosts."""
    text = (url or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://", "//")):
        if text.startswith("//"):
            text = "https:" + text
        if not is_store_url(text):
            return None
        path = urlsplit(text).path
    elif "://" in text or ":" in text.split("/", 1)[0]:
        return None
    else:
        path = text.split("?", 1)[0].split("#", 1)[0]
    m = _COMPLETE.match(path)
    if not m or m.group(1).lower() == "manual":
        return None
    return m.group(1)


def rememberable_url(url: str) -> bool:
    """Darf als „zuletzt besuchte Store-Seite“ gespeichert werden? Nur Store-Hosts, kein Checkout/Login,
    keine Codes oder Tokens in der Adresse."""
    if not is_store_url(url):
        return False
    sp = _split(url)
    if sp is None:
        return False
    path, query = sp
    if path.startswith(("/checkout", "/on_login_success", "/login", "/logout")):
        return False
    return not any(k.lower() in _SECRET_QUERY for k in query)


def store_login_url(current: str, lang: str) -> str:
    """Anmeldung der Website selbst: aktuelle Store-Seite (oder Startseite) mit ``##openlogin``."""
    base = current.split("#", 1)[0] if rememberable_url(current) else store_url("/", lang)
    return base + "##openlogin"


def classify_navigation(url: str) -> str:
    """Navigation im Store-Fenster: ``allow`` (normal laden), ``galaxy`` (goggalaxy://-Link), ``browser``
    (mailto:, steam: … an das System) oder ``ignore``."""
    scheme = urlsplit((url or "").strip()).scheme.lower()
    if scheme in ("http", "https", "about", "blob", "data", ""):
        return "allow"
    if scheme == "goggalaxy":
        return "galaxy"
    if scheme in ("javascript", "file"):
        return "ignore"
    return "browser"


def classify_new_window(url: str, link_clicked: bool) -> str:
    """Neues Fenster aus dem Store: ``popup`` (eigenes Fenster in derselben Sitzung – Zahlungsanbieter, PayPal),
    ``store`` (Link mit target=_blank auf gog.com → im Store öffnen), ``browser`` (Link auf fremde Seiten oder
    fremdes Schema → System-Browser) oder ``ignore``."""
    text = (url or "").strip()
    if not text or text.startswith("about:blank"):
        return "popup"
    scheme = urlsplit(text).scheme.lower()
    if scheme in ("javascript", "data", "file"):
        return "ignore"
    if scheme in ("blob", "about"):
        return "popup"
    if scheme not in ("http", "https"):
        return "browser"
    if link_clicked:
        return "store" if is_store_url(text) else "browser"
    return "popup"


def response_is_download(mime_supported: bool, disposition: str = "") -> bool:
    """Antwort speichern statt anzeigen: Typ, den die Web-Ansicht nicht darstellt (Offline-Installer, Goodies-ZIP),
    oder ``Content-Disposition: attachment``."""
    return not mime_supported or (disposition or "").strip().lower().startswith("attachment")


def galaxy_link_product(url: str) -> str | None:
    """Produkt-ID aus ``goggalaxy://openGameView/<id>`` (auch ``gog_<id>``), sonst None."""
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() != "goggalaxy":
        return None
    segs = [s for s in (parts.netloc + parts.path).split("/") if s]
    if len(segs) < 2:
        return None
    pid = segs[-1]
    if pid.lower().startswith("gog_"):
        pid = pid[4:]
    return pid if pid.isdigit() else None


def parse_wishlist_count(data) -> int:
    """Anzahl der Spiele aus ``embed.gog.com/user/wishlist.json`` (``{"wishlist": {"<id>": true, …}}``)."""
    if not isinstance(data, dict):
        return 0
    wl = data.get("wishlist", data)
    if isinstance(wl, dict):
        return sum(1 for k, v in wl.items() if v and str(k).isdigit())
    if isinstance(wl, list):
        return sum(1 for v in wl if v)
    return 0


def store_href_from_v2(data) -> str:
    """Produktseite aus ``api.gog.com/v2/games/<id>`` (``_links.store.href``), sonst ""."""
    try:
        href = data["_links"]["store"]["href"]
    except (KeyError, TypeError):
        return ""
    return href if isinstance(href, str) and is_store_url(href) else ""


def product_fallback(meta_url: str = "", entry_url: str = "", slug: str = "") -> str:
    """Produktseite ohne Netz: Produktkarte aus den Metadaten, Bibliotheks-Adresse, ``/game/<slug>``, sonst ``/``."""
    for url in (meta_url, entry_url):
        if url and (url.startswith("/") or is_store_url(url)):
            return url
    return f"/game/{slug}" if slug else "/"


def parse_page_message(text: str) -> dict | None:
    """Nachricht des Seiten-Skripts prüfen: ``{"type": "nav", "url": …}`` oder ``{"type": "user", "loggedIn": bool}``."""
    try:
        msg = json.loads(text) if isinstance(text, str) and len(text) < 8192 else None
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") == "nav" and isinstance(msg.get("url"), str):
        return {"type": "nav", "url": msg["url"]}
    if msg.get("type") == "user" and isinstance(msg.get("loggedIn"), bool):
        return {"type": "user", "loggedIn": msg["loggedIn"]}
    return None


def safe_download_name(suggested: str) -> str:
    """Dateiname eines Downloads ohne Pfadanteile und Steuerzeichen."""
    name = Path((suggested or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip().lstrip(".").strip()
    return name[:200] or "download"


def unique_download_path(directory: Path, suggested: str) -> Path:
    """Freier Zielpfad im Download-Ordner: ``setup.exe``, ``setup (1).exe``, …"""
    name = safe_download_name(suggested)
    target = Path(directory) / name
    p = Path(name)
    stem, suffix = (p.stem, p.suffix) if p.suffix and p.stem else (name, "")
    n = 1
    while target.exists():
        target = Path(directory) / f"{stem} ({n}){suffix}"
        n += 1
    return target
