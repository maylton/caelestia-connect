"""Publish a V4L2 webcam and open the phone camera only while it has a reader."""

from __future__ import annotations

import asyncio
import fcntl
import json
import select
import signal
import struct
from contextlib import suppress
from pathlib import Path

from ..config import CONFIG_DIR, RUNTIME_DIR, read_json, write_json_atomic
from .mirroring import _camera_rotation

# Linux x86-64 V4L2 ABI and v4l2loopback's client usage event.
SUBSCRIBE_EVENT = 0x4020565A
DEQUEUE_EVENT = 0x80885659
CLIENT_USAGE = 0x10E00001
WIDTH, HEIGHT = 1280, 720
saved_rotation = read_json(CONFIG_DIR / "webcam-rotation.json", {})
rotation_offset = saved_rotation.get("angle", 0) if isinstance(saved_rotation, dict) else 0
if rotation_offset not in (0, 90, 180, 270):
    rotation_offset = 0


async def phone_transport(serial: str) -> str:
    async def adb(*args: str) -> str:
        process = await asyncio.create_subprocess_exec("adb", *args, stdout=asyncio.subprocess.PIPE)
        output, _ = await process.communicate()
        return output.decode(errors="replace")

    devices = await adb("devices")
    if f"{serial}\tdevice" in devices.splitlines():
        return serial
    for line in (await adb("mdns", "services")).splitlines():
        if line.startswith(f"adb-{serial}-") and "_adb-tls-connect._tcp" in line:
            endpoint = line.split()[-1]
            await adb("connect", endpoint)
            if (await adb("-s", endpoint, "shell", "getprop", "ro.serialno")).strip() == serial:
                return endpoint
    raise RuntimeError("Phone is unavailable over USB or wireless ADB")


async def stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def selected_camera() -> str:
    settings = read_json(CONFIG_DIR / "camera-preview.json", {})
    if isinstance(settings, dict) and settings.get("facing") == "back":
        return "back"
    return "front"


def preview_active() -> bool:
    marker = read_json(RUNTIME_DIR / "camera-preview.json", {})
    pid = marker.get("pid") if isinstance(marker, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return any(b"caelestia_connect.ui" in arg for arg in command)
    except OSError:
        return False


def camera_input() -> str | None:
    for name in sorted(Path("/sys/class/video4linux").glob("video*/name")):
        try:
            if name.read_text().strip() == "Caelestia Camera Input":
                return "/dev/" + name.parent.name
        except OSError:
            continue
    return None


async def capture(serial: str, device: str) -> None:
    facing = selected_camera()
    transport = await phone_transport(serial)
    angle = await asyncio.to_thread(_camera_rotation, "adb", transport, facing)
    angle = 180 if 90 <= angle < 270 else 0
    input_device = camera_input()
    if input_device is None:
        offset = rotation_offset
        phone = await asyncio.create_subprocess_exec(
            "scrcpy", "--serial", transport, "--video-source=camera", f"--camera-facing={facing}",
            "--camera-size=1280x720", "--no-audio", f"--capture-orientation={angle}",
            f"--angle={offset}", f"--v4l2-sink={device}", "--no-window",
        )
        try:
            while phone.returncode is None:
                try:
                    await asyncio.wait_for(phone.wait(), timeout=0.5)
                except TimeoutError:
                    if rotation_offset != offset or selected_camera() != facing:
                        break
            if phone.returncode:
                raise RuntimeError("Phone camera stream exited")
        finally:
            await stop(phone)
        return
    phone = await asyncio.create_subprocess_exec(
        "scrcpy", "--serial", transport, "--video-source=camera", f"--camera-facing={facing}",
        "--camera-size=1280x720", "--no-audio", f"--capture-orientation={angle}",
        f"--v4l2-sink={input_device}", "--no-window",
    )
    converter = None
    try:
        # Wait until scrcpy has started writing before opening the capture side.
        state = Path("/sys/class/video4linux") / Path(input_device).name / "state"
        for _attempt in range(100):
            if phone.returncode is not None:
                raise RuntimeError("Phone camera stream exited")
            if state.read_text().strip() == "capture":
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Phone camera input did not start")
        while phone.returncode is None:
            offset = rotation_offset
            transforms = {0: "null", 90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}
            video_filter = (
                transforms[offset] + ",scale=1280:720:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2"
            )
            converter = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                "-f", "v4l2", "-input_format", "yuv420p", "-video_size", "1280x720",
                "-i", input_device, "-vf", video_filter, "-pix_fmt", "yuv420p",
                "-f", "v4l2", device,
            )
            try:
                while converter.returncode is None and phone.returncode is None:
                    try:
                        await asyncio.wait_for(converter.wait(), timeout=0.5)
                    except TimeoutError:
                        if rotation_offset != offset:
                            break
                        new_angle = await asyncio.to_thread(_camera_rotation, "adb", transport, facing)
                        new_angle = 180 if 90 <= new_angle < 270 else 0
                        if new_angle != angle or selected_camera() != facing:
                            return
                if converter.returncode:
                    raise RuntimeError("Full-frame converter exited")
            finally:
                await stop(converter)
                converter = None
        if phone.returncode:
            raise RuntimeError("Phone camera stream exited")
    finally:
        if converter is not None:
            await stop(converter)
        await stop(phone)


async def idle_frames(device: str) -> None:
    writer = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
        f"color=black:size={WIDTH}x{HEIGHT}:rate=15", "-pix_fmt", "yuv420p",
        "-f", "v4l2", device,
    )
    try:
        await writer.wait()
    finally:
        await stop(writer)


async def publish_camera(device: str) -> None:
    """Expose the ready capture device to PipeWire camera clients."""
    while True:
        try:
            dump = await asyncio.create_subprocess_exec("pw-dump", stdout=asyncio.subprocess.PIPE)
            output, _ = await dump.communicate()
            if dump.returncode == 0:
                props = [entry.get("info", {}).get("props", {}) for entry in json.loads(output)]
                if not any(
                    item.get("media.class") == "Video/Source" and item.get("api.v4l2.path") == device
                    for item in props
                ):
                    config = (
                        "{ factory.name = api.v4l2.source "
                        f"api.v4l2.path = {json.dumps(device)} "
                        'node.name = caelestia_phone_camera node.description = "Caelestia Phone Camera" '
                        "media.class = Video/Source object.linger = true }"
                    )
                    publish = await asyncio.create_subprocess_exec("pw-cli", "create-node", "spa-node-factory", config)
                    await publish.wait()
        except (OSError, ValueError) as error:
            print(f"Could not publish the webcam to PipeWire: {error}", flush=True)
        await asyncio.sleep(10)


async def serve(serial: str, device: str) -> None:
    with open(device, "rb", buffering=0) as events:
        fcntl.ioctl(events, SUBSCRIBE_EVENT, struct.pack("=8I", CLIENT_USAGE, 0, 1, 0, 0, 0, 0, 0))
        poll = select.poll()
        poll.register(events, select.POLLPRI)
        configure = await asyncio.create_subprocess_exec("v4l2-ctl", "-d", device, "-c", "keep_format=1")
        if await configure.wait():
            raise RuntimeError("Could not keep virtual webcam format")
        publication = asyncio.create_task(publish_camera(device))
        producer = None
        active = False
        retry = 0.0
        try:
            while True:
                while poll.poll(0):
                    event = bytearray(136)
                    fcntl.ioctl(events, DEQUEUE_EVENT, event)
                    active = bool(struct.unpack_from("=I", event, 8)[0])
                now = asyncio.get_running_loop().time()
                if producer and producer.done():
                    try:
                        producer.result()
                    except Exception as error:
                        print(f"Webcam producer stopped: {error!r}", flush=True)
                    retry = now + 3 if producer.exception() else 0
                    producer = None
                desired = "camera" if active and not preview_active() else "idle"
                if producer and producer.get_name() != desired:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    producer = None
                    retry = 0
                if producer is None and now >= retry:
                    operation = capture(serial, device) if desired == "camera" else idle_frames(device)
                    producer = asyncio.create_task(operation, name=desired)
                await asyncio.sleep(0.1)
        finally:
            publication.cancel()
            await asyncio.gather(publication, return_exceptions=True)
            if producer:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)


if __name__ == "__main__":
    import sys

    def rotate(amount: int) -> None:
        global rotation_offset
        rotation_offset = (rotation_offset + amount) % 360
        try:
            write_json_atomic(CONFIG_DIR / "webcam-rotation.json", {"angle": rotation_offset})
        except OSError as error:
            print(f"Could not save webcam rotation: {error}", flush=True)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, rotate, -90)
        loop.add_signal_handler(signal.SIGUSR2, rotate, 90)
        await serve(sys.argv[1], sys.argv[2])

    asyncio.run(main())
