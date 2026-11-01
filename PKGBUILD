# Maintainer: Cue-System90
# Reines Python-Paket ohne Build-Werkzeuge: Quellen werden direkt nach site-packages installiert.
# Zwei Pakete mit gleichem Inhalt: milkyway und das Übergangspaket galaxy-linux (Name bis 0.4.1). Dessen
# „Jetzt aktualisieren“ lädt nur Pakete namens galaxy-linux-<ver>-… und installiert sie ohne Rückfrage – mit dem
# Übergangspaket kommt so Milkyway auf den Rechner; Milkyways eigenes Update ersetzt es dann durch milkyway.
pkgbase=milkyway
pkgname=('milkyway' 'galaxy-linux')
pkgver=0.5.0
pkgrel=1
pkgdesc="Inoffizieller nativer Client für GOG.com mit Proton-Auswahl pro Spiel und Steam-kompatiblen Startoptionen"
arch=('any')
url="https://github.com/Cue-System90/gog_like_client_Arch_linux"
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
source=("$pkgbase-$pkgver.tar.gz")
sha256sums=('SKIP')

_install_app() {
  cd "$srcdir/$pkgbase-$pkgver"
  local site
  site="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  install -d "$pkgdir$site"
  cp -r milkyway "$pkgdir$site/"
  find "$pkgdir$site/milkyway" -name '__pycache__' -type d -prune -exec rm -rf {} +
  python -m compileall -q -d "$site/milkyway" "$pkgdir$site/milkyway"

  # Metadaten, damit pip/importlib das Paket kennen
  install -d "$pkgdir$site/$pkgbase-$pkgver.dist-info"
  printf 'Metadata-Version: 2.1\nName: milkyway\nVersion: %s\nSummary: %s\nLicense: MIT\nRequires-Dist: requests\n' "$pkgver" "$pkgdesc" \
    > "$pkgdir$site/$pkgbase-$pkgver.dist-info/METADATA"
  printf 'pacman\n' > "$pkgdir$site/$pkgbase-$pkgver.dist-info/INSTALLER"
  printf '[console_scripts]\nmilkyway = milkyway.__main__:main\n' > "$pkgdir$site/$pkgbase-$pkgver.dist-info/entry_points.txt"

  install -d "$pkgdir/usr/bin"
  cat > "$pkgdir/usr/bin/milkyway" <<'LAUNCHER'
#!/usr/bin/python3
import sys
from milkyway.__main__ import main
sys.exit(main())
LAUNCHER
  chmod 755 "$pkgdir/usr/bin/milkyway"

  install -Dm644 data/io.github.cue_system90.Milkyway.desktop "$pkgdir/usr/share/applications/io.github.cue_system90.Milkyway.desktop"
  install -Dm644 data/io.github.cue_system90.Milkyway.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.cue_system90.Milkyway.svg"
  install -Dm755 scripts/install-official-galaxy-wine.sh "$pkgdir/usr/share/$pkgbase/scripts/install-official-galaxy-wine.sh"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgbase/README.md"
  # „Neu bei uns“ im Zahnrad-Menü liest das Änderungsprotokoll aus dem Paketordner
  install -Dm644 CHANGELOG.md "$pkgdir$site/milkyway/CHANGELOG.md"
  install -Dm644 CHANGELOG.md "$pkgdir/usr/share/doc/$pkgbase/CHANGELOG.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgbase/LICENSE"
}

package_milkyway() {
  provides=('galaxy-linux')
  conflicts=('galaxy-linux')
  replaces=('galaxy-linux')
  _install_app
}

package_galaxy-linux() {
  pkgdesc="Übergangspaket: galaxy-linux heißt jetzt Milkyway – das nächste Client-Update ersetzt es durch milkyway"
  conflicts=('milkyway')
  _install_app
  ln -s milkyway "$pkgdir/usr/bin/galaxy-linux"     # Neustart nach dem Update aus 0.4.x sucht dieses Programm
}
