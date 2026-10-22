from ..i18n import _
from .base import Plugin

THRESHOLD_NONE = 0
THRESHOLD_LOW = 1


class BatteryPlugin(Plugin):
    id = "battery"
    name = _("Battery monitor")
    description = _("Show the battery of the device and report the battery of this computer")
    icon = "battery_full"
    incoming = ("kdeconnect.battery",)
    outgoing = ("kdeconnect.battery",)

    def __init__(self, device):
        super().__init__(device)
        self.charge = -1
        self.charging = False
        self._low_notified = False
        self._unsub = self.daemon.battery.subscribe(self._send_local)

    async def connected(self):
        await self.daemon.battery.start()
        self._send_local()

    def destroy(self):
        self._unsub()

    def _send_local(self):
        status = self.daemon.battery.status()
        if status is None:
            return
        low = status["charge"] <= 15 and status["discharging"]
        self.send(
            "kdeconnect.battery",
            {
                "isCharging": status["charging"],
                "currentCharge": status["charge"],
                "batteryQuantity": 1,
                "thresholdEvent": THRESHOLD_LOW if low else THRESHOLD_NONE,
            },
        )

    def handle(self, pkt):
        self.charging = pkt.get_bool("isCharging")
        self.charge = pkt.get_int("currentCharge", -1)
        if pkt.get_int("thresholdEvent") == THRESHOLD_LOW and not self.charging:
            if not self._low_notified:
                self._low_notified = True
                self.daemon.notifier.notify_nowait(
                    _("{device}: Low battery").format(device=self.device.name),
                    _("Battery at {charge}%").format(charge=self.charge),
                    icon="battery-caution",
                    source_device=self.device.name,
                    urgency=2,
                )
        elif self.charging or self.charge > 20:
            self._low_notified = False
        self.changed()

    def state(self):
        return {"charge": self.charge, "charging": self.charging}
