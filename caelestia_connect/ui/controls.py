"""Caelestia's own controls, redrawn with Cairo: StyledSlider (flat or wavy) and StyledSwitch.

Geometry and colours follow components/controls/StyledSlider.qml and StyledSwitch.qml of the shell.
"""

from __future__ import annotations

import math
import time

import cairo
from collections.abc import Callable

from gi.repository import Adw, Gdk, GObject, Gtk

HANDLE_W = 4.0
GAP = 4.0  # Tokens.spacing.extraSmall
INNER_RADIUS = 2.0  # Tokens.rounding.extraSmall / 2


def _blend(a, b, t: float) -> tuple[float, float, float, float]:
    return tuple(a[i] * (1 - t) + b[i] * t for i in range(3)) + (a[3],)


def _rounded_rect(cr, x: float, y: float, w: float, h: float, tl: float, tr: float, br: float, bl: float) -> None:
    if w <= 0 or h <= 0:
        return
    limit = min(w, h) / 2
    tl, tr, br, bl = (min(r, limit) for r in (tl, tr, br, bl))
    cr.new_sub_path()
    cr.arc(x + w - tr, y + tr, tr, -math.pi / 2, 0)
    cr.arc(x + w - br, y + h - br, br, 0, math.pi / 2)
    cr.arc(x + bl, y + h - bl, bl, math.pi / 2, math.pi)
    cr.arc(x + tl, y + tl, tl, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Slider(Gtk.DrawingArea):
    """Material 3 expressive slider as drawn by the shell.

    track: bar thickness (24 in settings rows, 12 for the media progress), radius: outer corners,
    wavy: draw the filled part as an animated sine wave (media progress).
    on_change(value) fires while dragging and on scroll; on_release(value) when the gesture ends.
    """

    def __init__(
        self,
        theme,
        track: int = 24,
        radius: float = 8,
        wavy: bool = False,
        frequency: int = 6,
        wave_period: float = 1.0,
        step: float = 0.05,
        on_change: Callable[[float], None] | None = None,
        on_release: Callable[[float], None] | None = None,
    ) -> None:
        self.track = track
        t = min(1.0, max(0.0, (track - 12) / 16))
        self._mult_idle = _lerp(3.0, 1.2, t)
        self._mult_pressed = _lerp(3.5, 1.5, t)
        super().__init__(
            content_height=math.ceil(track * self._mult_pressed), hexpand=True, valign=Gtk.Align.CENTER, focusable=True
        )
        self.theme = theme
        self.radius = radius
        self.wavy = wavy
        self.frequency = frequency
        self.wave_period = wave_period
        self.step = step
        self.on_change = on_change
        self.on_release = on_release
        self.value = 0.0
        self.animate_wave = False
        self.dragging = False
        self.drag_value = 0.0
        self._shown = 0.0  # animated position (Behavior on filledWidth)
        self._handle_mult = self._mult_idle
        self._phase = 0.0
        self._last = time.monotonic()
        self._drag_start_x = 0.0
        self._drag_start_value = 0.0
        self._moved = False
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    def set_value(self, value: float, animate: bool = True) -> None:
        self.value = min(1.0, max(0.0, float(value)))
        if not animate:
            self._shown = self.value
        self.queue_draw()

    @property
    def shown_value(self) -> float:
        return self.drag_value if self.dragging else self.value

    def _usable_width(self) -> float:
        return max(1.0, self.get_width() - HANDLE_W - GAP)

    def _begin(self, _g, x, _y) -> None:
        if not self.get_sensitive():
            return
        self.grab_focus()
        self.dragging = True
        self._moved = False
        self._drag_start_x = x
        self._drag_start_value = self.value
        self.drag_value = self.value

    def _update(self, _g, ox, _oy) -> None:
        if not self.dragging:
            return
        if abs(ox) > 1:
            self._moved = True
        self.drag_value = min(1.0, max(0.0, self._drag_start_value + ox / self.get_width()))
        self._shown = self.drag_value
        if self.on_change:
            self.on_change(self.drag_value)

    def _end(self, _g, ox, _oy) -> None:
        if not self.dragging:
            return
        self.dragging = False
        # A plain click jumps to the clicked position, a drag keeps its relative movement
        final = self.drag_value if self._moved else min(1.0, max(0.0, (self._drag_start_x + ox) / self.get_width()))
        self.value = final
        if self._moved:
            self._shown = final  # a click instead animates towards the new position
        if self.on_release:
            self.on_release(final)
        elif self.on_change:
            self.on_change(final)

    def _nudge(self, delta: float) -> None:
        self.set_value(self.value + delta)
        cb = self.on_release or self.on_change
        if cb:
            cb(self.value)

    def _scroll(self, _c, _dx, dy) -> bool:
        if not self.get_sensitive():
            return False
        self._nudge(-self.step if dy > 0 else self.step)
        return True

    def _key(self, _c, keyval, _code, _state) -> bool:
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            self._nudge(-self.step)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            self._nudge(self.step)
            return True
        return False

    def _tick(self, _w, _clock) -> bool:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if not self.get_mapped():
            return True
        target = self.shown_value
        redraw = False
        if abs(self._shown - target) > 1e-4:
            self._shown += (target - self._shown) * min(1.0, dt * 12)
            redraw = True
        mult = self._mult_pressed if self.dragging else self._mult_idle
        if abs(self._handle_mult - mult) > 1e-3:
            self._handle_mult += (mult - self._handle_mult) * min(1.0, dt * 18)
            redraw = True
        if self.wavy and self.animate_wave:
            self._phase = (self._phase + dt / self.wave_period * 2 * math.pi) % (2 * math.pi)
            redraw = True
        if redraw:
            self.queue_draw()
        return True

    def _draw(self, _area, cr, width: int, height: int) -> None:
        enabled = self.get_sensitive()
        fg = self.theme.rgba("primary") if enabled else self.theme.rgba("onSurface", 0.38)
        bg = self.theme.rgba("secondaryContainer") if enabled else self.theme.rgba("onSurface", 0.1)
        cy = height / 2
        track = self.track
        filled_w = self._usable_width() * min(1.0, max(0.0, self._shown))

        cr.set_source_rgba(*fg)
        if self.wavy:
            lw = int(track * 0.7)
            amplitude = lw * 0.5
            full = self._usable_width()
            start, end = lw / 2, filled_w - lw / 2
            if end > start:
                cr.set_line_width(lw)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                x = start
                cr.move_to(x, cy + amplitude * math.sin(self.frequency * 2 * math.pi * x / full + self._phase))
                while x < end:
                    x = min(end, x + 1)
                    cr.line_to(x, cy + amplitude * math.sin(self.frequency * 2 * math.pi * x / full + self._phase))
                cr.stroke()
        elif filled_w > 0:
            _rounded_rect(cr, 0, cy - track / 2, filled_w, track, self.radius, INNER_RADIUS, INNER_RADIUS, self.radius)
            cr.fill()

        hx = filled_w + GAP if filled_w > 0 else 0
        hh = track * self._handle_mult
        _rounded_rect(cr, hx, cy - hh / 2, HANDLE_W, hh, 2, 2, 2, 2)
        cr.set_source_rgba(*fg)
        cr.fill()

        # Remaining part, shrinking as it gets shorter, with the stop indicator at the end
        rx = hx + HANDLE_W + GAP
        rw = width - rx
        if rw > 0:
            op = min(rw, 12) / 12
            rh = track * (op if track <= 12 else min(op * 2, 1))
            cr.set_source_rgba(bg[0], bg[1], bg[2], bg[3] * op)
            _rounded_rect(cr, rx, cy - rh / 2, rw, rh, INNER_RADIUS, self.radius, self.radius, INNER_RADIUS)
            cr.fill()
            dot = 4 * op
            if dot > 0.5:
                cr.set_source_rgba(fg[0], fg[1], fg[2], fg[3] * op)
                cr.arc(width - 4 * op - dot / 2, cy, dot / 2, 0, 2 * math.pi)
                cr.fill()


class Switch(Gtk.DrawingArea):
    """StyledSwitch: pill track, round thumb that stretches while pressed, check / cross glyph."""

    __gtype_name__ = "CaelestiaSwitch"

    WIDTH = 54
    HEIGHT = 32

    def __init__(self, theme, active: bool = False) -> None:
        super().__init__(
            content_width=self.WIDTH,
            content_height=self.HEIGHT,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
            focusable=True,
            accessible_role=Gtk.AccessibleRole.SWITCH,
        )
        self.theme = theme
        self._active = bool(active)
        self._pos = 1.0 if active else 0.0  # animated thumb position 0..1
        self._pressed = False
        self._hover = False
        self._stretch = 0.0
        self._last = time.monotonic()
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self._set_pressed(True))
        click.connect("released", self._released)
        click.connect("stopped", lambda *_: self._set_pressed(False))
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(True))
        motion.connect("leave", lambda *_: self._set_hover(False))
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["switch"])
        self._update_state()

    @GObject.Property(type=bool, default=False)
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        value = bool(value)
        if value != self._active:
            self._active = value
            self._update_state()
            self.queue_draw()

    def get_active(self) -> bool:
        return self._active

    def set_active(self, value: bool) -> None:
        self.set_property("active", value)

    def toggle(self) -> None:
        if self.get_sensitive():
            self.set_active(not self._active)

    def _update_state(self) -> None:
        self.update_state(
            [Gtk.AccessibleState.CHECKED],
            [Gtk.AccessibleTristate.TRUE if self._active else Gtk.AccessibleTristate.FALSE],
        )

    def _set_pressed(self, pressed: bool) -> None:
        self._pressed = pressed
        self.queue_draw()

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self.queue_draw()

    def _released(self, gesture, _n, x, y) -> None:
        self._pressed = False
        if 0 <= x <= self.get_width() and 0 <= y <= self.get_height():
            self.toggle()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _key(self, _c, keyval, _code, _state) -> bool:
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.toggle()
            return True
        return False

    def _tick(self, _w, _clock) -> bool:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        target = 1.0 if self._active else 0.0
        stretch = 1.0 if self._pressed else 0.0
        redraw = False
        if abs(self._pos - target) > 1e-3:
            self._pos += (target - self._pos) * min(1.0, dt * 14)
            redraw = True
        elif self._pos != target:
            self._pos = target
            redraw = True
        if abs(self._stretch - stretch) > 1e-3:
            self._stretch += (stretch - self._stretch) * min(1.0, dt * 18)
            redraw = True
        if redraw:
            self.queue_draw()
        return True

    def _draw(self, _area, cr, width: int, height: int) -> None:
        enabled = self.get_sensitive()
        t = self._pos
        th = self.theme
        if enabled:
            # Colours.layer() in the shell lifts the off state above the row it sits on
            on_track, off_track = (
                th.rgba("primary"),
                _blend(th.rgba("surfaceContainerHighest"), th.rgba("onSurface"), 0.1),
            )
            on_thumb, off_thumb = th.rgba("onPrimary"), _blend(th.rgba("outline"), th.rgba("onSurface"), 0.15)
            on_icon, off_icon = th.rgba("primary"), th.rgba("surfaceContainerHighest")
        else:
            on_track, off_track = th.rgba("onSurface", 0.12), th.rgba("surfaceContainerHighest", 0.38)
            on_thumb, off_thumb = th.rgba("surface"), th.rgba("onSurface", 0.12)
            on_icon, off_icon = th.rgba("outline"), th.rgba("surfaceContainer")

        def mix(a, b):
            return tuple(a[i] * t + b[i] * (1 - t) for i in range(4))

        x0, y0 = (width - self.WIDTH) / 2, (height - self.HEIGHT) / 2
        _rounded_rect(cr, x0, y0, self.WIDTH, self.HEIGHT, *(self.HEIGHT / 2,) * 4)
        cr.set_source_rgba(*mix(on_track, off_track))
        cr.fill()

        pad = 2.0
        th_h = self.HEIGHT - 4
        th_w = th_h * (1 + 0.2 * self._stretch)
        tx = x0 + pad + (self.WIDTH - th_w - 2 * pad) * t
        ty = y0 + (self.HEIGHT - th_h) / 2
        _rounded_rect(cr, tx, ty, th_w, th_h, *(th_h / 2,) * 4)
        cr.set_source_rgba(*mix(on_thumb, off_thumb))
        cr.fill()
        if self._hover or self._pressed:
            hl = mix(th.rgba("primary"), th.rgba("onSurface"))
            _rounded_rect(cr, tx, ty, th_w, th_h, *(th_h / 2,) * 4)
            cr.set_source_rgba(hl[0], hl[1], hl[2], 0.1 if self._pressed else 0.08)
            cr.fill()

        # Glyph: a check when on, a cross when off, morphing between both
        size = th_h - 12
        gx, gy = tx + (th_w - size) / 2, ty + (th_h - size) / 2

        def P(fx, fy):
            return gx + size * fx, gy + size * fy

        cross = ((0.15, 0.15), (0.85, 0.85), (0.15, 0.85), (0.85, 0.15))
        check = ((0.15, 0.5), (0.4, 0.7), (0.4, 0.7), (0.85, 0.2))
        pts = [P(c[0] * t + x[0] * (1 - t), c[1] * t + x[1] * (1 - t)) for c, x in zip(check, cross, strict=True)]
        cr.set_line_width(2.4)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*mix(on_icon, off_icon))
        cr.move_to(*pts[0])
        cr.line_to(*pts[1])
        cr.move_to(*pts[2])
        cr.line_to(*pts[3])
        cr.stroke()


def switch_row(
    theme,
    title: str,
    subtitle: str = "",
    active: bool = False,
    on_toggle: Callable[[bool], None] | None = None,
    prefix: Gtk.Widget | None = None,
    before_switch: Gtk.Widget | None = None,
) -> tuple[Adw.ActionRow, Switch]:
    """Settings row like the shell's ToggleRow: text, optional widget, then the switch on the right."""
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
    row.add_css_class("c-row")
    if prefix is not None:
        row.add_prefix(prefix)
    if before_switch is not None:
        row.add_suffix(before_switch)
    sw = Switch(theme, active)
    row.add_suffix(sw)
    row.connect("activated", lambda *_: sw.toggle())
    if on_toggle:
        sw.connect("notify::active", lambda s, _p: on_toggle(s.get_active()))
    return row, sw
