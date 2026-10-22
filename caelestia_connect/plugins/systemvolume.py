"""Let the device control this computer's audio outputs."""

from ..i18n import _
from ..utils import spawn
from .base import Plugin


class SystemVolumePlugin(Plugin):
    id = "systemvolume"
    name = _("System volume")
    description = _("Control this computer's volume from the device")
    icon = "volume_up"
    incoming = ("kdeconnect.systemvolume.request",)
    outgoing = ("kdeconnect.systemvolume",)

    def __init__(self, device):
        super().__init__(device)
        self.audio = self.daemon.audio
        self.sinks: dict[str, dict] = {}
        self._unsub = self.audio.subscribe(lambda: spawn(self._diff()))

    def destroy(self):
        self._unsub()

    async def connected(self):
        await self._send_list()

    async def _send_list(self):
        sinks = await self.audio.sinks()
        self.sinks = {s["name"]: s for s in sinks}
        self.send("kdeconnect.systemvolume", {"sinkList": sinks})

    async def _diff(self):
        sinks = {s["name"]: s for s in await self.audio.sinks()}
        if set(sinks) != set(self.sinks):
            self.sinks = sinks
            self.send("kdeconnect.systemvolume", {"sinkList": list(sinks.values())})
            return
        for name, s in sinks.items():
            old = self.sinks.get(name, {})
            for key in ("volume", "muted", "enabled"):
                if old.get(key) != s[key]:
                    self.send("kdeconnect.systemvolume", {"name": name, key: s[key]})
        self.sinks = sinks

    async def handle(self, pkt):
        if pkt.has("requestSinks"):
            await self._send_list()
            return
        name = pkt.get_str("name")
        if name not in self.sinks:
            return
        if pkt.has("volume"):
            await self.audio.set_volume(name, pkt.get_int("volume"))
            await self.audio.set_mute(name, False)
        if pkt.has("muted"):
            await self.audio.set_mute(name, pkt.get_bool("muted"))
        if pkt.has("enabled") and pkt.get_bool("enabled"):
            await self.audio.set_default(name)
