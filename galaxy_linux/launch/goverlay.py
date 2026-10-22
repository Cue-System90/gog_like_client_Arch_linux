"""Anbindung an GOverlay / MangoHud / vkBasalt / GameMode / Gamescope.

Ältere GOverlay-Versionen schlugen für Steam eine Startoptionszeile wie ``ENABLE_VKBASALT=1 mangohud %command%`` vor;
diese Zeile versteht der Client weiterhin. GOverlay 1.9 arbeitet dagegen mit Profilen pro Spiel (Wrapper ``bgmod``) –
die setzt galaxy-linux beim Start selbst ein (``goverlay_profiles``), eine mangohud-Zeile ist dann überflüssig und
würde nur die globale MangoHud-Konfiguration nutzen.
"""
from __future__ import annotations

from ..i18n import tr

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import launch_options

HOME = Path.home()
MANGOHUD_CONF = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "MangoHud" / "MangoHud.conf"
VKBASALT_CONF = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "vkBasalt" / "vkBasalt.conf"
GOVERLAY_CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "goverlay"


@dataclass(frozen=True)
class ToolStatus:
    goverlay: bool
    mangohud: bool
    mangohud32: bool
    vkbasalt: bool
    gamemode: bool
    gamescope: bool
    mangohud_config: bool
    vkbasalt_config: bool


def _vulkan_layer_present(needle: str) -> bool:
    dirs = [Path("/usr/share/vulkan/implicit_layer.d"), Path("/usr/share/vulkan/explicit_layer.d"),
            Path("/etc/vulkan/implicit_layer.d"), HOME / ".local/share/vulkan/implicit_layer.d"]
    for d in dirs:
        try:
            for f in d.iterdir():
                if needle.lower() in f.name.lower():
                    return True
        except OSError:
            continue
    return False


def status() -> ToolStatus:
    return ToolStatus(
        goverlay=shutil.which("goverlay") is not None,
        mangohud=shutil.which("mangohud") is not None,
        mangohud32=Path("/usr/lib32/mangohud/libMangoHud.so").exists() or Path("/usr/lib32/libMangoHud.so").exists(),
        vkbasalt=_vulkan_layer_present("vkbasalt"),
        gamemode=shutil.which("gamemoderun") is not None,
        gamescope=shutil.which("gamescope") is not None,
        mangohud_config=MANGOHUD_CONF.exists(),
        vkbasalt_config=VKBASALT_CONF.exists(),
    )


def build_launch_options(mangohud: bool = True, vkbasalt: bool = False, gamemode: bool = False,
                         gamescope_args: str = "", extra_env: dict[str, str] | None = None,
                         game_args: str = "") -> str:
    """Erzeugt eine Steam-/GOverlay-kompatible Startoptionszeile."""
    parts: list[str] = []
    if vkbasalt:
        parts.append("ENABLE_VKBASALT=1")
    for k, v in (extra_env or {}).items():
        parts.append(f"{k}={v}")
    if gamescope_args:
        parts.append(f"gamescope {gamescope_args.strip()} --")
    if gamemode:
        parts.append("gamemoderun")
    if mangohud:
        parts.append("mangohud")
    parts.append(launch_options.COMMAND_TOKEN)
    if game_args:
        parts.append(game_args.strip())
    return " ".join(parts)


PROFILE_ENV = ("MANGOHUD", "MANGOHUD_CONFIGFILE", "ENABLE_VKBASALT", "VKBASALT_CONFIG_FILE")


def strip_profile_parts(text: str) -> str:
    """Startoptionen ohne die Teile, die ein GOverlay-Profil (bgmod) selbst übernimmt: Wrapper ``mangohud``, ``bgmod``,
    ``fgmod`` (samt folgendem ``--``) und die MangoHud-/vkBasalt-Variablen; alles andere bleibt."""
    from .goverlay_profiles import drop_goverlay_wrappers
    o = launch_options.parse(text)
    env = {k: v for k, v in o.env.items() if k not in PROFILE_ENV}
    wrapper = [w for w in drop_goverlay_wrappers(o.wrapper)[0] if os.path.basename(w) != "mangohud"]
    parts = [f"{k}={shlex.quote(v)}" if v else f"{k}=" for k, v in env.items()]
    parts += [shlex.quote(w) for w in wrapper]
    if env or wrapper:
        parts.append(launch_options.COMMAND_TOKEN)
    parts += [shlex.quote(a) for a in o.args]
    return " ".join(parts)


def import_from_goverlay(current: str = "", profile_active: bool = False) -> str:
    """Leitet die Startoptionen aus der aktuellen GOverlay-/MangoHud-Konfiguration ab.

    Hat das Spiel ein GOverlay-Profil (``profile_active``), setzt galaxy-linux dessen ``bgmod`` beim Start selbst ein:
    dann bleiben die vorhandenen Startoptionen ``current`` ohne mangohud-/bgmod-/vkBasalt-Teile – es wird keine
    „mangohud %command%“-Zeile geschrieben. Sonst wie bisher die Zeile aus der globalen Konfiguration
    (~/.config/MangoHud/MangoHud.conf, ~/.config/vkBasalt/vkBasalt.conf).
    """
    if profile_active:
        return strip_profile_parts(current)
    st = status()
    use_mangohud = st.mangohud and (st.mangohud_config or st.goverlay)
    use_vkbasalt = st.vkbasalt and st.vkbasalt_config
    return build_launch_options(mangohud=use_mangohud, vkbasalt=use_vkbasalt)


PRESETS: list[tuple[str, str]] = [
    (tr('MangoHud (GOverlay-Standard)'), "mangohud %command%"),
    (tr('MangoHud + vkBasalt'), "ENABLE_VKBASALT=1 mangohud %command%"),
    (tr('MangoHud + GameMode'), "gamemoderun mangohud %command%"),
    (tr('MangoHud nur per Variable'), "MANGOHUD=1 %command%"),
    (tr('Gamescope Vollbild + MangoHud'), "gamescope -f -- mangohud %command%"),
    (tr('Proton-Log aktivieren'), "PROTON_LOG=1 %command%"),
    (tr('DXVK-HUD (FPS)'), "DXVK_HUD=fps %command%"),
    (tr('Wayland nativ (Proton 9+)'), "PROTON_ENABLE_WAYLAND=1 %command%"),
    (tr('Keine Startoptionen'), ""),
]


def open_goverlay() -> bool:
    exe = shutil.which("goverlay")
    if not exe:
        return False
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
    try:
        subprocess.Popen([exe], env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def mangohud_config_path() -> Path:
    return MANGOHUD_CONF
