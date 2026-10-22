import asyncio
import json
import subprocess
import sys

from ..i18n import _
from .base import Plugin

IDLE_TIMEOUT = 30


class PresenterPlugin(Plugin):
    """Draws a laser pointer controlled by the device's presentation remote."""

    id = "presenter"
    name = _("Presentation remote")
    description = _("Use the device as a presentation remote with a laser pointer")
    icon = "co_present"
    incoming = ("kdeconnect.presenter",)
    outgoing = ()

    def __init__(self, device):
        super().__init__(device)
        self.proc: asyncio.subprocess.Process | None = None
        self._idle: asyncio.TimerHandle | None = None

    def destroy(self):
        self._close()

    async def _ensure(self):
        if self.proc is None or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "caelestia_connect.ui.presenter",
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _close(self):
        """Ending its input makes the overlay quit."""
        if self._idle:
            self._idle.cancel()
            self._idle = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.proc = None

    async def handle(self, pkt):
        # The overlay stays up while the remote is in use: starting GTK on every press is too slow
        if self._idle:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(IDLE_TIMEOUT, self._close)
        if pkt.get_bool("stop"):
            msg = {"stop": True}
            if self.proc is None:
                return
        else:
            msg = {"dx": pkt.get_float("dx"), "dy": pkt.get_float("dy")}
            await self._ensure()
        try:
            self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.proc = None
