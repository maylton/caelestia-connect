"""Plugin base class. A plugin handles a set of packet types for a single paired device."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..packet import Packet, Payload

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..device import Device

log = logging.getLogger(__name__)


def action(func: Callable) -> Callable:
    """Marks a plugin method as callable from IPC clients (GTK app, shell, CLI)."""
    func._ipc_action = True
    return func


class Plugin:
    id: str = ""
    name: str = ""
    description: str = ""
    icon: str = "extension"
    incoming: tuple[str, ...] = ()
    outgoing: tuple[str, ...] = ()
    enabled_by_default: bool = True
    # [{"key", "type": bool|choice|str|text|folder|file|commands, "label", "default", "choices"?}]
    settings_schema: list[dict[str, Any]] = []

    def __init__(self, device: Device) -> None:
        self.device = device
        self.daemon: Daemon = device.daemon
        self.log = logging.getLogger(f"plugin.{self.id}")

    async def connected(self) -> None:
        """Called when the plugin is loaded and every time the device (re)connects."""

    def handle(self, pkt: Packet):
        """Handle an incoming packet. May return a coroutine."""

    def destroy(self) -> None:
        """Release resources when the device disconnects, is unpaired or the plugin disabled."""

    def state(self) -> dict[str, Any] | None:
        """Data exposed to IPC clients under device.state[plugin_id]."""
        return None

    @classmethod
    def settings_values(cls, stored: dict[str, Any]) -> dict[str, Any]:
        return {s["key"]: stored.get(s["key"], s.get("default")) for s in cls.settings_schema}

    def setting(self, key: str) -> Any:
        stored = self.device.config.plugin_settings(self.id)
        if key in stored:
            return stored[key]
        for s in self.settings_schema:
            if s["key"] == key:
                return s.get("default")
        return None

    def set_setting(self, key: str, value: Any) -> None:
        self.device.config.plugin_settings(self.id)[key] = value
        self.device.config.save()
        self.settings_changed(key)
        self.changed()

    def settings_changed(self, key: str) -> None:
        """Hook called after a setting was changed from the UI."""

    def send(self, ptype: str, body: dict[str, Any] | None = None, payload: Payload | None = None) -> None:
        self.device.send_nowait(Packet(ptype, body, payload=payload))

    async def send_wait(
        self, ptype: str, body: dict[str, Any] | None = None, payload: Payload | None = None, transfer=None
    ) -> bool:
        return await self.device.send(Packet(ptype, body, payload=payload), transfer)

    def changed(self) -> None:
        self.daemon.state_changed()

    def actions(self) -> dict[str, Callable]:
        out = {}
        for name in dir(type(self)):
            attr = getattr(type(self), name, None)
            if callable(attr) and getattr(attr, "_ipc_action", False):
                out[name] = getattr(self, name)
        return out
