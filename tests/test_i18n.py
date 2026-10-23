import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux import i18n

PLACEHOLDER = re.compile(r"\{[^{}]*\}")


class PoTest(unittest.TestCase):
    def test_parse_po_continuation_and_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            po = Path(d) / "xx.po"
            po.write_text('msgid ""\nmsgstr ""\n"Language: xx\\n"\n\n'
                          '#: a.py:1\nmsgid "Zeile „eins“\\n"\n"zwei \\"x\\""\nmsgstr "line one\\n"\n"two \\"x\\""\n\n'
                          'msgid "offen"\nmsgstr ""\n', encoding="utf-8")
            cat = i18n.parse_po(po)
        self.assertEqual(cat, {'Zeile „eins“\nzwei "x"': 'line one\ntwo "x"'})

    def test_context(self):
        msgs = {"Kopieren": ["a.py:1"], "Zwischenablage\x04Kopieren": ["b.py:2"]}
        with tempfile.TemporaryDirectory() as d:
            po = Path(d) / "en.po"
            i18n.write_po(po, msgs, {"Kopieren": "Duplicate", "Zwischenablage\x04Kopieren": "Copy"}, "en")
            self.assertIn('msgctxt "Zwischenablage"', po.read_text())
            cat = i18n.parse_po(po)
        self.assertEqual(cat, {"Kopieren": "Duplicate", "Zwischenablage\x04Kopieren": "Copy"})
        try:
            i18n.setup("de")
            self.assertEqual(i18n.tr("Zwischenablage\x04Kopieren"), "Kopieren")
            i18n.setup("en")
            self.assertEqual(i18n.tr("Zwischenablage\x04Kopieren"), "Copy")
            self.assertEqual(i18n.tr("Kopieren"), "Duplicate")
        finally:
            i18n.setup("de")

    def test_write_and_read_roundtrip(self):
        msgs = {'A "b"\\c\n\td': ["x.py:1"], "neu": ["y.py:2"]}
        with tempfile.TemporaryDirectory() as d:
            po = Path(d) / "en.po"
            i18n.write_po(po, msgs, {'A "b"\\c\n\td': 'E "f"\\g\n\th'}, "en")
            self.assertEqual(i18n.parse_po(po), {'A "b"\\c\n\td': 'E "f"\\g\n\th'})


class EnglishCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = i18n.collect_messages()
        cls.catalog = i18n.parse_po(i18n.LOCALE_DIR / "en.po")

    def test_all_texts_translated(self):
        missing = [m for m in self.messages if m not in self.catalog]
        self.assertEqual(missing, [], f"{len(missing)} Texte ohne englische Übersetzung – scripts/i18n-update.py")

    def test_placeholders_match(self):
        for msgid, msgstr in self.catalog.items():
            self.assertEqual(sorted(PLACEHOLDER.findall(msgid)), sorted(PLACEHOLDER.findall(msgstr)), msgid)

    def test_whitespace_kept(self):
        for msgid, msgstr in self.catalog.items():
            lead = len(msgid) - len(msgid.lstrip(" \r\n"))
            self.assertEqual(msgid[:lead], msgstr[:lead], msgid)
            self.assertEqual(msgid.endswith("\n"), msgstr.endswith("\n"), msgid)

    def test_no_german_left(self):
        umlaut = [s for k, s in self.catalog.items() if s != k and re.search(r"[äöüÄÖÜß„]", s)]
        self.assertEqual(umlaut, [])


class SetupTest(unittest.TestCase):
    def tearDown(self):
        i18n.setup("de")

    def test_english_and_german(self):
        self.assertEqual(i18n.setup("en"), "en")
        self.assertEqual(i18n.tr("Einstellungen"), "Settings")
        self.assertEqual(i18n.tr("gibt es nicht"), "gibt es nicht")
        i18n.setup("de")
        self.assertEqual(i18n.tr("Einstellungen"), "Einstellungen")

    def test_detect_order(self):
        with mock.patch.object(i18n, "_settings_language", return_value="auto"):
            with mock.patch.dict(os.environ, {"GALAXY_LINUX_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANGUAGE": "", "LANG": "de_DE.UTF-8"}):
                self.assertEqual(i18n.detect(), "de")
            with mock.patch.dict(os.environ, {"GALAXY_LINUX_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANGUAGE": "", "LANG": "fr_FR.UTF-8"}):
                self.assertEqual(i18n.detect(), "en")
            with mock.patch.dict(os.environ, {"GALAXY_LINUX_LANG": "en", "LANG": "de_DE.UTF-8"}):
                self.assertEqual(i18n.detect(), "en")
        with mock.patch.object(i18n, "_settings_language", return_value="de"):
            with mock.patch.dict(os.environ, {"GALAXY_LINUX_LANG": "", "LC_ALL": "", "LANG": "en_US.UTF-8"}):
                self.assertEqual(i18n.detect(), "de")


if __name__ == "__main__":
    unittest.main()
