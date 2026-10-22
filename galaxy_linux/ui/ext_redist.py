"""Oberfläche der GOG-Abhängigkeiten (``content/redist.py``, ``launch/hook_redist.py``):

* Eigenschaften → Kompatibilität: Gruppe „Einrichtung im Prefix“ – Redistributables an/aus, Status je Prefix
  (Skripte und Redistributables mit Exit-Code), „Einrichtung erneut ausführen“ (beim nächsten Start),
* Einstellungen → Kompatibilität: Gruppe „Galaxy-Redistributables“ (gemeinsamer Ordner, „Jetzt aktualisieren“),
* beim Start der Oberfläche: fehlen benötigte Redistributables oder ist der Ordner veraltet, wird der Auftrag
  „Galaxy-Redistributables“ eingereiht (wie Heroics Prüfung beim Start).
"""
from __future__ import annotations

from ..i18n import tr

import logging
from pathlib import Path
from typing import Any

from ..content import build_records, redist
from ..launch import hook_redist
from ..settings import format_bytes
from . import prefs_registry, ui_registry
from .util import Adw, Gtk, icon_button, run_async

log = logging.getLogger(__name__)


def game_group(dlg: Any, pid: str) -> Any:
    """Eigenschaften → Kompatibilität (nur GOG-Windows-Spiele)."""
    backend = dlg.backend
    game = backend.registry.get(pid)
    if not hook_redist._is_gog_windows(game):
        return None
    grp = Adw.PreferencesGroup(title=tr('Einrichtung im Prefix'),
                               description=tr('Wie in GOG Galaxy laufen vor dem ersten Start die Installationsskripte '
                                              'des Spiels und die nötigen Redistributables (DirectX, Visual C++, .NET …) '
                                              'einmal je Prefix.'))
    rec = build_records.load_record(pid) or {}
    deps = ", ".join(rec.get("dependencies") or []) or tr('keine laut Manifest')
    sw = Adw.SwitchRow(title=tr('Redistributables im Prefix installieren'), subtitle=deps, active=dlg.gs.install_redist)
    sw.connect("notify::active", lambda r, _: dlg._save(install_redist=r.get_active()))
    grp.add(sw)
    rows = hook_redist.prefix_status(backend.launcher, pid)
    exp = Adw.ExpanderRow(title=tr('Status im Prefix'), subtitle=redist.summary_text(rows))
    if not rows:
        exp.add_row(Adw.ActionRow(title=tr('Noch nichts eingerichtet')))
    for r in rows:
        exp.add_row(Adw.ActionRow(title=r["title"], subtitle=redist.state_text(r)))
    grp.add(exp)
    again = Adw.ButtonRow(title=tr('Einrichtung erneut ausführen'))
    again.set_start_icon_name("view-refresh-symbolic")

    def rerun(*_: object) -> None:
        hook_redist.request_rerun(backend.launcher, pid)
        dlg.gs = backend.game_settings.get(pid)
        dlg.add_toast(Adw.Toast.new(tr('Die Einrichtung läuft beim nächsten Start erneut.')))
    again.connect("activated", rerun)
    grp.add(again)
    return grp


def global_group(dlg: Any) -> Any:
    """Einstellungen → Kompatibilität: gemeinsamer Ordner der Galaxy-Redistributables."""
    info = redist.cache_info()
    grp = Adw.PreferencesGroup(title=tr('Galaxy-Redistributables'),
                               description=tr('Gemeinsamer Ordner für ISI, Visual C++, DirectX, .NET … – wie in GOG '
                                              'Galaxy einmal für alle Spiele geladen.'))
    path = Adw.ActionRow(title=tr('Ordner'), subtitle=info["path"])
    o = icon_button("folder-open-symbolic", tr('Im Dateimanager öffnen'), ["flat"])
    o.set_valign(Gtk.Align.CENTER)
    o.set_sensitive(Path(info["path"]).is_dir())
    o.connect("clicked", lambda *_: dlg.backend.launcher.open_path(info["path"]))
    path.add_suffix(o)
    grp.add(path)
    loaded = ", ".join(info["installed"]) if info["installed"] else tr('noch nichts geladen')
    grp.add(Adw.ActionRow(title=tr('Geladen ({0})').format(format_bytes(info["size"])), subtitle=loaded))
    btn = Adw.ButtonRow(title=tr('Jetzt aktualisieren'))
    btn.set_start_icon_name("view-refresh-symbolic")

    def update(*_: object) -> None:
        hook_redist.queue_redist_job(dlg.backend)
        dlg.add_toast(Adw.Toast.new(tr('Galaxy-Redistributables werden aktualisiert – siehe Downloads.')))
    btn.connect("activated", update)
    grp.add(btn)
    return grp


def window_hook(win: Any) -> None:
    """Beim Start: Backend merken und im Hintergrund prüfen, ob der gemeinsame Ordner fehlt oder veraltet ist."""
    backend = win.backend
    hook_redist.attach_backend(backend)
    if not backend.settings.install_redist:
        return

    def done(need: bool) -> None:
        if need:
            hook_redist.queue_redist_job(backend)

    def fail(exc: Exception) -> None:
        log.warning("Prüfung der Galaxy-Redistributables fehlgeschlagen: %s", exc)
    run_async(lambda: hook_redist.redist_update_needed(backend), done, fail)


prefs_registry.register_game("compat", game_group, order=50, key="redist")
prefs_registry.register("compat", global_group, order=50, key="redist")
ui_registry.register_window_hook("redist", window_hook, order=50)
