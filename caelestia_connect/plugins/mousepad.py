from ..i18n import _
from .base import Plugin

MODIFIERS = (("ctrl", "KEY_LEFTCTRL"), ("alt", "KEY_LEFTALT"), ("shift", "KEY_LEFTSHIFT"), ("super", "KEY_LEFTMETA"))


class MousepadPlugin(Plugin):
    """Lets the device act as a touchpad and keyboard for this computer."""

    id = "mousepad"
    name = _("Virtual input")
    description = _("Use the device as a touchpad and keyboard")
    icon = "touch_app"
    incoming = ("kdeconnect.mousepad.request",)
    outgoing = ("kdeconnect.mousepad.keyboardstate",)

    async def connected(self):
        # Re-read the keyboard layout, the user may have switched it since the last connection
        self.daemon.vinput.refresh_layout()
        self.send("kdeconnect.mousepad.keyboardstate", {"state": self.daemon.vinput.available})

    async def handle(self, pkt):
        vi = self.daemon.vinput
        if not vi.available:
            return
        dx, dy = pkt.get_float("dx"), pkt.get_float("dy")
        if pkt.get_bool("singleclick"):
            vi.click("left")
        elif pkt.get_bool("doubleclick"):
            vi.click("left", 2)
        elif pkt.get_bool("middleclick"):
            vi.click("middle")
        elif pkt.get_bool("rightclick"):
            vi.click("right")
        elif pkt.get_bool("singlehold"):
            vi.button("left", True)
        elif pkt.get_bool("singlerelease"):
            vi.button("left", False)
        elif pkt.get_bool("scroll"):
            vi.scroll(dx, dy)
        elif pkt.get_str("key") or pkt.get_int("specialKey"):
            mods = tuple(key for flag, key in MODIFIERS if pkt.get_bool(flag))
            special = pkt.get_int("specialKey")
            if special:
                vi.special_key(special, mods)
            else:
                await vi.type_text(pkt.get_str("key"), mods, clipboard=self.daemon.clipboard)
            if pkt.get_bool("sendAck"):
                body = {k: pkt.get(k) for k in ("key", "specialKey", "shift", "ctrl", "alt", "super") if pkt.has(k)}
                body["isAck"] = True
                self.send("kdeconnect.mousepad.echo", body)
        elif dx or dy:
            vi.move(dx, dy)
