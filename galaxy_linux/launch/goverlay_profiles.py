"""GOverlay 1.9.4 pro Spiel: Spiele in GOverlay anzeigen, Cover vorbelegen, Profil-``bgmod`` beim Start voranstellen.

Grundlage: reference/overhaul-2026-10-22/goverlay.md §3–§7 („Rang 1 – A + W“).

* **A – Sichtbarkeit (ab 0.3.1 eigene Spieleansicht):** GOverlay kennt außer Steam nur die Ordnerliste
  ``${XDG_CONFIG_HOME:-~/.config}/goverlay/nonsteam_folders.txt``; jeder *direkte* Eintrag eines eingetragenen Ordners
  wird eine Spielkarte (Name = Eintrag). GOverlays ``FindFirst`` folgt Symlinks – deshalb legt galaxy-linux einen
  eigenen Ordner ``~/.local/share/galaxy-linux/goverlay-library/`` an, mit genau einem Symlink pro installiertem Spiel
  (GOG und extern, auch Linux-Spiele), benannt nach dem Spieltitel, Ziel = Spielordner. Kopien (gleicher Inhalt wie ein
  anderes externes Spiel) und ausgeblendete Spiele fehlen. Nur dieser Ordner steht in ``nonsteam_folders.txt``; früher
  eingetragene Bibliotheks-Wurzeln (``GlobalSettings.goverlay_added_folders``) werden entfernt, Zeilen des Nutzers
  bleiben unangetastet.
  Cover: ``~/.cache/goverlay/nonsteam_covers/<SanitizeFileName(Kartenname)>.jpg`` als echtes JPEG aus dem Cover von
  galaxy-linux (ohne Cover: dunkle Titelkarte), damit GOverlay nie bei Steam/Bing nach dem Namen sucht. Ein Verzeichnis
  der selbst geschriebenen Cover (Name → SHA-1) unterscheidet sie von einem Cover, das der Nutzer später in GOverlay
  setzt – das bleibt, solange es kein GOverlay-Platzhalter ist (``.fallback`` bzw. 150×215).
* **W – Anwendung:** Klickt man das Spiel in GOverlay an, entsteht ``${XDG_DATA_HOME:-~/.local/share}/goverlay/
  gameconfig/<SanitizeFileName(Kartenname)>/`` mit ``bgmod``. Gesucht wird: Profil aus den Eigenschaften →
  Kartenname → Name des Spielordners (Profile aus 0.3.0). Das globale Profil gilt nur auf ausdrücklichen Wunsch.
  Dieses ``bgmod`` (echte Datei, kein Symlink – bgmod erkennt sein Profil über ``/proc/self/exe``) kommt beim Start in
  ``LaunchPlan.tool_wrappers``. ``bgmod.conf`` ``[Config] gamemode=1`` → ``-- env gamemoderun`` dahinter,
  ``winedetectionenable=0`` → ``/WineDetectionEnabled:False`` ans Ende (beides liest bgmod nicht selbst). Eigene
  MangoHud-/vkBasalt-Schalter entfallen dann (außer MangoHud bei Linux-Spielen: bgmod setzt kein LD_PRELOAD, der
  ``mangohud``-Wrapper beachtet das ``MANGOHUD_CONFIGFILE`` des Profils).
* Vor Prüfen/Reparieren, Aktualisieren, Verschieben und Deinstallieren nimmt ``bgmod-uninstaller`` die Änderungen von
  bgmod im Spielordner zurück (wie GOverlays „Uninstall changes“, §5.5).

Alle Pfade werden bei jedem Aufruf aus der Umgebung gelesen (Tests setzen HOME/XDG_* auf Temp-Ordner).
"""
from __future__ import annotations

from ..i18n import tr

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

FOLDERS_FILE = "nonsteam_folders.txt"
SKIP_NAMES = ("prefixes", "common", "compatdata", "shadercache", "downloads", "tmp", "temp")   # überspringt GOverlay
VIEW_DIR = "goverlay-library"        # Spieleansicht im Datenordner von galaxy-linux
VIEW_STATE = "goverlay-library.json" # Produkt-ID → Kartenname (für die Profilsuche beim Start)
COVER_MANIFEST = "goverlay-covers.json"   # Cover-Schlüssel → SHA-1 der von galaxy-linux geschriebenen Datei
NAME_MAX = 120
PLACEHOLDER_SIZE = (150, 215)        # GOverlays Platzhalter-Cover (CARD_W × CARD_H)
COVER_SIZE = (600, 900)
GLOBAL_PROFILE = "global"
OFF = "none"                         # GameSettings.goverlay_profile: Profil aus
PROFILE_PATH_MAX = 255               # bgmod: ParamStr(0) ist ein FPC-shortstring (255 Byte)
SYSTEM_BGMOD_DIRS = [Path("/usr/lib")]   # Paket goverlay: /usr/lib/bgmod, /usr/lib/bgmod-uninstaller
GAME_PERFORMANCE = "game-performance"
WINE_DETECTION_OFF = "/WineDetectionEnabled:False"
GOVERLAY_WRAPPERS = ("bgmod", "fgmod")
_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- .+()[]")
FONTS = ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/noto/NotoSans-Bold.ttf", "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/TTF/LiberationSans-Bold.ttf")


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


# --------------------------------------------------------------------------- A: Spieleansicht (Symlinks)
def view_dir() -> Path:
    """Ordner mit einem Symlink pro Spiel – der einzige Eintrag von galaxy-linux in nonsteam_folders.txt."""
    from .. import paths
    return paths.DATA_DIR / VIEW_DIR


def _state_file() -> Path:
    from .. import paths
    return paths.DATA_DIR / VIEW_STATE


def normalize_folder(text: str) -> str:
    """Absoluter Pfad ohne abschließenden „/“ ("" für leere Angaben)."""
    t = str(text or "").strip()
    if not t:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(t)))


def card_name(title: str) -> str:
    """Kartenname (= Name des Symlinks) aus dem Spieltitel: ohne ™/®/©, „/“ ersetzt, ohne Steuerzeichen und
    führenden Punkt, Leerzeichen zusammengefasst, höchstens 120 Zeichen."""
    t = re.sub(r"[™®©]", "", str(title or ""))
    t = t.replace("/", "-").replace("\\", "-")
    t = "".join(ch for ch in t if ch >= " " and ch != "\x7f")
    t = " ".join(t.split()).lstrip(".").strip()
    if len(t) > NAME_MAX:
        t = t[:NAME_MAX].rstrip(" .-")
    return t or "game"


def _key(name: str) -> str:
    """Kollisionsschlüssel: gleicher Profil- und Cover-Name in GOverlay."""
    return goverlay_sanitize(name)


@dataclass
class ViewGame:
    pid: str
    title: str                 # Anzeigetitel (eigener Titel, sonst Spieltitel)
    target: str                # Spielordner (Ziel des Symlinks)
    custom_cover: str = ""     # eigenes Cover („Bearbeiten …“)
    name: str = ""             # vergebener Kartenname


def view_games(backend: Any) -> list[ViewGame]:
    """Spiele, die in GOverlay erscheinen sollen: installiert, Ordner vorhanden, keine Kopie, nicht ausgeblendet."""
    try:
        all_gs = backend.game_settings.items()
    except AttributeError:
        all_gs = {}
    out = []
    for g in backend.registry.all():
        if getattr(g, "duplicate_of", ""):
            continue
        gs = all_gs.get(g.product_id)
        if gs is not None and gs.hidden:
            continue
        target = normalize_folder(g.install_path)
        if not target or not os.path.isdir(target):
            continue
        title = (gs.custom_title if gs is not None else "") or g.title
        out.append(ViewGame(g.product_id, title, target, gs.custom_cover if gs is not None else ""))
    return out


def plan_view(games: list[ViewGame], existing: dict[str, str] | None = None,
              reserved: Iterable[str] = ()) -> list[ViewGame]:
    """Kartennamen vergeben. Jeder Titel ergibt seinen Namen; nur wenn zwei *verschiedene* Spiele denselben Namen
    (bzw. denselben Profil-/Cover-Namen in GOverlay) bekämen, erhält das spätere „ (2)“, „ (3)“ … Ein vorhandener
    Link, der schon auf das Spiel zeigt und zu seinem Titel passt, behält seinen Namen (Profile bleiben gültig).
    ``existing``: Name → Ziel der vorhandenen Links; ``reserved``: Namen fremder Dateien im Ordner."""
    existing = existing or {}
    used: set[str] = {_key(n) for n in reserved}
    by_target: dict[str, list[str]] = {}
    for name, target in sorted(existing.items()):
        by_target.setdefault(normalize_folder(target), []).append(name)
    ordered = sorted(games, key=lambda x: (card_name(x.title).lower(), x.pid))
    pending = []
    for g in ordered:
        base = card_name(g.title)
        pat = re.compile(re.escape(base) + r" \(\d+\)")
        keep = next((n for n in by_target.get(g.target, []) if (n == base or pat.fullmatch(n)) and _key(n) not in used
                     and n.lower() not in SKIP_NAMES), None)
        if keep:
            g.name = keep
            used.add(_key(keep))
        else:
            pending.append(g)
    for g in pending:
        base = card_name(g.title)
        name, n = base, 2
        while _key(name) in used or name.lower() in SKIP_NAMES:
            name, n = f"{base} ({n})", n + 1
        g.name = name
        used.add(_key(name))
    return sorted(ordered, key=lambda x: x.name.lower())


def current_links(root: Path | None = None) -> tuple[dict[str, str], list[str]]:
    """(Name → Ziel der Symlinks, Namen anderer Einträge) im Ansichtsordner."""
    root = root or view_dir()
    links: dict[str, str] = {}
    other: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return links, other
    for e in entries:
        if e.is_symlink():
            try:
                links[e.name] = os.readlink(e.path)
            except OSError:
                continue
        else:
            other.append(e.name)
    return links, other


@dataclass
class ViewReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    games: list[ViewGame] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def apply_view(plan: list[ViewGame], root: Path | None = None) -> ViewReport:
    """Symlinks wie geplant anlegen; veraltete, falsche und kaputte Links entfernen – nur Symlinks und nur in diesem
    Ordner, andere Dateien bleiben."""
    root = root or view_dir()
    root.mkdir(parents=True, exist_ok=True)
    want = {g.name: g.target for g in plan}
    rep = ViewReport(games=list(plan))
    links, _other = current_links(root)
    for name, target in links.items():
        path = root / name
        if name not in want or normalize_folder(target) != want[name] or not os.path.exists(path):
            try:
                os.unlink(path)
                rep.removed.append(name)
            except OSError as exc:
                log.warning("Veralteter Link %s nicht entfernt: %s", path, exc)
    for name, target in want.items():
        path = root / name
        if path.is_symlink():
            continue
        if path.exists():
            log.warning("%s ist keine Verknüpfung von galaxy-linux – bleibt unverändert", path)
            continue
        try:
            os.symlink(target, path)
            rep.added.append(name)
        except OSError as exc:
            log.warning("Verknüpfung %s → %s nicht angelegt: %s", path, target, exc)
    _write_state({g.pid: g.name for g in plan})
    if rep.changed:
        log.info("GOverlay-Spieleansicht %s: neu %s, entfernt %s", root, rep.added or "-", rep.removed or "-")
    return rep


def clear_view(root: Path | None = None) -> ViewReport:
    """Alle Symlinks der Ansicht entfernen (Schalter „Spiele in GOverlay anzeigen“ aus)."""
    root = root or view_dir()
    rep = ViewReport()
    links, _other = current_links(root)
    for name in links:
        try:
            os.unlink(root / name)
            rep.removed.append(name)
        except OSError:
            pass
    _write_state({})
    return rep


def _write_state(state: dict[str, str]) -> None:
    f = _state_file()
    try:
        if f.exists() and json.loads(f.read_text(encoding="utf-8")) == state:
            return
    except (OSError, ValueError):
        pass
    try:
        _write_atomic(f, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    except OSError as exc:
        log.debug("Zuordnung der GOverlay-Karten nicht gespeichert: %s", exc)


def read_state() -> dict[str, str]:
    try:
        d = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}


def view_name(game: Any, gs: Any = None) -> str:
    """Kartenname eines Spiels in GOverlay: wie zuletzt vergeben, sonst aus dem Titel."""
    name = read_state().get(str(game.product_id))
    if name:
        return name
    return card_name((getattr(gs, "custom_title", "") if gs is not None else "") or game.title)


def in_view(product_id: str) -> bool:
    """Erscheint das Spiel in GOverlay (Link vorhanden, Ansichtsordner in nonsteam_folders.txt)?"""
    name = read_state().get(str(product_id))
    return bool(name) and (view_dir() / name).is_symlink() and normalize_folder(str(view_dir())) in registered_roots()


def dry_run(backend: Any) -> list[ViewGame]:
    """Geplante Spielkarten, ohne etwas zu schreiben (auch nicht den Ansichtsordner)."""
    links, other = current_links()
    return plan_view(view_games(backend), links, other)


# --------------------------------------------------------------------------- A: nonsteam_folders.txt
def _line_key(line: str) -> str:
    t = line.strip()
    return os.path.normpath(t) if t.startswith("/") else t


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
    """Reine Logik des Abgleichs: fehlende, vorhandene Ordner anhängen; eigene, nicht mehr gewünschte Zeilen
    entfernen (z. B. die Bibliotheks-Wurzeln aus 0.3.0); alle anderen Zeilen (auch leere und die des Nutzers) bleiben
    unverändert und in ihrer Reihenfolge. Ein Ordner, den der Nutzer schon selbst eingetragen hat, gilt nicht als
    eigener (wird also nie entfernt)."""
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
    """Atomar ersetzen. Ist ``path`` ein Symlink (z. B. von stow/chezmoi in ein Dotfile-Repository), wird das Ziel
    ersetzt und der Link bleibt erhalten."""
    path = Path(os.path.realpath(path))
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
    """``nonsteam_folders.txt`` abgleichen: nur der Ansichtsordner gehört galaxy-linux. ``None``, wenn GOverlay nicht
    installiert ist. Die Datei wird nur geschrieben, wenn sich etwas ändert; ``plan.ours`` gehört danach nach
    ``settings.goverlay_added_folders``."""
    if not goverlay_installed():
        return None
    if enabled is None:
        enabled = bool(settings.goverlay_register_folders)
    path = folders_file()
    roots = [str(view_dir())]
    lines = read_lines(path)
    plan = plan_folder_lines(lines, roots, settings.goverlay_added_folders, enabled)
    if plan.changed:
        current = read_lines(path)
        if current != lines:            # inzwischen von GOverlay geändert: auf dem neuen Stand planen
            plan = plan_folder_lines(current, roots, settings.goverlay_added_folders, enabled)
    if plan.changed:
        _write_atomic(path, "".join(f"{x}\n" for x in plan.lines).encode("utf-8", "surrogateescape"))
        log.info("GOverlay-Ordnerliste %s: eingetragen %s, entfernt %s", path, plan.added or "-", plan.removed or "-")
    return plan


def registered_roots() -> set[str]:
    """Alle Ordner, die in nonsteam_folders.txt stehen (eigene und vom Nutzer)."""
    return {_line_key(x) for x in read_lines(folders_file()) if x.strip()}


# --------------------------------------------------------------------------- A: Cover
def _jpeg_bytes(source: Path) -> bytes | None:
    """Echtes JPEG aus einer Bilddatei: mit Pillow umwandeln (höchstens 600×900), sonst nur kopieren, wenn es schon
    ein JPEG ist."""
    try:
        from PIL import Image  # optional
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(source) as im:
                im = im.convert("RGB")
                im.thumbnail(COVER_SIZE)
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


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Breite × Höhe eines JPEG aus dem SOF-Segment (ohne Pillow)."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and i + 9 <= len(data):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if seg < 2:
            return None
        i += 2 + seg
    return None


def is_placeholder(data: bytes) -> bool:
    """GOverlays eigenes Ersatzbild (150×215 mit GOverlay-Symbol)."""
    return jpeg_size(data) == PLACEHOLDER_SIZE


def _font(size: int):
    from PIL import ImageFont
    for f in FONTS:
        if os.path.isfile(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # Pillow < 10.1
        return ImageFont.load_default()


def title_card(title: str) -> bytes | None:
    """Dunkle Titelkarte (600×900, JPEG) für Spiele ohne Cover; ``None`` ohne Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    w, h = COVER_SIZE
    im = Image.new("RGB", (w, h), (32, 36, 44))
    draw = ImageDraw.Draw(im)
    words = (title or "").split() or ["?"]
    for size in (64, 56, 48, 40, 34, 28):
        font = _font(size)
        lines, cur = [], ""
        for word in words:
            test = f"{cur} {word}".strip()
            if cur and draw.textlength(test, font=font) > w - 90:
                lines.append(cur)
                cur = word
            else:
                cur = test
        lines.append(cur)
        if len(lines) <= 7 and all(draw.textlength(x, font=font) <= w - 60 for x in lines):
            break
    line_h = int(size * 1.25)
    y = (h - line_h * len(lines)) // 2 - 20
    for line in lines:
        draw.text(((w - draw.textlength(line, font=font)) / 2, y), line, fill=(236, 236, 240), font=font)
        y += line_h
    draw.rectangle([w // 2 - 60, y + 24, w // 2 + 60, y + 30], fill=(138, 92, 200))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def cover_path(name: str) -> Path:
    return cover_dir() / f"{goverlay_sanitize(name)}.jpg"


def cached_image(url: str) -> Path | None:
    """Bild aus dem Bild-Cache von galaxy-linux (gleiche Benennung wie ``GogApi.cached_image``), ohne Download;
    ``file://``-Adressen (eigenes Cover) direkt."""
    if not url:
        return None
    if url.startswith("file://"):
        p = Path(url[7:])
        return p if p.is_file() else None
    from .. import paths
    ext = os.path.splitext(url.split("?")[0])[1] or ".img"
    p = paths.IMAGE_CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ext)
    try:
        return p if p.stat().st_size > 0 else None
    except OSError:
        return None


def cached_cover_url(backend: Any, product_id: str) -> str:
    """Hochkant-Cover-URL nur aus vorhandenen Daten (kein Netz): Erkennung externer Spiele, Speicher der Oberfläche,
    sonst GamesDB-Cache."""
    pid = str(product_id)
    g = backend.registry.get(pid) if hasattr(backend, "registry") else None
    if g is not None and getattr(g, "cover_url", ""):
        return g.cover_url
    url = (getattr(backend, "_covers", None) or {}).get(pid) or ""
    if url:
        return url
    meta = (getattr(backend, "_meta", None) or {}).get(pid)
    if meta is not None and getattr(meta, "vertical_cover", ""):
        return meta.vertical_cover
    if g is not None and getattr(g, "gog_match", ""):
        pid = g.gog_match
    from .. import paths
    from ..api.models import _img
    from ..jsonstore import read_json
    gdb = read_json(paths.API_CACHE_DIR / f"gamesdb-{pid}.json", None)
    if isinstance(gdb, dict):
        return _img(gdb.get("vertical_cover") or (gdb.get("game") or {}).get("vertical_cover"), "_glx_vertical_cover")
    return ""


def cover_source(backend: Any, game: ViewGame) -> Path | None:
    """Bilddatei für die Karte: eigenes Cover, sonst das Cover aus dem Bild-Cache (None: keins vorhanden)."""
    if game.custom_cover and os.path.isfile(game.custom_cover):
        return Path(game.custom_cover)
    try:
        return cached_image(cached_cover_url(backend, game.pid))
    except Exception as exc:
        log.debug("Cover für %s nicht ermittelbar: %s", game.pid, exc)
        return None


def _manifest_file() -> Path:
    from .. import paths
    return paths.DATA_DIR / COVER_MANIFEST


def read_manifest() -> dict[str, dict]:
    try:
        d = json.loads(_manifest_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def cover_decision(current: bytes | None, fallback_marker: bool, entry: dict | None, source_sig: str) -> str:
    """Was mit einem vorhandenen GOverlay-Cover geschieht: ``write`` (fehlt, Platzhalter, nicht von galaxy-linux,
    von galaxy-linux aber Quelle geändert), ``keep`` (vom Nutzer in GOverlay gesetzt) oder ``same`` (aktuell)."""
    if not current:
        return "write"
    if fallback_marker or is_placeholder(current):
        return "write"
    if entry is None:
        return "write"                     # GOverlays Steam/Bing-Treffer oder Cover aus 0.3.0
    if _sha1(current) != entry.get("sha1"):
        return "keep"                      # inzwischen in GOverlay geändert: Wahl des Nutzers
    return "same" if entry.get("src") == source_sig else "write"


def sync_covers(backend: Any, games: list[ViewGame]) -> int:
    """Cover aller Karten schreiben bzw. aktuell halten (nur aus dem Bild-Cache, kein Netz). Gibt die Zahl der
    geschriebenen Cover zurück. Eigene, nicht mehr gebrauchte Cover werden entfernt, fremde bleiben."""
    manifest = read_manifest()
    before = json.dumps(manifest, sort_keys=True)
    written = 0
    keys = set()
    for g in games:
        key = goverlay_sanitize(g.name)
        keys.add(key)
        dest = cover_dir() / f"{key}.jpg"
        marker = dest.with_name(dest.name + ".fallback")
        src = cover_source(backend, g)
        if src is not None:
            try:
                st = src.stat()
                sig = f"{src}|{st.st_mtime_ns}|{st.st_size}"
            except OSError:
                src, sig = None, ""
        if src is None:
            sig = f"card:{g.name}"
        try:
            current = dest.read_bytes()
        except OSError:
            current = None
        decision = cover_decision(current, marker.exists(), manifest.get(key), sig)
        if decision != "write":
            continue
        data = _jpeg_bytes(src) if src is not None else title_card(g.name)
        if data is None and src is not None:
            data, sig = title_card(g.name), f"card:{g.name}"
            if current and manifest.get(key, {}).get("src") == sig and _sha1(current) == manifest[key].get("sha1"):
                continue
        if not data:
            continue
        try:
            _write_atomic(dest, data)
            if marker.exists():
                marker.unlink()                # Platzhalter ersetzt – GOverlay soll unser Cover behalten
        except OSError as exc:
            log.warning("GOverlay-Cover %s nicht geschrieben: %s", dest, exc)
            continue
        manifest[key] = {"sha1": _sha1(data), "src": sig}
        written += 1
    for key in [k for k in manifest if k not in keys]:
        dest = cover_dir() / f"{key}.jpg"
        try:
            if _sha1(dest.read_bytes()) == manifest[key].get("sha1"):
                dest.unlink()                  # eigenes Cover einer verschwundenen Karte
        except OSError:
            pass
        del manifest[key]
    if json.dumps(manifest, sort_keys=True) != before:
        try:
            _write_atomic(_manifest_file(), json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode())
        except OSError as exc:
            log.debug("Cover-Verzeichnis nicht gespeichert: %s", exc)
    if written:
        log.info("GOverlay-Cover geschrieben: %d", written)
    return written


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
    from_options: bool = False   # Profil stammt aus einem bgmod-Pfad in den Startoptionen

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
    """Profilname nach dem Spielordner (so hieß die GOverlay-Karte bis 0.3.0)."""
    return goverlay_sanitize(Path(str(install_path)).name)


def auto_profile_names(game: Any, gs: Any = None) -> list[str]:
    """Automatische Profilnamen in Suchreihenfolge: Kartenname in GOverlay, danach der Spielordner (0.3.0)."""
    return [n for n in dict.fromkeys((goverlay_sanitize(view_name(game, gs)), auto_profile_name(game.install_path))) if n]


def resolve_profile(install_path: str, choice: str, settings: Any, names: Iterable[str] | None = None) -> Profile:
    """Profil eines Spiels: ``choice`` = GameSettings.goverlay_profile ("" automatisch, "none" aus, sonst Profilname –
    gilt auch, wenn die automatische Zuordnung global abgeschaltet ist). Automatisch: das erste fertige Profil unter
    ``names`` (Kartenname, dann Spielordner). Das globale GOverlay-Profil gilt nur ausdrücklich oder mit
    ``goverlay_global_fallback`` (Vorgabe aus)."""
    choice = (choice or "").strip()
    if choice == OFF:
        return Profile("off", reason="für dieses Spiel abgeschaltet")
    explicit = bool(choice)
    if not explicit and not settings.goverlay_use_profiles:
        return Profile("off", reason="GOverlay-Profile pro Spiel abgeschaltet")
    candidates = [choice] if explicit else [n for n in dict.fromkeys(names or [auto_profile_name(install_path)]) if n]
    first: Profile | None = None
    for name in candidates:
        if not _safe_profile_name(name):
            prof = Profile("none", name, reason="ungültiger Profilname")
        else:
            prof = _profile_at(GLOBAL_PROFILE if name == GLOBAL_PROFILE else "game", name, gameconfig_dir() / name)
        if prof.active:
            return prof
        first = first or prof
    prof = first or Profile("none", reason="kein Profilname")
    if settings.goverlay_use_profiles and settings.goverlay_global_fallback and GLOBAL_PROFILE not in candidates:
        glob = _profile_at(GLOBAL_PROFILE, GLOBAL_PROFILE, gameconfig_dir() / GLOBAL_PROFILE)
        if glob.active and not glob.needs_copy:
            return glob
    return prof


def describe(prof: Profile, registered: bool = True) -> str:
    """Kurztext für Spielseite und Eigenschaften. ``registered`` = Spiel erscheint in GOverlay."""
    if prof.from_options and prof.active:
        name = tr('globales GOverlay-Profil') if prof.kind == "global" else prof.name
        return tr('GOverlay-Profil: {0} (aus den Startoptionen, wird einmal vorangestellt)').format(name)
    if prof.kind == "game":
        return tr('GOverlay-Profil: {0} (pro Spiel)').format(prof.name)
    if prof.kind == "global":
        return tr('globales GOverlay-Profil')
    if prof.kind == "off":
        return tr('GOverlay-Profil: aus')
    if not registered:
        return tr('kein Profil – das Spiel erscheint nicht in GOverlay (Einstellungen → Kompatibilität → GOverlay)')
    return tr('kein Profil – Spiel in GOverlay anklicken, um sein Profil anzulegen')


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


def _wrapper_tail(tokens: list[str], i: int) -> int:
    """Index hinter ``bgmod`` samt GOverlays Anhang ``-- env gamemoderun`` (bzw. nur ``--``)."""
    if tokens[i:i + 4] == [tokens[i], "--", "env", "gamemoderun"]:
        return i + 4
    if tokens[i + 1:i + 2] == ["--"]:
        return i + 2
    return i + 1


def drop_goverlay_wrappers(tokens: list[str]) -> tuple[list[str], list[str]]:
    """``bgmod``/``fgmod`` samt direkt folgendem ``--`` bzw. ``-- env gamemoderun`` (GOverlays Befehl; das Profil
    bestimmt GameMode selbst) aus einer Wrapper-Liste entfernen → (Rest, entfernte Wrapper)."""
    kept: list[str] = []
    dropped: list[str] = []
    i = 0
    while i < len(tokens):
        if is_goverlay_wrapper(tokens[i]):
            dropped.append(tokens[i])
            i = _wrapper_tail(tokens, i)
            continue
        kept.append(tokens[i])
        i += 1
    return kept, dropped


def split_nonsteam_command(args: list[str]) -> tuple[list[str], list[str]]:
    """GOverlays Befehl für Nicht-Steam-Spiele hat kein ``%command%`` (``<Profil>/bgmod [-- env gamemoderun]
    [/WineDetectionEnabled:False]``); ohne ``%command%`` landen alle Token als Spielargumente. Führendes ``bgmod``
    samt Anhang wird wieder zum Wrapper → (Wrapper, Spielargumente)."""
    if not args or not is_goverlay_wrapper(args[0]):
        return [], list(args)
    end = _wrapper_tail(args, 0)
    return list(args[:end]), list(args[end:])


def wrapper_profile(tokens: Iterable[str]) -> str:
    """Name des GOverlay-Profils, dessen ``bgmod``/``fgmod`` in den Startoptionen steht ("" = keins)."""
    root = os.path.realpath(gameconfig_dir())
    for t in tokens:
        if is_goverlay_wrapper(t):
            parent = os.path.dirname(os.path.normpath(os.path.abspath(os.path.expanduser(str(t)))))
            if os.path.realpath(os.path.dirname(parent)) == root and _safe_profile_name(os.path.basename(parent)):
                return os.path.basename(parent)
    return ""


def apply_profile(plan: Any, prof: Profile, is_windows: bool, own_vkbasalt: bool = False,
                  which: Callable[[str], str | None] | None = None, keep_env: dict[str, str] | None = None,
                  drop_env: Iterable[str] = ()) -> list[str]:
    """Profil in den Startbefehl einsetzen (nur ``plan`` ändern, einmal pro Plan). Gibt Hinweise fürs Protokoll zurück.

    ``argv = [*Startoptionen-Wrapper, *gamescope, <Profil>/bgmod, (-- env [VAR=wert …] [gamemoderun]), *Schalter,
    umu-run …, spiel.exe, *Argumente, (/WineDetectionEnabled:False)]``

    ``keep_env``: ausdrücklich in galaxy-linux gesetzte Variablen (pro Spiel/Bibliothek, Startoptionen). bgmod exportiert
    den ``[Env]``-Abschnitt des Profils erst nach galaxy-linux – widerspricht er, kommen die eigenen Werte per ``env``
    hinter bgmod und gelten damit (z. B. „Wayland-Treiber: Aus“ gegen ``PROTON_ENABLE_WAYLAND=1`` im Profil).
    ``drop_env``: MangoHud-/vkBasalt-Variablen aus alten Startoptionen (0.1.x), die das Profil übernimmt."""
    notes: list[str] = []
    if not prof.active or prof.bgmod is None:
        return notes
    which = which or shutil.which
    tw = [str(prof.bgmod)]
    after: list[str] = []
    profile_env = prof.conf.sections.get("env", {})
    override = {k: v for k, v in sorted((keep_env or {}).items()) if k in profile_env and profile_env[k] != v}
    if override:
        after += [f"{k}={v}" for k, v in override.items()]
        notes.append("Umgebung aus galaxy-linux geht vor dem Profil: " + ", ".join(f"{k}={v}" for k, v in override.items()))
    if prof.conf.get("config", "gamemode") == "1":
        if which("gamemoderun"):
            after.append("gamemoderun")
            if "gamemoderun" in plan.wrappers:
                plan.wrappers = [w for w in plan.wrappers if w != "gamemoderun"]
        else:
            notes.append("gamemode=1 im Profil, aber gamemoderun ist nicht installiert")
    if after:
        tw += ["--", "env", *after]
    if (GAME_PERFORMANCE in prof.conf.broken_flags.get("env", [])
            and prof.conf.get("config", "GOVERLAY_TWEAKS") == "1" and which(GAME_PERFORMANCE)):
        tw.append(GAME_PERFORMANCE)            # „=game-performance“ übergeht bgmod (GOverlay-Fehler, §4.4)
        notes.append("„=game-performance“ im Profil: game-performance selbst vorangestellt")
    if is_windows and prof.conf.get("config", "winedetectionenable") == "0" \
            and WINE_DETECTION_OFF not in plan.game_args_suffix and WINE_DETECTION_OFF not in plan.args:
        plan.game_args_suffix.append(WINE_DETECTION_OFF)
    plan.user_wrappers, nested = drop_goverlay_wrappers(list(plan.user_wrappers))
    if nested:
        notes.append(f"GOverlay-Wrapper aus den Startoptionen ersetzt: {', '.join(nested)}")
    plan.args, nested = drop_goverlay_wrappers(list(plan.args))
    if nested:
        notes.append(f"GOverlay-Wrapper aus den Spielargumenten entfernt: {', '.join(nested)}")
    plan.tool_wrappers = [*tw, *drop_goverlay_wrappers(list(plan.tool_wrappers))[0]]
    if is_windows and "mangohud" in plan.wrappers:
        plan.wrappers = [w for w in plan.wrappers if w != "mangohud"]
        notes.append("eigener MangoHud-Schalter entfällt – das Profil bestimmt MangoHud")
    if is_windows and any(os.path.basename(w) == "mangohud" for w in plan.user_wrappers):
        plan.user_wrappers = [w for w in plan.user_wrappers if os.path.basename(w) != "mangohud"]
        notes.append("mangohud aus den Startoptionen entfällt – das Profil bestimmt MangoHud")
    if own_vkbasalt and plan.env.pop("ENABLE_VKBASALT", None) is not None:
        notes.append("eigener vkBasalt-Schalter entfällt – das Profil bestimmt vkBasalt")
    dropped = [k for k in drop_env if plan.env.pop(k, None) is not None]
    if dropped:
        notes.append(f"{', '.join(dropped)} aus den Startoptionen entfällt – das Profil bestimmt MangoHud/vkBasalt")
    return notes


def _library(launcher: Any, game: Any) -> Any:
    if launcher is not None and hasattr(launcher, "library_of"):
        try:
            return launcher.library_of(game)
        except Exception:  # pragma: no cover
            return None
    return None


def _launch_options(settings: Any, gs: Any, lib: Any) -> list[Any]:
    """Geparste Startoptionen wie beim Start: globale und die des Spiels (sonst die Vorgabe seiner Bibliothek)."""
    from . import launch_options
    out = []
    for text in (settings.default_launch_options, gs.launch_options or (lib.launch_options if lib is not None else "")):
        try:
            out.append(launch_options.parse(text))
        except launch_options.LaunchOptionsError:
            pass
    return out


def option_wrappers(settings: Any, gs: Any, lib: Any = None) -> list[str]:
    """Wrapper aus den Startoptionen (vor ``%command%`` bzw. GOverlays Nicht-Steam-Befehl ohne ``%command%``)."""
    out: list[str] = []
    for o in _launch_options(settings, gs, lib):
        out += [*o.wrapper, *split_nonsteam_command(o.args)[0]]
    return out


def _configured_env_keys(ctx: Any, inherited: bool = True) -> set[str]:
    """Umgebungsvariablen, die der Nutzer selbst gesetzt hat (Einstellungen, Startoptionen; mit ``inherited`` auch
    die Umgebung, aus der galaxy-linux gestartet wurde)."""
    keys = set(ctx.settings.env) | set(ctx.gs.env)
    if inherited:
        keys |= set(os.environ)
    for o in _launch_options(ctx.settings, ctx.gs, _library(ctx.launcher, ctx.game)):
        keys |= set(o.env)
    return keys


def _explicit_env(ctx: Any) -> tuple[dict[str, str], list[str]]:
    """(Variablen, die vor dem Profil gelten sollen, MangoHud-/vkBasalt-Variablen aus den Startoptionen)."""
    from .goverlay import PROFILE_ENV
    lib = _library(ctx.launcher, ctx.game)
    option_env: dict[str, str] = {}
    for o in _launch_options(ctx.settings, ctx.gs, lib):
        option_env.update(o.env)
    keys = set(ctx.gs.env) | set(option_env)
    if ctx.gs.wayland_driver in ("on", "off") or (lib is not None and lib.wayland_driver in ("on", "off")):
        keys.add("PROTON_ENABLE_WAYLAND")
    keep = {k: ctx.plan.env[k] for k in keys if k in ctx.plan.env and k not in PROFILE_ENV}
    own = set(ctx.settings.env) | set(ctx.gs.env)
    drop = [k for k in PROFILE_ENV if k in option_env and k not in own
            and (ctx.is_windows or not k.startswith("MANGOHUD"))]
    return keep, drop


def profile_for_context(ctx: Any) -> Profile:
    """Profil des Spiels; steht bei „Automatisch“ der bgmod eines Profils in den Startoptionen, gilt dieses (so wird
    bgmod nie doppelt vorangestellt)."""
    choice = (ctx.gs.goverlay_profile or "").strip()
    from_options = ""
    if not choice and ctx.plan is not None:
        from_options = wrapper_profile(ctx.plan.user_wrappers)
    prof = resolve_profile(ctx.game.install_path, from_options or choice, ctx.settings, auto_profile_names(ctx.game, ctx.gs))
    prof.from_options = bool(from_options)
    return prof


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
        keep, drop = _explicit_env(ctx)
        ctx.data["goverlay_notes"] = apply_profile(plan, prof, ctx.is_windows, own_vkbasalt=own_vk, keep_env=keep,
                                                   drop_env=drop)
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


def uninstall_candidates(install_path: str, choice: str, extra: Iterable[str] = ()) -> list[Path]:
    """Profile, deren bgmod nachweislich in diesem Spielordner gearbeitet hat (``GAME_DIR`` aus bgmod.conf,
    von bgmod zurückgeschrieben) und die einen bgmod-uninstaller haben – das globale Profil nie (geteilte Backups).
    ``extra``: weitere Profilnamen (z. B. aus einem bgmod-Pfad in den Startoptionen)."""
    names = [n for n in dict.fromkeys([(choice or "").strip(), *extra, auto_profile_name(install_path)])
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
    from_options = wrapper_profile(option_wrappers(backend.settings, gs, _library(getattr(backend, "launcher", None), g)))
    n = 0
    for pdir in uninstall_candidates(g.install_path, gs.goverlay_profile, [from_options, *auto_profile_names(g, gs)]):
        if status:
            status(tr('GOverlay-Änderungen werden zurückgenommen …'))
        from .. import paths
        run_uninstaller(pdir, g.install_path, paths.game_log_file(str(product_id)))
        n += 1
    if n:
        log.info("GOverlay-Änderungen vor „%s“ zurückgenommen: %s", action, g.title)
    return n


# --------------------------------------------------------------------------- Abgleich (Ansicht, Ordnerliste, Cover)
@dataclass
class SyncReport:
    installed: bool = True
    folders: FolderPlan | None = None
    view: ViewReport | None = None
    covers: int = 0


def sync_all(backend: Any, covers: bool = True) -> SyncReport:
    """Spieleansicht, Ordnerliste (und ``goverlay_added_folders``) und Cover abgleichen (Cover nur aus dem Bild-Cache,
    kein Netz). Läuft im Hintergrund oder auf der Kommandozeile – nie im GTK-Hauptthread."""
    with _sync_lock:
        return _sync_all(backend, covers)


_sync_lock = threading.Lock()


def _sync_all(backend: Any, covers: bool) -> SyncReport:
    s = backend.settings
    if not goverlay_installed():
        return SyncReport(installed=False)
    enabled = bool(s.goverlay_register_folders)
    rep = SyncReport()
    if enabled:
        links, other = current_links()
        rep.view = apply_view(plan_view(view_games(backend), links, other))
    else:
        rep.view = clear_view()
    rep.folders = sync_folders(s, enabled)
    if rep.folders is not None and rep.folders.ours != list(s.goverlay_added_folders):
        s.goverlay_added_folders = list(rep.folders.ours)
        s.save()
    if covers and enabled and rep.view is not None:
        rep.covers = sync_covers(backend, rep.view.games)
    return rep


def _cache_mtime() -> int:
    from .. import paths
    try:
        return os.stat(paths.IMAGE_CACHE_DIR).st_mtime_ns
    except OSError:
        return 0


class AutoSync:
    """Hält Spieleansicht, nonsteam_folders.txt und Cover im Hintergrund aktuell – nur in der Oberfläche angelegt
    (Start, Timer, fertige Aufträge, geänderte Titel). ``request()`` läuft nur, wenn sich Spiele, Titel, Cover oder
    Schalter geändert haben (oder ``force``); mehrere Anfragen während eines Laufs ergeben einen Nachlauf."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._running = False
        self._again = False
        self.runs = 0

    def signature(self) -> tuple:
        b = self.backend
        s = b.settings
        games = tuple(sorted((g.pid, g.title, g.target, g.custom_cover) for g in view_games(b)))
        covers = (len(getattr(b, "_covers", None) or {}), len(getattr(b, "_meta", None) or {}), _cache_mtime())
        return goverlay_installed(), bool(s.goverlay_register_folders), games, covers

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
def _profile_names(backend: Any, g: Any, gs: Any) -> list[str]:
    return auto_profile_names(g, gs)


def profile_for_game(backend: Any, product_id: str) -> Profile | None:
    g = backend.registry.get(str(product_id))
    if g is None:
        return None
    gs = backend.game_settings.get(str(product_id))
    choice = (gs.goverlay_profile or "").strip()
    from_options = ""
    if not choice:
        lib = _library(getattr(backend, "launcher", None), g)
        from_options = wrapper_profile(option_wrappers(backend.settings, gs, lib))
    prof = resolve_profile(g.install_path, from_options or choice, backend.settings, _profile_names(backend, g, gs))
    prof.from_options = bool(from_options)
    return prof


def status_for(backend: Any, product_id: str) -> str | None:
    """Text für die Spielseite (None: GOverlay nicht installiert oder Spiel nicht installiert)."""
    if not goverlay_installed():
        return None
    g = backend.registry.get(str(product_id))
    prof = profile_for_game(backend, product_id)
    if g is None or prof is None:
        return None
    return describe(prof, registered=in_view(str(product_id)))
