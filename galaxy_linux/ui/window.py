"""Hauptfenster im Aufbau von GOG Galaxy 2.0.

Titelleiste: links Zahnrad-Menü (wie Galaxy 2.1, siehe ui/menus.py) · Avatar-Menü · Suche (über der Seitenleiste),
rechts Zurück/Vor, Lesezeichen, Filter, „+“, Suchfeld, Ansicht und die Fensterknöpfe.
Seitenleiste: SPIELE (Zuletzt gespielt, Alle Spiele, Installiert, Downloads), darunter BIBLIOTHEKEN (eigene
Bibliotheken für Nicht-GOG-Spiele, je ein Eintrag mit Zähler), FREUNDE, STORE.
Inhalt: Cover-Raster in Abschnitten, Downloads-Seite, Spielansicht.
"""
from __future__ import annotations

from ..i18n import tr

import logging
import socket
import time
from pathlib import Path

from .. import APP_NAME, __version__, i18n
from ..api.client import NotLoggedIn
from ..settings import format_bytes, format_minutes
from ..store_links import store_lang, store_url
from . import menus, prefs_registry, ui_registry
from .bridge import GtkBridge
from .dialogs import LoginDialog, PreferencesDialog
from .game_page import GamePage
from .library_sync import LibrarySyncController
from .util import (Adw, Gdk, Gio, GLib, Gtk, ImageLoader, Pango, choose_folder, clamped_picture, fmt_eta, fmt_speed, icon_button, idle, label,
                   open_uri, pick_icon, relative_time, run_async)

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 232
COVER_SIZES = {"XS": (96, 135), "S": (116, 163), "M": (136, 191), "L": (164, 231), "XL": (200, 282)}   # Seitenverhältnis wie GOG-Boxart
SIZE_NAMES = {"XS": tr('Sehr klein'), "S": tr('Klein'), "M": tr('Mittel'), "L": tr('Groß'), "XL": tr('Sehr groß')}
SPACING = {"0": (4, 6), "S": (12, 16), "M": (24, 30), "L": (40, 46)}   # (Spalten, Zeilen) in px
SPACING_NAMES = {"0": tr('Keiner'), "S": tr('Klein'), "M": tr('Mittel'), "L": tr('Groß')}
LIB_VIEWS = ("all", "installed", "recent")        # dazu "lib:<ID>" je eigener Bibliothek
SORTS = [tr('Titel (A–Z)'), tr('Zuletzt gespielt'), tr('Spielzeit'), tr('Erscheinungsdatum')]


def is_lib_view(view: str) -> bool:
    return view in LIB_VIEWS or view.startswith("lib:")


# ----------------------------------------------------------------------------- Bausteine
class NavItem(Gtk.Button):
    def __init__(self, title: str, icon: str) -> None:
        super().__init__()
        self.add_css_class("nav-item")
        self.add_css_class("flat")
        box = Gtk.Box(spacing=10)
        box.append(Gtk.Image(icon_name=icon, pixel_size=16))
        self.lbl = label(title, ellipsize=True)
        self.lbl.set_hexpand(True)
        box.append(self.lbl)
        self.badge = label("", css=["nav-badge"])
        self.badge.set_visible(False)
        box.append(self.badge)
        self.set_child(box)

    def set_badge(self, text: str, accent: bool = False) -> None:
        self.badge.set_text(text)
        self.badge.set_visible(bool(text))
        if accent:
            self.badge.add_css_class("accent")
        else:
            self.badge.remove_css_class("accent")

    def set_selected(self, sel: bool) -> None:
        if sel:
            self.add_css_class("selected")
        else:
            self.remove_css_class("selected")


STATE_TEXT = {"installed": tr('Installiert'), "update": tr('Update verfügbar'), "running": tr('Läuft'), "busy": tr('Wird bearbeitet …'),
              "not_installed": tr('Nicht installiert')}


class CoverCard(Gtk.FlowBoxChild):
    def __init__(self, entry, size: tuple[int, int], show_title: bool) -> None:
        super().__init__()
        self.entry = entry
        self.state = "not_installed"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("cover-card")
        self.overlay = Gtk.Overlay()
        ext = str(entry.id).startswith("ext-")
        self.frame, self.pic = clamped_picture(size[0], size[1], ["cover", "external-cover"] if ext else ["cover"],
                                               placeholder=entry.title)
        self.overlay.set_child(self.frame)
        self.badge = label("", css=["cover-badge"])
        self.badge.set_halign(Gtk.Align.START)
        self.badge.set_valign(Gtk.Align.END)
        self.badge.set_margin_start(6)
        self.badge.set_margin_bottom(6)
        self.badge.set_visible(False)
        self.overlay.add_overlay(self.badge)
        self.new_badge = label(tr('NEU'), css=["new-badge"])     # neu in der Bibliothek (Bibliotheks-Abgleich)
        self.new_badge.set_halign(Gtk.Align.END)
        self.new_badge.set_valign(Gtk.Align.START)
        self.new_badge.set_margin_end(6)
        self.new_badge.set_margin_top(6)
        self.new_badge.set_visible(False)
        self.overlay.add_overlay(self.new_badge)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.END)
        self.progress.add_css_class("cover-progress")
        self.progress.set_visible(False)
        self.overlay.add_overlay(self.progress)
        box.append(self.overlay)
        self.title = label(entry.title, css=["cover-title"], xalign=0.5, ellipsize=True)
        self.title.set_size_request(size[0], -1)
        self.title.set_max_width_chars(1)
        self.title.set_visible(show_title)
        box.append(self.title)
        self.set_child(box)
        self.set_tooltip_text(entry.title)

    def set_new(self, new: bool) -> None:
        self.new_badge.set_visible(new)

    def set_state(self, state: str, fraction: float | None = None, busy_text: str = tr('LÄDT')) -> None:
        self.state = state
        text = {"update": tr('UPDATE'), "running": tr('LÄUFT'), "busy": busy_text}.get(state, "")
        self.badge.set_text(text)
        self.badge.set_visible(bool(text))
        for c in ("update", "running"):
            self.badge.remove_css_class(c)
        if state in ("update", "running"):
            self.badge.add_css_class(state)
        self.progress.set_visible(state == "busy" and fraction is not None)
        if fraction is not None:
            self.progress.set_fraction(fraction)


LIST_COLUMNS = ((tr('Status'), 18), (tr('Spielzeit'), 11), (tr('Zuletzt gespielt'), 14), (tr('Größe'), 9))


class GameListRow(Gtk.ListBoxRow):
    """Listenansicht wie in Galaxy 2.0: kleines Cover, Titel, Status, Spielzeit, zuletzt gespielt, Größe."""

    def __init__(self, win: "MainWindow", entry) -> None:
        super().__init__()
        self.entry = entry
        self.state = "not_installed"
        self.add_css_class("game-row")
        b = win.backend
        box = Gtk.Box(spacing=14, margin_top=4, margin_bottom=4, margin_start=8, margin_end=12)
        frame, self.pic = clamped_picture(34, 48, ["cover", "row-cover"])
        frame.set_valign(Gtk.Align.CENTER)
        box.append(frame)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = label(entry.title, css=["row-title"], ellipsize=True)
        g = b.registry.get(entry.id)
        lib = b.library_of(entry.id) if g and g.method == "external" else None
        src = (lib.name if lib else tr('Extern')) if g and g.method == "external" else \
            tr('GOG') + (tr(' · Linux') if "linux" in entry.platforms and "windows" not in entry.platforms else "")
        tt = Gtk.Box(spacing=8)
        tt.append(self.title)
        self.new_badge = label(tr('NEU'), css=["new-badge"])
        self.new_badge.set_valign(Gtk.Align.CENTER)
        self.new_badge.set_visible(False)
        tt.append(self.new_badge)
        tb.append(tt)
        tb.append(label(src, css=["dim", "caption"]))
        box.append(tb)
        minutes, last = b.playtime.get(entry.id)
        size = format_bytes(g.install_size) if g and g.install_size else "–"
        self.status = label("", css=["row-cell"], ellipsize=True)
        values = [self.status, label(format_minutes(minutes) if minutes else "–", css=["row-cell"]),
                  label(relative_time(last) if last else "–", css=["row-cell"]), label(size, css=["row-cell"])]
        for (_, width), lbl in zip(LIST_COLUMNS, values):
            lbl.set_width_chars(width)
            lbl.set_max_width_chars(width)
            lbl.set_xalign(0)
            box.append(lbl)
        self.set_child(box)
        self.set_tooltip_text(entry.title)

    def set_new(self, new: bool) -> None:
        self.new_badge.set_visible(new)

    def set_state(self, state: str, fraction: float | None = None, busy_text: str = tr('LÄDT')) -> None:
        self.state = state
        txt = STATE_TEXT.get(state, "")
        if state == "busy":
            txt = {tr('PREFIX'): tr('Prefix wird angelegt'), tr('PRÜFT'): tr('Wird geprüft'), tr('VERSCHIEBT'): tr('Wird verschoben')}.get(busy_text, tr('Lädt herunter'))
            if fraction:
                txt += f" · {fraction * 100:.0f} %"
        self.status.set_text(txt)
        for c in ("installed", "update", "running", "busy"):
            self.status.remove_css_class(f"st-{c}")
        self.status.add_css_class(f"st-{state}")


class LibrarySection(Gtk.Box):
    def __init__(self, win: "MainWindow", title: str, view: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.win = win
        self.layout = view["layout"]
        head = Gtk.Box(spacing=4)
        self.toggle = Gtk.Button()
        self.toggle.add_css_class("flat")
        self.toggle.add_css_class("sec-hdr-btn")
        hb = Gtk.Box(spacing=6)
        self.arrow = Gtk.Image(icon_name=pick_icon("pan-down-symbolic", "go-down-symbolic"), pixel_size=12)
        hb.append(self.arrow)
        self.title_lbl = label(title.upper(), css=["sec-hdr"])
        hb.append(self.title_lbl)
        self.count_lbl = label("", css=["sec-hdr", "dim"])
        hb.append(self.count_lbl)
        self.toggle.set_child(hb)
        self.toggle.connect("clicked", self._toggle)
        head.append(self.toggle)
        self.append(head)
        if self.layout == "list":
            self.header = Gtk.Box(spacing=14, margin_start=8, margin_end=12)
            self.header.add_css_class("list-header")
            sp = Gtk.Box()
            sp.set_size_request(34, -1)
            self.header.append(sp)
            t = label(tr('TITEL'), css=["stat-key"])
            t.set_hexpand(True)
            self.header.append(t)
            for name, width in LIST_COLUMNS:
                h = label(name.upper(), css=["stat-key"])
                h.set_width_chars(width)
                h.set_max_width_chars(width)
                h.set_xalign(0)
                self.header.append(h)
            self.append(self.header)
            self.flow = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
            self.flow.add_css_class("game-list")
            self.flow.connect("row-activated", lambda _l, r: win.show_game(r.entry.id))
        else:
            col, row = SPACING[view["spacing"] if view["spacing"] in SPACING else "M"]
            self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=col,
                                    row_spacing=row, max_children_per_line=40, min_children_per_line=1,
                                    valign=Gtk.Align.START, halign=Gtk.Align.START)
            self.flow.connect("child-activated", lambda _f, c: win.show_game(c.entry.id))
        self.flow.set_filter_func(win.card_filter)
        self.append(self.flow)
        self.cards: dict[str, Gtk.Widget] = {}

    def _toggle(self, *_: object) -> None:
        vis = not self.flow.get_visible()
        self.flow.set_visible(vis)
        if self.layout == "list":
            self.header.set_visible(vis)
        self.arrow.set_from_icon_name(pick_icon("pan-down-symbolic") if vis else pick_icon("pan-end-symbolic", "go-next-symbolic"))

    def set_entries(self, entries: list, size: tuple[int, int], show_title: bool) -> None:
        self.flow.remove_all()
        self.cards.clear()
        for e in entries:
            card = GameListRow(self.win, e) if self.layout == "list" else CoverCard(e, size, show_title)
            self.cards[e.id] = card
            self.flow.append(card)
            self.win.load_cover(e, card.pic)
            self.win.apply_card_state(card)
            card.set_new(self.win.library_sync.is_new(e.id))
        self.count_lbl.set_text(str(len(entries)))
        self.set_visible(bool(entries))


# ----------------------------------------------------------------------------- Fenster
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.backend = backend
        prefs_registry.load_extensions()    # ui/prefs_*.py, ui/ext_*.py – vor dem Aufbau von Spielansicht und Menüs
        self.images = ImageLoader(backend.api)
        self.set_default_size(1360, 860)
        self.add_css_class("galaxy")
        self.view = "all"
        self.current_pid: str | None = None
        self.history: list[tuple[str, str | None]] = []
        self.hist_idx = -1
        self.filters = {"installed": False, "not_installed": False, "favorites": False, "updates": False,
                        "windows": False, "linux": False}
        self._search = ""
        self.sections: list[LibrarySection] = []
        self.library_sync = LibrarySyncController(self)   # Abgleich mit GOG: Start, Intervall, Fokus, Login, Kauf, F5

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._build_header())
        body = Gtk.Box(vexpand=True)
        body.append(self._build_sidebar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        body.append(self.stack)
        root.append(body)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(root)
        self.set_content(self.toasts)

        # Bibliothek (Abschnitte)
        self.lib_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.lib_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28, margin_top=18, margin_bottom=28,
                               margin_start=28, margin_end=28, valign=Gtk.Align.START)
        self.lib_scroll.set_child(self.lib_box)
        self.stack.add_named(self.lib_scroll, "library")
        # Downloads
        self.dl_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.dl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=18, margin_bottom=28,
                              margin_start=28, margin_end=28, valign=Gtk.Align.START)
        self.dl_scroll.set_child(self.dl_box)
        self.stack.add_named(self.dl_scroll, "downloads")
        # Spielansicht
        self.page = GamePage(self)
        self.stack.add_named(self.page, "game")
        # Willkommen
        self.welcome = Adw.StatusPage(title="galaxy-linux", description=tr('Melde dich bei GOG an, um deine Bibliothek zu laden.'))
        self.welcome.set_icon_name(pick_icon("applications-games-symbolic", "input-gaming-symbolic"))
        wb = Gtk.Button(label=tr('BEI GOG ANMELDEN'))
        wb.add_css_class("btn-play")
        wb.set_halign(Gtk.Align.CENTER)
        wb.connect("clicked", lambda *_: self.do_login())
        self.welcome.set_child(wb)
        self.stack.add_named(self.welcome, "welcome")

        self.backend.jobs.listeners.append(lambda job: idle(self._on_job, job))
        # Rückfragen der Launch-Hooks (z. B. Cloud-Konflikt) als Dialog über diesem Fenster
        self.bridge = GtkBridge(self)
        self.backend.launcher.ui_bridge = self.bridge
        self.connect("close-request", lambda *_: (self.bridge.cancel_all(), False)[1])
        self._monitors: list = []
        self._scan_timer = 0
        self._scanning = False
        self._seen_unknown: set[str] = set()
        self._prefix_done: list[str] = []
        self._update_avatar()
        self.navigate("all")
        self.setup_folder_watch()
        GLib.timeout_add_seconds(2, lambda: (self.scan_folders_async(quiet=True), False)[1])
        # Bibliothek aus dem Cache steht schon – Abgleich kurz danach (leerer Cache: sofort), danach Update-Suche
        self.library_sync.start()
        ui_registry.run_window_hooks(self)      # Erweiterungen (ui/ext_*.py): Timer, Fokus, eigene Seiten …

    # ------------------------------------------------------------------ Kopfzeile
    def _build_header(self) -> Gtk.Widget:
        handle = Gtk.WindowHandle()
        bar = Gtk.Box()
        bar.add_css_class("titlebar-galaxy")
        left = Gtk.Box(spacing=0)
        left.add_css_class("sidebar-head")
        left.set_size_request(SIDEBAR_WIDTH, 44)
        left.set_hexpand(False)
        # Zahnrad = Hauptmenü wie in Galaxy 2.1 (Einstellungen, Bibliothek aktualisieren, Neu bei uns …)
        cog = Gtk.MenuButton(icon_name=pick_icon("emblem-system-symbolic", "preferences-system-symbolic"), tooltip_text=tr('Menü'),
                             menu_model=menus.main_menu_model())
        cog.add_css_class("flat")
        cog.add_css_class("hdr-btn")
        cog.set_margin_start(10)
        cog.set_valign(Gtk.Align.CENTER)
        self.main_menu_btn = cog
        left.append(cog)
        sp1 = Gtk.Box(hexpand=True)
        left.append(sp1)
        self.avatar = Adw.Avatar(size=26, show_initials=True, text=tr('GOG'))
        self.avatar_btn = Gtk.MenuButton(child=self.avatar, tooltip_text=tr('Konto'))
        self.avatar_btn.add_css_class("flat")
        self.avatar_btn.add_css_class("hdr-btn")
        self.avatar_btn.set_valign(Gtk.Align.CENTER)
        self.avatar_header = None          # Bild im Kopf des Avatar-Menüs (menus.avatar_popover)
        left.append(self.avatar_btn)
        sp2 = Gtk.Box(hexpand=True)
        left.append(sp2)
        sb = icon_button(pick_icon("system-search-symbolic", "edit-find-symbolic"), tr('Suchen'), ["flat", "hdr-btn"])
        sb.set_margin_end(10)
        sb.set_valign(Gtk.Align.CENTER)
        sb.connect("clicked", lambda *_: self.search.grab_focus())
        left.append(sb)
        bar.append(left)

        right = Gtk.Box(spacing=4, hexpand=True)
        right.add_css_class("content-head")
        self.back_btn = icon_button(pick_icon("go-previous-symbolic"), tr('Zurück'), ["flat", "hdr-btn"])
        self.back_btn.set_margin_start(10)
        self.back_btn.connect("clicked", lambda *_: self.go_back())
        self.fwd_btn = icon_button(pick_icon("go-next-symbolic"), tr('Vor'), ["flat", "hdr-btn"])
        self.fwd_btn.connect("clicked", lambda *_: self.go_forward())
        self.bookmark_btn = Gtk.ToggleButton(icon_name=pick_icon("user-bookmarks-symbolic", "bookmark-new-symbolic", "starred-symbolic"),
                                             tooltip_text=tr('Favorit'))
        self.bookmark_btn.add_css_class("flat")
        self.bookmark_btn.add_css_class("hdr-btn")
        self.bookmark_btn.connect("toggled", self._on_bookmark)
        for w in (self.back_btn, self.fwd_btn, self.bookmark_btn):
            w.set_valign(Gtk.Align.CENTER)
            right.append(w)

        center = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, hexpand=True, valign=Gtk.Align.CENTER)
        self.filter_btn = Gtk.MenuButton(popover=self._filter_popover())
        fb = Gtk.Box(spacing=6)
        fb.append(Gtk.Image(icon_name=pick_icon("funnel-symbolic", "view-filter-symbolic", "edit-find-replace-symbolic"), pixel_size=14))
        fb.append(label(tr('Filter')))
        self.filter_btn.set_child(fb)
        self.filter_btn.add_css_class("hdr-pill")
        center.append(self.filter_btn)
        plus = Gtk.MenuButton(icon_name=pick_icon("list-add-symbolic"), popover=self._plus_popover(), tooltip_text=tr('Bibliothek'))
        plus.add_css_class("hdr-pill")
        center.append(plus)
        self.search = Gtk.SearchEntry(placeholder_text=tr('Spiele suchen'))
        self.search.add_css_class("hdr-search")
        self.search.set_size_request(230, -1)
        self.search.connect("search-changed", self._on_search)
        center.append(self.search)
        view_btn = Gtk.MenuButton(icon_name=pick_icon("preferences-other-symbolic", "emblem-system-symbolic", "view-more-symbolic"),
                                  tooltip_text=tr('Ansicht'))
        # bei jedem Öffnen neu aufbauen: jede eigene Bibliothek hat ihre eigene gespeicherte Ansicht
        view_btn.set_create_popup_func(lambda b: b.set_popover(self._view_popover()))
        view_btn.add_css_class("hdr-pill")
        center.append(view_btn)
        right.append(center)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        right.append(self.spinner)
        controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.CENTER)
        controls.set_margin_end(6)
        right.append(controls)
        bar.append(right)
        handle.set_child(bar)
        return handle

    def _plus_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for text, cb in ((tr('Spiel hinzufügen …'), lambda: self.add_game(self._view_lib_id())),
                         (tr('Bibliothek anlegen …'), lambda: self.create_library()),
                         (tr('Spiele-Ordner nach Spielen durchsuchen'), lambda: self.scan_folders_async(quiet=False)),
                         (tr('Fehlende Prefixe für alle Spiele anlegen'), lambda: self.create_missing_prefixes()),
                         (tr('Verwaiste Prefixe entfernen …'), lambda: self.cleanup_prefixes()),
                         (tr('Spiele-Ordner öffnen'), lambda: self.backend.launcher.open_path(self.backend.settings.install_dir))):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            box.append(b)
        pop.set_child(box)
        return pop

    def _filter_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
        box.append(label(tr('FILTER'), css=["sec-hdr"]))
        self.filter_checks: dict[str, Gtk.CheckButton] = {}
        for key, text in (("installed", tr('Installiert')), ("not_installed", tr('Nicht installiert')), ("favorites", tr('Favoriten')),
                          ("updates", tr('Update verfügbar')), ("windows", tr('Windows')), ("linux", tr('Linux-nativ'))):
            c = Gtk.CheckButton(label=text)
            c.connect("toggled", lambda b, k=key: self._set_filter(k, b.get_active()))
            self.filter_checks[key] = c
            box.append(c)
        hidden = Gtk.CheckButton(label=tr('Versteckte Spiele anzeigen'), active=self.backend.settings.show_hidden)
        hidden.connect("toggled", self._toggle_hidden)
        box.append(hidden)
        reset = Gtk.Button(label=tr('Zurücksetzen'))
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: [c.set_active(False) for c in self.filter_checks.values()])
        box.append(reset)
        pop.set_child(box)
        return pop

    def _view_popover(self) -> Gtk.Popover:
        """Ansicht der Bibliothek: Raster/Liste, Covergröße, Abstand, Titel, Abschnitte, Sortierung (wird gespeichert –
        für die GOG-Ansichten gemeinsam, für jede eigene Bibliothek getrennt)."""
        lib = self._view_lib()
        v = self.backend.settings.view_settings(lib)
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        scope = label(tr('Gilt für die Bibliothek „{0}“').format(lib.name) if lib else tr('Gilt für Alle Spiele, Installiert und Zuletzt gespielt'),
                      css=["dim", "caption"])
        box.append(scope)

        def group(title: str, options: list[tuple[str, str]], current: str, key: str) -> Adw.ToggleGroup:
            box.append(label(title, css=["sec-hdr"]))
            tg = Adw.ToggleGroup()
            for name, text in options:
                tg.add(Adw.Toggle(name=name, label=text))
            tg.set_active_name(current)
            tg.connect("notify::active-name", lambda g, _: self._set_view(**{key: g.get_active_name()}))
            box.append(tg)
            return tg

        group(tr('DARSTELLUNG'), [("grid", tr('Raster')), ("list", tr('Liste'))], v["layout"], "layout")
        group(tr('COVERGRÖSSE'), [(k, k) for k in COVER_SIZES], v["cover_size"], "cover_size")
        group(tr('ABSTAND'), list(SPACING_NAMES.items()), v["spacing"], "spacing")
        titles = Gtk.CheckButton(label=tr('Titel unter den Covern'), active=v["titles"])
        titles.connect("toggled", lambda b: self._set_view(titles=b.get_active()))
        box.append(titles)
        secs = Gtk.CheckButton(label=tr('Abschnitte (Als Nächstes spielen …)'), active=v["sections"])
        secs.connect("toggled", lambda b: self._set_view(sections=b.get_active()))
        box.append(secs)
        box.append(label(tr('SORTIEREN'), css=["sec-hdr"]))
        sort_dd = Gtk.DropDown.new_from_strings(SORTS)
        sort_dd.set_selected(v["sort"] if 0 <= v["sort"] < len(SORTS) else 0)
        sort_dd.connect("notify::selected", lambda d, _: self._set_view(sort=d.get_selected()))
        box.append(sort_dd)
        pop.set_child(box)
        return pop

    # ------------------------------------------------------------------ Seitenleiste
    def _build_sidebar(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("sidebar")
        outer.set_size_request(SIDEBAR_WIDTH, -1)
        outer.set_hexpand(False)   # feste Breite – Kinder mit hexpand dürfen die Leiste nicht verbreitern
        # viele eigene Bibliotheken: die Einträge rollen, Status und Version bleiben unten stehen
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(box)
        outer.append(scroll)
        self.nav: dict[str, NavItem] = {}

        def section(title: str) -> None:
            box.append(label(title, css=["nav-section"]))

        def item(key: str, title: str, icon: str, enabled: bool = True, cb=None) -> NavItem:
            it = NavItem(title, icon)
            if cb is None:
                it.connect("clicked", lambda *_: self.navigate(key))
            else:
                it.connect("clicked", lambda *_: cb())
            it.set_sensitive(enabled)
            if not enabled:
                it.set_tooltip_text(tr('In diesem Port nicht verfügbar'))
            self.nav[key] = it
            box.append(it)
            return it

        section(tr('SPIELE'))
        item("recent", tr('Zuletzt gespielt'), pick_icon("document-open-recent-symbolic", "alarm-symbolic"))
        item("all", tr('Alle Spiele'), pick_icon("view-grid-symbolic", "view-app-grid-symbolic"))
        item("installed", tr('Installiert'), pick_icon("computer-symbolic", "video-display-symbolic"))
        item("downloads", tr('Downloads'), pick_icon("folder-download-symbolic", "go-down-symbolic"))
        # BIBLIOTHEKEN: eigene Bibliotheken (wie die Plattform-Einträge in Galaxy 2.0), „+“ legt eine neue an
        head = Gtk.Box()
        head.add_css_class("nav-section-row")
        sec = label(tr('BIBLIOTHEKEN'), css=["nav-section"])
        sec.set_hexpand(True)
        head.append(sec)
        add = icon_button(pick_icon("list-add-symbolic"), tr('Bibliothek anlegen …'), ["flat", "nav-add"])
        add.set_valign(Gtk.Align.END)
        add.connect("clicked", lambda *_: self.create_library())
        head.append(add)
        box.append(head)
        self.libnav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.libnav_box)
        self._rebuild_library_nav()
        section(tr('FREUNDE'))
        item("activity", tr('Aktivität'), pick_icon("emblem-shared-symbolic", "network-workgroup-symbolic"), enabled=False)
        item("friends", tr('Freunde online'), pick_icon("system-users-symbolic", "avatar-default-symbolic"), enabled=False)
        item("messages", tr('Nachrichten'), pick_icon("mail-unread-symbolic", "chat-symbolic"), enabled=False)
        section(tr('STORE'))
        item("featured", tr('Empfohlen'), pick_icon("starred-symbolic", "emblem-favorite-symbolic"), cb=lambda: self.open_store("/games"))
        item("discounts", tr('Angebote'), pick_icon("emblem-favorite-symbolic", "tag-symbolic"), cb=lambda: self.open_store("/promo"))
        box = outer
        self.status_lbl = label("", css=["dim", "caption"])
        self.status_lbl.set_margin_start(22)
        self.status_lbl.set_margin_bottom(4)
        self.status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.status_lbl)
        # Version und Rechner wie im Alltagstool – man sieht sofort, welche Fassung wo läuft
        ver = label(tr('galaxy-linux v{0} · {1}').format(__version__, socket.gethostname()), css=["dim", "caption"])
        ver.set_margin_start(22)
        ver.set_margin_bottom(12)
        ver.set_ellipsize(Pango.EllipsizeMode.END)
        ver.set_tooltip_text(tr('Version von galaxy-linux und Name dieses Rechners'))
        box.append(ver)
        return outer

    def _rebuild_library_nav(self) -> None:
        """Ein Seitenleisten-Eintrag je eigener Bibliothek; Rechtsklick öffnet ihr Menü."""
        for key in [k for k in self.nav if k.startswith("lib:")]:
            self.nav.pop(key)
        child = self.libnav_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.libnav_box.remove(child)
            child = nxt
        icon = pick_icon("application-x-executable-symbolic", "applications-games-symbolic")
        for lib in self.backend.libraries():
            key = f"lib:{lib.id}"
            it = NavItem(lib.name, icon)
            it.set_tooltip_text(tr('{0}\nPrefixe: {1}').format(lib.games_dir, lib.prefix_dir))
            it.connect("clicked", lambda _b, k=key: self.navigate(k))
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda _g, _n, x, y, w=it, lid=lib.id: self._library_menu(lid, w).popup())
            it.add_controller(click)
            self.nav[key] = it
            self.libnav_box.append(it)
        if not self.backend.libraries():
            it = NavItem(tr('Bibliothek anlegen …'), pick_icon("list-add-symbolic"))
            it.add_css_class("nav-hint")
            it.connect("clicked", lambda *_: self.create_library())
            self.libnav_box.append(it)
        if getattr(self, "view", "").startswith("lib:") and self.view in self.nav:
            self.nav[self.view].set_selected(True)

    # ------------------------------------------------------------------ Navigation
    def navigate(self, view: str, pid: str | None = None, push: bool = True) -> None:
        if view.startswith("lib:") and self.backend.get_library(view[4:]) is None:
            view = "all"                     # Bibliothek wurde inzwischen gelöscht
        if push:
            self.history = self.history[: self.hist_idx + 1]
            self.history.append((view, pid))
            self.hist_idx = len(self.history) - 1
        self.view = view
        self.current_pid = pid if view == "game" else None
        for k, it in self.nav.items():
            it.set_selected(k == view)
        if view == "game" and pid:
            self.library_sync.seen(pid)          # NEU entfällt, sobald die Spielansicht offen war
            self.page.set_game(pid)
            self.stack.set_visible_child_name("game")
        elif view == "downloads":
            self._rebuild_downloads()
            self.stack.set_visible_child_name("downloads")
        else:
            if not self.backend.library and not self.backend.registry.all() and not self.backend.api.logged_in:
                self.stack.set_visible_child_name("welcome")
            else:
                self._rebuild_library()
                self.stack.set_visible_child_name("library")
                self.lib_scroll.get_vadjustment().set_value(0)
        self.back_btn.set_sensitive(self.hist_idx > 0)
        self.fwd_btn.set_sensitive(self.hist_idx < len(self.history) - 1)
        self._sync_bookmark()
        self.update_nav_badges()

    def go_back(self) -> None:
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.navigate(*self.history[self.hist_idx], push=False)

    def go_forward(self) -> None:
        if self.hist_idx < len(self.history) - 1:
            self.hist_idx += 1
            self.navigate(*self.history[self.hist_idx], push=False)

    def show_game(self, pid: str) -> None:
        self.navigate("game", str(pid))

    def show_grid(self) -> None:
        self.navigate("all")

    def _sync_bookmark(self) -> None:
        self.bookmark_btn.set_sensitive(self.view == "game")
        self._bookmark_guard = True
        self.bookmark_btn.set_active(bool(self.current_pid and self.backend.game_settings.get(self.current_pid).favorite))
        self._bookmark_guard = False

    def _on_bookmark(self, btn: Gtk.ToggleButton) -> None:
        if getattr(self, "_bookmark_guard", False) or not self.current_pid:
            return
        self.backend.game_settings.update(self.current_pid, favorite=btn.get_active())
        self.toast(tr('Als Favorit markiert') if btn.get_active() else tr('Favorit entfernt'))

    # ------------------------------------------------------------------ Bibliothek
    def _view_lib_id(self) -> str | None:
        return self.view[4:] if self.view.startswith("lib:") else None

    def _view_lib(self):
        lid = self._view_lib_id()
        return self.backend.get_library(lid) if lid else None

    def _custom_entries(self, lib_id: str | None = None) -> list:
        """Spiele der eigenen Bibliotheken (oder nur einer) als Bibliothekseinträge."""
        out = []
        games = self.backend.library_games(lib_id) if lib_id else [g for g in self.backend.registry.all() if g.method == "external"]
        for g in games:
            if self.backend.game_settings.get(g.product_id).hidden and not self.backend.settings.show_hidden:
                continue
            e = self.backend.entry(g.product_id)
            if e:
                out.append(e)
        return out

    def _entries(self) -> list:
        """GOG-Bibliothek plus installierte GOG-Spiele (ohne externe Spiele)."""
        entries = {e.id: e for e in self.backend.visible_library()}
        for g in self.backend.registry.all():
            if g.method == "external":
                continue
            if g.product_id not in entries:
                e = self.backend.entry(g.product_id)
                if e:
                    entries[e.id] = e
        return list(entries.values())

    def _sorted(self, entries: list, sort: int = 0) -> list:
        pt = self.backend.playtime
        if sort == 1:
            return sorted(entries, key=lambda e: -pt.get(e.id)[1])
        if sort == 2:
            return sorted(entries, key=lambda e: -pt.get(e.id)[0])
        if sort == 3:
            return sorted(entries, key=lambda e: e.release_date or "", reverse=True)
        return sorted(entries, key=lambda e: e.title.lower())

    def _rebuild_library(self) -> None:
        child = self.lib_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lib_box.remove(child)
            child = nxt
        self.sections.clear()
        lib = self._view_lib()
        v = self.backend.settings.view_settings(lib)
        size = COVER_SIZES.get(v["cover_size"], COVER_SIZES["S"])
        reg, pt = self.backend.registry, self.backend.playtime
        ents = [] if lib else self._sorted(self._entries(), v["sort"])

        def add(title: str, items: list) -> None:
            if not items:
                return
            sec = LibrarySection(self, title, v)
            sec.set_entries(items, size, v["titles"])
            self.sections.append(sec)
            self.lib_box.append(sec)

        if lib is not None:
            self.lib_box.append(self._library_header(lib))
            games = self._sorted(self._custom_entries(lib.id), v["sort"])
            if v["sections"]:
                add(tr('Als Nächstes spielen'), [e for e in games if pt.get(e.id)[0] == 0][:12])
            add(lib.name, games)
            if not games:
                hint = label(tr('Noch keine Spiele in „{0}“. Spielordner nach {1} kopieren – sie werden nach dem Kopieren automatisch übernommen – oder einen beliebigen Ordner mit „Spiel hinzufügen …“ aufnehmen.').format(lib.name, lib.games_dir), css=["dim"], wrap=True)
                self.lib_box.append(hint)
        elif self.view == "installed":
            add(tr('Installiert'), [e for e in ents if reg.is_installed(e.id)])
        elif self.view == "recent":
            ents = ents + self._custom_entries()
            played = [e for e in ents if pt.get(e.id)[1] > 0]
            played.sort(key=lambda e: -pt.get(e.id)[1])
            add(tr('Zuletzt gespielt'), played[:30])
            inst = [g for g in reg.all()]
            inst.sort(key=lambda g: -g.installed_at)
            recent_inst = [self.backend.entry(g.product_id) for g in inst[:12]]
            if v["sections"]:
                add(tr('Kürzlich installiert'), [e for e in recent_inst if e])
            if not played and not inst:
                self.lib_box.append(label(tr('Noch nichts gespielt – installiere ein Spiel aus „Alle Spiele“.'), css=["dim"]))
        else:
            if v["sections"]:
                add(tr('Neu in der Bibliothek'), self.library_sync.new_entries(ents))
                next_to_play = [e for e in ents if reg.is_installed(e.id) and pt.get(e.id)[0] == 0]
                add(tr('Als Nächstes spielen'), next_to_play[:12])
            add(tr('Alle Spiele'), ents)
            if not ents:
                self.lib_box.append(label(tr('Keine Spiele in der Bibliothek. Über das Zahnrad → „Bibliothek aktualisieren“ (F5) die Bibliothek von GOG laden.'), css=["dim"]))

    def _library_header(self, lib) -> Gtk.Widget:
        """Kopf der Bibliotheksansicht: Ordner und die häufigsten Aktionen als Knöpfe, der Rest im „⋯“-Menü."""
        row = Gtk.Box(spacing=8)
        row.add_css_class("lib-head")
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        path = label(lib.games_dir, css=["dim", "caption"], ellipsize=True)
        path.set_tooltip_text(tr('Spiele: {0}\nPrefixe: {1}').format(lib.games_dir, lib.prefix_dir))
        info.append(path)
        row.append(info)
        for text, icon, cb in ((tr('Spiel hinzufügen …'), "list-add-symbolic", lambda *_: self.add_game(lib.id)),
                               (tr('Ordner durchsuchen'), "view-refresh-symbolic", lambda *_: self.scan_folders_async(quiet=False))):
            b = Gtk.Button()
            bb = Gtk.Box(spacing=6)
            bb.append(Gtk.Image(icon_name=pick_icon(icon), pixel_size=14))
            bb.append(label(text))
            b.set_child(bb)
            b.add_css_class("hdr-pill")
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", cb)
            row.append(b)
        more = Gtk.MenuButton(icon_name=pick_icon("view-more-symbolic", "open-menu-symbolic"), tooltip_text=tr('Weitere Aktionen'))
        more.add_css_class("hdr-pill")
        more.set_valign(Gtk.Align.CENTER)
        more.set_create_popup_func(lambda b: b.set_popover(self._library_menu(lib.id)))
        row.append(more)
        return row

    def _library_menu(self, lib_id: str, parent: Gtk.Widget | None = None) -> Gtk.Popover:
        """Aktionen einer eigenen Bibliothek – jeder Eintrag führt direkt aus (Löschen mit Rückfrage)."""
        lib = self.backend.get_library(lib_id)
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if lib is not None:
            box.append(label(lib.name.upper(), css=["sec-hdr"]))
            for text, cb in ((tr('Öffnen'), lambda: self.navigate(f"lib:{lib_id}")),
                             (tr('Spiel hinzufügen …'), lambda: self.add_game(lib_id)),
                             (tr('Ordner nach Spielen durchsuchen'), lambda: self.scan_folders_async(quiet=False)),
                             (tr('Fehlende Prefixe anlegen'), lambda: self.create_missing_prefixes(lib_id)),
                             (tr('Verwaiste Prefixe entfernen …'), lambda: self.cleanup_prefixes(lib_id)),
                             (tr('Spiele-Ordner öffnen'), lambda: self.backend.launcher.open_path(lib.games_dir)),
                             (tr('Prefix-Ordner öffnen'), lambda: self.backend.launcher.open_path(lib.prefix_dir)),
                             (tr('Bibliothek bearbeiten …'), lambda: self.open_preferences("libraries")),
                             (tr('Bibliothek löschen …'), lambda: self.delete_library(lib_id))):
                b = Gtk.Button(label=text)
                b.add_css_class("flat")
                b.get_child().set_xalign(0)
                if text == tr('Bibliothek löschen …'):
                    b.add_css_class("destructive-text")
                b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
                box.append(b)
        pop.set_child(box)
        if parent is not None:
            pop.set_parent(parent)
            pop.set_has_arrow(True)
            pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        return pop

    def load_cover(self, entry, picture: Gtk.Picture) -> None:
        """Echte Hochkant-Boxart aus der GOG-Spieledatenbank; fehlt sie, das Querformatbild vollständig zeigen."""
        pid = entry.id

        def got(url: str) -> None:
            if url:
                self.images.load(url, picture.set_paintable)
                return
            fallback = entry.image_url("_392.jpg")
            if fallback:
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                self.images.load(fallback, picture.set_paintable)
        run_async(lambda: self.backend.cover_url(pid), got, lambda e: got(""))

    def card_filter(self, child: CoverCard) -> bool:
        e = child.entry
        if self._search and self._search not in e.title.lower():
            return False
        st = child.state
        f = self.filters
        if f["installed"] and st not in ("installed", "update", "running", "busy"):
            return False
        if f["not_installed"] and st != "not_installed":
            return False
        if f["favorites"] and not self.backend.game_settings.get(e.id).favorite:
            return False
        if f["updates"] and st != "update":
            return False
        if f["windows"] and "windows" not in e.platforms:
            return False
        if f["linux"] and "linux" not in e.platforms:
            return False
        return True

    def apply_card_state(self, card: CoverCard) -> None:
        pid = card.entry.id
        state = self.backend.state(pid)
        frac, busy_text = None, tr('LÄDT')
        if state == "busy":
            j = self.backend.jobs.active_for(pid)
            if j and j.kind == "prefix":
                busy_text = tr('PREFIX')
            elif j and j.kind == "move":
                busy_text = tr('VERSCHIEBT')
            elif j and j.kind == "verify":
                busy_text = tr('PRÜFT')
            frac = j.progress.fraction if j and j.progress.total_bytes else (None if busy_text == tr('PREFIX') else 0.0)
        card.set_state(state, frac, busy_text)

    def refresh_game_state(self, pid: str) -> None:
        pid = str(pid)
        for sec in self.sections:
            card = sec.cards.get(pid)
            if card:
                self.apply_card_state(card)
                sec.flow.invalidate_filter()
        self.update_nav_badges()

    update_row_status = refresh_game_state  # Kompatibilität

    def update_nav_badges(self) -> None:
        ents = self._entries()
        inst = len([e for e in ents if self.backend.registry.is_installed(e.id)])
        self.nav["all"].set_badge(f"{len(ents):,}".replace(",", "."))
        self.nav["installed"].set_badge(str(inst))
        for lib in self.backend.libraries():
            it = self.nav.get(f"lib:{lib.id}")
            if it is not None:
                it.set_badge(str(len(self._custom_entries(lib.id))))
        active = len(self.backend.jobs.active())
        self.nav["downloads"].set_badge(str(active) if active else "", accent=bool(active))
        upd = len(self.backend.updates)
        self.status_lbl.set_text(tr('{0} Update(s) verfügbar').format(upd) if upd else "")

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._search = entry.get_text().strip().lower()
        if self.view in ("game", "downloads") and self._search:
            self.navigate("all")
        for sec in self.sections:
            sec.flow.invalidate_filter()

    def _set_filter(self, key: str, val: bool) -> None:
        self.filters[key] = val
        for sec in self.sections:
            sec.flow.invalidate_filter()

    def _toggle_hidden(self, btn: Gtk.CheckButton) -> None:
        self.backend.settings.show_hidden = btn.get_active()
        self.backend.save_settings()
        if is_lib_view(self.view):
            self._rebuild_library()

    def _set_view(self, **changes) -> None:
        """Ansicht speichern: in einer eigenen Bibliothek nur für diese, sonst für die GOG-Ansichten."""
        lib = self._view_lib()
        if lib is not None:
            self.backend.update_library(lib.id, view={**lib.view, **changes})
        else:
            st = self.backend.settings
            for k, v in changes.items():
                setattr(st, f"library_{k}", v)
            st.save()
        if is_lib_view(self.view):
            y = self.lib_scroll.get_vadjustment().get_value()
            self._rebuild_library()
            GLib.idle_add(lambda: (self.lib_scroll.get_vadjustment().set_value(y), False)[1])

    def populate(self) -> None:
        if is_lib_view(self.view):
            self._rebuild_library()
            self.stack.set_visible_child_name("library")
        self.update_nav_badges()

    # ------------------------------------------------------------------ Konto / Daten
    def _update_avatar(self) -> None:
        user = self.backend.api.user or {}
        name = user.get("username") or ("" if not self.backend.api.logged_in else tr('GOG'))
        self.avatar.set_text(name or tr('GOG'))
        self.avatar_btn.set_tooltip_text(name or tr('Nicht angemeldet'))
        self.avatar_btn.set_popover(menus.avatar_popover(self))
        url = user.get("avatar") or ""
        if url and not url.lower().endswith((".jpg", ".png", ".webp")):
            url += "_avm.jpg"
        if url:
            self.images.load(url, self._set_avatar_image)

    def _set_avatar_image(self, tex) -> None:
        self.avatar.set_custom_image(tex)
        if self.avatar_header is not None:
            self.avatar_header.set_custom_image(tex)

    def refresh_all(self) -> None:
        self._update_avatar()
        self.populate()

    # ------------------------------------------------------------------ Erweiterungspunkte Bibliotheks-Abgleich / Store
    def request_library_refresh(self, reason: str, force: bool = False) -> None:
        """Bibliothek mit GOG abgleichen. ``reason``: "startup", "login", "manual", "timer", "focus", "store",
        "purchase" (sofort, danach nach 5/15/60 s erneut, bis neue Spiele da sind) …; ``force`` = Vollabruf ohne
        Prüfsummen-Abkürzung. Läuft schon ein Abgleich, wird ein erzwungener Wunsch einmal nachgeholt."""
        log.info("Bibliotheksabgleich angefordert (%s%s)", reason, ", erzwungen" if force else "")
        self.library_sync.request(reason, force)

    def open_store(self, path: str) -> None:
        """Store-Seite öffnen (Pfad wie "/games", "/redeem" oder volle gog.com-Adresse) – vorerst im Browser,
        in der Sprache der Oberfläche. Wird vom eingebetteten Store ersetzt."""
        open_uri(store_url(path, store_lang(i18n.current())))

    def refresh_library_async(self, check_updates: bool | None = None) -> None:
        """Ältere Aufrufe: Bibliothek vollständig neu laden (wie Zahnrad → „Bibliothek aktualisieren“)."""
        self.request_library_refresh("manual", force=True)

    def check_updates_async(self, quiet: bool = False) -> None:
        def done(found: dict) -> None:
            for sec in self.sections:
                for c in sec.cards.values():
                    self.apply_card_state(c)
            if self.current_pid:
                self.page.refresh_state()
            self.update_nav_badges()
            if found:
                self.toast(tr('Updates verfügbar für {0} Spiel(e)').format(len(found)))
            elif not quiet:
                self.toast(tr('Alle installierten Spiele sind aktuell'))
        run_async(self.backend.check_updates, done, lambda e: None if quiet else self.toast(str(e)))

    # ------------------------------------------------------------------ Spiele-Ordner überwachen
    def setup_folder_watch(self) -> None:
        """Beobachtet die Spiele-Ordner: neue oder gelöschte Spielordner lösen eine Suche aus."""
        for m in self._monitors:
            try:
                m.cancel()
            except Exception:
                pass
        self._monitors = []
        if not self.backend.settings.watch_install_dirs:
            return
        roots = [*self.backend.install_roots(), *[lib.games_root for lib in self.backend.libraries() if lib.watch]]
        for root in dict.fromkeys(roots):
            if not root.is_dir():
                continue
            try:
                mon = Gio.File.new_for_path(str(root)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error as exc:
                log.warning("Spiele-Ordner %s kann nicht überwacht werden: %s", root, exc)
                continue
            mon.connect("changed", self._on_folder_changed)
            self._monitors.append(mon)
        log.info("Überwache %d Spiele-Ordner", len(self._monitors))

    def _on_folder_changed(self, _mon, _file, _other, event) -> None:
        relevant = (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_IN,
                    Gio.FileMonitorEvent.MOVED_OUT, Gio.FileMonitorEvent.RENAMED)
        if event in relevant:
            self.schedule_scan(6)

    def schedule_scan(self, seconds: int) -> None:
        if self._scan_timer:
            GLib.source_remove(self._scan_timer)

        def fire() -> bool:
            self._scan_timer = 0
            self.scan_folders_async(quiet=True)
            return False
        self._scan_timer = GLib.timeout_add_seconds(seconds, fire)

    def scan_folders_async(self, quiet: bool = True) -> None:
        if self._scanning:
            if not quiet:
                self.toast(tr('Spiele-Ordner werden bereits durchsucht …'))
            return
        if quiet and not self.backend.settings.watch_install_dirs:
            return
        self._scanning = True
        if not quiet:
            self.status_lbl.set_text(tr('Spiele-Ordner werden durchsucht …'))

        def done(report) -> None:
            self._scanning = False
            self.status_lbl.set_text("")
            if report is None:
                return
            if report.changed:
                self.populate()
                if self.current_pid:
                    self.page.refresh_state()
            self.update_nav_badges()
            msgs = []
            groups = {"": []}
            for g in report.imported:
                groups.setdefault(g.library if g.method == "external" else "", []).append(g)
            if any(k for k in groups if k):
                self.update_nav_badges()
            for key, lst in groups.items():
                if lst:
                    lib = self.backend.get_library(key) if key else None
                    what = tr('Spiel(e) in „{0}“').format(lib.name) if lib else (tr('externe(s) Spiel(e)') if key else tr('GOG-Spiel(e) im Spiele-Ordner'))
                    names = ", ".join(g.title for g in lst[:3]) + (" …" if len(lst) > 3 else "")
                    msgs.append(tr('{0} {1} erkannt: {2}').format(len(lst), what, names))
            if report.prefix_jobs:
                msgs.append(tr('{0} fehlende(r) Prefix(e) werden angelegt').format(report.prefix_jobs))
            if report.removed:
                msgs.append(tr('{0} Spiel(e) ausgetragen, Ordner fehlt: {1}').format(len(report.removed), ', '.join(report.removed[:3])))
            if report.copying:
                msgs.append(tr('Wird noch kopiert: {0} – wird danach übernommen').format(', '.join(report.copying[:3])))
                self.schedule_scan(20)
            new_unknown = [u for u in report.unknown if u not in self._seen_unknown]
            self._seen_unknown.update(report.unknown)
            if new_unknown and not quiet or (new_unknown and report.imported):
                msgs.append(tr('{0} Ordner ohne GOG-Spieldaten übersprungen').format(len(new_unknown)))
            if report.duplicates and not quiet:
                msgs.append(tr('{0} Ordner übersprungen: Spiel ist bereits an anderer Stelle installiert').format(len(report.duplicates)))
            if not msgs and not quiet:
                msgs.append(tr('Keine neuen Spiele gefunden'))
            for m in msgs:
                self.toast(m)

        def fail(exc: Exception) -> None:
            self._scanning = False
            self.status_lbl.set_text("")
            log.warning("Durchsuchen der Spiele-Ordner fehlgeschlagen: %s", exc)
            if not quiet:
                self.toast(tr('Durchsuchen fehlgeschlagen: {0}').format(exc))
        # Ordnerüberwachung (quiet) lässt Bibliotheken mit abgeschalteter Überwachung aus, „Durchsuchen“ nimmt alle
        run_async(lambda: self.backend.scan_install_dirs(auto=quiet), done, fail)

    # ------------------------------------------------------------------ Spiele und Prefixe von Hand verwalten
    def add_game(self, library: str | None = None) -> None:
        from .dialogs import AddGameDialog
        lib = self.backend.get_library(library) if library else None
        lib = lib or (self.backend.libraries()[0] if self.backend.libraries() else None)
        start = str(lib.games_root) if lib and lib.games_root.is_dir() else str(Path.home())

        def chosen(path: str) -> None:
            def ok(info) -> None:
                AddGameDialog(self.backend, info, self._after_add, library=library).present(self)
            run_async(lambda: self.backend.inspect_folder(path), ok, lambda e: self.toast(str(e)))
        choose_folder(self, tr('Spielordner wählen'), start, chosen)

    def _after_add(self, game) -> None:
        self.populate()
        self.toast(tr('„{0}“ hinzugefügt').format(game.title) + (tr(' – Prefix wird angelegt') if self.backend.jobs.active_for(game.product_id) else ""))
        self.show_game(game.product_id)

    def create_missing_prefixes(self, library: str | None = None) -> None:
        def done(n: int) -> None:
            self.toast(tr('{0} fehlende(r) Prefix(e) werden angelegt').format(n) if n else tr('Alle Spiele haben bereits einen Prefix'))
        run_async(lambda: self.backend.queue_missing_prefixes(library), done, lambda e: self.toast(str(e)))

    # ------------------------------------------------------------------ Eigene Bibliotheken
    def create_library(self) -> None:
        from .dialogs import LibraryDialog
        LibraryDialog(self.backend, self._after_library_created).present(self)

    def _after_library_created(self, lib) -> None:
        self.libraries_changed()
        self.navigate(f"lib:{lib.id}")
        self.toast(tr('Bibliothek „{0}“ angelegt – Ordner wird durchsucht …').format(lib.name))
        self.scan_folders_async(quiet=False)

    def delete_library(self, lib_id: str) -> None:
        from .dialogs import confirm_delete_library
        lib = self.backend.get_library(lib_id)
        if lib is None:
            return

        def do() -> None:
            try:
                games = self.backend.remove_library(lib_id)
            except Exception as exc:
                self.toast(str(exc))
                return
            self.libraries_changed()
            if self.view == f"lib:{lib_id}":
                self.navigate("all")
            self.toast(tr('Bibliothek „{0}“ gelöscht – {1} Spiel(e) ausgetragen, Dateien und Prefixe bleiben').format(lib.name, len(games)))
        confirm_delete_library(self, lib, len(self.backend.library_games(lib_id)), do)

    def libraries_changed(self) -> None:
        """Nach Anlegen/Umbenennen/Löschen/Ordnerwechsel: Seitenleiste, Überwachung und Ansicht neu aufbauen."""
        self._rebuild_library_nav()
        self.setup_folder_watch()
        if self.view.startswith("lib:") and self.backend.get_library(self.view[4:]) is None:
            self.navigate("all", push=False)
        else:
            for k, it in self.nav.items():
                it.set_selected(k == self.view)
            if self.view.startswith("lib:"):
                self._rebuild_library()
        self.update_nav_badges()

    def cleanup_prefixes(self, library: str | None = None) -> None:
        from .dialogs import confirm_orphan_prefixes

        def found(orphans) -> None:
            if not orphans:
                self.toast(tr('Keine verwaisten Prefixe gefunden'))
                return

            def delete(selected) -> None:
                run_async(lambda: self.backend.delete_orphan_prefixes(selected),
                          lambda done: self.toast(tr('{0} Prefix(e) gelöscht').format(len(done))), lambda e: self.toast(str(e)))
            confirm_orphan_prefixes(self, orphans, delete)
        run_async(lambda: self.backend.orphan_prefixes(library), found, lambda e: self.toast(str(e)))

    def toast(self, text: str) -> None:
        # Adw.Toast deutet den Text als Markup – Namen wie „Rock & Roll“ sonst leer
        self.toasts.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def do_login(self) -> None:
        LoginDialog(self.backend, self._after_login).present(self)

    def _after_login(self, user: dict) -> None:
        self.toast(tr('Angemeldet als {0}').format(user.get('username', '')))
        self._update_avatar()
        self.request_library_refresh("login", force=True)

    def do_logout(self) -> None:
        self.backend.api.logout()
        self.backend.library = []
        self.avatar.set_custom_image(None)
        self._update_avatar()
        self.navigate("all")
        self.toast(tr('Abgemeldet'))

    def open_preferences(self, page: str = "") -> None:
        def changed() -> None:
            self.libraries_changed()
            self.backend.launcher.tools(refresh=True)
            if self.current_pid:
                self.page.refresh_state()
        dlg = PreferencesDialog(self.backend, changed)
        if page:
            dlg.set_visible_page_name(page)
        dlg.present(self)

    # ------------------------------------------------------------------ Jobs / Downloads
    def _on_job(self, job) -> None:
        self.refresh_game_state(job.product_id)
        self.page.on_job(job)
        if self.backend.jobs.active():
            self.spinner.start()
        else:
            self.spinner.stop()
        if self.view == "downloads":
            self._update_download_rows(job)
        if job.kind == "prefix" and job.state == "done":
            self._prefix_done.append(job.title)
            if not any(j.kind == "prefix" for j in self.backend.jobs.active()):
                done, self._prefix_done = self._prefix_done, []
                self.toast(tr('Prefix angelegt: {0}').format(done[0]) if len(done) == 1 else tr('{0} Prefixe angelegt').format(len(done)))
        elif job.state == "done":
            self.toast(tr('{0} abgeschlossen: {1}').format(job.label, job.title))
            if is_lib_view(self.view):
                self._rebuild_library()
        elif job.state == "failed":
            self.toast(tr('{0} fehlgeschlagen: {1}').format(job.label, job.error))
        elif job.state == "cancelled":
            self.toast(tr('{0} abgebrochen: {1}').format(job.label, job.title))

    def _rebuild_downloads(self) -> None:
        child = self.dl_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.dl_box.remove(child)
            child = nxt
        self.dl_rows: dict[str, dict] = {}
        jobs = list(self.backend.jobs.jobs)
        active = [j for j in jobs if j.active]
        done = [j for j in jobs if not j.active]
        self.dl_box.append(label(tr('DOWNLOADS'), css=["sec-hdr"]))
        if not active:
            self.dl_box.append(label(tr('Keine aktiven Downloads.'), css=["dim"]))
        for j in active:
            self.dl_box.append(self._download_row(j))
        if done:
            hist = label(tr('VERLAUF'), css=["sec-hdr"])
            hist.set_margin_top(18)
            self.dl_box.append(hist)
            for j in reversed(done[-20:]):
                self.dl_box.append(self._download_row(j))

    def _download_row(self, job) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.add_css_class("dl-row")
        top = Gtk.Box(spacing=10)
        pic_frame, pic = clamped_picture(40, 56, ["cover"])
        e = self.backend.entry(job.product_id)
        if e:
            self.load_cover(e, pic)
        top.append(pic_frame)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        tb.append(label(job.title, css=["heading"], ellipsize=True))
        status = label("", css=["dim", "caption"], ellipsize=True)
        tb.append(status)
        top.append(tb)
        if job.active:
            c = icon_button(pick_icon("process-stop-symbolic", "window-close-symbolic"), tr('Abbrechen'), ["flat", "hdr-btn"])
            c.set_valign(Gtk.Align.CENTER)
            c.connect("clicked", lambda _b, j=job: self.backend.jobs.cancel(j))
            top.append(c)
        row.append(top)
        bar = Gtk.ProgressBar()
        bar.set_visible(job.active)
        row.append(bar)
        self.dl_rows[job.id] = {"status": status, "bar": bar}
        self._update_download_rows(job)
        return row

    def _update_download_rows(self, job) -> None:
        w = getattr(self, "dl_rows", {}).get(job.id)
        if not w:
            if job.active or self.view == "downloads":
                self._rebuild_downloads()
            return
        p = job.progress
        if job.active:
            if p.total_bytes:
                w["bar"].set_fraction(p.fraction)
                w["status"].set_text(tr('{0} · {1} · {2} / {3} · {4} · ETA {5} · {6}/{7} Dateien').format(job.label, job.status, format_bytes(p.done_bytes), format_bytes(p.total_bytes), fmt_speed(p.speed), fmt_eta(p.eta_seconds), p.files_done, p.files_total))
            else:
                w["bar"].pulse()
                w["status"].set_text(f"{job.label} · {job.status}")
        else:
            w["bar"].set_visible(False)
            w["status"].set_text(f"{job.label} · {job.status}" + (f" · {job.error}" if job.error else ""))
