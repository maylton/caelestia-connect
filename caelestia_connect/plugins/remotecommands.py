"""Trigger commands defined on the device (only useful with other desktops running KDE Connect)."""

import json

from ..i18n import _
from .base import Plugin, action


class RemoteCommandsPlugin(Plugin):
    id = "remotecommands"
    name = _("Remote commands")
    description = _("Trigger commands defined on the device")
    icon = "terminal"
    incoming = ("kdeconnect.runcommand", "kdeconnect.runcommand.output")
    outgoing = ("kdeconnect.runcommand.request",)

    def __init__(self, device):
        super().__init__(device)
        self.commands: dict = {}
        self.can_add = False
        self.output: list[str] = []

    async def connected(self):
        self.send("kdeconnect.runcommand.request", {"requestCommandList": True})

    def handle(self, pkt):
        if pkt.type == "kdeconnect.runcommand.output":
            if pkt.get_bool("commandStarted"):
                self.output = [f"$ {pkt.get_str('command')}"]
            for line in pkt.get_list("stdout") + pkt.get_list("stderr"):
                self.output.append(str(line))
            if pkt.get_bool("commandFinished"):
                self.output.append(_("[exit code {code}]").format(code=pkt.get_int("exitCode")))
            self.output = self.output[-200:]
            self.changed()
            return
        if pkt.has("commandList"):
            raw = pkt.get("commandList")
            try:
                cmds = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                cmds = {}
            self.commands = cmds if isinstance(cmds, dict) else {}
            self.can_add = pkt.get_bool("canAddCommand")
            self.changed()

    @action
    def trigger(self, key: str) -> bool:
        self.send("kdeconnect.runcommand.request", {"key": key})
        return True

    @action
    def edit_commands(self) -> bool:
        self.send("kdeconnect.runcommand.request", {"setup": True})
        return True

    def state(self):
        return {
            "commands": [{"key": k, **v} for k, v in self.commands.items() if isinstance(v, dict)],
            "canAddCommand": self.can_add,
            "output": self.output,
        }
