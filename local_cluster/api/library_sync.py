"""Bibliotheks-Abgleich mit GOG – Ersatz für Galaxys Push-Nachricht „user library updated“ (per Abfrage).

Ablauf eines Abgleichs (gog-apis.md §4, original-galaxy.md §7):

1. ``embed.gog.com/userData.json`` jedes Mal neu holen (Name, Avatar, Prüfsummen).
2. ``checksum.games`` als billiger Vorfilter: unverändert und nicht erzwungen → kein Vollabruf.
3. Vollabruf ``getFilteredProducts`` mit ``hiddenFlag=0`` und ``hiddenFlag=1`` (versteckte Spiele), zusammengeführt.
4. Unterschied zur bisherigen Bibliothek: neue IDs bekommen 14 Tage lang die Markierung „NEU“
   (``settings.library_new_ids``), Besitzliste des Installers und Metadaten-Caches werden geleert.

Neu gesichtet, aber versteckt (z. B. beim ersten Abgleich nach dem Update auf 0.2.0, der die versteckten Spiele
erstmals lädt) zählt nicht als Neukauf. Ist die bisherige Bibliothek leer (erster Login), gibt es keine Markierungen.

Die reinen Hilfen (ohne GTK und Netz) sind getrennt testbar; ``sync()`` braucht ein Backend.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .models import LibraryEntry

log = logging.getLogger(__name__)

NEW_KEEP_DAYS = 14          # so lange bleibt „NEU“ stehen (oder bis die Spielansicht geöffnet wurde)
FOCUS_MIN_GAP = 300         # s: frühestens so lange nach dem letzten Abgleich beim Wechsel ins Fenster
STARTUP_DELAY = 3           # s: nach dem Start erst die Bibliothek aus dem Cache zeigen
TIMER_TICK = 60             # s: Prüftakt des Intervall-Abgleichs
PURCHASE_DELAYS = (5, 15, 60)   # s: Wiederholungen nach einem Kauf, bis die neuen Spiele auftauchen
PURCHASE_REASONS = ("purchase", "purchase-retry")
NEW_SECTION_MAX = 12


@dataclass
class SyncResult:
    new_ids: list[str] = field(default_factory=list)       # neu in der Bibliothek (sichtbar, ohne ersten Abruf)
    removed_ids: list[str] = field(default_factory=list)   # nicht mehr in der Bibliothek
    total: int = 0
    skipped: bool = False                                  # Prüfsumme unverändert → kein Vollabruf
    reason: str = ""
    new_titles: list[str] = field(default_factory=list)
    first: bool = False                                    # bisherige Bibliothek war leer (Login, leerer Cache)

    @property
    def changed(self) -> bool:
        return bool(self.new_ids or self.removed_ids or self.first)


# --------------------------------------------------------------------------- reine Hilfen
def games_checksum(user: Any) -> str:
    """``checksum.games`` aus userData.json ("" bei Gast-Daten oder fehlendem Feld)."""
    cs = user.get("checksum") if isinstance(user, dict) else None
    val = cs.get("games") if isinstance(cs, dict) else None
    return str(val) if val else ""


def should_skip(checksum: str, stored: str, force: bool, have_library: bool) -> bool:
    """Vollabruf sparen: nur wenn nichts erzwungen ist, eine Bibliothek vorliegt und die Prüfsumme gleich blieb."""
    return bool(not force and have_library and checksum and checksum == stored)


def diff_ids(old: Iterable[Any], new: Iterable[Any]) -> tuple[list[str], list[str]]:
    """(neu, entfernt) in der Reihenfolge der jeweiligen Liste, ohne Doppelte."""
    old_l = list(dict.fromkeys(str(i) for i in old))
    new_l = list(dict.fromkeys(str(i) for i in new))
    old_s, new_s = set(old_l), set(new_l)
    return [i for i in new_l if i not in old_s], [i for i in old_l if i not in new_s]


def merge_hidden(visible: Iterable[LibraryEntry], hidden: Iterable[LibraryEntry]) -> list[LibraryEntry]:
    """Durchlauf hiddenFlag=0 und hiddenFlag=1 zusammenführen; Einträge nur aus dem zweiten gelten als versteckt."""
    out: dict[str, LibraryEntry] = {}
    for e in visible:
        out.setdefault(e.id, e)
    for e in hidden:
        if e.id not in out:
            e.is_hidden = True
            out[e.id] = e
    return sorted(out.values(), key=lambda e: (e.title.lower(), e.id))


def prune_new(marks: Any, now: float, keep_days: int = NEW_KEEP_DAYS) -> dict[str, float]:
    """Abgelaufene (und unlesbare) NEU-Markierungen entfernen."""
    limit = now - keep_days * 86400
    out: dict[str, float] = {}
    for k, v in (marks.items() if isinstance(marks, dict) else ()):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) >= limit:
            out[str(k)] = float(v)
    return out


def record_new(marks: dict[str, float], ids: Iterable[Any], now: float) -> dict[str, float]:
    """Erste Sichtung neuer IDs merken (vorhandene Zeitpunkte bleiben)."""
    out = dict(marks)
    for i in ids:
        out.setdefault(str(i), float(now))
    return out


def clear_new(marks: dict[str, float], product_id: Any) -> dict[str, float]:
    return {k: v for k, v in marks.items() if k != str(product_id)}


def is_new(marks: Any, product_id: Any, now: float | None = None, keep_days: int = NEW_KEEP_DAYS) -> bool:
    t = marks.get(str(product_id)) if isinstance(marks, dict) else None
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return False
    now = time.time() if now is None else now
    return now - float(t) < keep_days * 86400


def new_order(marks: dict[str, float], ids: Iterable[Any], now: float | None = None,
              limit: int = NEW_SECTION_MAX) -> list[str]:
    """IDs mit NEU-Markierung, zuletzt gesichtete zuerst (für „Neu in der Bibliothek“)."""
    hits = [str(i) for i in dict.fromkeys(ids) if is_new(marks, i, now)]
    hits.sort(key=lambda i: -marks[i])
    return hits[:limit]


def new_titles_text(titles: Iterable[str], limit: int = 3) -> str:
    """„A, B, C …“ – höchstens ``limit`` Titel."""
    t = [x for x in titles if x]
    return ", ".join(t[:limit]) + (" …" if len(t) > limit else "")


def interval_due(last: float, now: float, minutes: int) -> bool:
    """Intervall-Abgleich fällig? ``minutes`` <= 0 = aus."""
    return minutes > 0 and now - last >= minutes * 60


def focus_due(last: float, now: float, gap: float = FOCUS_MIN_GAP) -> bool:
    return now - last >= gap


class SyncGate:
    """Nur ein Abgleich zur Zeit. Ein erzwungener Wunsch während eines Laufs wird genau einmal nachgeholt,
    nicht erzwungene (Timer, Fokus) verfallen. Ein vorgemerkter Kauf-Abgleich wird nicht überschrieben
    (er ist selbst erzwungen und hält die Wiederholungen nach dem Kauf am Laufen)."""

    def __init__(self) -> None:
        self.running = False
        self.pending: tuple[str, bool] | None = None

    def begin(self, reason: str, force: bool = False) -> bool:
        if self.running:
            if force and not (self.pending and self.pending[0] in PURCHASE_REASONS):
                self.pending = (reason, True)
            return False
        self.running = True
        return True

    def finish(self) -> tuple[str, bool] | None:
        """Lauf beendet; liefert den nachzuholenden Wunsch (reason, force) oder None."""
        self.running = False
        pending, self.pending = self.pending, None
        return pending


class PurchaseRetry:
    """Nach einem Kauf: sofort abgleichen, danach nach 5, 15 und 60 s wiederholen, bis neue Spiele auftauchen
    (GOG trägt Käufe nicht immer sofort ein)."""

    def __init__(self, delays: Iterable[int] = PURCHASE_DELAYS) -> None:
        self.delays = tuple(int(d) for d in delays)
        self.attempt = 0
        self.active = False

    def start(self) -> None:
        self.active, self.attempt = True, 0

    def after_sync(self, found_new: bool, own: bool = True) -> int | None:
        """Nach einem Abgleich: Sekunden bis zum nächsten Versuch oder None (fertig, nicht aktiv oder – bei einem
        anderen Abgleich (``own`` = False, z. B. Timer) ohne neue Spiele – Zeitplan unverändert)."""
        if not self.active:
            return None
        if found_new:
            self.active = False
            return None
        if not own:
            return None
        if self.attempt >= len(self.delays):
            self.active = False
            return None
        delay = self.delays[self.attempt]
        self.attempt += 1
        return delay


# --------------------------------------------------------------------------- Abgleich (Arbeits-Thread)
_sync_lock = threading.Lock()
_marks_lock = threading.Lock()      # NEU-Markierungen: Abgleich (Arbeits-Thread) und Spielansicht (GTK-Thread)


def mark_seen(settings: Any, product_id: Any) -> bool:
    """NEU-Markierung entfernen (Spielansicht geöffnet); True, wenn es eine gab."""
    with _marks_lock:
        if str(product_id) not in settings.library_new_ids:
            return False
        settings.library_new_ids = clear_new(settings.library_new_ids, product_id)
        settings.save()
        return True


def reset_caches(backend: Any, ids: Iterable[str]) -> None:
    """Besitzliste des Installers (DLC-Auswahl) immer, Metadaten/Cover nur für die genannten IDs vergessen."""
    inst = getattr(backend, "installer", None)
    lock = getattr(inst, "_lock", None)
    if inst is not None and lock is not None:
        with lock:
            inst._owned = None
    ids = list(ids)
    meta_lock = getattr(backend, "_meta_lock", None)
    if ids and meta_lock is not None:
        with meta_lock:
            for pid in ids:
                backend._meta.pop(pid, None)
                backend._covers.pop(pid, None)


def sync(backend: Any, reason: str = "manual", force: bool = False,
         progress: Callable[[int, int], None] | None = None, now: float | None = None) -> SyncResult:
    """Bibliothek abgleichen (läuft im Arbeits-Thread, nie gleichzeitig zweimal). Wirft NotLoggedIn/ApiError."""
    from .client import NotLoggedIn
    api, s = backend.api, backend.settings
    if not api.logged_in:
        raise NotLoggedIn()
    with _sync_lock:
        user = api.user_data()
        checksum = games_checksum(user)
        old = list(backend.library)
        now = time.time() if now is None else now
        if should_skip(checksum, s.library_checksum, force, bool(old)):
            with _marks_lock:
                marks = prune_new(s.library_new_ids, now)
                if marks != s.library_new_ids:
                    s.library_new_ids = marks
                    s.save()
            log.info("Bibliotheksabgleich (%s): Prüfsumme unverändert – %d Einträge", reason, len(old))
            return SyncResult(total=len(old), skipped=True, reason=reason)
        entries = api.library(progress)
        by_id = {e.id: e for e in entries}
        first = not old
        seen, removed = diff_ids([e.id for e in old], by_id) if not first else (list(by_id), [])
        new_ids = [] if first else [i for i in seen if not by_id[i].is_hidden]
        backend.library = entries
        # neue Zuweisungen statt Änderungen an Ort und Stelle: der GTK-Thread kann gleichzeitig speichern
        with _marks_lock:
            s.library_new_ids = record_new(prune_new(s.library_new_ids, now), new_ids, now)
            s.library_checksum = checksum
            s.save()
        reset_caches(backend, seen)
        titles = [by_id[i].title or i for i in new_ids]
        log.info("Bibliotheksabgleich (%s%s): %d Einträge, %d neu%s, %d entfernt", reason, ", erzwungen" if force else "",
                 len(entries), len(new_ids), f" ({', '.join(titles[:5])})" if titles else "", len(removed))
        return SyncResult(new_ids, removed, len(entries), False, reason=reason, new_titles=titles, first=first)
