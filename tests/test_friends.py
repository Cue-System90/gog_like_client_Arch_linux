"""Freunde: Freundesliste, Einladungen, Präsenz, Profile und Spieltitel auswerten (HTTP nur nachgebildet), Schreib-
Anfragen (Annehmen, Ablehnen, Entfernen), Abruf-Takt mit Rückstellung nach Fehlern, Sortierung und Gruppierung,
Aktivitätsprotokoll und Benachrichtigungen – alles ohne Netz und ohne GTK."""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from galaxy_linux import friends as fr
from galaxy_linux import notifications as nt
from galaxy_linux import presence
from galaxy_linux.api import friends as fa
from galaxy_linux.api.client import ApiError, NotLoggedIn
from galaxy_linux.api.friends import Friend, Invitation, PresenceInfo
from galaxy_linux.store_links import store_url

ME = "48628349957132247"
A, B, C, D = "46988961654682898", "47510856674996320", "46988886297334688", "49000000000000001"


class Resp:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeApi:
    """Antworten je (Methode, URL-Teil); ``calls`` hält jede Anfrage fest."""

    def __init__(self, routes=None, logged_in=True):
        self.logged_in = logged_in
        self.user_id = ME if logged_in else ""
        self.routes = routes or {}
        self.calls = []
        self.games = {}
        self.products = {}

    def request(self, method, url, params=None, authenticated=True, **kw):
        self.calls.append((method, url, dict(params or {}), authenticated, kw.get("json")))
        for (m, part), fn in self.routes.items():
            if m == method and part in url:
                res = fn(dict(params or {})) if callable(fn) else fn
                if isinstance(res, Exception):
                    raise res
                return res
        return Resp({}, 404)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def gamesdb(self, pid):
        if isinstance(self.games.get(pid), Exception):
            raise self.games[pid]
        return self.games.get(pid)

    def product(self, pid):
        return self.products.get(pid)


FRIENDS_JSON = {"items": [
    {"user_id": A, "username": "adaliabooks", "is_employee": False,
     "images": {"medium": "https://images.gog.com/a_avm.jpg", "medium_2x": "https://images.gog.com/a_avm2.jpg"}},
    {"user_id": B, "username": "vidsgame", "is_employee": True, "images": {"medium_2x": "https://images.gog.com/b_avm2.jpg"}},
    {"user_id": C, "username": "", "images": None},
    {"user_id": A, "username": "doppelt"},
    {"username": "ohne-id"}, "kaputt"]}

INVITES_JSON = {"items": [
    {"id": "inv1", "friend_user_id": D, "friend_username": "zed", "friend_is_employee": False,
     "friend_images": {"medium": "https://images.gog.com/d_avm.jpg"}, "date_created": "2026-10-20T08:00:00+00:00"},
    {"id": "inv2", "friend_user_id": B, "friend_username": "vidsgame", "date_created": 1792483200000},
    {"id": "inv3"}]}

STATUS_JSON = {"total_count": 2, "limit": 250, "items": [
    {"user_id": A, "client_id": "46755278331571209", "game_id": "1207658924",
     "data": {"status": "Kapitel 3", "metadata": {}, "connect": ""}},
    {"user_id": B, "client_id": "46755278331571209", "data": {}}]}


def friend_api(extra=None):
    routes = {("GET", "/friends_main"): Resp(FRIENDS_JSON), ("GET", "/invitations"): Resp(INVITES_JSON),
              ("GET", "presence.gog.com/statuses"): Resp(STATUS_JSON),
              ("GET", f"users.gog.com/users/{C}"): Resp({"id": C, "username": "carol",
                                                         "avatar": {"small": "https://images.gog.com/c_avs.jpg",
                                                                    "medium": "https://images.gog.com/c_avm.jpg"}})}
    routes.update(extra or {})
    return FakeApi(routes)


class ParseTest(unittest.TestCase):
    def test_friends(self):
        fs = fa.parse_friends(FRIENDS_JSON)
        self.assertEqual([f.user_id for f in fs], [A, B, C])
        self.assertEqual(fs[0], Friend(A, "adaliabooks", "https://images.gog.com/a_avm.jpg", False))
        self.assertEqual(fs[1].avatar, "https://images.gog.com/b_avm2.jpg")
        self.assertTrue(fs[1].is_employee)
        self.assertEqual(fa.parse_friends({"items": "x"}), [])
        self.assertEqual(fa.parse_friends(None), [])
        self.assertEqual(fa.parse_friends([{"id": 5, "userName": "x", "avatar": "https://i/x.jpg"}]),
                         [Friend("5", "x", "https://i/x.jpg")])

    def test_invitations(self):
        inv = fa.parse_invitations(INVITES_JSON)
        self.assertEqual([(i.id, i.user_id, i.username) for i in inv], [("inv1", D, "zed"), ("inv2", B, "vidsgame")])
        self.assertEqual(inv[0].avatar, "https://images.gog.com/d_avm.jpg")
        self.assertEqual(inv[0].created, 1792483200.0)
        self.assertEqual(inv[1].created, 1792483200.0)        # Millisekunden
        self.assertEqual(fa.parse_invitations({}), [])

    def test_times(self):
        self.assertEqual(fa.parse_time("1760947200"), 1760947200.0)
        self.assertEqual(fa.parse_time("2026-10-20T08:00:00Z"), 1792483200.0)
        self.assertEqual(fa.parse_time("gestern"), 0.0)
        self.assertEqual(fa.parse_time(True), 0.0)
        self.assertEqual(fa.parse_time(None), 0.0)

    def test_statuses(self):
        st = fa.parse_statuses(STATUS_JSON, [A, B, C])
        self.assertEqual(st[A], PresenceInfo(A, True, False, "1207658924", "Kapitel 3", "", "46755278331571209"))
        self.assertTrue(st[A].playing)
        self.assertTrue(st[B].online)
        self.assertFalse(st[B].playing)
        self.assertEqual(st[C], PresenceInfo(C))             # angefragt, nicht genannt: offline
        away = fa.parse_statuses({"items": [{"user_id": A, "presence": "away"},
                                            {"user_id": B, "data": {"presence": "invisible"}},
                                            {"user_id": C, "game_title": {"*": "Gwent", "de-DE": "Gwent DE"},
                                             "game_id": 5}]}, [A, B, C])
        self.assertTrue(away[A].away)
        self.assertFalse(away[B].online)
        self.assertEqual((away[C].game_id, away[C].title), ("5", "Gwent DE"))
        self.assertEqual(fa.parse_statuses("kaputt", [A]), {A: PresenceInfo(A)})

    def test_profile_and_title(self):
        p = fa.parse_profile({"id": C, "username": "carol", "avatar": {"small": "s", "medium": "m"}})
        self.assertEqual((p.user_id, p.username, p.avatar), (C, "carol", "m"))
        self.assertIsNone(fa.parse_profile({"username": "x"}))
        self.assertIsNone(fa.parse_profile([]))
        self.assertEqual(fa.game_title({"title": {"*": "Witcher", "de-DE": "Witcher DE"}}), "Witcher DE")
        self.assertEqual(fa.game_title({"game": {"title": {"*": "Gwent"}}}), "Gwent")
        self.assertEqual(fa.game_title(None, {"title": "Cyberpunk 2077"}), "Cyberpunk 2077")
        self.assertEqual(fa.game_title(None, None), "")

    def test_urls(self):
        self.assertEqual(fa.chat_url(ME), f"https://chat.gog.com/users/{ME}/chat?_version=2&isEmbedFrom=galaxy")
        url = fa.chat_url(ME, Friend(A, "ada lia", "https://images.gog.com/a_avm.jpg"))
        self.assertIn(f"&second_participant_id={A}&second_participant_name=ada+lia&second_participant_platform=gog", url)
        self.assertIn("second_participant_avatar=https%3A%2F%2Fimages.gog.com%2Fa_avm.jpg", url)
        self.assertEqual(fa.profile_path("Yep/oleb"), "/u/Yep%2Foleb")
        # Profilseiten ohne Sprachsegment (/de/u/… liefert 404), alles andere mit
        self.assertEqual(store_url("/u/Yepoleb", "de"), "https://www.gog.com/u/Yepoleb")
        self.assertEqual(store_url("https://www.gog.com/en/u/Yepoleb", "de"), "https://www.gog.com/u/Yepoleb")
        self.assertEqual(store_url("/account/friends", "de"), "https://www.gog.com/de/account/friends")


class RequestTest(unittest.TestCase):
    def test_fetch_lists(self):
        api = friend_api()
        self.assertEqual([f.user_id for f in fa.fetch_friends(api)], [A, B, C])
        self.assertEqual(api.calls[0][1], f"https://chat.gog.com/users/{ME}/friends_main")
        self.assertTrue(api.calls[0][3])                     # mit Anmeldung
        self.assertEqual(len(fa.fetch_invitations(api)), 2)
        self.assertEqual(api.calls[-1][1], f"https://chat.gog.com/users/{ME}/invitations")

    def test_friends_fallback_and_errors(self):
        api = FakeApi({("GET", "/friends_main"): Resp({}, 404), ("GET", "/friends"): Resp(FRIENDS_JSON)})
        self.assertEqual(len(fa.fetch_friends(api)), 3)
        self.assertEqual(api.calls[-1][1], f"https://chat.gog.com/users/{ME}/friends")
        with self.assertRaises(NotLoggedIn):
            fa.fetch_friends(FakeApi({("GET", "/friends_main"): Resp({}, 401)}))
        with self.assertRaises(fa.FriendsError) as ctx:
            fa.fetch_friends(FakeApi({("GET", "/friends_main"): Resp({}, 503)}))
        self.assertEqual(ctx.exception.status, 503)
        with self.assertRaises(fa.FriendsError):
            fa.fetch_friends(FakeApi({("GET", "/friends_main"): Resp(ValueError("kein JSON"))}))
        with self.assertRaises(NotLoggedIn):
            fa.fetch_friends(FakeApi(logged_in=False))
        self.assertEqual(fa.fetch_invitations(FakeApi()), [])  # 404 = keine

    def test_statuses_batched(self):
        seen = []

        def answer(params):
            seen.append(params["user_id"].split(","))
            return Resp({"items": [{"user_id": params["user_id"].split(",")[0]}]})
        api = FakeApi({("GET", "presence.gog.com/statuses"): answer})
        ids = [str(1000 + i) for i in range(7)] + ["1000", ""]
        st = fa.fetch_statuses(api, ids, batch=3)
        self.assertEqual([len(s) for s in seen], [3, 3, 1])
        self.assertEqual(len(st), 7)
        self.assertEqual(sorted(u for u, p in st.items() if p.online), ["1000", "1003", "1006"])

    def test_profile_public(self):
        api = friend_api()
        p = fa.fetch_profile(api, C)
        self.assertEqual(p.username, "carol")
        self.assertFalse(api.calls[-1][3])                   # öffentlich, ohne Token
        self.assertIsNone(fa.fetch_profile(api, "123"))      # 404

    def test_writes(self):
        api = FakeApi({("POST", f"users.gog.com/users/{ME}/friends/{D}"): Resp(None, 200),
                       ("DELETE", f"users.gog.com/users/{ME}/invites/{D}"): Resp(None, 204),
                       ("DELETE", f"users.gog.com/users/{ME}/friends/{A}"): Resp(None, 204)})
        fa.accept_invitation(api, D)
        self.assertEqual(api.calls[-1][0:2], ("POST", f"https://users.gog.com/users/{ME}/friends/{D}"))
        self.assertEqual(api.calls[-1][4], {})
        fa.decline_invitation(api, D)
        self.assertEqual(api.calls[-1][0:2], ("DELETE", f"https://users.gog.com/users/{ME}/invites/{D}"))
        fa.remove_friend(api, A)
        self.assertEqual(api.calls[-1][0:2], ("DELETE", f"https://users.gog.com/users/{ME}/friends/{A}"))

    def test_write_errors_and_fallback(self):
        api = FakeApi({("DELETE", "chat.gog.com/users"): Resp(None, 204)})     # users.gog.com: 404
        fa.remove_friend(api, A)
        self.assertEqual([c[1] for c in api.calls], [f"https://users.gog.com/users/{ME}/friends/{A}",
                                                     f"https://chat.gog.com/users/{ME}/friends/{A}"])
        api = FakeApi({("POST", "users.gog.com"): Resp(None, 403)})
        with self.assertRaises(fa.FriendsError) as ctx:
            fa.accept_invitation(api, D)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("403", str(ctx.exception))
        with self.assertRaises(fa.FriendsError):
            fa.decline_invitation(FakeApi(), D)
        with self.assertRaises(NotLoggedIn):
            fa.remove_friend(FakeApi({("DELETE", "users.gog.com"): Resp(None, 401)}), A)
        with self.assertRaises(NotLoggedIn):
            fa.accept_invitation(FakeApi(logged_in=False), D)


class ScheduleTest(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(fr.next_delay(True, True), 120)
        self.assertEqual(fr.next_delay(False, True), 600)
        self.assertIsNone(fr.next_delay(True, False))
        self.assertIsNone(fr.next_delay(False, False, 3))

    def test_backoff(self):
        self.assertEqual([fr.next_delay(True, True, n) for n in range(6)], [120, 240, 480, 960, 1800, 1800])
        self.assertEqual([fr.next_delay(False, True, n) for n in range(3)], [600, 1200, 1800])

    def test_sidebar_fits(self):
        self.assertTrue(fr.sidebar_fits(1501, 900, 250))           # Galaxy: ab 1501 px
        self.assertFalse(fr.sidebar_fits(1500, 900, 250))
        self.assertFalse(fr.sidebar_fits(1920, 1899, 250))         # kein Platz neben dem übrigen Inhalt
        self.assertTrue(fr.sidebar_fits(2160, 1899, 250))

    def test_due_in(self):
        self.assertEqual(fr.due_in(0, 1000, True, True), 0.0)          # noch nie abgerufen
        self.assertEqual(fr.due_in(1000, 1030, True, True), 90.0)
        self.assertEqual(fr.due_in(1000, 1300, True, True), 0.0)       # überfällig, z. B. aus dem Tray zurück
        self.assertEqual(fr.due_in(1000, 1300, False, True), 300.0)
        self.assertEqual(fr.due_in(1000, 1030, True, True, failures=1), 210.0)
        self.assertIsNone(fr.due_in(1000, 1030, True, False))


def presence_map(**kw):
    out = {}
    for uid, state in kw.items():
        uid = {"a": A, "b": B, "c": C, "d": D}[uid]
        if state == "off":
            out[uid] = PresenceInfo(uid)
        elif state == "away":
            out[uid] = PresenceInfo(uid, online=True, away=True)
        elif state == "on":
            out[uid] = PresenceInfo(uid, online=True)
        else:
            out[uid] = PresenceInfo(uid, online=True, game_id=state)
    return out


FRIENDS = [Friend(A, "anna"), Friend(B, "Bert"), Friend(C, "carla"), Friend(D, "dora")]


class SortGroupTest(unittest.TestCase):
    def test_activity_sort(self):
        pres = presence_map(a="away", b="on", c="off", d="100")
        on, off = fr.sort_friends(FRIENDS, pres, {C: 50.0})
        self.assertEqual([f.username for f in on], ["dora", "Bert", "anna"])     # Spiel, online, abwesend
        self.assertEqual([f.username for f in off], ["carla"])
        pres = presence_map(a="off", b="off", c="off", d="off")
        _on, off = fr.sort_friends(FRIENDS, pres, {C: 50.0, B: 90.0})
        self.assertEqual([f.username for f in off], ["Bert", "carla", "anna", "dora"])   # zuletzt online, dann Name

    def test_name_sort(self):
        pres = presence_map(a="away", b="on", c="off", d="100")
        on, off = fr.sort_friends(FRIENDS, pres, {}, "name")
        self.assertEqual([f.username for f in on], ["anna", "Bert", "dora"])
        self.assertEqual([f.username for f in off], ["carla"])
        on, off = fr.sort_friends(FRIENDS, {}, {})                              # ohne Präsenz: alle offline
        self.assertEqual((on, len(off)), ([], 4))

    def test_group_by_game(self):
        pres = presence_map(a="200", b="on", c="100", d="200")
        on, _off = fr.sort_friends(FRIENDS, pres, {})
        groups = fr.group_online(on, pres, {"100": "Witcher 3", "200": "Cyberpunk 2077"}, "game")
        self.assertEqual([(t, [f.username for f in fs]) for t, fs in groups],
                         [("Cyberpunk 2077", ["anna", "dora"]), ("Witcher 3", ["carla"]), (fr.NOT_PLAYING, ["Bert"])])
        self.assertEqual(fr.group_online(on, pres, {}, "none"), [("", on)])
        self.assertEqual(fr.group_online([], pres, {}, "none"), [])
        # ohne bekannten Titel: Spiel-ID als Gruppenname
        self.assertEqual(fr.group_online([FRIENDS[2]], pres, {}, "game")[0][0], "100")

    def test_filter_and_status_text(self):
        self.assertEqual([f.username for f in fr.filter_friends(FRIENDS, " AN ")], ["anna"])
        self.assertEqual(len(fr.filter_friends(FRIENDS, "")), 4)
        rel = lambda ts: f"vor {int(ts)}"                                       # noqa: E731
        self.assertEqual(fr.status_text(PresenceInfo(A, True), "", 0, rel), "Online")
        self.assertEqual(fr.status_text(PresenceInfo(A, True, True), "", 0, rel), "Abwesend")
        self.assertEqual(fr.status_text(PresenceInfo(A, True, game_id="1"), "Gwent", 0, rel), "Spielt – Gwent")
        self.assertEqual(fr.status_text(PresenceInfo(A, True, game_id="1", status="Runde 2"), "Gwent", 0, rel),
                         "Spielt – Gwent – Runde 2")
        self.assertEqual(fr.status_text(PresenceInfo(A, True, game_id="1"), "", 0, rel), "Im Spiel")
        self.assertEqual(fr.status_text(PresenceInfo(A), "", 7, rel), "Zuletzt online vor 7")
        self.assertEqual(fr.status_text(None, "", 0, rel), "Offline")


class ActivityTest(unittest.TestCase):
    def test_events(self):
        friends = {f.user_id: f for f in FRIENDS}
        prev = presence_map(a="off", b="on", c="100", d="off")
        cur = presence_map(a="on", b="200", c="300", d="400")
        cur[D] = PresenceInfo(D, online=True, game_id="400")
        ev = fr.presence_events(prev, cur, friends, {"200": "Gwent", "400": "Witcher"}, 1000.0)
        self.assertEqual([(e.username, e.kind, e.game_id, e.title) for e in ev],
                         [("anna", "online", "", ""), ("Bert", "game", "200", "Gwent"), ("carla", "game", "300", ""),
                          ("dora", "game", "400", "Witcher")])                  # online mit Spiel: nur „spielt“
        self.assertEqual(fr.presence_events(None, cur, friends, {}, 1.0), [])  # erster Abruf
        self.assertEqual(fr.presence_events(cur, cur, friends, {}, 1.0), [])   # nichts geändert
        self.assertEqual(fr.presence_events({}, cur, friends, {}, 1.0), [])    # neue Freunde: nichts
        went_off = fr.presence_events(cur, presence_map(a="off", b="off", c="off", d="off"), friends, {}, 1.0)
        self.assertEqual(went_off, [])

    def test_log_keeps_50_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "friends_activity.json"
            log = fr.ActivityLog(path)
            log.record([fr.ActivityEvent(float(i), A, "anna", "online") for i in range(60)])
            log.seen_online([A, B], 99.0)
            log.save()
            again = fr.ActivityLog(path)
            self.assertEqual(len(again.events()), 50)
            self.assertEqual(again.events()[0].ts, 59.0)
            self.assertEqual(again.last_seen, {A: 99.0, B: 99.0})
            path.write_text('{"events": [{"ts": "x"}, {"ts": 1, "user_id": "", "kind": "online"}, 5,'
                            ' {"ts": 2, "user_id": "1", "kind": "kaputt"}], "last_seen": {"1": true, "2": 3}}')
            broken = fr.ActivityLog(path)
            self.assertEqual((broken.events(), broken.last_seen), ([], {"2": 3.0}))
            broken.clear()
            self.assertFalse(path.exists())


class NoticeTest(unittest.TestCase):
    def test_texts_keys_actions(self):
        ev = [fr.ActivityEvent(100.0, A, "anna", "online"), fr.ActivityEvent(200.0, B, "Bert", "game", "1207658924", "Witcher"),
              fr.ActivityEvent(300.0, C, "", "game", "99", "")]
        inv = [Invitation("inv1", D, "dora")]
        out = fr.notices(ev, inv, owned={"1207658924"})
        self.assertEqual([(n.kind, n.title, n.body, n.action) for n in out], [
            (fr.KIND_INVITE, "dora möchte dein Freund sein.", "Freundschaftseinladung erhalten", "view:friends"),
            (fr.KIND_ONLINE, "anna", "Jetzt online", "view:friends"),
            (fr.KIND_GAME, "Bert spielt jetzt", "Witcher", "game:1207658924"),
            (fr.KIND_GAME, f"{C} spielt jetzt", "", "view:friends")])
        self.assertEqual([n.key for n in out], ["friend-invite:inv1", f"friend-online:{A}:100",
                                                f"friend-game:{B}:1207658924:200", f"friend-game:{C}:99:300"])
        self.assertEqual(out[2].product_id, "1207658924")

    def test_types_and_defaults(self):
        keys = {t.key: t for t in nt.types()}
        self.assertEqual(keys["friend_invite"].title, "Freundschaftsanfrage")
        self.assertEqual(keys["friend_online"].title, "Freund geht online")
        self.assertEqual(keys["friend_playing"].title, "Freund startet ein Spiel")
        from galaxy_linux.settings import GlobalSettings
        s = GlobalSettings()
        self.assertEqual(nt.mode_for(s, fr.KIND_INVITE), nt.MODE_BOTH)
        self.assertEqual(nt.mode_for(s, fr.KIND_ONLINE), nt.MODE_CENTER)
        self.assertEqual(nt.mode_for(s, fr.KIND_GAME), nt.MODE_CENTER)
        # Vorgaben wirken: Anfrage im Hintergrund auf den Desktop, „online“ nur ins Benachrichtigungszentrum
        self.assertEqual(nt.route(nt.mode_for(s, fr.KIND_INVITE), window_focused=False), (True, True))
        self.assertEqual(nt.route(nt.mode_for(s, fr.KIND_INVITE), window_focused=True), (True, False))
        self.assertEqual(nt.route(nt.mode_for(s, fr.KIND_ONLINE), window_focused=False), (True, False))
        s.notify_friend_playing = "none"
        self.assertEqual(nt.route(nt.mode_for(s, fr.KIND_GAME), window_focused=False), (False, False))

    def test_store_posts_invitation_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = nt.NotificationStore(Path(d) / "n.json")
            for _ in range(3):                                   # jeder Abruf liefert die offene Einladung erneut
                for n in fr.notices([], [Invitation("inv1", D, "dora")]):
                    store.add(n.kind, n.title, n.body, n.action, key=n.key)
            self.assertEqual(len(store.items()), 1)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = [10_000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, api):
        return fr.FriendsService(api, self.dir / "act.json", self.dir / "prof.json", clock=lambda: self.now[0])

    def test_fetch_apply_and_titles(self):
        api = friend_api()
        api.games["1207658924"] = {"title": {"*": "The Witcher 3", "de-DE": "The Witcher 3 DE"}}
        svc = self.service(api)
        snap = svc.fetch()
        self.assertEqual([f.username for f in snap.friends], ["adaliabooks", "vidsgame", "carol"])   # Profil ergänzt
        self.assertEqual(snap.friends[2].avatar, "https://images.gog.com/c_avm.jpg")
        self.assertEqual(snap.titles, {"1207658924": "The Witcher 3 DE"})
        self.assertEqual(len(snap.invitations), 2)
        self.assertEqual([f.username for f in snap.online()], ["adaliabooks", "vidsgame"])
        self.assertEqual([f.username for f, _p in snap.playing()], ["adaliabooks"])
        self.assertEqual([n.kind for n in svc.apply(snap)], [fr.KIND_INVITE, fr.KIND_INVITE])   # erster Abruf
        self.assertEqual(svc.activity.last_seen, {A: 10_000.0, B: 10_000.0})
        # zweiter Abruf kurz danach: Freundesliste aus dem Speicher, Präsenz neu; Bert startet ein eigenes Spiel
        api.routes[("GET", "presence.gog.com/statuses")] = Resp({"items": [
            {"user_id": A, "game_id": "1207658924"}, {"user_id": B, "game_id": "1"}, {"user_id": C}]})
        api.games["1"] = ApiError("weg")
        api.products["1"] = {"title": "Gwent"}
        self.now[0] += 120
        before = len([c for c in api.calls if "/friends_main" in c[1]])
        snap2 = svc.fetch(known_titles={"1207658924": "Witcher (Bibliothek)"})
        self.assertEqual(len([c for c in api.calls if "/friends_main" in c[1]]), before)
        self.assertEqual(snap2.titles["1207658924"], "Witcher (Bibliothek)")
        self.assertEqual(snap2.titles["1"], "")              # GamesDB-Fehler: Titel bleibt leer, kein Absturz
        out = svc.apply(snap2, owned={"1"})
        self.assertEqual([(n.kind, n.title) for n in out if n.kind != fr.KIND_INVITE],
                         [(fr.KIND_GAME, "vidsgame spielt jetzt"), (fr.KIND_ONLINE, "carol")])
        self.assertEqual([e.kind for e in svc.activity.events()], ["game", "online"])
        self.assertTrue((self.dir / "act.json").exists())
        # nach 10 Minuten wieder die echte Freundesliste
        self.now[0] += 600
        svc.fetch()
        self.assertEqual(len([c for c in api.calls if "/friends_main" in c[1]]), before + 1)

    def test_profile_cache(self):
        api = friend_api()
        svc = self.service(api)
        svc.fetch()
        svc.fetch(force=True)
        prof = [c[1] for c in api.calls if "users.gog.com" in c[1]]
        self.assertEqual(sorted(prof), sorted([f"https://users.gog.com/users/{C}",       # Freund ohne Namen
                                               f"https://users.gog.com/users/{B}"]))     # Einladung ohne Bild (404)
        self.assertIn(C, (self.dir / "prof.json").read_text())
        self.now[0] += fr.PROFILE_MAX_AGE + 1
        svc.fetch(force=True)
        self.assertEqual(len([c for c in api.calls if "users.gog.com" in c[1]]), 4)        # nach 7 Tagen neu

    def test_errors(self):
        svc = self.service(FakeApi(logged_in=False))
        with self.assertRaises(NotLoggedIn):
            svc.fetch()
        api = friend_api({("GET", "/invitations"): Resp({}, 500)})
        svc = self.service(api)
        snap = svc.fetch()                                   # Einladungen kaputt: Freunde trotzdem
        self.assertEqual((len(snap.friends), snap.invitations), (3, []))
        api.routes[("GET", "/friends_main")] = Resp({}, 502)
        with self.assertRaises(ApiError):
            svc.fetch(force=True)
        svc.failed(ApiError("HTTP 502"))
        svc.failed(ApiError("HTTP 502"))
        self.assertEqual(svc.failures, 2)
        self.assertEqual(fr.next_delay(True, True, svc.failures), 480)
        svc.apply(snap)
        self.assertEqual((svc.failures, svc.last_error), (0, ""))
        svc.clear()
        self.assertIsNone(svc.snapshot)
        self.assertFalse((self.dir / "act.json").exists())

    def test_local_changes(self):
        svc = self.service(friend_api())
        svc.apply(svc.fetch())
        inv = svc.snapshot.invitations[0]
        svc.add_friend(inv)
        svc.drop_invitation(inv.user_id)
        self.assertIn(D, [f.user_id for f in svc.snapshot.friends])
        self.assertNotIn(D, [i.user_id for i in svc.snapshot.invitations])
        self.assertEqual(svc.snapshot.friends_at, 0.0)
        svc.drop_friend(A)
        self.assertNotIn(A, [f.user_id for f in svc.snapshot.friends])
        self.assertNotIn(A, svc.snapshot.presence)


class InvisibleTest(unittest.TestCase):
    def test_invisible_never_posts(self):
        calls = []
        api = types.SimpleNamespace(logged_in=True, set_presence=lambda g="": calls.append(("on", g)) or True,
                                    clear_presence=lambda: calls.append(("off",)) or True)
        p = presence.Presence(api, types.SimpleNamespace(presence_status="invisible"))
        p.set_game("1207658924")
        self.assertEqual(p.tick(), "")
        p.stop()
        self.assertEqual(calls, [])                          # weder „online“ noch ein Spiel an GOG


if __name__ == "__main__":
    unittest.main()
