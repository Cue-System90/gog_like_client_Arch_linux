#!/usr/bin/env bash
# Veröffentlichung für den Maintainer: baut das pacman-Paket mit scripts/build-package.sh in einen frischen Ordner und
# legt mit gh das GitHub-Release v<version> an – mit dem Paket galaxy-linux-<ver>-<rel>-any.pkg.tar.zst und
# SHA256SUMS; die Versionshinweise sind der Abschnitt „## <version> …“ aus CHANGELOG.md.
#
# NUR NACH FREIGABE AUSFÜHREN: „Nach Updates suchen“ und die tägliche Suche aller Nutzer finden das Release sofort,
# „Jetzt aktualisieren“ installiert dann dieses Paket (über pkexec pacman -U).
#
#   scripts/release.sh --dry-run   nur bauen, prüfen und zeigen, was veröffentlicht würde (ohne gh)
#   scripts/release.sh             bauen, prüfen, nachfragen, veröffentlichen
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="Cue-System90/gog_like_client_Arch_linux"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml")"
PKGVER="$(sed -n 's/^pkgver=//p' "$HERE/PKGBUILD")"
PKGREL="$(sed -n 's/^pkgrel=//p' "$HERE/PKGBUILD")"
PYVER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/galaxy_linux/__init__.py")"
TAG="v$VER"
die() { echo "release.sh: $*" >&2; exit 1; }

# Versionen müssen überall gleich sein, die Version braucht einen fertigen CHANGELOG-Abschnitt
[ -n "$VER" ] || die "keine Version in pyproject.toml"
[ "$PKGVER" = "$VER" ] || die "PKGBUILD pkgver=$PKGVER, pyproject.toml $VER"
[ "$PYVER" = "$VER" ] || die "galaxy_linux/__init__.py $PYVER, pyproject.toml $VER"
grep -q "^## $VER (" "$HERE/CHANGELOG.md" || die "CHANGELOG.md hat keinen Abschnitt „## $VER (…)“"
grep -q "^## $VER (in Arbeit)" "$HERE/CHANGELOG.md" && die "CHANGELOG.md: $VER ist noch „in Arbeit“"
[ -z "$(git -C "$HERE" status --porcelain)" ] || die "Arbeitskopie hat ungesicherte Änderungen"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
"$HERE/scripts/build-package.sh" "$WORK/dist" >/dev/null
PKG="$WORK/dist/galaxy-linux-$VER-$PKGREL-any.pkg.tar.zst"
SUMS="$WORK/dist/SHA256SUMS"
[ -f "$PKG" ] || die "Paket fehlt: $PKG"
( cd "$WORK/dist" && sha256sum --quiet -c SHA256SUMS ) || die "SHA256SUMS passt nicht zu den gebauten Dateien"
grep -q " $(basename "$PKG")\$" "$SUMS" || die "SHA256SUMS enthält das Paket nicht"

NOTES="$WORK/notes.md"
python3 - "$HERE" "$VER" "$NOTES" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from galaxy_linux import clientinfo
section, _older = clientinfo.changelog_section(Path(sys.argv[1], "CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[2])
if not section:
    sys.exit("kein CHANGELOG-Abschnitt")
body = section.split("\n", 1)[1].strip() if "\n" in section else ""
Path(sys.argv[3]).write_text(body + "\n", encoding="utf-8")
PY

echo "Release $TAG für $REPO"
echo "  Paket:       $(basename "$PKG") ($(du -h "$PKG" | cut -f1))"
echo "  SHA256SUMS:"; sed 's/^/    /' "$SUMS"
echo "  Hinweise:    $(wc -l < "$NOTES") Zeilen aus CHANGELOG.md"
if [ "$DRY" = 1 ]; then
  echo "--dry-run: nichts veröffentlicht."; sed 's/^/  | /' "$NOTES"; exit 0
fi

command -v gh >/dev/null || die "gh (github-cli) ist nicht installiert"
gh auth status >/dev/null 2>&1 || die "gh ist nicht angemeldet (gh auth login)"
# Das Release-Tag zeigt auf HEAD – der Stand muss auf GitHub liegen
[ -n "$(git -C "$HERE" branch -r --contains HEAD 2>/dev/null)" ] || die "HEAD ist noch nicht gepusht"
gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 && die "Release $TAG gibt es schon"

read -r -p "Release $TAG jetzt auf GitHub veröffentlichen? [j/N] " answer
[ "$answer" = "j" ] || [ "$answer" = "J" ] || die "abgebrochen"
gh release create "$TAG" "$PKG" "$SUMS" --repo "$REPO" --target "$(git -C "$HERE" rev-parse HEAD)" \
  --title "galaxy-linux $VER" --notes-file "$NOTES"
echo "Veröffentlicht: https://github.com/$REPO/releases/tag/$TAG"
