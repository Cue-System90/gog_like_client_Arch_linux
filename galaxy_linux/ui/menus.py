"""Menüs wie in GOG Galaxy 2.1: Zahnrad oben links (Hauptmenü) und Avatar-Menü, dazu App-Aktionen und Tastenkürzel.

Zahnrad (Reihenfolge wie im Original): Spiele & Freunde hinzufügen ▸ · Bibliothek aktualisieren | Einstellungen ·
Hilfe erhalten | Neu bei uns | Nach Updates suchen | Rechtliches und Datenschutz · Beenden.
Avatar: Kopf mit Bild und Name | Kontoverwaltung · Bisherige Bestellungen · Geldbeutel | Ausloggen
(abgemeldet nur „Bei GOG anmelden“).
"""
from __future__ import annotations

from ..i18n import tr

import logging

from .. import APP_NAME, __version__, clientinfo
from .util import Adw, Gio, GLib, Gtk, label, open_uri, run_async

log = logging.getLogger(__name__)

def install_actions(app) -> None:
    """App-Aktionen (``app.<name>``) und Tastenkürzel anlegen; alle wirken auf das Hauptfenster."""
    def w():
        return app.window

    actions = (
        ("refresh", lambda: w() and w().request_library_refresh("manual", force=True), ["F5", "<primary>r"]),
        ("check-updates", lambda: w() and w().check_updates_async(), []),
        ("preferences", lambda: w() and w().open_preferences(), ["<primary>comma"]),
        ("add-game", lambda: w() and w().add_game(w()._view_lib_id()), []),
        ("redeem", lambda: w() and w().open_store("/redeem"), []),
        ("scan-folders", lambda: w() and w().scan_folders_async(quiet=False), []),
        ("create-library", lambda: w() and w().create_library(), []),
        ("help", lambda: open_uri(clientinfo.ISSUES_URL), []),
        ("whats-new", lambda: w() and show_whats_new(w()), []),
        ("check-client-update", lambda: w() and check_client_update(w()), []),
        ("legal", lambda: w() and show_legal(w()), []),
        ("account", lambda: w() and w().open_store("/account/settings"), []),
        ("orders", lambda: w() and w().open_store("/account/settings/orders"), []),
        ("wallet", lambda: w() and w().open_store("/wallet"), []),
        ("login", lambda: w() and w().do_login(), []),
        ("logout", lambda: w() and confirm_logout(w()), []),
        ("about", lambda: app._about(), []),
        ("quit", lambda: w().request_quit() if w() else app.quit(), ["<primary>q"]),
    )
    for name, cb, accels in actions:
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_a, cb=cb: cb())
        app.add_action(a)
        if accels:
            app.set_accels_for_action(f"app.{name}", accels)


def main_menu_model() -> Gio.Menu:
    """Zahnrad-Menü; Abschnitte erscheinen mit Trennlinien."""
    menu = Gio.Menu()
    add = Gio.Menu()
    games = Gio.Menu()
    games.append(tr('Spiel manuell hinzufügen'), "app.add-game")
    games.append(tr('GOG-Code einlösen'), "app.redeem")
    games.append(tr('Nach GOG-Spielen durchsuchen'), "app.scan-folders")
    add.append_section(None, games)
    libs = Gio.Menu()
    libs.append(tr('Bibliothek anlegen …'), "app.create-library")
    add.append_section(None, libs)
    top = Gio.Menu()
    top.append_submenu(tr('Spiele & Freunde hinzufügen'), add)
    top.append(tr('Bibliothek aktualisieren'), "app.refresh")
    menu.append_section(None, top)
    for items in (((tr('Einstellungen'), "app.preferences"), (tr('Hilfe erhalten'), "app.help")),
                  ((tr('Neu bei uns'), "app.whats-new"),),
                  ((tr('Nach Updates suchen'), "app.check-client-update"),),
                  ((tr('Rechtliches und Datenschutz'), "app.legal"), (tr('Beenden'), "app.quit"))):
        sec = Gio.Menu()
        for text, action in items:
            sec.append(text, action)
        menu.append_section(None, sec)
    return menu


def avatar_popover(win) -> Gtk.PopoverMenu:
    """Avatar-Menü passend zum Anmeldestand (wird nach Login/Logout neu gebaut)."""
    menu = Gio.Menu()
    api = win.backend.api
    win.avatar_header = None
    if not api.logged_in:
        sec = Gio.Menu()
        sec.append(tr('Bei GOG anmelden'), "app.login")
        menu.append_section(None, sec)
        return Gtk.PopoverMenu.new_from_model(menu)
    head = Gio.Menu()
    item = Gio.MenuItem.new(None, None)
    item.set_attribute_value("custom", GLib.Variant.new_string("avatar-header"))
    head.append_item(item)
    menu.append_section(None, head)
    acc = Gio.Menu()
    acc.append(tr('Kontoverwaltung'), "app.account")
    acc.append(tr('Bisherige Bestellungen'), "app.orders")
    acc.append(tr('Geldbeutel'), "app.wallet")
    menu.append_section(None, acc)
    out = Gio.Menu()
    out.append(tr('Ausloggen'), "app.logout")
    menu.append_section(None, out)
    pop = Gtk.PopoverMenu.new_from_model(menu)
    name = api.username or tr('GOG')
    box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=8, margin_end=8)
    av = Adw.Avatar(size=32, show_initials=True, text=name)
    tex = win.avatar.get_custom_image()
    if tex is not None:
        av.set_custom_image(tex)
    box.append(av)
    box.append(label(name, css=["heading"], ellipsize=True))
    pop.add_child(box, "avatar-header")
    win.avatar_header = av
    return pop


# --------------------------------------------------------------------------- Aktionen mit Dialog
def confirm_logout(win) -> None:
    """Ausloggen mit Rückfrage wie in Galaxy; laufende Downloads werden abgebrochen."""
    from .dialogs import confirm_destructive

    from ..backend import is_network_job

    def do() -> None:
        for job in list(win.backend.jobs.active()):
            if is_network_job(job.kind):
                win.backend.jobs.cancel(job)
        win.do_logout()
    confirm_destructive(win, tr('Ausloggen'),
                        tr('Möchtest du dich wirklich ausloggen? Dadurch werden alle Vorgänge in der Downloadliste abgebrochen.'),
                        tr('Ausloggen'), do)


def show_whats_new(win) -> None:
    """„Neu bei uns“: Abschnitt der installierten Version aus CHANGELOG.md, ältere Versionen aufklappbar."""
    text = clientinfo.changelog_text()
    current, older = clientinfo.changelog_section(text, __version__)
    dlg = Adw.Dialog(title=tr('Neu bei uns'), content_width=600, content_height=560)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=18, margin_start=22, margin_end=22)
    box.append(label(f"{APP_NAME} {__version__}", css=["title-2"]))
    if current:
        box.append(label(clientinfo.markdown_to_plain(current), wrap=True))
    elif not text:
        box.append(label(tr('Kein Änderungsprotokoll gefunden.'), css=["dim"]))
    if older:
        exp = Gtk.Expander(label=tr('Frühere Versionen'), expanded=not current)
        exp.set_child(label(clientinfo.markdown_to_plain(older), wrap=True))
        box.append(exp)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    scroll.set_child(box)
    tv.set_content(scroll)
    dlg.set_child(tv)
    dlg.present(win)


def check_client_update(win) -> None:
    """„Nach Updates suchen“: neueste Veröffentlichung auf GitHub mit der installierten Version vergleichen."""
    def done(res: clientinfo.UpdateCheck) -> None:
        if res.state == "newer":
            toast = Adw.Toast.new(GLib.markup_escape_text(tr('Neues Update {0} verfügbar').format(res.latest)))
            toast.set_button_label(tr('Anzeigen'))
            toast.connect("button-clicked", lambda *_: open_uri(res.url))
            toast.set_timeout(10)
            win.toasts.add_toast(toast)
        elif res.state == "none":
            win.toast(tr('Noch keine Veröffentlichung'))
        else:
            win.toast(tr('Keine Updates für galaxy-linux verfügbar ({0})').format(__version__))

    win.toast(tr('Suche nach Updates …'))
    run_async(clientinfo.check_for_update, done, lambda e: win.toast(tr('Update-Suche fehlgeschlagen: {0}').format(e)))


def show_legal(win) -> None:
    """„Rechtliches und Datenschutz“: Hinweise aus der README und Lizenz."""
    paras = (
        tr('galaxy-linux {0} – inoffizieller, nativer Client für GOG.com.').format(__version__),
        tr('GOG und GOG Galaxy sind Marken von GOG sp. z o.o. Dieses Projekt steht in keiner Verbindung zu GOG.'),
        tr('Haftungsausschluss: inoffizielles Projekt, Nutzung auf eigenes Risiko. Die GOG-Nutzungsvereinbarung untersagt nicht genehmigte Drittprogramme; GOG duldet Open-Source-Clients wie Heroic, Minigalaxy oder Lutris seit Jahren, ein Anspruch darauf besteht aber nicht. Das Repository enthält keinen Code und keine Grafiken von GOG.'),
        tr('Datenschutz: Zugangsdaten bleiben auf diesem Rechner (~/.config/galaxy-linux/credentials.json mit Rechten 0600 oder im System-Schlüsselbund). Das Protokoll maskiert Tokens und Login-Codes. Der Client spricht mit den GOG-Diensten, für Cover eigener Bibliotheken mit Steam und für „Nach Updates suchen“ mit GitHub.'),
        tr('Lizenz: MIT. Quelltext und vollständiger Lizenztext: {0}').format(clientinfo.REPO_URL),
    )
    dlg = Adw.AlertDialog.new(tr('Rechtliches und Datenschutz'), None)
    text = label("\n\n".join(paras), wrap=True)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=420)
    scroll.set_child(text)
    dlg.set_extra_child(scroll)
    dlg.add_response("project", tr('Projektseite öffnen'))
    dlg.add_response("close", tr('Schließen'))
    dlg.set_default_response("close")
    dlg.set_close_response("close")
    dlg.connect("response", lambda _d, r: open_uri(clientinfo.REPO_URL) if r == "project" else None)
    dlg.present(win)
