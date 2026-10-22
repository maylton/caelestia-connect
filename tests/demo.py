"""Run Caelestia Connect against a fictional phone with sample data (no real device needed).

python3 tests/demo.py           isolated daemon; open the app with the printed XDG_RUNTIME_DIR
python3 tests/demo.py --shell   use the real runtime dir so the shell and the app pick it up
                                (stop caelestia-connectd first)
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import cairo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_android import FakeAndroid  # noqa: E402

PLAYER = "Music"
NOW = int(time.time() * 1000)
ART_URL = "kdeconnect:/artUri?demo=night-drive"
CONTACTS = {
    "alex": ("Alex Rivera", "+1 555 0100"),
    "sam": ("Sam Chen", "+1 555 0142"),
    "mom": ("Mom", "+1 555 0123"),
}


def _png(path: Path, size: int, painter) -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    painter(cairo.Context(surface), size)
    surface.write_to_png(str(path))
    return path.read_bytes()


def album_art(path: Path) -> bytes:
    def paint(cr, s):
        grad = cairo.LinearGradient(0, 0, s, s)
        grad.add_color_stop_rgb(0, 0.10, 0.12, 0.35)
        grad.add_color_stop_rgb(1, 0.85, 0.30, 0.45)
        cr.set_source(grad)
        cr.paint()
        cr.set_source_rgba(1, 0.85, 0.55, 0.9)
        cr.arc(s * 0.5, s * 0.62, s * 0.22, math.pi, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.05, 0.05, 0.12, 0.85)
        cr.rectangle(0, s * 0.62, s, s * 0.38)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.25)
        cr.set_line_width(s * 0.01)
        for i in range(1, 8):
            y = s * 0.62 + i * i * s * 0.006
            cr.move_to(0, y)
            cr.line_to(s, y)
            cr.stroke()

    return _png(path, 512, paint)


def avatar(path: Path, letter: str, rgb: tuple[float, float, float]) -> bytes:
    def paint(cr, s):
        cr.set_source_rgb(*rgb)
        cr.arc(s / 2, s / 2, s / 2, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(s * 0.5)
        ext = cr.text_extents(letter)
        cr.move_to(s / 2 - ext.width / 2 - ext.x_bearing, s / 2 - ext.height / 2 - ext.y_bearing)
        cr.show_text(letter)

    return _png(path, 96, paint)


def vcard(uid: str) -> str:
    name, number = CONTACTS[uid]
    return f"BEGIN:VCARD\nVERSION:2.1\nFN:{name}\nTEL;CELL:{number}\nX-KDECONNECT-TIMESTAMP:1\nEND:VCARD\n"


def sms_messages() -> list[dict]:
    minute = 60_000
    rows = [
        (1, "alex", 1, "Are we still on for lunch tomorrow?", 95),
        (1, "alex", 2, "Yes! 12:30 at the usual place?", 90),
        (1, "alex", 1, "Perfect, see you there", 12),
        (2, "sam", 1, "Sent you the slides for Friday", 300),
        (2, "sam", 2, "Thanks, I'll take a look tonight", 280),
        (3, "mom", 1, "Call me when you get home ❤️", 1500),
    ]
    out = []
    for i, (thread, uid, mtype, body, ago) in enumerate(rows):
        out.append(
            {
                "_id": i + 1,
                "thread_id": thread,
                "type": mtype,
                "body": body,
                "read": 1 if ago > 20 else 0,
                "date": NOW - ago * minute,
                "event": 1,
                "addresses": [{"address": CONTACTS[uid][1]}],
            }
        )
    return out


class DemoPhone:
    def __init__(self, host: str, cert_pem: str, assets: Path) -> None:
        self.phone = FakeAndroid(host, cert_pem)
        self.phone.name = "Pixel 9"
        self.assets = assets
        self.art = album_art(assets / "art.png")
        self.icons = {
            "alex": avatar(assets / "alex.png", "A", (0.24, 0.52, 0.89)),
            "mail": avatar(assets / "mail.png", "M", (0.85, 0.33, 0.29)),
            "cal": avatar(assets / "cal.png", "C", (0.20, 0.66, 0.45)),
        }
        self.started = time.time()

    def now_playing(self) -> dict:
        position = 84_000 + int((time.time() - self.started) * 1000)
        return {
            "player": PLAYER,
            "title": "Neon Tides",
            "artist": "The Satellites",
            "album": "Night Drive",
            "isPlaying": True,
            "length": 236_000,
            "pos": position,
            "canSeek": True,
            "canPlay": True,
            "canPause": True,
            "canGoNext": True,
            "canGoPrevious": True,
            "volume": 70,
            "albumArtUrl": ART_URL,
        }

    def send_state(self) -> None:
        p = self.phone
        p.send("kdeconnect.battery", {"currentCharge": 82, "isCharging": False, "thresholdEvent": 0})
        p.send("kdeconnect.connectivity_report", {"signalStrengths": {"1": {"networkType": "5G", "signalStrength": 4}}})
        p.send("kdeconnect.mpris", {"playerList": [PLAYER], "supportAlbumArtPayload": True})
        p.send("kdeconnect.mpris", self.now_playing())
        self.send_notifications()
        p.send("kdeconnect.sms.messages", {"version": 2, "messages": sms_messages()})

    def send_notifications(self) -> None:
        items = [
            ("demo|messages|1", "Messages", "Alex Rivera", "Perfect, see you there", "alex", "r1", []),
            ("demo|mail|2", "Mail", "Weekly report", "Your report for this week is ready", "mail", "", ["Archive"]),
            ("demo|calendar|3", "Calendar", "Team sync", "Starts in 15 minutes · Room 4", "cal", "", ["Snooze"]),
        ]
        for nid, app, title, text, icon, reply, actions in items:
            body = {
                "id": nid,
                "appName": app,
                "title": title,
                "text": text,
                "ticker": f"{title}: {text}",
                "isClearable": True,
                "silent": True,
                "payloadHash": f"demo-{icon}",
                "actions": actions,
            }
            if reply:
                body["requestReplyId"] = reply
            self.phone.send_with_payload("kdeconnect.notification", body, self.icons[icon]).join(10)

    def serve(self) -> None:
        """Answer the daemon the way the Android app does."""
        p = self.phone
        while True:
            with p.cond:
                p.cond.wait(1)
                pending, p.received[:] = list(p.received), []
            for pkt in pending:
                t, b = pkt["type"], pkt["body"]
                if t == "kdeconnect.mpris.request":
                    if b.get("albumArtUrl"):
                        p.send_with_payload(
                            "kdeconnect.mpris",
                            {
                                "player": PLAYER,
                                "transferringAlbumArt": True,
                                "albumArtUrl": ART_URL,
                            },
                            self.art,
                        )
                    elif b.get("requestPlayerList"):
                        p.send("kdeconnect.mpris", {"playerList": [PLAYER], "supportAlbumArtPayload": True})
                    elif b.get("requestNowPlaying"):
                        p.send("kdeconnect.mpris", self.now_playing())
                elif t == "kdeconnect.contacts.request_all_uids_timestamps":
                    p.send(
                        "kdeconnect.contacts.response_uids_timestamps",
                        {"uids": list(CONTACTS), **dict.fromkeys(CONTACTS, 1)},
                    )
                elif t == "kdeconnect.contacts.request_vcards_by_uid":
                    uids = [u for u in b.get("uids", []) if u in CONTACTS]
                    p.send("kdeconnect.contacts.response_vcards", {"uids": uids, **{u: vcard(u) for u in uids}})
                elif t in ("kdeconnect.sms.request_conversations", "kdeconnect.sms.request_conversation"):
                    p.send("kdeconnect.sms.messages", {"version": 2, "messages": sms_messages()})
                elif t == "kdeconnect.notification.request" and b.get("request"):
                    threading.Thread(target=self.send_notifications, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shell", action="store_true", help="use the real XDG_RUNTIME_DIR")
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="cc-demo-"))
    runtime = (
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if args.shell
        else tempfile.mkdtemp(prefix="ccd", dir="/tmp")
    )
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(base / "cfg"),
        XDG_CACHE_HOME=str(base / "cache"),
        XDG_DATA_HOME=str(base / "data"),
        XDG_RUNTIME_DIR=runtime,
        CAELESTIA_CONNECT_BUS_NAME="org.caelestia.Connect.Demo",
    )
    os.environ.update(env)
    from caelestia_connect import config

    config.write_json_atomic(config.CONFIG_DIR / "settings.json", {"name": "Workstation"})
    from caelestia_connect.cli import Client

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stop the daemon too when terminated
    daemon = subprocess.Popen(
        [sys.executable, "-m", "caelestia_connect.daemon", "--no-discovery", "-q"], cwd=ROOT, env=env
    )
    try:
        for _ in range(100):
            if config.IPC_SOCKET.exists():
                break
            time.sleep(0.1)
        client = Client()
        state = client.call("state")
        demo = DemoPhone("127.0.0.1", config.CERT_PATH.read_text(), base)
        demo.phone.connect_direct(state["self"]["tcpPort"], state["self"]["id"])
        demo.phone.send("kdeconnect.pair", {"pair": True, "timestamp": int(time.time())})
        for _ in range(50):
            dev = next((d for d in client.call("state")["devices"] if d["id"] == demo.phone.device_id), None)
            if dev and dev["pairState"] == "requestedByPeer":
                break
            time.sleep(0.1)
        client.call("device.accept", device=demo.phone.device_id)
        time.sleep(1)
        threading.Thread(target=demo.serve, daemon=True).start()
        demo.send_state()
        print(f"Demo phone connected. XDG_RUNTIME_DIR={runtime}  (Ctrl+C to stop)", flush=True)
        daemon.wait()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.terminate()
        daemon.wait(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
