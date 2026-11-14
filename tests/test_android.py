"""Screen mirroring, camera preview and webcam against fake adb/scrcpy/ffmpeg programs."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="cc-android-"))
BIN = TMP / "bin"
LOG = TMP / "calls"
os.environ["PATH"] = f"{BIN}:{os.environ['PATH']}"
os.environ["XDG_RUNTIME_DIR"] = str(TMP)

# Fake tools: every call is logged; their behaviour comes from files in TMP
FAKE_ADB = r"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
tmp = Path(sys.argv[0]).parent.parent
state = json.loads((tmp / "adb.json").read_text())
with open(tmp / "calls", "a") as f:
    f.write("adb " + " ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:1] == ["devices"]:
    print("List of devices attached")
    for serial, status in state["devices"].items():
        print(f"{serial}\t{status}")
elif args[:2] == ["mdns", "services"]:
    for line in state.get("mdns", []):
        print(line)
elif args[:1] == ["connect"]:
    state["devices"][args[1]] = "device"
    (tmp / "adb.json").write_text(json.dumps(state))
elif args[:1] == ["-s"] and "device_name" in args:
    print(state.get("names", {}).get(args[1], ""))
elif args[:1] == ["-s"] and "window" in args:
    print(f"mRotation={state.get('rotation', 0)}")
elif args[:1] == ["-s"] and "media.camera" in args:
    print("== Camera HAL device back\n  android.lens.facing (x):\n    [BACK ]")
    print("  android.sensor.orientation (x):\n    [90 ]")
    print("== Camera HAL device front\n  android.lens.facing (x):\n    [FRONT ]")
    print("  android.sensor.orientation (x):\n    [270 ]")
"""
FAKE_LONG = r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
tmp = Path(sys.argv[0]).parent.parent
with open(tmp / "calls", "a") as f:
    f.write(Path(sys.argv[0]).name + " " + " ".join(sys.argv[1:]) + f" pid={os.getpid()}\n")
if (tmp / (Path(sys.argv[0]).name + ".fail")).exists():
    sys.exit(1)
once = tmp / (Path(sys.argv[0]).name + ".failonce")
if once.exists():
    once.unlink()
    sys.exit(1)
time.sleep(600)
"""


def setup_tools() -> None:
    BIN.mkdir(exist_ok=True)
    for name, body in (("adb", FAKE_ADB), ("scrcpy", FAKE_LONG), ("ffmpeg", FAKE_LONG), ("v4l2-ctl", "#!/bin/sh\n")):
        (BIN / name).write_text(body)
        (BIN / name).chmod(0o755)


def adb_state(**state) -> None:
    (TMP / "adb.json").write_text(json.dumps({"devices": {}, **state}))
    LOG.write_text("")


def calls(tool: str) -> list[str]:
    return [line for line in LOG.read_text().splitlines() if line.startswith(tool + " ")]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


setup_tools()
from caelestia_connect.desktop import android, webcam  # noqa: E402
from caelestia_connect.plugins import android as android_plugin  # noqa: E402

# Never touch this machine's real v4l2loopback devices
webcam.find_loopback = lambda _label: None


class FakeConfig:
    def __init__(self) -> None:
        self.settings: dict = {}

    def plugin_settings(self, _pid: str) -> dict:
        return self.settings

    def save(self) -> None:
        pass


class FakeDevice:
    def __init__(self, name="Pixel 9", address="192.168.0.5") -> None:
        self.id = "a" * 32
        self.name = name
        self.address = address
        self.config = FakeConfig()
        self.daemon = type("D", (), {"state_changed": lambda self: None})()


def plugin(**kw) -> android_plugin.AndroidPlugin:
    return android_plugin.AndroidPlugin(FakeDevice(**kw))


def test_phone_found_by_address() -> None:
    adb_state(devices={"R5CX123": "device", "192.168.0.5:41234": "device"})
    found = asyncio.run(android.find("Pixel 9", "192.168.0.5"))
    assert found.serial == "192.168.0.5:41234", found


def test_phone_found_by_name() -> None:
    adb_state(devices={"R5CX123": "device", "ZY22": "device"}, names={"R5CX123": "Other", "ZY22": "Pixel 9"})
    assert asyncio.run(android.find("pixel 9", "")).serial == "ZY22"


def test_wireless_debugging_is_connected() -> None:
    adb_state(mdns=["adb-ZY22-abc\t_adb-tls-connect._tcp\t192.168.0.5:37001"])
    found = asyncio.run(android.find("Pixel 9", "192.168.0.5"))
    assert found.serial == "192.168.0.5:37001", found
    assert "adb connect 192.168.0.5:37001" in calls("adb")


def test_other_phone_is_not_picked() -> None:
    adb_state(devices={"R5CX123": "device"}, names={"R5CX123": "Someone else"})
    found = asyncio.run(android.find("Pixel 9", "192.168.0.5"))
    assert found.serial is None and found.authorized == ["R5CX123"]
    adb_state(devices={"R5CX123": "unauthorized"})
    assert asyncio.run(android.find("Pixel 9", "")).unauthorized


def test_mirroring() -> None:
    async def run():
        adb_state(devices={"192.168.0.5:41234": "device"})
        p = plugin()
        assert await p.mirror() == {}
        assert p.state()["mirroring"]
        (line,) = calls("scrcpy")
        assert "--serial 192.168.0.5:41234" in line and "--window-title=Pixel 9" in line
        pid = int(line.rsplit("pid=", 1)[1])
        p.destroy()
        await asyncio.sleep(0.3)
        assert not alive(pid)

        (TMP / "scrcpy.fail").touch()
        try:
            assert "error" in await plugin().mirror()
        finally:
            (TMP / "scrcpy.fail").unlink()

    asyncio.run(run())


def test_camera_preview_turns_with_the_phone() -> None:
    async def run():
        adb_state(devices={"ZY22": "device"}, names={"ZY22": "Pixel 9"}, rotation=0)
        p = plugin()
        assert await p.camera(facing="front") == {}
        assert p.setting("facing") == "front" and p.state()["preview"]
        first = calls("scrcpy")[-1]
        assert "--video-source=camera" in first and "--display-orientation=270" in first, first
        state = json.loads((TMP / "adb.json").read_text())
        state["rotation"] = 1  # phone turned to landscape
        (TMP / "adb.json").write_text(json.dumps(state))
        await asyncio.sleep(2.5)
        second = calls("scrcpy")[-1]
        assert "--display-orientation=180" in second, second
        assert not alive(int(first.rsplit("pid=", 1)[1]))
        await p.stop_camera()
        assert not p.state()["preview"]
        assert not alive(int(second.rsplit("pid=", 1)[1]))

    asyncio.run(run())


def test_both_cameras_are_upright() -> None:
    """The front sensor is mounted opposite to the back one: their pictures need turning the other way."""

    async def run():
        adb_state(devices={"ZY22": "device"}, rotation=0)
        assert await android.camera_rotation("ZY22", "back") == 90
        assert await android.camera_rotation("ZY22", "front") == 270
        for facing, capture in (("back", 180), ("front", 0)):
            LOG.write_text("")
            cam = webcam.PhoneWebcam(lambda: _const("ZY22"), lambda f=facing: (f, 0), lambda: None)
            cam.device = str(TMP / "video9")
            task = asyncio.ensure_future(cam._camera())
            await asyncio.sleep(0.8)
            (line,) = calls("scrcpy")
            assert f"--capture-orientation={capture}" in line, (facing, line)
            cam.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_camera_preview_retries_while_the_camera_is_closing() -> None:
    async def run():
        adb_state(devices={"ZY22": "device"}, names={"ZY22": "Pixel 9"})
        (TMP / "scrcpy.failonce").touch()
        p = plugin()
        assert await p.camera(facing="back") == {}
        assert len(calls("scrcpy")) == 2 and p.state()["preview"]
        await p.stop_camera()

    asyncio.run(run())


def test_webcam_feeds_the_camera_and_cleans_up() -> None:
    async def run():
        adb_state(devices={"ZY22": "device"}, names={"ZY22": "Pixel 9"})
        cam = webcam.PhoneWebcam(lambda: _const("ZY22"), lambda: ("back", 90), lambda: None)
        cam.device = str(TMP / "video9")
        task = asyncio.ensure_future(cam._camera())
        await asyncio.sleep(1)
        (line,) = calls("scrcpy")
        for arg in ("--camera-facing=back", f"--v4l2-sink={cam.device}", "--angle=90", "--no-window"):
            assert arg in line, line
        cam.stop()  # synchronous, as when the daemon exits
        await asyncio.sleep(0.3)
        assert not alive(int(line.rsplit("pid=", 1)[1]))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


async def _const(value):
    return value


def test_tools_die_with_the_daemon() -> None:
    """Even a daemon killed outright must not leave scrcpy holding the phone camera."""
    code = (
        f"import asyncio, sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from caelestia_connect.desktop import android\n"
        "async def main():\n"
        "    p = await android.start_tool('scrcpy', '--video-source=camera')\n"
        "    print(p.pid, flush=True); await asyncio.sleep(600)\n"
        "asyncio.run(main())\n"
    )
    daemon = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    pid = int(daemon.stdout.readline())
    time.sleep(0.3)
    assert alive(pid)
    daemon.send_signal(signal.SIGKILL)
    daemon.wait()
    for _ in range(30):
        if not alive(pid):
            break
        time.sleep(0.1)
    assert not alive(pid)


def test_ioctl_numbers() -> None:
    assert webcam.SUBSCRIBE_EVENT == 0x4020565A
    assert webcam.DEQUEUE_EVENT == 0x80885659


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
