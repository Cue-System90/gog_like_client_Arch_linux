#!/usr/bin/env bash
# Baut die pacman-Pakete (ohne Installation) und legt sie unter ../dist/ ab: local-cluster, dasselbe Paket als
# milkyway-… (Selbst-Update von 0.5.x) und das Übergangspaket galaxy-linux (Selbst-Update von 0.4.x, siehe PKGBUILD).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGVER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml")"
DIST="${1:-$HERE/../dist}"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/local-cluster-$PKGVER" "$DIST"
cp -r "$HERE/local_cluster" "$HERE/data" "$HERE/scripts" "$HERE/pyproject.toml" "$HERE/README.md" "$HERE/CHANGELOG.md" "$HERE/LICENSE" "$BUILD/local-cluster-$PKGVER/"
find "$BUILD/local-cluster-$PKGVER" -name '__pycache__' -type d -prune -exec rm -rf {} +
tar -C "$BUILD" -czf "$BUILD/local-cluster-$PKGVER.tar.gz" "local-cluster-$PKGVER"
cp "$HERE/PKGBUILD" "$BUILD/"
( cd "$BUILD" && PKGDEST="$DIST" SRCPKGDEST="$DIST" makepkg -f --noconfirm --nodeps )
cp "$BUILD/local-cluster-$PKGVER.tar.gz" "$DIST/local-cluster-$PKGVER-src.tar.gz"
install -m755 "$HERE/scripts/dist-install.sh" "$DIST/install.sh"
install -m755 "$HERE/scripts/uninstall.sh" "$DIST/uninstall.sh"
# dasselbe Paket local-cluster zusätzlich unter dem Namen, den Milkyway 0.5.x sucht (installiert mit --ask=4)
for f in "$DIST"/local-cluster-"$PKGVER"-*-any.pkg.tar.zst; do cp "$f" "$DIST/milkyway-${f##*/local-cluster-}"; done
( cd "$DIST" && sha256sum local-cluster-*.pkg.tar.zst milkyway-*.pkg.tar.zst galaxy-linux-*.pkg.tar.zst local-cluster-*-src.tar.gz install.sh uninstall.sh > SHA256SUMS )
echo "Paket liegt in: $DIST"; ls -la "$DIST"
