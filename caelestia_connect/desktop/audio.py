"""PulseAudio / PipeWire-pulse access through pactl."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..utils import Debouncer, LineProcess, run, which

log = logging.getLogger(__name__)

NORMAL_VOLUME = 65536


class Audio:
    def __init__(self) -> None:
        self.available = bool(which("pactl"))
        self._listeners: list[Callable[[], None]] = []
        self._sub: LineProcess | None = None
        self._debounce = Debouncer(0.15, self._notify)

    async def sinks(self) -> list[dict]:
        if not self.available:
            return []
        res = await run("pactl", "-f", "json", "list", "sinks", timeout=5)
        default = (await run("pactl", "get-default-sink", timeout=3)).stdout.strip()
        try:
            data = json.loads(res.stdout or "[]")
        except ValueError:
            return []
        out = []
        for s in data:
            vols = [int(ch.get("value", 0)) for ch in (s.get("volume") or {}).values() if isinstance(ch, dict)]
            out.append(
                {
                    "name": s.get("name", ""),
                    "description": s.get("description") or s.get("name", ""),
                    "volume": round(sum(vols) / len(vols)) if vols else 0,
                    "muted": bool(s.get("mute")),
                    "maxVolume": NORMAL_VOLUME,
                    "enabled": s.get("name") == default,
                }
            )
        return out

    async def set_volume(self, sink: str, raw: int) -> None:
        await run("pactl", "set-sink-volume", sink, str(max(0, int(raw))))

    async def set_mute(self, sink: str, muted: bool) -> None:
        await run("pactl", "set-sink-mute", sink, "1" if muted else "0")

    async def set_default(self, sink: str) -> None:
        await run("pactl", "set-default-sink", sink)

    async def unmute_all(self) -> list[str]:
        """Unmute every sink, returning those that were muted so they can be restored."""
        muted = [s["name"] for s in await self.sinks() if s["muted"]]
        for name in muted:
            await self.set_mute(name, False)
        return muted

    async def mute_all(self) -> list[str]:
        unmuted = [s["name"] for s in await self.sinks() if not s["muted"]]
        for name in unmuted:
            await self.set_mute(name, True)
        return unmuted

    def subscribe(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        if self.available and self._sub is None:
            self._sub = LineProcess(["pactl", "subscribe"], self._on_line)
            self._sub.start()

        def unsubscribe() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
            if not self._listeners and self._sub:
                self._sub.stop()
                self._sub = None

        return unsubscribe

    def _on_line(self, line: bytes) -> None:
        if b" sink " in line or b"server" in line:
            self._debounce()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("Audio listener failed")
