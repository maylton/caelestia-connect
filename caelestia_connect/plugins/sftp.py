"""Browse the device's storage: mounts its SFTP server with sshfs."""

import asyncio
import os
import subprocess
from pathlib import Path

from .. import config
from ..i18n import _
from ..utils import run, run_detached, spawn, which
from .base import Plugin, action

MOUNT_TIMEOUT = 15


def _is_mounted(path: Path) -> bool:
    try:
        target = str(path)
        with open("/proc/self/mounts") as f:
            return any(line.split()[1].replace("\\040", " ") == target for line in f)
    except OSError:
        return False


class SftpPlugin(Plugin):
    id = "sftp"
    name = _("Remote filesystem browser")
    description = _("Browse the device's files from this computer")
    icon = "folder_open"
    incoming = ("kdeconnect.sftp",)
    outgoing = ("kdeconnect.sftp.request",)

    def __init__(self, device):
        super().__init__(device)
        self.mount_point = config.RUNTIME_DIR / "mounts" / device.id
        self.proc: asyncio.subprocess.Process | None = None
        self.directories: list[dict] = []
        self.error = ""
        self.mounting = False
        self._waiter: asyncio.Future | None = None

    @property
    def mounted(self) -> bool:
        return _is_mounted(self.mount_point)

    def destroy(self):
        spawn(self.unmount(), name="sftp:unmount")

    @action
    async def mount(self) -> bool:
        if self.mounted:
            return True
        if not which("sshfs"):
            self.error = _("sshfs is not installed")
            self.changed()
            return False
        self.error = ""
        self.mounting = True
        self.changed()
        loop = asyncio.get_running_loop()
        self._waiter = loop.create_future()
        self.send("kdeconnect.sftp.request", {"startBrowsing": True})
        try:
            ok = await asyncio.wait_for(self._waiter, MOUNT_TIMEOUT)
        except TimeoutError:
            self.error = _("The device did not answer (is the storage permission granted?)")
            ok = False
        self.mounting = False
        self.changed()
        return ok

    @action
    async def browse(self, path: str = "") -> bool:
        if not await self.mount():
            return False
        target = path or (self.directories[0]["path"] if self.directories else str(self.mount_point))
        return run_detached("xdg-open", target)

    @action
    async def unmount(self) -> bool:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
        if self.mounted:
            for cmd in (
                ["fusermount3", "-u", "-z", str(self.mount_point)],
                ["fusermount", "-u", "-z", str(self.mount_point)],
                ["umount", "-l", str(self.mount_point)],
            ):
                if which(cmd[0]) and (await run(*cmd)).ok:
                    break
        self.proc = None
        self.changed()
        return True

    def _resolve(self, ok: bool) -> None:
        if self._waiter and not self._waiter.done():
            self._waiter.set_result(ok)

    async def handle(self, pkt):
        if pkt.has("serverRunning"):
            if not pkt.get_bool("serverRunning") and self.mounted:
                await self.unmount()
            return
        if pkt.has("errorMessage"):
            self.error = pkt.get_str("errorMessage")
            self._resolve(False)
            self.changed()
            return
        if not all(pkt.has(k) for k in ("user", "port", "path")):
            return
        await self._mount(pkt)

    async def _mount(self, pkt) -> None:
        await self.unmount()
        self.mount_point.mkdir(parents=True, exist_ok=True)
        host = self.device.address or pkt.get_str("ip")
        if ":" in host:
            host = f"[{host}]"
        remote_path = "/" if pkt.has("multiPaths") else pkt.get_str("path")
        args = [
            "sshfs",
            f"{pkt.get_str('user')}@{host}:{remote_path}",
            str(self.mount_point),
            "-p",
            pkt.get_str("port"),
            "-s",
            "-f",
            "-F",
            "/dev/null",
            "-o",
            f"IdentityFile={config.KEY_PATH}",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "HostKeyAlgorithms=+ssh-rsa",
            "-o",
            "PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o",
            f"uid={os.getuid()}",
            "-o",
            f"gid={os.getgid()}",
            "-o",
            "reconnect",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "password_stdin",
            "-o",
            f"fsname={self.device.name}",
        ]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.proc.stdin.write(pkt.get_str("password").encode() + b"\n")
            await self.proc.stdin.drain()
            self.proc.stdin.close()
        except OSError as e:
            self.error = str(e)
            self._resolve(False)
            return
        for _i in range(int(MOUNT_TIMEOUT / 0.25)):
            if self.mounted:
                break
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.25)
        if not self.mounted:
            err = b""
            if self.proc.returncode is not None and self.proc.stderr:
                err = await self.proc.stderr.read()
            self.error = err.decode(errors="replace").strip() or _("Could not mount the device")
            self._resolve(False)
            self.changed()
            return
        paths = pkt.get_list("multiPaths")
        names = pkt.get_list("pathNames")
        if paths:
            self.directories = [
                {"path": str(self.mount_point) + str(p), "name": str(n)} for p, n in zip(paths, names, strict=False)
            ]
        else:
            self.directories = [
                {"path": str(self.mount_point), "name": _("All files")},
                {"path": str(self.mount_point / "DCIM" / "Camera"), "name": _("Camera pictures")},
            ]
        self.error = ""
        self._resolve(True)
        self.changed()

    def state(self):
        return {
            "available": bool(which("sshfs")),
            "mounted": self.mounted,
            "mounting": self.mounting,
            "mountPoint": str(self.mount_point),
            "directories": self.directories if self.mounted else [],
            "error": self.error,
        }
