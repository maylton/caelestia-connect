"""Share this computer's mouse and keyboard with the device when the cursor leaves a screen edge."""

import asyncio
import time
from collections import deque

from ..desktop.inputcapture import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, InputCapture
from ..i18n import _
from ..utils import spawn
from .base import Plugin

# Qt::Edge values, as sent in "exitEdge"
QT_EDGES = {"top": 0x1, "left": 0x2, "right": 0x4, "bottom": 0x8}
WHEEL_STEP = 15 / 120
# A packet taking longer than this to be written means the link stalled: the pointer comes back
STALL_TIMEOUT = 2
MAX_QUEUED = 256


class ShareInputDevicesPlugin(Plugin):
    id = "shareinputdevices"
    name = _("Share mouse and keyboard")
    description = _("Move the cursor past a screen edge to control the device")
    icon = "mouse"
    incoming = ("kdeconnect.shareinputdevices",)
    outgoing = ("kdeconnect.shareinputdevices.request", "kdeconnect.mousepad.request")
    enabled_by_default = False
    settings_schema = [
        {
            "key": "edge",
            "type": "choice",
            "label": _("Screen edge"),
            "default": "left",
            "choices": [["left", _("Left")], ["right", _("Right")], ["top", _("Top")], ["bottom", _("Bottom")]],
        },
    ]

    def __init__(self, device):
        super().__init__(device)
        self.capture: InputCapture | None = None
        self.error = ""
        self._queue: deque[dict] = deque()
        self._sender: asyncio.Task | None = None
        self._sending_since = 0.0

    async def connected(self):
        if self.capture:
            # A new link: the device may have missed the start of the session and could never give it back
            self._give_back()
            return
        capture = InputCapture(
            self.setting("edge"),
            on_start=self._started,
            on_motion=lambda dx, dy: self._mouse(dx=dx, dy=dy),
            on_button=self._button,
            on_scroll=lambda dx, dy: self._mouse(scroll=True, dx=dx, dy=dy),
            on_scroll_discrete=lambda dx, dy: self._mouse(scroll=True, dx=dx * WHEEL_STEP, dy=-dy * WHEEL_STEP),
            on_key=lambda desc: self._mouse(**desc),
        )
        self.capture = capture
        try:
            await capture.start()
            self.error = ""
        except Exception as e:
            capture.stop()
            if self.capture is not capture:
                return  # unloaded while starting
            self.log.warning("Input capture unavailable: %s", e)
            self.error = str(e)
            self.capture = None
        self.changed()

    def destroy(self):
        if self.capture:
            self._give_back()
            self.capture.stop()
            self.capture = None

    def settings_changed(self, key):
        if key == "edge" and self.capture:
            spawn(self.capture.set_edge(self.setting("edge")))

    def handle(self, pkt):
        if self.capture and pkt.has("releaseDeltax"):
            vertical = self.setting("edge") in ("left", "right")
            self.capture.release(pkt.get_float("releaseDeltay" if vertical else "releaseDeltax"))

    def state(self):
        return {"ready": self.capture is not None, "error": self.error}

    def _started(self, dx: float, dy: float) -> None:
        self.send(
            "kdeconnect.shareinputdevices.request",
            {"exitEdge": QT_EDGES[self.setting("edge")], "deltax": round(dx), "deltay": round(dy)},
        )

    def _button(self, button: int, pressed: bool) -> None:
        # The mousepad protocol only has separate press and release for the left button
        if button == BTN_LEFT:
            self._mouse(**{"singlehold" if pressed else "singlerelease": True})
        elif button == BTN_RIGHT and pressed:
            self._mouse(rightclick=True)
        elif button == BTN_MIDDLE and pressed:
            self._mouse(middleclick=True)

    def _mouse(self, **body) -> None:
        last = self._queue[-1] if self._queue else None
        if last is not None and set(body) == set(last) == {"dx", "dy"}:
            last["dx"] += body["dx"]  # merge movements the link has not caught up with
            last["dy"] += body["dy"]
        else:
            self._queue.append(body)
        stalled = self._sending_since and time.monotonic() - self._sending_since > STALL_TIMEOUT
        if stalled or len(self._queue) > MAX_QUEUED:
            self.log.warning("The device stopped receiving input, giving the pointer back")
            self._give_back()
        elif self._sender is None:
            self._sender = spawn(self._send_queued(), name="shareinputdevices:send")

    async def _send_queued(self) -> None:
        try:
            while self._queue:
                body = self._queue.popleft()
                self._sending_since = time.monotonic()
                sent = await self.send_wait("kdeconnect.mousepad.request", body)
                self._sending_since = 0.0
                if not sent:
                    self._give_back()
        finally:
            self._sender = None

    def _give_back(self) -> None:
        self._queue.clear()
        if self.capture:
            self.capture.release()
