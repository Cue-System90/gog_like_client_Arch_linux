"""Globale Einstellungen, Pro-Spiel-Einstellungen, Installations-Registry und Spielzeit.

Alles liegt als JSON unter ~/.config/galaxy-linux bzw. ~/.local/share/galaxy-linux.
"""
from __future__ import annotations

from .i18n import tr

import locale
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .jsonstore import read_json, write_json


def _system_language() -> str:
    """Leitet die bevorzugte GOG-Sprache (z. B. de-DE) aus der Systemsprache ab."""
    lang = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not lang:
        try:
            lang = locale.getlocale()[0] or ""
        except Exception:
            lang = ""
    lang = lang.split(".")[0].replace("_", "-")
    if not lang or lang in ("C", "POSIX"):
        return "en-US"
    if "-" not in lang:
        lang = {"de": "de-DE", "en": "en-US", "fr": "fr-FR", "es": "es-ES", "it": "it-IT",
                "pl": "pl-PL", "ru": "ru-RU", "pt": "pt-BR", "zh": "zh-Hans", "ja": "ja-JP"}.get(lang, "en-US")
    return lang


def _from_dict(cls, data: dict[str, Any] | None):
    """Tolerantes Laden: unbekannte Schlüssel werden ignoriert, fehlende bekommen Defaults."""
    data = data or {}
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


DEFAULT_EXTERNAL_DIR = paths.DEFAULT_GAMES_DIR.parent / "External"
EXTERNAL_LIBRARY_ID = "external"     # die bis 0.1.4 fest eingebaute External-Bibliothek
VIEW_KEYS = ("layout", "cover_size", "spacing", "titles", "sections", "sort")


@dataclass
class CustomLibrary:
    """Eigene Bibliothek für Nicht-GOG-Spiele: jeder Unterordner von games_dir ist ein Spiel mit eigenem Prefix
    in prefix_dir. Die Vorgaben gelten nur, wenn das Spiel selbst nichts gesetzt hat."""
    id: str
    name: str
    games_dir: str
    prefix_dir: str
    compat_tool: str = ""          # "" = globale Vorgabe, sonst CompatTool-ID
    launch_options: str = ""       # Steam-Syntax wie pro Spiel
    wayland_driver: str = ""       # "" = globale Vorgabe, "on" / "off"
    prefix_dpi: int = 0            # 0 = globale Vorgabe
    watch: bool = True             # Ordner überwachen und kopierte Spiele übernehmen
    view: dict[str, Any] = field(default_factory=dict)   # Ansicht (layout, cover_size, …); fehlt ein Schlüssel → global

    @property
    def games_root(self) -> Path:
        return Path(os.path.expanduser(self.games_dir))

    @property
    def prefix_root(self) -> Path:
        return Path(os.path.expanduser(self.prefix_dir))


@dataclass
class GlobalSettings:
    install_dir: str = str(paths.DEFAULT_GAMES_DIR)
    prefix_dir: str = str(paths.DEFAULT_PREFIX_DIR)
    # "auto" = bestes gefundenes Proton (umu bevorzugt); sonst CompatTool-ID
    default_compat_tool: str = "auto"
    default_launch_options: str = ""
    prefer_umu: bool = True
    wayland_driver: bool = True       # Proton-Wayland-Treiber (PROTON_ENABLE_WAYLAND=1) als Vorgabe – KDE-Wayland ist Standard
    max_workers: int = 6
    language: str = field(default_factory=_system_language)
    ui_language: str = "auto"         # Sprache der Oberfläche: auto (Systemsprache) | de | en
    sync_playtime: bool = True
    install_redist: bool = True
    auto_check_updates: bool = True
    show_hidden: bool = False
    use_keyring: bool = False
    env: dict[str, str] = field(default_factory=dict)
    install_locations: list[str] = field(default_factory=list)   # weitere Installationsorte (wie im Windows-Client)
    shortcut_menu: bool = True        # nach der Installation Verknüpfung im Startmenü anlegen
    shortcut_desktop: bool = False    # nach der Installation Verknüpfung auf dem Schreibtisch anlegen
    watch_install_dirs: bool = True   # kopierte Spiele in den Spiele-Ordnern erkennen und übernehmen
    auto_create_prefixes: bool = True # fehlende Wine-Prefixe automatisch anlegen
    prefix_dpi: int = 0               # DPI für neue Prefixe: 0 = automatisch (KDE-Skalierung), 96 = unverändert
    libraries: list[CustomLibrary] = field(default_factory=list)   # eigene Bibliotheken (Nicht-GOG-Spiele)
    ignored_paths: list[str] = field(default_factory=list)   # von Hand entfernte Spielordner – Suche nimmt sie nicht wieder auf
    library_layout: str = "grid"      # grid | list
    library_cover_size: str = "S"     # XS S M L XL
    library_spacing: str = "M"        # 0 S M L
    library_titles: bool = True       # Titel unter den Covern
    library_sections: bool = True     # Abschnitte wie „Als Nächstes spielen“
    library_sort: int = 0

    _lock = threading.Lock()

    def library(self, lib_id: str) -> CustomLibrary | None:
        for lib in self.libraries:
            if lib.id == lib_id:
                return lib
        return None

    def library_for_path(self, path: str | Path) -> CustomLibrary | None:
        """Bibliothek, in deren Spiele-Ordner der Pfad liegt."""
        p = Path(os.path.expanduser(str(path))).resolve()
        for lib in self.libraries:
            root = lib.games_root.resolve()
            if p == root or root in p.parents:
                return lib
        return None

    def view_settings(self, lib: CustomLibrary | None = None) -> dict[str, Any]:
        """Ansicht der Bibliotheksseite; eigene Bibliotheken überschreiben einzelne Werte (Abschnitte dort aus)."""
        out = {k: getattr(self, f"library_{k}") for k in VIEW_KEYS}
        if lib is not None:
            out["sections"] = False
            out.update({k: v for k, v in lib.view.items() if k in VIEW_KEYS})
        return out

    def remember_location(self, path: str) -> None:
        path = os.path.expanduser(str(path).strip())
        if not path or path == os.path.expanduser(self.install_dir):
            return
        if path not in self.install_locations:
            self.install_locations.append(path)
            self.save()

    @classmethod
    def load(cls) -> "GlobalSettings":
        raw = read_json(paths.SETTINGS_FILE, {})
        s = _from_dict(cls, raw)
        libs = []
        for d in s.libraries if isinstance(s.libraries, list) else []:
            if isinstance(d, dict) and all(d.get(k) for k in ("id", "name", "games_dir", "prefix_dir")):
                lib = _from_dict(CustomLibrary, d)
                lib.view = dict(lib.view) if isinstance(lib.view, dict) else {}
                libs.append(lib)
        s.libraries = libs
        if "libraries" not in raw:
            s._migrate_external(raw)
        return s

    def _migrate_external(self, raw: dict[str, Any]) -> None:
        """Bis 0.1.4 gab es genau eine External-Bibliothek (external_games_dir/external_prefix_dir). Sie wird die erste
        eigene Bibliothek mit der ID „external“; die alte Datei bleibt als settings.json.vor-bibliotheken liegen."""
        games = str(raw.get("external_games_dir") or DEFAULT_EXTERNAL_DIR)
        prefixes = str(raw.get("external_prefix_dir") or DEFAULT_EXTERNAL_DIR / "prefixes")
        self.libraries = [CustomLibrary(id=EXTERNAL_LIBRARY_ID, name="External", games_dir=games, prefix_dir=prefixes)]
        if not raw:
            return                       # Neuinstallation: nichts zu sichern, gespeichert wird beim ersten Ändern
        backup = paths.SETTINGS_FILE.with_name(paths.SETTINGS_FILE.name + ".vor-bibliotheken")
        try:
            if paths.SETTINGS_FILE.exists() and not backup.exists():
                shutil.copy2(paths.SETTINGS_FILE, backup)
        except OSError:
            pass
        self.save()

    def save(self) -> None:
        with GlobalSettings._lock:
            write_json(paths.SETTINGS_FILE, asdict(self))


@dataclass
class GameSettings:
    """Pro-Spiel-Einstellungen, analog zu Steams Eigenschaften-Dialog."""
    compat_tool: str = ""          # "" = globale Vorgabe, "native" = kein Tool, sonst CompatTool-ID
    prefix: str = ""               # "" = <prefix_dir>/<product_id>
    launch_options: str = ""       # Steam-Syntax, z. B. "mangohud %command%"
    env: dict[str, str] = field(default_factory=dict)
    use_mangohud: bool = False
    use_vkbasalt: bool = False
    use_gamemode: bool = False
    use_gamescope: bool = False
    gamescope_args: str = "-f"
    play_task: str = ""            # Name des gewählten playTask, "" = primär
    language: str = ""             # Installationssprache, "" = global
    install_redist: bool = True
    redist_done: list[str] = field(default_factory=list)
    proton_verb: str = "waitforexitandrun"
    disable_protonfixes: bool = False
    umu_game_id: str = ""          # "" = umu-<product_id>
    wayland_driver: str = ""       # "" = globale Vorgabe, "on" / "off": Proton-Wayland-Treiber statt XWayland
    pre_launch: str = ""
    post_exit: str = ""
    hidden: bool = False
    favorite: bool = False


class GameSettingsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = read_json(paths.GAME_SETTINGS_FILE, {})

    def get(self, product_id: str) -> GameSettings:
        return _from_dict(GameSettings, self._data.get(str(product_id)))

    def set(self, product_id: str, settings: GameSettings) -> None:
        with self._lock:
            self._data[str(product_id)] = asdict(settings)
            write_json(paths.GAME_SETTINGS_FILE, self._data)

    def update(self, product_id: str, **changes: Any) -> GameSettings:
        s = self.get(product_id)
        for k, v in changes.items():
            setattr(s, k, v)
        self.set(product_id, s)
        return s


@dataclass
class InstalledGame:
    product_id: str
    title: str
    platform: str                 # windows | linux | osx
    install_path: str
    build_id: str = ""
    version_name: str = ""
    language: str = ""
    installed_dlcs: list[str] = field(default_factory=list)
    install_size: int = 0
    method: str = "content-system"  # content-system | linux-installer | offline-installer | imported | external
    executable: str = ""            # nur für Linux-Installer: Startskript/Binary
    installed_at: float = field(default_factory=time.time)
    branch: str = ""
    library: str = ""               # nur extern: ID der eigenen Bibliothek


class InstalledRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = read_json(paths.INSTALLED_FILE, {})

    def all(self) -> list[InstalledGame]:
        return [_from_dict(InstalledGame, v) for v in self._data.values()]

    def get(self, product_id: str) -> InstalledGame | None:
        v = self._data.get(str(product_id))
        return _from_dict(InstalledGame, v) if v else None

    def is_installed(self, product_id: str) -> bool:
        return str(product_id) in self._data

    def set(self, game: InstalledGame) -> None:
        with self._lock:
            self._data[str(game.product_id)] = asdict(game)
            write_json(paths.INSTALLED_FILE, self._data)

    def remove(self, product_id: str) -> None:
        with self._lock:
            self._data.pop(str(product_id), None)
            write_json(paths.INSTALLED_FILE, self._data)


class PlaytimeStore:
    """Lokale Spielzeit (Minuten) und letzter Start, analog zu Galaxy/Steam."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = read_json(paths.PLAYTIME_FILE, {})

    def get(self, product_id: str) -> tuple[int, float]:
        v = self._data.get(str(product_id), {})
        return int(v.get("minutes", 0)), float(v.get("last_played", 0))

    def add_session(self, product_id: str, minutes: int, ended_at: float | None = None) -> None:
        with self._lock:
            v = self._data.setdefault(str(product_id), {"minutes": 0, "last_played": 0})
            v["minutes"] = int(v.get("minutes", 0)) + max(0, int(minutes))
            v["last_played"] = ended_at or time.time()
            write_json(paths.PLAYTIME_FILE, self._data)

    def set_remote(self, product_id: str, minutes: int, last_played: float) -> None:
        """Übernimmt Werte vom GOG-Server, falls die höher sind (Mehrgeräte-Nutzung)."""
        with self._lock:
            v = self._data.setdefault(str(product_id), {"minutes": 0, "last_played": 0})
            if minutes > int(v.get("minutes", 0)):
                v["minutes"] = minutes
            if last_played > float(v.get("last_played", 0)):
                v["last_played"] = last_played
            write_json(paths.PLAYTIME_FILE, self._data)


def format_minutes(minutes: int) -> str:
    if minutes <= 0:
        return tr('noch nicht gespielt')
    if minutes < 60:
        return tr('{0} Min.').format(minutes)
    h, m = divmod(minutes, 60)
    return tr('{0} Std. {1} Min.').format(h, m) if m else tr('{0} Std.').format(h)


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B",) else f"{int(n)} {unit}"
        n /= 1024
    return tr('{0:.1f} TB').format(n)


def iter_ids(items: Iterable[Any]) -> list[str]:
    return [str(getattr(i, "product_id", i)) for i in items]
