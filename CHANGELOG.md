# Änderungsprotokoll

Neueste Version zuerst. „Neu bei uns“ im Zahnrad-Menü zeigt den Abschnitt der installierten Version.

## 0.4.0 (23.10.2026)

Dritter Schritt Richtung GOG Galaxy 2.1: die Gruppe GOG GALAXY mit Entdecken und Aktueller Übersicht, das
Benachrichtigungszentrum, Freunde mit Chat und Aktivität und das Selbst-Update wie in Galaxy. Dazu passt der Client
jetzt auf kleine und stark skalierte Bildschirme – bis 1280 × 800, auch auf einen 4K-Fernseher mit 225 %.

- **Seitenleiste wie Galaxy 2.1**: ganz oben die Gruppe **GOG GALAXY** mit „Entdecken“, „Suche“ (öffnet die Power
  Search) und „Aktuelle Übersicht“ (mit „Neu“, wenn „Was als Nächstes spielen“ einen neuen Vorschlag hat). Reihenfolge
  der Gruppen jetzt GOG GALAXY, GOG STORE, SPIELE, BIBLIOTHEKEN, LESEZEICHEN, FREUNDE. Beide Ansichten lassen sich unter
  Einstellungen → Allgemein → „Startseite“ wählen.
- **Entdecken**: native Seite mit waagerechten Regalen aus dem GOG-Katalog – „Spiele für dich“, „Neuerscheinungen“,
  „Angebote“, „Aus deiner Wunschliste“ (mit aktuellen Preisen), „Zusätzliche Inhalte für deine Spiele“ (nur wenn der
  Katalog den DLC-Filter mit der Anmeldung wirklich anwendet) und „Good Old Games“. Preise in Land und Währung des
  Kontos, Rabatte als Pille, angemeldet ohne Spiele im Besitz; ein Spiel steht nur in einem Regal. Ein Klick öffnet die
  Produktseite im eingebetteten Store, Spiele im Besitz ihre Spielansicht; „Alle anzeigen“ führt zur passenden
  Store-Seite. Alles lädt im Hintergrund, Antworten bleiben 10 Minuten zwischengespeichert, ohne Verbindung erscheint
  ein ruhiger Hinweis mit „Erneut versuchen“.
- **Aktuelle Übersicht** mit den Widgets „Zuletzt gespielt“, „Meine Übersicht“ (Spielzeit heute, gestern, diese Woche,
  dieser Monat und ein kleines Säulendiagramm der letzten 14 Tage), „Neu in der Bibliothek“ und „Was als Nächstes
  spielen“ (ein Vorschlag aus installierten, nie oder selten gespielten Spielen, mit „Überrasch mich“ und „Neu
  mischen“). „Ansicht anpassen“ schaltet Widgets an und ab und ordnet sie, die Wahl wird gespeichert. Dafür merkt sich
  galaxy-linux jetzt jede Spielsitzung (Beginn und Ende). Freunde-Widgets können sich später über
  `ui_registry.register_recent_widget` einhängen.
- **Benachrichtigungszentrum**: Glocke oben links neben der Suche mit der Zahl ungelesener Meldungen; die Liste zeigt
  die neuesten zuerst (Symbol, Text, Zeit), ein Klick führt die Aktion aus (Spielansicht, Downloads, Store-Seite …);
  „Als gelesen markieren“ und „Alle löschen“. Meldungen kommen von fertigen und fehlgeschlagenen Downloads, verfügbaren
  Updates, neuen Spielen in der Bibliothek, Konflikten und überschrittenem Kontingent der Cloud-Spielstände und
  Problemen mit Galaxy-Redistributables. Gespeichert werden höchstens 200 Meldungen der letzten 30 Tage.
- **Wunschlisten-Rabatte**: kurz nach dem Start und danach alle 6 Stunden prüft galaxy-linux angemeldet die
  Wunschliste. Beginnt oder wächst ein Rabatt, meldet es „„Titel“ ist jetzt um 40 % reduziert – 17,99 €“; ein Spiel,
  das vorher keinen Preis hatte, erscheint als „Neu erhältlich“. Ein Klick öffnet die Produktseite.
- **Einstellungen → Benachrichtigungen** wie Galaxy je Art (Installation/Update, Wunschliste, Neue Spiele in der
  Bibliothek, Cloud-Spielstände): „Desktop und Benachrichtigungszentrum“, „Nur Benachrichtigungszentrum“ oder „—“. Das
  Galaxy-Overlay gibt es unter Linux nicht, seine Rolle übernimmt das Benachrichtigungszentrum. Desktop-Meldungen
  erscheinen weiter nur, solange das Fenster nicht im Vordergrund ist; ein Klick darauf öffnet das Fenster und führt
  die Aktion aus.
- Downloads: die leere Warteschlange zeigt ihren Hinweis wieder (das „&“ im Text brach die Anzeige).
- **Seitenleiste FREUNDE wie Galaxy**: „Einladungen“ (nur mit offenen Freundschaftsanfragen, mit Zahl), „Freunde
  online“ (Zahl, öffnet die Freundesliste) und „Nachrichten“ (Chat) – nicht mehr ausgegraut.
- **Freundesliste**: oben Suche, „Ansicht“ (Erweiterte/Kompakte Liste, Nach Aktivität/Namen sortieren, Nicht gruppieren/
  Nach gespieltem Spiel gruppieren – je ein Klick, gespeichert) und „Freunde hinzufügen“. Erst „Erhaltene Einladungen
  und Empfehlungen“ mit „Annehmen“/„Ablehnen“, dann ONLINE (im Spiel, online, abwesend; Profilbild mit Präsenz-Punkt,
  „Spielt – Titel“ samt Status im Spiel) und OFFLINE („Zuletzt online …“, soweit galaxy-linux es beobachtet hat). Jede
  Zeile hat „Nachricht senden“, „Profil ansehen“ (gog.com/u/<Name> im eingebetteten Store) und „Aus Freundesliste
  entfernen“ (mit Rückfrage) als Knöpfe und im Kontextmenü; ein Klick auf die Zeile öffnet den Chat. „Freunde
  hinzufügen“ (auch im Zahnrad unter „Spiele & Freunde hinzufügen“) öffnet die Freundesseite von gog.com im Store –
  Galaxys eigene Suche nutzt eine Schnittstelle, auf die sich galaxy-linux nicht verlassen kann; die Liste sagt das.
- **Abgleich**: Freundesliste (chat.gog.com, höchstens alle 10 Minuten neu), Einladungen und Präsenz der Freunde
  (presence.gog.com, in Paketen zu 100) alle 2 Minuten bei sichtbarem Fenster, alle 10 Minuten verborgen oder im
  System-Tray, nie abgemeldet; nach Fehlern doppelt so lange (höchstens 30 Minuten). Fehlende Namen und Bilder kommen
  aus dem öffentlichen Profil (users.gog.com, 7 Tage zwischengespeichert), Titel laufender Spiele aus der Bibliothek,
  GamesDB oder api.gog.com. Endpunkte und Felder wie in der Weboberfläche von Galaxy 2.1.10.56.
- **Freunde-Seitenleiste rechts** bei Fenstern ab 1501 px: kompakte Liste der Freunde, die online sind. Einstellungen →
  Oberfläche → „Zeige eine Seitenleiste für Freunde, die online sind“. Sie erzwingt nie ein breiteres Fenster.
- **Nachrichten**: die Chat-Web-App von GOG (chat.gog.com, wie Galaxy mit `isEmbedFrom=galaxy`) in einer WebKit-Ansicht
  der gemeinsamen Sitzung; die erste Anfrage trägt das Zugriffstoken. Nimmt die App die Anmeldung nicht an (401/403),
  bleibt sie leer oder fehlt die Verbindung, erscheint ein Hinweis mit „GOG.COM ÖFFNEN“ (Store, dort per Cookies
  angemeldet) und „Erneut versuchen“. „Nachricht senden“ öffnet die Unterhaltung mit dem Freund.
- **Aktuelle Übersicht**: neue Widgets „Freunde spielen gerade“ (Cover des Spiels, Freund und Titel; ein Klick öffnet
  das Spiel bzw. seine Store-Seite) und „Aktivität“ (die letzten 50 beobachteten Ereignisse: online gekommen, spielt
  jetzt …, gespeichert in `friends_activity.json`, beim Abmelden gelöscht).
- **Benachrichtigungen für Freunde** wie Galaxy, je Art in Einstellungen → Benachrichtigungen: „Freundschaftsanfrage“
  (Vorgabe: Desktop und Benachrichtigungszentrum), „Freund geht online“ und „Freund startet ein Spiel“ (Vorgabe: nur
  Benachrichtigungszentrum). Jede Anfrage erscheint nur einmal, der erste Abgleich nach dem Start meldet nichts.
- Präsenz „Unsichtbar“ im Avatar-Menü meldet weiterhin nichts an GOG – weder „online“ noch das laufende Spiel –, damit
  Freunde keine Meldungen über dich bekommen.
- **Schmale Fenster und skalierte Bildschirme**: das Fenster ließ sich nicht schmaler als 1899 px machen und passte
  damit z. B. nicht auf einen 55"-4K-Fernseher mit 225 % (rund 1706 × 960 Punkte). Jetzt reichen 1066 px (Englisch
  960 px), jede Ansicht ist bis 1280 × 800 vollständig. In der Spielansicht brechen die Kennzahlen der Play-Leiste
  (Spielzeit, Zuletzt gespielt, Erfolge, Kompatibilität, Cloud-Spielstände), die Tags und die Knöpfe des
  Kompatibilitäts-Panels bei Bedarf in eine zweite Zeile um; lange Proton-Namen und Startdateien enden mit „…“ (ganz in
  der Liste und im Tooltip). Die Freunde-Seitenleiste erscheint weiter erst ab 1501 px und nur, wenn daneben Platz ist.
- **Selbst-Update wie Galaxy**: Zahnrad → „Nach Updates suchen“ und – mit „Beim Start nach Client-Updates suchen“
  (Einstellungen → Allgemein, Vorgabe an) höchstens einmal am Tag – fragen GitHub nach der neuesten Veröffentlichung.
  Ist sie neuer, zeigt „Neues Update verfügbar“ die Versionshinweise mit „Jetzt aktualisieren“ und „Später“, das
  Zahnrad bekommt einen blauen Punkt und sein Eintrag heißt „Jetzt aktualisieren“. Beim pacman-Paket lädt
  galaxy-linux das Paket und `SHA256SUMS` der Veröffentlichung, prüft die Prüfsumme und installiert mit
  `pkexec pacman -U` – das Passwort fragt nur der Dialog des Systems ab –, danach „Neu starten“. Läuft galaxy-linux aus
  dem Quellordner oder als Benutzerinstallation, öffnet „Jetzt aktualisieren“ die Veröffentlichung im Browser.
- **BEENDEN während der Prefix-Einrichtung** bricht jetzt auch einen laufenden Installationsschritt ab (ISI,
  Redistributables): seine Prozessgruppe wird beendet (nach 5 Sekunden hart), danach der wineserver des Prefix. Der
  Schritt zählt nicht als Fehlversuch und läuft beim nächsten Start erneut („Abgebrochen – läuft beim nächsten
  Start“ im Prefix-Status).
- **Ruhigeres Protokoll**: GTK-/GLib-Meldungen landen erst ab Warnung in `galaxy-linux.log` – die Dutzenden
  „Gdk-INFO: Vulkan: Loader Message“-Zeilen fallen weg; mit `GALAXY_LINUX_LOG=debug` kommt wieder alles hinein.
- Für den Maintainer: `scripts/release.sh` baut das Paket und legt das GitHub-Release mit Paket und `SHA256SUMS` an
  (Versionshinweise aus diesem Änderungsprotokoll; `--dry-run` veröffentlicht nichts).

## 0.3.1 (23.10.2026)

Rückmeldungen aus dem ersten echten Test von 0.3.0.

- **GOverlay ohne globale Konfiguration**: jedes installierte Spiel (GOG und extern, auch Linux-Spiele) erscheint in
  GOverlay **genau einmal, mit seinem Titel und Cover** – wie ein Steam-Spiel. galaxy-linux trägt dafür statt der
  Bibliotheks-Wurzeln einen eigenen Ordner mit einer Verknüpfung pro Spiel ein
  (`~/.local/share/galaxy-linux/goverlay-library/`); Sicherungs- und Hilfsordner („… - Kopie“, „saved_games“) tauchen
  nicht mehr als Spiele auf. Die früher eingetragenen Wurzeln werden entfernt, eigene Zeilen bleiben. Cover kommen aus
  galaxy-linux (ohne Cover eine Titelkarte), GOverlay sucht nicht mehr bei Steam nach dem Ordnernamen („Freelancer
  Simulator“); ein in GOverlay selbst gesetztes Cover bleibt. Das Profil heißt wie die Karte, Profile aus 0.3.0 (nach
  dem Ordnernamen) gelten weiter. Das globale GOverlay-Profil gilt nur, wenn man es ausdrücklich einschaltet.
- **Externe Spiele mit richtigem Namen**: offizieller Titel aus dem GOG-Spiel desselben Namens, dem GOG-Katalog bzw.
  Steam (streng geprüft), der Versionsinfo der .exe oder Wikidata; Cover notfalls von Wikipedia. Beispiele:
  „EmperorDAdRdM 1360 x 768“ → „Emperor: Rise of the Middle Kingdom (1360 x 768)“, „Herrscher des Olymp - Zeus
  1360 x 768“ → „Zeus and Poseidon (1360 x 768)“ mit dem Cover des GOG-Spiels. Ordner werden nie umbenannt.
- **Kopien und Varianten unterscheiden**: Kopien erkennt galaxy-linux am Inhalt (gleiche Dateien und Größen, ohne
  Spielstände und Einstellungen), nicht am Namen. Eine Kopie erscheint wie Galaxys gestapelte Duplikate nur einmal
  (Filter „Kopien anzeigen“, in den Details „Kopie von …“); eine Variante mit eigenem Inhalt („Freelancer HD Edition“,
  Breitbild-Fassungen) ist ein eigenes Spiel mit eigenem Namen, z. B. „Freelancer (HD Edition)“.
- **Bearbeiten …** wie in Galaxy (Anpassen-Menü und Rechtsklick): eigener Titel und eigenes Cover für jedes Spiel,
  je mit „Auf Standard zurücksetzen“ – gilt in Bibliothek, Spielansicht, Suche und GOverlay.
- **„N Update(s) verfügbar“** unten in der Seitenleiste ist jetzt ein Knopf: ein Klick öffnet die Downloads bei
  „Erhältliche Updates“.
- **Protokoll** (Zahnrad → Protokoll): galaxy-linux.log, comet.log und die Spielprotokolle mit Stufenfilter (Alle,
  Warnungen, Fehler), Suche, „Ordner öffnen“, „Kopieren“ und „Leeren“; neue Einträge erscheinen sofort. Tokens bleiben
  maskiert.

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
