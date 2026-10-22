"""Type on the device from this computer (needs the KDE Connect keyboard enabled on the phone)."""

from ..i18n import _
from .base import Plugin, action


class RemoteKeyboardPlugin(Plugin):
    id = "remotekeyboard"
    name = _("Remote keyboard")
    description = _("Type on the device using this computer's keyboard")
    icon = "keyboard"
    incoming = ("kdeconnect.mousepad.echo", "kdeconnect.mousepad.keyboardstate")
    outgoing = ("kdeconnect.mousepad.request",)

    def __init__(self, device):
        super().__init__(device)
        self.remote_active = False

    def handle(self, pkt):
        if pkt.type == "kdeconnect.mousepad.keyboardstate":
            state = pkt.get_bool("state")
            if state != self.remote_active:
                self.remote_active = state
                self.changed()

    @action
    def send_key(
        self,
        key: str = "",
        special_key: int = 0,
        shift: bool = False,
        ctrl: bool = False,
        alt: bool = False,
        super: bool = False,
    ) -> bool:
        self.send(
            "kdeconnect.mousepad.request",
            {
                "key": key,
                "specialKey": int(special_key),
                "shift": shift,
                "ctrl": ctrl,
                "alt": alt,
                "super": super,
                "sendAck": False,
            },
        )
        return True

    @action
    def send_text(self, text: str) -> bool:
        if not text:
            return False
        self.send("kdeconnect.mousepad.request", {"key": text, "sendAck": False})
        return True

    def state(self):
        return {"active": self.remote_active}
