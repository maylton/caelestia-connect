"""Show the device's notifications on this computer, with actions, replies and dismissal."""

import asyncio
import html
import re
import sys
import time

from .. import config
from ..i18n import _
from ..utils import run_detached, spawn
from .base import Plugin, action

AUTH_CODE_RE = re.compile(r"\b(\d{4,10})\b")
# Android never reports some removals (eg. media notifications when the app is closed), so the
# list is periodically re-requested and anything the phone does not send again is dropped.
RESYNC_INTERVAL = 120
RESYNC_GRACE = 6


class PhoneNotification:
    def __init__(self, pkt) -> None:
        self.desktop_id = 0
        self.icon = ""
        self.time = time.time()
        self.update(pkt)

    def update(self, pkt) -> None:
        self.id = pkt.get_str("id")
        self.app = pkt.get_str("appName")
        self.ticker = pkt.get_str("ticker")
        self.title = pkt.get_str("title")
        self.text = pkt.get_str("text")
        self.group = pkt.get_str("groupName")
        self.dismissable = pkt.get_bool("isClearable")
        self.silent = pkt.get_bool("silent")
        self.payload_hash = pkt.get_str("payloadHash")
        self.reply_id = pkt.get_str("requestReplyId")
        self.actions = [a for a in pkt.get_list("actions") if isinstance(a, str)]
        conv = [m for m in pkt.get_list("conversation") if isinstance(m, dict)]
        self.conversation = [{"sender": str(m.get("sender", "")), "content": str(m.get("content", ""))} for m in conv]
        if self.conversation:
            self.title = self.group or self.conversation[0]["sender"] or self.title
            self.text = "\n".join(
                f"{m['sender']}: {m['content']}" if m["sender"] else m["content"] for m in self.conversation
            )
        if not self.title and not self.text:
            self.text = self.ticker

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "appName": self.app,
            "title": self.title,
            "text": self.text,
            "ticker": self.ticker,
            "icon": self.icon,
            "dismissable": self.dismissable,
            "replyId": self.reply_id,
            "actions": self.actions,
            "time": int(self.time),
            "silent": self.silent,
        }


class NotificationsPlugin(Plugin):
    id = "notifications"
    name = _("Notification sync")
    description = _("Show the device's notifications on this computer")
    icon = "notifications"
    incoming = ("kdeconnect.notification",)
    outgoing = ("kdeconnect.notification.request", "kdeconnect.notification.reply", "kdeconnect.notification.action")
    settings_schema = [
        {"key": "showPopups", "type": "bool", "label": _("Show notifications on this computer"), "default": True},
        {
            "key": "copyAuthCodes",
            "type": "bool",
            "label": _("Copy verification codes when using a notification action"),
            "default": True,
        },
    ]

    def __init__(self, device):
        super().__init__(device)
        self.notifications: dict[str, PhoneNotification] = {}
        self._seen_since_sync: set[str] | None = None
        self._resync_task = spawn(self._resync_loop(), name="notifications:resync")

    def destroy(self):
        self._resync_task.cancel()
        for n in self.notifications.values():
            self.daemon.notifier.close_nowait(n.desktop_id)
        self.notifications.clear()

    async def connected(self):
        await self.resync()

    async def _resync_loop(self):
        while True:
            await asyncio.sleep(RESYNC_INTERVAL)
            await self.resync()

    async def resync(self):
        """Ask the phone for every active notification and forget the ones it no longer has."""
        self._seen_since_sync = set()
        self.send("kdeconnect.notification.request", {"request": True})
        await asyncio.sleep(RESYNC_GRACE)
        seen, self._seen_since_sync = self._seen_since_sync, None
        if seen is None:
            return
        stale = [nid for nid in self.notifications if nid not in seen]
        for nid in stale:
            n = self.notifications.pop(nid)
            self.daemon.notifier.close_nowait(n.desktop_id)
        if stale:
            self.changed()

    @staticmethod
    def _strip_prefix(nid: str) -> str:
        return nid.split("::", 1)[1] if nid.startswith("org.kde.kdeconnect_tp::") else nid

    async def handle(self, pkt):
        if pkt.get_bool("request"):
            return
        if pkt.get_bool("isCancel"):
            nid = self._strip_prefix(pkt.get_str("id"))
            n = self.notifications.pop(nid, None) or self.notifications.pop(pkt.get_str("id"), None)
            if n:
                self.daemon.notifier.close_nowait(n.desktop_id)
                self.changed()
            return
        nid = pkt.get_str("id")
        if self._seen_since_sync is not None:
            self._seen_since_sync.add(nid)
        n = self.notifications.get(nid)
        if n is None:
            n = self.notifications[nid] = PhoneNotification(pkt)
        else:
            n.update(pkt)
        if n.payload_hash and pkt.has_payload:
            safe = re.sub(r"[^A-Za-z0-9_-]", "", n.payload_hash)[:128]
            dest = config.CACHE_DIR / "icons" / f"{safe}.png"
            if dest.exists() or await self.device.download(pkt, dest):
                n.icon = str(dest)
        self.changed()
        if not n.silent and self.setting("showPopups"):
            await self._show(n)

    async def _show(self, n: PhoneNotification) -> None:
        actions = [("default", _("Open"))]
        actions += [(f"action:{a}", a) for a in n.actions]
        if n.reply_id:
            actions.append(("reply", _("Reply")))
        if n.dismissable:
            actions.append(("dismiss", _("Dismiss")))
        title = n.title or n.app
        body = html.escape(n.text) if n.text else ""
        n.desktop_id = await self.daemon.notifier.notify(
            title,
            body,
            icon=n.icon or "phone",
            image_path=n.icon or None,
            replaces=n.desktop_id,
            actions=actions,
            on_action=lambda key, nid=n.id: self._desktop_action(nid, key),
            on_reply=(lambda text, nid=n.id: self.reply(nid, text)) if n.reply_id else None,
            reply_placeholder=_("Reply to {name}").format(name=title) if n.reply_id else None,
            source_device=self.device.name,
            app_name=n.app or self.device.name,
        )

    def _desktop_action(self, nid: str, key: str) -> None:
        if key.startswith("action:"):
            self.invoke_action(nid, key[7:])
        elif key == "reply":
            run_detached(sys.executable, "-m", "caelestia_connect.ui", "--reply", self.device.id, nid)
        elif key == "dismiss":
            self.dismiss(nid)
        elif key == "default":
            run_detached(
                sys.executable, "-m", "caelestia_connect.ui", "--device", self.device.id, "--page", "notifications"
            )

    @action
    def dismiss(self, id: str) -> bool:
        n = self.notifications.pop(id, None)
        if n is None:
            return False
        self.send("kdeconnect.notification.request", {"cancel": id})
        self.daemon.notifier.close_nowait(n.desktop_id)
        self.changed()
        return True

    @action
    def dismiss_all(self) -> bool:
        for nid in [i for i, n in self.notifications.items() if n.dismissable]:
            self.dismiss(nid)
        return True

    @action
    def reply(self, id: str, message: str) -> bool:
        n = self.notifications.get(id)
        reply_id = n.reply_id if n else id
        if not reply_id or not message:
            return False
        self.send("kdeconnect.notification.reply", {"requestReplyId": reply_id, "message": message})
        return True

    @action
    def invoke_action(self, id: str, action: str) -> bool:
        self.send("kdeconnect.notification.action", {"key": id, "action": action})
        if self.setting("copyAuthCodes"):
            m = AUTH_CODE_RE.search(action.replace("⁣", ""))
            if m:
                spawn(self.daemon.clipboard.set_text(m.group(1)))
        return True

    @action
    def refresh(self) -> bool:
        spawn(self.resync())
        return True

    def _is_media(self, n: PhoneNotification) -> bool:
        """Media notifications duplicate the device's players, which are shown on their own."""
        if n.dismissable:
            return False
        remote = self.device.plugin("mprisremote")
        players = remote.players if remote else {}
        return any(n.app and (n.app == p or n.app.lower() in p.lower()) for p in players)

    def state(self):
        items = sorted((n for n in self.notifications.values() if not self._is_media(n)), key=lambda n: -n.time)
        return {"items": [n.to_dict() for n in items]}
