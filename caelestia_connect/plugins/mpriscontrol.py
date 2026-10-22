"""Expose this computer's media players to the device (the phone becomes a remote)."""

import asyncio
import hashlib
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

from gi.repository import GLib

from .. import config
from ..i18n import _
from ..packet import Payload
from ..utils import spawn
from .base import Plugin

ACTIONS = ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous")
MAX_ART_SIZE = 5 * 1024 * 1024


async def _fetch_art(url: str) -> Path | None:
    """Download remote album art (Spotify, browsers...) so it can be sent as a payload."""
    dest = config.CACHE_DIR / "albumart-local" / hashlib.sha1(url.encode()).hexdigest()
    if dest.exists():
        return dest

    def fetch() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "CaelestiaConnect"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(MAX_ART_SIZE + 1)

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except OSError:
        return None
    if not data or len(data) > MAX_ART_SIZE:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _artist(meta) -> str:
    a = meta.get("xesam:artist")
    if isinstance(a, list):
        return ", ".join(str(x) for x in a)
    return str(a or "")


class MprisControlPlugin(Plugin):
    id = "mpriscontrol"
    name = _("Multimedia control receiver")
    description = _("Control this computer's media players from the device")
    icon = "play_circle"
    incoming = ("kdeconnect.mpris.request",)
    outgoing = ("kdeconnect.mpris",)

    def __init__(self, device):
        super().__init__(device)
        self.client = self.daemon.mpris
        self._prev_volume = -1
        self.client.on_list.append(self._send_list)
        self.client.on_change.append(self._changed)
        self.client.on_seeked.append(self._seeked)

    def destroy(self):
        for lst, cb in (
            (self.client.on_list, self._send_list),
            (self.client.on_change, self._changed),
            (self.client.on_seeked, self._seeked),
        ):
            if cb in lst:
                lst.remove(cb)
        self.client.release(self)

    async def connected(self):
        await self.client.acquire(self)
        self._send_list()

    def _send_list(self):
        self.send("kdeconnect.mpris", {"playerList": list(self.client.players), "supportAlbumArtPayload": True})

    def _seeked(self, player, pos_us):
        self.send("kdeconnect.mpris", {"player": player.name, "pos": int(pos_us) // 1000})

    def _metadata_body(self, meta) -> dict:
        title = str(meta.get("xesam:title") or "")
        artist = _artist(meta)
        album = str(meta.get("xesam:album") or "")
        url = str(meta.get("xesam:url") or "")
        if not title and not artist and url.startswith("file://"):
            path = Path(unquote(urlparse(url).path))
            title = path.name
            album = album or path.parent.name
        length = meta.get("mpris:length")
        return {
            "title": title,
            "artist": artist,
            "album": album,
            "albumArtUrl": str(meta.get("mpris:artUrl") or ""),
            "length": int(length) // 1000 if isinstance(length, (int, float)) else -1,
            "url": url,
        }

    def _changed(self, player, props):
        spawn(self._send_changes(player, props))

    async def _send_changes(self, player, props):
        body = {}
        if "Volume" in props:
            vol = int(float(props["Volume"]) * 100)
            if vol != self._prev_volume:
                self._prev_volume = vol
                body["volume"] = vol
        if "Metadata" in props:
            body.update(self._metadata_body(props["Metadata"] or {}))
        mapping = {
            "CanPause": "canPause",
            "CanPlay": "canPlay",
            "CanGoNext": "canGoNext",
            "CanGoPrevious": "canGoPrevious",
            "Shuffle": "shuffle",
        }
        for k, v in mapping.items():
            if k in props:
                body[v] = bool(props[k])
        if "PlaybackStatus" in props:
            body["isPlaying"] = props["PlaybackStatus"] == "Playing"
        if "LoopStatus" in props:
            body["loopStatus"] = props["LoopStatus"]
        if not body:
            return
        body["player"] = player.name
        can_seek = bool(player.prop("CanSeek", False))
        body["canSeek"] = can_seek
        if can_seek:
            body["pos"] = await player.position() // 1000
        self.send("kdeconnect.mpris", body)

    async def handle(self, pkt):
        name = pkt.get_str("player")
        player = self.client.players.get(name)
        if pkt.has("albumArtUrl"):
            await self._send_album_art(player, pkt.get_str("albumArtUrl"))
            return
        if player is None or pkt.get_bool("requestPlayerList"):
            self._send_list()
            if player is None:
                return
        if pkt.has("action") and pkt.get_str("action") in ACTIONS:
            await player.call(pkt.get_str("action"))
        if pkt.has("setLoopStatus"):
            await player.set("LoopStatus", GLib.Variant("s", pkt.get_str("setLoopStatus")))
        if pkt.has("setShuffle"):
            await player.set("Shuffle", GLib.Variant("b", pkt.get_bool("setShuffle")))
        if pkt.has("setVolume"):
            await player.set("Volume", GLib.Variant("d", pkt.get_int("setVolume") / 100))
        if pkt.has("Seek"):
            await player.call("Seek", GLib.Variant("(x)", (pkt.get_int("Seek"),)))
        if pkt.has("SetPosition"):
            target = pkt.get_int("SetPosition") * 1000
            await player.call("Seek", GLib.Variant("(x)", (target - await player.position(),)))

        answer = {}
        if pkt.get_bool("requestNowPlaying"):
            answer.update(self._metadata_body(player.metadata))
            answer["pos"] = await player.position() // 1000
            answer["isPlaying"] = player.prop("PlaybackStatus") == "Playing"
            for k, v in (
                ("canPause", "CanPause"),
                ("canPlay", "CanPlay"),
                ("canGoNext", "CanGoNext"),
                ("canGoPrevious", "CanGoPrevious"),
                ("canSeek", "CanSeek"),
            ):
                answer[k] = bool(player.prop(v, False))
            if player.prop("LoopStatus") is not None:
                answer["loopStatus"] = player.prop("LoopStatus")
            if player.prop("Shuffle") is not None:
                answer["shuffle"] = bool(player.prop("Shuffle"))
        if pkt.get_bool("requestVolume"):
            answer["volume"] = int(float(player.prop("Volume", 1.0) or 0) * 100)
        if answer:
            answer["player"] = name
            self.send("kdeconnect.mpris", answer)

    async def _send_album_art(self, player, url: str):
        if player is None:
            return
        art = str(player.metadata.get("mpris:artUrl") or "")
        if not art or art != url:
            return
        if art.startswith(("http://", "https://")):
            path = await _fetch_art(art)
            if path is None:
                return
        elif art.startswith("file://"):
            path = Path(unquote(urlparse(art).path))
        else:
            return
        if not path.is_file():
            return
        self.send(
            "kdeconnect.mpris",
            {"transferringAlbumArt": True, "player": player.name, "albumArtUrl": url},
            payload=Payload(path),
        )
