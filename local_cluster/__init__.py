"""local:cluster: ein inoffizieller, nativer Client für GOG.com auf CachyOS / Arch Linux.

Der Client spricht dieselben GOG-Dienste wie der offizielle Windows-Client
(Auth, Bibliothek, Content-System v2, GamesDB, Gameplay/Playtime) und startet
Windows-Spiele wie Steam über ein frei wählbares Proton (per umu-launcher oder
direkt), Wine oder nativ.
"""

__version__ = "0.6.0"
APP_ID = "io.github.cue_system90.LocalCluster"
APP_NAME = "local:cluster"
USER_AGENT = f"local-cluster/{__version__} (Linux; CachyOS)"
