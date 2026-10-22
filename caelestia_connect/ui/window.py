"""Main window: device list on the side, the selected device on the right."""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gio, Gtk

from ..i18n import _
from . import widgets as w
from .device_page import DevicePage


class DeviceRow(Gtk.ListBoxRow):
    def __init__(self, device: dict) -> None:
        super().__init__()
        self.device_id = device["id"]
        box = Gtk.Box(spacing=12)
        self.icon = w.Icon("smartphone", "medium", filled=True)
        self.avatar = w.circle(self.icon)
        box.append(self.avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.name = w.label(css=["title-medium"], ellipsize=True)
        self.status = w.label(css=["body-small"], ellipsize=True)
        texts.append(self.name)
        texts.append(self.status)
        box.append(texts)
        self.badge = w.Icon("", "small", css=["primary-text"])
        box.append(self.badge)
        self.set_child(box)
        self.update(device)

    def update(self, d: dict) -> None:
        self.icon.set_icon(w.device_icon(d["type"]))
        self.name.set_label(d["name"])
        for c in ("connected", "offline"):
            self.avatar.remove_css_class(c)
        self.avatar.add_css_class(
            "connected" if d["reachable"] and d["paired"] else "offline" if not d["reachable"] else "x"
        )
        batt = d["state"].get("battery", {})
        if d["pairState"] == "requestedByPeer":
            status = _("Wants to pair")
        elif d["pairState"] == "requested":
            status = _("Waiting for the device…")
        elif not d["paired"]:
            status = _("Available to pair")
        elif not d["reachable"]:
            status = _("Disconnected")
        elif batt.get("charge", -1) >= 0:
            status = _("Connected · {charge}%").format(charge=batt["charge"]) + (" ⚡" if batt.get("charging") else "")
        else:
            status = _("Connected")
        self.status.set_label(status)
        unread = d["state"].get("sms", {}).get("unread", 0)
        self.badge.set_icon(
            "priority_high" if d["pairState"] == "requestedByPeer" else ("mark_chat_unread" if unread else "")
        )


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app) -> None:
        super().__init__(application=app, title=_("Caelestia Connect"))
        self.app = app
        self.add_css_class("caelestia")
        self.set_default_size(1100, 760)
        self.set_size_request(360, 480)
        self.rows: dict[str, DeviceRow] = {}
        self.selected: str | None = None
        self._device_order: list[str] = []

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=280, max_sidebar_width=340, sidebar_width_fraction=0.28)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.connect("apply", lambda *_: self.device_page.set_narrow(True))
        bp.connect("unapply", lambda *_: self.device_page.set_narrow(False))
        self.add_breakpoint(bp)
        bp_small = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp_small.add_setter(self.split, "collapsed", True)
        bp_small.connect("apply", lambda *_: self.device_page.set_compact(True))
        bp_small.connect("unapply", lambda *_: self.device_page.set_compact(False))
        self.add_breakpoint(bp_small)

        self.split.set_sidebar(self._build_sidebar())
        self.content_page = Adw.NavigationPage(title=_("Devices"), tag="content")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = self._build_empty()
        self.stack.add_named(self.empty, "empty")
        self.device_page = DevicePage(app, self)
        self.stack.add_named(self.device_page, "device")
        self.content_page.set_child(self.stack)
        self.split.set_content(self.content_page)

        app.listeners.append(self.on_state)
        self.connect(
            "destroy", lambda *_: app.listeners.remove(self.on_state) if self.on_state in app.listeners else None
        )
        self.on_state(app.state)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _build_sidebar(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title=_("Connect"), tag="sidebar")
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(w.label(_("Connect"), css=["title-medium"]))
        refresh = w.icon_button("refresh", _("Search for devices"), callback=lambda: self.app.call("refresh"))
        hb.pack_start(refresh)
        menu = Gio.Menu()
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("Add device by IP"), "win.add-ip")
        menu.append(_("About"), "app.about")
        menu.append(_("Quit"), "app.quit")
        mb = Gtk.MenuButton(menu_model=menu)
        mb.set_child(w.Icon("more_vert", "medium"))
        mb.add_css_class("flat")
        mb.add_css_class("round")
        hb.pack_end(mb)
        act = Gio.SimpleAction.new("add-ip", None)
        act.connect("activate", lambda *_: self.add_ip_dialog())
        self.add_action(act)
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=4, margin_bottom=12)
        me = Gtk.Box(spacing=12, margin_start=16, margin_end=16)
        me.add_css_class("surface-card")
        ic = w.Icon("desktop_windows", "medium", filled=True)
        av = w.circle(ic)
        av.add_css_class("connected")
        self.self_icon = ic
        me.append(av)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.self_name = w.label(css=["title-medium"], ellipsize=True)
        self.self_status = w.label(css=["body-small"], ellipsize=True)
        col.append(self.self_name)
        col.append(self.self_status)
        me.append(col)
        me.append(w.icon_button("edit", _("Rename this computer"), callback=self.rename_dialog))
        body.append(me)

        devices_title = w.section_title(_("Devices"))
        devices_title.set_margin_start(24)
        body.append(devices_title)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.add_css_class("devices")
        self.list.connect("row-selected", self._row_selected)
        self.list.set_placeholder(self._sidebar_placeholder())
        body.append(self.list)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(body)
        tv.set_content(scroller)
        page.set_child(tv)
        return page

    def _sidebar_placeholder(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=24, margin_start=24, margin_end=24)
        box.append(w.Icon("devices_other", "large", css=["dim"]))
        box.append(w.label(_("No devices found"), xalign=0.5, css=["title-medium"]))
        box.append(
            w.label(
                _("Open KDE Connect on your phone, on the same network."), xalign=0.5, wrap=True, css=["body-small"]
            )
        )
        return box

    def _build_empty(self) -> Gtk.Widget:
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
            margin_start=32,
            margin_end=32,
        )
        self.empty_icon = w.Icon("phonelink", "huge", filled=True)
        box.append(w.circle(self.empty_icon, "hero-icon"))
        self.empty_title = w.label(_("Connect your phone"), xalign=0.5, css=["headline"])
        box.append(self.empty_title)
        self.empty_text = w.label(
            _(
                "Install KDE Connect on your Android phone, open it while connected to the same network "
                "and this computer will show up there. Then pair from either side."
            ),
            xalign=0.5,
            wrap=True,
            css=["dim"],
        )
        self.empty_text.set_max_width_chars(48)
        self.empty_text.set_justify(Gtk.Justification.CENTER)
        box.append(self.empty_text)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        btns.append(w.text_button(_("Search again"), "refresh", ["filled"], lambda: self.app.call("refresh")))
        btns.append(w.text_button(_("Add by IP"), "lan", ["tonal"], self.add_ip_dialog))
        box.append(btns)
        tv.set_content(box)
        return tv

    def on_state(self, state: dict) -> None:
        if not state:
            self.self_name.set_label(_("Connecting to the service…"))
            self.self_status.set_label("")
            self.empty_title.set_label(_("Starting Caelestia Connect…"))
            self.stack.set_visible_child_name("empty")
            return
        me = state["self"]
        self.self_name.set_label(me["name"])
        self.self_status.set_label(_("Visible as {name}").format(name=me["name"]))
        self.self_icon.set_icon(w.device_icon(me["type"]))
        self.empty_title.set_label(_("Connect your phone"))
        devices = state["devices"]
        order = [d["id"] for d in devices]
        if order != self._device_order:
            self._device_order = order
            for row in list(self.rows.values()):
                self.list.remove(row)
            self.rows = {}
            for d in devices:
                row = DeviceRow(d)
                self.rows[d["id"]] = row
                self.list.append(row)
            if self.selected in self.rows:
                self.list.select_row(self.rows[self.selected])
        else:
            for d in devices:
                self.rows[d["id"]].update(d)
        if self.selected is None and devices:
            target = next((d for d in devices if d["paired"] and d["reachable"]), devices[0])
            self.list.select_row(self.rows[target["id"]])
        dev = next((d for d in devices if d["id"] == self.selected), None)
        if dev is None:
            self.stack.set_visible_child_name("empty")
        else:
            self.device_page.update(dev, state)
            self.stack.set_visible_child_name("device")

    def _row_selected(self, _list, row: DeviceRow | None) -> None:
        if row is None:
            return
        changed = self.selected != row.device_id
        self.selected = row.device_id
        dev = self.app.device(row.device_id)
        if dev:
            if changed:
                self.device_page.set_device(row.device_id)
            self.device_page.update(dev, self.app.state)
            self.stack.set_visible_child_name("device")
            self.content_page.set_title(dev["name"])
        self.split.set_show_content(True)

    def show_device(self, device_id: str | None, page: str | None) -> None:
        if device_id and device_id in self.rows:
            self.list.select_row(self.rows[device_id])
        if page:
            self.device_page.show_page(page)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def rename_dialog(self) -> None:
        name = self.app.state.get("self", {}).get("name", "")
        w.prompt(
            self,
            _("Rename this computer"),
            _("This is the name your devices will see."),
            _("Rename"),
            lambda v: self.app.call("settings.setName", name=v),
            initial=name,
        )

    def add_ip_dialog(self) -> None:
        def add(address: str) -> None:
            self.app.call(
                "devices.add", address=address, callback=lambda _r: self.toast(_("Trying {ip}…").format(ip=address))
            )

        w.prompt(
            self,
            _("Add device by IP"),
            _("Use this when the device is not found automatically (eg. different subnet or blocked broadcasts)."),
            _("Add"),
            add,
        )

    def show_preferences(self) -> None:
        from .dialogs import PreferencesDialog

        PreferencesDialog(self.app).present(self)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        files = [f.get_path() for f in value.get_files() if f.get_path()]
        dev = self.app.device(self.selected) if self.selected else None
        if not files or not dev or not dev["paired"]:
            return False
        self.app.plugin(
            dev["id"],
            "share",
            "share_files",
            paths=files,
            callback=lambda n: self.toast(_("Sending {n} files to {device}").format(n=n, device=dev["name"])),
        )
        return True
