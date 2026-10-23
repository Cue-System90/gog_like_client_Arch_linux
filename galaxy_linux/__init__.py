"""galaxy-linux: ein inoffizieller, nativer Client für GOG.com auf CachyOS / Arch Linux.

Der Client spricht dieselben GOG-Dienste wie der offizielle Windows-Client
(Auth, Bibliothek, Content-System v2, GamesDB, Gameplay/Playtime) und startet
Windows-Spiele wie Steam über ein frei wählbares Proton (per umu-launcher oder
direkt), Wine oder nativ.
"""

__version__ = "0.3.1"
APP_ID = "org.galaxylinux.Client"
APP_NAME = "galaxy-linux"
USER_AGENT = f"galaxy-linux/{__version__} (Linux; CachyOS)"
