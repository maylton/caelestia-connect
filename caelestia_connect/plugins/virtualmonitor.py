"""Use another computer as an extra monitor, or show another computer's virtual monitor here."""

import ipaddress
import socket

from ..desktop import virtualmonitor as vm
from ..i18n import _
from ..utils import spawn
from .base import Plugin, action

DEFAULT_PORT = 5901


def _free_port() -> int:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 100):
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


class VirtualMonitorPlugin(Plugin):
    id = "virtualmonitor"
    name = _("Virtual monitor")
    description = _("Use the other computer as an extra monitor")
    icon = "desktop_windows"
    incoming = ("kdeconnect.virtualmonitor", "kdeconnect.virtualmonitor.request")
    outgoing = ("kdeconnect.virtualmonitor", "kdeconnect.virtualmonitor.request")

    def __init__(self, device):
        super().__init__(device)
        self.server: vm.VirtualMonitorServer | None = None
        self.remote_resolution: dict = {}
        self.remote_client = False
        self.error = ""

    async def connected(self):
        resolution, scale = await vm.primary_resolution()
        self.send(
            "kdeconnect.virtualmonitor",
            {
                "resolutions": [{"resolution": resolution, "scale": scale}],
                "supports_rdp": vm.can_show("rdp"),
                "supports_virt_mon": vm.VirtualMonitorServer.available(),
            },
        )

    def destroy(self):
        if self.server:
            spawn(self.server.stop())
            self.server = None

    def _host(self) -> str:
        try:
            return str(ipaddress.ip_address(self.device.address))
        except ValueError:
            return ""  # Bluetooth only

    def handle(self, pkt):
        if pkt.type == "kdeconnect.virtualmonitor.request":
            if not pkt.has("password"):
                return
            host = self._host()
            shown = host and vm.show(
                pkt.get_str("protocol", "rdp"),
                host,
                pkt.get_int("port", DEFAULT_PORT),
                pkt.get_str("username", "user"),
                pkt.get_str("password"),
            )
            if not shown:
                self.send("kdeconnect.virtualmonitor", {"failed": 0})
            return
        resolutions = pkt.get_list("resolutions")
        if resolutions and isinstance(resolutions[0], dict):
            self.remote_resolution = resolutions[0]
        if pkt.has("supports_rdp"):
            self.remote_client = pkt.get_bool("supports_rdp")
        if pkt.has("failed"):
            self.error = _("The other computer could not open the remote session")
            self.destroy()
        self.changed()

    def state(self):
        return {
            "available": vm.VirtualMonitorServer.available() and self.remote_client and bool(self.remote_resolution),
            "active": bool(self.server and self.server.running),
            "error": self.error,
        }

    @action
    async def start(self) -> dict:
        self.error = ""
        if not vm.VirtualMonitorServer.available():
            self.error = _("Install wayvnc to use another computer as a monitor (sudo pacman -S wayvnc)")
        elif not self.remote_client or not self.remote_resolution:
            self.error = _("The other computer has no remote desktop client")
        elif not self._host():
            self.error = _("The other computer must be connected over the network")
        if self.error:
            self.changed()
            return {"error": self.error}
        await self.stop()
        server = vm.VirtualMonitorServer(f"CAELESTIA-{self.device.id[:6].upper()}")
        try:
            await server.start(
                str(self.remote_resolution.get("resolution") or "1920x1080"),
                float(self.remote_resolution.get("scale") or 1),
                _free_port(),
            )
        except RuntimeError as e:
            self.error = str(e)
            self.changed()
            return {"error": self.error}
        self.server = server
        self.send(
            "kdeconnect.virtualmonitor.request",
            {"protocol": "vnc", "username": server.username, "password": server.password, "port": server.port},
        )
        self.changed()
        return {}

    @action
    async def stop(self) -> None:
        if self.server:
            await self.server.stop()
            self.server = None
            self.changed()
