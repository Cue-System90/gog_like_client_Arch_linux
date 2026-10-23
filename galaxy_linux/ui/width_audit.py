"""Mindestbreiten prüfen: welche Widgets halten das Fenster breiter als der Bildschirm?

Ein 55"-4K-Fernseher mit 225 % Skalierung hat nur rund 1706 × 960 logische Pixel; jede Ansicht soll bis 1280 × 800
passen. ``culprits`` folgt von einem Widget aus den Kindern, die die Mindestbreite bestimmen, bis zu der Stelle, an der
sich die Breite aus nebeneinander liegenden Teilen zusammensetzt (eine waagerechte Zeile) oder ein einzelnes Widget
selbst zu breit ist – das ist der Kandidat zum Umbrechen, Kürzen oder Ausblenden.

Die Logik arbeitet auf beliebigen Knoten (``children``/``min_width``/``name`` als Funktionen) und lässt sich ohne GTK
testen; ``gtk_culprits`` und ``gtk_min_width`` sind die Anbindung an GTK für den unsichtbaren Oberflächentest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

TARGETS = ((1280, 800), (1706, 960))          # kleinste unterstützte Fenstergröße; 4K-Fernseher mit 225 %


@dataclass
class Culprit:
    path: str                                   # „MainWindow › Box › GamePage › Box.actionbar“
    min_width: int
    budget: int                                 # so breit dürfte das Widget an dieser Stelle höchstens sein
    parts: list[tuple[str, int]] = field(default_factory=list)   # breiteste Teile einer Zeile (Name, Mindestbreite)

    @property
    def excess(self) -> int:
        return self.min_width - self.budget

    def text(self) -> str:
        s = f"{self.path}: min {self.min_width} px > {self.budget} px (+{self.excess})"
        if self.parts:
            s += " – Teile: " + ", ".join(f"{n} {w}" for n, w in self.parts)
        return s


def fits(min_width: int, target: int) -> bool:
    """Passt ein Fenster mit dieser Mindestbreite auf einen Bildschirm der Breite ``target``?"""
    return 0 <= min_width <= target


def culprits(node: Any, budget: int, *, children: Callable[[Any], Iterable[Any]], min_width: Callable[[Any], int],
             name: Callable[[Any], str], path: str = "", max_parts: int = 6, depth: int = 60) -> list[Culprit]:
    """Widgets unter ``node``, die zusammen eine Mindestbreite über ``budget`` erzwingen.

    * ``node`` passt → nichts.
    * Die Mindestbreite ist mindestens die Summe der Kinder (waagerechte Zeile) → ``node`` selbst mit seinen breitesten
      Teilen; hier hilft Umbrechen oder Umordnen.
    * Sonst bestimmt das breiteste Kind die Breite (senkrechte Box, Stapel, Rahmen …): weiter in jedes Kind, das für
      sich zu breit ist; der Rand dieses Containers wird vom Budget abgezogen.
    * Kein Kind schuld (Blatt oder nur Rand) → ``node``.
    """
    m = int(min_width(node))
    here = f"{path} › {name(node)}" if path else name(node)
    if m <= budget:
        return []
    kids = [(c, int(min_width(c))) for c in children(node)]
    kids = [(c, w) for c, w in kids if w > 0]
    parts = [(name(c), w) for c, w in sorted(kids, key=lambda cw: -cw[1])[:max_parts]]
    if not kids or depth <= 0:
        return [Culprit(here, m, budget, parts)]
    widest = max(w for _c, w in kids)
    total = sum(w for _c, w in kids)
    if len(kids) > 1 and total > widest and m >= total:
        return [Culprit(here, m, budget, parts)]
    margin = m - widest
    out: list[Culprit] = []
    for c, w in kids:
        if w > budget - margin:
            out += culprits(c, budget - margin, children=children, min_width=min_width, name=name, path=here,
                            max_parts=max_parts, depth=depth - 1)
    return out or [Culprit(here, m, budget, parts)]


# --------------------------------------------------------------------------- GTK
def gtk_children(widget: Any) -> list[Any]:
    """Sichtbare Kinder (unsichtbare tragen nichts zur Größe bei)."""
    out = []
    c = widget.get_first_child()
    while c is not None:
        if c.get_visible():
            out.append(c)
        c = c.get_next_sibling()
    return out


def gtk_min_width(widget: Any) -> int:
    from gi.repository import Gtk
    return int(widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0])


def gtk_name(widget: Any) -> str:
    css = [c for c in widget.get_css_classes() if c not in ("horizontal", "vertical")]
    n = type(widget).__name__ + ("." + ".".join(css[:2]) if css else "")
    if hasattr(widget, "get_label") and isinstance(widget, _label_type()):
        text = (widget.get_label() or "")[:24]
        if text:
            n += f"„{text}“"
    return n


def _label_type():
    from gi.repository import Gtk
    return Gtk.Label


def gtk_culprits(widget: Any, budget: int) -> list[Culprit]:
    return culprits(widget, budget, children=gtk_children, min_width=gtk_min_width, name=gtk_name)
