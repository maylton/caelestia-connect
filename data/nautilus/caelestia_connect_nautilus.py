"""Nautilus extension: "Send to <device>" in the context menu of files."""

import json
import os
import socket
from urllib.parse import unquote, urlparse

from gi.repository import GObject, Nautilus

SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "caelestia-connect", "ipc.sock")
PT = os.environ.get("LANG", "").startswith("pt")


def _call(method, **params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCKET)
        s.sendall(json.dumps({"id": 1, "method": method, "params": params}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        msg = json.loads(buf.split(b"\n", 1)[0])
        return msg.get("result")
    finally:
        s.close()


class CaelestiaConnectMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, files):
        paths = []
        for f in files:
            uri = f.get_uri()
            if not uri.startswith("file://"):
                return []
            paths.append(unquote(urlparse(uri).path))
        if not paths:
            return []
        try:
            state = _call("state")
        except (OSError, ValueError):
            return []
        devices = [
            d
            for d in (state or {}).get("devices", [])
            if d["paired"] and d["reachable"] and any(p["id"] == "share" and p["loaded"] for p in d["plugins"])
        ]
        items = []
        for d in devices:
            label = ("Enviar para {}" if PT else "Send to {}").format(d["name"])
            item = Nautilus.MenuItem(name=f"CaelestiaConnect::send_{d['id']}", label=label, icon="phone")
            item.connect("activate", self._send, d["id"], paths)
            items.append(item)
        return items

    @staticmethod
    def _send(_item, device_id, paths):
        try:
            _call("device.call", device=device_id, plugin="share", action="share_files", args={"paths": paths})
        except (OSError, ValueError):
            pass
