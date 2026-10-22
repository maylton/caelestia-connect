"""End-to-end test: starts an isolated daemon and drives it with FakeAndroid."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_android import FakeAndroid  # noqa: E402


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="cc-it-"))
    run = Path(tempfile.mkdtemp(prefix="cc", dir="/tmp"))
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(base / "cfg"),
        XDG_CACHE_HOME=str(base / "cache"),
        XDG_DATA_HOME=str(base / "data"),
        XDG_RUNTIME_DIR=str(run),
        CAELESTIA_CONNECT_BUS_NAME="org.caelestia.Connect.Test",
        DBUS_SESSION_BUS_ADDRESS=os.environ.get("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus"),
    )
    os.environ.update(env)
    from caelestia_connect import config  # noqa: E402  (paths depend on env)
    from caelestia_connect.cli import Client  # noqa: E402

    with open(base / "daemon.log", "w") as log:
        daemon = subprocess.Popen(
            [sys.executable, "-m", "caelestia_connect.daemon", "-vv"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        ok = True
        try:
            for _ in range(50):
                if config.IPC_SOCKET.exists():
                    break
                time.sleep(0.1)
            c = Client()
            state = c.call("state")
            port = state["self"]["tcpPort"]
            cert_pem = config.CERT_PATH.read_text()
            host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"

            def check(name, cond):
                nonlocal ok
                print(("PASS " if cond else "FAIL ") + name, flush=True)
                ok &= bool(cond)

            def device():
                return next((d for d in c.call("state")["devices"] if d["id"] == phone.device_id), None)

            def wait(pred, timeout=8):
                end = time.time() + timeout
                while time.time() < end:
                    d = device()
                    if d and pred(d):
                        return d
                    time.sleep(0.1)
                return device()

            phone = FakeAndroid(host, cert_pem)
            phone.connect_by_broadcast()
            d = wait(lambda d: d["reachable"])
            check("broadcast connection", d and d["reachable"])
            check(
                "desktop identity has capabilities",
                "kdeconnect.ping" in phone.desktop_identity["body"]["incomingCapabilities"],
            )

            ts = int(time.time())
            phone.send("kdeconnect.pair", {"pair": True, "timestamp": ts})
            d = wait(lambda d: d["pairState"] == "requestedByPeer")
            check("pair request received", d["pairState"] == "requestedByPeer")
            check("verification key present", len(d["verificationKey"]) == 8)
            c.call("device.accept", device=phone.device_id)
            phone.wait_for("kdeconnect.pair", lambda b: b.get("pair") is True)
            d = wait(lambda d: d["paired"] and "battery" in [p["id"] for p in d["plugins"] if p["loaded"]])
            check("paired and plugins loaded", d["paired"])

            phone.wait_for("kdeconnect.notification.request", lambda b: b.get("request"))
            check("notification request sent on connect", True)
            phone.wait_for("kdeconnect.mpris.request", lambda b: b.get("requestPlayerList"))
            check("mpris player list requested", True)
            phone.wait_for("kdeconnect.runcommand", lambda b: "commandList" in b)
            check("command list sent", True)

            phone.send("kdeconnect.battery", {"currentCharge": 57, "isCharging": True, "thresholdEvent": 0})
            phone.send(
                "kdeconnect.connectivity_report",
                {"signalStrengths": {"6": {"networkType": "LTE", "signalStrength": 3}}},
            )
            d = wait(
                lambda d: (
                    d["state"].get("battery", {}).get("charge") == 57
                    and d["state"].get("connectivity_report", {}).get("networkType")
                )
            )
            check("battery reported", d["state"]["battery"]["charge"] == 57)
            check("connectivity reported", d["state"].get("connectivity_report", {}).get("networkType") == "LTE")

            c.call("device.call", device=phone.device_id, plugin="ping", action="ping", args={"message": "hello"})
            phone.wait_for("kdeconnect.ping", lambda b: b.get("message") == "hello")
            check("ping to phone", True)

            icon = (
                (ROOT / "data" / "test-icon.png").read_bytes()
                if (ROOT / "data" / "test-icon.png").exists()
                else b"\x89PNG fake"
            )
            t = phone.send_with_payload(
                "kdeconnect.notification",
                {
                    "id": "0|com.whatsapp|1",
                    "appName": "WhatsApp",
                    "title": "Maria",
                    "text": "Hi there!",
                    "ticker": "Maria: Hi there!",
                    "isClearable": True,
                    "silent": True,
                    "payloadHash": "abc123",
                    "requestReplyId": "r1",
                    "actions": ["Mark as read"],
                },
                icon,
            )
            t.join(10)
            d = wait(lambda d: any(i["icon"] for i in d["state"].get("notifications", {}).get("items", [])))
            items = d["state"].get("notifications", {}).get("items", [])
            check("notification listed", items and items[0]["title"] == "Maria")
            check(
                "notification icon downloaded",
                items and items[0]["icon"] and Path(items[0]["icon"]).read_bytes() == icon,
            )
            c.call(
                "device.call",
                device=phone.device_id,
                plugin="notifications",
                action="reply",
                args={"id": "0|com.whatsapp|1", "message": "all good"},
            )
            phone.wait_for(
                "kdeconnect.notification.reply",
                lambda b: b.get("message") == "all good" and b.get("requestReplyId") == "r1",
            )
            check("notification reply", True)

            data = os.urandom(300_000)
            dest_dir = base / "incoming"
            c.call(
                "device.setPluginSetting",
                device=phone.device_id,
                plugin="share",
                key="incomingPath",
                value=str(dest_dir),
            )
            t = phone.send_with_payload(
                "kdeconnect.share.request",
                {"filename": "photo.jpg", "numberOfFiles": 1, "totalPayloadSize": len(data)},
                data,
            )
            t.join(15)
            time.sleep(0.5)
            got = dest_dir / "photo.jpg"
            check("file received from phone", got.exists() and got.read_bytes() == data)

            src = base / "upload.bin"
            src.write_bytes(os.urandom(200_000))
            c.call(
                "device.call", device=phone.device_id, plugin="share", action="share_files", args={"paths": [str(src)]}
            )
            pkt = phone.wait_for("kdeconnect.share.request", lambda b: b.get("filename") == "upload.bin")
            check("file sent to phone", phone.fetch_payload(pkt) == src.read_bytes())

            c.call(
                "device.call", device=phone.device_id, plugin="share", action="share_text", args={"text": "some text"}
            )
            phone.wait_for("kdeconnect.share.request", lambda b: b.get("text") == "some text")
            check("text shared", True)

            phone.send("kdeconnect.mpris", {"playerList": ["Spotify"], "supportAlbumArtPayload": True})
            phone.send(
                "kdeconnect.mpris",
                {
                    "player": "Spotify",
                    "title": "Song",
                    "artist": "Artist",
                    "isPlaying": True,
                    "length": 200000,
                    "pos": 1000,
                    "canSeek": True,
                    "volume": 70,
                },
            )
            d = wait(
                lambda d: (
                    d["state"].get("mprisremote", {}).get("players")
                    and d["state"]["mprisremote"]["players"][0]["title"]
                )
            )
            check("remote player state", d["state"]["mprisremote"]["players"][0]["title"] == "Song")
            time.sleep(0.5)
            names = subprocess.run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True).stdout
            check("MPRIS player exported", "org.mpris.MediaPlayer2.caelestiaconnect.Spotify" in names)
            bus_name = next((row.split()[0] for row in names.splitlines() if "caelestiaconnect.Spotify" in row), "")
            if bus_name:
                subprocess.run(
                    [
                        "busctl",
                        "--user",
                        "call",
                        bus_name,
                        "/org/mpris/MediaPlayer2",
                        "org.mpris.MediaPlayer2.Player",
                        "Next",
                    ]
                )
                phone.wait_for("kdeconnect.mpris.request", lambda b: b.get("action") == "Next")
                check("MPRIS Next forwarded to phone", True)

            phone.send(
                "kdeconnect.sms.messages",
                {
                    "version": 2,
                    "messages": [
                        {
                            "_id": 1,
                            "event": 1,
                            "body": "Hello over SMS",
                            "addresses": [{"address": "+15550100"}],
                            "date": int(time.time() * 1000),
                            "type": 1,
                            "thread_id": 7,
                            "read": 0,
                        }
                    ],
                },
            )
            time.sleep(0.5)
            convs = c.call("device.call", device=phone.device_id, plugin="sms", action="conversations")
            check("sms conversation", convs and convs[0]["snippet"] == "Hello over SMS")
            c.call(
                "device.call",
                device=phone.device_id,
                plugin="sms",
                action="send_sms",
                args={"addresses": ["+15550100"], "message": "reply"},
            )
            phone.wait_for("kdeconnect.sms.request", lambda b: b.get("messageBody") == "reply")
            check("sms sent", True)

            phone.tls.close()
            wait(lambda d: not d["reachable"])
            phone2 = FakeAndroid(host, cert_pem)
            phone2.device_id, phone2.cert, phone2.cert_path, phone2.key_path = (
                phone.device_id,
                phone.cert,
                phone.cert_path,
                phone.key_path,
            )
            phone2.connect_direct(port, state["self"]["id"])
            phone = phone2
            d = wait(lambda d: d["reachable"] and d["paired"])
            check("reconnect as trusted (phone = TLS server)", d["reachable"] and d["paired"])

            c.call("device.unpair", device=phone.device_id)
            phone.wait_for("kdeconnect.pair", lambda b: b.get("pair") is False)
            check("unpair", not device()["paired"])
        except Exception as e:
            import traceback

            traceback.print_exc()
            print("FAIL exception:", e)
            ok = False
        finally:
            daemon.terminate()
            daemon.wait(5)
            if not ok:
                print("---- daemon log ----")
                print((base / "daemon.log").read_text()[-6000:])
    print("ALL PASSED" if ok else "SOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
