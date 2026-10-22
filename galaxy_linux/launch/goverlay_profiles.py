"""GOverlay 1.9.4 pro Spiel: Spiele-Ordner eintragen, Cover vorbelegen, Profil-``bgmod`` beim Start voranstellen.

Grundlage: reference/overhaul-2026-10-22/goverlay.md §3–§7 („Rang 1 – A + W“).

* **A – Sichtbarkeit:** GOverlay kennt außer Steam nur die Ordnerliste ``${XDG_CONFIG_HOME:-~/.config}/goverlay/
  nonsteam_folders.txt`` – eine Bibliotheks-Wurzel pro Zeile, jeder direkte Unterordner wird eine Spielkarte (Name =
  Ordnername). Eingetragen werden nur Wurzeln (GOG-Spiele-Ordner, weitere Installationsorte, Spiele-Ordner der eigenen
  Bibliotheken), nie einzelne Spielordner. Zeilen des Nutzers bleiben unangetastet; entfernt werden nur Zeilen, die
  galaxy-linux selbst eingetragen hat (``GlobalSettings.goverlay_added_folders``).
  Cover: ``~/.cache/goverlay/nonsteam_covers/<SanitizeFileName(Ordnername)>.jpg`` als echtes JPEG, nie ``.fallback``.
* **W – Anwendung:** Klickt man das Spiel in GOverlay an, entsteht ``${XDG_DATA_HOME:-~/.local/share}/goverlay/
  gameconfig/<SanitizeFileName(Ordnername)>/`` mit ``bgmod``. Dieses ``bgmod`` (echte Datei, kein Symlink – bgmod
  erkennt sein Profil über ``/proc/self/exe``) kommt beim Start in ``LaunchPlan.tool_wrappers``. ``bgmod.conf``
  ``[Config] gamemode=1`` → ``-- env gamemoderun`` dahinter, ``winedetectionenable=0`` → ``/WineDetectionEnabled:False``
  ans Ende (beides liest bgmod nicht selbst). Eigene MangoHud-/vkBasalt-Schalter entfallen dann (außer MangoHud bei
  Linux-Spielen: bgmod setzt kein LD_PRELOAD, der ``mangohud``-Wrapper beachtet das ``MANGOHUD_CONFIGFILE`` des Profils).
* Vor Prüfen/Reparieren, Aktualisieren, Verschieben und Deinstallieren nimmt ``bgmod-uninstaller`` die Änderungen von
  bgmod im Spielordner zurück (wie GOverlays „Uninstall changes“, §5.5).

Alle Pfade werden bei jedem Aufruf aus der Umgebung gelesen (Tests setzen HOME/XDG_* auf Temp-Ordner).
"""
from __future__ import annotations

from ..i18n import tr

import hashlib
import io
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

FOLDERS_FILE = "nonsteam_folders.txt"
SKIP_NAMES = ("prefixes", "common", "compatdata", "shadercache", "downloads", "tmp", "temp")   # überspringt GOverlay
GLOBAL_PROFILE = "global"
OFF = "none"                         # GameSettings.goverlay_profile: Profil aus
PROFILE_PATH_MAX = 255               # bgmod: ParamStr(0) ist ein FPC-shortstring (255 Byte)
SYSTEM_BGMOD_DIRS = [Path("/usr/lib")]   # Paket goverlay: /usr/lib/bgmod, /usr/lib/bgmod-uninstaller
GAME_PERFORMANCE = "game-performance"
WINE_DETECTION_OFF = "/WineDetectionEnabled:False"
GOVERLAY_WRAPPERS = ("bgmod", "fgmod")
_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- .+()[]")


# --------------------------------------------------------------------------- Namen und Pfade
def goverlay_sanitize(name: str) -> str:
    """Nachbau von GOverlays ``SanitizeFileName`` (byteweise auf UTF-8): jedes Byte außer ``a-z A-Z 0-9 _ - Leerzeichen
    . + ( ) [ ]`` → ``_``, Folgen von ``_`` → ein ``_``, Ränder (Bytes ≤ 0x20) weg, leer → ``game``."""
    out = bytearray()
    for b in str(name or "").encode("utf-8", "surrogateescape"):
        c = b if b in _SAFE else 0x5F
        if c == 0x5F and out and out[-1] == 0x5F:
            continue
        out.append(c)
    text = bytes(out).strip(bytes(range(0x21))).decode("ascii")
    return text or "game"


def config_dir() -> Path:
    """GOverlays Konfigurationsordner (``GetGoverlayFolder``): $XDG_CONFIG_HOME, falls vorhanden, sonst ~/.config."""
    x = os.environ.get("XDG_CONFIG_HOME") or ""
    base = Path(x) if x and Path(x).is_dir() else Path.home() / ".config"
    return base / "goverlay"


def data_dir() -> Path:
    """``GetHostDataDir`` + goverlay: $HOST_XDG_DATA_HOME → $XDG_DATA_HOME → ~/.local/share."""
    for var in ("HOST_XDG_DATA_HOME", "XDG_DATA_HOME"):
        v = os.environ.get(var) or ""
        if v:
            return Path(v) / "goverlay"
    return Path.home() / ".local" / "share" / "goverlay"


def gameconfig_dir() -> Path:
    return data_dir() / "gameconfig"


def folders_file() -> Path:
    return config_dir() / FOLDERS_FILE


def cover_dir() -> Path:
    """Cover-Cache für Nicht-Steam-Spiele – fest unter ~/.cache (GOverlay nutzt hier nicht $XDG_CACHE_HOME)."""
    return Path.home() / ".cache" / "goverlay" / "nonsteam_covers"


def goverlay_installed() -> bool:
    """GOverlay wurde mindestens einmal gestartet (Konfigurationsordner vorhanden)."""
    return config_dir().is_dir()


def list_profiles() -> list[str]:
    """Vorhandene Profilordner in gameconfig/ (inklusive ``global``)."""
    try:
        return sorted((p.name for p in gameconfig_dir().iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=str.lower)
    except OSError:
        return []


def _safe_profile_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


# --------------------------------------------------------------------------- A: nonsteam_folders.txt
def normalize_folder(text: str) -> str:
    """Absoluter Pfad ohne abschließenden „/“ ("" für leere Angaben)."""
    t = str(text or "").strip()
    if not t:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(t)))


def _line_key(line: str) -> str:
    t = line.strip()
    return os.path.normpath(t) if t.startswith("/") else t


def configured_roots(settings: Any) -> list[str]:
    """Spiele-Wurzeln aus den Einstellungen: GOG-Spiele-Ordner, weitere Installationsorte, Spiele-Ordner der eigenen
    Bibliotheken – nie „/“ oder der Persönliche Ordner (dann würde jeder Unterordner zur Spielkarte)."""
    raw = [getattr(settings, "install_dir", ""), *getattr(settings, "install_locations", []),
           *(getattr(lib, "games_dir", "") for lib in getattr(settings, "libraries", []))]
    home = normalize_folder(str(Path.home()))
    out: list[str] = []
    for r in raw:
        p = normalize_folder(r)
        if p and p not in out and p not in ("/", home):
            out.append(p)
    return out


@dataclass
class FolderPlan:
    lines: list[str]                     # neuer Dateiinhalt (Zeilen ohne Zeilenumbruch)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    ours: list[str] = field(default_factory=list)   # danach von galaxy-linux eingetragene Ordner

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def plan_folder_lines(lines: list[str], roots: Iterable[str], ours: Iterable[str], enabled: bool = True,
                      exists: Callable[[str], bool] = os.path.isdir) -> FolderPlan:
    """Reine Logik des Abgleichs: fehlende, vorhandene Wurzeln anhängen; eigene, nicht mehr gewünschte Zeilen
    entfernen; alle anderen Zeilen (auch leere und die des Nutzers) bleiben unverändert und in ihrer Reihenfolge.
    Eine Wurzel, die der Nutzer schon selbst eingetragen hat, gilt nicht als eigene (wird also nie entfernt)."""
    target = [t for t in dict.fromkeys(normalize_folder(r) for r in roots) if t] if enabled else []
    ours_keys = [k for k in dict.fromkeys(normalize_folder(o) for o in ours) if k]
    drop = {o for o in ours_keys if o not in target}
    kept, removed = [], []
    for line in lines:
        k = _line_key(line)
        if line.strip() and k in drop:
            if k not in removed:
                removed.append(k)
            continue
        kept.append(line)
    present = {_line_key(x) for x in kept if x.strip()}
    added = [t for t in target if t not in present and exists(t)]
    new_ours = [o for o in ours_keys if o in target and o in present] + added
    return FolderPlan(lines=kept + added, added=added, removed=removed, ours=new_ours)


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except FileNotFoundError:
        return []


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.galaxy-linux.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def sync_folders(settings: Any, enabled: bool | None = None) -> FolderPlan | None:
    """``nonsteam_folders.txt`` abgleichen. ``None``, wenn GOverlay nicht installiert ist. Die Datei wird nur
    geschrieben, wenn sich etwas ändert; ``plan.ours`` gehört danach nach ``settings.goverlay_added_folders``."""
    if not goverlay_installed():
        return None
    if enabled is None:
        enabled = bool(settings.goverlay_register_folders)
    path = folders_file()
    plan = plan_folder_lines(read_lines(path), configured_roots(settings), settings.goverlay_added_folders, enabled)
    if plan.changed:
        _write_atomic(path, "".join(f"{x}\n" for x in plan.lines).encode("utf-8", "surrogateescape"))
        log.info("GOverlay-Ordnerliste %s: eingetragen %s, entfernt %s", path, plan.added or "-", plan.removed or "-")
    return plan


def registered_roots() -> set[str]:
    """Alle Wurzeln, die in nonsteam_folders.txt stehen (eigene und vom Nutzer)."""
    return {_line_key(x) for x in read_lines(folders_file()) if x.strip()}


def folder_registered(install_path: str) -> bool:
    """Erscheint der Spielordner in GOverlay (Elternordner in nonsteam_folders.txt eingetragen)?"""
    folder = Path(normalize_folder(install_path))
    return folder.name.lower() not in SKIP_NAMES and str(folder.parent) in registered_roots()


# --------------------------------------------------------------------------- A: Cover
def _jpeg_bytes(source: Path) -> bytes | None:
    """Echtes JPEG aus dem Cover-Cache: mit Pillow umwandeln (höchstens 600×900), sonst nur kopieren, wenn es schon
    ein JPEG ist."""
    try:
        from PIL import Image  # optional
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(source) as im:
                im = im.convert("RGB")
                im.thumbnail((600, 900))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=90)
                return buf.getvalue()
        except Exception as exc:
            log.debug("Cover %s nicht umwandelbar: %s", source, exc)
    try:
        data = source.read_bytes()
    except OSError:
        return None
    return data if data[:3] == b"\xff\xd8\xff" else None


def cover_path(folder_name: str) -> Path:
    return cover_dir() / f"{goverlay_sanitize(folder_name)}.jpg"


def cover_needed(folder_name: str) -> bool:
    """Kein Cover oder nur GOverlays Platzhalter (``.jpg.fallback``)."""
    dest = cover_path(folder_name)
    marker = dest.with_name(dest.name + ".fallback")
    try:
        return marker.exists() or dest.stat().st_size == 0
    except OSError:
        return True


def seed_cover(folder_name: str, source: Path | None) -> bool:
    """Cover für die GOverlay-Karte ablegen; vorhandene echte Cover bleiben, Platzhalter werden ersetzt.
    Eine ``.fallback``-Datei wird nie angelegt (GOverlay würde das Cover sonst bei jedem Laden ersetzen)."""
    if not cover_needed(folder_name) or source is None or not Path(source).is_file():
        return False
    data = _jpeg_bytes(Path(source))
    if not data:
        return False
    dest = cover_path(folder_name)
    _write_atomic(dest, data)
    marker = dest.with_name(dest.name + ".fallback")
    if marker.exists():
        marker.unlink()                        # Platzhalter ersetzt – GOverlay soll unser Cover behalten
    log.info("GOverlay-Cover angelegt: %s", dest)
    return True


def cached_image(url: str) -> Path | None:
    """Bild aus dem Bild-Cache von galaxy-linux (gleiche Benennung wie ``GogApi.cached_image``), ohne Download."""
    if not url:
        return None
    from .. import paths
    ext = os.path.splitext(url.split("?")[0])[1] or ".img"
    p = paths.IMAGE_CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ext)
    try:
        return p if p.stat().st_size > 0 else None
    except OSError:
        return None


def cached_cover_url(backend: Any, product_id: str) -> str:
    """Hochkant-Cover-URL nur aus vorhandenen Daten (kein Netz): Speicher der Oberfläche, sonst GamesDB-Cache."""
    pid = str(product_id)
    url = (getattr(backend, "_covers", None) or {}).get(pid) or ""
    if url:
        return url
    meta = (getattr(backend, "_meta", None) or {}).get(pid)
    if meta is not None and getattr(meta, "vertical_cover", ""):
        return meta.vertical_cover
    from .. import paths
    from ..api.models import _img
    from ..jsonstore import read_json
    gdb = read_json(paths.API_CACHE_DIR / f"gamesdb-{pid}.json", None)
    if isinstance(gdb, dict):
        return _img(gdb.get("vertical_cover") or (gdb.get("game") or {}).get("vertical_cover"), "_glx_vertical_cover")
    return ""


# --------------------------------------------------------------------------- W: bgmod.conf
@dataclass
class BgmodConf:
    sections: dict[str, dict[str, str]] = field(default_factory=dict)   # Abschnitt (klein) → Schlüssel → Wert
    flags: dict[str, list[str]] = field(default_factory=dict)            # Zeilen ohne Schlüssel=Wert je Abschnitt
    broken_flags: dict[str, list[str]] = field(default_factory=dict)     # „=wert“ (Schlüssel leer, GOverlay-Fehler)

    def get(self, section: str, key: str, default: str = "") -> str:
        sec = self.sections.get(section.lower(), {})
        for k, v in sec.items():
            if k.lower() == key.lower():
                return v
        return default

    def has_flag(self, section: str, name: str) -> bool:
        """``name``, ``=name`` oder ``name=…`` im Abschnitt (bgmod erkennt nur die erste und letzte Form)."""
        s = section.lower()
        return (name in self.flags.get(s, []) or name in self.broken_flags.get(s, [])
                or any(k.lower() == name for k in self.sections.get(s, {})))


def parse_bgmod_conf(text: str) -> BgmodConf:
    """Toleranter INI-Parser für ``bgmod.conf`` (kein configparser: Zeilen wie ``game-performance`` oder
    ``=game-performance`` sind dort gültig bzw. kommen vor)."""
    conf = BgmodConf()
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            conf.sections.setdefault(section, {})
            continue
        if "=" not in line:
            conf.flags.setdefault(section, []).append(line)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            conf.broken_flags.setdefault(section, []).append(value.strip())
            continue
        conf.sections.setdefault(section, {})[key] = value.strip()
    return conf


def read_bgmod_conf(profile_dir: Path) -> BgmodConf:
    try:
        return parse_bgmod_conf((profile_dir / "bgmod.conf").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return BgmodConf()


# --------------------------------------------------------------------------- W: Profil auflösen
@dataclass
class Profile:
    kind: str                    # game | global | none | off
    name: str = ""               # Profilname (Ordner in gameconfig/)
    path: Path | None = None     # Profilordner
    bgmod: Path | None = None    # echter Pfad des Wrappers (auch wenn er erst aus der Vorlage kopiert wird)
    needs_copy: bool = False     # bgmod.conf da, bgmod fehlt → Vorlage kopieren (wie GOverlay beim Klick)
    conf: BgmodConf = field(default_factory=BgmodConf)
    reason: str = ""             # warum kein Profil (für das Protokoll)

    @property
    def active(self) -> bool:
        return self.kind in ("game", "global")


def template_files() -> tuple[Path, Path | None] | None:
    """Vorlage für bgmod (GetBGModPath = <data>/goverlay/bgmod/, sonst aus dem Paket unter /usr/lib)."""
    for d in (data_dir() / "bgmod", *SYSTEM_BGMOD_DIRS):
        b = d / "bgmod"
        if b.is_file():
            u = d / "bgmod-uninstaller"
            return b, (u if u.is_file() else None)
    return None


def _bgmod_state(pdir: Path) -> tuple[str, str]:
    """("ready"|"copy"|"", Grund). ready = ausführbare echte Datei im Profil; copy = bgmod.conf da, Wrapper fehlt
    (oder ist ein Symlink nach außen) und eine Vorlage ist vorhanden."""
    b = pdir / "bgmod"
    real = os.path.realpath(b)
    outside = b.is_symlink() and Path(real).parent != Path(os.path.realpath(pdir))
    if len(os.fsencode(real if not outside else os.path.realpath(pdir) + "/bgmod")) > PROFILE_PATH_MAX:
        return "", "Profilpfad länger als 255 Byte – bgmod fände sein Profil nicht"
    if not outside and b.is_file() and os.access(b, os.X_OK):
        return "ready", ""
    if (pdir / "bgmod.conf").is_file():
        if template_files() is not None:
            return "copy", ""
        return "", "bgmod fehlt im Profil und keine Vorlage gefunden"
    if outside:
        return "", "bgmod im Profil ist ein Symlink nach außen – bgmod fände sein Profil nicht"
    return "", "kein bgmod im Profil"


def _profile_at(kind: str, name: str, pdir: Path) -> Profile:
    state, reason = _bgmod_state(pdir)
    if not state:
        return Profile("none", name, pdir, reason=reason)
    real_dir = Path(os.path.realpath(pdir))
    bgmod = Path(os.path.realpath(pdir / "bgmod")) if state == "ready" else real_dir / "bgmod"
    return Profile(kind, name, pdir, bgmod=bgmod, needs_copy=state == "copy", conf=read_bgmod_conf(pdir))


def auto_profile_name(install_path: str) -> str:
    return goverlay_sanitize(Path(str(install_path)).name)


def resolve_profile(install_path: str, choice: str, settings: Any) -> Profile:
    """Profil eines Spiels: ``choice`` = GameSettings.goverlay_profile ("" automatisch nach Ordnername, "none" aus,
    sonst Profilname – gilt auch, wenn die automatische Zuordnung global abgeschaltet ist). Ohne fertiges Profil und
    mit ``goverlay_global_fallback`` gilt das globale GOverlay-Profil."""
    choice = (choice or "").strip()
    if choice == OFF:
        return Profile("off", reason="für dieses Spiel abgeschaltet")
    explicit = bool(choice)
    if not explicit and not settings.goverlay_use_profiles:
        return Profile("off", reason="GOverlay-Profile pro Spiel abgeschaltet")
    name = choice if explicit else auto_profile_name(install_path)
    if not _safe_profile_name(name):
        return Profile("none", name, reason="ungültiger Profilname")
    pdir = gameconfig_dir() / name
    prof = _profile_at(GLOBAL_PROFILE if name == GLOBAL_PROFILE else "game", name, pdir)
    if prof.active:
        return prof
    if settings.goverlay_use_profiles and settings.goverlay_global_fallback and name != GLOBAL_PROFILE:
        glob = _profile_at(GLOBAL_PROFILE, GLOBAL_PROFILE, gameconfig_dir() / GLOBAL_PROFILE)
        if glob.active and not glob.needs_copy:
            return glob
    return prof


def describe(prof: Profile, registered: bool = True) -> str:
    """Kurztext für Spielseite und Eigenschaften. ``registered`` = Spielordner erscheint in GOverlay."""
    if prof.kind == "game":
        return tr('GOverlay-Profil: {0} (pro Spiel)').format(prof.name)
    if prof.kind == "global":
        return tr('globales GOverlay-Profil')
    if prof.kind == "off":
        return tr('GOverlay-Profil: aus')
    if not registered:
        return tr('kein Profil – Spiele-Ordner ist nicht in GOverlay eingetragen')
    return tr('kein Profil – in GOverlay das Spiel anklicken')


def install_bgmod(pdir: Path) -> Path:
    """Wie GOverlay beim Klick auf die Karte (games_tab.pas:2203-2214): bgmod und bgmod-uninstaller aus der Vorlage
    kopieren (cp -f), chmod 755, ``fgmod -> bgmod``. Gibt den Pfad des kopierten bgmod zurück."""
    src = template_files()
    if src is None:
        raise OSError("keine bgmod-Vorlage gefunden")
    pdir.mkdir(parents=True, exist_ok=True)
    for s, name in ((src[0], "bgmod"), (src[1], "bgmod-uninstaller")):
        if s is None:
            continue
        dest = pdir / name
        tmp = pdir / f".{name}.galaxy-linux.tmp"
        shutil.copyfile(s, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)                  # ersetzt auch einen Symlink, statt ihm zu folgen
    link = pdir / "fgmod"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink("bgmod", link)
    log.info("bgmod aus der Vorlage %s ins GOverlay-Profil %s kopiert", src[0], pdir)
    return Path(os.path.realpath(pdir / "bgmod"))


# --------------------------------------------------------------------------- W: Startbefehl
def is_goverlay_wrapper(token: str) -> bool:
    return os.path.basename(str(token)) in GOVERLAY_WRAPPERS


def drop_goverlay_wrappers(tokens: list[str]) -> tuple[list[str], list[str]]:
    """``bgmod``/``fgmod`` (samt direkt folgendem ``--``) aus einer Wrapper-Liste entfernen → (Rest, entfernt)."""
    kept: list[str] = []
    dropped: list[str] = []
    skip_dashes = False
    for t in tokens:
        if is_goverlay_wrapper(t):
            dropped.append(t)
            skip_dashes = True
            continue
        if skip_dashes and t == "--":
            skip_dashes = False
            continue
        skip_dashes = False
        kept.append(t)
    return kept, dropped


def apply_profile(plan: Any, prof: Profile, is_windows: bool, own_vkbasalt: bool = False,
                  which: Callable[[str], str | None] | None = None) -> list[str]:
    """Profil in den Startbefehl einsetzen (nur ``plan`` ändern, einmal pro Plan). Gibt Hinweise fürs Protokoll zurück.

    ``argv = [*Startoptionen-Wrapper, *gamescope, <Profil>/bgmod, (-- env gamemoderun), *Schalter, umu-run …,
    spiel.exe, *Argumente, (/WineDetectionEnabled:False)]``"""
    notes: list[str] = []
    if not prof.active or prof.bgmod is None:
        return notes
    which = which or shutil.which
    tw = [str(prof.bgmod)]
    if prof.conf.get("config", "gamemode") == "1":
        if which("gamemoderun"):
            tw += ["--", "env", "gamemoderun"]
            if "gamemoderun" in plan.wrappers:
                plan.wrappers = [w for w in plan.wrappers if w != "gamemoderun"]
        else:
            notes.append("gamemode=1 im Profil, aber gamemoderun ist nicht installiert")
    if (GAME_PERFORMANCE in prof.conf.broken_flags.get("env", [])
            and prof.conf.get("config", "GOVERLAY_TWEAKS") == "1" and which(GAME_PERFORMANCE)):
        tw.append(GAME_PERFORMANCE)            # „=game-performance“ übergeht bgmod (GOverlay-Fehler, §4.4)
        notes.append("„=game-performance“ im Profil: game-performance selbst vorangestellt")
    if is_windows and prof.conf.get("config", "winedetectionenable") == "0" \
            and WINE_DETECTION_OFF not in plan.game_args_suffix:
        plan.game_args_suffix.append(WINE_DETECTION_OFF)
    plan.user_wrappers, nested = drop_goverlay_wrappers(list(plan.user_wrappers))
    if nested:
        notes.append(f"GOverlay-Wrapper aus den Startoptionen ersetzt: {', '.join(nested)}")
    plan.tool_wrappers = [*tw, *drop_goverlay_wrappers(list(plan.tool_wrappers))[0]]
    if is_windows and "mangohud" in plan.wrappers:
        plan.wrappers = [w for w in plan.wrappers if w != "mangohud"]
        notes.append("eigener MangoHud-Schalter entfällt – das Profil bestimmt MangoHud")
    if own_vkbasalt and plan.env.pop("ENABLE_VKBASALT", None) is not None:
        notes.append("eigener vkBasalt-Schalter entfällt – das Profil bestimmt vkBasalt")
    return notes


def _configured_env_keys(ctx: Any, inherited: bool = True) -> set[str]:
    """Umgebungsvariablen, die der Nutzer selbst gesetzt hat (Einstellungen, Startoptionen; mit ``inherited`` auch
    die Umgebung, aus der galaxy-linux gestartet wurde)."""
    from . import launch_options
    keys = set(ctx.settings.env) | set(ctx.gs.env)
    if inherited:
        keys |= set(os.environ)
    lib = None
    if ctx.launcher is not None and hasattr(ctx.launcher, "library_of"):
        try:
            lib = ctx.launcher.library_of(ctx.game)
        except Exception:  # pragma: no cover
            lib = None
    for text in (ctx.settings.default_launch_options, ctx.gs.launch_options, lib.launch_options if lib is not None else ""):
        try:
            keys |= set(launch_options.parse(text).env)
        except launch_options.LaunchOptionsError:
            pass
    return keys


def profile_for_context(ctx: Any) -> Profile:
    return resolve_profile(ctx.game.install_path, ctx.gs.goverlay_profile, ctx.settings)


def adjust_context(ctx: Any, prof: Profile | None = None) -> Profile:
    """``STEAM_COMPAT_INSTALL_PATH`` immer setzen (auch für umu – bgmod findet damit den Spielordner) und ein
    fertiges Profil einsetzen. Muss bgmod erst kopiert werden, passiert das in ``prepare``; die Vorschau zeigt es
    schon. Nur ``ctx.plan`` wird geändert; Hinweise landen in ``ctx.data`` und werden in ``prepare`` protokolliert."""
    plan = ctx.plan
    if "STEAM_COMPAT_INSTALL_PATH" not in _configured_env_keys(ctx, inherited=False):
        plan.env["STEAM_COMPAT_INSTALL_PATH"] = str(ctx.install_dir)
    prof = prof or profile_for_context(ctx)
    ctx.data["goverlay"] = prof
    if prof.active and (not prof.needs_copy or ctx.preview) and not ctx.data.get("goverlay_applied"):
        own_vk = bool(ctx.gs.use_vkbasalt) and "ENABLE_VKBASALT" not in _configured_env_keys(ctx)
        ctx.data["goverlay_notes"] = apply_profile(plan, prof, ctx.is_windows, own_vkbasalt=own_vk)
        ctx.data["goverlay_applied"] = True
    return prof


def prepare_context(ctx: Any) -> None:
    """Fehlt bgmod im Profil (bgmod.conf da), wird es wie von GOverlay aus der Vorlage kopiert und eingesetzt;
    danach Profil und Hinweise ins Protokoll."""
    prof = ctx.data.get("goverlay")
    if not isinstance(prof, Profile) or ctx.plan is None:
        return
    if prof.active and prof.needs_copy:
        ctx.status(tr('GOverlay-Profil „{0}“ wird eingerichtet …').format(prof.name))
        try:
            install_bgmod(prof.path)
        except OSError as exc:
            log.warning("bgmod konnte nicht ins GOverlay-Profil %s kopiert werden: %s", prof.path, exc)
            ctx.log_line(f"--- GOverlay: bgmod nicht kopiert: {exc}")
            return
        prof = adjust_context(ctx, profile_for_context(ctx))
    if prof.active and ctx.data.get("goverlay_applied"):
        notes = ctx.data.get("goverlay_notes") or []
        log.info("GOverlay-Profil %s (%s) für %s: %s", prof.name, prof.bgmod, ctx.product_id, "; ".join(notes) or "ok")
        ctx.log_line(f"--- GOverlay-Profil {prof.name}: {prof.bgmod}")
        for n in notes:
            ctx.log_line(f"--- GOverlay: {n}")
    elif prof.kind == "none" and prof.reason:
        log.debug("Kein GOverlay-Profil für %s (%s): %s", ctx.product_id, prof.name, prof.reason)


# --------------------------------------------------------------------------- bgmod-uninstaller (§5.5)
def _within(path: str, root: Path) -> bool:
    try:
        p, r = Path(path).resolve(), root.resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def uninstall_candidates(install_path: str, choice: str) -> list[Path]:
    """Profile, deren bgmod nachweislich in diesem Spielordner gearbeitet hat (``GAME_DIR`` aus bgmod.conf,
    von bgmod zurückgeschrieben) und die einen bgmod-uninstaller haben – das globale Profil nie (geteilte Backups)."""
    names = [n for n in dict.fromkeys([(choice or "").strip(), auto_profile_name(install_path)])
             if n and n not in (OFF, GLOBAL_PROFILE) and _safe_profile_name(n)]
    out = []
    for n in names:
        pdir = gameconfig_dir() / n
        exe = pdir / "bgmod-uninstaller"
        if not (exe.is_file() and os.access(exe, os.X_OK)):
            continue
        game_dir = read_bgmod_conf(pdir).get("config", "GAME_DIR")
        if game_dir and _within(game_dir, Path(install_path)):
            out.append(pdir)
    return out


def run_uninstaller(pdir: Path, install_path: str, log_file: Path | None = None, timeout: float = 120) -> int:
    """``BGMOD_CONFIG_DIR=<Profil>/ BGMOD_BACKUPS_DIR=<Profil>/backups STEAM_COMPAT_INSTALL_PATH=<Spiel>
    <Profil>/bgmod-uninstaller --`` (wie GOverlay)."""
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    env.update(BGMOD_CONFIG_DIR=f"{pdir}/", BGMOD_BACKUPS_DIR=str(pdir / "backups"), STEAM_COMPAT_INSTALL_PATH=str(install_path))
    cwd = install_path if os.path.isdir(install_path) else None
    out: Any = open(log_file, "ab") if log_file else subprocess.DEVNULL
    try:
        if log_file:
            out.write(f"--- GOverlay: bgmod-uninstaller ({pdir.name})\n".encode())
            out.flush()
        rc = subprocess.run([str(pdir / "bgmod-uninstaller"), "--"], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("bgmod-uninstaller (%s) fehlgeschlagen: %s", pdir, exc)
        return -1
    finally:
        if log_file:
            out.close()
    log.info("bgmod-uninstaller (%s) für %s beendet mit %s", pdir.name, install_path, rc)
    return rc


def undo_bgmod_changes(backend: Any, product_id: str, action: str, status: Callable[[str], None] | None = None) -> int:
    """Rückruf vor Prüfen/Reparieren, Aktualisieren, Verschieben und Deinstallieren (Backend.before_files_change)."""
    g = backend.registry.get(str(product_id))
    if g is None or not os.path.isdir(g.install_path):
        return 0
    gs = backend.game_settings.get(str(product_id))
    n = 0
    for pdir in uninstall_candidates(g.install_path, gs.goverlay_profile):
        if status:
            status(tr('GOverlay-Änderungen werden zurückgenommen …'))
        from .. import paths
        run_uninstaller(pdir, g.install_path, paths.game_log_file(str(product_id)))
        n += 1
    if n:
        log.info("GOverlay-Änderungen vor „%s“ zurückgenommen: %s", action, g.title)
    return n


# --------------------------------------------------------------------------- Abgleich (Ordner + Cover)
@dataclass
class SyncReport:
    installed: bool = True
    folders: FolderPlan | None = None
    covers: int = 0


def cover_source(backend: Any, product_id: str) -> Path | None:
    try:
        return cached_image(cached_cover_url(backend, product_id))
    except Exception as exc:
        log.debug("Cover für %s nicht ermittelbar: %s", product_id, exc)
        return None


def sync_all(backend: Any, covers: bool = True) -> SyncReport:
    """Ordnerliste abgleichen (und ``goverlay_added_folders`` speichern), danach Cover der Spiele in eingetragenen
    Wurzeln vorbelegen (nur aus dem Bild-Cache, kein Netz). Läuft im Hintergrund oder auf der Kommandozeile – nie im
    GTK-Hauptthread."""
    with _sync_lock:
        return _sync_all(backend, covers)


_sync_lock = threading.Lock()


def _sync_all(backend: Any, covers: bool) -> SyncReport:
    s = backend.settings
    if not goverlay_installed():
        return SyncReport(installed=False)
    rep = SyncReport(folders=sync_folders(s))
    if rep.folders is not None and rep.folders.ours != list(s.goverlay_added_folders):
        s.goverlay_added_folders = list(rep.folders.ours)
        s.save()
    if covers and s.goverlay_register_folders:
        roots = registered_roots()
        for g in backend.registry.all():
            folder = Path(normalize_folder(g.install_path))
            if str(folder.parent) not in roots or folder.name.lower() in SKIP_NAMES or not cover_needed(folder.name):
                continue
            try:
                rep.covers += int(seed_cover(folder.name, cover_source(backend, g.product_id)))
            except OSError as exc:
                log.warning("GOverlay-Cover für %s nicht geschrieben: %s", g.title, exc)
    return rep


class AutoSync:
    """Hält nonsteam_folders.txt und die Cover im Hintergrund aktuell – nur in der Oberfläche angelegt (Start,
    Timer, fertige Aufträge). ``request()`` läuft nur, wenn sich Ordner, Spiele oder Schalter geändert haben (oder
    ``force``); mehrere Anfragen während eines Laufs ergeben einen Nachlauf."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._running = False
        self._again = False
        self.runs = 0

    def signature(self) -> tuple:
        s = self.backend.settings
        games = tuple(sorted(normalize_folder(g.install_path) for g in self.backend.registry.all()))
        return goverlay_installed(), bool(s.goverlay_register_folders), tuple(configured_roots(s)), games

    def request(self, force: bool = False) -> bool:
        sig = self.signature()
        with self._lock:
            if sig == self._sig and not force:
                return False
            if self._running:
                self._again = True
                return True
            self._running = True
        threading.Thread(target=self._run, name="gl-goverlay", daemon=True).start()
        return True

    def _run(self) -> None:
        while True:
            with self._lock:
                self._again = False
            try:
                sig = self.signature()
                sync_all(self.backend, covers=True)
                with self._lock:
                    self._sig = sig
            except Exception:
                log.exception("GOverlay-Abgleich fehlgeschlagen")
            with self._lock:
                self.runs += 1
                if not self._again:
                    self._running = False
                    return


_auto: dict[int, AutoSync] = {}
_auto_lock = threading.Lock()


def auto_sync(backend: Any, create: bool = True) -> AutoSync | None:
    """AutoSync des Backends (die Oberfläche legt ihn an; ohne ``create`` nur einen vorhandenen)."""
    with _auto_lock:
        a = _auto.get(id(backend))
        if a is None or a.backend is not backend:
            if not create:
                return None
            a = _auto[id(backend)] = AutoSync(backend)
        return a


# --------------------------------------------------------------------------- Anzeige
def profile_for_game(backend: Any, product_id: str) -> Profile | None:
    g = backend.registry.get(str(product_id))
    if g is None:
        return None
    gs = backend.game_settings.get(str(product_id))
    return resolve_profile(g.install_path, gs.goverlay_profile, backend.settings)


def status_for(backend: Any, product_id: str) -> str | None:
    """Text für die Spielseite (None: GOverlay nicht installiert oder Spiel nicht installiert)."""
    if not goverlay_installed():
        return None
    g = backend.registry.get(str(product_id))
    prof = profile_for_game(backend, product_id)
    if g is None or prof is None:
        return None
    return describe(prof, registered=folder_registered(g.install_path))
