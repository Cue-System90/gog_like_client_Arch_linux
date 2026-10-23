"""Bibliotheksansicht wie Galaxy 2.1 (0.3.0): Sortieren, Gruppieren, Filter, Tags, Bewertung, Lesezeichen."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from galaxy_linux import library_view as lv
from galaxy_linux.api.models import LibraryEntry
from galaxy_linux.settings import GameSettings, GameSettingsStore, GlobalSettings


class Paths(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = Path(self.td.name)
        cfg, data, cache = base / "cfg", base / "data", base / "cache"
        self.cache = cache
        self.patch = mock.patch.multiple(
            "galaxy_linux.paths", CONFIG_DIR=cfg, DATA_DIR=data, CACHE_DIR=cache, LOG_DIR=data / "logs",
            MANIFEST_DIR=data / "manifests", IMAGE_CACHE_DIR=cache / "images", API_CACHE_DIR=cache / "api",
            SETTINGS_FILE=cfg / "settings.json", GAME_SETTINGS_FILE=cfg / "games.json", CREDENTIALS_FILE=cfg / "credentials.json",
            INSTALLED_FILE=data / "installed.json", PLAYTIME_FILE=data / "playtime.json")
        self.patch.start()
        cfg.mkdir(parents=True)
        lv._GAMESDB_CACHE.clear()

    def tearDown(self):
        self.patch.stop()
        self.td.cleanup()


def F(pid, title, **kw):
    return lv.GameFacts(id=pid, title=title, **kw)


class SortTest(unittest.TestCase):
    def test_title_ignores_article_and_desc(self):
        items = [F("1", "The Witcher"), F("2", "Albion"), F("3", "Zork")]
        self.assertEqual([f.title for f in lv.sort_facts(items)], ["Albion", "The Witcher", "Zork"])
        self.assertEqual([f.title for f in lv.sort_facts(items, "title", True)], ["Zork", "The Witcher", "Albion"])

    def test_missing_values_last_in_both_directions(self):
        items = [F("1", "A", rating=3), F("2", "B"), F("3", "C", rating=5)]
        self.assertEqual([f.id for f in lv.sort_facts(items, "rating", True)], ["3", "1", "2"])
        self.assertEqual([f.id for f in lv.sort_facts(items, "rating", False)], ["1", "3", "2"])
        items = [F("1", "A", playtime=10), F("2", "B", playtime=300), F("3", "C")]
        self.assertEqual([f.id for f in lv.sort_facts(items, "playtime", True)], ["2", "1", "3"])

    def test_resolve_legacy_and_new(self):
        self.assertEqual(lv.resolve_sort({"sort": 2}), ("playtime", True))
        self.assertEqual(lv.resolve_sort({"sort": 0}), ("title", False))
        self.assertEqual(lv.resolve_sort({"sort": 0, "sort_by": "critics", "sort_desc": False}), ("critics", False))
        self.assertEqual(lv.resolve_sort({"sort": "x", "sort_by": "unsinn"}), ("title", False))


class GroupTest(unittest.TestCase):
    def test_tags_multi_and_none_last(self):
        items = [F("1", "A", tags=["Rpg", "Fav"]), F("2", "B"), F("3", "C", tags=["fav"])]
        groups = lv.group_facts(items, "tags")
        self.assertEqual([g for g, _ in groups], ["Fav", "fav", "Rpg", "Keine Tags"])
        self.assertEqual([f.id for f in groups[0][1]], ["1"])
        self.assertEqual([f.id for f in groups[-1][1]], ["2"])

    def test_release_buckets(self):
        now = time.mktime(time.strptime("2026-10-23", "%Y-%m-%d")) + 3600
        items = [F("1", "A", release_date="2026-10-23"), F("2", "B", release_date="2026-10-10"),
                 F("3", "C", release_date="2026-02-01"), F("4", "D", release_date="2019-05-05"),
                 F("5", "E", release_date="2027-01-01"), F("6", "F")]
        names = [g for g, _ in lv.group_facts(items, "release", now)]
        self.assertEqual(names, ["Demnächst", "Heute", "Letzten 30 Tage", "Dieses Jahr veröffentlicht", "2019",
                                 "Unbekanntes Veröffentlichungsdatum"])

    def test_rating_installed_platform_critics(self):
        items = [F("1", "A", rating=5, state="installed", critics=93.5), F("2", "B", state="not_installed", critics=71),
                 F("3", "C", rating=2, state="update", source="lib:x", source_name="Repacks")]
        self.assertEqual([g for g, _ in lv.group_facts(items, "rating")], ["★★★★★", "★★", "Keine Bewertung"])
        self.assertEqual([(g, [f.id for f in l]) for g, l in lv.group_facts(items, "installed")],
                         [("Installiert", ["1", "3"]), ("Nicht installiert", ["2"])])
        self.assertEqual([g for g, _ in lv.group_facts(items, "platform")], ["GOG.com", "Repacks"])
        self.assertEqual([g for g, _ in lv.group_facts(items, "critics")], ["90–100", "70–79", "Keine Bewertung"])
        self.assertEqual(lv.group_facts(items, "none"), [("", items)])


class FilterTest(unittest.TestCase):
    def test_or_within_and_across(self):
        a = F("1", "A", os=["windows"], tags=["x"], rating=4, state="installed", genres=["RPG"])
        b = F("2", "B", os=["linux", "windows"], state="not_installed")
        c = F("3", "C", os=["linux"], tags=["y"], state="update", hidden=True)
        flt = lv.LibraryFilter(os=["linux"])
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f)], ["2"])          # c ausgeblendet
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f, show_hidden=True)], ["2", "3"])
        flt = lv.LibraryFilter(status=["installed"], tags=["x", ""])
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f, True)], ["1"])
        flt = lv.LibraryFilter(tags=[""])
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f, True)], ["2"])
        flt = lv.LibraryFilter(status=["update"], hidden="hidden")
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f)], ["3"])
        flt = lv.LibraryFilter(ratings=[0], genres=[])
        self.assertEqual([f.id for f in (a, b, c) if flt.matches(f, True)], ["2", "3"])

    def test_roundtrip_and_count(self):
        flt = lv.LibraryFilter(status=["installed"], ratings=[5, 0], favorites=True, hidden="visible")
        d = flt.to_dict()
        self.assertEqual(lv.LibraryFilter.from_dict(d), flt)
        self.assertEqual(flt.count(), 5)
        self.assertTrue(lv.LibraryFilter().is_empty())
        self.assertEqual(lv.LibraryFilter.from_dict({"ratings": [9, "3", "x"], "hidden": "egal"}).to_dict(), {"ratings": [3]})

    def test_search_in_tags_and_genres(self):
        f = F("1", "Albion", tags=["Kindheit"], genres=["Rollenspiel"], developers=["Blue Byte"])
        for q in ("alb", "kind", "rollen", "blue", ""):
            self.assertTrue(lv.matches_search(f, q), q)
        self.assertFalse(lv.matches_search(f, "zork"))


class TagsAndFactsTest(Paths):
    def test_tags_rename_delete_rating(self):
        gs = GameSettingsStore()
        lv.set_tag(gs, "1", "  Lieblings  Spiel ", True)
        lv.set_tag(gs, "2", "RPG", True)
        lv.set_tag(gs, "2", "Lieblings Spiel", True)
        self.assertEqual(lv.all_tags(gs), ["Lieblings Spiel", "RPG"])
        self.assertEqual(lv.rename_tag(gs, "Lieblings Spiel", "Favorit"), 2)
        self.assertEqual(gs.get("2").tags, ["Favorit", "RPG"])
        self.assertEqual(lv.delete_tag(gs, "RPG"), 1)
        self.assertEqual(lv.all_tags(gs), ["Favorit"])
        lv.set_tag(gs, "1", "Favorit", False)
        self.assertEqual(gs.get("1").tags, [])
        self.assertEqual(lv.set_rating(gs, "1", 9), 5)
        self.assertEqual(lv.set_rating(gs, "1", -1), 0)

    def test_facts_from_gamesdb_cache_and_hidden(self):
        api_dir = self.cache / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "gamesdb-7.json").write_text(json.dumps({
            "first_release_date": "2001-02-03T00:00:00",
            "game": {"genres": [{"name": {"*": "Strategy", "de-DE": "Strategie"}}], "aggregated_rating": 87.5,
                     "developers": [{"name": "Studio"}]}}))
        gs = GameSettingsStore()
        gs.update("7", tags=["a"], rating=4)
        settings = GlobalSettings()
        settings.library_new_ids = {"7": 1234.0}
        backend = SimpleNamespace(game_settings=gs, registry=SimpleNamespace(get=lambda pid: None),
                                  library_of=lambda pid: None, playtime=SimpleNamespace(get=lambda pid: (90, 55.0)),
                                  settings=settings, state=lambda pid: "not_installed", _meta={})
        e = LibraryEntry(id="7", title="Spiel", works_on={"windows": True, "linux": True}, is_hidden=True)
        f = lv.facts(backend, e)
        self.assertEqual((f.genres, f.critics, f.release_date, f.developers), (["Strategie"], 87.5, "2001-02-03", ["Studio"]))
        self.assertEqual((f.tags, f.rating, f.playtime, f.last_played, f.added, f.os), (["a"], 4, 90, 55.0, 1234.0,
                                                                                      ["windows", "linux"]))
        self.assertTrue(f.hidden)                       # bei GOG ausgeblendet
        gs.update("7", unhidden=True)
        self.assertFalse(lv.facts(backend, e).hidden)
        gs.update("7", hidden=True)
        self.assertTrue(lv.facts(backend, e).hidden)


class BookmarkTest(Paths):
    def test_add_dedup_rename_move_remove(self):
        s = GlobalSettings()
        b1 = lv.add_bookmark(s, lv.Bookmark(id="", name="Installiert RPG", view="installed", filter={"tags": ["RPG"]}))
        same = lv.add_bookmark(s, lv.Bookmark(id="x", name="anders", view="installed", filter={"tags": ["RPG"]}))
        self.assertEqual(same.id, b1.id)
        b2 = lv.add_bookmark(s, lv.Bookmark(id="g", name="Witcher", view="game", pid="1207658924"))
        self.assertEqual([b.name for b in lv.bookmarks(s)], ["Installiert RPG", "Witcher"])
        self.assertTrue(lv.rename_bookmark(s, b2.id, "  Hexer  "))
        self.assertTrue(lv.move_bookmark(s, b2.id, -1))
        self.assertFalse(lv.move_bookmark(s, b2.id, -1))
        self.assertEqual([b.name for b in lv.bookmarks(GlobalSettings.load())], ["Hexer", "Installiert RPG"])
        self.assertTrue(lv.remove_bookmark(s, b2.id))
        self.assertEqual(len(lv.bookmarks(s)), 1)
        self.assertIsNotNone(lv.find_bookmark(s, lv.Bookmark(id="", name="?", view="installed", filter={"tags": ["RPG"]})))

    def test_invalid_entries_dropped(self):
        self.assertIsNone(lv.Bookmark.from_dict({"name": "x", "view": "store:home"}))
        self.assertIsNone(lv.Bookmark.from_dict({"name": "x", "view": "game"}))
        self.assertIsNone(lv.Bookmark.from_dict({"view": "all"}))
        b = lv.Bookmark.from_dict({"name": "x", "view": "lib:ext", "sort_by": "egal", "group": "tags"})
        self.assertEqual((b.sort_by, b.group), ("", "tags"))

    def test_export_import(self):
        s = GlobalSettings()
        lv.add_bookmark(s, lv.Bookmark(id="a", name="Alle RPG", view="all", search="rpg"))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bm.json"
            self.assertEqual(lv.export_bookmarks(s, f), 1)
            data = json.loads(f.read_text())
            data["bookmarks"].append({"name": "Neu", "view": "recent"})
            data["bookmarks"].append({"name": "kaputt", "view": "irgendwo"})
            f.write_text(json.dumps(data))
            self.assertEqual(lv.import_bookmarks(s, f), 1)            # Duplikat und Ungültiges fallen weg
            self.assertEqual([b.name for b in lv.bookmarks(s)], ["Alle RPG", "Neu"])
            f.write_text("kein json")
            with self.assertRaises(ValueError):
                lv.import_bookmarks(s, f)


class SettingsCompatTest(Paths):
    def test_new_fields_default_and_view_keys(self):
        s, g = GlobalSettings(), GameSettings()
        self.assertEqual((s.library_group, s.starting_page, s.when_closing, s.pause_downloads_when_playing,
                          s.bandwidth_limit, s.keep_logged_in, s.presence_status), ("none", "all", "exit", True, False, True,
                                                                                   "online"))
        self.assertEqual((g.tags, g.rating, g.auto_update, g.custom_tasks, g.dlc_excluded), ([], 0, True, [], []))
        v = s.view_settings()
        self.assertIn("group", v)
        self.assertIn("sort_by", v)


if __name__ == "__main__":
    unittest.main()
