"""Use the device as a drawing tablet (virtual pen through uinput)."""

from ..desktop.input import VirtualTablet
from ..i18n import _
from .base import Plugin


class DigitizerPlugin(Plugin):
    id = "digitizer"
    name = _("Drawing tablet")
    description = _("Use the device as a drawing tablet")
    icon = "draw"
    incoming = ("kdeconnect.digitizer.session", "kdeconnect.digitizer")
    outgoing = ()

    def __init__(self, device):
        super().__init__(device)
        self.tablet = VirtualTablet(device.name)
        self.active = False

    def destroy(self):
        self.tablet.stop()

    def handle(self, pkt):
        if pkt.type == "kdeconnect.digitizer.session":
            act = pkt.get_str("action")
            if act == "start" and all(pkt.has(k) for k in ("width", "height", "resolutionX", "resolutionY")):
                self.active = self.tablet.start(
                    pkt.get_int("width"), pkt.get_int("height"), pkt.get_int("resolutionX"), pkt.get_int("resolutionY")
                )
                if not self.active:
                    self.daemon.notifier.notify_nowait(
                        _("Drawing tablet"), _("Could not create a virtual drawing tablet"), icon="dialog-error"
                    )
            elif act == "end":
                self.tablet.stop()
                self.active = False
            self.changed()
            return
        self.tablet.event(
            active=pkt.get_bool("active") if pkt.has("active") else None,
            touching=pkt.get_bool("touching") if pkt.has("touching") else None,
            tool=pkt.get_str("tool") if pkt.has("tool") else None,
            x=pkt.get_int("x") if pkt.has("x") else None,
            y=pkt.get_int("y") if pkt.has("y") else None,
            pressure=pkt.get_float("pressure") if pkt.has("pressure") else None,
        )

    def state(self):
        return {"active": self.active}
