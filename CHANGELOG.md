# Änderungsprotokoll

Neueste Version zuerst. „Neu bei uns“ im Zahnrad-Menü zeigt den Abschnitt der installierten Version.

## 0.3.0 (23.10.2026)

Zweiter Schritt Richtung GOG Galaxy 2.1: Bibliothek, Spielansicht, Downloads und Einstellungen wie im Original.

- **Bibliothek wie Galaxy**: eigene **Tags** (Rechtsklick → Tags verwalten …, „Neu erstellen …“, „Alle Tags
  verwalten“ zum Umbenennen und Löschen) und **Bewertung** mit 0–5 Sternen; **Sortieren nach** Titel, Bewertung,
  Spielzeit, Presse-Wertung, Veröffentlichungsdatum, Zuletzt gespielt, Tags, Plattform, Genres, Größe, Datum hinzugefügt
  (auf- oder absteigend); **Gruppieren nach** Tags, Plattform, Genre, Bewertung, Veröffentlichungsdatum, Status,
  Presse-Wertung, Datum hinzugefügt; **Filter** nach Status, Betriebssystem, Plattform, Genre, Bewertung, Tags,
  Favoriten und Ausgeblendet. Die Suche findet auch Tags, Genres und Entwickler; Liste mit Spalte Bewertung.
- **Lesezeichen**: der Lesezeichen-Knopf legt die aktuelle Ansicht (mit Filter, Suche, Sortierung, Gruppierung) oder
  ein Spiel in die Seitenleiste unter LESEZEICHEN; umbenennen, verschieben, löschen per Rechtsklick; Import/Export unter
  Einstellungen → Oberfläche.
- **Kontextmenü** (Rechtsklick in Raster und Liste) und **Anpassen-Menü** der Spielansicht in Galaxys Reihenfolge:
  Spielen/Installieren, Zusätzliche Dateiverknüpfungen ▸, Patch-Notizen, Installation verwalten ▸ (Konfigurieren …,
  Ordner importieren, Ordner anzeigen, Überprüfen / Reparieren, Deinstallieren), Nach Updates suchen, Spielforum,
  Produktseite, Tags, Bewerten ▸, Spiel ausblenden/wieder einblenden (mit „Rückgängig“); dazu Wine-Prefix ▸ und
  Verknüpfungen ▸.
- **Spielansicht mit Tabs** Übersicht / Mein Fortschritt / Extras: Medien und Presse-Wertung in der Übersicht; Erfolge
  mit Seltenheit, geheimen Erfolgen und Freischaltdatum; **Extras** mit Offline-Installern (Filter nach
  Betriebssystem und Sprache), Patches, Sprachpaketen, Goodies, „Alle herunterladen“, „Download-Ordner öffnen“ und
  der Sicherungskopie der Cloud-Speicherstände.
- **Konfigurieren …** (Installation verwalten): Spielsprache wechseln, „Automatisch auf die neuste Version
  aktualisieren“ oder eine ältere Version wählen (Rollback), Beta-Kanäle und private Kanäle mit Passwort, Cloud-Speicher
  und Galaxy-Funktionen pro Spiel, DLCs (de)installieren mit „Alle auswählen“, Standard-Dateiverknüpfung und eigene
  Dateiverknüpfungen mit Argumenten (Kopieren, Entfernen). Abgewählte DLCs bleiben auch bei Updates weg.
- **Downloads wie Galaxy**: Warteschlange, Erhältliche Updates, Abgeschlossen, Fehlgeschlagen; Pausieren und Fortsetzen
  einzeln oder alle, Alles abbrechen (mit Rückfrage), Leeren, Erneut versuchen, „Nach oben verschieben“ und Umsortieren
  per Ziehen und Ablegen; Phasen wie im Original, Geschwindigkeitsgraph (Aktuell, Durchschnitt, Spitze …). Offene
  Downloads überstehen einen Neustart, vor dem Download wird der freie Platz geprüft.
- **Bandbreite**: Begrenzung mit Zeitplan (auch über Mitternacht), „Downloads während des Spiels pausieren“ (geht
  danach von selbst weiter), Anzeige in Bit pro Sekunde.
- **Installationsdialog**: „Eigene DLCs installieren“ mit Auswahl, „Spiel automatisch aktualisieren“, „Alle
  enthaltenen Extras herunterladen“, Desktopverknüpfung, Hinweis bei zu wenig Speicherplatz.
- **Einstellungen in Galaxys Aufbau**: Allgemein (Sprache, galaxy-linux beim Systemstart öffnen, Eingeloggt bleiben,
  Startseite inkl. „Vorherige Sitzung fortsetzen“), Oberfläche (Bevorzugter Store, Lesezeichen, Verhalten des
  Hauptfensters: Minimieren, Schließen und Spielstart – auch in den **System-Tray**), Benachrichtigungen
  (Desktop-Meldung zu Installation/Update), Installieren/aktualisieren (Sprache, Pfade, Ordner für andere Downloads,
  Bandbreite), Spielfunktionen; dazu die Linux-Seiten Bibliotheken und Kompatibilität.
- **Spielzeit von GOG**: die Spielzeit aller Geräte wird übernommen (beim Öffnen eines Spiels und halbstündlich).
- **Online-Status** bei GOG wie Galaxy (Avatar-Menü: Online / Unsichtbar), mit dem laufenden Spiel.
- **Power Search** (Strg+F, Lupe über der Seitenleiste): eigene Spiele, Tags, Genres, Entwickler, Lesezeichen und der
  GOG-Store mit Preisen – Eingabe startet, installiert, zeigt oder kauft.
- Behoben: DLC-Anzeige nutzte GamesDB-Kennungen statt GOG-Produkt-IDs; Abbrechen eines Offline-Installer-Downloads
  galt als Fehler.

## 0.2.0 (23.10.2026)

Erster großer Umbau Richtung GOG Galaxy 2.1 mit allen Linux-Besonderheiten.

- **Zahnrad-Menü wie in Galaxy 2.1**: Spiele & Freunde hinzufügen (Spiel manuell hinzufügen, GOG-Code einlösen,
  Nach GOG-Spielen durchsuchen, Bibliothek anlegen), **Bibliothek aktualisieren** (F5 / Strg+R), Einstellungen,
  Hilfe erhalten, Neu bei uns, Nach Updates suchen, Rechtliches und Datenschutz, Beenden.
- **Bibliotheks-Abgleich**: neue Käufe erscheinen ohne Neustart – kurz nach dem Start, alle 10 Minuten (einstellbar),
  beim Wechsel ins Fenster, nach dem Login und nach einem Kauf. GOGs Prüfsumme spart den Vollabruf, solange sich
  nichts geändert hat. Neue Spiele tragen 14 Tage lang „NEU“ (bis zum ersten Öffnen) und stehen oben unter
  „Neu in der Bibliothek“; versteckte Spiele werden mitgeladen. `galaxy-linux list --refresh` nennt neue Spiele.
- **Avatar-Menü wie in Galaxy**: Kontoverwaltung, Bisherige Bestellungen, Geldbeutel, Ausloggen (mit Rückfrage,
  laufende Downloads werden abgebrochen); abgemeldet nur „Bei GOG anmelden“.
- Das „+“ in der Titelleiste enthält nur noch Hinzufügen und Prefix-Pflege.
- **Eingebetteter GOG-Store wie im Original**: Seitenleiste GOG STORE (Startseite, Alle Spiele, Einkaufen,
  Wunschliste mit Zähler, Warenkorb, Bisherige Bestellungen) zeigt die gog.com-Seiten im Client, in der Sprache der
  Oberfläche; Login und Store teilen sich die Sitzung. Nach einem Kauf wird die Bibliothek sofort abgeglichen,
  Zahlungs-Fenster (PayPal) öffnen im Client, Downloads landen im Download-Ordner. Ausblendbar unter Einstellungen →
  Oberfläche → Bevorzugter Store. Spielansicht „⋯“ → Im Store anzeigen. `webkitgtk-6.0` ist jetzt Pflichtpaket.
- Neue Einstellungsseite **Spielfunktionen**: Spiele automatisch aktualisieren, Cloud-Spielstände, Erfolge,
  Galaxy-Dienste im Spiel (comet), Erfassung der Spielzeit, GOverlay-Profile pro Spiel; pro Spiel abweichend unter
  Eigenschaften → Spielfunktionen.
- **GOverlay pro Spiel**: Die Spiele-Ordner erscheinen in GOverlay (Eintrag in `nonsteam_folders.txt`, Cover werden
  mitgeliefert); das dort angelegte Profil eines Spiels wird beim Start über `bgmod` angewendet statt der globalen
  MangoHud-Konfiguration. Die Spielseite zeigt das Profil, „GOverlay öffnen“ ersetzt „Aus GOverlay“.
- **Galaxy-Dienste im Spiel**: Spiele mit GOG Galaxy-SDK schalten Erfolge frei und nutzen Statistiken und
  Bestenlisten – über **comet** (Apache-2.0) und den GalaxyCommunication-Dienst im Prefix, wie bei Heroic. comet wird
  gesucht (eigener Pfad, PATH, Heroic) oder einmalig aus dem offiziellen Release geladen (SHA-256-geprüft); Status,
  Herunterladen und eigener Pfad unter Einstellungen → Kompatibilität → Galaxy-Dienste, im Terminal `galaxy-linux comet`.
- **GOG-Abhängigkeiten wie Galaxy und Heroic**: Redistributables (VC++, DirectX, .NET, PhysX, XNA …) liegen einmal in
  einem gemeinsamen Ordner statt in jedem Spiel; beim ersten Start richtet galaxy-linux den Prefix ein wie der
  Original-Installer (ISI-Installationsskripte bzw. Support-Programme, danach die Redistributables) und merkt sich pro
  Prefix, was schon da ist. DOSBox, ScummVM, nGlide und Sprach-Setups kommen in den Spielordner. Eigenschaften →
  Kompatibilität → Einrichtung im Prefix, Einstellungen → Kompatibilität → Galaxy-Redistributables, `galaxy-linux redist`.
- Durchgesehen und abgesichert: Nachbereitung nach dem Spiel geht auch beim Schließen des Fensters nicht verloren,
  Vorbereitung abbrechbar, Cloud-Löschungen nur mit Sicherung, GOverlay-`bgmod` nur einmal und das globale Profil nur
  auf Wunsch, keine Token-Reste von comet, Seitenleiste „Spiele im Besitz“ wie im Original.
- Unter der Haube: Erweiterungspunkte für den Spielstart (Launch-Hooks), für Einstellungsdialoge, Spielansicht und
  Auftragsarten; die Redistributables beim ersten Start laufen jetzt als Hook.
- **Cloud-Spielstände wie in Galaxy**: vor jedem Start herunterladen, nach dem Beenden hochladen; Konfliktdialog
  (GOG Galaxy-Cloud / Dieser Computer / Ignorieren) und Kontingent-Hinweis wie im Original; Sicherung der lokalen
  Stände, bevor sie ersetzt werden. Zustand als Kennzahl in der Spielansicht; „Jetzt hochladen“, „Jetzt
  herunterladen“, Sicherungen und Speicherorte unter Eigenschaften → Spielfunktionen; `galaxy-linux cloud <spiel>`.
- Installations-Datensätze (Build, Abhängigkeiten, Client-ID) lassen sich für übernommene Spiele nachträglich
  anlegen: `galaxy-linux records --create`. Prefix-Einrichtung erneut ausführen: `galaxy-linux prefix setup <spiel>`.

## 0.1.6 (19.10.2026)

- Oberfläche auf Deutsch und Englisch; folgt der Systemsprache, umstellbar in den Einstellungen.
- Erste öffentliche Version auf GitHub.

## 0.1.5 (19.10.2026)

- Beliebig viele eigene Bibliotheken für Spiele, die nicht von GOG stammen, mit eigenen Ordnern und Vorgaben.
- Die External-Bibliothek aus 0.1.3/0.1.4 wird automatisch zur ersten eigenen Bibliothek.

## 0.1.4 (19.10.2026)

- Bibliotheksansicht: Raster oder Liste, Covergröße, Abstand, Titel, Abschnitte und Sortierung, gespeichert.
- Echte Hochkant-Cover aus GOGs Spieledatenbank.

## 0.1.3 (19.10.2026)

- External-Spiele: Spiele, die nicht von GOG stammen, mit eigenem Prefix, erkannter Startdatei und Cover.

## 0.1.2 (19.10.2026)

- Kopierte Spiele: von Hand in die Spiele-Ordner kopierte GOG-Spiele werden erkannt und übernommen; fehlende
  Prefixe werden angelegt.

## 0.1.0 – 0.1.1 (ab 15.10.2026)

- Erste Fassungen: Login bei GOG, Bibliothek, Installation über das GOG-Content-System, Start über umu-launcher,
  Proton oder Wine mit Proton-Auswahl pro Spiel, Startoptionen in Steam-Syntax mit GOverlay-Übernahme.
