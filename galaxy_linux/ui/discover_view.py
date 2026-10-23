"""„Entdecken“ wie in GOG Galaxy 2.1: native Seite mit waagerechten Regalen aus dem GOG-Katalog – „Spiele für dich“,
„Neuerscheinungen“, „Angebote“, „Aus deiner Wunschliste“, „Zusätzliche Inhalte für deine Spiele“, „Good Old Games“.

Karten mit Cover, Titel, Preis und Rabatt; ein Klick öffnet die Produktseite im eingebetteten Store, Spiele im Besitz
öffnen ihre Spielansicht. Jedes Regal lädt für sich im Hintergrund (die Oberfläche wartet nie), leere Regale
entfallen; ohne Verbindung erscheint ein ruhiger Hinweis mit „Erneut versuchen“. Daten: ``api/discover.py``.
"""
from __future__ import annotations

from ..i18n import tr

import logging
import time

from .. import i18n
from ..api import discover as dc
from ..api.prices import format_amount, format_discount
from .util import Gtk, clamped_picture, label, pick_icon, run_async

log = logging.getLogger(__name__)

CARD_W, CARD_H = 150, 211          # Hochkant-Cover wie die Bibliothek (Seitenverhältnis der GOG-Boxart)


def price_widgets(product: dc.Product, owned: bool, lang: str) -> list[tuple[str, str]]:
    """(Text, CSS-Klasse) für die Preiszeile einer Karte."""
    if owned:
        return [(tr('IM BESITZ'), "price-owned")]
    p = product.price
    if p is None:
        if product.state == "coming-soon" or not product.price_text:
            return [(tr('Demnächst'), "price-base-plain")]
        return [(product.price_text, "price-final")]
    if p.free:
        return [(tr('Kostenlos'), "price-final")]
    out = []
    if p.discount:
        out.append((format_discount(p.discount), "price-discount"))
    out.append((format_amount(p.final, p.currency, lang), "price-final"))
    if p.discount:
        out.append((format_amount(p.base, p.currency, lang), "price-base"))
    return out


class ProductCard(Gtk.Button):
    def __init__(self, page: "DiscoverPage", product: dc.Product, owned: bool) -> None:
        super().__init__()
        self.page, self.product, self.owned = page, product, owned
        self.add_css_class("flat")
        self.add_css_class("discover-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("cover-card")
        self.frame, self.pic = clamped_picture(CARD_W, CARD_H, ["cover"], placeholder=product.title)
        box.append(self.frame)
        title = label(product.title, css=["cover-title"], ellipsize=True)
        title.set_size_request(CARD_W, -1)
        title.set_max_width_chars(1)
        box.append(title)
        prices = Gtk.Box(spacing=6)
        for text, css in price_widgets(product, owned, page.lang):
            prices.append(label(text, css=[css]))
        box.append(prices)
        self.set_child(box)
        self.set_tooltip_text(product.title)
        self.connect("clicked", lambda *_: page.open_product(self.product))
        self._load_cover()

    def _load_cover(self) -> None:
        images = self.page.win.images
        if self.product.cover:
            images.load(self.product.cover, self.pic.set_paintable)
            return
        pid, fallback = self.product.id, self.product.image

        def got(url: str) -> None:
            if url:
                images.load(url, self.pic.set_paintable)
            elif fallback:
                images.load(fallback, self.pic.set_paintable)
        # Wunschliste: Hochkant-Cover aus der GamesDB (wie die Bibliothek), sonst das Querformat-Bild
        run_async(lambda: self.page.win.backend.cover_url(pid), got, lambda _e: got(""))


class ShelfRow(Gtk.Box):
    """Ein Regal: Überschrift, „Alle anzeigen“, Pfeile, darunter die Karten in einer waagerechten Leiste."""

    def __init__(self, page: "DiscoverPage", shelf: dc.Shelf) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.page, self.shelf = page, shelf
        head = Gtk.Box(spacing=6)
        title = label(shelf.label, css=["shelf-title"])
        title.set_hexpand(True)
        head.append(title)
        if shelf.more:
            more = Gtk.Button(label=tr('Alle anzeigen'))
            more.add_css_class("flat")
            more.add_css_class("footer-link")
            more.set_valign(Gtk.Align.CENTER)
            more.connect("clicked", lambda *_: page.win.open_store(shelf.more))
            head.append(more)
        self.left = Gtk.Button(icon_name=pick_icon("go-previous-symbolic"), tooltip_text=tr('Zurück'))
        self.right = Gtk.Button(icon_name=pick_icon("go-next-symbolic"), tooltip_text=tr('Weiter'))
        for b, d in ((self.left, -1), (self.right, 1)):
            b.add_css_class("flat")
            b.add_css_class("hdr-btn")
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, d=d: self.page_by(d))
            head.append(b)
        self.append(head)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.START)
        self.spinner.set_size_request(24, 24)
        self.append(self.spinner)
        self.scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                         propagate_natural_height=True)
        self.scroll.add_css_class("shelf-scroll")
        self.cards = Gtk.Box(spacing=18, margin_bottom=10)
        self.scroll.set_child(self.cards)
        self.append(self.scroll)
        adj = self.scroll.get_hadjustment()
        for sig in ("value-changed", "changed"):
            adj.connect(sig, lambda *_: self._arrows())
        self.loading()

    def loading(self) -> None:
        self.set_visible(True)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.scroll.set_visible(False)
        self._arrows()

    def set_items(self, items: list[dc.Product], owned: set[str]) -> None:
        self.spinner.stop()
        self.spinner.set_visible(False)
        child = self.cards.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.cards.remove(child)
            child = nxt
        for p in items:
            self.cards.append(ProductCard(self.page, p, p.id in owned))
        self.scroll.get_hadjustment().set_value(0)
        self.scroll.set_visible(bool(items))
        self.set_visible(bool(items))

    def hide_shelf(self) -> None:
        self.spinner.stop()
        self.set_visible(False)

    def page_by(self, direction: int) -> None:
        adj = self.scroll.get_hadjustment()
        step = max(adj.get_page_size() - CARD_W, CARD_W)
        adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), adj.get_value() + direction * step)))

    def _arrows(self) -> None:
        adj = self.scroll.get_hadjustment()
        scrollable = self.scroll.get_visible() and adj.get_upper() - adj.get_page_size() > 1
        self.left.set_sensitive(scrollable and adj.get_value() > adj.get_lower() + 1)
        self.right.set_sensitive(scrollable and adj.get_value() < adj.get_upper() - adj.get_page_size() - 1)


class DiscoverPage(Gtk.ScrolledWindow):
    def __init__(self, win) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.service: dc.DiscoverService = win.discover_service
        self.lang = i18n.current()
        self._serial = 0
        self._loaded_at = 0.0
        self._login = None
        self._failed: set[str] = set()
        self._pending: set[str] = set()
        self._raw: dict[str, list] = {}           # geladene Regale (vor dem Entfernen von Doppelten)
        self._shown: dict[str, list[str]] = {}    # gezeigte IDs je Regal
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26, margin_top=18, margin_bottom=28, margin_start=28,
                      margin_end=28, valign=Gtk.Align.START)
        head = Gtk.Box(spacing=8)
        title = label(tr('Entdecken'), css=["page-title"])
        title.set_hexpand(True)
        head.append(title)
        reload = Gtk.Button(icon_name=pick_icon("view-refresh-symbolic"), tooltip_text=tr('Neu laden'))
        reload.add_css_class("flat")
        reload.add_css_class("hdr-btn")
        reload.set_valign(Gtk.Align.CENTER)
        reload.connect("clicked", lambda *_: self.refresh(force=True))
        head.append(reload)
        box.append(head)
        # ohne Verbindung: ruhiger Hinweis statt Fehlermeldungen
        self.offline = Gtk.Box(spacing=12)
        self.offline.add_css_class("panel")
        self.offline.append(Gtk.Image(icon_name=pick_icon("network-offline-symbolic", "network-wireless-offline-symbolic"),
                                      pixel_size=20, valign=Gtk.Align.CENTER))
        off_text = label(tr('Der GOG-Store ist gerade nicht erreichbar. Entdecken zeigt die Regale wieder, sobald eine Verbindung besteht.'),
                         css=["dim"], wrap=True)
        off_text.set_hexpand(True)
        self.offline.append(off_text)
        retry = Gtk.Button(label=tr('Erneut versuchen'))
        retry.add_css_class("hdr-pill")
        retry.set_valign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self.refresh(force=True))
        self.offline.append(retry)
        self.offline.set_visible(False)
        box.append(self.offline)
        self.shelves: dict[str, ShelfRow] = {}
        for sh in dc.SHELVES:
            row = ShelfRow(self, sh)
            self.shelves[sh.key] = row
            box.append(row)
        self.set_child(box)

    # ------------------------------------------------------------------ Laden
    def on_show(self) -> None:
        """Beim Öffnen: laden, wenn noch nie geladen, der Stand älter als der Zwischenspeicher ist oder sich die
        Anmeldung geändert hat."""
        login = self.win.backend.api.logged_in
        if (not self._loaded_at or time.monotonic() - self._loaded_at > dc.CACHE_TTL or login != self._login
                or self._failed):
            self.refresh()

    def refresh(self, force: bool = False) -> None:
        self._serial += 1
        serial = self._serial
        self._loaded_at = time.monotonic()
        self._login = self.win.backend.api.logged_in
        self._failed.clear()
        self._raw.clear()
        self._shown.clear()
        self._pending = {sh.key for sh in dc.SHELVES}
        self.offline.set_visible(False)
        lang = self.lang
        for sh in dc.SHELVES:
            row = self.shelves[sh.key]
            if sh.login and not self._login:
                row.hide_shelf()
                self._pending.discard(sh.key)
                continue
            row.loading()
            run_async(lambda k=sh.key: self.service.shelf(k, lang, force=force),
                      lambda items, k=sh.key: self._done(serial, k, items),
                      lambda exc, k=sh.key: self._fail(serial, k, exc))

    def owned_ids(self) -> set[str]:
        b = self.win.backend
        ids = {e.id for e in b.library}
        ids.update(g.product_id for g in b.registry.all() if g.method != "external")
        return ids

    def _done(self, serial: int, key: str, items: list) -> None:
        if serial != self._serial:
            return
        self._pending.discard(key)
        self._raw[key] = list(items)
        self._render()
        self._check_offline()

    def _render(self) -> None:
        """Regale ohne Doppelte zeigen; nur Regale, deren Inhalt sich dadurch ändert, werden neu aufgebaut."""
        owned = self.owned_ids()
        for key, items in dc.dedupe_shelves(self._raw, [sh.key for sh in dc.SHELVES]).items():
            ids = [p.id for p in items]
            if self._shown.get(key) != ids:
                self._shown[key] = ids
                self.shelves[key].set_items(items, owned)

    def _fail(self, serial: int, key: str, exc: Exception) -> None:
        if serial != self._serial:
            return
        log.info("Entdecken: Regal %s nicht geladen: %s", key, exc)
        self._pending.discard(key)
        self._failed.add(key)
        self.shelves[key].hide_shelf()
        self._check_offline()

    def _check_offline(self) -> None:
        """Hinweis nur, wenn kein Regal etwas zeigen kann und mindestens eines am Netz scheiterte."""
        if self._pending:
            return
        shown = any(r.get_visible() for r in self.shelves.values())
        self.offline.set_visible(bool(self._failed) and not shown)

    # ------------------------------------------------------------------ Klick
    def open_product(self, product: dc.Product) -> None:
        if product.id in self.owned_ids():
            self.win.show_game(product.id)
        else:
            self.win.open_store(product.store_path)
