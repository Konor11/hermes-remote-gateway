"""Local-PC access: let the REMOTE Hermes agent run commands and read/write
files on THIS laptop.

How it works
------------
The agent runs on the remote dashboard host. Hermes has an SSH terminal
backend: when it is active, both the shell tool AND the file tools
(ShellFileOperations) execute over SSH. So the remote agent can work on the
laptop's files exactly as if it were local.

The laptop is behind NAT, so the remote host cannot dial in. We therefore open
a REVERSE SSH tunnel from the laptop to the remote host:

    laptop:22  <===  remote:127.0.0.1:<port>  <=== `ssh -N -R 127.0.0.1:<port>:127.0.0.1:22 <server>`

Then the remote host's config points the terminal backend at that loopback port.

This module owns:
  * the sshd check on the laptop,
  * the two key pairs (laptop->server for the tunnel, server->laptop for the agent),
  * the reverse-tunnel supervisor (auto-restart on drop),
  * the server-side config snippet/apply.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_TUNNEL_KEY = Path.home() / ".ssh" / "hermes_remote_gateway_tunnel"
_SERVER_KEY_BASENAME = "hermes_laptop_access"
_LOG = _HOME / "remote-gateway-tunnel.log"


def _log(msg: str) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def server_ssh_host(url: str) -> str:
    """Hostname of the remote gateway host (ssh target for the reverse tunnel)."""
    host = url.strip()
    for pre in ("https://", "http://", "wss://", "ws://"):
        if host.startswith(pre):
            host = host[len(pre):]
    return host.split("/")[0].split(":")[0]


class LocalPCAccess:
    """Reverse-tunnel manager + one-time setup for local-PC access."""

    def __init__(self, url: str, port: int = 2222, server_user: str = "root",
                 laptop_user: str = "", ssh_port: int = 22):
        self.url = url
        self.port = port                       # loopback port on the REMOTE host
        self.server_user = server_user or "root"
        self.laptop_user = laptop_user or os.environ.get("USER") or "user"
        self.ssh_port = ssh_port               # the laptop's own sshd port
        self.server_host = server_ssh_host(url)
        self._proc: Optional[subprocess.Popen] = None
        self._stopping = False

    # ---- laptop sshd -------------------------------------------------

    def sshd_listening(self) -> bool:
        for probe in (["ss", "-tln"], ["netstat", "-tln"]):
            if shutil.which(probe[0]):
                out = _run(probe + []).stdout
                if any(f":{self.ssh_port}" in ln for ln in out.splitlines()):
                    return True
        # Fall back to a connect probe.
        try:
            with socket.create_connection(("127.0.0.1", self.ssh_port), timeout=2):
                return True
        except OSError:
            return False

    def ensure_sshd(self) -> tuple[bool, str]:
        """Best-effort: make sure an SSH server is running on the laptop."""
        if self.sshd_listening():
            return True, "sshd already listening"
        if not shutil.which("sshd") and not Path("/usr/sbin/sshd").exists():
            return False, ("no sshd installed — run: sudo apt install openssh-server "
                           "(or your distro's equivalent)")
        for cmd in (["sudo", "systemctl", "enable", "--now", "ssh"],
                    ["sudo", "systemctl", "enable", "--now", "sshd"],
                    ["sudo", "service", "ssh", "start"]):
            if not shutil.which(cmd[0]):
                continue
            _run(cmd)
            if self.sshd_listening():
                return True, f"sshd started via {' '.join(cmd)}"
        return False, "could not start sshd (tried systemctl ssh/sshd, service ssh)"

    # ---- keys --------------------------------------------------------

    def ensure_tunnel_key(self) -> Path:
        """Key the LAPTOP uses to log in to the REMOTE host (for `ssh -R`)."""
        _TUNNEL_KEY.parent.mkdir(parents=True, exist_ok=True)
        if not _TUNNEL_KEY.exists():
            _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
                  "hermes-remote-gateway-tunnel", "-f", str(_TUNNEL_KEY)])
        return _TUNNEL_KEY

    def tunnel_pubkey(self) -> str:
        return (_TUNNEL_KEY.with_suffix(".pub")).read_text(encoding="utf-8").strip()

    def _ssh(self, remote_cmd: str, password: str = "", timeout: int = 30) -> subprocess.CompletedProcess:
        base = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={timeout}"]
        if password:
            if not shutil.which("sshpass"):
                raise RuntimeError("sshpass is required to use --server-password")
            return _run(["sshpass", "-p", password] + base +
                        [f"{self.server_user}@{self.server_host}", remote_cmd], timeout=timeout + 15)
        if _TUNNEL_KEY.exists():
            base += ["-i", str(_TUNNEL_KEY)]
        return _run(base + [f"{self.server_user}@{self.server_host}", remote_cmd], timeout=timeout + 15)

    def install_tunnel_key_on_server(self, password: str = "") -> tuple[bool, str]:
        """Append the laptop's tunnel pubkey to the remote host's authorized_keys."""
        pub = self.tunnel_pubkey()
        cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
               "chmod 600 ~/.ssh/authorized_keys && "
               f"grep -qxF {_shq(pub)} ~/.ssh/authorized_keys || echo {_shq(pub)} >> ~/.ssh/authorized_keys; "
               "echo INSTALLED")
        res = self._ssh(cmd, password=password)
        return ("INSTALLED" in res.stdout), (res.stdout or res.stderr).strip()

    # ---- server-side agent key + config ------------------------------

    def server_agent_key(self) -> tuple[bool, str]:
        """Create the REMOTE host's key it uses to SSH into the laptop, and return its pubkey."""
        cmd = (
            f"test -f ~/.ssh/{_SERVER_KEY_BASENAME} || "
            f"ssh-keygen -t ed25519 -N '' -C hermes-laptop-access -f ~/.ssh/{_SERVER_KEY_BASENAME}; "
            f"cat ~/.ssh/{_SERVER_KEY_BASENAME}.pub"
        )
        res = self._ssh(cmd)
        out = res.stdout.strip()
        return bool(out), out

    def authorize_server_key_locally(self, pubkey: str) -> bool:
        ak = Path.home() / ".ssh" / "authorized_keys"
        ak.parent.mkdir(parents=True, exist_ok=True)
        existing = ak.read_text(encoding="utf-8") if ak.exists() else ""
        if pubkey.strip() and pubkey.strip() not in existing:
            with open(ak, "a", encoding="utf-8") as fh:
                fh.write(pubkey.strip() + "\n")
        os.chmod(ak, 0o600)
        return True

    def apply_server_config(self, apply: bool = True) -> str:
        """Write the remote host's terminal backend + awareness note. Returns the YAML snippet."""
        snippet = (
            "terminal:\n"
            "  backend: ssh\n"
            f"  ssh_host: 127.0.0.1\n"
            f"  ssh_port: {self.port}\n"
            f"  ssh_user: {self.laptop_user}\n"
            f"  ssh_key: ~/.ssh/{_SERVER_KEY_BASENAME}\n"
            f"  cwd: /home/{self.laptop_user}\n"
            "agent:\n"
            "  system_prompt: |\n"
            "    ## Execution host\n"
            f"    Your terminal and file tools execute on the user's LAPTOP "
            f"({self.laptop_user}@local-pc) over SSH, NOT on this server. All paths are the "
            "laptop's paths (its home directory is the working dir). Other Hermes features "
            "(skills, MCP, browser) still run on the server.\n"
        )
        if not apply:
            return snippet
        py = (
            "import yaml,pathlib\n"
            "p=pathlib.Path.home()/'.hermes'/'config.yaml'\n"
            "cfg=yaml.safe_load(p.read_text()) or {}\n"
            "t=cfg.setdefault('terminal',{})\n"
            f"t.update({{'backend':'ssh','ssh_host':'127.0.0.1','ssh_port':{self.port},"
            f"'ssh_user':{self.laptop_user!r},'ssh_key':'~/.ssh/{_SERVER_KEY_BASENAME}',"
            f"'cwd':'/home/{self.laptop_user}'}})\n"
            "a=cfg.setdefault('agent',{})\n"
            f"a['system_prompt']=({self._awareness_note()!r})\n"
            "p.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))\n"
            "print('CONFIG_APPLIED')\n"
        )
        res = self._ssh("python3 - <<'PYEOF'\n" + py + "PYEOF")
        return snippet + ("\n" + ("applied ✅" if "CONFIG_APPLIED" in res.stdout else
                                  f"apply failed: {res.stderr.strip()[:200]}"))

    def _awareness_note(self) -> str:
        return (
            "## Execution host\n"
            f"Your terminal and file tools execute on the user's LAPTOP ({self.laptop_user}@local-pc) "
            "over SSH, NOT on this server. All paths you see are the laptop's paths (its home "
            "directory is the working directory). Other Hermes features (skills, MCP, browser) "
            "still run on the server."
        )

    # ---- tunnel ------------------------------------------------------

    def tunnel_argv(self) -> list[str]:
        return [
            "ssh", "-N", "-T",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-i", str(_TUNNEL_KEY),
            "-R", f"127.0.0.1:{self.port}:127.0.0.1:{self.ssh_port}",
            f"{self.server_user}@{self.server_host}",
        ]

    def tunnel_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        # Another process may own it (e.g. started by the daemon).
        if shutil.which("pgrep"):
            res = _run(["pgrep", "-f", f"-R 127.0.0.1:{self.port}:127.0.0.1:{self.ssh_port}"])
            return bool(res.stdout.strip())
        return False

    def start_tunnel(self) -> bool:
        """Start the reverse tunnel as a supervised child (blocking spawn)."""
        if self.tunnel_running():
            return True
        self._stopping = False
        _log(f"starting tunnel: {' '.join(self.tunnel_argv())}")
        self._proc = subprocess.Popen(
            self.tunnel_argv(),
            stdout=open(_LOG, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        return self._proc.poll() is None

    def supervise(self) -> None:
        """Blocking: keep the tunnel up, restarting it after drops."""
        while not self._stopping:
            try:
                if not self.start_tunnel():
                    _log("tunnel exited immediately; retrying in 5s")
                    time.sleep(5)
                    continue
                assert self._proc is not None
                self._proc.wait()
                if self._stopping:
                    break
                _log(f"tunnel dropped (rc={self._proc.returncode}); reconnecting in 3s")
            except Exception as exc:  # noqa: BLE001
                _log(f"tunnel supervisor error: {exc}")
            time.sleep(3)

    def stop_tunnel(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if shutil.which("pkill"):
            _run(["pkill", "-f", f"-R 127.0.0.1:{self.port}:127.0.0.1:{self.ssh_port}"])

    # ---- status ------------------------------------------------------

    def status(self) -> dict:
        return {
            "server": f"{self.server_user}@{self.server_host}",
            "laptop_user": self.laptop_user,
            "port": self.port,
            "sshd": self.sshd_listening(),
            "tunnel": self.tunnel_running(),
            "tunnel_key": _TUNNEL_KEY.exists(),
        }


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


async def supervise_async(access: LocalPCAccess) -> None:
    """Async wrapper so the proxy daemon can own the supervisor task."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, access.supervise)
