"""Übersetzungen der Oberfläche.

Die Quelltexte sind Deutsch; weitere Sprachen liegen als gettext-Katalog in ``galaxy_linux/locale/<code>.po``.
Die Sprache wird beim Start festgelegt: ``GALAXY_LINUX_LANG`` → Einstellung ``ui_language`` → Systemsprache
(deutsch → Deutsch, sonst Englisch). Ein Wechsel wirkt nach einem Neustart.

Gleiche deutsche Wörter mit verschiedener Bedeutung bekommen einen Kontext wie bei gettext: ``tr("Kontext\x04Text")``
(im Katalog ``msgctxt``); angezeigt wird ohne Übersetzung nur der Text.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

LOCALE_DIR = Path(__file__).parent / "locale"


def N_(text: str) -> str:
    """Markiert einen Text für den Katalog, ohne ihn schon beim Import zu übersetzen."""
    return text


LANGUAGES = {"auto": N_("Automatisch (Systemsprache)"), "de": "Deutsch", "en": "English"}

_catalog: dict[str, str] = {}
_lang = "de"


def _unescape(s: str) -> str:
    return re.sub(r'\\(["\\nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


CONTEXT_SEP = "\x04"


def parse_po(path: Path) -> dict[str, str]:
    """Kleiner .po-Leser (msgctxt/msgid/msgstr mit Folgezeilen); leere Übersetzungen werden übergangen. Ein Kontext
    steht wie bei gettext vor dem Text: ``"Kontext\x04Text"``."""
    out: dict[str, str] = {}
    msgid = msgstr = None
    ctxt = None
    cur = None

    def flush() -> None:
        if msgid and msgstr:
            out[(ctxt + CONTEXT_SEP if ctxt else "") + msgid] = msgstr
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line.startswith("msgctxt "):
            flush()
            msgid = msgstr = None
            ctxt, cur = _unescape(line[9:-1]), "ctxt"
        elif line.startswith("msgid "):
            if cur != "ctxt":
                flush()
                ctxt = None
            msgid, msgstr, cur = _unescape(line[7:-1]), None, "id"
        elif line.startswith("msgstr "):
            msgstr, cur = _unescape(line[8:-1]), "str"
        elif line.startswith('"') and cur:
            if cur == "ctxt":
                ctxt = (ctxt or "") + _unescape(line[1:-1])
            elif cur == "id":
                msgid = (msgid or "") + _unescape(line[1:-1])
            else:
                msgstr = (msgstr or "") + _unescape(line[1:-1])
        elif not line:
            cur = None
    flush()
    return out


def _settings_language() -> str:
    from . import paths
    try:
        return str(json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8")).get("ui_language") or "auto")
    except (OSError, ValueError):
        return "auto"


def detect() -> str:
    lang = (os.environ.get("GALAXY_LINUX_LANG") or _settings_language() or "auto").lower()
    if lang in ("de", "en"):
        return lang
    sysl = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANGUAGE") or os.environ.get("LANG") or ""
    return "de" if sysl.lower().startswith("de") else "en"


def setup(lang: str | None = None) -> str:
    """Sprache festlegen und Katalog laden (vor dem Import der Oberfläche aufrufen)."""
    global _lang, _catalog
    _lang = lang or detect()
    _catalog = {} if _lang == "de" else parse_po(LOCALE_DIR / f"{_lang}.po")
    return _lang


def current() -> str:
    return _lang


def tr(text: str) -> str:
    """Übersetzt einen deutschen Quelltext in die eingestellte Sprache (unbekannt → unverändert, ohne Kontext)."""
    hit = _catalog.get(text)
    if hit is not None:
        return hit
    return text.split(CONTEXT_SEP, 1)[1] if CONTEXT_SEP in text else text


def collect_messages(package_dir: Path | None = None) -> dict[str, list[str]]:
    """Alle Literale aus tr('…') und N_('…') im Paket mit Fundstellen (für Katalogpflege und Tests)."""
    import ast
    package_dir = package_dir or Path(__file__).parent
    out: dict[str, list[str]] = {}
    for f in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.setdefault(node.args[0].value, []).append(f"{f.relative_to(package_dir.parent)}:{node.lineno}")
    return out


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


def _po_files(where: list[str]) -> list[str]:
    """Fundstellen ohne Zeilennummern (sortiert, ohne Doppelte) – der Katalog ändert sich so nicht bei jeder
    Verschiebung im Code, und parallele Zweige geraten seltener in Konflikt."""
    return sorted(dict.fromkeys(w.rsplit(":", 1)[0] if w.rsplit(":", 1)[-1].isdigit() else w for w in where))


def write_po(path: Path, messages: dict[str, list[str]], translations: dict[str, str], lang: str) -> None:
    """Katalog schreiben: Kopf, dann je Text Fundstellen (Dateien), msgid und msgstr (leer = noch nicht übersetzt).
    Reihenfolge: nach erster Datei, dann alphabetisch – stabil gegenüber Zeilenverschiebungen."""
    lines = ['# galaxy-linux – Übersetzung der Oberfläche', '# Quelltexte: Deutsch. Pflege mit scripts/i18n-update.py', 'msgid ""', 'msgstr ""',
             '"Content-Type: text/plain; charset=UTF-8\\n"', f'"Language: {lang}\\n"', ""]
    entries = sorted(((_po_files(where), msgid) for msgid, where in messages.items()),
                     key=lambda e: (e[0][0] if e[0] else "", e[1]))
    for files, msgid in entries:
        lines.append("#: " + " ".join(files[:3]))
        if CONTEXT_SEP in msgid:
            ctxt, text = msgid.split(CONTEXT_SEP, 1)
            lines.append(f'msgctxt "{_escape(ctxt)}"')
            lines.append(f'msgid "{_escape(text)}"')
        else:
            lines.append(f'msgid "{_escape(msgid)}"')
        lines.append(f'msgstr "{_escape(translations.get(msgid, ""))}"')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
