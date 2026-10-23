#!/usr/bin/env bash
# Eigenständiger Installer neben dem Paket (dist/):
#   ./install.sh            → vorhandene Version restlos entfernen (Paket, Nutzer-Installation, Cache), dann aktuelle installieren
#   ./install.sh --keep     → ohne vorheriges Entfernen (reines pacman -U Upgrade)
#   ./install.sh --purge    → vorher auch Einstellungen/Zugangsdaten/Logs löschen (Spiele bleiben)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(ls -t "$HERE"/galaxy-linux-*.pkg.tar.zst 2>/dev/null | head -1 || true)"
[ -n "$PKG" ] || { echo "Kein galaxy-linux-*.pkg.tar.zst neben diesem Skript gefunden."; exit 1; }
KEEP=0; PURGE=""
for a in "$@"; do case "$a" in --keep) KEEP=1;; --purge) PURGE="--purge";; esac; done
if [ -f "$HERE/SHA256SUMS" ]; then (cd "$HERE" && sha256sum -c --ignore-missing SHA256SUMS) || { echo "Prüfsumme stimmt nicht – Abbruch."; exit 1; }; fi
if [ "$KEEP" = 0 ]; then
  echo "Entferne vorhandene Version …"
  bash "$HERE/uninstall.sh" --quiet --keep-deps $PURGE
fi
echo "Installiere $(basename "$PKG") …"
sudo pacman -U --noconfirm "$PKG"
MISSING=""
for p in umu-launcher proton-cachyos mangohud lib32-mangohud goverlay winetricks; do pacman -Qq "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
[ -z "$MISSING" ] || echo "Empfohlene Pakete fehlen:$MISSING   (sudo pacman -S --needed$MISSING)"
echo "Fertig: galaxy-linux $(pacman -Q galaxy-linux | awk '{print $2}') installiert. Start: galaxy-linux · Protokoll: ~/.local/share/galaxy-linux/logs/galaxy-linux.log"
