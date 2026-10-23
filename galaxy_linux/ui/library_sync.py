"""Bibliotheks-Abgleich im Hauptfenster: Auslöser, Einzelflug, Spinner/Status, Meldungen, NEU-Markierungen.

Auslöser: Start (nach ~3 s, wenn die Bibliothek aus dem Cache schon steht; sofort bei leerem Cache), Intervall
(``library_sync_interval`` Minuten, 0 = aus), Wechsel ins Fenster (``library_sync_on_focus``, frühestens 5 min nach
dem letzten Abgleich), Login, Zahnrad „Bibliothek aktualisieren“/F5 (``manual``) und Kauf (``purchase``: sofort,
danach nach 5/15/60 s erneut, bis neue Spiele da sind). Die Logik ohne GTK steckt in ``api/library_sync.py``.
"""
from __future__ import annotations

from ..i18n import tr

import logging
import time

from ..api import library_sync as ls
from ..api.client import NotLoggedIn
from .util import Gio, GLib, idle, run_async

log = logging.getLogger(__name__)

INTERACTIVE = ("manual", "login", "startup", "purchase")   # Fehler melden, bei abgelaufener Sitzung Login zeigen


class LibrarySyncController:
    def __init__(self, win) -> None:
        self.win = win
        self.backend = win.backend
        self.gate = ls.SyncGate()
        self.retry = ls.PurchaseRetry()
        self.last_sync = time.monotonic()       # Intervall und Fokus zählen ab Programmstart (Start-Abgleich folgt)
        self._startup_src = 0
        self._tick_src = 0
        self._retry_src = 0
        self._started = False
        self._session_warned = False

    # ------------------------------------------------------------------ Auslöser
    def start(self) -> None:
        """Am Ende des Fensteraufbaus: Start-Abgleich, Intervall-Takt und Fokus-Signal einrichten."""
        if self._started:
            return
        self._started = True
        api = self.backend.api
        if api.logged_in and not self.backend.library:
            self.request("startup", force=True)
        elif api.logged_in:
            self._startup_src = GLib.timeout_add_seconds(ls.STARTUP_DELAY, self._startup)
        self._tick_src = GLib.timeout_add_seconds(ls.TIMER_TICK, self._tick)
        self.win.connect("notify::is-active", self._on_active)
        self.win.connect("close-request", self._on_close)

    def _startup(self) -> bool:
        self._startup_src = 0
        self.request("startup")
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        s = self.backend.settings
        if self.backend.api.logged_in and ls.interval_due(self.last_sync, time.monotonic(), s.library_sync_interval):
            self.request("timer")
        return GLib.SOURCE_CONTINUE

    def _on_active(self, win, _pspec) -> None:
        if (win.is_active() and self.backend.settings.library_sync_on_focus and self.backend.api.logged_in
                and ls.focus_due(self.last_sync, time.monotonic())):
            self.request("focus")

    def _on_close(self, *_: object) -> bool:
        for src in (self._startup_src, self._tick_src, self._retry_src):
            if src:
                GLib.source_remove(src)
        self._startup_src = self._tick_src = self._retry_src = 0
        return False

    def request(self, reason: str, force: bool = False) -> None:
        """Abgleich anfordern (Hauptthread). Läuft schon einer, wird nur ein erzwungener Wunsch einmal nachgeholt."""
        if not self.backend.api.logged_in:
            if reason in ("manual", "purchase"):
                self.win.do_login()
            if reason in ls.PURCHASE_REASONS:
                self._stop_retry()
            return
        if reason == "purchase":
            force = True
            self.retry.start()
            self._cancel_retry()
        if not self.gate.begin(reason, force):
            log.debug("Bibliotheksabgleich läuft schon – %s %s", reason, "vorgemerkt" if force else "verworfen")
            return
        self.win.spinner.start()
        self.win.status_lbl.set_text(tr('Bibliothek wird abgeglichen …'))

        def progress(p: int, t: int) -> None:
            idle(self._progress, p, t)
        run_async(lambda: self.backend.sync_library(reason, force, progress),
                  lambda res: self._done(reason, res), lambda exc: self._failed(reason, exc))

    # ------------------------------------------------------------------ Ergebnis (Hauptthread)
    def _progress(self, page: int, total: int) -> None:
        if self.gate.running:
            self.win.status_lbl.set_text(tr('Bibliothek wird geladen … {0}/{1}').format(page, total))

    def _stop_busy(self) -> None:
        if not self.backend.jobs.active():
            self.win.spinner.stop()
        self.win.status_lbl.set_text("")

    def _done(self, reason: str, res: ls.SyncResult) -> None:
        w, s = self.win, self.backend.settings
        self.last_sync = time.monotonic()
        self._stop_busy()
        if not self.backend.api.logged_in:          # inzwischen ausgeloggt
            self.backend.library = []
            self._stop_retry()
            self._next()
            return
        self._session_warned = False
        w._update_avatar()
        if res.changed:
            self._refresh_views()
        w.update_nav_badges()
        if res.new_ids:
            self._announce(res)
        elif res.first and reason in INTERACTIVE:
            w.toast(tr('{0} Spiele geladen').format(res.total))
        elif reason == "manual":
            w.toast(tr('Bibliothek ist aktuell'))
        self._after_purchase_sync(reason, bool(res.new_ids))
        check = s.auto_check_updates or s.feature_auto_update      # automatische Updates brauchen die Suche
        if reason == "manual" or (check and (reason in ("startup", "login") or res.new_ids)):
            w.check_updates_async(quiet=True)
        self._next()

    def _failed(self, reason: str, exc: Exception) -> None:
        w, s = self.win, self.backend.settings
        self.last_sync = time.monotonic()
        self._stop_busy()
        w.update_nav_badges()
        if isinstance(exc, NotLoggedIn):
            log.info("Bibliotheksabgleich (%s): nicht angemeldet bzw. Sitzung abgelaufen", reason)
            if reason in INTERACTIVE:
                w.toast(tr('Sitzung abgelaufen – bitte neu anmelden.'))
                w.do_login()
            elif not self._session_warned:
                self._session_warned = True
                w.toast(tr('Sitzung abgelaufen – bitte neu anmelden.'))
            self._stop_retry()
        else:
            log.warning("Bibliotheksabgleich (%s) fehlgeschlagen: %s", reason, exc)
            if reason in ("manual", "login", "purchase"):
                w.toast(tr('Bibliothek konnte nicht geladen werden: {0}').format(exc))
            self._after_purchase_sync(reason, False)
        if reason == "startup" and (s.auto_check_updates or s.feature_auto_update) and self.backend.registry.all():
            w.check_updates_async(quiet=True)
        self._next()

    def _next(self) -> None:
        pending = self.gate.finish()
        if pending:
            GLib.idle_add(lambda: (self.request(*pending), False)[1])

    def _refresh_views(self) -> None:
        """Bibliotheksansichten neu aufbauen, ohne Sprung nach oben (der Abgleich läuft meist im Hintergrund)."""
        from .window import is_lib_view
        w = self.win
        if w.stack.get_visible_child_name() == "welcome":
            w.navigate("all", push=False)
        elif is_lib_view(w.view):
            adj = w.lib_scroll.get_vadjustment()
            y = adj.get_value()
            w.populate()
            GLib.idle_add(lambda: (adj.set_value(y), False)[1])

    def _announce(self, res: ls.SyncResult) -> None:
        """Toast „N neue(s) Spiel(e): …“; ist das Fenster nicht aktiv, zusätzlich eine Desktop-Benachrichtigung."""
        text = tr('{0} neue(s) Spiel(e): {1}').format(len(res.new_ids), ls.new_titles_text(res.new_titles))
        self.win.toast(text)
        if self.win.is_active():
            return
        app = self.win.get_application()
        if app is None:
            return
        try:
            n = Gio.Notification.new(tr('Neu in der Bibliothek'))
            n.set_body(tr('„{0}“ wurde deiner Bibliothek hinzugefügt').format(res.new_titles[0]) if len(res.new_ids) == 1 else text)
            app.send_notification("library-new", n)
        except Exception as exc:  # pragma: no cover - ohne Benachrichtigungsdienst
            log.debug("Desktop-Benachrichtigung nicht möglich: %s", exc)

    # ------------------------------------------------------------------ Kauf: Wiederholungen
    def _after_purchase_sync(self, reason: str, found_new: bool) -> None:
        """Nach einem Kauf: nächsten Versuch planen; neue Spiele (auch aus einem anderen Abgleich) beenden die Reihe."""
        delay = self.retry.after_sync(found_new, reason in ls.PURCHASE_REASONS)
        if delay:
            self._schedule_retry(delay)
        elif not self.retry.active:
            self._cancel_retry()

    def _stop_retry(self) -> None:
        self.retry.active = False
        self._cancel_retry()

    def _schedule_retry(self, delay: int) -> None:
        self._cancel_retry()
        log.info("Kauf: erneuter Bibliotheksabgleich in %d s", delay)

        def fire() -> bool:
            self._retry_src = 0
            self.request("purchase-retry", force=True)
            return GLib.SOURCE_REMOVE
        self._retry_src = GLib.timeout_add_seconds(delay, fire)

    def _cancel_retry(self) -> None:
        if self._retry_src:
            GLib.source_remove(self._retry_src)
            self._retry_src = 0

    # ------------------------------------------------------------------ NEU-Markierungen
    def is_new(self, product_id: str) -> bool:
        return ls.is_new(self.backend.settings.library_new_ids, product_id)

    def new_entries(self, entries: list) -> list:
        """Einträge mit NEU-Markierung, zuletzt hinzugekommene zuerst (Abschnitt „Neu in der Bibliothek“)."""
        by_id = {e.id: e for e in entries}
        return [by_id[i] for i in ls.new_order(self.backend.settings.library_new_ids, by_id)]

    def seen(self, product_id: str) -> None:
        """Spielansicht geöffnet → NEU entfällt."""
        try:
            ls.mark_seen(self.backend.settings, product_id)
        except OSError as exc:  # pragma: no cover
            log.warning("NEU-Markierung konnte nicht gespeichert werden: %s", exc)
