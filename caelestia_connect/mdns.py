"""Minimal mDNS / DNS-SD responder and browser for the `_kdeconnect._udp` service.

Works without Avahi. We announce ourselves as `<device id>._kdeconnect._udp.local` with the TCP port and
a TXT record (id, name, type, protocol), answer queries for it, and browse for other devices. A resolved
device is handed to the LAN provider exactly like a UDP broadcast would be.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from .utils import spawn

log = logging.getLogger(__name__)

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
SERVICE = "_kdeconnect._udp.local"
TTL = 120

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV, TYPE_ANY = 1, 12, 16, 33, 255
CLASS_IN = 1
CACHE_FLUSH = 0x8000
UNICAST_RESPONSE = 0x8000
FLAG_RESPONSE = 0x8400  # QR + AA


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a possibly compressed name; returns (name, offset after the name)."""
    labels: list[str] = []
    end = None
    for _hop in range(64):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack_from("!H", data, offset)[0] & 0x3FFF
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        labels.append(data[offset + 1 : offset + 1 + length].decode(errors="replace"))
        offset += 1 + length
    return ".".join(labels), end if end is not None else offset


@dataclass
class Record:
    name: str
    rtype: int
    rclass: int
    ttl: int
    data: bytes
    offset: int  # rdata offset in the packet, needed to decode compressed names


@dataclass
class Message:
    flags: int
    questions: list[tuple[str, int, int]] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)

    @property
    def is_response(self) -> bool:
        return bool(self.flags & 0x8000)


def parse(data: bytes) -> Message:
    _id, flags, qd, an, ns, ar = struct.unpack_from("!6H", data, 0)
    msg = Message(flags)
    offset = 12
    for _ in range(qd):
        name, offset = decode_name(data, offset)
        qtype, qclass = struct.unpack_from("!2H", data, offset)
        offset += 4
        msg.questions.append((name.lower(), qtype, qclass))
    for _ in range(an + ns + ar):
        name, offset = decode_name(data, offset)
        rtype, rclass, ttl, length = struct.unpack_from("!2HIH", data, offset)
        offset += 10
        msg.records.append(Record(name.lower(), rtype, rclass, ttl, data[offset : offset + length], offset))
        offset += length
    return msg


def _rr(name: str, rtype: int, rdata: bytes, ttl: int = TTL, flush: bool = True) -> bytes:
    rclass = CLASS_IN | (CACHE_FLUSH if flush else 0)
    return encode_name(name) + struct.pack("!2HIH", rtype, rclass, ttl, len(rdata)) + rdata


def _txt(entries: dict[str, str]) -> bytes:
    out = b""
    for key, value in entries.items():
        raw = f"{key}={value}".encode()[:255]
        out += bytes([len(raw)]) + raw
    return out


def _parse_txt(rdata: bytes) -> dict[str, str]:
    out, i = {}, 0
    while i < len(rdata):
        length = rdata[i]
        entry = rdata[i + 1 : i + 1 + length].decode(errors="replace")
        i += 1 + length
        key, _, value = entry.partition("=")
        out[key] = value
    return out


def build(answers: list[bytes], additional: list[bytes] = (), questions: list[tuple[str, int, bool]] = ()) -> bytes:
    flags = FLAG_RESPONSE if answers else 0
    head = struct.pack("!6H", 0, flags, len(questions), len(answers), 0, len(additional))
    body = b"".join(
        encode_name(n) + struct.pack("!2H", t, CLASS_IN | (UNICAST_RESPONSE if qu else 0)) for n, t, qu in questions
    )
    return head + body + b"".join(answers) + b"".join(additional)


class MdnsService:
    def __init__(
        self,
        device_id: str,
        txt: Callable[[], dict[str, str]],
        port: Callable[[], int],
        on_found: Callable[[str, int, str, int], None],
    ) -> None:
        self.device_id = device_id
        self.instance = f"{device_id}.{SERVICE}"
        self.host = f"{device_id}.local"
        self.txt = txt
        self.port = port
        self.on_found = on_found
        self.transport: asyncio.DatagramTransport | None = None
        self.sock: socket.socket | None = None
        self.interfaces: list[ipaddress.IPv4Interface] = []
        self._browse_task: asyncio.Task | None = None

    async def start(self, interfaces: list[ipaddress.IPv4Interface]) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        try:
            sock.bind(("0.0.0.0", MDNS_PORT))
        except OSError as e:
            log.warning("mDNS disabled, cannot bind port %d: %s", MDNS_PORT, e)
            sock.close()
            return
        self.sock = sock
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        self.update_interfaces(interfaces)
        self._browse_task = spawn(self._browse_loop(), name="mdns:browse")
        log.info("mDNS announcing %s", self.instance)

    def stop(self) -> None:
        if self._browse_task:
            self._browse_task.cancel()
        if self.transport:
            for iface in self.interfaces:
                self._send(self._records(iface.ip, ttl=0), iface)  # goodbye
            self.transport.close()
            self.transport = None

    def update_interfaces(self, interfaces: list[ipaddress.IPv4Interface]) -> None:
        if self.sock is None:
            return
        for iface in interfaces:
            if iface not in self.interfaces:
                try:
                    membership = socket.inet_aton(MDNS_ADDR) + socket.inet_aton(str(iface.ip))
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                except OSError as e:
                    log.debug("Cannot join mDNS group on %s: %s", iface.ip, e)
        self.interfaces = interfaces
        self.announce()

    def announce(self) -> None:
        for iface in self.interfaces:
            self._send(self._records(iface.ip), iface)
        self.query()

    def query(self) -> None:
        for iface in self.interfaces:
            self._send(build([], questions=[(SERVICE, TYPE_PTR, False)]), iface)

    async def _browse_loop(self) -> None:
        # RFC 6762 style start-up queries, then a slow refresh
        for delay in (1, 3, 9):
            await asyncio.sleep(delay)
            self.announce()
        while True:
            await asyncio.sleep(60)
            self.query()

    def _records(self, ip: ipaddress.IPv4Address, ttl: int = TTL) -> bytes:
        srv = struct.pack("!3H", 0, 0, self.port()) + encode_name(self.host)
        answers = [_rr(SERVICE, TYPE_PTR, encode_name(self.instance), ttl, flush=False)]
        additional = [
            _rr(self.instance, TYPE_SRV, srv, ttl),
            _rr(self.instance, TYPE_TXT, _txt(self.txt()), ttl),
            _rr(self.host, TYPE_A, ip.packed, ttl),
        ]
        return build(answers, additional)

    def _send(self, data: bytes, iface: ipaddress.IPv4Interface, dest: tuple[str, int] | None = None) -> None:
        if self.sock is None or self.transport is None:
            return
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(iface.ip)))
            self.transport.sendto(data, dest or (MDNS_ADDR, MDNS_PORT))
        except OSError as e:
            log.debug("mDNS send on %s failed: %s", iface.ip, e)

    def _interface_for(self, address: str) -> ipaddress.IPv4Interface | None:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            return None
        for iface in self.interfaces:
            if ip in iface.network:
                return iface
        return self.interfaces[0] if self.interfaces else None

    def received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = parse(data)
        except (struct.error, IndexError, UnicodeError):
            return
        if msg.is_response:
            self._handle_response(msg, data, addr[0])
        else:
            self._handle_query(msg, addr)

    def _handle_query(self, msg: Message, addr: tuple[str, int]) -> None:
        names = {SERVICE.lower(), self.instance.lower(), self.host.lower()}
        wanted = [(n, t, c) for n, t, c in msg.questions if n in names]
        if not wanted:
            return
        iface = self._interface_for(addr[0])
        if iface is None:
            return
        reply = self._records(iface.ip)
        unicast = addr[1] != MDNS_PORT or any(c & UNICAST_RESPONSE for _n, _t, c in wanted)
        self._send(reply, iface, addr if unicast else None)

    def _handle_response(self, msg: Message, data: bytes, source: str) -> None:
        ptrs = [
            decode_name(data, r.offset)[0].lower()
            for r in msg.records
            if r.rtype == TYPE_PTR and r.name == SERVICE.lower()
        ]
        if not ptrs:
            return
        srv: dict[str, tuple[int, str]] = {}
        txt: dict[str, dict[str, str]] = {}
        addresses: dict[str, str] = {}
        for r in msg.records:
            if r.rtype == TYPE_SRV and len(r.data) >= 7:
                port = struct.unpack_from("!H", r.data, 4)[0]
                srv[r.name] = (port, decode_name(data, r.offset + 6)[0].lower())
            elif r.rtype == TYPE_TXT:
                txt[r.name] = _parse_txt(r.data)
            elif r.rtype == TYPE_A and len(r.data) == 4:
                addresses[r.name] = socket.inet_ntoa(r.data)
        for instance in ptrs:
            info = txt.get(instance, {})
            device_id = info.get("id") or instance.split(".", 1)[0]
            if device_id == self.device_id:
                continue
            if instance not in srv:
                iface = self._interface_for(source)
                if iface:
                    self._send(build([], questions=[(instance, TYPE_ANY, False)]), iface)
                continue
            port, target = srv[instance]
            address = addresses.get(target, source)
            try:
                protocol = int(info.get("protocol", "8"))
            except ValueError:
                protocol = 8
            log.debug("mDNS found %s at %s:%d", device_id, address, port)
            self.on_found(address, port, device_id, protocol)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, service: MdnsService) -> None:
        self.service = service

    def datagram_received(self, data: bytes, addr) -> None:
        self.service.received(data, addr)

    def error_received(self, exc) -> None:
        log.debug("mDNS socket error: %s", exc)
