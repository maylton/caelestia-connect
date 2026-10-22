"""Small Material 3 style building blocks."""

from __future__ import annotations

import json

from gi.repository import Adw, Gtk, Pango

from ..i18n import _

DEVICE_ICONS = {
    "phone": "smartphone",
    "smartphone": "smartphone",
    "tablet": "tablet_android",
    "tv": "tv",
    "laptop": "laptop",
    "desktop": "desktop_windows",
}


def device_icon(dtype: str) -> str:
    return DEVICE_ICONS.get(dtype, "devices")


class Icon(Gtk.Label):
    """Material Symbols Rounded glyph, the same icon font the Caelestia shell uses."""

    def __init__(self, name: str, size: str = "medium", filled: bool = False, css: list[str] | None = None) -> None:
        super().__init__(label=name)
        self.add_css_class("material-icon")
        self.add_css_class(f"icon-{size}")
        if filled:
            self.add_css_class("filled")
        for c in css or []:
            self.add_css_class(c)

    def set_icon(self, name: str) -> None:
        self.set_label(name)


def label(
    text: str = "",
    css: list[str] | None = None,
    xalign: float = 0,
    wrap: bool = False,
    ellipsize: bool = False,
    selectable: bool = False,
) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=xalign)
    for c in css or []:
        lb.add_css_class(c)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if selectable:
        lb.set_selectable(True)
    return lb


def icon_button(
    icon: str, tooltip: str = "", css: list[str] | None = None, size: str = "medium", callback=None
) -> Gtk.Button:
    b = Gtk.Button()
    b.set_child(Icon(icon, size))
    b.add_css_class("round")
    for c in css or ["flat"]:
        b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if callback:
        b.connect("clicked", lambda *_: callback())
    return b


def text_button(text: str, icon: str | None = None, css: list[str] | None = None, callback=None) -> Gtk.Button:
    b = Gtk.Button()
    box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
    if icon:
        box.append(Icon(icon, "small"))
    box.append(Gtk.Label(label=text))
    b.set_child(box)
    for c in css or ["tonal"]:
        b.add_css_class(c)
    if callback:
        b.connect("clicked", lambda *_: callback())
    return b


class Tile(Gtk.Button):
    """Quick action tile: big icon over a short label."""

    def __init__(self, icon: str, text: str, callback=None) -> None:
        super().__init__()
        self.add_css_class("tile")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.icon = Icon(icon, "large")
        box.append(self.icon)
        self.text = label(text, xalign=0.5, ellipsize=True, css=["body-small"])
        box.append(self.text)
        self.set_child(box)
        if callback:
            self.connect("clicked", lambda *_: callback())

    def set_active(self, active: bool) -> None:
        if active:
            self.add_css_class("active")
        else:
            self.remove_css_class("active")


def circle(icon: Icon, css: str = "avatar") -> Gtk.Box:
    """Round container (avatar / hero) with the glyph centered and no stretching."""
    box = Gtk.CenterBox(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=False, vexpand=False)
    box.add_css_class(css)
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_valign(Gtk.Align.CENTER)
    box.set_center_widget(icon)
    return box


def card(child: Gtk.Widget | None = None, css: list[str] | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.add_css_class("surface-card")
    for c in css or []:
        box.add_css_class(c)
    if child:
        box.append(child)
    return box


def section_title(text: str) -> Gtk.Label:
    return label(text, css=["label-caps"])


def pill(text: str, kind: str = "") -> Gtk.Label:
    lb = Gtk.Label(label=text)
    lb.add_css_class("pill")
    if kind:
        lb.add_css_class(kind)
    return lb


def clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def format_ms(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def plugin_loaded(device: dict, plugin_id: str) -> bool:
    return any(p["id"] == plugin_id and p["loaded"] for p in device.get("plugins", []))


def prompt(
    parent: Gtk.Widget, heading: str, body: str, ok_label: str, on_ok, initial: str = "", placeholder: str = ""
) -> None:
    """Dialog with a single text entry; on_ok receives the stripped, non-empty text."""
    dlg = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial, placeholder_text=placeholder, activates_default=True)
    dlg.set_extra_child(entry)
    dlg.add_response("cancel", _("Cancel"))
    dlg.add_response("ok", ok_label)
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("ok")

    def done(_dlg, response):
        text = entry.get_text().strip()
        if response == "ok" and text:
            on_ok(text)

    dlg.connect("response", done)
    dlg.present(parent)


def signature(obj) -> str:
    """Cheap change detection: views only rebuild a section when its data signature changes."""
    return json.dumps(obj, sort_keys=True, default=str)
