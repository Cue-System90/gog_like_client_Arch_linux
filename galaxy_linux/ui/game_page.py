"""Spielansicht nach GOG Galaxy 2.0: Hero-Artwork mit Logo, Play-Leiste mit Kennzahlen, Details in zwei Spalten.

Zusätzlich (wie Steam): Kompatibilitätstool und Startoptionen direkt auf der Seite.
"""
from __future__ import annotations

from ..i18n import tr

import time

from .. import paths
from ..jsonstore import read_json
from ..launch import goverlay, goverlay_profiles, launch_options
from ..content.installer import InstallError
from ..launch.launcher import LaunchError
from ..settings import format_bytes, format_minutes
from .dialogs import (GameSettingsDialog, InstallDialog, confirm_destructive, confirm_remove_game, confirm_uninstall,
                      move_installation)
from . import store, ui_registry
from .util import Adw, Gtk, Pango, fmt_eta, fmt_speed, icon_button, idle, label, pick_icon, relative_time, run_async


class Stat(Gtk.Box):
    def __init__(self, key: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.key = label(key.upper(), css=["stat-key"])
        self.val = label("–", css=["stat-val"], ellipsize=True)
        self.val.set_max_width_chars(26)
        self.append(self.key)
        self.append(self.val)

    def set(self, value: str) -> None:
        self.val.set_text(value)


class GamePage(Gtk.Box):
    def __init__(self, win) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.backend = win.backend
        self.images = win.images
        self.pid: str | None = None
        self.entry = None
        self.meta = None
        self._tool_ids: list[str] = []
        self._building = False
        # letzte Meldung von Start/Nachbereitung je Spiel: (Text, Ergebnis?) – Zwischenstände enden auf „…“
        self._hook_status: dict[str, tuple[str, bool]] = {}

        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # ----- Hero
        self.hero = Gtk.Overlay()
        self.hero.add_css_class("hero")
        self.bg = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.bg.set_size_request(-1, 400)
        self.hero.set_child(self.bg)
        shade = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.END)
        shade.add_css_class("hero-shade")
        shade.set_size_request(-1, 220)
        self.hero.add_overlay(shade)
        self.logo = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, halign=Gtk.Align.START, valign=Gtk.Align.END)
        self.logo.set_size_request(420, 150)
        self.logo.set_margin_start(28)
        self.logo.set_margin_bottom(10)
        self.logo.set_visible(False)
        self.hero.add_overlay(self.logo)
        self.title_lbl = label("", css=["hero-title"], wrap=True)
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_valign(Gtk.Align.END)
        self.title_lbl.set_margin_start(28)
        self.title_lbl.set_margin_bottom(14)
        self.title_lbl.set_max_width_chars(40)
        self.hero.add_overlay(self.title_lbl)
        content.append(self.hero)

        # ----- Play-Leiste
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("actionbar")
        self.play_btn = Gtk.Button(label=tr('▶   SPIELEN'))
        self.play_btn.add_css_class("btn-play")
        self.play_btn.connect("clicked", self._on_play)
        self.install_btn = Gtk.Button(label=tr('INSTALLIEREN'))
        self.install_btn.add_css_class("btn-install")
        self.install_btn.connect("clicked", self._on_install)
        self.update_btn = Gtk.Button(label=tr('AKTUALISIEREN'))
        self.update_btn.add_css_class("btn-secondary")
        self.update_btn.connect("clicked", self._on_update)
        self.cancel_btn = Gtk.Button(label=tr('ABBRECHEN'))
        self.cancel_btn.add_css_class("btn-danger")
        self.cancel_btn.connect("clicked", self._on_cancel)
        self.stop_btn = Gtk.Button(label=tr('BEENDEN'))
        self.stop_btn.add_css_class("btn-danger")
        self.stop_btn.connect("clicked", self._on_stop)
        self.menu_btn = Gtk.MenuButton(icon_name=pick_icon("view-more-symbolic", "open-menu-symbolic"), tooltip_text=tr('Weitere Aktionen'))
        self.menu_btn.add_css_class("btn-icon-big")
        self.menu_btn.set_popover(self._build_menu())
        for w in (self.play_btn, self.install_btn, self.update_btn, self.cancel_btn, self.stop_btn, self.menu_btn):
            w.set_valign(Gtk.Align.CENTER)
            bar.append(w)
        stats = Gtk.Box(spacing=34, margin_start=22, hexpand=True)
        self.stat_time = Stat(tr('Spielzeit'))
        self.stat_last = Stat(tr('Zuletzt gespielt'))
        self.stat_ach = Stat(tr('Erfolge'))
        self.stat_tool = Stat(tr('Kompatibilität'))
        tool_btn = Gtk.Button(child=self.stat_tool)
        tool_btn.add_css_class("flat")
        tool_btn.add_css_class("stat-btn")
        tool_btn.set_tooltip_text(tr('Proton/Wine und Startoptionen (wie Steam-Eigenschaften)'))
        tool_btn.connect("clicked", self._on_settings)
        for w in (self.stat_time, self.stat_last, self.stat_ach, tool_btn):
            stats.append(w)
        bar.append(stats)
        self.status_lbl = label("", css=["dim"], ellipsize=True)
        self.status_lbl.set_valign(Gtk.Align.CENTER)
        bar.append(self.status_lbl)
        self.settings_btn = icon_button(pick_icon("emblem-system-symbolic", "preferences-system-symbolic"), tr('Eigenschaften'), ["btn-icon-big"])
        self.settings_btn.set_valign(Gtk.Align.CENTER)
        self.settings_btn.connect("clicked", self._on_settings)
        bar.append(self.settings_btn)
        content.append(bar)

        # ----- Fortschritt
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.progress_box.add_css_class("progress-strip")
        self.progress_status = label("", css=["stat-key"])
        self.progress_bar = Gtk.ProgressBar()
        self.progress_detail = label("", css=["mono"], ellipsize=True)
        for w in (self.progress_status, self.progress_bar, self.progress_detail):
            self.progress_box.append(w)
        self.progress_box.set_visible(False)
        content.append(self.progress_box)

        # ----- Spalten
        cols = Gtk.Box(spacing=32, margin_start=28, margin_end=28, margin_top=18, margin_bottom=32)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, hexpand=True)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, hexpand=False)
        right.set_size_request(380, -1)
        cols.append(left)
        cols.append(right)
        content.append(cols)

        # Links: Über das Spiel, Erfolge
        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        about.append(label(tr('ÜBER DAS SPIEL'), css=["sec-hdr"]))
        self.tag_box = Gtk.Box(spacing=6)
        about.append(self.tag_box)
        self.summary_lbl = label("", css=["body-text"], wrap=True, selectable=True)
        self.summary_lbl.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
        about.append(self.summary_lbl)
        self.dlc_lbl = label("", css=["dim"], wrap=True)
        about.append(self.dlc_lbl)
        left.append(about)
        self.ach_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ach_hdr = label(tr('ERFOLGE'), css=["sec-hdr"])
        self.ach_box.append(self.ach_hdr)
        self.ach_icons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                                     max_children_per_line=30, halign=Gtk.Align.START)
        self.ach_box.append(self.ach_icons)
        self.ach_box.set_visible(False)
        left.append(self.ach_box)

        # Rechts: Details, Kompatibilität
        det = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        det.append(label(tr('SPIELDETAILS'), css=["sec-hdr"]))
        self.detail_grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.details: dict[str, Gtk.Label] = {}
        for i, key in enumerate((tr('Plattform'), tr('Status'), tr('Version'), tr('Größe'), tr('Ordner'), tr('Sprache'), tr('Genre'), tr('Entwickler'),
                                 tr('Publisher'), tr('Veröffentlicht'), tr('Prefix'))):
            k = label(key, css=["detail-key"])
            k.set_valign(Gtk.Align.START)
            v = label("–", css=["detail-val"], selectable=True, ellipsize=True)
            v.set_hexpand(True)
            v.set_max_width_chars(30)
            v.set_width_chars(10)
            v.connect("notify::label", lambda lbl, _p: lbl.set_tooltip_text(lbl.get_text()))
            self.detail_grid.attach(k, 0, i, 1, 1)
            self.detail_grid.attach(v, 1, i, 1, 1)
            self.details[key] = v
        det.append(self.detail_grid)
        right.append(det)

        comp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        comp.add_css_class("panel")
        comp.append(label(tr('KOMPATIBILITÄT (WIE STEAM)'), css=["sec-hdr"]))
        comp.append(label(tr('Proton / Wine'), css=["detail-key"]))
        self.tool_dd = Gtk.DropDown()
        self.tool_dd.add_css_class("compat-dd")
        self.tool_dd.connect("notify::selected", self._on_tool_changed)
        comp.append(self.tool_dd)
        self.exe_lbl = label(tr('Startdatei'), css=["detail-key"])
        self.exe_dd = Gtk.DropDown()
        self.exe_dd.add_css_class("compat-dd")
        self.exe_dd.connect("notify::selected", self._on_exe_changed)
        self._exe_names: list[str] = []
        comp.append(self.exe_lbl)
        comp.append(self.exe_dd)
        comp.append(label(tr('Startoptionen'), css=["detail-key"]))
        self.opts_entry = Gtk.Entry(placeholder_text=tr('z. B. mangohud %command%'))
        self.opts_entry.add_css_class("compat-entry")
        self.opts_entry.connect("activate", self._on_opts_apply)
        comp.append(self.opts_entry)
        ob = Gtk.Box(spacing=6)
        apply = Gtk.Button(label=tr('Übernehmen'))
        apply.connect("clicked", self._on_opts_apply)
        gov = Gtk.Button(label=tr('GOverlay öffnen'))
        gov.set_tooltip_text(tr('In GOverlay das Spiel anklicken, um sein Profil (MangoHud, vkBasalt …) einzurichten'))
        gov.set_sensitive(goverlay.status().goverlay)
        gov.connect("clicked", self._on_goverlay)
        more = Gtk.Button(label=tr('Eigenschaften …'))
        more.connect("clicked", self._on_settings)
        for b in (apply, gov, more):
            ob.append(b)
        comp.append(ob)
        self.gov_lbl = label("", css=["dim"], wrap=True)       # GOverlay-Profil des Spiels
        self.gov_lbl.set_visible(False)
        comp.append(self.gov_lbl)
        self.cmd_preview = label("", css=["mono"], wrap=True, selectable=True)
        self.cmd_preview.set_max_width_chars(44)
        self.cmd_preview.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
        comp.append(self.cmd_preview)
        right.append(comp)

        # Abschnitte der Erweiterungen (ui_registry.register_game_section)
        self._ext_sections: list[Gtk.Widget] = []
        for col, box in (("left", left), ("right", right), ("stats", stats)):
            for w in ui_registry.build_sections(col, self):
                box.append(w)
                self._ext_sections.append(w)

        scrolled.set_child(content)
        self.append(scrolled)

    # ------------------------------------------------------------------ Menü
    def _build_menu(self) -> Gtk.Popover:
        self._menu_btns: dict[str, Gtk.Button] = {}
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        for text, cb in (
            (tr('Dateien prüfen / reparieren'), self._on_verify),
            (tr('Installationsordner öffnen'), lambda *_: self._open_install_dir()),
            (tr('Installation verschieben …'), lambda *_: move_installation(self.win, self.backend, self.pid, self.refresh_state)),
            ("__shortcut_menu__", lambda *_: self._toggle_shortcut("menu")),
            ("__shortcut_desktop__", lambda *_: self._toggle_shortcut("desktop")),
            (tr('Wine-Prefix öffnen'), lambda *_: self.backend.launcher.open_path(self.backend.launcher.prefix_for(self.pid))),
            (tr('Winetricks'), self._on_winetricks),
            (tr('Protokoll öffnen'), lambda *_: self.backend.launcher.open_path(paths.game_log_file(self.pid))),
            (tr('Im Store anzeigen'), lambda *_: store.show_product(self.win, self.pid, self.meta, self.entry)),
            (tr('Prefix anlegen'), lambda *_: self._prefix_action("create")),
            (tr('Prefix neu anlegen …'), lambda *_: self._prefix_action("reset")),
            (tr('Prefix löschen …'), lambda *_: self._prefix_action("delete")),
            (tr('Verstecken'), lambda *_: (self.backend.game_settings.update(self.pid, hidden=True), self.win.toast(tr('Spiel versteckt (Filter → Versteckte anzeigen)')))),
            (tr('Aus Bibliothek entfernen …'), self._on_remove),
            (tr('Deinstallieren'), self._on_uninstall),
        ):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            self._menu_btns[text] = b
            if text == "__shortcut_menu__":
                self.menu_shortcut_btn = b
            elif text == "__shortcut_desktop__":
                self.desktop_shortcut_btn = b
            box.append(b)
        # Einträge der Erweiterungen (ui_registry.register_game_action) nach „Im Store anzeigen“
        anchor = self._menu_btns.get(tr('Im Store anzeigen'))
        self._ext_actions: list[tuple[object, Gtk.Button]] = []
        for act in ui_registry.game_actions():
            b = Gtk.Button(label=act.label)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, act=act: (pop.popdown(), ui_registry.run_action(act, self, self.pid)))
            box.insert_child_after(b, anchor)
            anchor = b
            self._ext_actions.append((act, b))
        pop.connect("show", lambda *_: self._on_menu_show())
        pop.set_child(box)
        return pop

    def _on_menu_show(self) -> None:
        self._update_shortcut_labels()
        for act, b in self._ext_actions:
            b.set_visible(ui_registry.action_visible(act, self, self.pid))

    def _update_shortcut_labels(self) -> None:
        if not self.pid:
            return
        installed = self.backend.registry.is_installed(self.pid)
        has_menu, has_desktop = self.backend.shortcut_state(self.pid)
        self.menu_shortcut_btn.set_label(tr('Startmenü-Verknüpfung entfernen') if has_menu else tr('Verknüpfung im Startmenü anlegen'))
        self.desktop_shortcut_btn.set_label(tr('Schreibtisch-Verknüpfung entfernen') if has_desktop else tr('Verknüpfung auf dem Schreibtisch anlegen'))
        self.menu_shortcut_btn.set_sensitive(installed)
        self.desktop_shortcut_btn.set_sensitive(installed)
        g = self.backend.registry.get(self.pid)
        ext = bool(g and g.method == "external")
        win = bool(g and g.platform == "windows")
        has_prefix = win and self.backend.launcher.prefix_exists(self.pid)
        vis = {tr('Dateien prüfen / reparieren'): installed and not ext, tr('Im Store anzeigen'): not ext and self.pid.isdigit(),
               tr('Installation verschieben …'): installed and not ext, tr('Prefix anlegen'): win and not has_prefix,
               tr('Prefix neu anlegen …'): has_prefix, tr('Prefix löschen …'): has_prefix,
               tr('Aus Bibliothek entfernen …'): installed, tr('Deinstallieren'): installed and not ext}
        for text, v in vis.items():
            if text in self._menu_btns:
                self._menu_btns[text].set_visible(v)

    def _toggle_shortcut(self, which: str) -> None:
        pid = self.pid
        has_menu, has_desktop = self.backend.shortcut_state(pid)
        menu, desktop = which == "menu", which == "desktop"
        exists = has_menu if menu else has_desktop
        where = tr('Startmenü') if menu else tr('Schreibtisch')

        def done(_r) -> None:
            self.win.toast(tr('Verknüpfung ({0}) {1}').format(where, tr('entfernt') if exists else tr('angelegt')))
        if exists:
            run_async(lambda: self.backend.remove_shortcuts(pid, menu=menu, desktop=desktop), done, lambda e: self.win.toast(str(e)))
        else:
            run_async(lambda: self.backend.create_shortcuts(pid, menu=menu, desktop=desktop), done, lambda e: self.win.toast(str(e)))

    # ------------------------------------------------------------------ Anzeige
    def set_game(self, pid: str) -> None:
        self.pid = str(pid)
        self.entry = self.backend.entry(self.pid)
        self.meta = None
        self.title_lbl.set_text(self.entry.title if self.entry else self.pid)
        self.title_lbl.set_visible(True)
        self.logo.set_visible(False)
        self.logo.set_paintable(None)
        self.bg.set_paintable(None)
        self.summary_lbl.set_text("")
        self.dlc_lbl.set_text("")
        self._clear(self.tag_box)
        self.ach_box.set_visible(False)
        self._clear_flow(self.ach_icons)
        self.status_lbl.set_text(self._hook_status.get(self.pid, ("", False))[0]
                                 if self.backend.launcher.is_running(self.pid) else "")
        pid = self.pid
        run_async(lambda: self.backend.meta(pid), lambda m: pid == self.pid and self._fill_meta(m))
        self._fill_quick()
        self.refresh_state()
        self._load_achievements()
        ui_registry.notify_sections(self._ext_sections, "set_game", self.pid)

    def _fill_meta(self, m) -> None:
        if m is None:
            return
        self.meta = m
        self.title_lbl.set_text(m.title)
        self.summary_lbl.set_text(m.summary or tr('Keine Beschreibung verfügbar.'))
        self._clear(self.tag_box)
        for gname in (m.genres + m.themes)[:6]:
            self.tag_box.append(label(gname, css=["tag"]))
        pid = self.pid
        if m.background:
            self.images.load(m.background, lambda tex: pid == self.pid and self.bg.set_paintable(tex))
        if m.logo:
            def logo_cb(tex) -> None:
                if pid != self.pid:
                    return
                self.logo.set_paintable(tex)
                self.logo.set_visible(True)
                self.title_lbl.set_visible(False)
            self.images.load(m.logo, logo_cb)
        d = self.details
        d[tr('Genre')].set_text(", ".join(m.genres) or "–")
        d[tr('Entwickler')].set_text(", ".join(m.developers) or "–")
        d[tr('Publisher')].set_text(", ".join(m.publishers) or "–")
        d[tr('Veröffentlicht')].set_text(m.release_date or "–")
        owned = {e.id for e in self.backend.library}
        dlcs = [x for x in m.dlc_ids if x in owned]
        if m.dlc_ids:
            inst = self.backend.registry.get(self.pid)
            installed_dlcs = set(inst.installed_dlcs) if inst else set()
            names = []
            for x in dlcs:
                e = self.backend.entry(x)
                names.append((e.title if e else x) + (" ✓" if x in installed_dlcs else ""))
            self.dlc_lbl.set_text(tr('DLCs im Besitz: {0} von {1}').format(len(dlcs), len(m.dlc_ids)) + (" – " + ", ".join(names) if names else ""))

    def _fill_quick(self) -> None:
        self._building = True
        gs = self.backend.game_settings.get(self.pid)
        tools = self.backend.launcher.tools()
        default, source = self.backend.launcher.default_tool(self.pid)
        self._tool_ids = [""] + [t.id for t in tools]
        names = [f"{source} ({default.name if default else tr('kein Tool gefunden')})"] + [t.label for t in tools]
        self.tool_dd.set_model(Gtk.StringList.new(names))
        self.tool_dd.set_selected(self._tool_ids.index(gs.compat_tool) if gs.compat_tool in self._tool_ids else 0)
        self.opts_entry.set_text(gs.launch_options)
        g = self.backend.registry.get(self.pid)
        ext = bool(g and g.method == "external")
        self.exe_lbl.set_visible(ext)
        self.exe_dd.set_visible(ext)
        if ext:
            tasks = self.backend.launcher.tasks_for(g)
            self._exe_names = [t.name for t in tasks]
            self.exe_dd.set_model(Gtk.StringList.new(self._exe_names or [tr('keine .exe gefunden')]))
            current = gs.play_task or g.executable
            if current in self._exe_names:
                self.exe_dd.set_selected(self._exe_names.index(current))
        self._building = False
        self._update_preview()

    def _on_exe_changed(self, *_: object) -> None:
        if self._building or not self.pid or not self._exe_names:
            return
        i = self.exe_dd.get_selected()
        if 0 <= i < len(self._exe_names):
            exe = self._exe_names[i]
            self.backend.set_external_exe(self.pid, exe)
            self.backend.game_settings.update(self.pid, play_task=exe)
            self._update_preview()

    def _update_preview(self) -> None:
        if not self.pid:
            return
        try:
            if self.backend.registry.is_installed(self.pid):
                self.cmd_preview.set_text(self.backend.launcher.preview_plan(self.pid).shell_preview())
            else:
                gs = self.backend.game_settings.get(self.pid)
                self.cmd_preview.set_text(launch_options.describe(gs.launch_options, ["<spiel>"]) if gs.launch_options else "")
        except (LaunchError, launch_options.LaunchOptionsError) as exc:
            self.cmd_preview.set_text(str(exc))

    def refresh_state(self) -> None:
        if not self.pid:
            return
        state = self.backend.state(self.pid)
        job = self.backend.jobs.active_for(self.pid)
        inst = self.backend.registry.get(self.pid)
        self.play_btn.set_visible(state in ("installed", "update"))
        self.install_btn.set_visible(state == "not_installed")
        self.update_btn.set_visible(state == "update")
        self.cancel_btn.set_visible(state == "busy")
        phase = self.backend.launcher.phase(self.pid) if state == "running" else ""
        # während der Nachbereitung (Cloud-Upload …) gibt es nichts zu beenden; vor dem Start bricht BEENDEN ab
        self.stop_btn.set_visible(state == "running" and phase != "finishing")
        self.stop_btn.set_tooltip_text(tr('Start abbrechen') if phase == "starting" else tr('Spiel beenden'))
        self.progress_box.set_visible(state == "busy")
        if job:
            self.on_job(job)
        minutes, last = self.backend.playtime.get(self.pid)
        self.stat_time.set(format_minutes(minutes) if minutes else "–")
        self.stat_last.set(relative_time(last) if last else "–")
        gs = self.backend.game_settings.get(self.pid)
        platform = inst.platform if inst else ("windows" if (self.entry and "windows" in self.entry.platforms) else "linux")
        tool = self.backend.launcher.resolve_tool(gs, platform, self.pid)
        if platform == "windows":
            runner = self.backend.launcher.runner_for(tool) if tool else "–"
            self.stat_tool.set(f"{tool.name}" if tool else tr('kein Proton gefunden'))
            self.stat_tool.key.set_text(tr('KOMPATIBILITÄT · {0}').format(runner.upper()) if tool else tr('KOMPATIBILITÄT'))
        else:
            self.stat_tool.set(tr('Nativ (Linux)'))
            self.stat_tool.key.set_text(tr('KOMPATIBILITÄT'))
        d = self.details
        if inst:
            lib = self.backend.library_of(self.pid)
            d[tr('Plattform')].set_text(tr('Windows (extern, nicht von GOG)') if inst.method == "external" else
                                    {"windows": tr('Windows (GOG.com)'), "linux": tr('Linux (GOG.com)'), "osx": tr('macOS (GOG.com)')}.get(inst.platform, inst.platform))
            d[tr('Status')].set_text({"content-system": tr('Installiert · Galaxy Content-System'), "linux-installer": tr('Installiert · Linux-Installer'),
                                  "offline-installer": tr('Installiert · Offline-Installer'),
                                  "imported": tr('Installiert · aus Spiele-Ordner übernommen'),
                                  "external": tr('Extern · Bibliothek „{0}“').format(lib.name if lib else tr('unbekannt'))}.get(inst.method, tr('Installiert'))
                                 + (tr(' · Update verfügbar') if state == "update" else ""))
            d[tr('Version')].set_text((inst.version_name or "–") + (tr('  (Build {0})').format(inst.build_id) if inst.build_id else ""))
            d[tr('Größe')].set_text(format_bytes(inst.install_size) if inst.install_size else "–")
            d[tr('Ordner')].set_text(inst.install_path)
            d[tr('Sprache')].set_text(inst.language or "–")
            d[tr('Prefix')].set_text(str(self.backend.launcher.prefix_for(self.pid)) if inst.platform == "windows" else "–")
        else:
            plats = ", ".join({"windows": "Windows", "linux": "Linux", "osx": "macOS"}[p] for p in (self.entry.platforms if self.entry else []))
            d[tr('Plattform')].set_text(tr('{0} (GOG.com)').format(plats or '–'))
            d[tr('Status')].set_text(tr('Nicht installiert'))
            for k in (tr('Version'), tr('Größe'), tr('Ordner'), tr('Prefix')):
                d[k].set_text("–")
            d[tr('Sprache')].set_text(self.backend.settings.language)
        gov = goverlay_profiles.status_for(self.backend, self.pid)
        self.gov_lbl.set_text(gov or "")
        self.gov_lbl.set_visible(bool(gov))
        self._update_preview()
        ui_registry.notify_sections(self._ext_sections, "refresh_state", self.pid)

    def on_job(self, job) -> None:
        if job.product_id != self.pid:
            return
        p = job.progress
        if job.active:
            self.progress_box.set_visible(True)
            self.progress_status.set_text(f"{job.label.upper()} · {job.status.upper()}")
            if p.total_bytes:
                self.progress_bar.set_fraction(p.fraction)
                self.progress_detail.set_text(
                    tr('{0:.1f} %   {1} / {2}   ↓ {3}   ETA {4}   {5}/{6} Dateien   {7}').format(p.fraction * 100, format_bytes(p.done_bytes), format_bytes(p.total_bytes), fmt_speed(p.speed), fmt_eta(p.eta_seconds), p.files_done, p.files_total, p.current))
            else:
                self.progress_bar.pulse()
                self.progress_detail.set_text(p.current or "")
            self.cancel_btn.set_visible(True)
            for w in (self.install_btn, self.play_btn, self.update_btn):
                w.set_visible(False)
        else:
            self.refresh_state()

    # ------------------------------------------------------------------ Aktionen
    def _on_play(self, *_: object) -> None:
        pid = self.pid
        self.play_btn.set_sensitive(False)
        self.status_lbl.set_text(tr('Starte …'))

        self._hook_status[pid] = (tr('Starte …'), False)

        def on_exit(p: str, code: int, minutes: int) -> None:
            idle(self._after_exit, p, code, minutes)

        def status(text: str) -> None:
            # Meldungen gehören zu diesem Spiel – die Seite zeigt vielleicht schon ein anderes
            def show() -> None:
                self._hook_status[pid] = (text, not text.rstrip().endswith("…"))
                if self.pid != pid:
                    return
                self.status_lbl.set_text(text)
                if self.backend.launcher.phase(pid) == "starting" and not self.stop_btn.get_visible():
                    self.play_btn.set_visible(False)
                    self.stop_btn.set_visible(True)
                    self.stop_btn.set_tooltip_text(tr('Start abbrechen'))
            idle(show)

        def done(rg) -> None:
            text = tr('Läuft · {0}').format(rg.plan.description)
            self._hook_status[pid] = (text, False)
            if self.pid == pid:
                self.play_btn.set_sensitive(True)
                self.status_lbl.set_text(text)
                self.refresh_state()
            self.win.refresh_game_state(pid)

        def fail(exc: Exception) -> None:
            self._hook_status.pop(pid, None)
            if self.pid == pid:
                self.play_btn.set_sensitive(True)
                self.status_lbl.set_text("")
                self.refresh_state()
            self.win.toast(tr('Start fehlgeschlagen: {0}').format(exc))
        run_async(lambda: self.backend.launch(pid, on_exit=on_exit, status_cb=status), done, fail)

    def _after_exit(self, pid: str, code: int, minutes: int) -> None:
        last, final = self._hook_status.pop(pid, ("", False))
        if pid == self.pid:
            self.status_lbl.set_text("")
            self.refresh_state()
        self.win.refresh_game_state(pid)
        e = self.backend.entry(pid)
        text = tr('{0} beendet (Exit-Code {1}, {2})').format(e.title if e else pid, code, format_minutes(minutes))
        if final and last:                     # Ergebnis der Nachbereitung (z. B. Cloud-Upload) nicht verlieren
            text += " · " + last
        self.win.toast(text)

    def _on_stop(self, *_: object) -> None:
        if self.backend.launcher.cancel_start(self.pid):
            self.status_lbl.set_text(tr('Start wird abgebrochen …'))
            return
        rg = self.backend.launcher.running(self.pid)
        if rg and rg.poll() is None:
            rg.terminate()

    def _on_install(self, *_: object) -> None:
        if not self.entry:
            return
        if not self.backend.api.logged_in:
            self.win.toast(tr('Bitte zuerst bei GOG anmelden.'))
            self.win.do_login()
            return
        pid = self.pid

        def confirm(v: dict) -> None:
            self.backend.install(pid, **v)
            self.refresh_state()
            self.win.refresh_game_state(pid)
        InstallDialog(self.backend, self.entry, self.meta, confirm).present(self.win)

    def _on_update(self, *_: object) -> None:
        self.backend.update(self.pid)
        self.refresh_state()

    def _on_verify(self, *_: object) -> None:
        if self.backend.registry.is_installed(self.pid):
            self.backend.verify(self.pid)
            self.refresh_state()

    def _on_cancel(self, *_: object) -> None:
        job = self.backend.jobs.active_for(self.pid)
        if job:
            self.backend.jobs.cancel(job)
            self.progress_status.set_text(tr('WIRD ABGEBROCHEN …'))

    def _on_settings(self, *_: object) -> None:
        GameSettingsDialog(self.backend, self.pid, on_changed=lambda: (self._fill_quick(), self.refresh_state())).present(self.win)

    def _on_tool_changed(self, *_: object) -> None:
        if self._building or not self.pid:
            return
        i = self.tool_dd.get_selected()
        if 0 <= i < len(self._tool_ids):
            self.backend.game_settings.update(self.pid, compat_tool=self._tool_ids[i])
            self.refresh_state()

    def _on_opts_apply(self, *_: object) -> None:
        text = self.opts_entry.get_text().strip()
        try:
            launch_options.parse(text)
        except launch_options.LaunchOptionsError as exc:
            self.win.toast(str(exc))
            return
        self.backend.game_settings.update(self.pid, launch_options=text)
        self._update_preview()
        self.win.toast(tr('Startoptionen gespeichert'))

    def _on_goverlay(self, *_: object) -> None:
        if not goverlay.open_goverlay():
            self.win.toast(tr('GOverlay ist nicht installiert.'))

    def _on_winetricks(self, *_: object) -> None:
        try:
            self.backend.launcher.run_winetricks(self.pid)
        except LaunchError as exc:
            self.win.toast(str(exc))

    def _open_install_dir(self) -> None:
        inst = self.backend.registry.get(self.pid)
        if inst:
            self.backend.launcher.open_path(inst.install_path)

    def _on_uninstall(self, *_: object) -> None:
        inst = self.backend.registry.get(self.pid)
        if not inst:
            return
        pid = self.pid

        def do(remove_prefix: bool) -> None:
            def done(_r) -> None:
                self.win.toast(tr('{0} deinstalliert').format(inst.title))
                if pid == self.pid:
                    self.refresh_state()
                self.win.refresh_game_state(pid)
            run_async(lambda: self.backend.uninstall(pid, remove_prefix), done, lambda e: self.win.toast(str(e)))
        confirm_uninstall(self.win, inst.title, do)

    def _on_remove(self, *_: object) -> None:
        inst = self.backend.registry.get(self.pid)
        if not inst:
            return
        pid = self.pid

        lib = self.backend.library_of(pid)

        def do(delete_files: bool, delete_prefix: bool) -> None:
            def done(_r) -> None:
                self.win.toast(tr('„{0}“ entfernt').format(inst.title))
                self.win.populate()
                self.win.navigate(f"lib:{lib.id}" if lib is not None else "all")
            run_async(lambda: self.backend.remove_game(pid, delete_files, delete_prefix), done, lambda e: self.win.toast(str(e)))
        confirm_remove_game(self.win, inst.title, inst.method == "external", do)

    def _prefix_action(self, action: str) -> None:
        pid = self.pid
        if action == "create":
            self.win.toast(tr('Prefix wird angelegt …') if self.backend.queue_prefix(pid) else tr('Prefix wird bereits bearbeitet'))
            self.refresh_state()
            return

        def run() -> None:
            try:
                if action == "reset":
                    self.backend.reset_prefix(pid)
                    self.win.toast(tr('Prefix wird neu angelegt …'))
                else:
                    self.win.toast(tr('Prefix gelöscht') if self.backend.delete_prefix(pid) else tr('Kein Prefix vorhanden'))
            except InstallError as exc:
                self.win.toast(str(exc))
            self.refresh_state()
        if action == "reset":
            confirm_destructive(self.win, tr('Prefix neu anlegen?'), tr('Der Prefix wird gelöscht und frisch angelegt. Spielstände, die im Prefix liegen, gehen dabei verloren.'), tr('Neu anlegen'), run)
        else:
            confirm_destructive(self.win, tr('Prefix löschen?'), tr('Der Prefix-Ordner wird gelöscht – inklusive Spielständen darin.'),
                                tr('Löschen'), run)

    # ------------------------------------------------------------------ Erfolge
    def _load_achievements(self) -> None:
        pid = self.pid
        self.stat_ach.set("–")
        rec = read_json(paths.MANIFEST_DIR / f"{pid}.json", {})
        cid, secret = rec.get("client_id"), rec.get("client_secret")
        if not (cid and secret and self.backend.api.logged_in):
            return

        def done(d: dict) -> None:
            if pid != self.pid:
                return
            items = [a for a in (d.get("items") or []) if a.get("visible", True) or a.get("date_unlocked")]
            if not items:
                return
            unlocked = [a for a in items if a.get("date_unlocked")]
            pct = int(round(100 * len(unlocked) / len(items))) if items else 0
            self.stat_ach.set(f"{len(unlocked)} / {len(items)}  ({pct} %)")
            self.ach_hdr.set_text(tr('ERFOLGE · {0} VON {1} FREIGESCHALTET').format(len(unlocked), len(items)))
            self._clear_flow(self.ach_icons)
            items.sort(key=lambda a: (not a.get("date_unlocked"), a.get("date_unlocked") or "", a.get("name", "")))
            for a in items[:48]:
                frame = Gtk.Box()
                frame.add_css_class("achievement-icon")
                if not a.get("date_unlocked"):
                    frame.add_css_class("locked")
                frame.set_overflow(Gtk.Overflow.HIDDEN)
                frame.set_size_request(56, 56)
                pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
                frame.append(pic)
                url = a.get("image_url_unlocked") if a.get("date_unlocked") else (a.get("image_url_locked") or a.get("image_url_unlocked"))
                if url:
                    self.images.load(url, pic.set_paintable)
                tip = a.get("name", "")
                if a.get("description"):
                    tip += "\n" + a["description"]
                if a.get("date_unlocked"):
                    tip += tr('\nFreigeschaltet: {0}').format(str(a['date_unlocked'])[:10])
                frame.set_tooltip_text(tip)
                self.ach_icons.append(frame)
            self.ach_box.set_visible(True)
        run_async(lambda: self.backend.api.achievements(cid, secret), done, lambda e: None)

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    @staticmethod
    def _clear_flow(flow: Gtk.FlowBox) -> None:
        flow.remove_all()
