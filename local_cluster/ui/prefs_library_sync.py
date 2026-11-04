"""Einstellungen → Allgemein: automatischer Bibliotheks-Abgleich (Intervall, beim Wechsel ins Fenster)."""
from __future__ import annotations

from ..i18n import tr

from . import prefs_registry
from .util import Adw

DEFAULT_INTERVAL = 10


def build_group(dlg) -> Adw.PreferencesGroup:
    s = dlg.s
    grp = Adw.PreferencesGroup(title=tr('Abgleich mit GOG'),
                               description=tr('Neue Käufe erscheinen ohne Neustart in der Bibliothek. Von Hand: Zahnrad → „Bibliothek aktualisieren“ (F5).'))
    auto = Adw.ExpanderRow(title=tr('Bibliothek automatisch abgleichen'),
                           subtitle=tr('Die ganze Bibliothek wird nur geladen, wenn GOG eine Änderung meldet.'),
                           show_enable_switch=True)
    auto.set_enable_expansion(s.library_sync_interval > 0)
    auto.set_expanded(s.library_sync_interval > 0)
    spin = Adw.SpinRow.new_with_range(1, 240, 1)
    spin.set_title(tr('Abstand in Minuten'))
    spin.set_value(s.library_sync_interval if s.library_sync_interval > 0 else DEFAULT_INTERVAL)
    spin.connect("notify::value", lambda r, _p: auto.get_enable_expansion() and dlg._save(library_sync_interval=int(r.get_value())))
    auto.add_row(spin)
    auto.connect("notify::enable-expansion",
                 lambda r, _p: dlg._save(library_sync_interval=int(spin.get_value()) if r.get_enable_expansion() else 0))
    grp.add(auto)
    focus = Adw.SwitchRow(title=tr('Beim Wechsel ins Fenster abgleichen'), subtitle=tr('höchstens alle 5 Minuten'),
                          active=s.library_sync_on_focus)
    focus.connect("notify::active", lambda r, _p: dlg._save(library_sync_on_focus=r.get_active()))
    grp.add(focus)
    return grp


prefs_registry.register("general", build_group, order=20, key="library-sync")
