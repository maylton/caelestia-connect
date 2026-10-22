"""SMS conversations."""

from __future__ import annotations

import base64
import time

from gi.repository import Adw, Gdk, GLib, Gtk

from ..i18n import _
from . import widgets as w


def _when(ms: int) -> str:
    t = ms / 1000
    now = time.time()
    if now - t < 86400 and time.localtime(t).tm_mday == time.localtime(now).tm_mday:
        return time.strftime("%H:%M", time.localtime(t))
    if now - t < 7 * 86400:
        return time.strftime("%a %H:%M", time.localtime(t))
    return time.strftime("%d/%m/%Y", time.localtime(t))


class ConversationRow(Gtk.ListBoxRow):
    def __init__(self, c: dict) -> None:
        super().__init__()
        self.conv = c
        box = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        initial = (c["title"][:1] or "?").upper()
        lb = (
            w.label(initial, xalign=0.5, css=["title-medium"])
            if initial.isalpha()
            else w.Icon("person", "medium", filled=True)
        )
        av = w.circle(lb)
        if not c["read"]:
            av.add_css_class("connected")
        box.append(av)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=6)
        name = w.label(c["title"], css=["title-medium"] if not c["read"] else [], ellipsize=True)
        name.set_hexpand(True)
        top.append(name)
        top.append(w.label(_when(c["date"]), css=["body-small"]))
        col.append(top)
        col.append(w.label(c["snippet"].replace("\n", " "), css=["body-small"], ellipsize=True))
        box.append(col)
        self.set_child(box)


class MessagesView(Gtk.Box):
    def __init__(self, app, page) -> None:
        super().__init__()
        self.app = app
        self.page = page
        self.device: dict = {}
        self.thread: str | None = None
        self.thread_addresses: list[str] = []
        self.thread_sub = None
        self._version = -1
        self._conv_sig = ""
        self._msg_sig = ""
        self._filter = ""

        self.split = Adw.NavigationSplitView(
            min_sidebar_width=260, max_sidebar_width=360, sidebar_width_fraction=0.36, hexpand=True
        )
        self.append(self.split)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_start=8, margin_end=8)
        top = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search conversations"), hexpand=True)
        self.search.connect("search-changed", self._search_changed)
        top.append(self.search)
        top.append(w.icon_button("edit_square", _("New message"), ["tonal"], callback=self.new_message))
        top.append(w.icon_button("refresh", _("Refresh"), callback=self._refresh))
        left.append(top)
        self.conv_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.conv_list.add_css_class("navigation-sidebar")
        self.conv_list.connect("row-selected", self._conv_selected)
        ph = w.label(
            _("No conversations yet. Make sure the SMS permission is granted in KDE Connect on the phone."),
            xalign=0.5,
            wrap=True,
            css=["body-small"],
        )
        ph.set_margin_top(24)
        self.conv_list.set_placeholder(ph)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.conv_list)
        left.append(sc)
        self.split.set_sidebar(Adw.NavigationPage(title=_("Conversations"), child=left))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=12, margin_start=16, margin_end=16, margin_top=8, margin_bottom=8)
        self.back = w.icon_button("arrow_back", _("Back"), callback=lambda: self.split.set_show_content(False))
        head.append(self.back)
        self.thread_title = w.label(css=["title-large"], ellipsize=True)
        self.thread_title.set_hexpand(True)
        head.append(self.thread_title)
        self.thread_number = w.label(css=["body-small"])
        head.append(self.thread_number)
        right.append(head)
        self.msg_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760)
        self.msg_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16, margin_bottom=12
        )
        clamp.set_child(self.msg_box)
        self.msg_scroll.set_child(clamp)
        right.append(self.msg_scroll)
        comp = Gtk.Box(spacing=8, margin_start=16, margin_end=16, margin_bottom=12, margin_top=4)
        self.attach_btn = w.icon_button("attach_file", _("Attach"), callback=self._attach)
        comp.append(self.attach_btn)
        self.entry = Gtk.Entry(placeholder_text=_("Text message"), hexpand=True)
        self.entry.add_css_class("composer")
        self.entry.connect("activate", lambda *_: self._send())
        comp.append(self.entry)
        comp.append(w.icon_button("send", _("Send"), ["filled"], callback=self._send))
        self.attachments: list[str] = []
        self.attach_label = w.label(css=["body-small"])
        right.append(self.attach_label)
        right.append(comp)
        self.empty_thread = w.label(_("Choose a conversation"), xalign=0.5, css=["dim"])
        self.thread_stack = Gtk.Stack()
        self.thread_stack.add_named(self.empty_thread, "empty")
        self.thread_stack.add_named(right, "thread")
        self.split.set_content(Adw.NavigationPage(title=_("Messages"), child=self.thread_stack))
        self.split.connect("notify::collapsed", lambda *_: self.back.set_visible(self.split.get_collapsed()))
        self.back.set_visible(False)

    def reset(self) -> None:
        self.thread = None
        self._version = -1
        self._conv_sig = ""
        self.thread_stack.set_visible_child_name("empty")

    def _refresh(self) -> None:
        if self.device:
            self.app.plugin(self.device["id"], "sms", "request_conversations")
            if self.thread:
                self.app.plugin(self.device["id"], "sms", "request_conversation", thread_id=self.thread)

    def _search_changed(self, entry) -> None:
        self._filter = entry.get_text().lower()
        self.conv_list.invalidate_filter()

    def update(self, d: dict) -> None:
        self.device = d
        st = d["state"].get("sms")
        if not st:
            return
        if st["version"] == self._version:
            return
        self._version = st["version"]
        self.app.plugin(d["id"], "sms", "conversations", callback=self._got_conversations)
        if self.thread:
            self._load_thread()

    def _got_conversations(self, convs: list) -> None:
        sig = str([(c["threadId"], c["date"], c["read"]) for c in convs])
        if sig == self._conv_sig:
            return
        self._conv_sig = sig
        selected = self.thread
        self.conv_list.remove_all()
        self.conv_list.set_filter_func(
            lambda row: (
                not self._filter
                or self._filter
                in (row.conv["title"] + " " + " ".join(row.conv["addresses"]) + " " + row.conv["snippet"]).lower()
            )
        )
        for c in convs:
            row = ConversationRow(c)
            self.conv_list.append(row)
            if c["threadId"] == selected:
                self.conv_list.select_row(row)

    def _conv_selected(self, _lb, row) -> None:
        if row is None:
            return
        c = row.conv
        if self.thread != c["threadId"]:
            self.thread = c["threadId"]
            self._msg_sig = ""
            self.app.plugin(self.device["id"], "sms", "request_conversation", thread_id=self.thread, count=50)
        self.thread_addresses = c["addresses"]
        self.thread_sub = c.get("subId")
        self.thread_title.set_label(c["title"])
        self.thread_number.set_label(", ".join(c["addresses"]) if c["title"] not in c["addresses"] else "")
        self.thread_stack.set_visible_child_name("thread")
        self.split.set_show_content(True)
        self._load_thread()

    def _load_thread(self) -> None:
        tid = self.thread
        self.app.plugin(
            self.device["id"], "sms", "messages", thread_id=tid, callback=lambda msgs: self._show_messages(tid, msgs)
        )

    def _show_messages(self, tid: str, msgs: list) -> None:
        if tid != self.thread:
            return
        sig = str([(m["date"], m["outgoing"], len(m["body"]), [a["path"] for a in m["attachments"]]) for m in msgs])
        if sig == self._msg_sig:
            return
        self._msg_sig = sig
        w.clear(self.msg_box)
        last_day = ""
        group = len(self.thread_addresses) > 1
        for m in msgs[-300:]:
            day = time.strftime("%d/%m/%Y", time.localtime(m["date"] / 1000))
            if day != last_day:
                last_day = day
                self.msg_box.append(w.label(day, xalign=0.5, css=["body-small"]))
            self.msg_box.append(self._bubble(m, group))
        GLib.timeout_add(80, self._scroll_bottom)

    def _scroll_bottom(self) -> bool:
        adj = self.msg_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _bubble(self, m: dict, group: bool) -> Gtk.Widget:
        outer = Gtk.Box(halign=Gtk.Align.END if m["outgoing"] else Gtk.Align.START)
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        b.add_css_class("bubble")
        if m["outgoing"]:
            b.add_css_class("out")
        if group and not m["outgoing"]:
            b.append(w.label(m["senderName"], css=["label-caps"]))
        for a in m["attachments"]:
            b.append(self._attachment(a))
        if m["body"]:
            body = w.label(m["body"], wrap=True, selectable=True)
            body.set_max_width_chars(48)
            b.append(body)
        b.append(w.label(time.strftime("%H:%M", time.localtime(m["date"] / 1000)), xalign=1, css=["body-small"]))
        outer.append(b)
        return outer

    def _attachment(self, a: dict) -> Gtk.Widget:
        if a["path"] and a["mimeType"].startswith("image/"):
            pic = Gtk.Picture.new_for_filename(a["path"])
            pic.set_size_request(220, 160)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            return pic
        if a["thumbnail"]:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(a["thumbnail"])))
                pic = Gtk.Picture.new_for_paintable(tex)
                pic.set_size_request(160, 120)
                btn = Gtk.Button(child=pic, css_classes=["flat"], tooltip_text=_("Download"))
                btn.connect(
                    "clicked",
                    lambda *_: self.app.plugin(
                        self.device["id"], "sms", "get_attachment", part_id=a["partId"], unique_id=a["uniqueId"]
                    ),
                )
                return btn
            except (GLib.Error, ValueError):
                pass
        return w.text_button(
            a["mimeType"] or _("Attachment"),
            "attach_file",
            ["tonal"],
            lambda: self.app.plugin(
                self.device["id"], "sms", "get_attachment", part_id=a["partId"], unique_id=a["uniqueId"]
            ),
        )

    def _attach(self) -> None:
        dlg = Gtk.FileDialog(title=_("Attach files"))

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.attachments += [f.get_path() for f in files if f.get_path()]
            self.attach_label.set_label("📎 " + ", ".join(p.rsplit("/", 1)[-1] for p in self.attachments))

        dlg.open_multiple(self.get_root(), None, done)

    def _send(self) -> None:
        text = self.entry.get_text().strip()
        if not self.thread_addresses or (not text and not self.attachments):
            return
        sub = self.thread_sub if isinstance(self.thread_sub, int) else -1
        self.app.plugin(
            self.device["id"],
            "sms",
            "send_sms",
            addresses=self.thread_addresses,
            message=text,
            attachments=self.attachments,
            sub_id=sub,
            callback=lambda ok: None if ok else self.app.toast(_("Could not send the message")),
        )
        self.entry.set_text("")
        self.attachments = []
        self.attach_label.set_label("")

    def new_message(self) -> None:
        dlg = Adw.Dialog(title=_("New message"), content_width=420, content_height=520)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=16, margin_end=16, margin_bottom=16
        )
        to = Gtk.Entry(placeholder_text=_("Phone number or contact"))
        box.append(to)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(lst)
        box.append(sc)
        msg = Gtk.Entry(placeholder_text=_("Text message"))
        msg.add_css_class("composer")
        box.append(msg)
        send = w.text_button(_("Send"), "send", ["filled"])
        box.append(send)
        tv.set_content(box)
        dlg.set_child(tv)
        contacts: list = []

        def fill(_e=None):
            q = to.get_text().lower()
            lst.remove_all()
            shown = 0
            for c in contacts:
                if q and q not in c["name"].lower() and not any(q in p for p in c["phones"]):
                    continue
                for p in c["phones"]:
                    row = Adw.ActionRow(title=c["name"] or p, subtitle=p, activatable=True)
                    row.connect("activated", lambda _r, p=p: to.set_text(p))
                    lst.append(row)
                    shown += 1
                if shown > 60:
                    break

        def got(cs):
            contacts.extend(cs or [])
            fill()

        to.connect("changed", fill)
        if w.plugin_loaded(self.device, "contacts"):
            self.app.plugin(self.device["id"], "contacts", "list_contacts", callback=got)

        def do_send(*_a):
            number, text = to.get_text().strip(), msg.get_text().strip()
            if number and text:
                self.app.plugin(
                    self.device["id"],
                    "sms",
                    "send_sms",
                    addresses=[number],
                    message=text,
                    callback=lambda _r: self.app.toast(_("Message sent")),
                )
                dlg.close()

        send.connect("clicked", do_send)
        msg.connect("activate", do_send)
        dlg.present(self.get_root())
