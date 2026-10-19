#!/usr/bin/env bash
# EXPERIMENTELL / NICHT UNTERSTÜTZT:
# Installiert den offiziellen GOG-Galaxy-Windows-Client (GOG_Galaxy_2.0.exe) in ein eigenes Wine-Prefix.
# Die EXE im Projektordner ist nur der Web-Installer-Stub (~1 MB), der den eigentlichen Client nachlädt.
# Der echte Linux-Port ist galaxy-linux (nativ). Dieses Skript ist nur ein Zusatz für Experimente.
set -euo pipefail
EXE="${1:-$(dirname "$0")/../../GOG_Galaxy_2.0.exe}"
PREFIX="${GALAXY_WINE_PREFIX:-$HOME/Games/GOG/official-galaxy-wine}"
command -v wine >/dev/null || { echo "wine fehlt (sudo pacman -S wine)"; exit 1; }
[ -f "$EXE" ] || { echo "Installer nicht gefunden: $EXE"; exit 1; }
export WINEPREFIX="$PREFIX" WINEARCH=win64 WINEDEBUG=-all
mkdir -p "$PREFIX"
echo "Prefix: $PREFIX"
wineboot -u
if command -v winetricks >/dev/null; then
  # Galaxy 2.0 (Chromium Embedded Framework) braucht übliche Grundlagen
  winetricks -q win10 corefonts vcrun2019 || true
fi
echo "Starte den GOG-Web-Installer … (lädt den Client aus dem Internet nach)"
wine "$EXE" || true
cat <<MSG

Falls die Installation durchlief, startet der Client mit:
  WINEPREFIX="$PREFIX" wine "$PREFIX/drive_c/Program Files (x86)/GOG Galaxy/GalaxyClient.exe"
Hinweis: Der Windows-Client unter Wine ist bekannt instabil (CEF/Chromium, Overlay, Updater).
Spiele, die er installiert, laufen im selben Wine-Prefix ohne Proton-Auswahl – dafür ist galaxy-linux da.
MSG
