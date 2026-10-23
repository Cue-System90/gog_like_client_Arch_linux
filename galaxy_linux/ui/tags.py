"""Tags wie in Galaxy 2.1: „Tags verwalten“ für ein Spiel (Häkchen wirken sofort, „Neu erstellen …“) und
„Alle Tags verwalten“ (umbenennen, löschen – für alle Spiele)."""
from __future__ import annotations

from ..i18n import tr

from typing import Callable

from .. import library_view as lv
from .util import Adw, GLib, Gtk, icon_button


class TagsDialog(Adw.Dialog):
    def __init__(self, backend, product_id: str, on_changed: Callable[[str], None]) -> None:
        entry = backend.entry(product_id)
        super().__init__(title=tr('Tags verwalten'), content_width=440, content_height=520)
        self.backend, self.pid, self.on_changed = backend, str(product_id), on_changed
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title=GLib.markup_escape_text(entry.title if entry else self.pid),
                                          description=tr('Tags zuweisen'))
        self.page.add(self.group)
        new = Adw.PreferencesGroup()
        self.new_row = Adw.EntryRow(title=tr('Neu erstellen …'), show_apply_button=True)
        self.new_row.connect("apply", self._create)
        self.new_row.connect("entry-activated", self._create)
        new.add(self.new_row)
        manage = Adw.ButtonRow(title=tr('Alle Tags verwalten'))
        manage.set_start_icon_name("document-edit-symbolic")
        manage.connect("activated", lambda *_: ManageTagsDialog(self.backend, self._all_changed).present(self))
        new.add(manage)
        self.page.add(new)
        tv.set_content(self.page)
        self.set_child(tv)
        self._rows: list[Gtk.Widget] = []
        self._rebuild()

    def _rebuild(self) -> None:
        for r in self._rows:
            self.group.remove(r)
        self._rows.clear()
        mine = set(self.backend.game_settings.get(self.pid).tags)
        tags = lv.all_tags(self.backend.game_settings)
        if not tags:
            r = Adw.ActionRow(title=tr('Noch keine Tags – unten einen neuen erstellen.'))
            self.group.add(r)
            self._rows.append(r)
        for t in tags:
            row = Adw.ActionRow(title=GLib.markup_escape_text(t), activatable=True)
            check = Gtk.CheckButton(active=t in mine, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda b, tag=t: self._toggle(tag, b.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.group.add(row)
            self._rows.append(row)

    def _toggle(self, tag: str, on: bool) -> None:
        lv.set_tag(self.backend.game_settings, self.pid, tag, on)
        self.on_changed(self.pid)

    def _create(self, row: Adw.EntryRow) -> None:
        tag = lv.clean_tag(row.get_text())
        if not tag:
            return
        lv.set_tag(self.backend.game_settings, self.pid, tag, True)
        row.set_text("")
        self._rebuild()
        self.on_changed(self.pid)

    def _all_changed(self) -> None:
        self._rebuild()
        self.on_changed("")


class ManageTagsDialog(Adw.Dialog):
    """Alle Tags verwalten: jede Zeile umbenennen (Eingabe übernehmen) oder löschen (mit Rückfrage)."""

    def __init__(self, backend, on_changed: Callable[[], None]) -> None:
        super().__init__(title=tr('Alle Tags verwalten'), content_width=480, content_height=560)
        self.backend, self.on_changed = backend, on_changed
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(description=tr('Umbenennen und Löschen gilt für alle Spiele, denen der Tag zugewiesen ist.'))
        page.add(self.group)
        tv.set_content(page)
        self.set_child(tv)
        self._rows: list[Gtk.Widget] = []
        self._rebuild()

    def _rebuild(self) -> None:
        for r in self._rows:
            self.group.remove(r)
        self._rows.clear()
        tags = lv.all_tags(self.backend.game_settings)
        if not tags:
            r = Adw.ActionRow(title=tr('Keine Tags'))
            self.group.add(r)
            self._rows.append(r)
        for t in tags:
            n = len(lv.games_with_tag(self.backend.game_settings, t))
            row = Adw.EntryRow(title=(tr('{0} Spiel') if n == 1 else tr('{0} Spiele')).format(n), text=t,
                               show_apply_button=True)
            row.set_tooltip_text(tr('Alle Spiele, denen diese Tags zugewiesen sind.'))
            row.connect("apply", lambda r, old=t: self._rename(old, r.get_text()))
            trash = icon_button("user-trash-symbolic", tr('Löschen'), ["flat"])
            trash.set_valign(Gtk.Align.CENTER)
            trash.connect("clicked", lambda _b, tag=t, cnt=n: self._delete(tag, cnt))
            row.add_suffix(trash)
            self.group.add(row)
            self._rows.append(row)

    def _rename(self, old: str, new: str) -> None:
        if lv.rename_tag(self.backend.game_settings, old, new):
            self.add_toast(Adw.Toast.new(GLib.markup_escape_text(tr('Tag umbenannt: {0}').format(lv.clean_tag(new)))))
            self._rebuild()
            self.on_changed()

    def _delete(self, tag: str, count: int) -> None:
        from .dialogs import confirm_destructive

        def do() -> None:
            lv.delete_tag(self.backend.game_settings, tag)
            self._rebuild()
            self.on_changed()
        confirm_destructive(self, tr('Tag „{0}“ löschen?').format(tag),
                            (tr('Der Tag wird von {0} Spiel entfernt.') if count == 1 else
                             tr('Der Tag wird von {0} Spielen entfernt.')).format(count), tr('Löschen'), do)
