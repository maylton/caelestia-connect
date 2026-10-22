"""Send and receive files, links and text."""

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import config
from ..i18n import _
from ..packet import Payload
from ..utils import human_size, run_detached, spawn, unique_path
from .base import Plugin, action

WEB_SCHEMES = ("http://", "https://", "mailto:")


def _clean_name(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name if name not in ("", ".", "..") else f"file-{int(time.time() * 1000)}"


class SharePlugin(Plugin):
    id = "share"
    name = _("Share and receive")
    description = _("Send and receive files, links and text")
    icon = "share"
    incoming = ("kdeconnect.share.request",)
    outgoing = ("kdeconnect.share.request", "kdeconnect.share.request.update")
    settings_schema = [
        {"key": "incomingPath", "type": "folder", "label": _("Save received files to"), "default": ""},
        {"key": "openUrls", "type": "bool", "label": _("Open received links automatically"), "default": True},
    ]

    def __init__(self, device):
        super().__init__(device)
        self._batch_total = 0
        self._batch_done = 0
        self._batch_files: list[str] = []
        self._receive_lock = asyncio.Lock()

    def destination(self) -> Path:
        custom = self.setting("incomingPath")
        base = Path(os.path.expanduser(custom)) if custom else self.daemon.settings.incoming_path
        base = Path(str(base).replace("%1", self.device.name))
        base.mkdir(parents=True, exist_ok=True)
        return base

    async def handle(self, pkt):
        if pkt.has_payload or pkt.has("filename"):
            async with self._receive_lock:
                await self._receive_file(pkt)
        elif pkt.has("text"):
            await self._receive_text(pkt.get_str("text"))
        elif pkt.has("url"):
            url = pkt.get_str("url")
            if url.startswith(WEB_SCHEMES) and self.setting("openUrls"):
                run_detached("xdg-open", url)
            else:
                await self._receive_text(url)

    async def _receive_file(self, pkt):
        name = _clean_name(pkt.get_str("filename"))
        dest_dir = self.destination()
        dest = unique_path(dest_dir / name)
        if not str(dest.resolve()).startswith(str(dest_dir.resolve()) + os.sep):
            return
        total_files = pkt.get_int("numberOfFiles", 1)
        if self._batch_done >= self._batch_total:
            self._batch_total, self._batch_done, self._batch_files = total_files, 0, []
        transfer = self.daemon.transfers.create(self.device, name, "download", pkt.payload_size)
        ok = True
        if pkt.has_payload:
            ok = await self.device.download(pkt, dest, transfer)
        else:
            dest.touch()
            transfer.finish(True, path=str(dest))
        self._batch_done += 1
        if not ok:
            self.daemon.notifier.notify_nowait(
                _("Transfer failed"),
                _("Could not receive {name} from {device}").format(name=name, device=self.device.name),
                icon="dialog-error",
                source_device=self.device.name,
            )
            return
        modified = pkt.get_int("lastModified")
        if modified > 0:
            try:
                os.utime(dest, (time.time(), modified / 1000))
            except OSError:
                pass
        self._batch_files.append(str(dest))
        if pkt.get_bool("open"):
            run_detached("xdg-open", str(dest))
        if self._batch_done >= self._batch_total:
            self._notify_received(list(self._batch_files), dest_dir)

    def _notify_received(self, files: list[str], folder: Path):
        if not files:
            return
        if len(files) == 1:
            title = _("File received from {device}").format(device=self.device.name)
            body = Path(files[0]).name
        else:
            title = _("{n} files received from {device}").format(n=len(files), device=self.device.name)
            body = ", ".join(Path(f).name for f in files[:4]) + ("…" if len(files) > 4 else "")
        image = (
            files[0]
            if len(files) == 1 and files[0].lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            else None
        )

        def on_action(key, files=files, folder=folder):
            if key in ("open", "default") and len(files) == 1:
                run_detached("xdg-open", files[0])
            elif key in ("folder", "default"):
                run_detached("xdg-open", str(folder))

        actions = [("default", _("Open"))]
        if len(files) == 1:
            actions.append(("open", _("Open")))
        actions.append(("folder", _("Open folder")))
        self.daemon.notifier.notify_nowait(
            title,
            body,
            icon="folder-download",
            image_path=image,
            actions=actions,
            on_action=on_action,
            source_device=self.device.name,
        )

    async def _receive_text(self, text: str):
        await self.daemon.clipboard.set_text(text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        link = lines[-1] if lines and lines[-1].startswith(WEB_SCHEMES) else ""
        actions = [("editor", _("Open in text editor"))]
        if link:
            actions.insert(0, ("link", _("Open link")))

        def on_action(key, text=text, link=link):
            if key == "link" and link:
                run_detached("xdg-open", link)
            elif key in ("editor", "default"):
                fd, path = tempfile.mkstemp(prefix="caelestia-connect-", suffix=".txt", dir=config.CACHE_DIR)
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                run_detached("xdg-open", path)

        self.daemon.notifier.notify_nowait(
            _("Text from {device} copied to the clipboard").format(device=self.device.name),
            text[:500],
            icon="edit-paste",
            actions=actions,
            on_action=on_action,
            source_device=self.device.name,
        )

    @action
    def share_files(self, paths: list, open: bool = False) -> int:
        files = []
        for p in paths or []:
            if isinstance(p, str) and p.startswith("file://"):
                p = unquote(urlparse(p).path)
            path = Path(os.path.expanduser(str(p)))
            if path.is_file() and os.access(path, os.R_OK):
                files.append(path)
            elif path.is_dir():
                files.extend(sorted(f for f in path.rglob("*") if f.is_file()))
        if not files:
            return 0
        spawn(self._send_files(files, bool(open)), name="share:send")
        return len(files)

    async def _send_files(self, files: list[Path], open_: bool):
        total = sum(f.stat().st_size for f in files)
        failed = 0
        for f in files:
            st = f.stat()
            transfer = self.daemon.transfers.create(self.device, f.name, "upload", st.st_size)
            body = {
                "filename": f.name,
                "lastModified": int(st.st_mtime * 1000),
                "open": open_,
                "numberOfFiles": len(files),
                "totalPayloadSize": total,
            }
            try:
                body["creationTime"] = int(getattr(st, "st_birthtime", st.st_mtime) * 1000)
            except (AttributeError, ValueError):
                pass
            ok = await self.send_wait("kdeconnect.share.request", body, payload=Payload(f), transfer=transfer)
            if not ok:
                failed += 1
                if transfer.cancel_requested or not self.device.reachable:
                    break
        if failed:
            self.daemon.notifier.notify_nowait(
                _("Transfer failed"),
                _("{n} of {total} files could not be sent to {device}").format(
                    n=failed, total=len(files), device=self.device.name
                ),
                icon="dialog-error",
            )
        else:
            self.daemon.notifier.notify_nowait(
                _("Sent to {device}").format(device=self.device.name),
                _("{n} files ({size})").format(n=len(files), size=human_size(total))
                if len(files) > 1
                else files[0].name,
                icon="document-send",
                transient=True,
            )

    @action
    def share_text(self, text: str) -> bool:
        if not text:
            return False
        self.send("kdeconnect.share.request", {"text": text})
        return True

    @action
    def share_url(self, url: str) -> bool:
        if not url:
            return False
        if url.startswith(("file://", "/")):
            return self.share_files([url]) > 0
        self.send("kdeconnect.share.request", {"url": url})
        return True

    @action
    def open_folder(self) -> bool:
        return run_detached("xdg-open", str(self.destination()))

    def state(self):
        return {"destination": str(self.destination())}
