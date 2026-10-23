"""Fußzeile der Seitenleiste: „N Update(s) verfügbar“ ist ein Knopf – ein Klick führt zu den Erhältlichen Updates."""
from __future__ import annotations

import unittest
from unittest import mock


class FooterTest(unittest.TestCase):
    def test_text(self):
        from galaxy_linux.ui import footer
        self.assertEqual(footer.updates_text(0), "")
        self.assertEqual(footer.updates_text(3), footer.tr('{0} Update(s) verfügbar').format(3))

    def test_one_click_opens_available_updates(self):
        from galaxy_linux.ui import footer
        calls = []
        win = mock.Mock()
        win.navigate.side_effect = lambda view: calls.append(("navigate", view))
        win.downloads.reveal_updates.side_effect = lambda: calls.append(("reveal", "updates"))
        footer.open_available_updates(win)
        self.assertEqual(calls, [("navigate", "downloads"), ("reveal", "updates")])


if __name__ == "__main__":
    unittest.main()
