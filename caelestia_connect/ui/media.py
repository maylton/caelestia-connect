"""Media card modelled on the Caelestia dashboard: cookie-shaped cover with a ring of bars, wavy
progress slider and the shuffle / previous / play / next / repeat button row."""

from __future__ import annotations

import math
import time

import cairo

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from ..i18n import _
from . import widgets as w
from .controls import Slider

BARS = 45
COOKIE_LOBES = 9
COOKIE_DEPTH = 0.06


def _cookie_radius(angle: float, radius: float, rotation: float) -> float:
    return radius * (1 - COOKIE_DEPTH + COOKIE_DEPTH * math.cos(COOKIE_LOBES * (angle - rotation)))


class CoverVisualiser(Gtk.DrawingArea):
    """Album art clipped to Material's 9-sided cookie, surrounded by animated bars."""

    def __init__(self, theme, size: int = 216, cover: int = 148) -> None:
        super().__init__(content_width=size, content_height=size, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.theme = theme
        self.cover = cover
        self.pixbuf: GdkPixbuf.Pixbuf | None = None
        self.path = ""
        self.playing = False
        self.levels = [0.02] * BARS
        self.rotation = 0.0
        self._last = time.monotonic()
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def set_art(self, path: str) -> None:
        if path == self.path:
            return
        self.path = path
        self.pixbuf = None
        if path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(path)
                side = min(pb.get_width(), pb.get_height())
                pb = pb.new_subpixbuf((pb.get_width() - side) // 2, (pb.get_height() - side) // 2, side, side)
                self.pixbuf = pb.scale_simple(self.cover * 2, self.cover * 2, GdkPixbuf.InterpType.BILINEAR)
            except GLib.Error:
                self.pixbuf = None
        self.queue_draw()

    def _tick(self, _widget, _clock) -> bool:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if not self.get_mapped():
            return True
        changed = False
        for i in range(BARS):
            if self.playing:
                # Decorative motion: the audio plays on the device, so there is no spectrum to show
                target = 0.18 + 0.32 * (0.5 + 0.5 * math.sin(now * 2.1 + i * 0.55)) * (
                    0.6 + 0.4 * math.sin(now * 0.7 + i * 1.7)
                )
            else:
                target = 0.02
            new = self.levels[i] + (target - self.levels[i]) * min(1.0, dt * 6)
            if abs(new - self.levels[i]) > 1e-4:
                changed = True
            self.levels[i] = new
        if self.playing:
            self.rotation = (self.rotation + dt * 0.25) % (2 * math.pi / COOKIE_LOBES)
            changed = True
        if changed:
            self.queue_draw()
        return True

    def _cookie(self, cr, cx: float, cy: float, radius: float) -> None:
        steps = 180
        for s in range(steps + 1):
            a = 2 * math.pi * s / steps
            r = _cookie_radius(a, radius, self.rotation)
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            if s == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.close_path()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        cx, cy = width / 2, height / 2
        radius = self.cover / 2
        spacing = 12
        max_mag = (min(width, height) - self.cover) / 2 - spacing
        stroke = max(2.0, 360 / BARS - 2) * 0.55
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(stroke)
        cr.set_source_rgba(*self.theme.rgba("primary"))
        for i, level in enumerate(self.levels):
            a = 2 * math.pi * i / BARS
            start = _cookie_radius(a, radius, self.rotation) + spacing + stroke / 2
            end = start + max(0.01, min(1.0, level)) * max_mag
            cr.move_to(cx + start * math.cos(a), cy + start * math.sin(a))
            cr.line_to(cx + end * math.cos(a), cy + end * math.sin(a))
            cr.stroke()
        self._cookie(cr, cx, cy, radius)
        if self.pixbuf is not None:
            cr.save()
            cr.clip()
            cr.translate(cx - radius, cy - radius)
            cr.scale(0.5, 0.5)
            Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
            cr.paint()
            cr.restore()
        else:
            cr.set_source_rgba(*self.theme.rgba("surfaceContainerHighest"))
            cr.fill()
            cr.set_source_rgba(*self.theme.rgba("onSurfaceVariant"))
            cr.select_font_face("Material Symbols Rounded")
            cr.set_font_size(56)
            glyph = "\ue405"  # music_note (Cairo's toy text API has no ligatures)
            ext = cr.text_extents(glyph)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
            cr.show_text(glyph)


class MediaCard(Gtk.Box):
    def __init__(self, app, on_action, on_seek, on_select, on_volume) -> None:
        super().__init__(spacing=24)
        self.add_css_class("surface-card")
        self.add_css_class("media-card")
        self.app = app
        self.on_action = on_action
        self.on_seek_ms = on_seek
        self.on_select = on_select
        self.on_volume = on_volume
        self.length = 0
        self.position = 0
        self.stamp = time.monotonic()
        self.playing = False

        self.visualiser = CoverVisualiser(app.theme)
        self.append(self.visualiser)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        self.title = w.label(css=["media-title"], ellipsize=True)
        self.title.set_hexpand(True)
        top.append(self.title)
        self.player_menu = Gtk.DropDown(model=Gtk.StringList(), valign=Gtk.Align.START)
        self.player_menu.add_css_class("player-pill")
        self._handler = self.player_menu.connect("notify::selected", self._selected)
        top.append(self.player_menu)
        col.append(top)
        self.artist = w.label(css=["media-artist"], ellipsize=True)
        self.album = w.label(css=["media-album"], ellipsize=True)
        col.append(self.artist)
        col.append(self.album)

        prog = Gtk.Box(spacing=8, margin_top=22)
        self.pos_label = w.label("0:00", xalign=0.5, css=["media-time"])
        self.len_label = w.label("0:00", xalign=0.5, css=["media-time"])
        for lb in (self.pos_label, self.len_label):
            lb.set_width_chars(5)
        # Same parameters as the dashboard: StyledSlider { wavy; waveFrequency: 5; waveDuration: 2000 }
        self.slider = Slider(
            app.theme, track=12, radius=12, wavy=True, frequency=5, wave_period=2.0, on_release=self._seek
        )
        prog.append(self.pos_label)
        prog.append(self.slider)
        prog.append(self.len_label)
        col.append(prog)

        row = Gtk.Box(spacing=4, margin_top=16)
        self.shuffle = self._btn("shuffle", "media-side", lambda: self.on_action("shuffle"))
        self.prev = self._btn("skip_previous", "media-tonal", lambda: self.on_action("Previous"))
        self.play = self._btn("play_arrow", "media-play", lambda: self.on_action("PlayPause"))
        self.play.set_hexpand(True)
        self.next = self._btn("skip_next", "media-tonal", lambda: self.on_action("Next"))
        self.repeat = self._btn("repeat", "media-side", lambda: self.on_action("repeat"))
        for b in (self.shuffle, self.prev, self.play, self.next, self.repeat):
            row.append(b)
        col.append(row)

        vol = Gtk.Box(spacing=8, margin_top=10)
        vol.append(w.Icon("volume_up", "small", css=["dim"]))
        self.volume = Slider(
            app.theme, track=12, radius=12, step=0.05, on_release=lambda v: self.on_volume(int(round(v * 100)))
        )
        vol.append(self.volume)
        col.append(vol)
        self.append(col)
        GLib.timeout_add(250, self._refresh_time)

    def set_compact(self, compact: bool) -> None:
        self.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
        self.set_spacing(12 if compact else 24)

    @staticmethod
    def _btn(icon: str, css: str, cb) -> Gtk.Button:
        b = Gtk.Button()
        b.set_child(w.Icon(icon, "large" if css != "media-side" else "medium"))
        b.add_css_class("media-btn")
        b.add_css_class(css)
        b.connect("clicked", lambda *_: cb())
        return b

    def _selected(self, dd, _p) -> None:
        item = dd.get_selected_item()
        if item is not None:
            self.on_select(item.get_string())

    def _seek(self, fraction: float) -> None:
        if self.length > 0:
            ms = int(fraction * self.length)
            self.position, self.stamp = ms, time.monotonic()
            self.on_seek_ms(ms)

    def _current_pos(self) -> int:
        pos = self.position
        if self.playing:
            pos += int((time.monotonic() - self.stamp) * 1000)
        return min(pos, self.length) if self.length else pos

    def _refresh_time(self) -> bool:
        if self.get_mapped():
            pos = self._current_pos()
            if self.slider.dragging:
                pos = int(self.slider.drag_value * self.length)
            self.pos_label.set_label(w.format_ms(pos))
            if self.length and not self.slider.dragging:
                self.slider.set_value(pos / self.length, animate=False)
        return True

    def update(self, players: list[dict], current: str) -> None:
        names = [p["name"] for p in players]
        model = self.player_menu.get_model()
        if [model.get_string(i) for i in range(model.get_n_items())] != names:
            self.player_menu.handler_block(self._handler)
            model.splice(0, model.get_n_items(), names)
            self.player_menu.handler_unblock(self._handler)
        cur = current if current in names else names[0]
        if self.player_menu.get_selected() != names.index(cur):
            self.player_menu.handler_block(self._handler)
            self.player_menu.set_selected(names.index(cur))
            self.player_menu.handler_unblock(self._handler)
        self.player_menu.set_visible(len(names) > 1)
        p = next(x for x in players if x["name"] == cur)
        self.title.set_label(p["title"] or p["name"])
        self.artist.set_label(p["artist"] or _("Unknown artist"))
        self.album.set_label(p["album"] or _("Unknown album"))
        self.visualiser.set_art(p["art"])
        self.visualiser.playing = p["playing"]
        self.playing = p["playing"]
        self.slider.animate_wave = p["playing"]
        self.length = max(0, p["length"])
        if abs(p["position"] - self._current_pos()) > 1500 or not self.playing:
            self.position, self.stamp = p["position"], time.monotonic()
        self.slider.set_sensitive(p["canSeek"] and self.length > 0)
        self.len_label.set_label(w.format_ms(self.length) if self.length else "--:--")
        self.play.get_child().set_icon("pause" if p["playing"] else "play_arrow")
        if p["playing"]:
            self.play.add_css_class("checked")
        else:
            self.play.remove_css_class("checked")
        self.prev.set_sensitive(p["canGoPrevious"])
        self.next.set_sensitive(p["canGoNext"])
        self.play.set_sensitive(p["canPlay"] or p["canPause"])
        self.shuffle.set_sensitive(p.get("hasShuffle", False))
        self.repeat.set_sensitive(p.get("hasLoop", False))
        for b, on in ((self.shuffle, p["shuffle"]), (self.repeat, p["loopStatus"] in ("Track", "Playlist"))):
            if on:
                b.add_css_class("checked")
            else:
                b.remove_css_class("checked")
        self.repeat.get_child().set_icon("repeat_one" if p["loopStatus"] == "Track" else "repeat")
        if not self.volume.dragging:
            self.volume.set_value(p["volume"] / 100)
        self._refresh_time()
