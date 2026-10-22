"""MPRIS: watch local media players and export remote (phone) players on the session bus."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from ..utils import spawn

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
OWN_PREFIX = "org.mpris.MediaPlayer2.caelestiaconnect."
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
IGNORED = ("org.mpris.MediaPlayer2.playerctld", "org.mpris.MediaPlayer2.kdeconnect.")


def _unpack(v: GLib.Variant | None) -> Any:
    return v.unpack() if v is not None else None


class LocalPlayer:
    def __init__(self, client: MprisClient, bus_name: str, identity: str, player: Gio.DBusProxy) -> None:
        self.client = client
        self.bus_name = bus_name
        self.identity = identity
        self.name = identity
        self.proxy = player
        self.proxy.connect("g-properties-changed", self._props_changed)
        self.proxy.connect("g-signal", self._signal)

    def prop(self, name: str, default: Any = None) -> Any:
        v = _unpack(self.proxy.get_cached_property(name))
        return default if v is None else v

    @property
    def metadata(self) -> dict[str, Any]:
        return self.prop("Metadata", {}) or {}

    async def position(self) -> int:
        """Position in microseconds (not cached by the proxy: it never emits changes)."""
        try:
            res = await self.proxy.get_connection().call(
                self.bus_name,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (PLAYER_IFACE, "Position")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                1000,
            )
            return int(res.unpack()[0])
        except (GLib.Error, TypeError, ValueError):
            return 0

    async def call(self, method: str, args: GLib.Variant | None = None) -> None:
        try:
            await self.proxy.call(method, args, Gio.DBusCallFlags.NONE, 2000)
        except GLib.Error as e:
            log.debug("MPRIS %s on %s failed: %s", method, self.name, e.message)

    async def set(self, prop: str, value: GLib.Variant) -> None:
        try:
            await self.proxy.get_connection().call(
                self.bus_name,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "Set",
                GLib.Variant("(ssv)", (PLAYER_IFACE, prop, value)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
            )
        except GLib.Error as e:
            log.debug("MPRIS set %s on %s failed: %s", prop, self.name, e.message)

    def _props_changed(self, _proxy, changed: GLib.Variant, _invalidated) -> None:
        self.client._player_changed(self, changed.unpack())

    def _signal(self, _proxy, _sender, signal: str, params: GLib.Variant) -> None:
        if signal == "Seeked":
            self.client._seeked(self, params.unpack()[0])


class MprisClient:
    """Keeps track of local players while at least one owner needs them.

    Callbacks: on_list(), on_change(player, props), on_seeked(player, us).
    """

    def __init__(self) -> None:
        self.bus: Gio.DBusConnection | None = None
        self.players: dict[str, LocalPlayer] = {}  # display name -> player
        self._sub_id = 0
        self.on_list: list[Callable[[], None]] = []
        self.on_change: list[Callable[[LocalPlayer, dict], None]] = []
        self.on_seeked: list[Callable[[LocalPlayer, int], None]] = []
        self._owners: set[int] = set()

    async def acquire(self, owner: object) -> None:
        first = not self._owners
        self._owners.add(id(owner))
        if not first:
            return
        self.bus = await Gio.bus_get(Gio.BusType.SESSION)
        self._sub_id = self.bus.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            None,
            Gio.DBusSignalFlags.NONE,
            self._name_owner_changed,
        )
        res = await self.bus.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
            None,
            GLib.VariantType("(as)"),
            Gio.DBusCallFlags.NONE,
            2000,
        )
        for name in res.unpack()[0]:
            if self._wanted(name):
                await self._add(name)
        self._emit_list()

    def release(self, owner: object) -> None:
        if id(owner) not in self._owners:
            return
        self._owners.discard(id(owner))
        if self._owners:
            return
        if self.bus and self._sub_id:
            self.bus.signal_unsubscribe(self._sub_id)
        self._sub_id = 0
        self.players.clear()

    @staticmethod
    def _wanted(name: str) -> bool:
        return name.startswith(MPRIS_PREFIX) and not name.startswith(OWN_PREFIX) and not name.startswith(IGNORED)

    def _name_owner_changed(self, _conn, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        name, old, new = params.unpack()
        if not self._wanted(name):
            return
        if old:
            self._remove(name)
        if new:
            spawn(self._add_and_emit(name))
        elif old:
            self._emit_list()

    async def _add_and_emit(self, name: str) -> None:
        await self._add(name)
        self._emit_list()

    async def _add(self, bus_name: str) -> None:
        if any(p.bus_name == bus_name for p in self.players.values()):
            return
        try:
            root = await Gio.DBusProxy.new(self.bus, Gio.DBusProxyFlags.NONE, None, bus_name, OBJECT_PATH, ROOT_IFACE)
            player = await Gio.DBusProxy.new(
                self.bus, Gio.DBusProxyFlags.NONE, None, bus_name, OBJECT_PATH, PLAYER_IFACE
            )
        except GLib.Error as e:
            log.debug("Could not watch %s: %s", bus_name, e.message)
            return
        identity = _unpack(root.get_cached_property("Identity")) or bus_name[len(MPRIS_PREFIX) :]
        unique = identity
        i = 2
        while unique in self.players:
            unique = f"{identity} [{i}]"
            i += 1
        lp = LocalPlayer(self, bus_name, identity, player)
        lp.name = unique
        self.players[unique] = lp

    def _remove(self, bus_name: str) -> None:
        for key, p in list(self.players.items()):
            if p.bus_name == bus_name:
                del self.players[key]

    def _emit_list(self) -> None:
        for cb in list(self.on_list):
            cb()

    def _player_changed(self, player: LocalPlayer, props: dict) -> None:
        for cb in list(self.on_change):
            cb(player, props)

    def _seeked(self, player: LocalPlayer, pos: int) -> None:
        for cb in list(self.on_seeked):
            cb(player, pos)


_INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""
_NODE = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION)

_TYPES = {
    "PlaybackStatus": "s",
    "LoopStatus": "s",
    "Rate": "d",
    "Shuffle": "b",
    "Metadata": "a{sv}",
    "Volume": "d",
    "Position": "x",
    "MinimumRate": "d",
    "MaximumRate": "d",
    "CanGoNext": "b",
    "CanGoPrevious": "b",
    "CanPlay": "b",
    "CanPause": "b",
    "CanSeek": "b",
    "CanControl": "b",
}


class ExportedPlayer:
    """A remote player published as org.mpris.MediaPlayer2.caelestiaconnect.<id> on its own connection.

    `controller` receives: action(name), seek(offset_us), set_position(pos_us), set_volume(0..1),
    set_loop(status), set_shuffle(bool), position_us() -> int.
    """

    _counter = 0

    def __init__(self, identity: str, controller: Any) -> None:
        ExportedPlayer._counter += 1
        self.identity = identity
        self.controller = controller
        self.bus_name = OWN_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", identity)[:40] + f"_{ExportedPlayer._counter}"
        self.conn: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._owner_id = 0
        self.props: dict[str, Any] = {
            "PlaybackStatus": "Stopped",
            "LoopStatus": "None",
            "Rate": 1.0,
            "Shuffle": False,
            "Metadata": {},
            "Volume": 1.0,
            "MinimumRate": 1.0,
            "MaximumRate": 1.0,
            "CanGoNext": True,
            "CanGoPrevious": True,
            "CanPlay": True,
            "CanPause": True,
            "CanSeek": False,
            "CanControl": True,
        }
        self.track_id = "/org/caelestia/connect/track/0"

    async def start(self) -> None:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.conn = await Gio.DBusConnection.new_for_address(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
        )
        for iface in _NODE.interfaces:
            self._reg_ids.append(
                self.conn.register_object(OBJECT_PATH, iface, self._method_call, self._get_property, self._set_property)
            )
        self._owner_id = Gio.bus_own_name_on_connection(
            self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None
        )

    def stop(self) -> None:
        if self.conn is None:
            return
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
        for rid in self._reg_ids:
            self.conn.unregister_object(rid)
        conn, self.conn = self.conn, None
        conn.close()

    def update(self, **props: Any) -> None:
        changed: dict[str, GLib.Variant] = {}
        for key, value in props.items():
            if key == "Metadata":
                value = self._metadata(value)
            if self.props.get(key) != value:
                self.props[key] = value
                changed[key] = self._variant(key, value)
        if changed and self.conn is not None:
            try:
                self.conn.emit_signal(
                    None,
                    OBJECT_PATH,
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (PLAYER_IFACE, changed, [])),
                )
            except GLib.Error:
                pass

    def seeked(self, pos_us: int) -> None:
        if self.conn is not None:
            try:
                self.conn.emit_signal(None, OBJECT_PATH, PLAYER_IFACE, "Seeked", GLib.Variant("(x)", (int(pos_us),)))
            except GLib.Error:
                pass

    def _metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"mpris:trackid": self.track_id}
        out.update({k: v for k, v in meta.items() if v not in (None, "", [])})
        return out

    @staticmethod
    def _meta_variant(meta: dict[str, Any]) -> GLib.Variant:
        typed: dict[str, GLib.Variant] = {}
        for k, v in meta.items():
            if k == "mpris:trackid":
                typed[k] = GLib.Variant("o", v)
            elif k == "mpris:length":
                typed[k] = GLib.Variant("x", int(v))
            elif k == "xesam:artist":
                typed[k] = GLib.Variant("as", v if isinstance(v, list) else [str(v)])
            else:
                typed[k] = GLib.Variant("s", str(v))
        return GLib.Variant("a{sv}", typed)

    def _variant(self, key: str, value: Any) -> GLib.Variant:
        if key == "Metadata":
            return self._meta_variant(value)
        t = _TYPES[key]
        if t == "d":
            value = float(value)
        elif t == "x":
            value = int(value)
        elif t == "b":
            value = bool(value)
        return GLib.Variant(t, value)

    def _get_property(self, _conn, _sender, _path, iface: str, prop: str):
        if iface == ROOT_IFACE:
            root = {
                "CanQuit": GLib.Variant("b", False),
                "CanRaise": GLib.Variant("b", False),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", self.identity),
                "DesktopEntry": GLib.Variant("s", "org.caelestia.Connect"),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
            return root.get(prop)
        if prop == "Position":
            try:
                return GLib.Variant("x", int(self.controller.position_us()))
            except Exception:
                return GLib.Variant("x", 0)
        if prop == "Metadata":
            return self._meta_variant(self.props["Metadata"] or self._metadata({}))
        if prop in self.props:
            return self._variant(prop, self.props[prop])
        return None

    def _set_property(self, _conn, _sender, _path, iface: str, prop: str, value: GLib.Variant) -> bool:
        v = value.unpack()
        try:
            if prop == "Volume":
                self.controller.set_volume(max(0.0, min(1.0, float(v))))
            elif prop == "LoopStatus":
                self.controller.set_loop(str(v))
            elif prop == "Shuffle":
                self.controller.set_shuffle(bool(v))
            else:
                return False
        except Exception:
            log.exception("MPRIS set %s failed", prop)
            return False
        return True

    def _method_call(self, _conn, _sender, _path, iface: str, method: str, params: GLib.Variant, invocation) -> None:
        try:
            if iface == PLAYER_IFACE:
                if method in ("Next", "Previous", "Pause", "PlayPause", "Stop", "Play"):
                    self.controller.action(method)
                elif method == "Seek":
                    self.controller.seek(params.unpack()[0])
                elif method == "SetPosition":
                    self.controller.set_position(params.unpack()[1])
            invocation.return_value(None)
        except Exception as e:
            log.exception("MPRIS %s failed", method)
            invocation.return_dbus_error("org.mpris.MediaPlayer2.Error", str(e))
