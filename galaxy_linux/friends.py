"""Freunde wie in GOG Galaxy 2.1 – Zustand und Regeln ohne GTK.

* Abruf (``FriendsService.fetch``, im Hintergrund): Freundesliste (höchstens alle 10 Minuten neu), erhaltene
  Einladungen und die Präsenz aller Freunde bei jedem Abruf; fehlende Namen/Bilder aus dem öffentlichen Profil
  (users.gog.com, 7 Tage zwischengespeichert), Titel laufender Spiele aus der eigenen Bibliothek, GamesDB oder
  api.gog.com (zwischengespeichert). HTTP: ``api/friends.py``.
* Takt (``next_delay``): alle 2 Minuten, solange das Fenster sichtbar ist, alle 10 Minuten im Hintergrund oder im
  System-Tray, nie abgemeldet; nach Fehlern doppelt so lange (höchstens 30 Minuten).
* Aktivität (``ActivityLog``): was der Client selbst beobachtet hat – „ist jetzt online“, „spielt jetzt …“ – die
  letzten 50 Ereignisse und wann ein Freund zuletzt online gesehen wurde; ``friends_activity.json`` im Datenordner.
  Der erste Abruf nach dem Start meldet nichts (alle, die schon online sind, wären sonst „gerade gekommen“).
* Benachrichtigungen (``notices``): „Freundschaftsanfrage“, „Freund geht online“, „Freund startet ein Spiel“ – je Typ
  in Einstellungen → Benachrichtigungen wählbar (Vorgabe: Anfragen auf den Desktop und ins Benachrichtigungszentrum,
  die beiden anderen nur ins Benachrichtigungszentrum).
* Darstellung: Status-Text, Sortierung (Aktivität/Name), Gruppierung (Spiel/keine), Suche.
"""
from __future__ import annotations

from .i18n import N_, tr

import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable

from . import notifications as nt
from .api import friends as fa
from .api.client import ApiError, NotLoggedIn
from .api.friends import Friend, Invitation, PresenceInfo
from .jsonstore import read_json, write_json

log = logging.getLogger(__name__)

VISIBLE_INTERVAL = 120        # s, Fenster sichtbar
HIDDEN_INTERVAL = 600         # s, Fenster verborgen, minimiert oder im System-Tray
MAX_BACKOFF = 1800            # s, längste Pause nach wiederholten Fehlern
FRIENDS_MAX_AGE = 600         # s, Freundesliste selbst seltener als Präsenz und Einladungen
PROFILE_MAX_AGE = 7 * 86400
ACTIVITY_MAX = 50
SIDEBAR_MIN_WIDTH = 1501      # Galaxy: FriendsSidebarStateService.VISIBILITY_THRESHOLD

PLAYING, ONLINE, AWAY, OFFLINE = "playing", "online", "away", "offline"
SORTS = ("activity", "name")
GROUPS = ("none", "game")
NOT_PLAYING = ""              # Gruppe „Nicht im Spiel“ bei „Nach gespieltem Spiel gruppieren“

# Benachrichtigungstypen (Einstellungen → Benachrichtigungen) – Bezeichnungen wie Galaxy 2.1
KIND_INVITE, KIND_ONLINE, KIND_GAME = "friend-invite", "friend-online", "friend-game"
nt.register_type(nt.NotificationType("friend_invite", N_("Freundschaftsanfrage"), "notify_friend_invite",
                                     default=nt.MODE_BOTH, order=50,
                                     subtitle=N_("Jemand möchte dein Freund sein")))
nt.register_type(nt.NotificationType("friend_online", N_("Freund geht online"), "notify_friend_online",
                                     default=nt.MODE_CENTER, order=60,
                                     subtitle=N_("Ein Freund ist jetzt online")))
nt.register_type(nt.NotificationType("friend_playing", N_("Freund startet ein Spiel"), "notify_friend_playing",
                                     default=nt.MODE_CENTER, order=70,
                                     subtitle=N_("Ein Freund hat ein Spiel gestartet")))
nt.register_kind(KIND_INVITE, "friend_invite")
nt.register_kind(KIND_ONLINE, "friend_online")
nt.register_kind(KIND_GAME, "friend_playing")


# --------------------------------------------------------------------------- Takt
def next_delay(visible: bool, logged_in: bool, failures: int = 0) -> int | None:
    """Sekunden bis zum nächsten Abruf; None = gar nicht (abgemeldet)."""
    if not logged_in:
        return None
    base = VISIBLE_INTERVAL if visible else HIDDEN_INTERVAL
    if failures > 0:
        return min(MAX_BACKOFF, base * 2 ** min(failures, 5))
    return base


def due_in(last_attempt: float, now: float, visible: bool, logged_in: bool, failures: int = 0) -> float | None:
    """Restzeit bis zum nächsten fälligen Abruf (0 = sofort), z. B. wenn das Fenster wieder sichtbar wird."""
    delay = next_delay(visible, logged_in, failures)
    if delay is None:
        return None
    if last_attempt <= 0:
        return 0.0
    return max(0.0, delay - (now - last_attempt))


def sidebar_fits(width: int, min_width: int, sidebar_width: int) -> bool:
    """Freunde-Seitenleiste zeigen? Wie Galaxy erst ab 1501 px Fensterbreite – und nur, wenn sie neben der
    Mindestbreite des übrigen Inhalts Platz hat (sie darf das Fenster nie breiter erzwingen)."""
    return width >= SIDEBAR_MIN_WIDTH and width - max(0, min_width) >= sidebar_width


# --------------------------------------------------------------------------- Status, Sortierung, Gruppen
def status_kind(p: PresenceInfo | None) -> str:
    if p is None or not p.online:
        return OFFLINE
    if p.game_id:
        return PLAYING
    return AWAY if p.away else ONLINE


def is_online(p: PresenceInfo | None) -> bool:
    return p is not None and p.online


def status_text(p: PresenceInfo | None, title: str, last_seen: float, rel: Callable[[float], str]) -> str:
    """„Online“, „Abwesend“, „Spielt – Titel“ (mit Status im Spiel), „Zuletzt online …“ oder „Offline“."""
    kind = status_kind(p)
    if kind == PLAYING:
        title = title or (p.title if p else "")
        if not title:
            return tr('Im Spiel')
        if p is not None and p.status:
            return tr('Spielt – {0} – {1}').format(title, p.status)
        return tr('Spielt – {0}').format(title)
    if kind == AWAY:
        return tr('Abwesend')
    if kind == ONLINE:
        return tr('Online')
    if last_seen > 0:
        return tr('Zuletzt online {0}').format(rel(last_seen))
    return tr('Offline')


def _name_key(f: Friend) -> tuple[str, str]:
    return (f.username or "").casefold(), f.user_id


_RANK = {PLAYING: 0, ONLINE: 1, AWAY: 2, OFFLINE: 3}


def sort_friends(friends: Iterable[Friend], presence: dict[str, PresenceInfo], last_seen: dict[str, float],
                 mode: str = "activity") -> tuple[list[Friend], list[Friend]]:
    """(online, offline). „Aktivität“: im Spiel, online, abwesend; offline nach „zuletzt online“. „Name“: alphabetisch."""
    online = [f for f in friends if is_online(presence.get(f.user_id))]
    ids = {f.user_id for f in online}
    offline = [f for f in friends if f.user_id not in ids]
    if mode == "name":
        return sorted(online, key=_name_key), sorted(offline, key=_name_key)
    online.sort(key=lambda f: (_RANK[status_kind(presence.get(f.user_id))], *_name_key(f)))
    offline.sort(key=lambda f: (-float(last_seen.get(f.user_id, 0) or 0), *_name_key(f)))
    return online, offline


def group_online(online: list[Friend], presence: dict[str, PresenceInfo], titles: dict[str, str],
                 mode: str = "none") -> list[tuple[str, list[Friend]]]:
    """Online-Freunde gruppieren: „none“ = eine Gruppe ohne Namen; „game“ = je Spiel (nach Titel), danach
    ``NOT_PLAYING`` für alle, die gerade nicht spielen. Die Reihenfolge innerhalb bleibt."""
    if mode != "game":
        return [("", list(online))] if online else []
    games: dict[str, list[Friend]] = {}
    rest: list[Friend] = []
    for f in online:
        p = presence.get(f.user_id)
        if p is not None and p.playing:
            title = titles.get(p.game_id) or p.title or p.game_id
            games.setdefault(title, []).append(f)
        else:
            rest.append(f)
    out = [(t, games[t]) for t in sorted(games, key=str.casefold)]
    if rest:
        out.append((NOT_PLAYING, rest))
    return out


def matches(name: str, query: str) -> bool:
    q = (query or "").strip().casefold()
    return not q or q in (name or "").casefold()


def filter_friends(friends: Iterable[Friend], query: str) -> list[Friend]:
    return [f for f in friends if matches(f.username, query)]


# --------------------------------------------------------------------------- Aktivität
@dataclass
class ActivityEvent:
    ts: float
    user_id: str
    username: str
    kind: str                     # online | game
    game_id: str = ""
    title: str = ""
    avatar: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> "ActivityEvent | None":
        if not isinstance(d, dict):
            return None
        names = {f.name for f in fields(cls)}
        try:
            e = cls(**{k: v for k, v in d.items() if k in names})
            e.ts = float(e.ts)
        except (TypeError, ValueError):
            return None
        return e if e.user_id and e.kind in ("online", "game") else None


class ActivityLog:
    """Die letzten ``max_items`` beobachteten Ereignisse (neueste zuerst) und „zuletzt online gesehen“ je Freund."""

    def __init__(self, path: Path | None, max_items: int = ACTIVITY_MAX) -> None:
        self.path = Path(path) if path else None
        self.max_items = max_items
        self._lock = threading.Lock()
        raw = read_json(self.path, {}) if self.path else {}
        raw = raw if isinstance(raw, dict) else {}
        self._events = [e for e in (ActivityEvent.from_dict(d) for d in raw.get("events") or []) if e is not None]
        self._events.sort(key=lambda e: -e.ts)
        del self._events[max_items:]
        seen = raw.get("last_seen")
        self.last_seen: dict[str, float] = {str(k): float(v) for k, v in seen.items()
                                            if isinstance(v, (int, float)) and not isinstance(v, bool)} \
            if isinstance(seen, dict) else {}

    def events(self) -> list[ActivityEvent]:
        with self._lock:
            return list(self._events)

    def record(self, events: Iterable[ActivityEvent]) -> int:
        new = [e for e in events if e.user_id]
        if not new:
            return 0
        with self._lock:
            self._events = sorted(new + self._events, key=lambda e: -e.ts)[: self.max_items]
        return len(new)

    def seen_online(self, user_ids: Iterable[str], now: float) -> None:
        with self._lock:
            for uid in user_ids:
                self.last_seen[uid] = now

    def clear(self) -> None:
        with self._lock:
            self._events = []
            self.last_seen = {}
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            data = {"events": [asdict(e) for e in self._events], "last_seen": dict(self.last_seen)}
        try:
            write_json(self.path, data)
        except OSError as exc:
            log.warning("Aktivität der Freunde nicht gespeichert: %s", exc)


def presence_events(prev: dict[str, PresenceInfo] | None, cur: dict[str, PresenceInfo],
                    friends: dict[str, Friend], titles: dict[str, str], now: float) -> list[ActivityEvent]:
    """Was sich seit dem letzten Abruf geändert hat: online gekommen, ein (anderes) Spiel gestartet. Wer online kommt
    und schon spielt, ergibt nur „spielt jetzt“. Ohne vorigen Stand (erster Abruf) und für neue Freunde: nichts."""
    if prev is None:
        return []
    out: list[ActivityEvent] = []
    for uid, p in cur.items():
        if uid not in prev or not p.online:
            continue
        was = prev[uid]
        f = friends.get(uid) or Friend(uid)
        if p.game_id and (not was.online or was.game_id != p.game_id):
            out.append(ActivityEvent(now, uid, f.username, "game", p.game_id, titles.get(p.game_id) or p.title,
                                     f.avatar))
        elif not was.online:
            out.append(ActivityEvent(now, uid, f.username, "online", avatar=f.avatar))
    return out


# --------------------------------------------------------------------------- Benachrichtigungen
@dataclass
class Notice:
    kind: str
    title: str
    body: str = ""
    action: str = ""
    key: str = ""
    product_id: str = ""


def notices(events: Iterable[ActivityEvent], invitations: Iterable[Invitation],
            owned: Iterable[str] = ()) -> list[Notice]:
    """Meldungen für das Benachrichtigungszentrum. Einladungen tragen ihre ID als Schlüssel – das Zentrum legt jede
    nur einmal an (auch nach einem Neustart); Ereignisse tragen ihren Zeitpunkt."""
    owned = set(owned)
    out: list[Notice] = []
    for inv in invitations:
        name = inv.username or inv.user_id
        out.append(Notice(KIND_INVITE, tr('{0} möchte dein Freund sein.').format(name),
                          tr('Freundschaftseinladung erhalten'), "view:friends", f"friend-invite:{inv.id}"))
    for e in events:
        name = e.username or e.user_id
        if e.kind == "game":
            action = f"game:{e.game_id}" if e.game_id in owned else "view:friends"
            out.append(Notice(KIND_GAME, tr('{0} spielt jetzt').format(name), e.title, action,
                              f"friend-game:{e.user_id}:{e.game_id}:{int(e.ts)}",
                              e.game_id if e.game_id in owned else ""))
        else:
            out.append(Notice(KIND_ONLINE, name, tr('Jetzt online'), "view:friends", f"friend-online:{e.user_id}:{int(e.ts)}"))
    return out


# --------------------------------------------------------------------------- Abruf
@dataclass
class Snapshot:
    friends: list[Friend] = field(default_factory=list)
    invitations: list[Invitation] = field(default_factory=list)
    presence: dict[str, PresenceInfo] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    friends_at: float = 0.0

    def presence_of(self, uid: str) -> PresenceInfo | None:
        return self.presence.get(uid)

    def online(self) -> list[Friend]:
        return [f for f in self.friends if is_online(self.presence.get(f.user_id))]

    def playing(self) -> list[tuple[Friend, PresenceInfo]]:
        return [(f, self.presence[f.user_id]) for f in self.friends
                if self.presence.get(f.user_id) is not None and self.presence[f.user_id].playing]


class ProfileCache:
    """Öffentliche Profile (Name, Bild) – im Speicher und als JSON-Datei, 7 Tage gültig."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        raw = read_json(self.path, {}) if self.path else {}
        self._data: dict[str, dict] = {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def get(self, api: Any, uid: str) -> fa.Profile | None:
        now = self.clock()
        with self._lock:
            hit = self._data.get(uid)
        if hit and now - float(hit.get("at", 0) or 0) < PROFILE_MAX_AGE:
            return fa.Profile(uid, str(hit.get("username") or ""), str(hit.get("avatar") or ""))
        try:
            prof = fa.fetch_profile(api, uid)
        except ApiError as exc:
            log.debug("Profil %s nicht abrufbar: %s", uid, exc)
            return fa.Profile(uid, str(hit.get("username") or ""), str(hit.get("avatar") or "")) if hit else None
        prof = prof or fa.Profile(uid)          # unbekannt (404): ebenfalls merken, nicht bei jedem Abruf fragen
        with self._lock:
            self._data[uid] = {"username": prof.username, "avatar": prof.avatar, "at": now}
            data = dict(self._data)
        if self.path is not None:
            try:
                write_json(self.path, data)
            except OSError as exc:
                log.debug("Profile nicht gespeichert: %s", exc)
        return prof


class FriendsService:
    """Abruf und Auswertung. ``fetch`` läuft im Hintergrund (Netz), ``apply`` danach im Hauptthread."""

    def __init__(self, api: Any, activity_path: Path | None = None, profile_path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.api = api
        self.clock = clock
        self.activity = ActivityLog(activity_path)
        self.profiles = ProfileCache(profile_path, clock)
        self.snapshot: Snapshot | None = None
        self.failures = 0
        self.last_attempt = 0.0
        self.last_error = ""
        self._titles: dict[str, str] = {}

    # Abruf (Hintergrund)
    def fetch(self, force: bool = False, known_titles: dict[str, str] | None = None, lang: str = "de-DE") -> Snapshot:
        api = self.api
        if not getattr(api, "logged_in", False):
            raise NotLoggedIn()
        now = self.clock()
        prev = self.snapshot
        if force or prev is None or now - prev.friends_at >= FRIENDS_MAX_AGE:
            friends, friends_at = fa.fetch_friends(api), now
        else:
            friends, friends_at = [Friend(**asdict(f)) for f in prev.friends], prev.friends_at
        try:
            invitations = fa.fetch_invitations(api)
        except NotLoggedIn:
            raise
        except ApiError as exc:             # Einladungen sind Nebensache – Freunde trotzdem zeigen
            log.info("Einladungen nicht abrufbar: %s", exc)
            invitations = list(prev.invitations) if prev else []
        presence = fa.fetch_statuses(api, [f.user_id for f in friends], lang) if friends else {}
        for person in [*friends, *invitations]:
            if not person.username or not person.avatar:
                prof = self.profiles.get(api, person.user_id)
                if prof is not None:
                    person.username = person.username or prof.username
                    person.avatar = person.avatar or prof.avatar
        titles: dict[str, str] = {}
        for p in presence.values():
            if p.playing:
                titles[p.game_id] = self.title(p.game_id, p.title, known_titles or {}, lang)
        return Snapshot(friends, invitations, presence, titles, now, friends_at)

    def title(self, game_id: str, hint: str = "", known: dict[str, str] | None = None, lang: str = "de-DE") -> str:
        """Titel eines Spiels: eigene Bibliothek, Zwischenspeicher, Präsenz-Angabe, GamesDB, api.gog.com."""
        if known and known.get(game_id):
            return known[game_id]
        if game_id in self._titles:
            return self._titles[game_id] or hint
        title = ""
        api = self.api
        try:
            if hasattr(api, "gamesdb"):
                title = fa.game_title(api.gamesdb(game_id), None, lang)
            if not title and hasattr(api, "product"):
                title = fa.game_title(None, api.product(game_id), lang)
        except ApiError as exc:
            log.debug("Titel für %s nicht abrufbar: %s", game_id, exc)
            return hint
        self._titles[game_id] = title
        return title or hint

    # Auswertung (Hauptthread)
    def apply(self, snap: Snapshot, owned: Iterable[str] = ()) -> list[Notice]:
        prev = self.snapshot.presence if self.snapshot is not None else None
        events = presence_events(prev, snap.presence, {f.user_id: f for f in snap.friends}, snap.titles, snap.fetched_at)
        self.activity.record(events)
        online = [uid for uid, p in snap.presence.items() if p.online]
        self.activity.seen_online(online, snap.fetched_at)
        if events or online:
            self.activity.save()
        self.snapshot = snap
        self.failures = 0
        self.last_error = ""
        self.last_attempt = snap.fetched_at
        return notices(events, snap.invitations, owned)

    def failed(self, exc: Exception) -> None:
        self.failures += 1
        self.last_attempt = self.clock()
        self.last_error = str(exc)

    def clear(self) -> None:
        """Abmelden: Stand, Fehler und beobachtete Aktivität vergessen."""
        self.snapshot = None
        self.failures = 0
        self.last_attempt = 0.0
        self.last_error = ""
        self.activity.clear()

    # Änderungen nach einer erfolgreichen Aktion (ohne neuen Abruf)
    def drop_invitation(self, user_id: str) -> None:
        if self.snapshot is not None:
            self.snapshot.invitations = [i for i in self.snapshot.invitations if i.user_id != user_id]

    def drop_friend(self, user_id: str) -> None:
        if self.snapshot is not None:
            self.snapshot.friends = [f for f in self.snapshot.friends if f.user_id != user_id]
            self.snapshot.presence.pop(user_id, None)

    def add_friend(self, inv: Invitation) -> None:
        if self.snapshot is not None and all(f.user_id != inv.user_id for f in self.snapshot.friends):
            self.snapshot.friends.append(Friend(inv.user_id, inv.username, inv.avatar, inv.is_employee))
            self.snapshot.friends_at = 0.0          # beim nächsten Abruf die echte Liste holen
