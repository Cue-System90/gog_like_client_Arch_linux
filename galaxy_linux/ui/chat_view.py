"""„Nachrichten“ wie in GOG Galaxy 2.1: die Chat-Web-App von GOG in einer WebKit-Ansicht.

Galaxy bettet ``chat.gog.com/users/<ich>/chat?_version=2&isEmbedFrom=galaxy`` in ein eigenes Fenster ein (für eine
Unterhaltung mit ``second_participant_*``). Hier läuft dieselbe Adresse in der gemeinsamen Web-Sitzung von Login und
Store (``ui/web.py``); die erste Anfrage trägt das Zugriffstoken als ``Authorization: Bearer`` – ohne antwortet
chat.gog.com mit 401. Ob die App danach ihre eigenen Anfragen anmeldet, ist außerhalb von Galaxy nicht bestätigt.
Antwortet sie mit 401/403, bleibt sie leer oder fehlt die Verbindung, erscheint ein klarer Hinweis mit „GOG.COM
ÖFFNEN“ (Store-Ansicht, dort ist man über die Cookies angemeldet) und „Erneut versuchen“.

Das Token geht nur an chat.gog.com: Navigation bleibt auf GOG-Hosts, fremde Links öffnet nach einem Klick der
System-Browser, alles andere wird verworfen.
"""
from __future__ import annotations

from ..i18n import tr

import logging
from urllib.parse import urlsplit

from ..api.client import NotLoggedIn
from ..api.friends import Friend, chat_url
from . import web
from .util import Adw, GLib, Gtk, icon_button, label, open_uri, pick_icon, run_async

log = logging.getLogger(__name__)

CHAT_HOST = "chat.gog.com"
BLANK_CHECK = 6           # s nach dem Laden: ist die App leer geblieben?
BLANK_JS = ("(function(){var b=document.body;if(!b)return 0;var t=(b.innerText||'').trim().length;"
            "return t+document.querySelectorAll('img,canvas,input,textarea,button,svg').length;})()")


def is_gog_host(url: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return host == "gog.com" or host.endswith(".gog.com")


class ChatPage(Gtk.Box):
    """Seite „messages“ im Inhaltsbereich."""

    def __init__(self, ctl) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("store-page")
        self.ctl, self.win = ctl, ctl.win
        self.view = None
        self._loaded_url = ""
        self._pending: Friend | None = None
        self._blank_timer = 0
        self.state = "idle"           # idle | loading | web | failed
        self.reason = ""
        bar = Gtk.Box(spacing=2)
        bar.add_css_class("store-bar")
        bar.append(label(tr('NACHRICHTEN'), css=["sec-hdr"]))
        sp = Gtk.Box(hexpand=True)
        bar.append(sp)
        self.reload_btn = icon_button(pick_icon("view-refresh-symbolic"), tr('Neu laden'), ["flat", "hdr-btn"])
        self.reload_btn.set_valign(Gtk.Align.CENTER)
        self.reload_btn.connect("clicked", lambda *_: self.open(self._pending, force=True))
        bar.append(self.reload_btn)
        self.append(bar)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.status = Adw.StatusPage(icon_name=pick_icon("mail-unread-symbolic", "chat-symbolic"))
        sb = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.gog_btn = Gtk.Button(label=tr('GOG.COM ÖFFNEN'))
        self.gog_btn.add_css_class("btn-play")
        self.gog_btn.connect("clicked", lambda *_: self.win.open_store("/"))
        sb.append(self.gog_btn)
        self.retry_btn = Gtk.Button(label=tr('ERNEUT VERSUCHEN'))
        self.retry_btn.add_css_class("btn-secondary")
        self.retry_btn.connect("clicked", lambda *_: self._retry())
        sb.append(self.retry_btn)
        self.status.set_child(sb)
        self.stack.add_named(self.status, "message")
        self.append(self.stack)

    # ------------------------------------------------------------------ Öffnen
    def on_show(self) -> None:
        if self.state in ("idle", "failed") and not self.ctl.demo:
            self.open(self._pending)
        elif self.ctl.demo and self.state == "idle":
            self.show_failure("auth", 401)

    def open(self, friend: Friend | None = None, force: bool = False) -> None:
        """Chat laden; mit ``friend`` die Unterhaltung mit ihm (wie Galaxys openRoomForUser)."""
        self._pending = friend
        api = self._api()
        if self.ctl.demo:
            self.show_failure("auth", 401)
            return
        if not api.logged_in:
            self.show_failure("logged_out")
            return
        WebKit = web.webkit()
        if WebKit is None:
            self.show_failure("webkit")
            return
        url = chat_url(api.user_id, friend)
        if not force and self.state in ("web", "loading") and url == self._loaded_url:
            return
        if self.view is None:
            self._build_view(WebKit)
        self.state = "loading"
        self._loaded_url = url

        def go(token: str) -> None:
            req = WebKit.URIRequest.new(url)
            headers = req.get_http_headers()
            if headers is not None:
                headers.append("Authorization", f"Bearer {token}")
            log.info("Chat wird geladen (chat.gog.com, mit Anmeldung%s)", ", Unterhaltung mit einem Freund" if friend else "")
            self.stack.set_visible_child_name("web")
            self.view.load_request(req)

        def fail(exc: Exception) -> None:
            self.show_failure("logged_out" if isinstance(exc, NotLoggedIn) else "network", detail=str(exc))
        run_async(api.access_token, go, fail)

    def _api(self):
        return self.win.backend.api

    def _build_view(self, WebKit) -> None:
        self.WebKit = WebKit
        self.view = web.new_view()
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("create", self._on_create)
        self.stack.add_named(self.view, "web")

    def forget(self) -> None:
        """Abmelden: Ansicht leeren, nächstes Öffnen lädt neu."""
        if self._blank_timer:
            GLib.source_remove(self._blank_timer)
            self._blank_timer = 0
        if self.view is not None:
            self.view.load_uri("about:blank")
        self._loaded_url = ""
        self._pending = None
        self.state = "idle"

    # ------------------------------------------------------------------ Hinweis statt Chat
    def show_failure(self, reason: str, status: int = 0, detail: str = "") -> None:
        self.state = "failed"
        self.reason = reason
        if self._blank_timer:
            GLib.source_remove(self._blank_timer)
            self._blank_timer = 0
        gog_hint = tr('Nachrichten erreichst du auf gog.com – dort bist du über den eingebetteten Store angemeldet.')
        if reason == "auth":
            title = tr('Der Chat hat die Anmeldung nicht angenommen')
            text = tr('Die Chat-Anwendung von GOG antwortete mit HTTP {0}. GOG Galaxy betreibt den Chat in einem eigenen, '
                      'angemeldeten Fenster; ob er sich außerhalb von Galaxy anmelden lässt, ist nicht bestätigt.').format(status)
        elif reason == "blank":
            title = tr('Der Chat blieb leer')
            text = tr('Die Chat-Anwendung von GOG hat nichts angezeigt – vermutlich erkennt sie die Anmeldung '
                      'außerhalb von GOG Galaxy nicht.')
        elif reason == "webkit":
            title = tr('Eingebetteter Chat nicht verfügbar')
            text = tr('Für den Chat fehlt das Paket webkitgtk-6.0.')
        elif reason == "logged_out":
            title = tr('Nicht angemeldet')
            text = tr('Melde dich bei GOG an, um Nachrichten zu lesen und zu schreiben.')
            gog_hint = ""
        else:
            title = tr('Der Chat konnte nicht geladen werden')
            text = tr('Keine Verbindung zu chat.gog.com: {0}').format(detail or tr('unbekannter Fehler'))
        self.status.set_title(title)
        self.status.set_description(f"{text}\n\n{gog_hint}".strip())
        self.gog_btn.set_visible(reason != "logged_out")
        self.retry_btn.set_label(tr('BEI GOG ANMELDEN') if reason == "logged_out" else tr('ERNEUT VERSUCHEN'))
        self.stack.set_visible_child_name("message")
        log.info("Chat: Hinweis statt Chat (%s%s)", reason, f", HTTP {status}" if status else "")

    def _retry(self) -> None:
        if self.reason == "logged_out":
            self.win.do_login()
        else:
            self.open(self._pending, force=True)

    # ------------------------------------------------------------------ WebKit
    def _on_decide_policy(self, view, decision, dtype) -> bool:
        WebKit = self.WebKit
        if dtype == WebKit.PolicyDecisionType.RESPONSE:
            if decision.is_main_frame_main_resource():
                resp = decision.get_response()
                status = resp.get_status_code() if resp is not None else 0
                if status in (401, 403) and CHAT_HOST in (resp.get_uri() or ""):
                    decision.ignore()
                    GLib.idle_add(lambda: (self.show_failure("auth", status), False)[1])
                    return True
            return False
        if dtype in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            req = action.get_request()
            uri = req.get_uri() if req is not None else ""
            if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION and (is_gog_host(uri) or uri == "about:blank"):
                return False
            decision.ignore()
            if action.is_user_gesture():
                if is_gog_host(uri):
                    self.win.open_store(uri)
                elif uri.startswith(("https://", "http://")):
                    open_uri(uri)
            return True
        return False

    def _on_create(self, view, action):
        req = action.get_request()
        uri = req.get_uri() if req is not None else ""
        if action.is_user_gesture() and uri.startswith(("https://", "http://")):
            if is_gog_host(uri):
                self.win.open_store(uri)
            else:
                open_uri(uri)
        return None

    def _on_load_changed(self, view, event) -> None:
        if event != self.WebKit.LoadEvent.FINISHED or self.state != "loading":
            return
        uri = view.get_uri() or ""
        if uri == "about:blank":
            return
        self.state = "web"
        if self._blank_timer:
            GLib.source_remove(self._blank_timer)
        self._blank_timer = GLib.timeout_add_seconds(BLANK_CHECK, self._check_blank)

    def _check_blank(self) -> bool:
        self._blank_timer = 0
        if self.view is None or self.state != "web":
            return False

        def done(view, res) -> None:
            try:
                n = view.evaluate_javascript_finish(res).to_int32()
            except Exception as exc:
                log.debug("Chat-Prüfung nicht möglich: %s", exc)
                return
            if n <= 0 and self.state == "web":
                self.show_failure("blank")
        self.view.evaluate_javascript(BLANK_JS, -1, None, None, None, done)
        return False

    def _on_load_failed(self, view, _event, failing_uri: str, error) -> bool:
        WebKit = self.WebKit
        if error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or \
                error.matches(WebKit.policy_error_quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False
        if self.state == "failed":
            return True
        self.show_failure("network", detail=error.message)
        return True
