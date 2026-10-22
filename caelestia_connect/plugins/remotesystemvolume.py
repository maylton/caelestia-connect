"""Control the audio outputs of the device (another computer running KDE Connect)."""

from ..i18n import _
from .base import Plugin, action


class RemoteSystemVolumePlugin(Plugin):
    id = "remotesystemvolume"
    name = _("Remote system volume")
    description = _("Control the device's volume")
    icon = "speaker"
    incoming = ("kdeconnect.systemvolume",)
    outgoing = ("kdeconnect.systemvolume.request",)

    def __init__(self, device):
        super().__init__(device)
        self.sinks: dict[str, dict] = {}

    async def connected(self):
        self.send("kdeconnect.systemvolume.request", {"requestSinks": True})

    def handle(self, pkt):
        if pkt.has("sinkList"):
            self.sinks = {s.get("name", ""): dict(s) for s in pkt.get_list("sinkList") if isinstance(s, dict)}
        else:
            s = self.sinks.get(pkt.get_str("name"))
            if s is not None:
                for key in ("volume", "muted", "enabled"):
                    if pkt.has(key):
                        s[key] = pkt.get(key)
        self.changed()

    @action
    def set_volume(self, name: str, volume: int) -> bool:
        self.send("kdeconnect.systemvolume.request", {"name": name, "volume": int(volume)})
        return True

    @action
    def set_muted(self, name: str, muted: bool) -> bool:
        self.send("kdeconnect.systemvolume.request", {"name": name, "muted": bool(muted)})
        return True

    def state(self):
        return {"sinks": list(self.sinks.values())}
