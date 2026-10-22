"""Kommandozeile – alles, was die Oberfläche kann, auch per Terminal/Skript."""
from __future__ import annotations

from .i18n import tr

import argparse
import json
import os
import shlex
import shutil
import sys
import time

from . import __version__, paths
from .api import auth
from .api.client import ApiError, NotLoggedIn
from .backend import Backend, Job
from .content.installer import InstallError
from .launch import compat, goverlay, launch_options
from .launch.launcher import LaunchError
from .settings import InstalledGame, coerce_value, format_bytes, format_minutes
from .ui.util import fmt_eta, fmt_speed


def _resolve(backend: Backend, text: str) -> str:
    text = text.strip()
    if text.isdigit() or backend.registry.get(text):
        return text
    exact = [g.product_id for g in backend.registry.all() if g.title.lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]
    hits = [e for e in backend.library if text.lower() in e.title.lower()]
    inst = [g for g in backend.registry.all() if text.lower() in g.title.lower()]
    ids = {e.id for e in hits} | {g.product_id for g in inst}
    if len(ids) == 1:
        return ids.pop()
    if not ids:
        sys.exit(tr('Kein Spiel gefunden für »{0}« (ggf. erst »galaxy-linux list --refresh«).').format(text))
    names = sorted({e.title for e in hits} | {g.title for g in inst})
    sys.exit("Mehrdeutig: " + ", ".join(names[:10]))


def _print_progress(job: Job) -> None:
    p = job.progress
    cols = shutil.get_terminal_size((100, 20)).columns
    if p.total_bytes:
        line = (tr('{0}: {1:5.1f}%  {2}/{3}  {4}  ETA {5}  {6}/{7} Dateien').format(job.status, p.fraction * 100, format_bytes(p.done_bytes), format_bytes(p.total_bytes), fmt_speed(p.speed), fmt_eta(p.eta_seconds), p.files_done, p.files_total))
    else:
        line = job.status
    sys.stderr.write("\r" + line[:cols - 1].ljust(cols - 1))
    sys.stderr.flush()


def _wait_job(job: Job) -> int:
    try:
        while job.active:
            time.sleep(0.25)
    except KeyboardInterrupt:
        job.cancel.set()
        sys.stderr.write("\nAbbruch angefordert …\n")
        while job.active:
            time.sleep(0.25)
    sys.stderr.write("\n")
    if job.state == "done":
        g = job.result if isinstance(job.result, InstalledGame) else None
        print(tr('{0} abgeschlossen: {1} → {2}').format(job.label, g.title, g.install_path) if g else tr('{0} abgeschlossen').format(job.label))
        return 0
    print(f"{job.label} {job.status}: {job.error}", file=sys.stderr)
    return 1


def cmd_login(b: Backend, a) -> int:
    code = a.code
    if not code:
        print(tr('Login-Seite im Browser öffnen und bei GOG anmelden. Danach zeigt der Browser eine leere Seite'))
        print(tr('von embed.gog.com – deren komplette Adresse (enthält code=…) hier einfügen:'))
        print(auth.AUTH_URL)
        try:
            code = input(tr('Weiterleitungs-URL oder Code: '))
        except EOFError:
            return 1
    c = auth.parse_code(code)
    if not c:
        print(tr('Kein Code erkannt.'), file=sys.stderr)
        return 1
    user = b.api.login_with_code(c)
    print(tr('Angemeldet als {0} (ID {1}).').format(user.get('username'), user.get('userId')))
    b.refresh_library(lambda p, t: sys.stderr.write("\r" + tr("Bibliothek: Seite {0}/{1}").format(p, t)))
    sys.stderr.write("\n")
    print(tr('{0} Spiele in der Bibliothek.').format(len(b.library)))
    return 0


def cmd_logout(b: Backend, a) -> int:
    b.api.logout()
    print(tr('Abgemeldet.'))
    return 0


def cmd_whoami(b: Backend, a) -> int:
    if not b.api.logged_in:
        print(tr('Nicht angemeldet.'))
        return 1
    u = b.api.user_data()
    print(tr('{0}  (ID {1}, E-Mail {2})').format(u.get('username'), u.get('userId'), u.get('email', '–')))
    return 0


def cmd_list(b: Backend, a) -> int:
    if a.refresh:
        b.refresh_library(lambda p, t: sys.stderr.write("\r" + tr("Bibliothek: Seite {0}/{1}").format(p, t)))
        sys.stderr.write("\n")
    if a.external or a.library:
        libs = [b.find_library(a.library)] if a.library else b.libraries()
        if a.json:
            print(json.dumps([{"library": lib.id, "product_id": g.product_id, "title": g.title, "install_path": g.install_path,
                               "executable": g.executable, "prefix": str(b.launcher.prefix_for(g.product_id)),
                               "prefix_exists": b.launcher.prefix_exists(g.product_id)}
                              for lib in libs for g in sorted(b.library_games(lib.id), key=lambda g: g.title.lower())],
                             indent=2, ensure_ascii=False))
            return 0
        for lib in libs:
            games = sorted(b.library_games(lib.id), key=lambda g: g.title.lower())
            print(tr('{0}: {1} Spiele · Ordner {2} · Prefixe {3}').format(lib.name, len(games), lib.games_dir, lib.prefix_dir))
            for g in games:
                pfx = tr('Prefix ✔') if b.launcher.prefix_exists(g.product_id) else tr('kein Prefix')
                print(f"  {g.product_id:45s} {g.title}  [{g.executable}] {pfx}")
        if not libs:
            print(tr('Keine eigenen Bibliotheken – anlegen mit »galaxy-linux library add NAME ORDNER«.'))
        return 0
    entries = b.visible_library()
    installed = {g.product_id: g for g in b.registry.all() if g.method != "external"}
    if a.installed:
        entries = [e for e in entries if e.id in installed]
        for pid, g in installed.items():
            if pid not in {e.id for e in entries}:
                entries.append(b.entry(pid))
    if a.json:
        print(json.dumps([{**e.to_dict(), "installed": e.id in installed,
                           "install_path": installed[e.id].install_path if e.id in installed else None} for e in entries], indent=2, ensure_ascii=False))
        return 0
    for e in sorted(entries, key=lambda x: x.title.lower()):
        mark = "✔" if e.id in installed else " "
        plat = "/".join(p.replace("osx", "mac") for p in e.platforms) or "–"
        print(f"{mark} {e.id:>11}  {e.title}  [{plat}]")
    print(tr('{0} Einträge, {1} installiert.').format(len(entries), len([e for e in entries if e.id in installed])))
    return 0


def cmd_search(b: Backend, a) -> int:
    for e in b.library:
        if a.text.lower() in e.title.lower():
            print(f"{e.id:>11}  {e.title}")
    return 0


def cmd_info(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    e = b.entry(pid)
    m = b.meta(pid)
    g = b.registry.get(pid)
    print(tr('{0}  (ID {1})').format(m.title if m else e.title if e else pid, pid))
    if m:
        print(tr('  Entwickler: {0} · Publisher: {1} · Release: {2}').format(', '.join(m.developers) or '–', ', '.join(m.publishers) or '–', m.release_date or '–'))
        print(tr('  Genres: {0} · Plattformen: {1} · Galaxy-Build: {2}').format(', '.join(m.genres) or '–', ', '.join(m.os_list) or '–', m.content_system or '–'))
        if m.summary:
            print("  " + m.summary.split("\n")[0][:200])
    if g:
        minutes, last = b.playtime.get(pid)
        print(tr('  Installiert: {0} ({1}, {2}, Version {3}, Build {4}, {5})').format(g.install_path, g.platform, g.method, g.version_name or '–', g.build_id or '–', format_bytes(g.install_size)))
        print(tr('  Spielzeit: {0} · zuletzt: {1}').format(format_minutes(minutes), time.strftime('%Y-%m-%d %H:%M', time.localtime(last)) if last else '–'))
        gs = b.game_settings.get(pid)
        lib = b.library_of(pid)
        if lib is not None:
            print(tr('  Bibliothek: {0} ({1})').format(lib.name, lib.games_dir))
        tool = b.launcher.resolve_tool(gs, g.platform, pid)
        print(tr('  Kompatibilitätstool: {0} · Startweg: {1}').format(tool.name if tool else tr('nativ'), b.launcher.runner_for(tool) if tool else 'native'))
        print(tr('  Startoptionen: {0}  · Prefix: {1}').format(gs.launch_options or '–', b.launcher.prefix_for(pid)))
        for t in b.launcher.tasks_for(g):
            print(tr('  Startdatei: {0}: {1} {2}{3}').format(t.name, t.path, t.arguments, tr(' (primär)') if t.is_primary else ''))
    else:
        print(tr('  Nicht installiert.'))
    if a.builds:
        for plat in ("windows", "linux", "osx"):
            try:
                builds = b.installer.builds(pid, plat)
            except ApiError:
                continue
            for bl in builds:
                print(tr('  Build {0}: {1} gen{2} {3} {4} {5}').format(plat, bl.build_id, bl.generation, bl.version_name, bl.branch or '', bl.date_published[:10]))
    return 0


def cmd_install(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    e = b.entry(pid)
    platform = a.platform
    if not platform:
        platform = "linux" if (e and "linux" in e.platforms and "windows" not in e.platforms) else "windows"
    kind = "install-offline" if a.offline else None
    job = b.install(pid, platform=platform, language=a.lang or "", install_root=a.dir or "", build_id=a.build or "",
                    include_dlcs=not a.no_dlc, on_progress=_print_progress)
    if kind:
        job.kind = kind
    return _wait_job(job)


def cmd_update(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    return _wait_job(b.update(pid, build_id=a.build or "", on_progress=_print_progress))


def cmd_verify(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    return _wait_job(b.verify(pid, on_progress=_print_progress))


def cmd_move(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    return _wait_job(b.move(pid, a.dir, on_progress=_print_progress))


def cmd_scan(b: Backend, a) -> int:
    print(tr('Durchsuche:'), ", ".join([*(str(r) for r in b.install_roots()), *(f"{lib.games_dir} ({lib.name})" for lib in b.libraries())]))
    rep = b.scan_install_dirs(create_prefixes=not a.no_prefix)
    if rep is None:
        print(tr('Es läuft bereits eine Suche.'))
        return 1
    for g in rep.imported:
        print(tr('übernommen: {0} ({1}, {2}) – {3}').format(g.title, g.product_id, g.platform, g.install_path))
    for t in rep.copying:
        print(tr('wird noch kopiert: {0}').format(t))
    for t in rep.removed:
        print(tr('ausgetragen (Ordner fehlt): {0}').format(t))
    for u in rep.unknown:
        print(tr('ohne GOG-Spieldaten: {0}').format(u))
    for u in rep.duplicates:
        print(tr('bereits anderswo installiert: {0}').format(u))
    print(tr('{0} Spiele installiert, {1} Prefix(e) werden angelegt.').format(len(b.registry.all()), rep.prefix_jobs))
    rc = 0
    for job in [j for j in b.jobs.active() if j.kind == "prefix"]:
        print(tr('Prefix für {0} …').format(job.title), flush=True)
        rc |= _wait_job(job)
    return rc


def cmd_shortcut(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    if not b.registry.is_installed(pid):
        print(tr('Spiel ist nicht installiert.'), file=sys.stderr)
        return 1
    menu = a.menu or not (a.menu or a.desktop)
    desktop = a.desktop or not (a.menu or a.desktop)
    if a.remove:
        for p in b.remove_shortcuts(pid, menu=menu, desktop=desktop):
            print("entfernt:", p)
    else:
        for p in b.create_shortcuts(pid, menu=menu, desktop=desktop):
            print("angelegt:", p)
    return 0


def cmd_uninstall(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    g = b.registry.get(pid)
    if not g:
        print(tr('Nicht installiert.'))
        return 1
    if not a.yes:
        ans = input(tr('{0} aus {1} entfernen{2}? [j/N] ').format(g.title, g.install_path, tr(' inkl. Prefix') if a.prefix else ''))
        if ans.lower() not in ("j", "y", "ja", "yes"):
            return 1
    b.uninstall(pid, remove_prefix=a.prefix)
    print(tr('Deinstalliert.'))
    return 0


def cmd_launch(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    extra = [x for x in a.args if x != "--"]
    task = None
    if a.task:
        g = b.registry.get(pid)
        gs = b.game_settings.get(pid)
        task = b.launcher.select_task(g, gs, a.task) if g else None
    if a.dry_run:
        plan = b.launcher.preview_plan(pid, task, extra)
        print(plan.shell_preview())
        return 0
    done = {}

    def on_exit(p, code, minutes):
        done["code"] = code
        done["minutes"] = minutes
    rg = b.launch(pid, task, extra, on_exit=on_exit, status_cb=lambda s: print(s, file=sys.stderr))
    print(tr('Gestartet (PID {0}, {1}); Protokoll: {2}').format(rg.process.pid, rg.plan.description, rg.log_file))
    if a.no_wait:
        return 0
    try:
        while "code" not in done:
            time.sleep(0.5)
    except KeyboardInterrupt:
        rg.terminate()
        return 130
    print(tr('Beendet mit Code {0} nach {1} Min.').format(done['code'], done['minutes']))
    return 0 if done["code"] == 0 else done["code"]


def cmd_tools(b: Backend, a) -> int:
    tools = b.launcher.tools(refresh=True)
    if a.json:
        print(json.dumps([t.__dict__ for t in tools], indent=2, ensure_ascii=False))
        return 0
    d = b.launcher.global_default_tool()
    for t in tools:
        flag = "*" if d and t.id == d.id else " "
        extra = tr('  (Runtime {0} → umu)').format(t.requires_runtime) if t.requires_runtime else ""
        print(f"{flag} {t.id}\n    {t.name} · {t.kind} · {t.source}{extra}")
    st = goverlay.status()
    print(tr('\numu-run: {0} · GOverlay: {1} · MangoHud: {2} · vkBasalt: {3} · GameMode: {4} · Gamescope: {5}').format(tr('ja') if compat.umu_available() else tr('nein'), st.goverlay, st.mangohud, st.vkbasalt, st.gamemode, st.gamescope))
    return 0


def cmd_config(b: Backend, a) -> int:
    if a.what == "locations":
        if a.add:
            b.settings.remember_location(a.add)
        if a.remove:
            b.settings.install_locations = [l for l in b.settings.install_locations if l != a.remove]
            b.save_settings()
        print(tr('Standard:'), b.settings.install_dir)
        for l in b.settings.install_locations:
            print(tr('Weiterer Ort:'), l)
        return 0
    if a.what == "global":
        if a.key is None:
            print(json.dumps(b.settings.__dict__, indent=2, ensure_ascii=False, default=str))
            return 0
        if not hasattr(b.settings, a.key) or a.key.startswith("_"):
            sys.exit(tr('Unbekannte Einstellung {0}').format(a.key))
        if a.value is None:
            print(json.dumps(getattr(b.settings, a.key), ensure_ascii=False, default=str))
            return 0
        try:
            val = coerce_value(getattr(b.settings, a.key), a.value)
        except ValueError as exc:
            sys.exit(str(exc))
        setattr(b.settings, a.key, val)
        b.save_settings()
        print(f"{a.key} = {val}")
        return 0
    pid = _resolve(b, a.game)
    gs = b.game_settings.get(pid)
    if a.what == "show":
        print(json.dumps(gs.__dict__, indent=2, ensure_ascii=False))
        return 0
    if a.what == "tool":
        v = a.value
        if v not in ("", "auto", "native") and compat.resolve(v, b.launcher.tools()) is None:
            # auch Teilnamen erlauben
            hits = [t for t in b.launcher.tools() if v.lower() in t.name.lower() or v.lower() in t.id.lower()]
            if len(hits) != 1:
                sys.exit(tr('Tool nicht gefunden – IDs mit »galaxy-linux tools« anzeigen.'))
            v = hits[0].id
        b.game_settings.update(pid, compat_tool="" if v == "auto" else v)
        print(tr('Kompatibilitätstool für {0}: {1}').format(pid, v or tr('globale Vorgabe')))
        return 0
    if a.what == "options":
        launch_options.parse(a.value)
        b.game_settings.update(pid, launch_options=a.value)
        print(tr('Startoptionen: {0}').format(a.value))
        return 0
    if a.what == "env":
        env = dict(gs.env)
        for kv in a.values:
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k] = v
            else:
                env.pop(kv, None)
        b.game_settings.update(pid, env=env)
        print(json.dumps(env, ensure_ascii=False))
        return 0
    if a.what == "prefix":
        b.game_settings.update(pid, prefix=a.value)
        print(tr('Prefix: {0}').format(a.value or tr('Standard')))
        return 0
    if a.what == "set":
        if not hasattr(gs, a.key):
            sys.exit(tr('Unbekannte Einstellung {0}').format(a.key))
        try:
            val = coerce_value(getattr(gs, a.key), a.value)
        except ValueError as exc:
            sys.exit(str(exc))
        b.game_settings.update(pid, **{a.key: val})
        print(f"{a.key} = {val}")
        return 0
    return 1


def cmd_goverlay(b: Backend, a) -> int:
    text = goverlay.import_from_goverlay()
    if a.game:
        pid = _resolve(b, a.game)
        b.game_settings.update(pid, launch_options=text)
        print(tr('Startoptionen für {0} gesetzt: {1}').format(pid, text))
    else:
        print(text)
    return 0


def cmd_updates(b: Backend, a) -> int:
    found = b.check_updates()
    if not found:
        print(tr('Alle installierten Spiele sind aktuell.'))
        return 0
    for pid, bl in found.items():
        g = b.registry.get(pid)
        print(f"{pid}  {g.title if g else ''}: {g.version_name if g else ''} → {bl.version_name} ({bl.date_published[:10]})")
    if a.install:
        rc = 0
        for pid in found:
            rc |= _wait_job(b.update(pid, on_progress=_print_progress))
        return rc
    return 0


def cmd_prefix(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    if a.action == "create":
        job = b.queue_prefix(pid)
        return _wait_job(job) if job else 0
    if a.action == "delete":
        print(tr('gelöscht:'), b.delete_prefix(pid) or tr('kein Prefix vorhanden'))
        return 0
    if a.action == "reset":
        job = b.reset_prefix(pid)
        return _wait_job(job) if job else 1
    if a.action == "winetricks":
        p = b.launcher.run_winetricks(pid, a.verbs or None)
        return p.wait()
    if a.action == "run":
        return b.launcher.run_in_prefix(pid, a.verbs, timeout=None)
    if a.action == "path":
        print(b.launcher.prefix_for(pid))
        return 0
    if a.action == "setup":
        ctx = b.launcher.run_prepare_only(pid, status_cb=lambda t: print(t, file=sys.stderr))
        print(tr('Einrichtung abgeschlossen: {0}').format(ctx.prefix))
        return 0
    return 1


def cmd_records(b: Backend, a) -> int:
    """Installations-Datensätze (Build, Abhängigkeiten, Produkte) anzeigen und für übernommene Spiele anlegen."""
    from .content import build_records
    games = [b.registry.get(_resolve(b, a.game))] if a.game else b.registry.all()
    rows = []
    for g in games:
        if g is None or g.method == "external":
            continue
        rec = build_records.ensure_record(b.api, g, force=a.force) if (a.create or a.force) else build_records.load_record(g.product_id)
        rows.append(build_records.summary(g, rec))
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for r in rows:
        state = tr('vollständig') if r["complete"] else (tr('unvollständig') if r["exists"] else tr('fehlt'))
        print(f"{r['product_id']:>12}  {r['title'][:40]:<40}  {state:<13} {tr('Build')} {r['build_id'] or '–'}  "
              f"{tr('Abhängigkeiten')}: {', '.join(r['dependencies']) or '–'}")
    return 0


def cmd_comet(b: Backend, a) -> int:
    """Galaxy-Dienste (comet): Status anzeigen, herunterladen bzw. aktualisieren."""
    from .launch import galaxy_services
    return galaxy_services.cli_command(b, a)


def cmd_add(b: Backend, a) -> int:
    if a.library:
        b.find_library(a.library)            # Tippfehler vor dem Durchsuchen melden
    info = b.inspect_folder(a.path)
    print(f"{tr('GOG-Spiel') if info['kind'] == 'gog' else tr('Externes Spiel')}: {info['title']}")
    if info["kind"] != "gog":
        print(tr('Startdatei-Kandidaten:'), ", ".join(info["candidates"][:6]) or "–")
    game = b.add_game_manually(a.path, title=a.title or "", exe=a.exe or "", create_prefix=not a.no_prefix,
                               library=a.library or "")
    lib = b.library_of(game.product_id)
    print(tr('hinzugefügt: {0} ({1}) · Startdatei {2}').format(game.title, game.product_id, game.executable or '–')
          + (tr(' · Bibliothek {0} · Prefix {1}').format(lib.name, b.launcher.prefix_for(game.product_id)) if lib else ""))
    job = b.jobs.active_for(game.product_id)
    return _wait_job(job) if job else 0


def cmd_remove(b: Backend, a) -> int:
    pid = _resolve(b, a.game)
    g = b.registry.get(pid)
    if not g:
        sys.exit(tr('Spiel ist nicht in der Bibliothek.'))
    if (a.delete_files or a.delete_prefix) and not a.yes:
        what = tr(" und ").join(x for x, f in ((tr("Spielordner"), a.delete_files), ("Prefix", a.delete_prefix)) if f)
        if input(tr('{0}: {1} wirklich löschen? [j/N] ').format(g.title, what)).lower() not in ("j", "ja", "y", "yes"):
            return 1
    b.remove_game(pid, delete_files=a.delete_files, delete_prefix=a.delete_prefix)
    print(tr('entfernt: {0}').format(g.title) + ("" if a.delete_files else tr(' (Ordner bleibt, wird bei der Suche nicht wieder aufgenommen)')))
    return 0


def cmd_prefixes(b: Backend, a) -> int:
    lib_id = b.find_library(a.library).id if a.library else None
    orphans = b.orphan_prefixes(lib_id)
    for d in orphans:
        print("verwaist:", d)
    if a.create_missing:
        print(tr('{0} fehlende Prefixe werden angelegt.').format(b.queue_missing_prefixes(lib_id)))
        rc = 0
        for job in [j for j in b.jobs.active() if j.kind == "prefix"]:
            rc |= _wait_job(job)
        return rc
    if a.delete_orphans and orphans:
        if not a.yes and input(tr('{0} verwaiste Prefixe löschen? [j/N] ').format(len(orphans))).lower() not in ("j", "ja", "y", "yes"):
            return 1
        print(tr('{0} gelöscht.').format(len(b.delete_orphan_prefixes(orphans))))
    elif not orphans:
        print(tr('Keine verwaisten Prefixe.'))
    return 0


WAYLAND_TEXT = {"": tr('globale Vorgabe'), "on": tr("an"), "off": tr('aus (XWayland)')}


def _print_library(b: Backend, lib) -> None:
    games = b.library_games(lib.id)
    tool = compat.resolve(lib.compat_tool, b.launcher.tools()) if lib.compat_tool else None
    print(tr('{0}  (ID {1}) · {2} Spiele').format(lib.name, lib.id, len(games)))
    print(tr('  Spiele-Ordner: {0}{1}').format(lib.games_dir, '' if lib.games_root.is_dir() else '  (fehlt)'))
    print(tr('  Prefix-Ordner: {0}').format(lib.prefix_dir))
    print(tr('  Kompatibilitätstool: {0} · Wayland-Treiber: {1} · DPI: {2} · Überwachung: {3}').format((tool.name if tool else lib.compat_tool + tr(' (nicht gefunden)')) if lib.compat_tool else tr('globale Vorgabe'), WAYLAND_TEXT.get(lib.wayland_driver, lib.wayland_driver), lib.prefix_dpi or tr('globale Vorgabe'), tr('an') if lib.watch else tr('aus')))
    print(tr('  Startoptionen: {0}').format(lib.launch_options or '–'))


def _library_defaults(b: Backend, a) -> dict:
    out = {}
    if getattr(a, "tool", None) is not None:
        v = a.tool
        if v not in ("", "auto", "default") and compat.resolve(v, b.launcher.tools()) is None:
            hits = [t for t in b.launcher.tools() if v.lower() in t.name.lower() or v.lower() in t.id.lower()]
            if len(hits) != 1:
                sys.exit(tr('Tool nicht gefunden – IDs mit »galaxy-linux tools« anzeigen.'))
            v = hits[0].id
        out["compat_tool"] = v
    for key, attr in (("launch_options", "options"), ("wayland_driver", "wayland"), ("prefix_dpi", "dpi")):
        if getattr(a, attr, None) is not None:
            out[key] = getattr(a, attr)
    return out


def cmd_library(b: Backend, a) -> int:
    if a.action == "list":
        libs = b.libraries()
        if a.json:
            print(json.dumps([{**lib.__dict__, "games": len(b.library_games(lib.id))} for lib in libs], indent=2, ensure_ascii=False))
            return 0
        for lib in libs:
            _print_library(b, lib)
        if not libs:
            print(tr('Keine eigenen Bibliotheken – anlegen mit »galaxy-linux library add NAME ORDNER«.'))
        return 0
    if a.action == "add":
        lib = b.create_library(a.name, a.games_dir, a.prefix_dir or "", **_library_defaults(b, a))
        print("angelegt:")
        _print_library(b, lib)
        if not a.no_scan:
            rep = b.scan_install_dirs(create_prefixes=not a.no_prefix)
            new = [g for g in (rep.imported if rep else []) if g.library == lib.id]
            for g in new:
                print(tr('übernommen: {0} ({1}) · Startdatei {2}').format(g.title, g.product_id, g.executable))
            rc = 0
            for job in [j for j in b.jobs.active() if j.kind == "prefix"]:
                print(tr('Prefix für {0} …').format(job.title), flush=True)
                rc |= _wait_job(job)
            return rc
        return 0
    lib = b.find_library(a.library)
    if a.action == "show":
        _print_library(b, lib)
        return 0
    if a.action == "rename":
        old = lib.name
        b.rename_library(lib.id, a.new_name)
        print(tr('umbenannt: {0} → {1}').format(old, lib.name))
        return 0
    if a.action == "set":
        a.value = " ".join(x for x in a.value if x != "--") if isinstance(a.value, list) else a.value
        if a.key in ("tool", "compat_tool"):
            a.tool = a.value
            changes = _library_defaults(b, a)
        else:
            key = {"options": "launch_options", "wayland": "wayland_driver", "dpi": "prefix_dpi"}.get(a.key, a.key)
            changes = {key: a.value}
        b.update_library(lib.id, **changes)
        _print_library(b, lib)
        return 0
    if a.action == "remove":
        n = len(b.library_games(lib.id))
        if not a.yes:
            ans = input(tr('Bibliothek „{0}“ löschen? {1} Spiele verschwinden aus dem Client; Spielordner und Prefixe bleiben erhalten. [j/N] ').format(lib.name, n))
            if ans.lower() not in ("j", "ja", "y", "yes"):
                return 1
        games = b.remove_library(lib.id)
        print(tr('gelöscht: {0} – {1} Spiele ausgetragen, Dateien in {2} und {3} bleiben').format(lib.name, len(games), lib.games_dir, lib.prefix_dir))
        return 0
    return 1


def cmd_log(b: Backend, a) -> int:
    from .logsetup import LOG_FILE
    if a.path:
        print(LOG_FILE)
        return 0
    if a.clear:
        for f in LOG_FILE.parent.glob(LOG_FILE.name + "*"):
            f.unlink(missing_ok=True)
        print(tr('Protokoll geleert.'))
        return 0
    if not LOG_FILE.exists():
        print(tr('Noch kein Protokoll unter {0}').format(LOG_FILE))
        return 1
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-a.tail:]:
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="galaxy-linux", description=tr('Inoffizieller Client für GOG.com – Kommandozeile'))
    p.add_argument("--version", action="version", version=tr('galaxy-linux {0}').format(__version__))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login", help=tr('bei GOG anmelden')); s.add_argument("--code", help=tr('Weiterleitungs-URL oder Code')); s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help=tr('abmelden')).set_defaults(fn=cmd_logout)
    sub.add_parser("whoami", help=tr('angemeldeten Nutzer zeigen')).set_defaults(fn=cmd_whoami)
    s = sub.add_parser("list", help=tr('Bibliothek anzeigen')); s.add_argument("--installed", action="store_true"); s.add_argument("--json", action="store_true"); s.add_argument("--refresh", action="store_true", help=tr('Bibliothek neu von GOG laden')); s.add_argument("--external", action="store_true", help=tr('Spiele aller eigenen Bibliotheken anzeigen')); s.add_argument("--library", help=tr('nur Spiele dieser Bibliothek (Name oder ID)')); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("search", help=tr('Bibliothek durchsuchen')); s.add_argument("text"); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("info", help=tr('Details zu einem Spiel')); s.add_argument("game", help=tr('ID oder Titel(teil)')); s.add_argument("--builds", action="store_true"); s.set_defaults(fn=cmd_info)
    s = sub.add_parser("install", help=tr('installieren')); s.add_argument("game"); s.add_argument("--platform", choices=["windows", "linux"]); s.add_argument("--lang"); s.add_argument("--dir", help=tr('Spiele-Ordner')); s.add_argument("--build", help=tr('bestimmte Build-ID')); s.add_argument("--no-dlc", action="store_true"); s.add_argument("--offline", action="store_true", help=tr('Offline-Installer + innoextract erzwingen')); s.set_defaults(fn=cmd_install)
    s = sub.add_parser("update", help=tr('aktualisieren')); s.add_argument("game"); s.add_argument("--build"); s.set_defaults(fn=cmd_update)
    s = sub.add_parser("verify", help=tr('Dateien prüfen/reparieren')); s.add_argument("game"); s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("move", help=tr('Installation in einen anderen Basisordner verschieben')); s.add_argument("game"); s.add_argument("--dir", required=True, help=tr('neuer Basisordner (das Spiel bekommt dort seinen Unterordner)')); s.set_defaults(fn=cmd_move)
    s = sub.add_parser("scan", help=tr('Spiele-Ordner nach kopierten Spielen durchsuchen und fehlende Prefixe anlegen')); s.add_argument("--no-prefix", action="store_true", help=tr('keine Prefixe anlegen')); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("add", help=tr('Spiel aus einem beliebigen Ordner hinzufügen (GOG oder extern)')); s.add_argument("path"); s.add_argument("--title"); s.add_argument("--exe", help=tr('Startdatei relativ zum Ordner')); s.add_argument("--library", help=tr('eigene Bibliothek für externe Spiele (Name oder ID)')); s.add_argument("--no-prefix", action="store_true"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("remove", help=tr('Spiel aus der Bibliothek entfernen')); s.add_argument("game"); s.add_argument("--delete-files", action="store_true"); s.add_argument("--delete-prefix", action="store_true"); s.add_argument("-y", "--yes", action="store_true"); s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("prefixes", help=tr('Prefixe global verwalten')); s.add_argument("--create-missing", action="store_true"); s.add_argument("--delete-orphans", action="store_true"); s.add_argument("--library", help=tr('nur diese Bibliothek')); s.add_argument("-y", "--yes", action="store_true"); s.set_defaults(fn=cmd_prefixes)
    s = sub.add_parser("shortcut", help=tr('Verknüpfung im Startmenü/auf dem Schreibtisch anlegen oder entfernen')); s.add_argument("game"); s.add_argument("--menu", action="store_true"); s.add_argument("--desktop", action="store_true"); s.add_argument("--remove", action="store_true"); s.set_defaults(fn=cmd_shortcut)
    s = sub.add_parser("uninstall", help=tr('deinstallieren')); s.add_argument("game"); s.add_argument("--prefix", action="store_true", help=tr('Wine-Prefix mitlöschen')); s.add_argument("-y", "--yes", action="store_true"); s.set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("launch", help=tr('starten')); s.add_argument("game"); s.add_argument("--task", help=tr('Name der Startdatei (playTask)')); s.add_argument("--dry-run", action="store_true", help=tr('nur Befehl anzeigen')); s.add_argument("--no-wait", action="store_true"); s.add_argument("args", nargs="*", help=tr('zusätzliche Argumente für das Spiel (nach --)')); s.set_defaults(fn=cmd_launch)
    s = sub.add_parser("tools", help=tr('gefundene Proton-/Wine-Versionen')); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_tools)
    s = sub.add_parser("config", help=tr('Einstellungen anzeigen und ändern')); cs = s.add_subparsers(dest="what", required=True)
    c = cs.add_parser("show", help=tr('Pro-Spiel-Einstellungen zeigen')); c.add_argument("game")
    c = cs.add_parser("tool", help=tr('Kompatibilitätstool setzen (ID, Namensteil, auto)')); c.add_argument("game"); c.add_argument("value")
    c = cs.add_parser("options", help=tr('Startoptionen setzen (Steam-Syntax)')); c.add_argument("game"); c.add_argument("value")
    c = cs.add_parser("env", help=tr("Umgebungsvariablen setzen (KEY=VAL) oder löschen (KEY)")); c.add_argument("game"); c.add_argument("values", nargs="+")
    c = cs.add_parser("prefix", help=tr('Prefix-Pfad setzen')); c.add_argument("game"); c.add_argument("value")
    c = cs.add_parser("set", help=tr('beliebige Pro-Spiel-Einstellung')); c.add_argument("game"); c.add_argument("key"); c.add_argument("value")
    c = cs.add_parser("global", help=tr('globale Einstellung zeigen/setzen (z. B. install_dir /pfad)')); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    c = cs.add_parser("locations", help=tr('Installationsorte anzeigen/ergänzen')); c.add_argument("--add"); c.add_argument("--remove")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("goverlay-import", help=tr('Startoptionen aus GOverlay/MangoHud ableiten')); s.add_argument("game", nargs="?"); s.set_defaults(fn=cmd_goverlay)
    s = sub.add_parser("updates", help=tr('nach Spiel-Updates suchen')); s.add_argument("--install", action="store_true"); s.set_defaults(fn=cmd_updates)
    s = sub.add_parser("log", help=tr('Protokoll anzeigen (für die Auswertung)')); s.add_argument("--tail", type=int, default=200); s.add_argument("--path", action="store_true"); s.add_argument("--clear", action="store_true"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("library", help=tr('eigene Bibliotheken anlegen, umbenennen, ändern, löschen')); ls = s.add_subparsers(dest="action", required=True)
    c = ls.add_parser("list", help=tr('Bibliotheken mit Ordnern und Vorgaben')); c.add_argument("--json", action="store_true")
    c = ls.add_parser("show", help=tr('eine Bibliothek anzeigen')); c.add_argument("library")
    c = ls.add_parser("add", help=tr('Bibliothek anlegen (Spiele-Ordner wird bei Bedarf erstellt)')); c.add_argument("name"); c.add_argument("games_dir")
    c.add_argument("--prefix-dir", help=tr('Prefix-Ordner (Vorgabe: <Spiele-Ordner>/prefixes)')); c.add_argument("--tool", help=tr('Kompatibilitätstool-Vorgabe (ID oder Namensteil)'))
    c.add_argument("--options", help=tr('Startoptionen-Vorgabe (Steam-Syntax)')); c.add_argument("--wayland", choices=["on", "off", "default"], help=tr('Wayland-Treiber-Vorgabe'))
    c.add_argument("--dpi", type=int, help=tr('DPI für neue Prefixe (0 = globale Vorgabe)')); c.add_argument("--no-scan", action="store_true", help=tr('Ordner nicht gleich durchsuchen'))
    c.add_argument("--no-prefix", action="store_true", help=tr('beim Durchsuchen keine Prefixe anlegen'))
    c = ls.add_parser("rename", help=tr('Bibliothek umbenennen')); c.add_argument("library"); c.add_argument("new_name")
    c = ls.add_parser("set", help=tr('Ordner oder Vorgabe ändern: games_dir, prefix_dir, tool, options, wayland (on/off/default), dpi, watch'))
    c.add_argument("library"); c.add_argument("key"); c.add_argument("value", nargs=argparse.REMAINDER, help=tr('Wert (darf mit - beginnen)'))
    c = ls.add_parser("remove", help=tr('Bibliothek löschen (Spielordner und Prefixe bleiben)')); c.add_argument("library"); c.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_library)
    s = sub.add_parser("prefix", help=tr('Prefix-Werkzeuge (setup = Einrichtung wie vor dem ersten Start erneut ausführen)')); s.add_argument("action", choices=["create", "delete", "reset", "winetricks", "run", "path", "setup"]); s.add_argument("game"); s.add_argument("verbs", nargs="*"); s.set_defaults(fn=cmd_prefix)
    s = sub.add_parser("records", help=tr('Installations-Datensätze (Build, Abhängigkeiten) anzeigen oder anlegen')); s.add_argument("game", nargs="?"); s.add_argument("--create", action="store_true", help=tr('fehlende Datensätze über das GOG-Content-System anlegen')); s.add_argument("--force", action="store_true", help=tr('vorhandene Datensätze neu anlegen')); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_records)
    s = sub.add_parser("comet", help=tr('Galaxy-Dienste (comet): Status anzeigen oder herunterladen')); s.add_argument("--download", action="store_true", help=tr('comet und den GalaxyCommunication-Dienst herunterladen bzw. aktualisieren')); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_comet)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        b = Backend()
        return int(args.fn(b, args) or 0)
    except NotLoggedIn:
        print(tr('Nicht angemeldet – bitte »galaxy-linux login« ausführen.'), file=sys.stderr)
        return 2
    except (ApiError, LaunchError, launch_options.LaunchOptionsError, auth.AuthError, InstallError) as exc:
        print(tr('Fehler: {0}').format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(tr('\nAbgebrochen.'), file=sys.stderr)
        return 130
