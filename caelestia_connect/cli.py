"""caelestia-connect-cli: command line client for scripts, keybinds and the shell."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

from . import config


class Client:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(config.IPC_SOCKET))
        self.file = self.sock.makefile("rb")
        self.next_id = 1

    def call(self, method: str, **params: Any) -> Any:
        mid = self.next_id
        self.next_id += 1
        self.sock.sendall(json.dumps({"id": mid, "method": method, "params": params}).encode() + b"\n")
        while True:
            line = self.file.readline()
            if not line:
                raise ConnectionError("daemon closed the connection")
            msg = json.loads(line)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result")

    def events(self):
        while True:
            line = self.file.readline()
            if not line:
                return
            msg = json.loads(line)
            if "event" in msg:
                yield msg


def _pick_device(state: dict, query: str | None) -> dict:
    devices = state["devices"]
    if query:
        for d in devices:
            if d["id"] == query or d["name"].lower() == query.lower():
                return d
        for d in devices:
            if query.lower() in d["name"].lower():
                return d
        raise SystemExit(f"No device matching '{query}'")
    usable = [d for d in devices if d["paired"] and d["reachable"]]
    if len(usable) == 1:
        return usable[0]
    if not usable:
        raise SystemExit("No paired device is connected")
    raise SystemExit("Several devices connected, choose one with -d")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="caelestia-connect-cli", description="Control Caelestia Connect")
    p.add_argument("-d", "--device", help="device id or name (default: the only connected device)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list known devices")
    sub.add_parser("state", help="dump the full daemon state as JSON")
    sub.add_parser("refresh", help="search for devices on the network")
    sub.add_parser("pair")
    sub.add_parser("unpair")
    sub.add_parser("accept", help="accept a pairing request")
    sub.add_parser("reject", help="reject a pairing request")
    pp = sub.add_parser("ping")
    pp.add_argument("message", nargs="?", default="")
    sub.add_parser("ring", help="make the device ring")
    sp = sub.add_parser("share", help="send files or a URL")
    sp.add_argument("items", nargs="+")
    tp = sub.add_parser("send-text", help="send text (copied to the device clipboard)")
    tp.add_argument("text")
    sub.add_parser("send-clipboard")
    sub.add_parser("browse", help="mount and open the device's files")
    sub.add_parser("unmount")
    smsp = sub.add_parser("sms", help="send an SMS")
    smsp.add_argument("number")
    smsp.add_argument("message")
    mp = sub.add_parser("media", help="control the device's media player")
    mp.add_argument("action", choices=["PlayPause", "Play", "Pause", "Next", "Previous", "Stop"])
    cp = sub.add_parser("command", help="run a command defined on the device")
    cp.add_argument("key")
    lp = sub.add_parser("lock", help="lock or unlock the device")
    lp.add_argument("state", choices=["lock", "unlock"], nargs="?", default="lock")
    ap = sub.add_parser("add-address", help="connect to a device by IP address")
    ap.add_argument("address")
    rp = sub.add_parser("call", help="call a plugin action: PLUGIN ACTION [JSON_ARGS]")
    rp.add_argument("plugin")
    rp.add_argument("action")
    rp.add_argument("args", nargs="?", default="{}")
    sub.add_parser("watch", help="print state changes as JSON lines")
    args = p.parse_args(argv)

    try:
        c = Client()
    except OSError:
        print("caelestia-connectd is not running", file=sys.stderr)
        return 1

    def dev() -> str:
        return _pick_device(c.call("state"), args.device)["id"]

    def plugin(name: str, action: str, **kw):
        return c.call("device.call", device=dev(), plugin=name, action=action, args=kw)

    cmd = args.cmd
    if cmd == "list":
        for d in c.call("state")["devices"]:
            flags = ("paired" if d["paired"] else d["pairState"]) + (", connected" if d["reachable"] else "")
            batt = d["state"].get("battery", {})
            extra = f" - battery {batt['charge']}%" if batt.get("charge", -1) >= 0 else ""
            print(f"{d['name']}  ({d['id']})  [{flags}]{extra}")
    elif cmd == "state":
        print(json.dumps(c.call("state"), indent=2, ensure_ascii=False))
    elif cmd == "refresh":
        c.call("refresh")
    elif cmd in ("pair", "unpair", "accept", "reject"):
        print(c.call(f"device.{cmd}", device=dev()))
    elif cmd == "ping":
        plugin("ping", "ping", message=args.message)
    elif cmd == "ring":
        plugin("findmyphone", "ring")
    elif cmd == "share":
        files = [str(Path(i).resolve()) for i in args.items if Path(i).exists()]
        urls = [i for i in args.items if not Path(i).exists()]
        if files:
            print(f"Sending {plugin('share', 'share_files', paths=files)} file(s)")
        for u in urls:
            plugin("share", "share_url", url=u)
    elif cmd == "send-text":
        plugin("share", "share_text", text=args.text)
    elif cmd == "send-clipboard":
        plugin("clipboard", "send_clipboard")
    elif cmd == "browse":
        print(plugin("sftp", "browse"))
    elif cmd == "unmount":
        plugin("sftp", "unmount")
    elif cmd == "sms":
        plugin("sms", "send_sms", addresses=[args.number], message=args.message)
    elif cmd == "media":
        plugin("mprisremote", "media_action", action=args.action)
    elif cmd == "command":
        plugin("remotecommands", "trigger", key=args.key)
    elif cmd == "lock":
        plugin("lockdevice", "set_locked", locked=args.state == "lock")
    elif cmd == "add-address":
        c.call("devices.add", address=args.address)
    elif cmd == "call":
        print(json.dumps(plugin(args.plugin, args.action, **json.loads(args.args)), indent=2, ensure_ascii=False))
    elif cmd == "watch":
        c.call("subscribe")
        try:
            for ev in c.events():
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
