"""Persistent configuration: identity, trusted devices and per-device plugin settings."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import const

log = logging.getLogger(__name__)


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "caelestia-connect"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "caelestia-connect"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "caelestia-connect"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "caelestia-connect"
IPC_SOCKET = RUNTIME_DIR / "ipc.sock"

CERT_PATH = CONFIG_DIR / "certificate.pem"
KEY_PATH = CONFIG_DIR / "privateKey.pem"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    os.chmod(RUNTIME_DIR, 0o700)


def write_json_atomic(path: Path, data: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", path, e)
        return default


def filter_device_name(name: str) -> str:
    name = "".join(c for c in (name or "") if c not in const.NAME_INVALID_CHARS)
    return name[: const.MAX_DEVICE_NAME_LENGTH]


def filter_device_id(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", device_id or "")


def is_valid_device_id(device_id: Any) -> bool:
    return isinstance(device_id, str) and re.match(const.DEVICE_ID_RE, device_id) is not None


def detect_device_type() -> str:
    try:
        out = subprocess.run(["hostnamectl", "chassis"], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if out in ("laptop", "convertible"):
        return "laptop"
    if out == "tablet":
        return "tablet"
    if out == "handset":
        return "phone"
    # Without hostnamed, a battery is the best hint of a laptop
    if not out and any(Path("/sys/class/power_supply").glob("BAT*")):
        return "laptop"
    return "desktop"


class Settings:
    """Global daemon settings (settings.json)."""

    DEFAULTS: dict[str, Any] = {
        "name": "",
        "customDevices": [],
        "incomingPath": "",
    }

    def __init__(self) -> None:
        self.path = CONFIG_DIR / "settings.json"
        self.data: dict[str, Any] = dict(self.DEFAULTS)
        self.data.update(read_json(self.path, {}))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, self.DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        write_json_atomic(self.path, self.data)

    @property
    def name(self) -> str:
        return filter_device_name(self.get("name") or socket.gethostname()) or "Linux"

    @property
    def incoming_path(self) -> Path:
        p = self.get("incomingPath")
        if p:
            return Path(os.path.expanduser(p))
        try:
            out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=2).stdout.strip()
            if out and out != str(Path.home()):
                return Path(out)
        except (OSError, subprocess.SubprocessError):
            pass
        return Path.home() / "Downloads"


class TrustedDevices:
    """Stores the certificate and metadata of every paired device (trusted_devices.json)."""

    def __init__(self) -> None:
        self.path = CONFIG_DIR / "trusted_devices.json"
        self.data: dict[str, dict[str, Any]] = read_json(self.path, {})

    def __contains__(self, device_id: str) -> bool:
        return device_id in self.data

    def ids(self) -> list[str]:
        return list(self.data)

    def get(self, device_id: str) -> dict[str, Any] | None:
        return self.data.get(device_id)

    def add(self, device_id: str, name: str, dtype: str, cert_pem: str, protocol_version: int) -> None:
        self.data[device_id] = {
            "name": name,
            "type": dtype,
            "certificate": cert_pem,
            "protocolVersion": protocol_version,
        }
        self._save()

    def update(self, device_id: str, **fields: Any) -> None:
        if device_id not in self.data:
            return
        self.data[device_id].update(fields)
        self._save()

    def remove(self, device_id: str) -> None:
        if self.data.pop(device_id, None) is not None:
            self._save()

    def clear(self) -> None:
        self.data = {}
        self._save()

    def _save(self) -> None:
        write_json_atomic(self.path, self.data)


class DeviceConfig:
    """Per-device settings: enabled plugins and plugin specific options."""

    def __init__(self, device_id: str) -> None:
        self.dir = CONFIG_DIR / "devices" / device_id
        self.path = self.dir / "config.json"
        self.data: dict[str, Any] = read_json(self.path, {})
        self.data.setdefault("plugins", {})
        self.data.setdefault("pluginSettings", {})

    def plugin_enabled(self, plugin_id: str, default: bool) -> bool:
        return bool(self.data["plugins"].get(plugin_id, default))

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        self.data["plugins"][plugin_id] = bool(enabled)
        self.save()

    def plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        return self.data["pluginSettings"].setdefault(plugin_id, {})

    def save(self) -> None:
        write_json_atomic(self.path, self.data)
