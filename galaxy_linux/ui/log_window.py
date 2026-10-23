"""Protokoll-Fenster (⚙ → „Protokoll“, Linux-Ergänzung): galaxy-linux.log, comet.log und die Spielprotokolle.

Zeigt die letzten ~3000 Zeilen und folgt neuen Zeilen, solange das Fenster offen ist. Filter: Stufe (Alle /
Warnungen / Fehler) und Suchtext; „Ordner öffnen“, „Kopieren“ (das Gezeigte) und „Leeren“ (mit Rückfrage).
Logik ohne GTK: ``galaxy_linux/logview.py``.
"""
from __future__ import annotations

from ..i18n import N_, tr

import logging

from .. import logview, paths
from .util import Adw, GLib, Gtk, Pango, open_uri, pick_icon

log = logging.getLogger(__name__)

FOLLOW_MS = 1000
LEVEL_NAMES = (("all", N_('Alle')), ("warning", N_('Warnungen')), ("error", N_('Fehler')))


class LogWindow(Adw.Window):
    def __init__(self, win) -> None:
        super().__init__(title=tr('Protokoll'), default_width=1100, default_height=720, transient_for=win)
        self.win = win
        self.backend = win.backend
        self.records: list[logview.Record] = []
        self.follower: logview.Follower | None = None
        self.level = "all"
        self.query = ""
        titles = {}
        for g in self.backend.registry.all():
            titles[g.product_id] = self.backend.display_title(g.product_id) if hasattr(self.backend, "display_title") else g.title
        self.sources = logview.sources(titles)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.source_dd = Gtk.DropDown.new_from_strings([s.label for s in self.sources])
        self.source_dd.set_tooltip_text(tr('Protokoll wählen'))
        self.source_dd.connect("notify::selected", lambda *_: self.load())
        hb.pack_start(self.source_dd)
        self.level_dd = Gtk.DropDown.new_from_strings([tr(n) for _k, n in LEVEL_NAMES])
        self.level_dd.set_tooltip_text(tr('Stufe'))
        self.level_dd.connect("notify::selected", self._on_level)
        hb.pack_start(self.level_dd)
        self.search = Gtk.SearchEntry(placeholder_text=tr('Im Protokoll suchen'))
        self.search.set_size_request(240, -1)
        self.search.connect("search-changed", self._on_search)
        hb.set_title_widget(self.search)
        for text, icon, cb in ((tr('Leeren'), "edit-clear-all-symbolic", self._confirm_clear),
                               (tr('Zwischenablage\x04Kopieren'), "edit-copy-symbolic", self._copy),
                               (tr('Ordner öffnen'), "folder-open-symbolic", self._open_folder)):
            b = Gtk.Button()
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image(icon_name=pick_icon(icon, "document-open-symbolic")))
            box.append(Gtk.Label(label=text))
            b.set_child(box)
            b.connect("clicked", lambda *_a, cb=cb: cb())
            hb.pack_end(b)
        tv.add_top_bar(hb)
        self.toasts = Adw.ToastOverlay()
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=10, right_margin=10, top_margin=8, bottom_margin=8)
        self.view.add_css_class("log-view")
        self.buffer = self.view.get_buffer()
        self.scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroll.set_child(self.view)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self.scroll)
        self.info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, margin_start=10, margin_end=10,
                              margin_top=4, margin_bottom=4)
        self.info.add_css_class("dim")
        self.info.add_css_class("caption")
        outer.append(self.info)
        self.toasts.set_child(outer)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self._timer = GLib.timeout_add(FOLLOW_MS, self._follow)
        self.connect("close-request", self._on_close)
        self.load()

    # ------------------------------------------------------------------ Laden und Folgen
    @property
    def source(self) -> logview.LogSource:
        i = self.source_dd.get_selected()
        return self.sources[i] if 0 <= i < len(self.sources) else self.sources[0]

    def load(self) -> None:
        src = self.source
        self.records = logview.split_records(logview.tail_lines(src.path, logview.MAX_LINES))
        self.follower = logview.Follower(src.path)
        self.follower.start_at_end()
        self._render(scroll_end=True)

    def _render(self, scroll_end: bool = False) -> None:
        shown = logview.filter_records(self.records, self.level, self.query)
        self.buffer.set_text(logview.render(shown))
        self.info.set_text(tr('{0} · {1} von {2} Einträgen').format(self.source.path, len(shown), len(self.records)))
        if scroll_end:
            GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _at_end(self) -> bool:
        adj = self.scroll.get_vadjustment()
        return adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40

    def _follow(self) -> bool:
        if self.follower is None:
            return True
        try:
            new = self.follower.read_new()
        except Exception:  # pragma: no cover – Anzeige darf nie scheitern
            log.debug("Protokoll nicht lesbar", exc_info=True)
            return True
        if self.follower.reset_flag:
            self.records = []
        if not new and not self.follower.reset_flag:
            return True
        at_end = self._at_end()
        before = len(self.records)
        added, merged = logview.append_lines(self.records, new)
        if len(self.records) > logview.MAX_LINES:
            self.records = self.records[-logview.MAX_LINES:]
        if self.follower.reset_flag or merged or len(self.records) < before + len(added) or self.query \
                or self.level != "all":
            self._render(scroll_end=at_end)
        else:
            shown = logview.filter_records(added, self.level, self.query)
            if shown:
                end = self.buffer.get_end_iter()
                self.buffer.insert(end, ("\n" if self.buffer.get_char_count() else "") + logview.render(shown))
            self.info.set_text(tr('{0} · {1} von {2} Einträgen').format(self.source.path, len(self.records), len(self.records)))
            if at_end:
                GLib.idle_add(self._scroll_to_end)
        return True

    def _on_close(self, *_: object) -> bool:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        return False

    # ------------------------------------------------------------------ Filter
    def _on_level(self, dd, _p) -> None:
        i = dd.get_selected()
        self.level = LEVEL_NAMES[i][0] if 0 <= i < len(LEVEL_NAMES) else "all"
        self._render(scroll_end=True)

    def _on_search(self, entry) -> None:
        self.query = entry.get_text()
        self._render(scroll_end=True)

    # ------------------------------------------------------------------ Knöpfe
    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def _open_folder(self) -> None:
        open_uri(paths.LOG_DIR.as_uri())

    def _copy(self) -> None:
        start, end = self.buffer.get_bounds()
        self.get_clipboard().set(self.buffer.get_text(start, end, False))
        self._toast(tr('In die Zwischenablage kopiert'))

    def _confirm_clear(self) -> None:
        from .dialogs import confirm_destructive
        src = self.source

        def do() -> None:
            try:
                logview.clear(src.path)
            except OSError as exc:
                self._toast(tr('Protokoll nicht geleert: {0}').format(exc))
                return
            log.info("Protokoll geleert: %s", src.path)
            self.load()
        confirm_destructive(self, tr('Protokoll leeren'),
                            tr('„{0}“ wirklich leeren? Die bisherigen Einträge gehen verloren.').format(src.label),
                            tr('Leeren'), do)


def open_log_window(win) -> LogWindow:
    """Protokoll-Fenster öffnen (ein offenes wird nach vorn geholt)."""
    existing = getattr(win, "log_window", None)
    if existing is not None and existing.get_visible():
        existing.present()
        return existing
    w = LogWindow(win)
    win.log_window = w
    w.present()
    return w
