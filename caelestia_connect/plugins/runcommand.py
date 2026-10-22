"""Run predefined commands on this computer from the device."""

import asyncio
import json
import os
import uuid

from ..i18n import _
from ..utils import spawn
from .base import Plugin, action

DEFAULT_COMMANDS = {
    "lock": {"name": _("Lock screen"), "command": "loginctl lock-session"},
    "suspend": {"name": _("Suspend"), "command": "systemctl suspend"},
    "mute": {"name": _("Mute / unmute"), "command": "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"},
    "screenshot": {"name": _("Screenshot"), "command": "caelestia screenshot"},
}


class RunCommandPlugin(Plugin):
    id = "runcommand"
    name = _("Run commands")
    description = _("Run predefined commands on this computer from the device")
    icon = "terminal"
    incoming = ("kdeconnect.runcommand.request",)
    outgoing = ("kdeconnect.runcommand", "kdeconnect.runcommand.output")
    settings_schema = [
        {"key": "commands", "type": "commands", "label": _("Commands"), "default": DEFAULT_COMMANDS},
    ]

    def __init__(self, device):
        super().__init__(device)
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self._next_id = 0

    def destroy(self):
        for p in self.processes.values():
            if p.returncode is None:
                p.terminate()

    def commands(self) -> dict:
        cmds = self.setting("commands")
        return cmds if isinstance(cmds, dict) else {}

    def settings_changed(self, key):
        if key == "commands":
            self._send_list()

    async def connected(self):
        self._send_list()

    def _send_list(self):
        self.send("kdeconnect.runcommand", {"commandList": json.dumps(self.commands()), "canAddCommand": True})

    async def handle(self, pkt):
        if pkt.get_bool("requestCommandList"):
            self._send_list()
        elif pkt.has("key"):
            cmd = self.commands().get(pkt.get_str("key"))
            if isinstance(cmd, dict) and cmd.get("command"):
                spawn(self._run(cmd["command"]))
        elif pkt.has("setup"):
            from ..utils import run_detached
            import sys

            run_detached(sys.executable, "-m", "caelestia_connect.ui", "--device", self.device.id, "--page", "settings")
        elif pkt.has("stop"):
            for p in self.processes.values():
                if p.returncode is None:
                    p.terminate()

    async def _run(self, command: str):
        cid = self._next_id
        self._next_id += 1
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.path.expanduser("~"),
        )
        self.processes[cid] = proc
        self.send("kdeconnect.runcommand.output", {"commandStarted": True, "command": command, "id": cid})

        async def pump(stream, key):
            batch = []
            while True:
                line = await stream.readline()
                if not line:
                    break
                batch.append(line.decode(errors="replace").rstrip("\n"))
                if len(batch) == 5:
                    self._output(cid, key, batch)
                    batch = []
            if batch:
                self._output(cid, key, batch)

        await asyncio.gather(pump(proc.stdout, "stdout"), pump(proc.stderr, "stderr"))
        code = await proc.wait()
        self.processes.pop(cid, None)
        self.send(
            "kdeconnect.runcommand.output", {"commandFinished": True, "success": code == 0, "exitCode": code, "id": cid}
        )

    def _output(self, cid, key, lines):
        body = {"commandOutput": True, "stdout": [], "stderr": [], "id": cid}
        body[key] = lines
        self.send("kdeconnect.runcommand.output", body)

    @action
    def add_command(self, name: str, command: str) -> str:
        cmds = dict(self.commands())
        key = uuid.uuid4().hex[:12]
        cmds[key] = {"name": name, "command": command}
        self.set_setting("commands", cmds)
        return key

    @action
    def remove_command(self, key: str) -> bool:
        cmds = dict(self.commands())
        if cmds.pop(key, None) is None:
            return False
        self.set_setting("commands", cmds)
        return True

    @action
    def edit_command(self, key: str, name: str, command: str) -> bool:
        cmds = dict(self.commands())
        if key not in cmds:
            return False
        cmds[key] = {"name": name, "command": command}
        self.set_setting("commands", cmds)
        return True

    def state(self):
        return {"commands": [{"key": k, **v} for k, v in self.commands().items() if isinstance(v, dict)]}
