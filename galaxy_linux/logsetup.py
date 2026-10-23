"""Datei-Logging für Auswertung: ~/.local/share/galaxy-linux/logs/galaxy-linux.log (rotierend, DEBUG).

Zugangsdaten werden nie protokolliert: Login-Codes, Access-/Refresh-Tokens, Client-Secret, Passwörter,
Sitzungs-IDs und signierte CDN-Parameter werden in URLs, Argumenten, Nachrichten und Tracebacks maskiert.
Die eigentlichen Tokens liegen ausschließlich in ~/.config/galaxy-linux/credentials.json (0600) oder im Schlüsselbund.
Konsole: INFO, mit GALAXY_LINUX_LOG=debug ebenfalls DEBUG; GALAXY_LINUX_LOG=off schaltet die Datei ab.
GTK/GLib-Meldungen kommen ab Warnung ins Protokoll, Infos und Debug-Ausgaben nur mit GALAXY_LINUX_LOG=debug.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.parse

from . import __version__, paths

LOG_FILE = paths.LOG_DIR / "galaxy-linux.log"
_SENSITIVE_HOSTS = ("auth.gog.com", "login.gog.com")
_SENSITIVE_KEYS = ("token", "secret", "code", "session", "sig", "key", "password", "auth")
_configured = False


def mask_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "<url>"
    if p.hostname and any(p.hostname.endswith(h) for h in _SENSITIVE_HOSTS):
        return f"{p.scheme}://{p.hostname}{p.path}?<maskiert>"
    if not p.query:
        return url
    kept = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            kept.append((k, "***"))
        else:
            kept.append((k, v))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(kept), ""))


import re as _re

_SECRET_PATTERNS = [
    (_re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (_re.compile(r"(?i)(refresh_token|client_secret|access_token|session_id|password|code|token|secret)=[^&\s\"']+"), r"\1=***"),
    (_re.compile(r"(?i)(\"?(?:refresh_token|access_token|client_secret|session_id|password)\"?\s*[:=]\s*)\"?[A-Za-z0-9._\-]+\"?"), r"\1***"),
    (_re.compile(r"\b[a-f0-9]{64}\b"), "<hex64>"),   # Client-Secret-Format, vorsorglich
]


def mask_text(text: str) -> str:
    """Entfernt Zugangsdaten (Tokens, Login-Codes, Secrets, Passwörter) aus beliebigem Text."""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def mask_argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for a in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if a in ("--code", "--password", "--token"):
            out.append(a)
            hide_next = True
        elif a.startswith(("--code=", "--password=", "--token=")):
            out.append(a.split("=", 1)[0] + "=***")
        elif "code=" in a or "token=" in a or a.startswith("http"):
            out.append(mask_url(a) if a.startswith("http") else mask_text(a))
        else:
            out.append(a)
    return out


class _MaskFilter(logging.Filter):
    """Letzte Sicherung: Zugangsdaten in jeder Log-Nachricht (inkl. Tracebacks) unkenntlich machen."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask_text(msg)
        if masked != msg:
            record.msg, record.args = masked, ()
        if record.exc_text:
            record.exc_text = mask_text(record.exc_text)
        return True


def setup(console_level: int | None = None, log_env: bool = True) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    mode = "debug" if debug_mode() else (os.environ.get("GALAXY_LINUX_LOG") or "").lower()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s: %(message)s")
    if mode != "off":
        try:
            paths.ensure_dirs()
            fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            fh.addFilter(_MaskFilter())
            root.addHandler(fh)
        except OSError as exc:
            print(f"Log-Datei nicht beschreibbar: {exc}", file=sys.stderr)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(console_level if console_level is not None else (logging.DEBUG if mode == "debug" else logging.INFO))
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    sh.addFilter(_MaskFilter())
    root.addHandler(sh)
    # Bibliotheken, die Query-Strings mit Tokens loggen würden, stummschalten
    for noisy in ("urllib3", "requests", "PIL", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb) -> None:
        logging.getLogger("galaxy_linux").critical("Unbehandelte Ausnahme", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = excepthook

    def thread_hook(args) -> None:
        logging.getLogger("galaxy_linux").error("Ausnahme im Thread %s", args.thread.name if args.thread else "?",
                                                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook
    if log_env:
        log_environment()


def _os_release() -> str:
    try:
        for line in open("/etc/os-release", encoding="utf-8"):
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _version_of(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        text = _ANSI.sub("", (out.stdout or out.stderr)).strip()
        return text.splitlines()[0].strip() if text else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def log_environment() -> None:
    log = logging.getLogger("galaxy_linux.env")
    log.info("galaxy-linux %s gestartet: %s", __version__, " ".join(mask_argv(sys.argv)))
    log.info("System: %s · Kernel %s · Python %s · Desktop %s · Session %s", _os_release(), platform.release(),
             platform.python_version(), os.environ.get("XDG_CURRENT_DESKTOP", "?"), os.environ.get("XDG_SESSION_TYPE", "?"))
    log.info("Pfade: config=%s data=%s cache=%s log=%s", paths.CONFIG_DIR, paths.DATA_DIR, paths.CACHE_DIR, LOG_FILE)
    tools = {}
    for name, cmd in (("umu-run", ["umu-run", "--version"]), ("wine", ["wine", "--version"]), ("mangohud", ["mangohud", "--version"]),
                      ("gamescope", ["gamescope", "--version"]), ("goverlay", None), ("winetricks", ["winetricks", "--version"]),
                      ("innoextract", ["innoextract", "--version"]), ("gamemoderun", None)):
        path = shutil.which(name)
        tools[name] = (_version_of(cmd)[:60] if (path and cmd) else ("ja" if path else "nein"))
    log.info("Werkzeuge: %s", ", ".join(f"{k}={v}" for k, v in tools.items()))
    gpu = _version_of(["sh", "-c", "lspci 2>/dev/null | grep -iE 'vga|3d' | head -2 | cut -d: -f3-"])
    if gpu and gpu != "?":
        log.info("GPU: %s", gpu.strip())


def log_gtk_versions() -> None:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, GLib, Gtk
        display = Gdk.Display.get_default()
        logging.getLogger("galaxy_linux.env").info(
            "GTK %d.%d.%d · libadwaita %d.%d.%d · Display %s · GLib %d.%d.%d",
            Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version(),
            Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version(),
            type(display).__name__ if display else "?", GLib.MAJOR_VERSION, GLib.MINOR_VERSION, GLib.MICRO_VERSION)
    except Exception as exc:  # pragma: no cover
        logging.getLogger("galaxy_linux.env").warning("GTK-Versionen nicht ermittelbar: %s", exc)


# GLib-Stufen (GLogLevelFlags) → Python-Stufen; FLAG_RECURSION (1) und FLAG_FATAL (2) werden ignoriert
_GLIB_LEVELS = ((1 << 2, logging.CRITICAL),    # G_LOG_LEVEL_ERROR
                (1 << 3, logging.ERROR),       # G_LOG_LEVEL_CRITICAL
                (1 << 4, logging.WARNING),     # G_LOG_LEVEL_WARNING
                (1 << 5, logging.INFO),        # G_LOG_LEVEL_MESSAGE
                (1 << 6, logging.INFO),        # G_LOG_LEVEL_INFO („Gdk-INFO: Vulkan: Loader Message …“)
                (1 << 7, logging.DEBUG))       # G_LOG_LEVEL_DEBUG


def debug_mode() -> bool:
    """GALAXY_LINUX_LOG=debug (oder GALAXY_LINUX_DEBUG): ausführliches Protokoll."""
    return (os.environ.get("GALAXY_LINUX_LOG") or "").lower() == "debug" or bool(os.environ.get("GALAXY_LINUX_DEBUG"))


def glib_bridge_level(flags: int, debug: bool = False) -> int | None:
    """Python-Stufe, mit der eine GLib-Meldung ins Protokoll kommt – oder None (nicht übernehmen). Standard: nur
    Warnungen und schlimmer (Gtk-WARNING, Gtk-CRITICAL …); Meldungen, Infos und Debug-Ausgaben von GTK/GDK (z. B.
    Dutzende „Vulkan: Loader Message“) nur mit GALAXY_LINUX_LOG=debug."""
    level = logging.WARNING
    for flag, lvl in _GLIB_LEVELS:
        if flags & flag:
            level = lvl
            break
    return level if (debug or level >= logging.WARNING) else None


def glib_writer(debug: bool, format_fields, passthrough, handled=None, logger: logging.Logger | None = None):
    """Schreibfunktion für ``GLib.log_set_writer_func``: übernimmt Meldungen gemäß ``glib_bridge_level`` ins
    Protokoll und reicht jede an ``passthrough`` (GLibs Standard-Ausgabe auf stderr bzw. ins Journal) weiter."""
    glog = logger or logging.getLogger("glib")

    def writer(log_level, fields, _n_fields=None, _user_data=None):
        lvl = glib_bridge_level(int(log_level), debug)
        if lvl is not None:
            try:
                glog.log(lvl, str(format_fields(log_level, fields, False)).strip())
            except Exception:
                pass
        try:
            return passthrough(log_level, fields)
        except Exception:
            return handled
    return writer


def install_glib_log_bridge() -> None:
    """Leitet Gtk-/GLib-Warnungen zusätzlich in die Log-Datei (Infos/Debug nur mit GALAXY_LINUX_LOG=debug). Auf
    stderr gilt GLibs Standard: Infos und Debug-Ausgaben nur mit G_MESSAGES_DEBUG."""
    try:
        from gi.repository import GLib
    except Exception:
        return
    glog = logging.getLogger("glib")
    passthrough = getattr(GLib, "log_writer_default", None) or GLib.log_writer_standard_streams
    writer = glib_writer(debug_mode(), GLib.log_writer_format_fields, passthrough, GLib.LogWriterOutput.HANDLED, glog)
    try:
        GLib.log_set_writer_func(writer, None)
    except Exception as exc:  # pragma: no cover
        glog.debug("GLib-Log-Brücke nicht verfügbar: %s", exc)
