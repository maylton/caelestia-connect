"""caelestia-connectd: the background service speaking the KDE Connect protocol."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any

from gi.repository import Gio

from . import __version__, config, crypto
from .api import Api
from .desktop.audio import Audio
from .desktop.clipboard import Clipboard
from .desktop.input import VirtualInput
from .desktop.mpris import MprisClient
from .desktop.notify import Notifier
from .desktop.system import Battery, Session
from .device import NOT_PAIRED, PAIRED, REQUESTED, REQUESTED_BY_PEER, Device
from .i18n import _
from .ipc import IpcServer
from .lan import LanLink, LanLinkProvider
from .plugins import Registry
from .transfers import Transfer, TransferManager
from .utils import Debouncer, spawn, which

log = logging.getLogger("caelestia-connect")

BUS_NAME = os.environ.get("CAELESTIA_CONNECT_BUS_NAME", "org.caelestia.Connect.Daemon")


class Daemon:
    def __init__(self, discovery: bool = True) -> None:
        config.ensure_dirs()
        self.settings = config.Settings()
        self.trusted = config.TrustedDevices()
        self.identity = crypto.Identity()
        if self.identity.regenerated and self.trusted.ids():
            log.warning("Identity regenerated: previously paired devices must pair again")
            self.trusted.clear()
        self.device_type = config.detect_device_type()
        self.plugins_registry = Registry()

        self.notifier = Notifier()
        self.clipboard = Clipboard()
        self.audio = Audio()
        self.mpris = MprisClient()
        self.vinput = VirtualInput()
        self.battery = Battery()
        self.session = Session()

        self.transfers = TransferManager(self.state_changed, self._transfer_finished)
        self.devices: dict[str, Device] = {}
        self.lan = LanLinkProvider(self, discovery)
        self.api = Api(self)
        self.ipc = IpcServer(self.api.handle)
        self._broadcast = Debouncer(0.08, self._send_state)
        self._pair_notifications: dict[str, int] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        log.info(
            "Caelestia Connect %s - device id %s (%s, %s)",
            __version__,
            self.identity.device_id,
            self.settings.name,
            self.device_type,
        )
        for device_id in self.trusted.ids():
            self.devices[device_id] = Device(self, device_id)
        await self.notifier.start()
        await self.session.start()
        await self.ipc.start()
        await self.lan.start()
        spawn(self._rebroadcast_loop(), name="rebroadcast")

    async def _rebroadcast_loop(self) -> None:
        # Phones that come back to the network (or whose app sleeps in the background) don't always
        # answer broadcasts: keep calling paired devices that are not connected, every minute,
        # including a direct connection to the address where they were last seen.
        await asyncio.sleep(2)
        self.reconnect_known()
        while True:
            await asyncio.sleep(60)
            if any(d.paired and not d.reachable for d in self.devices.values()):
                await self.lan.broadcast()
                self.reconnect_known()

    def remember_address(self, device_id: str, host: str, port: int) -> None:
        if device_id not in self.trusted or not host:
            return
        info = self.trusted.get(device_id) or {}
        port = port or int(info.get("lastPort") or 0) or 1716
        if info.get("lastAddress") != host or info.get("lastPort") != port:
            self.trusted.update(device_id, lastAddress=host, lastPort=port)

    def reconnect_known(self) -> None:
        for d in self.devices.values():
            info = self.trusted.get(d.id) if d.paired else None
            if info and not d.reachable and info.get("lastAddress"):
                self.lan.device_discovered(
                    info["lastAddress"],
                    int(info.get("lastPort") or 1716),
                    d.id,
                    int(info.get("protocolVersion") or 8),
                    force=True,
                )

    def stop(self) -> None:
        log.info("Shutting down")
        for d in self.devices.values():
            d.destroy()
        self.lan.stop()
        self.ipc.stop()
        self.clipboard.stop()
        self.vinput.close()
        self._stopping.set()

    async def wait(self) -> None:
        await self._stopping.wait()

    def link_established(self, link: LanLink, replaced: LanLink | None) -> None:
        device = self.devices.get(link.device_id)
        if device is None:
            device = self.devices[link.device_id] = Device(self, link.device_id)
        device.add_link(link, replaced)

    def link_lost(self, link: LanLink) -> None:
        device = self.devices.get(link.device_id)
        if device is None:
            return
        device.remove_link(link)
        if not device.paired and not device.reachable:
            device.destroy()
            del self.devices[device.id]
            self.state_changed()

    def get_device(self, device_id: str) -> Device:
        d = self.devices.get(device_id)
        if d is None:
            raise KeyError(f"unknown device {device_id}")
        return d

    def pair_state_changed(self, device: Device, prev: str, error: str) -> None:
        nid = self._pair_notifications.pop(device.id, 0)
        if nid:
            self.notifier.close_nowait(nid)
        state = device.pair_state
        if state == REQUESTED_BY_PEER:
            spawn(self._notify_pair_request(device))
        elif state == PAIRED and prev in (REQUESTED, REQUESTED_BY_PEER):
            self.notifier.notify_nowait(
                _("Paired with {device}").format(device=device.name),
                _("The device is ready to use"),
                icon="phone",
                transient=True,
            )
        elif state == NOT_PAIRED and prev in (REQUESTED, REQUESTED_BY_PEER) and error and error != "Canceled by user":
            self.notifier.notify_nowait(
                _("Pairing with {device} failed").format(device=device.name), _(error), icon="dialog-error"
            )
        elif state == NOT_PAIRED and prev == PAIRED:
            self.notifier.notify_nowait(
                _("Unpaired from {device}").format(device=device.name), "", icon="phone", transient=True
            )

    async def _notify_pair_request(self, device: Device) -> None:
        def on_action(key: str) -> None:
            if key == "accept":
                device.accept_pair()
            elif key == "reject":
                device.reject_pair()

        self._pair_notifications[device.id] = await self.notifier.notify(
            _("Pairing request from {device}").format(device=device.name),
            _("Verification key: {key}").format(key=device.verification_key()),
            icon="phone",
            actions=[("accept", _("Accept")), ("reject", _("Reject"))],
            on_action=on_action,
            urgency=2,
            resident=True,
        )

    def _transfer_finished(self, t: Transfer) -> None:
        self.ipc.broadcast({"event": "transfer", "data": t.to_dict()})

    def state_changed(self) -> None:
        self._broadcast()

    def state(self) -> dict[str, Any]:
        devices = sorted(self.devices.values(), key=lambda d: (not d.paired, not d.reachable, d.name.lower()))
        return {
            "version": __version__,
            "self": {
                "id": self.identity.device_id,
                "name": self.settings.name,
                "type": self.device_type,
                "tcpPort": self.lan.tcp_port,
                "fingerprint": crypto.certificate_fingerprint(self.identity.cert),
            },
            "settings": {
                "name": self.settings.name,
                "customDevices": self.settings.get("customDevices") or [],
                "incomingPath": str(self.settings.incoming_path),
            },
            "capabilities": {
                "uinput": self.vinput.available,
                "uinputError": self.vinput.error,
                "sshfs": bool(which("sshfs")),
                "clipboard": self.clipboard.available,
                "audio": self.audio.available,
            },
            "devices": [d.state() for d in devices],
            "transfers": self.transfers.to_list(),
        }

    def _send_state(self) -> None:
        self.ipc.broadcast({"event": "state", "data": self.state()})


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caelestia-connectd", description="Caelestia Connect daemon")
    parser.add_argument("-v", "--verbose", action="count", default=1, help="more logging (-vv for packets)")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument(
        "--no-discovery", action="store_true", help="do not broadcast nor listen on UDP (direct connections only)"
    )
    args = parser.parse_args(argv)
    _setup_logging(0 if args.quiet else args.verbose)

    from gi.events import GLibEventLoopPolicy

    exit_code = 0

    async def run() -> None:
        nonlocal exit_code
        loop = asyncio.get_running_loop()
        owned = loop.create_future()

        def acquired(*_a):
            if not owned.done():
                owned.set_result(True)

        def lost(*_a):
            if not owned.done():
                owned.set_result(False)

        owner = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, acquired, lost
        )
        try:
            got_name = await asyncio.wait_for(owned, 10)
        except TimeoutError:
            log.error("Timed out connecting to the D-Bus session bus")
            exit_code = 1
            return
        if not got_name:
            log.error("Another instance of caelestia-connectd is already running")
            exit_code = 1
            return
        daemon = Daemon(discovery=not args.no_discovery)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, daemon.stop)
        try:
            await daemon.start()
            await daemon.wait()
        finally:
            Gio.bus_unown_name(owner)

    with GLibEventLoopPolicy():
        loop = asyncio.get_event_loop()
        loop.run_until_complete(run())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
