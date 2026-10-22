"""Read and send SMS/MMS through the device."""

import base64
import mimetypes
import re
import time
from pathlib import Path

from .. import config
from ..config import read_json, write_json_atomic
from ..i18n import _
from ..utils import Debouncer
from .base import Plugin, action
from .contacts import contact_name

MESSAGE_TYPE_SENT = 2
SMS_REQUEST_VERSION = 2


def _addresses(msg: dict) -> list[str]:
    return [str(a.get("address", "")) for a in msg.get("addresses", []) if isinstance(a, dict)]


class SmsPlugin(Plugin):
    id = "sms"
    name = _("SMS")
    description = _("Read and send text messages")
    icon = "sms"
    incoming = ("kdeconnect.sms.messages", "kdeconnect.sms.attachment_file")
    outgoing = (
        "kdeconnect.sms.request",
        "kdeconnect.sms.request_conversations",
        "kdeconnect.sms.request_conversation",
        "kdeconnect.sms.request_attachment",
    )

    def __init__(self, device):
        super().__init__(device)
        self.cache_path = config.DATA_DIR / "sms" / f"{device.id}.json"
        self.attach_dir = config.CACHE_DIR / "sms-attachments" / device.id
        stored = read_json(self.cache_path, {})
        # thread_id (str) -> {date(str|int) uid: message}
        self.threads: dict[str, dict[str, dict]] = {
            str(t): {str(k): m for k, m in msgs.items()} for t, msgs in (stored.get("threads") or {}).items()
        }
        self.version = 0
        self._save = Debouncer(2.0, self._persist)

    def destroy(self):
        self._save.cancel()
        self._persist()

    def _persist(self):
        write_json_atomic(self.cache_path, {"threads": self.threads})

    async def connected(self):
        self.send("kdeconnect.sms.request_conversations")

    @staticmethod
    def _key(msg: dict) -> str:
        # SMS and MMS live in different tables and may share _id, so add the date
        return f"{msg.get('_id', '')}-{msg.get('date', '')}-{msg.get('type', '')}"

    async def handle(self, pkt):
        if pkt.type == "kdeconnect.sms.messages":
            for msg in pkt.get_list("messages"):
                if not isinstance(msg, dict):
                    continue
                tid = str(msg.get("thread_id", ""))
                if not tid:
                    continue
                thread = self.threads.setdefault(tid, {})
                if int(msg.get("type") or 0) == MESSAGE_TYPE_SENT:
                    for k in [
                        k for k, m in thread.items() if k.startswith("pending-") and m.get("body") == msg.get("body")
                    ]:
                        del thread[k]
                thread[self._key(msg)] = msg
            self.version += 1
            self._save()
            self.changed()
        elif pkt.type == "kdeconnect.sms.attachment_file" and pkt.has_payload:
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(pkt.get_str("filename")).name)
            if not name:
                return
            dest = self.attach_dir / name
            if await self.device.download(pkt, dest):
                self.version += 1
                self.changed()

    def _summary(self, tid: str, msgs: dict[str, dict]) -> dict:
        latest = max(msgs.values(), key=lambda m: int(m.get("date") or 0))
        addrs = _addresses(latest)
        names = [contact_name(self.device, a) or a for a in addrs]
        return {
            "threadId": tid,
            "addresses": addrs,
            "names": names,
            "title": ", ".join(names) or _("Unknown"),
            "snippet": str(latest.get("body") or (_("Attachment") if latest.get("attachments") else "")),
            "date": int(latest.get("date") or 0),
            "read": bool(latest.get("read", True)) or int(latest.get("type") or 0) == MESSAGE_TYPE_SENT,
            "count": len(msgs),
            "subId": latest.get("sub_id"),
        }

    @action
    def conversations(self) -> list:
        out = [self._summary(t, m) for t, m in self.threads.items() if m]
        out.sort(key=lambda c: -c["date"])
        return out

    @action
    def messages(self, thread_id: str) -> list:
        msgs = sorted(self.threads.get(str(thread_id), {}).values(), key=lambda m: int(m.get("date") or 0))
        out = []
        for m in msgs:
            addrs = _addresses(m)
            sender = addrs[0] if addrs else ""
            atts = []
            for a in m.get("attachments") or []:
                if not isinstance(a, dict):
                    continue
                uid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(a.get("unique_identifier", "")))
                local = self.attach_dir / uid if uid else None
                atts.append(
                    {
                        "partId": a.get("part_id"),
                        "mimeType": a.get("mime_type", ""),
                        "uniqueId": a.get("unique_identifier", ""),
                        "thumbnail": a.get("encoded_thumbnail", ""),
                        "path": str(local) if local and local.exists() else "",
                    }
                )
            out.append(
                {
                    "body": str(m.get("body") or ""),
                    "date": int(m.get("date") or 0),
                    "outgoing": int(m.get("type") or 0) == MESSAGE_TYPE_SENT,
                    "sender": sender,
                    "senderName": contact_name(self.device, sender) or sender,
                    "read": bool(m.get("read", True)),
                    "attachments": atts,
                }
            )
        return out

    @action
    def request_conversations(self) -> bool:
        self.send("kdeconnect.sms.request_conversations")
        return True

    @action
    def request_conversation(self, thread_id: str, start: int = -1, count: int = 100) -> bool:
        body = {"threadID": int(thread_id)}
        if start > 0:
            body["rangeStartTimestamp"] = int(start)
        if count > 0:
            body["numberToRequest"] = int(count)
        self.send("kdeconnect.sms.request_conversation", body)
        return True

    @action
    def send_sms(self, addresses: list, message: str = "", attachments: list | None = None, sub_id: int = -1) -> bool:
        addrs = [{"address": str(a)} for a in addresses or [] if str(a).strip()]
        if not addrs or (not message and not attachments):
            return False
        body = {"version": SMS_REQUEST_VERSION, "addresses": addrs}
        if message:
            body["messageBody"] = message
        if sub_id is not None and int(sub_id) != -1:
            body["subID"] = int(sub_id)
        files = []
        for p in attachments or []:
            path = Path(str(p).removeprefix("file://"))
            if path.is_file():
                files.append(
                    {
                        "fileName": path.name,
                        "base64EncodedFile": base64.b64encode(path.read_bytes()).decode(),
                        "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    }
                )
        if files:
            body["attachments"] = files
        self.send("kdeconnect.sms.request", body)
        # Show it right away; the device will report the real message once it is stored
        thread = None
        for tid, msgs in self.threads.items():
            if any(sorted(_addresses(m)) == sorted(a["address"] for a in addrs) for m in msgs.values()):
                thread = tid
                break
        if thread:
            now = int(time.time() * 1000)
            self.threads[thread][f"pending-{now}"] = {
                "body": message,
                "date": now,
                "type": MESSAGE_TYPE_SENT,
                "addresses": addrs,
                "thread_id": thread,
                "read": True,
            }
            self.version += 1
            self.changed()
            # Ask for the stored copy shortly after
            self.request_conversation(thread, count=10)
        return True

    @action
    def get_attachment(self, part_id: int, unique_id: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", unique_id)
        local = self.attach_dir / name
        if local.exists():
            return str(local)
        self.attach_dir.mkdir(parents=True, exist_ok=True)
        self.send("kdeconnect.sms.request_attachment", {"part_id": int(part_id), "unique_identifier": unique_id})
        return ""

    def state(self):
        unread = sum(1 for c in self.conversations() if not c["read"])
        return {"version": self.version, "threads": len(self.threads), "unread": unread}
