"""Keep the screen awake while the device is connected."""

import asyncio

from ..i18n import _
from ..utils import run, spawn, which
from .base import Plugin

_holders: set[str] = set()
_state = {"proc": None, "shell": False, "shell_was_enabled": False}


async def _acquire(device_id: str) -> None:
    first = not _holders
    _holders.add(device_id)
    if not first:
        return
    if which("qs"):
        res = await run("qs", "-c", "caelestia", "ipc", "call", "idleInhibitor", "isEnabled", timeout=3)
        if res.ok and res.stdout.strip() in ("true", "false"):
            _state["shell"] = True
            _state["shell_was_enabled"] = res.stdout.strip() == "true"
            await run("qs", "-c", "caelestia", "ipc", "call", "idleInhibitor", "enable", timeout=3)
            return
    if which("systemd-inhibit"):
        _state["proc"] = await asyncio.create_subprocess_exec(
            "systemd-inhibit",
            "--what=idle",
            "--who=Caelestia Connect",
            "--why=Device connected",
            "sleep",
            "infinity",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )


async def _release(device_id: str) -> None:
    _holders.discard(device_id)
    if _holders:
        return
    if _state["shell"]:
        _state["shell"] = False
        if not _state["shell_was_enabled"]:
            await run("qs", "-c", "caelestia", "ipc", "call", "idleInhibitor", "disable", timeout=3)
    proc = _state["proc"]
    if proc and proc.returncode is None:
        proc.terminate()
    _state["proc"] = None


class ScreensaverInhibitPlugin(Plugin):
    id = "screensaver_inhibit"
    name = _("Prevent screensaver")
    description = _("Keep the screen awake while the device is connected")
    icon = "coffee"
    incoming = ()
    # Not tied to any packet: offered to every device that talks to us
    outgoing = ("kdeconnect.ping",)
    enabled_by_default = False

    async def connected(self):
        await _acquire(self.device.id)

    def destroy(self):
        spawn(_release(self.device.id))
