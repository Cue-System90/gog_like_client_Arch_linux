#!/usr/bin/env bash
# Entfernt Milkyway restlos: pacman-Paket, Nutzer-Installation (install.sh --user), Cache – auch unter dem Namen
# bis 0.4.1 (galaxy-linux).
#   ./uninstall.sh            → Programm + Cache entfernen; Einstellungen, Zugangsdaten, Spielzeit, Logs bleiben
#   ./uninstall.sh --purge    → zusätzlich Einstellungen/Zugangsdaten/Daten/Logs löschen (Spiele & Prefixe bleiben)
#   ./uninstall.sh --games    → zusätzlich installierte Spiele und Wine-Prefixe (Spiele-Ordner aus den Einstellungen) löschen
#   ./uninstall.sh --keep-deps → Abhängigkeiten (webkitgtk-6.0 …) nicht mit entfernen – für Upgrades (install.sh)
set -uo pipefail
PURGE=0; GAMES=0; QUIET=0; KEEPDEPS=0
for a in "$@"; do case "$a" in --purge) PURGE=1;; --games) GAMES=1; PURGE=1;; --quiet|-q) QUIET=1;; --keep-deps) KEEPDEPS=1;; esac; done
say() { [ "$QUIET" = 1 ] || echo "$@"; }
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/milkyway"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/milkyway"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/milkyway"
LEGACY="galaxy-linux"     # Name bis 0.4.1: Ordner (nach dem Umzug Verweise), Paket, Nutzer-Installation
LCFG="${XDG_CONFIG_HOME:-$HOME/.config}/$LEGACY"
LDATA="${XDG_DATA_HOME:-$HOME/.local/share}/$LEGACY"
LCACHE="${XDG_CACHE_HOME:-$HOME/.cache}/$LEGACY"

# laufende Instanz beenden – nur den Client selbst (python -m milkyway bzw. …/bin/milkyway), nicht dieses Skript:
# dessen Pfad kann den Modulnamen ebenfalls enthalten (z. B. „gog milkyway_port/dist/install.sh“)
pkill -f -- '(-m (milkyway|galaxy_linux)|/bin/(milkyway|galaxy-linux))( |$)' 2>/dev/null && sleep 1 || true

# 1) pacman-Paket (milkyway oder galaxy-linux)
for P in milkyway "$LEGACY"; do
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
rm -rf "$HOME/.local/lib/milkyway" "$HOME/.local/lib/$LEGACY"
rm -f "$HOME/.local/bin/milkyway" "$HOME/.local/share/applications/io.github.cue_system90.Milkyway.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.cue_system90.Milkyway.svg" \
      "$HOME/.local/bin/$LEGACY" "$HOME/.local/share/applications/org.galaxylinux.Client.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/org.galaxylinux.Client.svg"
# verwaiste Systemdateien (falls ein Paket unsauber entfernt wurde)
for f in /usr/bin/milkyway /usr/share/applications/io.github.cue_system90.Milkyway.desktop /usr/share/icons/hicolor/scalable/apps/io.github.cue_system90.Milkyway.svg; do
  if [ -e "$f" ] && ! pacman -Qo "$f" >/dev/null 2>&1; then sudo rm -f "$f"; fi
done
for d in /usr/lib/python3*/site-packages/milkyway /usr/lib/python3*/site-packages/milkyway-*.dist-info /usr/share/milkyway /usr/share/doc/milkyway /usr/share/licenses/milkyway; do
  if [ -e "$d" ] && ! pacman -Qo "$d" >/dev/null 2>&1; then sudo rm -rf "$d"; fi
done
# 3) Cache immer
rm -rf "$CACHE" "$LCACHE"
# 4) Einstellungen/Daten optional
if [ "$PURGE" = 1 ]; then
  SET="$CFG/settings.json"; [ -f "$SET" ] || SET="$LCFG/settings.json"
  if [ "$GAMES" = 1 ] && [ -f "$SET" ]; then
    GAMES_DIR="$(python3 -c "import json,os;d=json.load(open('$SET'));print(os.path.expanduser(d.get('install_dir','')))" 2>/dev/null || true)"
    PREFIX_DIR="$(python3 -c "import json,os;d=json.load(open('$SET'));print(os.path.expanduser(d.get('prefix_dir','')))" 2>/dev/null || true)"
    for x in "$PREFIX_DIR" "$GAMES_DIR"; do
      if [ -n "$x" ] && [ -d "$x" ] && [ "$x" != "$HOME" ] && [ "$x" != "/" ]; then say "Lösche $x …"; rm -rf "$x"; fi
    done
  fi
  rm -rf "$CFG" "$DATA" "$HOME/.local/share/milkyway" "$LCFG" "$LDATA"
  say "Einstellungen, Zugangsdaten, Spielzeit und Logs entfernt."
else
  say "Einstellungen, Zugangsdaten und Logs bleiben unter $CFG und $DATA (löschen mit --purge)."
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
say "milkyway wurde entfernt."
