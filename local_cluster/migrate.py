"""Umzug von den früheren Namen zu local:cluster – läuft bei jedem Start, tut aber nur einmal etwas.

Frühere Namen: Milkyway (0.5.x, ``milkyway``) und galaxy-linux (bis 0.4.1). 0.5.0 hat die Ordner von galaxy-linux
schon nach ``…/milkyway`` gezogen und am alten Ort einen Verweis gelassen; wer von 0.4.x kommt, hat noch echte
Ordner ``…/galaxy-linux``.

- Ordner: ``~/.config``, ``~/.local/share`` und ``~/.cache`` – der Ordner unter einem früheren Namen wird zu
  ``…/local-cluster`` umbenannt (ein ``rename`` im selben Elternordner). Am alten Ort bleibt ein Verweis
  (``milkyway → local-cluster``), damit gespeicherte absolute Pfade weiter gelten (eigene Skripte, Wine-Prefixe);
  ältere Verweise (``galaxy-linux → milkyway``) zeigen danach direkt auf den neuen Ordner. War der alte Ordner selbst
  ein Verweis (Daten auf einer anderen Platte), zeigt der neue auf dasselbe Ziel.
- Hauptprotokoll: ``milkyway.log*`` bzw. ``galaxy-linux.log*`` → ``local-cluster.log*``.
- Spielverknüpfungen (Anwendungsmenü, Schreibtisch) und der Autostart-Eintrag: neuer Dateiname, Befehl, Schema
  ``local-cluster://`` und Programmsymbol; Name, Spielsymbol und Rechte bleiben.

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

LEGACY_NAMES = paths.LEGACY_PKGS                                    # Paket-/Ordnernamen, neuester zuerst
LEGACY_APP_IDS = ("io.github.cue_system90.Milkyway", "org.galaxylinux.Client")
LEGACY_DISPLAY = ("Milkyway", "galaxy-linux")                       # Programmname in Name=/Comment=
LEGACY_SCHEMES = tuple(n + "://" for n in LEGACY_NAMES)
SCHEME = paths.PKG + "://"


def legacy_dirs(new: Path) -> list[Path]:
    return [new.with_name(n) for n in LEGACY_NAMES]


def _own_link(link: Path) -> bool:
    """Verweis aus einem früheren Umzug: relativ auf einen Geschwisterordner mit einem unserer Namen."""
    target = os.readlink(link)
    return "/" not in target and target in (*LEGACY_NAMES, paths.PKG)


def move_dir(old: Path, new: Path) -> str | None:
    """``old`` → ``new`` umbenennen und am alten Ort einen Verweis lassen. Rückgabe: Meldung, None = nichts zu tun."""
    if old.is_symlink():
        if new.exists() or new.is_symlink():
            if _own_link(old) and os.readlink(old) != new.name and os.path.realpath(old) == os.path.realpath(new):
                old.unlink()
                old.symlink_to(new.name)                  # z. B. galaxy-linux → milkyway  ⇒  galaxy-linux → local-cluster
                return f"{old} → {new.name}"
            return None
        target = Path(os.path.realpath(old))
        if _own_link(old) or not target.is_dir():
            return None                                   # zeigt auf einen anderen früheren Namen: der kommt selbst dran
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


def migrate_dir(new: Path) -> list[str]:
    """Alle früheren Namen nacheinander: der neueste vorhandene Ordner zieht um, ältere Verweise folgen."""
    notes = []
    for old in legacy_dirs(new):
        msg = move_dir(old, new)
        if msg:
            notes.append(msg)
    # ein älterer Verweis kann erst nach dem Umzug des neueren direkt zeigen (galaxy-linux → milkyway → local-cluster)
    for old in legacy_dirs(new):
        if old.is_symlink():
            msg = move_dir(old, new)
            if msg and msg not in notes:
                notes.append(msg)
    return notes


def rename_logs(log_dir: Path) -> list[str]:
    out = []
    for name in LEGACY_NAMES:
        for p in sorted(log_dir.glob(name + ".log*")):
            q = p.with_name(paths.PKG + p.name[len(name):])
            if q.exists():
                continue
            try:
                p.rename(q)
                out.append(f"{p.name} → {q.name}")
            except OSError as exc:
                out.append(f"{p.name}: {exc}")
    return out


def rewrite_entry(text: str, command: str, legacy_data: list[str], data: str) -> str:
    """Inhalt einer alten .desktop-Datei auf local:cluster umschreiben: Befehl vor ``milkyway://…``/``galaxy-linux://…``
    bzw. ``--gui``, Schema, Programmsymbol/Fensterklasse, Pfade in alten Datenordnern, Programmname."""
    schemes = "|".join(re.escape(s) for s in LEGACY_SCHEMES)
    out = []
    for line in text.splitlines():
        if line.startswith("Exec="):
            m = re.match(r"Exec=.*?\s((?:" + schemes + r")\S*|--gui\b.*)$", line)
            if m:
                arg = m.group(1)
                for s in LEGACY_SCHEMES:
                    if arg.startswith(s):
                        arg = SCHEME + arg[len(s):]
                line = f"Exec={command} {arg}"
        elif line.startswith(("Icon=", "Path=")):
            for old in legacy_data:
                if old and line.split("=", 1)[1].startswith(old + "/"):
                    line = line.replace(old, data, 1)
                    break
        elif line.startswith("Comment"):
            for name in LEGACY_DISPLAY:
                line = line.replace(name, APP_NAME)
        elif line.startswith("Name") and line.split("=", 1)[-1] in LEGACY_DISPLAY:
            line = line.split("=", 1)[0] + "=" + APP_NAME             # Autostart; Spieltitel bleiben
        for old_id in LEGACY_APP_IDS:
            line = line.replace(old_id, APP_ID)
        out.append(line)
    return "\n".join(out) + "\n"


def move_entry(old: Path, new: Path, command: str, legacy_data: list[str], data: str) -> bool:
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


def legacy_shortcuts(d: Path) -> list[tuple[Path, str]]:
    """(Datei, Produkt-ID) der Spielverknüpfungen unter früheren Namen in ``d``."""
    out = []
    for name in LEGACY_NAMES:
        for p in sorted(d.glob(name + "-*.desktop")):
            out.append((p, p.name[len(name) + 1:-len(".desktop")]))
    return out


def migrate_shortcuts(dirs: list[Path], command: str, legacy_data: list[str], data: str) -> list[Path]:
    from .launch import shortcuts
    moved = []
    for d in dirs:
        for old, pid in legacy_shortcuts(d):
            new = d / shortcuts.desktop_file_name(pid)
            try:
                if move_entry(old, new, command, legacy_data, data):
                    moved.append(new)
            except OSError:
                continue
    return moved


def migrate_autostart(autostart_dir: Path, command: str) -> Path | None:
    new = autostart_dir / f"{APP_ID}.desktop"
    result = None
    for old_id in LEGACY_APP_IDS:
        old = autostart_dir / f"{old_id}.desktop"
        if not old.is_file():
            continue
        try:
            if move_entry(old, new, command.removesuffix(" --gui --minimized"), [], ""):
                result = new
        except OSError:
            continue
    return result


def run_early() -> list[str]:
    """Vor dem Logging (dessen Ordner liegt im Datenordner): Ordner und Protokolle. Rückgabe: Meldungen fürs Log."""
    notes = []
    for new in (paths.CONFIG_DIR, paths.DATA_DIR, paths.CACHE_DIR):
        notes += migrate_dir(new)
    if paths.LOG_DIR.is_dir():
        notes += rename_logs(paths.LOG_DIR)
    return notes


def run_late() -> list[Path]:
    """Nach dem Logging: Verknüpfungen und Autostart (beides nur, wenn es Einträge unter früheren Namen gibt)."""
    from . import autostart
    from .launch import shortcuts
    dirs = [d for d in (shortcuts.APPS_DIR, shortcuts.desktop_dir()) if d.is_dir()]
    auto_dir = autostart.autostart_file().parent
    if not any(legacy_shortcuts(d) for d in dirs) \
            and not any((auto_dir / f"{i}.desktop").exists() for i in LEGACY_APP_IDS):
        return []
    # absolut: Icon= steht immer mit absolutem Pfad in der Datei, XDG_DATA_HOME kann relativ gesetzt sein
    legacy_data = [os.path.abspath(p) for p in legacy_dirs(paths.DATA_DIR)]
    moved = migrate_shortcuts(dirs, shortcuts.launcher_command(), legacy_data, os.path.abspath(paths.DATA_DIR))
    if any(p.parent == shortcuts.APPS_DIR for p in moved):
        shortcuts._refresh_menu()
    auto = migrate_autostart(auto_dir, autostart.command())
    return moved + ([auto] if auto else [])
