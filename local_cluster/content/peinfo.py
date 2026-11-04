"""Versionsinfo (RT_VERSION) aus Windows-Programmen lesen – reines Python, ohne Zusatzpakete.

Gelesen wird nur die Zeichenkettentabelle (``StringFileInfo``) der ersten Sprache: ProductName, FileDescription,
CompanyName, ProductVersion … Damit bekommt ein externes Spiel seinen Namen, wenn der Ordnername nichts taugt
(„EmperorDAdRdM 1360 x 768“). Alles ist gegen kaputte oder fremde Dateien abgesichert: Fehler ergeben ``{}``.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

RT_VERSION = 16
MAX_READ = 64 << 20          # größere Dateien: nur Kopf, Abschnitte und Ressourcen lesen
MAX_RESOURCE = 1 << 20       # eine Versionsressource ist ein paar KB groß
# Werte, die keinen Spielnamen ergeben (Engine, Starter, Platzhalter der Entwicklungsumgebung)
GENERIC = re.compile(r"^(unreal engine.*|unity.*|launcher|game|application|setup|install(er)?|"
                     r"microsoft.*|win32 application|windows application|mfc application|"
                     r"(product|file) ?(name|description)|todo:.*|\W*|\d[\d., ]*)$", re.I)


@dataclass
class _Block:
    key: str
    kind: int
    value: bytes
    children: list["_Block"] = field(default_factory=list)
    end: int = 0


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _block(data: bytes, off: int, limit: int, depth: int = 0) -> _Block | None:
    if depth > 6 or off + 6 > limit:
        return None
    length, vlen, kind = struct.unpack_from("<HHH", data, off)
    if length < 6:
        return None
    end = min(off + length, limit)
    i = off + 6
    while i + 1 < end and data[i:i + 2] != b"\0\0":
        i += 2
    key = data[off + 6:i].decode("utf-16-le", "replace")
    pos = _align4(i + 2)
    size = vlen * 2 if kind == 1 else vlen
    value = data[pos:min(pos + size, end)]
    if kind == 1 and len(value) < size:           # manche Werkzeuge zählen Bytes statt Zeichen
        value = data[pos:end]
    pos = _align4(pos + size) if size else pos
    blk = _Block(key, kind, value, end=end)
    while pos < end:
        child = _block(data, pos, end, depth + 1)
        if child is None or child.end <= pos:
            break
        blk.children.append(child)
        pos = _align4(child.end)
    return blk


def parse_version_resource(data: bytes) -> dict[str, str]:
    """``VS_VERSIONINFO`` → Zeichenketten der ersten StringTable (Schlüssel wie in der Datei)."""
    try:
        root = _block(data, 0, len(data))
    except (struct.error, ValueError):
        return {}
    if root is None or root.key != "VS_VERSION_INFO":
        return {}
    for sfi in root.children:
        if sfi.key != "StringFileInfo":
            continue
        for table in sfi.children:
            out: dict[str, str] = {}
            for s in table.children:
                text = s.value.decode("utf-16-le", "replace").split("\0", 1)[0].strip()
                if s.key and text:
                    out[s.key] = text
            if out:
                return out
    return {}


class _PE:
    def __init__(self, f) -> None:
        self.f = f
        head = self._read(0, 64)
        if head[:2] != b"MZ":
            raise ValueError("kein MZ")
        (lfanew,) = struct.unpack_from("<I", head, 0x3C)
        coff = self._read(lfanew, 24)
        if coff[:4] != b"PE\0\0":
            raise ValueError("kein PE")
        nsec, opt_size = struct.unpack_from("<H12xH", coff, 6)
        opt = self._read(lfanew + 24, opt_size)
        (magic,) = struct.unpack_from("<H", opt, 0)
        dd = {0x10B: 96, 0x20B: 112}.get(magic)
        if dd is None or len(opt) < dd + 3 * 8:
            raise ValueError("unbekannter Optional Header")
        self.res_rva, self.res_size = struct.unpack_from("<II", opt, dd + 2 * 8)
        sec = self._read(lfanew + 24 + opt_size, 40 * min(nsec, 96))
        self.sections = []
        for k in range(len(sec) // 40):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", sec, k * 40 + 8)
            self.sections.append((va, max(vsize, rsize), raw, rsize))

    def _read(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        return self.f.read(n)

    def offset(self, rva: int) -> int | None:
        for va, size, raw, rsize in self.sections:
            if va <= rva < va + size and rva - va < max(rsize, 1):
                return rva - va + raw
        return None

    def version_resource(self) -> bytes:
        base = self.offset(self.res_rva) if self.res_rva else None
        if base is None:
            return b""
        size = min(self.res_size or MAX_RESOURCE, 16 << 20)
        res = self._read(base, size)

        def entries(off: int) -> list[tuple[int, int]]:
            if off + 16 > len(res):
                return []
            named, ids = struct.unpack_from("<HH", res, off + 12)
            out = []
            for k in range(min(named + ids, 4096)):
                p = off + 16 + 8 * k
                if p + 8 > len(res):
                    break
                out.append(struct.unpack_from("<II", res, p))
            return out

        for name, target in entries(0):
            if name != RT_VERSION or not target & 0x80000000:
                continue
            node = target
            for _level in range(2):              # Name/ID, dann Sprache – jeweils der erste Eintrag
                if not node & 0x80000000:
                    break
                sub = entries(node & 0x7FFFFFFF)
                if not sub:
                    return b""
                node = sub[0][1]
            if node & 0x80000000 or node + 16 > len(res):
                return b""
            data_rva, data_size = struct.unpack_from("<II", res, node)
            off = self.offset(data_rva)
            if off is None or not data_size:
                return b""
            return self._read(off, min(data_size, MAX_RESOURCE))
        return b""


def version_strings(path: str | Path) -> dict[str, str]:
    """ProductName, FileDescription … einer .exe/.dll (leeres dict, wenn nicht lesbar oder keine Versionsinfo)."""
    try:
        with open(path, "rb") as f:
            return parse_version_resource(_PE(f).version_resource())
    except (OSError, ValueError, struct.error):
        return {}


def game_name(info: dict[str, str]) -> str:
    """Brauchbarer Spielname aus der Versionsinfo: ProductName, sonst FileDescription – ohne Allerweltswerte
    („Unreal Engine“, „Launcher“, „Game“, leere oder rein numerische Angaben) und ohne ™/®."""
    for key in ("ProductName", "FileDescription"):
        v = re.sub(r"[™®©]", "", info.get(key, "") or "")
        v = re.sub(r"\s{2,}", " ", v).strip(" .-_")
        if v and not GENERIC.match(v) and len(v) <= 80:
            return v
    return ""
