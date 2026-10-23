"""Bandbreitenbegrenzung wie in Galaxy (Einstellungen → Installieren/aktualisieren → Bandbreite): „Bandbreite
begrenzen auf …“ und „Begrenzung planen von … bis …“ – gilt für alle Downloads von GOG (Spiele, Updates, Extras).

``Throttle`` ist ein Token-Eimer, den sich alle Download-Threads eines Prozesses teilen; die Rate wird bei jedem
Aufruf neu aus den Einstellungen gelesen (Änderungen und der Zeitplan wirken sofort).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

BLOCK = 64 * 1024        # Lesegröße beim gedrosselten Laden


def parse_hhmm(text: str) -> int | None:
    """„08:30“ → Minuten seit Mitternacht."""
    try:
        h, m = str(text).strip().split(":", 1)
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h * 60 + m
    return None


def in_window(start: str, stop: str, now: float | None = None) -> bool:
    """Liegt ``now`` im Zeitfenster? Fenster über Mitternacht (22:00–06:00) werden unterstützt; gleiche Zeiten = immer."""
    a, b = parse_hhmm(start), parse_hhmm(stop)
    if a is None or b is None or a == b:
        return True
    t = time.localtime(time.time() if now is None else now)
    cur = t.tm_hour * 60 + t.tm_min
    return a <= cur < b if a < b else (cur >= a or cur < b)


def limit_active(settings: Any, now: float | None = None) -> bool:
    if not getattr(settings, "bandwidth_limit", False) or int(getattr(settings, "bandwidth_limit_kib", 0) or 0) <= 0:
        return False
    if getattr(settings, "bandwidth_schedule", False):
        return in_window(settings.bandwidth_schedule_start, settings.bandwidth_schedule_stop, now)
    return True


def current_rate(settings: Any, now: float | None = None) -> float:
    """Erlaubte Bytes pro Sekunde, 0 = unbegrenzt."""
    return float(int(settings.bandwidth_limit_kib) * 1024) if limit_active(settings, now) else 0.0


class Throttle:
    """Token-Eimer über alle Threads. ``consume(n)`` wartet, bis n Bytes erlaubt sind (bei Rate 0 sofort)."""

    def __init__(self, rate_fn: Callable[[], float], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate_fn = rate_fn
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._last = clock()

    def rate(self) -> float:
        try:
            return max(0.0, float(self.rate_fn()))
        except Exception:
            return 0.0

    def consume(self, n: int, cancel: threading.Event | None = None) -> None:
        while n > 0:
            rate = self.rate()
            if rate <= 0:
                return
            with self._lock:
                now = self._clock()
                # höchstens eine halbe Sekunde ansparen – sonst gäbe es nach einer Pause einen Ausbruch
                self._tokens = min(rate * 0.5, self._tokens + (now - self._last) * rate)
                self._last = now
                take = min(n, max(1.0, self._tokens)) if self._tokens >= 1 else 0
                if take:
                    self._tokens -= take
                    n -= int(take)
                    wait = 0.0
                else:
                    wait = min(0.25, (min(n, BLOCK) - self._tokens) / rate)
            if wait > 0:
                if cancel is not None and cancel.is_set():
                    return
                self._sleep(wait)
