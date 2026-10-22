#!/usr/bin/env bash
# Baut das pacman-Paket (ohne Installation) und legt es unter ../dist/ ab.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGVER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml")"
DIST="${1:-$HERE/../dist}"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/galaxy-linux-$PKGVER" "$DIST"
cp -r "$HERE/galaxy_linux" "$HERE/data" "$HERE/scripts" "$HERE/pyproject.toml" "$HERE/README.md" "$HERE/CHANGELOG.md" "$HERE/LICENSE" "$BUILD/galaxy-linux-$PKGVER/"
find "$BUILD/galaxy-linux-$PKGVER" -name '__pycache__' -type d -prune -exec rm -rf {} +
tar -C "$BUILD" -czf "$BUILD/galaxy-linux-$PKGVER.tar.gz" "galaxy-linux-$PKGVER"
cp "$HERE/PKGBUILD" "$BUILD/"
( cd "$BUILD" && PKGDEST="$DIST" SRCPKGDEST="$DIST" makepkg -f --noconfirm --nodeps )
cp "$BUILD/galaxy-linux-$PKGVER.tar.gz" "$DIST/galaxy-linux-$PKGVER-src.tar.gz"
install -m755 "$HERE/scripts/dist-install.sh" "$DIST/install.sh"
install -m755 "$HERE/scripts/uninstall.sh" "$DIST/uninstall.sh"
( cd "$DIST" && sha256sum galaxy-linux-*.pkg.tar.zst galaxy-linux-*-src.tar.gz install.sh uninstall.sh > SHA256SUMS )
echo "Paket liegt in: $DIST"; ls -la "$DIST"
