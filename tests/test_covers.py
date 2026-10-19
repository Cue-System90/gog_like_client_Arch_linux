"""Strenger Titelabgleich für Cover externer Spiele: lieber kein Cover als ein falsches."""
import unittest

from galaxy_linux.api.covers import title_matches as m


class TitleMatchTest(unittest.TestCase):
    def test_matches(self):
        for q, c in [("Unreal", "Unreal Gold"), ("Anno 2070", "Anno 2070™"), ("Freelancer", "Freelancer"),
                     ("Soldier of Fortune", "Soldier of Fortune: Platinum Edition"), ("Need for Speed Shift", "Need for Speed: SHIFT"),
                     ("The Operative - No One Lives Forever", "The Operative: No One Lives Forever"),
                     ("Alan Wake", "Alan Wake Remastered")]:
            self.assertTrue(m(q, c), f"{q} ~ {c}")

    def test_rejects_other_games(self):
        for q, c in [("Soldier of Fortune", "Soldier of Fortune: Payback"), ("Soldier of Fortune", "Soldier of Fortune II: Double Helix"),
                     ("Emperor", "Emperor of the Fading Suns"), ("Emperor", "Emperor: Rise of the Middle Kingdom"),
                     ("Unreal", "Unreal Tournament 2004"), ("Anno 2070", "Anno 2070 - Deep Ocean"), ("Shift", "Shift Happens")]:
            self.assertFalse(m(q, c), f"{q} ~ {c}")


if __name__ == "__main__":
    unittest.main()
