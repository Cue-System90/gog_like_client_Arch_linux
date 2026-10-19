"""Einstieg: ohne Argumente startet die grafische Oberfläche, sonst die Kommandozeile."""
from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import logsetup
    quiet = bool(argv) and (argv[0] in ("log", "--version", "-h", "--help", "tools", "search", "list", "whoami", "info", "config", "goverlay-import", "prefix", "login", "logout"))
    logsetup.setup(log_env=not quiet)
    from . import i18n
    lang = i18n.setup()   # vor dem Import von Oberfläche/CLI – deren Modulkonstanten werden beim Import übersetzt
    logging.getLogger(__name__).debug("Sprache der Oberfläche: %s", lang)
    if argv and not argv[0].startswith("galaxy-linux://") and argv[0] not in ("--gui", "gui"):
        from .cli import main as cli_main
        return cli_main(argv)
    from .ui.app import run
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
