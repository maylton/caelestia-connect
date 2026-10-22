from ..i18n import _
from .base import Plugin


class ConnectivityReportPlugin(Plugin):
    id = "connectivity_report"
    name = _("Connectivity monitor")
    description = _("Show the mobile network signal of the device")
    icon = "signal_cellular_alt"
    incoming = ("kdeconnect.connectivity_report",)
    outgoing = ()

    def __init__(self, device):
        super().__init__(device)
        self.network_type = ""
        self.strength = -1
        self.subscriptions: list[dict] = []

    def handle(self, pkt):
        subs = pkt.get("signalStrengths")
        if not isinstance(subs, dict) or not subs:
            return
        self.subscriptions = [
            {
                "id": str(k),
                "networkType": str(v.get("networkType", "")),
                "signalStrength": int(v.get("signalStrength", -1)),
            }
            for k, v in subs.items()
            if isinstance(v, dict)
        ]
        if self.subscriptions:
            first = self.subscriptions[0]
            self.network_type = first["networkType"]
            self.strength = first["signalStrength"]
        self.changed()

    def state(self):
        return {"networkType": self.network_type, "strength": self.strength, "subscriptions": self.subscriptions}
