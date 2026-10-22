"""Input sharing never leaves the pointer on a device that cannot give it back."""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caelestia_connect.plugins import shareinputdevices  # noqa: E402
from caelestia_connect.plugins.shareinputdevices import ShareInputDevicesPlugin  # noqa: E402


class FakeCapture:
    def __init__(self) -> None:
        self.releases = 0

    def release(self, along=None) -> None:
        self.releases += 1


def plugin(send_wait) -> ShareInputDevicesPlugin:
    p = ShareInputDevicesPlugin.__new__(ShareInputDevicesPlugin)
    p.capture = FakeCapture()
    p.error = ""
    p._queue = shareinputdevices.deque()
    p._sender = None
    p._sending_since = 0.0
    p.log = logging.getLogger("test")
    p.send_wait = send_wait
    return p


def test_movements_are_merged_while_the_link_is_busy() -> None:
    sent = []

    async def send_wait(_type, body):
        sent.append(dict(body))
        await asyncio.sleep(0.05)
        return True

    async def run():
        p = plugin(send_wait)
        for _ in range(10):
            p._mouse(dx=1.0, dy=2.0)
        p._mouse(singlehold=True)
        await asyncio.sleep(0.3)
        assert sent == [{"dx": 10.0, "dy": 20.0}, {"singlehold": True}], sent
        assert p.capture.releases == 0

    asyncio.run(run())


def test_stalled_link_gives_the_pointer_back() -> None:
    shareinputdevices.STALL_TIMEOUT = 0.1

    async def send_wait(_type, _body):
        await asyncio.sleep(10)  # the device stopped reading

    async def run():
        p = plugin(send_wait)
        p._mouse(dx=1.0, dy=0.0)
        await asyncio.sleep(0.2)
        p._mouse(dx=1.0, dy=0.0)
        assert p.capture.releases == 1
        p._sender.cancel()

    asyncio.run(run())


def test_failed_send_gives_the_pointer_back() -> None:
    async def send_wait(_type, _body):
        return False

    async def run():
        p = plugin(send_wait)
        p._mouse(dx=1.0, dy=0.0)
        await asyncio.sleep(0.05)
        assert p.capture.releases == 1

    asyncio.run(run())


def test_reconnection_gives_the_pointer_back() -> None:
    async def run():
        p = plugin(None)
        await p.connected()
        assert p.capture.releases == 1

    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
