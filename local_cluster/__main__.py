"""Einstieg: ohne Argumente startet die grafische Oberfläche, sonst die Kommandozeile."""
from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import logsetup, migrate
    moved = migrate.run_early()          # Milkyway/galaxy-linux → local:cluster, vor dem Logging (Logordner im Datenordner)
    quiet = bool(argv) and (argv[0] in ("log", "--version", "-h", "--help", "tools", "search", "list", "whoami", "info", "config", "goverlay-import", "goverlay", "prefix", "login", "logout"))
    logsetup.setup(log_env=not quiet)
    from . import i18n
    lang = i18n.setup()   # vor dem Import von Oberfläche/CLI – deren Modulkonstanten werden beim Import übersetzt
    log = logging.getLogger(__name__)
    log.debug("Sprache der Oberfläche: %s", lang)
    for note in moved:
        log.info("Umzug: %s", note)
    try:
        for p in migrate.run_late():
            log.info("Umzug: %s neu geschrieben", p)
    except Exception:
        log.exception("Verknüpfungen unter dem alten Namen konnten nicht übernommen werden")
    if argv and not argv[0].startswith((migrate.SCHEME, *migrate.LEGACY_SCHEMES)) and argv[0] not in ("--gui", "gui"):
        from .cli import main as cli_main
        return cli_main(argv)
    from .ui.app import run
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
