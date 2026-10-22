"""GOverlay-Profil pro Spiel beim Start (siehe ``goverlay_profiles``): ``<Profil>/bgmod`` als Werkzeug-Wrapper,
``STEAM_COMPAT_INSTALL_PATH`` für alle Startwege, bgmod aus der Vorlage kopieren, wenn GOverlay nur ``bgmod.conf``
angelegt hat. Außerdem: vor Prüfen/Reparieren, Aktualisieren, Verschieben und Deinstallieren ``bgmod-uninstaller``
(Backend-Rückruf) und nach Installationen/Verschiebungen den GOverlay-Abgleich der Oberfläche anstoßen.
"""
from __future__ import annotations

from ..i18n import tr

import logging

from . import goverlay_profiles
from .hooks import LaunchContext, LaunchHook, register_hook

log = logging.getLogger(__name__)


class GOverlayHook(LaunchHook):
    name = "goverlay"
    order = 50

    def __init__(self) -> None:
        self.title = tr('GOverlay-Profil')

    def adjust_plan(self, ctx: LaunchContext) -> None:
        goverlay_profiles.adjust_context(ctx)

    def prepare(self, ctx: LaunchContext) -> None:
        goverlay_profiles.prepare_context(ctx)


def _after_job(backend, job) -> None:
    """Nach Installation/Verschiebung: neue Spielordner bekommen ihr Cover (nur, wenn die Oberfläche abgleicht)."""
    if job.state == "done" and (job.kind.startswith("install") or job.kind in ("move", "verify", "update")):
        sync = goverlay_profiles.auto_sync(backend, create=False)
        if sync is not None:
            sync.request(force=True)


register_hook(GOverlayHook())

try:
    from .. import backend as _backend
    _backend.on_before_files_change(goverlay_profiles.undo_bgmod_changes)
    _backend.on_job_finished(_after_job)
except Exception:  # pragma: no cover
    log.exception("GOverlay-Rückrufe konnten nicht angemeldet werden")
