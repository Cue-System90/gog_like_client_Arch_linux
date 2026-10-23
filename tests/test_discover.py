"""Entdecken: Katalog lesen, Preise und Rabatte je Währung, Region aus userData.json, Regal-Abfragen, DLC-Regal nur
mit wirkendem Filter, Wunschlisten-Regal, kurzer Zwischenspeicher und Offline-Verhalten – alles ohne Netz."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from galaxy_linux.api import discover as dc
from galaxy_linux.api import wishlist as wl
from galaxy_linux.api.client import ApiError, NotLoggedIn
from galaxy_linux.api.prices import Price, catalog_price, format_amount, format_discount, parse_cents, parse_money

NBSP = " "


def cat_product(pid, title, final="17.99", base="29.99", currency="EUR", **kw):
    price = None if final is None else {
        "final": f"{final} €", "base": f"{base} €", "discount": "-40%",
        "finalMoney": {"amount": final, "currency": currency, "discount": "12.00"},
        "baseMoney": {"amount": base, "currency": currency}}
    d = {"id": str(pid), "title": title, "slug": title.lower().replace(" ", "_"), "productType": "game",
         "coverVertical": f"https://img/{pid}_v.jpg", "coverHorizontal": f"https://img/{pid}_h.jpg",
         "storeLink": f"https://www.gog.com/de/game/{pid}", "productState": "default", "price": price}
    d.update(kw)
    return d


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeApi:
    """Antworten je (Pfadteil) – ``routes`` bildet einen Teil der URL auf eine Funktion (params, auth) → Resp ab."""

    def __init__(self, logged_in=False, user=None, routes=None):
        self.logged_in = logged_in
        self.user = user
        self.user_id = "42" if logged_in else ""
        self.routes = routes or {}
        self.calls = []

    def get(self, url, params=None, authenticated=True, **_kw):
        self.calls.append((url, dict(params or {}), authenticated))
        for part, fn in self.routes.items():
            if part in url:
                res = fn(dict(params or {}), authenticated)
                if isinstance(res, Exception):
                    raise res
                return res
        return Resp({}, 404)

    def get_json(self, url, **kw):
        r = self.get(url, **kw)
        if r.status_code == 401:
            raise NotLoggedIn()
        if not r.ok:
            raise ApiError(f"HTTP {r.status_code}")
        return r.json()


class PriceTest(unittest.TestCase):
    def test_parse_money_and_cents(self):
        self.assertEqual(parse_money("17.99"), 1799)
        self.assertEqual(parse_money("17,99"), 1799)
        self.assertEqual(parse_money(0), 0)
        self.assertEqual(parse_money("0.10"), 10)
        self.assertIsNone(parse_money("kaputt"))
        self.assertIsNone(parse_money(None))
        self.assertIsNone(parse_money(True))
        self.assertEqual(parse_cents("1999 EUR"), (1999, "EUR"))
        self.assertEqual(parse_cents("675 usd"), (675, "USD"))
        self.assertIsNone(parse_cents("abc EUR"))
        self.assertIsNone(parse_cents(None))

    def test_discount(self):
        self.assertEqual(Price(1799, 2999, "EUR").discount, 40)
        self.assertEqual(Price(299, 1999, "EUR").discount, 85)
        self.assertEqual(Price(1999, 1999, "EUR").discount, 0)
        self.assertEqual(Price(0, 0, "EUR").discount, 0)
        self.assertTrue(Price(0, 999, "EUR").free)
        self.assertEqual(Price(0, 999, "EUR").discount, 100)
        self.assertEqual(format_discount(40), "-40%")
        self.assertEqual(format_discount(0), "")

    def test_format_per_currency_and_language(self):
        self.assertEqual(format_amount(1799, "EUR", "de"), f"17,99{NBSP}€")
        self.assertEqual(format_amount(1799, "EUR", "en"), "€17.99")
        self.assertEqual(format_amount(1999, "USD", "en"), "$19.99")
        self.assertEqual(format_amount(1999, "USD", "de"), f"19,99{NBSP}$")
        self.assertEqual(format_amount(129999, "PLN", "de"), f"1.299,99{NBSP}zł")
        self.assertEqual(format_amount(129999, "PLN", "en"), f"zł{NBSP}1,299.99")
        self.assertEqual(format_amount(1200, "CHF", "en"), f"CHF{NBSP}12.00")
        self.assertEqual(format_amount(999, "GBP", "en"), "£9.99")
        self.assertEqual(format_amount(1999, "AUD", "en"), "A$19.99")
        self.assertEqual(format_amount(150000, "JPY", "en"), "¥1,500")
        self.assertEqual(format_amount(1999, "XYZ", "en"), f"XYZ{NBSP}19.99")
        self.assertEqual(format_amount(1999, "", "en"), "19.99")

    def test_catalog_price(self):
        p = catalog_price(cat_product(1, "A")["price"])
        self.assertEqual(p, Price(1799, 2999, "EUR"))
        self.assertIsNone(catalog_price(None))                    # „Demnächst“
        self.assertIsNone(catalog_price({"final": "1 €"}))         # ohne Beträge
        # Grundpreis kleiner als Endpreis (Datenfehler) → kein negativer Rabatt
        self.assertEqual(catalog_price({"finalMoney": {"amount": "5.00", "currency": "usd"},
                                        "baseMoney": {"amount": "4.00"}}), Price(500, 500, "USD"))


class CatalogTest(unittest.TestCase):
    def test_parse_catalog(self):
        data = {"productCount": 3, "products": [cat_product(1, "Alpha"), cat_product(1, "Alpha doppelt"),
                                                cat_product(2, "Beta", final=None, productState="coming-soon"),
                                                {"id": 3}, "kaputt"]}
        items = dc.parse_catalog(data)
        self.assertEqual([p.id for p in items], ["1", "2"])
        a, b = items
        self.assertEqual(a.cover, "https://img/1_v.jpg")
        self.assertEqual(a.price.discount, 40)
        self.assertEqual(a.store_path, "https://www.gog.com/de/game/1")
        self.assertIsNone(b.price)
        self.assertEqual(b.state, "coming-soon")
        self.assertEqual(dc.parse_catalog(None), [])
        self.assertEqual(dc.product_count(data), 3)
        self.assertEqual(dc.product_count({"productCount": "x"}), 0)
        self.assertEqual(dc.Product("9", "X", slug="x").store_path, "/game/x")

    def test_region(self):
        r = dc.region_from_userdata({"country": "de", "selectedCurrency": {"code": "eur", "symbol": "€"}}, "de")
        self.assertEqual((r.country, r.currency, r.locale, r.lang), ("DE", "EUR", "de-DE", "de"))
        r = dc.region_from_userdata({"country": "US", "selectedCurrency": "USD"}, "en")
        self.assertEqual((r.country, r.currency, r.locale), ("US", "USD", "en-US"))
        self.assertEqual(dc.region_from_userdata(None, "en"), dc.Region("", "", "en"))

    def test_params(self):
        region = dc.Region("DE", "EUR", "de")
        p = dc.catalog_params(dc.shelf("for_you"), region, logged_in=False)
        self.assertEqual(p["order"], "desc:trending")
        self.assertEqual(p["countryCode"], "DE")
        self.assertEqual(p["currencyCode"], "EUR")
        self.assertEqual(p["locale"], "de-DE")
        self.assertNotIn("hideOwned", p)
        self.assertEqual(dc.catalog_params(dc.shelf("for_you"), region, logged_in=True)["hideOwned"], "true")
        self.assertEqual(dc.catalog_params(dc.shelf("deals"), region, False)["discounted"], "eq:true")
        self.assertEqual(dc.catalog_params(dc.shelf("new"), region, False)["releaseStatuses"], "in:new-arrival")
        self.assertEqual(dc.catalog_params(dc.shelf("classics"), region, False)["tags"], "in:good-old-game")
        self.assertNotIn("onlyDlcForOwned", dc.catalog_params(dc.shelf("dlc"), region, False))
        self.assertEqual(dc.catalog_params(dc.shelf("dlc"), region, True)["onlyDlcForOwned"], "true")
        self.assertNotIn("countryCode", dc.catalog_params(dc.shelf("for_you"), dc.Region(), False))
        self.assertEqual([s.key for s in dc.SHELVES], ["for_you", "new", "deals", "wishlist", "dlc", "classics"])

    def test_dlc_filter_applied(self):
        self.assertTrue(dc.dlc_filter_applied(12, 4800))
        self.assertTrue(dc.dlc_filter_applied(0, 4800))
        self.assertFalse(dc.dlc_filter_applied(4800, 4800))       # Filter ignoriert
        self.assertFalse(dc.dlc_filter_applied(5, 0))

    def test_dedupe_shelves(self):
        def items(*ids):
            return [dc.Product(str(i), f"T{i}") for i in ids]
        raw = {"for_you": items(1, 2, 3), "deals": items(2, 4, 1, 5, 6), "wishlist": items(1, 4),
               "classics": items(3, 4, 7)}
        order = [s.key for s in dc.SHELVES]
        out = dc.dedupe_shelves(raw, order, show=2)
        self.assertEqual({k: [p.id for p in v] for k, v in out.items()},
                         {"for_you": ["1", "2"], "deals": ["4", "5"], "wishlist": ["1", "4"], "classics": ["3", "7"]})
        # „Spiele für dich“ noch nicht geladen: die anderen Regale zeigen vorerst alles
        out = dc.dedupe_shelves({"deals": items(2, 4)}, order, show=5)
        self.assertEqual([p.id for p in out["deals"]], ["2", "4"])
        self.assertNotIn("for_you", out)

    def test_sort_wishlist(self):
        items = [dc.Product("1", "Zulu", price=Price(1000, 1000, "EUR")),
                 dc.Product("2", "Alpha", price=None, state="coming-soon"),
                 dc.Product("3", "Mike", price=Price(500, 1000, "EUR")),
                 dc.Product("4", "Bravo", price=Price(250, 1000, "EUR"))]
        self.assertEqual([p.id for p in dc.sort_wishlist(items)], ["4", "3", "1", "2"])


class ServiceTest(unittest.TestCase):
    USER = {"country": "AT", "selectedCurrency": {"code": "EUR"}}

    def catalog_route(self, counts=None):
        counts = counts or {}

        def fn(params, auth):
            if params.get("productType") == "in:dlc":
                n = counts.get(("dlc", "onlyDlcForOwned" in params), 0)
                return Resp({"productCount": n, "products": [cat_product(900, "DLC")] if n else []})
            return Resp({"productCount": 1, "products": [cat_product(params.get("order", "x")[-3:], "Spiel")]})
        return fn

    def test_anonymous_catalog_shelf_and_cache(self):
        now = [1000.0]
        api = FakeApi(routes={"userData.json": lambda p, a: Resp({"country": "DE", "selectedCurrency": {"code": "EUR"}}),
                              "catalog.gog.com": self.catalog_route()})
        svc = dc.DiscoverService(api, clock=lambda: now[0])
        items = svc.shelf("for_you", "de")
        self.assertEqual(len(items), 1)
        cat = [c for c in api.calls if "catalog" in c[0]]
        self.assertEqual(cat[0][1]["countryCode"], "DE")
        self.assertFalse(cat[0][2])                       # ohne Anmeldung kein Bearer
        svc.shelf("for_you", "de")
        self.assertEqual(len([c for c in api.calls if "catalog" in c[0]]), 1)     # Zwischenspeicher
        now[0] += dc.CACHE_TTL + 1
        svc.shelf("for_you", "de")
        self.assertEqual(len([c for c in api.calls if "catalog" in c[0]]), 2)
        self.assertEqual(len([c for c in api.calls if "userData" in c[0]]), 1)     # Region länger gültig
        self.assertEqual(svc.shelf("wishlist", "de"), [])                          # nur angemeldet
        self.assertEqual(svc.shelf("dlc", "de"), [])

    def test_offline_keeps_last_state_or_raises(self):
        now = [0.0]
        state = {"fail": False}

        def cat(params, auth):
            return ApiError("offline") if state["fail"] else Resp({"products": [cat_product(1, "A")]})
        api = FakeApi(routes={"userData.json": lambda p, a: ApiError("offline"), "catalog.gog.com": cat})
        svc = dc.DiscoverService(api, clock=lambda: now[0])
        self.assertEqual(len(svc.shelf("deals", "en")), 1)
        state["fail"] = True
        now[0] += dc.CACHE_TTL + 5
        self.assertEqual(len(svc.shelf("deals", "en")), 1)                        # alter Stand
        with self.assertRaises(ApiError):
            svc.shelf("new", "en")                                                # nie geladen

    def test_logged_in_hides_owned_and_falls_back(self):
        def cat(params, auth):
            if auth:
                return Resp({}, 500)
            return Resp({"products": [cat_product(5, "Gast")]})
        api = FakeApi(logged_in=True, user=self.USER, routes={"catalog.gog.com": cat})
        svc = dc.DiscoverService(api)
        items = svc.shelf("for_you", "en")
        self.assertEqual([p.title for p in items], ["Gast"])
        calls = [c for c in api.calls if "catalog" in c[0]]
        self.assertTrue(calls[0][2] and calls[0][1]["hideOwned"] == "true")
        self.assertEqual(calls[0][1]["countryCode"], "AT")                        # Region aus den Nutzerdaten
        self.assertFalse(calls[1][2])
        self.assertNotIn("hideOwned", calls[1][1])
        self.assertFalse(any("userData" in c[0] for c in api.calls))

    def test_dlc_shelf_only_when_filter_works(self):
        api = FakeApi(logged_in=True, user=self.USER,
                      routes={"catalog.gog.com": self.catalog_route({("dlc", True): 7, ("dlc", False): 5000})})
        self.assertEqual([p.title for p in dc.DiscoverService(api).shelf("dlc", "de")], ["DLC"])
        api = FakeApi(logged_in=True, user=self.USER,
                      routes={"catalog.gog.com": self.catalog_route({("dlc", True): 5000, ("dlc", False): 5000})})
        with self.assertLogs("galaxy_linux.api.discover", "INFO"):
            self.assertEqual(dc.DiscoverService(api).shelf("dlc", "de"), [])

    def test_wishlist_shelf(self):
        prices = {"_embedded": {"items": [
            {"_embedded": {"product": {"id": 11}, "prices": [{"currency": {"code": "EUR"}, "basePrice": "1999 EUR",
                                                               "finalPrice": "999 EUR"}]}},
            {"_embedded": {"product": {"id": 12}, "prices": [{"currency": {"code": "EUR"}, "basePrice": "500 EUR",
                                                               "finalPrice": "500 EUR"}]}}]}}
        products = [{"id": 11, "title": "Elf", "slug": "elf", "links": {"product_card": "https://www.gog.com/game/elf"},
                     "images": {"logo2x": "//img/elf.jpg"}},
                    {"id": 12, "title": "Zwölf", "slug": "zwoelf", "links": {}, "images": {}},
                    {"id": 13, "title": "Dreizehn", "slug": "dreizehn", "links": {}, "images": {}}]
        api = FakeApi(logged_in=True, user=self.USER, routes={
            "wishlist.json": lambda p, a: Resp({"wishlist": {"13": True, "11": True, "12": True}}),
            "products/prices": lambda p, a: Resp(prices),
            "/products": lambda p, a: Resp(products)})
        wl._products.clear()
        items = dc.DiscoverService(api).shelf("wishlist", "de")
        self.assertEqual([p.id for p in items], ["11", "12", "13"])
        self.assertEqual(items[0].price.discount, 50)
        self.assertEqual(items[0].image, "https://img/elf.jpg")
        self.assertEqual(items[0].store_path, "https://www.gog.com/game/elf")
        self.assertIsNone(items[2].price)
        self.assertEqual(items[2].state, "coming-soon")
        self.assertEqual(items[1].store_path, "/game/zwoelf")

    def test_wishlist_shelf_logged_out_session(self):
        api = FakeApi(logged_in=True, user=self.USER, routes={"wishlist.json": lambda p, a: Resp({}, 401)})
        self.assertEqual(dc.DiscoverService(api).shelf("wishlist", "de"), [])


if __name__ == "__main__":
    unittest.main()
