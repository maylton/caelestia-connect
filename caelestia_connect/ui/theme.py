"""Caelestia look for GTK: Material 3 colours from the shell's scheme, its fonts and shapes.

The scheme file is watched so the app follows wallpaper / scheme changes live, like the shell does.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, Gtk

log = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "caelestia"
SCHEME_PATH = STATE_DIR / "scheme.json"
FONT_CANDIDATES = [
    Path.home() / ".config/quickshell/caelestia/assets/google-sans-flex",
    Path("/etc/xdg/quickshell/caelestia/assets/google-sans-flex"),
]
SANS = "GoogleSansFlex, Google Sans Flex, Google Sans, Rubik, sans-serif"
ICON_FONT = "Material Symbols Rounded"

# Fallback palette (Caelestia's default dark teal scheme)
FALLBACK = {
    "background": "0a0f0f",
    "surface": "0a0f0f",
    "surfaceContainerLowest": "000000",
    "surfaceContainerLow": "0e1514",
    "surfaceContainer": "131b1a",
    "surfaceContainerHigh": "192120",
    "surfaceContainerHighest": "1d2827",
    "surfaceBright": "242e2d",
    "onSurface": "dce8e6",
    "onSurfaceVariant": "a2adac",
    "outline": "6d7876",
    "outlineVariant": "3f4a49",
    "primary": "9bd0cc",
    "onPrimary": "0d4845",
    "primaryContainer": "255b58",
    "onPrimaryContainer": "b8ede9",
    "secondary": "b0ccc9",
    "onSecondary": "2c4543",
    "secondaryContainer": "27403e",
    "onSecondaryContainer": "a9c5c2",
    "tertiary": "d5efff",
    "onTertiary": "2e5c72",
    "tertiaryContainer": "b6e3fe",
    "onTertiaryContainer": "255369",
    "error": "fa746f",
    "onError": "490006",
    "errorContainer": "871f21",
    "onErrorContainer": "ff9993",
    "success": "b5ccba",
    "inverseSurface": "f6faf9",
    "inverseOnSurface": "515655",
    "scrim": "000000",
    "shadow": "000000",
}

CSS_TEMPLATE = Path(__file__).with_name("style.css").read_text()


_BG_DECL = re.compile(r"(background-color|--[a-z-]*(?:bg|background|backdrop)-color):\s*#%\((\w+)\)s")


def _translucent_backgrounds(css: str) -> str:
    """Draw every background with alpha < 1.

    GTK 4 derives the surface's opaque region from the opaque render nodes it draws (window,
    sidebar, cards...). Hyprland never blurs behind opaque regions, so any fully opaque background
    would show the wallpaper sharp through the window opacity rule instead of blurred.
    """
    return _BG_DECL.sub(lambda m: f"{m.group(1)}: alpha(#%({m.group(2)})s, 0.99)", css)


class Theme:
    def __init__(self) -> None:
        self.provider = Gtk.CssProvider()
        self.colours = dict(FALLBACK)
        self.mode = "dark"
        self._monitor: Gio.FileMonitor | None = None
        self._fonts_loaded = False

    def install(self) -> None:
        self._load_fonts()
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        self.reload()
        try:
            gfile = Gio.File.new_for_path(str(SCHEME_PATH))
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._changed)
        except Exception as e:
            log.debug("Cannot watch scheme: %s", e)

    def _load_fonts(self) -> None:
        if self._fonts_loaded:
            return
        self._fonts_loaded = True
        try:
            from gi.repository import PangoCairo

            fm = PangoCairo.FontMap.get_default()
            for d in FONT_CANDIDATES:
                for f in sorted(d.glob("*.ttf")) if d.is_dir() else []:
                    fm.add_font_file(str(f))
                    return
        except Exception as e:
            log.debug("Could not load Caelestia font: %s", e)

    def _changed(self, _mon, _f, _other, event) -> None:
        if event in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.MOVED_IN,
        ):
            self.reload()

    def reload(self) -> None:
        colours = dict(FALLBACK)
        try:
            data = json.loads(SCHEME_PATH.read_text())
            colours.update({k: str(v).lstrip("#") for k, v in data.get("colours", {}).items()})
            self.mode = data.get("mode", "dark")
        except (OSError, ValueError):
            pass
        self.colours = colours
        css = _translucent_backgrounds(CSS_TEMPLATE) % {**colours, "sans": SANS, "icon": ICON_FONT}
        self.provider.load_from_string(css)
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if self.mode == "light" else Adw.ColorScheme.FORCE_DARK
        )

    def rgba(self, name: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
        """Scheme colour as a Cairo source tuple."""
        value = int(self.colours.get(name, "ffffff")[:6], 16)
        return (value >> 16 & 255) / 255, (value >> 8 & 255) / 255, (value & 255) / 255, alpha
