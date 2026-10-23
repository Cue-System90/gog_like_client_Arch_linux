"""WebKitGTK 6.0 für Login und Store: eine gemeinsame, dauerhafte Sitzung (Cookies, Cache) für alle Web-Ansichten.

So bleibt die Anmeldung im eingebetteten Login auch im Store gültig, wie im Original (ein Browser-Profil).
WebKit wird erst beim ersten Gebrauch geladen; fehlt ``webkitgtk-6.0``, liefern die Funktionen None und die
Aufrufer weichen auf den System-Browser aus. Alles hier läuft im GTK-Hauptthread.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from .. import paths

log = logging.getLogger(__name__)

_webkit: Any = None
_checked = False
_session: Any = None
_download_handlers: list[Callable[[Any], None]] = []


#: Umgebung für WebKit, bevor der erste Web-Prozess startet (er erbt sie). WEBKIT_GST_DISABLE_GL_SINK: Videos auf
#: gog.com (z. B. die ``.gif.mp4``-Schleifen in Spielbeschreibungen) erschienen mit WebKitGTK 2.54 und NVIDIA als
#: grüne Flächen – die GL-Videoausgabe übergibt die Bilder per DMA-BUF falsch. Ohne GL-Sink stimmt das Bild, der
#: Rest der Seite bleibt GPU-beschleunigt. Ein vom Nutzer gesetzter Wert hat Vorrang.
WEBKIT_ENV = {"WEBKIT_GST_DISABLE_GL_SINK": "1"}


def prepare_environment() -> None:
    for key, value in WEBKIT_ENV.items():
        if key not in os.environ:
            os.environ[key] = value
            log.info("WebKit: %s=%s", key, value)


def webkit():
    """Das Modul ``gi.repository.WebKit`` (6.0, GTK4) oder None, wenn es nicht installiert ist."""
    global _webkit, _checked
    if not _checked:
        _checked = True
        prepare_environment()
        try:
            import gi
            gi.require_version("WebKit", "6.0")
            from gi.repository import WebKit  # type: ignore
            _webkit = WebKit
        except Exception as exc:
            log.info("WebKitGTK 6.0 nicht verfügbar (%s) – Login und Store laufen über den System-Browser", exc)
            _webkit = None
    return _webkit


def available() -> bool:
    return webkit() is not None


def data_dir() -> Path:
    return paths.DATA_DIR / "webkit"


def cache_dir() -> Path:
    return paths.CACHE_DIR / "webkit"


def _private_dir(path: Path) -> None:
    """Ordner nur für den Nutzer lesbar anlegen – dort liegen Cookies der GOG-Anmeldung."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def session():
    """Gemeinsame ``WebKit.NetworkSession`` (Daten unter ~/.local/share/galaxy-linux/webkit, Cache unter
    ~/.cache/galaxy-linux/webkit, Cookies dauerhaft in cookies.sqlite). None ohne WebKit."""
    global _session
    WebKit = webkit()
    if WebKit is None:
        return None
    if _session is None:
        try:
            _private_dir(data_dir())
            _private_dir(cache_dir())
            s = WebKit.NetworkSession.new(str(data_dir()), str(cache_dir()))
            s.get_cookie_manager().set_persistent_storage(str(data_dir() / "cookies.sqlite"),
                                                          WebKit.CookiePersistentStorage.SQLITE)
        except Exception:
            log.exception("Dauerhafte Web-Sitzung konnte nicht angelegt werden – nutze die Standardsitzung")
            s = WebKit.NetworkSession.get_default()
        s.connect("download-started", _on_download_started)
        _session = s
    return _session


def new_view(**props):
    """Neue ``WebKit.WebView`` in der gemeinsamen Sitzung (``props`` z. B. ``user_content_manager``). None ohne WebKit."""
    WebKit = webkit()
    if WebKit is None:
        return None
    if "related_view" not in props:
        s = session()
        if s is not None:
            props.setdefault("network_session", s)
    view = WebKit.WebView(**props)
    st = view.get_settings()
    st.set_enable_developer_extras(False)
    st.set_enable_back_forward_navigation_gestures(True)
    st.set_javascript_can_access_clipboard(False)
    return view


def add_download_handler(fn: Callable[[Any], None]) -> None:
    """``fn(download)`` für jeden Download der gemeinsamen Sitzung (Store, Popups, Login)."""
    if fn not in _download_handlers:
        _download_handlers.append(fn)


def remove_download_handler(fn: Callable[[Any], None]) -> None:
    if fn in _download_handlers:
        _download_handlers.remove(fn)


def _on_download_started(_session, download) -> None:
    for fn in list(_download_handlers):
        try:
            fn(download)
        except Exception:
            log.exception("Download-Behandlung fehlgeschlagen")


def clear_data(done: Callable[[], None] | None = None) -> bool:
    """Cookies, Speicher und Cache der gemeinsamen Sitzung löschen (Ausloggen). False ohne WebKit/Sitzungsdaten."""
    WebKit = webkit()
    if WebKit is None or (_session is None and not data_dir().exists()):
        return False
    s = session()
    if s is None:
        return False

    def finished(mgr, res) -> None:
        try:
            mgr.clear_finish(res)
            log.info("Web-Sitzung (Store/Login) geleert")
        except Exception as exc:
            log.warning("Web-Sitzung konnte nicht geleert werden: %s", exc)
        if done:
            done()
    s.get_website_data_manager().clear(WebKit.WebsiteDataTypes.ALL, 0, None, finished)
    return True
