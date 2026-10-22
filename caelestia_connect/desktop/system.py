"""Local battery (UPower), session lock (logind / Caelestia) and sound playback."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from gi.repository import Gio, GLib

from ..utils import run, spawn, which

log = logging.getLogger(__name__)


class Battery:
    """Local battery status through UPower's display device."""

    def __init__(self) -> None:
        self.proxy: Gio.DBusProxy | None = None
        self._listeners: list[Callable[[], None]] = []

    async def start(self) -> None:
        if self.proxy is not None:
            return
        try:
            bus = await Gio.bus_get(Gio.BusType.SYSTEM)
            self.proxy = await Gio.DBusProxy.new(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower/devices/DisplayDevice",
                "org.freedesktop.UPower.Device",
            )
            self.proxy.connect("g-properties-changed", lambda *_: self._emit())
        except GLib.Error as e:
            log.info("UPower unavailable: %s", e.message)
            self.proxy = None

    def _prop(self, name: str, default=None):
        if self.proxy is None:
            return default
        v = self.proxy.get_cached_property(name)
        return v.unpack() if v is not None else default

    def status(self) -> dict | None:
        """None when the machine has no battery."""
        if not self._prop("IsPresent", False) or self._prop("Type", 0) != 2:
            return None
        state = self._prop("State", 0)
        return {
            "charge": int(round(self._prop("Percentage", 0.0))),
            "charging": state in (1, 4),
            "discharging": state in (2, 6),
        }

    def subscribe(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def _emit(self) -> None:
        for cb in list(self._listeners):
            cb()


class Session:
    """Session lock state. Caelestia reports its own lock state over IPC; logind is the fallback."""

    def __init__(self) -> None:
        self.locked = False
        self.reported_by_shell = False
        self._listeners: list[Callable[[bool], None]] = []
        self._session: Gio.DBusProxy | None = None

    async def start(self) -> None:
        try:
            bus = await Gio.bus_get(Gio.BusType.SYSTEM)
            path = "/org/freedesktop/login1/session/auto"
            sid = os.environ.get("XDG_SESSION_ID")
            if sid:
                path = f"/org/freedesktop/login1/session/{sid}"
            self._session = await Gio.DBusProxy.new(
                bus, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.login1", path, "org.freedesktop.login1.Session"
            )
            self._session.connect("g-properties-changed", self._logind_changed)
            v = self._session.get_cached_property("LockedHint")
            if v is not None:
                self.locked = v.unpack()
        except GLib.Error as e:
            log.info("logind unavailable: %s", e.message)

    def _logind_changed(self, _proxy, changed: GLib.Variant, _inv) -> None:
        props = changed.unpack()
        if "LockedHint" in props and not self.reported_by_shell:
            self._set(bool(props["LockedHint"]))

    def report_from_shell(self, locked: bool) -> None:
        self.reported_by_shell = True
        self._set(locked)

    def _set(self, locked: bool) -> None:
        if locked != self.locked:
            self.locked = locked
            for cb in list(self._listeners):
                cb(locked)

    def subscribe(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    async def lock(self) -> bool:
        # `loginctl lock-session` triggers Caelestia's lock screen (and any other locker)
        res = await run("loginctl", "lock-session")
        if not res.ok and which("qs"):
            res = await run("qs", "-c", "caelestia", "ipc", "call", "lock", "lock")
        await asyncio.sleep(0.6)
        if not self.reported_by_shell:
            await self._poll_shell()
        return res.ok

    async def unlock(self) -> bool:
        res = await run("loginctl", "unlock-session")
        if which("qs"):
            await run("qs", "-c", "caelestia", "ipc", "call", "lock", "unlock")
        return res.ok

    async def _poll_shell(self) -> None:
        if not which("qs"):
            return
        res = await run("qs", "-c", "caelestia", "ipc", "call", "lock", "isLocked", timeout=3)
        if res.ok and res.stdout.strip() in ("true", "false"):
            self._set(res.stdout.strip() == "true")


SOUND_DIRS = [Path("/usr/share/sounds/freedesktop/stereo"), Path("/usr/share/sounds/ocean/stereo")]


def default_ring_sound() -> str:
    for d in SOUND_DIRS:
        for name in ("phone-incoming-call.oga", "alarm-clock-elapsed.oga", "bell.oga"):
            p = d / name
            if p.exists():
                return str(p)
    return ""


class SoundLoop:
    """Plays a sound file repeatedly until stopped (used by "find this device")."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, path: str, duration: float = 60) -> None:
        self.stop()
        player = which("pw-play") or which("paplay") or which("canberra-gtk-play")
        if not player or not path:
            log.warning("No sound player available")
            return
        self._task = spawn(self._loop(player, path, duration), name="soundloop")

    async def _loop(self, player: str, path: str, duration: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + duration
        args = [player, "-f", path] if player.endswith("canberra-gtk-play") else [player, path]
        try:
            while loop.time() < end:
                self._proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await self._proc.wait()
                await asyncio.sleep(0.2)
        finally:
            self._proc = None

    def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        if self._task:
            self._task.cancel()
            self._task = None
