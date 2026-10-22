"""A remote device: pairing state machine, links and loaded plugins."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from . import const, crypto
from .config import DeviceConfig
from .packet import Packet
from .utils import spawn

if TYPE_CHECKING:
    from .daemon import Daemon
    from .lan import LanLink
    from .plugins.base import Plugin
    from .transfers import Transfer

log = logging.getLogger(__name__)

NOT_PAIRED = "notPaired"
REQUESTED = "requested"
REQUESTED_BY_PEER = "requestedByPeer"
PAIRED = "paired"


class Device:
    def __init__(self, daemon: Daemon, device_id: str) -> None:
        self.daemon = daemon
        self.id = device_id
        self.name = device_id
        self.type = "unknown"
        self.protocol_version = 0
        self.incoming: set[str] = set()
        self.outgoing: set[str] = set()
        self.cert = None
        self.links: list[LanLink] = []
        self.plugins: dict[str, Plugin] = {}
        self.config = DeviceConfig(device_id)
        self.pair_state = NOT_PAIRED
        self.pair_timestamp = 0
        self._pair_timer: asyncio.TimerHandle | None = None
        self.pair_error = ""
        self.last_seen = 0.0

        trusted = daemon.trusted.get(device_id)
        if trusted:
            self.pair_state = PAIRED
            self.name = trusted.get("name") or device_id
            self.type = trusted.get("type") or "unknown"
            self.protocol_version = int(trusted.get("protocolVersion") or 0)
            try:
                self.cert = crypto.cert_from_pem(trusted["certificate"])
            except (KeyError, ValueError):
                self.cert = None
            caps = self.config.data.get("capabilities") or {}
            # Remember capabilities so the UI can show plugins of offline devices
            self.incoming = set(caps.get("incoming") or [])
            self.outgoing = set(caps.get("outgoing") or [])

    @property
    def paired(self) -> bool:
        return self.pair_state == PAIRED

    @property
    def reachable(self) -> bool:
        return any(not link.closed for link in self.links)

    @property
    def address(self) -> str:
        for link in self.links:
            if not link.closed:
                return link.address
        return ""

    @property
    def link(self) -> LanLink | None:
        for link in reversed(self.links):
            if not link.closed:
                return link
        return None

    def verification_key(self) -> str:
        if self.pair_state not in (REQUESTED, REQUESTED_BY_PEER) or self.cert is None:
            return ""
        return crypto.verification_key(self.daemon.identity.cert, self.cert, self.pair_timestamp)

    def add_link(self, link: LanLink, replaced: LanLink | None = None) -> None:
        if replaced in self.links:
            self.links.remove(replaced)
        self.links = [existing for existing in self.links if not existing.closed]
        self.links.append(link)
        link.on_packet = self.handle_packet
        info = link.info
        changed = (
            self.name != info["deviceName"]
            or self.type != info["deviceType"]
            or self.protocol_version != info["protocolVersion"]
        )
        self.name = info["deviceName"]
        self.type = info["deviceType"]
        self.protocol_version = info["protocolVersion"]
        self.cert = link.peer_cert
        self.last_seen = time.time()
        if info["incomingCapabilities"] or info["outgoingCapabilities"]:
            self.incoming = set(info["incomingCapabilities"])
            self.outgoing = set(info["outgoingCapabilities"])
            if self.paired:
                self._save_capabilities()
        if self.paired and changed:
            self.daemon.trusted.update(self.id, name=self.name, type=self.type, protocolVersion=self.protocol_version)
        self.reload_plugins(reconnected=True)
        self.daemon.state_changed()

    def remove_link(self, link: LanLink) -> None:
        if link in self.links:
            self.links.remove(link)
        self.last_seen = time.time()
        if not self.reachable:
            if self.pair_state in (REQUESTED, REQUESTED_BY_PEER):
                self._set_pair_state(NOT_PAIRED, "Device disconnected")
            self.reload_plugins()
        self.daemon.state_changed()

    def can_send(self, ptype: str) -> bool:
        return ptype in (const.PACKET_TYPE_PAIR, const.PACKET_TYPE_IDENTITY) or ptype in self.incoming

    async def send(self, pkt: Packet, transfer: Transfer | None = None) -> bool:
        if not self.paired and pkt.type != const.PACKET_TYPE_PAIR:
            log.debug("Not sending %s to unpaired %s", pkt.type, self.name)
            return False
        if not self.can_send(pkt.type):
            log.debug("%s does not accept %s", self.name, pkt.type)
            if transfer:
                transfer.finish(False, "unsupported")
            return False
        link = self.link
        if link is None:
            if transfer:
                transfer.finish(False, "unreachable")
            return False
        return await link.send(pkt, transfer)

    def send_nowait(self, pkt: Packet, transfer: Transfer | None = None) -> None:
        spawn(self.send(pkt, transfer), name=f"send:{pkt.type}")

    async def download(self, pkt: Packet, dest, transfer: Transfer | None = None) -> bool:
        link = self.link
        if link is None:
            if transfer:
                transfer.finish(False, "unreachable")
            return False
        return await link.download(pkt, dest, transfer)

    def handle_packet(self, pkt: Packet) -> None:
        if pkt.type == const.PACKET_TYPE_PAIR:
            self._handle_pair(pkt)
            return
        if not self.paired:
            log.info("Packet %s from unpaired device %s, telling it we are not paired", pkt.type, self.name)
            self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
            return
        handled = False
        for plugin in list(self.plugins.values()):
            if pkt.type in plugin.incoming:
                handled = True
                try:
                    res = plugin.handle(pkt)
                    if asyncio.iscoroutine(res):
                        spawn(res, name=f"{plugin.id}:handle")
                except Exception:
                    log.exception("Plugin %s failed to handle %s", plugin.id, pkt.type)
        if not handled:
            log.debug("No plugin handles %s", pkt.type)

    def _set_pair_state(self, state: str, error: str = "") -> None:
        prev = self.pair_state
        self.pair_state = state
        self.pair_error = error
        if state not in (REQUESTED, REQUESTED_BY_PEER) and self._pair_timer:
            self._pair_timer.cancel()
            self._pair_timer = None
        if prev != state:
            self.daemon.pair_state_changed(self, prev, error)
        self.daemon.state_changed()

    def _start_pair_timer(self) -> None:
        if self._pair_timer:
            self._pair_timer.cancel()
        self._pair_timer = asyncio.get_event_loop().call_later(const.PAIR_TIMEOUT, self._pair_timeout)

    def _pair_timeout(self) -> None:
        self._pair_timer = None
        if self.pair_state in (REQUESTED, REQUESTED_BY_PEER):
            self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
            self._set_pair_state(NOT_PAIRED, "Timed out")

    def _handle_pair(self, pkt: Packet) -> None:
        wants_pair = pkt.get_bool("pair")
        if wants_pair:
            if self.pair_state == REQUESTED:
                self._pairing_done()
            elif self.pair_state == REQUESTED_BY_PEER:
                log.debug("Ignoring repeated pairing request")
            else:
                if self.pair_state == PAIRED:
                    if not pkt.has("timestamp"):
                        return  # Late accept from a device we already trust
                    self._unpaired(notify_peer=False)
                timestamp = pkt.get_int("timestamp", -1)
                if timestamp < 0:
                    log.warning("Ignoring pairing request without timestamp")
                    return
                if abs(timestamp - int(time.time())) > const.PAIR_TIMESTAMP_TOLERANCE:
                    self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
                    self._set_pair_state(NOT_PAIRED, "Device clocks are out of sync")
                    return
                self.pair_timestamp = timestamp
                self._start_pair_timer()
                self._set_pair_state(REQUESTED_BY_PEER)
        else:
            if self.pair_state in (REQUESTED, REQUESTED_BY_PEER):
                self._set_pair_state(NOT_PAIRED, "Canceled by the other device")
            elif self.pair_state == PAIRED:
                self._unpaired(notify_peer=False)

    def request_pair(self) -> bool:
        if self.pair_state == PAIRED:
            return False
        if self.pair_state == REQUESTED_BY_PEER:
            return self.accept_pair()
        if not self.reachable:
            self.pair_error = "Device not reachable"
            self.daemon.state_changed()
            return False
        self.pair_timestamp = int(time.time())
        self._start_pair_timer()
        self._set_pair_state(REQUESTED)
        self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": True, "timestamp": self.pair_timestamp}))
        return True

    def accept_pair(self) -> bool:
        if self.pair_state == PAIRED:
            return True
        if self.pair_state != REQUESTED_BY_PEER:
            return False
        self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": True}))
        self._pairing_done()
        return True

    def reject_pair(self) -> bool:
        if self.pair_state not in (REQUESTED, REQUESTED_BY_PEER):
            return False
        self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
        self._set_pair_state(NOT_PAIRED, "Canceled by user")
        return True

    def unpair(self) -> None:
        self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
        if self.pair_state != NOT_PAIRED:
            self._unpaired(notify_peer=False)

    def _pairing_done(self) -> None:
        if self.cert is None:
            self._set_pair_state(NOT_PAIRED, "Missing certificate")
            return
        self.daemon.trusted.add(self.id, self.name, self.type, crypto.cert_to_pem(self.cert), self.protocol_version)
        self._save_capabilities()
        self._set_pair_state(PAIRED)
        self.reload_plugins(reconnected=True)

    def _save_capabilities(self) -> None:
        self.config.data["capabilities"] = {"incoming": sorted(self.incoming), "outgoing": sorted(self.outgoing)}
        self.config.save()

    def _unpaired(self, notify_peer: bool) -> None:
        if notify_peer:
            self.send_nowait(Packet(const.PACKET_TYPE_PAIR, {"pair": False}))
        self.daemon.trusted.remove(self.id)
        self._set_pair_state(NOT_PAIRED)
        self.reload_plugins()

    def supported_plugins(self) -> list[str]:
        return self.daemon.plugins_registry.for_capabilities(self.incoming, self.outgoing)

    def plugin_enabled(self, plugin_id: str) -> bool:
        cls = self.daemon.plugins_registry.get(plugin_id)
        return cls is not None and self.config.plugin_enabled(plugin_id, cls.enabled_by_default)

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        self.config.set_plugin_enabled(plugin_id, enabled)
        self.reload_plugins()
        self.daemon.state_changed()

    def reload_plugins(self, reconnected: bool = False) -> None:
        wanted: set[str] = set()
        if self.paired and self.reachable:
            wanted = {p for p in self.supported_plugins() if self.plugin_enabled(p)}
        for pid in list(self.plugins):
            if pid not in wanted:
                plugin = self.plugins.pop(pid)
                try:
                    plugin.destroy()
                except Exception:
                    log.exception("Error unloading plugin %s", pid)
        for pid in sorted(wanted):
            plugin = self.plugins.get(pid)
            is_new = plugin is None
            if is_new:
                cls = self.daemon.plugins_registry.get(pid)
                try:
                    plugin = cls(self)
                except Exception:
                    log.exception("Could not load plugin %s", pid)
                    continue
                self.plugins[pid] = plugin
            if is_new or reconnected:
                spawn(plugin.connected(), name=f"{pid}:connected")

    def plugin(self, plugin_id: str) -> Plugin | None:
        return self.plugins.get(plugin_id)

    def destroy(self) -> None:
        for plugin in self.plugins.values():
            try:
                plugin.destroy()
            except Exception:
                log.exception("Error unloading plugin %s", plugin.id)
        self.plugins.clear()

    def state(self) -> dict[str, Any]:
        registry = self.daemon.plugins_registry
        supported = self.supported_plugins()
        plugins_state: dict[str, Any] = {}
        for pid, plugin in self.plugins.items():
            try:
                s = plugin.state()
            except Exception:
                log.exception("Plugin %s state failed", pid)
                s = None
            if s is not None:
                plugins_state[pid] = s
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "paired": self.paired,
            "reachable": self.reachable,
            "pairState": self.pair_state,
            "pairError": self.pair_error,
            "verificationKey": self.verification_key(),
            "address": self.address,
            "protocolVersion": self.protocol_version,
            "lastSeen": int(self.last_seen),
            "fingerprint": crypto.certificate_fingerprint(self.cert) if self.cert else "",
            "supportedPlugins": supported,
            "plugins": [
                {
                    "id": pid,
                    "name": registry.get(pid).name,
                    "description": registry.get(pid).description,
                    "enabled": self.plugin_enabled(pid),
                    "loaded": pid in self.plugins,
                    "settings": registry.get(pid).settings_schema,
                    "values": registry.get(pid).settings_values(self.config.plugin_settings(pid)),
                }
                for pid in supported
                if registry.get(pid)
            ],
            "state": plugins_state,
        }
