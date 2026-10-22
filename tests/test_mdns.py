"""mDNS encoding round trip: an announcement from one service is resolved by another one."""

import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caelestia_connect import mdns  # noqa: E402

PHONE_ID = "0123456789abcdef0123456789abcdef"
DESKTOP_ID = "fedcba9876543210fedcba9876543210"


def service(device_id: str, found: list) -> mdns.MdnsService:
    return mdns.MdnsService(
        device_id,
        txt=lambda: {"id": device_id, "name": "Device", "type": "phone", "protocol": "8"},
        port=lambda: 1716,
        on_found=lambda *args: found.append(args),
    )


def test_announcement_is_resolved() -> None:
    found: list = []
    phone = service(PHONE_ID, [])
    desktop = service(DESKTOP_ID, found)
    desktop.interfaces = [ipaddress.IPv4Interface("192.168.1.2/24")]
    packet = phone._records(ipaddress.IPv4Address("192.168.1.5"))
    desktop.received(packet, ("192.168.1.5", mdns.MDNS_PORT))
    assert found == [("192.168.1.5", 1716, PHONE_ID, 8)], found


def test_own_announcement_is_ignored() -> None:
    found: list = []
    me = service(DESKTOP_ID, found)
    me.interfaces = [ipaddress.IPv4Interface("192.168.1.2/24")]
    me.received(me._records(ipaddress.IPv4Address("192.168.1.2")), ("192.168.1.2", mdns.MDNS_PORT))
    assert found == []


def test_compressed_names() -> None:
    # "a.b" followed by a pointer to it from "c.<ptr>"
    data = b"\x01a\x01b\x00\x01c\xc0\x00"
    assert mdns.decode_name(data, 0) == ("a.b", 5)
    assert mdns.decode_name(data, 5) == ("c.a.b", 9)


def test_query_is_answered() -> None:
    me = service(DESKTOP_ID, [])
    me.interfaces = [ipaddress.IPv4Interface("192.168.1.2/24")]
    sent = []
    me._send = lambda data, iface, dest=None: sent.append((data, dest))
    query = mdns.build([], questions=[(mdns.SERVICE, mdns.TYPE_PTR, False)])
    me.received(query, ("192.168.1.9", mdns.MDNS_PORT))
    assert len(sent) == 1 and sent[0][1] is None
    reply = mdns.parse(sent[0][0])
    assert reply.is_response
    assert {r.rtype for r in reply.records} == {mdns.TYPE_PTR, mdns.TYPE_SRV, mdns.TYPE_TXT, mdns.TYPE_A}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
