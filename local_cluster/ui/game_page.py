"""Spielansicht nach GOG Galaxy 2.1: Hero-Artwork mit Logo, Play-Leiste mit Kennzahlen und Anpassen-Menü, darunter
die Tabs Übersicht / Mein Fortschritt / Extras (``GameViewTab``).

Schmale Fenster (bis 1280 px, 4K-Fernseher mit 225 % ≈ 1706 px): die Kennzahlen der Play-Leiste und die Tags brechen
in eine zweite Zeile um (``Adw.WrapBox``), lange Werte und Auswahllisten kürzen mit „…“ – die Spielansicht hält das
Fenster nie breiter als nötig (``ui/width_audit.py``).

Zusätzlich (wie Steam): Kompatibilitätstool und Startoptionen direkt in der Übersicht. Alle Spielaktionen nehmen eine
Produkt-ID – das Kontextmenü der Bibliothek (ui/game_menu.py) nutzt sie auch für Spiele, die gerade nicht angezeigt
werden.
"""
from __future__ import annotations

from ..i18n import tr

from .. import paths
from ..content import extras as extras_mod
from ..content.installer import InstallError
from ..jsonstore import read_json
from ..launch import goverlay, goverlay_profiles, launch_options
from ..launch.launcher import LaunchError
from ..settings import format_bytes, format_minutes
from .dialogs import (GameSettingsDialog, InstallDialog, confirm_destructive, confirm_remove_game, confirm_uninstall,
                      move_installation)
from . import game_menu, store, ui_registry
from .util import (Adw, Gtk, Pango, choose_folder, clamped_picture, ellipsize_dropdown, fmt_eta, fmt_speed, icon_button,
                   idle, label, open_uri, pick_icon, relative_time, run_async)

RARITY = {"common": tr('Gewöhnlich'), "uncommon": tr('Ungewöhnlich'), "rare": tr('Selten'), "epic": tr('Episch'),
          "legendary": tr('Legendär')}
OS_FILTER = (("", tr('Alle')), ("windows", "Windows"), ("linux", "Linux"), ("mac", "macOS"))


class Stat(Gtk.Box):
    def __init__(self, key: str, pad: int = 10) -> None:
        # Rand wie die Kennzahl-Knöpfe der Play-Leiste (stat-btn: 10 px Innenabstand) – bricht die Zeile um, stehen
        # alle bündig untereinander
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, margin_start=pad,
                         margin_end=pad)
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
        self._extras: list = []
        self._extras_pid = ""
        # letzte Meldung von Start/Nachbereitung je Spiel: (Text, Ergebnis?) – Zwischenstände enden auf „…“
        self._hook_status: dict[str, tuple[str, bool]] = {}

        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller = scrolled
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
        self.install_btn.connect("clicked", lambda *_: self.install())
        self.update_btn = Gtk.Button(label=tr('AKTUALISIEREN'))
        self.update_btn.add_css_class("btn-secondary")
        self.update_btn.connect("clicked", lambda *_: self.update())
        self.resume_btn = Gtk.Button(label=tr('FORTSETZEN'))
        self.resume_btn.add_css_class("btn-install")
        self.resume_btn.connect("clicked", lambda *_: self._resume_job())
        self.cancel_btn = Gtk.Button(label=tr('ABBRECHEN'))
        self.cancel_btn.add_css_class("btn-danger")
        self.cancel_btn.connect("clicked", self._on_cancel)
        self.stop_btn = Gtk.Button(label=tr('BEENDEN'))
        self.stop_btn.add_css_class("btn-danger")
        self.stop_btn.connect("clicked", self._on_stop)
        # Anpassen-Knopf (Galaxy: Schieberegler rechts neben „Spielen“) – Menü bei jedem Öffnen neu
        self.menu_btn = Gtk.MenuButton(icon_name=pick_icon("emblem-system-symbolic", "view-more-symbolic", "open-menu-symbolic"),
                                       tooltip_text=tr('Anpassen'))
        self.menu_btn.add_css_class("btn-icon-big")
        self.menu_btn.set_create_popup_func(
            lambda b: b.set_popover(Gtk.PopoverMenu.new_from_model(self.win.game_actions.model(self.pid or ""))))
        for w in (self.play_btn, self.install_btn, self.update_btn, self.resume_btn, self.cancel_btn, self.stop_btn, self.menu_btn):
            w.set_valign(Gtk.Align.CENTER)
            bar.append(w)
        # bis refresh_state den Zustand kennt, nur ein Knopf – sonst hielten sechs Knöpfe das Fenster beim Start breit
        for w in (self.install_btn, self.update_btn, self.resume_btn, self.cancel_btn, self.stop_btn):
            w.set_visible(False)
        # Kennzahlen: eine Zeile, bei schmalem Fenster zwei (statt das Fenster breit zu halten)
        stats = Adw.WrapBox(child_spacing=14, line_spacing=10, margin_start=12, hexpand=True, valign=Gtk.Align.CENTER)
        stats.add_css_class("stats-row")
        self.stat_time = Stat(tr('Spielzeit'))
        self.stat_last = Stat(tr('Zuletzt gespielt'))
        self.stat_ach = Stat(tr('Erfolge'))
        self.stat_tool = Stat(tr('Kompatibilität'))
        tool_btn = Gtk.Button(child=self.stat_tool)
        tool_btn.add_css_class("flat")
        tool_btn.add_css_class("stat-btn")
        tool_btn.set_tooltip_text(tr('Proton/Wine und Startoptionen (wie Steam-Eigenschaften)'))
        tool_btn.connect("clicked", lambda *_: self.open_settings())
        for w in (self.stat_time, self.stat_last, self.stat_ach, tool_btn):
            stats.append(w)
        bar.append(stats)
        self.status_lbl = label("", css=["dim"], ellipsize=True)
        self.status_lbl.set_valign(Gtk.Align.CENTER)
        self.status_lbl.set_max_width_chars(48)
        self.status_lbl.connect("notify::label", lambda lbl, _p: lbl.set_tooltip_text(lbl.get_text() or None))
        bar.append(self.status_lbl)
        self.settings_btn = icon_button(pick_icon("preferences-other-symbolic", "emblem-system-symbolic"), tr('Eigenschaften'),
                                        ["btn-icon-big"])
        self.settings_btn.set_valign(Gtk.Align.CENTER)
        self.settings_btn.connect("clicked", lambda *_: self.open_settings())
        bar.append(self.settings_btn)
        self.actionbar = bar
        content.append(bar)

        # ----- Fortschritt
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.progress_box.add_css_class("progress-strip")
        ph = Gtk.Box(spacing=8)
        self.progress_status = label("", css=["stat-key"])
        self.progress_status.set_hexpand(True)
        ph.append(self.progress_status)
        self.pause_btn = Gtk.Button(label=tr('Pausieren'))
        self.pause_btn.add_css_class("flat")
        self.pause_btn.connect("clicked", lambda *_: self._toggle_pause())
        ph.append(self.pause_btn)
        downloads = Gtk.Button(label=tr('Downloads verwalten'))
        downloads.add_css_class("flat")
        downloads.connect("clicked", lambda *_: self.win.navigate("downloads"))
        ph.append(downloads)
        self.progress_box.append(ph)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_detail = label("", css=["mono"], ellipsize=True)
        for w in (self.progress_bar, self.progress_detail):
            self.progress_box.append(w)
        self.progress_box.set_visible(False)
        content.append(self.progress_box)

        # ----- Tabs wie Galaxy: Übersicht / Mein Fortschritt / Extras
        self.tabs = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        switcher = Gtk.StackSwitcher(stack=self.tabs, halign=Gtk.Align.START)
        switcher.add_css_class("game-tabs")
        switcher.set_margin_start(28)
        switcher.set_margin_top(14)
        content.append(switcher)
        self.tabs.add_titled(self._build_overview(stats), "overview", tr('Übersicht'))
        self.tabs.add_titled(self._build_progress(), "progression", tr('Mein Fortschritt'))
        self.tabs.add_titled(self._build_extras(), "extras", tr('Extras'))
        self.tabs.connect("notify::visible-child-name", lambda *_: self._on_tab())
        content.append(self.tabs)

        scrolled.set_child(content)
        self.append(scrolled)

    # ------------------------------------------------------------------ Aufbau der Tabs
    def _build_overview(self, stats: Gtk.Box) -> Gtk.Widget:
        cols = Gtk.Box(spacing=32, margin_start=28, margin_end=28, margin_top=14, margin_bottom=32)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, hexpand=True)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, hexpand=False)
        right.set_size_request(380, -1)
        cols.append(left)
        cols.append(right)

        # Links: Über das Spiel, Medien
        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        about.append(label(tr('ÜBER DAS SPIEL'), css=["sec-hdr"]))
        self.tag_box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        about.append(self.tag_box)
        self.summary_lbl = label("", css=["body-text"], wrap=True, selectable=True)
        self.summary_lbl.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
        about.append(self.summary_lbl)
        self.dlc_lbl = label("", css=["dim"], wrap=True)
        about.append(self.dlc_lbl)
        left.append(about)
        self.media_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.media_box.append(label(tr('MEDIEN'), css=["sec-hdr"]))
        self.media_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=10, row_spacing=10,
                                      max_children_per_line=3, halign=Gtk.Align.START)
        self.media_box.append(self.media_flow)
        self.media_box.set_visible(False)
        left.append(self.media_box)

        # Rechts: Details, Kompatibilität
        det = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        det.append(label(tr('SPIELDETAILS'), css=["sec-hdr"]))
        self.detail_grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.details: dict[str, Gtk.Label] = {}
        for i, key in enumerate((tr('Plattform'), tr('Status'), tr('Version'), tr('Größe'), tr('Ordner'), tr('Sprache'), tr('Genre'),
                                 tr('Presse-Wertung'), tr('Meine Bewertung'), tr('Tags'), tr('Entwickler'), tr('Publisher'),
                                 tr('Veröffentlicht'), tr('Prefix'))):
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
        self.tool_dd = ellipsize_dropdown(Gtk.DropDown())
        self.tool_dd.add_css_class("compat-dd")
        self.tool_dd.connect("notify::selected", self._on_tool_changed)
        comp.append(self.tool_dd)
        self.exe_lbl = label(tr('Startdatei'), css=["detail-key"])
        self.exe_dd = ellipsize_dropdown(Gtk.DropDown())
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
        ob = Adw.WrapBox(child_spacing=6, line_spacing=6)
        apply = Gtk.Button(label=tr('Übernehmen'))
        apply.connect("clicked", self._on_opts_apply)
        gov = Gtk.Button(label=tr('GOverlay öffnen'))
        gov.set_tooltip_text(tr('In GOverlay das Spiel anklicken, um sein Profil (MangoHud, vkBasalt …) einzurichten'))
        gov.set_sensitive(goverlay.status().goverlay)
        gov.connect("clicked", self._on_goverlay)
        more = Gtk.Button(label=tr('Eigenschaften …'))
        more.connect("clicked", lambda *_: self.open_settings())
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
        return cols

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=28, margin_end=28, margin_top=14,
                      margin_bottom=32)
        sums = Gtk.Box(spacing=34)
        self.prog_time = Stat(tr('Spielzeit'), pad=0)
        self.prog_last = Stat(tr('Zuletzt gespielt'), pad=0)
        self.prog_ach = Stat(tr('Erfolge'), pad=0)
        for w in (self.prog_time, self.prog_last, self.prog_ach):
            sums.append(w)
        box.append(sums)
        self.ach_hdr = label(tr('ERFOLGE'), css=["sec-hdr"])
        box.append(self.ach_hdr)
        self.ach_hint = label("", css=["dim"], wrap=True)
        box.append(self.ach_hint)
        self.ach_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.ach_list.add_css_class("boxed-list")
        box.append(self.ach_list)
        return box

    def _build_extras(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=28, margin_end=28, margin_top=14,
                      margin_bottom=32)
        top = Gtk.Box(spacing=10)
        top.append(label(tr('Betriebssystem:'), css=["dim"]))
        self.os_dd = Gtk.DropDown.new_from_strings([n for _k, n in OS_FILTER])
        self.os_dd.connect("notify::selected", lambda *_: self._render_extras())
        top.append(self.os_dd)
        top.append(label(tr('Sprache:'), css=["dim"]))
        self.lang_dd = Gtk.DropDown.new_from_strings([tr('Alle')])
        self._extra_langs: list[str] = [""]
        self.lang_dd.connect("notify::selected", lambda *_: self._render_extras())
        top.append(self.lang_dd)
        sp = Gtk.Box(hexpand=True)
        top.append(sp)
        self.dl_all_btn = Gtk.Button(label=tr('Alle herunterladen'))
        self.dl_all_btn.add_css_class("btn-secondary")
        self.dl_all_btn.connect("clicked", lambda *_: self._download_extras(self._visible_extras()))
        top.append(self.dl_all_btn)
        folder = Gtk.Button(label=tr('Download-Ordner öffnen'))
        folder.add_css_class("flat")
        folder.connect("clicked", lambda *_: self._open_extras_dir())
        top.append(folder)
        box.append(top)
        self.extras_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(self.extras_box)
        self.cloud_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.cloud_box.append(label(tr('SICHERUNGSKOPIE DEINER CLOUD-SPEICHERSTÄNDE'), css=["sec-hdr"]))
        crow = Gtk.Box(spacing=10)
        crow.append(label(tr('Alle Spielstände aus der GOG Galaxy-Cloud als Archiv in den Ordner für andere Downloads laden.'),
                          css=["dim"], wrap=True))
        cb = Gtk.Button(label=tr('Herunterladen'), valign=Gtk.Align.CENTER)
        cb.connect("clicked", lambda *_: self._cloud_backup())
        crow.append(cb)
        self.cloud_box.append(crow)
        box.append(self.cloud_box)
        return box

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
        self.media_flow.remove_all()
        self.media_box.set_visible(False)
        self.ach_list.remove_all()
        self.ach_hint.set_text("")
        self.ach_hdr.set_text(tr('ERFOLGE'))
        self._extras, self._extras_pid = [], ""
        self.tabs.set_visible_child_name("overview")
        self.status_lbl.set_text(self._hook_status.get(self.pid, ("", False))[0]
                                 if self.backend.launcher.is_running(self.pid) else "")
        pid = self.pid
        run_async(lambda: self.backend.meta(pid), lambda m: pid == self.pid and self._fill_meta(m))
        self._fill_quick()
        self.refresh_state()
        self._load_achievements()
        # Spielzeit aller Geräte von GOG übernehmen (höchstens alle 10 Minuten je Spiel)
        run_async(lambda: self.backend.pull_playtime([pid], max_age=600),
                  lambda n: n and pid == self.pid and self.refresh_state(), lambda e: None)
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
        d[tr('Presse-Wertung')].set_text(f"{m.rating:.0f} / 100" if m.rating is not None else "–")
        d[tr('Entwickler')].set_text(", ".join(m.developers) or "–")
        d[tr('Publisher')].set_text(", ".join(m.publishers) or "–")
        d[tr('Veröffentlicht')].set_text(m.release_date or "–")
        if m.dlc_ids:
            total = len(m.dlc_ids)

            def show(dlcs: list) -> None:
                if pid != self.pid:
                    return
                names = [t + (" ✓" if inst else "") for _d, t, inst in dlcs]
                self.dlc_lbl.set_text(tr('DLCs im Besitz: {0} von {1}').format(len(dlcs), total)
                                      + (" – " + ", ".join(names) if names else ""))
            run_async(lambda: self.backend.owned_dlcs(pid), show, lambda e: None)
        self.media_flow.remove_all()
        for url in m.screenshots[:6]:
            frame, pic = clamped_picture(240, 135, ["cover"])
            btn = Gtk.Button(child=frame)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, u=url: open_uri(u))
            self.media_flow.append(btn)
            self.images.load(url, pic.set_paintable)
        self.media_box.set_visible(bool(m.screenshots))

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
        paused = job is not None and job.state == "paused"
        self.play_btn.set_visible(state in ("installed", "update"))
        self.install_btn.set_visible(state == "not_installed")
        self.update_btn.set_visible(state == "update")
        self.resume_btn.set_visible(paused)
        self.cancel_btn.set_visible(state == "busy" and not paused)
        phase = self.backend.launcher.phase(self.pid) if state == "running" else ""
        # während der Nachbereitung (Cloud-Upload …) gibt es nichts zu beenden; vor dem Start bricht BEENDEN ab
        self.stop_btn.set_visible(state == "running" and phase != "finishing")
        self.stop_btn.set_tooltip_text(tr('Start abbrechen') if phase == "starting" else tr('Spiel beenden'))
        self.progress_box.set_visible(state == "busy")
        if job:
            self.on_job(job)
        minutes, last = self.backend.playtime.get(self.pid)
        for w in (self.stat_time, self.prog_time):
            w.set(format_minutes(minutes) if minutes else "–")
        for w in (self.stat_last, self.prog_last):
            w.set(relative_time(last) if last else "–")
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
                                 + (tr(' · Update verfügbar') if state == "update" else "")
                                 + self._copy_note(inst))
            ver = (inst.version_name or "–") + (tr('  (Build {0})').format(inst.build_id) if inst.build_id else "")
            if inst.branch:
                ver += f" · {inst.branch}"
            d[tr('Version')].set_text(ver)
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
        from .. import library_view as lv
        d[tr('Meine Bewertung')].set_text(lv.stars(gs.rating) if gs.rating else "–")
        d[tr('Tags')].set_text(", ".join(gs.tags) or "–")
        gov = goverlay_profiles.status_for(self.backend, self.pid)
        self.gov_lbl.set_text(gov or "")
        self.gov_lbl.set_visible(bool(gov))
        self._update_preview()
        ui_registry.notify_sections(self._ext_sections, "refresh_state", self.pid)

    def _copy_note(self, inst) -> str:
        """„ · Kopie von „Titel““ für Kopien (gleicher Inhalt wie ein anderes externes Spiel)."""
        orig = self.backend.copy_of(inst.product_id) if getattr(inst, "duplicate_of", "") else None
        if orig is None:
            return ""
        return " · " + tr('Kopie von „{0}“').format(self.backend.display_title(orig.product_id))

    def on_job(self, job) -> None:
        if job.product_id != self.pid:
            return
        p = job.progress
        if job.active:
            from ..content import phases
            self.progress_box.set_visible(True)
            paused = job.state == "paused"
            head = tr('PAUSIERT') if paused else (phases.label(job.step) or job.status).upper()
            self.progress_status.set_text(f"{job.label.upper()} · {head}")
            self.progress_box.set_tooltip_text(phases.tooltip(job.steps, job.step) or None)
            self.pause_btn.set_visible(job.pausable)
            self.pause_btn.set_label(tr('Fortsetzen') if paused else tr('Pausieren'))
            if p.total_bytes:
                self.progress_bar.set_fraction(p.fraction)
                self.progress_detail.set_text(
                    tr('{0:.1f} %   {1} / {2}   ↓ {3}   ETA {4}   {5}/{6} Dateien   {7}').format(p.fraction * 100, format_bytes(p.done_bytes), format_bytes(p.total_bytes), fmt_speed(p.speed), fmt_eta(p.eta_seconds), p.files_done, p.files_total, p.current))
            elif not paused:
                self.progress_bar.pulse()
                self.progress_detail.set_text(p.current or "")
            self.cancel_btn.set_visible(not paused)
            self.resume_btn.set_visible(paused)
            for w in (self.install_btn, self.play_btn, self.update_btn):
                w.set_visible(False)
        else:
            self.refresh_state()

    def _on_tab(self) -> None:
        if self.tabs.get_visible_child_name() == "extras" and self.pid and self._extras_pid != self.pid:
            self._load_extras()

    # ------------------------------------------------------------------ Spielen
    def _on_play(self, *_: object) -> None:
        if self.pid:
            self.play(self.pid)

    def play(self, pid: str | None = None, task=None) -> None:
        """Spiel starten (auch aus dem Kontextmenü oder der Power Search für ein anderes Spiel)."""
        pid = str(pid or self.pid)
        if not self.backend.registry.is_installed(pid) or self.backend.launcher.is_running(pid):
            return
        if pid == self.pid:
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
            self.win.game_launched(pid)

        def fail(exc: Exception) -> None:
            self._hook_status.pop(pid, None)
            if self.pid == pid:
                self.play_btn.set_sensitive(True)
                self.status_lbl.set_text("")
                self.refresh_state()
            self.win.refresh_game_state(pid)
            cancelled = str(exc) == tr('Start abgebrochen.')
            self.win.toast(str(exc) if cancelled else tr('Start fehlgeschlagen: {0}').format(exc))
        run_async(lambda: self.backend.launch(pid, task=task, on_exit=on_exit, status_cb=status), done, fail)

    def run_task(self, pid: str, task) -> None:
        """Zusätzliche Dateiverknüpfung: Programme über Proton/Wine starten, Handbücher und Links normal öffnen."""
        g = self.backend.registry.get(pid)
        if g is None:
            return
        launcher = self.backend.launcher
        if task.type == "URLTask":
            open_uri(task.link)
        elif launcher.opens_with_desktop(task):
            launcher.open_path(launcher.task_path(g, task))
        else:
            self.play(pid, task)

    def _after_exit(self, pid: str, code: int, minutes: int) -> None:
        last, final = self._hook_status.pop(pid, ("", False))
        if pid == self.pid:
            self.status_lbl.set_text("")
            self.refresh_state()
        self.win.refresh_game_state(pid)
        self.win.game_exited(pid)
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

    # ------------------------------------------------------------------ Installieren und verwalten
    def install(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        entry = self.backend.entry(pid)
        if not entry:
            return
        if not self.backend.api.logged_in:
            self.win.toast(tr('Bitte zuerst bei GOG anmelden.'))
            self.win.do_login()
            return

        def confirm(v: dict) -> None:
            goodies = v.pop("download_goodies", False)
            auto = v.pop("auto_update", True)
            excluded = v.pop("dlc_excluded", None)
            self.backend.game_settings.update(pid, auto_update=auto, **({"dlc_excluded": excluded} if excluded is not None else {}))
            self.backend.install(pid, **v)
            if goodies:
                run_async(lambda: [i for i in self.backend.extras(pid) if i.kind == "goodie"],
                          lambda items: items and self.backend.download_extras(pid, items), lambda e: None)
            if pid == self.pid:
                self.refresh_state()
            self.win.refresh_game_state(pid)
        InstallDialog(self.backend, entry, self.meta if pid == self.pid else None, confirm).present(self.win)

    def update(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        self.backend.update(pid)
        self.refresh_state()

    def verify(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        if self.backend.registry.is_installed(pid):
            self.backend.verify(pid)
            self.win.refresh_game_state(pid)
            if pid == self.pid:
                self.refresh_state()

    def check_updates(self, pid: str | None = None) -> None:
        """„Nach Updates suchen“ für ein Spiel (Galaxy: Snackbar „… wird geprüft“ bzw. „Keine Updates …“)."""
        pid = str(pid or self.pid)
        g = self.backend.registry.get(pid)
        if g is None:
            return
        self.win.toast(tr('Suche nach Updates für {0} …').format(g.title))
        pw = self.backend.game_settings.get(pid).branch_password

        def done(build) -> None:
            if build is not None:
                self.backend.updates[pid] = build
                self.win.toast(tr('Update verfügbar für {0}: {1}').format(g.title, build.version_name or build.build_id))
            else:
                self.backend.updates.pop(pid, None)
                self.win.toast(tr('Keine Updates für {0} verfügbar').format(g.title))
            self.win.refresh_game_state(pid)
            if pid == self.pid:
                self.refresh_state()
        run_async(lambda: self.backend.installer.check_update(g, pw), done, lambda e: self.win.toast(str(e)))

    def _on_cancel(self, *_: object) -> None:
        job = self.backend.jobs.active_for(self.pid)
        if job:
            self.backend.jobs.cancel(job)
            self.progress_status.set_text(tr('WIRD ABGEBROCHEN …'))

    def _toggle_pause(self) -> None:
        job = self.backend.jobs.active_for(self.pid)
        if job is None:
            return
        if job.state == "paused":
            self.backend.jobs.resume(job)
        else:
            self.backend.jobs.pause(job)

    def _resume_job(self) -> None:
        job = self.backend.jobs.active_for(self.pid)
        if job is not None:
            self.backend.jobs.resume(job)

    def open_settings(self, page: str = "", pid: str | None = None) -> None:
        """Eigenschaften öffnen (optional auf einer Seite); Änderungen erscheinen sofort auf der Spielseite."""
        pid = str(pid or self.pid)

        def changed() -> None:
            if pid == self.pid:
                self._fill_quick()
                self.refresh_state()
        dlg = GameSettingsDialog(self.backend, pid, on_changed=changed)
        if page:
            dlg.set_visible_page_name(page)
        dlg.present(self.win)

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

    def winetricks(self, pid: str | None = None) -> None:
        try:
            self.backend.launcher.run_winetricks(str(pid or self.pid))
        except LaunchError as exc:
            self.win.toast(str(exc))

    def open_install_dir(self, pid: str | None = None) -> None:
        inst = self.backend.registry.get(str(pid or self.pid))
        if inst:
            self.backend.launcher.open_path(inst.install_path)

    def open_log(self, pid: str | None = None) -> None:
        self.backend.launcher.open_path(paths.game_log_file(str(pid or self.pid)))

    def move(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        move_installation(self.win, self.backend, pid, lambda: self.win.refresh_game_state(pid))

    def import_folder(self, pid: str | None = None) -> None:
        """„Ordner importieren“: vorhandene Installation des Spiels aus einem Ordner übernehmen."""
        pid = str(pid or self.pid)
        entry = self.backend.entry(pid)

        def chosen(path: str) -> None:
            def work():
                info = self.backend.inspect_folder(path)
                if info["kind"] != "gog" or info["product_id"] != pid:
                    raise InstallError(tr('In diesem Ordner liegt keine Installation von {0}.').format(entry.title if entry else pid))
                return self.backend.add_game_manually(path)

            def done(game) -> None:
                self.win.toast(tr('„{0}“ übernommen').format(game.title))
                self.win.populate()
                if pid == self.pid:
                    self.refresh_state()
            run_async(work, done, lambda e: self.win.toast(str(e)))
        choose_folder(self.win, tr('Ordner importieren'), self.backend.settings.install_dir, chosen)

    def toggle_shortcut(self, which: str, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
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

    def uninstall(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        inst = self.backend.registry.get(pid)
        if not inst:
            return

        def do(remove_prefix: bool) -> None:
            def done(_r) -> None:
                self.win.toast(tr('{0} deinstalliert').format(inst.title))
                if pid == self.pid:
                    self.refresh_state()
                self.win.refresh_game_state(pid)
            run_async(lambda: self.backend.uninstall(pid, remove_prefix), done, lambda e: self.win.toast(str(e)))
        confirm_uninstall(self.win, inst.title, do)

    def remove(self, pid: str | None = None) -> None:
        pid = str(pid or self.pid)
        inst = self.backend.registry.get(pid)
        if not inst:
            return
        lib = self.backend.library_of(pid)

        def do(delete_files: bool, delete_prefix: bool) -> None:
            def done(_r) -> None:
                self.win.toast(tr('„{0}“ entfernt').format(inst.title))
                self.win.populate()
                if self.win.current_pid == pid:
                    self.win.navigate(f"lib:{lib.id}" if lib is not None else "all")
            run_async(lambda: self.backend.remove_game(pid, delete_files, delete_prefix), done, lambda e: self.win.toast(str(e)))
        confirm_remove_game(self.win, inst.title, inst.method == "external", do)

    def prefix_action(self, pid: str | None, action: str) -> None:
        pid = str(pid or self.pid)
        if action == "open":
            self.backend.launcher.open_path(self.backend.launcher.prefix_for(pid))
            return
        if action == "create":
            self.win.toast(tr('Prefix wird angelegt …') if self.backend.queue_prefix(pid) else tr('Prefix wird bereits bearbeitet'))
            self.win.refresh_game_state(pid)
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
            if pid == self.pid:
                self.refresh_state()
        if action == "reset":
            confirm_destructive(self.win, tr('Prefix neu anlegen?'), tr('Der Prefix wird gelöscht und frisch angelegt. Spielstände, die im Prefix liegen, gehen dabei verloren.'), tr('Neu anlegen'), run)
        else:
            confirm_destructive(self.win, tr('Prefix löschen?'), tr('Der Prefix-Ordner wird gelöscht – inklusive Spielständen darin.'),
                                tr('Löschen'), run)

    def open_product_link(self, pid: str | None, which: str) -> None:
        pid = str(pid or self.pid)
        if which == "store":
            store.show_product(self.win, pid, self.meta if pid == self.pid else None, self.backend.entry(pid))
            return
        game_menu.open_product_link(self.backend, pid, which,
                                    lambda url: open_uri(url) if url else self.win.toast(tr('Kein Link verfügbar')))

    def show_patch_notes(self, pid: str | None = None) -> None:
        """„Patch-Notizen anzeigen“: Änderungsprotokoll des Spiels von GOG."""
        pid = str(pid or self.pid)

        def show(m) -> None:
            text = (m.changelog if m else "") or tr('Für dieses Spiel gibt es keine Patch-Notizen.')
            dlg = Adw.Dialog(title=tr('Patch-Notizen'), content_width=620, content_height=560)
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            lbl = label(text, wrap=True, selectable=True)
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(18)
            lbl.set_margin_start(20)
            lbl.set_margin_end(20)
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            scroll.set_child(lbl)
            tv.set_content(scroll)
            dlg.set_child(tv)
            dlg.present(self.win)
        run_async(lambda: self.backend.meta(pid), show, lambda e: show(None))

    # ------------------------------------------------------------------ Mein Fortschritt
    def _load_achievements(self) -> None:
        pid = self.pid
        self.stat_ach.set("–")
        self.prog_ach.set("–")
        if not self.backend.settings.feature_achievements:     # Einstellungen → Spielfunktionen → Erfolge
            self.ach_hint.set_text(tr('Erfolge sind in den Einstellungen ausgeschaltet.'))
            return
        rec = read_json(paths.MANIFEST_DIR / f"{pid}.json", {})
        cid, secret = rec.get("client_id"), rec.get("client_secret")
        if not (cid and secret and self.backend.api.logged_in):
            self.ach_hint.set_text(tr('Für dieses Spiel gibt es keine Erfolge') if self.backend.registry.is_installed(pid)
                                   else tr('Erfolge erscheinen, sobald das Spiel installiert ist.'))
            return

        def done(d: dict) -> None:
            if pid != self.pid:
                return
            items = list(d.get("items") or [])
            if not items:
                self.ach_hint.set_text(tr('Für dieses Spiel gibt es keine Erfolge'))
                return
            unlocked = [a for a in items if a.get("date_unlocked")]
            pct = int(round(100 * len(unlocked) / len(items))) if items else 0
            for w in (self.stat_ach, self.prog_ach):
                w.set(f"{len(unlocked)} / {len(items)}  ({pct} %)")
            self.ach_hdr.set_text(tr('ERFOLGE · {0} VON {1} FREIGESCHALTET').format(len(unlocked), len(items)))
            self.ach_hint.set_text("" if unlocked else tr('Bisher keine Erfolge freigeschaltet.'))
            items.sort(key=lambda a: (not a.get("date_unlocked"), a.get("date_unlocked") or "",
                                      -(float(a.get("rarity") or 0)), a.get("name", "")))
            self.ach_list.remove_all()
            for a in items:
                self.ach_list.append(self._achievement_row(a))
        run_async(lambda: self.backend.api.achievements(cid, secret), done,
                  lambda e: pid == self.pid and self.ach_hint.set_text(tr('Erfolge nicht abrufbar: {0}').format(e)))

    def _achievement_row(self, a: dict) -> Gtk.Widget:
        got = bool(a.get("date_unlocked"))
        secret = not got and not a.get("visible", True)
        row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=8, margin_end=12)
        frame = Gtk.Box()
        frame.add_css_class("achievement-icon")
        if not got:
            frame.add_css_class("locked")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.set_size_request(48, 48)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
        frame.append(pic)
        url = a.get("image_url_unlocked") if got else (a.get("image_url_locked") or a.get("image_url_unlocked"))
        if url and not secret:
            self.images.load(url, pic.set_paintable)
        row.append(frame)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        mid.append(label(tr('Geheime Erfolge') if secret else a.get("name", ""), css=["heading"], ellipsize=True))
        desc = tr('Weiterspielen um diesen Erfolg freizuschalten') if secret else (a.get("description") or "")
        if desc:
            d = label(desc, css=["dim"], wrap=True)
            d.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            mid.append(d)
        row.append(mid)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        right.append(label(str(a["date_unlocked"])[:10] if got else tr('Nicht erreicht'), css=["caption"], xalign=1))
        slug = str(a.get("rarity_level_slug") or "").lower()
        rarity = a.get("rarity")
        if slug in RARITY or rarity is not None:
            text = RARITY.get(slug, "")
            if rarity is not None:
                try:
                    text = (text + " · " if text else "") + f"{float(rarity):.1f} %"
                except (TypeError, ValueError):
                    pass
            right.append(label(text, css=["dim", "caption"], xalign=1))
        row.append(right)
        return row

    # ------------------------------------------------------------------ Extras
    def _load_extras(self) -> None:
        pid = self.pid
        self._extras_pid = pid
        self._clear(self.extras_box)
        is_gog = bool(pid and pid.isdigit())
        self.cloud_box.set_visible(is_gog and self._cloud_possible(pid))
        if not is_gog:
            self.extras_box.append(label(tr('Keine Extras'), css=["dim"]))
            self.dl_all_btn.set_sensitive(False)
            return
        if not self.backend.api.logged_in:
            self.extras_box.append(label(tr('Bitte zuerst bei GOG anmelden.'), css=["dim"]))
            self.dl_all_btn.set_sensitive(False)
            return
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.START)
        self.extras_box.append(spinner)

        def done(items: list) -> None:
            if pid != self.pid:
                return
            self._extras = items
            langs = extras_mod.languages(items)
            self._extra_langs = [""] + [c for c, _n in langs]
            self.lang_dd.set_model(Gtk.StringList.new([tr('Alle')] + [n for _c, n in langs]))
            want = self.backend.settings.language.split("-")[0].lower()
            if want in self._extra_langs:
                self.lang_dd.set_selected(self._extra_langs.index(want))
            g = self.backend.registry.get(pid)
            os_keys = [k for k, _n in OS_FILTER]
            if g is not None and g.platform in os_keys:
                self.os_dd.set_selected(os_keys.index("mac" if g.platform == "osx" else g.platform))
            self._render_extras()

        def fail(exc: Exception) -> None:
            if pid == self.pid:
                self._clear(self.extras_box)
                self.extras_box.append(label(tr('Serververbindung fehlgeschlagen.') + f" {exc}", css=["dim"], wrap=True))
        run_async(lambda: self.backend.extras(pid), done, fail)

    def _cloud_possible(self, pid: str) -> bool:
        g = self.backend.registry.get(pid)
        return bool(g and g.platform == "windows" and g.method != "external" and self.backend.api.logged_in)

    def _visible_extras(self) -> list:
        os_name = OS_FILTER[self.os_dd.get_selected()][0] if self.os_dd.get_selected() < len(OS_FILTER) else ""
        i = self.lang_dd.get_selected()
        lang = self._extra_langs[i] if 0 <= i < len(self._extra_langs) else ""
        return extras_mod.filter_items(self._extras, os_name, lang)

    def _render_extras(self) -> None:
        if self._extras_pid != self.pid:
            return
        self._clear(self.extras_box)
        items = self._visible_extras()
        self.dl_all_btn.set_sensitive(bool(items))
        if not self._extras:
            self.extras_box.append(label(tr('Keine Extras'), css=["sec-hdr"]))
            self.extras_box.append(label(tr('Dieses Spiel bietet scheinbar keine zusätzlichen Inhalte zum Download.'), css=["dim"]))
            return
        if not items:
            self.extras_box.append(label(tr('Keine Dateien mit aktuellen Filtern gefunden.'), css=["dim"]))
            return
        for kind in extras_mod.KINDS:
            group = [i for i in items if i.kind == kind]
            if not group:
                continue
            total = sum(i.size for i in group)
            self.extras_box.append(label(f"{tr(extras_mod.KIND_NAMES[kind]).upper()}  ({len(group)} · {format_bytes(total)})",
                                         css=["sec-hdr"]))
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lst.add_css_class("boxed-list")
            for it in group:
                lst.append(self._extra_row(it))
            self.extras_box.append(lst)

    def _extra_row(self, it) -> Gtk.Widget:
        row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        title = it.name + (f" ({it.product_title})" if it.is_dlc else "")
        mid.append(label(title, css=["heading"], ellipsize=True))
        bits = [x for x in ({"windows": "Windows", "linux": "Linux", "mac": "macOS"}.get(it.os, it.os),
                            it.language_full, (tr('Version {0}').format(it.version) if it.version else ""), it.category,
                            format_bytes(it.size) if it.size else "") if x]
        mid.append(label(" · ".join(bits), css=["dim", "caption"], ellipsize=True))
        row.append(mid)
        btn = Gtk.Button(label=tr('Herunterladen'), valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._download_extras([it]))
        row.append(btn)
        return row

    def _download_extras(self, items: list) -> None:
        if not items or not self.pid:
            return
        job = self.backend.download_extras(self.pid, items)
        self.win.toast(tr('Zu den Downloads hinzugefügt') + f": {job.title}")

    def _open_extras_dir(self) -> None:
        e = self.backend.entry(self.pid or "")
        d = extras_mod.game_dir(self.backend.settings, e.title if e else str(self.pid))
        if not d.is_dir():
            d = extras_mod.extras_dir(self.backend.settings)
            d.mkdir(parents=True, exist_ok=True)
        self.backend.launcher.open_path(d)

    def _cloud_backup(self) -> None:
        if self.pid:
            job = self.backend.backup_cloud_saves(self.pid)
            self.win.toast(tr('Zu den Downloads hinzugefügt') + f": {job.title}")

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
