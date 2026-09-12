---
name: hermes-remote-gateway
description: "CLI plugin to connect Hermes CLI to a remote Hermes gateway (hermes serve) via WebSocket with OAuth/Token/Basic Auth"
version: 0.1.0
author: DKTunnel
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [cli, remote, gateway, websocket, oauth]
    min_hermes_version: "0.21.0"
---

# Hermes Remote Gateway CLI Plugin

Connects Hermes CLI to a remote `hermes serve` backend via WebSocket, providing native CLI UX with remote execution.

## Features

- **OAuth (Nous Portal)** — browser-based sign-in, auto token refresh
- **Session Token** — static token from dashboard
- **Basic Auth** — username/password for LAN/VPN
- **Interactive chat** — streaming responses, tool calls
- **Oneshot mode** — `hermes remote chat -q "..."`
- **Profile support** — select remote profile
- **Auto-reconnect** — with exponential backoff

## Installation

```bash
hermes skills install /root/.hermes/plugins/hermes-remote-gateway
# or
hermes plugins install <path-or-repo>
```

## Configuration

```yaml
# ~/.hermes/config.yaml
remote_gateway:
  url: "https://mydomen.com"
  auth: "oauth"           # oauth | token | basic
  token: ""               # for auth: token
  username: ""            # for auth: basic
  password: ""            # for auth: basic (or env var)
  profile: "default"      # remote profile name
  auto_reconnect: true
  reconnect_attempts: 5
  reconnect_delay: 2
```

## Commands

```bash
# Connect and interactive chat
hermes remote connect

# Oneshot query
hermes remote chat -q "What is the capital of France?"

# With explicit URL (overrides config)
hermes remote chat -q "test" --url https://mydomen.com --auth oauth

# Test connection
hermes remote status

# Disconnect
hermes remote disconnect
```

## Auth Flows

### OAuth (recommended for internet)
1. Run `hermes remote connect` or `hermes remote chat -q "..."`
2. Browser opens to Nous Portal → Authorize
3. Token saved, WebSocket connects with fresh ticket per session

### Session Token
1. Get token from Dashboard → Settings → Session Token
2. `hermes config set remote_gateway.token "your-token"`
3. `hermes config set remote_gateway.auth token`

### Basic Auth (LAN/VPN only)
```bash
hermes config set remote_gateway.auth basic
hermes config set remote_gateway.username login
hermes config set remote_gateway.password "secret"
# or via env: HERMES_REMOTE_GATEWAY_PASSWORD
```

## Protocol

Implements the same WebSocket protocol as Hermes Desktop:
- `hermes-gateway-v1` subprotocol
- OAuth: `Sec-WebSocket-Protocol: hermes-gateway-ticket.<ticket>`
- Token: `?token=<session_token>` query param
- JSON-RPC 2.0 over WebSocket for chat messages