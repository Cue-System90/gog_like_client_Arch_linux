"""Freunde wie in GOG Galaxy 2.1: Abgleich im Hintergrund, Seitenleiste FREUNDE, Freundesliste, Freunde-Seitenleiste,
Chat, Widgets der Aktuellen Übersicht und Benachrichtigungen. Liegt als ``win.friends`` am Hauptfenster.

* Abruf alle 2 Minuten bei sichtbarem Fenster, alle 10 Minuten verborgen/im System-Tray, nie abgemeldet; nach Fehlern
  länger (``friends.next_delay``). Wird das Fenster wieder sichtbar und ist ein Abruf fällig, sofort.
* Seitenleiste FREUNDE: „Einladungen“ (nur mit offenen Einladungen, mit Zahl), „Freunde online“ (Zahl, Freundesliste),
  „Nachrichten“ (Chat).
* Aktionen: Annehmen, Ablehnen, Entfernen (mit Rückfrage), Nachricht senden, Profil ansehen (gog.com/u/<name> im
  eingebetteten Store), Freunde hinzufügen (Freundesseite von gog.com im Store).
* Widgets „Freunde spielen gerade“ und „Aktivität“ (die letzten 50 beobachteten Ereignisse).
* Benachrichtigungen: Freundschaftsanfrage, Freund geht online, Freund startet ein Spiel – je Typ einstellbar.

Daten ohne GTK: ``galaxy_linux/friends.py``, HTTP: ``api/friends.py``.
"""
from __future__ import annotations

from ..i18n import N_, tr

import logging
import time
from typing import Any

from .. import friends as fr
from .. import i18n, paths
from ..api.client import NotLoggedIn
from ..api.friends import Friend, Invitation, profile_path
from . import notification_center, store, ui_registry
from .chat_view import ChatPage
from .friends_view import FriendsPage, FriendsSidebar, avatar
from .util import GLib, Gtk, clamped_picture, label, relative_time, run_async

log = logging.getLogger(__name__)

START_DELAY = 6          # s nach dem Start bis zum ersten Abruf
ADD_FRIENDS_PATH = "/account/friends"
PLAYING_MAX = 12
ACTIVITY_SHOWN = 50

notification_center.KIND_ICONS.update({
    fr.KIND_INVITE: ("contact-new-symbolic", "system-users-symbolic"),
    fr.KIND_ONLINE: ("system-users-symbolic", "avatar-default-symbolic"),
    fr.KIND_GAME: ("input-gaming-symbolic", "applications-games-symbolic"),
})


def _lang_locale() -> str:
    return "de-DE" if i18n.current() == "de" else "en-US"


class FriendsController:
    def __init__(self, win: Any) -> None:
        self.win = win
        self.api = win.backend.api
        self.service = fr.FriendsService(self.api, paths.FRIENDS_ACTIVITY_FILE, paths.FRIENDS_PROFILES_FILE)
        self.listeners: list = []
        self.demo = False               # unsichtbarer Oberflächentest: eingespielte Daten, kein Netz
        self._timer = 0
        self._busy = False
        self._force_again = False
        self._pending: set[str] = set()  # laufende Aktionen je Benutzer-ID
        self._recent_sig: tuple = ()
        self.page = FriendsPage(self)
        win.add_page("friends", self.page)
        self.chat = ChatPage(self)
        win.add_page("messages", self.chat)
        self.sidebar = FriendsSidebar(self)
        win.body.append(self.sidebar)
        win.connect("notify::visible", lambda *_: self._visibility_changed())
        if win.find_property("suspended") is not None:                                  # minimiert (GTK ≥ 4.12)
            win.connect("notify::suspended", lambda *_: self._visibility_changed())
        self._update_nav()
        if self.api.logged_in:
            self._schedule(START_DELAY)

    # ------------------------------------------------------------------ Zustand
    @property
    def available(self) -> bool:
        return self.demo or bool(self.api.logged_in)

    @property
    def snapshot(self) -> fr.Snapshot | None:
        return self.service.snapshot

    @property
    def error(self) -> str:
        return self.service.last_error

    def last_seen(self, uid: str) -> float:
        return float(self.service.activity.last_seen.get(uid, 0) or 0)

    def activity_last_seen(self) -> dict[str, float]:
        return dict(self.service.activity.last_seen)

    def is_busy(self, uid: str) -> bool:
        return uid in self._pending

    def window_visible(self) -> bool:
        w = self.win
        suspended = bool(w.is_suspended()) if hasattr(w, "is_suspended") else False
        return bool(w.get_visible() and not suspended)

    def notify(self) -> None:
        """Zähler, Freundesliste, Seitenleiste und Widgets auf den neuen Stand."""
        self._update_nav()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # pragma: no cover
                log.exception("Freunde-Anzeige fehlgeschlagen")
        self._refresh_overview()

    def _update_nav(self) -> None:
        nav = self.win.nav
        snap = self.snapshot if self.available else None
        online = len(snap.online()) if snap else 0
        invs = len(snap.invitations) if snap else 0
        if "friends" in nav:
            nav["friends"].set_badge(str(online) if snap is not None else "")
        if "requests" in nav:
            nav["requests"].set_badge(str(invs) if invs else "", accent=True)
            nav["requests"].set_visible(invs > 0)

    def _refresh_overview(self) -> None:
        """Aktuelle Übersicht nur neu aufbauen, wenn sich bei „Freunde spielen gerade“/„Aktivität“ etwas geändert hat."""
        snap = self.snapshot
        sig = (tuple(sorted((f.user_id, p.game_id) for f, p in snap.playing())) if snap else (),
               len(self.service.activity.events()), self.available)
        if sig == self._recent_sig:
            return
        self._recent_sig = sig
        win = self.win
        if win.view == "overview" and win.stack.get_visible_child_name() == "overview":
            y = win.overview.get_vadjustment().get_value()
            win.overview.rebuild()
            GLib.idle_add(lambda: (win.overview.get_vadjustment().set_value(y), False)[1])

    # ------------------------------------------------------------------ Takt
    def _schedule(self, delay: float | None) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if delay is None or self.demo:
            return
        self._timer = GLib.timeout_add(max(0, int(delay * 1000)), self._fire)

    def _fire(self) -> bool:
        self._timer = 0
        self.refresh()
        return False

    def _next(self) -> None:
        self._schedule(fr.next_delay(self.window_visible(), bool(self.api.logged_in), self.service.failures))

    def _visibility_changed(self) -> None:
        if self.demo or not self.api.logged_in or self._busy:
            return
        if self.service.last_attempt <= 0 and self._timer:
            return                         # erster Abruf ist schon geplant (kurz nach dem Start)
        due = fr.due_in(self.service.last_attempt, time.time(), self.window_visible(), True, self.service.failures)
        if due is not None:
            self._schedule(due)

    def on_view_shown(self) -> None:
        """Freundesliste geöffnet: ist der letzte Stand älter als der Takt, gleich nachholen."""
        self._visibility_changed()

    def refresh(self, force: bool = False) -> None:
        if self.demo:
            return
        if not self.api.logged_in:
            self._schedule(None)
            self.notify()
            return
        if self._busy:
            self._force_again = self._force_again or force
            return
        self._busy = True
        known = {e.id: e.title for e in self.win.backend.library}
        lang = _lang_locale()

        def done(snap: fr.Snapshot) -> None:
            self._busy = False
            if not self.api.logged_in:
                return
            owned = {e.id for e in self.win.backend.library}
            for n in self.service.apply(snap, owned):
                self.win.notifications.post(n.kind, n.title, n.body, action=n.action, product_id=n.product_id, key=n.key)
            self.notify()
            self._after_poll()

        def fail(exc: Exception) -> None:
            self._busy = False
            if isinstance(exc, NotLoggedIn):
                log.info("Freunde: nicht angemeldet – kein Abgleich")
                self.service.failed(exc)
                self._schedule(None)
                self.notify()
                return
            self.service.failed(exc)
            log.info("Freunde nicht abgeglichen (%d. Fehler in Folge): %s", self.service.failures, exc)
            self.notify()
            self._after_poll()
        run_async(lambda: self.service.fetch(force=force, known_titles=known, lang=lang), done, fail)

    def _after_poll(self) -> None:
        if self._force_again:
            self._force_again = False
            self._schedule(0)
        else:
            self._next()

    def on_login(self) -> None:
        self.service.clear()
        self.refresh(force=True)

    def on_logout(self) -> None:
        self._schedule(None)
        self.service.clear()
        self._pending.clear()
        self.chat.forget()
        self.notify()
        if self.win.view in ("friends", "messages"):
            self.page.rebuild()

    # ------------------------------------------------------------------ Aktionen
    def _action(self, uid: str, work, ok, what: str) -> None:
        if uid in self._pending:
            return
        self._pending.add(uid)
        self.notify()

        def done(_r) -> None:
            self._pending.discard(uid)
            ok()
            self.notify()

        def fail(exc: Exception) -> None:
            self._pending.discard(uid)
            log.warning("Freunde: %s fehlgeschlagen: %s", what, exc)
            self.win.toast(str(exc))
            self.notify()
        if self.demo:
            GLib.timeout_add(300, lambda: (done(None), False)[1])
            return
        run_async(work, done, fail)

    def accept(self, inv: Invitation) -> None:
        from ..api import friends as fa

        def ok() -> None:
            self.service.add_friend(inv)
            self.service.drop_invitation(inv.user_id)
            self.win.toast(tr('{0} ist jetzt dein Freund').format(inv.username or inv.user_id))
            self._schedule(2)                  # echte Liste und Präsenz kurz danach
        self._action(inv.user_id, lambda: fa.accept_invitation(self.api, inv.user_id), ok, "Annehmen")

    def decline(self, inv: Invitation) -> None:
        from ..api import friends as fa

        def ok() -> None:
            self.service.drop_invitation(inv.user_id)
            self.win.toast(tr('Einladung von {0} abgelehnt').format(inv.username or inv.user_id))
        self._action(inv.user_id, lambda: fa.decline_invitation(self.api, inv.user_id), ok, "Ablehnen")

    def confirm_remove(self, f: Friend) -> None:
        from .dialogs import confirm_destructive
        name = f.username or f.user_id
        confirm_destructive(self.win, tr('Aus Freundesliste entfernen'),
                            tr('Möchtest du {0} wirklich aus deiner Freundesliste entfernen?').format(name),
                            tr('Entfernen'), lambda: self.remove(f))

    def remove(self, f: Friend) -> None:
        from ..api import friends as fa

        def ok() -> None:
            self.service.drop_friend(f.user_id)
            self.win.toast(tr('{0} wurde aus der Freundesliste entfernt').format(f.username or f.user_id))
        self._action(f.user_id, lambda: fa.remove_friend(self.api, f.user_id), ok, "Entfernen")

    def open_chat(self, f: Friend | None = None) -> None:
        self.chat.open(f, force=f is not None)
        self.win.navigate("messages")

    def open_profile(self, f: Friend) -> None:
        if f.username:
            self.win.open_store(profile_path(f.username))
        else:
            self.win.toast(tr('Für diesen Freund ist kein Profilname bekannt.'))

    def add_friends(self) -> None:
        """Galaxys „Freunde hinzufügen“ sucht über eine eigene Schnittstelle – hier die Freundesseite von gog.com."""
        self.win.open_store(ADD_FRIENDS_PATH)

    # ------------------------------------------------------------------ Oberflächentest
    def inject(self, snap: fr.Snapshot, events: list[fr.ActivityEvent] | None = None,
               last_seen: dict[str, float] | None = None) -> list[fr.Notice]:
        """Daten ohne Netz einspielen (unsichtbarer Oberflächentest); Aktionen wirken nur lokal."""
        self.demo = True
        self._schedule(None)
        self.service.activity.path = None          # nichts in die Test-Home schreiben
        self.service.profiles.path = None
        out = self.service.apply(snap)
        if events:
            self.service.activity.record(events)
        if last_seen:
            self.service.activity.last_seen.update(last_seen)
        self.notify()
        return out

    # ------------------------------------------------------------------ Aktuelle Übersicht
    def playing(self) -> list[tuple[Friend, Any, str]]:
        snap = self.snapshot if self.available else None
        if snap is None:
            return []
        return [(f, p, snap.titles.get(p.game_id) or p.title) for f, p in snap.playing()]


# --------------------------------------------------------------------------- Widgets der Aktuellen Übersicht
def _ctl(page) -> FriendsController | None:
    return getattr(page.win, "friends", None)


def friends_playing(page):
    ctl = _ctl(page)
    if ctl is None:
        return None
    if not ctl.available:
        return label(tr('Melde dich an, um zu sehen, was deine Freunde spielen.'), css=["dim"], wrap=True)
    items = ctl.playing()
    if not items:
        return label(tr('Gerade spielt keiner deiner Freunde.') if ctl.snapshot is not None
                     else tr('Freunde werden abgeglichen …'), css=["dim"], wrap=True)
    b = page.backend
    cards = []
    for f, p, title in items[:PLAYING_MAX]:
        entry = b.entry(p.game_id)
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("discover-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("cover-card")
        frame, pic = clamped_picture(136, 191, ["cover"], placeholder=title or tr('Im Spiel'))
        box.append(frame)
        who = Gtk.Box(spacing=6)
        who.append(avatar(page.win, f.username, f.avatar, 20, fr.PLAYING))
        n = label(f.username, css=["cover-title"], ellipsize=True)
        n.set_max_width_chars(1)
        n.set_size_request(110, -1)
        who.append(n)
        box.append(who)
        t = label(title or tr('Im Spiel'), css=["dim", "caption"], ellipsize=True)
        t.set_size_request(136, -1)
        t.set_max_width_chars(1)
        box.append(t)
        btn.set_child(box)
        btn.set_tooltip_text(tr('{0} spielt {1}').format(f.username, title) if title else f.username)
        gid = p.game_id
        if entry is not None:
            page.win.load_cover(entry, pic)
            btn.connect("clicked", lambda *_a, g=gid: page.win.show_game(g))
        else:
            btn.connect("clicked", lambda *_a, g=gid: store.show_product(page.win, g))
        cards.append(btn)
    return page.cover_row(cards)


def friends_activity(page):
    ctl = _ctl(page)
    if ctl is None:
        return None
    if not ctl.available:
        return label(tr('Melde dich an, um die Aktivität deiner Freunde zu sehen.'), css=["dim"], wrap=True)
    events = ctl.service.activity.events()[:ACTIVITY_SHOWN]
    if not events:
        return label(tr('Noch keine Aktivität: galaxy-linux merkt sich hier, wann Freunde online kommen oder ein Spiel '
                        'starten (die letzten 50 Ereignisse).'), css=["dim"], wrap=True)
    lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lst.add_css_class("friend-list")
    for e in events:
        row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        box.append(avatar(page.win, e.username, e.avatar, 26))
        name = e.username or e.user_id
        if e.kind == "game":
            text = tr('{0} spielt jetzt „{1}“').format(name, e.title) if e.title else tr('{0} spielt jetzt').format(name)
        else:
            text = tr('{0} ist jetzt online').format(name)
        t = label(text, ellipsize=True)
        t.set_hexpand(True)
        box.append(t)
        box.append(label(relative_time(e.ts), css=["dim", "caption"]))
        row.set_child(box)
        lst.append(row)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=320)
    scroll.set_child(lst)
    return scroll


ui_registry.register_recent_widget("friends_playing", N_("Freunde spielen gerade"), friends_playing, order=50)
ui_registry.register_recent_widget("friends_activity", N_("Aktivität"), friends_activity, order=60)


def window_hook(win: Any) -> None:
    win.friends = FriendsController(win)


ui_registry.register_window_hook("friends", window_hook, order=40)
