"""Selbst-Update (clientinfo): Versionsvergleich, Auswahl der Release-Dateien, Prüfsumme (HTTP nachgestellt),
Entscheidung pacman-Paket ↔ Benutzerinstallation/Quellordner, pkexec-Befehl und -Ergebnisse, Neustart-Befehl.
Nie ein echter Download, kein pkexec, kein pacman -U."""
import hashlib
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from local_cluster import clientinfo

REPO = "Cue-System90/gog_like_client_Arch_linux"
DL = f"https://github.com/{REPO}/releases/download/v0.5.0/"
PKG = b"\x28\xb5\x2f\xfd" + b"local-cluster-0.5.0" * 5000           # Inhalt egal, nur die Prüfsumme zählt
PKG_SHA = hashlib.sha256(PKG).hexdigest()


class Resp:
    def __init__(self, status=200, data=None, text="", body=b""):
        self.status_code, self._data, self.text, self.body = status, data, text, body
        self.headers = {"Content-Length": str(len(body))} if body else {}
        self.closed = False

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._data

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


def api_release(tag="v0.5.0", assets=None, body="## 0.5.0\n\n- **Neu**: Selbst-Update"):
    names = assets if assets is not None else ["local-cluster-0.5.0-1-any.pkg.tar.zst", "SHA256SUMS",
                                               "local-cluster-0.5.0-src.tar.gz"]
    return {"tag_name": tag, "html_url": f"https://github.com/{REPO}/releases/tag/{tag}", "body": body,
            "published_at": "2026-10-23T12:00:00Z",
            "assets": [{"name": n, "browser_download_url": DL + n} for n in names]}


class VersionTest(unittest.TestCase):
    def test_newer(self):
        newer = clientinfo.is_newer
        self.assertTrue(newer("v0.4.1", "0.4.0"))
        self.assertTrue(newer("v0.10.0", "0.9.9"))
        self.assertTrue(newer("1.0", "0.99.99"))
        self.assertTrue(newer("v0.4.0.1", "0.4.0"))
        self.assertFalse(newer("v0.4.0", "0.4.0"))
        self.assertFalse(newer("0.4", "0.4.0"))
        self.assertFalse(newer("v0.3.9", "0.4.0"))
        self.assertFalse(newer("", "0.4.0"))
        self.assertFalse(newer("latest", "0.4.0"))

    def test_should_check_once_a_day(self):
        day = clientinfo.CHECK_INTERVAL
        self.assertTrue(clientinfo.should_check(0, 1_000_000))
        self.assertFalse(clientinfo.should_check(1_000_000, 1_000_000 + day - 1))
        self.assertTrue(clientinfo.should_check(1_000_000, 1_000_000 + day))
        self.assertTrue(clientinfo.should_check(1_000_000, 999_000))      # Uhr zurückgestellt


class ReleaseTest(unittest.TestCase):
    def check(self, data, status=200, current="0.4.0"):
        s = mock.Mock()
        s.get.return_value = Resp(status, data)
        return clientinfo.check_for_update(current, session=s)

    def test_check_reads_release_notes_and_assets(self):
        r = self.check(api_release())
        self.assertEqual((r.state, r.latest), ("newer", "0.5.0"))
        rel = r.release
        self.assertEqual(rel.notes, "## 0.5.0\n\n- **Neu**: Selbst-Update")
        self.assertEqual(rel.url, f"https://github.com/{REPO}/releases/tag/v0.5.0")
        self.assertEqual(clientinfo.package_asset(rel), ("local-cluster-0.5.0-1-any.pkg.tar.zst",
                                                         DL + "local-cluster-0.5.0-1-any.pkg.tar.zst"))
        self.assertEqual(clientinfo.sums_asset(rel), ("SHA256SUMS", DL + "SHA256SUMS"))
        self.assertEqual(self.check(api_release("v0.4.0")).state, "current")
        self.assertEqual(self.check(None, 404).state, "none")

    def test_asset_selection(self):
        rel = clientinfo.release_from_api(api_release(assets=[
            "local-cluster-0.5.0-1-any.pkg.tar.zst", "local-cluster-0.5.0-3-any.pkg.tar.zst",
            "local-cluster-0.4.9-7-any.pkg.tar.zst",                 # andere Version
            "local-cluster-0.5.0-2-x86_64.pkg.tar.zst",              # andere Architektur
            "local-cluster-0.5.0-9-any.pkg.tar.zst.sig", "SHA256SUMS",
            "galaxy-linux-0.5.0-8-any.pkg.tar.zst",              # Übergangspaket – nur für Clients bis 0.4.1
            "milkyway-0.5.0-9-any.pkg.tar.zst"]))                # gleiches Paket für Milkyway 0.5.x
        self.assertEqual(clientinfo.package_asset(rel)[0], "local-cluster-0.5.0-3-any.pkg.tar.zst")   # höchstes pkgrel
        rel.assets = {"local-cluster-0.5.0-1-any.pkg.tar.zst": "https://evil.example/local-cluster-0.5.0-1-any.pkg.tar.zst",
                      "SHA256SUMS": "https://evil.example/SHA256SUMS"}
        self.assertIsNone(clientinfo.package_asset(rel))          # nur Downloads dieses Repositorys
        self.assertIsNone(clientinfo.sums_asset(rel))
        self.assertIsNone(clientinfo.package_asset(clientinfo.release_from_api(api_release(assets=[]))))

    def test_parse_sha256sums(self):
        text = (f"{PKG_SHA}  local-cluster-0.5.0-1-any.pkg.tar.zst\n{'B' * 64} *install.sh\nkaputt\n"
                f"{'c' * 63}  zu-kurz\n")
        self.assertEqual(clientinfo.parse_sha256sums(text), {"local-cluster-0.5.0-1-any.pkg.tar.zst": PKG_SHA,
                                                             "install.sh": "b" * 64})


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dest = Path(self.td.name) / "client-update"
        self.rel = clientinfo.release_from_api(api_release())

    def tearDown(self):
        self.td.cleanup()

    def session(self, sums=None, body=PKG, sums_status=200):
        s = mock.Mock()
        name = "local-cluster-0.5.0-1-any.pkg.tar.zst"
        sums = f"{PKG_SHA}  {name}\n{'0' * 64}  local-cluster-0.5.0-src.tar.gz\n" if sums is None else sums

        def get(url, **kw):
            if url.endswith("SHA256SUMS"):
                return Resp(sums_status, text=sums)
            self.assertTrue(kw.get("stream"))
            self.pkg_resp = Resp(body=body)
            return self.pkg_resp
        s.get.side_effect = get
        return s

    def test_download_verifies_hash(self):
        self.dest.mkdir(parents=True)
        (self.dest / "local-cluster-0.4.0-1-any.pkg.tar.zst").write_bytes(b"alt")      # alter Download
        seen = []
        path = clientinfo.download_update(self.rel, self.dest, session=self.session(),
                                          progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(path, self.dest / "local-cluster-0.5.0-1-any.pkg.tar.zst")
        self.assertEqual(clientinfo.sha256_file(path), PKG_SHA)
        self.assertEqual(seen[-1], (len(PKG), len(PKG)))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), [path.name])
        self.assertTrue(self.pkg_resp.closed)

    def test_wrong_hash_discards_file(self):
        with self.assertRaises(clientinfo.UpdateCheckError) as cm:
            clientinfo.download_update(self.rel, self.dest, session=self.session(body=PKG + b"manipuliert"))
        self.assertIn("Prüfsumme", str(cm.exception))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_missing_checksum_or_assets(self):
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(self.rel, self.dest, session=self.session(sums=f"{'1' * 64}  anderes.zst\n"))
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(self.rel, self.dest, session=self.session(sums_status=404))
        no_sums = clientinfo.release_from_api(api_release(assets=["local-cluster-0.5.0-1-any.pkg.tar.zst"]))
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(no_sums, self.dest, session=self.session())
        no_pkg = clientinfo.release_from_api(api_release(assets=["SHA256SUMS"]))
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(no_pkg, self.dest, session=self.session())

    def test_cancel_and_network_error(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(self.rel, self.dest, session=self.session(), cancel=cancel)
        self.assertEqual(list(self.dest.iterdir()), [])
        s = mock.Mock()
        s.get.side_effect = requests.ConnectionError("offline")
        with self.assertRaises(clientinfo.UpdateCheckError):
            clientinfo.download_update(self.rel, self.dest, session=s)


class FlowTest(unittest.TestCase):
    def test_update_method(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "local_cluster"
            (src / "local_cluster").mkdir(parents=True)
            (src / "pyproject.toml").write_text("[project]\n")
            run = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
            self.assertEqual(clientinfo.update_method(src / "local_cluster", run), "source")
            user = Path(d) / ".local/lib/python3.14/site-packages/local_cluster"
            user.mkdir(parents=True)
            self.assertEqual(clientinfo.update_method(user, run), "user")
            run.assert_not_called()                                  # außerhalb /usr nie pacman fragen
        system = Path("/usr/lib/python3.14/site-packages/local_cluster")
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
        self.assertEqual(clientinfo.update_method(system, run), "pacman")
        self.assertEqual(run.call_args.args[0], ["pacman", "-Qq", "local-cluster"])
        # Übergangspaket galaxy-linux (Selbst-Update aus 0.4.x): auch ein pacman-Paket
        run = mock.Mock(side_effect=lambda cmd, **kw: types.SimpleNamespace(returncode=0 if cmd[2] == "galaxy-linux" else 1))
        self.assertEqual(clientinfo.update_method(system, run), "pacman")
        self.assertEqual([c.args[0][2] for c in run.call_args_list], ["local-cluster", "milkyway", "galaxy-linux"])
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=1))       # z. B. pip install nach /usr
        self.assertEqual(clientinfo.update_method(system, run), "user")
        run.side_effect = FileNotFoundError("pacman")
        self.assertEqual(clientinfo.update_method(system, run), "user")

    def test_update_action(self):
        rel = clientinfo.release_from_api(api_release())
        self.assertEqual(clientinfo.update_action("pacman", rel), "install")
        self.assertEqual(clientinfo.update_action("user", rel), "open")
        self.assertEqual(clientinfo.update_action("source", rel), "open")
        self.assertEqual(clientinfo.update_action("pacman", None), "open")
        bare = clientinfo.release_from_api(api_release(assets=["local-cluster-0.5.0-src.tar.gz"]))
        self.assertEqual(clientinfo.update_action("pacman", bare), "open")    # ohne Paket: Seite öffnen

    def test_install_command_and_results(self):
        path = Path("/tmp/x/local-cluster-0.5.0-1-any.pkg.tar.zst")
        self.assertEqual(clientinfo.install_command(path),
                         ["pkexec", "pacman", "-U", "--noconfirm", "--ask=4", str(path)])   # Konflikt galaxy-linux: Ja
        for rc, ok, word in ((0, True, "installiert"), (126, False, "Abgebrochen"), (127, False, "Nicht berechtigt"),
                             (1, False, "Exit-Code 1")):
            run = mock.Mock(return_value=types.SimpleNamespace(returncode=rc, stdout="out", stderr="err"))
            res = clientinfo.install_package(path, run=run)
            self.assertEqual((res.ok, res.code), (ok, rc))
            self.assertIn(word, res.message)
            self.assertEqual(run.call_args.args[0][:2], ["pkexec", "pacman"])
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)   # nie ein Passwort über stdin
        res = clientinfo.install_package(path, run=mock.Mock(side_effect=FileNotFoundError("pkexec")))
        self.assertEqual((res.ok, res.code), (False, -2))
        res = clientinfo.install_package(path, run=mock.Mock(side_effect=subprocess.TimeoutExpired("pkexec", 1)))
        self.assertEqual((res.ok, res.code), (False, -1))

    def test_restart_command(self):
        self.assertEqual(clientinfo.restart_command(lambda n: "/usr/bin/local-cluster", "/usr/bin/python3"),
                         ["/usr/bin/local-cluster"])
        self.assertEqual(clientinfo.restart_command(lambda n: None, "/usr/bin/python3"),
                         ["/usr/bin/python3", "-m", "local_cluster"])


if __name__ == "__main__":
    unittest.main()
