# Änderungsprotokoll

Neueste Version zuerst. „Neu bei uns“ im Zahnrad-Menü zeigt den Abschnitt der installierten Version.

## 0.2.0 – Umbau in Arbeit (22.10.2026)

Ziel des Umbaus: alle Funktionen von GOG Galaxy 2.1, dazu die Linux-Besonderheiten. Diese Version legt das
Fundament; weitere Bausteine folgen.

- **Zahnrad-Menü wie in Galaxy 2.1**: Spiele & Freunde hinzufügen (Spiel manuell hinzufügen, GOG-Code einlösen,
  Nach GOG-Spielen durchsuchen, Bibliothek anlegen), **Bibliothek aktualisieren** (F5 / Strg+R), Einstellungen,
  Hilfe erhalten, Neu bei uns, Nach Updates suchen, Rechtliches und Datenschutz, Beenden.
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
  Eigenschaften → Spielfunktionen. (Die Schalter wirken, sobald die zugehörigen Bausteine fertig sind.)
- Unter der Haube: Erweiterungspunkte für den Spielstart (Launch-Hooks), für Einstellungsdialoge, Spielansicht und
  Auftragsarten; die Redistributables beim ersten Start laufen jetzt als Hook.
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
