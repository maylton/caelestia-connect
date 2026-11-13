"""ADB and scrcpy: find the Android device behind a paired KDE Connect device and run tools against it.

scrcpy works over the Android debugging connection (USB debugging or Wireless debugging), which is separate
from KDE Connect pairing. The device is matched by its IP address or its device name.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from .. import config
from ..utils import run, which

log = logging.getLogger(__name__)

LOG_FILE = config.RUNTIME_DIR / "android.log"


def tools() -> dict[str, bool]:
    return {"adb": bool(which("adb")), "scrcpy": bool(which("scrcpy"))}


async def adb(*args: str, timeout: float = 10):
    return await run("adb", *args, timeout=timeout)


@dataclass
class Scan:
    serial: str | None = None  # the device that matches, if any
    authorized: list[str] = field(default_factory=list)
    unauthorized: bool = False  # a device is waiting for the "Allow USB debugging" prompt


async def _devices() -> list[tuple[str, str]]:
    res = await adb("devices")
    out = []
    for line in res.stdout.splitlines()[1:] if res.ok else []:
        serial, sep, status = line.partition("\t")
        if serial and sep:
            out.append((serial, status.strip()))
    return out


async def _connect_wireless(address: str) -> None:
    """Wireless debugging announces itself over mDNS: connect if the phone at `address` does."""
    res = await adb("mdns", "services", timeout=5)
    for line in res.stdout.splitlines() if res.ok else []:
        if "_adb-tls-connect._tcp" in line:
            endpoint = line.split()[-1]
            if endpoint.rpartition(":")[0] == address:
                await adb("connect", endpoint, timeout=8)
                return


async def find(name: str, address: str) -> Scan:
    found = await _devices()
    if address and not any(s.rpartition(":")[0] == address for s, st in found if st == "device"):
        await _connect_wireless(address)
        found = await _devices()
    scan = Scan(
        authorized=[s for s, st in found if st == "device"],
        unauthorized=any(st == "unauthorized" for _s, st in found),
    )
    for serial in scan.authorized:
        if address and serial.rpartition(":")[0] == address:
            scan.serial = serial
            return scan
    names = {}
    for serial in scan.authorized:
        res = await adb("-s", serial, "shell", "settings", "get", "global", "device_name", timeout=4)
        names[serial] = res.stdout.strip() if res.ok else ""
        if names[serial].casefold() == name.casefold():
            scan.serial = serial
            return scan
    # A single device whose name could not be read is most likely this one
    if len(scan.authorized) == 1 and not names[scan.authorized[0]]:
        scan.serial = scan.authorized[0]
    return scan


_sensors: dict[tuple[str, str], int] = {}


async def _sensor_orientation(serial: str, facing: str) -> int:
    key = (serial, facing)
    if key not in _sensors:
        front = facing == "front"
        _sensors[key] = 270 if front else 90
        res = await adb("-s", serial, "shell", "dumpsys", "media.camera", timeout=6)
        for block in res.stdout.split("== Camera HAL device ") if res.ok else []:
            lens = re.search(r"android\.lens\.facing[^\n]*\n\s*\[(FRONT|BACK|[01])\s*\]", block)
            angle = re.search(r"android\.sensor\.orientation[^\n]*\n\s*\[(0|90|180|270)\s*\]", block)
            if lens and angle and (lens[1] in ("FRONT", "0")) == front:
                _sensors[key] = int(angle[1])
                break
    return _sensors[key]


async def camera_rotation(serial: str, facing: str) -> int:
    """How much the camera picture must turn to be upright for the phone's current orientation."""
    sensor = await _sensor_orientation(serial, facing)
    device = 0
    res = await adb("-s", serial, "shell", "dumpsys", "window", "displays", timeout=4)
    if res.ok:
        proposed = re.search(r"mProposedRotation=ROTATION_(0|90|180|270)\b", res.stdout)
        current = re.search(r"\bmRotation=([0-3])\b", res.stdout)
        if proposed:
            device = int(proposed[1])
        elif current:
            device = int(current[1]) * 90
    return (device - sensor if facing == "front" else sensor + device) % 360


async def start_tool(*cmd: str) -> asyncio.subprocess.Process:
    """Start scrcpy or ffmpeg so that it never outlives the daemon. Output goes to the runtime log."""
    if which("setpriv"):
        cmd = ("setpriv", "--pdeathsig", "TERM", "--", *cmd)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "ab") as out:
        return await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out)


async def stop_tool(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), 3)
        except ProcessLookupError:
            pass
        except TimeoutError:
            process.kill()
            await process.wait()
