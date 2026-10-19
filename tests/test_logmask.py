"""Zugangsdaten dürfen nie im Protokoll landen."""
from __future__ import annotations

import logging
import unittest

from galaxy_linux import logsetup


class MaskTest(unittest.TestCase):
    def test_mask_url(self):
        self.assertEqual(logsetup.mask_url("https://auth.gog.com/token?client_id=1&client_secret=abc&refresh_token=xyz"),
                         "https://auth.gog.com/token?<maskiert>")
        m = logsetup.mask_url("https://embed.gog.com/on_login_success?origin=client&code=SECRETCODE")
        self.assertNotIn("SECRETCODE", m)
        self.assertIn("origin=client", m)
        m = logsetup.mask_url("https://gog-cdn.example/store/ab/cd/abcd?token=SIGNED&expires=1")
        self.assertNotIn("SIGNED", m)

    def test_mask_argv(self):
        argv = ["galaxy-linux", "login", "--code", "https://embed.gog.com/on_login_success?origin=client&code=ABC123"]
        self.assertEqual(logsetup.mask_argv(argv), ["galaxy-linux", "login", "--code", "***"])
        self.assertEqual(logsetup.mask_argv(["x", "--code=ABC"]), ["x", "--code=***"])
        m = logsetup.mask_argv(["x", "https://embed.gog.com/on_login_success?code=ABC"])
        self.assertNotIn("ABC", m[1])

    def test_filter_masks_messages_and_tracebacks(self):
        f = logsetup._MaskFilter()
        rec = logging.LogRecord("t", logging.INFO, "f", 1, "Header Authorization: Bearer eyJabc.def-ghi und refresh_token=RT123 code=C0DE", (), None)
        f.filter(rec)
        msg = rec.getMessage()
        for secret in ("eyJabc", "RT123", "C0DE"):
            self.assertNotIn(secret, msg)
        rec = logging.LogRecord("t", logging.ERROR, "f", 1, "Fehler", (), None)
        rec.exc_text = 'AuthError: {"access_token": "TOKENVALUE", "client_secret": "9d85c43b1482497dbbce61f6e4aa173a4f5cafb5a3ae5db5c86a7c8e4ffc9f3b"}'
        f.filter(rec)
        self.assertNotIn("TOKENVALUE", rec.exc_text)
        self.assertNotIn("9d85c43b", rec.exc_text)

    def test_mask_text_keeps_normal_lines(self):
        line = "GET https://api.gog.com/products/1207658924?expand=downloads -> 200 (1234 B, 80 ms)"
        self.assertEqual(logsetup.mask_text(line), line)


if __name__ == "__main__":
    unittest.main()
