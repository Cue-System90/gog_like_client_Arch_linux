"""Einstellungen → Kompatibilität → „Galaxy-Dienste“: comet-Status, Herunterladen/Aktualisieren, eigener Pfad.

Ein- und ausgeschaltet werden die Dienste unter „Spielfunktionen“ (global) bzw. „Eigenschaften → Spielfunktionen“
(pro Spiel) – diese Schalter liefert das Fundament. Die Suche nach comet (``comet --version``) und der Download
laufen im Hintergrund; die Zeilen werden über ``run_async`` in der Hauptschleife aktualisiert.
"""
from __future__ import annotations

from ..i18n import tr

import os
from pathlib import Path

from ..launch import galaxy_services as gsvc
from . import prefs_registry
from .util import Adw, Gio, GLib, Gtk, icon_button, idle, open_uri, run_async


class GalaxyServicesGroup:
    def __init__(self, dlg) -> None:
        self.dlg = dlg
        self.busy = False
        self.group = Adw.PreferencesGroup(
            title=tr('Galaxy-Dienste'),
            description=tr('comet ersetzt den Kommunikationsdienst von GOG Galaxy: Spiele mit Galaxy-SDK schalten Erfolge frei '
                           'und nutzen Statistiken und Bestenlisten. Ein- und ausschalten unter „Spielfunktionen“.'))
        self.comet_row = Adw.ActionRow(title="comet", subtitle=tr('Wird gesucht …'))
        self.comet_row.set_subtitle_selectable(True)
        self.group.add(self.comet_row)
        self.dummy_row = Adw.ActionRow(title=tr('GalaxyCommunication-Dienst'), subtitle="–")
        self.group.add(self.dummy_row)
        self.dl_row = Adw.ButtonRow(title=tr('comet herunterladen'))
        self.dl_row.set_start_icon_name("folder-download-symbolic")
        self.dl_row.connect("activated", lambda *_: self.download())
        self.dl_row.set_visible(False)
        self.group.add(self.dl_row)
        self.path_row = Adw.EntryRow(title=tr('Eigener Pfad zu comet (leer = automatisch suchen)'),
                                     text=dlg.s.comet_path, show_apply_button=True)
        self.path_row.connect("apply", lambda r: self.apply_path(r.get_text()))
        pick = icon_button("document-open-symbolic", tr('Datei wählen'), ["flat"])
        pick.connect("clicked", lambda *_: self.choose_file())
        self.path_row.add_suffix(pick)
        self.group.add(self.path_row)
        lic = Adw.ActionRow(title=tr('Lizenz'),
                            subtitle=tr('comet von imLinguin, {0} – wird unverändert aus dem offiziellen Release geladen und per SHA-256 geprüft.').format(gsvc.COMET_LICENSE))
        web = icon_button("external-link-symbolic", tr('Projektseite öffnen'), ["flat"])
        web.set_valign(Gtk.Align.CENTER)
        web.connect("clicked", lambda *_: open_uri(gsvc.COMET_REPO_URL))
        lic.add_suffix(web)
        self.group.add(lic)
        self.refresh()

    # Zustand
    def refresh(self) -> None:
        comet_path, enabled = self.dlg.s.comet_path, self.dlg.s.feature_galaxy_services     # Werte im Hauptthread lesen
        run_async(lambda: gsvc.status_info(comet_path, enabled), self.show, lambda e: self.comet_row.set_subtitle(str(e)))

    def show(self, info: dict) -> None:
        self.comet_row.set_subtitle(gsvc.status_text(info))
        self.dummy_row.set_subtitle(info["dummy"] or tr('Nicht gefunden – wird zusammen mit comet heruntergeladen.'))
        if self.busy:
            return
        label = gsvc.download_label(info)
        self.dl_row.set_visible(bool(label))
        if label:
            self.dl_row.set_title(label)
        self.dl_row.set_sensitive(True)

    def download(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.dl_row.set_sensitive(False)
        self.dl_row.set_title(tr('Wird heruntergeladen …'))
        comet_path = self.dlg.s.comet_path
        shown = [-1]

        def progress(done: int, total: int) -> None:          # Download-Thread → nur bei neuem Prozentwert
            pct = int(done * 100 / max(total, 1))
            if pct != shown[0]:
                shown[0] = pct
                idle(self.dl_row.set_title, tr('Wird heruntergeladen … {0} %').format(pct))

        def done(res) -> None:
            _tool, new_path = res
            self.busy = False
            if new_path:                      # ältere comet-Version hätte sonst Vorrang → eigenen Download eintragen
                self.path_row.set_text(new_path)
                self.dlg._save(comet_path=new_path)
            self.dlg.add_toast(Adw.Toast.new(tr('comet {0} heruntergeladen').format(gsvc.COMET_VERSION)))
            self.refresh()

        def failed(exc: Exception) -> None:
            self.busy = False
            self.dlg.add_toast(Adw.Toast.new(str(exc)))
            self.refresh()
        run_async(lambda: gsvc.download_and_select(comet_path, progress=progress), done, failed)

    def apply_path(self, text: str) -> None:
        text = text.strip()
        if text:
            p = Path(os.path.expanduser(text))
            if not (p.is_file() and os.access(p, os.X_OK)):
                self.dlg.add_toast(Adw.Toast.new(tr('Nicht gefunden oder nicht ausführbar: {0}').format(p)))
                return
        self.dlg._save(comet_path=text)
        self.comet_row.set_subtitle(tr('Wird gesucht …'))
        self.refresh()

    def choose_file(self) -> None:
        fd = Gtk.FileDialog(title=tr('comet auswählen'), modal=True)
        start = self.dlg.s.comet_path or os.path.expanduser("~")
        try:
            folder = start if os.path.isdir(start) else os.path.dirname(start)
            if folder:
                fd.set_initial_folder(Gio.File.new_for_path(folder))
        except Exception:
            pass

        def chosen(d: Gtk.FileDialog, res) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.path_row.set_text(f.get_path())
                self.apply_path(f.get_path())
        fd.open(self.dlg.get_root(), None, chosen)


def build_group(dlg):
    return GalaxyServicesGroup(dlg).group


prefs_registry.register("compat", build_group, order=40, key="galaxy_services")
