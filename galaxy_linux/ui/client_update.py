"""Selbst-Update wie GOG Galaxy 2.1 (§17): Zahnrad → „Nach Updates suchen“ und – einmal am Tag beim Start, wenn
„Beim Start nach Client-Updates suchen“ an ist – die neueste Veröffentlichung auf GitHub prüfen.

Ist sie neuer: Dialog mit den Versionshinweisen, „Jetzt aktualisieren“ / „Später“; das Zahnrad bekommt einen blauen
Punkt und sein Eintrag heißt „Jetzt aktualisieren“ (wie Galaxys ``app_settings__update_now``). „Jetzt aktualisieren“
lädt beim pacman-Paket das Paket samt ``SHA256SUMS``, prüft die Prüfsumme, installiert über ``pkexec pacman -U``
(Passwort nur im Polkit-Dialog des Systems), zeigt Fortschritt und Ergebnis und bietet „Neu starten“ an.
Benutzerinstallationen und der Quellordner öffnen die Veröffentlichung im Browser (``clientinfo.update_action``).
"""
from __future__ import annotations

from ..i18n import tr

import logging
import threading
import time

from .. import __version__, clientinfo, paths
from .util import Adw, GLib, Gtk, idle, label, open_uri, run_async

log = logging.getLogger(__name__)

START_DELAY = 20          # Sekunden nach dem Start (Bibliothek und Abgleich zuerst)
MENU_ITEM = "client-update"


def update_menu_item(on_click) -> Gtk.Widget:
    """Eintrag „Jetzt aktualisieren“ mit blauem Punkt für das Zahnrad-Menü (eigenes Widget im PopoverMenu)."""
    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.add_css_class("menu-update-item")
    box = Gtk.Box(spacing=10)
    text = label(tr('Jetzt aktualisieren'))
    text.set_hexpand(True)
    box.append(text)
    dot = Gtk.Box(valign=Gtk.Align.CENTER)
    dot.add_css_class("update-dot")
    box.append(dot)
    btn.set_child(box)
    btn.connect("clicked", lambda b: on_click(b))
    return btn


class ClientUpdate:
    """Zustand und Ablauf des Selbst-Updates für ein Hauptfenster (``win.client_update``)."""

    def __init__(self, win, auto: bool = True) -> None:
        self.win = win
        self.result: clientinfo.UpdateCheck | None = None
        self.checking = False
        self.installing = False
        self._dot = self._decorate_gear()
        if auto:
            GLib.timeout_add_seconds(START_DELAY, self._on_start)

    # ------------------------------------------------------------------ Zahnrad
    def _decorate_gear(self) -> Gtk.Widget | None:
        btn = getattr(self.win, "main_menu_btn", None)
        if btn is None:
            return None
        overlay = Gtk.Overlay()
        overlay.set_child(Gtk.Image(icon_name=btn.get_icon_name() or "open-menu-symbolic"))
        dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, can_target=False)
        dot.add_css_class("update-dot")
        dot.add_css_class("on-icon")
        dot.set_visible(False)
        overlay.add_overlay(dot)
        btn.set_child(overlay)
        return dot

    @property
    def available(self) -> bool:
        return self.result is not None and self.result.state == "newer"

    def _set_menu(self) -> None:
        from . import menus
        btn = getattr(self.win, "main_menu_btn", None)
        if btn is None:
            return
        btn.set_menu_model(menus.main_menu_model(update_available=self.available))
        if self.available:
            pop = btn.get_popover()

            def click(_b) -> None:
                pop.popdown()
                self.menu_action()
            if isinstance(pop, Gtk.PopoverMenu):
                pop.add_child(update_menu_item(click), MENU_ITEM)
        if self._dot is not None:
            self._dot.set_visible(self.available)
            btn.set_tooltip_text(tr('Menü – Update verfügbar') if self.available else tr('Menü'))

    # ------------------------------------------------------------------ Suche
    def _on_start(self) -> bool:
        s = self.win.backend.settings
        if s.check_client_updates and clientinfo.should_check(s.client_update_checked, time.time()):
            self.check(manual=False)
        return False

    def menu_action(self) -> None:
        """Zahnrad: „Jetzt aktualisieren“, wenn ein Update bekannt ist, sonst „Nach Updates suchen“."""
        if self.available:
            self.show(self.result)
        else:
            self.check(manual=True)

    def check(self, manual: bool = True) -> None:
        if self.checking:
            return
        self.checking = True
        if manual:
            self.win.toast(tr('Suche nach Updates …'))

        def done(res: clientinfo.UpdateCheck) -> None:
            self.checking = False
            s = self.win.backend.settings
            s.client_update_checked = time.time()
            self.win.backend.save_settings()
            if res.state == "newer":
                self.found(res)
            elif manual:
                self.win.toast(tr('Noch keine Veröffentlichung') if res.state == "none" else
                               tr('Keine Updates für galaxy-linux verfügbar ({0})').format(__version__))

        def fail(exc: Exception) -> None:
            self.checking = False
            log.info("Suche nach Client-Updates fehlgeschlagen: %s", exc)
            if manual:
                self.win.toast(tr('Update-Suche fehlgeschlagen: {0}').format(exc))
        run_async(clientinfo.check_for_update, done, fail)

    def found(self, res: clientinfo.UpdateCheck, show: bool = True) -> None:
        log.info("Client-Update gefunden: %s (installiert %s)", res.latest, __version__)
        self.result = res
        self._set_menu()
        if show:
            self.show(res)

    # ------------------------------------------------------------------ Dialog „Neues Update“
    def show(self, res: clientinfo.UpdateCheck) -> None:
        rel = res.release or clientinfo.Release(res.latest, url=res.url)
        dlg = Adw.AlertDialog.new(tr('Neues Update verfügbar'),
                                  tr('galaxy-linux {0} ist verfügbar – installiert ist {1}.').format(rel.version, __version__))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        notes = clientinfo.markdown_to_plain(rel.notes) or tr('Diese Veröffentlichung hat keine Versionshinweise.')
        text = label(notes, wrap=True)      # nicht auswählbar: sonst markiert GTK beim Öffnen den ganzen Text
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    max_content_height=320, min_content_width=300)
        scroll.set_child(text)
        scroll.add_css_class("update-notes")
        text.set_margin_top(10)
        text.set_margin_bottom(10)
        text.set_margin_start(12)
        text.set_margin_end(12)
        box.append(scroll)
        hint = label("", css=["dim", "caption"], wrap=True)
        hint.set_visible(False)
        box.append(hint)
        dlg.set_extra_child(box)
        dlg.set_prefer_wide_layout(True)              # wie Galaxy: „Jetzt aktualisieren“ und „Später“ nebeneinander
        dlg.add_response("later", tr('Später'))
        dlg.add_response("update", tr('Jetzt aktualisieren'))
        dlg.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("update")
        dlg.set_close_response("later")
        dlg.connect("response", lambda _d, r: self.update_now(res) if r == "update" else None)

        def method_known(method: str) -> None:
            if clientinfo.update_action(method, res.release) == "open":
                hint.set_text(tr('galaxy-linux ist hier nicht als pacman-Paket installiert – „Jetzt aktualisieren“ '
                                 'öffnet die Veröffentlichung im Browser.'))
                hint.set_visible(True)
        run_async(clientinfo.update_method, method_known, lambda e: None)
        dlg.present(self.win)

    # ------------------------------------------------------------------ Jetzt aktualisieren
    def update_now(self, res: clientinfo.UpdateCheck) -> None:
        if self.installing:
            return

        def decided(method: str) -> None:
            if clientinfo.update_action(method, res.release) == "install":
                self.install(res)
            else:
                open_uri(res.url)
                self.win.toast(tr('Veröffentlichung {0} im Browser geöffnet').format(res.latest))
        run_async(clientinfo.update_method, decided, lambda e: decided("user"))

    def install(self, res: clientinfo.UpdateCheck) -> "UpdateProgressDialog":
        self.installing = True
        dlg = UpdateProgressDialog(self, res)
        dlg.present(self.win)
        dlg.start()
        return dlg

    def restart(self) -> None:
        app = self.win.get_application()
        if app is not None:
            app.restart_requested = True
        self.win.request_quit()


class UpdateProgressDialog(Adw.Dialog):
    """Laden → Prüfsumme → Installieren (Polkit-Abfrage des Systems) → Ergebnis mit „Neu starten“."""

    def __init__(self, ctl: ClientUpdate, res: clientinfo.UpdateCheck) -> None:
        super().__init__(title=tr('galaxy-linux aktualisieren'), content_width=480)
        self.ctl, self.res = ctl, res
        self.cancel = threading.Event()
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=6, margin_bottom=20, margin_start=24,
                      margin_end=24)
        box.append(label(tr('galaxy-linux {0}').format(res.latest), css=["title-3"]))
        self.status = label(tr('Update wird geladen …'), wrap=True)
        box.append(self.status)
        self.bar = Gtk.ProgressBar()
        box.append(self.bar)
        self.detail = label("", css=["dim", "caption", "mono"], wrap=True, selectable=True)
        self.detail.set_visible(False)
        box.append(self.detail)
        self.buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
        box.append(self.buttons)
        tv.set_content(box)
        self.set_child(tv)
        self.set_can_close(False)
        self._buttons([(tr('Abbrechen'), self._cancel, False)])

    def _buttons(self, items) -> None:
        child = self.buttons.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.buttons.remove(child)
            child = nxt
        for text, cb, suggested in items:
            b = Gtk.Button(label=text)
            if suggested:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, cb=cb: cb())
            self.buttons.append(b)

    def _cancel(self) -> None:
        self.cancel.set()
        self.status.set_text(tr('Wird abgebrochen …'))

    def _close(self) -> None:
        self.ctl.installing = False
        self.set_can_close(True)
        self.close()

    def start(self) -> None:
        rel = self.res.release
        dest = paths.CACHE_DIR / "client-update"

        def progress(done: int, total: int) -> None:
            idle(self._progress, done, total)

        def work() -> clientinfo.InstallResult:
            path = clientinfo.download_update(rel, dest, progress=progress, cancel=self.cancel)
            idle(self._installing)
            log.info("Client-Update %s geladen und geprüft: %s – Installation über pkexec", rel.version, path.name)
            return clientinfo.install_package(path)
        run_async(work, self._done, self._failed)

    def _progress(self, done: int, total: int) -> None:
        if total:
            self.bar.set_fraction(min(1.0, done / total))
            self.status.set_text(tr('Update wird geladen … {0} %').format(int(100 * done / total)))
        else:
            self.bar.pulse()

    def _installing(self) -> None:
        self.bar.set_fraction(1.0)
        self.status.set_text(tr('Prüfsumme stimmt. Wird installiert – bitte die Abfrage des Systems bestätigen …'))
        self._buttons([])

    def _done(self, result: clientinfo.InstallResult) -> None:
        log.info("Client-Update: %s (Exit-Code %s)", result.message, result.code)
        self.status.set_text(result.message)
        if result.output and not result.ok:
            self.detail.set_text(result.output[-1200:])
            self.detail.set_visible(True)
        if result.ok:
            self._buttons([(tr('Später'), self._close, False), (tr('Neu starten'), self._restart, True)])
        else:
            self._buttons([(tr('Veröffentlichung öffnen'), lambda: (open_uri(self.res.url), self._close()), False),
                           (tr('Schließen'), self._close, True)])

    def _failed(self, exc: Exception) -> None:
        log.warning("Client-Update fehlgeschlagen: %s", exc)
        self.status.set_text(str(exc))
        self._buttons([(tr('Veröffentlichung öffnen'), lambda: (open_uri(self.res.url), self._close()), False),
                       (tr('Schließen'), self._close, True)])

    def _restart(self) -> None:
        self._close()
        self.ctl.restart()
