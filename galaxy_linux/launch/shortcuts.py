"""Verknüpfungen pro Spiel im Startmenü (Anwendungsmenü) und auf dem Schreibtisch – wie im Windows-Client.

Die Verknüpfung startet das Spiel über den Client (``galaxy-linux galaxy-linux://launch/<id>``):
läuft der Client bereits, übernimmt die laufende Instanz den Start (Spielzeit, Proton-Einstellungen).
"""
from __future__ import annotations

from ..i18n import tr

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import paths

log = logging.getLogger(__name__)

APPS_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
ICON_DIR = paths.DATA_DIR / "icons"


def desktop_dir() -> Path:
    """Schreibtisch-Ordner laut xdg-user-dirs (lokalisiert), sonst ~/Desktop bzw. ~/Schreibtisch."""
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5).stdout.strip()
        if out and Path(out).is_dir() and Path(out) != Path.home():
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("Schreibtisch", "Desktop"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home() / "Desktop"


def desktop_file_name(product_id: str) -> str:
    return f"galaxy-linux-{product_id}.desktop"


def menu_path(product_id: str) -> Path:
    return APPS_DIR / desktop_file_name(product_id)


def desktop_path(product_id: str) -> Path:
    return desktop_dir() / desktop_file_name(product_id)


def has_menu_shortcut(product_id: str) -> bool:
    return menu_path(product_id).exists()


def has_desktop_shortcut(product_id: str) -> bool:
    return desktop_path(product_id).exists()


_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./:=,+@%")


def exec_quote(arg: str) -> str:
    """Quoting nach Desktop-Entry-Spezifikation (doppelte Anführungszeichen, Backslash-Escapes)."""
    if arg and all(c in _SAFE for c in arg):
        return arg
    out = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return '"' + out + '"'


def launcher_command() -> str:
    """Startbefehl des Clients für Exec= (installiertes Programm, Nutzer-Installation oder Quellordner)."""
    exe = shutil.which("galaxy-linux")
    if exe:
        return exec_quote(exe)
    user_bin = Path.home() / ".local" / "bin" / "galaxy-linux"
    if user_bin.exists():
        return exec_quote(str(user_bin))
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name == "galaxy-linux" and argv0.exists():
        return exec_quote(str(argv0))
    pkg_root = Path(__file__).resolve().parents[2]
    return f"env {exec_quote('PYTHONPATH=' + str(pkg_root))} {exec_quote(sys.executable)} -m galaxy_linux"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", " ").strip()


def desktop_entry(product_id: str, title: str, icon: str | None, comment: str = "") -> str:
    cmd = launcher_command()
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        f"Name={_escape(title)}",
        f"Comment={_escape(comment or tr('Über galaxy-linux starten'))}",
        f"Exec={cmd} galaxy-linux://launch/{product_id}",
        f"Icon={icon or 'org.galaxylinux.Client'}",
        "Terminal=false",
        "Categories=Game;",
        "StartupNotify=true",
        "StartupWMClass=org.galaxylinux.Client",
        f"X-GOG-ProductId={product_id}",
        "Actions=Page;",
        "",
        "[Desktop Action Page]",
        "Name=" + tr("Spielseite im Client öffnen"),
        f"Exec={cmd} galaxy-linux://game/{product_id}",
    ]
    return "\n".join(lines) + "\n"


def store_icon(product_id: str, source: Path | None) -> str | None:
    """Spiel-Icon dauerhaft ablegen (PNG, falls Pillow vorhanden; sonst Originalformat) und Pfad zurückgeben."""
    if not source or not Path(source).exists():
        return None
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    target = ICON_DIR / f"{product_id}.png"
    try:
        from PIL import Image  # optional
        with Image.open(source) as im:
            im = im.convert("RGBA")
            if max(im.size) > 256:
                im.thumbnail((256, 256))
            im.save(target, "PNG")
        return str(target)
    except Exception:
        ext = Path(source).suffix or ".img"
        target = ICON_DIR / f"{product_id}{ext}"
        try:
            shutil.copyfile(source, target)
            return str(target)
        except OSError as exc:
            log.warning("Icon konnte nicht gespeichert werden: %s", exc)
            return None


def create(product_id: str, title: str, icon_path: str | None, menu: bool = True, desktop: bool = False,
           comment: str = "") -> list[Path]:
    """Legt die gewünschten Verknüpfungen an und gibt die geschriebenen Dateien zurück."""
    content = desktop_entry(product_id, title, icon_path, comment)
    written: list[Path] = []
    if menu:
        APPS_DIR.mkdir(parents=True, exist_ok=True)
        p = menu_path(product_id)
        p.write_text(content, encoding="utf-8")
        p.chmod(0o755)
        written.append(p)
    if desktop:
        d = desktop_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = desktop_path(product_id)
        p.write_text(content, encoding="utf-8")
        p.chmod(0o755)   # KDE/GNOME verlangen ausführbare .desktop-Dateien auf dem Schreibtisch
        written.append(p)
    _refresh_menu()
    log.info("Verknüpfungen für %s angelegt: %s", product_id, ", ".join(str(w) for w in written) or "keine")
    return written


def remove(product_id: str, menu: bool = True, desktop: bool = True) -> list[Path]:
    removed: list[Path] = []
    for flag, p in ((menu, menu_path(product_id)), (desktop, desktop_path(product_id))):
        if flag and p.exists():
            try:
                p.unlink()
                removed.append(p)
            except OSError as exc:
                log.warning("Verknüpfung %s konnte nicht entfernt werden: %s", p, exc)
    if menu and desktop:
        for f in ICON_DIR.glob(f"{product_id}.*"):
            try:
                f.unlink()
            except OSError:
                pass
    if removed:
        _refresh_menu()
        log.info("Verknüpfungen für %s entfernt: %s", product_id, ", ".join(str(r) for r in removed))
    return removed


def _refresh_menu() -> None:
    tool = shutil.which("update-desktop-database")
    if tool:
        try:
            subprocess.run([tool, str(APPS_DIR)], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
