"""Hauptfenster im Aufbau von GOG Galaxy 2.0.

Titelleiste: links Zahnrad-Menü (wie Galaxy 2.1, siehe ui/menus.py) · Avatar-Menü · Power Search (Strg+F, über der
Seitenleiste), rechts Zurück/Vor, Lesezeichen, Filter, „+“, Suchfeld, Ansicht (Sortieren nach, Gruppieren nach …) und
die Fensterknöpfe (Minimieren/Schließen je nach „Verhalten des Hauptfensters“, auch in den System-Tray).
Seitenleiste wie im Original: GOG GALAXY (Entdecken, Suche, Aktuelle Übersicht), GOG STORE (eingebettete
gog.com-Seiten, ui/store.py), SPIELE (Zuletzt gespielt, Alle Spiele, Installiert, Downloads), BIBLIOTHEKEN (eigene
Bibliotheken für Nicht-GOG-Spiele, je ein Eintrag mit Zähler), LESEZEICHEN, FREUNDE (Einladungen, Freunde online,
Nachrichten – ui/ext_friends.py). Inhalt: Entdecken (ui/discover_view.py), Aktuelle Übersicht (ui/recent_view.py),
Cover-Raster in Abschnitten oder Gruppen, Downloads-Seite, Spielansicht, Store, Freundesliste und Chat (Seiten der
Erweiterungen über ``add_page``); rechts ab 1501 px Fensterbreite die Freunde-Seitenleiste. Die Glocke neben der Suche
öffnet das Benachrichtigungszentrum (ui/notification_center.py).
Rechtsklick auf ein Spiel öffnet das Kontextmenü wie in Galaxy (ui/game_menu.py).
"""
from __future__ import annotations

from ..i18n import tr

import logging
import socket
import time
from pathlib import Path

from .. import APP_ID, APP_NAME, __version__, i18n, library_view as lv, paths, recent as rc
from ..api.client import NotLoggedIn
from ..api.discover import DiscoverService
from ..presence import Presence
from ..settings import format_bytes, format_minutes
from . import footer, menus, prefs_registry, store, ui_registry, util
from .bridge import GtkBridge
from .dialogs import LoginDialog, PreferencesDialog
from .discover_view import DiscoverPage
from .downloads_view import DownloadsView
from .game_menu import GameActions, attach_context_menu
from .game_page import GamePage
from .library_sync import LibrarySyncController
from .notification_center import NotificationCenter
from .recent_view import RecentPage
from .tray import Tray
from .util import (Adw, Gdk, Gio, GLib, Gtk, ImageLoader, Pango, choose_folder, clamped_picture, icon_button, idle, label,
                   pick_icon, relative_time, run_async)

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 232
COVER_SIZES = {"XS": (96, 135), "S": (116, 163), "M": (136, 191), "L": (164, 231), "XL": (200, 282)}   # Seitenverhältnis wie GOG-Boxart
SIZE_NAMES = {"XS": tr('Sehr klein'), "S": tr('Klein'), "M": tr('Mittel'), "L": tr('Groß'), "XL": tr('Sehr groß')}
SPACING = {"0": (4, 6), "S": (12, 16), "M": (24, 30), "L": (40, 46)}   # (Spalten, Zeilen) in px
SPACING_NAMES = {"0": tr('Keiner'), "S": tr('Klein'), "M": tr('Mittel'), "L": tr('Groß')}
LIB_VIEWS = ("all", "installed", "recent")        # dazu "lib:<ID>" je eigener Bibliothek, "bm:<ID>" je Lesezeichen
START_VIEWS = ("discover", "overview", "all", "installed")
PAGE_VIEWS = ("discover", "overview")              # eigene Seiten der Gruppe GOG GALAXY


def is_lib_view(view: str) -> bool:
    return view in LIB_VIEWS or view.startswith("lib:") or view.startswith("bm:")


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


LIST_COLUMNS = ((tr('Status'), 18), (tr('Bewertung'), 8), (tr('Spielzeit'), 11), (tr('Zuletzt gespielt'), 14), (tr('Größe'), 9))


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
        rating = b.game_settings.get(entry.id).rating
        values = [self.status, label("★" * rating if rating else "–", css=["row-cell"]),
                  label(format_minutes(minutes) if minutes else "–", css=["row-cell"]),
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
            attach_context_menu(self.win, card, lambda c=card: c.entry.id)
            self.win.load_cover(e, card.pic)
            self.win.apply_card_state(card)
            card.set_new(self.win.library_sync.is_new(e.id))
        self.count_lbl.set_text(str(len(entries)))
        self.set_visible(bool(entries))


class WidthProbe(Gtk.DrawingArea):
    """Unsichtbarer Streifen über die volle Fensterbreite (0 px hoch). Meldet Breitenänderungen an ``listeners`` –
    Galaxy zeigt die Freunde-Seitenleiste erst bei Fenstern ab 1501 px. (Gtk.Box nutzt einen Layout-Manager, dort
    käme ``size_allocate`` nicht an; die Zeichenfläche meldet ``resize``.)"""

    def __init__(self) -> None:
        super().__init__(hexpand=True, content_height=0, can_target=False, focusable=False)
        self.listeners: list = []
        self.width = 0
        self.connect("resize", self._on_resize)

    def _on_resize(self, _area, width: int, _height: int) -> None:
        if width == self.width:
            return
        self.width = width
        # nicht während der Größenberechnung umbauen – erst danach in der Hauptschleife
        for fn in list(self.listeners):
            GLib.idle_add(lambda fn=fn, w=width: (fn(w), False)[1])


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
        self.filter = lv.LibraryFilter()          # Filter-Knopf wie Galaxy (Status, OS, Plattform, Genre, Tags …)
        self._search = ""
        self._facts: dict[str, lv.GameFacts] = {}
        self._view_override: dict = {}            # Sortierung/Gruppierung eines geöffneten Lesezeichens
        self.sections: list[LibrarySection] = []
        self.library_sync = LibrarySyncController(self)   # Abgleich mit GOG: Start, Intervall, Fokus, Login, Kauf, F5
        self.game_actions = GameActions(self)     # Anpassen-Menü und Kontextmenü der Spiele (Aktionsgruppe „game“)
        self.discover_service = DiscoverService(backend.api)   # Katalog-Regale, Region (auch für die Wunschliste)
        self.notifications = NotificationCenter(self)          # Glocke, Liste, Desktop-Meldungen je Einstellung
        self.extra_pages: dict[str, Gtk.Widget] = {}           # Seiten der Erweiterungen (Freundesliste, Chat …)
        util.SPEED_IN_BITS[0] = backend.settings.speed_in_bits

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._build_header())
        self.width_probe = WidthProbe()         # Fensterbreite für die Freunde-Seitenleiste (ab 1501 px)
        root.append(self.width_probe)
        body = Gtk.Box(vexpand=True)
        self.body = body
        body.append(self._build_sidebar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        body.append(self.stack)
        root.append(body)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(root)
        self.set_content(self.toasts)

        # GOG GALAXY: Entdecken und Aktuelle Übersicht
        self.discover = DiscoverPage(self)
        self.stack.add_named(self.discover, "discover")
        self.overview = RecentPage(self)
        self.stack.add_named(self.overview, "overview")
        # Bibliothek (Abschnitte)
        self.lib_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.lib_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28, margin_top=18, margin_bottom=28,
                               margin_start=28, margin_end=28, valign=Gtk.Align.START)
        self.lib_scroll.set_child(self.lib_box)
        self.stack.add_named(self.lib_scroll, "library")
        # Downloads (Warteschlange, Updates, Abgeschlossen, Fehlgeschlagen)
        self.downloads = DownloadsView(self)
        self.stack.add_named(self.downloads, "downloads")
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
        self.connect("close-request", self._on_close_request)
        self._quit_timer = 0
        self._monitors: list = []
        self._scan_timer = 0
        self._scanning = False
        self._seen_unknown: set[str] = set()
        self._prefix_done: list[str] = []
        self._update_avatar()
        self.navigate(self._start_view())
        self.setup_folder_watch()
        GLib.timeout_add_seconds(2, lambda: (self.scan_folders_async(quiet=True), False)[1])
        # Namen, Kopien und Cover externer Spiele (content/identity.py) – nach der ersten Ordnersuche erneut
        GLib.timeout_add_seconds(6, lambda: (self.resolve_externals_async(), False)[1])
        # Bibliothek aus dem Cache steht schon – Abgleich kurz danach (leerer Cache: sofort), danach Update-Suche
        self.library_sync.start()
        # offene Downloads vom letzten Mal wieder einreihen (pausierte bleiben pausiert)
        self.backend.jobs.enable_persistence(paths.JOBS_FILE)
        # Online-Status bei GOG (Avatar-Menü „Online“/„Unsichtbar“) und System-Tray
        self.presence = Presence(self.backend.api, self.backend.settings)
        if self.backend.api.logged_in:
            self.presence.start()
        self.tray = Tray(APP_ID, APP_NAME, APP_ID, self.show_from_tray, self.request_quit, (tr('Öffnen'), tr('Beenden')))
        self.apply_window_settings()
        # Spielzeit aller Geräte von GOG: kurz nach dem Start und dann halbstündlich (je Spiel höchstens alle 6 h)
        GLib.timeout_add_seconds(25, lambda: (self.pull_playtime_async(), False)[1])
        GLib.timeout_add_seconds(1800, lambda: (self.pull_playtime_async(), True)[1])
        # „Neu“ bei der Aktuellen Übersicht: neuer Vorschlag am nächsten Tag
        GLib.timeout_add_seconds(1800, lambda: (self.update_overview_badge(), True)[1])
        self.update_overview_badge()
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
        # Glocke = Benachrichtigungszentrum (mit Zahl der ungelesenen Meldungen)
        left.append(self.notifications.button)
        sb = icon_button(pick_icon("system-search-symbolic", "edit-find-symbolic"), tr('Suche (Strg+F)'), ["flat", "hdr-btn"])
        sb.set_margin_end(10)
        sb.set_valign(Gtk.Align.CENTER)
        sb.connect("clicked", lambda *_: self.open_power_search())
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
                                             tooltip_text=tr('Lesezeichen'))
        self.bookmark_btn.add_css_class("flat")
        self.bookmark_btn.add_css_class("hdr-btn")
        self.bookmark_btn.connect("toggled", self._on_bookmark)
        for w in (self.back_btn, self.fwd_btn, self.bookmark_btn):
            w.set_valign(Gtk.Align.CENTER)
            right.append(w)

        center = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, hexpand=True, valign=Gtk.Align.CENTER)
        self.filter_btn = Gtk.MenuButton()
        # bei jedem Öffnen neu: Genres, Tags und Bibliotheken können sich geändert haben
        self.filter_btn.set_create_popup_func(lambda b: b.set_popover(self._filter_popover()))
        fb = Gtk.Box(spacing=6)
        fb.append(Gtk.Image(icon_name=pick_icon("funnel-symbolic", "view-filter-symbolic", "edit-find-replace-symbolic"), pixel_size=14))
        self.filter_lbl = label(tr('Filter'))
        fb.append(self.filter_lbl)
        self.filter_btn.set_child(fb)
        self.filter_btn.add_css_class("hdr-pill")
        center.append(self.filter_btn)
        plus = Gtk.MenuButton(icon_name=pick_icon("list-add-symbolic"), popover=self._plus_popover(), tooltip_text=tr('Bibliothek'))
        plus.add_css_class("hdr-pill")
        center.append(plus)
        self.search = Gtk.SearchEntry(placeholder_text=tr('Suche Spiele'))
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
        # Minimieren selbst übernehmen („Bei Minimierung des Fensters“: Taskleiste oder System-Tray)
        layout = Gtk.Settings.get_default().get_property("gtk-decoration-layout") or ":minimize,maximize,close"
        end = [b for b in layout.split(":", 1)[-1].split(",") if b]
        if "minimize" in end:
            mini = icon_button(pick_icon("window-minimize-symbolic"), tr('Minimieren'), ["flat", "hdr-btn", "minimize-btn"])
            mini.set_valign(Gtk.Align.CENTER)
            mini.connect("clicked", lambda *_: self.minimize_window())
            right.append(mini)
        controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.CENTER,
                                      decoration_layout=":" + ",".join(b for b in end if b != "minimize"))
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
        """Filter wie Galaxy: Status, Betriebssystem, Plattform, Genre, Bewertung, Tags, Favoriten, Ausgeblendet –
        innerhalb einer Kategorie ODER, zwischen den Kategorien UND; jede Änderung wirkt sofort."""
        pop = Gtk.Popover()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
        head = Gtk.Box(spacing=6)
        h = label(tr('FILTER'), css=["sec-hdr"])
        h.set_hexpand(True)
        head.append(h)
        reset = Gtk.Button(label=tr('Zurücksetzen'))
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: (pop.popdown(), self.reset_filter()))
        head.append(reset)
        outer.append(head)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    max_content_height=560, min_content_width=280)
        scroll.set_child(box)
        outer.append(scroll)
        flt = self.filter

        def category(title: str, cat: str, options: list) -> None:
            if not options:
                return
            chosen = getattr(flt, cat)
            exp = Gtk.Expander(label=title + (f"  ({len(chosen)})" if chosen else ""), expanded=bool(chosen))
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=10)
            for value, text in options:
                c = Gtk.CheckButton(label=text, active=value in chosen)
                c.connect("toggled", lambda b, v=value, k=cat: self._set_filter(k, v, b.get_active()))
                inner.append(c)
            exp.set_child(inner)
            box.append(exp)

        facts = list(self._facts.values())
        category(tr('Status'), "status", [(k, tr(lv.STATUS_NAMES[k])) for k in lv.STATUS_KEYS])
        category(tr('Betriebssystem & Konsolen'), "os", list(lv.OS_NAMES.items()))
        category(tr('Plattform'), "sources", [("gog", "GOG.com")] + [(f"lib:{l.id}", l.name) for l in self.backend.libraries()])
        category(tr('Genre'), "genres", [(g, g) for g in sorted({g for f in facts for g in f.genres}, key=str.lower)])
        category(tr('Bewertung'), "ratings", [(n, lv.stars(n)) for n in range(lv.MAX_RATING, 0, -1)] + [(0, tr('Keine Bewertung'))])
        tags = lv.all_tags(self.backend.game_settings)
        category(tr('Tags'), "tags", [(t, t) for t in tags] + [("", tr('Keine Tags'))])
        fav = Gtk.CheckButton(label=tr('Favoriten'), active=flt.favorites)
        fav.connect("toggled", lambda b: self._set_filter("favorites", True, b.get_active()))
        box.append(fav)
        if flt.copies or any(g.duplicate_of for g in self.backend.registry.all()):
            cop = Gtk.CheckButton(label=tr('Kopien anzeigen'), active=flt.copies)
            cop.set_tooltip_text(tr('Ordner mit gleichem Inhalt wie ein anderes Spiel (z. B. „… - Kopie“) – sonst nur einmal gezeigt'))
            cop.connect("toggled", lambda b: self._set_filter("copies", True, b.get_active()))
            box.append(cop)
        hid_keys = ["", "hidden", "visible"]
        hid = Gtk.DropDown.new_from_strings([tr('Ausgeblendete wie eingestellt'), tr('Nur ausgeblendete Spiele anzeigen.'),
                                             tr('Nur sichtbare Spiele anzeigen.')])
        hid.set_selected(hid_keys.index(flt.hidden) if flt.hidden in hid_keys else 0)
        hid.connect("notify::selected", lambda d, _p: self._set_filter("hidden", hid_keys[d.get_selected()], True))
        box.append(label(tr('Ausgeblendet'), css=["dim", "caption"]))
        box.append(hid)
        hidden = Gtk.CheckButton(label=tr('Versteckte Spiele anzeigen'), active=self.backend.settings.show_hidden)
        hidden.connect("toggled", self._toggle_hidden)
        box.append(hidden)
        pop.set_child(outer)
        return pop

    def _view_popover(self) -> Gtk.Popover:
        """Ansicht der Bibliothek: Raster/Liste, Covergröße, Abstand, Titel, Abschnitte, Sortieren nach (auf-/absteigend)
        und Gruppieren nach (wird gespeichert – für die GOG-Ansichten gemeinsam, für jede eigene Bibliothek getrennt)."""
        lib = self._view_lib()
        v = self._current_view(lib)
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
        box.append(label(tr('SORTIEREN NACH'), css=["sec-hdr"]))
        keys = list(lv.SORT_KEYS)
        sort_key, desc = lv.resolve_sort(v)
        sort_dd = Gtk.DropDown.new_from_strings([tr(lv.SORT_NAMES[k]) for k in keys])
        sort_dd.set_selected(keys.index(sort_key))
        box.append(sort_dd)
        order = Adw.ToggleGroup()
        order.add(Adw.Toggle(name="asc", label=tr('Aufsteigend')))
        order.add(Adw.Toggle(name="desc", label=tr('Absteigend')))
        order.set_active_name("desc" if desc else "asc")
        box.append(order)

        def sort_changed(d, _p) -> None:
            k = keys[d.get_selected()]
            default_desc = k in lv.DESC_BY_DEFAULT
            order.set_active_name("desc" if default_desc else "asc")
            self._set_view(sort_by=k, sort_desc=default_desc)
        sort_dd.connect("notify::selected", sort_changed)
        order.connect("notify::active-name", lambda g, _p: self._set_view(sort_by=keys[sort_dd.get_selected()],
                                                                           sort_desc=g.get_active_name() == "desc"))
        box.append(label(tr('GRUPPIEREN NACH'), css=["sec-hdr"]))
        gkeys = list(lv.GROUP_KEYS)
        gcur = v.get("group") if v.get("group") in gkeys else "none"
        group_dd = Gtk.DropDown.new_from_strings([tr(lv.GROUP_NAMES[k]) for k in gkeys])
        group_dd.set_selected(gkeys.index(gcur))
        group_dd.connect("notify::selected", lambda d, _p: self._set_view(group=gkeys[d.get_selected()]))
        box.append(group_dd)
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

        # GOG GALAXY wie im Original ganz oben: Entdecken, Suche (Power Search), Aktuelle Übersicht („Neu“ bei einem
        # neuen Vorschlag „Was als Nächstes spielen“)
        section(tr('GOG GALAXY'))
        item("discover", tr('Entdecken'), pick_icon("compass-symbolic", "find-location-symbolic", "starred-symbolic"))
        item("search", tr('Suche'), pick_icon("system-search-symbolic", "edit-find-symbolic"), cb=lambda: self.open_power_search())
        item("overview", tr('Aktuelle Übersicht'), pick_icon("view-dashboard-symbolic", "view-grid-symbolic",
                                                              "document-open-recent-symbolic"))
        # GOG STORE wie im Original vor den Spielen (Startseite … Bisherige Bestellungen), ausblendbar über
        # Einstellungen → Oberfläche → „Bevorzugter Store“
        box.append(store.build_sidebar_group(self, NavItem))
        section(tr('SPIELE'))
        item("recent", tr('Zuletzt gespielt'), pick_icon("document-open-recent-symbolic", "alarm-symbolic"))
        item("all", tr('Spiele im Besitz'), pick_icon("view-grid-symbolic", "view-app-grid-symbolic"))
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
        # LESEZEICHEN: gespeicherte Ansichten (mit Filter, Suche, Sortierung) und Spiele – Rechtsklick öffnet ihr Menü
        self.bm_header = label(tr('LESEZEICHEN'), css=["nav-section"])
        box.append(self.bm_header)
        self.bmnav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.bmnav_box)
        self._rebuild_bookmark_nav()
        # FREUNDE wie Galaxy: Einladungen (nur wenn welche da sind), Freunde online (Zähler, Freundesliste),
        # Nachrichten (Chat) – Inhalt und Zähler kommen aus ui/ext_friends.py
        section(tr('FREUNDE'))
        item("requests", tr('Einladungen'), pick_icon("contact-new-symbolic", "list-add-symbolic"),
             cb=lambda: self.open_friends("invitations")).set_visible(False)
        item("friends", tr('Freunde online'), pick_icon("system-users-symbolic", "avatar-default-symbolic"))
        item("messages", tr('Nachrichten'), pick_icon("mail-unread-symbolic", "chat-symbolic"))
        box = outer
        self.status_lbl = label("", css=["dim", "caption"])
        self.status_lbl.set_margin_start(22)
        self.status_lbl.set_margin_bottom(4)
        self.status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.status_lbl)
        # „N Update(s) verfügbar“: ein Klick öffnet die Downloads bei „Erhältliche Updates“
        self.updates_lbl = label("", css=["caption"], ellipsize=True)
        self.updates_btn = Gtk.Button(child=self.updates_lbl, tooltip_text=tr('Erhältliche Updates anzeigen'))
        self.updates_btn.add_css_class("flat")
        self.updates_btn.add_css_class("footer-link")
        self.updates_btn.set_halign(Gtk.Align.START)
        self.updates_btn.set_margin_start(14)
        self.updates_btn.set_margin_bottom(2)
        self.updates_btn.set_visible(False)
        self.updates_btn.connect("clicked", lambda *_: footer.open_available_updates(self))
        box.append(self.updates_btn)
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

    # ------------------------------------------------------------------ Lesezeichen
    def _bookmark(self, bid: str) -> lv.Bookmark | None:
        return next((b for b in lv.bookmarks(self.backend.settings) if b.id == bid), None)

    def _rebuild_bookmark_nav(self) -> None:
        for key in [k for k in self.nav if k.startswith("bm:")]:
            self.nav.pop(key)
        child = self.bmnav_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.bmnav_box.remove(child)
            child = nxt
        items = lv.bookmarks(self.backend.settings)
        self.bm_header.set_visible(bool(items))
        for b in items:
            icon = pick_icon("applications-games-symbolic") if b.view == "game" else \
                pick_icon("user-bookmarks-symbolic", "bookmark-new-symbolic", "starred-symbolic")
            it = NavItem(b.name, icon)
            it.set_tooltip_text(b.name)
            it.connect("clicked", lambda _b, bid=b.id: self.open_bookmark(bid))
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda _g, _n, x, y, w=it, bid=b.id: self._bookmark_menu(bid, w).popup())
            it.add_controller(click)
            self.nav[f"bm:{b.id}"] = it
            self.bmnav_box.append(it)
        if getattr(self, "view", "") in self.nav:
            self.nav[self.view].set_selected(True)

    def _bookmark_menu(self, bid: str, parent: Gtk.Widget) -> Gtk.Popover:
        """Wie Galaxys Lesezeichen-Menü: umbenennen, hoch/runter verschieben, löschen."""
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for text, cb in ((tr('Lesezeichen umbenennen'), lambda: self._rename_bookmark(bid)),
                         (tr('Hoch verschieben'), lambda: self._move_bookmark(bid, -1)),
                         (tr('Runter verschieben'), lambda: self._move_bookmark(bid, 1)),
                         (tr('Lesezeichen löschen'), lambda: self._delete_bookmark(bid))):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            if text == tr('Lesezeichen löschen'):
                b.add_css_class("destructive-text")
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            box.append(b)
        pop.set_child(box)
        pop.set_parent(parent)
        pop.set_has_arrow(True)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        return pop

    def _rename_bookmark(self, bid: str) -> None:
        b = self._bookmark(bid)
        if b is None:
            return
        dlg = Adw.AlertDialog.new(tr('Lesezeichen umbenennen'), None)
        entry = Gtk.Entry(text=b.name, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", tr('Abbrechen'))
        dlg.add_response("ok", tr('Umbenennen'))
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(_d, r: str) -> None:
            if r == "ok" and lv.rename_bookmark(self.backend.settings, bid, entry.get_text()):
                self._rebuild_bookmark_nav()
        dlg.connect("response", resp)
        dlg.present(self)

    def _move_bookmark(self, bid: str, delta: int) -> None:
        if lv.move_bookmark(self.backend.settings, bid, delta):
            self._rebuild_bookmark_nav()

    def _delete_bookmark(self, bid: str) -> None:
        if lv.remove_bookmark(self.backend.settings, bid):
            self._rebuild_bookmark_nav()
            if self.view == f"bm:{bid}":
                self.navigate("all")
            else:
                self._sync_bookmark()
            self.toast(tr('Lesezeichen gelöscht'))

    def open_bookmark(self, bid: str) -> None:
        b = self._bookmark(bid)
        if b is None:
            return
        if b.view == "game":
            self.show_game(b.pid)
        else:
            self.navigate(f"bm:{b.id}")

    def _bookmark_candidate(self) -> lv.Bookmark | None:
        """Was der Lesezeichen-Knopf jetzt anlegen würde: die Spielansicht oder die Bibliotheksansicht mit Filter, Suche,
        Sortierung und Gruppierung (Store und Downloads lassen sich nicht anlegen)."""
        if self.view == "game" and self.current_pid:
            e = self.backend.entry(self.current_pid)
            return lv.Bookmark(id="", name=e.title if e else self.current_pid, view="game", pid=self.current_pid)
        if not is_lib_view(self.view):
            return None
        base = self._base_view()
        lib = self._view_lib()
        name = {"all": tr('Spiele im Besitz'), "installed": tr('Installiert'), "recent": tr('Zuletzt gespielt')}.get(base) \
            or (lib.name if lib else base)
        text = self.search.get_text().strip()
        if text:
            name += f" · „{text}“"
        if not self.filter.is_empty():
            name += " · " + tr('{0} Filter').format(self.filter.count())
        ov = self._view_override
        return lv.Bookmark(id="", name=name, view=base, filter=self.filter.to_dict(), search=text,
                           sort_by=ov.get("sort_by", ""), sort_desc=bool(ov.get("sort_desc", False)), group=ov.get("group", ""))

    def _base_view(self) -> str:
        """Ansicht hinter einem Lesezeichen („bm:<ID>“), sonst die aktuelle."""
        if self.view.startswith("bm:"):
            b = self._bookmark(self.view[3:])
            return b.view if b is not None and b.view != "game" else "all"
        return self.view

    def _set_search_text(self, text: str) -> None:
        self._search_guard = True
        self.search.set_text(text)
        self._search_guard = False
        self._search = text.strip().lower()

    # ------------------------------------------------------------------ Navigation
    def navigate(self, view: str, pid: str | None = None, push: bool = True) -> None:
        if view.startswith("lib:") and self.backend.get_library(view[4:]) is None:
            view = "all"                     # Bibliothek wurde inzwischen gelöscht
        prev = getattr(self, "view", "")
        if view.startswith("bm:"):
            bm = self._bookmark(view[3:])
            if bm is None:
                view = "all"
            elif bm.view == "game":
                view, pid = "game", bm.pid
            else:
                # Lesezeichen öffnen: seine Filter, Suche, Sortierung und Gruppierung gelten, bis eine andere Ansicht kommt
                self.filter = lv.LibraryFilter.from_dict(bm.filter)
                ov: dict = {}
                if bm.sort_by:
                    ov.update(sort_by=bm.sort_by, sort_desc=bm.sort_desc)
                if bm.group:
                    ov["group"] = bm.group
                self._view_override = ov
                self._set_search_text(bm.search)
        elif prev.startswith("bm:") and is_lib_view(view):
            self.filter = lv.LibraryFilter()
            self._view_override = {}
            self._set_search_text("")
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
            self.downloads.rebuild()
            self.stack.set_visible_child_name("downloads")
        elif view == "discover":
            self.stack.set_visible_child_name("discover")
            self.discover.on_show()
        elif view == "overview":
            self.overview.on_show()
            self.stack.set_visible_child_name("overview")
        elif view.startswith("store:") and getattr(self, "store_page", None) is not None:
            self.stack.set_visible_child_name("store")
            self.store_page.sync_nav()
        elif view in self.extra_pages:
            self.stack.set_visible_child_name(view)
            show = getattr(self.extra_pages[view], "on_show", None)
            if callable(show):
                show()
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
        self._update_filter_label()
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

    def add_page(self, view: str, widget: Gtk.Widget) -> None:
        """Eigene Seite im Inhaltsbereich (Erweiterungen); ``navigate(view)`` zeigt sie und ruft ``on_show()``."""
        self.extra_pages[view] = widget
        self.stack.add_named(widget, view)

    def open_friends(self, section: str = "") -> None:
        """Freundesliste öffnen, ``section="invitations"`` springt zu den erhaltenen Einladungen."""
        self.navigate("friends")
        page = self.extra_pages.get("friends")
        if section and page is not None and hasattr(page, "focus_section"):
            page.focus_section(section)

    def show_grid(self) -> None:
        self.navigate("all")

    def _sync_bookmark(self) -> None:
        cand = self._bookmark_candidate()
        self.bookmark_btn.set_sensitive(cand is not None)
        self._bookmark_guard = True
        self.bookmark_btn.set_active(bool(cand is not None and lv.find_bookmark(self.backend.settings, cand)))
        self._bookmark_guard = False

    def _on_bookmark(self, btn: Gtk.ToggleButton) -> None:
        """Lesezeichen-Knopf wie Galaxy: aktuelle Ansicht bzw. das Spiel in die Seitenleiste legen oder wieder lösen."""
        if getattr(self, "_bookmark_guard", False):
            return
        cand = self._bookmark_candidate()
        if cand is None:
            return
        found = lv.find_bookmark(self.backend.settings, cand)
        if btn.get_active() and found is None:
            b = lv.add_bookmark(self.backend.settings, cand)
            self.toast(tr('Lesezeichen angelegt: {0}').format(b.name))
        elif not btn.get_active() and found is not None:
            lv.remove_bookmark(self.backend.settings, found.id)
            self.toast(tr('Lesezeichen gelöscht'))
            if self.view == f"bm:{found.id}":
                self.view = found.view if found.view != "game" else self.view
        self._rebuild_bookmark_nav()

    # ------------------------------------------------------------------ Bibliothek
    def _view_lib_id(self) -> str | None:
        base = self._base_view()
        return base[4:] if base.startswith("lib:") else None

    def _view_lib(self):
        lid = self._view_lib_id()
        return self.backend.get_library(lid) if lid else None

    def _current_view(self, lib=None) -> dict:
        """Gespeicherte Ansicht plus Sortierung/Gruppierung eines geöffneten Lesezeichens."""
        v = self.backend.settings.view_settings(lib)
        v.update(self._view_override)
        return v

    def _custom_entries(self, lib_id: str | None = None, include_hidden: bool = True) -> list:
        """Spiele der eigenen Bibliotheken (oder nur einer) als Bibliothekseinträge; ausgeblendete nur mit
        ``include_hidden`` (die Bibliotheksansicht entscheidet dann per Filter)."""
        out = []
        games = self.backend.library_games(lib_id) if lib_id else [g for g in self.backend.registry.all() if g.method == "external"]
        show = include_hidden or self.backend.settings.show_hidden
        for g in games:
            if not show and self.backend.game_settings.get(g.product_id).hidden:
                continue
            if not include_hidden and g.duplicate_of and not self.filter.copies:
                continue                      # Kopie: wie Galaxys gestapelte Duplikate nur einmal zählen
            e = self.backend.entry(g.product_id)
            if e:
                out.append(e)
        return out

    def _entries(self, include_hidden: bool = False) -> list:
        """GOG-Bibliothek plus installierte GOG-Spiele (ohne externe Spiele); ausgeblendete nur mit ``include_hidden``
        bzw. „Versteckte Spiele anzeigen“."""
        entries = {e.id: e for e in self.backend.visible_library(include_hidden=True if include_hidden else None)}
        for g in self.backend.registry.all():
            if g.method == "external":
                continue
            if g.product_id not in entries:
                e = self.backend.entry(g.product_id)
                if e:
                    entries[e.id] = e
        return list(entries.values())

    def _fact(self, entry) -> lv.GameFacts:
        f = self._facts.get(entry.id)
        if f is None:
            f = lv.facts(self.backend, entry)
            self._facts[entry.id] = f
        return f

    def _sorted(self, entries: list, view: dict | None = None) -> list:
        key, desc = lv.resolve_sort(view if isinstance(view, dict) else {"sort": view or 0})
        by_id = {e.id: e for e in entries}
        return [by_id[f.id] for f in lv.sort_facts([self._fact(e) for e in entries], key, desc)]

    def _rebuild_library(self) -> None:
        child = self.lib_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lib_box.remove(child)
            child = nxt
        self.sections.clear()
        self._facts.clear()
        lib = self._view_lib()
        base = self._base_view()
        v = self._current_view(lib)
        size = COVER_SIZES.get(v["cover_size"], COVER_SIZES["S"])
        reg, pt = self.backend.registry, self.backend.playtime
        ents = [] if lib else self._sorted(self._entries(include_hidden=True), v)
        group_key = v.get("group") if v.get("group") in lv.GROUP_KEYS else "none"

        def add(title: str, items: list) -> None:
            if not items:
                return
            sec = LibrarySection(self, title, v)
            sec.set_entries(items, size, v["titles"])
            self.sections.append(sec)
            self.lib_box.append(sec)

        def add_grouped(title: str, items: list) -> None:
            """„Gruppieren nach“: eine Gruppe je Tag, Genre, Plattform … statt einer Liste."""
            if group_key == "none":
                add(title, items)
                return
            by_id = {e.id: e for e in items}
            for name, facts in lv.group_facts([self._fact(e) for e in items], group_key):
                add(name, [by_id[f.id] for f in facts])

        if lib is not None:
            self.lib_box.append(self._library_header(lib))
            games = self._sorted(self._custom_entries(lib.id), v)
            if v["sections"]:
                add(tr('Als Nächstes spielen'), [e for e in games if pt.get(e.id)[0] == 0][:12])
            add_grouped(lib.name, games)
            if not games:
                hint = label(tr('Noch keine Spiele in „{0}“. Spielordner nach {1} kopieren – sie werden nach dem Kopieren automatisch übernommen – oder einen beliebigen Ordner mit „Spiel hinzufügen …“ aufnehmen.').format(lib.name, lib.games_dir), css=["dim"], wrap=True)
                self.lib_box.append(hint)
        elif base == "installed":
            add_grouped(tr('Installiert'), [e for e in ents if reg.is_installed(e.id)])
        elif base == "recent":
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
                self.lib_box.append(label(tr('Noch nichts gespielt – installiere ein Spiel aus „Spiele im Besitz“.'), css=["dim"]))
        else:
            if v["sections"] and group_key == "none":
                add(tr('Neu in der Bibliothek'), self.library_sync.new_entries(ents))
                next_to_play = [e for e in ents if reg.is_installed(e.id) and pt.get(e.id)[0] == 0]
                add(tr('Als Nächstes spielen'), next_to_play[:12])
            add_grouped(tr('Alle Spiele'), ents)
            if not ents:
                self.lib_box.append(label(tr('Keine Spiele in der Bibliothek. Über das Zahnrad → „Bibliothek aktualisieren“ (F5) die Bibliothek von GOG laden.'), css=["dim"]))
        self._refilter()

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
        """Suche (Titel, Tags, Genres, Entwickler) und Filter; der Status kommt aus der Karte (Downloads, Updates)."""
        f = self._fact(child.entry)
        f.state = child.state
        if not lv.matches_search(f, self._search):
            return False
        return self.filter.matches(f, self.backend.settings.show_hidden)

    def _refilter(self) -> None:
        """Filter neu anwenden; Abschnitte zeigen die Zahl der sichtbaren Spiele, leere verschwinden."""
        for sec in self.sections:
            sec.flow.invalidate_filter()
            n = sum(1 for c in sec.cards.values() if self.card_filter(c))
            sec.count_lbl.set_text(str(n))
            sec.set_visible(n > 0)

    def reset_filter(self) -> None:
        self.filter = lv.LibraryFilter()
        self._update_filter_label()
        self._refilter()
        self._sync_bookmark()

    def _update_filter_label(self) -> None:
        n = self.filter.count()
        self.filter_lbl.set_text(tr('Filter') + (f" ({n})" if n else ""))
        if n:
            self.filter_btn.add_css_class("accent")
        else:
            self.filter_btn.remove_css_class("accent")

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
        if self.view == "downloads":
            self.downloads.refresh()

    update_row_status = refresh_game_state  # Kompatibilität

    def update_nav_badges(self) -> None:
        ents = self._entries()
        inst = len([e for e in ents if self.backend.registry.is_installed(e.id)])
        self.nav["all"].set_badge(f"{len(ents):,}".replace(",", "."))
        self.nav["installed"].set_badge(str(inst))
        for lib in self.backend.libraries():
            it = self.nav.get(f"lib:{lib.id}")
            if it is not None:
                it.set_badge(str(len(self._custom_entries(lib.id, include_hidden=False))))
        active = len(self.backend.jobs.active())
        self.nav["downloads"].set_badge(str(active) if active else "", accent=bool(active))
        text = footer.updates_text(len(self.backend.updates))
        self.updates_lbl.set_text(text)
        self.updates_btn.set_visible(bool(text))

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        if getattr(self, "_search_guard", False):
            return
        self._search = entry.get_text().strip().lower()
        if (self.view in ("game", "downloads", "welcome", *PAGE_VIEWS, *self.extra_pages)
                or self.view.startswith("store:")) and self._search:
            self.navigate("all")
        self._refilter()
        self._sync_bookmark()

    def _set_filter(self, key: str, val, on: bool) -> None:
        if key == "favorites":
            self.filter.favorites = on
        elif key == "copies":
            self.filter.copies = on
            self.update_nav_badges()
        elif key == "hidden":
            self.filter.hidden = val
        else:
            self.filter.toggle(key, val, on)
        self._update_filter_label()
        self._refilter()
        self._sync_bookmark()

    def _toggle_hidden(self, btn: Gtk.CheckButton) -> None:
        self.backend.settings.show_hidden = btn.get_active()
        self.backend.save_settings()
        if is_lib_view(self.view):
            self._rebuild_library()

    # ------------------------------------------------------------------ Tags, Bewertung, Ausblenden, Konfigurieren
    def game_meta_changed(self, pid: str = "") -> None:
        """Tags oder Bewertung geändert: Ansicht neu sortieren/gruppieren bzw. Filter neu anwenden."""
        v = self._current_view(self._view_lib())
        key, _desc = lv.resolve_sort(v)
        if is_lib_view(self.view) and (v.get("group") in ("tags", "rating") or key in ("tags", "rating")):
            y = self.lib_scroll.get_vadjustment().get_value()
            self._rebuild_library()
            GLib.idle_add(lambda: (self.lib_scroll.get_vadjustment().set_value(y), False)[1])
        else:
            if pid:
                self._facts.pop(str(pid), None)
            else:
                self._facts.clear()
            self._refilter()
        if self.current_pid and (not pid or pid == self.current_pid):
            self.page.refresh_state()

    def manage_game_tags(self, pid: str) -> None:
        from .tags import TagsDialog
        TagsDialog(self.backend, pid, self.game_meta_changed).present(self)

    def set_game_hidden(self, pid: str, hidden: bool) -> None:
        """„Spiel ausblenden“ mit „Rückgängig“ wie Galaxy; „Spiel wieder einblenden“ auch für bei GOG ausgeblendete."""
        e = self.backend.entry(pid)
        if hidden:
            self.backend.game_settings.update(pid, hidden=True, unhidden=False)
            toast = Adw.Toast.new(GLib.markup_escape_text(tr('Das Spiel wurde ausgeblendet.')))
            toast.set_button_label(tr('Rückgängig'))
            toast.connect("button-clicked", lambda *_: self.set_game_hidden(pid, False))
            self.toasts.add_toast(toast)
        else:
            self.backend.game_settings.update(pid, hidden=False, unhidden=bool(e is not None and e.is_hidden))
        self._facts.pop(str(pid), None)
        if is_lib_view(self.view):
            self._rebuild_library()
        self.update_nav_badges()

    def configure_game(self, pid: str) -> None:
        from .configure import ConfigureDialog
        ConfigureDialog(self, pid).present(self)

    def open_power_search(self, text: str = "") -> None:
        from .power_search_dialog import PowerSearchDialog
        PowerSearchDialog(self, text).present(self)

    def _set_view(self, **changes) -> None:
        """Ansicht speichern: in einer eigenen Bibliothek nur für diese, sonst für die GOG-Ansichten."""
        for k in changes:                     # eigene Wahl geht vor der Sortierung/Gruppierung eines Lesezeichens
            self._view_override.pop(k, None)
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
        elif self.view == "overview":
            y = self.overview.get_vadjustment().get_value()
            self.overview.rebuild()
            GLib.idle_add(lambda: (self.overview.get_vadjustment().set_value(y), False)[1])
        self.update_nav_badges()
        self.update_overview_badge()

    def update_overview_badge(self) -> None:
        """„Neu“ bei der Aktuellen Übersicht, solange der Vorschlag „Was als Nächstes spielen“ noch nicht gezeigt wurde
        (selten aufgerufen: Start, Ansichtswechsel, Bibliothek/Installationen geändert, halbstündlich)."""
        it = self.nav.get("overview")
        if it is None:
            return
        try:
            pick = rc.suggestion(self.backend, time.time())
        except Exception:  # pragma: no cover
            log.exception("Vorschlag „Was als Nächstes spielen“ fehlgeschlagen")
            pick = None
        new = rc.is_new_suggestion(pick.id if pick else "", self.backend.settings.recent_next_seen)
        order = rc.merge_order(self.backend.settings.recent_widgets, [w.key for w in ui_registry.recent_widgets()])
        if "next_to_play" not in rc.enabled_keys(order):
            new = False                        # Widget abgeschaltet: nichts Neues zu sehen
        it.set_badge(tr('Neu') if new else "", accent=True)

    # ------------------------------------------------------------------ Konto / Daten
    def _update_avatar(self) -> None:
        user = self.backend.api.user or {}
        name = user.get("username") or ("" if not self.backend.api.logged_in else tr('GOG'))
        self.avatar.set_text(name or tr('GOG'))
        self.avatar_btn.set_tooltip_text(name or tr('Nicht angemeldet'))
        # das Menü nur neu bauen, wenn sich Anmeldung oder Name ändern – ein Abgleich im Hintergrund (Timer, Fokus)
        # würde sonst ein gerade geöffnetes Avatar-Menü schließen
        key = (self.backend.api.logged_in, name)
        if key != getattr(self, "_avatar_key", None) and not self.avatar_btn.get_active():
            self._avatar_key = key
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
        """Store-Seite öffnen (Pfad wie "/games", "/redeem" oder volle gog.com-Adresse; "" = zuletzt besuchte Seite)
        im eingebetteten Store in der Sprache der Oberfläche; ohne webkitgtk-6.0 im Browser (ui/store.py)."""
        store.open_store(self, path)

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
            self._post_updates(found)
            queued = self.backend.auto_update(found) if found and self.backend.settings.feature_auto_update else []
            if queued:
                self.toast(tr('Updates werden installiert: {0} Spiel(e)').format(len(queued)))
                if self.current_pid:
                    self.page.refresh_state()
            elif found:
                self.toast(tr('Updates verfügbar für {0} Spiel(e)').format(len(found)))
            elif not quiet:
                self.toast(tr('Alle installierten Spiele sind aktuell'))
        run_async(self.backend.check_updates, done, lambda e: None if quiet else self.toast(str(e)))

    def _post_updates(self, found: dict) -> None:
        """Benachrichtigungszentrum: „Updates verfügbar“ – je Stand (Spiele und Builds) nur einmal."""
        if not found:
            return
        titles = []
        for pid in found:
            e = self.backend.entry(pid)
            titles.append(e.title if e else pid)
        key = "updates:" + ",".join(f"{pid}@{getattr(b, 'build_id', '')}" for pid, b in sorted(found.items()))
        if len(found) == 1:
            title = tr('Update verfügbar: {0}').format(titles[0])
            body = ""
        else:
            title = tr('Updates verfügbar für {0} Spiel(e)').format(len(found))
            body = ", ".join(titles[:3]) + (" …" if len(titles) > 3 else "")
        self.notifications.post("update", title, body, action="updates", key=key)

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
            if any(g.method == "external" for g in report.imported):
                self.resolve_externals_async()
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

    def resolve_externals_async(self, force: bool = False) -> None:
        """Externe Spiele erkennen (Kopien, offizielle Namen, Cover) – im Hintergrund, danach Ansicht und GOverlay."""
        def done(changed: list) -> None:
            if not changed:
                return
            self.titles_changed(changed)
        run_async(lambda: self.backend.resolve_externals(force=force), done,
                  lambda e: log.warning("Erkennung externer Spiele fehlgeschlagen: %s", e))

    def titles_changed(self, pids: list | None = None) -> None:
        """Titel, Cover oder Kopien geändert („Bearbeiten …“, Erkennung): Bibliothek, Spielansicht und GOverlay."""
        self._facts.clear()
        if is_lib_view(self.view):
            y = self.lib_scroll.get_vadjustment().get_value()
            self._rebuild_library()
            GLib.idle_add(lambda: (self.lib_scroll.get_vadjustment().set_value(y), False)[1])
        if self.current_pid and self.view == "game" and (pids is None or self.current_pid in pids):
            self.page.set_game(self.current_pid)
        self.update_nav_badges()
        sync = getattr(self, "goverlay_sync", None)
        if sync is not None:
            sync.request()

    def edit_game(self, pid: str) -> None:
        """„Bearbeiten …“ wie in Galaxy: eigener Titel und eigenes Cover."""
        from .edit_dialog import EditGameDialog
        EditGameDialog(self, pid).present(self)

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

    # ------------------------------------------------------------------ Verhalten des Hauptfensters, Tray
    def _start_view(self) -> str:
        """Einstellungen → Allgemein → Startseite wie Galaxy: Vorherige Sitzung, Entdecken, Aktuelle Übersicht
        (``recent``), Store, Spiele im Besitz, Installiert."""
        s = self.backend.settings
        page = {"recent": "overview"}.get(s.starting_page, s.starting_page)
        if page == "session":
            last = s.last_view or "all"
            if last.startswith("game:") and self.backend.entry(last[5:]) is not None:
                GLib.idle_add(lambda: (self.show_game(last[5:]), False)[1])
                return "all"
            if last.startswith("bm:") and self._bookmark(last[3:]) is not None:
                return last
            if last.startswith("lib:") and self.backend.get_library(last[4:]) is not None:
                return last
            if last.startswith("store:") or last == "store":
                GLib.idle_add(lambda: (self.open_store(""), False)[1])
                return "all"
            return last if last in (*START_VIEWS, *LIB_VIEWS, "downloads") else "all"
        if page == "store":
            GLib.idle_add(lambda: (self.open_store(""), False)[1])
            return "all"
        return page if page in START_VIEWS else "all"

    def remember_view(self) -> None:
        """Für „Vorherige Sitzung fortsetzen“ beim Beenden merken, wo man war."""
        view = f"game:{self.current_pid}" if self.view == "game" and self.current_pid else self.view
        if view != self.backend.settings.last_view:
            self.backend.settings.last_view = view
            self.backend.save_settings()

    def _tray_wanted(self) -> bool:
        s = self.backend.settings
        return "tray" in (s.when_minimizing, s.when_closing, s.when_launching_game)

    def apply_window_settings(self) -> None:
        """Nach dem Start und nach Änderungen in den Einstellungen: Tray-Symbol, Bit/s-Anzeige, Online-Status."""
        util.SPEED_IN_BITS[0] = self.backend.settings.speed_in_bits
        if self._tray_wanted():
            self.tray.show()
        elif self.get_visible():
            self.tray.hide()
        if self.backend.api.logged_in:
            self.presence.start()
            self.presence.refresh()

    def _to_tray(self) -> bool:
        """Ins System-Tray: Fenster ausblenden, der Client läuft weiter. False, wenn es keinen Tray gibt."""
        if not self.tray.show():
            return False
        self.set_visible(False)
        log.info("Fenster in den System-Tray minimiert")
        return True

    def minimize_window(self) -> None:
        if self.backend.settings.when_minimizing == "tray" and self._to_tray():
            return
        self.minimize()

    def show_from_tray(self) -> None:
        self.set_visible(True)
        self.present()
        if not self._tray_wanted():
            self.tray.hide()

    def game_launched(self, pid: str) -> None:
        """„Wenn ein Spiel gestartet wird“: nichts, in die Taskleiste oder in den System-Tray; Online-Status mit Spiel."""
        mode = self.backend.settings.when_launching_game
        if mode == "tray" and self._to_tray():
            pass
        elif mode in ("taskbar", "tray"):
            self.minimize()
        self.presence.set_game(pid)

    def game_exited(self, pid: str) -> None:
        running = [p for p in self.backend.launcher.active_ids() if p != pid]
        self.presence.set_game(running[0] if running else "")
        # neue Spielzeit: Aktuelle Übersicht (Zuletzt gespielt, Meine Übersicht, Vorschlag) auf den neuen Stand
        if self.view == "overview":
            self.populate()
        else:
            self.update_overview_badge()

    def pull_playtime_async(self) -> None:
        """Spielzeit aller Geräte von GOG für installierte und zuletzt gespielte Spiele (je Spiel höchstens alle 6 h)."""
        if not self.backend.api.logged_in or not self.backend.settings.sync_playtime:
            return
        pids = [g.product_id for g in self.backend.registry.all() if g.method != "external"]
        pids += [e.id for e in self.backend.library if self.backend.playtime.get(e.id)[1] > 0 and e.id not in pids]

        def done(n: int) -> None:
            if n:
                if is_lib_view(self.view):
                    self._facts.clear()
                    self._refilter()
                if self.current_pid:
                    self.page.refresh_state()
        run_async(lambda: self.backend.pull_playtime(pids, limit=60), done, lambda e: None)

    def notify_desktop(self, key: str, title: str, body: str = "", kind: str = "download", action: str = "") -> None:
        """Ältere Aufrufe: Meldung über das Benachrichtigungszentrum (Desktop nur, solange das Fenster nicht im
        Vordergrund ist, je nach Einstellung des Typs)."""
        self.notifications.post(kind, title, body, action=action, key=key)

    # ------------------------------------------------------------------ Schließen / Beenden
    def _on_close_request(self, *_: object) -> bool:
        """Fenster schließen: „Bei Schließung des Fensters“ → System-Tray blendet nur aus. Laufen Spiele (oder ihre
        Nachbereitung: Spielzeit, Cloud-Upload, comet), wird es ebenfalls nur ausgeblendet und der Client endet erst
        danach – wie Galaxy, das im Infobereich weiterläuft."""
        self.remember_view()
        if self.backend.settings.when_closing == "tray" and self._to_tray():
            return True
        self.bridge.cancel_all()
        if self.backend.launcher.active_ids():
            self._hide_until_games_end()
            return True
        return False

    def request_quit(self) -> None:
        """„Beenden“ (Zahnrad, Strg+Q, Tray): wie Schließen – mit laufenden Spielen erst nach deren Ende."""
        self.remember_view()
        if self.backend.launcher.active_ids():
            self.bridge.cancel_all()
            self._hide_until_games_end()
            return
        app = self.get_application()
        if app is not None:
            app.quit()

    def _hide_until_games_end(self) -> None:
        log.info("Fenster geschlossen, Spiele laufen noch (%s) – Client endet nach dem Spielende",
                 ", ".join(self.backend.launcher.active_ids()))
        self.set_visible(False)
        if not self._quit_timer:
            self._quit_timer = GLib.timeout_add_seconds(2, self._quit_when_idle)

    def _quit_when_idle(self) -> bool:
        if self.get_visible() or (self.tray.shown and self.backend.settings.when_closing == "tray"):
            self._quit_timer = 0                # wieder geöffnet bzw. bleibt im System-Tray
            return False
        if self.backend.launcher.active_ids():
            return True
        self._quit_timer = 0
        app = self.get_application()
        if app is not None:
            app.quit()
        return False

    def do_login(self) -> None:
        LoginDialog(self.backend, self._after_login).present(self)

    def _after_login(self, user: dict) -> None:
        self.toast(tr('Angemeldet als {0}').format(user.get('username', '')))
        self._update_avatar()
        self.presence.start()
        self.presence.refresh()
        store.on_login(self)            # Store-Seite mit den neuen Cookies neu laden, Wunschliste zählen
        if getattr(self, "friends", None) is not None:
            self.friends.on_login()     # Freundesliste, Einladungen, Präsenz sofort holen
        self.request_library_refresh("login", force=True)

    def do_logout(self) -> None:
        # erst offline gehen (braucht noch die Anmeldung), dann abmelden
        run_async(lambda: self.presence.tick(final=True), lambda _r: self._finish_logout(), lambda _e: self._finish_logout())

    def _finish_logout(self) -> None:
        self.backend.logout()
        store.on_logout(self)           # auch die Store-Sitzung (Cookies) abmelden
        if getattr(self, "friends", None) is not None:
            self.friends.on_logout()    # Freunde, Zähler und beobachtete Aktivität vergessen, Chat schließen
        self.backend.library = []
        self.avatar.set_custom_image(None)
        self._update_avatar()
        self.navigate("all")
        self.toast(tr('Abgemeldet'))

    def open_preferences(self, page: str = "") -> None:
        def changed() -> None:
            self.libraries_changed()
            self.backend.launcher.tools(refresh=True)
            self.apply_window_settings()
            self._rebuild_bookmark_nav()
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
        if any(j.state == "running" for j in self.backend.jobs.active()):
            self.spinner.start()
        else:
            self.spinner.stop()
        if self.view == "downloads":
            self.downloads.refresh()
        if job.kind == "prefix" and job.state == "done":
            self._prefix_done.append(job.title)
            if not any(j.kind == "prefix" for j in self.backend.jobs.active()):
                done, self._prefix_done = self._prefix_done, []
                self.toast(tr('Prefix angelegt: {0}').format(done[0]) if len(done) == 1 else tr('{0} Prefixe angelegt').format(len(done)))
        elif job.state == "done":
            text = tr('{0} abgeschlossen: {1}').format(job.label, job.title) if job.title != job.label \
                else tr('{0} abgeschlossen').format(job.label)
            self.toast(text)
            self._post_job(job, tr('Installation abgeschlossen') if job.kind.startswith("install") else text)
            self.update_overview_badge()           # neu installiert: vielleicht ein neuer Vorschlag
            # Aufträge ohne Spiel der Bibliothek (z. B. Galaxy-Redistributables) ändern keine Karte – kein Neuaufbau,
            # der die Bibliothek nach oben springen ließe
            if is_lib_view(self.view) and (self.backend.registry.is_installed(job.product_id)
                                           or self.backend.entry(job.product_id) is not None):
                y = self.lib_scroll.get_vadjustment().get_value()
                self._rebuild_library()
                GLib.idle_add(lambda: (self.lib_scroll.get_vadjustment().set_value(y), False)[1])
        elif job.state == "failed":
            self.toast(tr('{0} fehlgeschlagen: {1}').format(job.label, job.error))
            self._post_job(job, tr('{0} fehlgeschlagen: {1}').format(job.label, job.error))
        elif job.state == "cancelled":
            self.toast(tr('{0} abgebrochen: {1}').format(job.label, job.title))

    def _post_job(self, job, body: str) -> None:
        """Benachrichtigungszentrum: Download/Installation/Update fertig oder fehlgeschlagen (Prefixe nicht). Ein Klick
        öffnet das Spiel bzw. bei Fehlern die Downloads; Galaxy-Redistributables melden sich als Problem."""
        if job.kind == "prefix":
            return
        failed = job.state == "failed"
        kind = "redist" if job.kind == "redist" and failed else "download"
        known = self.backend.entry(job.product_id) is not None or self.backend.registry.is_installed(job.product_id)
        action = "view:downloads" if failed or not known else f"game:{job.product_id}"
        self.notifications.post(kind, job.title, body, action=action, product_id=job.product_id if known else "",
                                key=f"job:{job.id}:{job.state}")
