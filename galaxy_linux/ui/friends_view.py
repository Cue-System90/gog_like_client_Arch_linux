"""Freundesliste wie in GOG Galaxy 2.1 („Freundesliste“) und die Freunde-Seitenleiste rechts.

Freundesliste: oben Suche, „Ansicht“ (Erweiterte/Kompakte Liste, Nach Aktivität/Namen sortieren, Nicht gruppieren/
Nach gespieltem Spiel gruppieren – je ein Klick, gespeichert) und „Freunde hinzufügen“ (öffnet die Freundesseite von
gog.com im eingebetteten Store). Darunter „Erhaltene Einladungen und Empfehlungen“ mit „Annehmen“/„Ablehnen“, dann
ONLINE (im Spiel, online, abwesend; gruppiert nach Spiel auf Wunsch) und OFFLINE („Zuletzt online …“, soweit
galaxy-linux es beobachtet hat). Jede Zeile hat „Nachricht senden“, „Profil ansehen“ und „Aus Freundesliste
entfernen“ als Knöpfe und im Kontextmenü (Rechtsklick); ein Klick auf die Zeile öffnet den Chat.

Freunde-Seitenleiste: kompakte Liste der Freunde, die online sind – nur bei Fenstern ab 1501 px und wenn
Einstellungen → Oberfläche → „Zeige eine Seitenleiste für Freunde, die online sind“ an ist (Galaxy: showFriendsSidebar).
Daten und Aktionen: ``win.friends`` (ui/ext_friends.py), Regeln ohne GTK: ``galaxy_linux/friends.py``.
"""
from __future__ import annotations

from ..i18n import tr

import logging

from gi.repository import Graphene

from .. import friends as fr
from ..api.friends import Friend, Invitation
from .util import Adw, Gio, GLib, Gtk, icon_button, label, pick_icon, relative_time

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 250
ICON_CHAT = ("chat-message-new-symbolic", "dialog-messages-symbolic", "mail-message-new-symbolic", "mail-send-symbolic")
ICON_PROFILE = ("avatar-default-symbolic", "user-info-symbolic", "system-users-symbolic")
ICON_REMOVE = ("list-remove-user-symbolic", "user-trash-symbolic", "list-remove-symbolic")


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def avatar(win, name: str, url: str, size: int, kind: str | None = None) -> Gtk.Widget:
    """Profilbild (Initialen, bis das Bild geladen ist) mit Präsenz-Punkt unten rechts wie in Galaxy."""
    ov = Gtk.Overlay()
    av = Adw.Avatar(size=size, text=name or "?", show_initials=True)
    ov.set_child(av)
    if kind is not None and kind != fr.OFFLINE:
        dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        dot.add_css_class("presence-dot")
        dot.add_css_class(kind)
        dot.set_can_target(False)
        ov.add_overlay(dot)
    if url:
        win.images.load(url, av.set_custom_image)
    return ov


def status_line(ctl, f: Friend) -> tuple[str, str]:
    """(Art, Text) für eine Zeile: playing/online/away/offline und „Spielt – …“, „Zuletzt online …“."""
    snap = ctl.snapshot
    p = snap.presence.get(f.user_id) if snap else None
    title = snap.titles.get(p.game_id, "") if snap and p and p.game_id else ""
    return fr.status_kind(p), fr.status_text(p, title, ctl.last_seen(f.user_id), relative_time)


def friend_menu(ctl, f: Friend, parent: Gtk.Widget) -> Gtk.Popover:
    """Kontextmenü wie Galaxys friend_context_menu: Nachricht senden, Profil ansehen, Aus Freundesliste entfernen."""
    pop = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6, margin_start=6,
                  margin_end=6)
    for text, cb, danger in ((tr('Nachricht senden'), lambda: ctl.open_chat(f), False),
                             (tr('Profil ansehen'), lambda: ctl.open_profile(f), False),
                             (tr('Aus Freundesliste entfernen'), lambda: ctl.confirm_remove(f), True)):
        b = Gtk.Button(label=text)
        b.add_css_class("flat")
        b.get_child().set_xalign(0)
        if danger:
            b.add_css_class("destructive-text")
        b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
        box.append(b)
    pop.set_child(box)
    pop.set_parent(parent)
    pop.set_has_arrow(True)
    pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
    return pop


class FriendRow(Gtk.ListBoxRow):
    def __init__(self, ctl, f: Friend, compact: bool, actions: bool = True) -> None:
        super().__init__()
        self.friend = f
        self.add_css_class("friend-row")
        kind, text = status_line(ctl, f)
        box = Gtk.Box(spacing=12 if not compact else 10, margin_top=4 if compact else 7, margin_bottom=4 if compact else 7,
                      margin_start=8, margin_end=6)
        box.append(avatar(ctl.win, f.username, f.avatar, 26 if compact else 40, kind))
        if compact:
            tb = Gtk.Box(spacing=8, hexpand=True, valign=Gtk.Align.CENTER)
        else:
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        name = label(f.username or f.user_id, css=["row-title"], ellipsize=True)
        tb.append(name)
        st = label(text, css=["friend-status", kind], ellipsize=True)
        if compact:
            st.set_hexpand(True)
        tb.append(st)
        box.append(tb)
        if actions:
            for icons, tip, cb in ((ICON_CHAT, tr('Nachricht senden'), lambda: ctl.open_chat(f)),
                                   (ICON_PROFILE, tr('Profil ansehen'), lambda: ctl.open_profile(f)),
                                   (ICON_REMOVE, tr('Aus Freundesliste entfernen'), lambda: ctl.confirm_remove(f))):
                b = icon_button(pick_icon(*icons), tip, ["flat", "friend-action"])
                b.set_valign(Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, cb=cb: cb())
                box.append(b)
        self.set_child(box)
        self.set_tooltip_text(f"{f.username} – {text}" if f.username else text)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda *_a: friend_menu(ctl, f, self).popup())
        self.add_controller(click)


class InvitationRow(Gtk.ListBoxRow):
    def __init__(self, ctl, inv: Invitation, compact: bool) -> None:
        super().__init__(activatable=False)
        self.invitation = inv
        self.add_css_class("friend-row")
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=8, margin_end=6)
        box.append(avatar(ctl.win, inv.username, inv.avatar, 26 if compact else 40))
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        tb.append(label(inv.username or inv.user_id, css=["row-title"], ellipsize=True))
        when = tr('Erhalten {0}').format(relative_time(inv.created)) if inv.created else tr('Freundschaftseinladung erhalten')
        tb.append(label(when, css=["friend-status"], ellipsize=True))
        box.append(tb)
        busy = ctl.is_busy(inv.user_id)
        if busy:
            sp = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
            box.append(sp)
        accept = Gtk.Button(label=tr('Annehmen'), valign=Gtk.Align.CENTER, sensitive=not busy)
        accept.add_css_class("friend-accept")
        accept.connect("clicked", lambda *_: ctl.accept(inv))
        decline = Gtk.Button(label=tr('Ablehnen'), valign=Gtk.Align.CENTER, sensitive=not busy)
        decline.add_css_class("friend-decline")
        decline.connect("clicked", lambda *_: ctl.decline(inv))
        box.append(accept)
        box.append(decline)
        self.set_child(box)


def _list() -> Gtk.ListBox:
    lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
    lst.add_css_class("friend-list")
    return lst


def _section_head(title: str, count: int | None = None) -> Gtk.Widget:
    hb = Gtk.Box(spacing=6)
    hb.append(label(title.upper(), css=["sec-hdr"]))
    if count is not None:
        hb.append(label(str(count), css=["sec-hdr", "dim"]))
    return hb


class FriendsPage(Gtk.ScrolledWindow):
    """Seite „friends“ im Inhaltsbereich."""

    def __init__(self, ctl) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.ctl, self.win = ctl, ctl.win
        self._query = ""
        self._inv_box: Gtk.Widget | None = None
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=18, margin_bottom=28,
                        margin_start=28, margin_end=28, valign=Gtk.Align.START)
        head = Gtk.Box(spacing=8)
        title = label(tr('Freundesliste'), css=["page-title"])
        title.set_hexpand(True)
        head.append(title)
        self.search = Gtk.SearchEntry(placeholder_text=tr('Freunde suchen'))
        self.search.add_css_class("hdr-search")
        self.search.set_size_request(220, -1)
        self.search.set_valign(Gtk.Align.CENTER)
        self.search.connect("search-changed", self._on_search)
        head.append(self.search)
        self.view_btn = Gtk.MenuButton(tooltip_text=tr('Ansicht'))
        vb = Gtk.Box(spacing=6)
        vb.append(Gtk.Image(icon_name=pick_icon("view-list-symbolic", "view-grid-symbolic"), pixel_size=14))
        vb.append(label(tr('Ansicht')))
        self.view_btn.set_child(vb)
        self.view_btn.add_css_class("hdr-pill")
        self.view_btn.set_valign(Gtk.Align.CENTER)
        self.view_btn.set_create_popup_func(lambda b: b.set_popover(self._view_popover()))
        head.append(self.view_btn)
        add = Gtk.Button()
        ab = Gtk.Box(spacing=6)
        ab.append(Gtk.Image(icon_name=pick_icon("contact-new-symbolic", "list-add-symbolic"), pixel_size=14))
        ab.append(label(tr('Freunde hinzufügen')))
        add.set_child(ab)
        add.add_css_class("hdr-pill")
        add.set_valign(Gtk.Align.CENTER)
        add.set_tooltip_text(tr('Öffnet die Freundesseite von gog.com im eingebetteten Store'))
        add.connect("clicked", lambda *_: ctl.add_friends())
        head.append(add)
        outer.append(head)
        self.hint = label(tr('„Freunde hinzufügen“ öffnet die Freundesseite von gog.com im eingebetteten Store: Galaxys '
                             'eigene Suche nach Benutzername oder E-Mail nutzt eine Schnittstelle, auf die sich '
                             'galaxy-linux nicht verlassen kann. Dort suchst du Freunde und schickst Einladungen; '
                             'angenommene Freunde erscheinen hier beim nächsten Abgleich.'), css=["dim", "caption"],
                          wrap=True)
        outer.append(self.hint)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        outer.append(self.body)
        self.set_child(outer)
        ctl.listeners.append(self._changed)

    # ------------------------------------------------------------------ Aufbau
    def on_show(self) -> None:
        self.rebuild()
        self.ctl.on_view_shown()

    def _changed(self) -> None:
        if self.get_mapped():
            y = self.get_vadjustment().get_value()
            self.rebuild()
            GLib.idle_add(lambda: (self.get_vadjustment().set_value(y), False)[1])

    def focus_section(self, section: str) -> None:
        if section == "invitations" and self._inv_box is not None:
            GLib.idle_add(lambda: (self._scroll_to(self._inv_box), False)[1])

    def _scroll_to(self, widget: Gtk.Widget) -> None:
        ok, point = widget.compute_point(self.get_child(), Graphene.Point())
        if ok:
            self.get_vadjustment().set_value(max(0.0, point.y - 8))

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text()
        self.rebuild()

    def _status_page(self, title: str, text: str, button: tuple[str, object] | None = None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=40, halign=Gtk.Align.CENTER)
        box.append(Gtk.Image(icon_name=pick_icon("system-users-symbolic", "avatar-default-symbolic"), pixel_size=48,
                             css_classes=["dim"]))
        box.append(label(title, css=["shelf-title"], xalign=0.5))
        box.append(label(text, css=["dim"], wrap=True, xalign=0.5))
        if button is not None:
            b = Gtk.Button(label=button[0], halign=Gtk.Align.CENTER)
            b.add_css_class("btn-secondary")
            b.set_margin_top(6)
            b.connect("clicked", lambda *_: button[1]())
            box.append(b)
        return box

    def rebuild(self) -> None:
        _clear(self.body)
        self._inv_box = None
        ctl = self.ctl
        s = self.win.backend.settings
        compact = bool(s.friends_compact)
        if not ctl.available:
            self.body.append(self._status_page(tr('Nicht angemeldet'), tr('Melde dich bei GOG an, um deine Freunde zu sehen.'),
                                               (tr('BEI GOG ANMELDEN'), self.win.do_login)))
            return
        snap = ctl.snapshot
        if snap is None:
            if ctl.error:
                self.body.append(self._status_page(tr('Freundesliste nicht verfügbar'),
                                                   tr('GOG hat nicht geantwortet: {0}').format(ctl.error),
                                                   (tr('ERNEUT VERSUCHEN'), lambda: ctl.refresh(force=True))))
            else:
                sp = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=40)
                sp.set_size_request(32, 32)
                self.body.append(sp)
                self.body.append(label(tr('Freundesliste wird geladen …'), css=["dim"], xalign=0.5))
            return
        if ctl.error:
            self.body.append(label(tr('Letzter Abgleich fehlgeschlagen ({0}) – angezeigt wird der vorige Stand.').format(
                ctl.error), css=["dim", "caption"], wrap=True))
        q = self._query
        invs = [i for i in snap.invitations if fr.matches(i.username, q)]
        if invs:
            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("widget-panel")
            panel.append(_section_head(tr('Erhaltene Einladungen und Empfehlungen'), len(invs)))
            lst = _list()
            for inv in invs:
                lst.append(InvitationRow(ctl, inv, compact))
            panel.append(lst)
            self.body.append(panel)
            self._inv_box = panel
        friends = fr.filter_friends(snap.friends, q)
        online, offline = fr.sort_friends(friends, snap.presence, ctl.activity_last_seen(), s.friends_sort)
        if not snap.friends:
            self.body.append(self._status_page(tr('Noch keine Freunde'),
                                               tr('Schicke Einladungen an andere GOG Galaxy-Nutzer – „Freunde hinzufügen“ '
                                                  'öffnet dafür gog.com.'), (tr('Freunde hinzufügen'), ctl.add_friends)))
            return
        if not friends and not invs:
            self.body.append(label(tr('Kein Freund passt zu „{0}“.').format(q.strip()), css=["dim"]))
            return
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sec.append(_section_head(tr('Online'), len(online)))
        if online:
            for title, members in fr.group_online(online, snap.presence, snap.titles, s.friends_group):
                if s.friends_group == "game":
                    sub = label((title if title != fr.NOT_PLAYING else tr('Nicht im Spiel')) + f" · {len(members)}",
                                css=["friend-group"], ellipsize=True)
                    sec.append(sub)
                lst = _list()
                lst.connect("row-activated", lambda _l, r: ctl.open_chat(r.friend))
                for f in members:
                    lst.append(FriendRow(ctl, f, compact))
                sec.append(lst)
        else:
            sec.append(label(tr('Keine Freunde online'), css=["dim"]))
        self.body.append(sec)
        if offline:
            sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            sec.append(_section_head(tr('Offline'), len(offline)))
            lst = _list()
            lst.connect("row-activated", lambda _l, r: ctl.open_chat(r.friend))
            for f in offline:
                lst.append(FriendRow(ctl, f, compact))
            sec.append(lst)
            self.body.append(sec)

    # ------------------------------------------------------------------ Ansicht
    def _view_popover(self) -> Gtk.Popover:
        """Wie Galaxys Ansichts-Menü der Freundesliste: je Gruppe eine Auswahl, ein Klick wirkt sofort."""
        s = self.win.backend.settings
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=8, margin_bottom=8, margin_start=10,
                      margin_end=10)

        def radio(options: list[tuple[object, str]], current: object, key: str) -> None:
            first = None
            for value, text in options:
                b = Gtk.CheckButton(label=text, active=value == current)
                if first is None:
                    first = b
                else:
                    b.set_group(first)
                b.connect("toggled", lambda btn, v=value: btn.get_active() and self._set(key, v))
                box.append(b)

        radio([(False, tr('Erweiterte Liste')), (True, tr('Kompakte Liste'))], bool(s.friends_compact), "friends_compact")
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        radio([("activity", tr('Nach Aktivität sortieren')), ("name", tr('Nach Namen sortieren'))],
              s.friends_sort if s.friends_sort in fr.SORTS else "activity", "friends_sort")
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        radio([("none", tr('Nicht gruppieren')), ("game", tr('Nach gespieltem Spiel gruppieren'))],
              s.friends_group if s.friends_group in fr.GROUPS else "none", "friends_group")
        pop.set_child(box)
        return pop

    def _set(self, key: str, value) -> None:
        s = self.win.backend.settings
        if getattr(s, key) == value:
            return
        setattr(s, key, value)
        self.win.backend.save_settings()
        self.rebuild()
        self.ctl.notify()


class FriendsSidebar(Gtk.ScrolledWindow):
    """Rechte Seitenleiste: Freunde, die online sind (kompakt). Sichtbar ab 1501 px Fensterbreite, wenn neben dem
    übrigen Inhalt Platz ist. Als waagerecht „externe“ Rollfläche trägt sie nichts zur Mindestbreite des Fensters bei –
    beim Verkleinern wird sie erst schmaler und verschwindet dann, statt das Fenster breit zu halten."""

    def __init__(self, ctl) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                         propagate_natural_width=True, hexpand=False)
        self.ctl, self.win = ctl, ctl.win
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.add_css_class("friends-sidebar")
        inner.set_size_request(SIDEBAR_WIDTH, -1)
        head = Gtk.Box(spacing=6, margin_top=14, margin_start=14, margin_end=10, margin_bottom=6)
        self.title = label(tr('FREUNDE ONLINE'), css=["sec-hdr"])
        head.append(self.title)
        self.count = label("", css=["sec-hdr", "dim"])
        self.count.set_hexpand(True)
        head.append(self.count)
        more = icon_button(pick_icon("view-list-symbolic", "system-users-symbolic"), tr('Freundesliste'), ["flat", "hdr-btn"])
        more.connect("clicked", lambda *_: self.win.navigate("friends"))
        head.append(more)
        inner.append(head)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(self.list_box)
        inner.append(scroll)
        self.set_child(inner)
        self._wide = False
        self.set_visible(False)
        ctl.listeners.append(self.update)
        self.win.width_probe.listeners.append(self.on_width)
        if self.win.width_probe.width:
            self.on_width(self.win.width_probe.width)

    def on_width(self, width: int) -> None:
        min_width = self.win.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        wide = fr.sidebar_fits(width, min_width, SIDEBAR_WIDTH)
        if wide != self._wide:
            self._wide = wide
            self.update()

    def wanted(self) -> bool:
        ctl = self.ctl
        snap = ctl.snapshot
        return bool(self._wide and self.win.backend.settings.show_friends_sidebar and ctl.available
                    and snap is not None and snap.friends)

    def update(self) -> None:
        show = self.wanted()
        self.set_visible(show)
        if not show:
            return
        _clear(self.list_box)
        snap = self.ctl.snapshot
        online, _off = fr.sort_friends(snap.friends, snap.presence, {}, "activity")
        self.count.set_text(str(len(online)))
        if not online:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=18, margin_start=14,
                          margin_end=14)
            box.append(label(tr('Keine Freunde online'), css=["row-title"]))
            box.append(label(tr('Hier siehst du deine Freunde, die momentan online oder in Spielen sind.'),
                             css=["dim", "caption"], wrap=True))
            self.list_box.append(box)
            return
        lst = _list()
        lst.connect("row-activated", lambda _l, r: self.ctl.open_chat(r.friend))
        for f in online:
            lst.append(FriendRow(self.ctl, f, compact=False, actions=False))
        self.list_box.append(lst)


def friends_sidebar_row(dlg) -> Adw.SwitchRow:
    """Einstellungen → Oberfläche → Anpassungen: Freunde-Seitenleiste wie Galaxy (showFriendsSidebar)."""
    row = Adw.SwitchRow(title=tr('Zeige eine Seitenleiste für Freunde, die online sind'),
                        subtitle=tr('Freunde-Seitenleiste – verfügbar bei Fenstern breiter als {0} px').format(
                            fr.SIDEBAR_MIN_WIDTH - 1),
                        active=bool(dlg.s.show_friends_sidebar))

    def changed(r, _p) -> None:
        dlg._save(show_friends_sidebar=r.get_active())
        win = getattr(Gio.Application.get_default(), "window", None)
        ctl = getattr(win, "friends", None)
        if ctl is not None and ctl.sidebar is not None:
            ctl.sidebar.update()
    row.connect("notify::active", changed)
    return row
