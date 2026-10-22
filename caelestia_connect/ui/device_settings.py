"""Per-device settings: plugins, their options and device information."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from . import widgets as w
from .controls import switch_row


class DeviceSettings(Adw.PreferencesPage):
    def __init__(self, app, page) -> None:
        super().__init__()
        self.app = app
        self.page = page
        self.device: dict = {}
        self._sig = ""
        self._groups: list[Adw.PreferencesGroup] = []
        self._expanded: set[str] = set()

    def update(self, d: dict, _state: dict) -> None:
        self.device = d
        sig = w.signature([d["id"], d["name"], d["address"], d["plugins"]])
        if sig == self._sig:
            return
        self._sig = sig
        for g in self._groups:
            self.remove(g)
        self._groups = []
        self._build()

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.add(g)
        self._groups.append(g)
        return g

    def _set(self, plugin: str, key: str, value) -> None:
        self.app.call("device.setPluginSetting", device=self.device["id"], plugin=plugin, key=key, value=value)

    def _build(self) -> None:
        d = self.device
        dev = d["id"]
        info = self._group(_("Device"))
        for title, value in (
            (_("Name"), d["name"]),
            (_("Address"), d["address"] or "—"),
            (_("Protocol version"), str(d["protocolVersion"])),
            (_("Certificate (SHA-256)"), d["fingerprint"]),
        ):
            row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True)
            row.add_css_class("property")
            info.add(row)
        unpair = Adw.ButtonRow(title=_("Unpair"), start_icon_name="edit-delete-symbolic")
        unpair.add_css_class("destructive-action")
        unpair.connect("activated", lambda *_: self._confirm_unpair())
        info.add(unpair)

        plugins = self._group(
            _("Features"),
            _("Choose what this device can do. Some features also need permissions in KDE Connect on the phone."),
        )
        for p in sorted(d["plugins"], key=lambda p: p["name"].lower()):
            arrow = None
            if p["settings"]:
                # The options arrow sits right before the switch, so every switch stays aligned
                arrow = Gtk.ToggleButton(
                    active=p["id"] in self._expanded, valign=Gtk.Align.CENTER, tooltip_text=_("Options")
                )
                arrow.set_child(w.Icon("expand_more", "medium"))
                arrow.add_css_class("flat")
                arrow.add_css_class("round")
                arrow.add_css_class("expand-arrow")
                if arrow.get_active():
                    arrow.add_css_class("expanded")
            row, _sw = switch_row(
                self.app.theme,
                p["name"],
                p["description"],
                p["enabled"],
                on_toggle=lambda on, pid=p["id"]: self._toggle(pid, on),
                before_switch=arrow,
            )
            plugins.add(row)
            if arrow is None:
                continue
            options = Gtk.ListBox(
                selection_mode=Gtk.SelectionMode.NONE, margin_start=16, margin_end=16, margin_top=2, margin_bottom=6
            )
            options.add_css_class("boxed-list")
            options.add_css_class("plugin-options")
            for s_ in p["settings"]:
                widget = self._setting_row(p["id"], s_, p["values"].get(s_["key"], s_.get("default")))
                if widget is not None:
                    options.append(widget)
            if p["id"] == "sendnotifications":
                test = Adw.ButtonRow(title=_("Send a test notification"))
                test.connect("activated", lambda *_: self.app.plugin(dev, "sendnotifications", "test"))
                options.append(test)
            revealer = Gtk.Revealer(
                child=options, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, reveal_child=arrow.get_active()
            )
            holder = Gtk.ListBoxRow(activatable=False, selectable=False, child=revealer)
            holder.add_css_class("plugin-options-row")
            holder.set_visible(revealer.get_reveal_child())

            def toggled(btn, rev=revealer, hold=holder, pid=p["id"]):
                open_ = btn.get_active()
                if open_:
                    self._expanded.add(pid)
                    btn.add_css_class("expanded")
                    hold.set_visible(True)
                else:
                    self._expanded.discard(pid)
                    btn.remove_css_class("expanded")
                rev.set_reveal_child(open_)

            revealer.connect(
                "notify::child-revealed",
                lambda rev, _p, hold=holder: hold.set_visible(rev.get_reveal_child() or rev.get_child_revealed()),
            )
            arrow.connect("toggled", toggled)
            plugins.add(holder)

    def _toggle(self, plugin: str, enabled: bool) -> None:
        cur = next((p for p in self.device["plugins"] if p["id"] == plugin), None)
        if cur and cur["enabled"] != enabled:
            self.app.call("device.setPluginEnabled", device=self.device["id"], plugin=plugin, enabled=enabled)

    def _setting_row(self, plugin: str, s: dict, value) -> Gtk.Widget | None:
        t = s["type"]
        if t == "bool":
            row, _sw = switch_row(
                self.app.theme, s["label"], "", bool(value), on_toggle=lambda on: self._set(plugin, s["key"], on)
            )
            return row
        if t == "choice":
            labels = [c[1] for c in s["choices"]]
            values = [c[0] for c in s["choices"]]
            row = Adw.ComboRow(title=s["label"], model=Gtk.StringList.new(labels))
            if value in values:
                row.set_selected(values.index(value))
            row.connect("notify::selected", lambda r, _p: self._set(plugin, s["key"], values[r.get_selected()]))
            return row
        if t in ("str", "text"):
            row = Adw.EntryRow(title=s["label"], text=str(value or ""), show_apply_button=True)
            if t == "text":
                row.set_title(s["label"] + " " + _("(separate with commas)"))
                row.set_text(", ".join(line for line in str(value or "").splitlines() if line))
                row.connect(
                    "apply",
                    lambda r: self._set(
                        plugin, s["key"], "\n".join(x.strip() for x in r.get_text().split(",") if x.strip())
                    ),
                )
            else:
                row.connect("apply", lambda r: self._set(plugin, s["key"], r.get_text()))
            return row
        if t in ("folder", "file"):
            row = Adw.ActionRow(title=s["label"], subtitle=str(value or _("Default")))
            btn = w.icon_button("folder_open" if t == "folder" else "audio_file", _("Choose"))
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", lambda *_: self._choose(plugin, s["key"], t == "folder", row))
            row.add_suffix(btn)
            if value:
                reset = w.icon_button("restart_alt", _("Default"))
                reset.set_valign(Gtk.Align.CENTER)
                reset.connect("clicked", lambda *_: self._set(plugin, s["key"], ""))
                row.add_suffix(reset)
            return row
        if t == "commands":
            return self._commands_editor(plugin, value or {})
        return None

    def _choose(self, plugin: str, key: str, folder: bool, row: Adw.ActionRow) -> None:
        dlg = Gtk.FileDialog(title=row.get_title())

        def done(d, res):
            try:
                f = d.select_folder_finish(res) if folder else d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._set(plugin, key, f.get_path())

        if folder:
            dlg.select_folder(self.get_root(), None, done)
        else:
            dlg.open(self.get_root(), None, done)

    def _commands_editor(self, plugin: str, commands: dict) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        box.append(
            w.label(
                _("Commands the device can run on this computer. They run with /bin/sh."), wrap=True, css=["body-small"]
            )
        )
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        dev = self.device["id"]
        for key, c in commands.items():
            if not isinstance(c, dict):
                continue
            row = Adw.ActionRow(title=c.get("name", key), subtitle=c.get("command", ""), subtitle_lines=2)
            rm = w.icon_button("delete", _("Remove"))
            rm.set_valign(Gtk.Align.CENTER)
            rm.connect("clicked", lambda *_a, k=key: self.app.plugin(dev, "runcommand", "remove_command", key=k))
            row.add_suffix(rm)
            lst.append(row)
        box.append(lst)
        add = Gtk.Box(spacing=8)
        name = Gtk.Entry(placeholder_text=_("Name"), width_chars=12)
        cmd = Gtk.Entry(placeholder_text=_("Command"), hexpand=True)
        add.append(name)
        add.append(cmd)

        def do_add(*_a):
            if name.get_text().strip() and cmd.get_text().strip():
                self.app.plugin(
                    dev, "runcommand", "add_command", name=name.get_text().strip(), command=cmd.get_text().strip()
                )

        cmd.connect("activate", do_add)
        add.append(w.icon_button("add", _("Add"), ["filled"], callback=do_add))
        box.append(add)
        return box

    def _confirm_unpair(self) -> None:
        dlg = Adw.AlertDialog(
            heading=_("Unpair {name}?").format(name=self.device["name"]),
            body=_("The devices will need to be paired again to exchange data."),
        )
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("unpair", _("Unpair"))
        dlg.set_response_appearance("unpair", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect(
            "response", lambda _d, r: r == "unpair" and self.app.call("device.unpair", device=self.device["id"])
        )
        dlg.present(self.get_root())
