"""Protokoll-Fenster ohne GTK: Protokolle finden, Ende lesen, neuen Zeilen folgen, nach Stufe und Suchtext filtern.

Das Hauptprotokoll (``galaxy-linux.log``) hat das Format ``JJJJ-MM-TT hh:mm:ss,mmm STUFE [Thread] Logger: Text``;
Folgezeilen (Tracebacks) gehören zum Eintrag davor. In Spiel- und comet-Protokollen ohne dieses Format wird die
Stufe aus dem Text geschätzt (Wine: ``err:`` / ``warn:``/``fixme:``). Angezeigt wird alles noch einmal durch
``logsetup.mask_text`` – Tokens erscheinen nie, auch nicht beim Kopieren.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import paths

MAX_LINES = 3000
LEVELS = ("all", "warning", "error")                   # Alle / Warnungen / Fehler
_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_MIN = {"all": 0, "warning": 30, "error": 40}
HEAD = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
GUESS_ERROR = re.compile(r"(\berr:|\berror\b|\bexception\b|traceback|\bfatal\b|\bcritical\b|fehlgeschlagen|\bfehler\b)", re.I)
GUESS_WARN = re.compile(r"(\bwarn(ing)?\b|\bwarn:|\bfixme:|\bwarnung\b)", re.I)


@dataclass
class Record:
    level: int          # 0 = unbekannt
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def line_level(line: str) -> int | None:
    """Stufe einer Kopfzeile des Hauptprotokolls (None: Folgezeile oder anderes Format)."""
    m = HEAD.match(line)
    return _RANK[m.group(1)] if m else None


def guess_level(line: str) -> int:
    if GUESS_ERROR.search(line):
        return 40
    if GUESS_WARN.search(line):
        return 30
    return 0


def split_records(lines: Iterable[str]) -> list[Record]:
    """Zeilen → Einträge. Im Format des Hauptprotokolls hängen Folgezeilen am Eintrag davor; sonst ist jede Zeile ein
    Eintrag mit geschätzter Stufe."""
    out: list[Record] = []
    structured = False
    for line in lines:
        lvl = line_level(line)
        if lvl is not None:
            structured = True
            out.append(Record(lvl, [line]))
        elif structured and out:
            out[-1].lines.append(line)
        else:
            out.append(Record(guess_level(line), [line]))
    return out


def append_lines(records: list[Record], lines: list[str]) -> tuple[list[Record], bool]:
    """Neue Zeilen an eine Eintragsliste hängen → (neue Einträge, letzter alter Eintrag geändert). Folgezeilen
    (Traceback) gehören zum letzten Eintrag des Hauptprotokolls – die Anzeige baut dann neu auf."""
    i = 0
    if records and line_level(records[-1].lines[0]) is not None:
        while i < len(lines) and line_level(lines[i]) is None:
            records[-1].lines.append(lines[i])
            i += 1
    added = split_records(lines[i:])
    records += added
    return added, i > 0


def filter_records(records: Iterable[Record], level: str = "all", query: str = "") -> list[Record]:
    """Einträge ab der Stufe (``all`` | ``warning`` | ``error``), die den Suchtext enthalten (ohne Groß/klein)."""
    minimum = _MIN.get(level, 0)
    q = (query or "").strip().lower()
    return [r for r in records if r.level >= minimum and (not q or q in r.text.lower())]


def render(records: Iterable[Record]) -> str:
    from .logsetup import mask_text
    return mask_text("\n".join(r.text for r in records))


def tail_lines(path: str | Path, n: int = MAX_LINES, block: int = 1 << 16) -> list[str]:
    """Die letzten ``n`` Zeilen einer Datei (liest vom Ende her, auch bei großen Dateien schnell)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            data = b""
            pos = end
            while pos > 0 and data.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if pos > 0 and lines:
        lines = lines[1:]                       # erste Zeile ist angeschnitten
    return lines[-n:]


class Follower:
    """Liest neu angehängte Zeilen; erkennt Leeren und Rotation (Datei kleiner bzw. andere Inode) → ``reset``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.inode = 0
        self._partial = b""
        self.reset_flag = False

    def start_at_end(self) -> None:
        try:
            st = self.path.stat()
            self.offset, self.inode = st.st_size, st.st_ino
        except OSError:
            self.offset, self.inode = 0, 0
        self._partial = b""

    def read_new(self) -> list[str]:
        """Neue vollständige Zeilen seit dem letzten Aufruf. Wurde die Datei geleert oder ersetzt, ist danach
        ``reset_flag`` gesetzt (die Anzeige lädt dann neu)."""
        self.reset_flag = False
        try:
            st = self.path.stat()
        except OSError:
            return []
        if st.st_ino != self.inode or st.st_size < self.offset:
            self.inode, self.offset, self._partial = st.st_ino, 0, b""
            self.reset_flag = True
        if st.st_size == self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(st.st_size - self.offset)
        except OSError:
            return []
        self.offset += len(data)
        data = self._partial + data
        *full, self._partial = data.split(b"\n")
        return [x.decode("utf-8", "replace") for x in full]


@dataclass
class LogSource:
    key: str             # main | comet | <Produkt-ID>
    label: str
    path: Path


def main_log() -> Path:
    return paths.LOG_DIR / "galaxy-linux.log"


def sources(titles: dict[str, str] | None = None) -> list[LogSource]:
    """Hauptprotokoll, comet.log und die Protokolle der Spiele (nach Titel sortiert)."""
    titles = titles or {}
    out = [LogSource("main", "galaxy-linux.log", main_log())]
    comet = paths.LOG_DIR / "comet.log"
    if comet.exists():
        out.append(LogSource("comet", "comet.log", comet))
    games = []
    try:
        entries = sorted(paths.LOG_DIR.glob("*.log"))
    except OSError:
        entries = []
    for p in entries:
        if p.name in ("galaxy-linux.log", "comet.log"):
            continue
        pid = p.stem
        games.append(LogSource(pid, titles.get(pid) or pid, p))
    games.sort(key=lambda s: s.label.lower())
    return out + games


def clear(path: str | Path) -> None:
    """Protokoll leeren (Datei bleibt, laufende Schreiber hängen danach weiter an)."""
    with open(path, "r+b") as f:
        f.truncate(0)
