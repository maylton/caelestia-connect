"""Wayland clipboard access through wl-clipboard (works without window focus via data-control)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable

from ..utils import LineProcess, run, which

log = logging.getLogger(__name__)

PASSWORD_HINT = "x-kde-passwordManagerHint"


class Clipboard:
    def __init__(self) -> None:
        self.text = ""
        self.timestamp = 0  # ms since epoch of the last change
        self.is_password = False
        self._listeners: list[Callable[[str, bool], None]] = []
        self._ignore: str | None = None
        self._watch: LineProcess | None = None
        self.available = bool(which("wl-paste") and which("wl-copy"))

    def start(self) -> None:
        if not self.available or self._watch:
            if not self.available:
                log.warning("wl-clipboard not installed, clipboard sync disabled")
            return
        self._watch = LineProcess(
            ["wl-paste", "--no-newline", "--type", "text", "--watch", "sh", "-c", "cat; printf '\\036\\037'"],
            self._on_record,
            separator=b"\x1e\x1f",
        )
        self._watch.start()

    def stop(self) -> None:
        if self._watch:
            self._watch.stop()
            self._watch = None

    def subscribe(self, cb: Callable[[str, bool], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    async def _on_record(self, record: bytes) -> None:
        text = record.decode(errors="replace")
        if text == self.text:
            return
        types = await run("wl-paste", "--list-types", timeout=2)
        self.is_password = PASSWORD_HINT in types.stdout
        self.text = text
        self.timestamp = int(time.time() * 1000)
        if self._ignore is not None and text == self._ignore:
            self._ignore = None
            return
        for cb in list(self._listeners):
            try:
                cb(text, self.is_password)
            except Exception:
                log.exception("Clipboard listener failed")

    async def set_text(self, text: str) -> None:
        if not self.available:
            return
        self._ignore = text
        self.text = text
        self.timestamp = int(time.time() * 1000)
        await self._copy(["wl-copy", "--type", "text/plain;charset=utf-8"], text.encode())

    async def set_file(self, path: str, mime: str) -> None:
        """Put a received file in the clipboard (images as image data, others as file uri)."""
        if not self.available:
            return
        if mime.startswith("image/"):
            with open(path, "rb") as f:
                await self._copy(["wl-copy", "--type", mime], f.read())
        else:
            from pathlib import Path

            await self._copy(["wl-copy", "--type", "text/uri-list"], (Path(path).as_uri() + "\r\n").encode())

    @staticmethod
    async def _copy(cmd: list[str], data: bytes) -> None:
        # wl-copy forks a server that keeps the selection alive: never give it our pipes
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            proc.stdin.write(data)
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 5)
        except (TimeoutError, OSError) as e:
            log.warning("wl-copy failed: %s", e)
