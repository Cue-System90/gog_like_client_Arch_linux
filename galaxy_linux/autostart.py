"""„GOG Galaxy beim Systemstart öffnen“ (Einstellungen → Allgemein): XDG-Autostart-Eintrag, der den Client
minimiert startet (im System-Tray, wenn es einen gibt)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import APP_ID, APP_NAME

MINIMIZED_ARG = "--minimized"


def autostart_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / f"{APP_ID}.desktop"


def command() -> str:
    exe = shutil.which("galaxy-linux") or "galaxy-linux"
    return f"{exe} --gui {MINIMIZED_ARG}"


def desktop_entry() -> str:
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=Unofficial GOG.com client",
        f"Exec={command()}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
        "X-KDE-autostart-after=panel",
        "",
    ))


def is_enabled() -> bool:
    f = autostart_file()
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return False
    return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text


def set_enabled(on: bool) -> Path:
    f = autostart_file()
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(desktop_entry(), encoding="utf-8")
        tmp.replace(f)
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    return f
