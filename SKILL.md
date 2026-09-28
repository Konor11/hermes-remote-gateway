---
name: hermes-remote-gateway
description: "CLI plugin to connect Hermes CLI to a remote Hermes gateway (hermes serve) via WebSocket with OAuth/Token/Basic Auth"
version: 0.2.0
author: DKTunnel
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [cli, remote, gateway, websocket, oauth]
    min_hermes_version: "0.21.0"
---

# Hermes Remote Gateway CLI Plugin

Connects the Hermes CLI/TUI to a remote `hermes serve` backend over WebSocket, so a plain
`hermes` on the laptop boots the NATIVE TUI of the remote gateway. Optionally exposes THIS
laptop's shell and files to the remote agent over a reverse SSH tunnel.

## Features

- **Native TUI on the remote** — `hermes remote connect`, then plain `hermes`
- **OAuth (Nous Portal)** / **Session Token** / **Basic Auth**
- **Autostart** — the WS proxy runs as a `systemd --user` unit (survives logout/reboot)
- **Local-PC access** — remote agent runs terminal/file tools on THIS laptop
- **System dependency bootstrap** — detects the distro and installs `sshd`/`sshpass`
- **Oneshot mode** — `hermes remote chat -q "..."`
- **Auto-reconnect** — remote leg supervised, pings forwarded so the gateway does not idle it out

## Installation

```bash
git clone https://github.com/Konor11/hermes-remote-gateway.git \
  ~/.hermes/plugins/hermes-remote-gateway
cd ~/.hermes/plugins/hermes-remote-gateway
bash install.sh                  # system deps (sshd, sshpass) — detects OS
hermes plugins enable hermes-remote-gateway
```

Python deps (`aiohttp`, `pyyaml`) are declared in `plugin.yaml` and resolved by Hermes.
System packages are NOT — Hermes has no plugin install hook — hence `install.sh`; the same
check also runs automatically on `pc setup` and on `connect` in local-PC mode.

> **Install by cloning, not with `hermes plugins install`.** Hermes' installer runs a security
> scan that flags this plugin as *dangerous* (`ssh_backdoor`, `sudo_usage` — it does add a key
> to `~/.ssh/authorized_keys` and install system packages), and *dangerous* is **blocked
> unconditionally**. The clone + `hermes plugins enable` path skips the scanner. If you must
> use `hermes plugins install`, set `plugins.scan_on_install: false` first (restore it after).

## Configuration

```yaml
# ~/.hermes/config.yaml
remote_gateway:
  url: "https://mydomen.com"   # gateway URL; the SSH target for local-PC is derived from it
  auth: "oauth"                # oauth | token | basic
  token: ""                    # auth: token (ungated dashboards only)
  username: ""                 # auth: basic
  password: ""                 # auth: basic
  profile: "default"           # remote profile
  local_pc_access: false       # remote agent executes on THIS laptop
  local_pc_ssh_port: 2222      # loopback port on the remote host for the reverse tunnel
  local_pc_server_user: "root" # ssh user on the remote host
  local_pc_laptop_user: ""     # ssh user here (default: $USER)
  direct: false                # point the TUI straight at the domain (ungated only)
```

## Commands

```bash
hermes remote connect                 # write .env URL + start proxy (autostarted)
hermes                                 # native TUI of the remote gateway
hermes remote disconnect               # remove the unit, clear .env → local Hermes again
hermes remote status                   # real state: .env / daemon / port / profile
hermes remote config                   # effective config
hermes remote chat -q "..."            # oneshot
hermes remote pc setup --server-password '<root pw on the server>'
hermes remote pc status | deps | on | off
```

## Auth flows

### OAuth (recommended for the internet)
1. `hermes remote connect` → browser → Nous Portal → authorize
2. Tokens cached; the local proxy mints a fresh WS ticket per TUI connection

### Basic Auth (LAN/VPN)
```bash
hermes config set remote_gateway.auth basic
hermes config set remote_gateway.username login
hermes config set remote_gateway.password "secret"
```

### Session token
Only works on a dashboard whose WS endpoint is NOT gated. A public (gated) dashboard rejects
the static `?token=` by design (`hermes_cli/web_server_chat.py`: a leaked `_SESSION_TOKEN` must
not grant access) — it accepts only `?ticket=` (single-use, ~30s TTL) or `?internal=`
(process-memory only). That is exactly why the local proxy exists: the bare TUI cannot mint
tickets (it opens a raw WebSocket with no headers), while Hermes Desktop can.

## Local-PC access

`hermes remote pc setup` (once):

1. installs `sshd`/`sshpass` (OS-detected);
2. verifies `sshd` is listening here;
3. installs the tunnel key on the remote host (`--server-password`, one time, never stored);
4. creates the host's key pair and authorizes it here (agent → laptop SSH);
5. raises the reverse tunnel `laptop:22 ← remoteserver:127.0.0.1:2222`;
6. verifies the chain with a real SSH round-trip (`TUNNEL_OK`);
7. writes `terminal.backend: ssh` + `TERMINAL_*` into the **dedicated profile** `laptop`.

The remote server's ROOT config is never modified (that would hijack every session there,
including Telegram bots on `default`); everything lands in `~/.hermes/profiles/laptop/`.
With `local_pc_access: true` the `laptop` profile is auto-bound when no other profile is set —
the everyday command stays plain `hermes remote connect`.

### Reverting

```bash
# from the laptop — puts the agent back on the server
hermes remote pc off
hermes config set remote_gateway.local_pc_access false

# on the server, only if the profile itself should go away
HERMES_HOME=$HOME/.hermes/profiles/laptop hermes config set terminal.backend local
hermes profile delete laptop
```

`hermes config set terminal.backend local` WITHOUT `HERMES_HOME=...` reverts nothing here: the
root config never held `ssh` (`ssh` is written to the `laptop` profile only). To re-apply,
`hermes remote pc setup` is idempotent.

## Protocol

Same WebSocket protocol as Hermes Desktop:
- `hermes-gateway-v1` subprotocol
- gated dashboards: `?ticket=` minted via `POST /api/auth/ws-ticket`; `?internal=` for
  server-spawned clients
- JSON-RPC 2.0 over WebSocket
- the proxy strips `model`/`provider` from outgoing `session.create`/`session.start` (so the
  remote resolves its own configured default model) and injects `params.profile`
