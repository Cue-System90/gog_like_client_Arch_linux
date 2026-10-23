"""„Aktuelle Übersicht“ (Recent) wie in GOG Galaxy 2.1: Widgets „Zuletzt gespielt“, „Meine Übersicht“ (lokale
Spielzeit heute, gestern, diese Woche, dieser Monat, dazu ein kleines Histogramm), „Neu in der Bibliothek“ und „Was als
Nächstes spielen“ (ein Vorschlag aus installierten, selten oder nie gespielten Spielen, „Überrasch mich“ und
„Neu mischen“). „Ansicht anpassen“ schaltet Widgets an und ab und ordnet sie (gespeichert in ``recent_widgets``).

Die Widgets kommen aus ``ui_registry.register_recent_widget`` – die eingebauten melden sich hier an, Freunde-Widgets
folgen später auf demselben Weg. Daten ohne GTK: ``galaxy_linux/recent.py``.
"""
from __future__ import annotations

from ..i18n import N_, tr

import logging
import time
from datetime import date

from .. import recent as rc
from ..settings import format_minutes
from . import ui_registry
from .game_menu import attach_context_menu
from .util import Gtk, clamped_picture, label, pick_icon, relative_time

log = logging.getLogger(__name__)

COVER = (136, 191)
BIG_COVER = (164, 231)
ROW_MAX = 12
NEW_DAYS = 30
HIST_HEIGHT = 72
WEEKDAYS = (N_("Wochentag\x04Mo"), N_("Wochentag\x04Di"), N_("Wochentag\x04Mi"), N_("Wochentag\x04Do"),
            N_("Wochentag\x04Fr"), N_("Wochentag\x04Sa"), N_("Wochentag\x04So"))


def fmt_minutes(minutes: int) -> str:
    return tr('{0} Min.').format(0) if minutes <= 0 else format_minutes(minutes)


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class RecentPage(Gtk.ScrolledWindow):
    def __init__(self, win) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win, self.backend = win, win.backend
        self.pick = ""                # gezeigter Vorschlag „Was als Nächstes spielen“
        self.pick_kind = "daily"      # daily | shuffle | surprise
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=18, margin_bottom=28, margin_start=28,
                      margin_end=28, valign=Gtk.Align.START)
        head = Gtk.Box(spacing=8)
        title = label(tr('Aktuelle Übersicht'), css=["page-title"])
        title.set_hexpand(True)
        head.append(title)
        self.customize = Gtk.MenuButton()
        cb = Gtk.Box(spacing=6)
        cb.append(Gtk.Image(icon_name=pick_icon("view-grid-symbolic", "view-app-grid-symbolic"), pixel_size=14))
        cb.append(label(tr('Ansicht anpassen')))
        self.customize.set_child(cb)
        self.customize.add_css_class("hdr-pill")
        self.customize.set_valign(Gtk.Align.CENTER)
        self.customize.set_create_popup_func(lambda b: b.set_popover(self._customize_popover()))
        head.append(self.customize)
        box.append(head)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        box.append(self.body)
        self.set_child(box)

    # ------------------------------------------------------------------ Aufbau
    def order(self) -> list[dict]:
        return rc.merge_order(self.backend.settings.recent_widgets, [w.key for w in ui_registry.recent_widgets()])

    def on_show(self) -> None:
        self.rebuild()
        self.get_vadjustment().set_value(0)

    def rebuild(self) -> None:
        _clear(self.body)
        reg = {w.key: w for w in ui_registry.recent_widgets()}
        shown = 0
        for key in rc.enabled_keys(self.order()):
            w = reg.get(key)
            content = ui_registry.build_recent_widget(w, self) if w is not None else None
            if content is None:
                continue
            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            panel.add_css_class("widget-panel")
            panel.append(label(tr(w.title).upper(), css=["sec-hdr"]))
            panel.append(content)
            self.body.append(panel)
            shown += 1
        if not shown:
            self.body.append(label(tr('Alle Widgets sind ausgeblendet – „Ansicht anpassen“ schaltet sie wieder ein.'),
                                   css=["dim"], wrap=True))
        self.mark_seen()

    def mark_seen(self) -> None:
        """Die Ansicht war offen: der Vorschlag des Tages gilt als gesehen – das „Neu“ in der Seitenleiste entfällt
        (auch wenn danach „Neu mischen“ einen anderen zeigt)."""
        s = self.backend.settings
        daily = rc.suggestion(self.backend, time.time())
        if daily is not None and s.recent_next_seen != daily.id:
            s.recent_next_seen = daily.id
            try:
                s.save()
            except OSError as exc:  # pragma: no cover
                log.warning("Einstellung nicht gespeichert: %s", exc)
        self.win.update_overview_badge()

    # ------------------------------------------------------------------ Ansicht anpassen
    def _customize_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10, margin_bottom=10, margin_start=12,
                      margin_end=12)
        self._fill_customize(box)
        pop.set_child(box)
        return pop

    def _fill_customize(self, box: Gtk.Box) -> None:
        _clear(box)
        box.append(label(tr('WIDGETS'), css=["sec-hdr"]))
        box.append(label(tr('An- und abschalten, mit den Pfeilen anordnen'), css=["dim", "caption"]))
        titles = {w.key: tr(w.title) for w in ui_registry.recent_widgets()}
        order = self.order()
        for i, item in enumerate(order):
            row = Gtk.Box(spacing=4)
            chk = Gtk.CheckButton(label=titles.get(item["key"], item["key"]), active=item["enabled"], hexpand=True)
            chk.connect("toggled", lambda b, k=item["key"]: self._change(box, rc.set_enabled(self.order(), k, b.get_active())))
            row.append(chk)
            for icon, delta, tip, ok in (("go-up-symbolic", -1, tr('Nach oben'), i > 0),
                                         ("go-down-symbolic", 1, tr('Nach unten'), i < len(order) - 1)):
                b = Gtk.Button(icon_name=pick_icon(icon), tooltip_text=tip)
                b.add_css_class("flat")
                b.set_sensitive(ok)
                b.connect("clicked", lambda _b, k=item["key"], d=delta: self._change(box, rc.move(self.order(), k, d)))
                row.append(b)
            box.append(row)

    def _change(self, box: Gtk.Box, order: list[dict]) -> None:
        s = self.backend.settings
        s.recent_widgets = order
        s.save()
        self.rebuild()
        self._fill_customize(box)

    # ------------------------------------------------------------------ Bausteine
    def cover_card(self, pid: str, title: str, caption: str = "", size: tuple[int, int] = COVER,
                   show_title: bool = True) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("discover-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("cover-card")
        frame, pic = clamped_picture(size[0], size[1], ["cover"], placeholder=title)
        box.append(frame)
        if show_title:
            t = label(title, css=["cover-title"], ellipsize=True)
            t.set_size_request(size[0], -1)
            t.set_max_width_chars(1)
            box.append(t)
        if caption:
            c = label(caption, css=["dim", "caption"], ellipsize=True)
            c.set_size_request(size[0], -1)
            c.set_max_width_chars(1)
            box.append(c)
        btn.set_child(box)
        btn.set_tooltip_text(title)
        btn.connect("clicked", lambda *_: self.win.show_game(pid))
        attach_context_menu(self.win, btn, lambda: pid)
        entry = self.backend.entry(pid)
        if entry is not None:
            self.win.load_cover(entry, pic)
        return btn

    @staticmethod
    def cover_row(cards: list[Gtk.Widget]) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        row = Gtk.Box(spacing=18, margin_bottom=8)
        for c in cards:
            row.append(c)
        scroll.set_child(row)
        return scroll

    def visible(self, pid: str) -> bool:
        g = self.backend.registry.get(pid)
        if g is not None and g.duplicate_of:
            return False
        return not self.backend.game_settings.get(pid).hidden or self.backend.settings.show_hidden


# --------------------------------------------------------------------------- Widgets
def recently_played(page: RecentPage):
    b = page.backend
    ids = {e.id for e in b.visible_library()} | {g.product_id for g in b.registry.all()}
    played = [(pid, *b.playtime.get(pid)) for pid in ids]
    played = [p for p in played if p[2] > 0 and page.visible(p[0])]
    played.sort(key=lambda p: -p[2])
    cards = []
    for pid, minutes, last in played[:ROW_MAX]:
        e = b.entry(pid)
        if e is not None:
            cards.append(page.cover_card(pid, e.title, f"{relative_time(last)} · {fmt_minutes(minutes)}"))
    if not cards:
        return label(tr('Noch nichts gespielt – gestartete Spiele erscheinen hier.'), css=["dim"], wrap=True)
    return page.cover_row(cards)


def my_summary(page: RecentPage):
    now = time.time()
    sessions = [(s, e) for _pid, s, e in page.backend.playtime.sessions(since=now - 62 * 86400)]
    sm = rc.summarize(sessions, now)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    stats = Gtk.Box(spacing=36)
    for key, minutes in ((tr('HEUTE'), sm.today), (tr('GESTERN'), sm.yesterday), (tr('DIESE WOCHE'), sm.week),
                         (tr('DIESER MONAT'), sm.month)):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.append(label(key, css=["stat-key"]))
        col.append(label(fmt_minutes(minutes), css=["stat-val"]))
        stats.append(col)
    box.append(stats)
    box.append(histogram(sm))
    box.append(label(tr('Spielzeit je Tag in den letzten 14 Tagen – gezählt werden Spiele, die galaxy-linux startet.'),
                     css=["dim", "caption"], wrap=True))
    return box


def histogram(sm: rc.Summary) -> Gtk.Widget:
    """Kleines Säulendiagramm: eine Säule je Tag, heute hervorgehoben; Tooltip mit Datum und Spielzeit."""
    grid = Gtk.Box(spacing=6, homogeneous=True)
    grid.add_css_class("histogram")
    peak = max(sm.peak, 1)
    today = date.today()
    for day, minutes in sm.days:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        area.set_size_request(-1, HIST_HEIGHT)
        area.append(Gtk.Box(vexpand=True))
        bar = Gtk.Box()
        bar.set_size_request(-1, max(2, int(HIST_HEIGHT * minutes / peak)) if minutes else 2)
        bar.add_css_class("hist-bar")
        if minutes <= 0:
            bar.add_css_class("empty")
        if day == today:
            bar.add_css_class("today")
        area.append(bar)
        col.append(area)
        col.append(label(tr(WEEKDAYS[day.weekday()]), css=["dim", "caption"], xalign=0.5))
        col.set_tooltip_text(f"{tr(WEEKDAYS[day.weekday()])} {day.day}.{day.month}.: {fmt_minutes(minutes)}")
        grid.append(col)
    return grid


def new_in_library(page: RecentPage):
    b = page.backend
    now = time.time()
    limit = now - NEW_DAYS * 86400
    items: dict[str, tuple[float, str]] = {}
    for pid, ts in (b.settings.library_new_ids or {}).items():
        if isinstance(ts, (int, float)) and ts >= limit:
            items[str(pid)] = (float(ts), tr('Hinzugefügt {0}').format(relative_time(float(ts))))
    for g in b.registry.all():
        if g.installed_at >= limit and (g.product_id not in items or items[g.product_id][0] < g.installed_at):
            items[g.product_id] = (float(g.installed_at), tr('Installiert {0}').format(relative_time(g.installed_at)))
    ordered = sorted(items.items(), key=lambda kv: -kv[1][0])
    cards = []
    for pid, (_ts, caption) in ordered:
        if len(cards) >= ROW_MAX or not page.visible(pid):
            continue
        e = b.entry(pid)
        if e is not None:
            cards.append(page.cover_card(pid, e.title, caption))
    if not cards:
        return label(tr('In den letzten 30 Tagen ist nichts Neues dazugekommen.'), css=["dim"], wrap=True)
    return page.cover_row(cards)


def next_to_play(page: RecentPage):
    b = page.backend
    now = time.time()
    day = rc.day_key(now)
    cands = rc.candidates(rc.installed_candidates(b))
    pool = rc.library_pool(b)
    by_id = {c.id: c for c in pool}
    by_id.update({c.id: c for c in cands})
    if page.pick not in by_id:
        first = rc.daily_pick(cands, day)
        page.pick, page.pick_kind = (first.id, "daily") if first else ("", "daily")
    cur = by_id.get(page.pick)

    def shuffle() -> None:
        nxt = rc.reshuffle(cands, page.pick, day)
        if nxt is not None:
            page.pick, page.pick_kind = nxt.id, "shuffle"
            page.rebuild()

    def surprise() -> None:
        nxt = rc.surprise(pool, exclude=page.pick)
        if nxt is not None:
            page.pick, page.pick_kind = nxt.id, "surprise"
            page.rebuild()

    box = Gtk.Box(spacing=22)
    buttons = Gtk.Box(spacing=8)
    surprise_btn = Gtk.Button(label=tr('Überrasch mich'))
    surprise_btn.add_css_class("btn-secondary")
    surprise_btn.set_sensitive(bool(pool))
    surprise_btn.connect("clicked", lambda *_: surprise())
    shuffle_btn = Gtk.Button()
    sb = Gtk.Box(spacing=6)
    sb.append(Gtk.Image(icon_name=pick_icon("media-playlist-shuffle-symbolic", "view-refresh-symbolic")))
    sb.append(label(tr('Neu mischen')))
    shuffle_btn.set_child(sb)
    shuffle_btn.add_css_class("btn-secondary")
    shuffle_btn.set_sensitive(len(cands) > 1 or (bool(cands) and page.pick_kind == "surprise"))
    shuffle_btn.connect("clicked", lambda *_: shuffle())
    if cur is None:
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        text.append(label(tr('Keine Vorschläge: alle installierten Spiele sind schon ausgiebig gespielt. Installiere '
                             'ein neues Spiel oder lass dich überraschen.'), css=["dim"], wrap=True))
        buttons.append(surprise_btn)
        text.append(buttons)
        box.append(text)
        return box
    box.append(page.cover_card(cur.id, cur.title, size=BIG_COVER, show_title=False))
    info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, valign=Gtk.Align.CENTER)
    info.append(label(cur.title, css=["next-title"], wrap=True))
    if cur.minutes <= 0:
        reason = tr('Noch nicht gespielt')
    else:
        reason = tr('Erst {0} gespielt').format(format_minutes(cur.minutes))
    if page.pick_kind == "surprise":
        reason = tr('Zufällig ausgewählt') + " · " + reason
    info.append(label(reason, css=["dim"]))
    installed = b.registry.is_installed(cur.id)
    main = Gtk.Button(label=tr('SPIELEN') if installed else tr('SPIEL ANZEIGEN'))
    main.add_css_class("btn-play")
    main.set_halign(Gtk.Align.START)
    main.set_margin_top(6)
    main.connect("clicked", lambda *_: page.win.page.play(cur.id) if installed else page.win.show_game(cur.id))
    info.append(main)
    buttons.append(surprise_btn)
    buttons.append(shuffle_btn)
    info.append(buttons)
    box.append(info)
    return box


ui_registry.register_recent_widget("recently_played", N_("Zuletzt gespielt"), recently_played, order=10)
ui_registry.register_recent_widget("summary", N_("Meine Übersicht"), my_summary, order=20)
ui_registry.register_recent_widget("new_in_library", N_("Neu in der Bibliothek"), new_in_library, order=30)
ui_registry.register_recent_widget("next_to_play", N_("Was als Nächstes spielen"), next_to_play, order=40)
