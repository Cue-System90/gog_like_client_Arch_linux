# galaxy-linux – unofficial GOG client for Linux

**Deutsch:** [README.md](README.md)

An unofficial, **native** client for **GOG.com** on **CachyOS / Arch Linux** that works like the native
Steam client: library, installation through GOG's own content system, **per-game Proton version**,
**Steam-style launch options** (e.g. the line **GOverlay** generates), playtime, achievements, updates,
prefix tools – as a GTK4/libadwaita app and as a command line tool. The interface is available in
**German and English**.

> **Why not a "port of the EXE"?** `GOG_Galaxy_2.0.exe` is a 964 KB UPX-packed web installer stub that
> only downloads the actual (closed-source) Windows client. There is no source code to port. This project
> therefore rebuilds the client natively and talks to **the same GOG services** as Galaxy (login via
> auth.gog.com, library, content system v2 with manifests/depots/chunks, GamesDB metadata, gameplay API
> for playtime and achievements). An optional script (`scripts/install-official-galaxy-wine.sh`) can
> additionally install the original client into a Wine prefix – experimental and not required.

GOG and GOG GALAXY are trademarks of GOG sp. z o.o. This project is not affiliated with GOG.

**Disclaimer:** unofficial project, use at your own risk. GOG's user agreement prohibits unauthorised
third-party programs; open-source clients such as Heroic, Minigalaxy and Lutris have been tolerated for
years, but there is no guarantee. This repository contains no code or artwork from GOG.

## Interface

The layout follows GOG Galaxy 2.0:

* **Title bar**: on the left the **gear menu** like in Galaxy 2.1 (Add games & friends ▸, **Refresh library** –
  also F5/Ctrl+R –, Settings, Get help, What's new, Check for updates, Legal and privacy, Quit) · **avatar menu**
  (Account management, Order history, Wallet, Sign out) · search (above the sidebar); on the right back/forward,
  bookmark (favourite), **Filter**, **+** (add game, create library, scan folders, prefix maintenance), the
  "Search games" field, the view menu (layout, sorting, cover size, titles) and the window buttons.
* **Sidebar** in the order of the original: GOG STORE → Home, All games, Deals (discounts without owned
  games), Wishlist (counter), Cart, Order history (can be hidden: Settings → Interface → Preferred store);
  GAMES → Last played, All games (counter), Installed (counter), Downloads (counter);
  LIBRARIES → one entry with counter per custom library (like the platform entries in Galaxy 2.0), "+"
  creates a new one, right-click opens its menu; FRIENDS (activity, friends online, messages – not
  available in this client, therefore greyed out).
* **Store**: like the original, the real gog.com pages inside the content area, with a slim bar (back,
  forward, reload, home, address). Login and store share their cookies.
* **Library**: portrait covers in sections ("Play next", "All games", "Last played", "Recently
  installed"), tooltip with the title, progress bar on the cover while downloading.
* **Game page**: hero artwork with the transparent game logo, below it the purple **PLAY**/**INSTALL**
  button, the "⋯" menu (verify, folder, prefix, Winetricks, log, View in store, hide, uninstall), the stats
  PLAYTIME · LAST PLAYED · ACHIEVEMENTS · COMPATIBILITY · CLOUD SAVES, then "About the game" (genres, description,
  DLCs), achievements as an icon row and "Game details". In addition – like Steam – a **Compatibility**
  block with Proton selection, launch options, the game's GOverlay profile, "Open GOverlay" and a command
  preview; the gear opens the full properties.
* **Downloads**: queue with progress, speed, remaining time, cancel and history.
* URIs: `galaxy-linux://game/<id>`, `galaxy-linux://launch/<id>`, `galaxy-linux://view/{all|recent|installed|downloads}`,
  `galaxy-linux://view/library/<library-id>`.

---

## Features

| Area | Implementation |
|---|---|
| Login | OAuth like the Galaxy client (login page inside the client with `webkitgtk-6.0`, otherwise browser login and pasting the redirect URL; 2FA works). Automatic token refresh. |
| Store | **Embedded GOG store** like in Galaxy (WebKitGTK 6.0, same session as the login): GOG STORE sidebar group, pages in the interface language, wishlist counter. Buying and paying inside the client (payment windows such as PayPal open in a window of their own); after a **completed purchase** the library is synchronised at once. Offline installers and goodies are saved to the Downloads folder, links to other sites open in the browser. If the website is not signed in, a notice offers "Sign in to the store". Game page "⋯" → **View in store**. Signing out also signs out the store. No custom user agent, no affiliate links. |
| Library | All purchased games with cover, background, description, genres, developer/publisher, release date and DLC ownership (GamesDB + api.gog.com). Search, filters (installed, favourites, updates, Windows, Linux native), hiding games. **Sync with GOG** like Galaxy, without a restart: shortly after start, every 10 minutes, when switching to the window (at most every 5 minutes), after login and after a purchase (then again after 5/15/60 s); manually via gear → "Refresh library" (F5). The `userData.json` checksum skips the full download while nothing changed; games hidden on GOG are loaded too. New games carry a violet **NEW** for 14 days (until first opened) and are listed at the top under "New in library". Configurable under Settings → General → Sync with GOG. |
| Install location | Like the Windows client: global games folder in the settings (default `~/Games/GOG`), games end up in `<folder>/<game name>`. The install dialog offers the default, remembered additional locations or "Other folder …" and shows the final path. **Move installation** (game page menu, properties, CLI `move`) – instant on the same drive, otherwise copied with progress. |
| Copied games | Games copied into the games folder (or an additional install location) by hand – e.g. from a Windows installation of GOG Galaxy – are detected automatically by their `goggame-<ID>.info`, also one level deeper (a copied "GOG Games" collection folder). The client waits until copying has finished, registers the game and its DLCs as installed (the "Installed" counter follows) and creates missing Wine prefixes in the prefix folder with the DPI of the desktop scaling (225 % → 216). Deleted game folders are removed from the library again; the prefix with the save games stays. Manually: "+" → "Scan games folders for games" or `galaxy-linux scan`. Can be switched off in the settings. |
| Library view | Via the view button (next to the search field): **grid** or **list** (columns title, status, playtime, last played, size), cover size XS–XL, spacing between covers, titles under the covers, sections on/off, sorting – everything is saved: shared for All games/Installed/Last played and **separately for each custom library** (the menu shows at the top what it applies to). Covers are real portrait box art from GOG's game database (the library images themselves are landscape and would be cropped). |
| Custom libraries | Any number of libraries for games that do not come from GOG (other stores, emulators, old CDs, own installations …). **Create**: "+" next to LIBRARIES in the sidebar, "+" in the title bar → "Create library …" or Settings → Libraries – name, **games folder** and **prefix folder** (empty = `<games folder>/prefixes`). Every subfolder of the games folder becomes a game with its own prefix in the prefix folder (named like the game folder). The folder is watched: copied games are added once copying has finished, deleted ones are removed (the prefix stays). The executable is detected (uninstallers, runtime packages, editors and servers are skipped) and can be changed on the game page and in the properties. Cover and description come from the GOG catalogue or from Steam, otherwise the title is shown on the card. Launching, Proton selection, launch options, GOverlay, shortcuts and playtime work like for GOG games (playtime stays local). **Defaults per library** (Settings → Libraries): compatibility tool, launch options, Wayland driver, DPI for new prefixes, folder watching – they apply only when the game itself has nothing set (game → library → global). **Rename / change folders** there; **delete** with confirmation – the games disappear from the client, game folders and prefixes stay on disk. |
| Managing games and prefixes | **Global**: "+" → scan games folders · create missing prefixes for all games · remove orphaned prefixes (list with selection). **Per library**: header of the library view → "Add game …", "Scan folder" and "⋯" (create missing prefixes, remove orphaned prefixes, open games/prefix folder, edit library, delete library) – the same menu via right-click on the sidebar entry. **Single game**: "+" → Add game … (any folder, GOG or external, name, executable and library selectable) · game page "⋯" → Remove from library (optionally with game folder and/or prefix) · create / recreate / delete prefix (also in the properties). Folders removed by hand are not picked up again by the scan; the list is in the settings and can be undone there. |
| Shortcuts | Per installed game a shortcut in the **application menu** and/or on the **desktop** (like the Windows client): selectable in the install dialog, later via the "⋯" menu, the properties or `galaxy-linux shortcut`. With the game icon; the game is started through the client (playtime, Proton settings). Shortcuts are removed when uninstalling. |
| Installation | **Galaxy content system v2** – exactly the path Galaxy uses: build manifest → depots (language, incl. all DLCs) → zlib chunks in parallel, MD5-verified, **resumable**. Plus redistributables (DirectX, VC++, .NET …) like Galaxy. Linux-native games via GOG's .sh installer. Games without a Galaxy build via the offline installer + `innoextract`. |
| Update / verify | Build comparison, updates download only changed chunks and remove obsolete files; "Verify files" repairs damaged chunks. |
| Launching like Steam | **Selectable per game:** every Proton found (Steam libraries, `compatibilitytools.d`, `/usr/share/steam/compatibilitytools.d` → **proton-cachyos**, Heroic), GE-Proton/UMU-Proton (auto-download via umu), system Wine, Lutris/Bottles runners. Launch method: **umu-launcher** (default, with protonfixes and the Steam Linux Runtime – required for `proton-cachyos-slr`), Proton directly or Wine. One Wine prefix per game. The **Proton Wayland driver** (`PROTON_ENABLE_WAYLAND=1`) is the default (KDE Wayland); XWayland only as a per-game exception under Properties → Compatibility. |
| Launch options | **Steam syntax 1:1**: `mangohud %command%`, `ENABLE_VKBASALT=1 mangohud %command% -novid`, `gamescope -f -- %command%`, `PROTON_LOG=1 %command%`, just `-windowed` … Global and per game. Preview of the resulting command. |
| GOverlay | **Per-game profiles like under Steam**: galaxy-linux adds its game folders to GOverlay's folder list (`~/.config/goverlay/nonsteam_folders.txt`) so the games show up in GOverlay (then "Refresh game library" there), and stores their covers. Clicking the game in GOverlay creates its profile; at launch galaxy-linux puts that profile's `bgmod` in front of the command (MangoHud, vkBasalt, OptiScaler, Lossless … with the game's settings instead of the global configuration), plus `gamemoderun` and `/WineDetectionEnabled:False` when chosen in the profile. Without an own profile the global GOverlay profile can be used. Before verify, update, move and uninstall, `bgmod-uninstaller` undoes the changes in the game folder. Settings → Compatibility → GOverlay; per game under Properties → Compatibility. Toggles for MangoHud, vkBasalt, GameMode, Gamescope. "Open GOverlay". |
| Properties dialog | Like Steam: compatibility, launch options, environment variables, executable (playTask), pre/post commands, redistributables, Winetricks, open prefix/folder/log, favourite, hide. |
| Playtime | Measured locally, optionally sent to GOG (appears in the GOG profile like with Galaxy). |
| Achievements | Per game via the gameplay API (game-specific token from the build manifest, like Galaxy). |
| Galaxy services (comet) | Games with the GOG Galaxy SDK unlock achievements and use stats and leaderboards in game: like Heroic, galaxy-linux sets up the placeholder service `GalaxyCommunication.exe` in the prefix (`sc create`, Galaxy client registry entry) and starts **comet** (a re-implementation of the Galaxy communication service on `127.0.0.1:9977`) before the game, stopping it afterwards. Tokens are never put on the command line but briefly into a private file (0600). If a comet is already running (e.g. Heroic's), it is shared. comet is searched for (own path → PATH → Heroic package → own download), otherwise downloaded once from the official release on first use (SHA-256 verified). Switches: Settings → Game features, per game Properties → Game features; status, download/update and own path under Settings → Compatibility → Galaxy services. Log: `…/logs/comet.log` (masked). |
| Cloud saves | Like Galaxy via `cloudstorage.gog.com`: before every launch newer saves are downloaded from the cloud into the prefix, after the game exits your own saves are uploaded. The save locations come from GOG's game configuration (`<?DOCUMENTS?>`, `<?APPLICATION_DATA_ROAMING?>` … → `drive_c/users/steamuser/…` in the prefix). On a conflict the client asks like Galaxy (GOG Galaxy Cloud / This computer / Ignore); above 200 MB Galaxy's quota notice appears. Before cloud files replace local ones a backup `~/.local/share/galaxy-linux/cloudsave-backups/<id>/<time>.tar.gz` is made (the last five are kept). Globally under Settings → Game features, per game under Properties → Game features (also "Upload now", "Download now", backups and save locations); the state is shown as a stat on the game page. GOG Windows games only (Linux versions have no cloud saves at GOG). |
| Settings | **General**: interface language, games and prefix folders, additional install locations, excluded game folders, preferred game language, parallel download connections, folder watching, automatic prefixes, shortcut defaults, update check at start, send playtime to GOG, show hidden games, store credentials in the system keyring. **Libraries**: create, edit and delete custom libraries. **Compatibility**: default compatibility tool, prefer umu-launcher, Wayland driver as default, redistributables, DPI for new prefixes (0 = from the desktop scaling), global launch options, overview of detected tools (umu, GOverlay, MangoHud, vkBasalt, GameMode, Gamescope). |
| Language | Interface in **German** and **English**, follows the system language; can be changed in the settings (see [Language](#language)). |
| Log | Detailed log for troubleshooting, credentials and tokens masked (see [Logging](#logging)). |
| CLI | Everything also from the terminal/scripts: `galaxy-linux install|launch|config|tools|…`. URI scheme `galaxy-linux://launch/<id>`. |

**Not implemented yet** (Galaxy features that would be projects of their own): friends/chat,
integration of other stores (Steam/Epic plugins), the in-game Galaxy overlay, a client auto-updater.

---

## Installation on CachyOS / Arch Linux

Current version: **0.2.0** (overhaul in progress) – changes in [CHANGELOG.md](CHANGELOG.md) (German).

```bash
git clone https://github.com/Cue-System90/gog_like_client_Arch_linux.git
cd gog_like_client_Arch_linux
./install.sh
```

`install.sh` checks the packages (missing required packages via pacman, recommended ones on request),
builds the pacman package into `../dist/` (next to the cloned folder), removes an existing version
completely (settings, credentials and games stay) and installs it with `sudo pacman -U`.
**Update:** `git pull && ./install.sh`.

Required: `python python-requests python-gobject gtk4 libadwaita webkitgtk-6.0 unzip xdg-utils`
(`webkitgtk-6.0` = embedded GOG store and GOG login inside the client; without it, store and login open in the browser).
Recommended: `umu-launcher proton-cachyos mangohud lib32-mangohud goverlay winetricks gamescope innoextract python-vdf`

```bash
./install.sh --purge   # as above, additionally deletes settings, credentials and logs first (games stay)
./install.sh --build   # only build the package (scripts/build-package.sh), stored in ../dist
./install.sh --user    # without root into ~/.local (launcher script ~/.local/bin/galaxy-linux + menu entry)
./install.sh --deps    # only check/install missing packages
scripts/uninstall.sh [--purge] [--games]   # remove; --purge also settings/credentials/logs, --games also games and prefixes
python3 -m galaxy_linux                    # run directly from the folder without installing
```

`--build` also places standalone scripts for other machines next to the package: `install.sh` (removes
the old version automatically, verifies the checksums in `SHA256SUMS`, installs via `pacman -U`) and
`uninstall.sh`.

### Logging

The client logs in detail to `~/.local/share/galaxy-linux/logs/galaxy-linux.log` by default (rotating,
5 MB × 3): system environment and detected tools, every GOG API call with status and duration,
installation plans (build, language, depots, sizes), download summaries, launch commands, exit codes,
errors with traceback and GTK warnings. Tokens, login codes and signed CDN parameters are masked; the
file can be shared as is.

```bash
galaxy-linux log            # last 200 lines
galaxy-linux log --path     # path of the log file
galaxy-linux log --clear    # clear the log
GALAXY_LINUX_LOG=off galaxy-linux    # no file logging
GALAXY_LINUX_LOG=debug galaxy-linux  # additional debug output on the console
```

Per game there is also `~/.local/share/galaxy-linux/logs/<id>.log` with the output of Proton/Wine/umu.

## Language

The interface is available in German and English. Without a setting it follows the system language
(German → German, everything else → English); change it under *Settings → General → Interface* (takes
effect after a restart), with `galaxy-linux config global ui_language en`, or for a single start with
`GALAXY_LINUX_LANG=en galaxy-linux`. Source strings are German and marked with `tr()`;
`scripts/i18n-update.py [language]` updates `galaxy_linux/locale/<language>.po` (new strings get an empty
`msgstr` and appear in German until translated). New translations are welcome.

## Getting started

1. **Sign in**: "Sign in to GOG". With `webkitgtk-6.0` installed, the GOG login page appears inside the
   client and the login is taken over automatically (like the Windows client). Otherwise the browser
   opens; after logging in, copy the address of the (empty) page
   `https://embed.gog.com/on_login_success?origin=client&code=…` and paste it – the login starts as soon
   as the code is recognised.
2. **Install**: choose a game → *Install* → platform (Windows via Proton / Linux native), language,
   folder, DLCs → the size is calculated live from the manifest.
3. **Choose Proton**: on the game page in the *Proton / Wine* field (or *Properties → Compatibility*).
   The default is the system Proton (`proton-cachyos`) via umu.
4. **GOverlay**: click *Open GOverlay* on the game page → "Refresh game library" in GOverlay → click the game
   and configure it. galaxy-linux then launches the game with this profile (shown in the *Compatibility* block).
5. **Play**.

Default folders: games `~/Games/GOG/<game>`, prefixes `~/Games/GOG/prefixes/<ID>`, configuration
`~/.config/galaxy-linux/`, data/logs `~/.local/share/galaxy-linux/`, cache `~/.cache/galaxy-linux/`.
Credentials are stored in `~/.config/galaxy-linux/credentials.json` (0600) or optionally in the system keyring.

## Launch options – syntax (like Steam)

```
VAR=value … wrapper … %command% … game arguments
```

* `MANGOHUD=1 %command%` – environment variable
* `mangohud %command%` – wrapper (GOverlay default)
* `ENABLE_VKBASALT=1 gamemoderun mangohud %command%` – several combined
* `gamescope -W 2560 -H 1440 -f -- %command%` – Gamescope
* `%command% -skipintro` – argument for the game; without `%command%` all tokens are appended

Order at launch: `wrappers-from-launch-options  [gamescope] [gamemoderun] [mangohud]  <umu-run|proton|wine> game.exe <args>`.

## Proton outside Steam – how games are launched

* **umu** (default): `WINEPREFIX=<prefix> PROTONPATH=<proton> GAMEID=umu-<id> STORE=gog PROTON_VERB=waitforexitandrun umu-run game.exe`
  umu brings the Steam Linux Runtime (even without Steam installed) and applies protonfixes.
  `proton-cachyos-slr` requires the container and therefore runs via umu.
* **Proton directly**: `STEAM_COMPAT_DATA_PATH=<prefix> STEAM_COMPAT_CLIENT_INSTALL_PATH=<steam> proton waitforexitandrun game.exe`
  (for classic Protons from the Steam library; SLR Protons only if the runtime is installed in Steam).
* **Wine**: `WINEPREFIX=<prefix> wine game.exe`

The actual command is shown as a preview on the game page and in the log `~/.local/share/galaxy-linux/logs/<id>.log`.

## Command line

```bash
galaxy-linux login                       # sign in (URL/code is requested)
galaxy-linux list [--installed] [--refresh] [--json]
galaxy-linux info "Witcher" --builds
galaxy-linux install 1207658924 [--platform windows|linux] [--lang en-US] [--dir ~/Games] [--no-dlc]
galaxy-linux update <game>  |  galaxy-linux verify <game>  |  galaxy-linux updates --install
galaxy-linux scan [--no-prefix]                   # register copied games, create missing prefixes
galaxy-linux library list [--json]               # custom libraries with folders, defaults and game count
galaxy-linux library add "Other stores" /mnt/games/other [--prefix-dir /mnt/games/prefixes] \
             [--tool cachyos] [--options "gamemoderun %command%"] [--wayland on|off|default] [--dpi 216]
galaxy-linux library rename "Other stores" "Old games"
galaxy-linux library set "Old games" prefix_dir /mnt/games/prefixes   # also games_dir, tool, options, wayland, dpi, watch
galaxy-linux library remove "Old games" [-y]     # remove games from the client – game folders and prefixes stay
galaxy-linux list --library "Old games"           # games of a library (--external: all libraries)
galaxy-linux add <folder> [--library NAME] [--title T] [--exe rel/path.exe] [--no-prefix]   # add a game by hand
galaxy-linux remove <game> [--delete-files] [--delete-prefix]            # remove from the library
galaxy-linux prefix create|reset|delete <game>    # create, reset or delete a game's prefix
galaxy-linux prefixes --create-missing | --delete-orphans [--library NAME]   # prefixes globally or per library
galaxy-linux move <game> --dir /mnt/games         # move an installation (subfolder is kept)
galaxy-linux shortcut <game> [--menu] [--desktop] [--remove]   # create/remove shortcuts
galaxy-linux config global install_dir ~/Games    # global games folder
galaxy-linux config global ui_language en         # interface language (auto, de, en)
galaxy-linux config locations --add /mnt/games    # additional install location for the dialog
galaxy-linux tools                       # detected Proton/Wine versions (IDs)
galaxy-linux config tool <game> cachyos  # compatibility tool (ID or part of the name, "auto")
galaxy-linux config options <game> "ENABLE_VKBASALT=1 mangohud %command%"
galaxy-linux goverlay [<game>] [--sync]  # GOverlay: registered folders and the profile of each game (--sync: register)
galaxy-linux goverlay-import <game>      # derive launch options from GOverlay (with a profile: no mangohud line)
galaxy-linux comet [--download] [--json]   # Galaxy services: comet status, download/update
galaxy-linux config env <game> DXVK_ASYNC=1
galaxy-linux launch <game> --dry-run     # only show the command
galaxy-linux launch <game> -- -windowed  # launch, arguments for the game
galaxy-linux prefix winetricks <game> vcrun2019
galaxy-linux cloud <game> [--status]      # sync cloud saves (both directions) or show their state
galaxy-linux cloud <game> --upload|--download [--force]    # one direction only; --force: one side replaces the other
galaxy-linux uninstall <game> [--prefix]
```

`<game>` is the GOG ID or a unique part of the title; games of custom libraries have IDs `ext-<folder name>`.
Libraries are addressed by name, a unique part of the name or ID. `galaxy-linux --help` and
`galaxy-linux <command> --help` list all options.

## Tests

```bash
python3 -m unittest discover -s tests -t .                       # offline tests (fake CDN, parsers, plans, translations)
GALAXY_LINUX_NET_TESTS=1 python3 -m unittest tests.test_net -v   # real GOG endpoints without an account
```

The network test downloads a small redistributable completely through the chunk system and checks the
MD5 sums – this verifies the download path against GOG's CDN. Installing real games requires a GOG account.

## Troubleshooting

* **"requires the Steam Linux Runtime … please install umu-launcher"** – `sudo pacman -S umu-launcher`
  or choose a different Proton in the settings.
* **First launch takes long** – umu downloads the Steam Linux Runtime (~500 MB) once to
  `~/.local/share/umu`; Proton creates the prefix; redistributables are installed.
* **MangoHud does not show up** – `lib32-mangohud` for 32-bit games; with umu `MANGOHUD=1 %command%` is enough.
* **Game without a Galaxy build** – installed automatically via the offline installer (`innoextract` required).
* **Logs** – `~/.local/share/galaxy-linux/logs/galaxy-linux.log` (client) and `…/logs/<id>.log` (game/Proton), `…/logs/comet.log` (Galaxy services); `galaxy-linux log`.

## Project structure

```
galaxy_linux/
  api/        auth.py (OAuth), client.py (GOG services), models.py, covers.py
  content/    manifest.py (v2 manifests), downloader.py (chunks), installer.py (install/update/verify/Linux/offline),
              scanner.py (copied GOG games), external.py (executable detection for custom-library games)
  launch/     compat.py (Proton/Wine detection), launch_options.py (Steam syntax), goverlay.py, launcher.py, shortcuts.py, display.py (scaling → DPI)
  ui/         app.py, window.py, game_page.py, dialogs.py, style.css
  locale/     en.po (English catalogue)               i18n.py  tr(), language detection, reading/writing .po
  backend.py  facade + job queue, custom libraries    cli.py   command line
  settings.py settings, libraries (CustomLibrary), registry
tests/        unit tests (fake CDN) + network tests
data/         .desktop file, icon        PKGBUILD, install.sh
scripts/      build-package.sh, uninstall.sh, i18n-update.py (maintain translations), install-official-galaxy-wine.sh
```

## Third-party tools

* **comet** – https://github.com/imLinguin/comet, copyright imLinguin and contributors, licensed under **Apache-2.0**
  (https://www.apache.org/licenses/LICENSE-2.0). galaxy-linux does not bundle comet: on request or on first use it
  downloads the unmodified files `comet-x86_64-unknown-linux-gnu` and `GalaxyCommunication-dummy.exe` from the
  official v0.3.2 release, verifies their SHA-256 sums and stores them with a NOTICE file under
  `~/.local/share/galaxy-linux/tools/comet/<version>/`.

License: MIT.
