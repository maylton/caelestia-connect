"""Methods the daemon exposes over IPC to the GTK app, the Caelestia shell and the CLI."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .daemon import Daemon
    from .device import Device
    from .ipc import IpcClientConnection


class Api:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.methods = {
            "subscribe": self.subscribe,
            "state": self.state,
            "ping": self.ping,
            "refresh": self.refresh,
            "settings.setName": self.set_name,
            "settings.setIncomingPath": self.set_incoming_path,
            "devices.add": self.add_address,
            "devices.remove": self.remove_address,
            "session.setLocked": self.set_locked,
            "transfer.cancel": self.cancel_transfer,
            "transfers.clear": self.clear_transfers,
        }
        self.device_methods = {
            "device.pair": self.pair,
            "device.accept": self.accept,
            "device.reject": self.reject,
            "device.unpair": self.unpair,
            "device.setPluginEnabled": self.set_plugin_enabled,
            "device.setPluginSetting": self.set_plugin_setting,
            "device.call": self.call_plugin,
        }

    async def handle(self, client: IpcClientConnection, method: str, params: dict) -> Any:
        if method in self.methods:
            result = self.methods[method](client, params)
        elif method in self.device_methods:
            device = self.daemon.get_device(str(params.get("device", "")))
            result = self.device_methods[method](device, params)
        else:
            raise ValueError(f"unknown method {method}")
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def subscribe(self, client, _p):
        client.subscribed = True
        return self.daemon.state()

    def state(self, _client, _p):
        return self.daemon.state()

    def ping(self, _client, _p):
        return "pong"

    async def refresh(self, _client, _p):
        await self.daemon.lan.broadcast()
        self.daemon.reconnect_known()
        return True

    async def set_name(self, _client, p):
        name = config.filter_device_name(str(p["name"]).strip())
        if not name:
            raise ValueError("invalid name")
        self.daemon.settings.set("name", name)
        # Devices only read the name during the handshake: reconnect so they pick it up
        for link in list(self.daemon.lan.links.values()):
            link.close()
        await self.daemon.lan.broadcast()
        self.daemon.state_changed()
        return name

    def set_incoming_path(self, _client, p):
        self.daemon.settings.set("incomingPath", str(p.get("path") or ""))
        self.daemon.state_changed()
        return True

    async def add_address(self, _client, p):
        address = str(p["address"]).strip()
        custom = list(self.daemon.settings.get("customDevices") or [])
        if address and address not in custom:
            custom.append(address)
            self.daemon.settings.set("customDevices", custom)
        await self.daemon.lan.broadcast(extra=[address])
        self.daemon.state_changed()
        return True

    def remove_address(self, _client, p):
        custom = [a for a in (self.daemon.settings.get("customDevices") or []) if a != p["address"]]
        self.daemon.settings.set("customDevices", custom)
        self.daemon.state_changed()
        return True

    def set_locked(self, _client, p):
        self.daemon.session.report_from_shell(bool(p.get("locked")))
        return True

    def cancel_transfer(self, _client, p):
        return self.daemon.transfers.cancel(int(p["id"]))

    def clear_transfers(self, _client, _p):
        self.daemon.transfers.clear_finished()
        return True

    def pair(self, device: Device, _p):
        return device.request_pair()

    def accept(self, device: Device, _p):
        return device.accept_pair()

    def reject(self, device: Device, _p):
        return device.reject_pair()

    def unpair(self, device: Device, _p):
        device.unpair()
        return True

    def set_plugin_enabled(self, device: Device, p):
        device.set_plugin_enabled(str(p["plugin"]), bool(p["enabled"]))
        return True

    def set_plugin_setting(self, device: Device, p):
        plugin_id, key = str(p["plugin"]), str(p["key"])
        plugin = device.plugin(plugin_id)
        if plugin is not None:
            plugin.set_setting(key, p.get("value"))
        else:
            device.config.plugin_settings(plugin_id)[key] = p.get("value")
            device.config.save()
            self.daemon.state_changed()
        return True

    def call_plugin(self, device: Device, p):
        plugin = device.plugin(str(p["plugin"]))
        if plugin is None:
            raise ValueError(f"plugin {p['plugin']} is not loaded for {device.name}")
        fn = plugin.actions().get(str(p["action"]))
        if fn is None:
            raise ValueError(f"unknown action {p['action']}")
        return fn(**(p.get("args") or {}))
