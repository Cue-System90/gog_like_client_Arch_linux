"""Dialoge: Login, Installation, Spiel-Eigenschaften (wie Steam), globale Einstellungen, eigene Bibliotheken."""
from __future__ import annotations

from ..i18n import LANGUAGES as UI_LANGUAGES, tr

import os
import shlex
from pathlib import Path
from typing import Callable

from .. import paths
from ..api import auth
from ..api.client import ApiError
from ..content.installer import InstallError, safe_dirname
from ..content.manifest import BuildManifest
from ..jsonstore import read_json
from ..launch import compat, goverlay, launch_options
from ..launch.launcher import LaunchError
from ..settings import GameSettings, format_bytes
from . import prefs_registry, store, web
from .util import (LANGUAGES, Adw, Gio, GLib, Gtk, choose_folder, copy_text, icon_button, idle, label,
                   language_index, open_uri, pick_icon, run_async)


# ----------------------------------------------------------------------------- Login
def _webkit():
    """WebKitGTK 6.0 für den eingebetteten Login wie im Windows-Client (gemeinsame Sitzung mit dem Store: ui/web.py)."""
    return web.webkit()


class LoginDialog(Adw.Dialog):
    """GOG-Login: eingebettete Anmeldeseite (WebKitGTK) oder Einfügen der Weiterleitungs-Adresse."""

    def __init__(self, backend, on_success: Callable[[dict], None]) -> None:
        super().__init__(title=tr('Bei GOG anmelden'), content_width=640)
        self.webkit = _webkit()
        if self.webkit:
            self.set_content_height(760)
        self.backend = backend
        self.on_success = on_success
        self._busy = False
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        self.error = label("", css=["error"], wrap=True)
        self.error.set_margin_start(16)
        self.error.set_margin_end(16)
        self.spinner = Gtk.Spinner()
        if self.webkit:
            self._build_webview(tv, hb)
        else:
            self._build_paste(tv)
        self.set_child(tv)

    # --- Variante 1: eingebetteter Browser
    def _build_webview(self, tv: Adw.ToolbarView, hb: Adw.HeaderBar) -> None:
        WebKit = self.webkit
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hint = label(tr('Melde dich bei GOG an – der Client übernimmt den Login automatisch.'), css=["dim"], wrap=True)
        hint.set_margin_start(16)
        hint.set_margin_end(16)
        hint.set_margin_top(6)
        hint.set_margin_bottom(6)
        box.append(hint)
        box.append(self.error)
        try:
            self.view = web.new_view()      # dieselben Cookies wie der eingebettete Store
        except Exception:
            self.view = WebKit.WebView()
        self.view.set_vexpand(True)
        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("load-changed", lambda v, ev: self.spinner.start() if ev != WebKit.LoadEvent.FINISHED else self.spinner.stop())
        box.append(self.view)
        tv.set_content(box)
        reload = icon_button(pick_icon("view-refresh-symbolic"), tr('Login-Seite neu laden'), ["flat"])
        reload.connect("clicked", lambda *_: self.view.load_uri(auth.AUTH_URL))
        hb.pack_start(reload)
        ext = Gtk.Button(label=tr('Im Browser anmelden'))
        ext.set_tooltip_text(tr('Stattdessen den System-Browser nutzen und die Adresse einfügen'))
        ext.connect("clicked", self._switch_to_paste)
        hb.pack_end(ext)
        hb.pack_end(self.spinner)
        self.view.load_uri(auth.AUTH_URL)

    def _on_decide_policy(self, view, decision, decision_type) -> bool:
        WebKit = self.webkit
        if decision_type not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        try:
            uri = decision.get_navigation_action().get_request().get_uri()
        except Exception:
            return False
        if uri.startswith("https://embed.gog.com/on_login_success"):
            code = auth.parse_code(uri)
            decision.ignore()
            if code:
                self._exchange(code)
            else:
                self.error.set_text(tr('GOG hat keinen Anmeldecode übergeben – bitte erneut versuchen.'))
            return True
        return False

    def _switch_to_paste(self, *_: object) -> None:
        self.webkit = None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self._build_paste(tv)
        self.set_child(tv)
        open_uri(auth.AUTH_URL)

    # --- Variante 2: Adresse einfügen
    def _build_paste(self, tv: Adw.ToolbarView) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=14, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(label(
            tr('1. „GOG-Login im Browser öffnen“ und bei GOG anmelden (auch mit Zwei-Faktor-Code).\n2. Nach dem Login landet der Browser auf einer leeren Seite von embed.gog.com – das ist richtig so.\n3. Die komplette Adresse dieser Seite aus der Adressleiste kopieren und hier einfügen. Der Login startet automatisch, sobald ein Code erkannt wird.'), wrap=True))
        open_btn = Gtk.Button(label=tr('GOG-Login im Browser öffnen'))
        open_btn.add_css_class("suggested-action")
        open_btn.add_css_class("big-button")
        open_btn.connect("clicked", lambda *_: open_uri(auth.AUTH_URL))
        box.append(open_btn)
        row = Gtk.Box(spacing=6)
        self.entry = Gtk.Entry(placeholder_text="https://embed.gog.com/on_login_success?origin=client&code=…", hexpand=True)
        self.entry.connect("activate", self._login_from_entry)
        self.entry.connect("changed", self._on_entry_changed)
        row.append(self.entry)
        paste = icon_button(pick_icon("edit-paste-symbolic"), tr('Aus der Zwischenablage einfügen'))
        paste.connect("clicked", self._paste_clipboard)
        row.append(paste)
        box.append(row)
        box.append(self.error)
        bottom = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        copy_btn = Gtk.Button(label=tr('Login-Adresse kopieren'))
        copy_btn.set_tooltip_text(tr('Falls sich der Browser nicht öffnet: Adresse kopieren und manuell aufrufen'))
        copy_btn.connect("clicked", lambda b: copy_text(b, auth.AUTH_URL))
        bottom.append(copy_btn)
        bottom.append(self.spinner)
        self.login_btn = Gtk.Button(label=tr('Anmelden'))
        self.login_btn.add_css_class("suggested-action")
        self.login_btn.connect("clicked", self._login_from_entry)
        bottom.append(self.login_btn)
        box.append(bottom)
        tv.set_content(box)
        self.entry.connect("map", lambda w: w.grab_focus())

    def _paste_clipboard(self, *_: object) -> None:
        clip = self.get_clipboard()

        def done(c, res) -> None:
            try:
                text = c.read_text_finish(res)
            except Exception:
                text = None
            if text:
                self.entry.set_text(text.strip())
        clip.read_text_async(None, done)

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        text = entry.get_text()
        if "code=" in text and auth.parse_code(text) and not self._busy:
            self._login_from_entry()

    def _login_from_entry(self, *_: object) -> None:
        code = auth.parse_code(self.entry.get_text())
        if not code:
            self.error.set_text(tr('Kein Anmeldecode gefunden – bitte die komplette Adresse der Seite nach dem Login einfügen (sie enthält „code=…“).'))
            return
        self._exchange(code)

    # --- gemeinsam
    def _exchange(self, code: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.error.set_text("")
        self.spinner.start()
        if hasattr(self, "login_btn"):
            self.login_btn.set_sensitive(False)

        def done(user: dict) -> None:
            self.spinner.stop()
            self._busy = False
            self.close()
            self.on_success(user)

        def fail(exc: Exception) -> None:
            self.spinner.stop()
            self._busy = False
            if hasattr(self, "login_btn"):
                self.login_btn.set_sensitive(True)
            self.error.set_text(str(exc))
            if self.webkit:
                self.view.load_uri(auth.AUTH_URL)
        run_async(lambda: self.backend.api.login_with_code(code), done, fail)


# ----------------------------------------------------------------------------- Installation
class InstallDialog(Adw.Dialog):
    def __init__(self, backend, entry, meta, on_confirm: Callable[[dict], None]) -> None:
        super().__init__(title=tr('{0} installieren').format(entry.title), content_width=600)
        self.backend, self.entry, self.meta, self.on_confirm = backend, entry, meta, on_confirm
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label=tr('Installieren'))
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._confirm)
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title=tr('Installation'))
        self.platforms = [p for p in entry.platforms if p in ("windows", "linux")] or ["windows"]
        names = {"windows": tr('Windows-Version (über Proton/Wine)'), "linux": tr('Linux-Version (nativ)')}
        self.platform_row = Adw.ComboRow(title=tr('Plattform'), model=Gtk.StringList.new([names[p] for p in self.platforms]))
        self.platform_row.set_selected(self.platforms.index("linux") if "linux" in self.platforms else 0)
        self.platform_row.connect("notify::selected", lambda *_: self._estimate())
        grp.add(self.platform_row)
        self.lang_row = Adw.ComboRow(title=tr('Sprache'), model=Gtk.StringList.new([n for _, n in LANGUAGES]))
        self.lang_row.set_selected(language_index(backend.settings.language))
        self.lang_row.connect("notify::selected", lambda *_: self._estimate())
        grp.add(self.lang_row)
        # Installationsort wie im Windows-Client: Standard (globaler Spiele-Ordner), gemerkte Orte, anderer Ordner
        self._locations = [backend.settings.install_dir] + [l for l in backend.settings.install_locations if l != backend.settings.install_dir]
        self._custom_root = ""
        self.loc_row = Adw.ComboRow(title=tr('Installationsort'))
        self._rebuild_locations()
        self.loc_row.connect("notify::selected", self._on_location)
        grp.add(self.loc_row)
        self._dirname = safe_dirname(entry.title)
        self.target_row = Adw.ActionRow(title=tr('Wird installiert nach'), subtitle="")
        self.target_row.set_subtitle_selectable(True)
        grp.add(self.target_row)
        self.dlc_row = Adw.SwitchRow(title=tr('Inkl. aller DLCs'), active=True)
        self.dlc_row.connect("notify::active", lambda *_: self._estimate())
        grp.add(self.dlc_row)
        page.add(grp)
        sc = Adw.PreferencesGroup(title=tr('Verknüpfungen'))
        self.menu_row = Adw.SwitchRow(title=tr('Verknüpfung im Startmenü anlegen'), active=backend.settings.shortcut_menu)
        sc.add(self.menu_row)
        self.desktop_row = Adw.SwitchRow(title=tr('Verknüpfung auf dem Schreibtisch anlegen'), active=backend.settings.shortcut_desktop)
        sc.add(self.desktop_row)
        page.add(sc)
        info = Adw.PreferencesGroup(title=tr('Details'))
        self.size_row = Adw.ActionRow(title=tr('Größe'), subtitle=tr('wird ermittelt …'))
        self.version_row = Adw.ActionRow(title=tr('Version'), subtitle="–")
        self.langs_row = Adw.ActionRow(title=tr('Verfügbare Sprachen'), subtitle="–")
        self.method_row = Adw.ActionRow(title=tr('Quelle'), subtitle="–")
        for r in (self.size_row, self.version_row, self.langs_row, self.method_row):
            info.add(r)
        page.add(info)
        tv.set_content(page)
        self.set_child(tv)
        self._update_target()
        self._estimate()

    def _rebuild_locations(self) -> None:
        names = [tr('Standard – globaler Spiele-Ordner ({0})').format(self._locations[0])] + self._locations[1:] + [tr('Anderer Ordner …')]
        self._building_loc = True
        self.loc_row.set_model(Gtk.StringList.new(names))
        self.loc_row.set_selected(0)
        self._building_loc = False

    def _on_location(self, *_: object) -> None:
        if getattr(self, "_building_loc", False):
            return
        i = self.loc_row.get_selected()
        if i == len(self._locations):            # „Anderer Ordner …“
            def chosen(path: str) -> None:
                if path not in self._locations:
                    self._locations.append(path)
                self._rebuild_locations()
                self._building_loc = True
                self.loc_row.set_selected(self._locations.index(path))
                self._building_loc = False
                self._update_target()
            choose_folder(self.get_root(), tr('Installationsordner wählen'), self._locations[0], chosen)
            self._building_loc = True
            self.loc_row.set_selected(0)
            self._building_loc = False
            return
        self._update_target()

    def current_root(self) -> str:
        i = min(self.loc_row.get_selected(), len(self._locations) - 1)
        return self._locations[i]

    def _update_target(self) -> None:
        self.target_row.set_subtitle(str(Path(os.path.expanduser(self.current_root())) / self._dirname))

    def values(self) -> dict:
        return {
            "platform": self.platforms[self.platform_row.get_selected()],
            "language": LANGUAGES[self.lang_row.get_selected()][0],
            "install_root": self.current_root(),
            "include_dlcs": self.dlc_row.get_active(),
            "shortcut_menu": self.menu_row.get_active(),
            "shortcut_desktop": self.desktop_row.get_active(),
        }

    def _estimate(self) -> None:
        v = self.values()
        self.size_row.set_subtitle(tr('wird ermittelt …'))
        api, inst = self.backend.api, self.backend.installer

        def work() -> dict:
            if v["platform"] == "linux":
                installers = inst._installers(self.entry.id, "linux", v["language"])
                if not installers:
                    raise ApiError(tr('Kein Linux-Installer gefunden'))
                i = installers[0]
                product = api.product(self.entry.id) or {}
                langs = sorted({x.get("language_full", "") for x in (product.get("downloads") or {}).get("installers", []) if x.get("os") == "linux"})
                return {"size": int(i.get("total_size", 0) or 0), "version": i.get("version", ""), "langs": ", ".join(l for l in langs if l), "method": tr('GOG-Linux-Installer (.sh)')}
            builds = inst.builds(self.entry.id, "windows")
            v2 = [b for b in builds if b.is_v2 and not b.branch] or [b for b in builds if b.is_v2]
            if not v2:
                installers = inst._installers(self.entry.id, "windows", v["language"])
                size = int(installers[0].get("total_size", 0) or 0) if installers else 0
                return {"size": size, "version": installers[0].get("version", "") if installers else "", "langs": "–", "method": tr('Offline-Installer + innoextract (kein Galaxy-Build)')}
            m = BuildManifest.parse(api.zlib_json(v2[0].link))
            depots, lang = m.select_depots(v["language"], inst.owned_ids(), v["include_dlcs"])
            return {"size": sum(d.size for d in depots), "download": sum(d.compressed_size for d in depots),
                    "version": v2[0].version_name, "langs": ", ".join(m.available_languages) or "–",
                    "method": tr('Galaxy Content-System (Build {0}), Sprache {1}').format(v2[0].build_id, lang),
                    "dirname": safe_dirname(m.install_directory or self.entry.title)}

        def done(d: dict) -> None:
            if d.get("dirname"):
                self._dirname = d["dirname"]
            self._update_target()
            s = format_bytes(d["size"])
            if d.get("download"):
                s += tr(' (Download {0})').format(format_bytes(d['download']))
            self.size_row.set_subtitle(s)
            self.version_row.set_subtitle(d.get("version") or "–")
            self.langs_row.set_subtitle(d.get("langs") or "–")
            self.method_row.set_subtitle(d.get("method") or "–")

        def fail(exc: Exception) -> None:
            self.size_row.set_subtitle(tr('nicht ermittelbar: {0}').format(exc))
        run_async(work, done, fail)

    def _confirm(self, *_: object) -> None:
        v = self.values()
        self.backend.settings.remember_location(v["install_root"])
        if (v["shortcut_menu"], v["shortcut_desktop"]) != (self.backend.settings.shortcut_menu, self.backend.settings.shortcut_desktop):
            self.backend.settings.shortcut_menu = v["shortcut_menu"]
            self.backend.settings.shortcut_desktop = v["shortcut_desktop"]
            self.backend.save_settings()
        self.close()
        self.on_confirm(v)


# ----------------------------------------------------------------------------- Spiel-Eigenschaften
class GameSettingsDialog(Adw.PreferencesDialog):
    """Eigenschaften eines Spiels – Kompatibilität, Startoptionen, Umgebung, Erweitert (wie Steam)."""

    def __init__(self, backend, product_id: str, on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(title=tr('Eigenschaften'), content_width=720, content_height=680)
        self.backend = backend
        self.pid = str(product_id)
        self.on_changed = on_changed or (lambda: None)
        self.gs: GameSettings = backend.game_settings.get(self.pid)
        self.installed = backend.registry.get(self.pid)
        entry = backend.entry(self.pid)
        self.set_title(tr('Eigenschaften – {0}').format(entry.title if entry else self.pid))
        self.tools = backend.launcher.tools()
        self._building = True
        prefs_registry.load_extensions()
        for page in (self._page_compat(), self._page_launch(), self._page_features(), self._page_env(), self._page_advanced()):
            for grp in prefs_registry.build_game_groups(page.get_name(), self, self.pid):
                page.add(grp)
            self.add(page)
        self._building = False
        self._refresh_preview()

    # Hilfen
    def _save(self, **changes) -> None:
        if self._building:
            return
        self.gs = self.backend.game_settings.update(self.pid, **changes)
        self._refresh_preview()
        self.on_changed()

    def _toast(self, text: str) -> None:
        self.add_toast(Adw.Toast.new(text))

    # Seite 1: Kompatibilität
    def _page_compat(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Kompatibilität'), icon_name="application-x-executable-symbolic", name="compat")
        grp = Adw.PreferencesGroup(title=tr('Kompatibilitätstool'),
                                   description=tr('Wie unter Steam: pro Spiel eine Proton-/Wine-Version erzwingen. Die globale Vorgabe gilt, wenn nichts gewählt ist.'))
        default, source = self.backend.launcher.default_tool(self.pid)
        self.tool_ids = [""] + [t.id for t in self.tools]
        names = [f"{source} ({default.name if default else tr('keine gefunden')})"] + [t.label for t in self.tools]
        self.tool_row = Adw.ComboRow(title=tr("Proton-/Wine-Version"), model=Gtk.StringList.new(names))
        self.tool_row.set_selected(self.tool_ids.index(self.gs.compat_tool) if self.gs.compat_tool in self.tool_ids else 0)
        self.tool_row.connect("notify::selected", lambda *_: self._save(compat_tool=self.tool_ids[self.tool_row.get_selected()]))
        grp.add(self.tool_row)
        self.runner_row = Adw.ActionRow(title=tr('Startweg'), subtitle="–")
        grp.add(self.runner_row)
        modes = ["", "on", "off"]
        lib = self.backend.library_of(self.pid)
        if lib is not None and lib.wayland_driver:
            inherited = tr('Vorgabe der Bibliothek ({0})').format(tr('an') if lib.wayland_driver == 'on' else tr('aus'))
        else:
            inherited = tr('Globale Vorgabe ({0})').format(tr('an') if self.backend.settings.wayland_driver else tr('aus'))
        self.wl_row = Adw.ComboRow(title=tr('Wayland-Treiber von Proton'),
                                   subtitle=tr('Behebt Vollbild-Probleme wie weiße Ränder unter XWayland'),
                                   model=Gtk.StringList.new([inherited, tr('An'), tr('Aus (XWayland)')]))
        self.wl_row.set_selected(modes.index(self.gs.wayland_driver) if self.gs.wayland_driver in modes else 0)
        self.wl_row.connect("notify::selected", lambda *_: self._save(wayland_driver=modes[self.wl_row.get_selected()]))
        grp.add(self.wl_row)
        page.add(grp)

        pg = Adw.PreferencesGroup(title=tr('Wine-Prefix'))
        saved, self.gs.prefix = self.gs.prefix, ""
        default_prefix = str(self.backend.launcher.prefix_for(self.pid, self.gs))
        self.gs.prefix = saved
        self.prefix_row = Adw.EntryRow(title=tr('Prefix-Ordner (leer = {0})').format(default_prefix), text=self.gs.prefix, show_apply_button=True)
        self.prefix_row.connect("apply", lambda r: self._save(prefix=r.get_text().strip()))
        b = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
        b.connect("clicked", lambda *_: choose_folder(self.get_root(), tr("Prefix-Ordner"), self.gs.prefix or default_prefix,
                                                      lambda p: (self.prefix_row.set_text(p), self._save(prefix=p))))
        self.prefix_row.add_suffix(b)
        o = icon_button("external-link-symbolic", tr('Im Dateimanager öffnen'), ["flat"])
        o.connect("clicked", lambda *_: self.backend.launcher.open_path(self.backend.launcher.prefix_for(self.pid)))
        self.prefix_row.add_suffix(o)
        pg.add(self.prefix_row)
        verbs = ["waitforexitandrun", "run"]
        self.verb_row = Adw.ComboRow(title=tr('Proton-Verb'), subtitle=tr('waitforexitandrun entspricht Steam'), model=Gtk.StringList.new(verbs))
        self.verb_row.set_selected(verbs.index(self.gs.proton_verb) if self.gs.proton_verb in verbs else 0)
        self.verb_row.connect("notify::selected", lambda *_: self._save(proton_verb=verbs[self.verb_row.get_selected()]))
        pg.add(self.verb_row)
        pf = Adw.SwitchRow(title=tr('protonfixes deaktivieren'), subtitle=tr('umu wendet sonst automatische Spiel-Fixes an'), active=self.gs.disable_protonfixes)
        pf.connect("notify::active", lambda r, _: self._save(disable_protonfixes=r.get_active()))
        pg.add(pf)
        gid = Adw.EntryRow(title=tr('umu GAMEID (leer = umu-{0})').format(self.pid), text=self.gs.umu_game_id, show_apply_button=True)
        gid.connect("apply", lambda r: self._save(umu_game_id=r.get_text().strip()))
        pg.add(gid)
        page.add(pg)

        mg = Adw.PreferencesGroup(title=tr('Prefix verwalten'))
        self.prefix_state = Adw.ActionRow(title=tr('Zustand'), subtitle="")
        mg.add(self.prefix_state)
        for title, icon, cb in ((tr('Prefix jetzt anlegen'), "list-add-symbolic", self._prefix_create),
                                (tr('Prefix neu anlegen (zurücksetzen)'), "view-refresh-symbolic", self._prefix_reset),
                                (tr('Prefix löschen'), "user-trash-symbolic", self._prefix_delete)):
            r = Adw.ButtonRow(title=title)
            r.set_start_icon_name(icon)
            r.set_sensitive(self.installed is not None and self.installed.platform == "windows")
            r.connect("activated", cb)
            mg.add(r)
        page.add(mg)
        self._update_prefix_state()
        return page

    def _update_prefix_state(self) -> None:
        p = self.backend.launcher.prefix_for(self.pid)
        if self.backend.jobs.active_for(self.pid):
            txt = tr('wird bearbeitet … · {0}').format(p)
        else:
            txt = (tr('vorhanden · ') if self.backend.launcher.prefix_exists(self.pid) else tr('noch nicht angelegt · ')) + str(p)
        self.prefix_state.set_subtitle(txt)

    def _prefix_create(self, *_: object) -> None:
        if self.backend.launcher.prefix_exists(self.pid):
            self._toast(tr('Prefix ist bereits vorhanden'))
            return
        self._toast(tr('Prefix wird angelegt …') if self.backend.queue_prefix(self.pid) else tr('Prefix wird bereits bearbeitet'))
        self._update_prefix_state()

    def _prefix_reset(self, *_: object) -> None:
        confirm_destructive(self, tr('Prefix neu anlegen?'), tr('Der Prefix wird gelöscht und frisch angelegt. Spielstände, die im Prefix liegen (Dokumente, AppData), gehen dabei verloren.'), tr('Neu anlegen'), self._do_reset)

    def _do_reset(self) -> None:
        try:
            self.backend.reset_prefix(self.pid)
            self._toast(tr('Prefix wird neu angelegt …'))
        except InstallError as exc:
            self._toast(str(exc))
        self._update_prefix_state()

    def _prefix_delete(self, *_: object) -> None:
        confirm_destructive(self, tr('Prefix löschen?'), tr('Der Prefix-Ordner wird gelöscht – inklusive Spielständen darin. Beim nächsten Start bzw. über „Prefix jetzt anlegen“ entsteht ein neuer.'), tr('Löschen'), self._do_delete)

    def _do_delete(self) -> None:
        try:
            p = self.backend.delete_prefix(self.pid)
            self._toast(tr('Prefix gelöscht') if p else tr('Kein Prefix vorhanden'))
        except InstallError as exc:
            self._toast(str(exc))
        self._update_prefix_state()

    # Seite 2: Startoptionen
    def _page_launch(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Startoptionen'), icon_name="utilities-terminal-symbolic", name="launch")
        grp = Adw.PreferencesGroup(title=tr('Startoptionen (Steam-Syntax)'),
                                   description=tr("Genau wie im Steam-Client: z. B. „mangohud %command%“, „ENABLE_VKBASALT=1 mangohud %command%“ "
                                                  "oder „gamescope -f -- %command% -novid“. GOverlay zeigt diese Zeile unter „Steam launch options“ an."))
        lib = self.backend.library_of(self.pid)
        title = tr('Startoptionen (leer = Vorgabe der Bibliothek: {0})').format(GLib.markup_escape_text(lib.launch_options)) \
            if lib is not None and lib.launch_options else tr('Startoptionen')
        self.opts_row = Adw.EntryRow(title=title, text=self.gs.launch_options, show_apply_button=True)
        self.opts_row.connect("apply", self._apply_opts)
        grp.add(self.opts_row)
        st = goverlay.status()
        imp = Adw.ButtonRow(title=tr('Aus GOverlay übernehmen'))
        imp.set_start_icon_name("document-import-symbolic")
        imp.connect("activated", self._import_goverlay)
        imp.set_sensitive(st.mangohud or st.goverlay)
        grp.add(imp)
        og = Adw.ButtonRow(title=tr('GOverlay öffnen (MangoHud konfigurieren)'))
        og.set_start_icon_name("preferences-system-symbolic")
        og.set_sensitive(st.goverlay)
        og.connect("activated", lambda *_: goverlay.open_goverlay())
        grp.add(og)
        preset_names = [tr('Vorlage wählen …')] + [n for n, _ in goverlay.PRESETS]
        self.preset_row = Adw.ComboRow(title=tr('Vorlage'), model=Gtk.StringList.new(preset_names))
        self.preset_row.connect("notify::selected", self._apply_preset)
        grp.add(self.preset_row)
        page.add(grp)

        tg = Adw.PreferencesGroup(title=GLib.markup_escape_text(tr('Overlays & Leistung (Schalter)')),
                                  description=tr('Werden zusätzlich zu den Startoptionen angewendet.'))
        self.sw_mango = Adw.SwitchRow(title="MangoHud", subtitle="FPS-Overlay" + ("" if st.mangohud else tr(' – nicht installiert')), active=self.gs.use_mangohud)
        self.sw_mango.set_sensitive(st.mangohud)
        self.sw_mango.connect("notify::active", lambda r, _: self._save(use_mangohud=r.get_active()))
        tg.add(self.sw_mango)
        self.sw_vk = Adw.SwitchRow(title="vkBasalt", subtitle="Post-Processing (ENABLE_VKBASALT=1)" + ("" if st.vkbasalt else tr(' – Layer nicht gefunden')), active=self.gs.use_vkbasalt)
        self.sw_vk.connect("notify::active", lambda r, _: self._save(use_vkbasalt=r.get_active()))
        tg.add(self.sw_vk)
        self.sw_gm = Adw.SwitchRow(title="GameMode", subtitle="gamemoderun" + ("" if st.gamemode else tr(' – nicht installiert')), active=self.gs.use_gamemode)
        self.sw_gm.set_sensitive(st.gamemode)
        self.sw_gm.connect("notify::active", lambda r, _: self._save(use_gamemode=r.get_active()))
        tg.add(self.sw_gm)
        self.sw_gs = Adw.SwitchRow(title=tr('Gamescope'), subtitle=tr('Micro-Compositor') + ("" if st.gamescope else tr(' – nicht installiert')), active=self.gs.use_gamescope)
        self.sw_gs.set_sensitive(st.gamescope)
        self.sw_gs.connect("notify::active", lambda r, _: self._save(use_gamescope=r.get_active()))
        tg.add(self.sw_gs)
        gsa = Adw.EntryRow(title=tr('Gamescope-Argumente'), text=self.gs.gamescope_args, show_apply_button=True)
        gsa.connect("apply", lambda r: self._save(gamescope_args=r.get_text()))
        tg.add(gsa)
        page.add(tg)

        pv = Adw.PreferencesGroup(title=tr('Resultierender Startbefehl'))
        self.preview = label("", css=["monospace-small"], wrap=True, selectable=True)
        self.preview.set_margin_top(6)
        self.preview.set_margin_bottom(6)
        self.preview.set_margin_start(8)
        self.preview.set_margin_end(8)
        pv.add(self.preview)
        page.add(pv)
        return page

    def _apply_opts(self, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        try:
            launch_options.parse(text)
        except launch_options.LaunchOptionsError as exc:
            self._toast(str(exc))
            return
        self._save(launch_options=text)
        self._toast(tr('Startoptionen gespeichert'))

    def _import_goverlay(self, *_: object) -> None:
        text = goverlay.import_from_goverlay()
        self.opts_row.set_text(text)
        self._save(launch_options=text)
        self._toast(tr('Aus GOverlay übernommen: {0}').format(text))

    def _apply_preset(self, row: Adw.ComboRow, _p) -> None:
        i = row.get_selected()
        if i <= 0 or self._building:
            return
        text = goverlay.PRESETS[i - 1][1]
        self.opts_row.set_text(text)
        self._save(launch_options=text)
        row.set_selected(0)

    def _refresh_preview(self) -> None:
        if not hasattr(self, "preview"):
            return
        try:
            if self.installed:
                plan = self.backend.launcher.preview_plan(self.pid)
                self.preview.set_text(plan.shell_preview())
                self.runner_row.set_subtitle({"umu": tr('umu-launcher (Proton im Steam-Linux-Runtime-Container)'),
                                              "proton": tr('Proton direkt'), "wine": tr('Wine'), "native": tr("nativ")}.get(plan.runner, plan.runner)
                                             + (f" · {plan.tool.name}" if plan.tool else ""))
            else:
                tool = self.backend.launcher.resolve_tool(self.gs, "windows", self.pid)
                self.preview.set_text(launch_options.describe(self.gs.launch_options, ["<spiel>"]))
                self.runner_row.set_subtitle(f"{self.backend.launcher.runner_for(tool)} · {tool.name}" if tool else "–")
        except (LaunchError, launch_options.LaunchOptionsError) as exc:
            self.preview.set_text(str(exc))

    # Seite 3: Umgebung
    def _page_env(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Umgebung'), icon_name="preferences-other-symbolic", name="env")
        self.env_group = Adw.PreferencesGroup(title=tr('Umgebungsvariablen'),
                                              description=tr("Zusätzlich zu VAR=wert in den Startoptionen; z. B. DXVK_ASYNC=1, PROTON_USE_WINED3D=1."))
        self._env_rows: list[Gtk.Widget] = []
        page.add(self.env_group)
        add = Adw.PreferencesGroup(title=tr('Variable hinzufügen'))
        self.env_key = Adw.EntryRow(title=tr('Name'))
        self.env_val = Adw.EntryRow(title=tr('Wert'))
        add.add(self.env_key)
        add.add(self.env_val)
        btn = Adw.ButtonRow(title=tr('Hinzufügen'))
        btn.set_start_icon_name("list-add-symbolic")
        btn.connect("activated", self._env_add)
        add.add(btn)
        page.add(add)
        self._rebuild_env()
        return page

    def _rebuild_env(self) -> None:
        for r in self._env_rows:
            self.env_group.remove(r)
        self._env_rows.clear()
        if not self.gs.env:
            r = Adw.ActionRow(title=tr('Keine Variablen gesetzt'))
            self.env_group.add(r)
            self._env_rows.append(r)
            return
        for k, v in sorted(self.gs.env.items()):
            r = Adw.ActionRow(title=k, subtitle=v)
            d = icon_button("user-trash-symbolic", tr('Entfernen'), ["flat"])
            d.connect("clicked", lambda _b, key=k: self._env_remove(key))
            r.add_suffix(d)
            self.env_group.add(r)
            self._env_rows.append(r)

    def _env_add(self, *_: object) -> None:
        k = self.env_key.get_text().strip()
        v = self.env_val.get_text()
        if not k or not k.replace("_", "a").isalnum():
            self._toast(tr('Ungültiger Variablenname'))
            return
        env = dict(self.gs.env)
        env[k] = v
        self._save(env=env)
        self.env_key.set_text("")
        self.env_val.set_text("")
        self._rebuild_env()

    def _env_remove(self, key: str) -> None:
        env = dict(self.gs.env)
        env.pop(key, None)
        self._save(env=env)
        self._rebuild_env()

    # Seite: Spielfunktionen (Galaxy: Konfigurieren → Funktionen)
    def _page_features(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Spielfunktionen'), icon_name="applications-games-symbolic", name="features")
        grp = Adw.PreferencesGroup(title=tr('Funktionen'),
                                   description=tr('Abweichend von „Einstellungen → Spielfunktionen“ nur für dieses Spiel.'))
        gog = self.installed is not None and self.installed.method != "external" and not self.pid.startswith("ext-")
        s = self.backend.settings
        modes = ["", "on", "off"]
        for key, title, glob in (("cloud_saves", tr('Lokale Speicherstände mit der GOG Galaxy-Cloud synchronisieren'), s.feature_cloud_saves),
                                 ("galaxy_services", tr('GOG Galaxy-Funktionen im Spiel nutzen'), s.feature_galaxy_services)):
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(
                [tr('Globale Vorgabe ({0})').format(tr('an') if glob else tr('aus')), tr('An'), tr('Aus')]))
            cur = getattr(self.gs, key)
            row.set_selected(modes.index(cur) if cur in modes else 0)
            row.set_sensitive(gog)
            row.connect("notify::selected", lambda r, _, k=key: self._save(**{k: modes[r.get_selected()]}))
            grp.add(row)
        page.add(grp)
        return page

    # Seite: Erweitert
    def _page_advanced(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Erweitert'), icon_name="applications-engineering-symbolic", name="advanced")
        grp = Adw.PreferencesGroup(title=tr('Start'))
        is_ext = bool(self.installed and self.installed.method == "external")
        if is_ext:
            name = Adw.EntryRow(title=tr('Name in der Bibliothek'), text=self.installed.title, show_apply_button=True)
            name.connect("apply", lambda r: (self.backend.rename_external(self.pid, r.get_text()), self.on_changed(),
                                             self._toast(tr("Name geändert – Cover wird neu gesucht"))))
            grp.add(name)
        tasks = self.backend.launcher.tasks_for(self.installed) if self.installed else []
        names = [tr('Standard (primärer Eintrag)')] + [t.name for t in tasks]
        self.task_row = Adw.ComboRow(title=tr('Startdatei'), subtitle=tr('erkannte .exe-Dateien im Spielordner') if is_ext else tr('aus goggame-*.info (playTasks)'),
                                     model=Gtk.StringList.new(names))
        tnames = [t.name for t in tasks]
        self.task_row.set_selected(tnames.index(self.gs.play_task) + 1 if self.gs.play_task in tnames else 0)

        def task_changed(*_: object) -> None:
            i = self.task_row.get_selected()
            chosen = "" if i == 0 else tnames[i - 1]
            self._save(play_task=chosen)
            if is_ext and chosen:
                self.backend.set_external_exe(self.pid, chosen)
        self.task_row.connect("notify::selected", task_changed)
        grp.add(self.task_row)
        if is_ext:
            pick = Adw.ButtonRow(title=tr('Andere .exe wählen …'))
            pick.set_start_icon_name("document-open-symbolic")
            pick.connect("activated", self._pick_exe)
            grp.add(pick)
        pre = Adw.EntryRow(title=tr("Befehl vor dem Start (sh -c)"), text=self.gs.pre_launch, show_apply_button=True)
        pre.connect("apply", lambda r: self._save(pre_launch=r.get_text()))
        grp.add(pre)
        post = Adw.EntryRow(title=tr("Befehl nach dem Beenden (sh -c)"), text=self.gs.post_exit, show_apply_button=True)
        post.connect("apply", lambda r: self._save(post_exit=r.get_text()))
        grp.add(post)
        page.add(grp)

        rg = Adw.PreferencesGroup(title=tr('Redistributables (DirectX, Visual C++, .NET …)'))
        rec = read_json(paths.MANIFEST_DIR / f"{self.pid}.json", {})
        deps = ", ".join(rec.get("dependencies") or []) or tr('keine laut Manifest')
        sw = Adw.SwitchRow(title=tr('Beim ersten Start im Prefix installieren'), subtitle=deps, active=self.gs.install_redist)
        sw.connect("notify::active", lambda r, _: self._save(install_redist=r.get_active()))
        rg.add(sw)
        again = Adw.ButtonRow(title=tr('Beim nächsten Start erneut installieren'))
        again.connect("activated", lambda *_: (self._save(redist_done=[]), self._toast(tr("Redistributables werden beim nächsten Start neu installiert"))))
        rg.add(again)
        page.add(rg)

        sg = Adw.PreferencesGroup(title=tr('Verknüpfungen'), description=tr('Starten das Spiel über den Client (Spielzeit, Proton-Einstellungen).'))
        has_menu, has_desktop = self.backend.shortcut_state(self.pid)
        self.sw_menu = Adw.SwitchRow(title=tr('Im Startmenü'), active=has_menu)
        self.sw_menu.set_sensitive(self.installed is not None)
        self.sw_menu.connect("notify::active", lambda r, _: self._toggle_shortcut("menu", r.get_active()))
        sg.add(self.sw_menu)
        self.sw_desktop = Adw.SwitchRow(title=tr('Auf dem Schreibtisch'), active=has_desktop)
        self.sw_desktop.set_sensitive(self.installed is not None)
        self.sw_desktop.connect("notify::active", lambda r, _: self._toggle_shortcut("desktop", r.get_active()))
        sg.add(self.sw_desktop)
        page.add(sg)

        tg = Adw.PreferencesGroup(title=tr('Werkzeuge'))
        mv = Adw.ButtonRow(title=tr('Installation verschieben …'))
        mv.set_start_icon_name("folder-symbolic")
        mv.set_sensitive(self.installed is not None)
        mv.connect("activated", lambda *_: move_installation(self.get_root(), self.backend, self.pid))
        tg.add(mv)
        for title, icon, cb in (
            (tr('Winetricks für dieses Prefix öffnen'), "applications-system-symbolic", self._winetricks),
            (tr('Prefix-Ordner öffnen'), "folder-symbolic", lambda *_: self.backend.launcher.open_path(self.backend.launcher.prefix_for(self.pid))),
            (tr('Installationsordner öffnen'), "folder-open-symbolic", lambda *_: self.installed and self.backend.launcher.open_path(self.installed.install_path)),
            (tr('Protokoll öffnen'), "text-x-generic-symbolic", lambda *_: self.backend.launcher.open_path(paths.game_log_file(self.pid))),
        ):
            r = Adw.ButtonRow(title=title)
            r.set_start_icon_name(icon)
            r.connect("activated", cb)
            tg.add(r)
        page.add(tg)

        og = Adw.PreferencesGroup(title=tr('Bibliothek'))
        fav = Adw.SwitchRow(title=tr('Favorit'), active=self.gs.favorite)
        fav.connect("notify::active", lambda r, _: self._save(favorite=r.get_active()))
        og.add(fav)
        hid = Adw.SwitchRow(title=tr('In der Bibliothek verstecken'), active=self.gs.hidden)
        hid.connect("notify::active", lambda r, _: self._save(hidden=r.get_active()))
        og.add(hid)
        page.add(og)
        return page

    def _pick_exe(self, *_: object) -> None:
        base = Path(self.installed.install_path)
        dlg = Gtk.FileDialog(title=tr('Startdatei wählen'), modal=True)
        flt = Gtk.FileFilter()
        flt.set_name("Windows-Programme (*.exe)")
        flt.add_pattern("*.exe")
        flt.add_pattern("*.EXE")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        dlg.set_initial_folder(Gio.File.new_for_path(str(base)))

        def done(d, res) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            try:
                rel = str(Path(f.get_path()).relative_to(base))
            except ValueError:
                self._toast(tr('Die Datei muss im Spielordner liegen'))
                return
            self.backend.set_external_exe(self.pid, rel)
            self._save(play_task=rel)
            self._toast(tr('Startdatei: {0}').format(rel))
        dlg.open(self.get_root(), None, done)

    def _winetricks(self, *_: object) -> None:
        try:
            self.backend.launcher.run_winetricks(self.pid)
        except LaunchError as exc:
            self._toast(str(exc))

    def _toggle_shortcut(self, which: str, on: bool) -> None:
        if self._building:
            return
        pid = self.pid
        menu, desktop = which == "menu", which == "desktop"

        def done(_r) -> None:
            self._toast((tr('Verknüpfung angelegt') if on else tr('Verknüpfung entfernt')) + (tr(' (Startmenü)') if menu else tr(' (Schreibtisch)')))
        if on:
            run_async(lambda: self.backend.create_shortcuts(pid, menu=menu, desktop=desktop), done, lambda e: self._toast(str(e)))
        else:
            run_async(lambda: self.backend.remove_shortcuts(pid, menu=menu, desktop=desktop), done, lambda e: self._toast(str(e)))


# ----------------------------------------------------------------------------- Globale Einstellungen
class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, backend, on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(title=tr('Einstellungen'), content_width=720, content_height=640)
        self.backend = backend
        self.s = backend.settings
        self.on_changed = on_changed or (lambda: None)
        self._lib_ext_groups: list[Adw.PreferencesGroup] = []
        prefs_registry.load_extensions()
        for page in (self._page_general(), self._page_libraries(), self._page_compat(), self._page_features()):
            groups = prefs_registry.build_groups(page.get_name(), self)
            for grp in groups:
                page.add(grp)
            if page.get_name() == "libraries":
                self._lib_ext_groups = groups
            self.add(page)

    def _save(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.s, k, v)
        self.backend.save_settings()
        self.on_changed()

    def _page_general(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Allgemein'), icon_name="preferences-system-symbolic", name="general")
        ui = Adw.PreferencesGroup(title=tr('Oberfläche'))
        codes = list(UI_LANGUAGES)
        ui_lang = Adw.ComboRow(title=tr('Sprache der Oberfläche'), subtitle=tr('wirkt nach einem Neustart des Clients'),
                               model=Gtk.StringList.new([tr(UI_LANGUAGES[c]) for c in codes]))
        ui_lang.set_selected(codes.index(self.s.ui_language) if self.s.ui_language in codes else 0)
        ui_lang.connect("notify::selected", lambda *_: (self._save(ui_language=codes[ui_lang.get_selected()]),
                                                       self.add_toast(Adw.Toast.new(tr('Sprache wird nach einem Neustart des Clients übernommen')))))
        ui.add(ui_lang)
        ui.add(store.preferred_store_row(self))     # GOG STORE in der Seitenleiste zeigen/ausblenden
        page.add(ui)
        grp = Adw.PreferencesGroup(title=tr('Ordner'))
        d = Adw.EntryRow(title=tr('Spiele-Ordner'), text=self.s.install_dir, show_apply_button=True)
        d.connect("apply", lambda r: self._save(install_dir=r.get_text().strip() or str(paths.DEFAULT_GAMES_DIR)))
        b = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
        b.connect("clicked", lambda *_: choose_folder(self.get_root(), tr("Spiele-Ordner"), self.s.install_dir, lambda p: (d.set_text(p), self._save(install_dir=p))))
        d.add_suffix(b)
        grp.add(d)
        self.loc_group = Adw.PreferencesGroup(title=tr('Weitere Installationsorte'),
                                              description=tr('Erscheinen im Installations-Dialog zur Auswahl; Standard ist der Spiele-Ordner oben.'))
        self._loc_rows: list[Gtk.Widget] = []
        self._rebuild_loc_rows()
        addb = Adw.ButtonRow(title=tr('Installationsort hinzufügen …'))
        addb.set_start_icon_name("list-add-symbolic")
        addb.connect("activated", lambda *_: choose_folder(self.get_root(), tr("Installationsort hinzufügen"), self.s.install_dir, self._add_location))
        self.loc_group.add(addb)
        p = Adw.EntryRow(title=tr('Prefix-Ordner (ein Prefix pro Spiel)'), text=self.s.prefix_dir, show_apply_button=True)
        p.connect("apply", lambda r: self._save(prefix_dir=r.get_text().strip() or str(paths.DEFAULT_PREFIX_DIR)))
        b2 = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
        b2.connect("clicked", lambda *_: choose_folder(self.get_root(), tr("Prefix-Ordner"), self.s.prefix_dir, lambda x: (p.set_text(x), self._save(prefix_dir=x))))
        p.add_suffix(b2)
        grp.add(p)
        page.add(grp)
        page.add(self.loc_group)

        self.ign_group = Adw.PreferencesGroup(title=tr('Ausgeschlossene Spielordner'),
                                              description=tr('Von Hand aus der Bibliothek entfernt – die Ordnersuche nimmt sie nicht wieder auf. Entfernen aus dieser Liste = beim nächsten Durchsuchen wieder aufnehmen.'))
        self._ign_rows: list[Gtk.Widget] = []
        self._rebuild_ignored()
        page.add(self.ign_group)

        dl = Adw.PreferencesGroup(title=tr('Downloads und Bibliothek'))
        lang = Adw.ComboRow(title=tr('Bevorzugte Spielsprache'), model=Gtk.StringList.new([n for _, n in LANGUAGES]))
        lang.set_selected(language_index(self.s.language))
        lang.connect("notify::selected", lambda *_: self._save(language=LANGUAGES[lang.get_selected()][0]))
        dl.add(lang)
        w = Adw.SpinRow.new_with_range(1, 16, 1)
        w.set_title(tr('Parallele Verbindungen'))
        w.set_value(self.s.max_workers)
        w.connect("notify::value", lambda *_: self._save(max_workers=int(w.get_value())))
        dl.add(w)
        for key, title, sub in (("watch_install_dirs", tr('Kopierte Spiele in den Spiele-Ordnern erkennen'), tr('neue Spielordner im GOG-Spiele-Ordner und in den Bibliotheken automatisch übernehmen')),
                                ("auto_create_prefixes", tr('Fehlende Wine-Prefixe automatisch anlegen'), tr('im Prefix-Ordner bzw. dem der Bibliothek, mit passender Bildschirm-Skalierung')),
                                ("shortcut_menu", tr('Nach der Installation Verknüpfung im Startmenü anlegen'), tr('Vorgabe für den Installations-Dialog')),
                                ("shortcut_desktop", tr('Nach der Installation Verknüpfung auf dem Schreibtisch anlegen'), tr('Vorgabe für den Installations-Dialog')),
                                ("auto_check_updates", tr('Beim Start nach Spiel-Updates suchen'), ""),
                                ("show_hidden", tr('Versteckte Spiele anzeigen'), ""),
                                ("use_keyring", tr('Zugangsdaten im System-Schlüsselbund speichern'), tr('statt in ~/.config/galaxy-linux/credentials.json (0600); wirkt beim nächsten Login'))):
            r = Adw.SwitchRow(title=title, subtitle=sub, active=getattr(self.s, key))
            r.connect("notify::active", lambda row, _, k=key: self._save(**{k: row.get_active()}))
            dl.add(r)
        page.add(dl)
        return page

    def _rebuild_ignored(self) -> None:
        for r in self._ign_rows:
            self.ign_group.remove(r)
        self._ign_rows.clear()
        if not self.s.ignored_paths:
            r = Adw.ActionRow(title=tr('Keine'))
            self.ign_group.add(r)
            self._ign_rows.append(r)
            return
        for pth in list(self.s.ignored_paths):
            r = Adw.ActionRow(title=Path(pth).name, subtitle=pth)
            b = icon_button("edit-undo-symbolic", tr('Wieder aufnehmen'), ["flat"])
            b.connect("clicked", lambda _b, x=pth: self._unignore(x))
            r.add_suffix(b)
            self.ign_group.add(r)
            self._ign_rows.append(r)

    def _unignore(self, pth: str) -> None:
        self.s.ignored_paths = [x for x in self.s.ignored_paths if x != pth]
        self.backend.save_settings()
        self._rebuild_ignored()
        self.on_changed()
        self.add_toast(Adw.Toast.new(tr('Wird beim nächsten Durchsuchen wieder aufgenommen')))

    def _rebuild_loc_rows(self) -> None:
        for r in self._loc_rows:
            self.loc_group.remove(r)
        self._loc_rows.clear()
        if not self.s.install_locations:
            r = Adw.ActionRow(title=tr('Keine weiteren Orte'))
            self.loc_group.add(r)
            self._loc_rows.append(r)
            return
        for loc in list(self.s.install_locations):
            r = Adw.ActionRow(title=loc)
            d = icon_button("user-trash-symbolic", tr('Entfernen'), ["flat"])
            d.connect("clicked", lambda _b, l=loc: self._remove_location(l))
            r.add_suffix(d)
            self.loc_group.add(r)
            self._loc_rows.append(r)

    def _add_location(self, path: str) -> None:
        self.s.remember_location(path)
        self.on_changed()
        # Zeilen neu aufbauen: Gruppe enthält zuletzt die Hinzufügen-Zeile, daher Orte davor einfügen
        self._rebuild_loc_rows()

    def _remove_location(self, path: str) -> None:
        self.s.install_locations = [l for l in self.s.install_locations if l != path]
        self.backend.save_settings()
        self._rebuild_loc_rows()

    # Seite: eigene Bibliotheken
    def _page_libraries(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Bibliotheken'), icon_name="folder-symbolic", name="libraries")
        self.lib_page = page
        self._lib_groups: list[Adw.PreferencesGroup] = []
        top = Adw.PreferencesGroup(title=tr('Eigene Bibliotheken'),
                                   description=tr('Für Spiele, die nicht von GOG stammen: Jeder Unterordner des Spiele-Ordners wird ein Spiel mit eigenem Prefix im Prefix-Ordner. Jede Bibliothek erscheint in der Seitenleiste unter BIBLIOTHEKEN. Die Vorgaben gelten für ihre Spiele, solange ein Spiel nichts Eigenes eingestellt hat.'))
        add = Adw.ButtonRow(title=tr('Bibliothek anlegen …'))
        add.set_start_icon_name("list-add-symbolic")
        add.connect("activated", lambda *_: LibraryDialog(self.backend, lambda lib: (self._rebuild_libraries(), self.on_changed(),
                                                                                       self.add_toast(Adw.Toast.new(tr("„{0}“ angelegt").format(lib.name)))))
                    .present(self))
        top.add(add)
        page.add(top)
        self._rebuild_libraries()
        return page

    def _rebuild_libraries(self) -> None:
        for g in self._lib_groups + self._lib_ext_groups:
            self.lib_page.remove(g)
        self._lib_groups.clear()
        for lib in self.backend.libraries():
            g = self._library_group(lib)
            self.lib_page.add(g)
            self._lib_groups.append(g)
        for g in self._lib_ext_groups:      # Gruppen der Erweiterungen bleiben unter den Bibliotheken
            self.lib_page.add(g)

    def _lib_save(self, lib_id: str, toast: str = "", **changes) -> bool:
        try:
            self.backend.update_library(lib_id, **changes)
        except (InstallError, launch_options.LaunchOptionsError) as exc:
            self.add_toast(Adw.Toast.new(str(exc)))
            return False
        if toast:
            self.add_toast(Adw.Toast.new(toast))
        self.on_changed()
        return True

    def _library_group(self, lib) -> Adw.PreferencesGroup:
        n = len(self.backend.library_games(lib.id))
        grp = Adw.PreferencesGroup(title=GLib.markup_escape_text(lib.name), description=(tr('{0} Spiel') if n == 1 else tr('{0} Spiele')).format(n))
        trash = icon_button("user-trash-symbolic", tr('Bibliothek löschen …'), ["flat"])
        trash.set_valign(Gtk.Align.CENTER)
        trash.connect("clicked", lambda *_: self._delete_library(lib.id))
        grp.set_header_suffix(trash)
        name = Adw.EntryRow(title=tr('Name'), text=lib.name, show_apply_button=True)
        name.connect("apply", lambda r: grp.set_title(GLib.markup_escape_text(lib.name)) if self._lib_save(lib.id, tr('Umbenannt'), name=r.get_text())
                     else r.set_text(lib.name))
        grp.add(name)
        for key, title, sub in (("games_dir", tr('Spiele-Ordner'), tr('jeder Unterordner ist ein Spiel')),
                                ("prefix_dir", tr('Prefix-Ordner'), tr('ein Prefix je Spiel, benannt wie der Spielordner – vorhandene Prefixe werden nicht verschoben'))):
            row = Adw.EntryRow(title=title, text=getattr(lib, key), show_apply_button=True)
            row.set_tooltip_text(sub)
            row.connect("apply", lambda r, k=key: (self._lib_save(lib.id, tr("Ordner gespeichert"), **{k: r.get_text()}),
                                                   r.set_text(getattr(lib, k))))
            fb = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
            fb.connect("clicked", lambda _b, r=row, k=key, t=title: choose_folder(
                self.get_root(), f"{t} – {lib.name}", getattr(lib, k),
                lambda x, r=r, k=k: self._lib_save(lib.id, tr("Ordner gespeichert"), **{k: x}) and r.set_text(x)))
            row.add_suffix(fb)
            ob = icon_button("external-link-symbolic", tr('Im Dateimanager öffnen'), ["flat"])
            ob.connect("clicked", lambda _b, k=key: self.backend.launcher.open_path(getattr(lib, k)))
            row.add_suffix(ob)
            grp.add(row)

        tools = self.backend.launcher.tools()
        glob = self.backend.launcher.global_default_tool()
        ids = [""] + [t.id for t in tools]
        names = [tr('Globale Vorgabe ({0})').format(glob.name if glob else tr('keine gefunden'))] + [t.label for t in tools]
        if lib.compat_tool and lib.compat_tool not in ids:
            ids.append(lib.compat_tool)
            names.append(tr('{0} (nicht gefunden)').format(lib.compat_tool))
        tool = Adw.ComboRow(title=tr('Kompatibilitätstool'), subtitle=tr('Vorgabe für die Spiele dieser Bibliothek'),
                            model=Gtk.StringList.new(names))
        tool.set_selected(ids.index(lib.compat_tool) if lib.compat_tool in ids else 0)
        tool.connect("notify::selected", lambda r, _: self._lib_save(lib.id, compat_tool=ids[r.get_selected()]))
        grp.add(tool)
        modes = ["", "on", "off"]
        wl = Adw.ComboRow(title=tr('Wayland-Treiber von Proton'),
                          model=Gtk.StringList.new([tr('Globale Vorgabe ({0})').format(tr('an') if self.s.wayland_driver else tr('aus')), tr('An'), tr('Aus (XWayland)')]))
        wl.set_selected(modes.index(lib.wayland_driver) if lib.wayland_driver in modes else 0)
        wl.connect("notify::selected", lambda r, _: self._lib_save(lib.id, wayland_driver=modes[r.get_selected()]))
        grp.add(wl)
        from ..launch.display import desktop_dpi
        dpi = Adw.SpinRow.new_with_range(0, 480, 12)
        dpi.set_title(tr('DPI für neue Prefixe'))
        dpi.set_subtitle(tr('0 = globale Vorgabe ({0})').format(self.s.prefix_dpi or tr('automatisch, derzeit {0}').format(desktop_dpi())))
        dpi.set_value(lib.prefix_dpi)
        dpi.connect("notify::value", lambda r, _: self._lib_save(lib.id, prefix_dpi=int(r.get_value())))
        grp.add(dpi)
        opts = Adw.EntryRow(title=tr('Startoptionen (Vorgabe, Steam-Syntax)'), text=lib.launch_options, show_apply_button=True)
        opts.connect("apply", lambda r: self._lib_save(lib.id, tr('Startoptionen gespeichert'), launch_options=r.get_text()))
        grp.add(opts)
        watch = Adw.SwitchRow(title=tr('Ordner überwachen'), subtitle=tr('kopierte Spiele nach dem Kopieren automatisch übernehmen'),
                              active=lib.watch)
        watch.connect("notify::active", lambda r, _: self._lib_save(lib.id, watch=r.get_active()))
        grp.add(watch)
        return grp

    def _delete_library(self, lib_id: str) -> None:
        lib = self.backend.get_library(lib_id)
        if lib is None:
            return

        def do() -> None:
            try:
                games = self.backend.remove_library(lib_id)
            except InstallError as exc:
                self.add_toast(Adw.Toast.new(str(exc)))
                return
            self._rebuild_libraries()
            self.on_changed()
            self.add_toast(Adw.Toast.new(tr('„{0}“ gelöscht – {1} Spiel(e) ausgetragen, Dateien bleiben').format(lib.name, len(games))))
        confirm_delete_library(self, lib, len(self.backend.library_games(lib_id)), do)

    def _page_compat(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Kompatibilität'), icon_name="application-x-executable-symbolic", name="compat")
        tools = self.backend.launcher.tools(refresh=True)
        grp = Adw.PreferencesGroup(title=tr('Standard-Kompatibilitätstool'), description=tr('Gilt für alle Windows-Spiele ohne eigene Auswahl.'))
        ids = ["auto"] + [t.id for t in tools]
        auto = compat.default_tool(tools, self.s.prefer_umu)
        names = [tr('Automatisch ({0})').format(auto.name if auto else tr('nichts gefunden'))] + [t.label for t in tools]
        row = Adw.ComboRow(title=tr("Proton-/Wine-Version"), model=Gtk.StringList.new(names))
        row.set_selected(ids.index(self.s.default_compat_tool) if self.s.default_compat_tool in ids else 0)
        row.connect("notify::selected", lambda *_: self._save(default_compat_tool=ids[row.get_selected()]))
        grp.add(row)
        umu = Adw.SwitchRow(title=tr('umu-launcher bevorzugen'), subtitle=tr('Proton im Steam-Linux-Runtime-Container mit protonfixes (empfohlen; für proton-cachyos-slr Pflicht)')
                            + ("" if compat.umu_available() else tr(' – umu-run nicht gefunden')), active=self.s.prefer_umu)
        umu.connect("notify::active", lambda r, _: self._save(prefer_umu=r.get_active()))
        grp.add(umu)
        wl = Adw.SwitchRow(title=tr('Wayland-Treiber von Proton als Vorgabe'),
                           subtitle=tr("Spiele laufen direkt unter Wayland statt XWayland (PROTON_ENABLE_WAYLAND=1); "
                                       "pro Spiel unter „Eigenschaften → Kompatibilität“ abschaltbar"),
                           active=self.s.wayland_driver)
        wl.connect("notify::active", lambda r, _: self._save(wayland_driver=r.get_active()))
        grp.add(wl)
        red = Adw.SwitchRow(title=tr('Redistributables mitladen und beim ersten Start installieren'), active=self.s.install_redist)
        red.connect("notify::active", lambda r, _: self._save(install_redist=r.get_active()))
        grp.add(red)
        from ..launch.display import desktop_dpi
        dpi = Adw.SpinRow.new_with_range(0, 480, 12)
        dpi.set_title(tr('DPI für neue Prefixe'))
        dpi.set_subtitle(tr('0 = automatisch aus der Desktop-Skalierung (derzeit {0}), 96 = unverändert').format(desktop_dpi()))
        dpi.set_value(self.s.prefix_dpi)
        dpi.connect("notify::value", lambda r, _: self._save(prefix_dpi=int(r.get_value())))
        grp.add(dpi)
        opts = Adw.EntryRow(title=tr('Globale Startoptionen (für alle Spiele)'), text=self.s.default_launch_options, show_apply_button=True)
        opts.connect("apply", lambda r: self._save(default_launch_options=r.get_text().strip()))
        grp.add(opts)
        page.add(grp)

        st = goverlay.status()
        info = Adw.PreferencesGroup(title=tr('Gefundene Werkzeuge'))
        for t in tools:
            info.add(Adw.ActionRow(title=t.name, subtitle=f"{t.kind} · {t.source} · {t.path}" + (tr(' · benötigt Runtime {0} (→ umu)').format(t.requires_runtime) if t.requires_runtime else "")))
        for name, ok in (("umu-launcher", compat.umu_available()), ("GOverlay", st.goverlay), ("MangoHud", st.mangohud),
                         (tr('MangoHud (32-Bit)'), st.mangohud32), ("vkBasalt", st.vkbasalt), ("GameMode", st.gamemode),
                         (tr('Gamescope'), st.gamescope), (tr('MangoHud-Konfiguration (GOverlay)'), st.mangohud_config)):
            info.add(Adw.ActionRow(title=name, subtitle=tr('gefunden') if ok else tr('nicht gefunden')))
        page.add(info)
        return page

    # Seite: Spielfunktionen (wie Galaxy 2.1: Einstellungen → Spielfunktionen → Globale Einstellungen)
    def _page_features(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr('Spielfunktionen'), icon_name="applications-games-symbolic", name="features")
        sections = (
            (tr('Allgemeine Funktionen zum Spiel'), tr('Globale Einstellungen – einzelne Spiele lassen sich unter „Eigenschaften → Spielfunktionen“ abweichend einstellen.'), (
                ("feature_auto_update", tr('Spiele automatisch aktualisieren'),
                 tr('Hiermit werden deine GOG-Spiele automatisch im Hintergrund aktualisiert und du musst Updates nicht selber ausführen.')),
                ("feature_cloud_saves", tr('Cloud-Spielstände'),
                 tr('Cloud-Spielstände lädt deine Spielstände herunter, wenn du ein unterstütztes GOG-Spiel installierst. Außerdem werden die Spielstände vor und nach jeder Session synchronisiert.')))),
            (tr('Funktionen im Spiel'), "", (
                ("feature_achievements", tr('Erfolge'),
                 tr('Erfolge werden bei unterstützten Spielen im Spiel freigeschaltet, hochgeladen und in galaxy-linux angezeigt.')),
                ("feature_galaxy_services", tr('Galaxy-Dienste im Spiel (comet)'),
                 tr('Erfolge, Statistiken und Bestenlisten für Spiele mit GOG Galaxy-SDK – comet ersetzt den Galaxy-Dienst im Prefix.')),
                ("sync_playtime", tr('Erfassung der Spielzeit'),
                 tr('Spielzeit nach jeder Sitzung an GOG übertragen – erscheint im GOG-Profil und in GOG Galaxy.')),
                ("goverlay_use_profiles", tr('GOverlay-Profile pro Spiel'),
                 tr('Spiele mit ihrem eigenen GOverlay-Profil (MangoHud, vkBasalt …) starten statt mit der globalen Konfiguration.')))),
        )
        for title, desc, rows in sections:
            grp = Adw.PreferencesGroup(title=title, description=desc)
            for key, row_title, sub in rows:
                r = Adw.SwitchRow(title=row_title, subtitle=sub, active=getattr(self.s, key))
                r.connect("notify::active", lambda row, _, k=key: self._save(**{k: row.get_active()}))
                grp.add(r)
            page.add(grp)
        return page


# ----------------------------------------------------------------------------- Verschieben
def move_installation(parent, backend, product_id: str, on_started: Callable[[], None] | None = None) -> None:
    """Ordner wählen → Bestätigung mit Zielpfad → Verschiebe-Job (wie „Installation verschieben“ im Windows-Client)."""
    inst = backend.registry.get(product_id)
    if inst is None:
        return
    src = Path(inst.install_path)

    def chosen(root: str) -> None:
        dst = Path(root) / src.name
        dlg = Adw.AlertDialog.new(tr('Installation verschieben?'), tr('{0}\n\nvon: {1}\nnach: {2}\n\nAuf demselben Laufwerk geht das sofort, sonst werden die Dateien kopiert.').format(inst.title, src, dst))
        dlg.add_response("cancel", tr('Abbrechen'))
        dlg.add_response("move", tr('Verschieben'))
        dlg.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("move")

        def resp(_d, r: str) -> None:
            if r == "move":
                backend.move(product_id, root)
                if on_started:
                    on_started()
        dlg.connect("response", resp)
        dlg.present(parent)
    choose_folder(parent, tr('Neuer Basisordner für die Installation'), str(src.parent), chosen)


# ----------------------------------------------------------------------------- Spiele und Prefixe von Hand verwalten
def confirm_destructive(parent, heading: str, body: str, action: str, callback: Callable[[], None]) -> None:
    dlg = Adw.AlertDialog.new(heading, body)
    dlg.add_response("cancel", tr('Abbrechen'))
    dlg.add_response("ok", action)
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_default_response("cancel")
    dlg.connect("response", lambda d, r: callback() if r == "ok" else None)
    dlg.present(parent)


class AddGameDialog(Adw.Dialog):
    """Ein Spiel aus einem beliebigen Ordner in die Bibliothek aufnehmen (GOG oder extern)."""

    def __init__(self, backend, info: dict, on_added: Callable[[object], None], library: str | None = None) -> None:
        super().__init__(title=tr('Spiel hinzufügen'), content_width=620)
        self.backend, self.info, self.on_added = backend, info, on_added
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label=tr('Hinzufügen'))
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._add)
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup()
        kind = tr('GOG-Spiel (goggame-Daten gefunden)') if info["kind"] == "gog" else tr('Externes Spiel (nicht von GOG)')
        grp.add(Adw.ActionRow(title=tr('Art'), subtitle=kind))
        grp.add(Adw.ActionRow(title=tr('Ordner'), subtitle=info["path"]))
        self.title_row = Adw.EntryRow(title=tr('Name in der Bibliothek'), text=info["title"])
        self.title_row.set_sensitive(info["kind"] != "gog")
        grp.add(self.title_row)
        self.cands = info.get("candidates") or []
        self.libs = backend.libraries()
        if info["kind"] != "gog":
            self.exe_row = Adw.ComboRow(title=tr('Startdatei'), subtitle=tr('automatisch erkannt – bei Bedarf ändern'),
                                        model=Gtk.StringList.new(self.cands or [tr('keine .exe gefunden')]))
            grp.add(self.exe_row)
            # Vorschlag: die Bibliothek, aus der „Spiel hinzufügen“ kam, sonst die, in deren Ordner das Spiel liegt
            self.lib_row = Adw.ComboRow(title=tr('Bibliothek'), subtitle=tr('Prefix kommt in den Prefix-Ordner dieser Bibliothek'),
                                        model=Gtk.StringList.new([lib.name for lib in self.libs] or [tr('keine – bitte zuerst anlegen')]))
            ids = [lib.id for lib in self.libs]
            want = info.get("library") or library or ""
            if want in ids:
                self.lib_row.set_selected(ids.index(want))
            grp.add(self.lib_row)
            if not self.cands or not self.libs:
                self.ok.set_sensitive(False)
        self.prefix_row = Adw.SwitchRow(title=tr('Prefix gleich anlegen'), active=True,
                                        subtitle=tr('im Prefix-Ordner der Bibliothek') if info["kind"] != "gog" else tr('im Prefix-Ordner'))
        grp.add(self.prefix_row)
        self.error = label("", css=["error"], wrap=True)
        grp.add(self.error)
        page.add(grp)
        tv.set_content(page)
        self.set_child(tv)

    def _add(self, *_: object) -> None:
        exe = self.cands[self.exe_row.get_selected()] if self.info["kind"] != "gog" and self.cands else ""
        lib = self.libs[self.lib_row.get_selected()].id if self.info["kind"] != "gog" and self.libs else ""
        title = self.title_row.get_text()
        create = self.prefix_row.get_active()
        self.ok.set_sensitive(False)

        def done(game) -> None:
            self.close()
            self.on_added(game)

        def fail(exc: Exception) -> None:
            self.ok.set_sensitive(True)
            self.error.set_text(str(exc))
        run_async(lambda: self.backend.add_game_manually(self.info["path"], title=title, exe=exe, create_prefix=create, library=lib),
                  done, fail)


class LibraryDialog(Adw.Dialog):
    """Neue eigene Bibliothek: Name, Spiele-Ordner, Prefix-Ordner – „Anlegen“ legt sie an und durchsucht den Ordner."""

    def __init__(self, backend, on_created: Callable[[object], None]) -> None:
        super().__init__(title=tr('Bibliothek anlegen'), content_width=620)
        self.backend, self.on_created = backend, on_created
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label=tr('Anlegen'))
        self.ok.add_css_class("suggested-action")
        self.ok.set_sensitive(False)
        self.ok.connect("clicked", self._create)
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(description=tr('Jeder Unterordner des Spiele-Ordners wird ein Spiel dieser Bibliothek; kopierte Spiele werden nach dem Kopieren automatisch übernommen. Vorgaben (Proton, Startoptionen, Wayland, DPI) danach in den Einstellungen.'))
        self.name_row = Adw.EntryRow(title=tr('Name in der Seitenleiste'))
        self.name_row.connect("changed", lambda *_: self._validate())
        grp.add(self.name_row)
        self.games_row = Adw.EntryRow(title=tr('Spiele-Ordner'))
        self.games_row.connect("changed", lambda *_: self._validate())
        fb = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
        fb.connect("clicked", lambda *_: choose_folder(self.get_root(), tr("Spiele-Ordner der Bibliothek"),
                                                       self.games_row.get_text() or str(Path.home()), self._games_chosen))
        self.games_row.add_suffix(fb)
        grp.add(self.games_row)
        self.prefix_row = Adw.EntryRow(title=tr('Prefix-Ordner (leer = Unterordner „prefixes“ im Spiele-Ordner)'))
        pb = icon_button("folder-open-symbolic", tr('Ordner wählen'), ["flat"])
        pb.connect("clicked", lambda *_: choose_folder(self.get_root(), tr("Prefix-Ordner der Bibliothek"),
                                                       self.prefix_row.get_text() or self.games_row.get_text() or str(Path.home()),
                                                       lambda p: self.prefix_row.set_text(p)))
        self.prefix_row.add_suffix(pb)
        grp.add(self.prefix_row)
        self.error = label("", css=["error"], wrap=True)
        grp.add(self.error)
        page.add(grp)
        tv.set_content(page)
        self.set_child(tv)

    def _games_chosen(self, path: str) -> None:
        self.games_row.set_text(path)
        if not self.name_row.get_text().strip():
            self.name_row.set_text(Path(path).name)

    def _validate(self) -> None:
        self.ok.set_sensitive(bool(self.name_row.get_text().strip() and self.games_row.get_text().strip()))
        self.error.set_text("")

    def _create(self, *_: object) -> None:
        try:
            lib = self.backend.create_library(self.name_row.get_text(), self.games_row.get_text(), self.prefix_row.get_text())
        except InstallError as exc:
            self.error.set_text(str(exc))
            return
        self.close()
        self.on_created(lib)


def confirm_delete_library(parent, lib, games: int, callback: Callable[[], None]) -> None:
    confirm_destructive(parent, tr('Bibliothek „{0}“ löschen?').format(lib.name),
                        (tr('{0} Spiel verschwindet aus dem Client.') if games == 1 else tr('{0} Spiele verschwinden aus dem Client.')).format(games) + ' '
                        + tr('Spielordner ({0}) und Prefixe ({1}) bleiben auf der Festplatte erhalten; eine neue Bibliothek mit demselben Ordner nimmt die Spiele samt Einstellungen und Spielzeit wieder auf.').format(lib.games_dir, lib.prefix_dir), tr('Löschen'), callback)


def confirm_remove_game(parent, title: str, external: bool, callback: Callable[[bool, bool], None]) -> None:
    """Aus der Bibliothek entfernen – wahlweise mit Spielordner und/oder Prefix."""
    dlg = Adw.AlertDialog.new(tr('„{0}“ aus der Bibliothek entfernen?').format(title),
                              tr('Ohne Häkchen bleiben alle Dateien liegen; der Ordner wird dann bei der Ordnersuche nicht wieder aufgenommen (rückgängig in den Einstellungen).'))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    files = Gtk.CheckButton(label=tr('Spielordner ebenfalls löschen'))
    prefix = Gtk.CheckButton(label=tr('Wine-Prefix ebenfalls löschen (inkl. Spielständen darin)'))
    box.append(files)
    box.append(prefix)
    dlg.set_extra_child(box)
    dlg.add_response("cancel", tr('Abbrechen'))
    dlg.add_response("remove", tr('Entfernen'))
    dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_default_response("cancel")
    dlg.connect("response", lambda d, r: callback(files.get_active(), prefix.get_active()) if r == "remove" else None)
    dlg.present(parent)


def confirm_orphan_prefixes(parent, orphans: list, callback: Callable[[list], None]) -> None:
    """Verwaiste Prefixe anzeigen; jeder einzeln abwählbar."""
    dlg = Adw.AlertDialog.new(tr('{0} verwaiste(r) Prefix(e)').format(len(orphans)),
                              tr('Diese Prefix-Ordner gehören zu keinem Spiel der Bibliothek. Ausgewählte werden gelöscht – inklusive Spielständen, die darin liegen.'))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    checks = []
    for d in orphans[:60]:
        c = Gtk.CheckButton(label=f"{d.name}  ({d.parent})", active=True)
        checks.append((c, d))
        box.append(c)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=min(320, 34 * len(checks)))
    sw.set_child(box)
    dlg.set_extra_child(sw)
    dlg.add_response("cancel", tr('Abbrechen'))
    dlg.add_response("delete", tr('Ausgewählte löschen'))
    dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_default_response("cancel")
    dlg.connect("response", lambda d, r: callback([x for c, x in checks if c.get_active()]) if r == "delete" else None)
    dlg.present(parent)


# ----------------------------------------------------------------------------- Deinstallation
def confirm_uninstall(parent: Gtk.Widget, title: str, callback: Callable[[bool], None]) -> None:
    dlg = Adw.AlertDialog.new(tr('{0} deinstallieren?').format(title), tr('Die Spieldateien werden gelöscht. Spielstände im Wine-Prefix bleiben erhalten, sofern das Prefix nicht mit gelöscht wird.'))
    check = Gtk.CheckButton(label=tr('Wine-Prefix ebenfalls löschen (inkl. Spielstände darin)'))
    dlg.set_extra_child(check)
    dlg.add_response("cancel", tr('Abbrechen'))
    dlg.add_response("uninstall", tr('Deinstallieren'))
    dlg.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_default_response("cancel")
    dlg.connect("response", lambda d, r: callback(check.get_active()) if r == "uninstall" else None)
    dlg.present(parent)
