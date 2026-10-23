"""Eingebetteter GOG-Store wie im Original: gog.com-Seiten in einer WebKit-Ansicht im Inhaltsbereich.

Galaxy zeigt den Store nicht nativ, sondern lädt die Website in einen Web-Rahmen; die Seitenleiste „GOG STORE“
springt auf feste Pfade (Startseite, Alle Spiele, Einkaufen, Wunschliste, Warenkorb, Bisherige Bestellungen).
Hier genauso, mit der gemeinsamen Web-Sitzung des Logins (``ui/web.py``). Kein eigener User-Agent, keine
Partner-Links. Ein Kaufabschluss (``/checkout/<id>/complete``) stößt den Bibliotheksabgleich an.
Ohne ``webkitgtk-6.0`` öffnen dieselben Einträge den System-Browser. Die reinen Regeln (Adressen, Erkennung,
Einordnung von Links) stehen GTK-frei in ``store_links.py``.
"""
from __future__ import annotations

from ..i18n import tr

import html
import json
import logging
import time
from pathlib import Path

from .. import i18n
from ..api import storefront
from ..api.client import NotLoggedIn
from ..store_links import (STORE_SECTIONS, checkout_order_id, classify_navigation, classify_new_window,
                           galaxy_link_product, is_store_url, parse_page_message, product_fallback,
                           rememberable_url, response_is_download, section_for_path, store_lang, store_locale,
                           store_login_url, store_url, unique_download_path)
from . import web
from .util import Adw, Gio, GLib, Gtk, Pango, icon_button, label, open_uri, pick_icon, run_async

log = logging.getLogger(__name__)

MSG_HANDLER = "galaxyLinux"
SCRIPT_HOSTS = ["https://www.gog.com/*", "https://gog.com/*", "https://embed.gog.com/*"]
WISHLIST_INTERVAL = 600       # Sekunden zwischen Abgleichen des Wunschlisten-Zählers
WISHLIST_MIN_GAP = 20         # Mindestabstand bei Abgleichen nach Seitenaufrufen
SAVE_DELAY = 3                # Sekunden, bevor die zuletzt besuchte Store-Seite gespeichert wird
CRASH_GAP = 10                # Web-Prozess höchstens alle 10 s automatisch neu laden

# Läuft am Dokumentanfang jeder gog.com-Seite: meldet SPA-Navigation (pushState/replaceState) und ob die Website
# angemeldet ist. Nur Meldungen an den Client, keine Änderungen an der Seite.
HOOK_JS = r"""
(function () {
  if (window.__galaxyLinuxHook) return;
  window.__galaxyLinuxHook = true;
  var h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.galaxyLinux;
  if (!h) return;
  function send(o) { try { h.postMessage(JSON.stringify(o)); } catch (e) {} }
  function nav() { send({type: 'nav', url: String(location.href)}); }
  ['pushState', 'replaceState'].forEach(function (n) {
    var orig = history[n];
    if (typeof orig !== 'function') return;
    history[n] = function () { var r = orig.apply(this, arguments); nav(); return r; };
  });
  window.addEventListener('popstate', nav);
  function user() {
    if (!/^(www|embed)\.gog\.com$/.test(location.hostname)) return;
    fetch('/userData.json', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && typeof d.isLoggedIn === 'boolean') send({type: 'user', loggedIn: d.isLoggedIn}); })
      .catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', user); else user();
})();
"""


def _lang() -> str:
    return store_lang(i18n.current())


def _main_window():
    app = Gio.Application.get_default()
    return getattr(app, "window", None)


def downloads_dir() -> Path:
    """Download-Ordner des Nutzers (XDG), sonst ~/Downloads."""
    d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    path = Path(d) if d else Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------------------- Seitenleiste
class StoreSidebar:
    """Gruppe „GOG STORE“ in der Seitenleiste; die Einträge landen in ``win.nav`` als ``store:<schlüssel>``."""

    def __init__(self, win, nav_item_cls) -> None:
        self.win = win
        self._wl_at = 0.0
        self._wl_busy = False
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.append(label(tr('GOG STORE'), css=["nav-section"]))
        entries = (
            ("home", tr('Startseite'), pick_icon("go-home-symbolic", "user-home-symbolic")),
            ("games", tr('Alle Spiele'), pick_icon("system-software-install-symbolic", "applications-games-symbolic",
                                                    "view-app-grid-symbolic")),
            ("deals", tr('Einkaufen'), pick_icon("tag-symbolic", "emblem-favorite-symbolic")),
            ("wishlist", tr('Wunschliste'), pick_icon("starred-symbolic", "emblem-favorite-symbolic")),
            ("cart", tr('Warenkorb'), pick_icon("shopping-cart-symbolic", "amarok_cart_view-symbolic", "cart-symbolic",
                                                "view-list-symbolic")),
            ("orders", tr('Bisherige Bestellungen'), pick_icon("view-list-ordered-symbolic", "view-list-symbolic",
                                                                "document-open-recent-symbolic")),
        )
        self.items = {}
        for key, title, icon in entries:
            it = nav_item_cls(title, icon)
            it.connect("clicked", lambda _b, k=key: self.win.open_store(STORE_SECTIONS[k]))
            self.items[key] = it
            win.nav[f"store:{key}"] = it
            self.widget.append(it)
        self.apply_visibility()
        GLib.timeout_add_seconds(4, lambda: (self.refresh_wishlist(force=True), False)[1])
        GLib.timeout_add_seconds(WISHLIST_INTERVAL, self._tick)

    def apply_visibility(self) -> None:
        """„Bevorzugter Store“: GOG.COM = sichtbar, „In der Seitenleiste ausblenden“ = Gruppe weg."""
        visible = bool(self.win.backend.settings.store_visible)
        self.widget.set_visible(visible)
        if visible:
            self.refresh_wishlist()

    def _tick(self) -> bool:
        self.refresh_wishlist(force=True)
        return True

    def refresh_wishlist(self, force: bool = False) -> None:
        """Zähler der Wunschliste über ``embed.gog.com/user/wishlist.json`` (nur angemeldet, nur sichtbar)."""
        api = self.win.backend.api
        if not api.logged_in:
            self.set_wishlist_count(0)
            return
        if self._wl_busy or not self.widget.get_visible():
            return
        if not force and time.monotonic() - self._wl_at < WISHLIST_MIN_GAP:
            return
        self._wl_busy = True
        self._wl_at = time.monotonic()

        def done(n: int) -> None:
            self._wl_busy = False
            self.set_wishlist_count(n)

        def fail(exc: Exception) -> None:
            self._wl_busy = False
            log.debug("Wunschliste nicht abrufbar: %s", exc)
            if isinstance(exc, NotLoggedIn):
                self.set_wishlist_count(0)
        run_async(lambda: storefront.wishlist_count(api), done, fail)

    def set_wishlist_count(self, n: int) -> None:
        self.items["wishlist"].set_badge(str(n) if n else "")


def build_sidebar_group(win, nav_item_cls) -> Gtk.Widget:
    """Seitenleisten-Gruppe anlegen (``win.store_sidebar``) und ihr Widget liefern."""
    win.store_sidebar = StoreSidebar(win, nav_item_cls)
    return win.store_sidebar.widget


def preferred_store_row(dlg) -> Adw.ComboRow:
    """Einstellungen → Oberfläche → Anpassungen: „Bevorzugter Store“ wie im Original."""
    row = Adw.ComboRow(title=tr('Bevorzugter Store'),
                       model=Gtk.StringList.new([tr('GOG.COM-Store'), tr('In Seitenleiste ausblenden')]))
    row.set_selected(0 if dlg.s.store_visible else 1)

    def changed(*_a) -> None:
        dlg._save(store_visible=row.get_selected() == 0)
        win = _main_window()
        if win is not None and getattr(win, "store_sidebar", None) is not None:
            win.store_sidebar.apply_visibility()
    row.connect("notify::selected", changed)
    return row


# ----------------------------------------------------------------------------- Öffnen
def ensure_page(win):
    """Store-Seite einmalig anlegen und in den Inhalts-Stack hängen; None ohne WebKitGTK 6.0."""
    page = getattr(win, "store_page", None)
    if page is not None:
        return page
    WebKit = web.webkit()
    if WebKit is None:
        return None
    try:
        page = StorePage(win, WebKit)
    except Exception:
        log.exception("Store-Ansicht konnte nicht angelegt werden")
        return None
    win.stack.add_named(page, "store")
    win.store_page = page
    return page


def open_store(win, path: str | None = None) -> None:
    """Store-Seite öffnen (Pfad wie "/games" oder volle gog.com-Adresse; leer = zuletzt besuchte Seite)."""
    lang = _lang()
    if not path:
        last = win.backend.settings.store_last_url
        path = last if rememberable_url(last) else "/"
    url = store_url(path, lang)
    if not is_store_url(url):              # fremde Seiten nie im Store-Fenster
        open_uri(url)
        return
    page = ensure_page(win)
    if page is None:
        if not getattr(win, "_store_fallback_noted", False):
            win._store_fallback_noted = True
            win.toast(tr('Eingebetteter Store nicht verfügbar (Paket webkitgtk-6.0 fehlt) – die Seite wurde im Browser geöffnet.'))
        open_uri(url)
        return
    page.load(url)
    view = f"store:{section_for_path(url) or 'page'}"
    if win.view != view or win.stack.get_visible_child_name() != "store":
        win.navigate(view)
    sidebar = getattr(win, "store_sidebar", None)
    if sidebar is not None:
        sidebar.refresh_wishlist()


def show_product(win, product_id: str, meta=None, entry=None) -> None:
    """„Im Store anzeigen“: Produktseite über api.gog.com/v2/games (``_links.store.href``), sonst Produktkarte/Slug."""
    fallback = product_fallback(getattr(meta, "gog_url", "") or "", getattr(entry, "gog_url", "") or "",
                                getattr(entry, "slug", "") or "")
    locale = store_locale(_lang())
    run_async(lambda: storefront.product_store_url(win.backend.api, product_id, locale),
              lambda href: win.open_store(href or fallback),
              lambda _e: win.open_store(fallback))


def on_login(win) -> None:
    """Nach dem Client-Login: Zähler abgleichen, eine offene Store-Seite neu laden (gemeinsame Cookies)."""
    sidebar = getattr(win, "store_sidebar", None)
    if sidebar is not None:
        sidebar.refresh_wishlist(force=True)
    page = getattr(win, "store_page", None)
    if page is not None:
        page.view.reload()


def on_logout(win) -> None:
    """Ausloggen: Cookies der Web-Sitzung löschen (Store ebenfalls abmelden), Zähler und letzte Seite vergessen."""
    sidebar = getattr(win, "store_sidebar", None)
    if sidebar is not None:
        sidebar.set_wishlist_count(0)
    st = win.backend.settings
    if st.store_last_url:
        st.store_last_url = ""
        st.save()
    page = getattr(win, "store_page", None)
    if page is not None:
        page.forget()
    web.clear_data((lambda: page.view.load_uri(store_url("/", _lang()))) if page is not None else None)


# ----------------------------------------------------------------------------- Store-Seite
class StorePage(Gtk.Box):
    """Schmale Leiste (Zurück, Vor, Neu laden, Startseite, Adresse) über der Web-Ansicht – kein weiteres Browser-Zubehör."""

    def __init__(self, win, WebKit) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("store-page")
        self.win = win
        self.WebKit = WebKit
        self._orders: set[str] = set()
        self._popups: set[StorePopup] = set()
        self._failed_downloads: set[str] = set()
        self._target = ""             # angeforderte Adresse, bis die Web-Ansicht sie übernimmt
        self._save_timer = 0
        self._crash_at = 0.0
        self.append(self._build_bar())
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("store-progress")
        self.progress.set_visible(False)
        self.append(self.progress)
        self.banner = Adw.Banner(title=tr('Du bist im Store nicht angemeldet.'), button_label=tr('Im Store anmelden'))
        self.banner.connect("button-clicked", lambda *_: self.login())
        self.append(self.banner)

        ucm = WebKit.UserContentManager()
        if ucm.register_script_message_handler(MSG_HANDLER, None):
            ucm.connect(f"script-message-received::{MSG_HANDLER}", self._on_message)
        ucm.add_script(WebKit.UserScript.new(HOOK_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                             WebKit.UserScriptInjectionTime.START, SCRIPT_HOSTS, None))
        self.view = web.new_view(user_content_manager=ucm)
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        self.view.connect("notify::uri", lambda v, _p: self._on_uri(v.get_uri() or ""))
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("notify::estimated-load-progress",
                          lambda v, _p: self.progress.set_fraction(v.get_estimated_load_progress()))
        self.view.connect("notify::is-loading", lambda *_: self._update_buttons())
        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("create", self._on_create)
        self.view.connect("web-process-terminated", self._on_crash)
        self.append(self.view)
        web.add_download_handler(self._on_download)

    # --- Leiste
    def _build_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=2)
        bar.add_css_class("store-bar")
        self.back_btn = icon_button(pick_icon("go-previous-symbolic"), tr('Zurück'), ["flat", "hdr-btn"])
        self.back_btn.connect("clicked", lambda *_: self.view.go_back())
        self.fwd_btn = icon_button(pick_icon("go-next-symbolic"), tr('Vor'), ["flat", "hdr-btn"])
        self.fwd_btn.connect("clicked", lambda *_: self.view.go_forward())
        self.reload_btn = icon_button(pick_icon("view-refresh-symbolic"), tr('Neu laden'), ["flat", "hdr-btn"])
        self.reload_btn.connect("clicked", lambda *_: self.view.stop_loading() if self.view.is_loading() else self.view.reload())
        home = icon_button(pick_icon("go-home-symbolic", "user-home-symbolic"), tr('Store-Startseite'), ["flat", "hdr-btn"])
        home.connect("clicked", lambda *_: self.win.open_store("/"))
        for b in (self.back_btn, self.fwd_btn, self.reload_btn, home):
            b.set_valign(Gtk.Align.CENTER)
            bar.append(b)
        self.url_lbl = label("", css=["store-url"], selectable=True)
        self.url_lbl.set_focusable(False)          # nicht beim Öffnen vorauswählen
        self.url_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.url_lbl.set_hexpand(True)
        self.url_lbl.set_margin_start(8)
        bar.append(self.url_lbl)
        self.back_btn.set_sensitive(False)
        self.fwd_btn.set_sensitive(False)
        return bar

    def _update_buttons(self) -> None:
        self.back_btn.set_sensitive(self.view.can_go_back())
        self.fwd_btn.set_sensitive(self.view.can_go_forward())
        loading = self.view.is_loading()
        self.reload_btn.set_icon_name(pick_icon("process-stop-symbolic") if loading else pick_icon("view-refresh-symbolic"))
        self.reload_btn.set_tooltip_text(tr('Laden abbrechen') if loading else tr('Neu laden'))

    # --- Navigation
    def load(self, url: str) -> None:
        self._target = url
        self.view.load_uri(url)

    def current_uri(self) -> str:
        """Gezeigte Adresse – oder die gerade angeforderte, solange die Web-Ansicht sie noch nicht übernommen hat."""
        return self._target or self.view.get_uri() or ""

    def login(self) -> None:
        """Banner „Im Store anmelden“: Anmeldedialog der Website selbst (setzt die Cookies der gemeinsamen Sitzung)."""
        self.banner.set_revealed(False)
        current = self.view.get_uri() or ""
        target = store_login_url(current, _lang())
        if current.split("#", 1)[0] == target.split("#", 1)[0]:
            # gleiche Seite mit neuem Fragment: erst Adresse setzen, dann neu laden, damit die Seite es auswertet
            self.view.evaluate_javascript(f"location.href = {json.dumps(target)}; location.reload();",
                                          -1, None, None, None, None)
        else:
            self.load(target)

    def forget(self) -> None:
        """Nach dem Ausloggen: erkannte Bestellungen und Banner zurücksetzen."""
        self._orders.clear()
        self.banner.set_revealed(False)

    def sync_nav(self) -> None:
        """Markierung in der Seitenleiste an die gezeigte Seite anpassen (ohne neuen Verlaufseintrag)."""
        win = self.win
        if not win.view.startswith("store:"):
            return
        view = f"store:{section_for_path(self.current_uri()) or 'page'}"
        if view == win.view:
            return
        win.view = view
        for k, it in win.nav.items():
            it.set_selected(k == view)
        if 0 <= win.hist_idx < len(win.history) and win.history[win.hist_idx][0].startswith("store:"):
            win.history[win.hist_idx] = (view, None)

    def _on_uri(self, uri: str) -> None:
        if uri and uri != "about:blank":
            self._target = ""
        self.url_lbl.set_text(uri)
        self.url_lbl.set_tooltip_text(uri)
        self._update_buttons()
        self.sync_nav()
        self.check_purchase(uri)

    def check_purchase(self, uri: str) -> None:
        """Kaufabschluss erkannt → Bibliothek einmal pro Bestellung abgleichen."""
        oid = checkout_order_id(uri)
        if not oid or oid in self._orders:
            return
        self._orders.add(oid)
        log.info("Kaufabschluss im Store erkannt – Bibliotheksabgleich")
        if self.win.backend.api.logged_in:
            self.win.toast(tr('Kauf abgeschlossen – die Bibliothek wird aktualisiert.'))
            self.win.request_library_refresh("purchase", force=True)
        sidebar = getattr(self.win, "store_sidebar", None)
        if sidebar is not None:
            sidebar.refresh_wishlist(force=True)

    def _on_load_changed(self, view, event) -> None:
        WebKit = self.WebKit
        if event == WebKit.LoadEvent.STARTED:
            self.progress.set_fraction(0.05)
            self.progress.set_visible(True)
        elif event == WebKit.LoadEvent.COMMITTED:
            self._on_uri(view.get_uri() or "")
        elif event == WebKit.LoadEvent.FINISHED:
            self.progress.set_visible(False)
            uri = view.get_uri() or ""
            self._on_uri(uri)
            self._remember(uri)
            sidebar = getattr(self.win, "store_sidebar", None)
            if sidebar is not None:
                sidebar.refresh_wishlist(force=section_for_path(uri) == "wishlist")
        self._update_buttons()

    def _remember(self, uri: str) -> None:
        """Zuletzt besuchte Store-Seite merken (gesammelt nach kurzer Pause, nicht bei jedem Seitenaufruf)."""
        st = self.win.backend.settings
        if not rememberable_url(uri) or st.store_last_url == uri:
            return
        st.store_last_url = uri
        if not self._save_timer:
            self._save_timer = GLib.timeout_add_seconds(SAVE_DELAY, self._save_settings)

    def _save_settings(self) -> bool:
        self._save_timer = 0
        try:
            self.win.backend.settings.save()
        except OSError as exc:
            log.warning("Zuletzt besuchte Store-Seite nicht gespeichert: %s", exc)
        return False

    def _on_load_failed(self, view, _event, failing_uri: str, error) -> bool:
        WebKit = self.WebKit
        self._target = ""
        if error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or \
                error.matches(WebKit.policy_error_quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False          # abgebrochen oder als Download weitergereicht
        log.warning("Store-Seite nicht geladen: %s", error.message)
        self.progress.set_visible(False)
        retry = failing_uri if is_store_url(failing_uri) else store_url("/", _lang())
        view.load_alternate_html(_error_html(retry, error.message), failing_uri, None)
        return True

    def _on_crash(self, view, reason) -> None:
        if reason == self.WebKit.WebProcessTerminationReason.TERMINATED_BY_API:
            return
        now = time.monotonic()
        if now - self._crash_at < CRASH_GAP:
            log.warning("Web-Prozess des Stores erneut beendet (%s) – kein automatisches Neuladen", reason)
            return
        self._crash_at = now
        log.warning("Web-Prozess des Stores beendet (%s) – Seite wird neu geladen", reason)
        self.win.toast(tr('Die Store-Ansicht wurde neu geladen.'))
        view.reload()

    def _on_decide_policy(self, view, decision, dtype) -> bool:
        WebKit = self.WebKit
        if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            req = decision.get_navigation_action().get_request()
            uri = req.get_uri() if req is not None else ""
            action = decision.get_navigation_action()
            kind = classify_navigation(uri)
            if kind == "allow":
                return False
            decision.ignore()
            if kind == "galaxy":
                self._galaxy_link(uri)
            elif kind == "browser" and action.is_user_gesture():      # nie ohne Klick an das System
                open_uri(uri)
            else:
                log.info("Store: Navigation zu %s verworfen", uri.split(":", 1)[0] + ":…")
            return True
        if dtype == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            # Links mit target=_blank schon hier einordnen; nur echte Popups (Zahlung) laufen weiter zu „create“
            action = decision.get_navigation_action()
            req = action.get_request()
            uri = req.get_uri() if req is not None else ""
            kind = classify_new_window(uri, action.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED)
            if kind == "popup":
                return False
            decision.ignore()
            if kind != "browser" or action.is_user_gesture():
                self._open_elsewhere(kind, uri)
            return True
        if dtype == WebKit.PolicyDecisionType.RESPONSE:
            if not decision.is_main_frame_main_resource():
                return False
            disposition = ""
            try:
                ok, value, _params = decision.get_response().get_http_headers().get_content_disposition()
                disposition = value if ok and value else ""
            except Exception:
                pass
            if response_is_download(decision.is_mime_type_supported(), disposition):
                decision.download()          # Offline-Installer, Goodies → Download-Ordner
                return True
        return False

    def _galaxy_link(self, uri: str) -> None:
        pid = galaxy_link_product(uri)
        if pid:
            self.win.show_game(pid)

    def _on_create(self, view, action):
        """Neues Fenster: Zahlungs-Popups in eigenem Fenster derselben Sitzung, fremde Links im System-Browser."""
        req = action.get_request()
        uri = req.get_uri() if req is not None else ""
        kind = classify_new_window(uri, action.get_navigation_type() == self.WebKit.NavigationType.LINK_CLICKED)
        if kind != "popup":
            if kind != "browser" or action.is_user_gesture():
                self._open_elsewhere(kind, uri)
            return None
        return StorePopup(self, view).view

    def _open_elsewhere(self, kind: str, uri: str) -> None:
        """Neues Fenster, das kein Popup wird: gog.com im Store, Fremdes im System-Browser, Rest verwerfen."""
        if kind == "browser":
            open_uri(uri)
        elif kind == "store":
            self.load(uri)

    # --- Seiten-Skript
    def _on_message(self, _ucm, value) -> None:
        try:
            msg = parse_page_message(value.to_string())
        except Exception:
            return
        if msg is None:
            return
        # Der Nachrichtenkanal steht jeder Seite im Store-Fenster offen (auch fremden Seiten und eingebetteten
        # Rahmen) – nur auf GOG-Seiten zählen, und den Kauf nur an der tatsächlich geladenen Adresse erkennen
        uri = self.view.get_uri() or ""
        if not is_store_url(uri):
            return
        if msg["type"] == "nav":
            self._on_uri(uri)                  # prüft auch den Kaufabschluss (check_purchase)
        elif msg["type"] == "user":
            self.banner.set_revealed(not msg["loggedIn"])

    # --- Downloads
    def _on_download(self, download) -> None:
        download.connect("decide-destination", self._dl_destination)
        download.connect("failed", self._dl_failed)
        download.connect("finished", self._dl_finished)

    def _dl_destination(self, download, suggested: str) -> bool:
        try:
            target = unique_download_path(downloads_dir(), suggested)
        except OSError as exc:
            log.warning("Download-Ordner nicht nutzbar: %s", exc)
            return False
        download.set_allow_overwrite(False)
        download.set_destination(str(target))
        log.info("Store-Download nach %s", target)
        self.win.toast(tr('Download gestartet: {0}').format(target.name))
        return True

    def _dl_failed(self, download, error) -> None:
        dest = download.get_destination() or ""
        self._failed_downloads.add(dest)
        if error.matches(self.WebKit.download_error_quark(), self.WebKit.DownloadError.CANCELLED_BY_USER):
            return
        log.warning("Store-Download fehlgeschlagen: %s", error.message)
        self.win.toast(tr('Download fehlgeschlagen: {0}').format(Path(dest).name or error.message))

    def _dl_finished(self, download) -> None:
        dest = download.get_destination() or ""
        if not dest or dest in self._failed_downloads:
            self._failed_downloads.discard(dest)
            return
        log.info("Store-Download abgeschlossen: %s", dest)
        toast = Adw.Toast.new(GLib.markup_escape_text(tr('Download abgeschlossen: {0}').format(Path(dest).name)))
        toast.set_button_label(tr('Ordner öffnen'))
        toast.connect("button-clicked", lambda *_: open_uri(Path(dest).parent.as_uri()))
        toast.set_timeout(8)
        self.win.toasts.add_toast(toast)


class StorePopup:
    """Fenster für ``window.open`` aus dem Store (PayPal, 3-D Secure …) – gleiche Sitzung, schließt mit dem Popup."""

    def __init__(self, page: StorePage, related) -> None:
        self.page = page
        self.view = page.WebKit.WebView(related_view=related)
        self.view.set_vexpand(True)
        self.window = Adw.Window(title=tr('GOG Store'), default_width=560, default_height=760,
                                 transient_for=page.win, destroy_with_parent=True)
        self.window.add_css_class("galaxy")
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(self.view)
        self.window.set_content(tv)
        self.view.connect("ready-to-show", self._show)
        self.view.connect("close", lambda *_: self.window.close())
        self.view.connect("create", page._on_create)
        self.view.connect("decide-policy", page._on_decide_policy)
        self.view.connect("notify::title", lambda v, _p: self.window.set_title(v.get_title() or tr('GOG Store')))
        self.view.connect("notify::uri", lambda v, _p: page.check_purchase(v.get_uri() or ""))
        self.window.connect("close-request", self._closed)
        page._popups.add(self)

    def _show(self, *_a) -> None:
        try:
            geom = self.view.get_window_properties().get_geometry()
            if geom.width >= 200 and geom.height >= 200:
                self.window.set_default_size(min(geom.width, 1280), min(geom.height, 1000))
        except Exception:
            pass
        self.window.present()

    def _closed(self, *_a) -> bool:
        self.page._popups.discard(self)
        return False


def _error_html(retry_uri: str, message: str) -> str:
    """Fehlerseite im Galaxy-Stil (dunkel, violetter Knopf) statt der WebKit-Standardseite."""
    esc = html.escape
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{background:#1b1b20;color:#d9d9de;font-family:sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:90vh;margin:0}div{text-align:center;max-width:560px;padding:0 20px}"
        "h1{font-size:17pt;color:#fff;font-weight:700}p{color:#8c8c92}"
        "a{display:inline-block;margin-top:16px;padding:11px 28px;background:#8b4fd6;color:#fff;border-radius:4px;"
        "text-decoration:none;font-weight:700;letter-spacing:1px}a:hover{background:#9a63df}</style></head><body><div>"
        f"<h1>{esc(tr('Die Seite konnte nicht geladen werden'))}</h1>"
        f"<p>{esc(tr('Prüfe die Internetverbindung und versuche es erneut.'))}</p>"
        f"<p><small>{esc(message or '')}</small></p>"
        f"<a href=\"{esc(retry_uri, quote=True)}\">{esc(tr('ERNEUT VERSUCHEN'))}</a>"
        "</div></body></html>"
    )
