"""Download-Ansicht wie in GOG Galaxy 2.1 (``downloadsView``): Warteschlange, Erhältliche Updates, Abgeschlossen,
Fehlgeschlagen; Alles pausieren / Alles fortsetzen / Alles abbrechen (mit Rückfrage) / Leeren; je Auftrag pausieren,
fortsetzen, „Nach oben verschieben“, abbrechen, umsortieren durch Ziehen und Ablegen; Geschwindigkeitsgraph mit
Aktuell, Durchschnitt, Spitze, Heruntergeladen, Verbleibend, Gesamt und dem Hinweis auf die Bandbreitenbegrenzung."""
from __future__ import annotations

from ..i18n import tr

import time
from collections import deque

from ..content import bandwidth, phases
from ..settings import format_bytes
from .util import (Adw, Gdk, GLib, GObject, Gtk, clamped_picture, fmt_eta, fmt_speed, icon_button, label, pick_icon)

GRAPH_SECONDS = 120


class SpeedGraph(Gtk.DrawingArea):
    """Netz-Geschwindigkeit der letzten zwei Minuten als Fläche (eine Probe je Sekunde)."""

    def __init__(self) -> None:
        super().__init__()
        self.samples: deque[float] = deque(maxlen=GRAPH_SECONDS)
        self.set_content_height(64)
        self.set_hexpand(True)
        self.add_css_class("speed-graph")
        self.set_draw_func(self._draw)

    def add(self, value: float) -> None:
        self.samples.append(max(0.0, value))
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if not self.samples:
            return
        peak = max(self.samples) or 1.0
        n = self.samples.maxlen or GRAPH_SECONDS
        step = width / max(1, n - 1)
        x0 = width - step * (len(self.samples) - 1)
        rgba = self.get_color()
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.25)
        cr.move_to(x0, height)
        for i, v in enumerate(self.samples):
            cr.line_to(x0 + i * step, height - (v / peak) * (height - 4))
        cr.line_to(x0 + (len(self.samples) - 1) * step, height)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.9)
        cr.set_line_width(1.5)
        for i, v in enumerate(self.samples):
            y = height - (v / peak) * (height - 4)
            (cr.move_to if i == 0 else cr.line_to)(x0 + i * step, y)
        cr.stroke()


class DownloadsView(Gtk.ScrolledWindow):
    def __init__(self, win) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.backend = win.backend
        self.rows: dict[str, dict] = {}
        self.updates_hdr: Gtk.Widget | None = None
        self.first_update_btn: Gtk.Widget | None = None
        self._structure: tuple = ()
        self._speed_job = ""
        self._speeds: list[float] = []
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=18, margin_bottom=28,
                        margin_start=28, margin_end=28, valign=Gtk.Align.START)
        head = Gtk.Box(spacing=8)
        title = label(tr('DOWNLOADS'), css=["sec-hdr"])
        title.set_hexpand(True)
        head.append(title)
        self.pause_all = Gtk.Button(label=tr('Alles pausieren'))
        self.pause_all.connect("clicked", lambda *_: self.backend.jobs.pause_all())
        self.resume_all = Gtk.Button(label=tr('Alles fortsetzen'))
        self.resume_all.connect("clicked", lambda *_: self.backend.jobs.resume_all())
        self.cancel_all = Gtk.Button(label=tr('Alles abbrechen'))
        self.cancel_all.connect("clicked", self._confirm_cancel_all)
        self.clear = Gtk.Button(label=tr('Leeren'))
        self.clear.set_tooltip_text(tr('Abgeschlossene und fehlgeschlagene Einträge entfernen'))
        self.clear.connect("clicked", lambda *_: (self.backend.jobs.clear_finished(), self.rebuild()))
        for b in (self.pause_all, self.resume_all, self.cancel_all, self.clear):
            b.add_css_class("hdr-pill")
            head.append(b)
        outer.append(head)

        # Geschwindigkeit („Wird gerade heruntergeladen“)
        self.speed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.speed_box.add_css_class("panel")
        self.speed_title = label("", css=["heading"], ellipsize=True)
        self.speed_box.append(self.speed_title)
        self.graph = SpeedGraph()
        self.speed_box.append(self.graph)
        stats = Gtk.Box(spacing=26)
        self.stat_labels: dict[str, Gtk.Label] = {}
        for key, name in (("current", tr('Aktuell')), ("average", tr('Durchschnitt')), ("peak", tr('Spitze')),
                          ("downloaded", tr('Heruntergeladen')), ("remaining", tr('Verbleibend')), ("total", tr('Gesamt'))):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.append(label(name.upper(), css=["stat-key"]))
            v = label("–", css=["stat-val"])
            col.append(v)
            stats.append(col)
            self.stat_labels[key] = v
        self.speed_box.append(stats)
        self.limit_box = Gtk.Box(spacing=8)
        self.limit_lbl = label("", css=["dim"], wrap=True)
        self.limit_lbl.set_hexpand(True)
        self.limit_box.append(self.limit_lbl)
        settings_btn = Gtk.Button(label=tr('Download-Einstellungen öffnen'))
        settings_btn.add_css_class("flat")
        settings_btn.connect("clicked", lambda *_: self.win.open_preferences("downloads"))
        self.limit_box.append(settings_btn)
        self.speed_box.append(self.limit_box)
        outer.append(self.speed_box)

        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.append(self.sections)
        self.set_child(outer)
        self._timer = GLib.timeout_add_seconds(1, self._tick)

    # ------------------------------------------------------------------ Aufbau
    def _key(self) -> tuple:
        jobs = self.backend.jobs
        return (tuple((j.id, j.state) for j in jobs.queue()),
                tuple((j.id, j.state) for j in jobs.jobs if not j.active),
                tuple(sorted(pid for pid in self.backend.updates if not jobs.active_for(pid))))

    def refresh(self) -> None:
        """Nach einer Auftragsänderung: bei neuer Struktur neu aufbauen, sonst nur die Zeilen aktualisieren."""
        if self._key() != self._structure:
            self.rebuild()
        else:
            for j in self.backend.jobs.queue():
                self.update_job(j)
        self._update_controls()

    def rebuild(self) -> None:
        self._structure = self._key()
        child = self.sections.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.sections.remove(child)
            child = nxt
        self.rows.clear()
        self.updates_hdr = None
        self.first_update_btn = None
        jobs = self.backend.jobs
        queue = jobs.queue()
        finished = [j for j in jobs.jobs if not j.active]
        done = sorted([j for j in finished if j.state == "done"], key=lambda j: -j.finished_at)
        failed = sorted([j for j in finished if j.state in ("failed", "cancelled")], key=lambda j: -j.finished_at)
        updates = [pid for pid in self.backend.updates if not jobs.active_for(pid)]
        if not (queue or finished or updates):
            st = Adw.StatusPage(title=tr('Deine Warteschlange ist leer'),
                                description=tr('Hier siehst du alle deine abgeschlossenen und aktiven Spiele-Installationen & -Updates.'))
            st.set_icon_name(pick_icon("folder-download-symbolic", "go-down-symbolic"))
            self.sections.append(st)
        self._section(tr('Warteschlange'), queue, self._job_row)
        self.updates_hdr = self._section(tr('Erhältliche Updates'), updates, self._update_row)
        self._section(tr('Abgeschlossen'), done[:30], self._job_row)
        self._section(tr('Fehlgeschlagen'), failed[:30], self._job_row)
        for j in queue:
            self.update_job(j)
        self._update_controls()

    def _section(self, title: str, items: list, make) -> Gtk.Widget | None:
        if not items:
            return None
        hdr = label(f"{title.upper()}  {len(items)}", css=["sec-hdr"])
        hdr.set_margin_top(12)
        self.sections.append(hdr)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("dl-list")
        for it in items:
            lst.append(make(it))
        self.sections.append(lst)
        return hdr

    def reveal_updates(self) -> None:
        """Zu „Erhältliche Updates“ rollen und den ersten Update-Knopf fokussieren (Fußzeile der Seitenleiste)."""
        GLib.timeout_add(120, self._do_reveal)

    def _do_reveal(self) -> bool:
        hdr = getattr(self, "updates_hdr", None)
        if hdr is None or hdr.get_parent() is None:
            return False
        try:
            from gi.repository import Graphene
            ok, pt = hdr.compute_point(self.get_child(), Graphene.Point().init(0, 0))
            y = pt.y if ok else None
        except Exception:  # pragma: no cover – ältere GTK-Fassungen
            res = hdr.translate_coordinates(self.get_child(), 0, 0)
            y = res[1] if res and res[0] is not False else None
        if y is not None:
            self.get_vadjustment().set_value(max(0.0, y - 12))
        if self.first_update_btn is not None:
            self.first_update_btn.grab_focus()
        return False

    def _cover(self, pid: str) -> Gtk.Widget:
        frame, pic = clamped_picture(40, 56, ["cover"])
        e = self.backend.entry(pid)
        if e:
            self.win.load_cover(e, pic)
        frame.set_valign(Gtk.Align.CENTER)
        return frame

    def _job_row(self, job) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False)
        row.add_css_class("dl-row")
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if job.active:
            handle = Gtk.Image(icon_name=pick_icon("list-drag-handle-symbolic", "open-menu-symbolic"), pixel_size=16)
            handle.set_tooltip_text(tr('Neusortieren durch ziehen und ablegen'))
            handle.add_css_class("dim")
            box.append(handle)
        box.append(self._cover(job.product_id))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        title = Gtk.Button(child=label(job.title, css=["heading"], ellipsize=True))
        title.add_css_class("flat")
        title.add_css_class("dl-title")
        title.set_tooltip_text(tr('Zum Spiel'))
        title.connect("clicked", lambda *_: self.win.show_game(job.product_id)
                      if self.backend.entry(job.product_id) else None)
        top.append(title)
        mid.append(top)
        status = label("", css=["dim", "caption"], ellipsize=True)
        mid.append(status)
        bar = Gtk.ProgressBar()
        bar.set_visible(job.active)
        mid.append(bar)
        box.append(mid)
        btns = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        w = {"status": status, "bar": bar, "row": row}
        if job.active:
            if job.pausable:
                pr = icon_button(pick_icon("media-playback-pause-symbolic"), tr('Pausieren'), ["flat", "hdr-btn"])
                pr.connect("clicked", lambda *_: self._toggle_pause(job))
                btns.append(pr)
                w["pause"] = pr
            front = icon_button(pick_icon("go-top-symbolic", "go-up-symbolic"), tr('Nach oben verschieben'), ["flat", "hdr-btn"])
            front.connect("clicked", lambda *_: self.backend.jobs.move_to_front(job))
            front.set_visible(job.state != "running")
            btns.append(front)
            stop = icon_button(pick_icon("process-stop-symbolic", "window-close-symbolic"), tr('Abbrechen'), ["flat", "hdr-btn"])
            stop.connect("clicked", lambda *_: self.backend.jobs.cancel(job))
            btns.append(stop)
            self._make_draggable(row, job)
        else:
            if job.state in ("failed", "cancelled") and (job.kind.startswith("install") or job.kind in ("update", "verify", "extras")):
                again = Gtk.Button(label=tr('Erneut versuchen'))
                again.add_css_class("flat")
                again.connect("clicked", lambda *_: self._retry(job))
                btns.append(again)
            rm = icon_button(pick_icon("window-close-symbolic"), tr('Entfernen'), ["flat", "hdr-btn"])
            rm.connect("clicked", lambda *_: (self.backend.jobs.remove(job), self.rebuild()))
            btns.append(rm)
        box.append(btns)
        row.set_child(box)
        self.rows[job.id] = w
        if not job.active:
            self._finished_text(job, w)
        return row

    def _update_row(self, pid: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(self._cover(pid))
        e = self.backend.entry(pid)
        b = self.backend.updates.get(pid)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        mid.append(label(e.title if e else pid, css=["heading"], ellipsize=True))
        mid.append(label(tr('Version {0}').format(b.version_name or b.build_id) if b else "", css=["dim", "caption"]))
        box.append(mid)
        up = Gtk.Button(label=tr('Update'), valign=Gtk.Align.CENTER)
        up.add_css_class("btn-secondary")
        if self.first_update_btn is None:
            self.first_update_btn = up
        up.connect("clicked", lambda *_: (self.backend.update(pid), self.refresh()))
        box.append(up)
        row.set_child(box)
        return row

    # ------------------------------------------------------------------ Ziehen und Ablegen
    def _make_draggable(self, row: Gtk.ListBoxRow, job) -> None:
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(GObject.Value(str, job.id)))
        row.add_controller(src)
        drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop.connect("drop", lambda _t, value, _x, _y: self._drop(value, job))
        row.add_controller(drop)

    def _drop(self, job_id: str, target) -> bool:
        moving = self.backend.jobs.get(str(job_id))
        if moving is None or moving is target:
            return False
        queue = self.backend.jobs.queue()
        if target not in queue:
            return False
        self.backend.jobs.move(moving, queue.index(target))
        self.rebuild()
        return True

    # ------------------------------------------------------------------ Aktionen
    def _toggle_pause(self, job) -> None:
        if job.state == "paused":
            self.backend.jobs.resume(job)
        else:
            self.backend.jobs.pause(job)

    def _retry(self, job) -> None:
        from ..backend import Job
        new = Job(kind="install" if job.kind == "install-offline" else job.kind, product_id=job.product_id, title=job.title,
                  platform=job.platform, language=job.language, install_root=job.install_root, build_id=job.build_id,
                  include_dlcs=job.include_dlcs, dlc_filter=job.dlc_filter, shortcut_menu=job.shortcut_menu,
                  shortcut_desktop=job.shortcut_desktop, data=dict(job.data))
        self.backend.jobs.remove(job)
        self.backend.jobs.add(new)
        self.rebuild()

    def _confirm_cancel_all(self, *_: object) -> None:
        dlg = Adw.AlertDialog.new(tr('Alles abbrechen'),
                                  tr('Dadurch werden alle Elemente in der Warteschlange gestoppt, einschließlich aktiver Downloads und Installationen.'))
        dlg.add_response("keep", tr('Nein, beibehalten'))
        dlg.add_response("cancel", tr('Ja, alles abbrechen'))
        dlg.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")
        dlg.set_close_response("keep")
        dlg.connect("response", lambda _d, r: self.backend.jobs.cancel_all() if r == "cancel" else None)
        dlg.present(self.win)

    # ------------------------------------------------------------------ Anzeige
    def _update_controls(self) -> None:
        q = self.backend.jobs.queue()
        self.pause_all.set_sensitive(any(j.pausable and j.state in ("queued", "running") for j in q))
        self.resume_all.set_sensitive(any(j.state == "paused" for j in q))
        self.cancel_all.set_sensitive(bool(q))
        self.clear.set_sensitive(any(not j.active for j in self.backend.jobs.jobs))

    def _status_text(self, job) -> str:
        p = job.progress
        if job.state == "paused":
            reason = {"game": tr('Die Einstellung „Downloads während des Spiels pausieren“ ist aktiviert.')}.get(job.pause_reason, "")
            return " · ".join(x for x in (job.label, tr('Pausiert'), reason) if x)
        if job.state == "queued":
            if self.backend.jobs.play_hold and job.pausable:
                return f"{job.label} · " + tr('Downloads im Spiel pausiert')
            return f"{job.label} · " + tr('In der Warteschlange')
        step = phases.label(job.step)
        if p.total_bytes:
            return tr('{0} · {1} · {2} / {3} · {4} · ETA {5} · {6}/{7} Dateien').format(
                job.label, step or job.status, format_bytes(p.done_bytes), format_bytes(p.total_bytes), fmt_speed(p.speed),
                fmt_eta(p.eta_seconds), p.files_done, p.files_total)
        return " · ".join(x for x in (job.label, step, job.status if job.status != step else "") if x)

    def _finished_text(self, job, w: dict) -> None:
        when = time.strftime("%H:%M", time.localtime(job.finished_at)) if job.finished_at else ""
        state = {"done": tr('Abgeschlossen'), "failed": tr('Fehlgeschlagen'), "cancelled": tr('Abgebrochen')}.get(job.state, job.status)
        w["status"].set_text(" · ".join(x for x in (job.label, state, when, job.error) if x))
        w["bar"].set_visible(False)

    def update_job(self, job) -> None:
        w = self.rows.get(job.id)
        if not w:
            return
        if not job.active:
            self._finished_text(job, w)
            return
        p = job.progress
        w["status"].set_text(self._status_text(job))
        w["row"].set_tooltip_text(phases.tooltip(job.steps, job.step) or None)
        if p.total_bytes:
            w["bar"].set_fraction(p.fraction)
        elif job.state == "running":
            w["bar"].pulse()
        if "pause" in w:
            paused = job.state == "paused"
            w["pause"].set_icon_name(pick_icon("media-playback-start-symbolic") if paused else pick_icon("media-playback-pause-symbolic"))
            w["pause"].set_tooltip_text(tr('Fortsetzen') if paused else tr('Pausieren'))

    def _tick(self) -> bool:
        """Jede Sekunde: Graph und Kennzahlen des laufenden Downloads, Hinweis auf Begrenzung bzw. Spiel-Pause."""
        if not self.get_mapped():
            return GLib.SOURCE_CONTINUE
        running = next((j for j in self.backend.jobs.queue() if j.state == "running" and j.progress.total_bytes), None)
        s = self.backend.settings
        if running is not None:
            if running.id != self._speed_job:
                self._speed_job, self._speeds = running.id, []
                self.graph.samples.clear()
            p = running.progress
            self._speeds.append(p.speed)
            self.graph.add(p.speed)
            nonzero = [v for v in self._speeds if v > 0]
            self.speed_title.set_text(tr('Wird gerade heruntergeladen') + f": {running.title}")
            vals = {"current": fmt_speed(p.speed), "average": fmt_speed(sum(nonzero) / len(nonzero) if nonzero else 0),
                    "peak": fmt_speed(max(nonzero) if nonzero else 0), "downloaded": format_bytes(p.downloaded_bytes),
                    "remaining": format_bytes(max(0, p.total_bytes - p.done_bytes)), "total": format_bytes(p.total_bytes)}
            for k, v in vals.items():
                self.stat_labels[k].set_text(v)
        self.speed_box.set_visible(running is not None or self.backend.jobs.play_hold)
        hints = []
        if bandwidth.limit_active(s):
            hints.append(tr('Bandbreitenbegrenzung ist aktiv') + " · " + tr('Download-Geschwindigkeit begrenzt auf:')
                         + " " + fmt_speed(bandwidth.current_rate(s)))
        if self.backend.jobs.play_hold:
            hints.append(tr('Downloads im Spiel pausiert'))
        self.limit_lbl.set_text("\n".join(hints))
        self.limit_box.set_visible(bool(hints))
        if running is None and self.backend.jobs.play_hold:
            self.speed_title.set_text(tr('Downloads pausiert'))
        return GLib.SOURCE_CONTINUE
