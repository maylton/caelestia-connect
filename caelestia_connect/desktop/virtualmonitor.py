"""Virtual monitors: a Hyprland headless output served over VNC, and remote desktop clients to show one."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .. import config
from ..utils import run, run_detached, which

log = logging.getLogger(__name__)

RDP_CLIENTS = ("xfreerdp3", "xfreerdp", "sdl-freerdp3", "sdl-freerdp")
URL_CLIENTS = ("krdc", "remmina")


async def primary_resolution() -> tuple[str, float]:
    """("WIDTHxHEIGHT" in physical pixels, scale) of the focused monitor."""
    res = await run("hyprctl", "monitors", "-j", timeout=3)
    try:
        monitors = json.loads(res.stdout) if res.ok else []
    except ValueError:
        monitors = []
    mon = next((m for m in monitors if m.get("focused")), monitors[0] if monitors else None)
    if not mon:
        return "1920x1080", 1.0
    return f"{mon['width']}x{mon['height']}", float(mon.get("scale") or 1)


def can_show(protocol: str) -> bool:
    if protocol == "rdp" and any(which(c) for c in RDP_CLIENTS):
        return True
    return any(which(c) for c in URL_CLIENTS) or _scheme_handler(protocol)


def _scheme_handler(protocol: str) -> bool:
    try:
        out = subprocess.run(
            ["xdg-mime", "query", "default", f"x-scheme-handler/{protocol}"], capture_output=True, text=True, timeout=3
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def show(protocol: str, host: str, port: int, username: str, password: str) -> bool:
    """Open a remote desktop session in full screen."""
    if protocol == "rdp":
        for client in RDP_CLIENTS:
            if which(client):
                return run_detached(
                    client, f"/v:{host}:{port}", f"/u:{username}", f"/p:{password}", "/cert:ignore", "/f"
                )
    url = f"{protocol}://{username}:{password}@{host}:{port}"
    for client in URL_CLIENTS:
        if which(client):
            return run_detached(client, url)
    return run_detached("xdg-open", url)


class VirtualMonitorServer:
    """A headless Hyprland output with the given mode, exported by wayvnc with TLS and a password."""

    username = "user"

    def __init__(self, name: str) -> None:
        self.output = name
        self.password = secrets.token_urlsafe(12)
        self.port = 0
        self.proc: asyncio.subprocess.Process | None = None
        self._dir: tempfile.TemporaryDirectory | None = None

    @staticmethod
    def available() -> bool:
        return bool(which("wayvnc") and which("hyprctl") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, resolution: str, scale: float, port: int) -> None:
        res = await run("hyprctl", "output", "create", "headless", self.output, timeout=5)
        if not res.ok or "ok" not in res.stdout:
            raise RuntimeError(f"Could not create the output: {res.stdout.strip() or res.stderr.strip()}")
        mode = f'output = "{self.output}", mode = "{resolution}@60", position = "auto-right", scale = {scale:g}'
        rule = f"hl.monitor({{ {mode} }})"
        await run("hyprctl", "eval", rule, timeout=5)
        self.port = port
        self._dir = tempfile.TemporaryDirectory(prefix="cc-vnc-", dir=config.RUNTIME_DIR)
        path = Path(self._dir.name)
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (path / "rsa.pem").write_bytes(
            rsa_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            )
        )
        (path / "config").write_text(
            "\n".join(
                [
                    "address=0.0.0.0",
                    f"port={port}",
                    "enable_auth=true",
                    f"username={self.username}",
                    f"password={self.password}",
                    f"certificate_file={config.CERT_PATH}",
                    f"private_key_file={config.KEY_PATH}",
                    f"rsa_private_key_file={path / 'rsa.pem'}",
                ]
            )
            + "\n"
        )
        self.proc = await asyncio.create_subprocess_exec(
            "wayvnc",
            "-C",
            str(path / "config"),
            "-o",
            self.output,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(self.proc.wait(), 1.5)
        except TimeoutError:
            return  # still running: listening
        err = (await self.proc.stderr.read()).decode(errors="replace").strip()
        await self.stop()
        raise RuntimeError(f"wayvnc exited: {err.splitlines()[-1] if err else self.proc.returncode}")

    async def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
        self.proc = None
        await run("hyprctl", "output", "remove", self.output, timeout=5)
        if self._dir:
            self._dir.cleanup()
            self._dir = None
