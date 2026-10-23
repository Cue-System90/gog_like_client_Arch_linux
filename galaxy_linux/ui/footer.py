"""Fußzeile der Seitenleiste: „N Update(s) verfügbar“ ist ein Knopf – ein Klick öffnet die Downloads bei
„Erhältliche Updates“. Ohne GTK (testbar mit einem Ersatzfenster)."""
from __future__ import annotations

from ..i18n import tr


def updates_text(count: int) -> str:
    """Text des Knopfs ("" = Knopf ausgeblendet)."""
    return tr('{0} Update(s) verfügbar').format(count) if count > 0 else ""


def open_available_updates(win) -> None:
    """Downloads zeigen und zu „Erhältliche Updates“ rollen (der erste Update-Knopf bekommt den Fokus)."""
    win.navigate("downloads")
    win.downloads.reveal_updates()
