"""Download-Ansicht wie Galaxy 2.1 (0.3.0): Bandbreite mit Zeitplan, Pausieren/Fortsetzen, Reihenfolge, Neustart,
„Downloads während des Spiels pausieren“, Phasen, Speicherplatz, DLC-Auswahl, Beta-Kanäle, Extras."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux import backend as bk
from galaxy_linux.content import bandwidth, extras, installer as inst_mod
from galaxy_linux.content.downloader import DownloadCancelled, Progress
from galaxy_linux.content.installer import InstallManager, InstallPlan, NotEnoughSpace
from galaxy_linux.content.manifest import Build, BuildManifest, Chunk, DepotFile
from galaxy_linux.content.downloader import FileJob
from galaxy_linux.settings import GlobalSettings, InstalledGame


def at(hhmm: str) -> float:
    return time.mktime(time.strptime(f"2026-10-23 {hhmm}", "%Y-%m-%d %H:%M"))


class BandwidthTest(unittest.TestCase):
    def test_window_and_rate(self):
        self.assertTrue(bandwidth.in_window("08:00", "23:00", at("12:00")))
        self.assertFalse(bandwidth.in_window("08:00", "23:00", at("23:30")))
        self.assertTrue(bandwidth.in_window("22:00", "06:00", at("23:30")))      # über Mitternacht
        self.assertTrue(bandwidth.in_window("22:00", "06:00", at("05:59")))
        self.assertFalse(bandwidth.in_window("22:00", "06:00", at("12:00")))
        self.assertTrue(bandwidth.in_window("kaputt", "06:00", at("12:00")))
        s = types.SimpleNamespace(bandwidth_limit=True, bandwidth_limit_kib=512, bandwidth_schedule=True,
                                  bandwidth_schedule_start="08:00", bandwidth_schedule_stop="10:00")
        self.assertEqual(bandwidth.current_rate(s, at("09:00")), 512 * 1024)
        self.assertEqual(bandwidth.current_rate(s, at("11:00")), 0)
        s.bandwidth_schedule = False
        self.assertEqual(bandwidth.current_rate(s, at("11:00")), 512 * 1024)
        s.bandwidth_limit = False
        self.assertFalse(bandwidth.limit_active(s))

    def test_throttle_paces(self):
        clock = [0.0]
        slept = []

        def sleep(d):
            slept.append(d)
            clock[0] += d
        t = bandwidth.Throttle(lambda: 1000.0, clock=lambda: clock[0], sleep=sleep)
        t.consume(3000)
        self.assertAlmostEqual(clock[0], 3.0, delta=0.6)          # ~3000 B bei 1000 B/s
        unlimited = bandwidth.Throttle(lambda: 0.0, clock=lambda: 0.0, sleep=lambda d: self.fail("darf nicht warten"))
        unlimited.consume(10**9)
        cancel = threading.Event()
        cancel.set()
        bandwidth.Throttle(lambda: 1.0, clock=lambda: 0.0, sleep=lambda d: self.fail("abgebrochen")).consume(10**6, cancel)


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.p1 = mock.patch.object(bk, "_job_finished_hooks", [])
        self.p1.start()
        self.running: set[str] = set()
        self.calls: list = []
        self.gate = threading.Event()
        test = self

        class Inst:
            def plan(self, pid, title, platform, language, **kw):
                test.calls.append(("plan", pid, kw.get("dlc_filter"), kw.get("dlc_exclude"), kw.get("password")))
                kw["status"]("Builds abrufen …")
                return types.SimpleNamespace(file_count=1, install_dir=test.base / pid)

            def install(self, plan, on_progress, cancel):
                on_progress(Progress(phase="Herunterladen", total_bytes=10, done_bytes=1, step="downloading"))
                while not test.gate.is_set():
                    if cancel.is_set():
                        raise DownloadCancelled()
                    time.sleep(0.01)
                return "ok"

            def update(self, pid, on_progress, cancel, status, **kw):
                test.calls.append(("update", pid, kw))
                return "upd"
        self.gs = types.SimpleNamespace(get=lambda pid: types.SimpleNamespace(branch_password="pw", dlc_excluded=["9"]))
        self.fake = types.SimpleNamespace(
            installer=Inst(), updates={}, game_settings=self.gs,
            settings=types.SimpleNamespace(pause_downloads_when_playing=True),
            launcher=types.SimpleNamespace(active_ids=lambda: sorted(self.running)),
            before_files_change=lambda *a: None, create_shortcuts=lambda *a: None)
        with mock.patch.object(threading.Thread, "start"):
            self.q = bk.JobQueue(self.fake)

    def tearDown(self):
        self.gate.set()
        self.p1.stop()
        self.td.cleanup()

    def run_bg(self, job):
        t = threading.Thread(target=self.q._execute, args=(job,), daemon=True)
        t.start()
        for _ in range(300):
            if job.step == "downloading":
                break
            time.sleep(0.01)
        return t


class JobQueueTest(QueueBase):
    def test_pause_resume_running_install(self):
        job = bk.Job("install", "1", "Demo")
        self.q.jobs.append(job)
        job.state = "queued"
        t = self.run_bg(job)
        self.assertEqual(job.state, "running")
        self.assertEqual(job.steps, ["install_started", "file_list"])
        self.assertTrue(self.q.pause(job))
        t.join(3)
        self.assertEqual((job.state, job.pause_reason), ("paused", "user"))
        self.assertTrue(self.q.resume(job))
        self.assertEqual((job.state, job.step, job.cancel.is_set()), ("queued", "pending", False))
        self.gate.set()
        self.q._execute(job)
        self.assertEqual((job.state, job.step), ("done", "complete"))
        self.assertEqual(self.calls[0][2:], (None, {"9"}, "pw"))

    def test_cancel_paused_and_queued(self):
        a, b = bk.Job("install", "1", "A"), bk.Job("update", "2", "B")
        for j in (a, b):
            self.q.jobs.append(j)
        self.assertTrue(self.q.pause(a))
        self.assertEqual(a.state, "paused")
        self.q.cancel_all()
        self.assertEqual((a.state, b.state), ("cancelled", "cancelled"))
        self.assertFalse(self.q.pause(bk.Job("prefix", "3", "P")))          # Prefix-Aufträge nicht pausierbar

    def test_move_and_next(self):
        jobs = [bk.Job("install", str(i), f"J{i}") for i in range(4)]
        done = bk.Job("install", "x", "fertig", state="done")
        self.q.jobs = [done, *jobs]
        self.assertTrue(self.q.move_to_front(jobs[3]))
        self.assertEqual([j.title for j in self.q.queue()], ["J3", "J0", "J1", "J2"])
        self.q.move(jobs[3], 99)
        self.assertEqual([j.title for j in self.q.queue()], ["J0", "J1", "J2", "J3"])
        self.assertIs(self.q._next(), jobs[0])
        self.q.pause(jobs[0])
        self.assertIs(self.q._next(), jobs[1])
        self.assertIn(done, self.q.jobs)

    def test_play_hold_pauses_and_resumes(self):
        job = bk.Job("install", "1", "Demo")
        user_paused = bk.Job("install", "2", "Selbst pausiert")
        self.q.jobs += [job, user_paused]
        self.q.pause(user_paused)
        t = self.run_bg(job)
        self.running.add("777")
        self.q.apply_play_pause()
        t.join(3)
        self.assertEqual((job.state, job.pause_reason), ("paused", "game"))
        self.assertTrue(self.q.play_hold)
        queued = bk.Job("update", "3", "Wartet")
        self.q.jobs.append(queued)
        self.assertIsNone(self.q._next())                   # während des Spiels startet kein Download
        self.running.clear()
        self.q.apply_play_pause()
        self.assertEqual((job.state, user_paused.state), ("queued", "paused"))
        self.fake.settings.pause_downloads_when_playing = False
        self.running.add("777")
        self.q.apply_play_pause()
        self.assertEqual(job.state, "queued")

    def test_persistence_roundtrip(self):
        path = self.base / "jobs.json"
        a = bk.Job("install", "1", "A", install_root="/spiele", dlc_filter=["5"], language="de-DE")
        b = bk.Job("update", "2", "B", build_id="77")
        c = bk.Job("prefix", "3", "C")                                         # wird nicht gespeichert
        d = bk.Job("verify", "4", "D", state="done")
        self.q.jobs += [a, b, c, d]
        self.q.pause(b)
        self.q.persist_path = path
        self.q._save()
        raw = json.loads(path.read_text())
        self.assertEqual([(r["product_id"], r["state"]) for r in raw], [("1", "queued"), ("2", "paused")])
        with mock.patch.object(threading.Thread, "start"):
            q2 = bk.JobQueue(self.fake)
        restored = q2.enable_persistence(path)
        self.assertEqual([(j.product_id, j.state, j.install_root, j.dlc_filter, j.language, j.build_id) for j in restored],
                         [("1", "queued", "/spiele", ["5"], "de-DE", ""), ("2", "paused", "", None, "", "77")])
        path.write_text("[{\"kind\": \"install\"}, 5, {\"kind\": \"update\", \"product_id\": \"9\", \"data\": []}]")
        with mock.patch.object(threading.Thread, "start"):
            self.assertEqual(bk.JobQueue(self.fake).enable_persistence(path), [])

    def test_move_counts_running_job_first(self):
        q1, run, q2 = bk.Job("install", "1", "A"), bk.Job("install", "2", "B", state="running"), bk.Job("install", "3", "C")
        self.q.jobs = [q1, run, q2]
        self.assertEqual([j.title for j in self.q.queue()], ["B", "A", "C"])
        self.q.move(q2, 1)                                    # in der Anzeige direkt hinter den laufenden
        self.assertEqual([j.title for j in self.q.queue()], ["B", "C", "A"])
        self.assertIs(self.q._next(), q2)

    def test_resume_during_play_overrides_hold(self):
        job = bk.Job("install", "1", "Demo")
        self.q.jobs.append(job)
        self.running.add("777")
        self.q.apply_play_pause()
        self.assertIsNone(self.q._next())
        self.q.pause(job)
        self.q.resume(job)                                    # von Hand fortgesetzt: läuft trotz Spiel
        self.assertIs(self.q._next(), job)
        self.running.clear()
        self.q.apply_play_pause()
        self.assertFalse(job.ignore_play_hold)

    def test_resume_while_pausing_requeues(self):
        job = bk.Job("install", "1", "Demo")
        self.q.jobs.append(job)
        t = self.run_bg(job)
        self.q.pause(job)
        self.assertTrue(self.q.resume(job))                   # noch beim Anhalten
        t.join(3)
        self.assertEqual(job.state, "queued")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def plan(self, sizes: dict[str, int]) -> InstallPlan:
        jobs = [FileJob(DepotFile(path=p, chunks=[Chunk("m", "c", n, n)]), "http://cdn") for p, n in sizes.items()]
        m = BuildManifest("1", "1", "Demo", "windows", 2, [], [], [])
        return InstallPlan("1", "Demo", "windows", "de-DE", self.base / "Demo", Build("1", 2), m, jobs=jobs)

    def test_space_needed_counts_only_missing_bytes(self):
        plan = self.plan({"a.bin": 1000, "b.bin": 500})
        (self.base / "Demo").mkdir()
        (self.base / "Demo" / "a.bin").write_bytes(b"x" * 400)
        self.assertEqual(InstallManager.space_needed(plan), 600 + 500)
        im = InstallManager(None, GlobalSettings(), None)
        with mock.patch.object(inst_mod.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10 << 20)):
            with self.assertRaises(NotEnoughSpace) as cm:
                im.check_disk_space(plan)                    # Reserve von 64 MiB unterschritten
        self.assertIn(str(self.base / "Demo"), str(cm.exception))
        with mock.patch.object(inst_mod.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1 << 30)):
            im.check_disk_space(plan)
        self.assertEqual(inst_mod.existing_dir(self.base / "neu" / "tief"), self.base)

    def test_update_follows_branch_and_passes_options(self):
        reg = types.SimpleNamespace(get=lambda pid: InstalledGame("1", "Demo", "windows", str(self.base), build_id="5",
                                                                     branch="beta", installed_dlcs=["7"]))
        im = InstallManager(None, GlobalSettings(), reg)
        builds = [Build("9", 2, branch=None), Build("8", 2, branch="beta"), Build("6", 2, branch="beta")]
        seen = {}
        with mock.patch.object(im, "builds", side_effect=lambda pid, plat, pw="": seen.setdefault("pw", pw) and builds or builds), \
                mock.patch.object(im, "plan", side_effect=lambda *a, **kw: seen.update(kw) or "PLAN"), \
                mock.patch.object(im, "install", return_value="ok"):
            self.assertEqual(im.update("1", password="geheim", dlc_exclude={"7"}), "ok")
        self.assertEqual((seen["build_id"], seen["dlc_exclude"], seen["password"], seen["pw"]), ("8", {"7"}, "geheim", "geheim"))
        with mock.patch.object(im, "builds", return_value=[Build("9", 2)]), \
                self.assertRaises(inst_mod.InstallError):
            im.update("1")

    def test_select_depots_with_exclude(self):
        man = BuildManifest.parse({"version": 2, "baseProductId": "1", "buildId": "b", "depots": [
            {"productId": "1", "manifest": "m1", "languages": ["*"]},
            {"productId": "7", "manifest": "m7", "languages": ["*"]},
            {"productId": "8", "manifest": "m8", "languages": ["*"]}], "products": []})

        class Api:
            logged_in = True

            def builds(self, pid, platform):
                return [{"build_id": "b", "generation": 2, "link": "L"}]

            def zlib_json(self, link):
                return man_raw

            def owned_ids(self):
                return {"1", "7", "8"}

            def secure_link(self, pid):
                return "http://cdn"

            def depot_manifest(self, md5):
                return {"depot": {"items": [{"type": "DepotFile", "path": md5 + ".bin", "chunks": []}]}}
        man_raw = {"version": 2, "baseProductId": "1", "buildId": "b", "depots": [
            {"productId": "1", "manifest": "m1"}, {"productId": "7", "manifest": "m7"}, {"productId": "8", "manifest": "m8"}]}
        im = InstallManager(Api(), GlobalSettings(), None)
        im._owned = {"1", "7", "8"}
        plan = im.plan("1", "Demo", install_dir=self.base / "D", dlc_exclude={"8"}, with_dependencies=False)
        self.assertEqual(sorted(j.file.path for j in plan.jobs), ["m1.bin", "m7.bin"])
        plan = im.plan("1", "Demo", install_dir=self.base / "D", dlc_filter=set(), with_dependencies=False)
        self.assertEqual([j.file.path for j in plan.jobs], ["m1.bin"])
        self.assertTrue(man.depots)


PRODUCT = {"id": 1, "title": "Demo", "downloads": {
    "installers": [{"id": "en1", "name": "Demo", "os": "windows", "language": "en", "language_full": "English",
                    "version": "1.0", "total_size": 30, "files": [{"id": "en1-1", "size": 10, "downlink": "/dl/a"},
                                                                  {"id": "en1-2", "size": 20, "downlink": "/dl/b"}]},
                   {"id": "de1", "name": "Demo", "os": "windows", "language": "de", "language_full": "Deutsch",
                    "files": [{"id": "x", "size": 5, "downlink": "/dl/c"}]},
                   {"id": "lin", "name": "Demo", "os": "linux", "language": "en", "language_full": "English",
                    "files": [{"id": "y", "size": 7, "downlink": "/dl/d"}]}],
    "bonus_content": [{"id": 3, "name": "Handbuch", "type": "manuals", "files": [{"id": 3, "size": 4, "downlink": "/dl/e"}]}],
    "patches": [], "language_packs": [{"id": "lp", "name": "Polski", "os": "windows", "language": "pl",
                                       "files": [{"size": 1}]}]},
    "expanded_dlcs": [{"id": 7, "title": "DLC Eins", "downloads": {"bonus_content": [
        {"id": 8, "name": "Soundtrack", "type": "audio", "files": [{"id": 8, "size": 6, "downlink": "/dl/f"}]}]}},
        {"id": 9, "title": "Nicht im Besitz", "downloads": {"installers": [
            {"id": "z", "os": "windows", "language": "en", "files": [{"id": "z", "size": 1, "downlink": "/dl/z"}]}]}}]}


class ExtrasTest(unittest.TestCase):
    def test_list_and_filter(self):
        items = extras.list_extras(PRODUCT, owned={"1", "7"})
        self.assertEqual([(i.kind, i.product_id, i.language, i.os) for i in items],
                         [("installer", "1", "en", "windows"), ("installer", "1", "de", "windows"),
                          ("installer", "1", "en", "linux"), ("goodie", "1", "", ""), ("goodie", "7", "", "")])
        self.assertEqual(items[0].size, 30)
        self.assertEqual(items[4].is_dlc, True)
        self.assertEqual(len(extras.list_extras(PRODUCT, owned=None)), 6)
        win_de = extras.filter_items(items, "windows", "de")
        self.assertEqual([(i.kind, i.language) for i in win_de], [("installer", "de"), ("goodie", ""), ("goodie", "")])
        self.assertEqual(extras.languages(items), [("de", "Deutsch"), ("en", "English")])
        self.assertEqual(extras.list_extras(None), [])

    def test_job_title_and_file_name(self):
        items = extras.list_extras(PRODUCT, owned={"1"})
        self.assertIn("Offline", extras.job_title(items[:2], "Demo"))
        self.assertEqual(extras.job_title(items[3:4], "Demo"), "Demo - Handbuch")
        self.assertEqual(extras.file_name("https://cdn/x/setup_demo%201.0.exe?token=1", "f"), "setup_demo 1.0.exe")
        self.assertEqual(extras.file_name("https://cdn/", "Mein Handbuch"), "Mein Handbuch")

    def test_run_job_downloads_and_skips_complete(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "Demo"
            items = extras.list_extras(PRODUCT, owned={"1", "7"})
            sel = [items[0], items[4]]
            (dest).mkdir()
            (dest / "a.bin").write_bytes(b"1" * 10)          # schon vollständig
            got = []

            class Api:
                def downlink(self, url):
                    return {"downlink": "https://cdn/" + url.rsplit("/", 1)[1] + ".bin?x=1"}

                def stream_download(self, url, target, cb, cancel, throttle=None):
                    got.append(Path(target).relative_to(dest).as_posix())
                    Path(target).parent.mkdir(parents=True, exist_ok=True)
                    Path(target).write_bytes(b"0")
                    cb(5, 5)
            backend = types.SimpleNamespace(api=Api(), settings=GlobalSettings(), installer=types.SimpleNamespace(throttle=None))
            job = bk.Job("extras", "1", "Demo", data={"items": [i.to_dict() for i in sel], "dest": str(dest)})
            progress = []
            out = extras.run_job(backend, job, progress.append, lambda s: None)
            self.assertEqual(got, ["b.bin", "DLC - DLC Eins/f.bin"])
            self.assertEqual(len(out), 3)
            self.assertEqual(progress[-1].done_bytes, 36)
            job.cancel.set()
            with self.assertRaises(DownloadCancelled):
                extras.run_job(backend, job, progress.append, lambda s: None)

    def test_registered_kinds(self):
        self.assertTrue(bk.is_pausable(extras.JOB_KIND))
        self.assertTrue(bk.is_network_job(extras.CLOUD_BACKUP_KIND))
        self.assertFalse(bk.is_pausable(extras.CLOUD_BACKUP_KIND))

    def test_default_dir_from_user_dirs(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": d}):
            Path(d, "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Heruntergeladen"\n')
            self.assertEqual(extras.default_extras_dir(), Path.home() / "Heruntergeladen" / "GOG")
            s = GlobalSettings()
            s.extras_dir = "~/Extras"
            self.assertEqual(extras.game_dir(s, "A: B"), Path.home() / "Extras" / "A B")


if __name__ == "__main__":
    unittest.main()
