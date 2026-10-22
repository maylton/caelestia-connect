"""Laser pointer overlay for the presentation remote (a layer-shell surface over all windows).

Reads JSON lines on stdin: {"dx": float, "dy": float} moves the dot, {"stop": true} hides it. Quits at the
end of the input.
"""

from __future__ import annotations

import json
import sys
import threading

import gi

from gi.repository import Gdk, Gio, GLib, Gtk

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
except (ValueError, ImportError):
    LayerShell = None

HIDE_AFTER_MS = 600


class Pointer(Gtk.Application):
    def __init__(self) -> None:
        # Not unique: a new overlay must never hand off to one that is still shutting down
        super().__init__(application_id="org.caelestia.Connect.Presenter", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.x = self.y = 0.5
        self.visible = False
        self._hide_id = 0
        self.window: Gtk.Window | None = None
        self.area: Gtk.DrawingArea | None = None

    def do_activate(self) -> None:
        win = Gtk.Window(application=self, decorated=False)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        if LayerShell is not None and LayerShell.is_supported():
            LayerShell.init_for_window(win)
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_namespace(win, "caelestia-connect-presenter")
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(win, edge, True)
            LayerShell.set_exclusive_zone(win, -1)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        else:
            win.fullscreen()
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True, can_target=False)
        self.area.set_draw_func(self._draw)
        win.set_child(self.area)
        win.set_can_target(False)
        win.present()
        surface = win.get_surface()
        if surface is not None:
            import cairo

            surface.set_input_region(cairo.Region())  # click-through
        self.window = win
        self.hold()
        threading.Thread(target=self._read_stdin, daemon=True).start()

    def _draw(self, _area, cr, width, height) -> None:
        if not self.visible:
            return
        x, y = self.x * width, self.y * height
        for radius, alpha in ((26, 0.18), (16, 0.35), (9, 0.95)):
            cr.set_source_rgba(1.0, 0.18, 0.2, alpha)
            cr.arc(x, y, radius, 0, 6.2832)
            cr.fill()

    def _read_stdin(self) -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            GLib.idle_add(self._handle, msg)
        GLib.idle_add(self.quit)

    def _handle(self, msg: dict) -> bool:
        if msg.get("stop"):
            self._hide()
            return False
        width = self.area.get_width() or 1
        height = self.area.get_height() or 1
        ratio = width / height
        self.x = min(1.0, max(0.0, self.x + float(msg.get("dx", 0))))
        self.y = min(1.0, max(0.0, self.y + float(msg.get("dy", 0)) * ratio))
        self.visible = True
        self.area.queue_draw()
        if self._hide_id:
            GLib.source_remove(self._hide_id)
        self._hide_id = GLib.timeout_add(HIDE_AFTER_MS, self._hide)
        return False

    def _hide(self) -> bool:
        if self._hide_id:
            GLib.source_remove(self._hide_id)
        self._hide_id = 0
        self.visible = False
        self.area.queue_draw()
        return False


if __name__ == "__main__":
    # gtk4-layer-shell must be loaded before libwayland: re-exec with LD_PRELOAD if needed
    import os

    lib = "/usr/lib/libgtk4-layer-shell.so"
    if LayerShell is not None and os.path.exists(lib) and lib not in os.environ.get("LD_PRELOAD", ""):
        os.environ["LD_PRELOAD"] = (os.environ.get("LD_PRELOAD", "") + " " + lib).strip()
        os.execv(sys.executable, [sys.executable, "-m", "caelestia_connect.ui.presenter"])
    Pointer().run([])
