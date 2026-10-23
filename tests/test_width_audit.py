"""Mindestbreiten-Prüfung (ui/width_audit.py) ohne GTK: Knoten mit Kindern, Mindestbreite und Art der Anordnung."""
import unittest

from galaxy_linux.ui import width_audit as wa


class Node:
    """``row``: Mindestbreite = Summe der Kinder + Rand (waagerechte Box); sonst breitestes Kind + Rand."""

    def __init__(self, name, own=0, children=(), row=False, margin=0):
        self.name, self.own, self.kids, self.row, self.margin = name, own, list(children), row, margin

    @property
    def min(self):
        if not self.kids:
            return self.own
        widths = [k.min for k in self.kids]
        return (sum(widths) if self.row else max(widths)) + self.margin


def audit(node, budget):
    return wa.culprits(node, budget, children=lambda n: n.kids, min_width=lambda n: n.min, name=lambda n: n.name)


class WidthAuditTest(unittest.TestCase):
    def test_fits(self):
        self.assertTrue(wa.fits(1066, 1280))
        self.assertTrue(wa.fits(1280, 1280))
        self.assertFalse(wa.fits(1899, 1706))
        self.assertFalse(wa.fits(-1, 1280))
        self.assertEqual(wa.TARGETS, ((1280, 800), (1706, 960)))

    def test_nothing_when_it_fits(self):
        self.assertEqual(audit(Node("Fenster", children=[Node("a", 300), Node("b", 400)], row=True), 1280), [])

    def test_row_is_reported_with_its_widest_parts(self):
        bar = Node("Play-Leiste", children=[Node("Spielen", 162), Node("Kennzahlen", 717), Node("Menü", 44)], row=True,
                   margin=56)
        page = Node("Spielansicht", children=[Node("Hero", 0), bar], margin=0)
        win = Node("Fenster", children=[Node("Seitenleiste", 232), page], row=True)
        # Fenster selbst ist eine Zeile → es wird gemeldet; die Seite allein hat ein eigenes Budget
        self.assertEqual(audit(win, 1280), [])
        top = audit(win, 1100)
        self.assertEqual([c.path for c in top], ["Fenster"])
        self.assertEqual(top[0].parts[0], ("Spielansicht", 979))
        found = audit(page, 1048 - 200)
        self.assertEqual(len(found), 1)
        c = found[0]
        self.assertEqual(c.path, "Spielansicht › Play-Leiste")
        self.assertEqual((c.min_width, c.budget, c.excess), (979, 848, 131))
        self.assertEqual([n for n, _w in c.parts], ["Kennzahlen", "Spielen", "Menü"])
        self.assertIn("Kennzahlen 717", c.text())

    def test_follows_only_children_that_are_too_wide_and_subtracts_margins(self):
        wide = Node("Auswahlliste", 557)
        panel = Node("Kompatibilität", children=[Node("Überschrift", 205), wide, Node("Knöpfe", 300)], margin=28)
        right = Node("rechts", children=[panel, Node("Details", 363)])
        # rechts ist eine senkrechte Box: Mindestbreite = Panel (585); Budget 400 → das Panel und darin die Liste
        found = audit(right, 400)
        self.assertEqual([c.path for c in found], ["rechts › Kompatibilität › Auswahlliste"])
        self.assertEqual(found[0].budget, 372)             # 400 − 28 Rand des Panels

    def test_leaf_and_multiple_culprits(self):
        stack = Node("Stapel", children=[Node("Spiel", 1187), Node("Freunde", 780), Node("Store", 1100)])
        found = audit(stack, 1048)
        self.assertEqual([c.path for c in found], ["Stapel › Spiel", "Stapel › Store"])
        self.assertTrue(all(not c.parts for c in found))

    def test_margin_is_taken_from_the_budget_of_the_child(self):
        box = Node("Rahmen", children=[Node("Inhalt", 1000)], margin=100)
        found = audit(box, 1048)
        self.assertEqual([(c.path, c.budget) for c in found], [("Rahmen › Inhalt", 948)])

    def test_depth_limit_reports_where_it_stops(self):
        deep = Node("a", children=[Node("b", children=[Node("c", 2000)])])
        found = wa.culprits(deep, 1000, children=lambda n: n.kids, min_width=lambda n: n.min, name=lambda n: n.name,
                            depth=1)
        self.assertEqual([c.path for c in found], ["a › b"])

    def test_zero_width_children_are_ignored(self):
        row = Node("Zeile", children=[Node("unsichtbar", 0), Node("Text", 1200)], row=True)
        found = audit(row, 1048)
        self.assertEqual([c.path for c in found], ["Zeile › Text"])


if __name__ == "__main__":
    unittest.main()
