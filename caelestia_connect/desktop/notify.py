"""Client for org.freedesktop.Notifications with action callbacks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from .. import APP_ID, APP_NAME
from ..utils import spawn

log = logging.getLogger(__name__)

# Marker so the "send notifications" plugin never echoes our own notifications back to the phone
SOURCE_HINT = "x-kdeconnect-source-device"


class Notifier:
    def __init__(self) -> None:
        self.proxy: Gio.DBusProxy | None = None
        self._actions: dict[int, Callable[[str], Any]] = {}
        self._closed: dict[int, Callable[[], Any]] = {}
        self._replies: dict[int, Callable[[str], Any]] = {}
        self.capabilities: list[str] = []

    async def start(self) -> None:
        try:
            bus = await Gio.bus_get(Gio.BusType.SESSION)
            self.proxy = await Gio.DBusProxy.new(
                bus,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications",
            )
            self.proxy.connect("g-signal", self._on_signal)
            res = await self.proxy.call("GetCapabilities", None, Gio.DBusCallFlags.NONE, 2000)
            self.capabilities = list(res.unpack()[0])
        except GLib.Error as e:
            log.warning("Notification server unavailable: %s", e.message)

    def _on_signal(self, _proxy, _sender, signal: str, params: GLib.Variant) -> None:
        args = params.unpack()
        nid = args[0]
        if signal == "ActionInvoked":
            cb = self._actions.get(nid)
            if cb:
                self._call(cb, args[1])
        elif signal == "NotificationReplied":
            cb = self._replies.get(nid)
            if cb:
                self._call(cb, args[1])
        elif signal == "NotificationClosed":
            cb = self._closed.pop(nid, None)
            self._actions.pop(nid, None)
            self._replies.pop(nid, None)
            if cb:
                self._call(cb)

    @staticmethod
    def _call(cb: Callable, *args) -> None:
        try:
            res = cb(*args)
            if hasattr(res, "__await__"):
                spawn(res)
        except Exception:
            log.exception("Notification callback failed")

    async def notify(
        self,
        summary: str,
        body: str = "",
        *,
        icon: str = APP_ID,
        actions: list[tuple[str, str]] | None = None,
        on_action: Callable[[str], Any] | None = None,
        on_close: Callable[[], Any] | None = None,
        on_reply: Callable[[str], Any] | None = None,
        replaces: int = 0,
        timeout: int = -1,
        urgency: int | None = None,
        image_path: str | None = None,
        source_device: str | None = None,
        app_name: str = APP_NAME,
        category: str | None = None,
        resident: bool = False,
        transient: bool = False,
        sound: str | None = None,
        reply_placeholder: str | None = None,
    ) -> int:
        if self.proxy is None:
            return 0
        flat: list[str] = []
        for key, label in actions or []:
            flat += [key, label]
        hints: dict[str, GLib.Variant] = {"desktop-entry": GLib.Variant("s", APP_ID)}
        if on_reply is not None and "inline-reply" in self.capabilities:
            flat += ["inline-reply", reply_placeholder or ""]
            if reply_placeholder:
                hints["x-kde-reply-placeholder-text"] = GLib.Variant("s", reply_placeholder)
        if urgency is not None:
            hints["urgency"] = GLib.Variant("y", urgency)
        if image_path:
            hints["image-path"] = GLib.Variant("s", image_path)
        if source_device:
            hints[SOURCE_HINT] = GLib.Variant("s", source_device)
            hints["x-kde-origin-name"] = GLib.Variant("s", source_device)
        if category:
            hints["category"] = GLib.Variant("s", category)
        if resident:
            hints["resident"] = GLib.Variant("b", True)
        if transient:
            hints["transient"] = GLib.Variant("b", True)
        if sound:
            hints["sound-name"] = GLib.Variant("s", sound)
        try:
            res = await self.proxy.call(
                "Notify",
                GLib.Variant("(susssasa{sv}i)", (app_name, replaces, icon, summary, body, flat, hints, timeout)),
                Gio.DBusCallFlags.NONE,
                5000,
            )
        except GLib.Error as e:
            log.warning("Notify failed: %s", e.message)
            return 0
        nid = res.unpack()[0]
        if on_action:
            self._actions[nid] = on_action
        if on_close:
            self._closed[nid] = on_close
        if on_reply:
            self._replies[nid] = on_reply
        return nid

    def notify_nowait(self, summary: str, body: str = "", **kw) -> None:
        spawn(self.notify(summary, body, **kw), name="notify")

    async def close(self, nid: int) -> None:
        if self.proxy is None or not nid:
            return
        self._actions.pop(nid, None)
        self._replies.pop(nid, None)
        self._closed.pop(nid, None)
        try:
            await self.proxy.call("CloseNotification", GLib.Variant("(u)", (nid,)), Gio.DBusCallFlags.NONE, 2000)
        except GLib.Error:
            pass

    def close_nowait(self, nid: int) -> None:
        if nid:
            spawn(self.close(nid), name="notify:close")
