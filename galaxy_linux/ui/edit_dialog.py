"""„Bearbeiten …“ wie in GOG Galaxy (``gameEditWindow``: Info und Medien): eigener Titel und eigenes Cover (Kachel)
für jedes Spiel, je mit „Auf Standard zurücksetzen“. Gilt überall – Bibliothek, Spielansicht, Suche, GOverlay
(Kartenname und Cover). Jede Änderung wirkt sofort (Titel mit Enter bzw. dem Häkchen übernehmen)."""
from __future__ import annotations

from ..i18n import tr

import logging

from .util import Adw, Gio, GLib, Gtk, clamped_picture, icon_button, pick_icon, run_async

log = logging.getLogger(__name__)

IMAGE_PATTERNS = ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.JPG", "*.JPEG", "*.PNG", "*.WEBP")


class EditGameDialog(Adw.Dialog):
    def __init__(self, win, pid: str) -> None:
        super().__init__(title=tr('Bearbeiten'), content_width=580)
        self.win = win
        self.backend = win.backend
        self.pid = str(pid)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()

        info = Adw.PreferencesGroup(title=tr('Info'))
        self.title_row = Adw.EntryRow(title=tr('Titel'), text=self.backend.display_title(self.pid), show_apply_button=True)
        self.title_row.connect("apply", lambda r: self._set_title(r.get_text()))
        self.reset_title = icon_button(pick_icon("edit-undo-symbolic", "edit-clear-symbolic"), tr('Auf Standard zurücksetzen'),
                                       ["flat"])
        self.reset_title.set_valign(Gtk.Align.CENTER)
        self.reset_title.connect("clicked", lambda *_: self._set_title(""))
        self.title_row.add_suffix(self.reset_title)
        info.add(self.title_row)
        self.default_row = Adw.ActionRow(title=tr('Standard'), subtitle=self.backend.default_title(self.pid))
        self.default_row.set_subtitle_selectable(True)
        info.add(self.default_row)
        page.add(info)

        media = Adw.PreferencesGroup(title=tr('Medien'))
        self.cover_row = Adw.ActionRow(title=tr('Kachel'))
        frame, self.pic = clamped_picture(68, 96, ["edit-cover"])
        frame.set_valign(Gtk.Align.CENTER)
        frame.set_margin_top(6)
        frame.set_margin_bottom(6)
        self.cover_row.add_prefix(frame)
        choose = Gtk.Button(label=tr('Datei auswählen …'), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_cover())
        self.cover_row.add_suffix(choose)
        self.reset_cover = icon_button(pick_icon("edit-undo-symbolic", "edit-clear-symbolic"), tr('Auf Standard zurücksetzen'),
                                       ["flat"])
        self.reset_cover.set_valign(Gtk.Align.CENTER)
        self.reset_cover.connect("clicked", lambda *_: self._set_cover(None))
        self.cover_row.add_suffix(self.reset_cover)
        media.add(self.cover_row)
        page.add(media)

        self.toasts.set_child(page)
        tv.set_content(self.toasts)
        self.set_child(tv)
        self._refresh()

    # ------------------------------------------------------------------ Anzeige
    def _refresh(self) -> None:
        gs = self.backend.game_settings.get(self.pid)
        self.reset_title.set_sensitive(bool(gs.custom_title))
        self.reset_cover.set_sensitive(bool(gs.custom_cover))
        self.cover_row.set_subtitle(tr('Eigenes Cover') if gs.custom_cover
                                    else tr('Automatisch · empfohlen: Hochformat, z. B. 342 × 482 Pixel'))
        self.pic.set_paintable(None)
        pid = self.pid
        run_async(lambda: self.backend.cover_url(pid), lambda url: url and self.win.images.load(url, self.pic.set_paintable),
                  lambda e: None)

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def _changed(self) -> None:
        self._refresh()
        self.win.titles_changed([self.pid])

    # ------------------------------------------------------------------ Titel
    def _set_title(self, text: str) -> None:
        title = self.backend.set_custom_title(self.pid, text)
        self.title_row.set_text(title)
        self._toast(tr('Titel gespeichert') if self.backend.game_settings.get(self.pid).custom_title
                    else tr('Titel auf Standard zurückgesetzt'))
        self._changed()

    # ------------------------------------------------------------------ Cover
    def _choose_cover(self) -> None:
        dlg = Gtk.FileDialog(title=tr('Cover wählen'), modal=True)
        flt = Gtk.FileFilter()
        flt.set_name(tr('Bilder (JPEG, PNG, WebP)'))
        for pat in IMAGE_PATTERNS:
            flt.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        dlg.set_default_filter(flt)

        def done(d: Gtk.FileDialog, res) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self._set_cover(f.get_path())
        dlg.open(self.win, None, done)

    def _set_cover(self, path: str | None) -> None:
        def ok(_dest) -> None:
            self._toast(tr('Cover gespeichert') if path else tr('Cover auf Standard zurückgesetzt'))
            self._changed()
        run_async(lambda: self.backend.set_custom_cover(self.pid, path), ok,
                  lambda e: self._toast(tr('Cover nicht übernommen: {0}').format(e)))
