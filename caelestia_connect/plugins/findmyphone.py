from ..i18n import _
from .base import Plugin, action


class FindMyPhonePlugin(Plugin):
    id = "findmyphone"
    name = _("Ring my phone")
    description = _("Make the device ring so you can find it")
    icon = "ring_volume"
    incoming = ()
    outgoing = ("kdeconnect.findmyphone.request",)

    @action
    def ring(self) -> bool:
        self.send("kdeconnect.findmyphone.request")
        return True
