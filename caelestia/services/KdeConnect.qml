pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io

// Live bridge to caelestia-connectd (KDE Connect compatible daemon) over its Unix socket.
Singleton {
    id: root

    property var state: ({})
    readonly property bool running: socket.connected && state.devices !== undefined
    readonly property var devices: state.devices ?? []
    readonly property var paired: devices.filter(d => d.paired)
    readonly property var connected: devices.filter(d => d.paired && d.reachable)
    readonly property var available: devices.filter(d => !d.paired && d.reachable)
    readonly property var pairRequests: devices.filter(d => d.pairState === "requestedByPeer")
    readonly property var primary: connected[0] ?? paired[0] ?? null
    readonly property var transfers: (state.transfers ?? []).filter(t => t.state === "pending" || t.state === "running")
    // The bar icon is only shown once something is paired or wants to pair
    readonly property bool shown: running && (paired.length > 0 || pairRequests.length > 0)

    readonly property string appPath: `${Quickshell.env("HOME")}/.local/bin/caelestia-connect`
    readonly property string socketPath: `${Quickshell.env("XDG_RUNTIME_DIR")}/caelestia-connect/ipc.sock`

    property int _nextId: 1
    property var _callbacks: ({})

    function call(method: string, params: var, callback: var): void {
        if (!socket.connected)
            return;
        const id = _nextId++;
        if (callback)
            _callbacks[id] = callback;
        socket.write(JSON.stringify({
            id: id,
            method: method,
            params: params ?? {}
        }) + "\n");
        socket.flush();
    }

    function plugin(device: string, pluginId: string, action: string, args: var, callback: var): void {
        call("device.call", {
            device: device,
            plugin: pluginId,
            action: action,
            args: args ?? {}
        }, callback);
    }

    function loaded(device: var, pluginId: string): bool {
        return device?.plugins?.some(p => p.id === pluginId && p.loaded) ?? false;
    }

    function battery(device: var): int {
        return device?.state?.battery?.charge ?? -1;
    }

    function charging(device: var): bool {
        return device?.state?.battery?.charging ?? false;
    }

    function signal(device: var): int {
        return device?.state?.connectivity_report?.strength ?? -1;
    }

    function deviceIcon(device: var): string {
        switch (device?.type) {
        case "tablet":
            return "tablet_android";
        case "tv":
            return "tv";
        case "laptop":
            return "laptop";
        case "desktop":
            return "desktop_windows";
        default:
            return "smartphone";
        }
    }

    function batteryIcon(charge: int, isCharging: bool): string {
        if (charge < 0)
            return "battery_unknown";
        if (isCharging)
            return "battery_charging_full";
        if (charge >= 95)
            return "battery_full";
        return `battery_${Math.max(0, Math.min(6, Math.floor(charge * 7 / 100)))}_bar`;
    }

    function signalIcon(strength: int): string {
        if (strength < 0)
            return "signal_cellular_off";
        return `signal_cellular_${Math.max(0, Math.min(4, strength))}_bar`;
    }

    function openApp(args: list<string>): void {
        Quickshell.execDetached([appPath, ...(args ?? [])]);
    }

    function refresh(): void {
        call("refresh", {}, null);
    }

    function pair(id: string): void {
        call("device.pair", { device: id }, null);
    }

    function accept(id: string): void {
        call("device.accept", { device: id }, null);
    }

    function reject(id: string): void {
        call("device.reject", { device: id }, null);
    }

    function ping(id: string): void {
        plugin(id, "ping", "ping", {}, null);
    }

    function ring(id: string): void {
        plugin(id, "findmyphone", "ring", {}, null);
    }

    function sendClipboard(id: string): void {
        plugin(id, "clipboard", "send_clipboard", {}, null);
    }

    function browse(id: string): void {
        plugin(id, "sftp", "browse", {}, null);
    }

    function sendFiles(id: string): void {
        openApp(["--device", id, "--pick-files"]);
    }

    function media(id: string, action: string): void {
        plugin(id, "mprisremote", "media_action", { action: action }, null);
    }

    function _handle(line: string): void {
        let msg;
        try {
            msg = JSON.parse(line);
        } catch (e) {
            return;
        }
        if (msg.event === "state") {
            root.state = msg.data;
        } else if (msg.id !== undefined) {
            if (msg.id === 0 && msg.result)
                root.state = msg.result;
            const cb = _callbacks[msg.id];
            if (cb) {
                delete _callbacks[msg.id];
                cb(msg.result, msg.error);
            }
        }
    }

    // Minimal catalogue so the shell UI follows the system language like the rest of Caelestia
    readonly property bool _pt: (Quickshell.env("LANG") ?? "").startsWith("pt")
    readonly property var _ptStrings: ({
            "Phone": "Celular",
            "Connected": "Conectado",
            "Disconnected": "Desconectado",
            "Charging": "Carregando",
            "Ping": "Ping",
            "Ring": "Tocar",
            "Send files": "Enviar arquivos",
            "Clipboard": "Área de transferência",
            "Files": "Arquivos",
            "Messages": "Mensagens",
            "Open app": "Abrir aplicativo",
            "Pair": "Parear",
            "Accept": "Aceitar",
            "Reject": "Recusar",
            "Pairing request": "Pedido de pareamento",
            "Key: %1": "Chave: %1",
            "Available devices": "Dispositivos disponíveis",
            "No paired devices": "Nenhum dispositivo pareado",
            "Open KDE Connect on your phone": "Abra o KDE Connect no celular",
            "%1 notifications": "%1 notificações",
            "1 notification": "1 notificação",
            "Sending %1": "Enviando %1",
            "Receiving %1": "Recebendo %1",
            "Ping sent": "Ping enviado",
            "Clipboard sent": "Área de transferência enviada",
            "Ringing %1": "Tocando %1",
            "Service not running": "Serviço não está em execução",
            "Start service": "Iniciar serviço",
            "Mobile": "Celular",
            "Remote input": "Controle remoto",
            "%1 unread": "%1 não lidas",
            "Search": "Buscar"
        })

    function tr(text: string): string {
        return (_pt && _ptStrings[text]) ? _ptStrings[text] : text;
    }

    Socket {
        id: socket

        path: root.socketPath
        connected: true

        onConnectedChanged: {
            if (connected) {
                socket.write(JSON.stringify({
                    id: 0,
                    method: "subscribe",
                    params: {}
                }) + "\n");
                socket.flush();
            } else {
                root.state = {};
                root._callbacks = {};
            }
        }

        parser: SplitParser {
            onRead: data => root._handle(data)
        }
    }

    property bool _startRequested: false

    Timer {
        // Reconnect when the daemon (re)starts. The shell starts it once per session itself: without
        // uwsm, Hyprland never reaches graphical-session.target, so the user unit would not start
        interval: 3000
        repeat: true
        running: !socket.connected
        onTriggered: {
            if (!root._startRequested) {
                root._startRequested = true;
                Quickshell.execDetached(["systemctl", "--user", "start", "caelestia-connectd.service"]);
            }
            socket.connected = false;
            socket.connected = true;
        }
    }
}
