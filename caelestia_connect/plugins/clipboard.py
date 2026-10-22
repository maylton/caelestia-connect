import mimetypes
import tempfile
from pathlib import Path

from .. import config
from ..i18n import _
from .base import Plugin, action


class ClipboardPlugin(Plugin):
    id = "clipboard"
    name = _("Clipboard")
    description = _("Share the clipboard between devices")
    icon = "content_paste"
    incoming = ("kdeconnect.clipboard", "kdeconnect.clipboard.connect", "kdeconnect.clipboard.file")
    outgoing = ("kdeconnect.clipboard", "kdeconnect.clipboard.connect", "kdeconnect.clipboard.file")
    settings_schema = [
        {
            "key": "autoShare",
            "type": "bool",
            "label": _("Automatically share the clipboard from this computer"),
            "default": True,
        },
        {
            "key": "sendPassword",
            "type": "bool",
            "label": _("Include passwords (as marked by password managers)"),
            "default": True,
        },
    ]

    def __init__(self, device):
        super().__init__(device)
        self.cb = self.daemon.clipboard
        self.cb.start()
        self._unsub = self.cb.subscribe(self._local_changed)

    def destroy(self):
        self._unsub()

    def _allowed(self, is_password: bool) -> bool:
        if not self.setting("autoShare"):
            return False
        return not is_password or bool(self.setting("sendPassword"))

    def _local_changed(self, text: str, is_password: bool):
        if self._allowed(is_password):
            self.send("kdeconnect.clipboard", {"content": text})

    async def connected(self):
        if self.cb.text and self._allowed(self.cb.is_password):
            self.send("kdeconnect.clipboard.connect", {"content": self.cb.text, "timestamp": self.cb.timestamp})

    async def handle(self, pkt):
        if pkt.type == "kdeconnect.clipboard.file":
            if not pkt.has_payload or not pkt.has("filename"):
                return
            name = Path(pkt.get_str("filename")).name or "clipboard"
            dest = Path(tempfile.mkdtemp(prefix="clipboard-", dir=config.CACHE_DIR)) / name
            if await self.device.download(pkt, dest):
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                await self.cb.set_file(str(dest), mime)
            return
        content = pkt.get_str("content")
        if pkt.type == "kdeconnect.clipboard":
            await self.cb.set_text(content)
        elif pkt.type == "kdeconnect.clipboard.connect":
            ts = pkt.get_int("timestamp")
            if ts == 0 or ts < self.cb.timestamp or not pkt.has("content"):
                return
            await self.cb.set_text(content)

    @action
    def send_clipboard(self) -> bool:
        if not self.cb.text:
            return False
        self.send("kdeconnect.clipboard", {"content": self.cb.text})
        return True
