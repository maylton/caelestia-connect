"""The phone camera as a webcam other applications can use (Meet, OBS, browsers...).

A v4l2loopback device labelled "Caelestia Phone Camera" is fed black frames while nobody watches, and the
phone camera (scrcpy) only while some application reads it. An optional second device labelled "Caelestia
Camera Input" lets ffmpeg fit a rotated picture into the landscape frame instead of cropping it.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import select
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..utils import run, spawn, which
from . import android

log = logging.getLogger(__name__)

OUTPUT_LABEL = "Caelestia Phone Camera"
INPUT_LABEL = "Caelestia Camera Input"
WIDTH, HEIGHT = 1280, 720
RETRY_DELAY = 3
PUBLISH_INTERVAL = 30


def _ioc(direction: int, number: int, size: int) -> int:
    return direction << 30 | size << 16 | ord("V") << 8 | number


# VIDIOC_SUBSCRIBE_EVENT / VIDIOC_DQEVENT, and v4l2loopback's "number of readers changed" event.
# struct v4l2_event is 136 bytes on 64-bit systems; its payload (here the reader count) starts at byte 8.
SUBSCRIBE_EVENT = _ioc(1, 90, 32)
DEQUEUE_EVENT = _ioc(2, 89, 136)
CLIENT_USAGE = 0x10E00001
SUPPORTED = struct.calcsize("P") == 8

ROTATE = {0: "null", 90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}


def find_loopback(label: str) -> str | None:
    for name in sorted(Path("/sys/class/video4linux").glob("video*/name")):
        try:
            if name.read_text().strip() == label:
                return f"/dev/{name.parent.name}"
        except OSError:
            continue
    return None


def missing_tools() -> list[str]:
    needed = {"scrcpy": "scrcpy", "adb": "android-tools", "ffmpeg": "ffmpeg", "v4l2-ctl": "v4l-utils"}
    return [package for cmd, package in needed.items() if not which(cmd)]


class PhoneWebcam:
    """Keeps the webcam device fed. `serial()` finds the phone, `options()` returns (facing, extra rotation)."""

    def __init__(
        self,
        serial: Callable[[], Awaitable[str | None]],
        options: Callable[[], tuple[str, int]],
        on_change: Callable[[], None],
    ) -> None:
        self.serial = serial
        self.options = options
        self.on_change = on_change
        self.device = find_loopback(OUTPUT_LABEL)
        self.preview = False  # the camera window owns the phone camera
        self.streaming = False
        self.camera_open = False  # scrcpy holds the phone camera
        self.error = ""
        self._task: asyncio.Task | None = None
        self._children: set[asyncio.subprocess.Process] = set()

    @property
    def available(self) -> bool:
        return SUPPORTED and self.device is not None and not missing_tools()

    def start(self) -> None:
        if self.available and self._task is None:
            self._task = spawn(self._serve(), name="webcam")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        for process in list(self._children):  # synchronous: the daemon may be exiting
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        self._children.clear()
        self._set(streaming=False)

    def _set(self, streaming: bool | None = None, error: str | None = None) -> None:
        changed = False
        if streaming is not None and streaming != self.streaming:
            self.streaming, changed = streaming, True
        if error is not None and error != self.error:
            if error:
                log.warning("Webcam: %s", error)
            self.error, changed = error, True
        if changed:
            self.on_change()

    async def _tool(self, *cmd: str) -> asyncio.subprocess.Process:
        process = await android.start_tool(*cmd)
        self._children.add(process)
        return process

    async def _end(self, process: asyncio.subprocess.Process | None) -> None:
        if process is not None:
            await android.stop_tool(process)
            self._children.discard(process)

    async def _serve(self) -> None:
        fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        publisher = None
        producer: asyncio.Task | None = None
        try:
            fcntl.ioctl(fd, SUBSCRIBE_EVENT, struct.pack("=8I", CLIENT_USAGE, 0, 1, 0, 0, 0, 0, 0))
            poll = select.poll()
            poll.register(fd, select.POLLPRI)
            # Keep the format between writers so readers don't see the device change under them
            await run("v4l2-ctl", "-d", self.device, "-c", "keep_format=1", timeout=5)
            publisher = spawn(self._publish(), name="webcam:publish")
            readers = False
            retry_at = 0.0
            loop = asyncio.get_running_loop()
            while True:
                while poll.poll(0):
                    event = bytearray(136)
                    fcntl.ioctl(fd, DEQUEUE_EVENT, event)
                    readers = bool(struct.unpack_from("=I", event, 8)[0])
                if producer and producer.done():
                    failed = not producer.cancelled() and producer.exception() is not None
                    if failed:
                        self._set(error=str(producer.exception()))
                        retry_at = loop.time() + RETRY_DELAY
                    producer = None
                wanted = "camera" if readers and not self.preview else "idle"
                if producer and producer.get_name() != wanted:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    producer = None
                if producer is None and loop.time() >= retry_at:
                    work = self._camera() if wanted == "camera" else self._idle()
                    producer = asyncio.create_task(work, name=wanted)
                self._set(streaming=wanted == "camera" and producer is not None)
                await asyncio.sleep(0.1)
        except OSError as e:
            self._set(error=f"cannot use {self.device}: {e.strerror or e}")
        finally:
            for task in (producer, publisher):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (producer, publisher) if t), return_exceptions=True)
            os.close(fd)

    async def _idle(self) -> None:
        """Black frames: with exclusive_caps, applications only list the device while something writes."""
        writer = await self._tool(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
            "-i", f"color=black:size={WIDTH}x{HEIGHT}:rate=15", "-pix_fmt", "yuv420p", "-f", "v4l2", self.device,
        )  # fmt: skip
        try:
            await writer.wait()
        finally:
            await self._end(writer)

    async def _camera(self) -> None:
        serial = await self.serial()
        if not serial:
            raise RuntimeError("the phone is not connected over USB or wireless debugging")
        facing, extra = self.options()
        # Capture upright landscape frames; the extra rotation is applied afterwards
        angle = await android.camera_rotation(serial, facing)
        capture = 180 if 90 <= angle < 270 else 0
        source = (
            "scrcpy", "--serial", serial, "--video-source=camera", f"--camera-facing={facing}",
            f"--camera-size={WIDTH}x{HEIGHT}", "--no-audio", "--no-window", f"--capture-orientation={capture}",
        )  # fmt: skip
        fitting = find_loopback(INPUT_LABEL)
        self.camera_open = True
        phone = await self._tool(
            *source, f"--v4l2-sink={fitting or self.device}", *([] if fitting else [f"--angle={extra}"])
        )
        converter = None
        self._set(error="")
        try:
            if fitting:
                await self._wait_capturing(phone, fitting)
                converter = await self._tool(
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                    "-f", "v4l2", "-input_format", "yuv420p", "-video_size", f"{WIDTH}x{HEIGHT}", "-i", fitting,
                    "-vf", f"{ROTATE[extra]},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:"
                    f"force_divisible_by=2,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
                    "-pix_fmt", "yuv420p", "-f", "v4l2", self.device,
                )  # fmt: skip
            while phone.returncode is None and (converter is None or converter.returncode is None):
                await asyncio.sleep(1)
                if self.options() != (facing, extra):
                    return  # restarted with the new options
                new_angle = await android.camera_rotation(serial, facing)
                if (180 if 90 <= new_angle < 270 else 0) != capture:
                    return
            raise RuntimeError("the phone camera stream stopped")
        finally:
            await self._end(converter)
            await self._end(phone)
            self.camera_open = False

    @staticmethod
    async def _wait_capturing(phone: asyncio.subprocess.Process, device: str) -> None:
        state = Path("/sys/class/video4linux") / Path(device).name / "state"
        for _attempt in range(100):
            if phone.returncode is not None:
                raise RuntimeError("the phone camera stream stopped")
            if state.read_text().strip() == "capture":
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("the phone camera did not start")

    async def _publish(self) -> None:
        """WirePlumber only looks at the device when it appears, before it can be read: add it to PipeWire."""
        while which("pw-dump") and which("pw-cli"):
            res = await run("pw-dump", timeout=10)
            try:
                nodes = [o.get("info", {}).get("props", {}) for o in json.loads(res.stdout)] if res.ok else []
            except ValueError:
                nodes = []
            if res.ok and not any(
                n.get("media.class") == "Video/Source" and n.get("api.v4l2.path") == self.device for n in nodes
            ):
                await run(
                    "pw-cli", "create-node", "spa-node-factory",
                    f"{{ factory.name = api.v4l2.source api.v4l2.path = {json.dumps(self.device)} "
                    f'node.name = caelestia_phone_camera node.description = "{OUTPUT_LABEL}" '
                    "media.class = Video/Source object.linger = true }",
                    timeout=10,
                )  # fmt: skip
            await asyncio.sleep(PUBLISH_INTERVAL)
