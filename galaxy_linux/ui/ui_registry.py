"""Erweiterungspunkte der Oberfläche außerhalb der Einstellungsdialoge (siehe auch ``prefs_registry``).

Funktionen hängen sich hier ein, ohne ``window.py``/``game_page.py`` zu ändern::

    register_game_action("setup", tr('Setup erneut ausführen'), lambda page, pid: …,
                         visible=lambda page, pid: page.backend.registry.is_installed(pid))
    register_game_section("cloud", lambda page: CloudBox(page), column="right")
    register_window_hook(lambda win: win.connect("notify::is-active", …))
    register_recent_widget("friends", N_("Freunde"), lambda page: FriendsWidget(page), order=60)

* Spiel-Aktionen erscheinen im „⋯“-Menü der Spielansicht (nach „Im Store anzeigen“); ``visible`` wird bei jedem
  Öffnen des Menüs geprüft.
* Spiel-Abschnitte: ``builder(page)`` liefert einmalig ein Gtk.Widget für die linke (``left``: unter „Über das Spiel“
  und „Erfolge“) oder rechte Spalte (``right``: unter „Kompatibilität“) oder eine kleine Kennzahl in der Play-Leiste
  (``stats``: nach SPIELZEIT · ZULETZT GESPIELT · ERFOLGE · KOMPATIBILITÄT). Hat das Widget ``set_game(pid)`` bzw.
  ``refresh_state(pid)``, ruft die Spielansicht sie beim Spielwechsel bzw. bei Zustandsänderungen auf.
* Fenster-Hooks laufen einmal am Ende von ``MainWindow.__init__`` (Timer, Fokus-Signale, eigene Stack-Seiten).
* Widgets der Aktuellen Übersicht (Recent, ``ui/recent_view.py``): ``builder(page)`` liefert bei jedem Aufbau der
  Ansicht ein Gtk.Widget (oder None); der Titel (deutscher Text, ``N_``) erscheint als Überschrift und unter „Ansicht
  anpassen“, wo man das Widget an-/abschaltet und verschiebt. Neue Widgets kommen eingeschaltet ans Ende.

Alle Rückrufe laufen im GTK-Hauptthread. Fehler werden protokolliert und übersprungen. Erweiterungsmodule
``galaxy_linux/ui/ext_<name>.py`` (und ``prefs_<name>.py``) lädt ``prefs_registry.load_extensions()`` automatisch.
Dieses Modul selbst braucht kein GTK.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

COLUMNS = ("left", "right", "stats")


@dataclass
class GameAction:
    key: str
    label: str
    callback: Callable[[Any, str], None]
    visible: Callable[[Any, str], bool] | None = None
    order: int = 100


@dataclass
class GameSection:
    key: str
    builder: Callable[[Any], Any]
    column: str = "left"
    order: int = 100


@dataclass
class RecentWidget:
    key: str
    title: str                       # deutscher Text (N_), wird beim Anzeigen übersetzt
    builder: Callable[[Any], Any]
    order: int = 100


_actions: dict[str, GameAction] = {}
_sections: dict[str, GameSection] = {}
_window_hooks: dict[str, tuple[int, Callable[[Any], None]]] = {}
_recent: dict[str, RecentWidget] = {}
_lock = threading.Lock()


def register_game_action(key: str, label: str, callback: Callable[[Any, str], None],
                         visible: Callable[[Any, str], bool] | None = None, order: int = 100) -> None:
    """Eintrag im „⋯“-Menü der Spielansicht; ``callback(page, product_id)``."""
    with _lock:
        _actions[key] = GameAction(key, label, callback, visible, order)


def register_game_section(key: str, builder: Callable[[Any], Any], column: str = "left", order: int = 100) -> None:
    """Abschnitt in der Spielansicht; ``builder(page)`` → Gtk.Widget oder None."""
    if column not in COLUMNS:
        raise ValueError(f"unbekannte Spalte: {column}")
    with _lock:
        _sections[key] = GameSection(key, builder, column, order)


def register_window_hook(key: str, fn: Callable[[Any], None], order: int = 100) -> None:
    """``fn(window)`` am Ende von ``MainWindow.__init__``."""
    with _lock:
        _window_hooks[key] = (order, fn)


def register_recent_widget(key: str, title: str, builder: Callable[[Any], Any], order: int = 100) -> None:
    """Widget der Aktuellen Übersicht; ``builder(page)`` → Gtk.Widget oder None."""
    with _lock:
        _recent[key] = RecentWidget(key, title, builder, order)


def unregister(key: str) -> None:
    with _lock:
        _actions.pop(key, None)
        _sections.pop(key, None)
        _window_hooks.pop(key, None)
        _recent.pop(key, None)


def recent_widgets() -> list[RecentWidget]:
    with _lock:
        items = list(_recent.values())
    return sorted(items, key=lambda w: (w.order, w.key))


def build_recent_widget(widget: RecentWidget, page: Any) -> Any:
    try:
        return widget.builder(page)
    except Exception:
        log.exception("Widget %s der Aktuellen Übersicht fehlgeschlagen", widget.key)
        return None


def game_actions() -> list[GameAction]:
    with _lock:
        items = list(_actions.values())
    return sorted(items, key=lambda a: (a.order, a.key))


def game_sections(column: str) -> list[GameSection]:
    with _lock:
        items = [s for s in _sections.values() if s.column == column]
    return sorted(items, key=lambda s: (s.order, s.key))


def action_visible(action: GameAction, page: Any, product_id: str) -> bool:
    if not product_id:
        return False
    if action.visible is None:
        return True
    try:
        return bool(action.visible(page, product_id))
    except Exception:
        log.exception("Sichtbarkeit der Spiel-Aktion %s fehlgeschlagen", action.key)
        return False


def run_action(action: GameAction, page: Any, product_id: str) -> None:
    try:
        action.callback(page, product_id)
    except Exception:
        log.exception("Spiel-Aktion %s fehlgeschlagen", action.key)


def build_sections(column: str, page: Any) -> list[Any]:
    out = []
    for sec in game_sections(column):
        try:
            w = sec.builder(page)
        except Exception:
            log.exception("Abschnitt %s der Spielansicht fehlgeschlagen", sec.key)
            continue
        if w is not None:
            out.append(w)
    return out


def notify_sections(widgets: list[Any], method: str, product_id: str) -> None:
    """``widget.<method>(product_id)`` für alle Abschnitte, die die Methode haben."""
    for w in widgets:
        fn = getattr(w, method, None)
        if callable(fn):
            try:
                fn(product_id)
            except Exception:
                log.exception("Abschnitt %s.%s fehlgeschlagen", type(w).__name__, method)


def run_window_hooks(window: Any) -> list[str]:
    with _lock:
        items = sorted(_window_hooks.items(), key=lambda kv: (kv[1][0], kv[0]))
    ran = []
    for key, (_order, fn) in items:
        try:
            fn(window)
            ran.append(key)
        except Exception:
            log.exception("Fenster-Erweiterung %s fehlgeschlagen", key)
    return ran
