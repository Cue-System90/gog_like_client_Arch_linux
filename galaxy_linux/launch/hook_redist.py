"""Redistributables (DirectX, Visual C++, .NET …) beim ersten Start im Prefix installieren.

Liest die Liste ``redist`` aus dem Installations-Datensatz ``manifests/<id>.json`` (vom Installer geschrieben)
und führt jeden noch nicht erledigten Eintrag einmal im Prefix aus. Erledigte IDs stehen in
``GameSettings.redist_done``; ein fehlendes Programm gilt als erledigt (Verhalten wie bis 0.1.6) – außer bei
nachträglich angelegten Datensätzen (``content/build_records.py``, ``origin``): dort liegen die Installer meist
nicht im Spielordner, sie bleiben offen für eine spätere Abhängigkeits-Verwaltung.
"""
from __future__ import annotations

from ..i18n import tr

import logging
from pathlib import Path

from .. import paths
from ..jsonstore import read_json
from .hooks import LaunchContext, LaunchHook, register_hook

log = logging.getLogger(__name__)


def pending_redists(record: dict, done: list[str]) -> list[dict]:
    """Einträge aus dem Datensatz, die noch nicht installiert wurden."""
    return [r for r in record.get("redist", []) if r.get("path") and r.get("id") not in done]


class RedistHook(LaunchHook):
    name = "redist"
    order = 10
    setup = True

    def __init__(self) -> None:
        self.title = tr('Redistributables')

    def enabled(self, ctx: LaunchContext) -> bool:
        return ctx.is_windows and ctx.gs.install_redist

    def prepare(self, ctx: LaunchContext) -> None:
        from .launcher import split_windows_args, to_posix
        record = read_json(paths.MANIFEST_DIR / f"{ctx.product_id}.json", {})
        redists = pending_redists(record, ctx.gs.redist_done)
        if not redists:
            return
        install_dir = Path(ctx.game.install_path)
        done = list(ctx.gs.redist_done)
        late_record = record.get("origin") == "build-record"
        for r in redists:
            exe = install_dir / to_posix(r["path"])
            if not exe.exists():
                if late_record:
                    log.info("Redistributable %s fehlt im Spielordner (%s) – bleibt offen", r["id"], exe)
                    continue
                done.append(r["id"])
                continue
            ctx.status(tr('Installiere {0} …').format(r.get('name') or r['id']))
            log.info("Redistributable %s wird installiert: %s %s", r["id"], exe, r.get("arguments", ""))
            code = ctx.run_in_prefix([str(exe), *split_windows_args(r.get("arguments", ""))])
            log.info("Redistributable %s beendet mit %s", r["id"], code)
            done.append(r["id"])
            ctx.update_game_settings(redist_done=list(done))
        if done != list(ctx.gs.redist_done):
            ctx.update_game_settings(redist_done=list(done))


register_hook(RedistHook())
