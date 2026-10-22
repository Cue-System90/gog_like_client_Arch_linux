"""Adressen des GOG-Stores: Pfade wie ``/game/<slug>`` oder ``/redeem`` in eine lokalisierte gog.com-Adresse wandeln.

Reine Hilfsfunktionen (ohne GTK); ``MainWindow.open_store(path)`` nutzt sie.
"""
from __future__ import annotations

from urllib.parse import urlsplit

STORE_HOST = "https://www.gog.com"
STORE_HOSTS = {"www.gog.com", "gog.com", "embed.gog.com"}
STORE_LANGS = {"en", "de", "fr", "pl", "ru", "zh", "es", "it", "pt", "ja", "ko", "tr", "cs", "hu", "nl", "sv", "uk"}


def store_lang(ui_lang: str) -> str:
    """Sprache der Store-Seiten passend zur Oberfläche: Deutsch → de, sonst en."""
    return "de" if (ui_lang or "").lower().startswith("de") else "en"


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
