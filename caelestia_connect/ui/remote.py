"""Remote input: a touchpad that drives the device's pointer and a keyboard that types on it."""

from __future__ import annotations

from gi.repository import Adw, Gdk, GLib, Gtk

from ..i18n import _
from . import widgets as w

# Gdk keyval -> KDE Connect specialKey
SPECIAL = {
    Gdk.KEY_BackSpace: 1,
    Gdk.KEY_Tab: 2,
    Gdk.KEY_Left: 4,
    Gdk.KEY_Up: 5,
    Gdk.KEY_Right: 6,
    Gdk.KEY_Down: 7,
    Gdk.KEY_Page_Up: 8,
    Gdk.KEY_Page_Down: 9,
    Gdk.KEY_Home: 10,
    Gdk.KEY_End: 11,
    Gdk.KEY_Return: 12,
    Gdk.KEY_KP_Enter: 12,
    Gdk.KEY_Delete: 13,
    Gdk.KEY_Escape: 14,
    Gdk.KEY_Sys_Req: 15,
    Gdk.KEY_Scroll_Lock: 16,
    **{getattr(Gdk, f"KEY_F{i}"): 20 + i for i in range(1, 13)},
}
SENSITIVITY = 1.6


class RemoteView(Gtk.ScrolledWindow):
    def __init__(self, app, page) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app = app
        self.page = page
        self.device: dict = {}
        self._last = (0.0, 0.0)
        self._pending = [0.0, 0.0]
        self._flush_id = 0
        self._dragged = False

        clamp = Adw.Clamp(maximum_size=760)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=8,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )
        clamp.set_child(box)
        self.set_child(clamp)

        self.pointer_card = w.card()
        self.pointer_card.append(w.section_title(_("Touchpad")))
        self.pointer_card.append(
            w.label(
                _(
                    "Drag to move the device's pointer, click to tap, scroll to scroll. "
                    'Requires the "Remote control" accessibility permission of KDE Connect on the phone.'
                ),
                wrap=True,
                css=["body-small"],
            )
        )
        self.pad = Gtk.Box(vexpand=True, hexpand=True)
        self.pad.set_size_request(-1, 300)
        self.pad.add_css_class("touchpad")
        icon = w.Icon("touch_app", "huge", css=["dim"])
        icon.set_hexpand(True)
        icon.set_opacity(0.25)
        self.pad.append(icon)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.pad.add_controller(drag)
        click = Gtk.GestureClick(button=0)
        click.connect("released", self._click)
        self.pad.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.pad.add_controller(scroll)
        self.pointer_card.append(self.pad)
        btns = Gtk.Box(spacing=8, homogeneous=True)
        btns.append(w.text_button(_("Left click"), "left_click", ["tonal"], lambda: self._cmd(singleclick=True)))
        btns.append(w.text_button(_("Middle"), "mouse", ["tonal"], lambda: self._cmd(middleclick=True)))
        btns.append(w.text_button(_("Right click"), "right_click", ["tonal"], lambda: self._cmd(rightclick=True)))
        self.pointer_card.append(btns)
        box.append(self.pointer_card)

        self.kb_card = w.card()
        head = Gtk.Box(spacing=8)
        head.append(w.section_title(_("Keyboard")))
        head.append(Gtk.Box(hexpand=True))
        self.kb_status = w.pill("")
        head.append(self.kb_status)
        self.kb_card.append(head)
        self.kb_card.append(
            w.label(
                _(
                    "Click the field below and type: keys are sent to the device as you press them. "
                    'On the phone, select "KDE Connect remote keyboard" as the input method.'
                ),
                wrap=True,
                css=["body-small"],
            )
        )
        self.kb = Gtk.Entry(placeholder_text=_("Type here to type on the device…"))
        self.kb.add_css_class("composer")
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.kb.add_controller(keys)
        self.kb_card.append(self.kb)
        row = Gtk.Box(spacing=8)
        self.text = Gtk.Entry(placeholder_text=_("Or send a whole text at once"), hexpand=True)
        self.text.add_css_class("composer")
        self.text.connect("activate", lambda *_: self._send_text())
        row.append(self.text)
        row.append(w.icon_button("send", _("Send"), ["filled"], callback=self._send_text))
        self.kb_card.append(row)
        box.append(self.kb_card)

    def update(self, d: dict) -> None:
        self.device = d
        self.pointer_card.set_visible(w.plugin_loaded(self.device, "remotecontrol"))
        self.kb_card.set_visible(w.plugin_loaded(self.device, "remotekeyboard"))
        active = d["state"].get("remotekeyboard", {}).get("active")
        self.kb_status.set_label(
            _("Keyboard active on the device") if active else _("Keyboard not active on the device")
        )
        for c in ("ok", "warn"):
            self.kb_status.remove_css_class(c)
        self.kb_status.add_css_class("ok" if active else "warn")

    def _cmd(self, **body) -> None:
        if self.device:
            self.app.plugin(self.device["id"], "remotecontrol", "command", body=body)

    def _drag_begin(self, _g, x, y) -> None:
        self._last = (0.0, 0.0)
        self._dragged = False
        self.pad.add_css_class("active")

    def _drag_update(self, _g, ox, oy) -> None:
        dx, dy = ox - self._last[0], oy - self._last[1]
        self._last = (ox, oy)
        if abs(ox) + abs(oy) > 4:
            self._dragged = True
        self._pending[0] += dx * SENSITIVITY
        self._pending[1] += dy * SENSITIVITY
        if not self._flush_id:
            self._flush_id = GLib.timeout_add(16, self._flush)

    def _flush(self) -> bool:
        self._flush_id = 0
        dx, dy = self._pending
        self._pending = [0.0, 0.0]
        if (dx or dy) and self.device:
            self.app.plugin(self.device["id"], "remotecontrol", "move", dx=dx, dy=dy)
        return False

    def _drag_end(self, *_a) -> None:
        self.pad.remove_css_class("active")

    def _click(self, gesture, n_press, _x, _y) -> None:
        if self._dragged:
            return
        button = gesture.get_current_button()
        if button == 3:
            self._cmd(rightclick=True)
        elif button == 2:
            self._cmd(middleclick=True)
        elif n_press == 2:
            self._cmd(doubleclick=True)
        else:
            self._cmd(singleclick=True)

    def _scroll(self, _c, dx, dy) -> bool:
        self._cmd(scroll=True, dx=-dx * 10, dy=-dy * 10)
        return True

    def _key(self, _ctrl, keyval, _code, state) -> bool:
        if not self.device:
            return False
        mods = {
            "ctrl": bool(state & Gdk.ModifierType.CONTROL_MASK),
            "alt": bool(state & Gdk.ModifierType.ALT_MASK),
            "shift": bool(state & Gdk.ModifierType.SHIFT_MASK),
            "super": bool(state & Gdk.ModifierType.SUPER_MASK),
        }
        if keyval in (
            Gdk.KEY_Shift_L,
            Gdk.KEY_Shift_R,
            Gdk.KEY_Control_L,
            Gdk.KEY_Control_R,
            Gdk.KEY_Alt_L,
            Gdk.KEY_Alt_R,
            Gdk.KEY_Super_L,
            Gdk.KEY_Super_R,
            Gdk.KEY_ISO_Level3_Shift,
        ):
            return True
        special = SPECIAL.get(keyval, 0)
        if special:
            self.app.plugin(self.device["id"], "remotekeyboard", "send_key", special_key=special, **mods)
            return True
        ch = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else ""
        if ch:
            if not (mods["ctrl"] or mods["alt"] or mods["super"]):
                mods["shift"] = False  # already applied to the character
            self.app.plugin(self.device["id"], "remotekeyboard", "send_key", key=ch, **mods)
            return True
        return False

    def _send_text(self) -> None:
        text = self.text.get_text()
        if text and self.device:
            self.app.plugin(
                self.device["id"],
                "remotekeyboard",
                "send_text",
                text=text,
                callback=lambda _r: self.app.toast(_("Text sent")),
            )
            self.text.set_text("")
