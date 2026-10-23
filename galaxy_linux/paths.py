"""XDG-konforme Pfade für Konfiguration, Daten, Cache und Spiele."""
from __future__ import annotations

import os
from pathlib import Path

PKG = "galaxy-linux"


def _xdg(var: str, fallback: str) -> Path:
    val = os.environ.get(var)
    return Path(val) if val else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / PKG
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / PKG
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / PKG

LOG_DIR = DATA_DIR / "logs"
MANIFEST_DIR = DATA_DIR / "manifests"
IMAGE_CACHE_DIR = CACHE_DIR / "images"
API_CACHE_DIR = CACHE_DIR / "api"

DEFAULT_GAMES_DIR = Path.home() / "Games" / "GOG"
DEFAULT_PREFIX_DIR = DEFAULT_GAMES_DIR / "prefixes"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
GAME_SETTINGS_FILE = CONFIG_DIR / "games.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
INSTALLED_FILE = DATA_DIR / "installed.json"
PLAYTIME_FILE = DATA_DIR / "playtime.json"
JOBS_FILE = DATA_DIR / "jobs.json"          # offene Downloads (überstehen einen Neustart der Oberfläche)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, LOG_DIR, MANIFEST_DIR, IMAGE_CACHE_DIR, API_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def game_log_file(product_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{product_id}.log"
