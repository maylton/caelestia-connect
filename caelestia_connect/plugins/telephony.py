"""Notifications for incoming and missed calls, with an action to mute the ringtone."""

import base64
import hashlib

from .. import config
from ..i18n import _
from .base import Plugin, action
from .contacts import contact_name


class TelephonyPlugin(Plugin):
    id = "telephony"
    name = _("Telephony integration")
    description = _("Show notifications for calls")
    icon = "call"
    incoming = ("kdeconnect.telephony",)
    outgoing = ("kdeconnect.telephony.request_mute",)

    def __init__(self, device):
        super().__init__(device)
        self.nid = 0
        self.call: dict | None = None

    def destroy(self):
        self.daemon.notifier.close_nowait(self.nid)

    def _photo(self, pkt) -> str | None:
        thumb = pkt.get_str("phoneThumbnail")
        if not thumb:
            return None
        try:
            data = base64.b64decode(thumb)
        except ValueError:
            return None
        path = config.CACHE_DIR / "contacts" / (hashlib.sha1(data).hexdigest() + ".jpg")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return str(path)

    def _close(self):
        self.daemon.notifier.close_nowait(self.nid)
        self.nid = 0

    async def handle(self, pkt):
        event = pkt.get_str("event")
        if pkt.get_bool("isCancel") or event == "talking":
            self._close()
            if pkt.get_bool("isCancel"):
                self.call = None
            else:
                self.call = {**(self.call or {}), "event": "talking"}
            self.changed()
            return
        if event == "sms":
            return  # legacy packet, handled by notifications
        number = pkt.get_str("phoneNumber") or _("unknown number")
        contact = pkt.get_str("contactName") or contact_name(self.device, number) or number
        photo = self._photo(pkt)
        if event == "ringing":
            text = _("Incoming call from {name}").format(name=contact)
            actions = [("mute", _("Mute call"))]
        elif event == "missedCall":
            text = _("Missed call from {name}").format(name=contact)
            actions = []
        else:
            return
        self.call = {"event": event, "number": number, "contact": contact, "photo": photo or ""}
        self.nid = await self.daemon.notifier.notify(
            self.device.name,
            text,
            icon="call-start",
            image_path=photo,
            actions=actions,
            on_action=lambda key: self.mute() if key == "mute" else None,
            replaces=self.nid,
            resident=event == "ringing",
            urgency=2 if event == "ringing" else 1,
            category="x-kde.call" if event == "ringing" else "x-kde.call.missed",
            source_device=self.device.name,
        )
        if event == "missedCall":
            self.nid = 0
        self.changed()

    @action
    def mute(self) -> bool:
        self.send("kdeconnect.telephony.request_mute", {"action": "mute"})
        return True

    def state(self):
        return {"call": self.call}
