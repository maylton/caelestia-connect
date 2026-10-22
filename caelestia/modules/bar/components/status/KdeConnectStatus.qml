pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.services

Item {
    id: root

    required property color colour

    readonly property var device: KdeConnect.primary
    readonly property bool busy: KdeConnect.pairRequests.length > 0 || KdeConnect.transfers.length > 0
    readonly property int charge: KdeConnect.battery(device)

    // Collapses to nothing until a device is paired (or asks to pair), like the lock status icons
    property real shownHeight: KdeConnect.shown ? layout.implicitHeight : 0

    implicitWidth: layout.implicitWidth
    implicitHeight: Math.round(shownHeight)
    clip: true

    Behavior on shownHeight {
        Anim {
            type: Anim.SlowEffects
        }
    }

    ColumnLayout {
        id: layout

        anchors.centerIn: parent
        spacing: Tokens.spacing.medium / 2
        scale: KdeConnect.shown ? 1 : 0.5
        opacity: KdeConnect.shown ? 1 : 0

        Behavior on opacity {
            Anim {
                type: Anim.DefaultEffects
            }
        }

        Behavior on scale {
            Anim {}
        }

        MaterialIcon {
            id: icon

            animate: true
            text: {
                if (KdeConnect.pairRequests.length > 0)
                    return "phonelink_ring";
                if (!root.device)
                    return "phonelink";
                return root.device.reachable ? KdeConnect.deviceIcon(root.device) : "phonelink_off";
            }
            color: root.colour
            fill: root.device?.reachable ? 1 : 0

            SequentialAnimation on opacity {
                running: root.busy
                alwaysRunToEnd: true
                loops: Animation.Infinite

                Anim {
                    from: 1
                    to: 0.25
                    duration: Tokens.anim.durations.large
                    easing: Tokens.anim.standardAccel
                }
                Anim {
                    from: 0.25
                    to: 1
                    duration: Tokens.anim.durations.large
                    easing: Tokens.anim.standardDecel
                }
            }
        }

        // Low battery warning of the phone
        Loader {
            Layout.alignment: Qt.AlignHCenter
            active: !!root.device?.reachable && root.charge >= 0 && root.charge <= 15 && !KdeConnect.charging(root.device)
            visible: active

            sourceComponent: MaterialIcon {
                animate: true
                text: "battery_alert"
                color: Colours.palette.m3error
                fill: 1
            }
        }
    }
}
