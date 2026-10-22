"""Standalone windows (quick reply, share target) and the preferences dialog."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from . import widgets as w


class ReplyWindow(Adw.Window):
    """Small window opened from the "Reply" action of a phone notification."""

    def __init__(self, app, device: dict, notif_id: str, notif: dict | None) -> None:
        super().__init__(application=app, title=_("Reply"), default_width=420, modal=False)
        self.add_css_class("caelestia")
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=20, margin_end=20, margin_bottom=20
        )
        title = (notif or {}).get("title") or (notif or {}).get("appName") or device["name"]
        head = Gtk.Box(spacing=12)
        if notif and notif.get("icon"):
            img = Gtk.Image.new_from_file(notif["icon"])
            img.set_pixel_size(40)
            head.append(img)
        else:
            head.append(w.Icon("chat", "large", css=["primary-text"]))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(w.label(title, css=["title-large"], ellipsize=True))
        col.append(w.label(_("via {device}").format(device=device["name"]), css=["body-small"]))
        head.append(col)
        box.append(head)
        if notif and notif.get("text"):
            t = w.label(notif["text"], wrap=True, css=["dim"])
            t.set_lines(6)
            box.append(w.card(t))
        self.entry = Gtk.Entry(placeholder_text=_("Message"), activates_default=True)
        self.entry.add_css_class("composer")
        box.append(self.entry)
        send = w.text_button(_("Send"), "send", ["filled"])
        send.set_halign(Gtk.Align.END)
        box.append(send)
        tv.set_content(box)
        self.set_content(tv)

        def do_send(*_a):
            text = self.entry.get_text().strip()
            if not text:
                return
            app.plugin(device["id"], "notifications", "reply", id=notif_id, message=text)
            self.close()

        send.connect("clicked", do_send)
        self.entry.connect("activate", do_send)
        self.entry.grab_focus()


class ShareWindow(Adw.Window):
    """Choose a device to send files to (used by "Open with" in file managers)."""

    def __init__(self, app, files: list[str], device_id: str | None) -> None:
        super().__init__(application=app, title=_("Send to device"), default_width=380)
        self.add_css_class("caelestia")
        self.app = app
        self.files = files
        devices = [d for d in app.state.get("devices", []) if d["paired"] and d["reachable"]]
        if device_id and files:
            target = next((d for d in devices if d["id"] == device_id), None)
            if target:
                GLib.idle_add(lambda: (self._send(target), False)[1])
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=16, margin_end=16, margin_bottom=16
        )
        n = len(files)
        box.append(
            w.label(
                _("Send {n} files to:").format(n=n)
                if n != 1
                else _("Send {name} to:").format(name=files[0].rsplit("/", 1)[-1] if files else ""),
                css=["title-medium"],
                wrap=True,
            )
        )
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        for d in devices:
            row = Adw.ActionRow(title=d["name"], activatable=True)
            row.add_prefix(w.Icon(w.device_icon(d["type"]), "medium", filled=True, css=["primary-text"]))
            row.connect("activated", lambda _r, d=d: self._send(d))
            lst.append(row)
        if not devices:
            box.append(w.label(_("No connected device"), css=["dim"]))
        box.append(lst)
        tv.set_content(box)
        self.set_content(tv)

    def _send(self, d: dict) -> None:
        if self.files:
            self.app.plugin(d["id"], "share", "share_files", paths=self.files)
        self.close()


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app) -> None:
        super().__init__(title=_("Preferences"))
        self.app = app
        st = app.state
        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")
        self.add(page)

        g = Adw.PreferencesGroup(title=_("This computer"))
        page.add(g)
        name = Adw.EntryRow(title=_("Device name"), text=st.get("self", {}).get("name", ""), show_apply_button=True)
        name.connect("apply", lambda r: app.call("settings.setName", name=r.get_text()))
        g.add(name)
        dl = Adw.ActionRow(title=_("Received files"), subtitle=st.get("settings", {}).get("incomingPath", ""))
        btn = w.icon_button("folder_open", _("Choose"))
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._pick_folder(dl))
        dl.add_suffix(btn)
        g.add(dl)
        fp = Adw.ActionRow(
            title=_("Certificate (SHA-256)"),
            subtitle=st.get("self", {}).get("fingerprint", ""),
            subtitle_selectable=True,
        )
        fp.add_css_class("property")
        g.add(fp)

        g2 = Adw.PreferencesGroup(
            title=_("Devices by IP address"),
            description=_("Devices that are contacted directly, for networks that block discovery."),
        )
        page.add(g2)
        for addr in st.get("settings", {}).get("customDevices", []):
            row = Adw.ActionRow(title=addr)
            rm = w.icon_button("delete", _("Remove"))
            rm.set_valign(Gtk.Align.CENTER)
            rm.connect("clicked", lambda *_a, a=addr, r=row: (app.call("devices.remove", address=a), g2.remove(r)))
            row.add_suffix(rm)
            g2.add(row)
        add = Adw.EntryRow(title=_("Add an IP address"), show_apply_button=True)
        add.connect("apply", lambda r: app.call("devices.add", address=r.get_text()))
        g2.add(add)

        caps = st.get("capabilities", {})
        g3 = Adw.PreferencesGroup(title=_("System integration"))
        page.add(g3)
        checks = [
            (
                _("Virtual input (/dev/uinput)"),
                caps.get("uinput"),
                caps.get("uinputError") or _("Needed to control this computer from the phone"),
            ),
            (_("File browsing (sshfs)"), caps.get("sshfs"), _("Install with: sudo pacman -S sshfs")),
            (_("Clipboard (wl-clipboard)"), caps.get("clipboard"), _("Install with: sudo pacman -S wl-clipboard")),
            (_("Audio control (pactl)"), caps.get("audio"), _("Install with: sudo pacman -S libpulse")),
        ]
        for title, ok, hint in checks:
            row = Adw.ActionRow(title=title, subtitle=_("Available") if ok else hint)
            row.add_prefix(
                w.Icon(
                    "check_circle" if ok else "error",
                    "medium",
                    filled=True,
                    css=["primary-text" if ok else "error-text"],
                )
            )
            g3.add(row)
        fw = Adw.ActionRow(
            title=_("Firewall"),
            subtitle=_(
                "Ports 1714-1764 (TCP and UDP) must be open, eg: "
                "sudo ufw allow 1714:1764/udp && sudo ufw allow 1714:1764/tcp"
            ),
            subtitle_selectable=True,
        )
        fw.add_prefix(w.Icon("shield", "medium", filled=True, css=["dim"]))
        g3.add(fw)

    def _pick_folder(self, row: Adw.ActionRow) -> None:
        dlg = Gtk.FileDialog(title=_("Received files"))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.app.call("settings.setIncomingPath", path=f.get_path())
                row.set_subtitle(f.get_path())

        dlg.select_folder(self.get_root(), None, done)
