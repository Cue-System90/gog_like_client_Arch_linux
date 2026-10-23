"""Power Search wie in Galaxy 2.1 (Strg+F, Lupe über der Seitenleiste): eigene Spiele, Lesezeichen und der GOG-Store
in einer Liste; Eingabe bzw. Klick führt die Hauptaktion aus (Spielen, Installieren, Spiel anzeigen, Im Store kaufen,
Lesezeichen anzeigen). Die Logik ohne GTK steckt in ``galaxy_linux/power_search.py``."""
from __future__ import annotations

from ..i18n import tr

from urllib.parse import quote

from .. import i18n, power_search as ps
from .util import Adw, GLib, Gtk, label, pick_icon, run_async

STORE_DELAY_MS = 350


class PowerSearchDialog(Adw.Dialog):
    def __init__(self, win, text: str = "") -> None:
        super().__init__(title=tr('Suche'), content_width=640, content_height=560)
        self.win, self.backend = win, win.backend
        self._timer = 0
        self._serial = 0
        self._local: list[ps.Result] = []
        self._store: list[ps.Result] = []
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6, margin_bottom=12, margin_start=14,
                      margin_end=14)
        self.entry = Gtk.SearchEntry(placeholder_text=tr('Spiele, Tags, Genres, Entwickler, Lesezeichen, Store …'))
        self.entry.connect("search-changed", self._changed)
        self.entry.connect("activate", lambda *_: self._activate_first())
        self.entry.connect("stop-search", lambda *_: self.close())
        box.append(self.entry)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda _l, row: self._run(row.result))
        self.list.set_header_func(self._header)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(self.list)
        box.append(scroll)
        self.hint = label(tr('Durchsuche Tags, Genres, Bewertungen & mehr...'), css=["dim"], wrap=True)
        box.append(self.hint)
        tv.set_content(box)
        self.set_child(tv)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.entry.add_controller(keys)
        if text:
            self.entry.set_text(text)
        self.connect("map", lambda *_: self.entry.grab_focus())

    # ------------------------------------------------------------------ Suche
    def _changed(self, entry: Gtk.SearchEntry) -> None:
        q = entry.get_text().strip()
        self._serial += 1
        self._local = ps.local_results(self.backend, q) if q else []
        self._store = []
        self._render(q)
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if len(q) >= 2:
            serial = self._serial
            self._timer = GLib.timeout_add(STORE_DELAY_MS, lambda: self._query_store(q, serial))

    def _query_store(self, q: str, serial: int) -> bool:
        self._timer = 0
        user = self.backend.api.user or {}
        locale = "de-DE" if i18n.current() == "de" else "en-US"
        owned = {e.id for e in self.backend.library}

        def done(products: list) -> None:
            if serial != self._serial:
                return
            self._store = ps.store_results(products, owned)
            self._render(q)
        run_async(lambda: self.backend.api.catalog_search(q, 8, locale, str(user.get("country") or "")), done,
                  lambda e: None)
        return GLib.SOURCE_REMOVE

    def _render(self, q: str) -> None:
        self.list.remove_all()
        shown_ids = {r.product_id for r in self._local if r.kind == "game"}
        results = self._local + [r for r in self._store if r.product_id not in shown_ids]
        if q:
            results.append(ps.Result("find", tr('Finde „{0}“ im Store').format(q), "", "buy",
                                     url=f"/games?query={quote(q)}"))
        for r in results:
            self.list.append(self._row(r))
        self.hint.set_visible(not q)
        first = self.list.get_row_at_index(0)
        if first is not None:
            self.list.select_row(first)

    def _row(self, r: ps.Result) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.result = r
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
        icon = {"game": "applications-games-symbolic", "store": "emblem-shared-symbolic",
                "bookmark": "user-bookmarks-symbolic", "find": "system-search-symbolic"}.get(r.kind, "applications-games-symbolic")
        box.append(Gtk.Image(icon_name=pick_icon(icon, "applications-games-symbolic"), pixel_size=18))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        mid.append(label(r.title, css=["heading"], ellipsize=True))
        if r.subtitle:
            mid.append(label(r.subtitle, css=["dim", "caption"], ellipsize=True))
        box.append(mid)
        btn = Gtk.Button(label=r.action_label, valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self._run(r))
        box.append(btn)
        row.set_child(box)
        return row

    def _header(self, row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        kind = getattr(row, "result", None).kind if getattr(row, "result", None) else ""
        prev = getattr(before, "result", None).kind if before is not None and getattr(before, "result", None) else None
        group = {"game": tr('Spiele'), "bookmark": tr('Lesezeichen'), "store": tr('Store'), "find": tr('Store')}
        if prev is not None and group.get(prev) == group.get(kind):
            row.set_header(None)
            return
        h = label(group.get(kind, "").upper(), css=["sec-hdr"])
        h.set_margin_top(8)
        h.set_margin_start(10)
        h.set_margin_bottom(2)
        row.set_header(h)

    # ------------------------------------------------------------------ Aktionen
    def _key(self, _ctl, keyval, _code, _state) -> bool:
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            rows = []
            i = 0
            while (r := self.list.get_row_at_index(i)) is not None:
                rows.append(r)
                i += 1
            if not rows:
                return False
            sel = self.list.get_selected_row()
            idx = rows.index(sel) if sel in rows else -1
            idx = max(0, min(len(rows) - 1, idx + (1 if keyval == Gdk.KEY_Down else -1)))
            self.list.select_row(rows[idx])
            return True
        return False

    def _activate_first(self) -> None:
        row = self.list.get_selected_row() or self.list.get_row_at_index(0)
        if row is not None:
            self._run(row.result)

    def _run(self, r: ps.Result) -> None:
        self.close()
        w = self.win
        if r.action == "play":
            w.page.play(r.product_id)
        elif r.action == "install":
            w.page.install(r.product_id)
        elif r.action == "show_bookmark":
            w.open_bookmark(r.bookmark_id)
        elif r.action == "buy":
            w.open_store(r.url or f"/game/{r.product_id}")
        elif r.product_id:
            w.show_game(r.product_id)
