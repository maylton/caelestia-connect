"""KDE Connect network packets: newline terminated JSON objects."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

from . import config


class Payload:
    """Data attached to an outgoing packet, streamed over a dedicated TLS socket."""

    def __init__(self, path: str | os.PathLike | None = None, data: bytes | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.data = data
        if self.path is not None:
            self.size = self.path.stat().st_size
        else:
            self.size = len(data or b"")

    def open(self):
        if self.path is not None:
            return open(self.path, "rb")
        return io.BytesIO(self.data or b"")


class Packet:
    __slots__ = ("type", "body", "id", "payload", "payload_size", "transfer_info")

    def __init__(self, type: str, body: dict[str, Any] | None = None, *, payload: Payload | None = None) -> None:
        self.type = type
        self.body: dict[str, Any] = dict(body or {})
        self.id = int(time.time() * 1000)
        self.payload = payload
        self.payload_size = payload.size if payload else 0
        self.transfer_info: dict[str, Any] = {}

    # Lenient accessors: other implementations sometimes send numbers as strings and vice versa
    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.body

    def get_str(self, key: str, default: str = "") -> str:
        v = self.body.get(key)
        if v is None:
            return default
        return v if isinstance(v, str) else str(v)

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.body.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.body.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.body.get(key, default)
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return bool(v)

    def get_list(self, key: str) -> list:
        v = self.body.get(key)
        return v if isinstance(v, list) else []

    @property
    def has_payload(self) -> bool:
        return bool(self.transfer_info) or self.payload is not None

    def serialize(self) -> bytes:
        obj: dict[str, Any] = {"id": self.id, "type": self.type, "body": self.body}
        if self.payload is not None or self.transfer_info:
            obj["payloadSize"] = self.payload_size
            obj["payloadTransferInfo"] = self.transfer_info
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"

    @classmethod
    def parse(cls, data: bytes | str) -> Packet:
        obj = json.loads(data)
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str) or not obj["type"]:
            raise ValueError("packet has no type")
        body = obj.get("body")
        pkt = cls(obj["type"], body if isinstance(body, dict) else {})
        try:
            pkt.id = int(obj.get("id", pkt.id))
        except (TypeError, ValueError):
            pass
        try:
            pkt.payload_size = int(obj.get("payloadSize") or 0)
        except (TypeError, ValueError):
            pkt.payload_size = 0
        info = obj.get("payloadTransferInfo")
        pkt.transfer_info = info if isinstance(info, dict) else {}
        if "deviceId" in pkt.body and isinstance(pkt.body["deviceId"], str):
            pkt.body["deviceId"] = config.filter_device_id(pkt.body["deviceId"])
        return pkt

    def __repr__(self) -> str:
        extra = f" payload={self.payload_size}" if self.has_payload else ""
        return f"<Packet {self.type} {self.body}{extra}>"
