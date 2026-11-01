"""Umzug von galaxy-linux (Name bis 0.4.1) zu Milkyway – läuft bei jedem Start, tut aber nur einmal etwas.

- Ordner: ``~/.config/galaxy-linux``, ``~/.local/share/galaxy-linux`` und ``~/.cache/galaxy-linux`` werden zu
  ``…/milkyway`` umbenannt (ein ``rename`` im selben Elternordner). Am alten Ort bleibt ein Verweis
  (``galaxy-linux → milkyway``), damit gespeicherte absolute Pfade weiter gelten (eigene Skripte, Wine-Prefixe).
  War der alte Ordner selbst ein Verweis (Daten auf einer anderen Platte), zeigt der neue auf dasselbe Ziel.
- Hauptprotokoll: ``galaxy-linux.log*`` → ``milkyway.log*``.
- Spielverknüpfungen (Anwendungsmenü, Schreibtisch) und der Autostart-Eintrag: neuer Dateiname, Befehl, Schema
  ``milkyway://`` und Programmsymbol; Name, Spielsymbol und Rechte bleiben.

Woanders: der Schlüsselbund-Eintrag zieht beim ersten Lesen um (``api/auth.py``), GOverlays Ordnerliste beim
nächsten Abgleich (der alte Ansichtsordner steht in ``goverlay_added_folders`` und wird ersetzt), der Zustand der
Prefix-Einrichtung beim ersten Zugriff (``content/redist.py``).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import APP_ID, APP_NAME, paths

LEGACY_APP_ID = "org.galaxylinux.Client"
LEGACY_SCHEME = paths.LEGACY_PKG + "://"
SCHEME = paths.PKG + "://"


def legacy_dir(new: Path) -> Path:
    return new.with_name(paths.LEGACY_PKG)


def move_dir(old: Path, new: Path) -> str | None:
    """``old`` → ``new`` umbenennen und am alten Ort einen Verweis lassen. Rückgabe: Meldung, None = nichts zu tun."""
    if old.is_symlink():
        target = Path(os.path.realpath(old))
        if new.exists() or new.is_symlink() or not target.is_dir():
            return None                                   # z. B. der eigene Verweis von einem früheren Start
        new.symlink_to(target)
        return f"{new} → {target} (wie {old})"
    if not old.is_dir():
        return None
    if new.exists() or new.is_symlink():
        return f"{old} bleibt unverändert: {new} gibt es schon"
    try:
        os.rename(old, new)
    except OSError as exc:
        return f"{old} → {new} fehlgeschlagen: {exc}"
    try:
        old.symlink_to(new.name)
    except OSError as exc:
        return f"{old} → {new} (ohne Verweis am alten Ort: {exc})"
    return f"{old} → {new}"


def rename_logs(log_dir: Path) -> list[str]:
    out = []
    for p in sorted(log_dir.glob(paths.LEGACY_PKG + ".log*")):
        q = p.with_name(paths.PKG + p.name[len(paths.LEGACY_PKG):])
        if q.exists():
            continue
        try:
            p.rename(q)
            out.append(f"{p.name} → {q.name}")
        except OSError as exc:
            out.append(f"{p.name}: {exc}")
    return out


def rewrite_entry(text: str, command: str, legacy_data: str, data: str) -> str:
    """Inhalt einer alten .desktop-Datei auf Milkyway umschreiben: Befehl vor ``galaxy-linux://…`` bzw.
    ``--gui``, Schema, Programmsymbol/Fensterklasse, Pfade im alten Datenordner, Name im Kommentar."""
    out = []
    for line in text.splitlines():
        if line.startswith("Exec="):
            m = re.match(r"Exec=.*?\s(" + re.escape(LEGACY_SCHEME) + r"\S*|--gui\b.*)$", line)
            if m:
                line = f"Exec={command} {m.group(1).replace(LEGACY_SCHEME, SCHEME, 1)}"
        elif line.startswith(("Icon=", "Path=")):
            line = line.replace(legacy_data, data, 1)
        elif line.startswith(("Comment", "Name")):
            line = line.replace(paths.LEGACY_PKG, APP_NAME)
        out.append(line.replace(LEGACY_APP_ID, APP_ID))
    return "\n".join(out) + "\n"


def move_entry(old: Path, new: Path, command: str, legacy_data: str, data: str) -> bool:
    if new.exists():
        old.unlink(missing_ok=True)
        return False
    text = old.read_text(encoding="utf-8", errors="surrogateescape")
    tmp = new.with_name(new.name + ".tmp")
    tmp.write_text(rewrite_entry(text, command, legacy_data, data), encoding="utf-8", errors="surrogateescape")
    shutil.copymode(old, tmp)
    tmp.replace(new)
    old.unlink()
    return True


def migrate_shortcuts(dirs: list[Path], command: str, legacy_data: str, data: str) -> list[Path]:
    from .launch import shortcuts
    moved = []
    for d in dirs:
        for old in sorted(d.glob(paths.LEGACY_PKG + "-*.desktop")):
            pid = old.name[len(paths.LEGACY_PKG) + 1:-len(".desktop")]
            new = d / shortcuts.desktop_file_name(pid)
            try:
                if move_entry(old, new, command, legacy_data, data):
                    moved.append(new)
            except OSError:
                continue
    return moved


def migrate_autostart(autostart_dir: Path, command: str) -> Path | None:
    old = autostart_dir / f"{LEGACY_APP_ID}.desktop"
    new = autostart_dir / f"{APP_ID}.desktop"
    if not old.is_file():
        return None
    try:
        return new if move_entry(old, new, command.removesuffix(" --gui --minimized"), "", "") else None
    except OSError:
        return None


def run_early() -> list[str]:
    """Vor dem Logging (dessen Ordner liegt im Datenordner): Ordner und Protokolle. Rückgabe: Meldungen fürs Log."""
    notes = []
    for new in (paths.CONFIG_DIR, paths.DATA_DIR, paths.CACHE_DIR):
        msg = move_dir(legacy_dir(new), new)
        if msg:
            notes.append(msg)
    if paths.LOG_DIR.is_dir():
        notes += rename_logs(paths.LOG_DIR)
    return notes


def run_late() -> list[Path]:
    """Nach dem Logging: Verknüpfungen und Autostart (beides nur, wenn es Einträge unter dem alten Namen gibt)."""
    from . import autostart
    from .launch import shortcuts
    dirs = [shortcuts.APPS_DIR, shortcuts.desktop_dir()]
    if not any(any(d.glob(paths.LEGACY_PKG + "-*.desktop")) for d in dirs if d.is_dir()) \
            and not (autostart.autostart_file().parent / f"{LEGACY_APP_ID}.desktop").exists():
        return []
    legacy_data = str(legacy_dir(paths.DATA_DIR))
    moved = migrate_shortcuts([d for d in dirs if d.is_dir()], shortcuts.launcher_command(), legacy_data,
                              str(paths.DATA_DIR))
    if any(p.parent == shortcuts.APPS_DIR for p in moved):
        shortcuts._refresh_menu()
    auto = migrate_autostart(autostart.autostart_file().parent, autostart.command())
    return moved + ([auto] if auto else [])
