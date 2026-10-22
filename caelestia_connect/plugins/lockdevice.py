from ..i18n import _
from .base import Plugin, action


class LockDevicePlugin(Plugin):
    id = "lockdevice"
    name = _("Lock device")
    description = _("Lock this computer from the device and see if it is locked")
    icon = "lock"
    incoming = ("kdeconnect.lock.request", "kdeconnect.lock")
    outgoing = ("kdeconnect.lock.request", "kdeconnect.lock")

    def __init__(self, device):
        super().__init__(device)
        self.remote_locked = False
        self._unsub = self.daemon.session.subscribe(lambda _l: self._send_state())

    def destroy(self):
        self._unsub()

    async def connected(self):
        self.send("kdeconnect.lock.request", {"requestLocked": None})
        self._send_state()

    def _send_state(self):
        self.send("kdeconnect.lock", {"isLocked": self.daemon.session.locked})

    async def handle(self, pkt):
        if pkt.has("isLocked"):
            locked = pkt.get_bool("isLocked")
            if locked != self.remote_locked:
                self.remote_locked = locked
                self.changed()
        if pkt.has("requestLocked"):
            self._send_state()
        if pkt.has("lockResult"):
            ok = pkt.get_bool("lockResult")
            self.daemon.notifier.notify_nowait(
                self.device.name,
                _("Remote lock successful") if ok else _("Remote lock failed"),
                icon="system-lock-screen" if ok else "dialog-error",
                source_device=self.device.name,
            )
        if pkt.has("setLocked"):
            if pkt.get_bool("setLocked"):
                ok = await self.daemon.session.lock()
                self.send("kdeconnect.lock", {"lockResult": ok})
            else:
                await self.daemon.session.unlock()
            self._send_state()

    @action
    def set_locked(self, locked: bool = True) -> bool:
        self.send("kdeconnect.lock.request", {"setLocked": bool(locked)})
        return True

    def state(self):
        return {"remoteLocked": self.remote_locked}
