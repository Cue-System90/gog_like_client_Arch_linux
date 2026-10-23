"""Tests für die GOG-Abhängigkeiten (content/redist.py, launch/hook_redist.py, Installer) – ohne Netz, mit einem
Ausschnitt des echten GOG-Abhängigkeits-Repositorys als Beispiel."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.api.client import ApiError
from galaxy_linux.backend import JOB_KINDS, Job, JobQueue
from galaxy_linux.content import build_records, redist
from galaxy_linux.content.downloader import FileJob
from galaxy_linux.content.installer import InstallManager
from galaxy_linux.content.manifest import DepotFile, parse_dependency_repository
from galaxy_linux.launch import hook_redist
from galaxy_linux.launch.hooks import LaunchContext
from galaxy_linux.settings import GameSettingsStore, GlobalSettings, InstalledGame, InstalledRegistry

MSVC2005_ARGS = '/q:a /c:"VCREDI~3.EXE /q:a /c:""msiexec /i vcredist.msi /qb"" "'

# Ausschnitt aus content-system.gog.com/dependencies/repository?generation=2 (build_id 59705672826648994)
REPOSITORY = {"build_id": "59705672826648994", "depots": [
    {"dependencyId": "ISI", "readableName": "Install Script Interpreter", "manifest": "m-isi", "internal": True,
     "executable": {"path": "__redist/ISI/scriptinterpreter.exe", "arguments": ""}},
    {"dependencyId": "DOSBox074", "readableName": "DOX Box 74", "manifest": "m-dosbox", "internal": True,
     "executable": {"path": "", "arguments": ""}},
    {"dependencyId": "language_setup", "readableName": "Language Setup", "manifest": "m-lang", "internal": True,
     "executable": {"path": "", "arguments": ""}},
    {"dependencyId": "ScummVM", "readableName": "Scumm VM", "manifest": "m-scumm", "internal": True,
     "executable": {"path": "", "arguments": ""}},
    {"dependencyId": "AdobeAir", "readableName": "Adobe Air", "manifest": "m-air", "internal": True,
     "executable": {"path": "__redist/AdobeAir/AIRRuntimeInstaller.exe", "arguments": "-silent"}},
    {"dependencyId": "MSVC2017", "readableName": "Visual C++ Redist 2017", "manifest": "m-vc17",
     "executable": {"path": "__redist\\MSVC2017\\VC_redist.x86.exe", "arguments": "/install /quiet /norestart"}},
    {"dependencyId": "MSVC2005", "readableName": "Visual C++ Redist 2005", "manifest": "m-vc05",
     "executable": {"path": "__redist/MSVC2005/vcredist_x86.exe", "arguments": MSVC2005_ARGS}},
    {"dependencyId": "DirectX", "readableName": "DirectX 9.0c", "manifest": "m-dx",
     "executable": {"path": "__redist/DirectX/DXSETUP.exe", "arguments": "/silent"}},
    {"dependencyId": "PHYSXLEGACY", "readableName": "NVIDIA PhysX Legacy (9.13.0604)", "manifest": "m-physx",
     "executable": {"path": "__redist/PHYSXLEGACY/PhysX-9.13.0604-SystemSoftware-Legacy.msi",
                    "arguments": "/passive /norestart"}},
    {"dependencyId": "XNA", "readableName": "XNA 3.5", "manifest": "m-xna",
     "executable": {"path": "__redist/XNA/xnafx31_redist.msi", "arguments": "/qn"}},
]}

ISI_ARGV_TAIL = ["/nodesktopshorctut", "/nodesktopshortcut"]


def item(path, md5="00", flags=None):
    return {"type": "DepotFile", "path": path, "flags": flags or [],
            "chunks": [{"md5": md5, "compressedMd5": md5, "size": 3, "compressedSize": 3}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = root = Path(self.td.name)
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", DATA_DIR=root / "data", MANIFEST_DIR=root / "data" / "manifests",
            LOG_DIR=root / "logs", SETTINGS_FILE=root / "settings.json", GAME_SETTINGS_FILE=root / "games.json",
            INSTALLED_FILE=root / "installed.json", PLAYTIME_FILE=root / "playtime.json")
        self.patch.start()
        (root / "logs").mkdir()
        self.state_patch = mock.patch.multiple(hook_redist, _tried=set(), _backend_ref=None)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.patch.stop()
        self.td.cleanup()

    def put_cache(self, ids, build_id="59705672826648994"):
        """Gemeinsamen Ordner mit Programmdateien der IDs und .redist-manifest anlegen."""
        repo = parse_dependency_repository(REPOSITORY)
        for i in ids:
            exe = redist.redist_root() / repo[i].executable_path
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"MZ")
        data = dict(REPOSITORY, build_id=build_id, installed=list(ids))
        redist.cache_manifest_path().parent.mkdir(parents=True, exist_ok=True)
        redist.cache_manifest_path().write_text(json.dumps(data))


# ----------------------------------------------------------------------------- Einteilung
class ClassificationTest(Base):
    def test_global_paths(self):
        self.assertTrue(redist.is_global_path("__redist/ISI/scriptinterpreter.exe"))
        self.assertTrue(redist.is_global_path("__redist\\MSVC2017\\VC_redist.x86.exe"))
        self.assertTrue(redist.is_global_path("\\__redist\\DirectX\\DXSETUP.exe"))
        self.assertFalse(redist.is_global_path(""))
        self.assertFalse(redist.is_global_path("DOSBOX/dosbox.exe"))

    def test_split_from_repository_fixture(self):
        repo = parse_dependency_repository(REPOSITORY)
        ids = ["ISI", "MSVC2017", "DOSBox074", "language_setup", "AdobeAir", "DirectX", "Unbekannt", "MSVC2017", "ScummVM"]
        glob, game, unknown = redist.split_dependencies(ids, repo)
        self.assertEqual(glob, ["ISI", "MSVC2017", "AdobeAir", "DirectX"])      # internal zählt nicht (AdobeAir)
        self.assertEqual(game, ["DOSBox074", "language_setup", "ScummVM"])       # internal → trotzdem Spielordner
        self.assertEqual(unknown, ["Unbekannt"])
        entries = redist.installer_entries(ids, repo)
        self.assertEqual([e["id"] for e in entries], ["MSVC2017", "AdobeAir", "DirectX"])   # ohne ISI, ohne Doppelte
        self.assertEqual(entries[0], {"id": "MSVC2017", "name": "Visual C++ Redist 2017",
                                      "path": "__redist/MSVC2017/VC_redist.x86.exe",
                                      "arguments": "/install /quiet /norestart"})

    def test_full_repository_68_ids(self):
        """Vollständiges Repository (Build 59705672826648994, nur IDs/Pfade/Argumente): 49 global, 19 Spielordner."""
        raw = json.loads((Path(__file__).parent / "data" / "gog_redist_repository.json").read_text(encoding="utf-8"))
        repo = parse_dependency_repository(raw)
        self.assertEqual(len(repo), 68)
        glob, game, unknown = redist.split_dependencies(list(repo), repo)
        self.assertEqual((len(glob), len(game), unknown), (49, 19, []))
        self.assertIn("ISI", glob)
        self.assertTrue({"MSVC2005", "MSVC2026_x64", "PHYSXLEGACY", "UE4REDIST", "openAL", "dotNetDesktopRuntime90"}
                        <= set(glob))
        self.assertTrue({"DOSBox063", "DOSBox074glide", "ScummVM", "nGlide_105", "language_setup", "SuspendLauncher",
                         "DOSBOXConfigurator"} <= set(game))
        self.assertTrue(all(repo[i].internal for i in game))                     # alle internal – trotzdem Spielordner
        self.assertTrue({"AdobeAir", "QuickTime", "Foxit"} <= {i for i in glob if repo[i].internal})
        entries = redist.installer_entries(glob, repo)
        self.assertEqual(len(entries), 48)                                       # ISI ist kein Installer
        self.assertTrue(all(redist.is_global_path(e["path"]) for e in entries))

    def test_required_ids_like_heroic(self):
        repo = parse_dependency_repository(REPOSITORY)
        recs = [{"dependency_ids": ["MSVC2017", "DOSBox074"]}, None,
                {"dependency_ids": ["DirectX", "MSVC2017", "ScummVM"]}]
        self.assertEqual(redist.required_ids(recs, repo), ["ISI", "MSVC2017", "DirectX"])
        old = {"redist": [{"id": "DirectX", "path": "__redist/DirectX/DXSETUP.exe"}, {"id": "x", "path": "x.exe"}]}
        self.assertEqual(redist.required_ids([old]), ["ISI", "DirectX"])            # Datensatz bis 0.1.6, ohne Repository

    def test_record_global_entries(self):
        repo = parse_dependency_repository(REPOSITORY)
        rec = {"dependency_ids": ["ISI", "DOSBox074", "PHYSXLEGACY"]}
        self.assertEqual([e["id"] for e in redist.record_global_entries(rec, repo)], ["PHYSXLEGACY"])
        legacy = {"redist": [{"id": "MSVC2015", "name": "VC 2015", "path": "__redist\\MSVC2015\\VC_redist.x86.exe",
                              "arguments": "/q"}]}
        self.assertEqual(redist.record_global_entries(legacy, repo),
                         [{"id": "MSVC2015", "name": "VC 2015", "path": "__redist/MSVC2015/VC_redist.x86.exe",
                           "arguments": "/q"}])

    def test_build_records_use_classification(self):
        repo = parse_dependency_repository(REPOSITORY)
        red, names = build_records.redist_entries(["ISI", "DOSBox074", "MSVC2017", "AdobeAir"], repo, {"ISI"})
        self.assertEqual([r["id"] for r in red], ["MSVC2017", "AdobeAir"])
        self.assertEqual(names, ["DOX Box 74", "Visual C++ Redist 2017", "Adobe Air"])

    def test_missing_ids_and_outdated(self):
        self.assertEqual(redist.missing_ids(["ISI", "DirectX"]), ["ISI", "DirectX"])
        self.put_cache(["ISI", "DirectX"], build_id="1")
        self.assertEqual(redist.missing_ids(["ISI", "DirectX", "MSVC2017"]), ["MSVC2017"])
        (redist.redist_root() / "__redist/DirectX/DXSETUP.exe").unlink()             # Datei gelöscht → fehlt
        self.assertEqual(redist.missing_ids(["ISI", "DirectX"]), ["DirectX"])
        self.assertFalse(redist.cache_outdated("1"))
        self.assertTrue(redist.cache_outdated("2"))
        self.assertEqual(redist.cache_info()["installed"], ["ISI", "DirectX"])


# ----------------------------------------------------------------------------- Befehle
class CommandTest(Base):
    def test_windows_paths_with_spaces_and_apostrophes(self):
        self.assertEqual(redist.to_windows_path("/mnt/games/GOG Games/No Man's Sky"),
                         "Z:\\mnt\\games\\GOG Games\\No Man's Sky")
        self.assertEqual(redist.to_windows_path(Path("/a/b/")), "Z:\\a\\b")
        with mock.patch.dict("os.environ", {"HOME": "/home/tester"}):
            self.assertEqual(redist.to_windows_path("~/x y"), "Z:\\home\\tester\\x y")

    def test_language(self):
        self.assertEqual((redist.language_name("de-DE"), redist.language_code("de-DE")), ("German", "de-DE"))
        self.assertEqual((redist.language_name("English"), redist.language_code("English")), ("English", "en-US"))
        self.assertEqual((redist.language_name("pt-BR"), redist.language_code("pt-BR")), ("Portuguese", "pt-BR"))
        self.assertEqual((redist.language_name("Deutsch"), redist.language_code("")), ("German", "en-US"))
        self.assertEqual(redist.language_name("xx-YY"), "English")

    def test_scriptinterpreter_argv_exact(self):
        argv = redist.isi_argv("/r/__redist/ISI/scriptinterpreter.exe", "Z:\\g\\No Man's Sky", "German", "1446213994",
                               "5555", "7.06_Cosmos_180836", "de-DE", "Z:\\s\\gog-support\\1446213994")
        self.assertEqual(argv, ["/r/__redist/ISI/scriptinterpreter.exe", "/VERYSILENT", "/DIR=Z:\\g\\No Man's Sky",
                                "/Language=German", "/LANG=German", "/ProductId=1446213994", "/galaxyclient",
                                "/buildId=5555", "/versionName=7.06_Cosmos_180836", "/lang-code=de-DE",
                                "/supportDir=Z:\\s\\gog-support\\1446213994", "/nodesktopshorctut",
                                "/nodesktopshortcut"])

    def test_temp_and_v1_argv(self):
        self.assertEqual(redist.temp_executable_argv("/s/setup.exe", "Z:\\g", "German", "de-DE", "200", "5", "1.0"),
                         ["/s/setup.exe", "/VERYSILENT", "/DIR=Z:\\g", "/Language=german", "/LANG=german",
                          "/lang-code=de-DE", "/ProductId=200", "/galaxyclient", "/buildId=5", "/versionName=1.0",
                          *ISI_ARGV_TAIL])
        argv = redist.v1_support_argv("/s/x.exe", "Z:\\g", "English", "100", "9", "")
        self.assertIn("/ProductId=100", argv)                       # echte Produkt-ID (Heroic übergibt die Sprache)
        self.assertEqual(argv[-2:], ISI_ARGV_TAIL)

    def test_redist_argv(self):
        self.assertEqual(redist.redist_argv("PHYSXLEGACY", "/r/__redist/PHYSXLEGACY/P.msi", "/passive /norestart"),
                         ["msiexec", "/i", "Z:\\r\\__redist\\PHYSXLEGACY\\P.msi", "/passive", "/norestart", "/qb"])
        self.assertEqual(redist.redist_argv("XNA", "/r/x.msi", "/qn"), ["msiexec", "/i", "Z:\\r\\x.msi", "/qn"])
        self.assertEqual(redist.redist_argv("DirectX", "/r/DXSETUP.exe", "/silent"), ["/r/DXSETUP.exe", "/silent"])
        self.assertEqual(redist.redist_argv("MSVC2005", "/r/vc.exe", MSVC2005_ARGS),
                         ["/r/vc.exe", "/q:a", "/c:VCREDI~3.EXE /q:a /c:msiexec /i vcredist.msi /qb "])   # shlex wie Heroic
        self.assertEqual(redist.redist_argv("X", "/r/x.exe", 'a "b'), ["/r/x.exe", "a", "b"])   # kaputte Quotes

    def test_build_steps_order_and_fallbacks(self):
        game = self.root / "Games" / "No Man's Sky"
        (game / "__redist" / "DirectX").mkdir(parents=True)
        (game / "__redist" / "DirectX" / "DXSETUP.exe").write_bytes(b"MZ")        # Kopie von 0.1.6 im Spielordner
        self.put_cache(["ISI", "MSVC2017"])
        rec = {"script_interpreter": True, "products": ["100", "200"],
               "support": [{"product_id": "200", "name": "Demo DLC", "temp_executable": "setup.exe"}]}
        entries = [{"id": "MSVC2017", "name": "VC 2017", "path": "__redist/MSVC2017/VC_redist.x86.exe",
                    "arguments": "/q"},
                   {"id": "DirectX", "name": "DirectX", "path": "__redist/DirectX/DXSETUP.exe", "arguments": "/silent"}]
        steps = redist.build_steps("100", "Demo", rec, game, build_id="5555", version_name="1.0", language="de-DE",
                                   redist=entries)
        self.assertEqual([s.key for s in steps], ["isi:100:5555", "isi:200:5555", "redist:MSVC2017", "redist:DirectX"])
        self.assertEqual(steps[1].title, "Installationsskript (ISI) für Demo DLC")
        self.assertEqual(steps[0].argv[0], str(redist.redist_root() / "__redist/ISI/scriptinterpreter.exe"))
        self.assertIn(f"/DIR=Z:{str(game).replace('/', chr(92))}", steps[0].argv)
        self.assertEqual(steps[0].cwd, str(game))
        self.assertEqual(steps[2].cwd, str(redist.redist_root()))                 # Startordner wie Heroic
        self.assertEqual(steps[3].exe, str(game / "__redist/DirectX/DXSETUP.exe"))  # Rückfall Spielordner
        self.assertTrue(all(s.is_script for s in steps[:2]) and not steps[2].is_script)
        rec["script_interpreter"] = False                                          # v2 ohne ISI → temp_executable
        steps = redist.build_steps("100", "Demo", rec, game, build_id="5555", version_name="1.0", language="de-DE")
        self.assertEqual([s.key for s in steps], ["temp:200:5555"])
        self.assertEqual(steps[0].exe, str(redist.support_dir("100") / "200" / "setup.exe"))

    def test_v1_support_commands(self):
        game = self.root / "Games" / "Alt"
        rec = {"products": ["100"], "support_commands": [
            {"gameID": "100", "executable": "/support/setup.exe", "languages": ["Neutral"]},
            {"gameID": "100", "executable": "support/lang_de.exe", "languages": ["German"]},
            {"gameID": "100", "executable": "support/lang_fr.exe", "languages": ["French"]}]}
        steps = redist.build_steps("100", "Alt", rec, game, build_id="77", version_name="1.2", language="de-DE")
        self.assertEqual([s.key for s in steps], ["v1:100:77:support/setup.exe", "v1:100:77:support/lang_de.exe"])
        self.assertEqual(steps[0].exe, str(redist.support_dir("100") / "100" / "support" / "setup.exe"))
        self.assertIn("/ProductId=100", steps[1].argv)                             # nicht die Sprache wie Heroic
        self.assertIn("/Language=german", steps[1].argv)
        self.assertTrue(all(s.is_script for s in steps))


# ----------------------------------------------------------------------------- Zustand und Wiederholungen
class StateTest(Base):
    def test_retry_limit_and_single_report(self):
        st = redist.load_setup_state(self.root / "pfx")
        self.assertTrue(redist.step_due(st, "redist:DirectX"))
        reports = [redist.record_result(st, "redist:DirectX", False, 1, "DirectX") for _ in range(redist.MAX_RETRIES)]
        self.assertEqual(reports, [False] * redist.MAX_RETRIES)
        self.assertTrue(redist.step_due(st, "redist:DirectX"))                       # 1 Versuch + 2 Wiederholungen
        self.assertTrue(redist.record_result(st, "redist:DirectX", False, 1))        # 4. Fehlschlag → aufgegeben, melden
        self.assertFalse(redist.step_due(st, "redist:DirectX"))
        self.assertTrue(redist.step_gave_up(st, "redist:DirectX"))
        self.assertFalse(redist.record_result(st, "redist:DirectX", False, 1))       # nur einmal melden
        self.assertTrue(redist.steps_finished(st, ["redist:DirectX"]))
        redist.reset_steps(st, ["redist:DirectX"])
        self.assertTrue(redist.step_due(st, "redist:DirectX"))
        self.assertEqual(st["reported"], [])
        redist.record_result(st, "redist:DirectX", False, 5)
        self.assertFalse(redist.record_result(st, "redist:DirectX", True, 0))        # Erfolg setzt Versuche zurück
        self.assertEqual(st["steps"]["redist:DirectX"]["tries"], 0)

    def test_state_file_roundtrip_and_tolerance(self):
        pfx = self.root / "pfx"
        pfx.mkdir()
        (pfx / redist.SETUP_STATE).write_text('{"steps": [], "products": "kaputt"}')
        st = redist.load_setup_state(pfx)
        self.assertEqual((st["steps"], st["products"], st["reported"]), ({}, {}, []))
        redist.record_result(st, "isi:1:2", True, 0, "ISI")
        redist.save_setup_state(pfx, st)
        self.assertTrue(redist.load_setup_state(pfx)["steps"]["isi:1:2"]["ok"])
        redist.request_rerun(pfx, "1")
        self.assertTrue(redist.load_setup_state(pfx)["products"]["1"]["rerun"])

    def test_decide_force(self):
        st = redist.load_setup_state(self.root / "pfx")
        self.assertEqual(redist.decide_force(st, "1", "5", [], "/g"), "")               # erster Start
        redist.mark_product(st, "1", "5", "/g")
        self.assertEqual(redist.decide_force(st, "1", "5", ["1:5"], "/g"), "")
        self.assertEqual(redist.decide_force(st, "1", "6", ["1:5"], "/g"), "")         # Update: neue Skript-Schlüssel
        self.assertEqual(redist.decide_force(st, "1", "5", ["1:5"], "/neu"), "scripts")  # verschoben
        self.assertEqual(redist.decide_force(st, "1", "5", [], "/g"), "all")          # setup_done gelöscht
        self.assertEqual(redist.decide_force(st, "1", "5", ["1:5"], "/g", prepare_only=True), "all")
        st["products"]["1"]["rerun"] = True
        self.assertEqual(redist.decide_force(st, "1", "5", ["1:5"], "/g"), "all")
        self.assertEqual(redist.updated_setup_done(["1:4", "2:9"], "1", "5"), ["2:9", "1:5"])
        self.assertEqual(redist.updated_setup_done(["1:4", "2:9"], "1"), ["2:9"])

    def test_migrate_and_status_rows(self):
        st = redist.load_setup_state(self.root / "pfx")
        self.assertEqual(redist.migrate_redist_done(st, ["DirectX", "MSVC2017"]), 2)
        redist.record_result(st, "isi:1:5", False, 2, "ISI")
        steps = [redist.SetupStep("isi:1:5", "isi", "ISI", [], "", ""),
                 redist.SetupStep("redist:DirectX", "redist", "DirectX", [], "", "", dep_id="DirectX"),
                 redist.SetupStep("redist:XNA", "redist", "XNA", [], "", "", dep_id="XNA")]
        rows = redist.status_rows(st, steps)
        self.assertEqual([(r["key"], r["state"]) for r in rows],
                         [("isi:1:5", "failed"), ("redist:DirectX", "ok"), ("redist:XNA", "pending"),
                          ("redist:MSVC2017", "other")])
        self.assertEqual(redist.summary_text(rows), "1 von 3 erledigt")
        self.assertEqual(redist.state_text(rows[0]),
                         "Fehlgeschlagen (Exit-Code 2) – neuer Versuch beim nächsten Start (1/4)")
        self.assertEqual(redist.state_text(rows[1]), "Installiert")


# ----------------------------------------------------------------------------- Gemeinsamer Ordner laden
class FakeDownloader:
    """Ersatz für ChunkDownloader: legt die Dateien an, ohne Netz."""
    calls: list = []

    def __init__(self, *_a, **_k):
        pass

    def download(self, jobs, root, *_a, **_k):
        FakeDownloader.calls.append(sorted(j.file.path for j in jobs))
        for j in jobs:
            p = j.dest(Path(root))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MZ")


class FakeRepoApi:
    def __init__(self, build_id="59705672826648994"):
        self.build_id = build_id
        self.calls: list = []

    def get_json(self, url, **kw):
        self.calls.append(("get", url, kw.get("authenticated")))
        return {"build_id": self.build_id, "repository_manifest": "https://cdn/repo"}

    def zlib_json(self, url):
        self.calls.append(("zlib", url))
        return json.loads(json.dumps(REPOSITORY))

    def dependency_link(self):
        self.calls.append(("link",))
        return "https://cdn/dep"

    def dependency_depot_manifest(self, md5):
        self.calls.append(("dep", md5))
        repo = parse_dependency_repository(REPOSITORY)
        dep = next(d for d in repo.values() if d.manifest == md5)
        return {"depot": {"items": [item(dep.executable_path or f"{dep.dependency_id}/x.dll")]}}

    def new_session(self):  # pragma: no cover
        raise AssertionError("kein Netz im Test")


class DownloadTest(Base):
    def setUp(self):
        super().setUp()
        FakeDownloader.calls = []
        self.dl_patch = mock.patch.object(redist, "ChunkDownloader", FakeDownloader)
        self.dl_patch.start()

    def tearDown(self):
        self.dl_patch.stop()
        super().tearDown()

    def test_download_keeps_isi_and_refreshes_on_new_build(self):
        api = FakeRepoApi()
        with self.assertLogs("galaxy_linux.content.redist", "WARNING"):
            got = redist.download_global_redists(api, ["MSVC2017", "DOSBox074", "Fehlt"])
        self.assertEqual(got, ["ISI", "MSVC2017"])                                   # ISI immer, nur globale
        self.assertIn(("get", f"{redist.CONTENT_SYSTEM}/dependencies/repository", False), api.calls)
        self.assertTrue((redist.redist_root() / "__redist/ISI/scriptinterpreter.exe").is_file())
        m = redist.load_cache_manifest()
        self.assertEqual((m["build_id"], m["installed"]), ("59705672826648994", ["ISI", "MSVC2017"]))
        self.assertEqual(len(m["depots"]), len(REPOSITORY["depots"]))                 # Repository für später (offline)
        api.calls.clear()
        self.assertEqual(redist.download_global_redists(api, ["MSVC2017"]), ["ISI", "MSVC2017"])
        self.assertNotIn(("link",), api.calls)                                       # aktuell → nichts geladen
        api2 = FakeRepoApi(build_id="2")                                             # neues Repository → erneut
        self.assertEqual(redist.download_global_redists(api2, ["DirectX"]), ["ISI", "DirectX", "MSVC2017"])
        self.assertIn(("link",), api2.calls)
        self.assertEqual(redist.load_cache_manifest()["build_id"], "2")

    def test_stale_files_removed(self):
        self.put_cache(["ISI"], build_id="1")
        old = redist.redist_root() / "__redist/Alt/alt.exe"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"MZ")
        m = redist.load_cache_manifest()
        m["files"] = ["__redist/Alt/alt.exe", "../ausserhalb.txt"]
        redist.cache_manifest_path().write_text(json.dumps(m))
        redist.download_global_redists(FakeRepoApi(), [])
        self.assertFalse(old.exists())
        self.assertFalse((redist.redist_root() / "__redist/Alt").exists())


# ----------------------------------------------------------------------------- Installer
BUILD_MANIFEST = {
    "version": 2, "buildId": "5555", "baseProductId": "100", "installDirectory": "Demo", "platform": "windows",
    "clientId": "c", "clientSecret": "s", "scriptInterpreter": True,
    "dependencies": ["ISI", "MSVC2017", "DOSBox074", "language_setup", "DirectX"],
    "products": [{"productId": "100", "name": "Demo"}, {"productId": "200", "name": "Demo DLC"}],
    "depots": [{"manifest": "d-base", "productId": "100", "languages": ["*"]},
               {"manifest": "d-dlc", "productId": "200", "languages": ["*"]}],
}
DEPOTS = {"d-base": [item("game.exe", "a1"), item("goggame-100.info", "a2"), item("support\\icon.ico", "a3", ["support"])],
          "d-dlc": [item("dlc.pak", "b1"), item("setup_dlc.exe", "b2", ["support", "executable"])]}


class FakeInstallApi(FakeRepoApi):
    def builds(self, pid, platform="windows"):
        return [{"build_id": "5555", "generation": 2, "link": "https://cdn/meta/5555", "version_name": "1.0"}]

    def zlib_json(self, url):
        if url == "https://cdn/meta/5555":
            return json.loads(json.dumps(BUILD_MANIFEST))
        return super().zlib_json(url)

    def owned_ids(self):
        return {"100", "200"}

    def secure_link(self, pid, path="/"):
        return f"https://cdn/{pid}"

    def depot_manifest(self, md5):
        return {"depot": {"items": DEPOTS[md5]}}

    def dependency_repository(self):
        return json.loads(json.dumps(REPOSITORY))


class InstallerTest(Base):
    def test_plan_splits_dependencies_and_support(self):
        api = FakeInstallApi()
        inst = InstallManager(api, GlobalSettings(install_dir=str(self.root / "Games")), InstalledRegistry())
        plan = inst.plan("100", "Demo")
        game_paths = sorted(j.file.path for j in plan.jobs if j.root is None)
        # Spieldateien + Spielordner-Pakete (internal: DOSBox, language_setup); globale Pakete nicht im Spielordner
        self.assertEqual(game_paths, ["DOSBox074/x.dll", "dlc.pak", "game.exe", "goggame-100.info",
                                      "language_setup/x.dll"])
        self.assertNotIn(("dep", "m-vc17"), api.calls)
        support = sorted((str(j.root), j.file.path) for j in plan.jobs if j.root is not None)
        self.assertEqual(support, [(str(redist.support_dir("100")), "100/support/icon.ico"),
                                   (str(redist.support_dir("100")), "200/setup_dlc.exe")])
        self.assertEqual([r["id"] for r in plan.redist], ["MSVC2017", "DirectX"])
        self.assertEqual(plan.game_dependencies, ["DOSBox074", "language_setup"])
        self.assertEqual(plan.dependency_names, ["Visual C++ Redist 2017", "DirectX 9.0c", "DOX Box 74", "Language Setup"])

        with mock.patch("galaxy_linux.content.installer.ChunkDownloader", FakeDownloader):
            inst.install(plan)
        rec = build_records.load_record("100")
        self.assertNotIn("100/support/icon.ico", rec["files"])
        self.assertIn("DOSBox074/x.dll", rec["files"])
        self.assertTrue((redist.support_dir("100") / "200" / "setup_dlc.exe").is_file())
        self.assertFalse((plan.install_dir / "setup_dlc.exe").exists())
        self.assertTrue(redist.support_ready("100", "5555"))
        self.assertEqual((rec["origin"], rec["script_interpreter"], rec["game_dependencies"]),
                         ("installer", True, ["DOSBox074", "language_setup"]))
        self.assertEqual(rec["product_names"], {"100": "Demo", "200": "Demo DLC"})
        steps = redist.build_steps("100", "Demo", rec, plan.install_dir, build_id="5555", version_name="1.0",
                                   language="en-US")
        self.assertEqual([s.title for s in steps][:2], ["Installationsskript (ISI) für Demo",
                                                        "Installationsskript (ISI) für Demo DLC"])
        inst.uninstall("100")
        self.assertFalse(redist.support_dir("100").exists())

    def test_support_file_job(self):
        f = DepotFile("support/a.exe", [], ["support"], sfc_ref=(0, 3))
        job = redist.support_file_job(f, "200", "https://e", "sfc", root=self.root / "s")
        self.assertIsInstance(job, FileJob)
        self.assertEqual((job.file.path, job.sfc_key, job.dest(self.root / "x")),
                         ("200/support/a.exe", "sfc", self.root / "s" / "200/support/a.exe"))
        self.assertEqual(f.path, "support/a.exe")                         # Original unverändert


# ----------------------------------------------------------------------------- Einrichtung im Prefix (Hook)
class SetupHookTest(Base):
    def setUp(self):
        super().setUp()
        self.game_dir = self.root / "Games" / "No Man's Sky"
        self.game_dir.mkdir(parents=True)
        self.prefix = self.root / "prefixes" / "100"
        self.gs = GameSettingsStore()
        self.settings = GlobalSettings(language="en-US")
        self.game = InstalledGame("100", "Demo", "windows", str(self.game_dir), build_id="5555", version_name="1.0",
                                  language="de-DE")
        self.registry = InstalledRegistry()
        self.registry.set(self.game)
        self.record = {"product_id": "100", "build_id": "5555", "client_id": "c", "client_secret": "s",
                       "platform": "windows", "language": "de-DE", "install_dir": str(self.game_dir),
                       "script_interpreter": True, "products": ["100", "200"], "dlcs": ["200"], "support": [],
                       "dependency_ids": ["ISI", "MSVC2017", "DOSBox074"], "version_name": "1.0",
                       "redist": [{"id": "MSVC2017", "name": "Visual C++ Redist 2017",
                                   "path": "__redist/MSVC2017/VC_redist.x86.exe", "arguments": "/install /quiet"}]}
        self.write_record()
        redist.write_support_marker("100", "5555", ["100", "200"], 0)
        self.statuses: list[str] = []
        self.rc = {}
        # Schritte laufen über Popen (eigene Prozessgruppe, BEENDEN-fähig); self.run zeichnet Befehl und Optionen auf
        # und liefert den Exit-Code – ein Wert None in self.rc heißt „läuft, bis BEENDEN“
        self.run = mock.Mock(side_effect=self.fake_run)
        self.run_patch = mock.patch.object(hook_redist, "_spawn",
                                           side_effect=lambda argv, **kw: FakePopen(self.run(argv, **kw).returncode))
        self.run_patch.start()
        self.term_patch = mock.patch.object(hook_redist, "terminate_group", return_value="term")
        self.terminate = self.term_patch.start()

    def tearDown(self):
        self.term_patch.stop()
        self.run_patch.stop()
        super().tearDown()

    def write_record(self):
        build_records.record_path("100").parent.mkdir(parents=True, exist_ok=True)
        build_records.record_path("100").write_text(json.dumps(self.record))

    def fake_run(self, argv, **kw):
        exe = argv[1] if argv[0] == "umu-run" else argv[0]
        name = Path(exe).name
        return types.SimpleNamespace(returncode=self.rc[name] if name in self.rc else 0)

    def ctx(self, prepare_only=False, api=None):
        launcher = types.SimpleNamespace(
            game_settings=self.gs, ui_bridge=None, registry=self.registry, settings=self.settings,
            prefix_for=lambda pid, gs=None: self.prefix,
            prefix_command=mock.Mock(side_effect=lambda pid, argv, verb, extra_env: (["umu-run", *argv],
                                                                                     {"PROTON_VERB": verb})),
            wineserver_command=mock.Mock(return_value=([sys.executable, "-c", "pass"], dict(os.environ))))
        self.launcher = launcher
        return LaunchContext(product_id="100", game=self.game, gs=self.gs.get("100"), settings=self.settings, plan=None,
                             install_dir=self.game_dir, exe=None, prefix=self.prefix, runner="umu", tool=None,
                             log_file=self.root / "logs" / "100.log", status=self.statuses.append,
                             ask=lambda *a: a[-1], launcher=launcher, api=api, prepare_only=prepare_only)

    def ran(self):
        return [Path(c.args[0][1]).name for c in self.run.call_args_list]

    def test_first_start_runs_isi_per_product_then_redists_once(self):
        self.put_cache(["ISI", "MSVC2017"])
        out = hook_redist.setup_prefix(self.ctx())
        self.assertTrue(out["finished"])
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe", "VC_redist.x86.exe"])
        isi = self.run.call_args_list[0].args[0]
        game_win = "Z:" + str(self.game_dir).replace("/", "\\")
        self.assertEqual(isi[1:], [str(redist.redist_root() / "__redist/ISI/scriptinterpreter.exe"), "/VERYSILENT",
                                   f"/DIR={game_win}", "/Language=German", "/LANG=German", "/ProductId=100",
                                   "/galaxyclient", "/buildId=5555", "/versionName=1.0", "/lang-code=de-DE",
                                   "/supportDir=" + redist.to_windows_path(redist.support_dir("100")),
                                   "/nodesktopshorctut", "/nodesktopshortcut"])
        self.assertIn("/ProductId=200", self.run.call_args_list[1].args[0])
        self.assertEqual(self.run.call_args_list[2].kwargs["cwd"], str(redist.redist_root()))
        self.assertEqual(self.run.call_args_list[0].kwargs["cwd"], str(self.game_dir))
        verbs = {c.kwargs["verb"] for c in self.launcher.prefix_command.call_args_list}
        self.assertEqual(verbs, {"run"})
        self.assertEqual(self.gs.get("100").setup_done, ["100:5555"])
        state = json.loads((self.prefix / redist.SETUP_STATE).read_text())
        self.assertEqual(state["products"]["100"]["build_id"], "5555")
        self.assertTrue(all(s["ok"] for s in state["steps"].values()))
        log = (self.root / "logs" / "100.log").read_text()
        self.assertIn("Einrichtung redist:MSVC2017: Exit-Code 0", log)
        self.run.reset_mock()
        out = hook_redist.setup_prefix(self.ctx())                                  # zweiter Start: nichts
        self.assertTrue(out["finished"])
        self.run.assert_not_called()

    def test_cancel_stops_before_next_step(self):
        self.put_cache(["ISI", "MSVC2017"])
        ctx = self.ctx()

        def run_and_cancel(argv, **kw):
            ctx.cancel.set()                                   # BEENDEN während des ersten Schritts
            return self.fake_run(argv, **kw)
        self.run.side_effect = run_and_cancel
        out = hook_redist.setup_prefix(ctx)
        self.assertEqual(self.ran(), ["scriptinterpreter.exe"])
        self.assertFalse(out["finished"])
        self.assertEqual(len(out["pending"]), 2)
        self.assertEqual(self.gs.get("100").setup_done, [])

    def test_cancel_during_running_step_is_not_a_failed_try(self):
        """BEENDEN während ISI läuft: Prozessgruppe beenden, wineserver -k, Schritt „abgebrochen“ statt Fehlversuch."""
        self.put_cache(["ISI", "MSVC2017"])
        self.rc["scriptinterpreter.exe"] = None                       # läuft, bis BEENDEN kommt
        ctx = self.ctx()
        threading.Timer(0.3, ctx.cancel.set).start()
        out = hook_redist.setup_prefix(ctx)
        self.assertEqual(self.ran(), ["scriptinterpreter.exe"])
        self.terminate.assert_called_once()
        self.launcher.wineserver_command.assert_called_once_with("100")
        self.assertEqual(out["cancelled"], ["isi:100:5555"])
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["pending"], ["isi:100:5555", "isi:200:5555", "redist:MSVC2017"])
        self.assertFalse(out["finished"])
        state = redist.load_setup_state(self.prefix)
        step = state["steps"]["isi:100:5555"]
        self.assertEqual((step["ok"], step["tries"]), (False, 0))
        self.assertTrue(redist.step_due(state, "isi:100:5555"))
        self.assertEqual(state["reported"], [])
        rows = hook_redist.prefix_status(self.launcher, "100")
        self.assertEqual([r["state"] for r in rows], ["cancelled", "pending", "pending"])
        self.assertIn("Abgebrochen", redist.state_text(rows[0]))
        self.assertIn("Einrichtung abgebrochen", self.statuses[-1])
        log = (self.root / "logs" / "100.log").read_text()
        self.assertIn("--- Abgebrochen (BEENDEN)", log)
        self.assertIn("Einrichtung isi:100:5555: abgebrochen", log)
        self.assertEqual(self.gs.get("100").setup_done, [])
        # nächster Start: alles läuft wieder, der abgebrochene Schritt zählt als Erstversuch
        self.rc["scriptinterpreter.exe"] = 0
        self.run.reset_mock()
        out = hook_redist.setup_prefix(self.ctx())
        self.assertTrue(out["finished"])
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe", "VC_redist.x86.exe"])
        step = redist.load_setup_state(self.prefix)["steps"]["isi:100:5555"]
        self.assertTrue(step["ok"])
        self.assertNotIn("cancelled", step)

    def test_cancel_after_failed_try_keeps_counter(self):
        self.put_cache(["ISI", "MSVC2017"])
        self.rc["VC_redist.x86.exe"] = 1603
        hook_redist.setup_prefix(self.ctx())                              # Fehlversuch 1
        self.rc["VC_redist.x86.exe"] = None
        ctx = self.ctx()
        threading.Timer(0.3, ctx.cancel.set).start()
        out = hook_redist.setup_prefix(ctx)
        self.assertEqual(out["cancelled"], ["redist:MSVC2017"])
        step = redist.load_setup_state(self.prefix)["steps"]["redist:MSVC2017"]
        self.assertEqual((step["tries"], step["rc"]), (1, 1603))           # kein weiterer Fehlversuch

    def test_failed_step_retried_three_times_then_reported_once(self):
        self.put_cache(["ISI", "MSVC2017"])
        self.rc["VC_redist.x86.exe"] = 1603
        with self.assertLogs("galaxy_linux.launch.hook_redist", "INFO"):
            for n in range(redist.MAX_RETRIES + 1):
                out = hook_redist.setup_prefix(self.ctx())
                self.assertEqual(out["failed"], ["redist:MSVC2017"], n)
                self.assertEqual(out["finished"], n == redist.MAX_RETRIES, n)        # aufgegeben = abgeschlossen
        self.assertEqual(self.ran().count("scriptinterpreter.exe"), 2)               # Skripte nur einmal
        self.assertEqual(self.ran().count("VC_redist.x86.exe"), redist.MAX_RETRIES + 1)
        reported = [s for s in self.statuses if "Exit-Code 1603" in s]
        self.assertEqual(len(reported), 1)
        self.assertEqual(self.gs.get("100").setup_done, ["100:5555"])               # aufgegeben = abgeschlossen
        self.run.reset_mock()
        hook_redist.setup_prefix(self.ctx())
        self.run.assert_not_called()
        rows = hook_redist.prefix_status(self.launcher, "100")
        self.assertEqual([r["state"] for r in rows], ["ok", "ok", "gave_up"])

    def test_offline_missing_programs_stay_pending_without_counting(self):
        out = hook_redist.setup_prefix(self.ctx())                                   # leerer Ordner, kein Netz
        self.run.assert_not_called()
        self.assertEqual(out["pending"], ["isi:100:5555", "isi:200:5555", "redist:MSVC2017"])
        state = redist.load_setup_state(self.prefix)
        self.assertEqual(state["steps"], {})
        self.assertEqual(self.gs.get("100").setup_done, [])
        self.put_cache(["ISI", "MSVC2017"])                                          # später online geladen
        self.assertTrue(hook_redist.setup_prefix(self.ctx())["finished"])

    def test_isi_waits_for_support_files(self):
        self.put_cache(["ISI", "MSVC2017"])
        (redist.support_dir("100") / redist.SUPPORT_MARKER).unlink()                 # z. B. übernommenes Spiel
        api = types.SimpleNamespace(logged_in=False)                                 # ohne Anmeldung nicht ladbar
        out = hook_redist.setup_prefix(self.ctx(api=api))
        self.assertEqual(self.ran(), ["VC_redist.x86.exe"])                          # Redistributables laufen schon
        self.assertEqual(out["pending"], ["isi:100:5555", "isi:200:5555"])
        self.assertNotIn("isi:100:5555", redist.load_setup_state(self.prefix)["steps"])   # kein Fehlversuch
        self.assertEqual(self.gs.get("100").setup_done, [])
        self.run.reset_mock()
        with mock.patch.object(redist, "download_support_files",
                               side_effect=lambda *a, **k: redist.write_support_marker("100", "5555", ["100"], 1)) as dl:
            out = hook_redist.setup_prefix(self.ctx(api=types.SimpleNamespace(logged_in=True)))
        dl.assert_called_once()
        self.assertTrue(out["finished"])
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe"])

    def test_update_reruns_scripts_only_and_rerun_request_repeats_all(self):
        self.put_cache(["ISI", "MSVC2017"])
        hook_redist.setup_prefix(self.ctx())
        self.run.reset_mock()
        self.record.update(build_id="6666", version_name="2.0")                      # Update
        self.write_record()
        redist.write_support_marker("100", "6666", ["100", "200"], 0)                # schreibt der Installer
        hook_redist.setup_prefix(self.ctx())
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe"])
        self.assertIn("/buildId=6666", self.run.call_args_list[0].args[0])
        self.assertEqual(self.gs.get("100").setup_done, ["100:6666"])
        self.run.reset_mock()
        hook_redist.request_rerun(self.launcher, "100")                              # „Einrichtung erneut ausführen“
        self.assertEqual(self.gs.get("100").setup_done, [])
        hook_redist.setup_prefix(self.ctx())
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe", "VC_redist.x86.exe"])
        self.run.reset_mock()
        hook_redist.setup_prefix(self.ctx())                                         # Merker verbraucht
        self.run.assert_not_called()
        hook_redist.setup_prefix(self.ctx(prepare_only=True))                        # galaxy-linux prefix setup
        self.assertEqual(len(self.ran()), 3)

    def test_update_installs_new_redists_per_prefix(self):
        """Anders als Heroic: neue Redistributables aus einem Update kommen auch in bestehende Prefixe."""
        self.put_cache(["ISI", "MSVC2017", "DirectX"])
        hook_redist.setup_prefix(self.ctx())
        self.run.reset_mock()
        self.record.update(build_id="6666", dependency_ids=["ISI", "MSVC2017", "DirectX"])
        self.record["redist"].append({"id": "DirectX", "name": "DirectX 9.0c", "path": "__redist/DirectX/DXSETUP.exe",
                                      "arguments": "/silent"})
        self.write_record()
        redist.write_support_marker("100", "6666", ["100", "200"], 0)
        self.assertTrue(hook_redist.setup_prefix(self.ctx())["finished"])
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe", "DXSETUP.exe"])
        steps = redist.load_setup_state(self.prefix)["steps"]
        self.assertTrue(steps["redist:DirectX"]["ok"] and steps["redist:MSVC2017"]["ok"])

    def test_moved_install_reruns_scripts(self):
        self.put_cache(["ISI", "MSVC2017"])
        hook_redist.setup_prefix(self.ctx())
        self.run.reset_mock()
        new = self.root / "Andere" / "Demo"
        self.game_dir.rename(self.root / "tmp")
        new.parent.mkdir()
        (self.root / "tmp").rename(new)
        self.game_dir = new
        self.game.install_path = str(new)
        hook_redist.setup_prefix(self.ctx())
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe"])

    def test_migrates_redist_done_from_old_prefix(self):
        self.put_cache(["ISI", "MSVC2017"])
        (self.prefix / "pfx").mkdir(parents=True)
        (self.prefix / "pfx" / "system.reg").write_text("WINE REGISTRY")
        self.gs.update("100", redist_done=["MSVC2017"])
        hook_redist.setup_prefix(self.ctx())
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe"])
        self.assertEqual(self.gs.get("100").redist_done, [])                      # einmal übernommen, dann weg

    def test_legacy_redist_done_not_applied_to_new_prefix(self):
        """Merker aus 0.1.x gilt nicht für einen später neu angelegten Prefix (anderer Prefix-Ordner o. Ä.)."""
        self.put_cache(["ISI", "MSVC2017"])
        (self.prefix / "pfx").mkdir(parents=True)
        (self.prefix / "pfx" / "system.reg").write_text("WINE REGISTRY")
        self.gs.update("100", redist_done=["MSVC2017"], setup_done=["100:4444"])  # Einrichtung lief schon (alter Prefix)
        hook_redist.setup_prefix(self.ctx())
        self.assertIn("VC_redist.x86.exe", self.ran())
        self.assertEqual(self.gs.get("100").redist_done, [])

    def test_expired_login_offline_keeps_other_steps(self):
        """Abgelaufenes Token ohne Netz: AuthError aus builds()/secure_link() lässt nur die betroffenen Schritte offen."""
        from galaxy_linux.api.auth import AuthError
        self.put_cache(["ISI", "MSVC2017"])
        self.record.pop("script_interpreter")                                    # Datensatz von 0.1.6
        self.write_record()
        redist.write_support_marker("100", "5555", [], 0)
        (redist.support_dir("100") / ".galaxy-linux-support.json").unlink(missing_ok=True)
        api = types.SimpleNamespace(logged_in=True, builds=mock.Mock(side_effect=AuthError("offline")),
                                    zlib_json=mock.Mock(side_effect=AuthError("offline")))
        with self.assertLogs("galaxy_linux", "WARNING"):
            out = hook_redist.setup_prefix(self.ctx(api=api))
        self.assertIn("VC_redist.x86.exe", self.ran())
        self.assertEqual(out["failed"], [])

    def test_redists_switched_off_scripts_still_run(self):
        self.put_cache(["ISI", "MSVC2017"])
        self.gs.update("100", install_redist=False)
        hook_redist.setup_prefix(self.ctx())
        self.assertEqual(self.ran(), ["scriptinterpreter.exe", "scriptinterpreter.exe"])

    def test_missing_cache_uses_job_and_waits(self):
        backend = types.SimpleNamespace(api=object(), settings=types.SimpleNamespace(max_workers=2),
                                        registry=self.registry, installer=None, updates={})
        backend.jobs = JobQueue(backend)
        hook_redist.attach_backend(backend)
        fetched = []

        def fake_download(api, ids, **kw):
            fetched.append((threading.current_thread().name, list(ids)))
            self.put_cache(["ISI", "MSVC2017"])
            return ["ISI", "MSVC2017"]
        with mock.patch.object(redist, "download_global_redists", side_effect=fake_download):
            out = hook_redist.setup_prefix(self.ctx(api=object()))
        self.assertTrue(out["finished"])
        self.assertEqual(fetched, [(threading.current_thread().name, ["ISI", "MSVC2017"])])   # sofort, nicht in der Warteschlange
        job = backend.jobs.jobs[-1]
        self.assertEqual((job.kind, job.state, job.title, job.label), ("redist", "done", "Galaxy-Redistributables",
                                                                      "Galaxy-Redistributables"))
        self.assertIs(JOB_KINDS["redist"].network, False)


class FakePopen:
    """Ersatz für subprocess.Popen: ``returncode`` None = läuft, bis ``terminate_group`` (gepatcht) es beendet."""

    def __init__(self, returncode):
        self.returncode = returncode
        self.pid = None                         # nie an os.killpg geben

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class CancellableStepTest(Base):
    """run_step mit echten, harmlosen Prozessen (sh/sleep): eigene Prozessgruppe, SIGTERM, SIGKILL nach der Frist."""

    def setUp(self):
        super().setUp()
        self.prefix = self.root / "prefix"
        self.procs = []
        self.ws = mock.Mock(return_value=([sys.executable, "-c", "pass"], dict(os.environ)))

    def ctx(self, script):
        launcher = types.SimpleNamespace(
            prefix_command=lambda pid, argv, verb, extra_env: (["sh", "-c", script], dict(os.environ)),
            wineserver_command=self.ws)
        return LaunchContext(product_id="100", game=None, gs=None, settings=None, plan=None, install_dir=self.root,
                             exe=None, prefix=self.prefix, runner="umu", tool=None, log_file=self.root / "logs" / "100.log",
                             status=lambda t: None, ask=lambda *a: a[-1], launcher=launcher)

    def step(self):
        return redist.SetupStep("isi:100:1", "isi", "ISI", ["scriptinterpreter.exe"], str(self.root), "isi.exe")

    def run_cancelled(self, script, after=0.3):
        real = subprocess.Popen

        def spawn(argv, **kw):
            p = real(argv, **kw)
            self.procs.append(p)
            return p
        ctx = self.ctx(script)
        threading.Timer(after, ctx.cancel.set).start()
        t0 = time.monotonic()
        with mock.patch.object(hook_redist, "_spawn", side_effect=spawn):
            with self.assertRaises(hook_redist.StepCancelled):
                hook_redist.run_step(ctx, self.step())
        return time.monotonic() - t0

    def assert_group_gone(self):
        self.assertEqual(len(self.procs), 1)
        with self.assertRaises(ProcessLookupError):
            os.killpg(self.procs[0].pid, 0)

    def test_cancel_terminates_whole_group(self):
        # Kindprozess im Hintergrund wie Wine-Programme hinter umu-run
        took = self.run_cancelled("sleep 30 & sleep 30; wait")
        self.assertLess(took, 4)
        self.assert_group_gone()
        self.ws.assert_called_once_with("100")
        log = (self.root / "logs" / "100.log").read_text()
        self.assertIn("--- Abgebrochen (BEENDEN)", log)
        self.assertNotIn("Exit-Code", log)

    def test_sigkill_after_grace_when_sigterm_is_ignored(self):
        with mock.patch.object(hook_redist, "TERM_GRACE", 0.5):
            took = self.run_cancelled("trap '' TERM; sleep 30 & sleep 30; wait")
        self.assertGreaterEqual(took, 0.7)
        self.assertLess(took, 4)
        self.assert_group_gone()

    def test_normal_exit_and_timeout(self):
        ctx = self.ctx("exit 3")
        self.assertEqual(hook_redist.run_step(ctx, self.step()), 3)
        self.ws.assert_not_called()
        ctx = self.ctx("sleep 30")
        with mock.patch.object(hook_redist, "TERM_GRACE", 0.5):
            self.assertEqual(hook_redist.run_step(ctx, self.step(), timeout=0.3), -1)
        self.ws.assert_called_once()                                 # auch nach Zeitüberschreitung aufräumen
        self.assertIn("--- Zeitüberschreitung", (self.root / "logs" / "100.log").read_text())

    def test_missing_program_is_exit_code_minus_two(self):
        ctx = self.ctx("")
        ctx.launcher.prefix_command = lambda *a, **k: (["/nonexistent/umu-run"], dict(os.environ))
        self.assertEqual(hook_redist.run_step(ctx, self.step()), -2)
        self.ws.assert_not_called()


class JobTest(Base):
    def test_run_now_and_queue_dedup(self):
        backend = types.SimpleNamespace(installer=None, updates={}, registry=InstalledRegistry(), api=None,
                                        settings=types.SimpleNamespace(max_workers=1, install_redist=True))
        backend.jobs = JobQueue(backend)
        gate = threading.Event()
        with mock.patch.object(redist, "download_global_redists", side_effect=lambda *a, **k: gate.wait(5) and ["ISI"]):
            j1 = hook_redist.queue_redist_job(backend)
            j2 = hook_redist.queue_redist_job(backend)
            self.assertIs(j1, j2)                                                     # nur ein Auftrag
            gate.set()
            for _ in range(200):
                if not j1.active:
                    break
                threading.Event().wait(0.02)
        self.assertEqual(j1.state, "done")
        self.assertEqual(j1.result, ["ISI"])
        job = Job(kind="redist", product_id="gog-redist", title="x", data={"ids": [], "full": False})
        with mock.patch.object(redist, "download_global_redists", return_value=["ISI"]) as dl:
            backend.jobs.run_now(job)
        self.assertEqual((job.state, dl.call_args.args[1]), ("done", []))

    def test_update_needed_checks(self):
        backend = types.SimpleNamespace(registry=InstalledRegistry(), api=FakeRepoApi(build_id="2"),
                                        settings=types.SimpleNamespace(install_redist=True))
        self.assertFalse(hook_redist.redist_update_needed(backend))                  # keine Windows-Spiele
        backend.registry.set(InstalledGame("100", "Demo", "windows", str(self.root)))
        build_records.record_path("100").parent.mkdir(parents=True, exist_ok=True)
        build_records.record_path("100").write_text(json.dumps({"build_id": "1", "dependency_ids": ["MSVC2017"]}))
        self.assertTrue(hook_redist.redist_update_needed(backend))                   # fehlt
        self.put_cache(["ISI", "MSVC2017"], build_id="2")
        self.assertFalse(hook_redist.redist_update_needed(backend))
        self.put_cache(["ISI", "MSVC2017"], build_id="1")
        self.assertTrue(hook_redist.redist_update_needed(backend))                   # Repository geändert
        backend.settings.install_redist = False
        self.assertFalse(hook_redist.redist_update_needed(backend))
        backend.settings.install_redist = True
        (redist.redist_root() / "__redist/MSVC2017/VC_redist.x86.exe").unlink()      # fehlt – aber offline:
        backend.api = mock.Mock(get_json=mock.Mock(side_effect=ApiError("offline")))
        self.assertFalse(hook_redist.redist_update_needed(backend))                  # kein Auftrag, der nur scheitert


class UpgradeTest(Base):
    def test_old_record_is_completed_once(self):
        old = {"product_id": "100", "build_id": "5555", "client_id": "c", "redist": [], "files": ["game.exe"]}
        self.assertTrue(redist.needs_upgrade(old))
        build_records.record_path("100").parent.mkdir(parents=True, exist_ok=True)
        game = InstalledGame("100", "Demo", "windows", str(self.root), version_name="1.0")
        rec = redist.upgrade_record(FakeInstallApi(), game, old)
        self.assertEqual((rec["script_interpreter"], rec["files"], rec["products"], rec["manifest_link"]),
                         (True, ["game.exe"], ["100"], "https://cdn/meta/5555"))
        self.assertFalse(redist.needs_upgrade(rec))
        self.assertEqual(build_records.load_record("100"), rec)


if __name__ == "__main__":
    unittest.main()
