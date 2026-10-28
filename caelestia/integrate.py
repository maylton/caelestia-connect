#!/usr/bin/env python3
"""Add (or remove) the Caelestia Connect bar icon and popout to a Caelestia shell checkout.

Usage: integrate.py [--shell DIR] [--uninstall] [--dry-run]

DIR defaults to the shell Quickshell loads (~/.config/quickshell/caelestia, following symlinks).
Every change is delimited by "caelestia-connect" markers so it can be re-applied or removed safely.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = [
    "services/KdeConnect.qml",
    "modules/bar/components/status/KdeConnectStatus.qml",
    "modules/bar/popouts/KdeConnectPopout.qml",
    "modules/bar/popouts/kdeconnect/AvailableDeviceRow.qml",
    "modules/bar/popouts/kdeconnect/DeviceCard.qml",
    "modules/bar/popouts/kdeconnect/PairRequestCard.qml",
    "modules/bar/popouts/kdeconnect/TransferRow.qml",
]

STATUS_FUNCTION = """    // caelestia-connect: begin
    // Add the KDE Connect icon next to bluetooth unless the config already lists "kdeConnect"
    function withKdeConnect(values: var): var {
        const out = Array.from(values);
        if (out.some(e => e.id === "kdeConnect"))
            return out;
        const at = out.findIndex(e => e.id === "battery");
        out.splice(at < 0 ? out.length : at, 0, {
            id: "kdeConnect",
            enabled: true
        });
        return out;
    }
    // caelestia-connect: end

"""

STATUS_COLLAPSED = """        // caelestia-connect: begin
        if (entry.id === "kdeConnect")
            return !KdeConnect.shown;
        // caelestia-connect: end
"""

STATUS_DELEGATE = """                // caelestia-connect: begin
                DelegateChoice {
                    roleValue: "kdeConnect"
                    delegate: EntryWrapper {
                        KdeConnectStatus {
                            colour: root.colour
                        }
                    }
                }
                // caelestia-connect: end
"""

POPOUT = """        // caelestia-connect: begin
        Popout {
            name: "kdeconnect"
            sourceComponent: KdeConnectPopout {
                popouts: root.popouts
            }
        }
        // caelestia-connect: end
"""

MODEL_ORIG = "values: root.Config.bar.statusIcons.values.filter(e => e.enabled)"
MODEL_NEW = (
    "values: root.withKdeConnect(root.Config.bar.statusIcons.values).filter(e => e.enabled) // caelestia-connect"
)


BLUETOOTH_IMPORT = """// caelestia-connect: begin
import qs.services
// caelestia-connect: end
"""
BLUETOOTH_ORIG = "values: Bluetooth.devices.values.filter(d => d.state !== BluetoothDeviceState.Disconnected)"
BLUETOOTH_NEW = (
    "values: Bluetooth.devices.values.filter(d => d.state !== BluetoothDeviceState.Disconnected"
    " && !KdeConnect.isPairedPhone(d.address)) // caelestia-connect"
)


def strip_blocks(text: str) -> str:
    text = re.sub(r"[ \t]*// caelestia-connect: begin.*?// caelestia-connect: end\n\n?", "", text, flags=re.S)
    return text.replace(MODEL_NEW, MODEL_ORIG).replace(BLUETOOTH_NEW, BLUETOOTH_ORIG)


def patch_bluetooth_status(text: str) -> str:
    text = strip_blocks(text)
    if BLUETOOTH_ORIG not in text or "import qs.components\n" not in text:
        raise RuntimeError("BluetoothStatus.qml: connected devices model not found (shell version not supported)")
    text = text.replace(BLUETOOTH_ORIG, BLUETOOTH_NEW)
    return text.replace("import qs.components\n", "import qs.components\n" + BLUETOOTH_IMPORT, 1)


def patch_status_icons(text: str) -> str:
    text = strip_blocks(text)
    if MODEL_ORIG not in text:
        raise RuntimeError("StatusIcons.qml: status icon model not found (shell version not supported)")
    text = text.replace(MODEL_ORIG, MODEL_NEW)
    anchor = "    // Entries that can shrink to nothing, spacing included\n"
    if anchor not in text:
        anchor = "    function collapsed("
    idx = text.index(anchor)
    text = text[:idx] + STATUS_FUNCTION + text[idx:]
    m = re.search(r"function collapsed\(entry: var\): bool \{\n", text)
    if not m:
        raise RuntimeError("StatusIcons.qml: collapsed() not found")
    text = text[: m.end()] + STATUS_COLLAPSED + text[m.end() :]
    m = re.search(r"\n(\s*)DelegateChoice \{\n\s*roleValue: \"battery\"", text)
    if not m:
        raise RuntimeError("StatusIcons.qml: battery delegate not found")
    return text[: m.start() + 1] + STATUS_DELEGATE + text[m.start() + 1 :]


def patch_content(text: str) -> str:
    text = strip_blocks(text)
    m = re.search(r"\n(\s*)Popout \{\n\s*name: \"battery\"", text)
    if not m:
        raise RuntimeError("popouts/Content.qml: battery popout not found")
    return text[: m.start() + 1] + POPOUT + text[m.start() + 1 :]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", type=Path, default=Path.home() / ".config/quickshell/caelestia")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    shell = args.shell.expanduser().resolve()
    status = shell / "modules/bar/components/StatusIcons.qml"
    content = shell / "modules/bar/popouts/Content.qml"
    bluetooth = shell / "modules/bar/components/status/BluetoothStatus.qml"
    if not status.exists() or not content.exists() or not bluetooth.exists():
        print(f"{shell} does not look like a Caelestia shell", file=sys.stderr)
        return 1
    if str(shell).startswith(("/etc/", "/usr/")):
        print(f"{shell} is a system install; use a local copy (see README)", file=sys.stderr)
        return 1

    if args.uninstall:
        patched = {path: strip_blocks(path.read_text()) for path in (status, content, bluetooth)}
    else:
        patched = {
            status: patch_status_icons(status.read_text()),
            content: patch_content(content.read_text()),
            bluetooth: patch_bluetooth_status(bluetooth.read_text()),
        }

    if args.dry_run:
        print(patched[status] if "--status" in sys.argv else "dry run ok")
        return 0

    for path, text in patched.items():
        backup = path.with_suffix(path.suffix + ".orig")
        if not backup.exists() and not args.uninstall:
            shutil.copy2(path, backup)
        path.write_text(text)
    for rel in FILES:
        dest = shell / rel
        if args.uninstall:
            dest.unlink(missing_ok=True)
            if dest.parent.name == "kdeconnect" and dest.parent.exists() and not any(dest.parent.iterdir()):
                dest.parent.rmdir()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / rel, dest)
    print(("Removed from " if args.uninstall else "Integrated into ") + str(shell))
    return 0


if __name__ == "__main__":
    sys.exit(main())
