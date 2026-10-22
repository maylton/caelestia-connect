pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

StyledRect {
    id: request

    required property var modelData

    Layout.fillWidth: true
    Layout.rightMargin: Tokens.padding.extraSmall
    implicitHeight: requestLayout.implicitHeight + Tokens.padding.medium * 2
    radius: Tokens.rounding.large
    color: Colours.palette.m3primaryContainer

    ColumnLayout {
        id: requestLayout

        anchors.fill: parent
        anchors.margins: Tokens.padding.medium
        spacing: Tokens.spacing.small

        RowLayout {
            spacing: Tokens.spacing.small

            MaterialIcon {
                text: "phonelink_ring"
                color: Colours.palette.m3onPrimaryContainer
                fill: 1
            }

            StyledText {
                Layout.fillWidth: true
                text: `${KdeConnect.tr("Pairing request")}: ${request.modelData.name}`
                color: Colours.palette.m3onPrimaryContainer
                elide: Text.ElideRight
            }
        }

        StyledText {
            text: KdeConnect.tr("Key: %1").arg(request.modelData.verificationKey)
            color: Colours.palette.m3onPrimaryContainer
            font: Tokens.font.mono.medium
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Tokens.spacing.small

            IconTextButton {
                Layout.fillWidth: true
                type: IconTextButton.Tonal
                verticalPadding: Tokens.padding.extraSmall
                icon: "close"
                text: KdeConnect.tr("Reject")
                onClicked: KdeConnect.reject(request.modelData.id)
            }

            IconTextButton {
                Layout.fillWidth: true
                verticalPadding: Tokens.padding.extraSmall
                icon: "check"
                text: KdeConnect.tr("Accept")
                onClicked: KdeConnect.accept(request.modelData.id)
            }
        }
    }
}
