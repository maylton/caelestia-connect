from ..desktop.system import SoundLoop, default_ring_sound
from ..i18n import _
from ..utils import spawn
from .base import Plugin, action


class FindThisDevicePlugin(Plugin):
    id = "findthisdevice"
    name = _("Find this device")
    description = _("Let the device make this computer ring")
    icon = "volume_up"
    incoming = ("kdeconnect.findmyphone.request",)
    outgoing = ()
    settings_schema = [
        {"key": "ringtone", "type": "file", "label": _("Sound"), "default": ""},
    ]

    def __init__(self, device):
        super().__init__(device)
        self.sound = SoundLoop()
        self._muted: list[str] = []
        self._nid = 0

    def destroy(self):
        self.stop()

    async def handle(self, pkt):
        if self.sound.playing:
            self.stop()
            return
        self._muted = await self.daemon.audio.unmute_all()
        self.sound.start(self.setting("ringtone") or default_ring_sound(), duration=60)
        self._nid = await self.daemon.notifier.notify(
            _("{device} is looking for this computer").format(device=self.device.name),
            _("Click to stop ringing"),
            icon="audio-volume-high",
            actions=[("default", _("Stop")), ("stop", _("Stop"))],
            on_action=lambda _a: self.stop(),
            urgency=2,
            resident=True,
            source_device=self.device.name,
        )
        self.changed()

    @action
    def stop(self) -> bool:
        self.sound.stop()
        if self._muted:
            muted, self._muted = self._muted, []
            spawn(self._remute(muted))
        if self._nid:
            self.daemon.notifier.close_nowait(self._nid)
            self._nid = 0
        self.changed()
        return True

    async def _remute(self, sinks):
        for s in sinks:
            await self.daemon.audio.set_mute(s, True)

    def state(self):
        return {"ringing": self.sound.playing}
