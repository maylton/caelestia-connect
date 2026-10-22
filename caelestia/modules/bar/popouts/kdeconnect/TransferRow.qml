pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.services

ColumnLayout {
    id: transfer

    required property var modelData

    Layout.fillWidth: true
    Layout.rightMargin: Tokens.padding.extraSmall
    spacing: Tokens.spacing.extraSmall

    StyledText {
        Layout.fillWidth: true
        text: KdeConnect.tr(transfer.modelData.direction === "upload" ? "Sending %1" : "Receiving %1").arg(transfer.modelData.filename)
        elide: Text.ElideMiddle
        font: Tokens.font.body.small
    }

    StyledRect {
        Layout.fillWidth: true
        implicitHeight: 4
        radius: Tokens.rounding.full
        color: Colours.palette.m3surfaceContainerHighest

        StyledRect {
            width: parent.width * (transfer.modelData.total > 0 ? transfer.modelData.bytes / transfer.modelData.total : 0)
            height: parent.height
            radius: Tokens.rounding.full
            color: Colours.palette.m3primary
        }
    }
}
