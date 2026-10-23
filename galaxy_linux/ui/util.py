"""Hilfen für die Oberfläche: Threads → Hauptschleife, Bild-Cache, Formatierung."""
from __future__ import annotations

from ..i18n import tr

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..settings import format_bytes  # noqa: E402

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="gl-ui")


def idle(fn: Callable, *args: Any, **kw: Any) -> None:
    """Ruft fn in der GTK-Hauptschleife auf."""
    def _cb() -> bool:
        try:
            fn(*args, **kw)
        except Exception:  # pragma: no cover
            log.exception("UI-Callback fehlgeschlagen")
        return GLib.SOURCE_REMOVE
    GLib.idle_add(_cb)


def run_async(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> None:
    def _work() -> None:
        try:
            res = fn()
        except Exception as exc:
            log.debug("Hintergrundaufgabe fehlgeschlagen", exc_info=True)
            if on_error:
                idle(on_error, exc)
            else:
                log.warning("Hintergrundaufgabe fehlgeschlagen: %s", exc)
            return
        if on_done:
            idle(on_done, res)
    _pool.submit(_work)


class ImageLoader:
    """Lädt Bilder über den API-Cache im Hintergrund und liefert Gdk.Texture an die Hauptschleife."""

    def __init__(self, api) -> None:
        self.api = api
        self._cache: dict[str, Gdk.Texture] = {}
        self._lock = threading.Lock()
        self._failed: set[str] = set()

    def load(self, url: str, callback: Callable[[Gdk.Texture], None]) -> None:
        if not url or url in self._failed:
            return
        with self._lock:
            tex = self._cache.get(url)
        if tex is not None:
            callback(tex)
            return

        def work() -> Gdk.Texture | None:
            path = self.api.cached_image(url)
            if not path:
                return None
            try:
                return Gdk.Texture.new_from_filename(str(path))
            except GLib.Error as exc:
                log.debug("Bild %s nicht ladbar: %s", path, exc)
                return None

        def done(tex: Gdk.Texture | None) -> None:
            if tex is None:
                self._failed.add(url)
                return
            with self._lock:
                self._cache[url] = tex
            callback(tex)

        run_async(work, done)


SPEED_IN_BITS = [False]      # Einstellung „Download-Geschwindigkeit in Bit pro Sekunde anzeigen“


def fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "–"
    if SPEED_IN_BITS[0]:
        bits = bps * 8
        for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
            if bits < 1000 or unit == "Gbit/s":
                return f"{bits:.0f} {unit}" if unit == "bit/s" else f"{bits:.1f} {unit}"
            bits /= 1000
    return f"{format_bytes(bps)}/s"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return tr('{0} min {1:02d} s').format(m, s)
    h, m = divmod(m, 60)
    return tr('{0} h {1:02d} min').format(h, m)


_icon_cache: dict[tuple, str] = {}


def pick_icon(*names: str, default: str = "image-missing-symbolic") -> str:
    """Ersten im Icon-Theme vorhandenen Namen wählen (Adwaita-Namen unterscheiden sich je Version)."""
    key = tuple(names)
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for n in names:
            if theme.has_icon(n):
                _icon_cache[key] = n
                return n
    except Exception:
        pass
    _icon_cache[key] = names[0] if names else default
    return _icon_cache[key]


def relative_time(ts: float) -> str:
    import time as _t
    if not ts:
        return tr("nie")
    d = _t.time() - ts
    if d < 3600:
        return tr('gerade eben') if d < 120 else tr('vor {0} Min.').format(int(d // 60))
    if d < 86400 and _t.localtime(ts).tm_mday == _t.localtime().tm_mday:
        return tr("heute")
    if d < 2 * 86400:
        return tr("gestern")
    if d < 30 * 86400:
        return tr('vor {0} Tagen').format(int(d // 86400))
    return _t.strftime("%d.%m.%Y", _t.localtime(ts))


def clamped_picture(width: int, height: int, css: list[str] | None = None,
                    placeholder: str = "") -> tuple[Gtk.Widget, Gtk.Picture]:
    """Bild mit fest begrenzter Größe (COVER-Zuschnitt); ohne Clamp würde das Bild auf Originalbreite wachsen.
    Mit ``placeholder`` steht der Text hinter dem Bild – sichtbar, solange (oder falls) kein Cover geladen ist."""
    pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
    child = pic
    if placeholder:
        ov = Gtk.Overlay()
        # Platzhalter füllt die ganze Fläche (sonst bestimmt seine Textbreite die Bildgröße)
        ph = Gtk.Label(label=placeholder, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=1,
                       halign=Gtk.Align.FILL, valign=Gtk.Align.FILL, hexpand=True, vexpand=True, xalign=0.5, yalign=0.5)
        ph.set_size_request(width - 16, -1)
        ph.set_wrap_mode(Pango.WrapMode.WORD_CHAR)   # lange Wörter ohne Leerzeichen umbrechen statt die Karte zu verbreitern
        ph.add_css_class("cover-placeholder")
        ov.set_child(ph)
        ov.add_overlay(pic)
        child = ov
    inner = Adw.Clamp(orientation=Gtk.Orientation.VERTICAL, maximum_size=height, tightening_threshold=height, child=child)
    outer = Adw.Clamp(maximum_size=width, tightening_threshold=width, child=inner)
    frame = Gtk.Box(hexpand=False, vexpand=False, halign=Gtk.Align.START, valign=Gtk.Align.START)
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    frame.set_size_request(width, height)
    for c in css or []:
        frame.add_css_class(c)
    frame.append(outer)
    return frame, pic


def open_uri(uri: str) -> None:
    try:
        Gtk.UriLauncher.new(uri).launch(None, None, None)
    except Exception as exc:  # pragma: no cover
        log.warning("URI %s konnte nicht geöffnet werden: %s", uri, exc)


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


def label(text: str = "", css: list[str] | None = None, xalign: float = 0.0, wrap: bool = False,
          ellipsize: bool = False, markup: bool = False, selectable: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(xalign=xalign)
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    for c in css or []:
        lbl.add_css_class(c)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    lbl.set_selectable(selectable)
    return lbl


def icon_button(icon: str, tooltip: str, css: list[str] | None = None) -> Gtk.Button:
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    for c in css or []:
        b.add_css_class(c)
    return b


def choose_folder(parent: Gtk.Window | None, title: str, initial: str, callback: Callable[[str], None]) -> None:
    dlg = Gtk.FileDialog(title=title, modal=True)
    try:
        if initial:
            dlg.set_initial_folder(Gio.File.new_for_path(initial))
    except Exception:
        pass

    def done(d: Gtk.FileDialog, res) -> None:
        try:
            f = d.select_folder_finish(res)
        except GLib.Error:
            return
        if f and f.get_path():
            callback(f.get_path())
    dlg.select_folder(parent, None, done)


LANGUAGES: list[tuple[str, str]] = [
    ("de-DE", tr('Deutsch')), ("en-US", tr('English')), ("fr-FR", "Français"), ("es-ES", "Español"), ("it-IT", tr('Italiano')),
    ("pl-PL", tr('Polski')), ("ru-RU", "Русский"), ("pt-BR", tr('Português (Brasil)')), ("zh-Hans", "中文 (简体)"),
    ("ja-JP", "日本語"), ("cs-CZ", "Čeština"), ("hu-HU", tr('Magyar')), ("ko-KR", "한국어"), ("tr-TR", tr('Türkçe')),
    ("nl-NL", tr('Nederlands')), ("uk-UA", "Українська"), ("sv-SE", tr('Svenska')), ("es-MX", tr('Español (México)')),
]


def language_index(code: str) -> int:
    code = (code or "").lower()
    for i, (c, _) in enumerate(LANGUAGES):
        if c.lower() == code:
            return i
    for i, (c, _) in enumerate(LANGUAGES):
        if c.split("-")[0].lower() == code.split("-")[0]:
            return i
    return 1
