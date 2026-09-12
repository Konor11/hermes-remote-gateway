"""
Hermes Remote Gateway Plugin - Main Entry Point
Registers the `hermes remote` command with Hermes CLI
"""
import argparse
import sys
from typing import List, Optional, Callable, Any

from .config import RemoteGatewayConfig
from .commands import run_command


def register(ctx) -> None:
    """Register the plugin with Hermes CLI"""
    
    def setup_remote_parser(subparser: argparse.ArgumentParser) -> None:
        """Setup the remote subcommand parser - receives subparser from main parser"""
        subparser.add_argument(
            "--url", help="Remote gateway URL (e.g., https://mydomen.com)"
        )
        subparser.add_argument(
            "--auth", choices=["oauth", "token", "basic"], help="Authentication mode"
        )
        subparser.add_argument("--token", help="Session token (for token auth)")
        subparser.add_argument("--username", help="Username (for basic auth)")
        subparser.add_argument("--password", help="Password (for basic auth)")
        subparser.add_argument("--profile", help="Remote profile name")
        
        # Subcommands
        remote_subparsers = subparser.add_subparsers(dest="remote_command", help="Remote gateway commands")
        
        # connect command
        connect_parser = remote_subparsers.add_parser("connect", help="Connect and start interactive chat")
        connect_parser.add_argument("--url", help="Remote gateway URL (e.g., https://mydomen.com)")
        connect_parser.add_argument("--auth", choices=["oauth", "token", "basic"], help="Authentication mode")
        connect_parser.add_argument("--token", help="Session token (for token auth)")
        connect_parser.add_argument("--username", help="Username (for basic auth)")
        connect_parser.add_argument("--password", help="Password (for basic auth)")
        connect_parser.add_argument("--profile", help="Remote profile name")
        
        # chat command
        chat_parser = remote_subparsers.add_parser("chat", help="Send a single query (oneshot mode)")
        chat_parser.add_argument("-q", "--query", required=True, help="Query to send")
        chat_parser.add_argument("--url", help="Remote gateway URL")
        chat_parser.add_argument("--auth", choices=["oauth", "token", "basic"], help="Authentication mode")
        chat_parser.add_argument("--token", help="Session token (for token auth)")
        chat_parser.add_argument("--username", help="Username (for basic auth)")
        chat_parser.add_argument("--password", help="Password (for basic auth)")
        chat_parser.add_argument("--profile", help="Remote profile name")
        chat_parser.add_argument("--session-id", help="Resume specific session")
        chat_parser.add_argument("-Q", "--quiet", action="store_true", help="Suppress streaming output, print only final response")
        
        # status command
        status_parser = remote_subparsers.add_parser("status", help="Show connection status")
        
        # disconnect command
        disconnect_parser = remote_subparsers.add_parser("disconnect", help="Disconnect from gateway")
        
        # config command
        config_parser = remote_subparsers.add_parser("config", help="Show current configuration")
    
    def handle_remote_command(args: argparse.Namespace) -> int:
        """Handle the remote command"""
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
        
        # Run the command
        import asyncio
        return asyncio.run(run_command(args, base_config))
    
    # Register the CLI command - setup_fn receives the subparser, handler_fn becomes the command handler
    ctx.register_cli_command(
        name="remote",
        help="Connect to a remote Hermes gateway (hermes serve)",
        setup_fn=setup_remote_parser,
        handler_fn=handle_remote_command,
        description="Connect Hermes CLI to a remote Hermes gateway via WebSocket with OAuth/Token/Basic Auth"
    )


# For backward compatibility - direct invocation
def main(argv: List[str] = None) -> int:
    """Direct entry point for `hermes-remote` command"""
    from .commands import create_parser
    
    parser = create_parser()
    args = parser.parse_args(argv)
    
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
    
    import asyncio
    return asyncio.run(run_command(args, base_config))


if __name__ == "__main__":
    sys.exit(main())