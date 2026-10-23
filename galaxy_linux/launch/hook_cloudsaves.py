"""Cloud-Spielstände rund um den Spielstart (wie Galaxy: vor jeder Sitzung herunterladen, danach hochladen).

* ``prepare``: Abgleich nur in Richtung Prefix (``download``, Statuszeile „Synchronisiere Cloud-Spielstände …“) –
  lokal neuere Stände bleiben bis nach dem Spielen liegen; bei einem Konflikt fragt ``ctx.ask`` wie Galaxy
  (GOG Galaxy-Cloud / Dieser Computer / Ignorieren) und zeigt, wann beide Seiten zuletzt geändert wurden.
* ``after_exit``: Abgleich nur in Richtung Cloud (``upload``), mit Prüfung des Kontingents (200 MB, Galaxys Dialog
  „Benutzerdaten können für dieses Spiel nicht synchronisiert werden“).
Aktiv für GOG-Windows-Spiele (Linux-Versionen haben keine Cloud-Spielstände), wenn angemeldet und
„Cloud-Spielstände“ global bzw. pro Spiel an ist. Fehler halten den Start nie auf; sie landen im Protokoll, in der
Statuszeile und als Meldung. Logik: ``content/cloudsaves.py``.
"""
from __future__ import annotations

from ..i18n import tr

import logging

from ..content import cloudsaves
from .hooks import LaunchContext, LaunchHook, register_hook

log = logging.getLogger(__name__)


class CloudSavesHook(LaunchHook):
    name = "cloudsaves"
    order = 30

    def __init__(self) -> None:
        self.title = tr('Cloud-Spielstände')

    def enabled(self, ctx: LaunchContext) -> bool:
        if ctx.preview or ctx.prepare_only or not (ctx.is_windows and ctx.is_gog):
            return False
        if ctx.api is None or not getattr(ctx.api, "logged_in", False):
            return False
        return cloudsaves.enabled_for(_current_gs(ctx), ctx.settings)

    def prepare(self, ctx: LaunchContext) -> None:
        res = self.sync(ctx, "download")
        if res is not None and res.state == "busy":
            # „Jetzt hochladen/herunterladen“ läuft noch (evtl. mit offener Rückfrage) – nicht auf halb ersetzten
            # Spielständen starten, sondern warten und danach selbst abgleichen
            ctx.status(tr('Warte auf die laufende Synchronisation der Cloud-Spielstände …'))
            if cloudsaves.wait_idle(ctx.product_id, getattr(ctx, "cancel", None)):
                self.sync(ctx, "download")

    def after_exit(self, ctx: LaunchContext, exit_code: int, minutes: int) -> None:
        pre = ctx.data.get(f"{self.name}:download")
        # ohne erfolgreichen Abgleich vor dem Spielen (offline, Fehler, erst während des Spielens eingeschaltet) ist
        # eine lokal fehlende Datei kein Beleg dafür, dass sie gelöscht wurde
        keep = not isinstance(pre, cloudsaves.SyncResult) or pre.state in ("busy", "unsupported", "cancelled")
        self.sync(ctx, "upload", keep_cloud=keep)

    def sync(self, ctx: LaunchContext, mode: str, keep_cloud: bool = False) -> cloudsaves.SyncResult | None:
        try:
            res = cloudsaves.sync_game(ctx.launcher, ctx.api, ctx.product_id, mode, ask=ctx.ask, status=ctx.status,
                                       prefix=ctx.prefix, runner=ctx.runner, keep_cloud=keep_cloud,
                                       save_timestamps=lambda ts: ctx.update_game_settings(cloud_timestamps=ts))
        except cloudsaves.CloudError as exc:
            log.warning("Cloud-Abgleich %s (%s) fehlgeschlagen: %s", ctx.product_id, mode, exc)
            ctx.log_line(f"--- Cloud-Spielstände ({mode}) fehlgeschlagen: {exc}")
            ctx.status(tr('Cloud-Synchronisierung fehlgeschlagen: {0}').format(exc))
            ctx.notify(tr('Cloud-Synchronisation fehlgeschlagen'))
            return None
        ctx.data[f"{self.name}:{mode}"] = res
        ctx.log_line(f"--- Cloud-Spielstände ({mode}): {res.state} " + ", ".join(
            f"{r.name}={r.done} hoch={r.uploaded} runter={r.downloaded}" for r in res.locations))
        if res.state in ("conflict", "quota", "pending") and res.message:
            ctx.status(res.message)
        elif res.state == "synced" and res.changed:
            ctx.status(res.summary())
            if mode == "upload":
                ctx.notify(tr('Synchronisiert in GOG Galaxy-Cloud'))
        return res


def _current_gs(ctx: LaunchContext):
    """Pro-Spiel-Einstellungen von jetzt – der Schalter kann während des Spielens geändert worden sein (``ctx.gs`` ist
    der Stand beim Start)."""
    store = getattr(ctx.launcher, "game_settings", None)
    if store is not None:
        try:
            return store.get(ctx.product_id)
        except Exception:  # pragma: no cover
            log.debug("Pro-Spiel-Einstellungen nicht lesbar", exc_info=True)
    return ctx.gs


register_hook(CloudSavesHook())
