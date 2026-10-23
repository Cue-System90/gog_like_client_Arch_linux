"""Benachrichtigungszentrum wie in GOG Galaxy 2.1 (Glocke in der Kopfzeile) – Datenhaltung und Regeln ohne GTK.

Jede Meldung hat eine Art (``kind``: download, update, redist, library, cloud, wishlist, available …), Titel, Text,
Zeitpunkt, gelesen/ungelesen und eine Aktion, die ein Klick ausführt (``game:<id>``, ``store:<pfad>``,
``view:<ansicht>``, ``updates``, ``prefs:<seite>``). Gespeichert wird in ``~/.local/share/galaxy-linux/
notifications.json``: höchstens 200 Meldungen, keine älter als 30 Tage.

Pro Benachrichtigungstyp (Einstellungen → Benachrichtigungen) gilt wie in Galaxy eine Auswahl. Galaxy kennt
„Desktop & Overlay“, „Desktop“, „Overlay“ und „—“; ein Galaxy-Overlay gibt es unter Linux nicht, das
Benachrichtigungszentrum übernimmt dessen Rolle: „Desktop und Benachrichtigungszentrum“ (``desktop``),
„Nur Benachrichtigungszentrum“ (``center``) und „—“ (``none``). Desktop-Meldungen (Gio.Notification) erscheinen nur,
solange das Fenster nicht im Vordergrund ist – außer bei Typen, die das ausdrücklich verlangen (``focus_desktop``).

Weitere Typen (z. B. Freunde) meldet ``register_type`` an; die Einstellungsseite baut ihre Zeilen daraus.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

from .i18n import N_
from .jsonstore import read_json, write_json

log = logging.getLogger(__name__)

MAX_ITEMS = 200
MAX_AGE_DAYS = 30

MODE_BOTH, MODE_CENTER, MODE_NONE = "desktop", "center", "none"
MODES = (MODE_BOTH, MODE_CENTER, MODE_NONE)
MODE_NAMES = {MODE_BOTH: N_("Desktop und Benachrichtigungszentrum"), MODE_CENTER: N_("Nur Benachrichtigungszentrum"),
              MODE_NONE: "—"}


@dataclass
class NotificationType:
    key: str                 # downloads | wishlist | library | cloud | friends …
    title: str               # deutscher Text (N_), wird beim Anzeigen übersetzt
    setting: str             # Feldname in GlobalSettings
    default: str = MODE_BOTH
    order: int = 100
    focus_desktop: bool = False     # Desktop-Meldung auch bei aktivem Fenster
    subtitle: str = ""              # Erklärung in den Einstellungen (N_)


_types: dict[str, NotificationType] = {}
_types_lock = threading.Lock()

# Art einer Meldung → Typ der Einstellung
KIND_TYPE = {"download": "downloads", "update": "downloads", "redist": "downloads", "library": "library",
             "cloud": "cloud", "wishlist": "wishlist", "available": "wishlist"}


def register_type(t: NotificationType) -> None:
    """Benachrichtigungstyp anmelden (gleicher Schlüssel ersetzt)."""
    with _types_lock:
        _types[t.key] = t


def register_kind(kind: str, type_key: str) -> None:
    """Weitere Meldungsart einem Typ zuordnen (z. B. ``friend-online`` → ``friends``)."""
    KIND_TYPE[kind] = type_key


def types() -> list[NotificationType]:
    with _types_lock:
        items = list(_types.values())
    return sorted(items, key=lambda t: (t.order, t.key))


def type_for_kind(kind: str) -> NotificationType | None:
    with _types_lock:
        return _types.get(KIND_TYPE.get(kind, kind))


def mode_for(settings: Any, kind: str) -> str:
    """Eingestellte Auswahl für die Art einer Meldung (unbekannte Werte → Vorgabe des Typs)."""
    t = type_for_kind(kind)
    if t is None:
        return MODE_BOTH
    value = getattr(settings, t.setting, t.default)
    return value if value in MODES else t.default


def route(mode: str, window_focused: bool, focus_desktop: bool = False) -> tuple[bool, bool]:
    """(ins Benachrichtigungszentrum, auf den Desktop) für eine Auswahl und den Fensterzustand."""
    if mode == MODE_NONE:
        return False, False
    desktop = mode == MODE_BOTH and (not window_focused or focus_desktop)
    return True, desktop


register_type(NotificationType(
    "downloads", N_("Installation/Update"), "notify_download_status", order=10,
    subtitle=N_("Downloads fertig oder fehlgeschlagen, Updates verfügbar, Probleme mit Galaxy-Redistributables")))
register_type(NotificationType("wishlist", N_("Wunschliste"), "notify_wishlist", order=20,
                               subtitle=N_("Rabatte und neu erhältliche Spiele deiner Wunschliste")))
register_type(NotificationType("library", N_("Neue Spiele in der Bibliothek"), "notify_new_games", order=30,
                               subtitle=N_("Käufe und eingelöste Codes, sobald der Abgleich sie findet")))
register_type(NotificationType("cloud", N_("Cloud-Spielstände"), "notify_cloud_saves", order=40,
                               subtitle=N_("Konflikte, überschrittenes Kontingent und Fehler beim Synchronisieren")))


# --------------------------------------------------------------------------- Speicher
@dataclass
class Notification:
    id: str
    kind: str
    title: str
    body: str = ""
    created: float = 0.0
    read: bool = False
    action: str = ""
    product_id: str = ""
    key: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> "Notification | None":
        if not isinstance(d, dict) or not d.get("id") or not d.get("title"):
            return None
        names = {f.name for f in fields(cls)}
        try:
            n = cls(**{"kind": "", **{k: v for k, v in d.items() if k in names}})
            n.created = float(n.created or 0)
            n.read = bool(n.read)
            return n
        except (TypeError, ValueError):
            return None


class NotificationStore:
    """Meldungen des Benachrichtigungszentrums, neueste zuerst. ``key`` verhindert Doppelte: eine Meldung mit
    bekanntem Schlüssel (auch einer schon gelöschten, innerhalb der 30 Tage) wird nicht noch einmal angelegt."""

    def __init__(self, path: Path, max_items: int = MAX_ITEMS, max_age_days: int = MAX_AGE_DAYS,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.max_age = max_age_days * 86400
        self.clock = clock
        self._lock = threading.RLock()
        self.listeners: list[Callable[[], None]] = []
        raw = read_json(self.path, {})
        raw = raw if isinstance(raw, dict) else {}
        self._items = [n for n in (Notification.from_dict(d) for d in raw.get("items") or []) if n is not None]
        keys = raw.get("keys")
        self._keys: dict[str, float] = {str(k): float(v) for k, v in keys.items()
                                        if isinstance(v, (int, float)) and not isinstance(v, bool)} \
            if isinstance(keys, dict) else {}
        self._items.sort(key=lambda n: -n.created)
        if self._prune():
            self._save()

    # Lesen
    def items(self) -> list[Notification]:
        with self._lock:
            if self._prune():
                self._save()
            return list(self._items)

    def get(self, nid: str) -> Notification | None:
        with self._lock:
            return next((n for n in self._items if n.id == nid), None)

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for n in self._items if not n.read)

    # Schreiben
    def add(self, kind: str, title: str, body: str = "", action: str = "", product_id: str = "", key: str = "",
            now: float | None = None) -> Notification | None:
        now = self.clock() if now is None else now
        with self._lock:
            self._prune(now)
            if key and key in self._keys:
                return None
            n = Notification(id=uuid.uuid4().hex[:12], kind=kind, title=title, body=body, created=now, action=action,
                             product_id=str(product_id or ""), key=key)
            self._items.insert(0, n)
            if key:
                self._keys[key] = now
            self._prune(now)
            self._save()
        self._changed()
        return n

    def mark_read(self, nid: str | None = None) -> int:
        """Eine (``nid``) oder alle Meldungen als gelesen markieren; liefert die Zahl der geänderten."""
        with self._lock:
            hits = [n for n in self._items if not n.read and (nid is None or n.id == nid)]
            for n in hits:
                n.read = True
            if hits:
                self._save()
        if hits:
            self._changed()
        return len(hits)

    def remove(self, nid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [n for n in self._items if n.id != nid]
            changed = len(self._items) != before
            if changed:
                self._save()
        if changed:
            self._changed()
        return changed

    def clear(self) -> int:
        """„Alle löschen“: Meldungen weg, bekannte Schlüssel bleiben (nichts kommt doppelt wieder)."""
        with self._lock:
            n = len(self._items)
            self._items = []
            self._save()
        self._changed()
        return n

    # intern
    def _prune(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        limit = now - self.max_age
        items = [n for n in self._items if n.created >= limit][: self.max_items]
        keys = {k: v for k, v in self._keys.items() if v >= limit}
        changed = len(items) != len(self._items) or len(keys) != len(self._keys)
        self._items, self._keys = items, keys
        return changed

    def _save(self) -> None:
        try:
            write_json(self.path, {"items": [asdict(n) for n in self._items], "keys": self._keys})
        except OSError as exc:
            log.warning("Benachrichtigungen nicht gespeichert: %s", exc)

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # pragma: no cover
                log.exception("Benachrichtigungs-Listener fehlgeschlagen")
