"""Pause media (or mute) on this computer while the phone rings or is in a call."""

from ..i18n import _
from .base import Plugin


class PauseMusicPlugin(Plugin):
    id = "pausemusic"
    name = _("Pause media during calls")
    description = _("Pause music/videos during a phone call")
    icon = "pause_circle"
    incoming = ("kdeconnect.telephony",)
    outgoing = ()
    settings_schema = [
        {
            "key": "conditionTalking",
            "type": "bool",
            "label": _("Only when talking (not while ringing)"),
            "default": False,
        },
        {"key": "actionPause", "type": "bool", "label": _("Pause media players"), "default": True},
        {"key": "actionMute", "type": "bool", "label": _("Mute system sound"), "default": False},
        {"key": "actionResume", "type": "bool", "label": _("Resume automatically when the call ends"), "default": True},
    ]

    def __init__(self, device):
        super().__init__(device)
        self.paused: list = []
        self.muted: list[str] = []

    def destroy(self):
        self.daemon.mpris.release(self)

    async def connected(self):
        await self.daemon.mpris.acquire(self)

    async def handle(self, pkt):
        event = pkt.get_str("event")
        if self.setting("conditionTalking"):
            if event != "talking":
                return
        elif event not in ("ringing", "talking"):
            return
        if not pkt.get_bool("isCancel"):
            if self.setting("actionMute") and not self.muted:
                self.muted = await self.daemon.audio.mute_all()
            if self.setting("actionPause"):
                for p in list(self.daemon.mpris.players.values()):
                    if p.prop("PlaybackStatus") == "Playing" and p not in self.paused:
                        self.paused.append(p)
                        await p.call("Pause" if p.prop("CanPause", True) else "Stop")
        else:
            resume = self.setting("actionResume")
            if self.muted and resume:
                for s in self.muted:
                    await self.daemon.audio.set_mute(s, False)
            self.muted = []
            if self.paused and resume:
                for p in self.paused:
                    if p in self.daemon.mpris.players.values():
                        await p.call("Play")
            self.paused = []
