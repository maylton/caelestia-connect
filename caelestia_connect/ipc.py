"""Line-delimited JSON IPC over a Unix socket.

Used by the GTK app, the Caelestia shell (Quickshell `Socket`) and the CLI.

  request:  {"id": 1, "method": "device.pair", "params": {"device": "..."}}
  response: {"id": 1, "result": ...} or {"id": 1, "error": "message"}
  event:    {"event": "state", "data": {...}}   (sent to subscribed clients on every change)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from . import config

log = logging.getLogger(__name__)


def encode(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode() + b"\n"


class IpcClientConnection:
    def __init__(self, server: IpcServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.subscribed = False

    def send(self, obj: Any) -> None:
        if self.writer.is_closing():
            return
        try:
            self.writer.write(encode(obj))
        except (ConnectionError, RuntimeError):
            pass

    async def run(self) -> None:
        try:
            while True:
                try:
                    line = await self.reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.send({"error": "invalid json"})
                    continue
                if not isinstance(msg, dict):
                    continue
                asyncio.get_event_loop().create_task(self._dispatch(msg))
        except ConnectionError:
            pass
        finally:
            self.server.clients.discard(self)
            try:
                self.writer.close()
            except RuntimeError:
                pass

    async def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            self.send({"id": mid, "error": "bad request"})
            return
        try:
            result = await self.server.handler(self, method, params)
            if mid is not None:
                self.send({"id": mid, "result": result})
        except Exception as e:  # report every failure to the caller
            if not isinstance(e, (KeyError, ValueError, TypeError, PermissionError)):
                log.exception("IPC method %s failed", method)
            if mid is not None:
                self.send({"id": mid, "error": str(e) or type(e).__name__})
        try:
            await self.writer.drain()
        except (ConnectionError, RuntimeError):
            pass


class IpcServer:
    def __init__(self, handler: Callable[[IpcClientConnection, str, dict], Awaitable[Any]]) -> None:
        self.handler = handler
        self.clients: set[IpcClientConnection] = set()
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = config.IPC_SOCKET
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._client, path=str(path), limit=64 * 1024 * 1024)
        os.chmod(path, 0o600)
        log.info("IPC listening on %s", path)

    async def _client(self, reader, writer) -> None:
        conn = IpcClientConnection(self, reader, writer)
        self.clients.add(conn)
        await conn.run()

    def broadcast(self, obj: Any) -> None:
        data = None
        for c in list(self.clients):
            if c.subscribed:
                if data is None:
                    data = encode(obj)
                if not c.writer.is_closing():
                    try:
                        c.writer.write(data)
                    except (ConnectionError, RuntimeError):
                        pass

    def stop(self) -> None:
        if self.server:
            self.server.close()
        for c in list(self.clients):
            try:
                c.writer.close()
            except RuntimeError:
                pass
        try:
            config.IPC_SOCKET.unlink()
        except FileNotFoundError:
            pass


class IpcClient:
    """Async client used by the GTK app and the CLI."""

    def __init__(self, on_event: Callable[[str, Any], None] | None = None) -> None:
        self.on_event = on_event
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self.on_disconnect: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(str(config.IPC_SOCKET), limit=64 * 1024 * 1024)
        self._task = asyncio.get_event_loop().create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "event" in msg:
                    if self.on_event:
                        try:
                            self.on_event(msg["event"], msg.get("data"))
                        except Exception:
                            log.exception("IPC event handler failed")
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result"))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("daemon disconnected"))
            self._pending.clear()
            self.writer = None
            if self.on_disconnect:
                self.on_disconnect()

    async def call(self, method: str, timeout: float = 30, **params: Any) -> Any:
        if not self.connected:
            raise ConnectionError("not connected to the daemon")
        mid = self._next_id
        self._next_id += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        self.writer.write(encode({"id": mid, "method": method, "params": params}))
        await self.writer.drain()
        return await asyncio.wait_for(fut, timeout)

    def close(self) -> None:
        if self.writer:
            self.writer.close()
        if self._task:
            self._task.cancel()
