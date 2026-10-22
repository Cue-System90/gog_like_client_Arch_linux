"""Spielstart wie im Steam-Client: Kompatibilitätstool pro Spiel, Prefix, Startoptionen, Spielzeit.

Unterstützte Wege für Windows-Spiele:
  * umu-launcher (Standard auf CachyOS): ``WINEPREFIX PROTONPATH GAMEID STORE umu-run spiel.exe``
    – nötig für SLR-Protons wie proton-cachyos-slr, bringt protonfixes mit.
  * Proton direkt: ``STEAM_COMPAT_DATA_PATH … proton waitforexitandrun spiel.exe``
  * Wine: ``WINEPREFIX … wine spiel.exe``
Linux-native Spiele starten direkt (start.sh).
"""
from __future__ import annotations

from ..i18n import tr

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..settings import (DEFAULT_EXTERNAL_DIR, CustomLibrary, GameSettings, GameSettingsStore, GlobalSettings, InstalledGame,
                        InstalledRegistry, PlaytimeStore)
from . import compat, hooks as launch_hooks, launch_options
from .compat import CompatTool
from .hooks import ConsoleBridge, LaunchContext

log = logging.getLogger(__name__)


class LaunchError(Exception):
    pass


@dataclass
class PlayTask:
    name: str
    path: str = ""
    arguments: str = ""
    working_dir: str = ""
    is_primary: bool = False
    category: str = "game"
    type: str = "FileTask"
    link: str = ""
    languages: list[str] = field(default_factory=lambda: ["*"])

    @property
    def is_file(self) -> bool:
        return self.type == "FileTask" and bool(self.path)


def to_posix(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("/")


def split_windows_args(s: str) -> list[str]:
    """Argumente im Windows-Stil zerlegen (Anführungszeichen beachten, Backslashes behalten)."""
    s = (s or "").strip()
    if not s:
        return []
    out, cur, quote = [], "", False
    for ch in s:
        if ch == '"':
            quote = not quote
        elif ch.isspace() and not quote:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- goggame-*.info
def find_game_info(install_dir: Path, product_id: str) -> Path | None:
    for cand in (install_dir / f"goggame-{product_id}.info",
                 install_dir / "game" / f"goggame-{product_id}.info",
                 install_dir / "data" / "noarch" / "game" / f"goggame-{product_id}.info"):
        if cand.is_file():
            return cand
    # Fallback: irgendeine goggame-*.info (z. B. bei abweichender rootGameId)
    for sub in (install_dir, install_dir / "game"):
        try:
            for f in sub.glob("goggame-*.info"):
                return f
        except OSError:
            continue
    return None


def read_game_info(install_dir: Path, product_id: str) -> dict | None:
    f = find_game_info(install_dir, product_id)
    if not f:
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def play_tasks(info: dict | None) -> list[PlayTask]:
    out: list[PlayTask] = []
    for t in (info or {}).get("playTasks") or []:
        out.append(PlayTask(
            name=str(t.get("name") or t.get("path") or tr('Start')),
            path=str(t.get("path") or ""),
            arguments=str(t.get("arguments") or ""),
            working_dir=str(t.get("workingDir") or ""),
            is_primary=bool(t.get("isPrimary", False)),
            category=str(t.get("category") or "game"),
            type=str(t.get("type") or "FileTask"),
            link=str(t.get("link") or ""),
            languages=list(t.get("languages") or ["*"]),
        ))
    return out


def file_tasks(info: dict | None) -> list[PlayTask]:
    return [t for t in play_tasks(info) if t.is_file]


def primary_task(tasks: list[PlayTask]) -> PlayTask | None:
    files = [t for t in tasks if t.is_file]
    for t in files:
        if t.is_primary:
            return t
    for t in files:
        if t.category == "game":
            return t
    return files[0] if files else None


def guess_executable(install_dir: Path) -> PlayTask | None:
    """Ohne goggame-Info: plausibelste .exe im Installationsordner wählen."""
    skip = re.compile(r"(unins|setup|redist|vcredist|dxsetup|crash|report|launcher_helper|__)", re.I)
    cands: list[tuple[int, Path]] = []
    for depth, pattern in ((0, "*.exe"), (1, "*/*.exe"), (2, "*/*/*.exe")):
        for p in install_dir.glob(pattern):
            if skip.search(p.name):
                continue
            try:
                cands.append((depth * 10**12 - p.stat().st_size, p))
            except OSError:
                continue
    if not cands:
        return None
    cands.sort()
    p = cands[0][1]
    return PlayTask(name=p.stem, path=str(p.relative_to(install_dir)), working_dir=str(p.parent.relative_to(install_dir)), is_primary=True)


# --------------------------------------------------------------------------- Plan & Prozess
@dataclass
class LaunchPlan:
    """Startbefehl aus geordneten Slots; ``argv`` setzt sie zusammen:

    ``[*user_wrappers, *compositor, *tool_wrappers, *wrappers, *base, *args, *game_args_suffix]``

    * user_wrappers – Wrapper aus den Startoptionen vor ``%command%``
    * compositor    – ``gamescope … --``
    * tool_wrappers – Werkzeug-Wrapper hinter dem Compositor (z. B. GOverlay ``<Profil>/bgmod``)
    * wrappers      – Schalter des Clients (``gamemoderun``, ``mangohud``)
    * base          – ``umu-run``/``proton …``/``wine`` + Startdatei + Argumente des playTasks
    * args          – Argumente aus den Startoptionen nach ``%command%``
    * game_args_suffix – ganz am Ende, z. B. ``/WineDetectionEnabled:False``
    """
    env: dict[str, str]
    cwd: str
    tool: CompatTool | None
    prefix: str
    runner: str                  # umu | proton | wine | native
    description: str
    base: list[str] = field(default_factory=list)
    user_wrappers: list[str] = field(default_factory=list)
    compositor: list[str] = field(default_factory=list)
    tool_wrappers: list[str] = field(default_factory=list)
    wrappers: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    game_args_suffix: list[str] = field(default_factory=list)
    exe: str = ""                # absolute Startdatei ("" bei command_override)

    @property
    def argv(self) -> list[str]:
        return [*self.user_wrappers, *self.compositor, *self.tool_wrappers, *self.wrappers, *self.base,
                *self.args, *self.game_args_suffix]

    @argv.setter
    def argv(self, value: list[str]) -> None:
        """Ganzen Befehl ersetzen: landet in ``base``, alle anderen Slots werden geleert."""
        self.base = list(value)
        self.user_wrappers, self.compositor, self.tool_wrappers, self.wrappers = [], [], [], []
        self.args, self.game_args_suffix = [], []

    def shell_preview(self) -> str:
        keys = ("WINEPREFIX", "PROTONPATH", "GAMEID", "STORE", "PROTON_VERB", "STEAM_COMPAT_DATA_PATH",
                "MANGOHUD", "ENABLE_VKBASALT", "PROTON_LOG", "DXVK_HUD", "PROTONFIXES_DISABLE")
        env = " ".join(f"{k}={shlex.quote(self.env[k])}" for k in keys if k in self.env and self.env[k] != os.environ.get(k))
        extra = {k: v for k, v in self.env.items() if k not in keys and os.environ.get(k) != v and not k.startswith("STEAM_COMPAT")}
        env2 = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(extra.items())[:8])
        return " ".join(x for x in (env, env2, shlex.join(self.argv)) if x)


@dataclass
class RunningGame:
    product_id: str
    process: subprocess.Popen
    started_at: float
    plan: LaunchPlan
    log_file: Path
    ctx: LaunchContext | None = None
    finishing: bool = False       # Prozess beendet, Nachbereitung (Spielzeit, Hooks) läuft noch

    def poll(self) -> int | None:
        return self.process.poll()

    def terminate(self) -> None:
        try:
            os.killpg(self.process.pid, 15)
        except (ProcessLookupError, PermissionError):
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass


class Launcher:
    def __init__(self, settings: GlobalSettings, game_settings: GameSettingsStore, registry: InstalledRegistry,
                 playtime: PlaytimeStore, api=None) -> None:
        self.settings = settings
        self.game_settings = game_settings
        self.registry = registry
        self.playtime = playtime
        self.api = api
        self._tools: list[CompatTool] | None = None
        self._running: dict[str, RunningGame] = {}
        self._starting: set[str] = set()      # Start vorbereitet (Hooks laufen), Prozess noch nicht gestartet
        self._lock = threading.Lock()
        # Rückfragen/Meldungen der Launch-Hooks: Terminal (CLI) bzw. ui/bridge.GtkBridge (Oberfläche)
        self.ui_bridge = ConsoleBridge()

    # ------------------------------------------------------------------ Tools
    def tools(self, refresh: bool = False) -> list[CompatTool]:
        if self._tools is None or refresh:
            self._tools = compat.discover()
        return self._tools

    def global_default_tool(self) -> CompatTool | None:
        if self.settings.default_compat_tool and self.settings.default_compat_tool != "auto":
            t = compat.resolve(self.settings.default_compat_tool, self.tools())
            if t:
                return t
        return compat.default_tool(self.tools(), self.settings.prefer_umu)

    def library_of(self, game: InstalledGame | str | None) -> CustomLibrary | None:
        """Eigene Bibliothek eines externen Spiels (Zuordnung, sonst nach Ordner)."""
        if isinstance(game, str):
            game = self.registry.get(game)
        if game is None or game.method != "external":
            return None
        return self.settings.library(game.library) or self.settings.library_for_path(game.install_path)

    def default_tool(self, product_id: str | None = None) -> tuple[CompatTool | None, str]:
        """Tool, das ohne eigene Auswahl des Spiels gilt, und woher es kommt (für „Vorgabe (…)“ in der Oberfläche)."""
        lib = self.library_of(product_id) if product_id else None
        if lib is not None and lib.compat_tool:
            t = compat.resolve(lib.compat_tool, self.tools())
            if t:
                return t, tr('Vorgabe der Bibliothek „{0}“').format(lib.name)
            log.warning("Kompatibilitätstool %s der Bibliothek %s nicht gefunden, nutze globale Vorgabe", lib.compat_tool, lib.name)
        return self.global_default_tool(), tr('Globale Vorgabe')

    def resolve_tool(self, gs: GameSettings, platform: str, product_id: str | None = None) -> CompatTool | None:
        if platform != "windows":
            return None
        if gs.compat_tool and gs.compat_tool != compat.NATIVE.id:
            t = compat.resolve(gs.compat_tool, self.tools())
            if t:
                return t
            log.warning("Kompatibilitätstool %s nicht gefunden, nutze Vorgabe", gs.compat_tool)
        return self.default_tool(product_id)[0]

    def prefix_for(self, product_id: str, gs: GameSettings | None = None) -> Path:
        gs = gs or self.game_settings.get(product_id)
        if gs.prefix:
            return Path(os.path.expanduser(gs.prefix))
        game = self.registry.get(product_id)
        if game is not None and game.method == "external":
            # externe Spiele: ein Prefix je Spiel im Prefix-Ordner der Bibliothek, benannt wie der Spielordner
            lib = self.library_of(game)
            root = lib.prefix_root if lib is not None else DEFAULT_EXTERNAL_DIR / "prefixes"
            return root / Path(game.install_path).name
        return Path(os.path.expanduser(self.settings.prefix_dir)) / str(product_id)

    def wayland_mode(self, product_id: str, gs: GameSettings | None = None) -> str:
        """Wayland-Treiber an ("on") oder aus ("off") – Spiel vor Bibliothek vor globaler Vorgabe."""
        gs = gs or self.game_settings.get(product_id)
        lib = self.library_of(product_id)
        mode = gs.wayland_driver or (lib.wayland_driver if lib is not None else "")
        if mode in ("on", "off"):
            return mode
        return "on" if self.settings.wayland_driver else "off"

    def runner_for(self, tool: CompatTool) -> str:
        if tool.kind == "umu-latest":
            return "umu"
        if tool.kind == "wine":
            return "wine"
        if tool.kind == "proton":
            if compat.umu_available() and (self.settings.prefer_umu or compat.tool_needs_umu(tool)):
                return "umu"
            return "proton"
        return "native"

    # ------------------------------------------------------------------ Plan
    def tasks_for(self, game: InstalledGame) -> list[PlayTask]:
        install_dir = Path(game.install_path)
        if game.method == "external":
            from ..content import external
            cands = [c.rel for c in external.exe_candidates(install_dir, game.title)]
            if game.executable and game.executable not in cands and (install_dir / to_posix(game.executable)).exists():
                cands.insert(0, game.executable)
            primary = game.executable if game.executable in cands else (cands[0] if cands else "")
            return [PlayTask(name=c, path=c, is_primary=(c == primary)) for c in cands]
        info = read_game_info(install_dir, game.product_id)
        tasks = file_tasks(info)
        if game.platform == "linux":
            if game.executable:
                tasks.insert(0, PlayTask(name=tr('Start (start.sh)'), path=game.executable, is_primary=True))
            elif (install_dir / "start.sh").exists():
                tasks.insert(0, PlayTask(name=tr('Start (start.sh)'), path="start.sh", is_primary=True))
        if not tasks and game.platform == "windows":
            g = guess_executable(install_dir)
            if g:
                tasks.append(g)
        return tasks

    def select_task(self, game: InstalledGame, gs: GameSettings, task_name: str | None = None) -> PlayTask:
        tasks = self.tasks_for(game)
        if not tasks:
            raise LaunchError(tr('Keine startbare Datei gefunden (goggame-*.info fehlt und keine .exe erkannt).'))
        wanted = task_name or gs.play_task
        if wanted:
            for t in tasks:
                if t.name == wanted:
                    return t
        return primary_task(tasks) or tasks[0]

    def _base_env(self, gs: GameSettings) -> dict[str, str]:
        env = os.environ.copy()
        for k in ("LD_PRELOAD",):
            env.pop(k, None)
        env.update(self.settings.env)
        env.update(gs.env)
        return env

    def build_plan(self, product_id: str, task: PlayTask | None = None, extra_args: list[str] | None = None,
                   command_override: list[str] | None = None) -> LaunchPlan:
        game = self.registry.get(product_id)
        if not game:
            raise LaunchError(tr('Spiel ist nicht installiert.'))
        gs = self.game_settings.get(product_id)
        install_dir = Path(game.install_path)
        if not install_dir.is_dir():
            raise LaunchError(tr('Installationsordner fehlt: {0}').format(install_dir))
        env = self._base_env(gs)
        prefix = self.prefix_for(product_id, gs)
        lib = self.library_of(game)

        exe_path = ""
        if command_override:
            base = list(command_override)
            cwd = str(install_dir)
            tool: CompatTool | None = None
            runner = "native"
        else:
            task = task or self.select_task(game, gs)
            exe = install_dir / to_posix(task.path)
            if game.platform == "linux" and task.path.endswith("start.sh") and not exe.exists():
                exe = install_dir / "start.sh"
            if not exe.exists():
                raise LaunchError(tr('Startdatei nicht gefunden: {0}').format(exe))
            exe_path = str(exe)
            if task.working_dir:
                cwd_path = install_dir / to_posix(task.working_dir)
                cwd = str(cwd_path if cwd_path.is_dir() else exe.parent)
            else:
                cwd = str(exe.parent)
            args = split_windows_args(task.arguments) + list(extra_args or [])
            tool = self.resolve_tool(gs, game.platform, product_id)
            runner = "native"
            base = [str(exe), *args]

            if game.platform == "windows":
                if tool is None:
                    raise LaunchError(tr('Kein Proton/Wine gefunden. Bitte proton-cachyos, umu-launcher oder wine installieren.'))
                runner = self.runner_for(tool)
                if runner in ("umu", "proton") and "PROTON_ENABLE_WAYLAND" not in gs.env:
                    # Wayland-Treiber: Spiele, die im Fenstermodus starten und erst dann auf Vollbild wechseln, behalten
                    # unter XWayland den weißen Wine-Fensterrahmen im Bild (z. B. Surviving Mars Relaunched).
                    if self.wayland_mode(product_id, gs) == "on":
                        env["PROTON_ENABLE_WAYLAND"] = "1"
                    elif gs.wayland_driver == "off" or (lib is not None and lib.wayland_driver == "off"):
                        env["PROTON_ENABLE_WAYLAND"] = "0"
                if runner == "umu":
                    umu = compat.umu_path()
                    if not umu:
                        raise LaunchError(tr('umu-run nicht gefunden (Paket umu-launcher).'))
                    env["WINEPREFIX"] = str(prefix)
                    env["PROTONPATH"] = tool.path
                    env["GAMEID"] = gs.umu_game_id or f"umu-{product_id}"
                    env["STORE"] = "none" if str(product_id).startswith("ext-") else "gog"
                    env["PROTON_VERB"] = gs.proton_verb or "waitforexitandrun"
                    if gs.disable_protonfixes:
                        env["PROTONFIXES_DISABLE"] = "1"
                    base = [umu, str(exe), *args]
                elif runner == "proton":
                    proton = Path(tool.path) / "proton"
                    verb = gs.proton_verb or "waitforexitandrun"
                    env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
                    root = compat.steam_root()
                    if root is None:
                        root = Path(os.path.expanduser(self.settings.prefix_dir)) / ".steam-client-stub"
                    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(root)
                    env.setdefault("STEAM_COMPAT_APP_ID", "0")
                    env.setdefault("SteamAppId", "0")
                    env.setdefault("SteamGameId", f"gog-{product_id}")
                    env["STEAM_COMPAT_INSTALL_PATH"] = str(install_dir)
                    if tool.requires_runtime:
                        rt = compat.find_runtime(tool.requires_runtime)
                        if rt is None:
                            raise LaunchError(
                                tr('{0} benötigt die Steam Linux Runtime (AppID {1}). Bitte umu-launcher installieren oder ein anderes Proton wählen.').format(tool.name, tool.requires_runtime))
                        base = [str(rt / "_v2-entry-point"), f"--verb={verb}", "--", str(proton), verb, str(exe), *args]
                    else:
                        base = [str(proton), verb, str(exe), *args]
                elif runner == "wine":
                    pfx = prefix / "pfx"
                    env["WINEPREFIX"] = str(pfx if pfx.exists() else prefix)
                    env.setdefault("WINEDEBUG", "-all")
                    base = [tool.path, str(exe), *args]

        # Schalter wie in GOverlay/Steam
        if gs.use_vkbasalt:
            env["ENABLE_VKBASALT"] = "1"
        compositor: list[str] = []
        wrappers: list[str] = []
        if gs.use_gamescope and shutil.which("gamescope"):
            compositor = ["gamescope", *shlex.split(gs.gamescope_args or ""), "--"]
        if gs.use_gamemode and shutil.which("gamemoderun"):
            wrappers.append("gamemoderun")
        if gs.use_mangohud and shutil.which("mangohud"):
            wrappers.append("mangohud")

        # Startoptionen des Spiels, sonst die Vorgabe seiner Bibliothek; die globalen kommen immer davor
        game_opts = gs.launch_options or (lib.launch_options if lib is not None else "")
        opts_text = launch_options.merge(self.settings.default_launch_options, game_opts) \
            if (self.settings.default_launch_options and game_opts) \
            else (game_opts or self.settings.default_launch_options)
        opts = launch_options.parse(opts_text)
        env.update(opts.env)
        desc = f"{runner}" + (f" · {tool.name}" if tool else "")
        return LaunchPlan(env=env, cwd=cwd, tool=tool, prefix=str(prefix), runner=runner, description=desc,
                          base=base, user_wrappers=list(opts.wrapper), compositor=compositor, wrappers=wrappers,
                          args=list(opts.args), exe=exe_path)

    # ------------------------------------------------------------------ Start
    def is_running(self, product_id: str) -> bool:
        """Läuft das Spiel noch – oder seine Nachbereitung (z. B. Cloud-Upload nach dem Beenden)?"""
        if str(product_id) in self._starting:
            return True
        rg = self._running.get(str(product_id))
        return rg is not None and (rg.finishing or rg.poll() is None)

    def running(self, product_id: str) -> RunningGame | None:
        return self._running.get(str(product_id))

    def launch(self, product_id: str, task: PlayTask | None = None, extra_args: list[str] | None = None,
               on_exit: Callable[[str, int, int], None] | None = None,
               status_cb: Callable[[str], None] | None = None) -> RunningGame:
        product_id = str(product_id)
        with self._lock:
            if self.is_running(product_id):
                raise LaunchError(tr('Das Spiel läuft bereits.'))
            self._starting.add(product_id)
        try:
            return self._launch(product_id, task, extra_args, on_exit, status_cb)
        finally:
            with self._lock:
                self._starting.discard(product_id)

    def _launch(self, product_id: str, task: PlayTask | None, extra_args: list[str] | None,
                on_exit: Callable[[str, int, int], None] | None, status_cb: Callable[[str], None] | None) -> RunningGame:
        game = self.registry.get(product_id)
        if game is None:
            raise LaunchError(tr('Spiel ist nicht installiert.'))

        plan = self.build_plan(product_id, task, extra_args)
        if game.platform == "windows":
            Path(plan.prefix).mkdir(parents=True, exist_ok=True)
        log_file = paths.game_log_file(product_id)
        ctx = self.make_context(product_id, plan, status_cb, log_file)
        launch_hooks.load_builtin_hooks()
        launch_hooks.run_adjust(ctx)           # Startbefehl ergänzen (z. B. GOverlay-Profil)
        launch_hooks.run_prepare(ctx)          # Redistributables, Galaxy-Dienste, Cloud …
        plan, gs = ctx.plan, ctx.gs
        if status_cb:
            status_cb(tr('Starte …'))
        if gs.pre_launch.strip():
            self._run_hook(gs.pre_launch, plan, log_file)
        stub = plan.env.get("STEAM_COMPAT_CLIENT_INSTALL_PATH")
        if stub and stub.endswith(".steam-client-stub"):
            Path(stub).mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as lf:
            lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} Start ({plan.description}) =====\n".encode())
            lf.write((plan.shell_preview() + "\n").encode())
            lf.flush()
            try:
                proc = subprocess.Popen(plan.argv, env=plan.env, cwd=plan.cwd, stdout=lf, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                raise LaunchError(tr('Start fehlgeschlagen: {0}').format(exc)) from exc
        log.info("Spiel %s gestartet (PID %d) via %s: %s", product_id, proc.pid, plan.description, plan.shell_preview())
        rg = RunningGame(product_id, proc, time.time(), plan, log_file, ctx)
        with self._lock:
            self._running[product_id] = rg
        threading.Thread(target=self._watch, args=(rg, gs, on_exit), name=f"gl-watch-{product_id}", daemon=True).start()
        return rg

    def _watch(self, rg: RunningGame, gs: GameSettings, on_exit) -> None:
        code = rg.process.wait()
        rg.finishing = True               # bis zum Ende der Nachbereitung gilt das Spiel als laufend
        ended = time.time()
        minutes = int(round((ended - rg.started_at) / 60.0))
        try:
            log.info("Spiel %s beendet: Exit-Code %s nach %.0f s", rg.product_id, code, ended - rg.started_at)
            self.playtime.add_session(rg.product_id, minutes, ended)
            with open(rg.log_file, "ab") as lf:
                lf.write(f"===== Ende, Exit-Code {code}, {minutes} Min. =====\n".encode())
            if gs.post_exit.strip():
                try:
                    self._run_hook(gs.post_exit, rg.plan, rg.log_file)
                except Exception as exc:  # pragma: no cover
                    log.warning("post_exit fehlgeschlagen: %s", exc)
            if self.api is not None and self.settings.sync_playtime and minutes > 0 and not str(rg.product_id).startswith("ext-"):
                try:
                    self.api.post_playtime(rg.product_id, minutes, ended)
                except Exception as exc:  # pragma: no cover
                    log.warning("Spielzeit-Sync fehlgeschlagen: %s", exc)
            if rg.ctx is not None:
                # z. B. Cloud-Upload – kein Neustart mitten im Upload (is_running bleibt bis hier wahr)
                launch_hooks.run_after_exit(rg.ctx, code, minutes)
        except Exception:  # pragma: no cover
            log.exception("Nachbereitung von %s fehlgeschlagen", rg.product_id)
        finally:
            with self._lock:
                if self._running.get(rg.product_id) is rg:
                    self._running.pop(rg.product_id, None)
            rg.finishing = False
        if on_exit:
            on_exit(rg.product_id, code, minutes)

    # ------------------------------------------------------------------ Launch-Hooks
    def make_context(self, product_id: str, plan: LaunchPlan | None, status_cb: Callable[[str], None] | None = None,
                     log_file: Path | None = None, prepare_only: bool = False, preview: bool = False) -> LaunchContext:
        """Kontext für die Launch-Hooks (siehe launch/hooks.py)."""
        product_id = str(product_id)
        game = self.registry.get(product_id)
        if game is None:
            raise LaunchError(tr('Spiel ist nicht installiert.'))
        gs = self.game_settings.get(product_id)
        if plan is not None:
            prefix, runner, tool = Path(plan.prefix), plan.runner, plan.tool
            exe = Path(plan.exe) if plan.exe else None
        else:
            tool = self.resolve_tool(gs, game.platform, product_id)
            runner = self.runner_for(tool) if tool is not None else "native"
            prefix, exe = self.prefix_for(product_id, gs), None
        def status(text: str) -> None:
            if status_cb:
                status_cb(text)

        def ask(title: str, body: str, options: list[tuple[str, str]], default_id: str) -> str:
            try:
                return self.ui_bridge.ask(title, body, options, default_id)
            except Exception as exc:
                log.warning("Rückfrage „%s“ fehlgeschlagen: %s", title, exc)
                return default_id

        return LaunchContext(product_id=product_id, game=game, gs=gs, settings=self.settings, plan=plan,
                             install_dir=Path(game.install_path), exe=exe, prefix=prefix, runner=runner, tool=tool,
                             log_file=log_file or paths.game_log_file(product_id), status=status, ask=ask,
                             launcher=self, api=self.api, prepare_only=prepare_only, preview=preview)

    def preview_plan(self, product_id: str, task: PlayTask | None = None, extra_args: list[str] | None = None) -> LaunchPlan:
        """Startbefehl wie beim echten Start, inklusive ``adjust_plan`` der Hooks, aber ohne etwas auszuführen –
        für Befehlsvorschau und ``launch --dry-run`` (darf im Hauptthread laufen)."""
        plan = self.build_plan(product_id, task, extra_args)
        ctx = self.make_context(product_id, plan, preview=True)
        launch_hooks.load_builtin_hooks()
        launch_hooks.run_adjust(ctx)
        return ctx.plan

    def run_prepare_only(self, product_id: str, status_cb: Callable[[str], None] | None = None) -> LaunchContext:
        """Nur die Prefix-Einrichtung (Hooks mit ``setup = True``) ausführen, ohne das Spiel zu starten –
        für „Setup erneut ausführen“. ``ctx.plan`` ist ``None``, wenn kein Startbefehl gebaut werden kann."""
        product_id = str(product_id)
        with self._lock:
            if self.is_running(product_id):
                raise LaunchError(tr('Das Spiel läuft bereits.'))
            self._starting.add(product_id)
        try:
            game = self.registry.get(product_id)
            if game is None:
                raise LaunchError(tr('Spiel ist nicht installiert.'))
            try:
                plan: LaunchPlan | None = self.build_plan(product_id)
            except LaunchError as exc:
                log.info("Setup %s ohne Startbefehl: %s", product_id, exc)
                plan = None
            ctx = self.make_context(product_id, plan, status_cb, prepare_only=True)
            if ctx.is_windows:
                if ctx.tool is None:
                    raise LaunchError(tr('Kein Proton/Wine verfügbar.'))
                ctx.prefix.mkdir(parents=True, exist_ok=True)
            launch_hooks.load_builtin_hooks()
            ran = launch_hooks.run_prepare(ctx, setup_only=True)
            log.info("Setup %s ausgeführt: %s", product_id, ", ".join(ran) or "-")
            return ctx
        finally:
            with self._lock:
                self._starting.discard(product_id)

    def _run_hook(self, cmd: str, plan: LaunchPlan, log_file: Path) -> None:
        with open(log_file, "ab") as lf:
            lf.write(f"--- Hook: {cmd}\n".encode())
            subprocess.run(["/bin/sh", "-c", cmd], env=plan.env, cwd=plan.cwd, stdout=lf, stderr=subprocess.STDOUT, timeout=600)

    # ------------------------------------------------------------------ Im Prefix ausführen (Redists, Winetricks)
    def prefix_command(self, product_id: str, exe_argv: list[str], verb: str = "waitforexitandrun",
                       extra_env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
        """Befehl + Umgebung, um ein beliebiges Windows-Programm im Prefix des Spiels auszuführen."""
        gs = self.game_settings.get(product_id)
        tool = self.resolve_tool(gs, "windows", product_id)
        if tool is None:
            raise LaunchError(tr('Kein Proton/Wine verfügbar.'))
        env = self._base_env(gs)
        env.update(extra_env or {})
        prefix = self.prefix_for(product_id, gs)
        prefix.mkdir(parents=True, exist_ok=True)
        runner = self.runner_for(tool)
        if runner == "umu":
            env.update({"WINEPREFIX": str(prefix), "PROTONPATH": tool.path, "GAMEID": gs.umu_game_id or f"umu-{product_id}",
                        "STORE": "none" if str(product_id).startswith("ext-") else "gog", "PROTON_VERB": verb})
            return [compat.umu_path() or "umu-run", *exe_argv], env
        if runner == "proton":
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            root = compat.steam_root() or prefix.parent / ".steam-client-stub"
            root.mkdir(parents=True, exist_ok=True)
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(root)
            proton = str(Path(tool.path) / "proton")
            if tool.requires_runtime:
                rt = compat.find_runtime(tool.requires_runtime)
                if rt is None:
                    raise LaunchError(tr('Steam Linux Runtime fehlt – bitte umu-launcher installieren.'))
                return [str(rt / "_v2-entry-point"), f"--verb={verb}", "--", proton, verb, *exe_argv], env
            return [proton, verb, *exe_argv], env
        pfx = prefix / "pfx"
        env["WINEPREFIX"] = str(pfx if pfx.exists() else prefix)
        return [tool.path, *exe_argv], env

    def run_in_prefix(self, product_id: str, exe_argv: list[str], timeout: float | None = 1800,
                      extra_env: dict[str, str] | None = None) -> int:
        argv, env = self.prefix_command(product_id, exe_argv, extra_env=extra_env)
        log_file = paths.game_log_file(product_id)
        with open(log_file, "ab") as lf:
            lf.write(f"--- Im Prefix: {shlex.join(argv)}\n".encode())
            lf.flush()
            try:
                return subprocess.run(argv, env=env, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                lf.write("--- Zeitüberschreitung\n".encode())
                return -1

    def winetricks_command(self, product_id: str, verbs: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
        gs = self.game_settings.get(product_id)
        tool = self.resolve_tool(gs, "windows", product_id)
        if tool is None:
            raise LaunchError(tr('Kein Proton/Wine verfügbar.'))
        env = self._base_env(gs)
        prefix = self.prefix_for(product_id, gs)
        runner = self.runner_for(tool)
        verbs = list(verbs or [])
        if runner == "umu":
            env.update({"WINEPREFIX": str(prefix), "PROTONPATH": tool.path, "GAMEID": gs.umu_game_id or f"umu-{product_id}", "STORE": "none" if str(product_id).startswith("ext-") else "gog"})
            return [compat.umu_path() or "umu-run", "winetricks", *(verbs or ["--gui"])], env
        wt = shutil.which("winetricks")
        if not wt:
            raise LaunchError(tr('winetricks ist nicht installiert.'))
        if runner == "proton":
            files = Path(tool.path) / "files"
            if not files.is_dir():
                files = Path(tool.path) / "dist"
            env["WINE"] = str(files / "bin" / "wine")
            env["WINESERVER"] = str(files / "bin" / "wineserver")
            env["WINEPREFIX"] = str(prefix / "pfx")
            env["PATH"] = f"{files / 'bin'}:{env.get('PATH', '')}"
            env["LD_LIBRARY_PATH"] = f"{files / 'lib64'}:{files / 'lib'}:{env.get('LD_LIBRARY_PATH', '')}"
        else:
            pfx = prefix / "pfx"
            env["WINEPREFIX"] = str(pfx if pfx.exists() else prefix)
            env["WINE"] = tool.path
        return [wt, *(verbs or ["--gui"])], env

    def run_winetricks(self, product_id: str, verbs: list[str] | None = None) -> subprocess.Popen:
        argv, env = self.winetricks_command(product_id, verbs)
        return subprocess.Popen(argv, env=env, start_new_session=True)

    # Redistributables beim ersten Start: launch/hook_redist.py (RedistHook)

    # ------------------------------------------------------------------ Verschiedenes
    def open_path(self, path: Path | str) -> None:
        subprocess.Popen(["xdg-open", str(path)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def prefix_exists(self, product_id: str) -> bool:
        p = self.prefix_for(product_id)
        return (p / "system.reg").exists() or (p / "pfx" / "system.reg").exists()

    def apply_dpi(self, product_id: str, dpi: int | None = None) -> int | None:
        """Setzt LogPixels im Prefix passend zur Desktop-Skalierung (225 % → 216), damit Fenster lesbar sind."""
        from .display import desktop_dpi
        lib = self.library_of(product_id)
        dpi = dpi or (lib.prefix_dpi if lib is not None else 0) or self.settings.prefix_dpi or desktop_dpi()
        if dpi <= 96:
            return None
        rc = self.run_in_prefix(product_id, ["reg", "add", r"HKCU\Control Panel\Desktop", "/v", "LogPixels",
                                             "/t", "REG_DWORD", "/d", str(int(dpi)), "/f"],
                                timeout=300, extra_env={"PROTONFIXES_DISABLE": "1"})
        log.info("Prefix %s: LogPixels=%s (rc %s)", product_id, dpi, rc)
        return dpi

    def create_prefix(self, product_id: str) -> int:
        """Leeren Prefix anlegen (umu: ``umu-run ""``; Proton/Wine: wineboot)."""
        gs = self.game_settings.get(product_id)
        tool = self.resolve_tool(gs, "windows", product_id)
        if tool is None:
            raise LaunchError(tr('Kein Proton/Wine verfügbar.'))
        runner = self.runner_for(tool)
        if runner == "umu":
            return self.run_in_prefix(product_id, [""])
        if runner == "proton":
            argv, env = self.prefix_command(product_id, ["wineboot", "-u"], verb="run")
            return subprocess.run(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        argv, env = self.prefix_command(product_id, ["wineboot", "-u"])
        return subprocess.run([shutil.which("wineboot") or "wineboot", "-u"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
