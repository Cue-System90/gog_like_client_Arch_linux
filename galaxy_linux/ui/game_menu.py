"""Spielmenüs wie in GOG Galaxy 2.1 – gemeinsam für den Anpassen-Knopf der Spielansicht und das Kontextmenü
(Rechtsklick) in Raster und Liste.

Reihenfolge wie im Original (``GameViewMoreButtonComponent``, Kontextmenü ``client.js`` 2280194):
Spielen/Installieren · Zusätzliche Dateiverknüpfungen ▸ · Patch-Notizen anzeigen · Eigenschaften … ·
Installation verwalten ▸ (Konfigurieren …, Ordner importieren, Ordner anzeigen, Überprüfen / Reparieren, Deinstallieren)
· Nach Updates suchen · Spielforum ansehen · Produktseite ansehen | Tags verwalten …, Bewerten ▸, Spiel ausblenden |
Linux: Wine-Prefix ▸, Verknüpfungen ▸, Protokoll öffnen, Aus Bibliothek entfernen …, Einträge der Erweiterungen.

Die Aktionen hängen als Gruppe ``game`` am Hauptfenster und wirken auf ``GameActions.pid`` (beim Öffnen gesetzt).
"""
from __future__ import annotations

from ..i18n import tr

import logging

from .. import library_view as lv
from . import ui_registry
from .util import Gdk, Gio, GLib, Gtk, run_async

log = logging.getLogger(__name__)


class GameActions:
    def __init__(self, win) -> None:
        self.win = win
        self.backend = win.backend
        self.pid = ""
        self._tasks: list = []
        group = Gio.SimpleActionGroup()
        simple = {
            "play": lambda: self.win.page.play(self.pid),
            "install": lambda: self.win.page.install(self.pid),
            "resume": lambda: self._resume(),
            "patch-notes": lambda: self.win.page.show_patch_notes(self.pid),
            "properties": lambda: self.win.page.open_settings(pid=self.pid),
            "configure": lambda: self.win.configure_game(self.pid),
            "import-folder": lambda: self.win.page.import_folder(self.pid),
            "show-folder": lambda: self.win.page.open_install_dir(self.pid),
            "verify": lambda: self.win.page.verify(self.pid),
            "move": lambda: self.win.page.move(self.pid),
            "uninstall": lambda: self.win.page.uninstall(self.pid),
            "check-updates": lambda: self.win.page.check_updates(self.pid),
            "forum": lambda: self.win.page.open_product_link(self.pid, "forum"),
            "support": lambda: self.win.page.open_product_link(self.pid, "support"),
            "store": lambda: self.win.page.open_product_link(self.pid, "store"),
            "tags": lambda: self.win.manage_game_tags(self.pid),
            "hide": lambda: self.win.set_game_hidden(self.pid, True),
            "unhide": lambda: self.win.set_game_hidden(self.pid, False),
            "prefix-open": lambda: self.win.page.prefix_action(self.pid, "open"),
            "prefix-create": lambda: self.win.page.prefix_action(self.pid, "create"),
            "prefix-reset": lambda: self.win.page.prefix_action(self.pid, "reset"),
            "prefix-delete": lambda: self.win.page.prefix_action(self.pid, "delete"),
            "winetricks": lambda: self.win.page.winetricks(self.pid),
            "log": lambda: self.win.page.open_log(self.pid),
            "shortcut-menu": lambda: self.win.page.toggle_shortcut("menu", self.pid),
            "shortcut-desktop": lambda: self.win.page.toggle_shortcut("desktop", self.pid),
            "remove": lambda: self.win.page.remove(self.pid),
        }
        for name, cb in simple.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: self._run(cb))
            group.add_action(a)
        self.rate = Gio.SimpleAction.new_stateful("rate", GLib.VariantType.new("s"), GLib.Variant("s", "0"))
        self.rate.connect("activate", self._on_rate)
        group.add_action(self.rate)
        task = Gio.SimpleAction.new("task", GLib.VariantType.new("s"))
        task.connect("activate", lambda _a, v: self._run(lambda: self._run_task(int(v.get_string()))))
        group.add_action(task)
        ext = Gio.SimpleAction.new("ext", GLib.VariantType.new("s"))
        ext.connect("activate", lambda _a, v: self._run(lambda: self._run_ext(v.get_string())))
        group.add_action(ext)
        win.insert_action_group("game", group)

    def _run(self, cb) -> None:
        if not self.pid:
            return
        try:
            cb()
        except Exception as exc:
            log.exception("Spielaktion fehlgeschlagen")
            self.win.toast(str(exc))

    def _resume(self) -> None:
        job = self.backend.jobs.active_for(self.pid)
        if job is not None:
            self.backend.jobs.resume(job)

    def _on_rate(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        if not self.pid:
            return
        n = lv.set_rating(self.backend.game_settings, self.pid, int(value.get_string() or 0))
        action.set_state(GLib.Variant("s", str(n)))
        self.win.game_meta_changed(self.pid)

    def _run_task(self, i: int) -> None:
        if 0 <= i < len(self._tasks):
            self.win.page.run_task(self.pid, self._tasks[i])

    def _run_ext(self, key: str) -> None:
        for act in ui_registry.game_actions():
            if act.key == key:
                ui_registry.run_action(act, self.win.page, self.pid)

    # ------------------------------------------------------------------ Menü
    def model(self, pid: str, context: bool = False) -> Gio.Menu:
        """Menü für ein Spiel (``context``: Rechtsklick in der Bibliothek, mit Spielen/Installieren am Anfang)."""
        self.pid = str(pid)
        b = self.backend
        g = b.registry.get(self.pid)
        installed = g is not None
        ext = bool(g and g.method == "external") or self.pid.startswith("ext-")
        gog = not ext and self.pid.isdigit()
        state = b.state(self.pid)
        job = b.jobs.active_for(self.pid)
        gs = b.game_settings.get(self.pid)
        self.rate.set_state(GLib.Variant("s", str(gs.rating or 0)))
        menu = Gio.Menu()

        top = Gio.Menu()
        if context:
            if state in ("installed", "update"):
                top.append(tr('Spielen'), "game.play")
            elif state == "not_installed" and gog:
                top.append(tr('Installieren'), "game.install")
            elif job is not None and job.state == "paused":
                top.append(tr('Fortsetzen'), "game.resume")
        self._tasks = []
        if installed:
            try:
                self._tasks = b.launcher.additional_tasks(g)
            except Exception:  # pragma: no cover – Menü darf nie scheitern
                log.exception("Zusätzliche Dateiverknüpfungen nicht ermittelbar")
        if self._tasks:
            sub = Gio.Menu()
            for i, t in enumerate(self._tasks[:30]):
                item = Gio.MenuItem.new(t.name, None)
                item.set_action_and_target_value("game.task", GLib.Variant("s", str(i)))
                sub.append_item(item)
            top.append_submenu(tr('Zusätzliche Dateiverknüpfungen'), sub)
        if gog and not context:
            top.append(tr('Patch-Notizen anzeigen'), "game.patch-notes")
        top.append(tr('Eigenschaften …'), "game.properties")
        manage = Gio.Menu()
        m1 = Gio.Menu()
        if installed and gog and g.platform == "windows" and g.method in ("content-system", "imported"):
            m1.append(tr('Konfigurieren …'), "game.configure")
        if gog:
            m1.append(tr('Ordner importieren'), "game.import-folder")
        if installed:
            m1.append(tr('Ordner anzeigen'), "game.show-folder")
        if installed and not ext:
            m1.append(tr('Überprüfen / Reparieren'), "game.verify")
            m1.append(tr('Installation verschieben …'), "game.move")
        manage.append_section(None, m1)
        if installed and not ext:
            m2 = Gio.Menu()
            m2.append(tr('Deinstallieren'), "game.uninstall")
            manage.append_section(None, m2)
        if manage.get_n_items() and (m1.get_n_items() or installed):
            top.append_submenu(tr('Installation verwalten'), manage)
        if installed and gog:
            top.append(tr('Nach Updates suchen'), "game.check-updates")
        if gog and not context:
            top.append(tr('Spielforum ansehen'), "game.forum")
            top.append(tr('Hilfe erhalten'), "game.support")
            top.append(tr('Produktseite ansehen'), "game.store")
        menu.append_section(None, top)

        lib = Gio.Menu()
        lib.append(tr('Tags verwalten …'), "game.tags")
        rate = Gio.Menu()
        for n in range(lv.MAX_RATING, -1, -1):
            item = Gio.MenuItem.new(lv.stars(n), None)
            item.set_action_and_target_value("game.rate", GLib.Variant("s", str(n)))
            rate.append_item(item)
        lib.append_submenu(tr('Bewerten'), rate)
        e = b.entry(self.pid)
        if e is not None and lv.is_hidden(e, gs):
            lib.append(tr('Spiel wieder einblenden'), "game.unhide")
        else:
            lib.append(tr('Spiel ausblenden'), "game.hide")
        menu.append_section(None, lib)

        if installed:
            linux = Gio.Menu()
            if g.platform == "windows":
                pfx = Gio.Menu()
                has_prefix = b.launcher.prefix_exists(self.pid)
                pfx.append(tr('Wine-Prefix öffnen'), "game.prefix-open")
                if has_prefix:
                    pfx.append(tr('Prefix neu anlegen …'), "game.prefix-reset")
                    pfx.append(tr('Prefix löschen …'), "game.prefix-delete")
                else:
                    pfx.append(tr('Prefix anlegen'), "game.prefix-create")
                pfx.append(tr('Winetricks'), "game.winetricks")
                linux.append_submenu(tr('Wine-Prefix'), pfx)
            has_menu, has_desktop = b.shortcut_state(self.pid)
            sc = Gio.Menu()
            sc.append(tr('Startmenü-Verknüpfung entfernen') if has_menu else tr('Verknüpfung im Startmenü anlegen'),
                      "game.shortcut-menu")
            sc.append(tr('Schreibtisch-Verknüpfung entfernen') if has_desktop else tr('Verknüpfung auf dem Schreibtisch anlegen'),
                      "game.shortcut-desktop")
            linux.append_submenu(tr('Verknüpfungen'), sc)
            linux.append(tr('Protokoll öffnen'), "game.log")
            linux.append(tr('Aus Bibliothek entfernen …'), "game.remove")
            menu.append_section(None, linux)
        exts = Gio.Menu()
        for act in ui_registry.game_actions():
            if ui_registry.action_visible(act, self.win.page, self.pid):
                item = Gio.MenuItem.new(act.label, None)
                item.set_action_and_target_value("game.ext", GLib.Variant("s", act.key))
                exts.append_item(item)
        if exts.get_n_items():
            menu.append_section(None, exts)
        return menu

    def popup_at(self, pid: str, widget: Gtk.Widget, x: float, y: float) -> Gtk.PopoverMenu:
        """Kontextmenü (Rechtsklick) an der Mausposition."""
        pop = Gtk.PopoverMenu.new_from_model(self.model(pid, context=True))
        pop.set_parent(widget)
        pop.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        pop.popup()
        return pop


def attach_context_menu(win, widget: Gtk.Widget, pid_fn) -> None:
    """Rechtsklick auf ``widget`` öffnet das Spielmenü für ``pid_fn()``."""
    click = Gtk.GestureClick(button=3)
    click.connect("pressed", lambda g, _n, x, y: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                                 win.game_actions.popup_at(pid_fn(), widget, x, y)))
    widget.add_controller(click)


def open_product_link(backend, pid: str, which: str, on_url) -> None:
    """Forum-, Support- oder Produktseite aus ``api.gog.com/products`` (im Hintergrund)."""
    def work() -> str:
        prod = backend.api.product(pid) or {}
        links = prod.get("links") or {}
        if which == "forum":
            return links.get("forum") or ""
        if which == "support":
            return links.get("support") or ""
        return links.get("product_card") or ""
    run_async(work, on_url, lambda e: on_url(""))
