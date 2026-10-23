"""GLib-Log-Brücke (logsetup.glib_writer): nur Warnungen und schlimmer ins Protokoll, Infos/Debug nur im Debug-Modus;
GLibs eigene Ausgabe läuft immer weiter."""
import logging
import os
import unittest
from unittest import mock

from galaxy_linux import logsetup

ERROR, CRITICAL, WARNING, MESSAGE, INFO, DEBUG = 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
FATAL, RECURSION = 1 << 1, 1


class LogBridgeTest(unittest.TestCase):
    def test_levels(self):
        lvl = logsetup.glib_bridge_level
        self.assertEqual(lvl(ERROR | FATAL), logging.CRITICAL)
        self.assertEqual(lvl(CRITICAL), logging.ERROR)
        self.assertEqual(lvl(WARNING | RECURSION), logging.WARNING)
        for flags in (MESSAGE, INFO, DEBUG):
            self.assertIsNone(lvl(flags), flags)
        self.assertEqual(lvl(INFO, debug=True), logging.INFO)
        self.assertEqual(lvl(MESSAGE, debug=True), logging.INFO)
        self.assertEqual(lvl(DEBUG, debug=True), logging.DEBUG)
        self.assertEqual(lvl(0), logging.WARNING)                 # unbekannte Stufe: lieber zeigen

    def _run(self, debug):
        passed = []
        writer = logsetup.glib_writer(debug, lambda lvl, fields, color: f"Gdk-{fields}\n",
                                      lambda lvl, fields: passed.append(fields) or "ok", "handled",
                                      logging.getLogger("glib.test"))
        with self.assertLogs("glib.test", logging.DEBUG) as cm:
            logging.getLogger("glib.test").debug("Anfang")
            for flags, text in ((INFO, "INFO: Vulkan: Loader Message"), (DEBUG, "DEBUG: x"),
                                (WARNING, "WARNING: Theme parsing error"), (CRITICAL, "CRITICAL: assertion failed")):
                self.assertEqual(writer(flags, text, 1, None), "ok")
        self.assertEqual(len(passed), 4)                          # GLibs Ausgabe bekommt alles
        return [(r.levelname, r.getMessage()) for r in cm.records[1:]]

    def test_default_keeps_warnings_only(self):
        self.assertEqual(self._run(False), [("WARNING", "Gdk-WARNING: Theme parsing error"),
                                            ("ERROR", "Gdk-CRITICAL: assertion failed")])

    def test_debug_mode_bridges_everything(self):
        got = self._run(True)
        self.assertEqual([m for _l, m in got], ["Gdk-INFO: Vulkan: Loader Message", "Gdk-DEBUG: x",
                                                "Gdk-WARNING: Theme parsing error", "Gdk-CRITICAL: assertion failed"])
        self.assertEqual(got[0][0], "INFO")

    def test_errors_in_formatting_or_passthrough_are_swallowed(self):
        def boom(*_a):
            raise RuntimeError("kaputt")
        writer = logsetup.glib_writer(False, boom, boom, "handled", logging.getLogger("glib.test"))
        self.assertEqual(writer(WARNING, "x", 1, None), "handled")

    def test_debug_mode_from_environment(self):
        with mock.patch.dict(os.environ, {"GALAXY_LINUX_LOG": "debug"}, clear=False):
            self.assertTrue(logsetup.debug_mode())
        with mock.patch.dict(os.environ, {"GALAXY_LINUX_LOG": "", "GALAXY_LINUX_DEBUG": ""}, clear=False):
            self.assertFalse(logsetup.debug_mode())
        with mock.patch.dict(os.environ, {"GALAXY_LINUX_LOG": "", "GALAXY_LINUX_DEBUG": "1"}, clear=False):
            self.assertTrue(logsetup.debug_mode())


if __name__ == "__main__":
    unittest.main()
