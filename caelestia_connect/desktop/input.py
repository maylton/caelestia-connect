"""Virtual pointer/keyboard via /dev/uinput, plus text typing that respects the active XKB layout."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
import logging
import subprocess
import unicodedata

log = logging.getLogger(__name__)

try:
    import evdev
    from evdev import ecodes as e
except ImportError:
    evdev = None
    e = None

# KDE Connect "specialKey" codes -> evdev key names
SPECIAL_KEYS = {
    1: "KEY_BACKSPACE",
    2: "KEY_TAB",
    3: "KEY_LINEFEED",
    4: "KEY_LEFT",
    5: "KEY_UP",
    6: "KEY_RIGHT",
    7: "KEY_DOWN",
    8: "KEY_PAGEUP",
    9: "KEY_PAGEDOWN",
    10: "KEY_HOME",
    11: "KEY_END",
    12: "KEY_ENTER",
    13: "KEY_DELETE",
    14: "KEY_ESC",
    15: "KEY_SYSRQ",
    16: "KEY_SCROLLLOCK",
    17: "KEY_LEFTCTRL",
    18: "KEY_LEFTALT",
    19: "KEY_LEFTSHIFT",
    20: "KEY_LEFTMETA",
    **{20 + i: f"KEY_F{i}" for i in range(1, 13)},
}

# Combining character -> dead keysym, used to type accented letters on layouts with dead keys
DEAD_KEYS = {
    "̀": 0xFE50,
    "́": 0xFE51,
    "̂": 0xFE52,
    "̃": 0xFE53,
    "̄": 0xFE54,
    "̆": 0xFE55,
    "̇": 0xFE56,
    "̈": 0xFE57,
    "̊": 0xFE58,
    "̋": 0xFE59,
    "̌": 0xFE5A,
    "̧": 0xFE5B,
    "̨": 0xFE5C,
}


class _RuleNames(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


class XkbMapper:
    """Builds a keysym -> (evdev keycode, modifier keys) table from the user's layout."""

    def __init__(self) -> None:
        self.table: dict[int, tuple[int, tuple[str, ...]]] = {}
        self.lib = None
        path = ctypes.util.find_library("xkbcommon")
        if not path:
            log.warning("libxkbcommon not found, only ASCII text typing will work")
            return
        lib = ctypes.CDLL(path)
        lib.xkb_context_new.restype = ctypes.c_void_p
        lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
        lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames), ctypes.c_int]
        for fn in ("xkb_keymap_min_keycode", "xkb_keymap_max_keycode"):
            getattr(lib, fn).argtypes = [ctypes.c_void_p]
            getattr(lib, fn).restype = ctypes.c_uint32
        lib.xkb_keymap_num_layouts_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.xkb_keymap_num_levels_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        lib.xkb_keymap_key_get_syms_by_level.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
        ]
        lib.xkb_keymap_key_get_mods_for_level.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_size_t,
        ]
        lib.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
        lib.xkb_keymap_mod_get_index.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.xkb_keymap_mod_get_index.restype = ctypes.c_uint32
        lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        lib.xkb_context_unref.argtypes = [ctypes.c_void_p]
        lib.xkb_utf32_to_keysym.argtypes = [ctypes.c_uint32]
        lib.xkb_utf32_to_keysym.restype = ctypes.c_uint32
        self.lib = lib

    @staticmethod
    def current_layout() -> dict[str, str]:
        try:
            out = subprocess.run(["hyprctl", "devices", "-j"], capture_output=True, text=True, timeout=2).stdout
            kbs = json.loads(out).get("keyboards", [])
            main = next((k for k in kbs if k.get("main")), kbs[0] if kbs else None)
            if main:
                return {
                    "layout": main.get("layout", ""),
                    "variant": main.get("variant", ""),
                    "options": main.get("options", ""),
                    "model": main.get("model", ""),
                }
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return {}

    def rebuild(self) -> None:
        self.table = {}
        if self.lib is None:
            return
        lib = self.lib
        names = self.current_layout()
        rn = _RuleNames(
            None,
            (names.get("model") or "").encode() or None,
            (names.get("layout") or "").encode() or None,
            (names.get("variant") or "").encode() or None,
            (names.get("options") or "").encode() or None,
        )
        ctx = lib.xkb_context_new(0)
        keymap = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(rn), 0)
        if not keymap:
            lib.xkb_context_unref(ctx)
            return
        mod_keys = {}
        for mod, key in (
            ("Shift", "KEY_LEFTSHIFT"),
            ("Mod5", "KEY_RIGHTALT"),
            ("Control", "KEY_LEFTCTRL"),
            ("Mod1", "KEY_LEFTALT"),
            ("Lock", None),
            ("Mod2", None),
        ):
            idx = lib.xkb_keymap_mod_get_index(keymap, mod.encode())
            if idx != 0xFFFFFFFF:
                mod_keys[1 << idx] = key
        lo, hi = lib.xkb_keymap_min_keycode(keymap), lib.xkb_keymap_max_keycode(keymap)
        syms = ctypes.POINTER(ctypes.c_uint32)()
        masks = (ctypes.c_uint32 * 8)()
        for kc in range(lo, hi + 1):
            if lib.xkb_keymap_num_layouts_for_key(keymap, kc) < 1:
                continue
            for level in range(lib.xkb_keymap_num_levels_for_key(keymap, kc, 0)):
                n = lib.xkb_keymap_key_get_syms_by_level(keymap, kc, 0, level, ctypes.byref(syms))
                if n != 1:
                    continue
                sym = syms[0]
                nmasks = lib.xkb_keymap_key_get_mods_for_level(keymap, kc, 0, level, masks, 8)
                best = None
                for m in masks[:nmasks]:
                    keys = []
                    ok = True
                    for bit, key in mod_keys.items():
                        if m & bit:
                            if key is None:
                                ok = False
                                break
                            keys.append(key)
                    if ok and (best is None or len(keys) < len(best)):
                        best = keys
                if best is None:
                    continue
                prev = self.table.get(sym)
                # Prefer keys of the main block (evdev < 248) and as few modifiers as possible
                rank = (kc - 8 >= 248, len(best))
                if prev is None or rank < (prev[0] >= 248, len(prev[1])):
                    self.table[sym] = (kc - 8, tuple(best))
        lib.xkb_keymap_unref(keymap)
        lib.xkb_context_unref(ctx)
        log.debug("XKB table built for %s with %d keysyms", names.get("layout"), len(self.table))

    def keysym(self, ch: str) -> int:
        if self.lib is None:
            return 0
        cp = ord(ch)
        return self.lib.xkb_utf32_to_keysym(cp)

    def lookup(self, ch: str) -> list[tuple[int, tuple[str, ...]]] | None:
        """Key presses needed to type `ch` (a dead key sequence if necessary)."""
        if ch == "\n":
            return [(e.KEY_ENTER, ())]
        if ch == "\t":
            return [(e.KEY_TAB, ())]
        sym = self.keysym(ch)
        if sym and sym in self.table:
            return [self.table[sym]]
        decomposed = unicodedata.normalize("NFD", ch)
        if len(decomposed) == 2 and decomposed[1] in DEAD_KEYS:
            base = self.lookup(decomposed[0])
            dead = self.table.get(DEAD_KEYS[decomposed[1]])
            if base and dead:
                return [dead] + base
        return None


class VirtualInput:
    """Lazily created uinput device that can move the pointer, click, scroll and type."""

    def __init__(self) -> None:
        self.dev = None
        self.error = ""
        self.mapper = XkbMapper()
        self._mapper_ready = False
        self._rem_x = 0.0
        self._rem_y = 0.0

    @property
    def available(self) -> bool:
        return self._ensure()

    def _ensure(self) -> bool:
        if self.dev is not None:
            return True
        if evdev is None:
            self.error = "python-evdev is not installed"
            return False
        keys = [v for k, v in e.ecodes.items() if k.startswith("KEY_") and isinstance(v, int) and v < 0x2FF]
        caps = {
            e.EV_KEY: sorted(set(keys + [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA])),
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
        }
        try:
            self.dev = evdev.UInput(caps, name="Caelestia Connect virtual input", vendor=0x1716, product=0x0001)
            self.error = ""
            return True
        except (OSError, evdev.UInputError) as err:
            self.error = f"Cannot open /dev/uinput: {err}"
            log.warning(self.error)
            return False

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def _syn(self) -> None:
        self.dev.syn()

    def move(self, dx: float, dy: float) -> None:
        if not self._ensure():
            return
        self._rem_x += dx
        self._rem_y += dy
        ix, iy = int(self._rem_x), int(self._rem_y)
        self._rem_x -= ix
        self._rem_y -= iy
        if ix:
            self.dev.write(e.EV_REL, e.REL_X, ix)
        if iy:
            self.dev.write(e.EV_REL, e.REL_Y, iy)
        if ix or iy:
            self._syn()

    def button(self, btn: str, down: bool) -> None:
        if not self._ensure():
            return
        code = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}[btn]
        self.dev.write(e.EV_KEY, code, 1 if down else 0)
        self._syn()

    def click(self, btn: str = "left", count: int = 1) -> None:
        for _ in range(count):
            self.button(btn, True)
            self.button(btn, False)

    def scroll(self, dx: float, dy: float) -> None:
        if not self._ensure():
            return
        if dy:
            step = 1 if dy > 0 else -1
            self.dev.write(e.EV_REL, e.REL_WHEEL, step)
            self.dev.write(e.EV_REL, e.REL_WHEEL_HI_RES, step * 120)
        if dx:
            step = 1 if dx > 0 else -1
            self.dev.write(e.EV_REL, e.REL_HWHEEL, step)
            self.dev.write(e.EV_REL, e.REL_HWHEEL_HI_RES, step * 120)
        if dx or dy:
            self._syn()

    def key(self, code: int, down: bool) -> None:
        self.dev.write(e.EV_KEY, code, 1 if down else 0)
        self._syn()

    def tap(self, code: int, modifiers: tuple[str, ...] = ()) -> None:
        if not self._ensure():
            return
        mods = [e.ecodes[m] for m in modifiers]
        for m in mods:
            self.key(m, True)
        self.key(code, True)
        self.key(code, False)
        for m in reversed(mods):
            self.key(m, False)

    def special_key(self, special: int, modifiers: tuple[str, ...] = ()) -> bool:
        name = SPECIAL_KEYS.get(int(special))
        if not name:
            return False
        self.tap(e.ecodes[name], modifiers)
        return True

    def _ensure_mapper(self) -> None:
        if not self._mapper_ready:
            self.mapper.rebuild()
            self._mapper_ready = True

    def refresh_layout(self) -> None:
        self._mapper_ready = False

    async def type_text(self, text: str, modifiers: tuple[str, ...] = (), clipboard=None) -> None:
        """Type text; with modifiers held (eg ctrl+c) each char is sent as a shortcut."""
        if not self._ensure():
            return
        self._ensure_mapper()
        fallback = []
        for ch in text:
            seq = self.mapper.lookup(ch)
            if seq is None and modifiers and ch.isascii():
                seq = self.mapper.lookup(ch.lower())
            if seq is None:
                fallback.append(ch)
                continue
            if fallback:
                await self._paste("".join(fallback), clipboard)
                fallback = []
            for code, mods in seq:
                all_mods = tuple(dict.fromkeys(modifiers + mods))
                self.tap(code, all_mods)
        if fallback:
            await self._paste("".join(fallback), clipboard)

    async def _paste(self, text: str, clipboard) -> None:
        """Characters missing from the layout (emoji...) go through the clipboard + ctrl+v."""
        if clipboard is None:
            return
        previous = clipboard.text
        await clipboard.set_text(text)
        await asyncio.sleep(0.05)
        self.tap(e.KEY_V, ("KEY_LEFTCTRL",))
        await asyncio.sleep(0.15)
        await clipboard.set_text(previous)


class VirtualTablet:
    """Absolute pen device for the drawing tablet (digitizer) plugin."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.dev = None

    def start(self, width: int, height: int, res_x: int, res_y: int) -> bool:
        self.stop()
        if evdev is None:
            return False
        caps = {
            e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH],
            e.EV_ABS: [
                (e.ABS_X, evdev.AbsInfo(0, 0, max(1, width - 1), 0, 0, res_x)),
                (e.ABS_Y, evdev.AbsInfo(0, 0, max(1, height - 1), 0, 0, res_y)),
                (e.ABS_PRESSURE, evdev.AbsInfo(0, 0, 1023, 0, 0, 0)),
            ],
        }
        try:
            self.dev = evdev.UInput(
                caps,
                name=f"{self.name} (drawing tablet)",
                vendor=0x1716,
                product=0x0002,
                input_props=[e.INPUT_PROP_DIRECT, e.INPUT_PROP_POINTER],
            )
            return True
        except (OSError, evdev.UInputError, TypeError) as err:
            log.warning("Could not create virtual tablet: %s", err)
            self.dev = None
            return False

    def stop(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def event(self, active=None, touching=None, tool=None, x=None, y=None, pressure=None) -> None:
        if self.dev is None:
            return
        w = self.dev.write
        if active is False:
            for code in (e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH):
                w(e.EV_KEY, code, 0)
            for code in (e.ABS_PRESSURE, e.ABS_X, e.ABS_Y):
                w(e.EV_ABS, code, 0)
        else:
            skip_pressure = False
            if touching is not None:
                w(e.EV_KEY, e.BTN_TOUCH, 1 if touching else 0)
                if not touching:
                    w(e.EV_ABS, e.ABS_PRESSURE, 0)
                    skip_pressure = True
            if tool is not None:
                w(e.EV_KEY, e.BTN_TOOL_PEN, 1 if tool == "Pen" else 0)
                w(e.EV_KEY, e.BTN_TOOL_RUBBER, 1 if tool == "Rubber" else 0)
            if x is not None:
                w(e.EV_ABS, e.ABS_X, int(x))
            if y is not None:
                w(e.EV_ABS, e.ABS_Y, int(y))
            if pressure is not None and not skip_pressure:
                w(e.EV_ABS, e.ABS_PRESSURE, int(float(pressure) * 1023))
        self.dev.syn()
