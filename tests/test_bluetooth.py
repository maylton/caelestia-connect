"""Bluetooth multiplexing over a local socket pair: packets, long lines, payloads and flow control."""

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caelestia_connect import bluetooth  # noqa: E402
from caelestia_connect.packet import Packet, Payload  # noqa: E402


async def pair() -> tuple[bluetooth.Multiplexer, bluetooth.Channel, bluetooth.Multiplexer, bluetooth.Channel]:
    a, b = socket.socketpair()
    for s in (a, b):
        s.setblocking(False)
    mux_a = bluetooth.Multiplexer(a, lambda: None)
    mux_b = bluetooth.Multiplexer(b, lambda: None)
    return mux_a, await mux_a.start(), mux_b, await mux_b.start()


def links(mux_a, chan_a, mux_b, chan_b):
    info = {"deviceId": "x" * 32, "deviceName": "Peer"}
    return (
        bluetooth.BluetoothLink(mux_a, chan_a, info, None, "00:00:00:00:00:01"),
        bluetooth.BluetoothLink(mux_b, chan_b, info, None, "00:00:00:00:00:02"),
    )


def test_long_packets() -> None:
    async def run():
        _ma, chan_a, _mb, chan_b = await pair()
        line = b'{"body": "' + b"x" * 50_000 + b'"}\n'
        writer = asyncio.create_task(chan_a.write(line + b"short\n"))  # blocks until the reader asks for more
        assert await asyncio.wait_for(chan_b.readline(1 << 20), 5) == line
        assert await asyncio.wait_for(chan_b.readline(1 << 20), 5) == b"short\n"
        await writer
        assert chan_b.requested <= bluetooth.BUFFER_SIZE and chan_a.free_write <= bluetooth.BUFFER_SIZE

    asyncio.run(run())


def test_payload_transfer() -> None:
    async def run():
        link_a, link_b = links(*await pair())
        received: list[Packet] = []
        link_b.on_packet = received.append
        data = os.urandom(300_000)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "src.bin")
            src.write_bytes(data)
            pkt = Packet("kdeconnect.share.request", {"filename": "src.bin"}, payload=Payload(src))
            pkt.payload_size = len(data)
            upload = asyncio.create_task(link_a.send(pkt))
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.02)
            dest = Path(tmp, "dest.bin")
            assert await asyncio.wait_for(link_b.download(received[0], dest), 10)
            assert await upload
            assert dest.read_bytes() == data

    asyncio.run(run())


def test_unclaimed_payload_is_dropped() -> None:
    async def run():
        bluetooth.UNCLAIMED_PAYLOAD_TIMEOUT = 0.2
        link_a, link_b = links(*await pair())
        pkt = Packet("kdeconnect.notification", {"id": "1"}, payload=Payload(data=os.urandom(50_000)))
        pkt.payload_size = 50_000
        assert not await asyncio.wait_for(link_a.send(pkt), 5)  # the receiver closes the channel
        assert await link_a.send(Packet("kdeconnect.ping"))

    asyncio.run(run())


def test_disconnect_closes_links() -> None:
    async def run():
        link_a, link_b = links(*await pair())
        closed = []
        link_b.on_closed = closed.append
        link_a.close()
        for _ in range(50):
            if closed:
                break
            await asyncio.sleep(0.02)
        assert closed == [link_b]

    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
