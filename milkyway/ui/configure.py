"""Fenster „Konfigurieren …“ wie in GOG Galaxy 2.1 (Installation verwalten → Konfigurieren …):

* Installation: Sprache (lädt die Sprachdateien), „Automatisch auf die neuste Version aktualisieren“ – aus: Version
  wählen (Rollback, „Mehr Versionen anzeigen“), Beta-Kanäle mit „Private Kanäle hinzufügen“,
* Funktionen: Cloud-Speicherstände und Galaxy-Funktionen im Spiel pro Spiel,
* DLC: eigene DLCs (de)installieren, „Alle auswählen“,
* Start-Parameter: Standardmäßige Dateiverknüpfung und benutzerdefinierte Dateiverknüpfungen / Argumente.

Jede Auswahl wirkt sofort (Aufträge erscheinen in den Downloads), kein „OK“.
"""
from __future__ import annotations

from ..i18n import tr

import logging
from pathlib import Path

from ..content import build_records
from ..content.installer import InstallError
from ..content.manifest import BuildManifest
from ..settings import resolve_switch
from .util import LANGUAGES, Adw, Gio, GLib, Gtk, icon_button, run_async

log = logging.getLogger(__name__)

SHORT_VERSIONS = 5


def _lang_name(code: str) -> str:
    for c, name in LANGUAGES:
        if c.lower() == code.lower() or c.split("-")[0].lower() == code.split("-")[0].lower():
            return name
    return code


class ConfigureDialog(Adw.PreferencesDialog):
    def __init__(self, win, product_id: str) -> None:
        super().__init__(content_width=760, content_height=640)
        self.win, self.backend, self.pid = win, win.backend, str(product_id)
        self.game = self.backend.registry.get(self.pid)
        title = self.game.title if self.game else self.pid
        self.set_title(tr('Konfiguration von „{0}“').format(title))
        self._building = True
        self._builds: list = []
        self._show_all = False
        for page in (self._page_installation(), self._page_features(), self._page_dlc(), self._page_launch()):
            self.add(page)
        self._building = False
        self._load_builds()
        self._load_languages()

    def _toast(self, text: str) -> None:
        self.add_toast(Adw.Toast.new(GLib.markup_escape_text(text)))

    def _job(self, fn, done_text: str) -> None:
        try:
            fn()
        except InstallError as exc:
            self._toast(str(exc))
            return
        self._toast(done_text)
        self.win.refresh_game_state(self.pid)
        if self.win.current_pid == self.pid:
            self.win.page.refresh_state()

    # ------------------------------------------------------------------ Installation
    def _page_installation(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Installation'), icon_name="drive-harddisk-symbolic", name="installation")
        lg = Adw.PreferencesGroup(title=tr('Sprache'))
        self.lang_row = Adw.ComboRow(title=tr('Sprache'), subtitle=tr('wird ermittelt …'),
                                     model=Gtk.StringList.new([_lang_name(self.game.language) if self.game and self.game.language else "–"]))
        self.lang_row.set_sensitive(False)
        self._langs: list[str] = []
        self.lang_row.connect("notify::selected", self._on_language)
        lg.add(self.lang_row)
        page.add(lg)

        ug = Adw.PreferencesGroup(title=tr('Automatische Updates'))
        gs = self.backend.game_settings.get(self.pid)
        self.auto_row = Adw.SwitchRow(title=tr('Automatisch auf die neuste Version aktualisieren'), active=gs.auto_update)
        self.auto_row.connect("notify::active", self._on_auto)
        ug.add(self.auto_row)
        if not self.backend.settings.feature_auto_update:
            ug.set_description(tr('Automatische Updates sind in den Einstellungen deaktiviert – wir benachrichtigen dich, wenn ein Update verfügbar ist.'))
        self.version_row = Adw.ComboRow(title=tr('Version'), subtitle=tr('wird ermittelt …'), model=Gtk.StringList.new(["–"]))
        self.version_row.connect("notify::selected", self._on_version)
        ug.add(self.version_row)
        self.more_row = Adw.ButtonRow(title=tr('Mehr Versionen anzeigen'))
        self.more_row.connect("activated", self._toggle_more)
        ug.add(self.more_row)
        page.add(ug)

        bg = Adw.PreferencesGroup(title=tr('Beta-Kanäle'))
        self.branch_row = Adw.ComboRow(title=tr('Kanal'), subtitle=tr('wird ermittelt …'), model=Gtk.StringList.new([tr('Ausgeschaltet')]))
        self._branches: list[str | None] = [None]
        self.branch_row.connect("notify::selected", self._on_branch)
        bg.add(self.branch_row)
        self.pw_row = Adw.PasswordEntryRow(title=tr('Passwort für privaten Kanal ändern') if gs.branch_password
                                           else tr('Private Kanäle hinzufügen'), show_apply_button=True)
        self.pw_row.connect("apply", self._on_password)
        bg.add(self.pw_row)
        page.add(bg)
        self._update_version_visibility()
        return page

    def _update_version_visibility(self) -> None:
        manual = not self.auto_row.get_active()
        self.version_row.set_visible(manual)
        self.more_row.set_visible(manual and len(self._channel_builds()) > SHORT_VERSIONS)

    def _channel_builds(self) -> list:
        branch = (self.game.branch or None) if self.game else None
        return [b for b in self._builds if (b.branch or None) == branch]

    def _load_builds(self) -> None:
        def done(builds) -> None:
            self._builds = builds
            self._fill_versions()
            self._fill_branches()

        def fail(exc) -> None:
            for row in (self.version_row, self.branch_row):
                row.set_subtitle(tr('nicht ermittelbar: {0}').format(exc))
        run_async(lambda: self.backend.available_builds(self.pid), done, fail)

    def _fill_versions(self) -> None:
        builds = self._channel_builds()
        shown = builds if self._show_all else builds[:SHORT_VERSIONS]
        cur = self.game.build_id if self.game else ""
        if cur and cur not in [b.build_id for b in shown]:
            shown = shown + [b for b in builds if b.build_id == cur]
        self._shown_builds = shown
        names = [b.label + (tr(' (installiert)') if b.build_id == cur else "") for b in shown] or ["–"]
        self._building = True
        self.version_row.set_model(Gtk.StringList.new(names))
        ids = [b.build_id for b in shown]
        if cur in ids:
            self.version_row.set_selected(ids.index(cur))
        self._building = False
        self.version_row.set_subtitle(tr('Ältere Version wählen, um zu ihr zurückzukehren.'))
        self.more_row.set_title(tr('Weniger Versionen anzeigen') if self._show_all else tr('Mehr Versionen anzeigen'))
        self._update_version_visibility()

    def _toggle_more(self, *_: object) -> None:
        self._show_all = not self._show_all
        self._fill_versions()

    def _fill_branches(self) -> None:
        branches = [None] + sorted({b.branch for b in self._builds if b.branch})
        self._branches = branches
        self._building = True
        self.branch_row.set_model(Gtk.StringList.new([tr('Ausgeschaltet')] + [str(b) for b in branches[1:]]))
        cur = (self.game.branch or None) if self.game else None
        self.branch_row.set_selected(branches.index(cur) if cur in branches else 0)
        self._building = False
        self.branch_row.set_subtitle(tr('{0} Kanäle verfügbar').format(len(branches) - 1) if len(branches) > 1
                                     else tr('Für dieses Spiel gibt es keine öffentlichen Beta-Kanäle.'))

    def _on_auto(self, row: Adw.SwitchRow, _p) -> None:
        if self._building:
            return
        self.backend.set_auto_update(self.pid, row.get_active())
        self._update_version_visibility()

    def _on_version(self, row: Adw.ComboRow, _p) -> None:
        if self._building or not getattr(self, "_shown_builds", None):
            return
        b = self._shown_builds[row.get_selected()]
        if self.game and b.build_id == self.game.build_id:
            return
        self._job(lambda: self.backend.change_build(self.pid, b.build_id),
                  tr('Version {0} wird installiert – siehe Downloads.').format(b.version_name or b.build_id))

    def _on_branch(self, row: Adw.ComboRow, _p) -> None:
        if self._building:
            return
        br = self._branches[row.get_selected()]
        if self.game and (self.game.branch or None) == br:
            return
        self._job(lambda: self.backend.change_branch(self.pid, br),
                  tr('Kanal „{0}“ wird installiert – siehe Downloads.').format(br or tr('Ausgeschaltet')))

    def _on_password(self, row: Adw.PasswordEntryRow) -> None:
        pw = row.get_text()
        if not pw:
            return

        def done(found: list[str]) -> None:
            row.set_text("")
            row.set_title(tr('Passwort für privaten Kanal ändern'))
            self._toast(tr('Privater Kanal hinzugefügt: {0}').format(", ".join(found)))
            self._load_builds()
        run_async(lambda: self.backend.add_private_channel(self.pid, pw), done, lambda e: self._toast(str(e)))

    def _load_languages(self) -> None:
        g = self.game

        def work() -> list[str]:
            rec = build_records.load_record(self.pid) or {}
            link = str(rec.get("manifest_link") or "")
            if not link:
                build = next((b for b in self.backend.available_builds(self.pid) if b.build_id == g.build_id), None)
                link = build.link if build else ""
            if not link:
                return []
            return BuildManifest.parse(self.backend.api.zlib_json(link)).available_languages

        def done(langs: list[str]) -> None:
            self._langs = langs
            if not langs:
                self.lang_row.set_subtitle(tr('Sprache im Spiel wählbar'))
                return
            cur = (g.language or "").lower()
            names = [_lang_name(code) for code in langs]
            self._building = True
            self.lang_row.set_model(Gtk.StringList.new(names))
            idx = next((i for i, c in enumerate(langs) if c.lower() == cur), None)
            if idx is None:
                idx = next((i for i, c in enumerate(langs) if c.split("-")[0].lower() == cur.split("-")[0]), 0)
            self.lang_row.set_selected(idx)
            self._building = False
            self.lang_row.set_sensitive(len(langs) > 1)
            self.lang_row.set_subtitle(tr('Wechsel lädt die Sprachdateien herunter') if len(langs) > 1 else tr('Nur eine Sprache verfügbar'))
        if g is None:
            return
        run_async(work, done, lambda e: self.lang_row.set_subtitle(tr('nicht ermittelbar: {0}').format(e)))

    def _on_language(self, row: Adw.ComboRow, _p) -> None:
        if self._building or not self._langs:
            return
        code = self._langs[row.get_selected()]
        if self.game and code.lower() == (self.game.language or "").lower():
            return
        self._job(lambda: self.backend.change_language(self.pid, code),
                  tr('Sprache wird geändert: {0} – siehe Downloads.').format(_lang_name(code)))

    # ------------------------------------------------------------------ Funktionen
    def _page_features(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Funktionen'), icon_name="applications-games-symbolic", name="features")
        gs = self.backend.game_settings.get(self.pid)
        s = self.backend.settings
        windows = bool(self.game and self.game.platform == "windows" and self.game.method != "external")
        for key, title, label, glob, off_text in (
                ("cloud_saves", tr('Cloud-Speicherstände'), tr('Lokale Speicherstände mit der GOG Galaxy-Cloud synchronisieren'),
                 s.feature_cloud_saves, tr('Cloud-Speicherstände sind in den Einstellungen deaktiviert.')),
                ("galaxy_services", tr('Einblendungen'), tr('GOG Galaxy-Funktionen im Spiel nutzen'), s.feature_galaxy_services,
                 tr('Galaxy-Dienste im Spiel sind in den Einstellungen deaktiviert.'))):
            grp = Adw.PreferencesGroup(title=title)
            row = Adw.SwitchRow(title=label, active=resolve_switch(getattr(gs, key), glob))
            row.set_sensitive(windows and glob)
            if not glob:
                row.set_subtitle(off_text)
            elif not windows:
                row.set_subtitle(tr('Nur für die Windows-Version des Spiels.'))
            row.connect("notify::active", lambda r, _p, k=key, g=glob: self._building or self.backend.game_settings.update(
                self.pid, **{k: "" if r.get_active() == g else ("on" if r.get_active() else "off")}))
            grp.add(row)
            page.add(grp)
        return page

    # ------------------------------------------------------------------ DLC
    def _page_dlc(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('DLC'), icon_name="list-add-symbolic", name="dlc")
        self.dlc_group = Adw.PreferencesGroup(title=tr('Eigene DLCs'),
                                              description=tr('Häkchen setzen oder entfernen installiert bzw. entfernt den DLC sofort.'))
        all_btn = Gtk.Button(label=tr('Alle auswählen'), valign=Gtk.Align.CENTER)
        all_btn.add_css_class("flat")
        all_btn.connect("clicked", self._select_all)
        self.dlc_group.set_header_suffix(all_btn)
        page.add(self.dlc_group)
        self._dlc_checks: dict[str, Gtk.CheckButton] = {}
        self._dlc_rows: list[Gtk.Widget] = []
        run_async(lambda: self.backend.owned_dlcs(self.pid), self._fill_dlcs, lambda e: self._fill_dlcs([]))
        return page

    def _fill_dlcs(self, dlcs: list) -> None:
        for r in self._dlc_rows:
            self.dlc_group.remove(r)
        self._dlc_rows.clear()
        self._dlc_checks.clear()
        if not dlcs:
            r = Adw.ActionRow(title=tr('Keine DLCs im Besitz'))
            self.dlc_group.add(r)
            self._dlc_rows.append(r)
            return
        self._building = True
        for did, title, installed in dlcs:
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=tr('Installiert') if installed else "",
                                activatable=True)
            check = Gtk.CheckButton(active=installed, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda *_: self._apply_dlcs())
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.dlc_group.add(row)
            self._dlc_rows.append(row)
            self._dlc_checks[did] = check
        self._building = False

    def _select_all(self, *_: object) -> None:
        self._building = True
        for c in self._dlc_checks.values():
            c.set_active(True)
        self._building = False
        self._apply_dlcs()

    def _apply_dlcs(self) -> None:
        if self._building:
            return
        sel = [d for d, c in self._dlc_checks.items() if c.get_active()]
        self._job(lambda: self.backend.change_dlcs(self.pid, sel), tr('DLC-Auswahl wird übernommen – siehe Downloads.'))

    # ------------------------------------------------------------------ Start-Parameter
    def _page_launch(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Start-Parameter'), icon_name="utilities-terminal-symbolic", name="launch")
        self.launch_page = page
        dg = Adw.PreferencesGroup(title=tr('Standardmäßige Dateiverknüpfung'))
        self.default_row = Adw.ComboRow(title=tr('Dateiverknüpfung'))
        self.default_row.connect("notify::selected", self._on_default)
        dg.add(self.default_row)
        page.add(dg)
        self.custom_group = Adw.PreferencesGroup(title=tr('Benutzerdefinierte Dateiverknüpfungen / Argumente'),
                                                 description=tr('Pfad relativ zum Spielordner oder absolut; Argumente wie in der Windows-Verknüpfung.'))
        page.add(self.custom_group)
        add = Adw.PreferencesGroup()
        btn = Adw.ButtonRow(title=tr('Weitere Dateiverknüpfungen / Argumente hinzufügen'))
        btn.set_start_icon_name("list-add-symbolic")
        btn.connect("activated", lambda *_: self._add_task())
        add.add(btn)
        page.add(add)
        self._custom_rows: list[Gtk.Widget] = []
        self._rebuild_tasks()
        return page

    def _tasks(self) -> list[dict]:
        return [dict(d) for d in self.backend.game_settings.get(self.pid).custom_tasks if isinstance(d, dict)]

    def _save_tasks(self, tasks: list[dict]) -> None:
        self.backend.game_settings.update(self.pid, custom_tasks=tasks)
        self._rebuild_tasks()
        self.win.page.refresh_state()

    def _rebuild_tasks(self) -> None:
        g = self.game
        names = []
        if g is not None:
            try:
                names = [t.name for t in self.backend.launcher.tasks_for(g)]
            except Exception:  # pragma: no cover
                names = []
        self._task_names = names
        cur = self.backend.game_settings.get(self.pid).play_task
        was = self._building
        self._building = True
        self.default_row.set_model(Gtk.StringList.new([tr('Standard (primärer Eintrag)')] + names))
        self.default_row.set_selected(names.index(cur) + 1 if cur in names else 0)
        self._building = was
        for r in self._custom_rows:
            self.custom_group.remove(r)
        self._custom_rows.clear()
        for i, t in enumerate(self._tasks()):
            exp = Adw.ExpanderRow(title=GLib.markup_escape_text(t.get("name") or tr('Meine Bezeichnung')),
                                  subtitle=GLib.markup_escape_text(" ".join(x for x in (t.get("path", ""), t.get("arguments", "")) if x)))
            for key, title in (("name", tr('Meine Bezeichnung')), ("path", tr('Datei')), ("arguments", tr('Argumente'))):
                row = Adw.EntryRow(title=title, text=str(t.get(key) or ""), show_apply_button=True)
                row.connect("apply", lambda r, idx=i, k=key: self._set_field(idx, k, r.get_text()))
                if key == "path":
                    pick = icon_button("document-open-symbolic", tr('Dateiverknüpfung auswählen'), ["flat"])
                    pick.set_valign(Gtk.Align.CENTER)
                    pick.connect("clicked", lambda _b, idx=i: self._pick_file(idx))
                    row.add_suffix(pick)
                exp.add_row(row)
            acts = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12, halign=Gtk.Align.END)
            dup = Gtk.Button(label=tr('Kopieren'))
            dup.connect("clicked", lambda _b, idx=i: self._duplicate(idx))
            rem = Gtk.Button(label=tr('Dateiverknüpfung entfernen'))
            rem.add_css_class("destructive-action")
            rem.connect("clicked", lambda _b, idx=i: self._remove_task(idx))
            acts.append(dup)
            acts.append(rem)
            exp.add_row(acts)
            self.custom_group.add(exp)
            self._custom_rows.append(exp)
        if not self._custom_rows:
            r = Adw.ActionRow(title=tr('Keine eigenen Einträge'))
            self.custom_group.add(r)
            self._custom_rows.append(r)

    def _on_default(self, row: Adw.ComboRow, _p) -> None:
        if self._building:
            return
        i = row.get_selected()
        self.backend.game_settings.update(self.pid, play_task="" if i == 0 else self._task_names[i - 1])
        self.win.page.refresh_state()

    def _unique_name(self, base: str) -> str:
        names = set(getattr(self, "_task_names", [])) | {t.get("name") for t in self._tasks()}
        name, n = base, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        return name

    def _add_task(self) -> None:
        g = self.game
        path = ""
        if g is not None:
            try:
                primary = self.backend.launcher.select_task(g, self.backend.game_settings.get(self.pid))
                path = primary.path
            except Exception:
                path = ""
        tasks = self._tasks()
        tasks.append({"name": self._unique_name(tr('Meine Bezeichnung')), "path": path, "arguments": "", "working_dir": ""})
        self._save_tasks(tasks)

    def _duplicate(self, i: int) -> None:
        tasks = self._tasks()
        if 0 <= i < len(tasks):
            copy = dict(tasks[i])
            copy["name"] = self._unique_name(str(copy.get("name") or tr('Meine Bezeichnung')))
            tasks.insert(i + 1, copy)
            self._save_tasks(tasks)

    def _remove_task(self, i: int) -> None:
        tasks = self._tasks()
        if 0 <= i < len(tasks):
            removed = tasks.pop(i)
            if self.backend.game_settings.get(self.pid).play_task == removed.get("name"):
                self.backend.game_settings.update(self.pid, play_task="")
            self._save_tasks(tasks)

    def _set_field(self, i: int, key: str, value: str) -> None:
        tasks = self._tasks()
        if not 0 <= i < len(tasks):
            return
        value = value.strip()
        if key == "name":
            if not value:
                return
            old = tasks[i].get("name")
            value = value if value == old else self._unique_name(value)
            if self.backend.game_settings.get(self.pid).play_task == old:
                self.backend.game_settings.update(self.pid, play_task=value)
        if key == "path" and value and self.game is not None:
            p = Path(value).expanduser()
            try:
                value = str(p.relative_to(self.game.install_path)) if p.is_absolute() else value
            except ValueError:
                pass
        tasks[i][key] = value
        self._save_tasks(tasks)

    def _pick_file(self, i: int) -> None:
        g = self.game
        dlg = Gtk.FileDialog(title=tr('Dateiverknüpfung auswählen'), modal=True)
        if g is not None:
            dlg.set_initial_folder(Gio.File.new_for_path(g.install_path))

        def done(d, res) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._set_field(i, "path", f.get_path())
        dlg.open(self.get_root(), None, done)
