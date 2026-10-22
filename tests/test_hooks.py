"""Tests für den Startbefehl aus Slots, die Launch-Hooks (Reihenfolge, Fehler-Isolation, Start-Ablauf),
den Redistributable-Hook und die Rückfrage-Brücke ohne Oberfläche."""
from __future__ import annotations

import io
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.launch import compat, hooks
from galaxy_linux.launch.hook_redist import RedistHook, pending_redists
from galaxy_linux.launch.hooks import ConsoleBridge, LaunchContext, LaunchHook
from galaxy_linux.launch.launcher import Launcher, LaunchError, LaunchPlan
from galaxy_linux.settings import GameSettings, GameSettingsStore, GlobalSettings, InstalledGame, InstalledRegistry, PlaytimeStore


class Recorder(LaunchHook):
    def __init__(self, name, order, calls, fail=None, setup=False, enabled=True):
        self.name, self.order, self.calls, self.fail, self.setup, self._enabled = name, order, calls, fail, setup, enabled

    def enabled(self, ctx):
        if self._enabled == "raise":
            raise RuntimeError("kaputt")
        return self._enabled

    def adjust_plan(self, ctx):
        self.calls.append(("adjust", self.name))

    def prepare(self, ctx):
        self.calls.append(("prepare", self.name))
        if self.fail:
            raise self.fail

    def after_exit(self, ctx, exit_code, minutes):
        self.calls.append(("exit", self.name, exit_code, minutes))
        if self.fail:
            raise self.fail


class TempLauncherBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.root = root
        self.patch = mock.patch.multiple("galaxy_linux.paths",
                                         SETTINGS_FILE=root / "settings.json", GAME_SETTINGS_FILE=root / "games.json",
                                         INSTALLED_FILE=root / "installed.json", PLAYTIME_FILE=root / "playtime.json",
                                         MANIFEST_DIR=root / "manifests", LOG_DIR=root / "logs")
        self.patch.start()
        (root / "logs").mkdir()
        (root / "manifests").mkdir()
        self.game_dir = root / "Games" / "Demo"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "game.exe").write_bytes(b"MZ")
        (self.game_dir / "goggame-1.info").write_text(json.dumps({
            "gameId": "1", "playTasks": [{"category": "game", "isPrimary": True, "name": "Demo", "path": "game.exe",
                                          "arguments": "-fullscreen \"-x y\"", "type": "FileTask"}]}))
        self.lin_dir = root / "Games" / "Lin"
        self.lin_dir.mkdir(parents=True)
        (self.lin_dir / "start.sh").write_text("#!/bin/sh\n")
        self.settings = GlobalSettings(install_dir=str(root / "Games"), prefix_dir=str(root / "prefixes"), prefer_umu=True,
                                       sync_playtime=False)
        self.gs = GameSettingsStore()
        self.registry = InstalledRegistry()
        self.registry.set(InstalledGame("1", "Demo", "windows", str(self.game_dir)))
        self.registry.set(InstalledGame("2", "Lin", "linux", str(self.lin_dir), executable="start.sh"))
        self.launcher = Launcher(self.settings, self.gs, self.registry, PlaytimeStore())
        # leere Hook-Liste, keine automatisch geladenen Module
        self.hooks_patch = mock.patch.dict(hooks._hooks, clear=True)
        self.hooks_patch.start()
        self.loaded_patch = mock.patch.object(hooks, "_loaded", True)
        self.loaded_patch.start()

    def tearDown(self):
        self.loaded_patch.stop()
        self.hooks_patch.stop()
        self.patch.stop()
        self.td.cleanup()

    def umu_plan(self, **gs):
        proton = compat.CompatTool("proton:/opt/p", "Proton X", "proton", "/opt/p", "x", "System", requires_runtime="1628350")
        self.gs.set("1", GameSettings(compat_tool=proton.id, **gs))
        with mock.patch.object(compat, "resolve", return_value=proton), \
             mock.patch.object(compat, "umu_available", return_value=True), \
             mock.patch.object(compat, "umu_path", return_value="/usr/bin/umu-run"), \
             mock.patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"):
            return self.launcher.build_plan("1")


# ----------------------------------------------------------------------------- Slots des Startbefehls
class PlanSlotsTest(TempLauncherBase):
    def test_default_argv_unchanged(self):
        plan = self.umu_plan()
        exe = str(self.game_dir / "game.exe")
        self.assertEqual(plan.argv, ["/usr/bin/umu-run", exe, "-fullscreen", "-x y"])
        self.assertEqual(plan.base, plan.argv)
        for slot in (plan.user_wrappers, plan.compositor, plan.tool_wrappers, plan.wrappers, plan.args, plan.game_args_suffix):
            self.assertEqual(slot, [])
        self.assertEqual(plan.exe, exe)

    def test_argv_order_like_0_1_6(self):
        plan = self.umu_plan(launch_options="ENABLE_VKBASALT=1 mangohud %command% -skipintro", use_gamescope=True,
                             gamescope_args="-f -W 1920", use_gamemode=True, use_mangohud=True)
        exe = str(self.game_dir / "game.exe")
        # 0.1.6: [*Startoptionen-Wrapper, gamescope … --, gamemoderun, mangohud, umu-run, exe, Task-Argumente, Startoptionen-Argumente]
        self.assertEqual(plan.argv, ["mangohud", "gamescope", "-f", "-W", "1920", "--", "gamemoderun", "mangohud",
                                     "/usr/bin/umu-run", exe, "-fullscreen", "-x y", "-skipintro"])
        self.assertEqual(plan.user_wrappers, ["mangohud"])
        self.assertEqual(plan.compositor, ["gamescope", "-f", "-W", "1920", "--"])
        self.assertEqual(plan.wrappers, ["gamemoderun", "mangohud"])
        self.assertEqual(plan.args, ["-skipintro"])

    def test_tool_wrappers_and_suffix(self):
        plan = self.umu_plan(use_gamescope=True, gamescope_args="-f", use_gamemode=True)
        plan.tool_wrappers.append("/profile/bgmod")
        plan.game_args_suffix.append("/WineDetectionEnabled:False")
        argv = plan.argv
        self.assertEqual(argv[:4], ["gamescope", "-f", "--", "/profile/bgmod"])
        self.assertEqual(argv[4], "gamemoderun")
        self.assertEqual(argv[-1], "/WineDetectionEnabled:False")
        self.assertIn("/profile/bgmod", plan.shell_preview())

    def test_tool_wrappers_without_compositor_after_user_wrappers(self):
        plan = self.umu_plan(launch_options="strangle 60 %command% -x")
        plan.tool_wrappers = ["/p/bgmod"]
        self.assertEqual(plan.argv[:3], ["strangle", "60", "/p/bgmod"])
        self.assertEqual(plan.argv[-1], "-x")

    def test_argv_setter_replaces_command(self):
        plan = LaunchPlan(env={}, cwd="/", tool=None, prefix="", runner="native", description="native",
                          base=["a"], user_wrappers=["w"], args=["-x"])
        plan.argv = ["x", "y"]
        self.assertEqual(plan.argv, ["x", "y"])
        self.assertEqual((plan.user_wrappers, plan.args, plan.base), ([], [], ["x", "y"]))

    def test_native_plan(self):
        plan = self.launcher.build_plan("2")
        self.assertEqual(plan.argv, [str(self.lin_dir / "start.sh")])
        self.assertEqual(plan.exe, str(self.lin_dir / "start.sh"))


# ----------------------------------------------------------------------------- Hook-Registry
class HookRegistryTest(TempLauncherBase):
    def ctx(self, pid="2", plan="build"):
        p = self.launcher.build_plan(pid) if plan == "build" else plan
        self.status: list[str] = []
        return self.launcher.make_context(pid, p, self.status.append)

    def test_order_and_names(self):
        calls: list = []
        for name, order in (("c", 50), ("a", 10), ("b", 30), ("a2", 10)):
            hooks.register_hook(Recorder(name, order, calls))
        self.assertEqual([h.name for h in hooks.hooks()], ["a", "a2", "b", "c"])
        ran = hooks.run_prepare(self.ctx())
        self.assertEqual(ran, ["a", "a2", "b", "c"])
        self.assertEqual([c[1] for c in calls], ["a", "a2", "b", "c"])
        hooks.register_hook(Recorder("b", 5, calls))          # gleicher Name ersetzt
        self.assertEqual([h.name for h in hooks.hooks()], ["b", "a", "a2", "c"])
        hooks.unregister_hook("b")
        self.assertEqual([h.name for h in hooks.hooks()], ["a", "a2", "c"])

    def test_failing_hook_is_isolated(self):
        calls: list = []
        hooks.register_hook(Recorder("eins", 1, calls))
        hooks.register_hook(Recorder("kaputt", 2, calls, fail=RuntimeError("Fehler X")))
        hooks.register_hook(Recorder("drei", 3, calls))
        hooks.register_hook(Recorder("aus", 4, calls, enabled=False))
        hooks.register_hook(Recorder("enabled-kaputt", 5, calls, enabled="raise"))
        ctx = self.ctx()
        with self.assertLogs("galaxy_linux.launch.hooks", "WARNING"):
            ran = hooks.run_prepare(ctx)
        self.assertEqual(ran, ["eins", "drei"])
        self.assertEqual([c[1] for c in calls], ["eins", "kaputt", "drei"])
        self.assertTrue(any("kaputt" in s and "Fehler X" in s for s in self.status))
        self.assertIn("Hook kaputt fehlgeschlagen: Fehler X", ctx.log_file.read_text())

    def test_launch_error_aborts(self):
        calls: list = []
        hooks.register_hook(Recorder("stopp", 1, calls, fail=LaunchError("bewusst")))
        hooks.register_hook(Recorder("danach", 2, calls))
        with self.assertLogs("galaxy_linux.launch.hooks", "WARNING"), self.assertRaises(LaunchError):
            hooks.run_prepare(self.ctx())
        self.assertEqual([c[1] for c in calls], ["stopp"])

    def test_after_exit_isolated_even_for_launch_error(self):
        calls: list = []
        hooks.register_hook(Recorder("a", 1, calls, fail=LaunchError("x")))
        hooks.register_hook(Recorder("b", 2, calls))
        with self.assertLogs("galaxy_linux.launch.hooks", "WARNING"):
            ran = hooks.run_after_exit(self.ctx(), 3, 7)
        self.assertEqual(ran, ["b"])
        self.assertEqual(calls, [("exit", "a", 3, 7), ("exit", "b", 3, 7)])

    def test_setup_only_and_adjust(self):
        calls: list = []
        hooks.register_hook(Recorder("setup", 1, calls, setup=True))
        hooks.register_hook(Recorder("normal", 2, calls))
        self.assertEqual(hooks.run_prepare(self.ctx(), setup_only=True), ["setup"])
        self.assertEqual(hooks.run_adjust(self.ctx()), ["setup", "normal"])
        self.assertEqual(hooks.run_adjust(self.ctx(plan=None)), [])

    def test_explicit_items(self):
        calls: list = []
        items = [Recorder("z", 2, calls), Recorder("y", 1, calls)]
        self.assertEqual(hooks.run_prepare(self.ctx(), items=items), ["y", "z"])

    def test_context_fields(self):
        ctx = self.ctx("2")
        self.assertEqual(ctx.product_id, "2")
        self.assertEqual(ctx.runner, "native")
        self.assertEqual(ctx.install_dir, self.lin_dir)
        self.assertEqual(ctx.exe, self.lin_dir / "start.sh")
        self.assertFalse(ctx.is_windows)
        self.assertTrue(ctx.is_gog)
        self.assertIs(ctx.launcher, self.launcher)
        ctx.data["x"] = 1
        ctx.update_game_settings(cloud_saves="off")
        self.assertEqual(self.gs.get("2").cloud_saves, "off")
        self.assertEqual(ctx.gs.cloud_saves, "off")
        # ask: Vorgabe der Brücke; Fehler der Brücke → Vorgabe
        self.launcher.ui_bridge = types.SimpleNamespace(ask=mock.Mock(side_effect=RuntimeError("weg")), notify=mock.Mock())
        with self.assertLogs("galaxy_linux.launch.launcher", "WARNING"):
            self.assertEqual(ctx.ask("T", "B", [("a", "A"), ("b", "B")], "b"), "b")
        ctx.notify("Hallo")
        self.launcher.ui_bridge.notify.assert_called_once_with("Hallo")


# ----------------------------------------------------------------------------- Start-Ablauf mit Hooks
class FakeProc:
    pid = 4242

    def __init__(self, code=0):
        self.code = code
        self.released = threading.Event()

    def wait(self):
        self.released.wait(5)
        return self.code

    def poll(self):
        return self.code if self.released.is_set() else None


class LaunchFlowTest(TempLauncherBase):
    def test_launch_runs_hooks_and_composes_argv(self):
        events: list = []
        launcher = self.launcher

        class Overlay(LaunchHook):
            name, order = "overlay", 50

            def adjust_plan(self, ctx):
                ctx.plan.tool_wrappers.append("/p/bgmod")
                ctx.plan.env["MANGOHUD_CONFIGFILE"] = "/p/MangoHud.conf"

            def prepare(self, ctx):
                events.append(("prepare", launcher.is_running(ctx.product_id)))
                ctx.status("Bereite vor")

            def after_exit(self, ctx, code, minutes):
                events.append(("after", code, launcher.is_running(ctx.product_id)))
        hooks.register_hook(Overlay())
        proc = FakeProc(code=3)
        exited = threading.Event()
        status: list[str] = []
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            rg = launcher.launch("2", on_exit=lambda *a: (events.append(("on_exit",) + a), exited.set()), status_cb=status.append)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
        self.assertEqual(argv, ["/p/bgmod", str(self.lin_dir / "start.sh")])
        self.assertEqual(env["MANGOHUD_CONFIGFILE"], "/p/MangoHud.conf")
        self.assertIs(rg.ctx.plan, rg.plan)
        self.assertTrue(launcher.is_running("2"))
        with self.assertRaises(LaunchError):
            launcher.launch("2")                          # läuft bereits
        proc.released.set()
        self.assertTrue(exited.wait(5))
        self.assertEqual(events[0], ("prepare", True))                  # während der Vorbereitung gilt es als laufend
        self.assertEqual(events[1], ("after", 3, True))                 # Nachbereitung: noch „laufend“
        self.assertEqual(events[2][:3], ("on_exit", "2", 3))
        self.assertFalse(launcher.is_running("2"))
        self.assertIn("Bereite vor", status)

    def test_failed_start_releases_lock(self):
        class Boom(LaunchHook):
            name = "boom"

            def prepare(self, ctx):
                raise LaunchError("nein")
        hooks.register_hook(Boom())
        with self.assertLogs("galaxy_linux.launch.hooks", "WARNING"), self.assertRaises(LaunchError):
            self.launcher.launch("2")
        self.assertFalse(self.launcher.is_running("2"))

    def test_preview_plan_only_adjusts(self):
        calls: list = []

        class Wrap(Recorder):
            def adjust_plan(self, ctx):
                super().adjust_plan(ctx)
                self_ctx_preview.append(ctx.preview)
                ctx.plan.tool_wrappers.append("/w")
        self_ctx_preview: list = []
        hooks.register_hook(Wrap("w", 1, calls))
        plan = self.launcher.preview_plan("2")
        self.assertEqual(plan.argv, ["/w", str(self.lin_dir / "start.sh")])
        self.assertEqual(calls, [("adjust", "w")])
        self.assertEqual(self_ctx_preview, [True])

    def test_run_prepare_only(self):
        calls: list = []
        hooks.register_hook(Recorder("setup", 1, calls, setup=True))
        hooks.register_hook(Recorder("cloud", 2, calls))
        ctx = self.launcher.run_prepare_only("2")
        self.assertTrue(ctx.prepare_only)
        self.assertEqual(calls, [("prepare", "setup")])
        self.assertFalse(self.launcher.is_running("2"))
        (self.lin_dir / "start.sh").unlink()                       # ohne Startdatei: plan None, Einrichtung trotzdem
        ctx = self.launcher.run_prepare_only("2")
        self.assertIsNone(ctx.plan)
        with self.assertRaises(LaunchError):
            self.launcher.run_prepare_only("999")


# ----------------------------------------------------------------------------- Prefix-Einrichtung als Hook
class RedistHookTest(TempLauncherBase):
    """Grundsätzliches zum Hook; der Ablauf der Einrichtung steht in tests/test_redist.py."""

    def make_ctx(self, record: dict, pid: str = "1"):
        (self.root / "manifests" / f"{pid}.json").write_text(json.dumps(record))
        fake = types.SimpleNamespace(game_settings=self.gs, run_in_prefix=mock.Mock(return_value=0), ui_bridge=None)
        game = self.registry.get(pid)
        return LaunchContext(product_id=pid, game=game, gs=self.gs.get(pid), settings=self.settings, plan=None,
                             install_dir=Path(game.install_path), exe=None, prefix=self.root / "prefixes" / pid,
                             runner="umu", tool=None, log_file=self.root / "logs" / f"{pid}.log", status=lambda t: None,
                             ask=lambda *a: a[-1], launcher=fake)

    def test_pending_redists_legacy_format(self):
        record = {"redist": [{"id": "DirectX", "path": "__redist/DirectX/DXSETUP.exe"},
                             {"id": "MSVC2017", "path": "__redist/MSVC2017/VC_redist.x86.exe"}, {"id": "x", "path": ""}]}
        self.assertEqual([r["id"] for r in pending_redists(record, ["DirectX"])], ["MSVC2017"])

    def test_enabled_only_for_gog_windows(self):
        self.assertTrue(RedistHook().enabled(self.make_ctx({})))
        self.gs.update("1", install_redist=False)          # schaltet nur die Redistributables ab, nicht die Skripte
        ctx = self.make_ctx({})
        self.assertTrue(RedistHook().enabled(ctx))
        self.assertFalse(RedistHook().enabled(self.make_ctx({}, pid="2")))       # Linux
        self.registry.set(InstalledGame("ext-x", "X", "windows", str(self.game_dir), method="external"))
        self.assertFalse(RedistHook().enabled(self.make_ctx({}, pid="ext-x")))

    def test_without_record_nothing_runs(self):
        ctx = self.make_ctx({})
        with mock.patch.multiple("galaxy_linux.paths", DATA_DIR=self.root / "data"), \
                mock.patch("galaxy_linux.launch.hook_redist.subprocess.run") as run:
            RedistHook().prepare(ctx)
        run.assert_not_called()
        self.assertEqual(self.gs.get("1").setup_done, [])

    def test_registered_as_builtin(self):
        import importlib
        import sys
        sys.modules.pop("galaxy_linux.launch.hook_redist", None)      # Modul neu laden → meldet sich selbst an
        with mock.patch.object(hooks, "_loaded", False):
            hooks.load_builtin_hooks()
            self.assertTrue(hooks._loaded)
        importlib.import_module("galaxy_linux.launch.hook_redist")
        self.assertIn("redist", [h.name for h in hooks.hooks()])
        h = next(h for h in hooks.hooks() if h.name == "redist")
        self.assertTrue(h.setup)
        self.assertEqual(h.order, 10)


# ----------------------------------------------------------------------------- Rückfragen ohne Oberfläche
class ConsoleBridgeTest(unittest.TestCase):
    OPTS = [("cloud", "Cloud"), ("local", "Lokal")]

    def test_not_interactive_returns_default(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(ConsoleBridge().ask("T", "B", self.OPTS, "local"), "local")

    def test_numbered_choice(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stderr", io.StringIO()):
            with mock.patch("builtins.input", return_value="1"):
                self.assertEqual(ConsoleBridge().ask("T", "B", self.OPTS, "local"), "cloud")
            with mock.patch("builtins.input", return_value=""):
                self.assertEqual(ConsoleBridge().ask("T", "B", self.OPTS, "local"), "local")
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertEqual(ConsoleBridge().ask("T", "B", self.OPTS, "cloud"), "cloud")
            with mock.patch("builtins.input", return_value="local"):
                self.assertEqual(ConsoleBridge().ask("T", "B", self.OPTS, "cloud"), "local")

    def test_launcher_default_bridge(self):
        self.assertIsInstance(Launcher(GlobalSettings(), mock.Mock(), mock.Mock(), mock.Mock()).ui_bridge, ConsoleBridge)


if __name__ == "__main__":
    unittest.main()
