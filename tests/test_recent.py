"""Aktuelle Übersicht: Widget-Reihenfolge (auch gespeichert), Spielzeit-Statistik „Meine Übersicht“ mit Sitzungen
über Mitternacht, Vorschlag „Was als Nächstes spielen“ (Tagesvorschlag, Neu mischen, Überrasch mich, „Neu“) und die
Sitzungen im Spielzeit-Speicher – ohne GTK."""
from __future__ import annotations

import random
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from galaxy_linux import recent as rc
from galaxy_linux.settings import GlobalSettings, PlaytimeStore

UTC = timezone.utc


def ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


class WidgetOrderTest(unittest.TestCase):
    def test_merge(self):
        avail = ["a", "b", "c"]
        self.assertEqual(rc.merge_order([], avail), [{"key": "a", "enabled": True}, {"key": "b", "enabled": True},
                                                     {"key": "c", "enabled": True}])
        saved = [{"key": "c", "enabled": False}, {"key": "weg", "enabled": True}, {"key": "a"}, {"key": "c"}, "x"]
        self.assertEqual(rc.merge_order(saved, avail), [{"key": "c", "enabled": False}, {"key": "a", "enabled": True},
                                                        {"key": "b", "enabled": True}])
        self.assertEqual(rc.merge_order(None, ["a"]), [{"key": "a", "enabled": True}])

    def test_move_and_toggle(self):
        order = rc.merge_order([], ["a", "b", "c"])
        self.assertEqual([i["key"] for i in rc.move(order, "c", -1)], ["a", "c", "b"])
        self.assertEqual([i["key"] for i in rc.move(order, "a", -1)], ["a", "b", "c"])     # schon oben
        self.assertEqual([i["key"] for i in rc.move(order, "a", 5)], ["b", "c", "a"])
        self.assertEqual(rc.move(order, "zz", 1), order)
        off = rc.set_enabled(order, "b", False)
        self.assertEqual(rc.enabled_keys(off), ["a", "c"])
        self.assertEqual(rc.enabled_keys(order), ["a", "b", "c"])     # Original unverändert

    def test_persisted_in_settings(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "settings.json"
            with mock.patch("galaxy_linux.paths.SETTINGS_FILE", cfg):
                s = GlobalSettings.load()
                order = rc.move(rc.merge_order(s.recent_widgets, rc.BUILTIN_WIDGETS), "next_to_play", -3)
                s.recent_widgets = rc.set_enabled(order, "summary", False)
                s.save()
                again = GlobalSettings.load()
                merged = rc.merge_order(again.recent_widgets, rc.BUILTIN_WIDGETS + ("friends_activity",))
        self.assertEqual([i["key"] for i in merged], ["next_to_play", "recently_played", "summary", "new_in_library",
                                                      "friends_activity"])
        self.assertEqual(rc.enabled_keys(merged), ["next_to_play", "recently_played", "new_in_library",
                                                   "friends_activity"])


class SummaryTest(unittest.TestCase):
    def test_split_over_midnight(self):
        parts = rc.split_by_day(ts(2026, 10, 22, 23, 30), ts(2026, 10, 23, 1, 0), UTC)
        self.assertEqual(parts, {date(2026, 10, 22): 30.0, date(2026, 10, 23): 60.0})
        self.assertEqual(rc.split_by_day(10, 5, UTC), {})

    def test_summary_periods(self):
        now = ts(2026, 10, 23, 12, 0)          # Freitag
        sessions = [
            (ts(2026, 10, 23, 9, 0), ts(2026, 10, 23, 10, 30)),     # heute 90
            (ts(2026, 10, 22, 23, 30), ts(2026, 10, 23, 0, 15)),    # gestern 30, heute 15
            (ts(2026, 10, 19, 20, 0), ts(2026, 10, 19, 21, 0)),     # Montag dieser Woche 60
            (ts(2026, 10, 18, 20, 0), ts(2026, 10, 18, 22, 0)),     # Sonntag letzter Woche 120
            (ts(2026, 10, 1, 10, 0), ts(2026, 10, 1, 10, 45)),      # dieser Monat 45
            (ts(2026, 9, 30, 10, 0), ts(2026, 9, 30, 11, 0)),       # letzter Monat
        ]
        s = rc.summarize(sessions, now, UTC)
        self.assertEqual(s.today, 105)
        self.assertEqual(s.yesterday, 30)
        self.assertEqual(s.week, 105 + 30 + 60)
        self.assertEqual(s.month, 105 + 30 + 60 + 120 + 45)
        self.assertEqual(len(s.days), rc.HISTOGRAM_DAYS)
        self.assertEqual(s.days[-1], (date(2026, 10, 23), 105))
        self.assertEqual(s.days[0][0], date(2026, 10, 10))
        self.assertEqual(dict(s.days)[date(2026, 10, 18)], 120)
        self.assertEqual(s.peak, 120)
        empty = rc.summarize([], now, UTC)
        self.assertEqual((empty.today, empty.week, empty.month, empty.peak), (0, 0, 0, 0))

    def test_week_starts_monday(self):
        monday = ts(2026, 10, 19, 8, 0)
        s = rc.summarize([(ts(2026, 10, 18, 10, 0), ts(2026, 10, 18, 11, 0))], monday, UTC)
        self.assertEqual((s.today, s.yesterday, s.week), (0, 60, 0))

    def test_playtime_store_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("galaxy_linux.paths.PLAYTIME_FILE", Path(d) / "playtime.json"):
                st = PlaytimeStore()
                st.add_session("1", 30, ended_at=10_000.0, started_at=8_200.0)
                st.add_session("1", 10, ended_at=20_000.0)                 # Start aus der Dauer
                st.add_session("2", 0, ended_at=30_000.0)                  # 0 Minuten: keine Sitzung
                again = PlaytimeStore()
                self.assertEqual(again.get("1"), (40, 20_000.0))
                self.assertEqual(sorted(again.sessions()), [("1", 8200.0, 10000.0), ("1", 19400.0, 20000.0)])
                self.assertEqual(again.sessions(since=15_000), [("1", 19400.0, 20000.0)])
                # alte Sitzungen fallen nach SESSION_KEEP_DAYS heraus
                late = 20_000.0 + 401 * 86400
                again.add_session("1", 5, ended_at=late)
                self.assertEqual([s[2] for s in again.sessions()], [late])


class NextToPlayTest(unittest.TestCase):
    def items(self):
        return [rc.Candidate("1", "Viel gespielt", minutes=900), rc.Candidate("2", "Nie A", installed_at=10),
                rc.Candidate("3", "Selten", minutes=30), rc.Candidate("4", "Nie B", installed_at=20),
                rc.Candidate("5", "Grenze", minutes=rc.RARELY_MINUTES)]

    def test_candidates(self):
        self.assertEqual([c.id for c in rc.candidates(self.items())], ["4", "2", "3"])

    def test_daily_pick_stable_and_prefers_never_played(self):
        cands = rc.candidates(self.items())
        pick = rc.daily_pick(cands, "2026-10-23")
        self.assertIn(pick.id, ("2", "4"))
        self.assertEqual(rc.daily_pick(list(reversed(cands)), "2026-10-23").id, pick.id)
        picks = {rc.daily_pick(cands, f"2026-10-{d:02d}").id for d in range(1, 29)}
        self.assertEqual(picks, {"2", "4"})              # über die Tage wechselt der Vorschlag
        self.assertIsNone(rc.daily_pick([], "2026-10-23"))

    def test_reshuffle_cycles(self):
        cands = rc.candidates(self.items())
        day = "2026-10-23"
        first = rc.daily_pick(cands, day).id
        seen = [first]
        cur = first
        for _ in range(len(cands) - 1):
            cur = rc.reshuffle(cands, cur, day).id
            seen.append(cur)
        self.assertEqual(sorted(seen), ["2", "3", "4"])
        self.assertEqual(rc.reshuffle(cands, cur, day).id, first)          # ringsum
        self.assertEqual(rc.reshuffle(cands, "weg", day).id, first)
        self.assertIsNone(rc.reshuffle([], "1", day))
        self.assertEqual(rc.reshuffle(cands[:1], cands[0].id, day).id, cands[0].id)

    def test_surprise(self):
        pool = self.items()
        rng = random.Random(1)
        for _ in range(20):
            self.assertNotEqual(rc.surprise(pool, exclude="1", rng=rng).id, "1")
        self.assertEqual(rc.surprise(pool[:1], exclude="1").id, "1")
        self.assertIsNone(rc.surprise([]))

    def test_new_badge(self):
        self.assertTrue(rc.is_new_suggestion("4", ""))
        self.assertTrue(rc.is_new_suggestion("4", "2"))
        self.assertFalse(rc.is_new_suggestion("4", "4"))
        self.assertFalse(rc.is_new_suggestion("", "4"))
        self.assertEqual(rc.day_key(ts(2026, 10, 23, 23, 59), UTC), "2026-10-23")


if __name__ == "__main__":
    unittest.main()
