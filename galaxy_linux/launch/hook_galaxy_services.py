"""Launch-Hooks für die Galaxy-Dienste (Logik in ``galaxy_services.py``).

* ``galaxy_service`` (order 20, Prefix-Einrichtung): GalaxyCommunication-Platzhalterdienst und Registry-Eintrag
  im Prefix – läuft auch bei „Setup erneut ausführen“ (``galaxy-linux prefix setup``).
* ``comet`` (order 25): comet vor dem Spiel starten (oder einen laufenden mitbenutzen), nach dem Ende beenden.

Aktiv für Windows-GOG-Spiele mit Anmeldung, wenn „Galaxy-Dienste im Spiel (comet)“ für das Spiel gilt
(Eigenschaften → Spielfunktionen vor Einstellungen → Spielfunktionen).
"""
from __future__ import annotations

from ..i18n import tr

import logging

from . import galaxy_services as gsvc
from .hooks import LaunchContext, LaunchHook, register_hook

log = logging.getLogger(__name__)


class GalaxyServiceHook(LaunchHook):
    name = "galaxy_service"
    order = 20
    setup = True

    def __init__(self) -> None:
        self.title = tr('GalaxyCommunication-Dienst')

    def enabled(self, ctx: LaunchContext) -> bool:
        return gsvc.wanted(ctx)

    def prepare(self, ctx: LaunchContext) -> None:
        done = gsvc.setup_dummy_service(ctx)
        if done:
            ctx.log_line(f"--- Galaxy-Dienste eingerichtet: {', '.join(done)}")


class CometHook(LaunchHook):
    name = "comet"
    order = 25

    def __init__(self) -> None:
        self.title = tr('Galaxy-Dienste (comet)')

    def enabled(self, ctx: LaunchContext) -> bool:
        if ctx.data.get(self.name):        # gestartet → auch nach dem Ende wieder abmelden
            return True
        return not ctx.prepare_only and gsvc.wanted(ctx)

    def prepare(self, ctx: LaunchContext) -> None:
        gsvc.start_comet(ctx)

    def after_exit(self, ctx: LaunchContext, exit_code: int, minutes: int) -> None:
        if ctx.data.get(self.name):
            gsvc.COMET.release(ctx.product_id)


register_hook(GalaxyServiceHook())
register_hook(CometHook())
