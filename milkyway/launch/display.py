"""Bildschirm-Skalierung des Desktops ermitteln, damit Windows-Programme im Prefix lesbar bleiben.

KDE schreibt bei „Legacy-Anwendungen skalieren selbst“ ``Xft.dpi`` (z. B. 216 bei 225 %).
Fallback ist ``[Xwayland] Scale`` aus kwinrc. Ergebnis 96 = keine Anpassung.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def desktop_dpi() -> int:
    try:
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"^Xft\.dpi:\s*(\d+)", out, re.M)
        if m and int(m.group(1)) >= 96:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kwinrc"
    try:
        section = ""
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section == "Xwayland" and line.startswith("Scale="):
                scale = float(line.split("=", 1)[1])
                if scale > 1:
                    return int(round(96 * scale))
    except (OSError, ValueError):
        pass
    return 96
