"""Online-Status bei GOG wie in Galaxy (Avatar-Menü „Online“ / „Unsichtbar“): solange der Client läuft, alle 5 Minuten
„online“ melden – mit dem laufenden Spiel; „Unsichtbar“, Abmelden und Beenden löschen den Status."""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

INTERVAL = 300.0         # Galaxy-Konfiguration: intervals.presence = 5 (Minuten)


class Presence:
    def __init__(self, api: Any, settings: Any, interval: float = INTERVAL) -> None:
        self.api = api
        self.settings = settings
        self.interval = interval
        self._game = ""
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.sent = ""           # zuletzt gemeldet: "" (nichts), "online", "online:<spiel>", "offline"

    @property
    def visible(self) -> bool:
        return getattr(self.settings, "presence_status", "online") != "invisible"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="gl-presence", daemon=True)
            self._thread.start()
        self._wake.set()

    def set_game(self, product_id: str = "") -> None:
        """Laufendes Spiel melden ("" = keins) – sofort, nicht erst beim nächsten Takt."""
        with self._lock:
            self._game = str(product_id or "")
        self._wake.set()

    def refresh(self) -> None:
        """Nach Login, Logout oder Wechsel Online/Unsichtbar."""
        self._wake.set()

    def stop(self) -> None:
        """Beim Beenden: Status löschen (wie Galaxy, das offline geht)."""
        self._stop.set()
        self._wake.set()
        self.tick(final=True)

    def tick(self, final: bool = False) -> str:
        """Einmal melden; gibt zurück, was gemeldet wurde ("online…", "offline" oder "")."""
        api = self.api
        if not getattr(api, "logged_in", False):
            self.sent = ""
            return ""
        with self._lock:
            game = self._game
        try:
            if final or not self.visible:
                if self.sent and self.sent != "offline":
                    api.clear_presence()
                    self.sent = "offline"
                    return "offline"
                return ""
            ok = api.set_presence(game)
            self.sent = (f"online:{game}" if game else "online") if ok else self.sent
            return self.sent if ok else ""
        except Exception as exc:       # Netz weg: beim nächsten Takt erneut
            log.debug("Online-Status nicht gemeldet: %s", exc)
            return ""

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self.interval)
            self._wake.clear()
