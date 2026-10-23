"""Phasen eines Auftrags wie in Galaxys Download-Ansicht (``JobPhase``): Anzeige als Status und im Tooltip."""
from __future__ import annotations

from ..i18n import N_, tr

PHASES: tuple[tuple[str, str], ...] = (
    ("pending", N_("Ausstehend")),
    ("install_started", N_("Installation begonnen")),
    ("scanning", N_("Installierte Dateien werden gescannt")),
    ("file_list", N_("Dateiliste wird abgerufen")),
    ("allocating", N_("Speicherplatz wird zugewiesen")),
    ("downloading", N_("Wird heruntergeladen")),
    ("patching", N_("Patches werden ausgeführt")),
    ("verifying", N_("Dateien werden verifiziert")),
    ("moving", N_("Verschiebe Dateien")),
    ("setup", N_("Einrichtung wird ausgeführt")),
    ("finalizing", N_("Wird finalisiert")),
    ("cloud", N_("Cloud-Speicher wird synchronisiert")),
    ("complete", N_("Installation abgeschlossen")),
)
NAMES = dict(PHASES)
ORDER = {k: i for i, (k, _) in enumerate(PHASES)}


def label(step: str) -> str:
    return tr(NAMES[step]) if step in NAMES else ""


def tooltip(steps: list[str], current: str) -> str:
    """Bisherige Phasen mit ✓, die laufende mit ▸ – wie Galaxys Phasen-Tooltip."""
    lines = []
    for s in steps:
        if s in NAMES and s != current:
            lines.append("✓ " + label(s))
    if current in NAMES:
        lines.append("▸ " + label(current))
    return "\n".join(lines)
