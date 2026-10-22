"""Brücke zwischen Launch-Hooks (Start-Thread) und Oberfläche: Rückfragen als Adw.AlertDialog, Meldungen als Toast.

``ask()`` darf aus jedem Thread gerufen werden: Der Dialog entsteht immer im GTK-Hauptthread (``idle``), der
aufrufende Arbeits-Thread wartet auf die Antwort. Wird ``ask()`` im Hauptthread selbst gerufen, dreht sich die
Hauptschleife weiter, bis geantwortet ist (keine Verklemmung).
"""
from __future__ import annotations

import logging
import threading

from .util import Adw, GLib, idle

log = logging.getLogger(__name__)


class GtkBridge:
    def __init__(self, window, timeout: float | None = 3600) -> None:
        self.window = window
        self.timeout = timeout
        self._pending: list[tuple[threading.Event, dict]] = []
        self._lock = threading.Lock()

    def ask(self, title: str, body: str, options: list[tuple[str, str]], default_id: str) -> str:
        if not options:
            return default_id
        ev = threading.Event()
        result = {"id": default_id}
        with self._lock:
            self._pending.append((ev, result))

        def answer(rid: str) -> None:
            if rid in {o[0] for o in options}:
                result["id"] = rid
            ev.set()

        def show() -> None:
            try:
                dlg = Adw.AlertDialog.new(title, body)
                for oid, text in options:
                    dlg.add_response(oid, text)
                ids = [o[0] for o in options]
                if default_id in ids:
                    dlg.set_default_response(default_id)
                    dlg.set_close_response(default_id)
                dlg.connect("response", lambda _d, rid: answer(rid))
                dlg.present(self.window)
            except Exception:                 # Fenster weg o. Ä.: Vorgabe statt endlosem Warten
                log.exception("Rückfrage „%s“ konnte nicht angezeigt werden", title)
                ev.set()

        try:
            if threading.current_thread() is threading.main_thread():
                show()
                ctx = GLib.MainContext.default()
                while not ev.is_set():
                    ctx.iteration(True)
            else:
                idle(show)
                if not ev.wait(self.timeout):
                    log.warning("Rückfrage „%s“ ohne Antwort – Vorgabe %s", title, default_id)
        finally:
            with self._lock:
                self._pending = [p for p in self._pending if p[0] is not ev]
        return result["id"]

    def notify(self, text: str) -> None:
        idle(self.window.toast, text)

    def cancel_all(self) -> None:
        """Offene Rückfragen mit der Vorgabe beantworten (z. B. beim Schließen des Fensters)."""
        with self._lock:
            pending, self._pending = self._pending, []
        for ev, _res in pending:
            ev.set()
