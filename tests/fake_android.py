"""A minimal, independent KDE Connect peer that behaves like the Android app.

It uses Python's ssl module (OpenSSL) instead of GLib/GnuTLS, so it exercises the daemon against
a different TLS stack. Run the integration test with tests/run_integration.py.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ANDROID_INCOMING = [
    "kdeconnect.battery",
    "kdeconnect.clipboard",
    "kdeconnect.clipboard.connect",
    "kdeconnect.contacts.request_all_uids_timestamps",
    "kdeconnect.contacts.request_vcards_by_uid",
    "kdeconnect.findmyphone.request",
    "kdeconnect.mousepad.keyboardstate",
    "kdeconnect.mousepad.request",
    "kdeconnect.mpris",
    "kdeconnect.mpris.request",
    "kdeconnect.notification",
    "kdeconnect.notification.action",
    "kdeconnect.notification.reply",
    "kdeconnect.notification.request",
    "kdeconnect.ping",
    "kdeconnect.runcommand",
    "kdeconnect.runcommand.output",
    "kdeconnect.sftp.request",
    "kdeconnect.share.request",
    "kdeconnect.share.request.update",
    "kdeconnect.sms.request",
    "kdeconnect.sms.request_attachment",
    "kdeconnect.sms.request_conversation",
    "kdeconnect.sms.request_conversations",
    "kdeconnect.systemvolume",
    "kdeconnect.telephony.request_mute",
]
ANDROID_OUTGOING = [
    "kdeconnect.battery",
    "kdeconnect.clipboard",
    "kdeconnect.clipboard.connect",
    "kdeconnect.connectivity_report",
    "kdeconnect.contacts.response_uids_timestamps",
    "kdeconnect.contacts.response_vcards",
    "kdeconnect.digitizer",
    "kdeconnect.digitizer.session",
    "kdeconnect.findmyphone.request",
    "kdeconnect.mousepad.echo",
    "kdeconnect.mousepad.keyboardstate",
    "kdeconnect.mousepad.request",
    "kdeconnect.mpris",
    "kdeconnect.mpris.request",
    "kdeconnect.notification",
    "kdeconnect.notification.request",
    "kdeconnect.ping",
    "kdeconnect.presenter",
    "kdeconnect.runcommand.request",
    "kdeconnect.sftp",
    "kdeconnect.share.request",
    "kdeconnect.sms.attachment_file",
    "kdeconnect.sms.messages",
    "kdeconnect.systemvolume.request",
    "kdeconnect.telephony",
]


def make_identity(tmp: str):
    key = ec.generate_private_key(ec.SECP256R1())
    device_id = uuid.uuid4().hex
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, device_id),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "KDE"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Kde connect"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(tmp, "cert.pem")
    key_path = os.path.join(tmp, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            )
        )
    return device_id, cert, cert_path, key_path


def packet(ptype: str, body: dict, **extra) -> bytes:
    obj = {"id": int(time.time() * 1000), "type": ptype, "body": body, **extra}
    return json.dumps(obj).encode() + b"\n"


def read_line(sock) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        c = sock.recv(1)
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


class FakeAndroid:
    def __init__(self, host_ip: str, daemon_cert_pem: str | None = None) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fakeandroid-")
        self.device_id, self.cert, self.cert_path, self.key_path = make_identity(self.tmp)
        self.host_ip = host_ip
        self.daemon_cert_pem = daemon_cert_pem
        self.name = "Test Pixel"
        self.tls: ssl.SSLSocket | None = None
        self.received: list[dict] = []
        self.cond = threading.Condition()
        self._send_lock = threading.Lock()

    def identity(self, **extra) -> dict:
        return {
            "deviceId": self.device_id,
            "deviceName": self.name,
            "deviceType": "phone",
            "protocolVersion": 8,
            "incomingCapabilities": ANDROID_INCOMING,
            "outgoingCapabilities": ANDROID_OUTGOING,
            **extra,
        }

    # Android (TLS client side when the desktop connected to us after our broadcast)
    def client_ctx(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2  # Android caps at TLS 1.2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    def server_ctx(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        # Python cannot request a client certificate without validating it: trust the daemon cert
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.load_verify_locations(cadata=self.daemon_cert_pem)
        return ctx

    def connect_by_broadcast(self, timeout: float = 10) -> None:
        """We announce ourselves over UDP; the daemon connects back to our TCP port."""
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = 0
        for p in range(1740, 1764):
            try:
                srv.bind(("0.0.0.0", p))
                port = p
                break
            except OSError:
                continue
        srv.listen(1)
        srv.settimeout(timeout)
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind((self.host_ip, 0))
        u.sendto(
            packet(
                "kdeconnect.identity",
                {
                    "deviceId": self.device_id,
                    "deviceName": self.name,
                    "protocolVersion": 8,
                    "tcpPort": port,
                    "deviceType": "phone",
                },
            ),
            (self.host_ip, 1716),
        )
        u.close()
        conn, _addr = srv.accept()
        srv.close()
        conn.settimeout(timeout)
        ident = json.loads(read_line(conn))
        assert ident["type"] == "kdeconnect.identity", ident
        assert ident["body"]["targetDeviceId"] == self.device_id, ident
        # The desktop opened the TCP connection, so it is the TLS server and we are the client
        self.tls = self.client_ctx().wrap_socket(conn, server_side=False)
        self._secure_identity()

    def connect_direct(self, port: int, target_id: str, timeout: float = 10) -> None:
        """We received the desktop's broadcast and connect to its TCP port (we are the TLS server)."""
        conn = socket.create_connection((self.host_ip, port), timeout=timeout)
        conn.sendall(packet("kdeconnect.identity", self.identity(targetDeviceId=target_id, targetProtocolVersion="8")))
        self.tls = self.server_ctx().wrap_socket(conn, server_side=True)
        self._secure_identity()

    def _secure_identity(self) -> None:
        self.tls.sendall(packet("kdeconnect.identity", self.identity()))
        self.desktop_identity = json.loads(read_line(self.tls))
        assert self.desktop_identity["type"] == "kdeconnect.identity"
        self.tls.settimeout(None)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            while True:
                pkt = json.loads(read_line(self.tls))
                with self.cond:
                    self.received.append(pkt)
                    self.cond.notify_all()
        except (ConnectionError, OSError, ValueError):
            pass

    def send(self, ptype: str, body: dict | None = None) -> None:
        with self._send_lock:
            self.tls.sendall(packet(ptype, body or {}))

    def wait_for(self, ptype: str, pred=lambda b: True, timeout: float = 10) -> dict:
        end = time.time() + timeout
        with self.cond:
            while True:
                for i, p in enumerate(self.received):
                    if p["type"] == ptype and pred(p["body"]):
                        return self.received.pop(i)
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(f"no {ptype} received; got {[p['type'] for p in self.received]}")
                self.cond.wait(left)

    def send_with_payload(self, ptype: str, body: dict, data: bytes) -> threading.Thread:
        """Serve `data` like Android does: listen, announce the port, wait for the desktop to fetch it."""
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", 0))
        srv.listen(1)
        srv.settimeout(10)
        port = srv.getsockname()[1]
        result = {}

        def serve():
            try:
                conn, _ = srv.accept()
                tls = self.server_ctx().wrap_socket(conn, server_side=True)
                tls.sendall(data)
                tls.close()
                result["ok"] = True
            except Exception as e:
                result["error"] = repr(e)
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.result = result
        t.start()
        with self._send_lock:
            self.tls.sendall(packet(ptype, body, payloadSize=len(data), payloadTransferInfo={"port": port}))
        return t

    def fetch_payload(self, pkt: dict) -> bytes:
        port = pkt["payloadTransferInfo"]["port"]
        size = pkt["payloadSize"]
        conn = socket.create_connection((self.host_ip, port), timeout=10)
        tls = self.client_ctx().wrap_socket(conn, server_side=False)
        data = b""
        while len(data) < size:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
        tls.close()
        return data
