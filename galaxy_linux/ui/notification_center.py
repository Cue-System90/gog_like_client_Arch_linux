"""Benachrichtigungszentrum wie in GOG Galaxy 2.1: Glocke in der Kopfzeile mit der Zahl ungelesener Meldungen, darunter
ein Popover mit der Liste (neueste zuerst; Symbol, Text, Zeit), „Als gelesen markieren“ und „Alle löschen“. Ein Klick
auf eine Meldung führt ihre Aktion aus (Spielansicht, Store-Seite, Downloads …) und markiert sie als gelesen.

``post()`` ist der eine Weg für alle Quellen (Downloads, Updates, neue Spiele, Cloud-Spielstände, Redistributables,
Wunschliste, Freunde): Je Typ entscheidet Einstellungen → Benachrichtigungen, ob die Meldung ins Zentrum und
auf den Desktop (Gio.Notification, nur wenn das Fenster nicht im Vordergrund ist) geht. Ein Klick auf die
Desktop-Meldung öffnet das Fenster und führt dieselbe Aktion aus (``app.notification``).
Daten und Regeln ohne GTK: ``galaxy_linux/notifications.py``.
"""
from __future__ import annotations

from ..i18n import tr

import logging

from .. import notifications as nt, paths
from . import footer
from .util import GLib, Gio, Gtk, Pango, label, pick_icon, relative_time

log = logging.getLogger(__name__)

KIND_ICONS = {
    "download": ("folder-download-symbolic", "go-down-symbolic"),
    "update": ("software-update-available-symbolic", "view-refresh-symbolic"),
    "redist": ("dialog-warning-symbolic", "emblem-important-symbolic"),
    "library": ("applications-games-symbolic", "input-gaming-symbolic"),
    "cloud": ("weather-overcast-symbolic", "folder-remote-symbolic"),
    "wishlist": ("starred-symbolic", "emblem-favorite-symbolic"),
    "available": ("starred-symbolic", "emblem-favorite-symbolic"),
}
BELL = ("preferences-system-notifications-symbolic", "alarm-symbolic")
LIST_HEIGHT = 460


def kind_icon(kind: str) -> str:
    return pick_icon(*KIND_ICONS.get(kind, ("dialog-information-symbolic",)))


def count_text(n: int) -> str:
    return "99+" if n > 99 else (str(n) if n > 0 else "")


class NotificationCenter:
    """Glocke, Popover und Weiterleitung; liegt als ``win.notifications`` am Hauptfenster."""

    def __init__(self, win) -> None:
        self.win = win
        self.store = nt.NotificationStore(paths.NOTIFICATIONS_FILE)
        self.store.listeners.append(self._changed)
        self.button = Gtk.MenuButton(tooltip_text=tr('Benachrichtigungen'))
        self.button.add_css_class("flat")
        self.button.add_css_class("hdr-btn")
        self.button.set_valign(Gtk.Align.CENTER)
        ov = Gtk.Overlay()
        ov.set_child(Gtk.Image(icon_name=pick_icon(*BELL), pixel_size=16))
        self.count = label("", css=["notif-count"])
        self.count.set_halign(Gtk.Align.END)
        self.count.set_valign(Gtk.Align.START)
        self.count.set_can_target(False)
        ov.add_overlay(self.count)
        self.button.set_child(ov)
        self.popover: Gtk.Popover | None = None
        self.list: Gtk.ListBox | None = None
        self.button.set_create_popup_func(lambda b: b.set_popover(self._build_popover()))
        self._update_count()

    # ------------------------------------------------------------------ Meldungen annehmen
    def post(self, kind: str, title: str, body: str = "", action: str = "", product_id: str = "",
             key: str = "") -> nt.Notification | None:
        """Meldung je nach Einstellung ins Zentrum und/oder auf den Desktop (GTK-Hauptthread)."""
        t = nt.type_for_kind(kind)
        focused = bool(self.win.get_visible() and self.win.is_active())
        to_center, to_desktop = nt.route(nt.mode_for(self.win.backend.settings, kind), focused,
                                         bool(t and t.focus_desktop))
        if not to_center:
            return None
        n = self.store.add(kind, title, body, action=action, product_id=product_id, key=key)
        if n is None:
            return None                      # schon gemeldet (gleicher Schlüssel)
        if to_desktop:
            self._desktop(n)
        return n

    def _desktop(self, n: nt.Notification) -> None:
        app = self.win.get_application()
        if app is None:
            return
        try:
            g = Gio.Notification.new(n.title)
            if n.body:
                g.set_body(n.body)
            g.set_default_action_and_target("app.notification", GLib.Variant("s", n.id))
            app.send_notification(f"gl-{n.kind}-{n.id}", g)
        except Exception as exc:  # pragma: no cover - ohne Benachrichtigungsdienst
            log.debug("Desktop-Benachrichtigung nicht möglich: %s", exc)

    # ------------------------------------------------------------------ Aktionen
    def open(self, nid: str) -> None:
        """Meldung anklicken (im Popover oder auf dem Desktop): gelesen markieren und ihre Aktion ausführen."""
        n = self.store.get(nid)
        if n is None:
            return
        self.store.mark_read(nid)
        if self.popover is not None:
            self.popover.popdown()
        self.perform(n.action)

    def perform(self, action: str) -> None:
        w = self.win
        kind, _sep, arg = (action or "").partition(":")
        try:
            if kind == "game" and arg:
                w.show_game(arg)
            elif kind == "store":
                w.open_store(arg or "/")
            elif kind == "view" and arg:
                w.navigate(arg)
            elif kind == "updates":
                footer.open_available_updates(w)
            elif kind == "prefs":
                w.open_preferences(arg)
        except Exception:
            log.exception("Aktion der Benachrichtigung „%s“ fehlgeschlagen", action)

    # ------------------------------------------------------------------ Glocke und Popover
    def _changed(self) -> None:
        self._update_count()
        if self.popover is not None and self.popover.get_visible():
            self._fill()

    def _update_count(self) -> None:
        n = self.store.unread_count()
        self.count.set_text(count_text(n))
        self.count.set_visible(n > 0)
        self.button.set_tooltip_text(tr('Benachrichtigungen') + (f" ({n})" if n else ""))

    def _build_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("notif-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=8, margin_start=8,
                      margin_end=8)
        box.set_size_request(380, -1)
        head = Gtk.Box(spacing=4)
        title = label(tr('BENACHRICHTIGUNGEN'), css=["sec-hdr"])
        title.set_hexpand(True)
        title.set_margin_start(4)
        head.append(title)
        self.read_btn = Gtk.Button(label=tr('Als gelesen markieren'))
        self.read_btn.add_css_class("flat")
        self.read_btn.connect("clicked", lambda *_: self.store.mark_read())
        head.append(self.read_btn)
        self.clear_btn = Gtk.Button(label=tr('Alle löschen'))
        self.clear_btn.add_css_class("flat")
        self.clear_btn.connect("clicked", lambda *_: self.store.clear())
        head.append(self.clear_btn)
        box.append(head)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.list.add_css_class("notif-list")
        self.list.connect("row-activated", lambda _l, row: self.open(row.nid))
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                         max_content_height=LIST_HEIGHT)
        self.scroll.set_child(self.list)
        box.append(self.scroll)
        self.empty = label(tr('Keine Benachrichtigungen'), css=["dim"], xalign=0.5)
        self.empty.set_margin_top(18)
        self.empty.set_margin_bottom(18)
        box.append(self.empty)
        pop.set_child(box)
        self.popover = pop
        self._fill()
        return pop

    def _fill(self) -> None:
        if self.list is None:
            return
        self.list.remove_all()
        items = self.store.items()
        for n in items:
            self.list.append(self._row(n))
        self.empty.set_visible(not items)
        self.scroll.set_visible(bool(items))
        self.read_btn.set_sensitive(any(not n.read for n in items))
        self.clear_btn.set_sensitive(bool(items))

    def _row(self, n: nt.Notification) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.nid = n.id
        row.add_css_class("notif-row")
        if not n.read:
            row.add_css_class("unread")
        box = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        icon = Gtk.Image(icon_name=kind_icon(n.kind), pixel_size=18, valign=Gtk.Align.START)
        icon.add_css_class("notif-icon")
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(label(n.title, css=["notif-title"], wrap=True))
        if n.body:
            body = label(n.body, css=["notif-body"], wrap=True)
            body.set_lines(3)
            body.set_ellipsize(Pango.EllipsizeMode.END)
            text.append(body)
        text.append(label(relative_time(n.created), css=["dim", "caption"]))
        box.append(text)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("notif-dot")
        dot.set_visible(not n.read)
        box.append(dot)
        row.set_child(box)
        row.set_tooltip_text(n.title)
        return row
