#!/usr/bin/env python3
"""Übersetzungskatalog pflegen.

Sammelt alle tr('…')/N_('…')-Texte aus galaxy_linux/ und schreibt galaxy_linux/locale/<sprache>.po neu:
vorhandene Übersetzungen bleiben, neue Texte kommen mit leerem msgstr dazu (erscheinen bis dahin Deutsch),
nicht mehr verwendete fallen weg.

    scripts/i18n-update.py            # en
    scripts/i18n-update.py fr         # neuen Katalog anlegen
    scripts/i18n-update.py en --merge uebersetzungen.json   # {"deutscher Text": "Übersetzung", …} einspielen
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from galaxy_linux import i18n  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("lang", nargs="?", default="en")
    ap.add_argument("--merge", action="append", default=[], help="JSON-Datei mit Übersetzungen (mehrfach möglich)")
    a = ap.parse_args()
    po = i18n.LOCALE_DIR / f"{a.lang}.po"
    messages = i18n.collect_messages()
    known = i18n.parse_po(po)
    for f in a.merge:
        known.update({k: v for k, v in json.loads(Path(f).read_text(encoding="utf-8")).items() if v})
    i18n.write_po(po, messages, known, a.lang)
    missing = [m for m in messages if m not in known]
    obsolete = [m for m in known if m not in messages]
    print(f"{po.relative_to(ROOT)}: {len(messages)} Texte, {len(messages) - len(missing)} übersetzt, "
          f"{len(missing)} offen, {len(obsolete)} entfernt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
