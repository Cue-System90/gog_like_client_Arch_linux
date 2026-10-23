"""GOG-Freunde wie in GOG Galaxy 2.1: Freundesliste, Einladungen, Präsenz der Freunde, Profile – HTTP und Auswertung.

Endpunkte (Belege: gog-apis.md §8, gogapidocs ``galaxy.rst``, und die Web-Oberfläche der lokal installierten
GOG Galaxy 2.1.10.56 unter ``Program Files/GOG Galaxy/web``):

* Freundesliste: ``GET chat.gog.com/users/<ich>/friends_main`` (Galaxy 2.1, ``vendor.js`` FriendsFetcherService) →
  ``items[]{user_id, username, is_employee, images{medium, medium_2x}}``; ältere Fassung ``…/friends`` (gogapidocs)
  dient als Rückfall bei 404.
* Erhaltene Einladungen: ``GET chat.gog.com/users/<ich>/invitations`` (``friendsListWindow.js``
  FriendshipInvitesFetcher) → ``items[]{id, friend_user_id, friend_username, friend_images, friend_is_employee,
  date_created}``.
* Annehmen ``POST users.gog.com/users/<ich>/friends/<id>`` (200), Ablehnen ``DELETE users.gog.com/users/<ich>/invites/
  <id>`` (204), Entfernen ``DELETE users.gog.com/users/<ich>/friends/<id>`` (204; Galaxys Kontextmenü nutzt
  ``DELETE chat.gog.com/users/<ich>/friends/<id>``, hier der Rückfall) – alles aus Galaxys
  FriendshipStateManagerService. Live mit einem Konto geprüft ist davon nichts; Fehler kommen als ``FriendsError``.
* Präsenz: ``GET presence.gog.com/statuses?user_id=a,b,c`` (``vendor.js`` PresenceFetcherService) → ``items[]{user_id,
  client_id, game_id, data{status, metadata, connect}}``; wer fehlt, ist offline. „Abwesend“ (``presence``-Feld) ist
  nicht belegt und wird nur erkannt, falls GOG es mitliefert.
* Profil: ``GET users.gog.com/users/<id>`` (öffentlich) → ``{id, username, avatar{small … large_2x}}``.
* Chat: Galaxy lädt ``chat.gog.com/users/<ich>/chat?_version=2&isEmbedFrom=galaxy`` (``client.js`` ChatHolderService),
  für eine Unterhaltung mit ``second_participant_id/name/platform/avatar`` (ChatHelpers).
* Profilseite: ``https://www.gog.com/u/<name>`` (Galaxy ProfileLauncher, ohne Sprachpräfix).

Keine GTK-Abhängigkeit; ``api`` ist ein ``GogApi`` (oder in Tests ein Ersatz mit ``get``/``request``).
"""
from __future__ import annotations

from ..i18n import tr

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from .client import ApiError, NotLoggedIn
from .models import _localized

log = logging.getLogger(__name__)

CHAT = "https://chat.gog.com"
USERS = "https://users.gog.com"
PRESENCE = "https://presence.gog.com"
PRESENCE_BATCH = 100          # Benutzer je Abfrage (GOG liefert höchstens 250 Einträge)
PLATFORM = "gog"


class FriendsError(ApiError):
    """Freunde-Anfrage abgelehnt; ``status`` = HTTP-Status (0 = unbekannt)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Friend:
    user_id: str
    username: str = ""
    avatar: str = ""
    is_employee: bool = False


@dataclass
class Invitation:
    id: str
    user_id: str                  # wer einlädt (friend_user_id)
    username: str = ""
    avatar: str = ""
    created: float = 0.0
    is_employee: bool = False


@dataclass
class PresenceInfo:
    user_id: str
    online: bool = False
    away: bool = False
    game_id: str = ""             # Produkt-ID des laufenden Spiels
    status: str = ""              # Status im Spiel („data.status“), z. B. „Kapitel 3“
    title: str = ""               # Spieltitel, falls GOG ihn mitliefert
    client_id: str = ""

    @property
    def playing(self) -> bool:
        return self.online and bool(self.game_id)


@dataclass
class Profile:
    user_id: str
    username: str = ""
    avatar: str = ""


# --------------------------------------------------------------------------- Auswertung
def _id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text if text and text.lower() not in ("none", "null", "0") else ""


def _items(data: Any) -> list[dict]:
    items = data.get("items") if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def avatar_url(images: Any) -> str:
    """Mittleres Profilbild aus ``images``/``avatar`` (dict mit medium, medium_2x, small …) oder einer Adresse."""
    if isinstance(images, str):
        return images
    if isinstance(images, dict):
        for key in ("medium", "medium_2x", "small", "small_2x", "large", "sdk_img_64"):
            v = images.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def parse_time(value: Any) -> float:
    """Zeitpunkt aus Unixzeit (auch Millisekunden) oder ISO-8601; 0.0, wenn unlesbar."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            ts = float(text)
        else:
            try:
                ts = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
    else:
        return 0.0
    return ts / 1000.0 if ts > 1e12 else ts


def parse_friends(data: Any) -> list[Friend]:
    out: list[Friend] = []
    seen: set[str] = set()
    for it in _items(data):
        uid = _id(it.get("user_id") or it.get("id") or it.get("userId"))
        if not uid or uid in seen:
            continue
        seen.add(uid)
        name = it.get("username") or it.get("userName") or ""
        out.append(Friend(uid, str(name), avatar_url(it.get("images") or it.get("avatar") or it.get("avatars")),
                          bool(it.get("is_employee"))))
    return out


def parse_invitations(data: Any) -> list[Invitation]:
    out: list[Invitation] = []
    seen: set[str] = set()
    for it in _items(data):
        uid = _id(it.get("friend_user_id") or it.get("user_id") or it.get("from_user_id"))
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append(Invitation(id=_id(it.get("id")) or uid, user_id=uid,
                              username=str(it.get("friend_username") or it.get("username") or ""),
                              avatar=avatar_url(it.get("friend_images") or it.get("images")),
                              created=parse_time(it.get("date_created") or it.get("created_date")),
                              is_employee=bool(it.get("friend_is_employee"))))
    return out


def parse_statuses(data: Any, user_ids: list[str] | None = None, lang: str = "de-DE") -> dict[str, PresenceInfo]:
    """Antwort von ``presence.gog.com/statuses``: jeder genannte Benutzer ist online; angefragte, die fehlen, offline."""
    out = {uid: PresenceInfo(uid) for uid in (user_ids or [])}
    for it in _items(data):
        uid = _id(it.get("user_id"))
        if not uid:
            continue
        d = it.get("data") if isinstance(it.get("data"), dict) else {}
        state = str(it.get("presence") or d.get("presence") or "online").lower()
        if state in ("offline", "invisible", "unknown"):
            out[uid] = PresenceInfo(uid)
            continue
        status = d.get("status") if isinstance(d.get("status"), str) else ""
        if not status and isinstance(d.get("in_game_status"), str):
            status = d["in_game_status"]
        title = it.get("game_title") or d.get("game_title") or ""
        out[uid] = PresenceInfo(uid, online=True, away=state == "away", game_id=_id(it.get("game_id") or d.get("game_id")),
                                status=status.strip(), title=_localized(title, lang) if title else "",
                                client_id=_id(it.get("client_id")))
    return out


def parse_profile(data: Any) -> Profile | None:
    if not isinstance(data, dict):
        return None
    uid = _id(data.get("id") or data.get("user_id"))
    if not uid:
        return None
    return Profile(uid, str(data.get("username") or ""), avatar_url(data.get("avatar") or data.get("images")))


def chat_url(user_id: str, friend: Friend | None = None) -> str:
    """Adresse der Chat-Web-App wie in Galaxy; mit ``friend`` öffnet sie die Unterhaltung mit diesem Freund."""
    url = f"{CHAT}/users/{quote(str(user_id))}/chat?_version=2&isEmbedFrom=galaxy"
    if friend is not None and friend.user_id and friend.username:
        url += "&" + urlencode({"second_participant_id": friend.user_id, "second_participant_name": friend.username,
                                "second_participant_platform": PLATFORM, "second_participant_avatar": friend.avatar})
    return url


def profile_path(username: str) -> str:
    """Pfad der Profilseite auf gog.com (wie Galaxys „Profil ansehen“)."""
    return f"/u/{quote(username, safe='')}"


def game_title(gamesdb: Any = None, product: Any = None, lang: str = "de-DE") -> str:
    """Spieltitel aus einer GamesDB- oder api.gog.com-Produktantwort."""
    if isinstance(gamesdb, dict):
        title = _localized(gamesdb.get("title"), lang) or _localized((gamesdb.get("game") or {}).get("title"), lang)
        if title:
            return str(title)
    if isinstance(product, dict) and product.get("title"):
        return str(product["title"])
    return ""


# --------------------------------------------------------------------------- Anfragen
def _me(api: Any) -> str:
    uid = _id(getattr(api, "user_id", ""))
    if not getattr(api, "logged_in", False) or not uid:
        raise NotLoggedIn()
    return uid


def _json(r: Any, what: str) -> Any:
    if r.status_code == 401:
        raise NotLoggedIn()
    if not r.ok:
        raise FriendsError(tr('{0}: HTTP {1}').format(what, r.status_code), r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise FriendsError(tr('Ungültige JSON-Antwort von {0}').format(what)) from exc


def fetch_friends(api: Any) -> list[Friend]:
    me = _me(api)
    r = api.get(f"{CHAT}/users/{me}/friends_main", timeout=20, retries=1)
    if r.status_code == 404:
        r = api.get(f"{CHAT}/users/{me}/friends", timeout=20, retries=1)
    return parse_friends(_json(r, "chat.gog.com friends"))


def fetch_invitations(api: Any) -> list[Invitation]:
    me = _me(api)
    r = api.get(f"{CHAT}/users/{me}/invitations", timeout=20, retries=1)
    if r.status_code == 404:
        return []
    return parse_invitations(_json(r, "chat.gog.com invitations"))


def fetch_statuses(api: Any, user_ids: list[str], lang: str = "de-DE",
                   batch: int = PRESENCE_BATCH) -> dict[str, PresenceInfo]:
    """Präsenz aller Freunde, in Paketen zu ``batch`` Benutzern."""
    _me(api)
    ids = list(dict.fromkeys(u for u in user_ids if u))
    out: dict[str, PresenceInfo] = {}
    for i in range(0, len(ids), max(1, batch)):
        part = ids[i:i + batch]
        r = api.get(f"{PRESENCE}/statuses", params={"user_id": ",".join(part)}, timeout=20, retries=1)
        out.update(parse_statuses(_json(r, "presence.gog.com statuses"), part, lang))
    return out


def fetch_profile(api: Any, user_id: str) -> Profile | None:
    r = api.get(f"{USERS}/users/{quote(str(user_id))}", authenticated=False, timeout=15, retries=1)
    if r.status_code == 404:
        return None
    return parse_profile(_json(r, "users.gog.com"))


def _write(api: Any, method: str, url: str) -> Any:
    kw: dict[str, Any] = {"json": {}} if method == "POST" else {}
    r = api.request(method, url, timeout=20, retries=0, **kw)
    if r.status_code == 401:
        raise NotLoggedIn()
    return r


def accept_invitation(api: Any, user_id: str) -> None:
    """Einladung annehmen (Galaxy: ``POST users/<ich>/friends/<id>`` → 200)."""
    me = _me(api)
    r = _write(api, "POST", f"{USERS}/users/{me}/friends/{quote(str(user_id))}")
    if not r.ok:
        raise FriendsError(tr('Die Einladung konnte nicht angenommen werden (HTTP {0}).').format(r.status_code),
                           r.status_code)


def decline_invitation(api: Any, user_id: str) -> None:
    """Einladung ablehnen (Galaxy: ``DELETE users/<ich>/invites/<id>`` → 204)."""
    me = _me(api)
    r = _write(api, "DELETE", f"{USERS}/users/{me}/invites/{quote(str(user_id))}")
    if not r.ok:
        raise FriendsError(tr('Die Einladung konnte nicht abgelehnt werden (HTTP {0}).').format(r.status_code),
                           r.status_code)


def remove_friend(api: Any, user_id: str) -> None:
    """Aus der Freundesliste entfernen (Galaxy: ``DELETE users/<ich>/friends/<id>`` → 204, Rückfall chat.gog.com)."""
    me = _me(api)
    fid = quote(str(user_id))
    r = _write(api, "DELETE", f"{USERS}/users/{me}/friends/{fid}")
    if r.status_code in (404, 405):
        r = _write(api, "DELETE", f"{CHAT}/users/{me}/friends/{fid}")
    if not r.ok:
        raise FriendsError(tr('Der Freund konnte nicht entfernt werden (HTTP {0}).').format(r.status_code),
                           r.status_code)
