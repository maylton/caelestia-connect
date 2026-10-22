"""Plugin registry."""

from __future__ import annotations

from .base import Plugin
from .battery import BatteryPlugin
from .clipboard import ClipboardPlugin
from .connectivity_report import ConnectivityReportPlugin
from .contacts import ContactsPlugin
from .digitizer import DigitizerPlugin
from .findmyphone import FindMyPhonePlugin
from .findthisdevice import FindThisDevicePlugin
from .lockdevice import LockDevicePlugin
from .mousepad import MousepadPlugin
from .mpriscontrol import MprisControlPlugin
from .mprisremote import MprisRemotePlugin
from .notifications import NotificationsPlugin
from .pausemusic import PauseMusicPlugin
from .ping import PingPlugin
from .presenter import PresenterPlugin
from .remotecommands import RemoteCommandsPlugin
from .remotecontrol import RemoteControlPlugin
from .remotekeyboard import RemoteKeyboardPlugin
from .remotesystemvolume import RemoteSystemVolumePlugin
from .runcommand import RunCommandPlugin
from .screensaver_inhibit import ScreensaverInhibitPlugin
from .sendnotifications import SendNotificationsPlugin
from .sftp import SftpPlugin
from .share import SharePlugin
from .shareinputdevices import ShareInputDevicesPlugin
from .sms import SmsPlugin
from .systemvolume import SystemVolumePlugin
from .telephony import TelephonyPlugin
from .virtualmonitor import VirtualMonitorPlugin

ALL_PLUGINS: list[type[Plugin]] = [
    BatteryPlugin,
    ClipboardPlugin,
    ConnectivityReportPlugin,
    ContactsPlugin,
    DigitizerPlugin,
    FindMyPhonePlugin,
    FindThisDevicePlugin,
    LockDevicePlugin,
    MousepadPlugin,
    MprisControlPlugin,
    MprisRemotePlugin,
    NotificationsPlugin,
    PauseMusicPlugin,
    PingPlugin,
    PresenterPlugin,
    RemoteCommandsPlugin,
    RemoteControlPlugin,
    RemoteKeyboardPlugin,
    RemoteSystemVolumePlugin,
    RunCommandPlugin,
    ScreensaverInhibitPlugin,
    SendNotificationsPlugin,
    SftpPlugin,
    SharePlugin,
    ShareInputDevicesPlugin,
    SmsPlugin,
    SystemVolumePlugin,
    TelephonyPlugin,
    VirtualMonitorPlugin,
]


class Registry:
    def __init__(self) -> None:
        self.plugins: dict[str, type[Plugin]] = {p.id: p for p in ALL_PLUGINS}

    def get(self, plugin_id: str) -> type[Plugin] | None:
        return self.plugins.get(plugin_id)

    def incoming_capabilities(self) -> list[str]:
        return sorted({t for p in self.plugins.values() for t in p.incoming})

    def outgoing_capabilities(self) -> list[str]:
        return sorted({t for p in self.plugins.values() for t in p.outgoing})

    def for_capabilities(self, incoming: set[str], outgoing: set[str]) -> list[str]:
        """Plugins useful with a device that accepts `incoming` and sends `outgoing` packet types."""
        return sorted(
            pid
            for pid, p in self.plugins.items()
            if any(t in outgoing for t in p.incoming) or any(t in incoming for t in p.outgoing)
        )
