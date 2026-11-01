"""„Aktuelle Übersicht“ (Recent) wie in GOG Galaxy 2.1 – die Daten ohne GTK.

* Widgets an-/abschalten und anordnen („Ansicht anpassen“): Liste ``[{"key", "enabled"}]`` in
  ``settings.recent_widgets``; neue Widgets (z. B. später Freunde über ``ui_registry.register_recent_widget``) kommen
  eingeschaltet ans Ende, unbekannte fallen heraus.
* „Meine Übersicht“: lokale Spielzeit heute, gestern, diese Woche (ab Montag), diesen Monat und je Tag der letzten
  14 Tage. Sitzungen über Mitternacht werden auf die Tage aufgeteilt.
* „Was als Nächstes spielen“: ein Vorschlag aus installierten, selten oder nie gespielten Spielen – je Tag fest
  (gleiche Wahl nach einem Neustart), „Neu mischen“ geht zum nächsten Kandidaten, „Überrasch mich“ wählt zufällig
  aus allen Spielen. Ein Vorschlag, den die Ansicht noch nicht gezeigt hat, gibt der Seitenleiste ein „Neu“.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Iterable

BUILTIN_WIDGETS = ("recently_played", "summary", "new_in_library", "next_to_play")
HISTOGRAM_DAYS = 14
RARELY_MINUTES = 120          # „selten gespielt“: weniger als 2 Stunden


# --------------------------------------------------------------------------- Widgets
def merge_order(saved: Any, available: Iterable[str]) -> list[dict]:
    """Gespeicherte Reihenfolge mit den vorhandenen Widgets abgleichen."""
    avail = list(dict.fromkeys(available))
    out: list[dict] = []
    seen: set[str] = set()
    for item in saved if isinstance(saved, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if key in avail and key not in seen:
            seen.add(key)
            out.append({"key": key, "enabled": bool(item.get("enabled", True))})
    out += [{"key": k, "enabled": True} for k in avail if k not in seen]
    return out


def move(order: list[dict], key: str, delta: int) -> list[dict]:
    """Widget um ``delta`` Plätze verschieben (an den Rändern bleibt es stehen)."""
    out = [dict(i) for i in order]
    idx = next((i for i, it in enumerate(out) if it["key"] == key), None)
    if idx is None:
        return out
    new = max(0, min(len(out) - 1, idx + delta))
    out.insert(new, out.pop(idx))
    return out


def set_enabled(order: list[dict], key: str, enabled: bool) -> list[dict]:
    return [{**i, "enabled": bool(enabled)} if i["key"] == key else dict(i) for i in order]


def enabled_keys(order: list[dict]) -> list[str]:
    return [i["key"] for i in order if i.get("enabled")]


# --------------------------------------------------------------------------- Meine Übersicht
@dataclass
class Summary:
    today: int = 0
    yesterday: int = 0
    week: int = 0
    month: int = 0
    days: list[tuple[date, int]] = field(default_factory=list)    # älteste zuerst

    @property
    def peak(self) -> int:
        return max((m for _d, m in self.days), default=0)


def _local(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)


def split_by_day(start: float, end: float, tz: tzinfo | None = None) -> dict[date, float]:
    """Minuten einer Sitzung je Kalendertag (Ortszeit bzw. ``tz``)."""
    out: dict[date, float] = {}
    if end <= start:
        return out
    cur = start
    while cur < end:
        d = _local(cur, tz)
        midnight = datetime.combine(d.date() + timedelta(days=1), datetime.min.time(), d.tzinfo)
        stop = min(end, midnight.timestamp())
        out[d.date()] = out.get(d.date(), 0.0) + (stop - cur) / 60.0
        cur = stop
    return out


def daily_totals(sessions: Iterable[tuple[float, float]], tz: tzinfo | None = None) -> dict[date, float]:
    totals: dict[date, float] = {}
    for start, end in sessions:
        for d, m in split_by_day(float(start), float(end), tz).items():
            totals[d] = totals.get(d, 0.0) + m
    return totals


def summarize(sessions: Iterable[tuple[float, float]], now: float, tz: tzinfo | None = None,
              days: int = HISTOGRAM_DAYS) -> Summary:
    """Spielzeit heute, gestern, diese Woche (Montag–heute), dieser Monat und je Tag der letzten ``days`` Tage."""
    totals = daily_totals(sessions, tz)
    today = _local(now, tz).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def total(since: date, until: date = today) -> int:
        return int(round(sum(m for d, m in totals.items() if since <= d <= until)))
    yesterday = today - timedelta(days=1)
    hist = [(today - timedelta(days=i), int(round(totals.get(today - timedelta(days=i), 0.0))))
            for i in range(days - 1, -1, -1)]
    return Summary(today=total(today), yesterday=total(yesterday, yesterday), week=total(week_start),
                   month=total(month_start), days=hist)


# --------------------------------------------------------------------------- Was als Nächstes spielen
@dataclass
class Candidate:
    id: str
    title: str
    minutes: int = 0
    last_played: float = 0.0
    installed_at: float = 0.0


def candidates(items: Iterable[Candidate], rarely: int = RARELY_MINUTES) -> list[Candidate]:
    """Installierte Spiele, nie oder selten gespielt: nie gespielte zuerst, dann die mit der wenigsten Spielzeit,
    bei Gleichstand die zuletzt installierten."""
    pool = [c for c in items if c.minutes < rarely]
    pool.sort(key=lambda c: (c.minutes > 0, c.minutes, -c.installed_at, c.title.lower(), c.id))
    return pool


def _shuffled(cands: list[Candidate], day: str) -> list[Candidate]:
    seed = int(hashlib.sha1(day.encode()).hexdigest()[:12], 16)
    order = list(cands)
    random.Random(seed).shuffle(order)
    # nie gespielte bleiben vorn – der Tag mischt nur innerhalb der Gruppen
    order.sort(key=lambda c: c.minutes > 0)
    return order


def daily_pick(cands: list[Candidate], day: str) -> Candidate | None:
    """Vorschlag des Tages (gleich nach einem Neustart, neu am nächsten Tag)."""
    order = _shuffled(cands, day)
    return order[0] if order else None


def reshuffle(cands: list[Candidate], current: str, day: str) -> Candidate | None:
    """„Neu mischen“: der nächste Kandidat nach ``current`` (ringsum), nie derselbe, solange es andere gibt."""
    order = _shuffled(cands, day)
    if not order:
        return None
    ids = [c.id for c in order]
    if current not in ids:
        return order[0]
    return order[(ids.index(current) + 1) % len(order)]


def surprise(pool: list[Candidate], exclude: str = "", rng: random.Random | None = None) -> Candidate | None:
    """„Überrasch mich“: ein zufälliges Spiel aus allen (außer dem gerade gezeigten)."""
    choices = [c for c in pool if c.id != exclude] or list(pool)
    if not choices:
        return None
    return (rng or random.Random()).choice(choices)


def is_new_suggestion(pick: str, seen: str) -> bool:
    """„Neu“-Kennzeichen in der Seitenleiste: es gibt einen Vorschlag, den die Ansicht noch nicht gezeigt hat."""
    return bool(pick) and pick != seen


def day_key(now: float, tz: tzinfo | None = None) -> str:
    return _local(now, tz).date().isoformat()


# --------------------------------------------------------------------------- aus dem Backend
def installed_candidates(backend: Any) -> list[Candidate]:
    """Installierte Spiele (ohne ausgeblendete und ohne Kopien) mit Spielzeit – Grundlage des Vorschlags."""
    gs_all = backend.game_settings.items()
    out = []
    for g in backend.registry.all():
        gs = gs_all.get(g.product_id)
        if getattr(g, "duplicate_of", "") or (gs is not None and gs.hidden):
            continue
        e = backend.entry(g.product_id)
        minutes, last = backend.playtime.get(g.product_id)
        out.append(Candidate(g.product_id, e.title if e else g.title, minutes, last, float(g.installed_at or 0)))
    return out


def library_pool(backend: Any) -> list[Candidate]:
    """„Überrasch mich“: alle Spiele – installierte und die der GOG-Bibliothek (ohne ausgeblendete)."""
    pool = {c.id: c for c in installed_candidates(backend)}
    for e in backend.visible_library():
        if e.id not in pool and getattr(e, "is_game", True):
            minutes, last = backend.playtime.get(e.id)
            pool[e.id] = Candidate(e.id, e.title, minutes, last)
    return list(pool.values())


def suggestion(backend: Any, now: float) -> Candidate | None:
    """Vorschlag des Tages für „Was als Nächstes spielen“."""
    return daily_pick(candidates(installed_candidates(backend)), day_key(now))
