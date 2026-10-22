"""Forward this computer's notifications to the device (monitors org.freedesktop.Notifications.Notify)."""

import itertools
import logging
import re

from gi.repository import Gio, GLib

from ..desktop.notify import SOURCE_HINT
from ..i18n import _
from ..packet import Payload
from .base import Plugin, action

log = logging.getLogger(__name__)
_ids = itertools.count(1)
_TAG_RE = re.compile(r"<[^>]+>")


class NotificationMonitor:
    """Becomes a D-Bus monitor on a private connection and reports every Notify() call."""

    def __init__(self) -> None:
        self.conn: Gio.DBusConnection | None = None
        self.listeners: list = []
        self._filter_id = 0

    def start(self) -> None:
        if self.conn is not None:
            return
        try:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.conn = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            self._filter_id = self.conn.add_filter(self._filter)
            self.conn.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.Monitoring",
                "BecomeMonitor",
                GLib.Variant(
                    "(asu)", (["type='method_call',interface='org.freedesktop.Notifications',member='Notify'"], 0)
                ),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error as e:
            log.warning("Cannot monitor notifications: %s", e.message)
            self.stop()

    def stop(self) -> None:
        if self.conn is not None:
            if self._filter_id:
                self.conn.remove_filter(self._filter_id)
            try:
                self.conn.close_sync(None)
            except GLib.Error:
                pass
            self.conn = None

    def _filter(self, _conn, message: Gio.DBusMessage, incoming: bool):
        # Runs in GDBus' worker thread: hop back to the main loop
        if (
            incoming
            and message.get_message_type() == Gio.DBusMessageType.METHOD_CALL
            and message.get_member() == "Notify"
            and message.get_signature() == "susssasa{sv}i"
        ):
            args = message.get_body().unpack()
            GLib.idle_add(self._dispatch, args)
        return

    def _dispatch(self, args) -> bool:
        for cb in list(self.listeners):
            try:
                cb(*args)
            except Exception:
                log.exception("Notification listener failed")
        return False


_monitor = NotificationMonitor()


class SendNotificationsPlugin(Plugin):
    id = "sendnotifications"
    name = _("Send notifications")
    description = _("Show this computer's notifications on the device")
    icon = "send_to_mobile"
    incoming = ("kdeconnect.notification.request",)
    outgoing = ("kdeconnect.notification",)
    enabled_by_default = False
    settings_schema = [
        {"key": "persistentOnly", "type": "bool", "label": _("Only persistent notifications"), "default": False},
        {"key": "includeBody", "type": "bool", "label": _("Include the notification body"), "default": True},
        {"key": "syncIcons", "type": "bool", "label": _("Send icons"), "default": True},
        {
            "key": "minUrgency",
            "type": "choice",
            "label": _("Minimum urgency"),
            "default": 0,
            "choices": [[0, _("Low")], [1, _("Normal")], [2, _("Critical")]],
        },
        {"key": "blockedApps", "type": "text", "label": _("Blocked applications (one per line)"), "default": ""},
    ]

    def __init__(self, device):
        super().__init__(device)
        _monitor.start()
        _monitor.listeners.append(self._on_notify)
        self.sent: dict[str, dict] = {}

    def destroy(self):
        if self._on_notify in _monitor.listeners:
            _monitor.listeners.remove(self._on_notify)
        if not _monitor.listeners:
            _monitor.stop()

    def handle(self, pkt):
        if pkt.has("cancel"):
            # The device dismissed one of our notifications: nothing to close on our side reliably
            self.sent.pop(pkt.get_str("cancel"), None)

    def _on_notify(self, app_name, replaces_id, app_icon, summary, body, actions, hints, timeout):
        if SOURCE_HINT in hints or app_name == "Caelestia Connect":
            return
        if self.setting("persistentOnly") and timeout != 0:
            return
        urgency = hints.get("urgency")
        if isinstance(urgency, int) and urgency < int(self.setting("minUrgency") or 0):
            return
        blocked = {app.strip().lower() for app in (self.setting("blockedApps") or "").splitlines() if app.strip()}
        if app_name.lower() in blocked or not summary:
            return
        text = _TAG_RE.sub("", body or "") if self.setting("includeBody") else ""
        nid = str(replaces_id) if replaces_id else f"cc{next(_ids)}"
        packet_body = {
            "id": nid,
            "appName": app_name or _("Notification"),
            "ticker": f"{summary}: {text}" if text else summary,
            "isClearable": True,
            "title": summary,
            "silent": False,
        }
        if text:
            packet_body["text"] = text
        payload = None
        if self.setting("syncIcons") and not replaces_id:
            payload = self._icon_payload(app_icon, hints)
        self.sent[nid] = packet_body
        self.send("kdeconnect.notification", packet_body, payload=payload)

    @staticmethod
    def _icon_payload(app_icon: str, hints: dict):
        path = hints.get("image-path") or hints.get("image_path") or app_icon
        if isinstance(path, str) and path.startswith("file://"):
            path = path[7:]
        if isinstance(path, str) and path.startswith("/") and path.lower().endswith(".png"):
            try:
                return Payload(path)
            except OSError:
                return None
        return None

    @action
    def test(self) -> bool:
        self.send(
            "kdeconnect.notification",
            {
                "id": f"cc{next(_ids)}",
                "appName": "Caelestia Connect",
                "title": _("Test notification"),
                "ticker": _("Test notification"),
                "text": _("Notifications from this computer will appear here"),
                "isClearable": True,
                "silent": False,
            },
        )
        return True
