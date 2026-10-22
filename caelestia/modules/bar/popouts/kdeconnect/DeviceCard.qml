pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services
import ".."

ColumnLayout {
    id: dev

    required property var modelData
    required property PopoutState popouts
    readonly property var st: modelData.state ?? {}
    readonly property int charge: KdeConnect.battery(modelData)
    readonly property var player: {
        const m = st.mprisremote;
        if (!m || !m.players?.length)
            return null;
        return m.players.find(p => p.name === m.current) ?? m.players[0];
    }

    Layout.fillWidth: true
    Layout.rightMargin: Tokens.padding.extraSmall
    spacing: Tokens.spacing.small
    opacity: modelData.reachable ? 1 : 0.6

    RowLayout {
        Layout.fillWidth: true
        spacing: Tokens.spacing.medium

        StyledRect {
            implicitWidth: implicitHeight
            implicitHeight: devIcon.implicitHeight + Tokens.padding.small * 2
            radius: Tokens.rounding.full
            color: dev.modelData.reachable ? Colours.palette.m3primaryContainer : Colours.palette.m3surfaceContainerHighest

            MaterialIcon {
                id: devIcon

                anchors.centerIn: parent
                text: KdeConnect.deviceIcon(dev.modelData)
                color: dev.modelData.reachable ? Colours.palette.m3onPrimaryContainer : Colours.palette.m3onSurfaceVariant
                fontStyle: Tokens.font.icon.medium
                fill: 1
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0

            StyledText {
                Layout.fillWidth: true
                text: dev.modelData.name
                elide: Text.ElideRight
                font: Tokens.font.body.builders.medium.weight(Font.Medium).build()
            }

            StyledText {
                Layout.fillWidth: true
                elide: Text.ElideRight
                color: Colours.palette.m3onSurfaceVariant
                font: Tokens.font.body.small
                text: {
                    if (!dev.modelData.reachable)
                        return KdeConnect.tr("Disconnected");
                    const parts = [KdeConnect.tr("Connected")];
                    const unread = dev.st.sms?.unread ?? 0;
                    if (unread > 0)
                        parts.push(KdeConnect.tr("%1 unread").arg(unread));
                    return parts.join(" · ");
                }
            }
        }

        MaterialIcon {
            visible: dev.modelData.reachable && KdeConnect.signal(dev.modelData) >= 0
            text: KdeConnect.signalIcon(KdeConnect.signal(dev.modelData))
            color: Colours.palette.m3onSurfaceVariant
        }

        MaterialIcon {
            visible: dev.modelData.reachable && dev.charge >= 0
            text: KdeConnect.batteryIcon(dev.charge, KdeConnect.charging(dev.modelData))
            color: dev.charge <= 15 && !KdeConnect.charging(dev.modelData) ? Colours.palette.m3error : Colours.palette.m3onSurfaceVariant
            fill: 1
        }

        StyledText {
            visible: dev.modelData.reachable && dev.charge >= 0
            text: `${dev.charge}%`
            color: Colours.palette.m3onSurfaceVariant
            font: Tokens.font.body.small
        }
    }

    // Quick actions
    RowLayout {
        Layout.fillWidth: true
        visible: dev.modelData.reachable
        spacing: Tokens.spacing.small

        Action {
            icon: "notifications_active"
            available: KdeConnect.loaded(dev.modelData, "ping")
            onTriggered: {
                KdeConnect.ping(dev.modelData.id);
                Toaster.toast(KdeConnect.tr("Ping sent"), dev.modelData.name, "notifications_active", Toast.Info);
            }
        }
        Action {
            icon: "ring_volume"
            available: KdeConnect.loaded(dev.modelData, "findmyphone")
            onTriggered: {
                KdeConnect.ring(dev.modelData.id);
                Toaster.toast(KdeConnect.tr("Ringing %1").arg(dev.modelData.name), "", "ring_volume", Toast.Info);
            }
        }
        Action {
            icon: "upload_file"
            available: KdeConnect.loaded(dev.modelData, "share")
            onTriggered: {
                dev.popouts.hasCurrent = false;
                KdeConnect.sendFiles(dev.modelData.id);
            }
        }
        Action {
            icon: "content_paste_go"
            available: KdeConnect.loaded(dev.modelData, "clipboard")
            onTriggered: {
                KdeConnect.sendClipboard(dev.modelData.id);
                Toaster.toast(KdeConnect.tr("Clipboard sent"), dev.modelData.name, "content_paste_go", Toast.Info);
            }
        }
        Action {
            icon: "folder_open"
            available: KdeConnect.loaded(dev.modelData, "sftp")
            onTriggered: {
                dev.popouts.hasCurrent = false;
                KdeConnect.browse(dev.modelData.id);
            }
        }
        Action {
            icon: "sms"
            available: KdeConnect.loaded(dev.modelData, "sms")
            onTriggered: {
                dev.popouts.hasCurrent = false;
                KdeConnect.openApp(["--device", dev.modelData.id, "--page", "messages"]);
            }
        }
    }

    // Media playing on the device
    StyledRect {
        Layout.fillWidth: true
        visible: dev.modelData.reachable && dev.player !== null
        implicitHeight: mediaRow.implicitHeight + Tokens.padding.small * 2
        radius: Tokens.rounding.large
        color: Colours.tPalette.m3surfaceContainerHigh

        RowLayout {
            id: mediaRow

            anchors.fill: parent
            anchors.margins: Tokens.padding.small
            spacing: Tokens.spacing.small

            ClipRect {
                implicitWidth: 36
                implicitHeight: 36
                radius: Tokens.rounding.small
                color: Colours.palette.m3surfaceContainerHighest

                Image {
                    anchors.fill: parent
                    visible: (dev.player?.art ?? "") !== ""
                    source: dev.player?.art ? `file://${dev.player.art}` : ""
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    sourceSize.width: 72
                    sourceSize.height: 72
                }

                MaterialIcon {
                    anchors.centerIn: parent
                    visible: !(dev.player?.art)
                    text: "music_note"
                    color: Colours.palette.m3onSurfaceVariant
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                StyledText {
                    Layout.fillWidth: true
                    text: dev.player?.title || dev.player?.name || ""
                    elide: Text.ElideRight
                }

                StyledText {
                    Layout.fillWidth: true
                    text: dev.player?.artist || dev.player?.name || ""
                    elide: Text.ElideRight
                    color: Colours.palette.m3onSurfaceVariant
                    font: Tokens.font.body.small
                }
            }

            IconButton {
                type: IconButton.Text
                icon: "skip_previous"
                onClicked: KdeConnect.media(dev.modelData.id, "Previous")
            }
            IconButton {
                type: IconButton.Text
                icon: dev.player?.playing ? "pause" : "play_arrow"
                onClicked: KdeConnect.media(dev.modelData.id, "PlayPause")
            }
            IconButton {
                type: IconButton.Text
                icon: "skip_next"
                onClicked: KdeConnect.media(dev.modelData.id, "Next")
            }
        }
    }

    StyledText {
        readonly property int count: dev.st.notifications?.items?.length ?? 0

        visible: dev.modelData.reachable && count > 0
        text: count === 1 ? KdeConnect.tr("1 notification") : KdeConnect.tr("%1 notifications").arg(count)
        color: Colours.palette.m3onSurfaceVariant
        font: Tokens.font.body.small
    }

    component Action: IconButton {
        property bool available: true
        signal triggered

        visible: available
        type: IconButton.Tonal
        onClicked: triggered()
    }
}
