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
    DEST="$HOME/.local/lib/galaxy-linux"
    rm -rf "$DEST" && mkdir -p "$DEST" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
    cp -r "$HERE/galaxy_linux" "$DEST/"
    install -m644 "$HERE/CHANGELOG.md" "$DEST/galaxy_linux/CHANGELOG.md"
    install -m755 "$HERE/scripts/galaxy-linux" "$HOME/.local/bin/galaxy-linux"
    install -m644 "$HERE/data/org.galaxylinux.Client.desktop" "$HOME/.local/share/applications/"
    install -m644 "$HERE/data/org.galaxylinux.Client.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
    sed -i "s|^Exec=.*|Exec=$HOME/.local/bin/galaxy-linux %u|" "$HOME/.local/share/applications/org.galaxylinux.Client.desktop"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "Installiert nach $DEST. Start: ~/.local/bin/galaxy-linux (oder über das Anwendungsmenü)."
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Hinweis: ~/.local/bin ist nicht im PATH.";; esac
    ;;
  --build)
    "$HERE/scripts/build-package.sh" "${2:-$HERE/../dist}"
    ;;
  *)
    check_deps
    # Paket genau dieser Quellversion (nach git pull wird neu gebaut statt ein älteres Paket zu nehmen)
    PKG="$(ls -t "$HERE/../dist"/galaxy-linux-"$PKGVER"-*-any.pkg.tar.zst 2>/dev/null | head -1 || true)"
    if [ -z "$PKG" ]; then
      "$HERE/scripts/build-package.sh" "$HERE/../dist"
      PKG="$(ls -t "$HERE/../dist"/galaxy-linux-"$PKGVER"-*-any.pkg.tar.zst | head -1)"
    fi
    if pacman -Qq galaxy-linux >/dev/null 2>&1 || [ -d "$HOME/.local/lib/galaxy-linux" ]; then
      echo "Entferne vorhandene Version restlos (Einstellungen bleiben; --purge löscht sie) …"
      bash "$HERE/scripts/uninstall.sh" --quiet $( [ "${1:-}" = "--purge" ] && echo --purge )
    fi
    echo "Installiere $PKG"
    sudo pacman -U --noconfirm "$PKG"
    echo "Fertig. Start: galaxy-linux (oder über das Anwendungsmenü). Protokoll: ~/.local/share/galaxy-linux/logs/galaxy-linux.log"
    ;;
esac
