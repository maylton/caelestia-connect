"""Where a phone's sound comes out: connecting its Bluetooth audio (A2DP source) sends it to this computer,
disconnecting it brings the sound back to the phone. BlueZ and PipeWire do the rest."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

from ..utils import Debouncer

log = logging.getLogger(__name__)

A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
BLUEZ = "org.bluez"


@dataclass
class PhoneAudio:
    path: str
    address: str
    name: str
    on_computer: bool
    playing: bool


class BluezAudio:
    """Bonded devices that can send audio to this computer, kept up to date from BlueZ signals."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self.on_change = on_change
        self.devices: list[PhoneAudio] = []
        self._bus: Gio.DBusConnection | None = None
        self._subscription = 0
        self._refresh = Debouncer(0.3, self.refresh)

    async def start(self) -> None:
        try:
            self._bus = await Gio.bus_get(Gio.BusType.SYSTEM)
        except GLib.Error as e:
            log.info("No system bus for Bluetooth audio: %s", e.message)
            return
        self._subscription = self._bus.signal_subscribe(
            BLUEZ, None, None, None, None, Gio.DBusSignalFlags.NONE, lambda *_: self._refresh()
        )
        await self.refresh()

    def stop(self) -> None:
        self._refresh.cancel()
        if self._bus and self._subscription:
            self._bus.signal_unsubscribe(self._subscription)
        self._subscription = 0

    async def refresh(self) -> None:
        try:
            res = await self._bus.call(
                BLUEZ,
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
                None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE,
                5000,
            )
            objects = res.unpack()[0]
        except GLib.Error as e:
            log.debug("Listing Bluetooth devices failed: %s", e.message)
            objects = {}
        transports = {
            props["Device"]: props.get("State", "")
            for ifaces in objects.values()
            if (props := ifaces.get("org.bluez.MediaTransport1"))
        }
        devices = []
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and dev.get("Paired") and A2DP_SOURCE in dev.get("UUIDs", []):
                devices.append(
                    PhoneAudio(
                        path,
                        dev.get("Address", ""),
                        dev.get("Alias") or dev.get("Name", ""),
                        path in transports,
                        transports.get(path) == "active",
                    )
                )
        if devices != self.devices:
            self.devices = devices
            self.on_change()

    async def set_on_computer(self, device: PhoneAudio, on_computer: bool) -> None:
        """Raises GLib.Error when BlueZ refuses."""
        await self._bus.call(
            BLUEZ,
            device.path,
            "org.bluez.Device1",
            "ConnectProfile" if on_computer else "DisconnectProfile",
            GLib.Variant("(s)", (A2DP_SOURCE,)),
            None,
            Gio.DBusCallFlags.NONE,
            20000,
        )
        await self.refresh()
