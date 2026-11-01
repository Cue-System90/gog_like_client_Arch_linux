#!/usr/bin/env bash
# Baut die pacman-Pakete (ohne Installation) und legt sie unter ../dist/ ab: milkyway und das Übergangspaket
# galaxy-linux (für das Selbst-Update von 0.4.x, siehe PKGBUILD).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGVER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml")"
DIST="${1:-$HERE/../dist}"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/milkyway-$PKGVER" "$DIST"
cp -r "$HERE/milkyway" "$HERE/data" "$HERE/scripts" "$HERE/pyproject.toml" "$HERE/README.md" "$HERE/CHANGELOG.md" "$HERE/LICENSE" "$BUILD/milkyway-$PKGVER/"
find "$BUILD/milkyway-$PKGVER" -name '__pycache__' -type d -prune -exec rm -rf {} +
tar -C "$BUILD" -czf "$BUILD/milkyway-$PKGVER.tar.gz" "milkyway-$PKGVER"
cp "$HERE/PKGBUILD" "$BUILD/"
( cd "$BUILD" && PKGDEST="$DIST" SRCPKGDEST="$DIST" makepkg -f --noconfirm --nodeps )
cp "$BUILD/milkyway-$PKGVER.tar.gz" "$DIST/milkyway-$PKGVER-src.tar.gz"
install -m755 "$HERE/scripts/dist-install.sh" "$DIST/install.sh"
install -m755 "$HERE/scripts/uninstall.sh" "$DIST/uninstall.sh"
( cd "$DIST" && sha256sum milkyway-*.pkg.tar.zst galaxy-linux-*.pkg.tar.zst milkyway-*-src.tar.gz install.sh uninstall.sh > SHA256SUMS )
echo "Paket liegt in: $DIST"; ls -la "$DIST"
