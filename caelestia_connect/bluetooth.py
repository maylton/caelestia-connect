"""Bluetooth (RFCOMM) links, compatible with the KDE Connect Bluetooth backend.

BlueZ owns the sockets: we register a profile for the KDE Connect service UUID, BlueZ publishes the SDP
record, accepts connections and opens outgoing ones (ConnectProfile), and hands us the socket in
Profile1.NewConnection. A single connection carries everything, split into flow-controlled channels
(see "Multiplexing protocol.md" in KDE Connect). There is no TLS: identities carry the certificate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gi.repository import Gio, GLib

from . import const, crypto
from .lan import link_info
from .packet import Packet
from .utils import spawn

if TYPE_CHECKING:
    from .daemon import Daemon
    from .transfers import Transfer

log = logging.getLogger(__name__)

SERVICE_UUID = "185f3df4-3268-4e3f-9fca-d4d5059915bd"
DEFAULT_CHANNEL = uuid.UUID("a0d0aaf4-1072-4d81-aa35-902a954b1266")
PROFILE_PATH = "/org/caelestia/Connect/bluetooth"
BUFFER_SIZE = 4096
CONNECT_INTERVAL = 30
# The Android app closes connections it accepted right after the handshake: a peer that does this only
# keeps the links it opens itself (when its app starts or its network changes), so we stop dialing it
SHORT_LINK = 10
UNCLAIMED_PAYLOAD_TIMEOUT = 30

MSG_VERSION, MSG_OPEN, MSG_CLOSE, MSG_READ, MSG_WRITE = range(5)

PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg type="o" direction="in"/><arg type="h" direction="in"/><arg type="a{sv}" direction="in"/>
    </method>
    <method name="RequestDisconnection"><arg type="o" direction="in"/></method>
  </interface>
</node>
"""


def _message(kind: int, channel: uuid.UUID | None, data: bytes = b"") -> bytes:
    return struct.pack("!BH", kind, len(data)) + (channel.bytes if channel else bytes(16)) + data


class Channel:
    def __init__(self, mux: Multiplexer, channel_id: uuid.UUID) -> None:
        self.mux = mux
        self.id = channel_id
        self.buffer = bytearray()
        self.requested = 0
        self.free_write = 0
        self.open = True
        self._readable = asyncio.Event()
        self._writable = asyncio.Event()

    def _request_more(self, past_buffer: bool = False) -> None:
        # The peer never has more than BUFFER_SIZE bytes of outstanding requests (Android relies on it)
        amount = BUFFER_SIZE - self.requested - (0 if past_buffer else len(self.buffer))
        if self.open and amount > 0:
            self.requested += amount
            self.mux.send(_message(MSG_READ, self.id, struct.pack("!H", amount)))

    def _received(self, data: bytes) -> None:
        self.requested -= len(data)
        self.buffer += data
        self._readable.set()

    def _granted(self, amount: int) -> None:
        self.free_write += amount
        self._writable.set()

    def _closed(self) -> None:
        self.open = False
        self._readable.set()
        self._writable.set()

    async def read(self, size: int) -> bytes:
        """Up to `size` bytes; b"" once the channel is closed and drained."""
        while not self.buffer and self.open:
            self._readable.clear()
            await self._readable.wait()
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        self._request_more()
        return data

    async def readline(self, limit: int) -> bytes:
        while b"\n" not in self.buffer:
            if not self.open:
                return b""
            if len(self.buffer) > limit:
                raise ValueError("line too long")
            self._readable.clear()
            self._request_more(past_buffer=True)  # a packet can be longer than one window
            await self._readable.wait()
        end = self.buffer.index(b"\n") + 1
        line = bytes(self.buffer[:end])
        del self.buffer[:end]
        self._request_more()
        return line

    async def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            while self.open and self.free_write == 0:
                self._writable.clear()
                await self._writable.wait()
            if not self.open:
                raise ConnectionError("channel closed")
            n = min(self.free_write, len(view), BUFFER_SIZE)
            self.free_write -= n
            self.mux.send(_message(MSG_WRITE, self.id, bytes(view[:n])))
            view = view[n:]
            await self.mux.drain()

    def close(self) -> None:
        if self.open:
            self.mux.send(_message(MSG_CLOSE, self.id))
            self.mux.channels.pop(self.id, None)
            self._closed()


class Multiplexer:
    def __init__(self, sock: socket.socket, on_closed: Callable[[], None]) -> None:
        self.sock = sock
        self.on_closed = on_closed
        self.channels: dict[uuid.UUID, Channel] = {}
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.closed = False
        self._task: asyncio.Task | None = None

    async def start(self) -> Channel:
        self.reader, self.writer = await asyncio.open_connection(sock=self.sock)
        self.send(_message(MSG_VERSION, None, struct.pack("!HH", 1, 1)))
        default = self._add(DEFAULT_CHANNEL)
        self._task = spawn(self._read_loop(), name="bluetooth:mux")
        return default

    def send(self, data: bytes) -> None:
        if self.writer and not self.closed:
            self.writer.write(data)

    async def drain(self) -> None:
        if self.writer and not self.closed:
            try:
                await self.writer.drain()
            except OSError:
                self.close()

    def new_channel(self) -> Channel:
        channel_id = uuid.uuid4()
        self.send(_message(MSG_OPEN, channel_id))
        return self._add(channel_id)

    def _add(self, channel_id: uuid.UUID) -> Channel:
        channel = self.channels[channel_id] = Channel(self, channel_id)
        channel._request_more()
        return channel

    async def _read_loop(self) -> None:
        got_version = False
        try:
            while True:
                header = await self.reader.readexactly(19)
                kind, length = struct.unpack_from("!BH", header)
                channel_id = uuid.UUID(bytes=header[3:19])
                data = await self.reader.readexactly(length) if length else b""
                if kind == MSG_VERSION:
                    lowest, highest = struct.unpack_from("!HH", data)
                    if lowest > 1 or highest < 1:
                        raise ConnectionError(f"unsupported multiplexing version {lowest}-{highest}")
                    got_version = True
                    continue
                if not got_version:
                    raise ConnectionError("no protocol version message")
                channel = self.channels.get(channel_id)
                if kind == MSG_OPEN:
                    if channel is None:
                        self._add(channel_id)
                elif channel is None:
                    continue
                elif kind == MSG_READ:
                    channel._granted(struct.unpack("!H", data)[0])
                elif kind == MSG_WRITE:
                    channel._received(data)
                elif kind == MSG_CLOSE:
                    self.channels.pop(channel_id, None)
                    channel._closed()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, struct.error) as e:
            log.info("Bluetooth connection closed: %s", e or type(e).__name__)
        except asyncio.CancelledError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for channel in list(self.channels.values()):
            channel._closed()
        self.channels.clear()
        if self.writer:
            self.writer.close()
        else:
            self.sock.close()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
        self.on_closed()


class BluetoothLink:
    """A KDE Connect link over a multiplexed RFCOMM connection."""

    priority = 10

    def __init__(self, mux: Multiplexer, channel: Channel, info: dict[str, Any], peer_cert, address: str) -> None:
        self.mux = mux
        self.channel = channel
        self.info = info
        self.device_id: str = info["deviceId"]
        self.peer_cert = peer_cert
        self.address = address
        self.on_packet: Callable[[Packet], None] | None = None
        self.on_closed: Callable[[BluetoothLink], None] | None = None
        self._payloads: dict[str, Channel] = {}
        self._write_lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._closed = False
        self.dialed_at = 0.0  # set when we opened the connection
        self._task = spawn(self._read_loop(), name=f"bluetooth:{self.device_id}")

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self.channel.readline(const.MAX_PACKET_SIZE)
                except ValueError:
                    log.warning("Discarding oversized packet from %s", self.device_id)
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    pkt = Packet.parse(line)
                except (ValueError, json.JSONDecodeError) as e:
                    log.warning("Bad packet from %s: %s", self.device_id, e)
                    continue
                log.debug("<- %s %s (bluetooth)", self.device_id, pkt)
                self._claimable(pkt)
                if self.on_packet:
                    try:
                        self.on_packet(pkt)
                    except Exception:
                        log.exception("Error handling packet %s", pkt.type)
        except asyncio.CancelledError:
            pass
        finally:
            self.close()

    def _claimable(self, pkt: Packet) -> None:
        # The sender waits for us to read the payload: drop it if no plugin asks for it
        key = str((pkt.transfer_info or {}).get("uuid", ""))
        try:
            channel = self.mux.channels.get(uuid.UUID(key))
        except ValueError:
            return
        if channel is None:
            return
        self._payloads[key] = channel
        asyncio.get_running_loop().call_later(UNCLAIMED_PAYLOAD_TIMEOUT, self._drop_payload, key)

    def _drop_payload(self, key: str) -> None:
        channel = self._payloads.pop(key, None)
        if channel:
            channel.close()

    async def _write(self, data: bytes) -> bool:
        if self._closed:
            return False
        async with self._write_lock:
            try:
                await self.channel.write(data)
                return True
            except ConnectionError as e:
                log.warning("Write to %s failed: %s", self.device_id, e)
                self.close()
                return False

    async def send(self, pkt: Packet, transfer: Transfer | None = None) -> bool:
        if pkt.payload is None:
            log.debug("-> %s %s (bluetooth)", self.device_id, pkt)
            return await self._write(pkt.serialize())
        async with self._upload_lock:
            channel = self.mux.new_channel()
            pkt.transfer_info = {"uuid": str(channel.id)}
            if not await self._write(pkt.serialize()):
                channel.close()
                if transfer:
                    transfer.finish(False, "disconnected")
                return False
            sent = 0
            try:
                with pkt.payload.open() as f:
                    while chunk := f.read(BUFFER_SIZE * 4):
                        if transfer and transfer.cancel_requested:
                            raise asyncio.CancelledError
                        await channel.write(chunk)
                        sent += len(chunk)
                        if transfer:
                            transfer.progress(sent)
            except (ConnectionError, OSError, asyncio.CancelledError) as e:
                log.warning("Upload to %s failed: %s", self.device_id, e or type(e).__name__)
                if transfer:
                    transfer.finish(False, str(e) or type(e).__name__)
                return False
            finally:
                channel.close()
            if transfer:
                transfer.finish(True)
            return True

    async def download(self, pkt: Packet, dest: Path, transfer: Transfer | None = None) -> bool:
        channel = self._payloads.pop(str((pkt.transfer_info or {}).get("uuid", "")), None)
        if channel is None:
            if transfer:
                transfer.finish(False, "payload no longer available")
            return False
        size = pkt.payload_size
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        received = 0
        try:
            with open(tmp, "wb") as f:
                while size < 0 or received < size:
                    if transfer and transfer.cancel_requested:
                        raise asyncio.CancelledError
                    chunk = await channel.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if transfer:
                        transfer.progress(received)
            if size >= 0 and received < size:
                raise OSError(f"incomplete transfer ({received}/{size})")
            os.replace(tmp, dest)
        except (OSError, asyncio.CancelledError) as e:
            log.warning("Download from %s failed: %s", self.device_id, e or type(e).__name__)
            tmp.unlink(missing_ok=True)
            if transfer:
                transfer.finish(False, str(e) or type(e).__name__)
            return False
        finally:
            channel.close()
        if transfer:
            transfer.finish(True, path=str(dest))
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.mux.close()
        if self._task and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()
        if self.on_closed:
            self.on_closed(self)


class BluetoothLinkProvider:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.bus: Gio.DBusConnection | None = None
        self.links: dict[str, BluetoothLink] = {}
        self._addresses: set[str] = set()
        self._connecting: set[str] = set()
        self._passive: set[str] = set()
        self._registration = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            self.bus = await Gio.bus_get(Gio.BusType.SYSTEM)
            node = Gio.DBusNodeInfo.new_for_xml(PROFILE_XML)
            self._registration = self.bus.register_object(PROFILE_PATH, node.interfaces[0], self._profile_call)
            options = {
                "Name": GLib.Variant("s", "KDE Connect"),
                "RequireAuthentication": GLib.Variant("b", True),
                "RequireAuthorization": GLib.Variant("b", False),
                "AutoConnect": GLib.Variant("b", False),
            }
            await self.bus.call(
                "org.bluez",
                "/org/bluez",
                "org.bluez.ProfileManager1",
                "RegisterProfile",
                GLib.Variant("(osa{sv})", (PROFILE_PATH, SERVICE_UUID, options)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
            )
        except GLib.Error as e:
            log.info("Bluetooth unavailable: %s", e.message)
            self._unregister()
            return
        log.info("Bluetooth service registered")
        self._task = spawn(self._connect_loop(), name="bluetooth:connect")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for link in list(self.links.values()):
            link.close()
        if self.bus and self._registration:
            self.bus.call(
                "org.bluez",
                "/org/bluez",
                "org.bluez.ProfileManager1",
                "UnregisterProfile",
                GLib.Variant("(o)", (PROFILE_PATH,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
            )
        self._unregister()

    def _unregister(self) -> None:
        if self.bus and self._registration:
            self.bus.unregister_object(self._registration)
        self._registration = 0

    async def _connect_loop(self) -> None:
        """Connect to bonded devices that offer the KDE Connect service and are not linked yet."""
        while True:
            try:
                res = await self.bus.call(
                    "org.bluez",
                    "/",
                    "org.freedesktop.DBus.ObjectManager",
                    "GetManagedObjects",
                    None,
                    GLib.VariantType("(a{oa{sa{sv}}})"),
                    Gio.DBusCallFlags.NONE,
                    5000,
                )
                for path, ifaces in res.unpack()[0].items():
                    dev = ifaces.get("org.bluez.Device1")
                    if not dev or not dev.get("Paired") or SERVICE_UUID not in dev.get("UUIDs", []):
                        continue
                    address = dev.get("Address")
                    if address in self._addresses or address in self._passive or path in self._connecting:
                        continue
                    spawn(self._connect(path), name="bluetooth:connect-device")
            except GLib.Error as e:
                log.debug("Listing Bluetooth devices failed: %s", e.message)
            await asyncio.sleep(CONNECT_INTERVAL)

    async def _connect(self, path: str) -> None:
        self._connecting.add(path)
        try:
            await self.bus.call(
                "org.bluez",
                path,
                "org.bluez.Device1",
                "ConnectProfile",
                GLib.Variant("(s)", (SERVICE_UUID,)),
                None,
                Gio.DBusCallFlags.NONE,
                20000,
            )
        except GLib.Error as e:
            log.debug("Bluetooth connection to %s failed: %s", path, e.message)
        finally:
            # NewConnection arrives before ConnectProfile returns
            self._connecting.discard(path)

    def _profile_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        if method == "NewConnection":
            device_path, fd_index, _props = params.unpack()
            fds = invocation.get_message().get_unix_fd_list()
            fd = fds.get(fd_index) if fds else -1
            invocation.return_value(None)
            if fd >= 0:
                spawn(self._new_connection(device_path, fd), name="bluetooth:handshake")
        elif method == "RequestDisconnection":
            address = _address(params.unpack()[0])
            for link in list(self.links.values()):
                if link.address == address:
                    link.close()
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    async def _new_connection(self, device_path: str, fd: int) -> None:
        address = _address(device_path)
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        if address in self._addresses:
            sock.close()
            return
        self._addresses.add(address)
        initiator = device_path in self._connecting
        mux = Multiplexer(sock, lambda: self._addresses.discard(address))
        try:
            channel = await mux.start()
            await asyncio.sleep(0.5)  # same settling delay as the other implementations
            if not initiator:
                await channel.write(self._identity())
            line = await asyncio.wait_for(channel.readline(const.MAX_IDENTITY_PACKET_SIZE), const.IDENTITY_TIMEOUT)
            ident = Packet.parse(line)
            if initiator:
                await channel.write(self._identity())
        except (TimeoutError, ConnectionError, OSError, ValueError) as e:
            log.info("Bluetooth identity exchange with %s failed: %s", address, e or type(e).__name__)
            mux.close()
            return
        link = self._established(mux, channel, ident, address)
        if link and initiator:
            link.dialed_at = time.monotonic()

    def _identity(self) -> bytes:
        body = self.daemon.lan.identity_body(full=True)
        body["certificate"] = self.daemon.identity.cert_pem
        return Packet(const.PACKET_TYPE_IDENTITY, body).serialize()

    def _established(self, mux: Multiplexer, channel: Channel, ident: Packet, address: str) -> BluetoothLink | None:
        device_id = ident.get_str("deviceId")
        info = link_info(ident, device_id, ident.get_int("protocolVersion"))
        try:
            peer_cert = crypto.cert_from_pem(ident.get_str("certificate"))
        except ValueError:
            peer_cert = None
        if (
            info is None
            or device_id == self.daemon.identity.device_id
            or peer_cert is None
            or crypto.cert_common_name(peer_cert) != device_id
        ):
            reason = "bad identity" if info is None else "bad certificate"
            log.warning("Invalid Bluetooth identity from %s (%s)", address, reason)
            mux.close()
            return None
        trusted = self.daemon.trusted.get(device_id)
        if trusted and not crypto.same_certificate(crypto.cert_from_pem(trusted["certificate"]), peer_cert):
            log.warning("Bluetooth device %s presented a different certificate", device_id)
            mux.close()
            return None
        old = self.links.get(device_id)
        link = BluetoothLink(mux, channel, info, peer_cert, address)
        link.on_closed = self._link_closed
        self.links[device_id] = link
        log.info("Connected to %s (%s) over Bluetooth", info["deviceName"], device_id)
        self.daemon.link_established(link, replaced=old)
        if old is not None and not old.closed:
            old.on_closed = None
            old.close()
        return link

    def _link_closed(self, link: BluetoothLink) -> None:
        if link.dialed_at and time.monotonic() - link.dialed_at < SHORT_LINK:
            log.info("%s closes the connections we open, waiting for it to connect instead", link.address)
            self._passive.add(link.address)
        if self.links.get(link.device_id) is link:
            del self.links[link.device_id]
        self.daemon.link_lost(link)


def _address(device_path: str) -> str:
    return device_path.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":")
