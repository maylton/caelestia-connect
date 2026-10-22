"""LAN backend: UDP discovery, TCP + TLS links and payload streams.

Handshake (protocol version 8), as implemented by KDE Connect:
  1. A device broadcasts an unencrypted identity packet over UDP (port 1716) with its TCP port.
  2. Whoever receives it connects over TCP and sends its own identity (with targetDeviceId).
  3. TLS is started with inverted roles: the side that opened the TCP connection acts as the TLS
     *server*, the side that accepted it acts as the TLS *client*. Certificates are self-signed and
     verified against the stored certificate once the device is paired.
  4. Both ends send their full identity again, now encrypted, and the link is ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gi.repository import Gio, GLib

from . import config, const, crypto
from .packet import Packet
from .utils import run, spawn

if TYPE_CHECKING:
    from .daemon import Daemon
    from .transfers import Transfer

log = logging.getLogger(__name__)

PRIO = GLib.PRIORITY_DEFAULT
CHUNK = 64 * 1024


def _normalize_address(addr: str) -> str:
    if addr.startswith("::ffff:") and "." in addr:
        return addr[7:]
    return addr


def _peer_address(conn: Gio.SocketConnection) -> str:
    try:
        return _normalize_address(conn.get_remote_address().get_address().to_string())
    except GLib.Error:
        return ""


def _configure_socket(conn: Gio.SocketConnection) -> None:
    """Keepalive tuned like KDE Connect, so dead phones are noticed in ~60s instead of hours."""
    sock = conn.get_socket()
    try:
        sock.set_keepalive(True)
        sock.set_option(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.set_option(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.set_option(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        sock.set_option(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18), 60000)
        sock.set_option(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (GLib.Error, TypeError, AttributeError) as e:
        log.debug("Could not configure socket: %s", e)


def _gio_cert_der(gcert: Gio.TlsCertificate) -> bytes:
    data = gcert.props.certificate
    return bytes(data) if data is not None else b""


async def _read_line(stream: Gio.DataInputStream, limit: int) -> bytes | None:
    line, _length = await stream.read_line_async(PRIO)
    if line is None:
        return None
    if len(line) > limit:
        raise ValueError("line too long")
    return bytes(line)


class TlsVerifier:
    """accept-certificate handler: anything while unpaired, only the stored cert once paired."""

    def __init__(self, expected_der: bytes | None) -> None:
        self.expected_der = expected_der

    def __call__(self, _conn, peer_cert: Gio.TlsCertificate, _errors) -> bool:
        if self.expected_der is None:
            return True
        ok = _gio_cert_der(peer_cert) == self.expected_der
        if not ok:
            log.warning("Rejecting peer certificate that does not match the paired one")
        return ok


def _tls_server(base: Gio.IOStream, cert: Gio.TlsCertificate, expected_der: bytes | None) -> Gio.TlsServerConnection:
    tls = Gio.TlsServerConnection.new(base, cert)
    tls.props.authentication_mode = (
        Gio.TlsAuthenticationMode.REQUIRED if expected_der else Gio.TlsAuthenticationMode.REQUESTED
    )
    tls.props.require_close_notify = False
    tls.connect("accept-certificate", TlsVerifier(expected_der))
    return tls


def _tls_client(base: Gio.IOStream, cert: Gio.TlsCertificate, expected_der: bytes | None) -> Gio.TlsClientConnection:
    tls = Gio.TlsClientConnection.new(base, None)
    tls.set_certificate(cert)
    tls.props.require_close_notify = False
    tls.connect("accept-certificate", TlsVerifier(expected_der))
    return tls


class LanLink:
    """An established, encrypted connection to a remote device."""

    def __init__(
        self,
        provider: LanLinkProvider,
        tls: Gio.TlsConnection,
        base: Gio.SocketConnection,
        info: dict[str, Any],
        peer_cert,
        address: str,
        reader: Gio.DataInputStream,
    ) -> None:
        self.provider = provider
        self.tls = tls
        self.base = base
        self.info = info
        self.device_id: str = info["deviceId"]
        self.peer_cert = peer_cert
        self.address = address
        self.on_packet: Callable[[Packet], None] | None = None
        self.on_closed: Callable[[LanLink], None] | None = None
        self._write_lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._closed = False
        # Reuse the reader that consumed the encrypted identity: it may already hold buffered packets
        self._reader = reader
        self._task = spawn(self._read_loop(), name=f"link:{self.device_id}")

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await _read_line(self._reader, const.MAX_PACKET_SIZE)
                except ValueError:
                    log.warning("Discarding oversized packet from %s", self.device_id)
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    pkt = Packet.parse(line)
                except (ValueError, json.JSONDecodeError) as e:
                    log.warning("Bad packet from %s: %s", self.device_id, e)
                    continue
                log.debug("<- %s %s", self.device_id, pkt)
                if self.on_packet:
                    try:
                        self.on_packet(pkt)
                    except Exception:
                        log.exception("Error handling packet %s", pkt.type)
        except GLib.Error as e:
            log.info("Link to %s closed: %s", self.device_id, e.message)
        except asyncio.CancelledError:
            pass
        finally:
            self.close()

    async def _write(self, data: bytes) -> bool:
        if self._closed:
            return False
        async with self._write_lock:
            try:
                await self.tls.get_output_stream().write_all_async(data, PRIO)
                return True
            except GLib.Error as e:
                log.warning("Write to %s failed: %s", self.device_id, e.message)
                self.close()
                return False

    async def send(self, pkt: Packet, transfer: Transfer | None = None) -> bool:
        """Send a packet. Packets with a payload are queued and streamed one after another."""
        if pkt.payload is not None:
            async with self._upload_lock:
                return await self._upload(pkt, transfer)
        log.debug("-> %s %s", self.device_id, pkt)
        return await self._write(pkt.serialize())

    async def _upload(self, pkt: Packet, transfer: Transfer | None) -> bool:
        listener = Gio.SocketListener()
        port = 0
        for candidate in range(const.MIN_PAYLOAD_PORT, const.MAX_PAYLOAD_PORT + 1):
            try:
                listener.add_inet_port(candidate, None)
                port = candidate
                break
            except GLib.Error:
                continue
        if not port:
            if transfer:
                transfer.finish(False, "no free port")
            return False
        try:
            pkt.transfer_info = {"port": port}
            log.debug("-> %s %s (payload on port %d)", self.device_id, pkt, port)
            if not await self._write(pkt.serialize()):
                if transfer:
                    transfer.finish(False, "disconnected")
                return False
            try:
                conn, _src = await asyncio.wait_for(listener.accept_async(), const.PAYLOAD_ACCEPT_TIMEOUT)
            except TimeoutError:
                if transfer:
                    transfer.finish(False, "timeout")
                return False
            _configure_socket(conn)
            tls = _tls_server(conn, self.provider.tls_cert, crypto.cert_der(self.peer_cert))
            try:
                await asyncio.wait_for(tls.handshake_async(PRIO), const.IDENTITY_TIMEOUT)
                out = tls.get_output_stream()
                sent = 0
                with pkt.payload.open() as f:
                    while True:
                        if transfer and transfer.cancel_requested:
                            raise asyncio.CancelledError
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        await out.write_all_async(chunk, PRIO)
                        sent += len(chunk)
                        if transfer:
                            transfer.progress(sent)
                try:
                    await tls.close_async(PRIO)
                except GLib.Error:
                    pass
                if transfer:
                    transfer.finish(True)
                return True
            except (TimeoutError, GLib.Error, OSError, asyncio.CancelledError) as e:
                msg = getattr(e, "message", "") or type(e).__name__
                log.warning("Upload to %s failed: %s", self.device_id, msg)
                try:
                    conn.close()
                except GLib.Error:
                    pass
                if transfer:
                    transfer.finish(False, msg)
                return False
        finally:
            listener.close()

    async def download(self, pkt: Packet, dest: Path, transfer: Transfer | None = None) -> bool:
        """Fetch the payload announced by `pkt` and store it at `dest` (atomically)."""
        try:
            port = int(pkt.transfer_info.get("port"))
        except (TypeError, ValueError):
            if transfer:
                transfer.finish(False, "missing port")
            return False
        size = pkt.payload_size
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        client = Gio.SocketClient()
        client.props.timeout = 10
        conn = None
        try:
            addr = Gio.InetSocketAddress.new_from_string(self.address, port)
            conn = await client.connect_async(addr)
            conn.get_socket().set_timeout(60)
            _configure_socket(conn)
            tls = _tls_client(conn, self.provider.tls_cert, crypto.cert_der(self.peer_cert))
            await asyncio.wait_for(tls.handshake_async(PRIO), const.IDENTITY_TIMEOUT)
            stream = tls.get_input_stream()
            received = 0
            with open(tmp, "wb") as f:
                while size < 0 or received < size:
                    if transfer and transfer.cancel_requested:
                        raise asyncio.CancelledError
                    want = CHUNK if size < 0 else min(CHUNK, size - received)
                    data = await stream.read_bytes_async(want, PRIO)
                    chunk = data.get_data() if data else b""
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if transfer:
                        transfer.progress(received)
            if size >= 0 and received < size:
                raise OSError(f"incomplete transfer ({received}/{size})")
            os.replace(tmp, dest)
            try:
                await tls.close_async(PRIO)
            except GLib.Error:
                pass
            if transfer:
                transfer.finish(True, path=str(dest))
            return True
        except (TimeoutError, GLib.Error, OSError, asyncio.CancelledError) as e:
            msg = getattr(e, "message", "") or str(e) or type(e).__name__
            log.warning("Download from %s failed: %s", self.device_id, msg)
            try:
                tmp.unlink()
            except OSError:
                pass
            if conn is not None:
                try:
                    conn.close()
                except GLib.Error:
                    pass
            if transfer:
                transfer.finish(False, msg)
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.base.close()
        except GLib.Error:
            pass
        if self._task and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()
        if self.on_closed:
            self.on_closed(self)


class LanLinkProvider:
    def __init__(self, daemon: Daemon, discovery: bool = True) -> None:
        self.daemon = daemon
        self.discovery = discovery
        self.tls_cert = daemon.identity.tls_certificate()
        self.tcp_port = 0
        self.listener: Gio.SocketListener | None = None
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.links: dict[str, LanLink] = {}
        self._last_by_ip: dict[str, float] = {}
        self._last_by_id: dict[str, float] = {}
        self._accept_task: asyncio.Task | None = None
        self._monitor_id = 0
        self._last_network_change = 0.0

    async def start(self) -> None:
        self.listener = Gio.SocketListener()
        for port in range(const.MIN_TCP_PORT, const.MAX_TCP_PORT + 1):
            try:
                self.listener.add_inet_port(port, None)
                self.tcp_port = port
                break
            except GLib.Error:
                continue
        if not self.tcp_port:
            log.error("No free TCP port in %d-%d", const.MIN_TCP_PORT, const.MAX_TCP_PORT)
        else:
            log.info("Listening on TCP port %d", self.tcp_port)
            self._accept_task = spawn(self._accept_loop(), name="lan:accept")

        if not self.discovery:
            log.info("Discovery disabled: only direct connections are accepted")
            return
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", const.UDP_PORT))
            self.udp_transport, _ = await loop.create_datagram_endpoint(lambda: _UdpProtocol(self), sock=sock)
            log.info("Listening for UDP discovery on port %d", const.UDP_PORT)
        except OSError as e:
            log.error("Could not bind UDP port %d: %s", const.UDP_PORT, e)
            sock.close()

        monitor = Gio.NetworkMonitor.get_default()
        self._monitor_id = monitor.connect("network-changed", self._network_changed)
        await self.broadcast()

    def stop(self) -> None:
        if self._monitor_id:
            Gio.NetworkMonitor.get_default().disconnect(self._monitor_id)
            self._monitor_id = 0
        if self._accept_task:
            self._accept_task.cancel()
        if self.listener:
            self.listener.close()
        if self.udp_transport:
            self.udp_transport.close()
        for link in list(self.links.values()):
            link.close()

    def _network_changed(self, _monitor, available: bool) -> None:
        now = time.monotonic()
        if available and now - self._last_network_change > 1:
            self._last_network_change = now
            # Give DHCP a moment before announcing ourselves on the new network
            asyncio.get_event_loop().call_later(1.5, lambda: spawn(self.broadcast()))

    def _identity_body(self, full: bool) -> dict[str, Any]:
        d = self.daemon
        body: dict[str, Any] = {
            "deviceId": d.identity.device_id,
            "deviceName": d.settings.name,
            "deviceType": d.device_type,
            "protocolVersion": const.PROTOCOL_VERSION,
        }
        if full:
            body["incomingCapabilities"] = d.plugins_registry.incoming_capabilities()
            body["outgoingCapabilities"] = d.plugins_registry.outgoing_capabilities()
        return body

    async def _local_ipv4(self) -> list[tuple[str, str]]:
        """(address, broadcast) for every interface that is up."""
        res = await run("ip", "-j", "-4", "addr", "show", "up", timeout=3)
        out: list[tuple[str, str]] = []
        if res.ok:
            try:
                for iface in json.loads(res.stdout):
                    if "LOOPBACK" in iface.get("flags", []):
                        continue
                    out.extend(
                        (a["local"], a.get("broadcast", ""))
                        for a in iface.get("addr_info", [])
                        if a.get("family") == "inet" and a.get("local")
                    )
            except ValueError:
                pass
        return out

    async def broadcast(self, extra: list[str] | None = None) -> None:
        if not self.tcp_port or not self.discovery:
            return
        body = self._identity_body(full=False)
        body["tcpPort"] = self.tcp_port
        data = Packet(const.PACKET_TYPE_IDENTITY, body).serialize()
        targets = {"255.255.255.255"}
        targets.update(self.daemon.settings.get("customDevices") or [])
        targets.update(extra or [])
        ifaces = await self._local_ipv4()
        for _addr, bcast in ifaces:
            if bcast:
                targets.add(bcast)
        sources = [a for a, _ in ifaces] or ["0.0.0.0"]
        for src in sources:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((src, 0))
                for t in targets:
                    try:
                        s.sendto(data, (t, const.UDP_PORT))
                    except OSError as e:
                        log.debug("UDP send %s -> %s failed: %s", src, t, e)
            except OSError as e:
                log.debug("Could not broadcast from %s: %s", src, e)
            finally:
                s.close()
        log.debug("Broadcasted identity to %s", ", ".join(sorted(targets)))

    def _rate_limited(self, table: dict[str, float], key: str) -> bool:
        now = time.monotonic()
        if len(table) > 1024:
            for k in [k for k, t in table.items() if now - t > const.RATE_LIMIT_SECONDS]:
                del table[k]
        last = table.get(key)
        if last is not None and now - last < const.RATE_LIMIT_SECONDS:
            return True
        table[key] = now
        return False

    def udp_received(self, data: bytes, addr: tuple) -> None:
        host = _normalize_address(addr[0])
        if host.startswith("127."):
            return
        if self._rate_limited(self._last_by_ip, host):
            return
        try:
            pkt = Packet.parse(data)
        except ValueError:
            return
        if pkt.type != const.PACKET_TYPE_IDENTITY:
            return
        device_id = pkt.get_str("deviceId")
        if not config.is_valid_device_id(device_id) or device_id == self.daemon.identity.device_id:
            return
        self.daemon.remember_address(device_id, host, pkt.get_int("tcpPort"))
        self.device_discovered(host, pkt.get_int("tcpPort"), device_id, pkt.get_int("protocolVersion"))

    def device_discovered(
        self, host: str, port: int, device_id: str, protocol_version: int, force: bool = False
    ) -> None:
        if protocol_version < 8:
            log.info("Ignoring %s: protocol version %d is too old", device_id, protocol_version)
            return
        if not (const.MIN_TCP_PORT <= port <= const.MAX_TCP_PORT):
            return
        if not force and self._rate_limited(self._last_by_id, device_id):
            return
        if self._is_downgrade(device_id, protocol_version):
            log.warning("Refusing protocol downgrade from %s", device_id)
            return
        spawn(self._connect(host, port, device_id, protocol_version), name=f"lan:connect:{host}")

    def _is_downgrade(self, device_id: str, protocol_version: int) -> bool:
        trusted = self.daemon.trusted.get(device_id)
        return bool(trusted) and int(trusted.get("protocolVersion") or 0) > protocol_version

    async def _connect(self, host: str, port: int, device_id: str, protocol_version: int) -> None:
        client = Gio.SocketClient()
        client.props.timeout = 10
        try:
            conn = await client.connect_async(Gio.InetSocketAddress.new_from_string(host, port))
        except (GLib.Error, TypeError) as e:
            log.info("Could not connect to %s:%d (%s), asking for a reverse connection", host, port, e)
            await self.broadcast(extra=[host])
            return
        # The connect timeout would otherwise apply to every later read and kill idle links
        conn.get_socket().set_timeout(0)
        _configure_socket(conn)
        body = self._identity_body(full=False)
        body["targetDeviceId"] = device_id
        body["targetProtocolVersion"] = protocol_version
        try:
            await conn.get_output_stream().write_all_async(Packet(const.PACKET_TYPE_IDENTITY, body).serialize(), PRIO)
        except GLib.Error as e:
            log.info("Could not send identity to %s: %s", host, e.message)
            conn.close()
            return
        await self._secure(conn, device_id, protocol_version, as_server=True, port=port)

    async def _accept_loop(self) -> None:
        while True:
            try:
                conn, _src = await self.listener.accept_async()
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED):
                    return
                log.warning("Accept failed: %s", e.message)
                await asyncio.sleep(0.5)
                continue
            host = _peer_address(conn)
            if self._rate_limited(self._last_by_ip, f"tcp:{host}"):
                conn.close()
                continue
            _configure_socket(conn)
            spawn(self._incoming(conn), name=f"lan:incoming:{host}")

    async def _incoming(self, conn: Gio.SocketConnection) -> None:
        reader = Gio.DataInputStream.new(conn.get_input_stream())
        reader.props.close_base_stream = False
        # Read byte-by-byte buffering would be wasteful; the peer stays silent after its identity
        # until we start the TLS handshake, so nothing gets swallowed by the buffer.
        try:
            line = await asyncio.wait_for(_read_line(reader, const.MAX_IDENTITY_PACKET_SIZE), const.IDENTITY_TIMEOUT)
            if not line:
                raise ValueError("no identity")
            pkt = Packet.parse(line)
        except (TimeoutError, ValueError, GLib.Error) as e:
            log.info("Invalid incoming connection from %s: %s", _peer_address(conn), e)
            conn.close()
            return
        own_id = self.daemon.identity.device_id
        device_id = pkt.get_str("deviceId")
        target = pkt.get_str("targetDeviceId")
        target_pv = pkt.get("targetProtocolVersion")
        if pkt.type != const.PACKET_TYPE_IDENTITY or not config.is_valid_device_id(device_id) or device_id == own_id:
            conn.close()
            return
        if target and target != own_id:
            log.info("Connection meant for another device (%s)", target)
            conn.close()
            return
        if target_pv is not None:
            try:
                if int(target_pv) != const.PROTOCOL_VERSION:
                    conn.close()
                    return
            except (TypeError, ValueError):
                pass
        protocol_version = pkt.get_int("protocolVersion")
        if protocol_version < 8 or self._is_downgrade(device_id, protocol_version):
            conn.close()
            return
        await self._secure(conn, device_id, protocol_version, as_server=False)

    async def _secure(
        self, conn: Gio.SocketConnection, device_id: str, protocol_version: int, as_server: bool, port: int = 0
    ) -> None:
        host = _peer_address(conn)
        trusted = self.daemon.trusted.get(device_id)
        expected_der = None
        if trusted:
            try:
                expected_der = crypto.cert_der(crypto.cert_from_pem(trusted["certificate"]))
            except (KeyError, ValueError):
                log.warning("Stored certificate for %s is invalid", device_id)
        if (
            not trusted
            and sum(1 for link in self.links.values() if link.device_id not in self.daemon.trusted)
            >= const.MAX_UNPAIRED_CONNECTIONS
        ):
            conn.close()
            return
        tls = (_tls_server if as_server else _tls_client)(conn, self.tls_cert, expected_der)
        try:
            await asyncio.wait_for(tls.handshake_async(PRIO), const.IDENTITY_TIMEOUT)
        except (TimeoutError, GLib.Error) as e:
            log.info("TLS handshake with %s (%s) failed: %s", device_id, host, getattr(e, "message", e))
            conn.close()
            return

        try:
            await tls.get_output_stream().write_all_async(
                Packet(const.PACKET_TYPE_IDENTITY, self._identity_body(full=True)).serialize(), PRIO
            )
            reader = Gio.DataInputStream.new(tls.get_input_stream())
            reader.props.close_base_stream = False
            reader.set_buffer_size(CHUNK)
            line = await asyncio.wait_for(_read_line(reader, const.MAX_IDENTITY_PACKET_SIZE), const.IDENTITY_TIMEOUT)
            if not line:
                raise ValueError("no encrypted identity")
            ident = Packet.parse(line)
        except (TimeoutError, GLib.Error, ValueError) as e:
            log.info("Encrypted identity exchange with %s failed: %s", device_id, getattr(e, "message", e))
            conn.close()
            return

        name = config.filter_device_name(ident.get_str("deviceName"))
        if (
            ident.type != const.PACKET_TYPE_IDENTITY
            or not name
            or ident.get_str("deviceId") != device_id
            or ident.get_int("protocolVersion") != protocol_version
        ):
            log.warning("Device %s does not implement protocol version 8 correctly", device_id)
            conn.close()
            return

        peer_gcert = tls.get_peer_certificate()
        if peer_gcert is None:
            log.warning("Device %s did not present a certificate", device_id)
            conn.close()
            return
        peer_cert = crypto.cert_from_der(_gio_cert_der(peer_gcert))
        if crypto.cert_common_name(peer_cert) != device_id:
            log.warning("Certificate CN does not match device id %s", device_id)
            conn.close()
            return

        old = self.links.get(device_id)
        if old is not None and not old.closed and not crypto.same_certificate(old.peer_cert, peer_cert):
            log.warning("Refusing to replace link to %s with a different certificate", device_id)
            conn.close()
            return

        info = {
            "deviceId": device_id,
            "deviceName": name,
            "deviceType": ident.get_str("deviceType", "unknown"),
            "protocolVersion": protocol_version,
            "incomingCapabilities": [c for c in ident.get_list("incomingCapabilities") if isinstance(c, str)],
            "outgoingCapabilities": [c for c in ident.get_list("outgoingCapabilities") if isinstance(c, str)],
        }
        link = LanLink(self, tls, conn, info, peer_cert, host, reader)
        link.on_closed = self._link_closed
        self.links[device_id] = link
        log.info("Connected to %s (%s) at %s", name, device_id, host)
        self.daemon.remember_address(device_id, host, port)
        self.daemon.link_established(link, replaced=old)
        if old is not None and not old.closed:
            old.on_closed = None
            old.close()

    def _link_closed(self, link: LanLink) -> None:
        if self.links.get(link.device_id) is link:
            del self.links[link.device_id]
        self.daemon.link_lost(link)


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, provider: LanLinkProvider) -> None:
        self.provider = provider

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) > const.MAX_IDENTITY_PACKET_SIZE:
            return
        self.provider.udp_received(data, addr)

    def error_received(self, exc) -> None:
        log.debug("UDP error: %s", exc)
