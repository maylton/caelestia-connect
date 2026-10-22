"""Control the device's pointer and keyboard from this computer."""

from ..i18n import _
from .base import Plugin, action

ALLOWED = {
    "dx",
    "dy",
    "singleclick",
    "doubleclick",
    "middleclick",
    "rightclick",
    "singlehold",
    "singlerelease",
    "scroll",
    "key",
    "specialKey",
    "shift",
    "ctrl",
    "alt",
    "super",
    "sendAck",
}


class RemoteControlPlugin(Plugin):
    id = "remotecontrol"
    name = _("Remote control")
    description = _("Control the device's pointer from this computer")
    icon = "mouse"
    incoming = ()
    outgoing = ("kdeconnect.mousepad.request",)

    @action
    def move(self, dx: float, dy: float) -> bool:
        self.send("kdeconnect.mousepad.request", {"dx": dx, "dy": dy})
        return True

    @action
    def command(self, body: dict) -> bool:
        body = {k: v for k, v in (body or {}).items() if k in ALLOWED}
        if not body:
            return False
        self.send("kdeconnect.mousepad.request", body)
        return True
