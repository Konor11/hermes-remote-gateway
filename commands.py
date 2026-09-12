"""
Hermes Remote Gateway Plugin - CLI Commands
"""
import asyncio
import shutil
import sys
from typing import Optional, List
import argparse

from .config import RemoteGatewayConfig
from .client import RemoteGatewayClient, ConnectionState
from .auth import RemoteGatewayAuth


class RemoteGatewayCLI:
    """CLI interface for remote gateway"""
    
    def __init__(self, config: RemoteGatewayConfig):
        self.config = config
        self.client: Optional[RemoteGatewayClient] = None
        self.auth = RemoteGatewayAuth(config)
        self._response_queue: asyncio.Queue = asyncio.Queue()
        _final_response: Optional[str] = None
        self._streaming = False
    
    async def connect(self) -> bool:
        """Connect to remote gateway"""
        self.client = RemoteGatewayClient(self.config, self.auth)
        self.client.on_message = self._handle_message
        self.client.on_state_change = self._handle_state_change
        self.client.on_error = self._handle_error
        
        print(f"🔌 Connecting to {self.config.url}...")
        return await self.client.connect()
    
    async def disconnect(self):
        """Disconnect from remote gateway"""
        if self.client:
            await self.client.disconnect()
            self.client = None
    
    async def chat(self, query: str, profile: str = None, 
                   session_id: str = None, quiet: bool = False) -> str:
        """Send a chat query and return response"""
        if not self.client or not self.client.is_connected():
            if not await self.connect():
                raise RuntimeError("Failed to connect to remote gateway")
        
        # Wait for connection
        if not await self.client.wait_for_connected(10.0):
            raise RuntimeError("Connection timeout")
        
        # Clear response queue
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        self._final_response = None
        self._streaming = True
        
        # Send chat message
        if not await self.client.send_chat(query, profile or self.config.profile, session_id):
            raise RuntimeError("Failed to send chat message")
        
        # Wait for response
        if quiet:
            # Just wait for final response
            try:
                response = await asyncio.wait_for(
                    self._wait_for_final_response(), timeout=self.config.request_timeout
                )
                return response
            except asyncio.TimeoutError:
                raise RuntimeError("Request timeout")
        else:
            # Print streaming response
            return await self._print_streaming_response()
    
    async def _wait_for_final_response(self) -> str:
        """Wait for final response"""
        while self._final_response is None:
            await asyncio.sleep(0.1)
        return self._final_response
    
    async def _print_streaming_response(self) -> str:
        """Print streaming response to stdout"""
        full_response = ""
        
        while self._streaming:
            try:
                message = await asyncio.wait_for(
                    self._response_queue.get(), timeout=self.config.request_timeout
                )
                
                if isinstance(message, ChatStreamChunk):
                    # Print chunk
                    sys.stdout.write(message.content)
                    sys.stdout.flush()
                    full_response += message.content
                    
                    if message.done:
                        print()  # Newline at end
                        self._streaming = False
                        break
                        
                elif isinstance(message, ChatResponse):
                    # Final response (non-streaming)
                    sys.stdout.write(message.content)
                    sys.stdout.flush()
                    full_response = message.content
                    print()
                    self._streaming = False
                    break
                    
                elif isinstance(message, ErrorMessage):
                    print(f"\n❌ Error: {message.message}")
                    self._streaming = False
                    raise RuntimeError(message.message)
                    
            except asyncio.TimeoutError:
                print("\n⏱️  Request timeout")
                self._streaming = False
                raise RuntimeError("Request timeout")
        
        return full_response
    
    async def _handle_message(self, message):
        """Handle incoming message"""
        await self._response_queue.put(message)
        
        # Capture final response
        if isinstance(message, (ChatResponse, ChatStreamChunk)):
            if isinstance(message, ChatStreamChunk) and message.done:
                self._final_response = getattr(self, '_accumulated_content', '')
            elif isinstance(message, ChatResponse):
                self._final_response = message.content
            
            # Accumulate streaming content
            if isinstance(message, ChatStreamChunk):
                if not hasattr(self, '_accumulated_content'):
                    self._accumulated_content = ""
                self._accumulated_content += message.content
    
    async def _handle_state_change(self, state: ConnectionState):
        """Handle connection state change"""
        state_messages = {
            ConnectionState.CONNECTING: "🔌 Connecting...",
            ConnectionState.CONNECTED: "✅ Connected",
            ConnectionState.RECONNECTING: "🔄 Reconnecting...",
            ConnectionState.ERROR: "❌ Connection error",
            ConnectionState.DISCONNECTED: "🔌 Disconnected",
        }
        msg = state_messages.get(state, f"State: {state.value}")
        if state != ConnectionState.CONNECTED:  # Don't spam on connect
            print(msg, file=sys.stderr)
    
    async def _handle_error(self, error: Exception):
        """Handle error"""
        print(f"\n❌ Error: {error}", file=sys.stderr)
        self._streaming = False
    
    async def status(self) -> dict:
        """Get connection status"""
        if not self.client:
            return {"connected": False, "state": "not_initialized"}
        
        return {
            "connected": self.client.is_connected(),
            "state": self.client.state.value,
            "url": self.config.url,
            "auth": self.config.auth,
            "profile": self.config.profile,
            "session_id": self.client.current_session_id,
        }
    
    async def interactive_tui(self) -> int:
        """Connect (variant B): write HERMES_TUI_GATEWAY_URL to .env and
        spawn a local WebSocket proxy daemon. After this, a plain `hermes`
        boots the native TUI pointed at the LOCAL proxy (which tunnels to
        the remote gateway). `hermes remote disconnect` stops the daemon.
        """
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        env_file = home / ".env"
        ready_file = home / "remote-gateway-ready"
        state_file = home / "remote-gateway.json"
        local_port = self.config.oauth_callback_port

        # If a daemon is already active, keep it (idempotent connect).
        if ready_file.exists() and not state_file.with_suffix(".stop").exists():
            print(f"✅ Already connected (daemon on 127.0.0.1:{local_port})")
            return 0

        print(f"🔌 Connecting to {self.config.url} (native TUI via local proxy)...")

        # 1. Pre-check auth (mint a ticket now so we fail fast on bad creds).
        try:
            await asyncio.wait_for(self.auth.get_websocket_url(), timeout=90)
        except asyncio.TimeoutError:
            raise RuntimeError("Authorization timed out while obtaining WebSocket URL")
        except Exception as e:
            raise RuntimeError(f"Authentication failed: {e}")

        # 2. Locate the hermes binary only to find the venv/script for the daemon.
        hermes_bin = os.environ.get("HERMES_BIN") or shutil_which("hermes") or "/usr/local/bin/hermes"

        # 3. Spawn the proxy daemon (detached; persists after this process).
        daemon_module = Path(__file__).resolve().parent / "daemon.py"
        daemon_cmd = [
            sys.executable, str(daemon_module),
            "--url", self.config.url,
            "--auth", self.config.auth,
            "--port", str(local_port),
            "--state-file", str(state_file),
        ]
        # Pass credentials via env to avoid leaking them in argv/ps.
        daemon_env = os.environ.copy()
        daemon_env["HERMES_REMOTE_GATEWAY_TOKEN"] = self.config.token
        daemon_env["HERMES_REMOTE_GATEWAY_USERNAME"] = self.config.username
        daemon_env["HERMES_REMOTE_GATEWAY_PASSWORD"] = self.config.password

        # Detach: start in background, don't wait (like a system service).
        proc = subprocess.Popen(
            daemon_cmd, env=daemon_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # 4. Wait for the daemon to signal readiness.
        deadline = time.time() + 30
        while time.time() < deadline:
            if ready_file.exists():
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Proxy daemon exited early (code {proc.returncode}). "
                    "Check that hermes_cli.remote_gateway.daemon is importable.")
            await asyncio.sleep(0.25)
        else:
            proc.terminate()
            raise RuntimeError("Timed out waiting for the proxy daemon to start")

        # 5. Write HERMES_TUI_GATEWAY_URL into .env so a plain `hermes`
        #    points the native TUI at the local proxy.
        gateway_var = f"HERMES_TUI_GATEWAY_URL=ws://127.0.0.1:{local_port}/api/ws\n"
        lines = []
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
        lines = [l for l in lines if not l.startswith("HERMES_TUI_GATEWAY_URL=")]
        lines.append(gateway_var.rstrip("\n"))
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env_file.chmod(0o600)

        print(f"⚡ Connected. Now just run `hermes` — it opens the native TUI "
              f"tunnelled to {self.config.url} via 127.0.0.1:{local_port}.")
        print(f"   To return to local Hermes: `hermes remote disconnect`")
        return 0

    async def disconnect_remote(self) -> int:
        """Stop the proxy daemon and remove HERMES_TUI_GATEWAY_URL from .env
        so a plain `hermes` returns to the LOCAL runtime."""
        import os
        import signal as _sig
        from pathlib import Path

        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        env_file = home / ".env"
        ready_file = home / "remote-gateway-ready"
        state_file = home / "remote-gateway.json"
        stop_file = state_file.with_suffix(".stop")

        # 1. Signal the daemon to stop (it writes remote-gateway-ready after
        #    startup; daemon also watches for the .stop marker).
        stop_file.write_text("stop\n")
        try:
            ready_file.unlink(missing_ok=True)
        except OSError:
            pass

        # 2. Remove HERMES_TUI_GATEWAY_URL from .env.
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
            lines = [l for l in lines if not l.startswith("HERMES_TUI_GATEWAY_URL=")]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if "\n".join(lines) == "":
                env_file.write_text("", encoding="utf-8")

        # 3. Look for any lingering daemon process and terminate it.
        import subprocess
        try:
            out = subprocess.run(
                ["pgrep", "-f", "remote-gateway/daemon.py"],
                capture_output=True, text=True, timeout=5)
            for pid in out.stdout.split():
                pid = pid.strip()
                if pid.isdigit():
                    try:
                        os.kill(int(pid), _sig.SIGTERM)
                    except OSError:
                        pass
        except Exception:
            pass

        try:
            stop_file.unlink(missing_ok=True)
        except OSError:
            pass

        return 0

    async def interactive(self):
        """Run interactive chat session"""
        print(f"🔌 Connecting to {self.config.url}...")
        if not await self.connect():
            print("❌ Failed to connect")
            return
        
        print("✅ Connected! Type 'exit' or 'quit' to leave, 'help' for commands.\n")
        
        try:
            while self.client and self.client.is_connected():
                try:
                    query = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("You: ").strip()
                    )
                except (EOFError, KeyboardInterrupt):
                    break
                
                if not query:
                    continue
                
                if query.lower() in ('exit', 'quit', 'q'):
                    break
                
                if query.lower() in ('help', 'h', '?'):
                    print("""
Commands:
  exit, quit, q    - Exit interactive mode
  help, h, ?       - Show this help
  status           - Show connection status
  profile <name>   - Switch profile
                    """)
                    continue
                
                if query.lower() == 'status':
                    status = await self.status()
                    print(f"Status: {status}")
                    continue
                
                if query.lower().startswith('profile '):
                    new_profile = query[8:].strip()
                    if new_profile:
                        self.config.profile = new_profile
                        print(f"Switched to profile: {new_profile}")
                    continue
                
                # Send chat
                await self.chat(query)
                
        finally:
            await self.disconnect()
            print("\n👋 Disconnected")


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for remote gateway commands"""
    parser = argparse.ArgumentParser(
        prog="hermes remote",
        description="Connect Hermes CLI to a remote Hermes gateway"
    )
    
    subparsers = parser.add_subparsers(dest="remote_command", help="Commands")
    
    # connect command
    connect_parser = subparsers.add_parser("connect", help="Connect and start interactive chat")
    connect_parser.add_argument("--url", help="Remote gateway URL")
    connect_parser.add_argument("--auth", choices=["oauth", "token", "basic"], help="Auth mode")
    connect_parser.add_argument("--token", help="Session token (for token auth)")
    connect_parser.add_argument("--username", help="Username (for basic auth)")
    connect_parser.add_argument("--password", help="Password (for basic auth)")
    connect_parser.add_argument("--profile", help="Remote profile name")
    
    # chat command
    chat_parser = subparsers.add_parser("chat", help="Send a single query (oneshot)")
    chat_parser.add_argument("-q", "--query", required=True, help="Query to send")
    chat_parser.add_argument("--url", help="Remote gateway URL")
    chat_parser.add_argument("--auth", choices=["oauth", "token", "basic"], help="Auth mode")
    chat_parser.add_argument("--token", help="Session token (for token auth)")
    chat_parser.add_argument("--username", help="Username (for basic auth)")
    chat_parser.add_argument("--password", help="Password (for basic auth)")
    chat_parser.add_argument("--profile", help="Remote profile name")
    chat_parser.add_argument("--session-id", help="Resume specific session")
    chat_parser.add_argument("-Q", "--quiet", action="store_true", help="Suppress streaming output")
    
    # status command
    status_parser = subparsers.add_parser("status", help="Show connection status")
    
    # disconnect command
    disconnect_parser = subparsers.add_parser("disconnect", help="Disconnect from gateway")
    
    # config command
    config_parser = subparsers.add_parser("config", help="Show current configuration")
    
    return parser


async def run_command(args: argparse.Namespace, base_config: RemoteGatewayConfig) -> int:
    """Run a command with given arguments"""
    # Override config with CLI args
    config = RemoteGatewayConfig(
        url=args.url or base_config.url,
        auth=args.auth or base_config.auth,
        token=args.token or base_config.token,
        username=args.username or base_config.username,
        password=args.password or base_config.password,
        profile=args.profile or base_config.profile,
    )
    
    # Validate
    valid, error = config.validate()
    if not valid:
        print(f"❌ Configuration error: {error}", file=sys.stderr)
        return 1
    
    cli = RemoteGatewayCLI(config)
    
    try:
        if args.remote_command == "connect":
            if config.native_tui:
                return await cli.interactive_tui()
            await cli.interactive()
            return 0
            
        elif args.remote_command == "chat":
            response = await cli.chat(
                query=args.query,
                profile=args.profile,
                session_id=args.session_id,
                quiet=args.quiet
            )
            if args.quiet:
                print(response)
            return 0
            
        elif args.remote_command == "status":
            status = await cli.status()
            print(f"Connected: {'✅' if status['connected'] else '❌'}")
            print(f"State: {status['state']}")
            print(f"URL: {status['url']}")
            print(f"Auth: {status['auth']}")
            print(f"Profile: {status['profile']}")
            if status['session_id']:
                print(f"Session: {status['session_id']}")
            return 0
            
        elif args.remote_command == "disconnect":
            code = await cli.disconnect_remote()
            print("🔌 Disconnected from remote gateway")
            return code
            
        elif args.remote_command == "config":
            print("Current configuration:")
            print(f"  URL: {config.url}")
            print(f"  Auth: {config.auth}")
            print(f"  Profile: {config.profile}")
            print(f"  Auto-reconnect: {config.auto_reconnect}")
            return 0
            
        else:
            print("No command specified. Use --help for usage.", file=sys.stderr)
            return 1
            
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return 1
    finally:
        await cli.disconnect()


def main(argv: List[str] = None) -> int:
    """Main entry point"""
    parser = create_parser()
    args = parser.parse_args(argv)
    
    # Load base config from Hermes config.yaml
    import os
    from pathlib import Path
    import yaml
    
    config_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
    base_config = RemoteGatewayConfig()
    
    if config_path.exists():
        try:
            with open(config_path) as f:
                hermes_config = yaml.safe_load(f) or {}
            base_config = RemoteGatewayConfig.from_config(hermes_config)
        except Exception:
            pass
    
    return asyncio.run(run_command(args, base_config))


if __name__ == "__main__":
    sys.exit(main())