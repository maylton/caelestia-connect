"""Device page: pairing screen or the tabs of a paired device."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from . import widgets as w
from .overview import Overview


class PairView(Gtk.Box):
    def __init__(self, app) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        self.app = app
        self.device: dict = {}
        self.icon = w.Icon("smartphone", "huge", filled=True)
        self.append(w.circle(self.icon, "hero-icon"))
        self.title = w.label(xalign=0.5, css=["headline"])
        self.append(self.title)
        self.text = w.label(xalign=0.5, wrap=True, css=["dim"])
        self.text.set_max_width_chars(46)
        self.text.set_justify(Gtk.Justification.CENTER)
        self.append(self.text)
        self.key = w.label(xalign=0.5, css=["verification-key"], selectable=True)
        self.append(self.key)
        self.buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.append(self.buttons)
        self.info = w.label(xalign=0.5, wrap=True, css=["body-small"])
        self.append(self.info)

    def update(self, d: dict) -> None:
        self.device = d
        self.icon.set_icon(w.device_icon(d["type"]))
        self.title.set_label(d["name"])
        w.clear(self.buttons)
        dev = d["id"]
        state = d["pairState"]
        self.key.set_visible(bool(d["verificationKey"]))
        self.key.set_label(d["verificationKey"])
        if state == "requestedByPeer":
            self.text.set_label(
                _("{name} wants to pair with this computer. Make sure the same key is shown on both devices.").format(
                    name=d["name"]
                )
            )
            self.buttons.append(
                w.text_button(_("Reject"), "close", ["tonal"], lambda: self.app.call("device.reject", device=dev))
            )
            self.buttons.append(
                w.text_button(_("Accept"), "check", ["filled"], lambda: self.app.call("device.accept", device=dev))
            )
        elif state == "requested":
            self.text.set_label(
                _("Accept the request on {name}. Make sure the same key is shown on both devices.").format(
                    name=d["name"]
                )
            )
            self.buttons.append(
                w.text_button(_("Cancel"), "close", ["tonal"], lambda: self.app.call("device.reject", device=dev))
            )
        elif not d["reachable"]:
            self.text.set_label(_("This device is not reachable."))
        else:
            self.text.set_label(
                _(
                    "This device is not paired. Pairing lets both devices exchange files, "
                    "notifications, media controls and more."
                )
            )
            self.buttons.append(
                w.text_button(_("Pair"), "link", ["filled"], lambda: self.app.call("device.pair", device=dev))
            )
        err = d.get("pairError") or ""
        info = []
        if err and state == "notPaired":
            info.append(_("Last attempt: {error}").format(error=_(err)))
        if d.get("address"):
            info.append(d["address"])
        self.info.set_label(" · ".join(info))


class DevicePage(Gtk.Box):
    def __init__(self, app, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.window = window
        self.device_id: str | None = None
        self.device: dict = {}

        self.toolbar = Adw.ToolbarView(vexpand=True)
        self.header = Adw.HeaderBar()
        self.view_stack = Adw.ViewStack()
        self.switcher = Adw.ViewSwitcher(stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header.set_title_widget(self.switcher)
        self.toolbar.add_top_bar(self.header)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.view_stack)
        self.toolbar.add_bottom_bar(self.switcher_bar)

        self.overview = Overview(app, self)
        self.view_stack.add_titled_with_icon(self.overview, "overview", _("Overview"), "view-grid-symbolic")
        from .messages import MessagesView
        from .remote import RemoteView
        from .device_settings import DeviceSettings

        self.messages = MessagesView(app, self)
        self.view_stack.add_titled_with_icon(self.messages, "messages", _("Messages"), "mail-unread-symbolic")
        self.remote = RemoteView(app, self)
        self.view_stack.add_titled_with_icon(self.remote, "remote", _("Remote input"), "input-mouse-symbolic")
        self.settings = DeviceSettings(app, self)
        self.view_stack.add_titled_with_icon(self.settings, "settings", _("Settings"), "emblem-system-symbolic")

        self.pair_view = PairView(app)
        self.outer = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.outer.add_named(self.view_stack, "paired")
        pair_tv_content = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        pair_tv_content.set_child(self.pair_view)
        self.outer.add_named(pair_tv_content, "pair")
        self.toolbar.set_content(self.outer)
        self.append(self.toolbar)
        self.narrow = False

    def set_compact(self, compact: bool) -> None:
        self.overview.media_card.set_compact(compact)

    def set_narrow(self, narrow: bool) -> None:
        self.narrow = narrow
        paired = bool(self.device.get("paired"))
        self.switcher.set_visible(paired and not narrow)
        self.switcher_bar.set_reveal(paired and narrow)

    def set_device(self, device_id: str) -> None:
        self.device_id = device_id
        self.view_stack.set_visible_child_name("overview")
        self.messages.reset()

    def show_page(self, page: str) -> None:
        if page in ("overview", "messages", "remote", "settings"):
            self.view_stack.set_visible_child_name(page)
        elif page == "notifications":
            self.view_stack.set_visible_child_name("overview")

    def update(self, d: dict, state: dict) -> None:
        if d["id"] != self.device_id:
            self.set_device(d["id"])
        self.device = d
        if d["paired"]:
            self.outer.set_visible_child_name("paired")
            self.switcher.set_visible(not self.narrow)
            self.switcher_bar.set_reveal(self.narrow)
            self.view_stack.get_page(self.messages).set_visible(w.plugin_loaded(self.device, "sms"))
            self.view_stack.get_page(self.remote).set_visible(
                w.plugin_loaded(self.device, "remotecontrol") or w.plugin_loaded(self.device, "remotekeyboard")
            )
            self.overview.update(d, state)
            self.messages.update(d)
            self.remote.update(d)
            self.settings.update(d, state)
        else:
            self.outer.set_visible_child_name("pair")
            self.switcher.set_visible(False)
            self.switcher_bar.set_reveal(False)
            self.pair_view.update(d)

    def pick_files(self) -> None:
        dlg = Gtk.FileDialog(title=_("Send files to {name}").format(name=self.device.get("name", "")), modal=True)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [f.get_path() for f in files if f.get_path()]
            if paths:
                self.app.plugin(
                    self.device_id,
                    "share",
                    "share_files",
                    paths=paths,
                    callback=lambda n: self.app.toast(_("Sending {n} files").format(n=n)),
                )

        dlg.open_multiple(self.window, None, done)

    def send_text_dialog(self) -> None:
        dlg = Adw.AlertDialog(
            heading=_("Send link or text"), body=_("Links open on the device; text is copied to its clipboard.")
        )
        view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8
        )
        view.set_size_request(320, 100)
        frame = Gtk.ScrolledWindow(min_content_height=100)
        frame.set_child(view)
        frame.add_css_class("card")
        dlg.set_extra_child(frame)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("send", _("Send"))
        dlg.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)

        def done(_d, resp):
            buf = view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            if resp != "send" or not text:
                return
            if "\n" not in text and text.startswith(("http://", "https://", "mailto:", "tel:", "geo:")):
                self.app.plugin(
                    self.device_id, "share", "share_url", url=text, callback=lambda _r: self.app.toast(_("Link sent"))
                )
            else:
                self.app.plugin(
                    self.device_id, "share", "share_text", text=text, callback=lambda _r: self.app.toast(_("Text sent"))
                )

        dlg.connect("response", done)
        dlg.present(self.window)

    def reply_dialog(self, n: dict) -> None:
        def send(message: str) -> None:
            self.app.plugin(
                self.device_id,
                "notifications",
                "reply",
                id=n["id"],
                message=message,
                callback=lambda _r: self.app.toast(_("Reply sent")),
            )

        w.prompt(
            self.window,
            _("Reply to {name}").format(name=n["title"] or n["appName"]),
            n["text"][:300],
            _("Send"),
            send,
            placeholder=_("Message"),
        )
