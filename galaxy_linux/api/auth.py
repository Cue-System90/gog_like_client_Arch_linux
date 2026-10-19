"""OAuth2-Login gegen auth.gog.com mit den öffentlichen Galaxy-Client-Zugangsdaten.

Ablauf wie im offiziellen Client: Login-Seite im Browser, Weiterleitung auf
embed.gog.com/on_login_success?code=..., Code gegen Access-/Refresh-Token tauschen.
"""
from __future__ import annotations

from ..i18n import tr

import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass

import requests

from .. import USER_AGENT, paths
from ..jsonstore import read_json, write_json

log = logging.getLogger(__name__)

# Öffentliche Zugangsdaten des GOG-Galaxy-Clients (dieselben wie bei Heroic, Lutris, Minigalaxy).
CLIENT_ID = "46899977096215655"
CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"
AUTH_URL = (
    "https://auth.gog.com/auth?client_id=" + CLIENT_ID
    + "&redirect_uri=" + urllib.parse.quote(REDIRECT_URI, safe="")
    + "&response_type=code&layout=client2"
)
TOKEN_URL = "https://auth.gog.com/token"
_HEADERS = {"User-Agent": USER_AGENT}


class AuthError(Exception):
    pass


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str = ""
    session_id: str = ""
    client_id: str = CLIENT_ID

    def expired(self, margin: int = 120) -> bool:
        return time.time() >= self.expires_at - margin

    @classmethod
    def from_token_response(cls, d: dict, client_id: str = CLIENT_ID) -> "Credentials":
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_at=time.time() + int(d.get("expires_in", 3600)),
            user_id=str(d.get("user_id", "")),
            session_id=str(d.get("session_id", "")),
            client_id=client_id,
        )


def parse_code(text: str) -> str | None:
    """Akzeptiert den rohen Code oder die komplette Weiterleitungs-URL."""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return None
    if "code=" in text:
        try:
            q = urllib.parse.urlparse(text).query
            code = urllib.parse.parse_qs(q).get("code", [None])[0]
            if code:
                return code
        except ValueError:
            pass
        m = re.search(r"code=([A-Za-z0-9_\-]+)", text)
        return m.group(1) if m else None
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", text):
        return text
    return None


def _token_request(params: dict, client_id: str = CLIENT_ID) -> Credentials:
    log.debug("Token-Anfrage (%s)", params.get("grant_type"))
    try:
        r = requests.get(TOKEN_URL, params=params, headers=_HEADERS, timeout=30)
    except requests.RequestException as exc:
        raise AuthError(tr('Verbindung zu auth.gog.com fehlgeschlagen: {0}').format(exc)) from exc
    log.debug("Token-Antwort HTTP %s", r.status_code)
    if r.status_code != 200:
        detail, code = "", ""
        try:
            j = r.json()
            detail = j.get("error_description") or ""
            code = j.get("error") or ""
        except ValueError:
            detail = r.text[:200]
        raise AuthError(_friendly_error(params.get("grant_type", ""), code, detail, r.status_code))
    try:
        return Credentials.from_token_response(r.json(), client_id)
    except (KeyError, ValueError) as exc:
        raise AuthError(tr('Unerwartete Antwort von auth.gog.com')) from exc


def _friendly_error(grant_type: str, code: str, detail: str, status: int) -> str:
    d = (detail or "").lower()
    if "code doesn't exist" in d or "invalid for the client" in d or code == "invalid_grant" and grant_type == "authorization_code":
        return (tr('Der Anmeldecode ist ungültig, abgelaufen oder wurde bereits verwendet. Bitte die GOG-Login-Seite erneut öffnen, anmelden und die neue Adresse einfügen.'))
    if code == "invalid_client":
        return tr('GOG akzeptiert die Client-Zugangsdaten nicht. Bitte den Client aktualisieren.')
    if grant_type == "refresh_token":
        return tr('Die Sitzung ist abgelaufen – bitte neu bei GOG anmelden.')
    return tr('GOG-Login abgelehnt (HTTP {0}): {1}').format(status, detail or code or tr('unbekannter Fehler'))


def exchange_code(code: str) -> Credentials:
    return _token_request({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })


def refresh(creds: Credentials) -> Credentials:
    if not creds.refresh_token:
        raise AuthError(tr('Kein Refresh-Token vorhanden – bitte neu anmelden.'))
    new = _token_request({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
    })
    if not new.user_id:
        new.user_id = creds.user_id
    return new


def game_token(user_refresh_token: str, client_id: str, client_secret: str) -> Credentials:
    """Spiel-spezifisches Token (für Erfolge/Cloud-Saves), wie es Galaxy für jedes Spiel holt."""
    return _token_request({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": user_refresh_token,
        "without_new_session": 1,
    }, client_id=client_id)


class CredentialStore:
    """Speichert Tokens in ~/.config/galaxy-linux/credentials.json (0600) oder optional im Schlüsselbund."""

    _SECRET_LABEL = tr('galaxy-linux – GOG-Zugangsdaten')

    def __init__(self, use_keyring: bool = False) -> None:
        self.use_keyring = use_keyring
        self._schema = None
        if use_keyring:
            try:
                import gi
                gi.require_version("Secret", "1")
                from gi.repository import Secret  # type: ignore
                self._Secret = Secret
                self._schema = Secret.Schema.new(
                    "org.galaxylinux.Client", Secret.SchemaFlags.NONE,
                    {"account": Secret.SchemaAttributeType.STRING},
                )
            except Exception as exc:  # pragma: no cover - abhängig vom System
                log.warning("Schlüsselbund nicht verfügbar (%s), nutze Datei.", exc)
                self.use_keyring = False

    def load(self) -> Credentials | None:
        raw = None
        if self.use_keyring and self._schema is not None:
            try:
                raw = self._Secret.password_lookup_sync(self._schema, {"account": "gog"}, None)
            except Exception as exc:  # pragma: no cover
                log.warning("Schlüsselbund-Lesefehler: %s", exc)
        if raw is None:
            data = read_json(paths.CREDENTIALS_FILE, None)
        else:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        if not data or not data.get("access_token"):
            return None
        try:
            return Credentials(**{k: data[k] for k in ("access_token", "refresh_token", "expires_at") }
                               | {k: data.get(k, "") for k in ("user_id", "session_id")}
                               | {"client_id": data.get("client_id", CLIENT_ID)})
        except (KeyError, TypeError):
            return None

    def save(self, creds: Credentials) -> None:
        payload = asdict(creds)
        if self.use_keyring and self._schema is not None:
            try:
                self._Secret.password_store_sync(
                    self._schema, {"account": "gog"}, self._Secret.COLLECTION_DEFAULT,
                    self._SECRET_LABEL, json.dumps(payload), None)
                return
            except Exception as exc:  # pragma: no cover
                log.warning("Schlüsselbund-Schreibfehler (%s), nutze Datei.", exc)
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        write_json(paths.CREDENTIALS_FILE, payload, mode=0o600)

    def clear(self) -> None:
        if self.use_keyring and self._schema is not None:
            try:
                self._Secret.password_clear_sync(self._schema, {"account": "gog"}, None)
            except Exception:  # pragma: no cover
                pass
        try:
            os.unlink(paths.CREDENTIALS_FILE)
        except FileNotFoundError:
            pass
