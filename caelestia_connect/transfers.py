"""Tracks file transfers so the UI and the shell can show progress."""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from typing import Any

_ids = itertools.count(1)


class Transfer:
    def __init__(
        self, manager: TransferManager, device_id: str, device_name: str, filename: str, direction: str, total: int
    ) -> None:
        self.manager = manager
        self.id = next(_ids)
        self.device_id = device_id
        self.device_name = device_name
        self.filename = filename
        self.direction = direction  # "upload" | "download"
        self.total = max(int(total or 0), 0)
        self.bytes = 0
        self.state = "pending"  # pending | running | done | failed | cancelled
        self.error = ""
        self.path = ""
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self.cancel_requested = False
        self._last_emit = 0.0

    def progress(self, nbytes: int) -> None:
        self.bytes = nbytes
        self.state = "running"
        now = time.monotonic()
        if now - self._last_emit > 0.25:
            self._last_emit = now
            self.manager.changed()

    def finish(self, ok: bool, error: str = "", path: str = "") -> None:
        if self.state in ("done", "failed", "cancelled"):
            return
        if self.cancel_requested and not ok:
            self.state = "cancelled"
        else:
            self.state = "done" if ok else "failed"
        self.error = error
        if path:
            self.path = path
        if ok and self.total:
            self.bytes = self.total
        self.finished_at = time.monotonic()
        self.manager.finished(self)

    @property
    def speed(self) -> float:
        elapsed = (self.finished_at or time.monotonic()) - self.started
        return self.bytes / elapsed if elapsed > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device_id,
            "deviceName": self.device_name,
            "filename": self.filename,
            "direction": self.direction,
            "bytes": self.bytes,
            "total": self.total,
            "state": self.state,
            "error": self.error,
            "path": self.path,
            "speed": int(self.speed),
        }


class TransferManager:
    KEEP_FINISHED = 20

    def __init__(self, on_change: Callable[[], None], on_finished: Callable[[Transfer], None]) -> None:
        self.transfers: dict[int, Transfer] = {}
        self._on_change = on_change
        self._on_finished = on_finished

    def create(self, device, filename: str, direction: str, total: int) -> Transfer:
        t = Transfer(self, device.id, device.name, filename, direction, total)
        self.transfers[t.id] = t
        self._prune()
        self.changed()
        return t

    def cancel(self, transfer_id: int) -> bool:
        t = self.transfers.get(int(transfer_id))
        if t is None or t.state not in ("pending", "running"):
            return False
        t.cancel_requested = True
        return True

    def clear_finished(self) -> None:
        for tid in [i for i, t in self.transfers.items() if t.state not in ("pending", "running")]:
            del self.transfers[tid]
        self.changed()

    def changed(self) -> None:
        self._on_change()

    def finished(self, t: Transfer) -> None:
        self._on_finished(t)
        self.changed()

    def _prune(self) -> None:
        done = [t for t in self.transfers.values() if t.state not in ("pending", "running")]
        for t in done[: max(0, len(done) - self.KEEP_FINISHED)]:
            del self.transfers[t.id]

    def to_list(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in sorted(self.transfers.values(), key=lambda t: -t.id)]
