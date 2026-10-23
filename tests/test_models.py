"""Spiel-Metadaten aus api.gog.com/GamesDB (api/models.py) – ohne Netz."""
import unittest

from galaxy_linux.api.models import GameMeta, LibraryEntry


class DescriptionTest(unittest.TestCase):
    def entry(self, pid: str = "1077527220") -> LibraryEntry:
        return LibraryEntry(id=pid, title="Demo")

    def test_product_feature_placeholder_removed(self):
        product = {"description": {
            "lead": "",
            "full": "Witty storytelling for all ages.\n\n<br>\n<ul><li>product_feature_1077527220</li></ul>",
            "whats_cool_about_it": "product_feature_1077527220"}}
        m = GameMeta.from_sources(self.entry(), None, product)
        self.assertEqual(m.summary, "Witty storytelling for all ages.")
        crlf = {"description": {"full": "<p>as well as your own...\r\n</p>\n<br>\n<ul><li>product_feature_1446213994</li></ul>"}}
        self.assertEqual(GameMeta.from_sources(self.entry("1446213994"), None, crlf).summary, "as well as your own...")

    def test_normal_text_and_gamesdb_summary_kept(self):
        product = {"description": {"full": "<p>Line one</p><ul><li>Feature A</li><li>product_feature list</li></ul>"}}
        m = GameMeta.from_sources(self.entry(), None, product)
        self.assertEqual(m.summary, "Line one\nFeature A\nproduct_feature list")
        gdb = {"summary": {"*": "A much longer summary from GamesDB\nproduct_feature_1"}}
        self.assertEqual(GameMeta.from_sources(self.entry(), gdb, None).summary, "A much longer summary from GamesDB")


if __name__ == "__main__":
    unittest.main()
