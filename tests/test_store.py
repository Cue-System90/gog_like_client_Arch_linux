"""Eingebetteter Store: Adressen, Kauferkennung, Link-Einordnung, Wunschliste – ohne WebKit, ohne Netz."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux import store_links as sl
from galaxy_linux.api import storefront
from galaxy_linux.api.client import ApiError
from galaxy_linux.ui import web


class LocaleUrlTest(unittest.TestCase):
    def test_lang_and_locale(self):
        self.assertEqual(sl.store_lang("de"), "de")
        self.assertEqual(sl.store_lang("de_AT.UTF-8"), "de")
        self.assertEqual(sl.store_lang("en"), "en")
        self.assertEqual(sl.store_lang("fr"), "en")
        self.assertEqual(sl.store_lang(""), "en")
        self.assertEqual(sl.store_locale("de"), "de-DE")
        self.assertEqual(sl.store_locale("en"), "en-US")

    def test_sections_get_language_prefix(self):
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["home"], "de"), "https://www.gog.com/de/")
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["games"], "en"), "https://www.gog.com/en/games")
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["deals"], "de"),
                         "https://www.gog.com/de/games?order=desc:discount&discounted=true&hideOwned=true")
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["wishlist"], "de"), "https://www.gog.com/de/account/wishlist")
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["cart"], "en"), "https://www.gog.com/en/checkout")
        self.assertEqual(sl.store_url(sl.STORE_SECTIONS["orders"], "en"), "https://www.gog.com/en/account/settings/orders")

    def test_full_urls_are_relocalised(self):
        self.assertEqual(sl.store_url("https://www.gog.com/en/game/the_witcher", "de"),
                         "https://www.gog.com/de/game/the_witcher")
        self.assertEqual(sl.store_url("https://embed.gog.com/game/x?a=1", "en"), "https://www.gog.com/en/game/x?a=1")
        self.assertEqual(sl.store_url("https://www.gog.com/de", "en"), "https://www.gog.com/en/")
        self.assertEqual(sl.store_url("https://example.com/x", "de"), "https://example.com/x")

    def test_section_for_path(self):
        self.assertEqual(sl.section_for_path("https://www.gog.com/de/"), "home")
        self.assertEqual(sl.section_for_path("https://www.gog.com/en/games?page=2"), "games")
        self.assertEqual(sl.section_for_path("https://www.gog.com/de/games?order=desc:discount&discounted=true"), "deals")
        self.assertEqual(sl.section_for_path("/account/wishlist"), "wishlist")
        self.assertEqual(sl.section_for_path("https://www.gog.com/de/checkout/abc/complete"), "cart")
        self.assertEqual(sl.section_for_path("/en/account/settings/orders"), "orders")
        self.assertIsNone(sl.section_for_path("https://www.gog.com/de/game/the_witcher"))
        self.assertIsNone(sl.section_for_path("https://example.com/games"))

    def test_rememberable_and_login(self):
        self.assertTrue(sl.rememberable_url("https://www.gog.com/de/game/x"))
        self.assertFalse(sl.rememberable_url("https://www.gog.com/de/checkout/abc"))
        self.assertFalse(sl.rememberable_url("https://www.gog.com/redeem?code=ABCD"))
        self.assertFalse(sl.rememberable_url("https://example.com/"))
        self.assertEqual(sl.store_login_url("https://www.gog.com/de/games", "de"), "https://www.gog.com/de/games##openlogin")
        self.assertEqual(sl.store_login_url("https://www.gog.com/de/checkout/x", "de"), "https://www.gog.com/de/##openlogin")


class PurchaseMatcherTest(unittest.TestCase):
    def test_complete_pages(self):
        self.assertEqual(sl.checkout_order_id("/de/checkout/abc123/complete"), "abc123")
        self.assertEqual(sl.checkout_order_id("/checkout/x/complete?x=1"), "x")
        self.assertEqual(sl.checkout_order_id("https://www.gog.com/en/checkout/Ab-9_z/complete"), "Ab-9_z")
        self.assertEqual(sl.checkout_order_id("https://www.gog.com/checkout/abc/complete/#top"), "abc")
        self.assertEqual(sl.checkout_order_id("https://embed.gog.com/pt-br/checkout/q1/complete"), "q1")

    def test_negative_cases(self):
        for url in ("", "/checkout", "/de/checkout/abc", "/de/checkout/abc/payment", "/checkout/abc/complete/extra",
                    "/de/checkout/manual/complete", "/account/checkout/abc/complete", "/de/checkout//complete",
                    "https://example.com/checkout/abc/complete", "https://www.gog.com.evil.org/checkout/a/complete",
                    "javascript:/checkout/a/complete", "/games?next=/checkout/a/complete"):
            self.assertIsNone(sl.checkout_order_id(url), url)


class LinkClassificationTest(unittest.TestCase):
    def test_new_windows(self):
        # Link mit target=_blank: gog.com bleibt im Store, fremde Seiten in den System-Browser
        self.assertEqual(sl.classify_new_window("https://www.gog.com/de/game/x", True), "store")
        self.assertEqual(sl.classify_new_window("https://www.youtube.com/watch?v=1", True), "browser")
        self.assertEqual(sl.classify_new_window("https://support.gog.com/hc", True), "browser")
        self.assertEqual(sl.classify_new_window("mailto:a@example.com", True), "browser")
        self.assertEqual(sl.classify_new_window("mailto:a@example.com", False), "ignore")     # window.open ohne Klick
        self.assertEqual(sl.classify_new_window("galaxy-linux://launch/1207658924", True), "ignore")
        self.assertEqual(sl.classify_new_window("steam://run/1", True), "ignore")
        # window.open aus Skripten (Zahlungsanbieter) → eigenes Fenster in derselben Sitzung
        self.assertEqual(sl.classify_new_window("https://www.paypal.com/checkoutnow?token=x", False), "popup")
        self.assertEqual(sl.classify_new_window("", False), "popup")
        self.assertEqual(sl.classify_new_window("about:blank", True), "popup")
        self.assertEqual(sl.classify_new_window("javascript:void(0)", True), "ignore")
        self.assertEqual(sl.classify_new_window("file:///etc/passwd", False), "ignore")

    def test_navigation(self):
        self.assertEqual(sl.classify_navigation("https://www.gog.com/de/"), "allow")
        self.assertEqual(sl.classify_navigation("https://checkoutshopper-live.adyen.com/x"), "allow")
        self.assertEqual(sl.classify_navigation("about:blank"), "allow")
        self.assertEqual(sl.classify_navigation("goggalaxy://openGameView/1207658924"), "galaxy")
        self.assertEqual(sl.classify_navigation("mailto:a@example.com"), "browser")
        # Seiten im Store dürfen keine lokalen Programme auslösen (galaxy-linux://launch startet Spiele)
        self.assertEqual(sl.classify_navigation("steam://run/1"), "ignore")
        self.assertEqual(sl.classify_navigation("galaxy-linux://launch/1207658924"), "ignore")
        self.assertEqual(sl.classify_navigation("GALAXY-LINUX://launch/ext-x"), "ignore")
        self.assertEqual(sl.classify_navigation("file:///etc/passwd"), "ignore")

    def test_galaxy_links_and_downloads(self):
        self.assertEqual(sl.galaxy_link_product("goggalaxy://openGameView/1207658924"), "1207658924")
        self.assertEqual(sl.galaxy_link_product("goggalaxy://openGameView/gog_1207658924"), "1207658924")
        self.assertIsNone(sl.galaxy_link_product("goggalaxy://openGameView/abc"))
        self.assertIsNone(sl.galaxy_link_product("https://www.gog.com/1"))
        self.assertTrue(sl.response_is_download(False))
        self.assertTrue(sl.response_is_download(True, 'attachment; filename="manual.pdf"'))
        self.assertFalse(sl.response_is_download(True, "inline"))
        self.assertFalse(sl.response_is_download(True))

    def test_page_messages(self):
        self.assertEqual(sl.parse_page_message('{"type": "nav", "url": "https://www.gog.com/de/"}'),
                         {"type": "nav", "url": "https://www.gog.com/de/"})
        self.assertEqual(sl.parse_page_message('{"type": "user", "loggedIn": false}'), {"type": "user", "loggedIn": False})
        self.assertIsNone(sl.parse_page_message('{"type": "user", "loggedIn": "no"}'))
        self.assertIsNone(sl.parse_page_message("kein json"))
        self.assertIsNone(sl.parse_page_message('["nav"]'))

    def test_download_names(self):
        self.assertEqual(sl.safe_download_name("../../etc/setup.exe"), "setup.exe")
        self.assertEqual(sl.safe_download_name("..\\x\\.hidden"), "hidden")
        self.assertEqual(sl.safe_download_name(""), "download")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sl.unique_download_path(Path(d), "setup.exe"), Path(d) / "setup.exe")
            (Path(d) / "setup.exe").touch()
            (Path(d) / "setup (1).exe").touch()
            self.assertEqual(sl.unique_download_path(Path(d), "setup.exe"), Path(d) / "setup (2).exe")


class WishlistTest(unittest.TestCase):
    def test_parse_count(self):
        self.assertEqual(sl.parse_wishlist_count({"wishlist": {"1": True, "2": True, "3": False}, "checksum": "x"}), 2)
        self.assertEqual(sl.parse_wishlist_count({"wishlist": []}), 0)          # PHP: leere Liste statt {}
        self.assertEqual(sl.parse_wishlist_count({"wishlist": {"abc": True}}), 0)
        self.assertEqual(sl.parse_wishlist_count(None), 0)
        self.assertEqual(sl.parse_wishlist_count("x"), 0)

    def test_count_via_bearer_api(self):
        api = mock.Mock()
        api.get_json.return_value = {"wishlist": {"10": True, "20": True}}
        self.assertEqual(storefront.wishlist_count(api), 2)
        self.assertEqual(api.get_json.call_args.args[0], "https://embed.gog.com/user/wishlist.json")
        api.get_json.side_effect = ApiError("HTTP 403")
        with self.assertRaises(ApiError):
            storefront.wishlist_count(api)

    def test_product_store_url(self):
        resp = mock.Mock(ok=True, status_code=200)
        resp.json.return_value = {"_links": {"store": {"href": "https://www.gog.com/de/game/the_witcher"}}}
        api = mock.Mock()
        api.get.return_value = resp
        with mock.patch.dict(storefront._links, clear=True):
            self.assertEqual(storefront.product_store_url(api, "1207658924", "de-DE"), "https://www.gog.com/de/game/the_witcher")
            self.assertEqual(storefront.product_store_url(api, "1207658924", "de-DE"), "https://www.gog.com/de/game/the_witcher")
            self.assertEqual(api.get.call_count, 1)                      # zwischengespeichert
            self.assertEqual(api.get.call_args.kwargs["params"], {"locale": "de-DE"})
            self.assertFalse(api.get.call_args.kwargs["authenticated"])
            api.get.return_value = mock.Mock(ok=False, status_code=404)
            self.assertEqual(storefront.product_store_url(api, "1", "en-US"), "")
            api.get.side_effect = ApiError("offline")
            self.assertEqual(storefront.product_store_url(api, "2", "en-US"), "")

    def test_store_href_validation_and_fallback(self):
        self.assertEqual(sl.store_href_from_v2({"_links": {"store": {"href": "https://evil.example/x"}}}), "")
        self.assertEqual(sl.store_href_from_v2({}), "")
        self.assertEqual(sl.product_fallback("https://www.gog.com/game/a", "/game/b", "c"), "https://www.gog.com/game/a")
        self.assertEqual(sl.product_fallback("", "/game/b", "c"), "/game/b")
        self.assertEqual(sl.product_fallback("", "", "the_witcher"), "/game/the_witcher")
        self.assertEqual(sl.product_fallback(), "/")


class WebWithoutWebKitTest(unittest.TestCase):
    def setUp(self):
        self._saved = (web._webkit, web._checked, web._session)
        web._webkit, web._checked, web._session = None, False, None

    def tearDown(self):
        web._webkit, web._checked, web._session = self._saved

    def test_missing_webkit_falls_back(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("gi.require_version", side_effect=ValueError("Namespace WebKit not available")), \
                mock.patch.object(web.paths, "DATA_DIR", Path(d) / "data"), \
                mock.patch.object(web.paths, "CACHE_DIR", Path(d) / "cache"):
            self.assertIsNone(web.webkit())
            self.assertFalse(web.available())
            self.assertIsNone(web.session())
            self.assertIsNone(web.new_view())
            self.assertFalse(web.clear_data())
            self.assertFalse((Path(d) / "data").exists())          # ohne WebKit nichts anlegen


if __name__ == "__main__":
    unittest.main()
