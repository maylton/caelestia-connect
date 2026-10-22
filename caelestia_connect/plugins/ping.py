from ..i18n import _
from .base import Plugin, action


class PingPlugin(Plugin):
    id = "ping"
    name = _("Ping")
    description = _("Send and receive pings")
    icon = "notifications_active"
    incoming = ("kdeconnect.ping",)
    outgoing = ("kdeconnect.ping",)

    def handle(self, pkt):
        message = pkt.get_str("message") or _("Ping!")
        self.daemon.notifier.notify_nowait(self.device.name, message, icon="phone", source_device=self.device.name)

    @action
    def ping(self, message: str = "") -> bool:
        self.send("kdeconnect.ping", {"message": message} if message else {})
        return True
