"""Identität externer Spiele (0.3.1): Kopie oder Variante, offizielle Namen (GOG, Katalog, Versionsinfo der .exe,
Wikidata), Wikipedia-Cover, eigener Titel und eigenes Cover („Bearbeiten …“). Kein Netz: Katalog, Steam und Wikidata
sind nachgebildet."""
from __future__ import annotations

import os
import shutil
import struct
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from galaxy_linux.api import covers, wikidata
from galaxy_linux.content import identity, peinfo
from galaxy_linux.settings import CustomLibrary, InstalledGame


# ----------------------------------------------------------------------------- PE-Fixture
def _vs(key: str, value: bytes = b"", kind: int = 0, children: list[bytes] | None = None, vlen: int | None = None) -> bytes:
    k = (key + "\0").encode("utf-16-le")
    body = k + b"\0" * ((-(6 + len(k))) % 4) + value
    if children:
        body += b"\0" * ((-(6 + len(body))) % 4)
        body += b"".join(c + b"\0" * ((-len(c)) % 4) for c in children)
    wvl = vlen if vlen is not None else (len(value) // 2 if kind == 1 else len(value))
    return struct.pack("<HHH", 6 + len(body), wvl, kind) + body


def version_resource(strings: dict[str, str]) -> bytes:
    rows = [_vs(k, (v + "\0").encode("utf-16-le"), 1) for k, v in strings.items()]
    table = _vs("040904b0", children=rows, kind=1)
    sfi = _vs("StringFileInfo", children=[table], kind=1)
    fixed = struct.pack("<I", 0xFEEF04BD) + b"\0" * 48
    return _vs("VS_VERSION_INFO", fixed, 0, [sfi])


def make_pe(strings: dict[str, str] | None, pe32plus: bool = False, pad: int = 0) -> bytes:
    """Kleinste Windows-.exe mit einer Versionsressource (oder ohne Ressourcen bei ``strings=None``)."""
    vs = version_resource(strings) if strings is not None else b""
    rsrc = b""
    if strings is not None:
        rsrc += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 16, 0x80000000 | 0x18)
        rsrc += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 1, 0x80000000 | 0x30)
        rsrc += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 0x409, 0x48)
        rsrc += struct.pack("<IIII", 0x1000 + 0x58, len(vs), 0, 0)
        rsrc += vs
    opt_size = 240 if pe32plus else 224
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664 if pe32plus else 0x14C, 1, 0, 0, 0, opt_size, 0x102)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if pe32plus else 0x10B)
    dd = 112 if pe32plus else 96
    struct.pack_into("<I", opt, dd - 4, 16)
    if strings is not None:
        struct.pack_into("<II", opt, dd + 16, 0x1000, len(rsrc))
    sec = b".rsrc\0\0\0" + struct.pack("<IIIIIIHHI", len(rsrc), 0x1000, len(rsrc), 0x200, 0, 0, 0, 0, 0x40000040)
    head = bytes(dos) + coff + bytes(opt) + sec
    return head + b"\0" * (0x200 - len(head)) + rsrc + b"\0" * pad


class PeInfoTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.root / name
        p.write_bytes(data)
        return p

    def test_reads_string_table(self):
        info = {"CompanyName": "Sierra", "FileDescription": "Zeus and Poseidon", "ProductName": "Zeus & Poseidon"}
        for plus in (False, True):
            p = self.write(f"Zeus{plus}.exe", make_pe(info, pe32plus=plus))
            self.assertEqual(peinfo.version_strings(p), info)
            self.assertEqual(peinfo.game_name(peinfo.version_strings(p)), "Zeus & Poseidon")

    def test_generic_values_and_broken_files(self):
        self.assertEqual(peinfo.game_name({"ProductName": "Unreal Engine", "FileDescription": "The Sinking City 2"}),
                         "The Sinking City 2")
        self.assertEqual(peinfo.game_name({"ProductName": "Need for Speed™ SHIFT"}), "Need for Speed SHIFT")
        for bad in ({"ProductName": "Launcher"}, {"ProductName": "  "}, {"FileDescription": "1.0.0.0"}, {}):
            self.assertEqual(peinfo.game_name(bad), "", bad)
        self.assertEqual(peinfo.version_strings(self.write("leer.exe", make_pe(None))), {})
        self.assertEqual(peinfo.version_strings(self.write("text.exe", b"#!/bin/sh\n")), {})
        self.assertEqual(peinfo.version_strings(self.write("kurz.exe", make_pe({"ProductName": "X"})[:300])), {})
        self.assertEqual(peinfo.version_strings(self.root / "fehlt.exe"), {})
        self.assertEqual(peinfo.parse_version_resource(b"\x10\x00"), {})


# ----------------------------------------------------------------------------- Namen
class NameTest(unittest.TestCase):
    def test_copy_markers(self):
        for name, want in (("Freelancer - Kopie", ("Freelancer", "Kopie")), ("Freelancer HD Edition - Kopie", ("Freelancer HD Edition", "Kopie")),
                           ("Zeus and Poseidon_bck", ("Zeus and Poseidon", "bck")), ("Spiel (2)", ("Spiel", "2")),
                           ("Spiel - Copy (3)", ("Spiel", "Copy 3")), ("Kopie von Spiel", ("Spiel", "Kopie von")),
                           ("Spiel.bak", ("Spiel", "bak")), ("Spiel backup", ("Spiel", "backup")), ("Spiel - Sicherung", ("Spiel", "Sicherung")),
                           ("GRID (2019)", ("GRID (2019)", "")), ("Freelancer", ("Freelancer", ""))):
            self.assertEqual(identity.split_copy_marker(name), want, name)

    def test_variant_labels(self):
        for name, want in (("Freelancer HD Edition", ("Freelancer", "HD Edition")), ("EmperorDAdRdM 1360 x 768", ("EmperorDAdRdM", "1360 x 768")),
                           ("Herrscher des Olymp - Zeus 1360 x 768", ("Herrscher des Olymp - Zeus", "1360 x 768")),
                           ("Return to Castle Wolfenstein - original", ("Return to Castle Wolfenstein", "original")),
                           ("Spiel [Widescreen]", ("Spiel", "Widescreen")), ("Spiel Remastered Mod", ("Spiel", "Remastered Mod")),
                           ("Spiel - Fan-Patch", ("Spiel", "Fan-Patch")), ("Spiel Modded", ("Spiel", "Modded")),
                           ("Spiel HD 1920x1080", ("Spiel", "HD 1920x1080")), ("Anno 2070 Complete Edition", ("Anno 2070 Complete Edition", "")),
                           ("HD", ("HD", ""))):
            self.assertEqual(identity.split_variant(name), want, name)
        self.assertEqual(identity.name_parts("Freelancer HD Edition - Kopie"), ("Freelancer", "HD Edition", "Kopie"))
        self.assertEqual(identity.variant_title("Zeus and Poseidon", "1360 x 768"), "Zeus and Poseidon (1360 x 768)")
        self.assertEqual(identity.variant_title("Unreal", ""), "Unreal")


# ----------------------------------------------------------------------------- Kopie oder Variante
def files(root: Path, spec: dict[str, int]) -> Path:
    for rel, size in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return root


GAME_FILES = {f"DATA/file{i:03d}.dat": 1000 + i for i in range(120)} | {"EXE/Freelancer.exe": 4000, "DLLS/a.dll": 300}


class CopyDetectionTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def folders(self, *names: str) -> list[identity.FolderInfo]:
        return [identity.FolderInfo(identity.external.external_id(n), self.root / n) for n in names]

    def detect(self, *names: str) -> dict[str, str]:
        infos = self.folders(*names)
        return identity.find_copies(infos, {f.pid: identity.fingerprint(f.path) for f in infos})

    def test_copy_with_kopie_name_and_identical_content(self):
        files(self.root / "Freelancer", GAME_FILES)
        shutil.copytree(self.root / "Freelancer", self.root / "Freelancer - Kopie")
        # Spielstände, Einstellungen und Protokolle zählen nicht
        files(self.root / "Freelancer - Kopie", {"SAVE/slot1.sav": 77, "EXE/freelancer.ini": 12, "EXE/flspew.log": 9000,
                                                  "Screenshots/a.png": 5000})
        self.assertEqual(self.detect("Freelancer", "Freelancer - Kopie"), {"ext-freelancer-kopie": "ext-freelancer"})
        # Original ohne Kennzeichen – auch wenn die Kopie älter ist
        past = time.time() - 3600
        os.utime(self.root / "Freelancer - Kopie", (past, past))
        self.assertEqual(self.detect("Freelancer - Kopie", "Freelancer"), {"ext-freelancer-kopie": "ext-freelancer"})

    def test_kopie_with_different_content_is_a_variant(self):
        files(self.root / "Freelancer", GAME_FILES)
        files(self.root / "Freelancer - Kopie", {**GAME_FILES, **{f"MOD/m{i}.dat": 50 + i for i in range(40)}})
        self.assertEqual(self.detect("Freelancer", "Freelancer - Kopie"), {})

    def test_hd_variant_and_oldest_wins_without_marker(self):
        files(self.root / "Freelancer", GAME_FILES)
        hd = {k: v * 3 for k, v in GAME_FILES.items()} | {"JFLP/readme.txt": 10}
        files(self.root / "Freelancer HD Edition", hd)
        self.assertEqual(self.detect("Freelancer", "Freelancer HD Edition"), {})
        # zwei Ordner ohne Kennzeichen mit gleichem Inhalt: der ältere ist das Original
        shutil.copytree(self.root / "Freelancer", self.root / "Freelancer Zweitinstallation")
        past = time.time() - 7200
        os.utime(self.root / "Freelancer", (past, past))
        self.assertEqual(self.detect("Freelancer Zweitinstallation", "Freelancer"),
                         {"ext-freelancer-zweitinstallation": "ext-freelancer"})

    def test_bck_name_alone_is_no_copy(self):
        files(self.root / "Zeus and Poseidon_bck", GAME_FILES)
        files(self.root / "Anderes Spiel", {"x.dat": 5})
        self.assertEqual(self.detect("Zeus and Poseidon_bck", "Anderes Spiel"), {})

    def test_fingerprint_cache(self):
        files(self.root / "Spiel", GAME_FILES)
        cache_file = self.root / "fp.json"
        cache = identity.FingerprintCache(cache_file)
        first = cache.get(self.root / "Spiel")
        cache.save()
        self.assertTrue(cache_file.exists())
        again = identity.FingerprintCache(cache_file)
        with mock.patch.object(identity, "fingerprint", side_effect=AssertionError("neu berechnet")):
            self.assertEqual(again.get(self.root / "Spiel").hashes, first.hashes)
        files(self.root / "Spiel", {"neu.dat": 1})                       # Ordner geändert → neu berechnen
        self.assertNotEqual(again.get(self.root / "Spiel").hashes, first.hashes)
        again.prune([])
        self.assertEqual(again.data, {})
        self.assertIsNone(again.get(self.root / "fehlt"))
        self.assertAlmostEqual(identity.jaccard(frozenset({1, 2, 3}), frozenset({1, 2, 4})), 0.5)


# ----------------------------------------------------------------------------- Titel
def cat(base: str, title: str = "", cover: str = "https://c/x.jpg", rule: str = "strict") -> dict:
    return {"source": "gog", "id": "1", "title": title or base, "base": base, "cover": cover, "rule": rule, "query": base}


class ResolveTest(unittest.TestCase):
    def lookup(self, table: dict):
        calls = []

        def fn(queries, company, errors):
            calls.append((list(queries), company))
            for q in queries:
                if q in table:
                    return table[q]
            return None
        fn.calls = calls
        return fn

    def test_zeus_variant_takes_gog_title(self):
        ident = identity.resolve("Herrscher des Olymp - Zeus 1360 x 768", None, [("1207659039", "Zeus and Poseidon")],
                                 pe_info={"ProductName": "Zeus & Poseidon", "CompanyName": "Sierra"})
        self.assertEqual((ident.title, ident.base_title, ident.label, ident.source, ident.gog_match),
                         ("Zeus and Poseidon (1360 x 768)", "Zeus and Poseidon", "1360 x 768", "gog", "1207659039"))

    def test_emperor_subtitle_from_catalog_with_company(self):
        lookup = self.lookup({"Emperor": cat("Emperor: Rise of the Middle Kingdom", rule="subtitle")})
        ident = identity.resolve("EmperorDAdRdM 1360 x 768", None, [], lookup,
                                 pe_info={"ProductName": "Emperor", "CompanyName": "Sierra"})
        self.assertEqual(ident.title, "Emperor: Rise of the Middle Kingdom (1360 x 768)")
        self.assertEqual(ident.source, "catalog")
        self.assertEqual(lookup.calls[-1], (["EmperorDAdRdM", "Emperor"], "Sierra"))

    def test_freelancer_pe_title_and_wikipedia_cover(self):
        wiki_calls = []

        def wiki(queries, errors):
            wiki_calls.append(queries)
            return wikidata.WikiGame("Q937066", {"en": "Freelancer", "de": "Freelancer: The Universe of Possibility"},
                                     "Freelancer (video game)", "https://upload.wikimedia.org/fl.jpg")
        ident = identity.resolve("Freelancer HD Edition", None, [], self.lookup({}), wiki,
                                 pe_info={"ProductName": "Freelancer", "CompanyName": "Microsoft Corporation"})
        self.assertEqual((ident.title, ident.source, ident.cover, ident.wikidata),
                         ("Freelancer (HD Edition)", "pe", "https://upload.wikimedia.org/fl.jpg", "Q937066"))
        self.assertEqual(wiki_calls, [["Freelancer"]])                   # Cover zum Grundspiel
        self.assertTrue(ident.complete)

    def test_edition_words_and_wikidata_title(self):
        anno = identity.resolve("Anno 2070 Complete Edition", None, [], self.lookup({"Anno 2070": cat("Anno 2070", "Anno 2070™")}))
        self.assertEqual((anno.title, anno.source), ("Anno 2070 Complete Edition", "catalog"))

        def wiki(queries, errors):
            return wikidata.WikiGame("Q936968", {"en": "The Operative: No One Lives Forever", "de": "No One Lives Forever"})
        nolf = identity.resolve("The Operative - No One Lives Forever GOTY Edition", None, [], self.lookup({}), wiki)
        self.assertEqual((nolf.title, nolf.source), ("The Operative: No One Lives Forever GOTY Edition", "wikidata"))
        de = identity.resolve("The Operative - No One Lives Forever GOTY Edition", None, [], self.lookup({}), wiki, lang="de")
        self.assertEqual(de.title, "No One Lives Forever")              # deutsche Bezeichnung, kein gemeinsamer Anfang

    def test_label_part_of_official_title(self):
        ident = identity.resolve("Garry's Mod", None, [], self.lookup({"Garry's Mod": cat("Garry's Mod")}))
        self.assertEqual((ident.title, ident.label), ("Garry's Mod", ""))
        gog = identity.resolve("Spiel HD", None, [("5", "Spiel HD")])
        self.assertEqual((gog.title, gog.label, gog.gog_match), ("Spiel HD", "", "5"))

    def test_folder_fallback_copy_label_and_offline(self):
        ident = identity.resolve("Mein Spiel - Kopie", None, [], copy_label=True)
        self.assertEqual((ident.title, ident.source), ("Mein Spiel (Kopie)", "folder"))
        self.assertEqual(identity.resolve("Mein Spiel - Kopie").title, "Mein Spiel")

        def offline(queries, company, errors):
            errors.append(OSError("kein Netz"))
            return None
        ident = identity.resolve("Unreal", None, [], offline, pe_info={"ProductName": "Unreal"})
        self.assertEqual((ident.title, ident.source, ident.complete), ("Unreal", "pe", False))

    def test_never_freelancer_simulator(self):
        self.assertFalse(covers.title_matches("Freelancer", "Freelancer Simulator"))
        self.assertFalse(covers.subtitle_matches("Freelancer", "Freelancer Simulator"))
        ident = identity.resolve("Freelancer", None, [("9", "Freelancer Simulator")])
        self.assertEqual((ident.title, ident.gog_match), ("Freelancer", ""))


class CatalogRulesTest(unittest.TestCase):
    def test_official_prefix_and_remainder(self):
        self.assertEqual(covers.official_prefix("Anno 2070™", "Anno 2070"), "Anno 2070")
        self.assertEqual(covers.official_prefix("Soldier of Fortune: Platinum Edition", "Soldier of Fortune"), "Soldier of Fortune")
        self.assertEqual(covers.official_prefix("Need for Speed™ SHIFT", "need for speed shift"), "Need for Speed SHIFT")
        self.assertEqual(covers.remainder("Anno 2070 Complete Edition", "Anno 2070"), "Complete Edition")
        self.assertEqual(covers.remainder("The Operative - No One Lives Forever GOTY Edition",
                                          "The Operative: No One Lives Forever"), "GOTY Edition")
        self.assertEqual(covers.remainder("Herrscher des Olymp - Zeus", "Zeus and Poseidon"), "")
        self.assertEqual(covers.remainder("Unreal", "Unreal"), "")
        self.assertEqual(covers.remainder("Unrealistic", "Unreal"), "")

    def test_subtitle_needs_company(self):
        self.assertTrue(covers.subtitle_matches("Emperor", "Emperor: Rise of the Middle Kingdom"))
        self.assertFalse(covers.subtitle_matches("Emperor", "Emperor of the Fading Suns"))
        self.assertTrue(covers.company_matches("Sierra", ["Sierra"]))
        self.assertTrue(covers.company_matches("Sierra On-Line, Inc.", ["Impressions Games", "Sierra Entertainment"]))
        self.assertFalse(covers.company_matches("Microsoft Corporation", ["Sierra"]))
        self.assertFalse(covers.company_matches("Games Inc.", ["Some Games Inc."]))     # nur Füllwörter

    def test_gog_catalog_rules(self):
        products = [{"id": 1, "title": "Emperor of the Fading Suns Enhanced", "developers": ["Holistic Design"], "publishers": []},
                    {"id": 2, "title": "Emperor: Rise of the Middle Kingdom", "developers": ["Sierra"], "publishers": ["Activision"],
                     "coverVertical": "https://images.gog.com/emp.jpg"}]

        class Resp:
            ok = True

            def json(self):
                return {"products": products}
        session = types.SimpleNamespace(get=lambda *a, **k: Resp())
        self.assertIsNone(covers._gog(session, "Emperor"))               # ohne Hersteller kein Untertitel-Treffer
        hit = covers._gog(session, "Emperor", "Sierra")
        self.assertEqual((hit["id"], hit["base"], hit["rule"], hit["cover"]),
                         ("2", "Emperor: Rise of the Middle Kingdom", "subtitle", "https://images.gog.com/emp.jpg"))
        self.assertIsNone(covers._gog(session, "Emperor", "Holistic"))   # „of the Fading Suns“ ist kein Untertitel
        products.append({"id": 3, "title": "Emperor: Battle for Dune", "developers": ["Sierra"], "publishers": []})
        self.assertIsNone(covers._gog(session, "Emperor", "Sierra"))     # mehrdeutig → lieber nichts


# ----------------------------------------------------------------------------- Wikidata / Wikipedia
class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


SEARCH = {
    ("Freelancer", "en"): {"search": [{"id": "Q215279", "label": "freelancer", "match": {"type": "label", "text": "freelancer"}},
                                      {"id": "Q937066", "label": "Freelancer", "match": {"type": "label", "text": "Freelancer"}},
                                      {"id": "Q2360310", "label": "Freelancer Simulator", "match": {"type": "label", "text": "Freelancer Simulator"}}]},
    ("The Operative: No One Lives Forever", "en"): {"search": [{"id": "Q936968", "label": "The Operative: No One Lives Forever",
                                                                "match": {"type": "label", "text": "The Operative: No One Lives Forever"}}]},
}
CLAIMS = {"Q215279": ["Q28640"], "Q937066": ["Q7889"], "Q936968": ["Q7889"], "Q2360310": ["Q7889"]}
ENTITIES = {
    "Q937066": {"labels": {"en": {"value": "Freelancer"}, "de": {"value": "Freelancer: The Universe of Possibility"}},
                "aliases": {}, "sitelinks": {"enwiki": {"title": "Freelancer (video game)"}}},
    "Q936968": {"labels": {"en": {"value": "The Operative: No One Lives Forever"}, "de": {"value": "No One Lives Forever"}},
                "aliases": {"de": [{"value": "The Operative: No One Lives Forever"}]},
                "sitelinks": {"enwiki": {"title": "The Operative: No One Lives Forever"}}},
}
SUMMARY = {"Freelancer_%28video_game%29": {"originalimage": {"source": "https://upload.wikimedia.org/wikipedia/en/4/4d/Fl_box.jpg?utm_source=en.wikipedia.org&utm_campaign=api"}},
           "The_Operative%3A_No_One_Lives_Forever": {"thumbnail": {"source": "https://upload.wikimedia.org/nolf.jpg"}}}


class FakeWiki:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        assert "galaxy-linux" in (headers or {}).get("User-Agent", "")
        p = params or {}
        if p.get("action") == "wbsearchentities":
            return FakeResp(SEARCH.get((p["search"], p["language"]), {"search": []}))
        if p.get("action") == "wbgetclaims":
            return FakeResp({"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": c}}}} for c in CLAIMS.get(p["entity"], [])]}})
        if p.get("action") == "wbgetentities":
            return FakeResp({"entities": {p["ids"]: ENTITIES.get(p["ids"], {"missing": ""})}})
        title = url.rsplit("/", 1)[1]
        return FakeResp(SUMMARY[title]) if title in SUMMARY else FakeResp({}, 404)


class WikidataTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.patch = mock.patch("galaxy_linux.paths.API_CACHE_DIR", Path(self.td.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_parsers(self):
        self.assertEqual(wikidata.parse_search(SEARCH[("Freelancer", "en")])[1], ("Q937066", ["Freelancer", "Freelancer"]))
        self.assertEqual(wikidata.parse_search({"error": "x"}), [])
        self.assertEqual(wikidata.parse_instance_of({"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q7889"}}}},
                                                                         {"mainsnak": {}}]}}), {"Q7889"})
        game = wikidata.parse_entity({"entities": {"Q936968": ENTITIES["Q936968"]}}, "Q936968")
        self.assertEqual((game.label("en"), game.label("de"), game.enwiki),
                         ("The Operative: No One Lives Forever", "No One Lives Forever", "The Operative: No One Lives Forever"))
        self.assertIsNone(wikidata.parse_entity({"entities": {"Q1": {"missing": ""}}}, "Q1"))
        self.assertEqual(wikidata.parse_summary(SUMMARY["Freelancer_%28video_game%29"]),
                         "https://upload.wikimedia.org/wikipedia/en/4/4d/Fl_box.jpg")
        self.assertEqual(wikidata.parse_summary({}), "")
        self.assertEqual(wikidata.query_variants("The Operative - No One Lives Forever"),
                         ["The Operative - No One Lives Forever", "The Operative: No One Lives Forever"])

    def test_find_game_only_video_games_with_same_name(self):
        session = FakeWiki()
        game = wikidata.find_game(session, ["Freelancer"])
        self.assertEqual((game.qid, game.label(), game.image),
                         ("Q937066", "Freelancer", "https://upload.wikimedia.org/wikipedia/en/4/4d/Fl_box.jpg"))
        claims = [p["entity"] for _u, p in session.calls if p.get("action") == "wbgetclaims"]
        self.assertEqual(claims, ["Q215279", "Q937066"])                 # „Freelancer Simulator“ nie geprüft
        n = len(session.calls)
        self.assertEqual(wikidata.find_game(session, ["Freelancer"]).qid, "Q937066")
        self.assertEqual(len(session.calls), n)                          # aus dem Cache
        nolf = wikidata.find_game(session, ["The Operative - No One Lives Forever"])
        self.assertEqual((nolf.qid, nolf.image), ("Q936968", "https://upload.wikimedia.org/nolf.jpg"))
        self.assertIsNone(wikidata.find_game(session, ["Gibt Es Nicht"]))

    def test_network_errors_are_not_cached(self):
        class Down:
            def get(self, *a, **k):
                raise OSError("offline")
        errors: list = []
        self.assertIsNone(wikidata.find_game(Down(), ["Freelancer"], errors))
        self.assertEqual(len(errors), 1)
        self.assertEqual(wikidata.find_game(FakeWiki(), ["Freelancer"]).qid, "Q937066")


# ----------------------------------------------------------------------------- Backend: Erkennung, Bearbeiten …
class BackendIdentityTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        self.base = base
        self.gog = base / "GOG"
        self.ext = base / "Repack" / "02.Games"
        for d in (self.gog, self.ext, base / "Repack" / "01.Prefixes"):
            d.mkdir(parents=True)
        cfg, data, cache = base / "cfg", base / "data", base / "cache"
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        from galaxy_linux.backend import Backend
        self.b = Backend()
        s = self.b.settings
        s.install_dir, s.prefix_dir = str(self.gog), str(self.gog / "prefixes")
        s.libraries = [CustomLibrary(id="external", name="External", games_dir=str(self.ext), prefix_dir=str(base / "Repack" / "01.Prefixes"))]
        fl = files(self.ext / "Freelancer", GAME_FILES)
        (fl / "EXE" / "Freelancer.exe").write_bytes(make_pe({"ProductName": "Freelancer", "CompanyName": "Microsoft"}, pad=300_000))
        shutil.copytree(fl, self.ext / "Freelancer - Kopie")
        hd = files(self.ext / "Freelancer HD Edition", {k: v * 2 for k, v in GAME_FILES.items()})
        (hd / "EXE" / "Freelancer.exe").write_bytes(make_pe({"ProductName": "Freelancer"}, pad=310_000))
        zeus = files(self.ext / "Herrscher des Olymp - Zeus 1360 x 768", {"DATA/zeus.dat": 5000})
        (zeus / "Zeus.exe").write_bytes(make_pe({"ProductName": "Zeus & Poseidon", "CompanyName": "Sierra"}, pad=400_000))
        gz = files(self.gog / "Zeus and Poseidon_bck", {"zeus.exe": 400_000})
        self.b.registry.set(InstalledGame("1207659039", "Zeus and Poseidon", "windows", str(gz), method="imported"))
        past = time.time() - 600
        for p in [self.ext, *self.ext.rglob("*")]:
            os.utime(p, (past, past))
        self.b.scan_install_dirs(wait=0.05, create_prefixes=False)

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()

    def test_resolve_externals_offline(self):
        changed = self.b.resolve_externals(network=False)
        self.assertIn("ext-freelancer-kopie", changed)
        reg = self.b.registry
        self.assertEqual(reg.get("ext-freelancer-kopie").duplicate_of, "ext-freelancer")
        self.assertEqual(reg.get("ext-freelancer").title, "Freelancer")
        self.assertEqual(reg.get("ext-freelancer-hd-edition").title, "Freelancer (HD Edition)")
        z = reg.get("ext-herrscher-des-olymp-zeus-1360-x-768")
        self.assertEqual((z.title, z.gog_match, z.variant), ("Zeus and Poseidon (1360 x 768)", "1207659039", "1360 x 768"))
        self.assertEqual(z.identity, 0)                                   # ohne Netz: später vollständig erkennen
        self.assertEqual(self.b.copy_of("ext-freelancer-kopie").product_id, "ext-freelancer")
        from galaxy_linux.launch import goverlay_profiles as gp
        self.assertEqual([v.name for v in gp.dry_run(self.b)],
                         ["Freelancer", "Freelancer (HD Edition)", "Zeus and Poseidon", "Zeus and Poseidon (1360 x 768)"])
        from galaxy_linux import library_view as lv
        f = lv.facts(self.b, self.b.entry("ext-freelancer-kopie"))
        self.assertEqual(f.copy_of, "Freelancer")
        self.assertFalse(lv.LibraryFilter().matches(f))                   # Kopien nur mit „Kopien anzeigen“
        self.assertTrue(lv.LibraryFilter(copies=True).matches(f))
        self.assertTrue(lv.LibraryFilter.from_dict(lv.LibraryFilter(copies=True).to_dict()).copies)

    def test_resolve_externals_online_marks_complete(self):
        with mock.patch.object(covers, "lookup", return_value=None) as lk, \
             mock.patch.object(wikidata, "find_game", return_value=None):
            self.b.resolve_externals(network=True)
            self.assertTrue(lk.called)
        g = self.b.registry.get("ext-freelancer")
        self.assertEqual((g.identity, g.title_source), (identity.IDENTITY_VERSION, "pe"))
        with mock.patch.object(covers, "lookup", side_effect=AssertionError("nicht erneut")):
            self.assertEqual(self.b.resolve_externals(network=True), [])  # schon erkannt

    def test_manual_title_before_031_is_kept(self):
        self.b.registry.update("ext-freelancer", title="Mein Freelancer")
        self.b.resolve_externals(network=False)
        self.assertEqual(self.b.display_title("ext-freelancer"), "Mein Freelancer")
        self.assertEqual(self.b.registry.get("ext-freelancer").title, "Freelancer")

    def test_custom_title_and_cover_everywhere(self):
        from galaxy_linux.api.models import LibraryEntry
        self.b.library = [LibraryEntry(id="1207659039", title="Zeus and Poseidon")]
        self.assertEqual(self.b.set_custom_title("1207659039", "  Zeus  &  Poseidon  "), "Zeus & Poseidon")
        self.assertEqual(self.b.entry("1207659039").title, "Zeus & Poseidon")
        self.assertEqual([e.title for e in self.b.visible_library()], ["Zeus & Poseidon"])
        self.assertEqual(self.b.library[0].title, "Zeus and Poseidon")      # Bibliothek selbst unverändert
        self.assertEqual(self.b.default_title("1207659039"), "Zeus and Poseidon")
        self.b.set_custom_title("1207659039", "Zeus and Poseidon")          # wie automatisch → zurückgesetzt
        self.assertEqual(self.b.game_settings.get("1207659039").custom_title, "")
        img = self.base / "cover.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nbild")
        dest = self.b.set_custom_cover("ext-freelancer", img)
        self.assertTrue(Path(dest).is_file() and Path(dest).parent == self.b.custom_cover_dir())
        self.assertEqual(self.b.cover_url("ext-freelancer"), "file://" + dest)
        self.assertEqual(self.b.api.cached_image("file://" + dest), Path(dest))
        with mock.patch.object(covers, "lookup", return_value=None):
            self.assertEqual(self.b.meta("ext-freelancer").vertical_cover, "file://" + dest)
        img.unlink()
        self.assertEqual(self.b.cover_url("ext-freelancer"), "file://" + dest)   # Kopie bleibt
        self.b.set_custom_cover("ext-freelancer", None)
        self.assertFalse(Path(dest).exists())
        self.assertEqual(self.b.game_settings.get("ext-freelancer").custom_cover, "")


if __name__ == "__main__":
    unittest.main()
