"""Erweiterungspunkt für Einstellungsdialoge: Funktionen hängen eigene Gruppen an, ohne die Dialoge zu ändern.

Globale Einstellungen (``PreferencesDialog``), Seiten: ``general`` (Allgemein), ``interface`` (Oberfläche),
``notifications`` (Benachrichtigungen), ``downloads`` (Installieren/aktualisieren), ``features`` (Spielfunktionen),
``libraries`` (Bibliotheken), ``compat`` (Kompatibilität)::

    register("features", lambda dlg: meine_gruppe(dlg))        # dlg.backend, dlg.s (GlobalSettings), dlg._save(**k)

Spiel-Eigenschaften (``GameSettingsDialog``), Seiten: ``compat``, ``launch``, ``features``, ``env``, ``advanced``::

    register_game("features", lambda dlg, pid: meine_gruppe(dlg, pid))   # dlg.backend, dlg.gs, dlg._save(**k)

Ein Builder gibt eine ``Adw.PreferencesGroup`` zurück (oder ``None`` = nichts anzeigen). Gruppen erscheinen nach den
eingebauten in ``order``-Reihenfolge. Ein fehlerhafter Builder wird protokolliert und übersprungen. Builder laufen im
GTK-Hauptthread; ``dlg._save(**felder)`` speichert sofort (wie alle Schalter der Dialoge, kein „OK“).
Module ``galaxy_linux/ui/prefs_<name>.py`` und ``galaxy_linux/ui/ext_<name>.py`` werden von ``load_extensions()``
automatisch importiert und rufen dort ``register``/``register_game`` (bzw. die Funktionen aus ``ui_registry``) auf.
Dieses Modul selbst braucht kein GTK.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

PAGES = ("general", "interface", "notifications", "downloads", "features", "libraries", "compat")
GAME_PAGES = ("compat", "launch", "features", "env", "advanced")

Builder = Callable[[Any], Any]
GameBuilder = Callable[[Any, str], Any]

_global: dict[str, dict[str, tuple[int, Builder]]] = {}
_game: dict[str, dict[str, tuple[int, GameBuilder]]] = {}
_lock = threading.Lock()
_load_lock = threading.RLock()
_loaded = False


def _key(builder: Callable, key: str) -> str:
    return key or f"{getattr(builder, '__module__', '')}.{getattr(builder, '__qualname__', repr(builder))}"


def register(page_id: str, builder: Builder, order: int = 100, key: str = "") -> None:
    """Gruppe für eine Seite des globalen Einstellungsdialogs anmelden (gleicher ``key`` ersetzt)."""
    if page_id not in PAGES:
        raise ValueError(f"unbekannte Einstellungsseite: {page_id}")
    with _lock:
        _global.setdefault(page_id, {})[_key(builder, key)] = (order, builder)


def register_game(page_id: str, builder: GameBuilder, order: int = 100, key: str = "") -> None:
    """Gruppe für eine Seite der Spiel-Eigenschaften anmelden (gleicher ``key`` ersetzt)."""
    if page_id not in GAME_PAGES:
        raise ValueError(f"unbekannte Eigenschaften-Seite: {page_id}")
    with _lock:
        _game.setdefault(page_id, {})[_key(builder, key)] = (order, builder)


def unregister(key: str) -> None:
    with _lock:
        for table in (*_global.values(), *_game.values()):
            table.pop(key, None)


def builders(page_id: str) -> list[Builder]:
    with _lock:
        items = list(_global.get(page_id, {}).items())
    return [b for _k, (_o, b) in sorted(items, key=lambda kv: (kv[1][0], kv[0]))]


def game_builders(page_id: str) -> list[GameBuilder]:
    with _lock:
        items = list(_game.get(page_id, {}).items())
    return [b for _k, (_o, b) in sorted(items, key=lambda kv: (kv[1][0], kv[0]))]


def build_groups(page_id: str, dialog: Any) -> list[Any]:
    """Alle angemeldeten Gruppen einer globalen Seite bauen (None und Fehler fallen heraus)."""
    out = []
    for b in builders(page_id):
        try:
            grp = b(dialog)
        except Exception:
            log.exception("Einstellungsgruppe für Seite %s fehlgeschlagen", page_id)
            continue
        if grp is not None:
            out.append(grp)
    return out


def build_game_groups(page_id: str, dialog: Any, product_id: str) -> list[Any]:
    """Alle angemeldeten Gruppen einer Seite der Spiel-Eigenschaften bauen."""
    out = []
    for b in game_builders(page_id):
        try:
            grp = b(dialog, str(product_id))
        except Exception:
            log.exception("Eigenschaften-Gruppe für Seite %s (%s) fehlgeschlagen", page_id, product_id)
            continue
        if grp is not None:
            out.append(grp)
    return out


def extension_modules(names: list[str]) -> list[str]:
    """Namen der Erweiterungsmodule (``prefs_*``, ``ext_*``) aus einer Modulliste, sortiert."""
    return sorted(n for n in names if (n.startswith("prefs_") and n != "prefs_registry") or n.startswith("ext_"))


def load_extensions() -> None:
    """Importiert einmalig alle Module ``galaxy_linux/ui/prefs_*.py`` und ``ext_*.py`` (außer diesem)."""
    global _loaded
    with _load_lock:
        if _loaded:
            return
        pkg = sys.modules[__package__]
        for name in extension_modules([m.name for m in pkgutil.iter_modules(pkg.__path__)]):
            try:
                importlib.import_module(f"{__package__}.{name}")
            except Exception:
                log.exception("Oberflächen-Erweiterung %s konnte nicht geladen werden", name)
        _loaded = True
