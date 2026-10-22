# galaxy-linux – inoffizieller GOG-Client für Linux

Ein inoffizieller, **nativer** Client für **GOG.com** auf **CachyOS / Arch Linux**, der sich so
benutzen lässt wie der native Steam-Client: Bibliothek, Installation über GOGs eigenes
Content-System, **Proton-Version pro Spiel**, **Steam-kompatible Startoptionen** (z. B. die Zeile,
die **GOverlay** erzeugt), Spielzeit, Erfolge, Updates, Prefix-Werkzeuge – als GTK4/libadwaita-App
und als Kommandozeile.

> **Warum kein „Port der EXE“?** `GOG_Galaxy_2.0.exe` ist ein 964 KB großer, UPX-gepackter
> Web-Installer-Stub, der den eigentlichen (geschlossenen) Windows-Client nur nachlädt. Es gibt keinen
> Quellcode, den man portieren könnte. Dieses Projekt baut den Client deshalb nativ nach und spricht
> **dieselben GOG-Dienste** wie Galaxy (Login über auth.gog.com, Bibliothek, Content-System v2 mit
> Manifesten/Depots/Chunks, GamesDB-Metadaten, Gameplay-API für Spielzeit und Erfolge).
> Ein optionales Skript (`scripts/install-official-galaxy-wine.sh`) installiert den Original-Client
> zusätzlich in ein Wine-Prefix – experimentell und nicht nötig.

GOG und GOG Galaxy sind Marken von GOG sp. z o.o. Dieses Projekt steht in keiner Verbindung zu GOG.

**Haftungsausschluss:** inoffizielles Projekt, Nutzung auf eigenes Risiko. Die GOG-Nutzungsvereinbarung
untersagt nicht genehmigte Drittprogramme; GOG duldet Open-Source-Clients wie Heroic, Minigalaxy oder Lutris
seit Jahren, ein Anspruch darauf besteht aber nicht. Das Repository enthält keinen Code und keine Grafiken von GOG.

**English:** complete documentation in [README.en.md](README.en.md).

## Oberfläche

Der Aufbau folgt GOG Galaxy 2.0:

* **Titelleiste**: links das **Zahnrad-Menü** wie in Galaxy 2.1 (Spiele & Freunde hinzufügen ▸, **Bibliothek
  aktualisieren** – auch F5/Strg+R –, Einstellungen, Hilfe erhalten, Neu bei uns, Nach Updates suchen, Rechtliches und
  Datenschutz, Beenden) · **Avatar-Menü** (Kontoverwaltung, Bisherige Bestellungen, Geldbeutel, Ausloggen) · Suche
  (über der Seitenleiste); rechts Zurück/Vor, Lesezeichen (Favorit), **Filter**, **+** (Spiel hinzufügen, Bibliothek
  anlegen, Ordner durchsuchen, Prefix-Pflege), Suchfeld „Spiele suchen“, Ansicht (Sortierung, Covergröße, Titel
  ein/aus) und die Fensterknöpfe.
* **Seitenleiste** in der Reihenfolge des Originals: GOG STORE → Startseite, Alle Spiele, Einkaufen (Angebote ohne
  eigene Spiele), Wunschliste (Zähler), Warenkorb, Bisherige Bestellungen (ausblendbar: Einstellungen → Oberfläche →
  Bevorzugter Store); SPIELE → Zuletzt gespielt, Alle Spiele (Zähler), Installiert (Zähler), Downloads (Zähler);
  BIBLIOTHEKEN → je eigene Bibliothek ein Eintrag mit Zähler (wie die Plattform-Einträge in Galaxy 2.0), „+“ legt eine
  neue an, Rechtsklick öffnet ihr Menü; FREUNDE (Aktivität, Freunde online, Nachrichten – im Port nicht verfügbar,
  daher ausgegraut).
* **Store**: wie im Original die echten gog.com-Seiten im Inhaltsbereich, mit schmaler Leiste (Zurück, Vor, Neu laden,
  Startseite, Adresse). Login und Store teilen sich die Cookies.
* **Bibliothek**: Hochkant-Cover ohne Beschriftung in Abschnitten („Als Nächstes spielen“, „Alle Spiele“,
  „Zuletzt gespielt“, „Kürzlich installiert“), Tooltip mit Titel, Fortschrittsbalken auf dem Cover während des Downloads.
* **Spielansicht**: Hero-Artwork mit transparentem Spiel-Logo, darunter der violette **SPIELEN**/**INSTALLIEREN**-Knopf,
  „⋯“-Menü (Prüfen, Ordner, Prefix, Winetricks, Protokoll, Im Store anzeigen, Verstecken, Deinstallieren), Kennzahlen
  SPIELZEIT · ZULETZT GESPIELT · ERFOLGE · KOMPATIBILITÄT, dann „Über das Spiel“ (Genres, Beschreibung, DLCs),
  Erfolge als Symbolreihe und „Spieldetails“. Zusätzlich – wie bei Steam – ein Block **Kompatibilität** mit
  Proton-Auswahl, Startoptionen, GOverlay-Profil des Spiels, „GOverlay öffnen“ und Befehlsvorschau; das Zahnrad öffnet
  die vollständigen Eigenschaften.
* **Downloads**: Warteschlange mit Fortschritt, Geschwindigkeit, Restzeit, Abbrechen und Verlauf.
* URIs: `galaxy-linux://game/<id>`, `galaxy-linux://launch/<id>`, `galaxy-linux://view/{all|recent|installed|downloads}`,
  `galaxy-linux://view/library/<Bibliotheks-ID>`.

---

## Funktionen

| Bereich | Umsetzung |
|---|---|
| Login | OAuth wie im Galaxy-Client (Browser-Login, Weiterleitungs-URL einfügen, 2FA funktioniert). Token-Refresh automatisch. |
| Store | **Eingebetteter GOG-Store** wie in Galaxy (WebKitGTK 6.0, gleiche Sitzung wie der Login): Seitenleiste GOG STORE, Seiten in der Sprache der Oberfläche, Wunschlisten-Zähler. Kauf und Bezahlung im Client (Zahlungs-Fenster wie PayPal öffnen in einem eigenen Fenster); nach dem **Kaufabschluss** wird die Bibliothek sofort abgeglichen. Offline-Installer und Goodies landen im Download-Ordner, Links auf fremde Seiten öffnen im Browser. Ist die Website nicht angemeldet, bietet ein Hinweis „Im Store anmelden“ an. Spielansicht „⋯“ → **Im Store anzeigen**. Ausloggen meldet auch den Store ab. Kein eigener User-Agent, keine Partner-Links. |
| Bibliothek | Alle gekauften Spiele mit Cover, Hintergrund, Beschreibung, Genres, Entwickler/Publisher, Release, DLC-Besitz (GamesDB + api.gog.com). Suche, Filter (installiert, Favoriten, Updates, Windows, Linux-nativ), Verstecken. **Abgleich mit GOG** wie bei Galaxy ohne Neustart: kurz nach dem Start, alle 10 Minuten, beim Wechsel ins Fenster (frühestens alle 5 Minuten), nach dem Login und nach einem Kauf (dann erneut nach 5/15/60 s); von Hand über Zahnrad → „Bibliothek aktualisieren“ (F5). Die Prüfsumme aus `userData.json` spart den Vollabruf, solange sich nichts geändert hat; auf GOG versteckte Spiele werden mitgeladen. Neue Spiele tragen 14 Tage (bis zum ersten Öffnen) ein violettes **NEU** und stehen oben unter „Neu in der Bibliothek“. Einstellbar unter Einstellungen → Allgemein → Abgleich mit GOG. |
| Installationsort | Wie im Windows-Client: globaler Spiele-Ordner in den Einstellungen (Standard `~/Games/GOG`), Spiele landen dort unter `<Ordner>/<Spielname>`. Im Installations-Dialog wählbar: Standard, gemerkte weitere Orte oder „Anderer Ordner …“; der fertige Zielpfad wird angezeigt. **Installation verschieben** (Spielseite-Menü, Eigenschaften, CLI `move`) – sofort auf demselben Laufwerk, sonst Kopie mit Fortschritt. |
| Kopierte Spiele | Spiele, die von Hand in den Spiele-Ordner (oder einen weiteren Installationsort) kopiert werden – z. B. aus einer Windows-Installation von GOG Galaxy – erkennt der Client automatisch an ihrer `goggame-<ID>.info`, auch eine Ebene tiefer (kopierter Sammelordner „GOG Games“). Er wartet, bis der Kopiervorgang abgeschlossen ist, übernimmt Spiel und DLCs als installiert (Zähler „Installiert“ passt sich an) und legt fehlende Wine-Prefixe im Prefix-Ordner an, mit der DPI der Desktop-Skalierung (225 % → 216). Gelöschte Spielordner werden wieder ausgetragen, der Prefix mit Spielständen bleibt. Manuell: „+“ → „Spiele-Ordner nach Spielen durchsuchen“ oder `galaxy-linux scan`. Abschaltbar in den Einstellungen. |
| Bibliotheksansicht | Über den Ansichtsknopf (rechts neben der Suche): **Raster** oder **Liste** (Spalten Titel, Status, Spielzeit, zuletzt gespielt, Größe), Covergröße XS–XL, Abstand zwischen den Covern, Titel unter den Covern, Abschnitte ein/aus, Sortierung – alles wird gespeichert: gemeinsam für Alle Spiele/Installiert/Zuletzt gespielt und **getrennt für jede eigene Bibliothek** (das Menü zeigt oben, wofür es gilt). Cover sind echte Hochkant-Boxart aus GOGs Spieledatenbank (die Bibliotheksbilder selbst sind Querformat und würden beschnitten). |
| Eigene Bibliotheken | Beliebig viele Bibliotheken für Spiele, die nicht von GOG stammen (andere Stores, Emulatoren, alte CDs, eigene Installationen …). **Anlegen**: „+“ neben BIBLIOTHEKEN in der Seitenleiste, „+“ in der Titelleiste → „Bibliothek anlegen …“ oder Einstellungen → Bibliotheken – Name, **Spiele-Ordner** und **Prefix-Ordner** (leer = `<Spiele-Ordner>/prefixes`). Jeder Unterordner des Spiele-Ordners wird ein Spiel mit eigenem Prefix im Prefix-Ordner (benannt wie der Spielordner). Der Ordner wird überwacht: kopierte Spiele werden nach dem Kopieren übernommen, gelöschte ausgetragen (Prefix bleibt). Die Startdatei wird erkannt (Deinstaller, Laufzeitpakete, Editoren, Server fallen heraus) und ist auf der Spielseite und in den Eigenschaften umstellbar. Cover und Beschreibung kommen aus dem GOG-Katalog oder von Steam, sonst steht der Titel auf der Karte. Start, Proton-Wahl, Startoptionen, GOverlay, Verknüpfungen und Spielzeit funktionieren wie bei GOG-Spielen (Spielzeit bleibt lokal). **Vorgaben je Bibliothek** (Einstellungen → Bibliotheken): Kompatibilitätstool, Startoptionen, Wayland-Treiber, DPI für neue Prefixe, Ordnerüberwachung – sie gelten nur, wenn das Spiel selbst nichts eingestellt hat (Spiel → Bibliothek → global). **Umbenennen/Ordner ändern** dort; **Löschen** mit Rückfrage – die Spiele verschwinden aus dem Client, Spielordner und Prefixe bleiben liegen. Die bis 0.1.4 fest eingebaute „External“-Bibliothek wird beim ersten Start zur ersten eigenen Bibliothek (gleiche IDs, Prefixe, Einstellungen und Spielzeiten; Sicherung der alten Dateien als `settings.json.vor-bibliotheken` und `installed.json.vor-bibliotheken`). |
| Spiele und Prefixe verwalten | **Global**: „+“ → Spiele-Ordner durchsuchen · Fehlende Prefixe für alle Spiele anlegen · Verwaiste Prefixe entfernen (Liste mit Auswahl). **Je Bibliothek**: Kopf der Bibliotheksansicht → „Spiel hinzufügen …“, „Ordner durchsuchen“ und „⋯“ (Fehlende Prefixe anlegen, Verwaiste Prefixe entfernen, Spiele-/Prefix-Ordner öffnen, Bibliothek bearbeiten, Bibliothek löschen) – dasselbe Menü per Rechtsklick auf den Eintrag in der Seitenleiste. **Einzeln**: „+“ → Spiel hinzufügen … (beliebiger Ordner, GOG oder extern, Name, Startdatei und Bibliothek wählbar) · Spielseite „⋯“ → Aus Bibliothek entfernen (wahlweise mit Ordner und/oder Prefix) · Prefix anlegen / neu anlegen / löschen (auch in den Eigenschaften). Von Hand entfernte Ordner nimmt die Suche nicht wieder auf; die Liste steht in den Einstellungen und lässt sich dort zurücknehmen. |
| Verknüpfungen | Pro installiertem Spiel Verknüpfung im **Startmenü** und/oder auf dem **Schreibtisch** (wie im Windows-Client): im Installations-Dialog wählbar, nachträglich über das „⋯“-Menü, die Eigenschaften oder `galaxy-linux shortcut`. Mit Spiel-Icon; der Start läuft über den Client (Spielzeit, Proton-Einstellungen). Beim Deinstallieren werden sie entfernt. |
| Installation | **Galaxy Content-System v2** – exakt der Weg, den Galaxy nutzt: Build-Manifest → Depots (Sprache, inkl. aller DLCs) → zlib-Chunks parallel, MD5-geprüft, **fortsetzbar**. Dazu Redistributables (DirectX, VC++, .NET …) wie bei Galaxy. Linux-native Spiele über den GOG-.sh-Installer. Spiele ohne Galaxy-Build über Offline-Installer + `innoextract`. |
| Update / Prüfen | Build-Vergleich, Update lädt nur geänderte Chunks, entfernt veraltete Dateien; „Dateien prüfen“ repariert beschädigte Chunks. |
| Starten wie Steam | **Pro Spiel wählbar:** jedes gefundene Proton (Steam-Bibliotheken, `compatibilitytools.d`, `/usr/share/steam/compatibilitytools.d` → **proton-cachyos**, Heroic), GE-Proton/UMU-Proton (Auto-Download über umu), System-Wine, Lutris-/Bottles-Runner. Startweg: **umu-launcher** (Standard, mit protonfixes und Steam Linux Runtime – nötig für `proton-cachyos-slr`), Proton direkt oder Wine. Ein Wine-Prefix pro Spiel. **Wayland-Treiber von Proton** (`PROTON_ENABLE_WAYLAND=1`) ist Vorgabe (KDE-Wayland); XWayland nur als Ausnahme pro Spiel unter Eigenschaften → Kompatibilität. |
| Startoptionen | **Steam-Syntax 1:1**: `mangohud %command%`, `ENABLE_VKBASALT=1 mangohud %command% -novid`, `gamescope -f -- %command%`, `PROTON_LOG=1 %command%`, nur `-windowed` … Globale + pro Spiel. Vorschau des resultierenden Befehls. |
| GOverlay | **Profile pro Spiel wie unter Steam**: galaxy-linux trägt seine Spiele-Ordner in GOverlays Ordnerliste (`~/.config/goverlay/nonsteam_folders.txt`) ein, damit die Spiele in GOverlay erscheinen (danach dort „Refresh game library“), und legt die Cover dafür ab. Ein Klick auf das Spiel in GOverlay legt sein Profil an; beim Start setzt galaxy-linux dessen `bgmod` vor den Befehl (MangoHud, vkBasalt, OptiScaler, Lossless … mit den Einstellungen des Spiels statt der globalen Konfiguration), dazu `gamemoderun` und `/WineDetectionEnabled:False`, wenn im Profil gewählt. Ohne eigenes Profil gilt auf Wunsch das globale GOverlay-Profil. Vor Prüfen, Aktualisieren, Verschieben und Deinstallieren nimmt `bgmod-uninstaller` die Änderungen im Spielordner zurück. Einstellungen → Kompatibilität → GOverlay; pro Spiel unter Eigenschaften → Kompatibilität. Schalter für MangoHud, vkBasalt, GameMode, Gamescope. „GOverlay öffnen“. |
| Eigenschaften-Dialog | wie Steam: Kompatibilität, Startoptionen, Umgebungsvariablen, Startdatei (playTask), Pre-/Post-Befehle, Redistributables, Winetricks, Prefix/Ordner/Protokoll öffnen, Favorit, verstecken. |
| Spielzeit | lokal gemessen, optional an GOG übertragen (erscheint im GOG-Profil wie bei Galaxy). |
| Erfolge | pro Spiel über die Gameplay-API (spiel-spezifisches Token aus dem Build-Manifest, wie Galaxy). |
| Galaxy-Dienste (comet) | Spiele mit GOG Galaxy-SDK schalten Erfolge frei und nutzen Statistiken und Bestenlisten im Spiel: wie bei Heroic legt galaxy-linux im Prefix den Platzhalterdienst `GalaxyCommunication.exe` an (`sc create`, Registry-Eintrag des Galaxy-Clients) und startet vor dem Spiel **comet** (Nachbau des Galaxy-Kommunikationsdienstes auf `127.0.0.1:9977`), nach dem Ende wird es beendet. Die Tokens stehen nie auf der Kommandozeile, sondern kurzzeitig in einer privaten Datei (0600). Läuft schon ein comet (z. B. von Heroic), wird er mitbenutzt. comet wird gesucht (eigener Pfad → PATH → Heroic-Paket → eigener Download), sonst beim ersten Bedarf einmalig aus dem offiziellen Release geladen (SHA-256-geprüft). Schalter: Einstellungen → Spielfunktionen, pro Spiel Eigenschaften → Spielfunktionen; Status, Herunterladen/Aktualisieren und eigener Pfad unter Einstellungen → Kompatibilität → Galaxy-Dienste. Protokoll: `…/logs/comet.log` (maskiert). |
| Einstellungen | **Allgemein**: Sprache der Oberfläche, Spiele- und Prefix-Ordner, weitere Installationsorte, ausgeschlossene Spielordner, bevorzugte Spielsprache, parallele Download-Verbindungen, Ordnerüberwachung, automatische Prefixe, Verknüpfungs-Vorgaben, Update-Suche beim Start, Spielzeit an GOG übertragen, versteckte Spiele anzeigen, Zugangsdaten im System-Schlüsselbund. **Bibliotheken**: eigene Bibliotheken anlegen, bearbeiten, löschen. **Kompatibilität**: Standard-Kompatibilitätstool, umu-launcher bevorzugen, Wayland-Treiber als Vorgabe, Redistributables, DPI für neue Prefixe (0 = aus der Desktop-Skalierung), globale Startoptionen, Übersicht der gefundenen Werkzeuge (umu, GOverlay, MangoHud, vkBasalt, GameMode, Gamescope). |
| Sprache | Oberfläche auf **Deutsch** und **Englisch**, folgt der Systemsprache; umstellbar in den Einstellungen (siehe [Sprache](#sprache)). |
| Protokoll | Ausführliches Log für die Fehlersuche, Zugangsdaten und Tokens maskiert (siehe [Protokollierung](#protokollierung-für-die-auswertung)). |
| CLI | alles auch im Terminal/Skript: `galaxy-linux install|launch|config|tools|…`. URI-Schema `galaxy-linux://launch/<id>`. |

**Noch nicht umgesetzt** (Galaxy-Funktionen, die ein eigenes Projekt wären): Freunde/Chat, Cloud-Saves,
Integration anderer Stores (Steam/Epic-Plugins), Galaxy-Overlay im Spiel, Auto-Updater des Clients.

---

## Installation auf CachyOS / Arch Linux

Aktuelle Version: **0.2.0** (Umbau in Arbeit) – Änderungen in [CHANGELOG.md](CHANGELOG.md).

```bash
git clone https://github.com/Cue-System90/gog_like_client_Arch_linux.git
cd gog_like_client_Arch_linux
./install.sh
```

`install.sh` prüft die Pakete (fehlende Pflichtpakete per pacman, empfohlene auf Nachfrage), baut das
pacman-Paket nach `../dist/` (neben dem geklonten Ordner), entfernt eine vorhandene Version restlos
(Einstellungen, Zugangsdaten und Spiele bleiben) und installiert per `sudo pacman -U`.
**Update:** `git pull && ./install.sh`.

Pflicht: `python python-requests python-gobject gtk4 libadwaita webkitgtk-6.0 unzip xdg-utils`
(`webkitgtk-6.0` = eingebetteter GOG-Store und GOG-Login direkt im Client; fehlt es, öffnen Store und Login im Browser).
Empfohlen: `umu-launcher proton-cachyos mangohud lib32-mangohud goverlay winetricks gamescope innoextract python-vdf`

```bash
./install.sh --purge   # wie oben, löscht vorher zusätzlich Einstellungen, Zugangsdaten und Logs (Spiele bleiben)
./install.sh --build   # Paket nur bauen (scripts/build-package.sh), Ablage in ../dist
./install.sh --user    # ohne Root nach ~/.local (Startskript ~/.local/bin/galaxy-linux + Menüeintrag)
./install.sh --deps    # nur fehlende Pakete prüfen/installieren
scripts/uninstall.sh [--purge] [--games]   # entfernen; --purge auch Einstellungen/Zugangsdaten/Logs, --games auch Spiele und Prefixe
python3 -m galaxy_linux                    # direkt aus dem Ordner starten, ohne Installation
```

`--build` legt neben dem Paket eigenständige Skripte für andere Rechner ab: `install.sh` (entfernt die alte
Version automatisch, prüft die Prüfsummen in `SHA256SUMS`, installiert per `pacman -U`) und `uninstall.sh`.

### Protokollierung (für die Auswertung)

Der Client protokolliert standardmäßig ausführlich nach
`~/.local/share/galaxy-linux/logs/galaxy-linux.log` (rotierend, 5 MB × 3): Systemumgebung und gefundene
Werkzeuge, jeden GOG-API-Aufruf mit Status und Dauer, Installationspläne (Build, Sprache, Depots, Größen),
Download-Zusammenfassungen, Startbefehle, Exit-Codes, Fehler mit Traceback sowie GTK-Warnungen.
Tokens, Login-Codes und signierte CDN-Parameter werden maskiert; die Datei kann so weitergegeben werden.

```bash
galaxy-linux log            # letzte 200 Zeilen
galaxy-linux log --path     # Pfad der Logdatei
galaxy-linux log --clear    # Protokoll leeren
GALAXY_LINUX_LOG=off galaxy-linux    # Datei-Logging aus
GALAXY_LINUX_LOG=debug galaxy-linux  # zusätzlich Debug auf der Konsole
```

Pro Spiel gibt es außerdem `~/.local/share/galaxy-linux/logs/<id>.log` mit der Ausgabe von Proton/Wine/umu.

## Sprache

Die Oberfläche gibt es auf Deutsch und Englisch. Ohne Einstellung folgt sie der Systemsprache (deutsch → Deutsch,
sonst Englisch); umstellen unter *Einstellungen → Allgemein → Oberfläche* (wirkt nach einem Neustart),
per `galaxy-linux config global ui_language en` oder für einen Start mit `GALAXY_LINUX_LANG=en galaxy-linux`.
Quelltexte sind Deutsch und mit `tr()` markiert; `scripts/i18n-update.py [sprache]` aktualisiert
`galaxy_linux/locale/<sprache>.po` (neue Texte mit leerem `msgstr`, bis dahin erscheinen sie Deutsch).

## Erste Schritte

1. **Anmelden**: „Bei GOG anmelden“. Ist `webkitgtk-6.0` installiert, erscheint die GOG-Login-Seite direkt im
   Client und der Login wird automatisch übernommen (wie im Windows-Client). Sonst öffnet sich der Browser;
   nach dem Login die Adresse der (leeren) Seite `https://embed.gog.com/on_login_success?origin=client&code=…`
   kopieren und einfügen – der Login startet, sobald der Code erkannt wird.
2. **Installieren**: Spiel wählen → *Installieren* → Plattform (Windows über Proton / Linux nativ),
   Sprache, Ordner, DLCs → Größe wird live aus dem Manifest ermittelt.
3. **Proton wählen**: auf der Spielseite im Feld *Proton / Wine* (oder *Eigenschaften → Kompatibilität*).
   Standard ist das System-Proton (`proton-cachyos`) über umu.
4. **GOverlay**: auf der Spielseite *GOverlay öffnen* → in GOverlay „Refresh game library“ → das Spiel anklicken
   und einstellen. galaxy-linux startet das Spiel dann mit diesem Profil (Anzeige im Block *Kompatibilität*).
5. **Spielen**.

Standard-Ordner: Spiele `~/Games/GOG/<Spiel>`, Prefixe `~/Games/GOG/prefixes/<ID>`, Konfiguration
`~/.config/galaxy-linux/`, Daten/Logs `~/.local/share/galaxy-linux/`, Cache `~/.cache/galaxy-linux/`.
Zugangsdaten liegen in `~/.config/galaxy-linux/credentials.json` (0600) oder optional im Schlüsselbund.

## Startoptionen – Syntax (wie Steam)

```
VAR=wert … wrapper … %command% … spielargumente
```

* `MANGOHUD=1 %command%` – Umgebungsvariable
* `mangohud %command%` – Wrapper (GOverlay-Standard)
* `ENABLE_VKBASALT=1 gamemoderun mangohud %command%` – mehrere kombiniert
* `gamescope -W 2560 -H 1440 -f -- %command%` – Gamescope
* `%command% -skipintro` – Argument ans Spiel; ohne `%command%` werden alle Token angehängt

Reihenfolge beim Start: `wrapper-aus-Startoptionen  [gamescope] [gamemoderun] [mangohud]  <umu-run|proton|wine> spiel.exe <args>`.

## Proton außerhalb von Steam – so wird gestartet

* **umu** (Standard): `WINEPREFIX=<prefix> PROTONPATH=<proton> GAMEID=umu-<id> STORE=gog PROTON_VERB=waitforexitandrun umu-run spiel.exe`
  umu bringt die Steam Linux Runtime mit (auch ohne installiertes Steam) und wendet protonfixes an.
  `proton-cachyos-slr` setzt den Container voraus und läuft daher über umu.
* **Proton direkt**: `STEAM_COMPAT_DATA_PATH=<prefix> STEAM_COMPAT_CLIENT_INSTALL_PATH=<steam> proton waitforexitandrun spiel.exe`
  (für klassische Protons aus der Steam-Bibliothek; SLR-Protons nur, wenn die Runtime in Steam installiert ist).
* **Wine**: `WINEPREFIX=<prefix> wine spiel.exe`

Der tatsächliche Befehl steht auf der Spielseite als Vorschau und im Protokoll `~/.local/share/galaxy-linux/logs/<id>.log`.

## Kommandozeile

```bash
galaxy-linux login                       # Login (URL/Code wird abgefragt)
galaxy-linux list [--installed] [--refresh] [--json]
galaxy-linux info "Witcher" --builds
galaxy-linux install 1207658924 [--platform windows|linux] [--lang de-DE] [--dir ~/Spiele] [--no-dlc]
galaxy-linux update <spiel>  |  galaxy-linux verify <spiel>  |  galaxy-linux updates --install
galaxy-linux scan [--no-prefix]                   # kopierte Spiele übernehmen, fehlende Prefixe anlegen
galaxy-linux library list [--json]               # eigene Bibliotheken mit Ordnern, Vorgaben und Spielezahl
galaxy-linux library add "Andere Stores" /mnt/spiele/andere [--prefix-dir /mnt/spiele/prefixe] \
             [--tool cachyos] [--options "gamemoderun %command%"] [--wayland on|off|default] [--dpi 216]
galaxy-linux library rename "Andere Stores" "Alte Spiele"
galaxy-linux library set "Alte Spiele" prefix_dir /mnt/spiele/prefixe   # auch games_dir, tool, options, wayland, dpi, watch
galaxy-linux library remove "Alte Spiele" [-y]   # Spiele austragen – Spielordner und Prefixe bleiben
galaxy-linux list --library "Alte Spiele"         # Spiele einer Bibliothek (--external: alle Bibliotheken)
galaxy-linux add <ordner> [--library NAME] [--title T] [--exe rel/pfad.exe] [--no-prefix]   # Spiel von Hand hinzufügen
galaxy-linux remove <spiel> [--delete-files] [--delete-prefix]           # aus der Bibliothek entfernen
galaxy-linux prefix create|reset|delete <spiel>   # Prefix eines Spiels anlegen, zurücksetzen, löschen
galaxy-linux prefixes --create-missing | --delete-orphans [--library NAME]   # Prefixe global oder je Bibliothek
galaxy-linux move <spiel> --dir /mnt/spiele       # Installation verschieben (Unterordner bleibt)
galaxy-linux shortcut <spiel> [--menu] [--desktop] [--remove]   # Verknüpfungen anlegen/entfernen
galaxy-linux config global install_dir ~/Spiele   # globaler Spiele-Ordner
galaxy-linux config locations --add /mnt/spiele   # weiterer Installationsort für den Dialog
galaxy-linux tools                       # gefundene Proton-/Wine-Versionen (IDs)
galaxy-linux config tool <spiel> cachyos # Kompatibilitätstool (ID oder Namensteil, "auto")
galaxy-linux config options <spiel> "ENABLE_VKBASALT=1 mangohud %command%"
galaxy-linux goverlay [<spiel>] [--sync] # GOverlay: eingetragene Ordner und Profil pro Spiel (--sync: eintragen)
galaxy-linux goverlay-import <spiel>     # Startoptionen aus GOverlay ableiten (mit Profil: ohne mangohud-Zeile)
galaxy-linux comet [--download] [--json]   # Galaxy-Dienste: comet-Status, herunterladen/aktualisieren
galaxy-linux config env <spiel> DXVK_ASYNC=1
galaxy-linux launch <spiel> --dry-run    # Befehl nur anzeigen
galaxy-linux launch <spiel> -- -windowed # starten, Argumente ans Spiel
galaxy-linux prefix winetricks <spiel> vcrun2019
galaxy-linux uninstall <spiel> [--prefix]
```

`<spiel>` ist die GOG-ID oder ein eindeutiger Titelteil; Spiele eigener Bibliotheken haben IDs `ext-<ordnername>`.
Bibliotheken werden über Namen, eindeutigen Namensteil oder ID angesprochen.

## Tests

```bash
python3 -m unittest discover -s tests -t .                       # Offline-Tests (Fake-CDN, Parser, Pläne)
GALAXY_LINUX_NET_TESTS=1 python3 -m unittest tests.test_net -v   # echte GOG-Endpunkte ohne Konto
```

Der Netz-Test lädt ein kleines Redistributable komplett über das Chunk-System und prüft MD5s – damit ist
der Download-Pfad gegen GOGs CDN verifiziert. Installationen echter Spiele brauchen ein GOG-Konto.

## Problemlösung

* **„benötigt die Steam Linux Runtime … bitte umu-launcher installieren“** – `sudo pacman -S umu-launcher`
  oder in den Einstellungen ein anderes Proton wählen.
* **Erster Start dauert lange** – umu lädt einmalig die Steam Linux Runtime (~500 MB) nach
  `~/.local/share/umu`; Proton legt das Prefix an; Redistributables werden installiert.
* **MangoHud erscheint nicht** – `lib32-mangohud` für 32-Bit-Spiele; bei umu reicht `MANGOHUD=1 %command%`.
* **Spiel ohne Galaxy-Build** – wird automatisch über den Offline-Installer installiert (`innoextract` nötig).
* **Logs** – `~/.local/share/galaxy-linux/logs/galaxy-linux.log` (Client) und `…/logs/<id>.log` (Spiel/Proton), `…/logs/comet.log` (Galaxy-Dienste); `galaxy-linux log`.

## Projektstruktur

```
galaxy_linux/
  api/        auth.py (OAuth), client.py (GOG-Dienste), models.py
  content/    manifest.py (v2-Manifeste), downloader.py (Chunks), installer.py (Install/Update/Verify/Linux/Offline),
              scanner.py (kopierte GOG-Spiele), external.py (Startdatei-Erkennung für Spiele eigener Bibliotheken)
  launch/     compat.py (Proton/Wine-Erkennung), launch_options.py (Steam-Syntax), goverlay.py, launcher.py
  ui/         app.py, window.py, game_page.py, dialogs.py, style.css
  locale/     en.po (englischer Katalog)              i18n.py  tr(), Spracherkennung, .po lesen/schreiben
  backend.py  Fassade + Job-Warteschlange, eigene Bibliotheken     cli.py  Kommandozeile
  settings.py Einstellungen, Bibliotheken (CustomLibrary), Registry
tests/        Unit-Tests (Fake-CDN) + Netz-Tests
data/         .desktop, Icon        PKGBUILD, install.sh
scripts/      build-package.sh, uninstall.sh, i18n-update.py (Übersetzungen pflegen), install-official-galaxy-wine.sh
```

## Drittanbieter-Werkzeuge

* **comet** – https://github.com/imLinguin/comet, Copyright imLinguin und Mitwirkende, Lizenz **Apache-2.0**
  (https://www.apache.org/licenses/LICENSE-2.0). galaxy-linux liefert comet nicht mit: Auf Wunsch oder beim ersten
  Bedarf lädt es die unveränderten Dateien `comet-x86_64-unknown-linux-gnu` und `GalaxyCommunication-dummy.exe`
  aus dem offiziellen Release v0.3.2, prüft ihre SHA-256-Summen und legt sie mit einer NOTICE-Datei unter
  `~/.local/share/galaxy-linux/tools/comet/<version>/` ab.

Lizenz: MIT.
