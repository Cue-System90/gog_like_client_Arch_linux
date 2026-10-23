"""Wunschliste: IDs, Batch-Preise in Paketen, Titel, Erkennung begonnener/gewachsener Rabatte und neu erhältlicher
Spiele, gespeicherter Stand, Fälligkeit der Prüfung – alles ohne Netz."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from galaxy_linux.api import wishlist as wl
from galaxy_linux.api.client import ApiError
from galaxy_linux.api.prices import Price

from tests.test_discover import FakeApi, Resp


def price_item(pid, final, base, currency="EUR"):
    return {"_embedded": {"product": {"id": int(pid)},
                          "prices": [{"currency": {"code": "USD"}, "basePrice": f"{base + 100} USD",
                                      "finalPrice": f"{final + 100} USD"},
                                     {"currency": {"code": currency}, "basePrice": f"{base} {currency}",
                                      "finalPrice": f"{final} {currency}", "bonusWalletFunds": f"0 {currency}"}]}}


class ParseTest(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(wl.parse_ids({"wishlist": {"1": True, "2": False, "x": True, "3": True}, "checksum": "c"}),
                         ["1", "3"])
        self.assertEqual(wl.parse_ids({"wishlist": ["4", 5]}), ["4", "5"])
        self.assertEqual(wl.parse_ids(None), [])
        self.assertEqual(wl.parse_ids({"wishlist": "kaputt"}), [])

    def test_chunks(self):
        ids = [str(i) for i in range(120)] + ["5"]
        parts = wl.chunks(ids)
        self.assertEqual([len(p) for p in parts], [50, 50, 20])
        self.assertEqual(parts[0][0], "0")

    def test_batch_prices_picks_currency(self):
        data = {"_embedded": {"items": [price_item(1, 999, 1999), price_item(2, 500, 500),
                                        {"_embedded": {"product": {"id": 3}, "prices": []}}, "kaputt"]}}
        out = wl.parse_batch_prices(data, "EUR")
        self.assertEqual(out, {"1": Price(999, 1999, "EUR"), "2": Price(500, 500, "EUR")})
        self.assertEqual(wl.parse_batch_prices(data, "")["1"].currency, "USD")      # ohne Wunsch: erste Währung
        self.assertEqual(wl.parse_batch_prices(data, "GBP")["1"].currency, "USD")   # unbekannt: erste Währung
        self.assertEqual(wl.parse_batch_prices(None), {})

    def test_products(self):
        out = wl.parse_products([{"id": 7, "title": "Sieben", "slug": "sieben",
                                  "links": {"product_card": "https://www.gog.com/game/sieben"},
                                  "images": {"logo": "//img/7.jpg"}}, {"title": "ohne ID"}])
        self.assertEqual(out, {"7": {"title": "Sieben", "slug": "sieben", "link": "https://www.gog.com/game/sieben",
                                     "image": "https://img/7.jpg"}})


class DetectTest(unittest.TestCase):
    def snap(self, **prices):
        return wl.snapshot(prices.keys(), {k: v for k, v in prices.items() if v is not None})

    def test_started_and_grown(self):
        old = self.snap(a=Price(1000, 1000, "EUR"), b=Price(800, 1000, "EUR"), c=Price(500, 1000, "EUR"),
                        d=Price(500, 1000, "EUR"))
        new = self.snap(a=Price(700, 1000, "EUR"),      # begonnen (30 %)
                        b=Price(500, 1000, "EUR"),      # gewachsen 20 → 50 %
                        c=Price(500, 1000, "EUR"),      # unverändert
                        d=Price(750, 1000, "EUR"))      # kleiner geworden
        self.assertEqual(wl.detect_changes(old, new), [("discount", "b"), ("discount", "a")])

    def test_ended_then_restarted(self):
        old = self.snap(a=Price(1000, 1000, "EUR"))
        self.assertEqual(wl.detect_changes(self.snap(a=Price(500, 1000, "EUR")), old), [])          # Rabatt endet
        self.assertEqual(wl.detect_changes(old, self.snap(a=Price(500, 1000, "EUR"))), [("discount", "a")])

    def test_available_and_new_ids(self):
        old = self.snap(a=None, b=Price(1000, 1000, "EUR"))
        new = self.snap(a=Price(2000, 2000, "EUR"), b=Price(1000, 1000, "EUR"), n=Price(100, 1000, "EUR"))
        # „a“ hatte keinen Preis und ist jetzt erhältlich; „n“ ist neu auf der Wunschliste (kein Vergleich möglich)
        self.assertEqual(wl.detect_changes(old, new), [("available", "a")])
        self.assertEqual(wl.detect_changes(None, new), [])
        self.assertEqual(wl.detect_changes(new, self.snap(a=None)), [])     # wieder ohne Preis: nichts


class TextTest(unittest.TestCase):
    def test_notification_text(self):
        from galaxy_linux import i18n
        c = wl.Change("discount", "1", Price(1799, 2999, "EUR"), "Gwent")
        try:
            i18n.setup("de")
            self.assertEqual(wl.notification_text(c, "de"), ("wishlist", "„Gwent“ ist jetzt um 40 % reduziert – 17,99\u00a0€"))
            self.assertEqual(wl.notification_text(wl.Change("available", "2", Price(0, 0, "USD")), "de"),
                             ("available", "Neu erhältlich: „2“ – Kostenlos"))
            i18n.setup("en")
            self.assertEqual(wl.notification_text(c, "en"), ("wishlist", "“Gwent” is now 40% off – €17.99"))
        finally:
            i18n.setup("de")
        self.assertEqual(wl.notification_key(c), "discount:1:40:1799")


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "wishlist_state.json"
        self.ids = {"1": True, "2": True, "3": True}
        self.prices = {"1": (1999, 1999), "2": (1000, 1000)}
        self.fail = False
        wl._products.clear()

        def prices(params, auth):
            if self.fail:
                return Resp({}, 503)
            ids = params["ids"].split(",")
            return Resp({"_embedded": {"items": [price_item(i, *self.prices[i]) for i in ids if i in self.prices]}})
        self.api = FakeApi(logged_in=True, routes={
            "wishlist.json": lambda p, a: Resp({"wishlist": self.ids}),
            "products/prices": prices,
            "/products": lambda p, a: Resp([{"id": int(i), "title": f"Spiel {i}",
                                             "links": {"product_card": f"https://www.gog.com/game/s{i}"}}
                                            for i in p["ids"].split(",")])})

    def tearDown(self):
        self.td.cleanup()

    def test_first_check_records_only(self):
        self.assertTrue(wl.due(self.path, now=1000))
        self.assertEqual(wl.check(self.api, self.path, "DE", "EUR", now=1000), [])
        state = json.loads(self.path.read_text())
        self.assertEqual(state["checked"], 1000)
        self.assertEqual(state["items"]["1"]["final"], 1999)
        self.assertFalse(state["items"]["3"]["available"])
        self.assertFalse(wl.due(self.path, now=1000 + wl.CHECK_INTERVAL - 1))
        self.assertTrue(wl.due(self.path, now=1000 + wl.CHECK_INTERVAL))
        price_calls = [c for c in self.api.calls if "products/prices" in c[0]]
        self.assertEqual(price_calls[0][1]["countryCode"], "DE")
        self.assertEqual(price_calls[0][1]["currency"], "EUR")
        self.assertFalse(price_calls[0][2])

    def test_discount_and_available(self):
        wl.check(self.api, self.path, "DE", "EUR", now=1000)
        self.prices = {"1": (999, 1999), "2": (1000, 1000), "3": (2999, 2999)}
        changes = wl.check(self.api, self.path, "DE", "EUR", locale="de-DE", now=2000)
        self.assertEqual([(c.kind, c.product_id, c.discount) for c in changes], [("discount", "1", 50),
                                                                                 ("available", "3", 0)])
        self.assertEqual(changes[0].title, "Spiel 1")
        self.assertEqual(changes[0].link, "https://www.gog.com/game/s1")
        self.assertEqual(changes[0].price, Price(999, 1999, "EUR"))
        # gleicher Rabatt bei der nächsten Prüfung: keine erneute Meldung
        self.assertEqual(wl.check(self.api, self.path, "DE", "EUR", now=3000), [])

    def test_failure_keeps_state(self):
        wl.check(self.api, self.path, "DE", "EUR", now=1000)
        before = self.path.read_text()
        self.fail = True
        with self.assertRaises(ApiError):
            wl.check(self.api, self.path, "DE", "EUR", now=2000)
        self.assertEqual(self.path.read_text(), before)

    def test_many_ids_in_batches(self):
        self.ids = {str(i): True for i in range(1, 121)}
        self.prices = {str(i): (1000, 1000) for i in range(1, 121)}
        wl.check(self.api, self.path, now=1000)
        self.assertEqual(len([c for c in self.api.calls if "products/prices" in c[0]]), 3)


if __name__ == "__main__":
    unittest.main()
