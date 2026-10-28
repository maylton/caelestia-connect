"""Overview tab of a paired device: status, quick actions, media, transfers and notifications."""

from __future__ import annotations

import time

from gi.repository import Adw, Gio, Gtk, Pango

from ..i18n import _
from ..utils import human_size
from . import widgets as w


class Overview(Gtk.ScrolledWindow):
    def __init__(self, app, page) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.app = app
        self.page = page
        self.device: dict = {}
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        self.box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=8,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )
        clamp.set_child(self.box)
        self.set_child(clamp)
        self._sigs: dict[str, str] = {}

        hero = Gtk.Box(spacing=20)
        hero.add_css_class("hero")
        self.hero_icon = w.Icon("smartphone", "huge", filled=True)
        hero.append(w.circle(self.hero_icon, "hero-icon"))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_name = w.label(css=["headline"], ellipsize=True)
        col.append(self.hero_name)
        self.pills = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=8,
            column_spacing=6,
            row_spacing=6,
            homogeneous=False,
        )
        col.append(self.pills)
        hero.append(col)
        self.box.append(hero)

        self.tiles = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=6,
            min_children_per_line=3,
            column_spacing=8,
            row_spacing=8,
            homogeneous=True,
        )
        self.box.append(self.tiles)

        self.media = Gtk.Box()
        self.box.append(self.media)
        self._build_media()

        self.transfers = w.card()
        self.box.append(self.transfers)

        self.notifs = w.card()
        self.box.append(self.notifs)

        self.commands = w.card()
        self.box.append(self.commands)

        self.files = w.card()
        self.box.append(self.files)

    def _act(self, plugin: str, action: str, toast: str | None = None, **args):
        def run():
            self.app.plugin(
                self.device["id"],
                plugin,
                action,
                callback=(lambda _r: self.app.toast(toast)) if toast else None,
                **args,
            )

        return run

    def _rebuild_tiles(self) -> None:
        d = self.device
        tiles = []
        if w.plugin_loaded(self.device, "ping"):
            tiles.append(("notifications_active", _("Ping"), self._act("ping", "ping", _("Ping sent"))))
        if w.plugin_loaded(self.device, "findmyphone"):
            tiles.append(
                ("ring_volume", _("Ring"), self._act("findmyphone", "ring", _("Ringing {name}").format(name=d["name"])))
            )
        if w.plugin_loaded(self.device, "share"):
            tiles.append(("upload_file", _("Send files"), self.page.pick_files))
            tiles.append(("link", _("Send link/text"), self.page.send_text_dialog))
        if w.plugin_loaded(self.device, "clipboard"):
            tiles.append(
                ("content_paste_go", _("Send clipboard"), self._act("clipboard", "send_clipboard", _("Clipboard sent")))
            )
        if w.plugin_loaded(self.device, "sftp"):
            tiles.append(("folder_open", _("Browse files"), self._browse))
        if w.plugin_loaded(self.device, "remotecontrol") or w.plugin_loaded(self.device, "remotekeyboard"):
            tiles.append(("mouse", _("Remote input"), lambda: self.page.show_page("remote")))
        if w.plugin_loaded(self.device, "sms"):
            tiles.append(("sms", _("Messages"), lambda: self.page.show_page("messages")))
        if w.plugin_loaded(self.device, "lockdevice"):
            locked = d["state"].get("lockdevice", {}).get("remoteLocked")
            tiles.append(
                (
                    "lock_open" if locked else "lock",
                    _("Unlock") if locked else _("Lock"),
                    self._act("lockdevice", "set_locked", locked=not locked),
                )
            )
        if w.plugin_loaded(self.device, "virtualmonitor"):
            monitor = d["state"].get("virtualmonitor", {})
            if monitor.get("active"):
                tiles.append(("desktop_access_disabled", _("Stop extending"), self._act("virtualmonitor", "stop")))
            elif monitor.get("available"):
                tiles.append(("desktop_windows", _("Extend display"), self._extend_display))
        if w.plugin_loaded(self.device, "findthisdevice") and d["state"].get("findthisdevice", {}).get("ringing"):
            tiles.append(("volume_off", _("Stop ringing"), self._act("findthisdevice", "stop")))
        sig = w.signature([(t[0], t[1]) for t in tiles])
        if self._sigs.get("tiles") == sig:
            return
        self._sigs["tiles"] = sig
        self.tiles.remove_all()
        for icon, text, cb in tiles:
            self.tiles.append(w.Tile(icon, text, cb))

    def _extend_display(self) -> None:
        def done(result):
            if isinstance(result, dict) and result.get("error"):
                self.app.toast(result["error"])

        self.app.plugin(self.device["id"], "virtualmonitor", "start", callback=done)

    def _browse(self) -> None:
        sftp = self.device["state"].get("sftp", {})
        if not sftp.get("available", True):
            self.app.toast(_("Install sshfs to browse the device's files (sudo pacman -S sshfs)"))
            return
        self.app.toast(_("Mounting {name}…").format(name=self.device["name"]))
        self.app.plugin(
            self.device["id"],
            "sftp",
            "browse",
            callback=lambda ok: None if ok else self.app.toast(_("Could not mount the device")),
        )

    def _build_media(self) -> None:
        from .media import MediaCard

        def dev() -> str:
            return self.device["id"]

        self.media_card = MediaCard(
            self.app,
            on_action=lambda a: self.app.plugin(dev(), "mprisremote", "media_action", action=a),
            on_seek=lambda ms: self.app.plugin(dev(), "mprisremote", "set_position", position=ms),
            on_select=lambda name: self.app.plugin(dev(), "mprisremote", "select_player", player=name),
            on_volume=lambda v: self.app.plugin(dev(), "mprisremote", "set_volume", volume=v),
            on_output=lambda: self.app.plugin(dev(), "phoneaudio", "toggle", callback=self._output_changed),
        )
        self.media.append(self.media_card)

    def _output_changed(self, result) -> None:
        if isinstance(result, dict) and result.get("error"):
            self.app.toast(result["error"])

    def _update_media(self) -> None:
        st = self.device["state"].get("mprisremote")
        players = (st or {}).get("players") or []
        visible = bool(players) and w.plugin_loaded(self.device, "mprisremote")
        self.media.set_visible(visible)
        if visible:
            self.media_card.update(players, st.get("current") or players[0]["name"])
            self.media_card.update_output(self.device["state"].get("phoneaudio"))

    def _update_notifications(self) -> None:
        items = self.device["state"].get("notifications", {}).get("items", [])
        self.notifs.set_visible(w.plugin_loaded(self.device, "notifications"))
        sig = w.signature(items)
        if self._sigs.get("notifs") == sig:
            return
        self._sigs["notifs"] = sig
        w.clear(self.notifs)
        head = Gtk.Box(spacing=8)
        head.append(w.section_title(_("Notifications")))
        head.append(Gtk.Box(hexpand=True))
        dev = self.device["id"]
        if any(i["dismissable"] for i in items):
            head.append(
                w.text_button(
                    _("Clear all"), "clear_all", ["flat"], lambda: self.app.plugin(dev, "notifications", "dismiss_all")
                )
            )
        self.notifs.append(head)
        if not items:
            self.notifs.append(w.label(_("No notifications"), css=["body-small"]))
            return
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("flat")
        for n in items[:30]:
            lst.append(self._notification_row(dev, n))
        self.notifs.append(lst)

    def _notification_row(self, dev: str, n: dict) -> Gtk.Widget:
        row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6)
        if n["icon"]:
            pic = Gtk.Image.new_from_file(n["icon"])
            pic.set_pixel_size(36)
            pic.set_valign(Gtk.Align.START)
            row.append(pic)
        else:
            row.append(w.Icon("notifications", "large", css=["dim"]))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        top = Gtk.Box(spacing=6)
        top.append(w.label(n["appName"], css=["label-caps"]))
        top.append(w.label("· " + time.strftime("%H:%M", time.localtime(n["time"])), css=["body-small"]))
        col.append(top)
        if n["title"]:
            col.append(w.label(n["title"], css=["title-medium"], wrap=True))
        if n["text"]:
            t = w.label(n["text"], wrap=True, css=["dim"])
            t.set_lines(4)
            t.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(t)
        btns = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, column_spacing=6)

        def invoke(action: str) -> None:
            self.app.plugin(dev, "notifications", "invoke_action", id=n["id"], action=action)

        for a in n["actions"][:3]:
            btns.append(w.text_button(a, None, ["tonal"], lambda a=a: invoke(a)))
        if len(n["actions"]) > 3:
            pop = Gtk.Popover()
            pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for a in n["actions"][3:]:
                b = w.text_button(a, None, ["flat"], lambda a=a: (pop.popdown(), invoke(a)))
                pbox.append(b)
            pop.set_child(pbox)
            more = Gtk.MenuButton(popover=pop)
            more.set_child(w.Icon("more_horiz", "small"))
            more.add_css_class("tonal")
            more.add_css_class("round")
            btns.append(more)
        if n["replyId"]:
            btns.append(w.text_button(_("Reply"), "reply", ["tonal"], lambda: self.page.reply_dialog(n)))
        if btns.get_first_child():
            col.append(btns)
        row.append(col)
        if n["dismissable"]:
            row.append(
                w.icon_button(
                    "close", _("Dismiss"), callback=lambda: self.app.plugin(dev, "notifications", "dismiss", id=n["id"])
                )
            )
        return row

    def _update_transfers(self, state: dict) -> None:
        items = [t for t in state.get("transfers", []) if t["device"] == self.device["id"]][:8]
        self.transfers.set_visible(bool(items))
        sig = w.signature([(t["id"], t["state"], t["bytes"] // max(1, t["total"] // 50 or 1)) for t in items])
        if self._sigs.get("transfers") == sig:
            return
        self._sigs["transfers"] = sig
        w.clear(self.transfers)
        head = Gtk.Box()
        head.append(w.section_title(_("Transfers")))
        head.append(Gtk.Box(hexpand=True))
        if any(t["state"] not in ("pending", "running") for t in items):
            head.append(w.text_button(_("Clear"), None, ["flat"], lambda: self.app.call("transfers.clear")))
        self.transfers.append(head)
        for t in items:
            r = Gtk.Box(spacing=12)
            r.append(
                w.Icon(
                    "download" if t["direction"] == "download" else "upload",
                    "medium",
                    css=["error-text"] if t["state"] == "failed" else ["primary-text"],
                )
            )
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            col.append(w.label(t["filename"], ellipsize=True))
            if t["state"] in ("pending", "running"):
                pb = Gtk.ProgressBar(fraction=(t["bytes"] / t["total"]) if t["total"] else 0)
                col.append(pb)
                col.append(
                    w.label(
                        f"{human_size(t['bytes'])} / {human_size(t['total'])} · {human_size(t['speed'])}/s",
                        css=["body-small"],
                    )
                )
            else:
                text = {
                    "done": _("Completed"),
                    "failed": _("Failed: {error}").format(error=t["error"]),
                    "cancelled": _("Cancelled"),
                }.get(t["state"], t["state"])
                col.append(w.label(f"{text} · {human_size(t['total'])}", css=["body-small"]))
            r.append(col)
            if t["state"] in ("pending", "running"):
                r.append(
                    w.icon_button(
                        "close", _("Cancel"), callback=lambda tid=t["id"]: self.app.call("transfer.cancel", id=tid)
                    )
                )
            elif t["state"] == "done" and t["path"]:
                path = t["path"]
                r.append(
                    w.icon_button(
                        "open_in_new",
                        _("Open"),
                        callback=lambda p=path: Gio.AppInfo.launch_default_for_uri(
                            Gio.File.new_for_path(p).get_uri(), None
                        ),
                    )
                )
            self.transfers.append(r)

    def _update_commands(self) -> None:
        st = self.device["state"].get("remotecommands", {})
        cmds = st.get("commands", [])
        self.commands.set_visible(bool(cmds) and w.plugin_loaded(self.device, "remotecommands"))
        sig = w.signature(cmds)
        if self._sigs.get("cmds") == sig:
            return
        self._sigs["cmds"] = sig
        w.clear(self.commands)
        self.commands.append(w.section_title(_("Commands")))
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, column_spacing=8, row_spacing=8
        )
        for c in cmds:
            flow.append(
                w.text_button(
                    c.get("name", c["key"]),
                    "terminal",
                    ["tonal"],
                    lambda k=c["key"]: self.app.plugin(self.device["id"], "remotecommands", "trigger", key=k),
                )
            )
        self.commands.append(flow)

    def _update_files(self) -> None:
        st = self.device["state"].get("sftp", {})
        self.files.set_visible(w.plugin_loaded(self.device, "sftp") and (st.get("mounted") or bool(st.get("error"))))
        sig = w.signature(st)
        if self._sigs.get("sftp") == sig:
            return
        self._sigs["sftp"] = sig
        w.clear(self.files)
        head = Gtk.Box()
        head.append(w.section_title(_("Files")))
        head.append(Gtk.Box(hexpand=True))
        if st.get("mounted"):
            head.append(
                w.text_button(
                    _("Unmount"), "eject", ["flat"], lambda: self.app.plugin(self.device["id"], "sftp", "unmount")
                )
            )
        self.files.append(head)
        if st.get("error"):
            self.files.append(w.label(st["error"], wrap=True, css=["error-text"]))
        for dct in st.get("directories", []):
            self.files.append(
                w.text_button(
                    dct["name"],
                    "folder",
                    ["tonal"],
                    lambda p=dct["path"]: self.app.plugin(self.device["id"], "sftp", "browse", path=p),
                )
            )

    def update(self, d: dict, state: dict) -> None:
        self.device = d
        self.hero_icon.set_icon(w.device_icon(d["type"]))
        self.hero_name.set_label(d["name"])
        pills = []
        if d["reachable"]:
            pills.append(("● " + _("Connected"), "ok"))
        else:
            pills.append((_("Disconnected"), "warn"))
        batt = d["state"].get("battery")
        if batt and batt.get("charge", -1) >= 0:
            pills.append(
                (
                    f"{batt['charge']}%" + (" · " + _("charging") if batt["charging"] else ""),
                    "warn" if batt["charge"] <= 15 and not batt["charging"] else "",
                )
            )
        conn = d["state"].get("connectivity_report")
        if conn and conn.get("strength", -1) >= 0:
            net = conn["networkType"] if conn["networkType"] not in ("", "Unknown") else _("Mobile")
            pills.append((f"{net} · {'▂▄▆█'[: max(1, conn['strength'])]}", ""))
        call = d["state"].get("telephony", {}).get("call")
        if call:
            pills.append(
                ((_("Call") + ": " + call.get("contact", "")) if call.get("contact") else _("In a call"), "ok")
            )
        sig = w.signature(pills)
        if self._sigs.get("pills") != sig:
            self._sigs["pills"] = sig
            self.pills.remove_all()
            for text, kind in pills:
                self.pills.append(w.pill(text, kind))
        self._rebuild_tiles()
        self._update_media()
        self._update_transfers(state)
        self._update_notifications()
        self._update_commands()
        self._update_files()
