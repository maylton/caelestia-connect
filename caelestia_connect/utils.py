"""Small async helpers shared by the daemon and the plugins."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, name: str | None = None) -> asyncio.Task:
    """Run a coroutine in the background, keeping a reference and logging failures."""
    task = asyncio.get_event_loop().create_task(coro, name=name)
    _tasks.add(task)

    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("Task %s failed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(done)
    return task


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


class Result:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout_bytes = stdout
        self.stderr_bytes = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode(errors="replace")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode(errors="replace")


async def run(*cmd: str, input: bytes | None = None, timeout: float = 15, env: dict | None = None) -> Result:
    """Run a command asynchronously and capture its output. Never raises for missing programs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **env} if env else None,
            start_new_session=True,
        )
    except OSError as e:
        return Result(127, b"", str(e).encode())
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return Result(124, b"", b"timeout")
    return Result(proc.returncode or 0, out, err)


def run_detached(*cmd: str, env: dict | None = None) -> bool:
    """Start a program without waiting for it (file managers, browsers...)."""
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, **env} if env else None,
        )
        return True
    except OSError as e:
        log.warning("Could not start %s: %s", cmd[0], e)
        return False


class LineProcess:
    """Long running process whose stdout is consumed record by record (eg. `pactl subscribe`)."""

    def __init__(
        self, cmd: list[str], on_record: Callable[[bytes], Any], separator: bytes = b"\n", restart_delay: float = 3.0
    ) -> None:
        self.cmd = cmd
        self.on_record = on_record
        self.separator = separator
        self.restart_delay = restart_delay
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._stopped = False
            self._task = spawn(self._run(), name=f"lineprocess:{self.cmd[0]}")

    def stop(self) -> None:
        self._stopped = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stopped:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                log.warning("Could not start %s: %s", self.cmd[0], e)
                return
            buf = b""
            try:
                while True:
                    chunk = await self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while self.separator in buf:
                        record, buf = buf.split(self.separator, 1)
                        try:
                            res = self.on_record(record)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception:
                            log.exception("Error handling output of %s", self.cmd[0])
            finally:
                if self.proc.returncode is None:
                    try:
                        self.proc.terminate()
                    except ProcessLookupError:
                        pass
                await self.proc.wait()
            if not self._stopped:
                await asyncio.sleep(self.restart_delay)


class Debouncer:
    """Coalesces bursts of calls into a single call after `delay` seconds."""

    def __init__(self, delay: float, callback: Callable[[], Awaitable[None] | None]) -> None:
        self.delay = delay
        self.callback = callback
        self._handle: asyncio.TimerHandle | None = None

    def __call__(self) -> None:
        if self._handle is None:
            self._handle = asyncio.get_event_loop().call_later(self.delay, self._fire)

    def _fire(self) -> None:
        self._handle = None
        res = self.callback()
        if asyncio.iscoroutine(res):
            spawn(res)

    def cancel(self) -> None:
        if self._handle:
            self._handle.cancel()
            self._handle = None


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def unique_path(path):
    """foo.txt -> foo (1).txt if foo.txt already exists."""
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1
