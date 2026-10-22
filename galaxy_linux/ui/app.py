"""Adw.Application: Aktionen, CSS, Fenster, galaxy-linux://-URIs."""
from __future__ import annotations

from ..i18n import tr

import logging
import sys
from pathlib import Path

from .. import APP_ID, APP_NAME, __version__
from ..backend import Backend
from .util import Adw, Gdk, Gio, GLib, Gtk, idle

log = logging.getLogger(__name__)


class GalaxyApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.backend: Backend | None = None
        self.window = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        from .. import logsetup
        logsetup.log_gtk_versions()
        logsetup.install_glib_log_bridge()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_path(str(Path(__file__).parent / "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.backend = Backend()
        from .menus import install_actions
        install_actions(self)      # Zahnrad-/Avatar-Menü, F5/Strg+R, Strg+Q …

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow
            self.window = MainWindow(self, self.backend)
        self.window.present()

    def do_open(self, files, n_files: int, hint: str) -> None:
        self.do_activate()
        for f in files:
            uri = f.get_uri()
            self._handle_uri(uri)

    def _handle_uri(self, uri: str) -> None:
        # galaxy-linux://launch/<id>  |  galaxy-linux://game/<id>  |  galaxy-linux://view/<ansicht>  |  …/view/library/<ID>
        if not uri.startswith("galaxy-linux://"):
            return
        rest = uri[len("galaxy-linux://"):].strip("/").split("/")
        if rest and rest[0] == "view" and len(rest) >= 3 and rest[1] == "library":
            self.window.navigate(f"lib:{rest[2]}")
            return
        if rest and rest[0] == "view" and len(rest) >= 2 and rest[1] == "external":   # bis 0.1.4
            lib = self.backend.get_library("external") or next(iter(self.backend.libraries()), None)
            self.window.navigate(f"lib:{lib.id}" if lib else "all")
            return
        if rest and rest[0] == "view" and len(rest) >= 2 and rest[1] in ("all", "recent", "installed", "downloads"):
            self.window.navigate(rest[1])
            return
        if len(rest) >= 2 and (rest[1].isdigit() or rest[1].startswith("ext-")):
            action, pid = rest[0], rest[1]
            self.window.show_game(pid)
            if action == "launch" and self.backend.registry.is_installed(pid):
                idle(self.window.page._on_play)

    def _about(self) -> None:
        dlg = Adw.AboutDialog(application_name=APP_NAME, version=__version__, developer_name=tr('Inoffizielles Community-Projekt'),
                              comments=tr('Nativer GOG-Client für CachyOS/Arch Linux. Nutzt dieselben GOG-Dienste wie der offizielle Galaxy-Client (Login, Bibliothek, Content-System v2, GamesDB, Spielzeit, Erfolge) und startet Windows-Spiele über ein frei wählbares Proton (umu-launcher), Wine oder nativ.\n\nGOG und GOG Galaxy sind Marken von GOG sp. z o.o. Dieses Projekt steht in keiner Verbindung zu GOG.'),
                              license_type=Gtk.License.MIT_X11, application_icon="applications-games-symbolic")
        dlg.present(self.window)


def run(argv: list[str]) -> int:
    GLib.set_prgname(APP_ID)          # WM_CLASS / StartupWMClass → richtiges Symbol in Taskleiste und Fensterwechsler
    GLib.set_application_name(APP_NAME)
    app = GalaxyApp()
    return app.run([sys.argv[0], *[a for a in argv if a not in ("--gui", "gui")]])
