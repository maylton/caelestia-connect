"""Certificate handling: identity generation and pairing verification codes."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import config

log = logging.getLogger(__name__)


def _write_secret(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _generate_certificate(key, common_name: str) -> x509.Certificate:
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "KDE"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "KDE Connect"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    year = datetime.timedelta(days=365)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - year)
        .not_valid_after(now + 10 * year)
        .sign(key, hashes.SHA512())
    )


class Identity:
    """Our own private key + self-signed certificate. The certificate CN is the device id."""

    def __init__(self) -> None:
        self.key = None
        self.cert: x509.Certificate | None = None
        self.regenerated = False
        self._load_or_generate()

    def _load_or_generate(self) -> None:
        try:
            self.key = serialization.load_pem_private_key(config.KEY_PATH.read_bytes(), None)
        except (OSError, ValueError):
            self.key = None
        if self.key is None:
            log.info("Generating a new private key")
            self.key = ec.generate_private_key(ec.SECP256R1())
            _write_secret(
                config.KEY_PATH,
                self.key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            self.cert = None
        else:
            try:
                self.cert = x509.load_pem_x509_certificate(config.CERT_PATH.read_bytes())
                now = datetime.datetime.now(datetime.UTC)
                if not (self.cert.not_valid_before_utc < now < self.cert.not_valid_after_utc):
                    log.warning("Certificate is not valid at this time, regenerating")
                    self.cert = None
                elif self.cert.public_key().public_numbers() != self.key.public_key().public_numbers():
                    log.warning("Certificate does not match private key, regenerating")
                    self.cert = None
            except (OSError, ValueError):
                self.cert = None
        if self.cert is None:
            device_id = config.filter_device_id(uuid.uuid4().hex)
            log.info("Generating a new certificate, device id %s", device_id)
            self.cert = _generate_certificate(self.key, device_id)
            _write_secret(config.CERT_PATH, self.cert.public_bytes(serialization.Encoding.PEM))
            self.regenerated = True

    @property
    def device_id(self) -> str:
        cn = self.cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        return config.filter_device_id(cn)

    @property
    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    @property
    def key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def tls_certificate(self):
        from gi.repository import Gio

        return Gio.TlsCertificate.new_from_pem(self.cert_pem + self.key_pem, -1)


def cert_from_der(der: bytes) -> x509.Certificate:
    return x509.load_der_x509_certificate(der)


def cert_from_pem(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def cert_to_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def cert_common_name(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return config.filter_device_id(attrs[0].value) if attrs else ""


def public_key_der(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def cert_der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def same_certificate(a: x509.Certificate, b: x509.Certificate) -> bool:
    return cert_der(a) == cert_der(b)


def verification_key(own: x509.Certificate, peer: x509.Certificate, timestamp: int) -> str:
    """8 hex chars both ends display while pairing (identical to KDE Connect / Android)."""
    a, b = public_key_der(own), public_key_der(peer)
    if a < b:
        a, b = b, a
    digest = hashlib.sha256(a + b + str(int(timestamp)).encode()).hexdigest()
    return digest[:8].upper()


def certificate_fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
