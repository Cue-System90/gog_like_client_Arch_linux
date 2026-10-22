"""Bibliotheks-Abgleich: Unterschied und NEU-Buchführung mit Ablauf, Prüfsummen-Abkürzung, versteckte Spiele,
Wiederholungen nach einem Kauf, Einzelflug, Backend und Kommandozeile – alles ohne Netz."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from galaxy_linux.api import library_sync as ls
from galaxy_linux.api.client import ApiError, GogApi, NotLoggedIn
from galaxy_linux.api.models import LibraryEntry
from galaxy_linux.i18n import tr
from galaxy_linux.settings import GlobalSettings

DAY = 86400
NOW = 1_800_000_000.0


def entry(pid, title: str = "", hidden: bool = False) -> LibraryEntry:
    return LibraryEntry(id=str(pid), title=title or f"Spiel {pid}", is_hidden=hidden)


def product(pid, title: str = "") -> dict:
    return {"id": int(pid), "title": title or f"Spiel {pid}", "worksOn": {"Windows": True}}


class TempPaths(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        cfg, data, cache = self.base / "cfg", self.base / "data", self.base / "cache"
        self.cfg, self.cache = cfg, cache
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        cfg.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()


# ----------------------------------------------------------------------------- reine Hilfen
class HelperTest(unittest.TestCase):
    def test_games_checksum(self):
        self.assertEqual(ls.games_checksum({"checksum": {"games": "c1fc44f3", "cart": None}}), "c1fc44f3")
        self.assertEqual(ls.games_checksum({"isLoggedIn": False, "checksum": {"games": None}}), "")   # Gast-Daten
        self.assertEqual(ls.games_checksum({"checksum": "kaputt"}), "")
        self.assertEqual(ls.games_checksum(None), "")
        self.assertEqual(ls.games_checksum({}), "")

    def test_should_skip(self):
        self.assertTrue(ls.should_skip("abc", "abc", False, True))
        self.assertFalse(ls.should_skip("abc", "abc", True, True))      # erzwungen
        self.assertFalse(ls.should_skip("abc", "abc", False, False))    # noch keine Bibliothek
        self.assertFalse(ls.should_skip("", "", False, True))           # keine Prüfsumme → immer voll laden
        self.assertFalse(ls.should_skip("neu", "alt", False, True))

    def test_diff_ids(self):
        new, removed = ls.diff_ids([1, "2", 3], ["3", 4, "2", 5, "4"])
        self.assertEqual(new, ["4", "5"])
        self.assertEqual(removed, ["1"])
        self.assertEqual(ls.diff_ids([], []), ([], []))

    def test_merge_hidden(self):
        visible = [entry(2, "beta"), entry(1, "Alpha")]
        hidden = [entry(3, "Gamma"), entry(2, "beta")]
        merged = ls.merge_hidden(visible, hidden)
        self.assertEqual([e.id for e in merged], ["1", "2", "3"])       # nach Titel, ohne Doppelte
        self.assertEqual({e.id: e.is_hidden for e in merged}, {"1": False, "2": False, "3": True})

    def test_new_marks_with_expiry(self):
        marks = ls.record_new({}, ["1", 2], NOW)
        self.assertEqual(marks, {"1": NOW, "2": NOW})
        later = ls.record_new(marks, ["2", "3"], NOW + 50)
        self.assertEqual(later, {"1": NOW, "2": NOW, "3": NOW + 50})    # erste Sichtung bleibt
        old = {"alt": NOW - 15 * DAY, "fast": NOW - 13 * DAY, "kaputt": "x", "bool": True, "frisch": NOW}
        self.assertEqual(ls.prune_new(old, NOW), {"fast": NOW - 13 * DAY, "frisch": NOW})
        self.assertEqual(ls.prune_new(None, NOW), {})
        self.assertTrue(ls.is_new(old, "fast", NOW))
        self.assertFalse(ls.is_new(old, "alt", NOW))
        self.assertFalse(ls.is_new(old, "kaputt", NOW))
        self.assertFalse(ls.is_new(old, "fehlt", NOW))
        self.assertFalse(ls.is_new({"x": NOW - 14 * DAY}, "x", NOW))      # genau 14 Tage → abgelaufen
        self.assertEqual(ls.clear_new({"1": NOW, "2": NOW}, 1), {"2": NOW})

    def test_new_order(self):
        marks = {"a": NOW - 10, "b": NOW, "c": NOW - 5, "alt": NOW - 20 * DAY}
        self.assertEqual(ls.new_order(marks, ["a", "b", "c", "alt", "x"], NOW), ["b", "c", "a"])
        self.assertEqual(ls.new_order(marks, ["a", "b", "c"], NOW, limit=2), ["b", "c"])

    def test_new_titles_text(self):
        self.assertEqual(ls.new_titles_text(["A", "B"]), "A, B")
        self.assertEqual(ls.new_titles_text(["A", "B", "C"]), "A, B, C")
        self.assertEqual(ls.new_titles_text(["A", "", "B", "C", "D"]), "A, B, C …")

    def test_interval_and_focus(self):
        self.assertFalse(ls.interval_due(0, 599, 10))
        self.assertTrue(ls.interval_due(0, 600, 10))
        self.assertFalse(ls.interval_due(0, 10 ** 9, 0))                 # 0 = aus
        self.assertFalse(ls.focus_due(100, 399))
        self.assertTrue(ls.focus_due(100, 400))


class PurchaseRetryTest(unittest.TestCase):
    def test_retries_5_15_60_then_stop(self):
        r = ls.PurchaseRetry()
        r.start()
        self.assertEqual([r.after_sync(False) for _ in range(4)], [5, 15, 60, None])
        self.assertFalse(r.active)
        self.assertIsNone(r.after_sync(False))

    def test_stops_when_new_games_appear(self):
        r = ls.PurchaseRetry()
        r.start()
        self.assertEqual(r.after_sync(False), 5)
        self.assertIsNone(r.after_sync(True))
        self.assertIsNone(r.after_sync(False))

    def test_inactive_and_restart(self):
        r = ls.PurchaseRetry((1, 2))
        self.assertIsNone(r.after_sync(False))                         # ohne Kauf keine Wiederholung
        r.start()
        self.assertEqual(r.after_sync(False), 1)
        r.start()                                                       # neuer Kauf → wieder von vorn
        self.assertEqual(r.after_sync(False), 1)

    def test_other_syncs_do_not_advance_but_may_finish(self):
        r = ls.PurchaseRetry()
        r.start()
        self.assertEqual(r.after_sync(False), 5)
        self.assertIsNone(r.after_sync(False, own=False))               # Timer-Abgleich ohne Neues: Plan bleibt
        self.assertTrue(r.active)
        self.assertEqual(r.after_sync(False), 15)
        self.assertIsNone(r.after_sync(True, own=False))                # Timer findet den Kauf → fertig
        self.assertFalse(r.active)


class SyncGateTest(unittest.TestCase):
    def test_single_flight_with_one_forced_follow_up(self):
        g = ls.SyncGate()
        self.assertTrue(g.begin("startup"))
        self.assertFalse(g.begin("timer"))                              # nicht erzwungen → verworfen
        self.assertIsNone(g.pending)
        self.assertFalse(g.begin("manual", force=True))
        self.assertFalse(g.begin("purchase", force=True))               # nur ein Nachholer, der letzte gilt
        self.assertEqual(g.finish(), ("purchase", True))
        self.assertFalse(g.running)
        self.assertTrue(g.begin("purchase", True))
        self.assertIsNone(g.finish())

    def test_pending_purchase_is_kept(self):
        g = ls.SyncGate()
        self.assertTrue(g.begin("timer"))
        self.assertFalse(g.begin("purchase-retry", force=True))
        self.assertFalse(g.begin("manual", force=True))                 # Kauf-Abgleich ist schon vorgemerkt
        self.assertEqual(g.finish(), ("purchase-retry", True))


# ----------------------------------------------------------------------------- GOG-Abruf mit versteckten Spielen
class ClientLibraryTest(TempPaths):
    def api(self) -> GogApi:
        store = mock.Mock()
        store.load.return_value = None
        return GogApi(store)

    def test_two_passes_hidden_merged(self):
        pages = {(0, 1): {"products": [product(10, "Beta"), product(11, "Alpha")], "totalPages": 2, "hasHiddenProducts": True},
                 (0, 2): {"products": [product(12, "Delta")], "totalPages": 2},
                 (1, 1): {"products": [product(13, "Gamma"), product(11, "Alpha")], "totalPages": 1}}
        calls, prog = [], []

        def get_json(url, params=None, **kw):
            calls.append((params["hiddenFlag"], params["page"]))
            self.assertTrue(url.endswith("/account/getFilteredProducts"))
            return pages[(params["hiddenFlag"], params["page"])]
        api = self.api()
        with mock.patch.object(api, "get_json", side_effect=get_json):
            lib = api.library(lambda p, t: prog.append((p, t)))
        self.assertEqual(calls, [(0, 1), (0, 2), (1, 1)])
        self.assertEqual([(e.id, e.is_hidden) for e in lib], [("11", False), ("10", False), ("12", False), ("13", True)])
        self.assertEqual(prog, [(1, 2), (2, 2), (3, 3)])
        cached = json.loads((self.cache / "api" / "library.json").read_text())
        self.assertEqual({d["id"]: d["is_hidden"] for d in cached}, {"10": False, "11": False, "12": False, "13": True})
        self.assertEqual([e.id for e in api.cached_library()], ["11", "10", "12", "13"])

    def test_hidden_pass_failure_keeps_cached_hidden_games(self):
        api_dir = self.cache / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "library.json").write_text(json.dumps([entry(1).to_dict(), entry(9, "Versteckt", hidden=True).to_dict()]))

        def get_json(url, params=None, **kw):
            if params["hiddenFlag"]:
                raise ApiError("HTTP 500")
            return {"products": [product(1), product(2)], "totalPages": 1, "hasHiddenProducts": True}
        api = self.api()
        with mock.patch.object(api, "get_json", side_effect=get_json), self.assertLogs("galaxy_linux.api.client", "WARNING"):
            lib = api.library()
        self.assertEqual([(e.id, e.is_hidden) for e in lib], [("1", False), ("2", False), ("9", True)])

    def test_hidden_pass_not_logged_in_propagates(self):
        def get_json(url, params=None, **kw):
            if params["hiddenFlag"]:
                raise NotLoggedIn()
            return {"products": [product(1)], "totalPages": 1}
        api = self.api()
        with mock.patch.object(api, "get_json", side_effect=get_json), self.assertRaises(NotLoggedIn):
            api.library()

    def test_no_second_pass_without_hidden_products(self):
        calls = []

        def get_json(url, params=None, **kw):
            calls.append(params["hiddenFlag"])
            return {"products": [product(1)], "totalPages": 1, "hasHiddenProducts": False}
        api = self.api()
        with mock.patch.object(api, "get_json", side_effect=get_json):
            self.assertEqual([e.id for e in api.library()], ["1"])
        self.assertEqual(calls, [0])


# ----------------------------------------------------------------------------- Abgleich (Backend-Teil)
class FakeApi:
    def __init__(self) -> None:
        self.logged_in = True
        self.checksum = "c1"
        self.entries = [entry(1), entry(2)]
        self.library_calls = 0
        self.user = None

    def user_data(self) -> dict:
        self.user = {"userId": "7", "username": "tester", "checksum": {"games": self.checksum}}
        return self.user

    def library(self, progress=None) -> list[LibraryEntry]:
        self.library_calls += 1
        if progress:
            progress(1, 1)
        return [LibraryEntry.from_dict(e.to_dict()) for e in self.entries]


def fake_backend() -> SimpleNamespace:
    return SimpleNamespace(api=FakeApi(), settings=GlobalSettings(), library=[],
                           installer=SimpleNamespace(_lock=threading.Lock(), _owned={"alt"}),
                           _meta={}, _covers={}, _meta_lock=threading.Lock())


class SyncTest(TempPaths):
    def test_first_sync_marks_nothing(self):
        b = fake_backend()
        res = ls.sync(b, "startup", now=NOW)
        self.assertTrue(res.first and res.changed and not res.skipped)
        self.assertEqual((res.new_ids, res.removed_ids, res.total), ([], [], 2))
        self.assertEqual([e.id for e in b.library], ["1", "2"])
        self.assertEqual(b.settings.library_checksum, "c1")
        self.assertEqual(b.settings.library_new_ids, {})
        self.assertIsNone(b.installer._owned)                           # Besitzliste wird neu geholt
        saved = GlobalSettings.load()
        self.assertEqual(saved.library_checksum, "c1")

    def test_unchanged_checksum_skips_full_fetch_unless_forced(self):
        b = fake_backend()
        ls.sync(b, "startup", now=NOW)
        b.installer._owned = {"alt"}
        res = ls.sync(b, "timer", now=NOW + 60)
        self.assertTrue(res.skipped)
        self.assertFalse(res.changed)
        self.assertEqual(res.total, 2)
        self.assertEqual(b.api.library_calls, 1)
        self.assertEqual(b.installer._owned, {"alt"})                    # nichts geändert → Cache bleibt
        res = ls.sync(b, "manual", force=True, now=NOW + 120)
        self.assertFalse(res.skipped)
        self.assertEqual(b.api.library_calls, 2)
        self.assertFalse(res.changed)

    def test_changed_checksum_records_new_and_removed(self):
        b = fake_backend()
        ls.sync(b, "startup", now=NOW)
        b._meta.update({"3": "m3", "4": "m4", "2": "m2"})
        b._covers.update({"3": "c3"})
        b.api.checksum = "c2"
        b.api.entries = [entry(2), entry(3, "Neues Spiel"), entry(4, "Alt, aber versteckt", hidden=True)]
        res = ls.sync(b, "focus", now=NOW + 100)
        self.assertEqual(res.new_ids, ["3"])                            # versteckt neu gesichtet zählt nicht als Kauf
        self.assertEqual(res.removed_ids, ["1"])
        self.assertEqual(res.new_titles, ["Neues Spiel"])
        self.assertTrue(res.changed and not res.first)
        self.assertEqual(b.settings.library_new_ids, {"3": NOW + 100})
        self.assertEqual(b.settings.library_checksum, "c2")
        self.assertNotIn("3", b._meta)
        self.assertNotIn("4", b._meta)
        self.assertNotIn("3", b._covers)
        self.assertEqual(b._meta["2"], "m2")                            # Bekanntes bleibt im Cache
        saved = GlobalSettings.load()
        self.assertEqual(saved.library_new_ids, {"3": NOW + 100})

    def test_marks_expire_and_page_visit_clears(self):
        b = fake_backend()
        ls.sync(b, "startup", now=NOW)
        b.settings.library_new_ids = {"1": NOW - 15 * DAY, "2": NOW - DAY}
        res = ls.sync(b, "timer", now=NOW)                              # auch der abgekürzte Abgleich räumt auf
        self.assertTrue(res.skipped)
        self.assertEqual(b.settings.library_new_ids, {"2": NOW - DAY})
        self.assertEqual(GlobalSettings.load().library_new_ids, {"2": NOW - DAY})
        self.assertTrue(ls.mark_seen(b.settings, "2"))
        self.assertFalse(ls.mark_seen(b.settings, "2"))
        self.assertEqual(GlobalSettings.load().library_new_ids, {})

    def test_not_logged_in(self):
        b = fake_backend()
        b.api.logged_in = False
        with self.assertRaises(NotLoggedIn):
            ls.sync(b, "timer")


class BackendAndCliTest(TempPaths):
    def setUp(self):
        super().setUp()
        api_dir = self.cache / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "library.json").write_text(json.dumps([entry(1, "Altes Spiel").to_dict()]))
        (self.cfg / "settings.json").write_text(json.dumps({"libraries": [], "library_checksum": "alt",
                                                            "install_dir": str(self.base / "games"),
                                                            "prefix_dir": str(self.base / "pfx")}))
        user = {"userId": "7", "username": "tester", "checksum": {"games": "neu"}}
        fetched = [entry(1, "Altes Spiel"), entry(2, "Neues Spiel")]
        self.patches = [
            mock.patch.object(GogApi, "logged_in", new_callable=mock.PropertyMock, return_value=True),
            mock.patch.object(GogApi, "user_data", autospec=True, side_effect=lambda api: setattr(api, "user", user) or user),
            mock.patch.object(GogApi, "library", autospec=True,
                              side_effect=lambda api, progress=None: [LibraryEntry.from_dict(e.to_dict()) for e in fetched]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_backend_sync_library(self):
        from galaxy_linux.backend import Backend
        b = Backend()
        self.assertEqual([e.id for e in b.library], ["1"])
        res = b.sync_library("manual")
        self.assertEqual((res.new_ids, res.total, res.skipped), (["2"], 2, False))
        self.assertIn("2", b.settings.library_new_ids)
        self.assertIsNone(b.installer._owned)
        self.assertEqual(b.api.username, "tester")
        self.assertTrue(b.sync_library("timer").skipped)                # Prüfsumme jetzt bekannt
        self.assertEqual([e.id for e in b.refresh_library()], ["1", "2"])

    def test_cli_list_refresh_prints_new_games(self):
        from galaxy_linux import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["list", "--refresh"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn(tr('{0} neue(s) Spiel(e): {1}').format(1, "Neues Spiel"), text.splitlines()[0])
        self.assertIn("2", GlobalSettings.load().library_new_ids)
        rows = {ln.split()[0]: ln for ln in text.splitlines() if ln.startswith("  ")}   # „✔“-Spalte leer
        self.assertTrue(rows["2"].endswith(tr('NEU')))                  # Liste kennzeichnet neue Spiele
        self.assertFalse(rows["1"].endswith(tr('NEU')))

    def test_cli_json_stays_parseable(self):
        from galaxy_linux import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["list", "--refresh", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual({d["id"]: d["new"] for d in json.loads(out.getvalue())}, {"1": False, "2": True})
        self.assertIn("Neues Spiel", err.getvalue())


# ----------------------------------------------------------------------------- Steuerung im Fenster (ohne Anzeige)
try:
    from galaxy_linux.ui import library_sync as uls
except (ImportError, ValueError):     # pragma: no cover - ohne PyGObject/GTK 4
    uls = None


class FakeGLib:
    SOURCE_REMOVE, SOURCE_CONTINUE = False, True

    def __init__(self) -> None:
        self.timeouts: dict[int, tuple[int, object]] = {}
        self.idles: list = []
        self.removed: list[int] = []
        self._next = 0

    def timeout_add_seconds(self, delay, fn):
        self._next += 1
        self.timeouts[self._next] = (delay, fn)
        return self._next

    def source_remove(self, src):
        self.removed.append(src)
        self.timeouts.pop(src, None)

    def idle_add(self, fn):
        self.idles.append(fn)

    def run_idles(self) -> None:
        while self.idles:
            self.idles.pop(0)()

    def fire(self, delay: int) -> None:
        src = next(k for k, (d, _f) in self.timeouts.items() if d == delay)
        _d, fn = self.timeouts.pop(src)
        fn()


@unittest.skipIf(uls is None, "PyGObject/GTK 4 fehlt")
class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.glib = FakeGLib()
        self.calls: list = []        # offene Abgleiche: (fn, done, fail)
        settings = GlobalSettings()
        settings.save = mock.Mock()
        backend = SimpleNamespace(api=SimpleNamespace(logged_in=True), settings=settings, library=[entry(1)],
                                  jobs=SimpleNamespace(active=lambda: []), registry=SimpleNamespace(all=lambda: []),
                                  sync_library=mock.Mock())
        self.app = mock.Mock()
        self.win = mock.Mock()
        self.win.backend = backend
        self.win.view = "all"
        self.win.is_active.return_value = True
        self.win.get_application.return_value = self.app
        self.win.stack.get_visible_child_name.return_value = "library"
        self.toasts: list[str] = []
        self.win.toast.side_effect = self.toasts.append
        for p in (mock.patch.object(uls, "GLib", self.glib),
                  mock.patch.object(uls, "run_async", lambda fn, done, fail: self.calls.append((fn, done, fail))),
                  mock.patch.object(uls, "idle", lambda fn, *a: fn(*a))):
            p.start()
            self.addCleanup(p.stop)
        self.ctl = uls.LibrarySyncController(self.win)

    def finish(self, res: ls.SyncResult | Exception | None = None):
        """Ältesten offenen Abgleich beenden; liefert (reason, force) des Aufrufs."""
        fn, done, fail = self.calls.pop(0)
        fn()
        reason, force, _progress = self.win.backend.sync_library.call_args.args
        if isinstance(res, Exception):
            fail(res)
        else:
            done(res or ls.SyncResult(total=1))
        self.glib.run_idles()
        return reason, force

    def test_start_schedules_startup_timer_and_focus(self):
        self.ctl.start()
        self.assertEqual(sorted(d for d, _f in self.glib.timeouts.values()), [ls.STARTUP_DELAY, ls.TIMER_TICK])
        self.assertEqual(self.calls, [])                                # Cache steht → erst nach ~3 s
        self.glib.fire(ls.STARTUP_DELAY)
        self.assertEqual(self.finish(), ("startup", False))
        self.ctl._on_close()
        self.assertEqual(self.glib.timeouts, {})                        # Intervall-Takt beendet
        self.assertNotIn(0, self.glib.removed)

    def test_empty_cache_syncs_at_once(self):
        self.win.backend.library = []
        self.ctl.start()
        self.assertEqual(self.finish(ls.SyncResult(total=3, first=True)), ("startup", True))
        self.assertIn(tr('{0} Spiele geladen').format(3), self.toasts)

    def test_manual_up_to_date_and_update_check(self):
        self.ctl.request("manual", force=True)
        self.win.spinner.start.assert_called_once()
        self.finish()
        self.assertEqual(self.toasts, [tr('Bibliothek ist aktuell')])
        self.win.spinner.stop.assert_called_once()
        self.win.check_updates_async.assert_called_once_with(quiet=True)
        self.assertFalse(self.ctl.gate.running)

    def test_single_flight_with_forced_follow_up(self):
        self.ctl.request("timer")
        self.ctl.request("focus")                                       # verworfen
        self.ctl.request("manual", force=True)                          # vorgemerkt
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.finish(), ("timer", False))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.finish(), ("manual", True))
        self.assertEqual(self.calls, [])

    def test_new_games_toast_and_desktop_notification(self):
        self.win.is_active.return_value = False
        self.ctl.request("timer")
        self.finish(ls.SyncResult(new_ids=["1", "2", "3", "4"], total=9, new_titles=["A", "B", "C", "D"]))
        self.assertEqual(self.toasts, [tr('{0} neue(s) Spiel(e): {1}').format(4, "A, B, C …")])
        self.app.send_notification.assert_called_once()
        self.win.populate.assert_called_once()                          # Ansicht neu, Bildlauf bleibt
        self.win.check_updates_async.assert_called_once_with(quiet=True)

    def test_purchase_retries_until_new_games(self):
        self.ctl.request("purchase")
        self.assertEqual(self.finish(), ("purchase", True))
        self.assertEqual([d for d, _f in self.glib.timeouts.values()], [5])
        self.glib.fire(5)
        self.assertEqual(self.finish(), ("purchase-retry", True))
        self.assertEqual([d for d, _f in self.glib.timeouts.values()], [15])
        self.glib.fire(15)
        self.finish(ls.SyncResult(new_ids=["7"], total=2, new_titles=["Gekauft"]))
        self.assertEqual(self.glib.timeouts, {})                        # gefunden → keine weiteren Versuche
        self.assertFalse(self.ctl.retry.active)

    def test_purchase_retries_stop_after_60_s(self):
        self.ctl.request("purchase")
        self.finish()
        for delay in (5, 15, 60):
            self.glib.fire(delay)
            if delay == 15:                                             # Fehler zählen als Versuch
                with self.assertLogs("galaxy_linux.ui.library_sync", "WARNING"):
                    self.finish(ApiError("offline"))
            else:
                self.finish()
        self.assertEqual(self.glib.timeouts, {})
        self.assertFalse(self.ctl.retry.active)

    def test_focus_only_after_five_minutes(self):
        self.ctl._on_active(self.win, None)
        self.assertEqual(self.calls, [])
        self.ctl.last_sync -= ls.FOCUS_MIN_GAP
        self.win.backend.settings.library_sync_on_focus = False
        self.ctl._on_active(self.win, None)
        self.assertEqual(self.calls, [])
        self.win.backend.settings.library_sync_on_focus = True
        self.ctl._on_active(self.win, None)
        self.assertEqual(self.finish(), ("focus", False))

    def test_logged_out_manual_opens_login(self):
        self.win.backend.api.logged_in = False
        self.ctl.request("timer")
        self.win.do_login.assert_not_called()
        self.ctl.request("manual", force=True)
        self.win.do_login.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_session_expired_in_background_warns_once(self):
        for _ in range(2):
            self.ctl.request("timer")
            self.finish(NotLoggedIn())
        self.assertEqual(self.toasts, [tr('Sitzung abgelaufen – bitte neu anmelden.')])
        self.win.do_login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
