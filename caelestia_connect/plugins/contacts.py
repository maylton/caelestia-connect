"""Synchronize the device's contacts as vCards (used to show names in SMS and calls)."""

import re
from pathlib import Path

from .. import config
from ..i18n import _
from .base import Plugin, action

TIMESTAMP_FIELD = "X-KDECONNECT-TIMESTAMP"


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_vcard(text: str) -> dict:
    card = {"name": "", "phones": [], "emails": [], "timestamp": 0}
    for line in _unfold(text):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        prop = key.split(";", 1)[0].upper()
        value = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        if prop == "FN" and value:
            card["name"] = value
        elif prop == "N" and not card["name"]:
            parts = [p for p in value.split(";") if p]
            card["name"] = " ".join(reversed(parts[:2]))
        elif prop == "TEL" and value:
            card["phones"].append(value)
        elif prop == "EMAIL" and value:
            card["emails"].append(value)
        elif prop == TIMESTAMP_FIELD:
            try:
                card["timestamp"] = int(value)
            except ValueError:
                pass
    return card


def normalize_number(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    return digits[-8:] if len(digits) >= 8 else digits


def contact_name(device, number: str) -> str:
    """Name of the contact with this phone number, if contacts are synchronized for the device."""
    plugin = device.plugin("contacts")
    return plugin.lookup(number) if plugin else ""


class ContactsPlugin(Plugin):
    id = "contacts"
    name = _("Contacts synchronizer")
    description = _("Synchronize the device's contacts")
    icon = "contacts"
    incoming = ("kdeconnect.contacts.response_uids_timestamps", "kdeconnect.contacts.response_vcards")
    outgoing = ("kdeconnect.contacts.request_all_uids_timestamps", "kdeconnect.contacts.request_vcards_by_uid")

    def __init__(self, device):
        super().__init__(device)
        self.dir = config.DATA_DIR / "contacts" / device.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.contacts: dict[str, dict] = {}
        self._by_number: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        self.contacts = {}
        for f in self.dir.glob("*.vcf"):
            try:
                self.contacts[f.stem] = parse_vcard(f.read_text(errors="replace"))
            except OSError:
                continue
        self._index()

    def _index(self) -> None:
        self._by_number = {}
        for uid, c in self.contacts.items():
            for p in c["phones"]:
                n = normalize_number(p)
                if n:
                    self._by_number.setdefault(n, uid)

    def lookup(self, number: str) -> str:
        uid = self._by_number.get(normalize_number(number))
        return self.contacts[uid]["name"] if uid else ""

    async def connected(self):
        self.send("kdeconnect.contacts.request_all_uids_timestamps")

    def _path(self, uid: str) -> Path | None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", uid)
        if not safe or safe.startswith("."):
            return None
        return self.dir / f"{safe}.vcf"

    def handle(self, pkt):
        uids = [str(u) for u in pkt.get_list("uids")]
        if pkt.type == "kdeconnect.contacts.response_uids_timestamps":
            wanted = []
            known = set()
            for uid in uids:
                path = self._path(uid)
                if path is None:
                    continue
                known.add(path.stem)
                local = self.contacts.get(path.stem)
                if local is None or local["timestamp"] != pkt.get_int(uid):
                    wanted.append(uid)
            for stale in set(self.contacts) - known:
                p = self.dir / f"{stale}.vcf"
                p.unlink(missing_ok=True)
                self.contacts.pop(stale, None)
            if wanted:
                self.send("kdeconnect.contacts.request_vcards_by_uid", {"uids": wanted})
            self._index()
            self.changed()
        elif pkt.type == "kdeconnect.contacts.response_vcards":
            for uid in uids:
                path = self._path(uid)
                vcard = pkt.get_str(uid)
                if path is None or not vcard:
                    continue
                path.write_text(vcard)
                self.contacts[path.stem] = parse_vcard(vcard)
            self._index()
            self.changed()

    @action
    def sync(self) -> bool:
        self.send("kdeconnect.contacts.request_all_uids_timestamps")
        return True

    @action
    def list_contacts(self) -> list:
        return sorted(
            (
                {"uid": u, "name": c["name"], "phones": c["phones"], "emails": c["emails"]}
                for u, c in self.contacts.items()
            ),
            key=lambda c: c["name"].lower(),
        )

    def state(self):
        return {"count": len(self.contacts)}
