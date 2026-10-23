"""Wunschlisten-Rabatte wie in GOG Galaxy 2.1 (Changelog 2.0.50; gog-apis.md §6): Kurz nach dem Start und danach alle
6 Stunden prüft galaxy-linux angemeldet die Wunschliste (wishlist.json + Batch-Preise). Hat ein Rabatt begonnen oder
ist er gewachsen, meldet das Benachrichtigungszentrum „„Titel“ ist jetzt um 40 % reduziert – 17,99 €“; ein Spiel, das
vorher keinen Preis hatte, „Neu erhältlich: „Titel“ – Preis“. Ein Klick öffnet die Produktseite im Store. Der Stand der letzten
Prüfung liegt in ``wishlist_state.json`` im Datenordner (Logik: ``api/wishlist.py``).
"""
from __future__ import annotations

import logging
from typing import Any

from .. import i18n, paths
from ..api import wishlist as wl
from ..api.client import NotLoggedIn
from . import ui_registry
from .util import GLib, run_async

log = logging.getLogger(__name__)

TICK = 600          # s: Takt, in dem die 6-Stunden-Frist geprüft wird


class WishlistWatcher:
    def __init__(self, win: Any) -> None:
        self.win = win
        self.busy = False
        GLib.timeout_add_seconds(wl.START_DELAY, lambda: (self.check(), False)[1])
        GLib.timeout_add_seconds(TICK, self._tick)

    def _tick(self) -> bool:
        if wl.due(paths.WISHLIST_STATE_FILE):
            self.check()
        return GLib.SOURCE_CONTINUE

    def check(self) -> None:
        api = self.win.backend.api
        if self.busy or not api.logged_in:
            return
        self.busy = True
        lang = i18n.current()
        service = self.win.discover_service

        def work() -> list[wl.Change]:
            region = service.region(lang)
            return wl.check(api, paths.WISHLIST_STATE_FILE, region.country, region.currency, region.locale)

        def done(changes: list[wl.Change]) -> None:
            self.busy = False
            for c in changes:
                kind, text = wl.notification_text(c, lang)
                self.win.notifications.post(kind, text, action=f"store:{c.link or f'/game/{c.product_id}'}",
                                            product_id=c.product_id, key=wl.notification_key(c))

        def fail(exc: Exception) -> None:
            self.busy = False
            if not isinstance(exc, NotLoggedIn):
                log.info("Wunschliste nicht geprüft: %s", exc)
        run_async(work, done, fail)


def window_hook(win: Any) -> None:
    win.wishlist_watcher = WishlistWatcher(win)


ui_registry.register_window_hook("wishlist", window_hook, order=60)
