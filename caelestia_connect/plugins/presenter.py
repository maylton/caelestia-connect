import asyncio
import json
import subprocess
import sys

from ..i18n import _
from .base import Plugin


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

    def destroy(self):
        self._stop()

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

    def _stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write(b'{"stop": true}\n')
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.proc = None

    async def handle(self, pkt):
        if pkt.get_bool("stop"):
            self._stop()
            return
        await self._ensure()
        try:
            self.proc.stdin.write(json.dumps({"dx": pkt.get_float("dx"), "dy": pkt.get_float("dy")}).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.proc = None
