"""Cloud-Speicherstände in der Oberfläche (Logik: ``content/cloudsaves.py``, Spielstart: ``launch/hook_cloudsaves.py``).

* Spielansicht: Kennzahl CLOUD-SPEICHERSTÄNDE in der Play-Leiste („Synchronisiert · vor 5 Min.“, „Konflikt“,
  „Aus“ …), Tooltip mit Galaxys Wortlaut; ein Klick öffnet Eigenschaften → Spielfunktionen.
* Eigenschaften → Spielfunktionen: Gruppe „Cloud-Speicherstände“ mit Zustand, „Jetzt hochladen“, „Jetzt
  herunterladen“, „Lokale Sicherungen öffnen“ und den aufgelösten Speicherorten. Der Schalter „Lokale Speicherstände
  mit der GOG Galaxy-Cloud synchronisieren“ (Globale Vorgabe/An/Aus) steht direkt darüber in der eingebauten Gruppe.

Netz und Dateien laufen in ``run_async``; Rückfragen (Konflikt, Kontingent, Überschreiben der Cloud) zeigt
``launcher.ui_bridge`` (GtkBridge) als Adw.AlertDialog.
"""
from __future__ import annotations

from ..i18n import tr

import logging

from ..content import cloudsaves
from ..settings import format_bytes
from . import prefs_registry, ui_registry
from .util import Adw, GLib, Gtk, label, pick_icon, run_async

log = logging.getLogger(__name__)

_WARN = {"conflict": "warning", "quota": "warning", "pending": "warning", "error": "error",
         "off": "dim-label", "unsupported": "dim-label"}


def applicable(backend, pid: str | None) -> bool:
    """Installiertes GOG-Windows-Spiel – nur dort gibt es Cloud-Speicherstände (Linux-Versionen haben keine)."""
    if not pid:
        return False
    g = backend.registry.get(str(pid))
    return g is not None and g.platform == "windows" and g.method != "external" and not str(pid).startswith("ext-")


def open_properties(page, pid: str | None) -> None:
    """Eigenschaften des Spiels auf der Seite „Spielfunktionen“ öffnen."""
    if not pid:
        return
    from .dialogs import GameSettingsDialog
    dlg = GameSettingsDialog(page.backend, pid, on_changed=page.refresh_state)
    dlg.set_visible_page_name("features")
    dlg.present(page.win)


# --------------------------------------------------------------------------- Spielansicht
class CloudStat(Gtk.Button):
    """Kennzahl in der Play-Leiste; während eines Abgleichs fragt sie alle zwei Sekunden den Zustand ab."""

    def __init__(self, page) -> None:
        super().__init__(valign=Gtk.Align.CENTER)
        self.page = page
        self.pid: str | None = None
        self._timer = 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.key = label(tr('Cloud-Speicherstände').upper(), css=["stat-key"])
        self.val = label("–", css=["stat-val"], ellipsize=True)
        self.val.set_max_width_chars(26)
        box.append(self.key)
        box.append(self.val)
        self.set_child(box)
        self.add_css_class("flat")
        self.add_css_class("stat-btn")
        self.connect("clicked", lambda *_: open_properties(self.page, self.pid))
        self.set_visible(False)

    def set_game(self, pid: str) -> None:
        self.refresh_state(pid)

    def refresh_state(self, pid: str) -> None:
        self.pid = str(pid)
        backend = self.page.backend
        if not applicable(backend, self.pid):
            self.set_visible(False)
            return
        kind, at, message = cloudsaves.cloud_status(self.pid, backend.game_settings.get(self.pid), backend.settings)
        self.val.set_text(cloudsaves.status_label(kind, at))
        self.set_tooltip_text(cloudsaves.status_tooltip(kind, at, message))
        for css in set(_WARN.values()):
            self.val.remove_css_class(css)
        if kind in _WARN:
            self.val.add_css_class(_WARN[kind])
        self.set_visible(True)
        if kind == "busy" and not self._timer:
            self._timer = GLib.timeout_add_seconds(2, self._poll)

    def _poll(self) -> bool:
        self._timer = 0
        if self.pid:
            self.refresh_state(self.pid)
        return GLib.SOURCE_REMOVE


# --------------------------------------------------------------------------- Eigenschaften → Spielfunktionen
class CloudGroup(Adw.PreferencesGroup):
    def __init__(self, dlg, pid: str) -> None:
        super().__init__(title=tr('Cloud-Speicherstände'))
        self.dlg, self.pid, self.backend = dlg, str(pid), dlg.backend
        self._busy = False
        self._loc_rows: list[Gtk.Widget] = []

        self.state_row = Adw.ActionRow(use_markup=False)
        self.state_row.set_title(tr('Zustand'))
        self.state_row.set_subtitle_selectable(True)
        self.add(self.state_row)
        self.up_row = Adw.ButtonRow(title=tr('Jetzt hochladen'), start_icon_name=pick_icon("go-up-symbolic"))
        self.up_row.connect("activated", lambda *_: self._sync("force-upload"))
        self.down_row = Adw.ButtonRow(title=tr('Jetzt herunterladen'), start_icon_name=pick_icon("go-down-symbolic"))
        self.down_row.connect("activated", lambda *_: self._sync("force-download"))
        self.backup_row = Adw.ActionRow(activatable=True, use_markup=False)
        self.backup_row.set_title(tr('Lokale Sicherungen öffnen'))
        self.backup_row.add_suffix(Gtk.Image.new_from_icon_name(pick_icon("folder-open-symbolic", "folder-symbolic")))
        self.backup_row.connect("activated", lambda *_: self._open_backups())
        self.locs_row = Adw.ExpanderRow(use_markup=False)
        self.locs_row.set_title(tr('Speicherorte'))
        self.locs_row.set_subtitle(tr('werden ermittelt …'))
        for r in (self.up_row, self.down_row, self.backup_row, self.locs_row):
            self.add(r)

        # Zustand auch nach Änderungen am Schalter darüber (Globale Vorgabe/An/Aus) neu anzeigen
        orig = dlg.on_changed
        dlg.on_changed = lambda: (orig(), self.refresh())
        self.refresh()
        self._load_locations()

    # Anzeige
    def _toast(self, text: str) -> None:
        self.dlg.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def refresh(self) -> None:
        gs = self.backend.game_settings.get(self.pid)
        kind, at, message = cloudsaves.cloud_status(self.pid, gs, self.backend.settings)
        text = cloudsaves.status_tooltip(kind, at, message)
        if gs.cloud_saves == "" and not self.backend.settings.feature_cloud_saves:
            text += "\n" + tr('Cloud-Speicherstände sind in den Einstellungen (Spielfunktionen) deaktiviert.')
        self.state_row.set_subtitle(text)
        n = len(cloudsaves.list_backups(self.pid))
        self.backup_row.set_subtitle(tr('Sicherungen: {0} · {1}').format(n, cloudsaves.backup_dir(self.pid)) if n
                                     else tr('Noch keine Sicherungen'))
        busy = self._busy or kind == "busy"
        for r in (self.up_row, self.down_row):
            r.set_sensitive(not busy)

    def _load_locations(self) -> None:
        pid, backend = self.pid, self.backend

        def work() -> list[tuple]:
            gc = cloudsaves.game_cloud(backend.launcher, backend.api, pid, need_secret=False)
            return [(loc.name, loc.template, loc.path, *cloudsaves.dir_stats(loc.path)) for loc in gc.locations]

        def done(rows: list[tuple]) -> None:
            for r in self._loc_rows:
                self.locs_row.remove(r)
            self._loc_rows = []
            self.locs_row.set_subtitle(", ".join(r[0] for r in rows) or tr('keine'))
            for name, template, path, files, size, _latest in rows:
                row = Adw.ActionRow(use_markup=False)       # erst danach Texte setzen (Pfade mit „&“)
                row.set_title(name)
                row.set_subtitle(str(path) if path else tr('nicht auflösbar: {0}').format(template))
                row.set_subtitle_selectable(True)
                row.add_suffix(label(tr('Dateien: {0} · {1}').format(files, format_bytes(size)), css=["dim-label"]))
                btn = Gtk.Button(icon_name=pick_icon("folder-open-symbolic", "folder-symbolic"),
                                 tooltip_text=tr('Ordner öffnen'), valign=Gtk.Align.CENTER)
                btn.add_css_class("flat")
                btn.set_sensitive(bool(path) and path.is_dir())
                btn.connect("clicked", lambda _b, p=path: self.backend.launcher.open_path(p))
                row.add_suffix(btn)
                self.locs_row.add_row(row)
                self._loc_rows.append(row)

        def fail(exc: Exception) -> None:
            self.locs_row.set_subtitle(str(exc))

        run_async(work, done, fail)

    # Aktionen
    def _open_backups(self) -> None:
        if not cloudsaves.list_backups(self.pid):
            self._toast(tr('Noch keine Sicherungen – sie entstehen, bevor Spielstände aus der Cloud lokale Dateien ersetzen.'))
            return
        self.backend.launcher.open_path(cloudsaves.backup_dir(self.pid))

    def _sync(self, mode: str) -> None:
        pid, backend = self.pid, self.backend
        if backend.launcher.is_running(pid):
            self._toast(tr('Das Spiel läuft – die Spielstände werden nach dem Beenden synchronisiert.'))
            return
        if not backend.api.logged_in:
            self._toast(tr('Nicht angemeldet – Cloud-Speicherstände brauchen eine GOG-Anmeldung.'))
            return
        self._busy = True
        self.state_row.set_subtitle(tr('Synchronisiere Cloud-Spielstände …'))
        self.refresh_buttons()
        bridge = backend.launcher.ui_bridge

        def work() -> cloudsaves.SyncResult:
            return cloudsaves.sync_game(backend.launcher, backend.api, pid, mode, ask=bridge.ask, confirm=True)

        def done(res: cloudsaves.SyncResult) -> None:
            self._busy = False
            self._toast(res.summary() + (" · " + tr('Sicherung angelegt') if res.backup else ""))
            self.dlg.on_changed()
            self._load_locations()

        def fail(exc: Exception) -> None:
            self._busy = False
            self._toast(tr('Cloud-Synchronisierung fehlgeschlagen: {0}').format(exc))
            self.refresh()

        run_async(work, done, fail)

    def refresh_buttons(self) -> None:
        for r in (self.up_row, self.down_row):
            r.set_sensitive(not self._busy)


def _group(dlg, pid: str):
    return CloudGroup(dlg, pid) if applicable(dlg.backend, pid) else None


ui_registry.register_game_section("cloudsaves", CloudStat, column="stats", order=50)
prefs_registry.register_game("features", _group, order=30, key="cloudsaves")
