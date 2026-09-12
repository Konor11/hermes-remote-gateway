"""
Hermes Remote Gateway Plugin - Configuration
"""
from dataclasses import dataclass, field
from typing import Optional
import os


@dataclass
class RemoteGatewayConfig:
    """Configuration for remote gateway connection"""
    url: str = ""
    auth: str = "oauth"  # oauth | token | basic
    token: str = ""
    username: str = ""
    password: str = ""
    profile: str = "default"
    auto_reconnect: bool = True
    reconnect_attempts: int = 5
    reconnect_delay: float = 2.0
    # OAuth specific
    oauth_callback_port: int = 43827
    # Connection
    ping_interval: float = 30.0
    ping_timeout: float = 10.0
    # Request timeout
    request_timeout: float = 300.0
    # If true, HERMES_TUI_GATEWAY_URL is used to launch native TUI (Путь 2).
    native_tui: bool = True

    def __post_init__(self):
        # Read from env vars if not set
        if not self.url:
            self.url = os.environ.get("HERMES_REMOTE_GATEWAY_URL", "")
        if not self.token and self.auth == "token":
            self.token = os.environ.get("HERMES_REMOTE_GATEWAY_TOKEN", "")
        if not self.username and self.auth == "basic":
            self.username = os.environ.get("HERMES_REMOTE_GATEWAY_USERNAME", "")
        if not self.password and self.auth == "basic":
            self.password = os.environ.get("HERMES_REMOTE_GATEWAY_PASSWORD", "")

    @classmethod
    def from_config(cls, config: dict) -> "RemoteGatewayConfig":
        """Create from config.yaml remote_gateway section"""
        rg = config.get("remote_gateway", {})
        return cls(
            url=rg.get("url", ""),
            auth=rg.get("auth", "oauth"),
            token=rg.get("token", ""),
            username=rg.get("username", ""),
            password=rg.get("password", ""),
            profile=rg.get("profile", "default"),
            auto_reconnect=rg.get("auto_reconnect", True),
            reconnect_attempts=rg.get("reconnect_attempts", 5),
            reconnect_delay=rg.get("reconnect_delay", 2.0),
            oauth_callback_port=rg.get("oauth_callback_port", 43827),
            native_tui=rg.get("native_tui", True),
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "auth": self.auth,
            "token": self.token,
            "username": self.username,
            "password": self.password,
            "profile": self.profile,
            "auto_reconnect": self.auto_reconnect,
            "reconnect_attempts": self.reconnect_attempts,
            "reconnect_delay": self.reconnect_delay,
        }

    def validate(self) -> tuple[bool, str]:
        """Validate config, return (is_valid, error_message)"""
        if not self.url:
            return False, "Remote gateway URL is required"

        # Normalize URL
        if not self.url.startswith(("http://", "https://")):
            self.url = "https://" + self.url

        if self.auth not in ("oauth", "token", "basic"):
            return False, f"Invalid auth mode: {self.auth}. Must be oauth, token, or basic"

        if self.auth == "token" and not self.token:
            return False, "Session token required for token auth"

        if self.auth == "basic" and not (self.username and self.password):
            return False, "Username and password required for basic auth"

        return True, ""