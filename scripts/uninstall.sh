#!/usr/bin/env bash
# Entfernt galaxy-linux restlos: pacman-Paket, Nutzer-Installation (install.sh --user), Cache.
#   ./uninstall.sh            → Programm + Cache entfernen; Einstellungen, Zugangsdaten, Spielzeit, Logs bleiben
#   ./uninstall.sh --purge    → zusätzlich Einstellungen/Zugangsdaten/Daten/Logs löschen (Spiele & Prefixe bleiben)
#   ./uninstall.sh --games    → zusätzlich installierte Spiele und Wine-Prefixe (Spiele-Ordner aus den Einstellungen) löschen
set -uo pipefail
PURGE=0; GAMES=0; QUIET=0
for a in "$@"; do case "$a" in --purge) PURGE=1;; --games) GAMES=1; PURGE=1;; --quiet|-q) QUIET=1;; esac; done
say() { [ "$QUIET" = 1 ] || echo "$@"; }
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/galaxy-linux"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/galaxy-linux"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/galaxy-linux"

# laufende Instanz beenden – nur den Client selbst (python -m galaxy_linux bzw. …/bin/galaxy-linux), nicht dieses Skript:
# dessen Pfad kann den Modulnamen ebenfalls enthalten (z. B. „gog galaxy_linux_port/dist/install.sh“)
pkill -f -- '(-m galaxy_linux|/bin/galaxy-linux)( |$)' 2>/dev/null && sleep 1 || true

# 1) pacman-Paket
if pacman -Qq galaxy-linux >/dev/null 2>&1; then
  say "Entferne pacman-Paket galaxy-linux $(pacman -Q galaxy-linux | awk '{print $2}') …"
  sudo pacman -Rns --noconfirm galaxy-linux || sudo pacman -R --noconfirm galaxy-linux
fi
# 2) Nutzer-Installation (install.sh --user) und Reste früherer Varianten
rm -rf "$HOME/.local/lib/galaxy-linux"
rm -f "$HOME/.local/bin/galaxy-linux" "$HOME/.local/share/applications/org.galaxylinux.Client.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/org.galaxylinux.Client.svg"
# verwaiste Systemdateien (falls ein Paket unsauber entfernt wurde)
for f in /usr/bin/galaxy-linux /usr/share/applications/org.galaxylinux.Client.desktop /usr/share/icons/hicolor/scalable/apps/org.galaxylinux.Client.svg; do
  if [ -e "$f" ] && ! pacman -Qo "$f" >/dev/null 2>&1; then sudo rm -f "$f"; fi
done
for d in /usr/lib/python3*/site-packages/galaxy_linux /usr/lib/python3*/site-packages/galaxy-linux-*.dist-info /usr/share/galaxy-linux /usr/share/doc/galaxy-linux /usr/share/licenses/galaxy-linux; do
  if [ -e "$d" ] && ! pacman -Qo "$d" >/dev/null 2>&1; then sudo rm -rf "$d"; fi
done
# 3) Cache immer
rm -rf "$CACHE"
# 4) Einstellungen/Daten optional
if [ "$PURGE" = 1 ]; then
  if [ "$GAMES" = 1 ] && [ -f "$CFG/settings.json" ]; then
    GAMES_DIR="$(python3 -c "import json,os;d=json.load(open('$CFG/settings.json'));print(os.path.expanduser(d.get('install_dir','')))" 2>/dev/null || true)"
    PREFIX_DIR="$(python3 -c "import json,os;d=json.load(open('$CFG/settings.json'));print(os.path.expanduser(d.get('prefix_dir','')))" 2>/dev/null || true)"
    for x in "$PREFIX_DIR" "$GAMES_DIR"; do
      if [ -n "$x" ] && [ -d "$x" ] && [ "$x" != "$HOME" ] && [ "$x" != "/" ]; then say "Lösche $x …"; rm -rf "$x"; fi
    done
  fi
  rm -rf "$CFG" "$DATA" "$HOME/.local/share/galaxy-linux"
  say "Einstellungen, Zugangsdaten, Spielzeit und Logs entfernt."
else
  say "Einstellungen, Zugangsdaten und Logs bleiben unter $CFG und $DATA (löschen mit --purge)."
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
say "galaxy-linux wurde entfernt."
