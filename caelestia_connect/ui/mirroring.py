"""Open an interactive scrcpy session from a paired device's overview."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from functools import lru_cache

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import CONFIG_DIR, RUNTIME_DIR, read_json, write_json_atomic
from ..i18n import _


def _adb_devices(adb: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        [adb, "devices"], capture_output=True, text=True, errors="replace", check=True, timeout=10
    )
    devices = []
    for line in result.stdout.splitlines():
        serial, separator, status = line.partition("\t")
        if serial and separator:
            devices.append((serial, status.strip()))
    return devices


@lru_cache(maxsize=16)
def _camera_sensor(adb: str, serial: str, facing: str) -> int:
    front = facing == "front"
    default = 270 if front else 90
    try:
        result = subprocess.run(
            [adb, "-s", serial, "shell", "dumpsys", "media.camera"],
            capture_output=True, text=True, errors="replace", check=True, timeout=4,
        )
        for block in result.stdout.split("== Camera HAL device "):
            lens = re.search(r"android\.lens\.facing[^\n]*\n\s*\[(FRONT|BACK|[01])\s*\]", block)
            angle = re.search(r"android\.sensor\.orientation[^\n]*\n\s*\[(0|90|180|270)\s*\]", block)
            if lens and angle and (lens[1] in ("FRONT", "0")) == front:
                return int(angle[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return default


def _camera_rotation(adb: str, serial: str, facing: str) -> int:
    sensor = _camera_sensor(adb, serial, facing)
    device = 0
    try:
        result = subprocess.run(
            [adb, "-s", serial, "shell", "dumpsys", "window", "displays"],
            capture_output=True, text=True, errors="replace", check=True, timeout=4,
        )
        proposed = re.search(r"mProposedRotation=ROTATION_(0|90|180|270)\b", result.stdout)
        current = re.search(r"\bmRotation=([0-3])\b", result.stdout)
        if proposed:
            device = int(proposed[1])
        elif current:
            device = int(current[1]) * 90
    except (OSError, subprocess.SubprocessError):
        pass
    return (device - sensor if facing == "front" else sensor + device) % 360


def _rotation_offset() -> int:
    settings = read_json(CONFIG_DIR / "webcam-rotation.json", {})
    value = settings.get("angle", 0) if isinstance(settings, dict) else 0
    return value if value in (0, 90, 180, 270) else 0


def show_mirroring(app, window, camera: bool = False) -> None:
    adb = shutil.which("adb")
    scrcpy = shutil.which("scrcpy")
    dialog = Adw.AlertDialog(heading=_("Phone camera") if camera else _("Mirror screen"))
    dialog.add_response("cancel", _("Close"))
    dialog.set_close_response("cancel")

    if not adb or not scrcpy:
        dialog.set_body(_("Install scrcpy and Android platform tools (adb) to mirror your phone."))
        dialog.present(window)
        return

    dialog.set_body(_("Looking for Android devices…"))
    dialog.present(window)
    closed = False

    def on_closed(_dialog) -> None:
        nonlocal closed
        closed = True

    dialog.connect("closed", on_closed)

    async def discover() -> None:
        try:
            devices = await asyncio.to_thread(_adb_devices, adb)
        except (OSError, subprocess.SubprocessError):
            if not closed:
                dialog.set_body(_("Could not list Android devices. Check that adb is working and try again."))
            return
        if closed:
            return

        serials = [serial for serial, status in devices if status == "device"]
        if not serials:
            if any(status == "unauthorized" for _, status in devices):
                dialog.set_body(_("Unlock your phone and allow USB debugging, then reopen Mirror screen."))
            else:
                dialog.set_body(
                    _(
                        "No authorized Android device found. Connect with USB debugging or Android Wireless debugging, "
                        "then reopen Mirror screen."
                    )
                )
            return

        dialog.set_body(
            _("Choose a phone and camera. Close the camera window to stop streaming.")
            if camera else _("Choose the Android device to view and control in a separate window.")
        )
        picker = Adw.ComboRow(title=_("Android device"), model=Gtk.StringList.new(serials))
        group = Adw.PreferencesGroup()
        group.add(picker)
        if camera:
            facing = Adw.ComboRow(title=_("Camera"), model=Gtk.StringList.new([_("Front"), _("Back")]))
            preferences = read_json(CONFIG_DIR / "camera-preview.json", {})
            if isinstance(preferences, dict) and preferences.get("facing") == "back":
                facing.set_selected(1)
            def remember_facing(row, _pspec):
                lens = "front" if row.get_selected() == 0 else "back"
                try:
                    write_json_atomic(CONFIG_DIR / "camera-preview.json", {"facing": lens})
                except OSError:
                    app.toast(_("Could not save the camera selection."))

            facing.connect("notify::selected", remember_facing)
            group.add(facing)
        dialog.set_extra_child(group)
        dialog.add_response("start", _("Start camera") if camera else _("Start mirroring"))
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")

        async def launch(serial: str, lens: str | None) -> None:
            if lens is None:
                try:
                    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
                    launcher.setenv("ADB", adb, True)
                    process = launcher.spawnv([scrcpy, "--serial", serial])
                except GLib.Error:
                    app.toast(_("Could not start scrcpy."))
                    return

                def finished(proc, result) -> None:
                    try:
                        proc.wait_check_finish(result)
                    except GLib.Error:
                        app.toast(_("Screen mirroring failed. Check the Android debugging connection and try again."))

                process.wait_check_async(None, finished)
                return

            preview_marker = RUNTIME_DIR / "camera-preview.json"
            try:
                write_json_atomic(preview_marker, {"pid": os.getpid()})
            except OSError:
                app.toast(_("Could not reserve the phone camera."))
                return
            try:
                # Give the webcam service time to release the phone camera.
                await asyncio.sleep(2)
                base_angle = await asyncio.to_thread(_camera_rotation, adb, serial, lens)
                angle = (base_angle + _rotation_offset()) % 360
                try:
                    process = await asyncio.create_subprocess_exec(
                        scrcpy, "--serial", serial, "--video-source=camera", "--no-audio",
                        "--max-size=1280", f"--camera-facing={lens}", f"--display-orientation={angle}",
                        "--shortcut-mod=lalt",
                    )
                except OSError:
                    app.toast(_("Could not start scrcpy."))
                    return
                waiter = asyncio.create_task(process.wait())
                try:
                    next_sensor_check = 0.0
                    while True:
                        done, pending = await asyncio.wait({waiter}, timeout=0.2)
                        if done:
                            if waiter.result():
                                app.toast(_("Phone camera failed. Check that the phone camera is available."))
                            return
                        now = asyncio.get_running_loop().time()
                        if now >= next_sensor_check:
                            base_angle = await asyncio.to_thread(_camera_rotation, adb, serial, lens)
                            next_sensor_check = now + 0.5
                        new_angle = (base_angle + _rotation_offset()) % 360
                        turns = ((new_angle - angle) % 360) // 90
                        for _turn in range(turns):
                            try:
                                dispatch = await asyncio.create_subprocess_exec(
                                    "hyprctl", "dispatch",
                                    f'hl.dsp.send_shortcut({{mods="ALT", key="Right", window="pid:{process.pid}"}})',
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                )
                                output, error = await dispatch.communicate()
                            except OSError:
                                break
                            if dispatch.returncode or output.strip() != b"ok":
                                break
                            angle = (angle + 90) % 360
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
            finally:
                preview_marker.unlink(missing_ok=True)

        def start(_dialog, response) -> None:
            if response != "start":
                return
            serial = picker.get_selected_item().get_string()
            lens = ("front" if facing.get_selected() == 0 else "back") if camera else None
            app.spawn(launch(serial, lens))

        dialog.connect("response", start)

    app.spawn(discover())
