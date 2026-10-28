"""Choose whether the phone's sound plays on the phone or on this computer (over Bluetooth)."""

import asyncio
import re

from gi.repository import GLib

from ..desktop.phoneaudio import BluezAudio, PhoneAudio
from ..i18n import _
from .base import Plugin, action

# Android pauses the music when its Bluetooth output goes away; give it time to do so before resuming
RESUME_DELAY = 1.2

MAC = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class PhoneAudioPlugin(Plugin):
    id = "phoneaudio"
    name = _("Audio output")
    description = _("Choose whether the phone's sound plays on the phone or on this computer")
    icon = "speaker"
    incoming = ()
    # Not tied to any packet: offered to every device that talks to us
    outgoing = ("kdeconnect.ping",)

    def __init__(self, device):
        super().__init__(device)
        self.audio = BluezAudio(self.changed)
        self._started = False

    async def connected(self):
        if not self._started:
            self._started = True
            await self.audio.start()
        self._remember_address()

    def destroy(self):
        self.audio.stop()

    def _remember_address(self) -> None:
        """A Bluetooth link tells which Bluetooth device this is; keep it for when only Wi-Fi is up."""
        settings = self.device.config.plugin_settings(self.id)
        for link in self.device.links:
            if MAC.match(link.address) and settings.get("address") != link.address.upper():
                settings["address"] = link.address.upper()
                self.device.config.save()

    def _phone(self) -> PhoneAudio | None:
        address = self.device.config.plugin_settings(self.id).get("address", "")
        by_name = [d for d in self.audio.devices if d.name.casefold() == self.device.name.casefold()]
        for d in self.audio.devices:
            if d.address.upper() == address:
                return d
        return by_name[0] if len(by_name) == 1 else None

    def state(self):
        phone = self._phone()
        if phone is None:
            return {"available": False}
        return {"available": True, "onComputer": phone.on_computer, "playing": phone.playing}

    @action
    async def toggle(self) -> dict:
        phone = self._phone()
        return await self.set_on_computer(not phone.on_computer) if phone else {"error": _("Phone not found")}

    @action
    async def set_on_computer(self, on_computer: bool) -> dict:
        phone = self._phone()
        if phone is None:
            return {"error": _("Phone not found")}
        was_playing = None if on_computer else self._playing_player()
        try:
            await self.audio.set_on_computer(phone, bool(on_computer))
        except GLib.Error as e:
            self.log.info("Changing the audio output failed: %s", e.message)
            return {
                "error": _(
                    "Could not change the audio output. Check that Bluetooth is on and that Media audio is "
                    "enabled for this computer in the phone's Bluetooth settings."
                )
            }
        if was_playing:
            await self._resume(was_playing)
        return {}

    def _playing_player(self) -> str | None:
        remote = self.device.plugin("mprisremote")
        return next((p.name for p in remote.players.values() if p.playing), None) if remote else None

    async def _resume(self, player: str) -> None:
        """Keep the music going on the phone's own speaker."""
        await asyncio.sleep(RESUME_DELAY)
        remote = self.device.plugin("mprisremote")
        paused = remote and player in remote.players and not remote.players[player].playing
        self.log.info("Resume check: %s is %s", player, "paused" if paused else "still playing")
        if paused:
            remote.media_action("Play", player)
