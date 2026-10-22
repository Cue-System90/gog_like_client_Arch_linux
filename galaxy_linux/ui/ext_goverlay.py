"""GOverlay pro Spiel in der Oberfläche (Logik: ``launch/goverlay_profiles.py``).

* Einstellungen → Kompatibilität: Gruppe „GOverlay“ (Spiele-Ordner eintragen, Profile pro Spiel, globales Profil als
  Rückfall, „GOverlay öffnen“).
* Eigenschaften → Kompatibilität: Profil des Spiels (Automatisch / Aus / vorhandene Profile) und „GOverlay öffnen“.
* Fenster: Abgleich der Ordnerliste und der Cover kurz nach dem Start (wenn die Cover geladen sind) und danach,
  sobald sich Spiele-Ordner, Bibliotheken oder Spiele ändern (Prüfung jede Minute, Arbeit im Hintergrund).
"""
from __future__ import annotations

from ..i18n import tr

import logging

from ..launch import goverlay, goverlay_profiles as gp
from . import prefs_registry, ui_registry
from .util import Adw, GLib, Gtk, run_async

log = logging.getLogger(__name__)

START_DELAY = 15        # Sekunden nach dem Start: Cover der Bibliothek sind dann meist im Bild-Cache
CHECK_INTERVAL = 60     # Sekunden zwischen den Prüfungen auf geänderte Ordner/Spiele


def _open_goverlay(dlg) -> None:
    if not goverlay.open_goverlay():
        dlg.add_toast(Adw.Toast.new(tr('GOverlay ist nicht installiert.')))


def _open_row() -> Adw.ButtonRow:
    row = Adw.ButtonRow(title=tr('GOverlay öffnen'))
    row.set_start_icon_name("preferences-system-symbolic")
    row.set_sensitive(goverlay.status().goverlay)
    return row


def _folders_text(backend) -> str:
    if not gp.goverlay_installed():
        return tr('GOverlay nicht gefunden – eingetragen wird nach dem ersten Start von GOverlay.')
    ours = list(backend.settings.goverlay_added_folders)
    return "\n".join(ours) if ours else tr('keine (von galaxy-linux)')


# --------------------------------------------------------------------------- Einstellungen → Kompatibilität
def build_prefs_group(dlg) -> Adw.PreferencesGroup:
    s = dlg.s
    grp = Adw.PreferencesGroup(
        title="GOverlay",
        description=tr('GOverlay findet außer Steam-Spielen nur Spiele in eingetragenen Ordnern. Ein Klick auf das Spiel '
                       'in GOverlay legt sein Profil an (MangoHud, vkBasalt, OptiScaler …); galaxy-linux startet das '
                       'Spiel dann mit diesem Profil statt mit der globalen Konfiguration.'))
    status = Adw.ActionRow(title=tr('Von galaxy-linux eingetragene Ordner'), subtitle=_folders_text(dlg.backend))
    status.set_subtitle_selectable(True)

    def resync() -> None:
        def done(_rep) -> None:
            status.set_subtitle(_folders_text(dlg.backend))
        run_async(lambda: gp.sync_all(dlg.backend), done,
                  lambda e: dlg.add_toast(Adw.Toast.new(tr('GOverlay-Ordnerliste nicht geschrieben: {0}').format(e))))

    rows = (
        ("goverlay_register_folders", tr('Spiele-Ordner in GOverlay eintragen'),
         tr('Trägt den GOG-Spiele-Ordner, weitere Installationsorte und die Spiele-Ordner eigener Bibliotheken in '
            'GOverlays Ordnerliste ein (nie einzelne Spiele). Danach in GOverlay „Refresh game library“ wählen oder '
            'GOverlay neu starten.'), resync),
        ("goverlay_use_profiles", tr('GOverlay-Profile pro Spiel verwenden'),
         tr('Startet jedes Spiel mit dem bgmod-Wrapper seines GOverlay-Profils. Die eigenen Schalter MangoHud und '
            'vkBasalt entfallen dann (MangoHud bleibt bei Linux-Spielen).'), None),
        ("goverlay_global_fallback", tr('Globales GOverlay-Profil als Rückfall'),
         tr('Spiele ohne eigenes Profil starten mit dem globalen GOverlay-Profil.'), None),
    )
    for key, title, sub, after in rows:
        row = Adw.SwitchRow(title=title, subtitle=sub, active=getattr(s, key))

        def toggled(r, _p, key=key, after=after) -> None:
            if getattr(dlg.s, key) == r.get_active():
                return
            dlg._save(**{key: r.get_active()})
            if after:
                after()
        row.connect("notify::active", toggled)
        # derselbe Schalter steht auch unter „Spielfunktionen“ – beim Anzeigen den aktuellen Wert übernehmen
        row.connect("map", lambda r, key=key: r.get_active() != getattr(dlg.s, key) and r.set_active(getattr(dlg.s, key)))
        grp.add(row)
    grp.add(status)
    op = _open_row()
    op.connect("activated", lambda *_: _open_goverlay(dlg))
    grp.add(op)
    return grp


# --------------------------------------------------------------------------- Eigenschaften → Kompatibilität
def build_game_group(dlg, pid: str) -> Adw.PreferencesGroup | None:
    g = dlg.backend.registry.get(pid)
    if g is None:
        return None
    grp = Adw.PreferencesGroup(
        title="GOverlay",
        description=tr('Mit diesem GOverlay-Profil (MangoHud, vkBasalt, OptiScaler …) startet das Spiel. Automatisch = '
                       'das Profil mit dem Namen des Spielordners, das GOverlay beim Anklicken des Spiels anlegt.'))
    current = dlg.gs.goverlay_profile
    names = [n for n in gp.list_profiles()]
    ids = ["", gp.OFF, *names]
    if current not in ids:
        ids.append(current)
    labels = [tr('Automatisch (Ordner „{0}“)').format(gp.auto_profile_name(g.install_path)), tr('Aus'),
              *[tr('globales GOverlay-Profil') if n == gp.GLOBAL_PROFILE else n for n in ids[2:]]]
    row = Adw.ComboRow(title=tr('GOverlay-Profil'), model=Gtk.StringList.new(labels))
    row.set_selected(ids.index(current))

    def subtitle() -> None:
        text = gp.status_for(dlg.backend, pid)
        row.set_subtitle(text if text is not None else tr('GOverlay nicht gefunden'))
    subtitle()

    def changed(*_: object) -> None:
        i = row.get_selected()
        if 0 <= i < len(ids):
            dlg._save(goverlay_profile=ids[i])
            subtitle()
    row.connect("notify::selected", changed)
    grp.add(row)
    op = _open_row()
    op.connect("activated", lambda *_: _open_goverlay(dlg))
    grp.add(op)
    return grp


# --------------------------------------------------------------------------- Fenster
def window_hook(win) -> None:
    sync = gp.auto_sync(win.backend)
    win.goverlay_sync = sync
    GLib.timeout_add_seconds(START_DELAY, lambda: (sync.request(force=True), False)[1])
    GLib.timeout_add_seconds(CHECK_INTERVAL, lambda: (sync.request(), True)[1])


prefs_registry.register("compat", build_prefs_group, order=40, key="goverlay")
prefs_registry.register_game("compat", build_game_group, order=40, key="goverlay")
ui_registry.register_window_hook("goverlay", window_hook, order=50)
