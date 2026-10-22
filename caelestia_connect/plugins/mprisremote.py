"""Control the device's media players; they are also published as MPRIS players on this computer."""

import asyncio
import hashlib
import time

from .. import config
from ..desktop.mpris import ExportedPlayer
from ..i18n import _
from ..utils import spawn
from .base import Plugin, action

# Android keeps the media session of apps that merely sit in the background (YouTube, music apps...)
# and KDE Connect reports all of them. Players that are not playing are hidden after this long.
IDLE_TIMEOUT = 600


class RemotePlayer:
    def __init__(self, plugin: "MprisRemotePlugin", name: str) -> None:
        self.plugin = plugin
        self.name = name
        self.title = self.artist = self.album = ""
        self.art_url = ""
        self.local_art = ""
        self.length = 0
        self.volume = 50
        self.last_pos = 0
        self.last_pos_time = time.monotonic()
        self.playing = False
        self.can_play = self.can_pause = self.can_next = self.can_prev = True
        self.can_seek = False
        self.loop = "None"
        self.shuffle = False
        self.has_loop = False
        self.has_shuffle = False
        self.export: ExportedPlayer | None = None
        self.last_playing = 0.0  # monotonic time it was last seen playing, 0 = not since we connected

    def position(self) -> int:
        if self.playing:
            return self.last_pos + int((time.monotonic() - self.last_pos_time) * 1000)
        return self.last_pos

    def parse(self, pkt) -> None:
        track_changed = False
        for attr, key in (("title", "title"), ("artist", "artist"), ("album", "album")):
            if pkt.has(key):
                v = pkt.get_str(key)
                if v != getattr(self, attr):
                    setattr(self, attr, v)
                    track_changed = True
        if pkt.has("length"):
            self.length = pkt.get_int("length")
        if pkt.has("albumArtUrl"):
            url = pkt.get_str("albumArtUrl")
            if url != self.art_url:
                self.art_url = url
                self.local_art = ""
                if url:
                    self.plugin.fetch_art(self, url)
        if pkt.has("volume"):
            self.volume = pkt.get_int("volume", self.volume)
        if pkt.has("pos"):
            self.last_pos = pkt.get_int("pos")
            self.last_pos_time = time.monotonic()
        if pkt.has("isPlaying"):
            was_playing = self.playing
            self.playing = pkt.get_bool("isPlaying")
            if self.playing:
                self.last_playing = time.monotonic()
                if not was_playing:
                    self.plugin.player_started(self)
            elif was_playing:
                self.last_playing = time.monotonic()
        for attr, key in (
            ("can_seek", "canSeek"),
            ("can_play", "canPlay"),
            ("can_pause", "canPause"),
            ("can_prev", "canGoPrevious"),
            ("can_next", "canGoNext"),
            ("shuffle", "shuffle"),
        ):
            if pkt.has(key):
                setattr(self, attr, pkt.get_bool(key))
        if pkt.has("loopStatus"):
            self.loop = pkt.get_str("loopStatus") or "None"
            self.has_loop = True
        if pkt.has("shuffle"):
            self.has_shuffle = True
        self.sync_export(seeked=pkt.has("pos") or track_changed)

    def sync_export(self, seeked: bool = False) -> None:
        if self.export is None:
            return
        meta = {
            "xesam:title": self.title,
            "xesam:artist": [self.artist] if self.artist else [],
            "xesam:album": self.album,
        }
        if self.length > 0:
            meta["mpris:length"] = self.length * 1000
        if self.local_art:
            meta["mpris:artUrl"] = "file://" + self.local_art
        self.export.update(
            PlaybackStatus="Playing" if self.playing else "Paused",
            Metadata=meta,
            Volume=self.volume / 100,
            CanSeek=self.can_seek,
            CanPlay=self.can_play,
            CanPause=self.can_pause,
            CanGoNext=self.can_next,
            CanGoPrevious=self.can_prev,
            LoopStatus=self.loop,
            Shuffle=self.shuffle,
        )
        if seeked:
            self.export.seeked(self.position() * 1000)

    # ExportedPlayer controller interface
    def action(self, name: str) -> None:
        self.plugin.send_request(self.name, action=name)

    def seek(self, offset_us: int) -> None:
        self.set_position(self.position() * 1000 + int(offset_us))

    def set_position(self, pos_us: int) -> None:
        ms = max(0, int(pos_us) // 1000)
        self.last_pos, self.last_pos_time = ms, time.monotonic()
        self.plugin.send_request(self.name, SetPosition=ms)
        if self.export:
            self.export.seeked(ms * 1000)

    def set_volume(self, vol: float) -> None:
        self.plugin.send_request(self.name, setVolume=int(vol * 100))

    def set_loop(self, status: str) -> None:
        self.plugin.send_request(self.name, setLoopStatus=status)

    def set_shuffle(self, value: bool) -> None:
        self.plugin.send_request(self.name, setShuffle=value)

    def position_us(self) -> int:
        return self.position() * 1000

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "art": self.local_art,
            "length": self.length,
            "position": self.position(),
            "volume": self.volume,
            "playing": self.playing,
            "canPlay": self.can_play,
            "canPause": self.can_pause,
            "canGoNext": self.can_next,
            "canGoPrevious": self.can_prev,
            "canSeek": self.can_seek,
            "loopStatus": self.loop,
            "shuffle": self.shuffle,
            "hasLoop": self.has_loop,
            "hasShuffle": self.has_shuffle,
        }


class MprisRemotePlugin(Plugin):
    id = "mprisremote"
    name = _("Multimedia controls")
    description = _("Control the device's media players (also shown in the shell's media widgets)")
    icon = "music_note"
    incoming = ("kdeconnect.mpris",)
    outgoing = ("kdeconnect.mpris.request",)
    settings_schema = [
        {
            "key": "exportMpris",
            "type": "bool",
            "label": _("Show device players in the shell media controls"),
            "default": True,
        },
        {
            "key": "hideIdle",
            "type": "bool",
            "label": _("Hide players paused for more than 10 minutes"),
            "default": True,
        },
    ]

    def __init__(self, device):
        super().__init__(device)
        self.players: dict[str, RemotePlayer] = {}
        self.current = ""
        self._art_requested: set[str] = set()
        self._activity_task = spawn(self._activity_loop(), name="mprisremote:activity")

    def destroy(self):
        self._activity_task.cancel()
        for p in self.players.values():
            if p.export:
                p.export.stop()
        self.players.clear()

    def settings_changed(self, key):
        if key in ("exportMpris", "hideIdle"):
            self._sync_activity()

    def is_active(self, p: RemotePlayer) -> bool:
        if not self.setting("hideIdle") or p.playing:
            return True
        return p.last_playing > 0 and time.monotonic() - p.last_playing < IDLE_TIMEOUT

    def active_players(self) -> list[RemotePlayer]:
        return [p for p in self.players.values() if self.is_active(p)]

    def player_started(self, p: RemotePlayer) -> None:
        current = self.players.get(self.current)
        if current is None or not current.playing or current is p:
            self.current = p.name

    def _sync_activity(self) -> None:
        """Export only active players over MPRIS and keep `current` pointing at one of them."""
        export = self.setting("exportMpris")
        for p in self.players.values():
            if export and self.is_active(p):
                if p.export is None:
                    spawn(self._export(p))
            elif p.export is not None:
                p.export.stop()
                p.export = None
        active = self.active_players()
        if not any(p.name == self.current for p in active):
            playing = [p for p in active if p.playing]
            recent = sorted(active, key=lambda p: -p.last_playing)
            candidates = playing or recent
            self.current = candidates[0].name if candidates else ""

    async def _activity_loop(self):
        while True:
            await asyncio.sleep(30)
            before = (self.current, [p.name for p in self.active_players()])
            self._sync_activity()
            if before != (self.current, [p.name for p in self.active_players()]):
                self.changed()

    async def connected(self):
        self.send("kdeconnect.mpris.request", {"requestPlayerList": True})

    def send_request(self, player: str, **body) -> None:
        body["player"] = player
        self.send("kdeconnect.mpris.request", body)

    def fetch_art(self, player: RemotePlayer, url: str) -> None:
        path = self._art_path(url)
        if path.exists():
            player.local_art = str(path)
            return
        if url not in self._art_requested:
            self._art_requested.add(url)
            self.send_request(player.name, albumArtUrl=url)

    @staticmethod
    def _art_path(url: str):
        return config.CACHE_DIR / "albumart" / hashlib.sha1(url.encode()).hexdigest()

    async def _export(self, player: RemotePlayer) -> None:
        if player.export is not None or not self.setting("exportMpris") or not self.is_active(player):
            return
        exp = ExportedPlayer(f"{player.name} ({self.device.name})", player)
        try:
            await exp.start()
        except Exception as e:
            self.log.warning("Could not export MPRIS player: %s", e)
            return
        if player.name not in self.players or player.export is not None or not self.is_active(player):
            exp.stop()
            return
        player.export = exp
        player.sync_export()

    async def handle(self, pkt):
        if pkt.get_bool("transferringAlbumArt"):
            url = pkt.get_str("albumArtUrl")
            if pkt.has_payload and url:
                dest = self._art_path(url)
                if await self.device.download(pkt, dest):
                    for p in self.players.values():
                        if p.art_url == url:
                            p.local_art = str(dest)
                            p.sync_export()
                    self.changed()
            return
        if pkt.has("player"):
            name = pkt.get_str("player")
            p = self.players.get(name)
            if p is None:
                p = self.players[name] = RemotePlayer(self, name)
            p.parse(pkt)
        if pkt.has("playerList"):
            names = [n for n in pkt.get_list("playerList") if isinstance(n, str)]
            for name in list(self.players):
                if name not in names:
                    p = self.players.pop(name)
                    if p.export:
                        p.export.stop()
            for name in names:
                if name not in self.players:
                    self.players[name] = RemotePlayer(self, name)
                    self.send_request(name, requestNowPlaying=True, requestVolume=True)
        self._sync_activity()
        self.changed()

    def _player(self, name: str = "") -> RemotePlayer | None:
        return self.players.get(name or self.current)

    @action
    def select_player(self, player: str) -> bool:
        if player in self.players:
            self.current = player
            self.send_request(player, requestNowPlaying=True, requestVolume=True)
            self.changed()
            return True
        return False

    @action
    def media_action(self, action: str, player: str = "") -> bool:
        p = self._player(player)
        if p is None:
            return False
        if action in ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous"):
            p.action(action)
        elif action == "shuffle":
            p.set_shuffle(not p.shuffle)
        elif action == "repeat":
            p.set_loop({"None": "Track", "Track": "Playlist"}.get(p.loop, "None"))
        else:
            return False
        return True

    @action
    def set_position(self, position: int, player: str = "") -> bool:
        p = self._player(player)
        if p:
            p.set_position(int(position) * 1000)
            self.changed()
        return p is not None

    @action
    def set_volume(self, volume: int, player: str = "") -> bool:
        p = self._player(player)
        if p:
            p.volume = int(volume)
            self.send_request(p.name, setVolume=int(volume))
        return p is not None

    @action
    def refresh(self) -> bool:
        self.send("kdeconnect.mpris.request", {"requestPlayerList": True})
        return True

    def state(self):
        return {
            "current": self.current,
            "players": [p.to_dict() for p in self.active_players()],
            "hidden": [p.name for p in self.players.values() if not self.is_active(p)],
        }
