pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

RowLayout {
    id: avail

    required property var modelData

    Layout.fillWidth: true
    Layout.rightMargin: Tokens.padding.extraSmall
    spacing: Tokens.spacing.small

    MaterialIcon {
        text: KdeConnect.deviceIcon(avail.modelData)
    }

    StyledText {
        Layout.fillWidth: true
        text: avail.modelData.name
        elide: Text.ElideRight
    }

    IconTextButton {
        type: IconTextButton.Tonal
        verticalPadding: Tokens.padding.extraSmall
        icon: avail.modelData.pairState === "requested" ? "hourglass_top" : "link"
        text: avail.modelData.pairState === "requested" ? avail.modelData.verificationKey : KdeConnect.tr("Pair")
        onClicked: avail.modelData.pairState === "requested" ? KdeConnect.reject(avail.modelData.id) : KdeConnect.pair(avail.modelData.id)
    }
}
