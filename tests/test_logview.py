"""Protokoll-Fenster (Logik ohne GTK): Einträge, Stufen, Suche, Ende lesen, Folgen, Leeren, Quellen."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux import logview

MAIN = """2026-10-23 09:18:03,329 INFO  [gl-goverlay] galaxy_linux.launch.goverlay_profiles: GOverlay-Cover angelegt
2026-10-23 09:18:04,000 DEBUG [MainThread] galaxy_linux.backend: Einstellungen geladen
2026-10-23 09:18:05,100 WARNING [gl-ui_0] galaxy_linux.backend: Metadaten für 1 nicht verfügbar
2026-10-23 09:18:06,200 ERROR [gl-ui_1] galaxy_linux: Ausnahme im Thread gl-ui_1
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: kaputt
2026-10-23 09:18:07,300 INFO  [MainThread] galaxy_linux.api: GET https://api.gog.com/x?access_token=geheim123
2026-10-23 09:18:08,400 CRITICAL [MainThread] galaxy_linux: Unbehandelte Ausnahme
""".splitlines()


class RecordsTest(unittest.TestCase):
    def test_levels_and_tracebacks(self):
        recs = logview.split_records(MAIN)
        self.assertEqual([r.level for r in recs], [20, 10, 30, 40, 20, 50])
        self.assertEqual(len(recs[3].lines), 4)                          # Traceback gehört zum Fehler
        self.assertEqual(len(logview.filter_records(recs, "all")), 6)
        self.assertEqual([r.level for r in logview.filter_records(recs, "warning")], [30, 40, 50])
        self.assertEqual([r.level for r in logview.filter_records(recs, "error")], [40, 50])
        hits = logview.filter_records(recs, "all", "VALUEERROR")
        self.assertEqual(len(hits), 1)
        self.assertIn("Traceback", hits[0].text)                         # Suche findet den ganzen Eintrag
        self.assertEqual(logview.filter_records(recs, "error", "cover"), [])

    def test_game_log_levels_are_guessed(self):
        recs = logview.split_records(["0024:err:module:import_dll Library X not found", "0024:fixme:ntdll:stub",
                                      "Spiel gestartet", "Exception in thread main"])
        self.assertEqual([r.level for r in recs], [40, 30, 0, 40])
        self.assertEqual(len(logview.filter_records(recs, "warning")), 3)

    def test_render_masks_tokens(self):
        text = logview.render(logview.split_records(MAIN))
        self.assertNotIn("geheim123", text)
        self.assertIn("access_token=***", text)

    def test_append_lines_merges_continuations(self):
        recs = logview.split_records(MAIN[:4])
        added, merged = logview.append_lines(recs, ["Traceback (most recent call last):", "  File y", MAIN[7]])
        self.assertTrue(merged)
        self.assertEqual(len(recs[-2].lines), 3)
        self.assertEqual(len(added), 1)
        added, merged = logview.append_lines(recs, [MAIN[0]])
        self.assertEqual((len(added), merged), (1, False))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.patch = mock.patch("galaxy_linux.paths.LOG_DIR", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_tail_lines(self):
        f = self.dir / "galaxy-linux.log"
        f.write_text("".join(f"Zeile {i}\n" for i in range(20000)))
        self.assertEqual(logview.tail_lines(f, 3), ["Zeile 19997", "Zeile 19998", "Zeile 19999"])
        self.assertEqual(len(logview.tail_lines(f, 3000)), 3000)
        self.assertEqual(logview.tail_lines(f, 3000)[0], "Zeile 17000")
        self.assertEqual(logview.tail_lines(self.dir / "fehlt.log"), [])
        small = self.dir / "klein.log"
        small.write_text("a\nb\n")
        self.assertEqual(logview.tail_lines(small, 3000), ["a", "b"])

    def test_follower_append_clear_rotate(self):
        f = self.dir / "galaxy-linux.log"
        f.write_text("alt\n")
        fol = logview.Follower(f)
        fol.start_at_end()
        self.assertEqual(fol.read_new(), [])
        with open(f, "a") as fh:
            fh.write("neu 1\nhalb")
        self.assertEqual(fol.read_new(), ["neu 1"])
        with open(f, "a") as fh:
            fh.write("e Zeile\n")
        self.assertEqual(fol.read_new(), ["halbe Zeile"])
        logview.clear(f)                                                 # „Leeren“
        self.assertEqual(f.read_text(), "")
        self.assertEqual(fol.read_new(), [])
        self.assertTrue(fol.reset_flag)
        with open(f, "a") as fh:
            fh.write("nach dem Leeren\n")
        self.assertEqual(fol.read_new(), ["nach dem Leeren"])
        os.replace(f, self.dir / "galaxy-linux.log.1")                   # Rotation
        f.write_text("rotiert\n")
        self.assertEqual(fol.read_new(), ["rotiert"])
        self.assertTrue(fol.reset_flag)

    def test_sources(self):
        for name in ("galaxy-linux.log", "galaxy-linux.log.1", "comet.log", "ext-freelancer.log", "1207659039.log"):
            (self.dir / name).write_text("x\n")
        srcs = logview.sources({"1207659039": "Zeus and Poseidon", "ext-freelancer": "Freelancer"})
        self.assertEqual([s.label for s in srcs], ["galaxy-linux.log", "comet.log", "Freelancer", "Zeus and Poseidon"])
        self.assertEqual(srcs[0].path, self.dir / "galaxy-linux.log")


if __name__ == "__main__":
    unittest.main()
