#!/usr/bin/env bash
# Entfernt local:cluster restlos: pacman-Paket, Nutzer-Installation (install.sh --user), Cache – auch unter den
# früheren Namen (Milkyway 0.5.x, galaxy-linux bis 0.4.1).
#   ./uninstall.sh            → Programm + Cache entfernen; Einstellungen, Zugangsdaten, Spielzeit, Logs bleiben
#   ./uninstall.sh --purge    → zusätzlich Einstellungen/Zugangsdaten/Daten/Logs löschen (Spiele & Prefixe bleiben)
#   ./uninstall.sh --games    → zusätzlich installierte Spiele und Wine-Prefixe (Spiele-Ordner aus den Einstellungen) löschen
#   ./uninstall.sh --keep-deps → Abhängigkeiten (webkitgtk-6.0 …) nicht mit entfernen – für Upgrades (install.sh)
set -uo pipefail
PURGE=0; GAMES=0; QUIET=0; KEEPDEPS=0
for a in "$@"; do case "$a" in --purge) PURGE=1;; --games) GAMES=1; PURGE=1;; --quiet|-q) QUIET=1;; --keep-deps) KEEPDEPS=1;; esac; done
say() { [ "$QUIET" = 1 ] || echo "$@"; }
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/local-cluster"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/local-cluster"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/local-cluster"
# frühere Namen (Milkyway 0.5.x, galaxy-linux bis 0.4.1): Ordner (nach dem Umzug Verweise), Pakete, Nutzer-Installation
NAMES=(local-cluster milkyway galaxy-linux)
IDS=(io.github.cue_system90.LocalCluster io.github.cue_system90.Milkyway org.galaxylinux.Client)

# laufende Instanz beenden – nur den Client selbst (python -m … bzw. …/bin/…), nicht dieses Skript:
# dessen Pfad kann den Modulnamen ebenfalls enthalten (z. B. „gog galaxy_linux_port/dist/install.sh“)
pkill -f -- '(-m (local_cluster|milkyway|galaxy_linux)|/bin/(local-cluster|milkyway|galaxy-linux))( |$)' 2>/dev/null && sleep 1 || true

# 1) pacman-Paket (unter jedem der Namen)
for P in "${NAMES[@]}"; do
  pacman -Qq "$P" >/dev/null 2>&1 || continue
  say "Entferne pacman-Paket $P $(pacman -Q "$P" | awk '{print $2}') …"
  if [ "$KEEPDEPS" = 1 ]; then
    # Upgrade: -s würde webkitgtk-6.0 samt Abhängigkeiten entfernen, die pacman -U gleich wieder lädt
    sudo pacman -Rn --noconfirm "$P" || sudo pacman -R --noconfirm "$P"
  else
    sudo pacman -Rns --noconfirm "$P" || sudo pacman -R --noconfirm "$P"
  fi
done
# 2) Nutzer-Installation (install.sh --user) und Reste früherer Varianten
for N in "${NAMES[@]}"; do
  rm -rf "$HOME/.local/lib/$N"
  rm -f "$HOME/.local/bin/$N"
done
for I in "${IDS[@]}"; do
  rm -f "$HOME/.local/share/applications/$I.desktop" "$HOME/.local/share/icons/hicolor/scalable/apps/$I.svg"
done
# verwaiste Systemdateien (falls ein Paket unsauber entfernt wurde)
for f in /usr/bin/local-cluster /usr/bin/milkyway /usr/bin/galaxy-linux \
         /usr/share/applications/io.github.cue_system90.LocalCluster.desktop \
         /usr/share/icons/hicolor/scalable/apps/io.github.cue_system90.LocalCluster.svg; do
  if { [ -e "$f" ] || [ -L "$f" ]; } && ! pacman -Qo "$f" >/dev/null 2>&1; then sudo rm -f "$f"; fi
done
for d in /usr/lib/python3*/site-packages/local_cluster /usr/lib/python3*/site-packages/local-cluster-*.dist-info /usr/share/local-cluster /usr/share/doc/local-cluster /usr/share/licenses/local-cluster; do
  if [ -e "$d" ] && ! pacman -Qo "$d" >/dev/null 2>&1; then sudo rm -rf "$d"; fi
done
# 3) Cache immer (auch Verweise unter früheren Namen)
for N in "${NAMES[@]}"; do rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/$N"; done
# 4) Einstellungen/Daten optional
if [ "$PURGE" = 1 ]; then
  SET=""
  for N in "${NAMES[@]}"; do
    [ -z "$SET" ] && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/$N/settings.json" ] && SET="${XDG_CONFIG_HOME:-$HOME/.config}/$N/settings.json"
  done
  if [ "$GAMES" = 1 ] && [ -n "$SET" ]; then
    GAMES_DIR="$(python3 -c "import json,os;d=json.load(open('$SET'));print(os.path.expanduser(d.get('install_dir','')))" 2>/dev/null || true)"
    PREFIX_DIR="$(python3 -c "import json,os;d=json.load(open('$SET'));print(os.path.expanduser(d.get('prefix_dir','')))" 2>/dev/null || true)"
    for x in "$PREFIX_DIR" "$GAMES_DIR"; do
      if [ -n "$x" ] && [ -d "$x" ] && [ "$x" != "$HOME" ] && [ "$x" != "/" ]; then say "Lösche $x …"; rm -rf "$x"; fi
    done
  fi
  for N in "${NAMES[@]}"; do
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/$N" "${XDG_DATA_HOME:-$HOME/.local/share}/$N" "$HOME/.local/share/$N"
  done
  say "Einstellungen, Zugangsdaten, Spielzeit und Logs entfernt."
else
  say "Einstellungen, Zugangsdaten und Logs bleiben unter $CFG und $DATA (löschen mit --purge)."
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
say "local:cluster wurde entfernt."
