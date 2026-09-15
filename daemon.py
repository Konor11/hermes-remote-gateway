"""
Hermes Remote Gateway Plugin - LocalWebSocket Proxy Daemon

Listens on 127.0.0.1:<port> and proxies JSON-RPC frames to the remote Hermes
gateway (/api/ws). The native Hermes TUI connects to the LOCAL endpoint (via
HERMES_TUI_GATEWAY_URL written to ~/.hermes/.env at `hermes remote connect`),
so Hermes never knows about the remote host. The daemon mints fresh WS tickets
and keeps the remote session alive until `hermes remote disconnect`.

Wire protocol is identical both legs: newline-delimited JSON-RPC with PING
heartbeats, exactly like tui_gateway/ws.py. So the local TUI can't tell the
difference between this proxy and a local gateway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

# Bootstrapping: this module is run both as part of the Hermes plugin package
# (via hermes_plugins.hermes_remote_gateway) AND as a standalone daemon process
# (`python /abs/path/daemon.py`). When run standalone the package name is None,
# so add the plugin dir to sys.path and import the sibling modules flat, with a
# fallback to relative (package) imports when loaded by Hermes.
if __name__ == "__main__" and __package__ in (None, ""):
    # Standalone run (python /abs/path/daemon.py). Register the plugin dir as
    # a namespace package so all sibling relative imports (from .config, etc.)
    # resolve, then import ourselves through it and delegate to main().
    import importlib
    import types
    _root = Path(__file__).resolve().parent
    _pkg = types.ModuleType("hermes_remote_gateway")
    _pkg.__path__ = [str(_root)]
    _pkg.__package__ = "hermes_remote_gateway"
    sys.modules["hermes_remote_gateway"] = _pkg
    _mod = importlib.import_module("hermes_remote_gateway.daemon")
    sys.modules["__main__"] = _mod
    _mod.main()
    sys.exit(0)

from .auth import RemoteGatewayAuth
from .config import RemoteGatewayConfig

_log = logging.getLogger("hermes_remote_gateway.daemon")

# Default state file (matches config path resolution).
def _default_state_file() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "remote-gateway.json"


class RemoteGatewayProxy:
    def __init__(self, config: RemoteGatewayConfig, state_file: Path | None = None):
        self.config = config
        self.auth = RemoteGatewayAuth(config)
        self.state_file = state_file or _default_state_file()
        self._pending_tunnels: set = set()
        self._session: Optional[aiohttp.ClientSession] = None

    # ---- JSON-RPC frame helpers ------------------------------------

    def _route_frame(self, frame: dict) -> Optional[dict]:
        """Apply proxy-side adjustments for frames in flight.

        Return None → forward the frame to the remote gateway; return a dict →
        answer locally and/or skip forwarding.

        IMPORTANT: `gateway.ping` MUST be forwarded to the remote gateway, not
        answered locally. The remote gateway uses the client's periodic pings as
        its liveness signal; if we swallow them it thinks the (proxy) client has
        gone silent and closes the idle tunnel after ~40s — which is exactly why
        the TUI kept dying mid-session. Forwarding pings keeps the remote side
        alive; its pong is relayed back to the TUI.
        """
        if not isinstance(frame, dict):
            return None
        return None

    # ---- WebSocket tunnel to remote gateway ------------------------

    async def _open_remote_tunnel(self) -> aiohttp.ClientWebSocketResponse:
        ws_url = await asyncio.wait_for(self.auth.get_websocket_url(), timeout=30)
        # Reuse ONE ClientSession across reconnects; a new session per tunnel
        # leaked ("Unclosed client session") and accumulated on every drop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None))
        ws = await self._session.ws_connect(
            ws_url,
            protocols=["hermes-gateway-v1"],
            autoping=False,
        )
        return ws

    async def _proxy_leg(self, local_ws: web.WebSocketResponse,
                         remote_ws: aiohttp.ClientWebSocketResponse,
                         name: str) -> None:
        """Forward one direction: local->remote (TUI sends JSON-RPC)."""
        try:
            async for msg in local_ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    reply = self._route_frame(frame)
                    if reply is not None:
                        await local_ws.send_json(reply)
                        continue
                    await remote_ws.send_str(msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    await remote_ws.send_bytes(msg.data)
                elif msg.type == web.WSMsgType.PING:
                    await remote_ws.ping()
                elif msg.type == web.WSMsgType.PONG:
                    await remote_ws.pong()
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING,
                                  web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
        except Exception as exc:  # noqa: BLE001
            _log.debug("[daemon] %s leg ended: %s", name, exc)

    async def _proxy_remote_to_local(self, local_ws: web.WebSocketResponse,
                                     remote_ws: aiohttp.ClientWebSocketResponse,
                                     name: str) -> None:
        """Forward remote->local (tui_gateway events/replies)."""
        try:
            async for msg in remote_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await local_ws.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await local_ws.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.PING:
                    await local_ws.send_bytes(msg.data)  # raw websocket ping
                elif msg.type == aiohttp.WSMsgType.PONG:
                    await local_ws.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception as exc:  # noqa: BLE001
            _log.debug("[daemon] %s remote leg ended: %s", name, exc)

    async def _handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        # NOTE: do NOT declare `protocols=[...]` here. The native TUI connects
        # via undici WebSocket with NO Sec-WebSocket-Protocol header (see
        # gatewayClient.ts: new WebSocket(attachUrl)). If we declare a required
        # subprotocol, aiohttp rejects the handshake with 403 when the client
        # omits it, and the TUI falls back to spawning a local gateway. Accept
        # any client; the remote leg still sends hermes-gateway-v1.
        local_ws = web.WebSocketResponse(autoping=False)
        await local_ws.prepare(request)
        _log.info("[daemon] local client connected: %s %s",
                  request.remote, request.headers.get("Sec-WebSocket-Protocol", "(no subprotocol)"))

        # IMPORTANT: the local (TUI) socket must stay OPEN for the whole
        # session. Only the REMOTE leg is reconnected when it drops — a killed
        # tunnel must not make the TUI see "gateway exited". We do NOT send a
        # synthetic gateway.ready; the real one is relayed by the down-leg.
        try:
            await self._run_tunnel(local_ws)
        finally:
            await local_ws.close()
        return local_ws

    async def _run_tunnel(self, local_ws: web.WebSocketResponse) -> None:
        """Drive one TUI session end-to-end, reconnecting the REMOTE leg on
        drops while keeping the LOCAL (TUI) socket stable."""

        remote_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        up_task: asyncio.Task | None = None
        down_task: asyncio.Task | None = None
        local_done_evt = asyncio.Event()

        async def open_remote_with_retry() -> bool:
            nonlocal remote_ws
            attempt = 0
            while not local_done_evt.is_set():
                try:
                    remote_ws = await self._open_remote_tunnel()
                    _log.info("[daemon] remote tunnel established")
                    return True
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    wait = min(2.0 * attempt, 15.0)
                    _log.warning("[daemon] remote connect failed (attempt %d): %s; retrying in %.0fs",
                                 attempt, exc, wait)
                    await asyncio.sleep(wait)
            return False

        # up-leg consumes the local (TUI) socket: it ends when local disconnects.
        async def up_and_mark_local_done():
            nonlocal up_task, down_task
            try:
                if remote_ws is not None:
                    await self._proxy_leg(local_ws, remote_ws, "up")
            finally:
                local_done_evt.set()

        async def run_legs(ws: aiohttp.ClientWebSocketResponse):
            nonlocal up_task, down_task
            up_task = asyncio.create_task(up_and_mark_local_done())
            down_task = asyncio.create_task(
                self._proxy_remote_to_local(local_ws, ws, "down"))

        if not await open_remote_with_retry():
            return

        try:
            while not local_done_evt.is_set():
                await run_legs(remote_ws)
                # wait until either leg finishes (remote dropped) or local closes
                done, pending = await asyncio.wait(
                    {up_task, down_task}, return_when=asyncio.FIRST_COMPLETED)
                if local_done_evt.is_set():
                    for t in pending:
                        t.cancel()
                    break
                # down-leg ended (remote dropped): cancel stale legs, reconnect
                for t in pending:
                    t.cancel()
                try:
                    await remote_ws.close()
                except Exception:
                    pass
                _log.warning("[daemon] remote tunnel dropped; reconnecting")
                if not await open_remote_with_retry():
                    break
        finally:
            for t in (up_task, down_task):
                if t is not None:
                    t.cancel()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "remote": self.config.url})

    # ---- Lifecycle --------------------------------------------------

    def start(self) -> int:
        """Run the proxy until SIGTERM/SIGINT/listen-stop. Returns exit code."""
        app = web.Application()
        app.router.add_get("/api/ws", self._handle_connection)
        app.router.add_get("/health", self._health)

        runner = web.AppRunner(app)
        stop = asyncio.Event()
        ready_file = self.state_file.parent / "remote-gateway-ready"
        stop_file = self.state_file.with_suffix(".stop")

        def _handle_signal(sig, _frame) -> None:
            _log.info("[daemon] signal %s received, shutting down", sig)
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass

        async def _serve() -> int:
            # Install signal handlers now that a loop is running.
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        asyncio.get_running_loop().add_signal_handler(
                            sig, _handle_signal, sig, None)
                    except (NotImplementedError, RuntimeError):
                        pass
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", self.config.oauth_callback_port)
            await site.start()
            _log.info("[daemon] listening on ws://127.0.0.1:%d/api/ws", self.config.oauth_callback_port)
            try:
                ready_file.write_text(f"{self.config.oauth_callback_port}\n")
            except OSError:
                pass

            async def _watch_stop() -> None:
                while not stop.is_set():
                    if stop_file.exists():
                        _log.info("[daemon] stop marker detected; shutting down")
                        stop.set()
                        return
                    await asyncio.sleep(0.5)

            watcher = asyncio.create_task(_watch_stop())
            await stop.wait()
            watcher.cancel()
            await runner.cleanup()
            # Close the reused remote ClientSession so asyncio doesn't warn
            # about unclosed resources on shutdown.
            if self._session is not None and not self._session.closed:
                await self._session.close()
            try:
                ready_file.unlink(missing_ok=True)
            except OSError:
                pass
            return 0

        try:
            asyncio.run(_serve())
            return 0
        except KeyboardInterrupt:
            return 0


def main(argv: list[str] | None = None) -> int:
    """`hermes remote-gateway-daemon` entry point (spawned by `remote connect`)."""
    import argparse
    parser = argparse.ArgumentParser(prog="hermes-remote-gateway-daemon")
    parser.add_argument("--url", required=False, default="")
    parser.add_argument("--auth", default="oauth", choices=["oauth", "token", "basic"])
    parser.add_argument("--token", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--port", type=int, default=43827)
    parser.add_argument("--state-file", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    config = RemoteGatewayConfig(
        url=args.url,
        auth=args.auth,
        token=args.token,
        username=args.username,
        password=args.password,
        oauth_callback_port=args.port,
    )
    state_file = Path(args.state_file) if args.state_file else _default_state_file()
    proxy = RemoteGatewayProxy(config, state_file=state_file)
    return proxy.start()


if __name__ == "__main__":
    sys.exit(main())