#!/usr/bin/env bash
# Installation auf CachyOS / Arch Linux.
#   ./install.sh          → entfernt eine vorhandene Version restlos und installiert das Paket aus ../dist (baut es bei Bedarf)
#   ./install.sh --purge  → wie oben, löscht vorher auch Einstellungen/Zugangsdaten/Logs
#   ./uninstall: scripts/uninstall.sh [--purge] [--games]
#   ./install.sh --build  → nur das Paket bauen und in ../dist ablegen
#   ./install.sh --user   → Installation ohne Root nach ~/.local (Startskript + .desktop)
#   ./install.sh --deps   → nur fehlende Pakete anzeigen/installieren
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKGVER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml")"

REQUIRED=(python python-requests python-gobject gtk4 libadwaita webkitgtk-6.0 unzip xdg-utils)
RECOMMENDED=(umu-launcher proton-cachyos mangohud lib32-mangohud goverlay winetricks gamescope innoextract python-vdf)

missing() {
  local m=()
  for p in "$@"; do pacman -Qq "$p" >/dev/null 2>&1 || m+=("$p"); done
  printf '%s\n' "${m[@]}"
}

check_deps() {
  local req rec
  req=$(missing "${REQUIRED[@]}")
  rec=$(missing "${RECOMMENDED[@]}")
  if [ -n "$req" ]; then
    echo "Fehlende Pflichtpakete: $req"
    read -r -p "Jetzt mit pacman installieren? [J/n] " a
    if [[ "${a:-j}" =~ ^[JjYy]$ ]]; then sudo pacman -S --needed $req; else exit 1; fi
  fi
  if [ -n "$rec" ]; then
    echo "Empfohlene Pakete fehlen: $rec"
    read -r -p "Empfohlene Pakete installieren? [J/n] " a
    if [[ "${a:-j}" =~ ^[JjYy]$ ]]; then sudo pacman -S --needed $rec || true; fi
  fi
}

case "${1:-}" in
  --deps) check_deps; exit 0 ;;
  --user)
    check_deps
    DEST="$HOME/.local/lib/local-cluster"
    rm -rf "$DEST" && mkdir -p "$DEST" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
    cp -r "$HERE/local_cluster" "$DEST/"
    install -m644 "$HERE/CHANGELOG.md" "$DEST/local_cluster/CHANGELOG.md"
    install -m755 "$HERE/scripts/local-cluster" "$HOME/.local/bin/local-cluster"
    install -m644 "$HERE/data/io.github.cue_system90.LocalCluster.desktop" "$HOME/.local/share/applications/"
    install -m644 "$HERE/data/io.github.cue_system90.LocalCluster.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
    sed -i "s|^Exec=.*|Exec=$HOME/.local/bin/local-cluster %u|" "$HOME/.local/share/applications/io.github.cue_system90.LocalCluster.desktop"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "Installiert nach $DEST. Start: ~/.local/bin/local-cluster (oder über das Anwendungsmenü)."
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Hinweis: ~/.local/bin ist nicht im PATH.";; esac
    ;;
  --build)
    "$HERE/scripts/build-package.sh" "${2:-$HERE/../dist}"
    ;;
  *)
    check_deps
    # Paket genau dieser Quellversion (nach git pull wird neu gebaut statt ein älteres Paket zu nehmen)
    PKG="$(ls -t "$HERE/../dist"/local-cluster-"$PKGVER"-*-any.pkg.tar.zst 2>/dev/null | head -1 || true)"
    if [ -z "$PKG" ]; then
      "$HERE/scripts/build-package.sh" "$HERE/../dist"
      PKG="$(ls -t "$HERE/../dist"/local-cluster-"$PKGVER"-*-any.pkg.tar.zst | head -1)"
    fi
    # auch unter früheren Namen (Milkyway 0.5.x, galaxy-linux bis 0.4.1): Paket oder Nutzer-Installation
    OLD=0
    for N in local-cluster milkyway galaxy-linux; do
      if pacman -Qq "$N" >/dev/null 2>&1 || [ -d "$HOME/.local/lib/$N" ]; then OLD=1; fi
    done
    if [ "$OLD" = 1 ]; then
      echo "Entferne vorhandene Version restlos (Einstellungen bleiben; --purge löscht sie) …"
      bash "$HERE/scripts/uninstall.sh" --quiet --keep-deps $( [ "${1:-}" = "--purge" ] && echo --purge )
    fi
    echo "Installiere $PKG"
    sudo pacman -U --noconfirm "$PKG"
    echo "Fertig. Start: local-cluster (oder über das Anwendungsmenü). Protokoll: ~/.local/share/local-cluster/logs/local-cluster.log"
    for N in milkyway galaxy-linux; do
      D="${XDG_CONFIG_HOME:-$HOME/.config}/$N"
      if [ -d "$D" ] && [ ! -L "$D" ]; then
        echo "Einstellungen und Anmeldung aus $D übernimmt local:cluster beim ersten Start."
      fi
    done
    ;;
esac
