#!/usr/bin/env bash
# Installs Caelestia Connect for the current user and integrates it with the Caelestia shell.
#   ./install.sh               install / update
#   ./install.sh --no-shell    do not touch the Caelestia shell
#   ./install.sh --webcam      also create the virtual webcam for the phone camera (asks for sudo once)
#   ./install.sh --uninstall   remove everything (keeps pairing data in ~/.config/caelestia-connect)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
NAUTILUS="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus-python/extensions"
SHELL_DIR="${CAELESTIA_SHELL_DIR:-$HOME/.config/quickshell/caelestia}"
PY="$(command -v python3)"
WEBCAM_MODPROBE=/etc/modprobe.d/caelestia-connect-webcam.conf
WEBCAM_LOAD=/etc/modules-load.d/caelestia-connect-webcam.conf

setup_webcam() {
    echo ":: Setting up the virtual webcam (v4l2loopback)"
    command -v ffmpeg >/dev/null && command -v v4l2-ctl >/dev/null ||
        echo "   The webcam also needs: sudo pacman -S --needed ffmpeg v4l-utils"
    if ! modinfo v4l2loopback >/dev/null 2>&1; then
        echo "   The v4l2loopback module is missing. Install it with your kernel's headers, for example:"
        echo "   sudo pacman -S --needed v4l2loopback-dkms linux-headers   # or linux-zen-headers, linux-lts-headers..."
        echo "   then run ./install.sh --webcam again."
        return 1
    fi
    if [ -e "$WEBCAM_MODPROBE" ] && grep -rlq "Caelestia Phone Camera" /sys/class/video4linux/*/name 2>/dev/null; then
        echo "   Already set up."
        return 0
    fi
    if grep -rqs "^options v4l2loopback" /etc/modprobe.d --exclude="$(basename "$WEBCAM_MODPROBE")"; then
        echo "   Another v4l2loopback configuration exists in /etc/modprobe.d; not changing it."
        echo "   Add the Caelestia devices to it instead (see the README)."
        return 1
    fi
    # Two devices: the webcam other apps see, and an input used to fit rotated pictures into its frame
    echo 'options v4l2loopback devices=2 exclusive_caps=1,1 card_label="Caelestia Phone Camera,Caelestia Camera Input"' |
        sudo tee "$WEBCAM_MODPROBE" >/dev/null
    echo v4l2loopback | sudo tee "$WEBCAM_LOAD" >/dev/null
    if lsmod | grep -q '^v4l2loopback'; then
        echo "   v4l2loopback is already loaded with other settings: reboot to apply."
    else
        sudo modprobe v4l2loopback
        echo "   Created \"Caelestia Phone Camera\". It is set up again automatically at boot."
    fi
}

shell=1
webcam=0
case "${1:-}" in
--uninstall)
    systemctl --user disable --now caelestia-connectd.service 2>/dev/null || true
    rm -f "$BIN/caelestia-connect" "$BIN/caelestia-connectd" "$BIN/caelestia-connect-cli" \
        "$APPS/org.caelestia.Connect.desktop" "$ICONS/org.caelestia.Connect.svg" \
        "$UNITS/caelestia-connectd.service" "$NAUTILUS/caelestia_connect_nautilus.py"
    [ -d "$SHELL_DIR" ] && "$PY" "$SRC/caelestia/integrate.py" --shell "$SHELL_DIR" --uninstall || true
    if [ -e "$WEBCAM_MODPROBE" ] || [ -e "$WEBCAM_LOAD" ]; then
        echo "Removing the virtual webcam configuration (sudo)"
        sudo rm -f "$WEBCAM_MODPROBE" "$WEBCAM_LOAD" || true
    fi
    systemctl --user daemon-reload || true
    echo "Caelestia Connect removed."
    exit 0
    ;;
--no-shell) shell=0 ;;
--webcam) webcam=1 ;;
esac

echo ":: Checking dependencies"
missing=()
"$PY" -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null || missing+=(libadwaita python-gobject)
"$PY" -c 'import cryptography' 2>/dev/null || missing+=(python-cryptography)
"$PY" -c 'import evdev' 2>/dev/null || missing+=(python-evdev)
command -v wl-copy >/dev/null || missing+=(wl-clipboard)
command -v pactl >/dev/null || missing+=(libpulse)
command -v sshfs >/dev/null || missing+=(sshfs)
command -v scrcpy >/dev/null || missing+=(scrcpy)
command -v adb >/dev/null || missing+=(android-tools android-udev)
if ((${#missing[@]})); then
    echo "   Optional/required packages missing: ${missing[*]}"
    echo "   Install with: sudo pacman -S --needed ${missing[*]}"
fi
if [ ! -w /dev/uinput ]; then
    echo "   /dev/uinput is not writable: controlling this PC from the phone will not work."
    echo "   Fix with: echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/60-uinput.rules"
fi

echo ":: Installing launchers into $BIN"
mkdir -p "$BIN" "$APPS" "$ICONS" "$UNITS" "$NAUTILUS"
for pair in "caelestia-connect:caelestia_connect.ui" "caelestia-connectd:caelestia_connect.daemon" \
    "caelestia-connect-cli:caelestia_connect.cli"; do
    name="${pair%%:*}" mod="${pair#*:}"
    cat >"$BIN/$name" <<EOF
#!/bin/sh
export PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m $mod "\$@"
EOF
    chmod +x "$BIN/$name"
done

echo ":: Installing desktop entry, icon and Nautilus extension"
sed "s|@BIN@|$BIN|g" "$SRC/data/org.caelestia.Connect.desktop" >"$APPS/org.caelestia.Connect.desktop"
cp "$SRC/data/org.caelestia.Connect.svg" "$ICONS/"
cp "$SRC/data/nautilus/caelestia_connect_nautilus.py" "$NAUTILUS/"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${ICONS%/scalable/apps}" 2>/dev/null || true

echo ":: Installing and starting the user service"
sed "s|@BIN@|$BIN|g" "$SRC/data/caelestia-connectd.service" >"$UNITS/caelestia-connectd.service"
systemctl --user daemon-reload
systemctl --user enable caelestia-connectd.service >/dev/null
systemctl --user restart caelestia-connectd.service

if ((shell)) && [ -d "$SHELL_DIR" ]; then
    echo ":: Integrating with the Caelestia shell ($SHELL_DIR)"
    if "$PY" "$SRC/caelestia/integrate.py" --shell "$SHELL_DIR"; then
        echo "   Restart the shell to load it: qs -c caelestia kill; caelestia shell -d"
    fi
fi

if command -v ufw >/dev/null && [ -r /etc/ufw/user.rules ] && ! grep -q 1714 /etc/ufw/user.rules; then
    echo ":: ufw is active without KDE Connect ports. Open them with:"
    echo "   sudo ufw allow 1714:1764/udp && sudo ufw allow 1714:1764/tcp"
fi
if ((webcam)); then
    setup_webcam || true
elif [ ! -e "$WEBCAM_MODPROBE" ]; then
    echo ":: To use the phone camera as a webcam, run: ./install.sh --webcam"
fi

echo "Done. Open \"Caelestia Connect\" from the launcher, or use the phone icon in the bar."
