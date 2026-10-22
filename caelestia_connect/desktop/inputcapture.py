"""Captures the local pointer and keyboard when the cursor hits a screen edge.

Uses the xdg InputCapture portal for the barrier and activation, and libei (as a receiver) for the
captured events. Key events are translated with the keymap provided by the compositor.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import mmap
import os
import secrets
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from ..utils import spawn

log = logging.getLogger(__name__)

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.InputCapture"

CAP_KEYBOARD, CAP_POINTER = 1, 2

# libei
EV_SEAT_ADDED = 3
EV_DEVICE_ADDED = 5
EV_START_EMULATING = 200
EV_POINTER_MOTION = 300
EV_BUTTON = 500
EV_SCROLL_DELTA = 600
EV_SCROLL_DISCRETE = 603
EV_KEYBOARD_MODIFIERS = 7
EV_KEYBOARD_KEY = 700
EI_CAP_POINTER, EI_CAP_KEYBOARD, EI_CAP_SCROLL, EI_CAP_BUTTON = 1 << 0, 1 << 2, 1 << 4, 1 << 5
EI_KEYMAP_XKB = 1

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112

# keysym -> KDE Connect "specialKey"
SPECIAL_KEYSYMS = {
    0xFF08: 1,  # BackSpace
    0xFF09: 2,  # Tab
    0xFE20: 2,  # ISO_Left_Tab
    0xFF51: 4,
    0xFF52: 5,
    0xFF53: 6,
    0xFF54: 7,
    0xFF55: 8,
    0xFF56: 9,
    0xFF50: 10,
    0xFF57: 11,
    0xFF0D: 12,  # Return
    0xFF8D: 12,  # KP_Enter
    0xFFFF: 13,  # Delete
    0xFF1B: 14,  # Escape
    0xFF15: 15,  # Sys_Req
    0xFF14: 16,  # Scroll_Lock
    **{0xFFBE + i: 21 + i for i in range(12)},  # F1..F12
}


def _bind(lib, name: str, restype, *argtypes) -> None:
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


def _load_libei():
    path = ctypes.util.find_library("ei")
    if not path:
        return None
    lib = ctypes.CDLL(path)
    p, u32, i32, dbl, c_bool = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_double, ctypes.c_bool
    _bind(lib, "ei_new_receiver", p, p)
    _bind(lib, "ei_configure_name", None, p, ctypes.c_char_p)
    _bind(lib, "ei_setup_backend_fd", ctypes.c_int, p, ctypes.c_int)
    _bind(lib, "ei_get_fd", ctypes.c_int, p)
    _bind(lib, "ei_dispatch", None, p)
    _bind(lib, "ei_get_event", p, p)
    _bind(lib, "ei_unref", p, p)
    _bind(lib, "ei_event_unref", p, p)
    _bind(lib, "ei_event_get_type", ctypes.c_int, p)
    _bind(lib, "ei_event_get_seat", p, p)
    _bind(lib, "ei_event_get_device", p, p)
    _bind(lib, "ei_device_has_capability", c_bool, p, ctypes.c_int)
    _bind(lib, "ei_device_keyboard_get_keymap", p, p)
    _bind(lib, "ei_keymap_get_type", ctypes.c_int, p)
    _bind(lib, "ei_keymap_get_fd", ctypes.c_int, p)
    _bind(lib, "ei_keymap_get_size", ctypes.c_size_t, p)
    _bind(lib, "ei_event_emulating_get_sequence", u32, p)
    for name in (
        "ei_event_pointer_get_dx",
        "ei_event_pointer_get_dy",
        "ei_event_scroll_get_dx",
        "ei_event_scroll_get_dy",
    ):
        _bind(lib, name, dbl, p)
    for name in ("ei_event_scroll_get_discrete_dx", "ei_event_scroll_get_discrete_dy"):
        _bind(lib, name, i32, p)
    for name in (
        "ei_event_button_get_button",
        "ei_event_keyboard_get_key",
        "ei_event_keyboard_get_xkb_mods_depressed",
        "ei_event_keyboard_get_xkb_mods_latched",
        "ei_event_keyboard_get_xkb_mods_locked",
        "ei_event_keyboard_get_xkb_group",
    ):
        _bind(lib, name, u32, p)
    for name in ("ei_event_button_get_is_press", "ei_event_keyboard_get_key_is_press"):
        _bind(lib, name, c_bool, p)
    # Variadic, terminated by NULL: leave argtypes unset
    lib.ei_seat_bind_capabilities.restype = None
    return lib


def _load_xkb():
    path = ctypes.util.find_library("xkbcommon")
    if not path:
        return None
    lib = ctypes.CDLL(path)
    p, u32 = ctypes.c_void_p, ctypes.c_uint32
    _bind(lib, "xkb_context_new", p, ctypes.c_int)
    _bind(lib, "xkb_context_unref", None, p)
    _bind(lib, "xkb_keymap_new_from_buffer", p, p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int)
    _bind(lib, "xkb_keymap_unref", None, p)
    _bind(lib, "xkb_state_new", p, p)
    _bind(lib, "xkb_state_unref", None, p)
    _bind(lib, "xkb_state_update_key", ctypes.c_int, p, u32, ctypes.c_int)
    _bind(lib, "xkb_state_update_mask", ctypes.c_int, p, u32, u32, u32, u32, u32, u32)
    _bind(lib, "xkb_state_key_get_one_sym", u32, p, u32)
    _bind(lib, "xkb_state_mod_name_is_active", ctypes.c_int, p, ctypes.c_char_p, ctypes.c_int)
    _bind(lib, "xkb_keysym_to_utf8", ctypes.c_int, u32, ctypes.c_char_p, ctypes.c_size_t)
    return lib


class Keyboard:
    """xkb state for the keymap of the captured keyboard."""

    MODS = {"shift": b"Shift", "ctrl": b"Control", "alt": b"Mod1", "super": b"Mod4"}
    MODS_EFFECTIVE = 1 << 3

    def __init__(self, lib, fd: int, size: int) -> None:
        self.lib = lib
        self.ctx = lib.xkb_context_new(0)
        with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as m:
            data = m[:size].rstrip(b"\0")
        self.keymap = lib.xkb_keymap_new_from_buffer(self.ctx, data, len(data), 1, 0)
        self.state = lib.xkb_state_new(self.keymap) if self.keymap else None

    def close(self) -> None:
        if self.state:
            self.lib.xkb_state_unref(self.state)
        if self.keymap:
            self.lib.xkb_keymap_unref(self.keymap)
        self.lib.xkb_context_unref(self.ctx)
        self.state = self.keymap = None

    def update_modifiers(self, depressed: int, latched: int, locked: int, group: int) -> None:
        if self.state:
            self.lib.xkb_state_update_mask(self.state, depressed, latched, locked, 0, 0, group)

    def key(self, keycode: int, pressed: bool) -> dict[str, Any] | None:
        """Updates the state; for a press returns the KDE Connect key description, if any."""
        if not self.state:
            return None
        self.lib.xkb_state_update_key(self.state, keycode, 1 if pressed else 0)
        if not pressed:
            return None
        sym = self.lib.xkb_state_key_get_one_sym(self.state, keycode)
        mods = {
            k: bool(self.lib.xkb_state_mod_name_is_active(self.state, v, self.MODS_EFFECTIVE))
            for k, v in self.MODS.items()
        }
        special = SPECIAL_KEYSYMS.get(sym, 0)
        if sym == 0xFE20:
            mods["shift"] = True
        text = ""
        if not special:
            buf = ctypes.create_string_buffer(8)
            if self.lib.xkb_keysym_to_utf8(sym, buf, len(buf)) > 0:
                text = buf.value.decode(errors="ignore")
            if not text or not text.isprintable():
                return None
        return {"key": text, "specialKey": special, **mods}


class _Portal:
    """Calls on the InputCapture portal, including the Request/Response dance."""

    def __init__(self, bus: Gio.DBusConnection) -> None:
        self.bus = bus
        self.sender = bus.get_unique_name()[1:].replace(".", "_")

    async def call(self, method: str, params: GLib.Variant, reply: str = "(o)") -> GLib.Variant:
        return await self.bus.call(
            PORTAL, PORTAL_PATH, IFACE, method, params, GLib.VariantType(reply), Gio.DBusCallFlags.NONE, -1
        )

    async def request(self, method: str, build: Callable[[str], GLib.Variant]) -> dict[str, Any]:
        token = f"caelestia_{secrets.token_hex(6)}"
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        future = asyncio.get_running_loop().create_future()

        def on_response(_bus, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            if not future.done():
                future.set_result((code, results))

        sub = self.bus.signal_subscribe(
            PORTAL, "org.freedesktop.portal.Request", "Response", path, None, Gio.DBusSignalFlags.NONE, on_response
        )
        try:
            await self.call(method, build(token))
            code, results = await asyncio.wait_for(future, 60)
        finally:
            self.bus.signal_unsubscribe(sub)
        if code != 0:
            raise RuntimeError(f"{method} was denied or cancelled ({code})")
        return results


class InputCapture:
    """One capture session with a pointer barrier on `edge` ("left", "right", "top" or "bottom").

    Callbacks: on_start(dx, dy) with the cursor offset along the barrier, on_motion(dx, dy),
    on_button(button, pressed), on_scroll(dx, dy), on_scroll_discrete(dx, dy), on_key(description).
    """

    def __init__(self, edge: str, **callbacks: Callable) -> None:
        self.edge = edge
        self.cb = callbacks
        self.ei_lib = _load_libei()
        self.xkb_lib = _load_xkb()
        self.portal: _Portal | None = None
        self.session: str | None = None
        self.zone_set = 0
        self.zones: list[tuple[int, int, int, int]] = []
        self.barrier: tuple[int, int, int, int] | None = None
        self.active = False
        self._activation = 0
        self._entry = 0.0
        self._sequence = 0
        self._queued: list[tuple] = []
        self._signals: list[int] = []
        self._ei = None
        self._watch = 0
        self._keyboard: Keyboard | None = None

    @property
    def available(self) -> bool:
        return self.ei_lib is not None

    async def start(self) -> None:
        if not self.available:
            raise RuntimeError("libei is not installed")
        bus = await Gio.bus_get(Gio.BusType.SESSION)
        self.portal = _Portal(bus)
        for name in ("Activated", "Deactivated", "Disabled", "ZonesChanged"):
            self._signals.append(
                bus.signal_subscribe(PORTAL, IFACE, name, PORTAL_PATH, None, Gio.DBusSignalFlags.NONE, self._on_signal)
            )
        results = await self.portal.request(
            "CreateSession",
            lambda token: GLib.Variant(
                "(sa{sv})",
                (
                    "",
                    {
                        "handle_token": GLib.Variant("s", token),
                        "session_handle_token": GLib.Variant("s", f"caelestia_{secrets.token_hex(6)}"),
                        "capabilities": GLib.Variant("u", CAP_KEYBOARD | CAP_POINTER),
                    },
                ),
            ),
        )
        self.session = results["session_handle"]
        await self._connect_eis()
        await self._update_zones()

    def stop(self) -> None:
        if self.portal:
            for sub in self._signals:
                self.portal.bus.signal_unsubscribe(sub)
            if self.session:
                self.portal.bus.call(
                    PORTAL,
                    self.session,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                )
        self._signals.clear()
        self.session = None
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self._ei:
            self.ei_lib.ei_unref(self._ei)
            self._ei = None
        if self._keyboard:
            self._keyboard.close()
            self._keyboard = None
        self.active = False

    async def set_edge(self, edge: str) -> None:
        if edge != self.edge:
            self.edge = edge
            if self.session:
                await self._set_barrier()

    def release(self, along: float | None = None) -> None:
        """Gives the pointer back, `along` pixels along the barrier (None keeps the entry point)."""
        if not self.session or not self.barrier or not self.active:
            return
        self.active = False
        x1, y1, x2, y2 = self.barrier
        inward = {"left": (2, 0), "right": (-2, 0), "top": (0, 2), "bottom": (0, -2)}[self.edge]
        if along is None:
            along = self._entry
        if x1 == x2:
            pos = (x1 + inward[0], min(max(y1 + along, y1), y2))
        else:
            pos = (min(max(x1 + along, x1), x2), y1 + inward[1])
        options = {
            "activation_id": GLib.Variant("u", self._activation),
            "cursor_position": GLib.Variant("(dd)", (float(pos[0]), float(pos[1]))),
        }
        self.portal.bus.call(
            PORTAL,
            PORTAL_PATH,
            IFACE,
            "Release",
            GLib.Variant("(oa{sv})", (self.session, options)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
        )

    async def _connect_eis(self) -> None:
        res, fds = await self.portal.bus.call_with_unix_fd_list(
            PORTAL,
            PORTAL_PATH,
            IFACE,
            "ConnectToEIS",
            GLib.Variant("(oa{sv})", (self.session, {})),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        fd = fds.get(res.unpack()[0])
        lib = self.ei_lib
        self._ei = lib.ei_new_receiver(None)
        lib.ei_configure_name(self._ei, b"Caelestia Connect")
        if lib.ei_setup_backend_fd(self._ei, fd) != 0:
            os.close(fd)
            raise RuntimeError("Cannot set up the libei connection")
        self._watch = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, lib.ei_get_fd(self._ei), GLib.IOCondition.IN, self._on_ei_readable
        )

    async def _update_zones(self) -> None:
        results = await self.portal.request(
            "GetZones",
            lambda token: GLib.Variant("(oa{sv})", (self.session, {"handle_token": GLib.Variant("s", token)})),
        )
        self.zone_set = results["zone_set"]
        self.zones = [(int(x), int(y), int(w), int(h)) for w, h, x, y in results["zones"]]
        await self._set_barrier()

    async def _set_barrier(self) -> None:
        if not self.zones:
            return
        zones = self.zones
        if self.edge == "left":
            x, y, w, h = min(zones, key=lambda z: z[0])
            self.barrier = (x, y, x, y + h - 1)
        elif self.edge == "right":
            x, y, w, h = max(zones, key=lambda z: z[0] + z[2])
            self.barrier = (x + w, y, x + w, y + h - 1)
        elif self.edge == "top":
            x, y, w, h = min(zones, key=lambda z: z[1])
            self.barrier = (x, y, x + w - 1, y)
        else:
            x, y, w, h = max(zones, key=lambda z: z[1] + z[3])
            self.barrier = (x, y + h, x + w - 1, y + h)
        barrier = {"barrier_id": GLib.Variant("u", 1), "position": GLib.Variant("(iiii)", self.barrier)}
        results = await self.portal.request(
            "SetPointerBarriers",
            lambda token: GLib.Variant(
                "(oa{sv}aa{sv}u)", (self.session, {"handle_token": GLib.Variant("s", token)}, [barrier], self.zone_set)
            ),
        )
        if results.get("failed_barriers"):
            log.warning("The compositor rejected the pointer barrier %s", self.barrier)
        await self.portal.call("Enable", GLib.Variant("(oa{sv})", (self.session, {})), reply="()")
        log.info("Input capture ready on the %s edge %s", self.edge, self.barrier)

    def _on_signal(self, _bus, _sender, _path, _iface, signal: str, params: GLib.Variant) -> None:
        session, options = params.unpack()
        if session != self.session:
            return
        if signal == "Activated":
            self._activation = options.get("activation_id", 0)
            x, y = options.get("cursor_position", (0.0, 0.0))
            x1, y1, x2, _y2 = self.barrier or (0, 0, 0, 0)
            self._entry = (y - y1) if x1 == x2 else (x - x1)
            self.active = True
            self._emit("on_start", x - x1, y - y1)
            queued, self._queued = self._queued, []
            for event in queued:
                self._dispatch(*event)
        elif signal == "Deactivated":
            self.active = False
        elif signal == "ZonesChanged" and options.get("zone_set", 0) >= self.zone_set:
            spawn(self._update_zones(), name="inputcapture:zones")

    def _on_ei_readable(self, _fd, _condition) -> bool:
        lib = self.ei_lib
        if not self._ei:
            return False
        lib.ei_dispatch(self._ei)
        while event := lib.ei_get_event(self._ei):
            try:
                self._ei_event(event)
            except Exception:
                log.exception("libei event handling failed")
            lib.ei_event_unref(event)
        return True

    def _ei_event(self, event) -> None:
        lib = self.ei_lib
        etype = lib.ei_event_get_type(event)
        if etype == EV_SEAT_ADDED:
            seat = lib.ei_event_get_seat(event)
            caps = (EI_CAP_KEYBOARD, EI_CAP_POINTER, EI_CAP_BUTTON, EI_CAP_SCROLL)
            lib.ei_seat_bind_capabilities(
                ctypes.c_void_p(seat), *(ctypes.c_int(c) for c in caps), ctypes.c_void_p(None)
            )
        elif etype == EV_DEVICE_ADDED:
            device = lib.ei_event_get_device(event)
            if lib.ei_device_has_capability(device, EI_CAP_KEYBOARD) and self.xkb_lib:
                keymap = lib.ei_device_keyboard_get_keymap(device)
                if keymap and lib.ei_keymap_get_type(keymap) == EI_KEYMAP_XKB:
                    if self._keyboard:
                        self._keyboard.close()
                    self._keyboard = Keyboard(
                        self.xkb_lib, lib.ei_keymap_get_fd(keymap), lib.ei_keymap_get_size(keymap)
                    )
        elif etype == EV_START_EMULATING:
            self._sequence = lib.ei_event_emulating_get_sequence(event)
        elif etype == EV_POINTER_MOTION:
            self._input("motion", lib.ei_event_pointer_get_dx(event), lib.ei_event_pointer_get_dy(event))
        elif etype == EV_BUTTON:
            self._input("button", lib.ei_event_button_get_button(event), lib.ei_event_button_get_is_press(event))
        elif etype == EV_SCROLL_DELTA:
            self._input("scroll", lib.ei_event_scroll_get_dx(event), lib.ei_event_scroll_get_dy(event))
        elif etype == EV_SCROLL_DISCRETE:
            self._input(
                "discrete", lib.ei_event_scroll_get_discrete_dx(event), lib.ei_event_scroll_get_discrete_dy(event)
            )
        elif etype == EV_KEYBOARD_MODIFIERS:
            self._input(
                "mods",
                lib.ei_event_keyboard_get_xkb_mods_depressed(event),
                lib.ei_event_keyboard_get_xkb_mods_latched(event),
                lib.ei_event_keyboard_get_xkb_mods_locked(event),
                lib.ei_event_keyboard_get_xkb_group(event),
            )
        elif etype == EV_KEYBOARD_KEY:
            self._input("key", lib.ei_event_keyboard_get_key(event), lib.ei_event_keyboard_get_key_is_press(event))

    def _input(self, kind: str, *args) -> None:
        # Events can arrive before the portal's Activated signal that carries the entry position
        if self._sequence > self._activation:
            self._queued.append((kind, *args))
        else:
            self._dispatch(kind, *args)

    def _dispatch(self, kind: str, *args) -> None:
        if kind == "mods":
            if self._keyboard:
                self._keyboard.update_modifiers(*args)
            return
        if not self.active:
            return
        if kind == "motion":
            self._emit("on_motion", *args)
        elif kind == "button":
            self._emit("on_button", *args)
        elif kind == "scroll":
            self._emit("on_scroll", *args)
        elif kind == "discrete":
            self._emit("on_scroll_discrete", *args)
        elif kind == "key":
            code, pressed = args
            desc = self._keyboard.key(code + 8, pressed) if self._keyboard else None
            if desc and desc["specialKey"] == 14 and desc["ctrl"] and desc["alt"]:
                self.release()  # Ctrl+Alt+Esc always gives the pointer back
                return
            if desc:
                self._emit("on_key", desc)

    def _emit(self, name: str, *args) -> None:
        cb = self.cb.get(name)
        if cb:
            cb(*args)
