"""Dialogs that start screen mirroring or the camera preview of a paired Android device."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ..i18n import _

WEBCAM_SETUP = {
    "device": _("To use the camera in other apps too, run ./install.sh --webcam once."),
    "tools": _("To use the camera in other apps too, install scrcpy, android-tools, ffmpeg and v4l-utils."),
    "unsupported": _("Using the camera in other apps needs a 64-bit system."),
}


def show(app, window, device: dict, camera: bool = False) -> None:
    """Mirroring starts right away when the phone is found; otherwise (and for the camera) ask first."""
    dev = device["id"]
    dialog = Adw.AlertDialog(heading=_("Phone camera") if camera else _("Mirror screen"))
    dialog.add_response("cancel", _("Close"))
    dialog.set_close_response("cancel")
    dialog.set_body(_("Looking for Android devices…"))
    dialog.present(window)
    closed = False

    def on_closed(_dialog) -> None:
        nonlocal closed
        closed = True

    dialog.connect("closed", on_closed)

    def report(result) -> None:
        if isinstance(result, dict) and result.get("error"):
            app.toast(result["error"])

    def found(result) -> None:
        if closed or not isinstance(result, dict):
            return
        serials = result.get("devices") or []
        if not serials:
            dialog.set_body(
                _("Unlock your phone and allow USB debugging, then try again.")
                if result.get("unauthorized")
                else _(
                    "No Android device found. Enable USB debugging and connect a cable, or turn on Wireless "
                    "debugging, then try again."
                )
            )
            return
        serial = result.get("serial") or ""
        if serial and not camera:
            dialog.close()
            app.plugin(dev, "android", "mirror", serial=serial, callback=report)
            return

        group = Adw.PreferencesGroup()
        picker = None
        if not serial:
            # Several devices and none is clearly this one
            picker = Adw.ComboRow(title=_("Android device"), model=Gtk.StringList.new(serials))
            group.add(picker)
        facing = None
        if camera:
            facing = Adw.ComboRow(title=_("Camera"), model=Gtk.StringList.new([_("Front camera"), _("Rear camera")]))
            settings = next((p.get("values") or {} for p in device["plugins"] if p["id"] == "android"), {})
            facing.set_selected(1 if settings.get("facing") == "back" else 0)
            group.add(facing)
        dialog.set_extra_child(group)

        webcam = device["state"].get("android", {}).get("webcam", {})
        if not camera:
            dialog.set_body(_("Choose the Android device to view and control in a separate window."))
        elif webcam.get("setup"):
            dialog.set_body(WEBCAM_SETUP.get(webcam["setup"], ""))
        else:
            dialog.set_body(_("Other apps can also use the camera as “Caelestia Phone Camera”."))
        dialog.add_response("start", _("Start camera") if camera else _("Start mirroring"))
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")

        def start(_dialog, response) -> None:
            if response != "start":
                return
            chosen = picker.get_selected_item().get_string() if picker else serial
            if camera:
                lens = "back" if facing.get_selected() == 1 else "front"
                app.plugin(dev, "android", "camera", serial=chosen, facing=lens, callback=report)
            else:
                app.plugin(dev, "android", "mirror", serial=chosen, callback=report)

        dialog.connect("response", start)

    app.plugin(dev, "android", "scan", callback=found)
