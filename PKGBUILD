# Maintainer: lokal (CachyOS)
# Reines Python-Paket ohne Build-Werkzeuge: Quellen werden direkt nach site-packages installiert.
pkgname=galaxy-linux
pkgver=0.4.1
pkgrel=1
pkgdesc="Inoffizieller nativer Client für GOG.com mit Proton-Auswahl pro Spiel und Steam-kompatiblen Startoptionen"
arch=('any')
url="https://www.gog.com/galaxy"
license=('MIT')
depends=('python>=3.11' 'python-requests' 'python-gobject' 'gtk4' 'libadwaita' 'webkitgtk-6.0' 'unzip' 'xdg-utils')
optdepends=(
  'umu-launcher: Proton außerhalb von Steam (empfohlen, nötig für proton-cachyos-slr)'
  'proton-cachyos: CachyOS-Proton-Build'
  'wine: System-Wine als Alternative zu Proton'
  'winetricks: Zusatzkomponenten im Prefix'
  'mangohud: FPS-Overlay (GOverlay)'
  'lib32-mangohud: MangoHud für 32-Bit-Spiele'
  'goverlay: grafische MangoHud-Konfiguration'
  'gamescope: Micro-Compositor'
  'gamemode: Leistungsmodus (gamemoderun)'
  'innoextract: Offline-Installer für Spiele ohne Galaxy-Build'
  'python-vdf: Steam-VDF-Dateien lesen'
  'python-pillow: Bildkonvertierung'
  'polkit: „Jetzt aktualisieren“ installiert Client-Updates über pkexec'
)
source=("$pkgname-$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
  cd "$srcdir/$pkgname-$pkgver"
  local site
  site="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  install -d "$pkgdir$site"
  cp -r galaxy_linux "$pkgdir$site/"
  find "$pkgdir$site/galaxy_linux" -name '__pycache__' -type d -prune -exec rm -rf {} +
  python -m compileall -q -d "$site/galaxy_linux" "$pkgdir$site/galaxy_linux"

  # Metadaten, damit pip/importlib das Paket kennen
  install -d "$pkgdir$site/$pkgname-$pkgver.dist-info"
  printf 'Metadata-Version: 2.1\nName: galaxy-linux\nVersion: %s\nSummary: %s\nLicense: MIT\nRequires-Dist: requests\n' "$pkgver" "$pkgdesc" \
    > "$pkgdir$site/$pkgname-$pkgver.dist-info/METADATA"
  printf 'pacman\n' > "$pkgdir$site/$pkgname-$pkgver.dist-info/INSTALLER"
  printf '[console_scripts]\ngalaxy-linux = galaxy_linux.__main__:main\n' > "$pkgdir$site/$pkgname-$pkgver.dist-info/entry_points.txt"

  install -d "$pkgdir/usr/bin"
  cat > "$pkgdir/usr/bin/galaxy-linux" <<'LAUNCHER'
#!/usr/bin/python3
import sys
from galaxy_linux.__main__ import main
sys.exit(main())
LAUNCHER
  chmod 755 "$pkgdir/usr/bin/galaxy-linux"

  install -Dm644 data/org.galaxylinux.Client.desktop "$pkgdir/usr/share/applications/org.galaxylinux.Client.desktop"
  install -Dm644 data/org.galaxylinux.Client.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/org.galaxylinux.Client.svg"
  install -Dm755 scripts/install-official-galaxy-wine.sh "$pkgdir/usr/share/$pkgname/scripts/install-official-galaxy-wine.sh"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  # „Neu bei uns“ im Zahnrad-Menü liest das Änderungsprotokoll aus dem Paketordner
  install -Dm644 CHANGELOG.md "$pkgdir$site/galaxy_linux/CHANGELOG.md"
  install -Dm644 CHANGELOG.md "$pkgdir/usr/share/doc/$pkgname/CHANGELOG.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
