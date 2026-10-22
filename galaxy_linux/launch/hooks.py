"""Erweiterungspunkte rund um den Spielstart (Launch-Hooks).

Ablauf in ``Launcher.launch()`` (im Start-Thread, nie im GTK-Hauptthread):
  1. ``build_plan()`` baut den Startbefehl aus Slots (siehe ``LaunchPlan``),
  2. ``LaunchContext`` wird angelegt,
  3. ``adjust_plan(ctx)`` aller aktiven Hooks – nur ``ctx.plan`` ändern (env, Slots wie ``tool_wrappers``), ohne
     Seiteneffekte; läuft auch für die Befehlsvorschau (``Launcher.preview_plan()``, ``ctx.preview``) im Hauptthread,
  4. ``prepare(ctx)`` aller aktiven Hooks in ``order``-Reihenfolge – sie dürfen ``ctx.plan`` ändern und
     Einrichtungsschritte im Prefix ausführen (dürfen blockieren, z. B. ``ctx.ask(...)``),
  5. Prozessstart; nach dem Ende ``after_exit(ctx, exit_code, minutes)`` aller Hooks (nach Spielzeit-Upload,
     vor dem ``on_exit``-Rückruf – das Spiel gilt bis dahin als laufend).

``Launcher.run_prepare_only()`` führt nur die Hooks mit ``setup = True`` aus (Prefix-Einrichtung, „Setup erneut
ausführen“); dort ist ``ctx.prepare_only`` gesetzt und ``ctx.plan`` kann ``None`` sein (z. B. Startdatei fehlt).
Auch das läuft in einem Arbeits-Thread (Oberfläche: ``run_async``).

Ein Hook, der eine Ausnahme wirft, wird protokolliert und über ``ctx.status`` gemeldet; der Start läuft weiter.
Nur ``LaunchError`` aus ``prepare`` bricht den Start bewusst ab.

Neue Hooks liegen als eigene Module ``galaxy_linux/launch/hook_<name>.py`` und rufen beim Import
``register_hook(MeinHook())`` auf; ``load_builtin_hooks()`` importiert alle diese Module automatisch (beim Anlegen
des ``Backend`` und vor jedem Start). Dort dürfen sie auch ``backend.register_job_kind(...)`` und
``backend.on_job_finished(...)`` aufrufen.

Empfohlene ``order``-Bereiche:
  10–19  Prefix-Einrichtung (Redistributables, ISI-Skripte, Registry)        – meist ``setup = True``
  20–29  Galaxy-Dienste (GalaxyCommunication-Dienst, comet)
  30–39  Cloud-Speicherstände (Download vor dem Start, Upload nach dem Ende)
  50–59  Overlays/Werkzeuge (GOverlay-Profil → ``plan.tool_wrappers``/env)
  90+    Sonstiges
"""
from __future__ import annotations

from ..i18n import tr

import importlib
import logging
import pkgutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from ..settings import GameSettings, GlobalSettings, InstalledGame
    from .compat import CompatTool
    from .launcher import LaunchPlan

log = logging.getLogger(__name__)

AskFn = Callable[[str, str, "list[tuple[str, str]]", str], str]


# --------------------------------------------------------------------------- Rückfragen / Meldungen
class ConsoleBridge:
    """Rückfragen ohne Oberfläche: im Terminal nummerierte Auswahl, sonst die Vorgabe."""

    def ask(self, title: str, body: str, options: list[tuple[str, str]], default_id: str) -> str:
        ids = [o[0] for o in options]
        if not options:
            return default_id
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except (AttributeError, ValueError):
            interactive = False
        if not interactive:
            return default_id
        print(f"\n{title}\n{body}", file=sys.stderr)
        for i, (oid, label) in enumerate(options, 1):
            print(f"  {i}) {label}" + (" *" if oid == default_id else ""), file=sys.stderr)
        try:
            ans = input(tr('Auswahl [Eingabe = Vorgabe]: ')).strip()
        except (EOFError, KeyboardInterrupt):
            return default_id
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return ids[int(ans) - 1]
        return ans if ans in ids else default_id

    def notify(self, text: str) -> None:
        print(text, file=sys.stderr)


# --------------------------------------------------------------------------- Kontext
@dataclass
class LaunchContext:
    product_id: str
    game: "InstalledGame"
    gs: "GameSettings"
    settings: "GlobalSettings"
    plan: "LaunchPlan | None"
    install_dir: Path
    exe: Path | None
    prefix: Path
    runner: str                    # umu | proton | wine | native
    tool: "CompatTool | None"
    log_file: Path
    status: Callable[[str], None]
    ask: AskFn
    data: dict[str, Any] = field(default_factory=dict)    # private Ablage der Hooks, Schlüssel = Hook-Name
    launcher: Any = None
    api: Any = None
    prepare_only: bool = False     # run_prepare_only(): nur Prefix-Einrichtung, kein Spielstart
    preview: bool = False          # preview_plan(): nur adjust_plan(), nichts ausführen

    @property
    def is_windows(self) -> bool:
        return self.game.platform == "windows"

    @property
    def is_gog(self) -> bool:
        """Spiel aus der GOG-Bibliothek (keine eigene Bibliothek/ext-ID)."""
        return self.game.method != "external" and not str(self.product_id).startswith("ext-")

    def update_game_settings(self, **changes: Any) -> "GameSettings":
        """Pro-Spiel-Einstellungen speichern und ``ctx.gs`` aktualisieren."""
        self.gs = self.launcher.game_settings.update(self.product_id, **changes)
        return self.gs

    def run_in_prefix(self, argv: list[str], timeout: float | None = 1800, extra_env: dict[str, str] | None = None) -> int:
        """Windows-Programm im Prefix des Spiels ausführen (Ausgabe ins Spielprotokoll)."""
        return self.launcher.run_in_prefix(self.product_id, argv, timeout=timeout, extra_env=extra_env)

    def notify(self, text: str) -> None:
        bridge = getattr(self.launcher, "ui_bridge", None)
        if bridge is not None:
            try:
                bridge.notify(text)
            except Exception:  # pragma: no cover
                log.debug("Meldung an die Oberfläche fehlgeschlagen", exc_info=True)

    def log_line(self, text: str) -> None:
        """Zeile ins Spielprotokoll schreiben."""
        try:
            with open(self.log_file, "ab") as lf:
                lf.write((text.rstrip("\n") + "\n").encode())
        except OSError:
            pass


# --------------------------------------------------------------------------- Hooks
class LaunchHook:
    """Basisklasse. Unterklassen setzen ``name`` (eindeutig) und ``order`` und überschreiben, was sie brauchen."""
    name: str = "hook"
    title: str = ""                # Anzeigename für Meldungen (Vorgabe: name)
    order: int = 100
    setup: bool = False            # gehört zur Prefix-Einrichtung → läuft auch bei run_prepare_only()

    def enabled(self, ctx: LaunchContext) -> bool:
        return True

    def adjust_plan(self, ctx: LaunchContext) -> None:
        """Nur ``ctx.plan`` anpassen (env, Slots) – schnell, ohne Seiteneffekte; ``ctx.plan`` ist hier nie None."""

    def prepare(self, ctx: LaunchContext) -> None:
        """Vor dem Start (nach ``adjust_plan``): Einrichtung im Prefix, Downloads, Rückfragen."""

    def after_exit(self, ctx: LaunchContext, exit_code: int, minutes: int) -> None:
        """Nach dem Spielende (im Überwachungs-Thread), z. B. Cloud-Upload."""


_hooks: dict[str, LaunchHook] = {}
_hooks_lock = threading.Lock()
_load_lock = threading.RLock()
_loaded = False


def register_hook(hook: LaunchHook) -> LaunchHook:
    """Hook anmelden; ein vorhandener Hook gleichen Namens wird ersetzt."""
    with _hooks_lock:
        _hooks[hook.name] = hook
    return hook


def unregister_hook(name: str) -> None:
    with _hooks_lock:
        _hooks.pop(name, None)


def hooks() -> list[LaunchHook]:
    """Alle angemeldeten Hooks, sortiert nach ``order`` (dann Name)."""
    with _hooks_lock:
        items = list(_hooks.values())
    return sorted(items, key=lambda h: (h.order, h.name))


def load_builtin_hooks() -> None:
    """Importiert einmalig alle Module ``galaxy_linux/launch/hook_*.py`` (sie melden ihre Hooks selbst an)."""
    global _loaded
    with _load_lock:
        if _loaded:
            return
        pkg = sys.modules[__package__]
        for mod in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
            if mod.name.startswith("hook_"):
                try:
                    importlib.import_module(f"{__package__}.{mod.name}")
                except Exception:
                    log.exception("Launch-Hook-Modul %s konnte nicht geladen werden", mod.name)
        _loaded = True


def _active(ctx: LaunchContext, items: list[LaunchHook] | None, setup_only: bool) -> list[LaunchHook]:
    out = []
    for h in (hooks() if items is None else sorted(items, key=lambda h: (h.order, h.name))):
        if setup_only and not h.setup:
            continue
        try:
            if h.enabled(ctx):
                out.append(h)
        except Exception as exc:
            log.warning("Hook %s: enabled() fehlgeschlagen: %s", h.name, exc)
    return out


def run_adjust(ctx: LaunchContext, items: list[LaunchHook] | None = None) -> list[str]:
    """``adjust_plan`` aller aktiven Hooks (ohne ``ctx.plan`` nichts). Fehler werden nur protokolliert."""
    ran: list[str] = []
    if ctx.plan is None:
        return ran
    for h in _active(ctx, items, False):
        try:
            h.adjust_plan(ctx)
            ran.append(h.name)
        except Exception as exc:
            log.warning("Hook %s (Startbefehl) fehlgeschlagen: %s", h.name, exc, exc_info=True)
    return ran


def run_prepare(ctx: LaunchContext, items: list[LaunchHook] | None = None, setup_only: bool = False) -> list[str]:
    """``prepare`` aller aktiven Hooks. Gibt die Namen der ausgeführten Hooks zurück.
    ``LaunchError`` bricht ab (wird weitergereicht), andere Fehler werden gemeldet und übergangen."""
    from .launcher import LaunchError
    ran: list[str] = []
    for h in _active(ctx, items, setup_only):
        try:
            h.prepare(ctx)
            ran.append(h.name)
        except LaunchError:
            log.warning("Hook %s bricht den Start ab", h.name)
            raise
        except Exception as exc:
            log.warning("Hook %s (Vorbereitung) fehlgeschlagen: %s", h.name, exc, exc_info=True)
            ctx.log_line(f"--- Hook {h.name} fehlgeschlagen: {exc}")
            try:
                ctx.status(tr('Vorbereitung „{0}“ fehlgeschlagen: {1}').format(h.title or h.name, exc))
            except Exception:  # pragma: no cover
                pass
    return ran


def run_after_exit(ctx: LaunchContext, exit_code: int, minutes: int, items: list[LaunchHook] | None = None) -> list[str]:
    """``after_exit`` aller aktiven Hooks; Fehler (auch LaunchError) werden nur gemeldet."""
    ran: list[str] = []
    for h in _active(ctx, items, False):
        try:
            h.after_exit(ctx, exit_code, minutes)
            ran.append(h.name)
        except Exception as exc:
            log.warning("Hook %s (nach dem Beenden) fehlgeschlagen: %s", h.name, exc, exc_info=True)
            ctx.log_line(f"--- Hook {h.name} fehlgeschlagen: {exc}")
            try:
                ctx.status(tr('Nachbereitung „{0}“ fehlgeschlagen: {1}').format(h.title or h.name, exc))
            except Exception:  # pragma: no cover
                pass
    return ran
