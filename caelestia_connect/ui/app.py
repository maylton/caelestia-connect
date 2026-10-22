"""GTK4 / libadwaita client application."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME, __version__
from ..i18n import _
from ..ipc import IpcClient
from .theme import Theme

log = logging.getLogger(__name__)


class ConnectApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.theme = Theme()
        self.client = IpcClient(on_event=self._on_event)
        self.client.on_disconnect = self._on_disconnect
        self.state: dict[str, Any] = {}
        self.listeners: list[Callable[[dict], None]] = []
        self.window = None
        self._connecting = False
        self._tasks: set[asyncio.Task] = set()
        self._pending_actions: list[Callable[[], None]] = []
        self.set_option_context_summary(APP_NAME)
        self.add_main_option(
            "device", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING, _("Device to show"), "ID"
        )
        self.add_main_option(
            "page",
            ord("p"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            _("Page: overview, messages, remote, settings"),
            "PAGE",
        )
        self.add_main_option(
            "reply",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING_ARRAY,
            _("Reply to a notification: DEVICE NOTIFICATION"),
            "ID",
        )
        self.add_main_option(
            "share", ord("s"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Send the given files to a device"), None
        )
        self.add_main_option(
            "pick-files", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Choose files to send to --device"), None
        )
        self.add_main_option("sms", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Open the messages"), None)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.theme.install()
        for name, cb, accel in (
            ("quit", lambda *_: self.quit(), "<primary>q"),
            ("about", lambda *_: self._about(), None),
            ("preferences", lambda *_: self.window and self.window.show_preferences(), "<primary>comma"),
            ("refresh", lambda *_: self.call("refresh"), "F5"),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])
        self.spawn(self._connect_loop())

    def do_command_line(self, cmdline: Gio.ApplicationCommandLine) -> int:
        opts = cmdline.get_options_dict().end().unpack()
        files = [cmdline.create_file_for_arg(a).get_path() for a in cmdline.get_arguments()[1:]]
        files = [f for f in files if f]
        reply = opts.get("reply")
        if reply and len(reply) >= 2:
            self._run_when_ready(lambda: self._open_reply(reply[0], reply[1]))
            self.hold_for_dialog()
            return 0
        if opts.get("pick-files") and opts.get("device"):
            self._run_when_ready(lambda: self._pick_files(opts["device"]))
            self.hold_for_dialog()
            return 0
        if opts.get("share") or (files and not opts.get("device")):
            self._run_when_ready(lambda: self._share(files, opts.get("device")))
            if files and opts.get("share"):
                self.hold_for_dialog()
                return 0
        self._ensure_window()
        device = opts.get("device")
        page = opts.get("page") or ("messages" if opts.get("sms") else None)
        if device or page:
            self._run_when_ready(lambda: self.window.show_device(device, page))
        self.window.present()
        return 0

    def hold_for_dialog(self) -> None:
        self.hold()
        GLib.timeout_add_seconds(1, self._release_if_no_windows)

    def _release_if_no_windows(self) -> bool:
        if not self.get_windows() and not self._pending_actions and not getattr(self, "_picking", False):
            self.release()
            return False
        return True

    def _ensure_window(self):
        if self.window is None:
            from .window import MainWindow

            self.window = MainWindow(self)
            self.window.connect("close-request", self._window_closed)
        return self.window

    def _window_closed(self, *_):
        self.window = None
        return False

    def _run_when_ready(self, fn: Callable[[], None]) -> None:
        if self.state:
            fn()
        else:
            self._pending_actions.append(fn)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("UI task failed", exc_info=task.exception())

    def call(self, method: str, /, callback: Callable[[Any], None] | None = None, **params) -> None:
        async def run():
            try:
                res = await self.client.call(method, **params)
            except Exception as e:
                self.toast(_("Error: {error}").format(error=e))
                return
            if callback:
                callback(res)

        self.spawn(run())

    def plugin(self, device: str, plugin: str, action: str, /, callback=None, **args) -> None:
        # Positional-only: plugin actions may themselves take an `action` argument (media, notifications)
        self.call("device.call", callback=callback, device=device, plugin=plugin, action=action, args=args)

    def toast(self, text: str) -> None:
        if self.window:
            self.window.toast(text)

    async def _connect_loop(self) -> None:
        if self._connecting:
            return
        self._connecting = True
        started = False
        try:
            while not self.client.connected:
                try:
                    await self.client.connect()
                    self.state = await self.client.call("subscribe")
                    self._emit()
                    for fn in self._pending_actions:
                        fn()
                    self._pending_actions.clear()
                    break
                except (OSError, ConnectionError):
                    if not started:
                        started = True
                        self._start_daemon()
                    self.state = {}
                    self._emit()
                    await asyncio.sleep(1)
        finally:
            self._connecting = False

    @staticmethod
    def _start_daemon() -> None:
        if (
            shutil.which("systemctl")
            and subprocess.run(
                ["systemctl", "--user", "start", "caelestia-connectd.service"], capture_output=True
            ).returncode
            == 0
        ):
            return
        subprocess.Popen(
            [sys.executable, "-m", "caelestia_connect.daemon", "-q"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _on_disconnect(self) -> None:
        self.state = {}
        GLib.idle_add(self._emit)
        GLib.timeout_add(1000, lambda: (self.spawn(self._connect_loop()), False)[1])

    def _on_event(self, event: str, data: Any) -> None:
        if event == "state":
            self.state = data
            self._emit()
        elif event == "transfer" and data and data.get("state") == "failed":
            self.toast(_("Transfer of {name} failed").format(name=data.get("filename")))

    def _emit(self) -> bool:
        for cb in list(self.listeners):
            try:
                cb(self.state)
            except Exception:
                log.exception("State listener failed")
        return False

    def device(self, device_id: str) -> dict | None:
        for d in self.state.get("devices", []):
            if d["id"] == device_id:
                return d
        return None

    def _open_reply(self, device_id: str, notif_id: str) -> None:
        from .dialogs import ReplyWindow

        d = self.device(device_id)
        if d is None:
            return
        items = d["state"].get("notifications", {}).get("items", [])
        n = next((i for i in items if i["id"] == notif_id), None)
        ReplyWindow(self, d, notif_id, n).present()

    def _share(self, files: list[str], device_id: str | None) -> None:
        from .dialogs import ShareWindow

        ShareWindow(self, files, device_id).present()

    def _pick_files(self, device_id: str) -> None:
        d = self.device(device_id)
        name = d["name"] if d else ""
        self._picking = True
        dlg = Gtk.FileDialog(title=_("Send files to {name}").format(name=name), modal=False)

        def done(dialog, res):
            self._picking = False
            try:
                chosen = dialog.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [f.get_path() for f in chosen if f.get_path()]
            if paths:
                self.plugin(device_id, "share", "share_files", paths=paths)

        dlg.open_multiple(self.window, None, done)

    def _about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            developer_name="Caelestia Connect",
            license_type=Gtk.License.GPL_3_0,
            comments=_("KDE Connect compatible client for the Caelestia shell"),
            website="https://kdeconnect.kde.org",
        )
        about.present(self.window)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    from gi.events import GLibEventLoopPolicy

    app = ConnectApp()
    with GLibEventLoopPolicy():
        return app.run(argv if argv is not None else sys.argv)
