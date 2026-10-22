pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services
import "./kdeconnect"

ColumnLayout {
    id: root

    required property PopoutState popouts

    width: 320
    spacing: Tokens.spacing.small

    RowLayout {
        Layout.topMargin: Tokens.padding.medium
        Layout.rightMargin: Tokens.padding.extraSmall
        Layout.fillWidth: true

        StyledText {
            Layout.fillWidth: true
            text: "KDE Connect"
            font: Tokens.font.body.builders.medium.weight(Font.Medium).build()
        }

        IconButton {
            visible: KdeConnect.running
            type: IconButton.Text
            icon: "refresh"
            onClicked: KdeConnect.refresh()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        visible: !KdeConnect.running
        spacing: Tokens.spacing.small

        StyledText {
            text: KdeConnect.tr("Service not running")
            color: Colours.palette.m3onSurfaceVariant
            font: Tokens.font.body.small
        }

        IconTextButton {
            Layout.fillWidth: true
            inactiveColour: Colours.palette.m3primaryContainer
            inactiveOnColour: Colours.palette.m3onPrimaryContainer
            verticalPadding: Tokens.padding.extraSmall
            icon: "play_arrow"
            text: KdeConnect.tr("Start service")
            onClicked: Quickshell.execDetached(["systemctl", "--user", "start", "caelestia-connectd.service"])
        }
    }

    Repeater {
        model: ScriptModel {
            values: KdeConnect.pairRequests
        }

        PairRequestCard {}
    }

    StyledText {
        Layout.rightMargin: Tokens.padding.extraSmall
        visible: KdeConnect.running && KdeConnect.paired.length === 0 && KdeConnect.pairRequests.length === 0
        text: KdeConnect.tr("No paired devices") + " · " + KdeConnect.tr("Open KDE Connect on your phone")
        color: Colours.palette.m3onSurfaceVariant
        font: Tokens.font.body.small
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }

    Repeater {
        model: ScriptModel {
            values: [...KdeConnect.paired].sort((a, b) => b.reachable - a.reachable)
        }

        DeviceCard {
            popouts: root.popouts
        }
    }

    Repeater {
        model: ScriptModel {
            values: KdeConnect.transfers
        }

        TransferRow {}
    }

    StyledText {
        Layout.topMargin: Tokens.spacing.small
        visible: KdeConnect.available.filter(d => d.pairState !== "requestedByPeer").length > 0
        text: KdeConnect.tr("Available devices")
        color: Colours.palette.m3onSurfaceVariant
        font: Tokens.font.body.small
    }

    Repeater {
        model: ScriptModel {
            values: KdeConnect.available.filter(d => d.pairState !== "requestedByPeer")
        }

        AvailableDeviceRow {}
    }

    IconTextButton {
        Layout.fillWidth: true
        Layout.topMargin: Tokens.spacing.medium
        inactiveColour: Colours.palette.m3primaryContainer
        inactiveOnColour: Colours.palette.m3onPrimaryContainer
        verticalPadding: Tokens.padding.extraSmall
        text: KdeConnect.tr("Open app")
        icon: "phonelink_setup"

        onClicked: {
            root.popouts.hasCurrent = false;
            KdeConnect.openApp([]);
        }
    }
}
