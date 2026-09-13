"""
Hermes Remote Gateway Plugin - Authentication
Handles OAuth (Nous Portal native PKCE), Session Token, and Basic Auth flows
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import webbrowser
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode

import aiohttp

from .config import RemoteGatewayConfig


@dataclass
class PKCEPair:
    verifier: str
    challenge: str
    method: str = "S256"


@dataclass
class NativeTokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    provider: str
    user_id: str


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback"""
    
    def __init__(self, *args, callback_event: asyncio.Event = None, 
                 auth_code: Dict = None, **kwargs):
        self.callback_event = callback_event
        self.auth_code = auth_code
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/callback", "/auth/callback"):
            params = parse_qs(parsed.query)
            if "code" in params:
                self.auth_code["code"] = params["code"][0]
                self.auth_code["state"] = params.get("state", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"""
                <html><body>
                <h1>Authorization successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>window.close();</script>
                </body></html>
                """)
                if self.callback_event:
                    self.callback_event.set()
                return
            elif "error" in params:
                self.auth_code["error"] = params["error"][0]
                self.auth_code["error_description"] = params.get("error_description", [""])[0]
                self.send_response(400)
                self.end_headers()
                if self.callback_event:
                    self.callback_event.set()
                return
        
        self.send_response(404)
        self.end_headers()
    
    def log_message(self, format, *args):
        pass


class RemoteGatewayAuth:
    """Handles authentication for remote gateway"""
    
    def __init__(self, config: RemoteGatewayConfig):
        self.config = config
        self._oauth_tokens: Dict[str, Any] = {}
        self._session_cookies: List[Dict[str, str]] = []
        self._callback_port = config.oauth_callback_port
    
    def _generate_pkce_pair(self) -> PKCEPair:
        """Generate PKCE verifier/challenge pair (RFC 7636 S256)"""
        verifier = secrets.token_urlsafe(32)  # 43 chars base64url
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip('=')
        return PKCEPair(verifier=verifier, challenge=challenge)
    
    async def get_websocket_auth(self) -> Dict[str, Any]:
        """Get authentication data for WebSocket connection"""
        if self.config.auth == "oauth":
            return await self._get_oauth_ws_auth()
        elif self.config.auth == "token":
            return self._get_token_ws_auth()
        elif self.config.auth == "basic":
            return await self._get_basic_ws_auth()
        else:
            raise ValueError(f"Unknown auth mode: {self.config.auth}")

    async def get_websocket_url(self) -> str:
        """Return a plain WS URL suitable for HERMES_TUI_GATEWAY_URL."""
        if self.config.auth in ("token", "oauth"):
            auth = await self.get_websocket_auth()
            return auth["url"]
        # Basic auth: cookies in headers won't be sent by native TUI's
        # WebSocket connection (it only takes a URL). Mint a WS ticket
        # using the basic session instead, so the URL carries ?ticket=.
        await self._basic_login()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in self._session_cookies)
        parsed = urlparse(self.config.url)
        ticket_url = urlunparse((
            parsed.scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/api/auth/ws-ticket", "", "", ""))
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ticket_url, headers={"Cookie": cookie_header},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to get WS ticket for basic auth: {resp.status}")
                data = await resp.json()
        ticket = data.get("ticket")
        if not ticket:
            raise RuntimeError("No ticket in WS-ticket response")
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((
            ws_scheme, parsed.netloc, parsed.path.rstrip("/") + "/api/ws",
            "", f"ticket={ticket}", ""))
    
    def _get_token_ws_auth(self) -> Dict[str, Any]:
        """Session token auth - token in query param"""
        parsed = urlparse(self.config.url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = urlunparse((
            ws_scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/api/ws",
            "", f"token={self.config.token}", ""
        ))
        return {
            "url": ws_url,
            "headers": {},
            "subprotocols": ["hermes-gateway-v1"],
            "query_params": {"token": self.config.token}
        }
    
    async def _get_basic_ws_auth(self) -> Dict[str, Any]:
        """Basic auth - login via password-login, then use cookies for WS"""
        if not self._session_cookies:
            await self._basic_login()
        
        parsed = urlparse(self.config.url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = urlunparse((
            ws_scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/api/ws",
            "", "", ""
        ))
        
        # Build cookie header from session cookies
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in self._session_cookies
        )
        
        return {
            "url": ws_url,
            "headers": {"Cookie": cookie_header} if cookie_header else {},
            "subprotocols": ["hermes-gateway-v1"],
            "query_params": {}
        }
    
    async def _basic_login(self):
        """Login via /auth/password-login and store session cookies"""
        parsed = urlparse(self.config.url)
        login_url = urlunparse((
            parsed.scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/auth/password-login",
            "", "", ""
        ))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                login_url,
                json={"username": self.config.username, "password": self.config.password, "provider": "basic"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Basic auth login failed: {resp.status} - {error_text}")
                
                # Extract session cookies from Set-Cookie headers (HttpOnly cookies
                # are not reliably exposed via aiohttp's cookie jar).
                self._session_cookies = []
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                for header in set_cookie_headers:
                    name, _, rest = header.partition("=")
                    name = name.strip()
                    if name.startswith(("hermes_session_", "__Host-hermes_session_",
                                        "__Secure-hermes_session_")):
                        value = rest.split(";")[0].strip()
                        self._session_cookies.append({"name": name, "value": value})
                
                if not self._session_cookies:
                    raise RuntimeError("Basic auth login succeeded but no session cookies received")
    
    async def _get_oauth_ws_auth(self) -> Dict[str, Any]:
        """OAuth auth - get fresh WS ticket from /api/auth/ws-ticket"""
        # Ensure we have valid OAuth tokens
        await self._ensure_oauth_tokens()
        
        # Get WS ticket
        parsed = urlparse(self.config.url)
        ticket_url = urlunparse((
            parsed.scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/api/auth/ws-ticket",
            "", "", ""
        ))
        
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {self._oauth_tokens['access_token']}"}
            async with session.post(ticket_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401:
                    await self._refresh_oauth_tokens()
                    headers["Authorization"] = f"Bearer {self._oauth_tokens['access_token']}"
                    async with session.post(ticket_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                        if resp2.status != 200:
                            raise RuntimeError(f"Failed to get WS ticket after refresh: {resp2.status}")
                        data = await resp2.json()
                elif resp.status != 200:
                    raise RuntimeError(f"Failed to get WS ticket: {resp.status}")
                else:
                    data = await resp.json()
        
        ticket = data.get("ticket")
        if not ticket:
            raise RuntimeError("No ticket in response")
        
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = urlunparse((
            ws_scheme, parsed.netloc,
            parsed.path.rstrip("/") + "/api/ws",
            "", f"ticket={ticket}", ""
        ))
        
        return {
            "url": ws_url,
            "headers": {},
            "subprotocols": [f"hermes-gateway-ticket.{ticket}", "hermes-gateway-v1"],
            "query_params": {"ticket": ticket}
        }
    
    async def _ensure_oauth_tokens(self):
        """Ensure we have valid OAuth tokens, run flow if needed"""
        if self._oauth_tokens.get("access_token"):
            if self._oauth_tokens.get("expires_at", 0) > asyncio.get_event_loop().time() + 60:
                return
        
        if await self._load_oauth_state():
            if self._oauth_tokens.get("expires_at", 0) > asyncio.get_event_loop().time() + 60:
                return
            if await self._refresh_oauth_tokens():
                return
        
        await self._run_native_oauth_flow()
    
    def _generate_pkce_pair(self) -> PKCEPair:
        """Generate PKCE verifier/challenge pair (RFC 7636 S256)"""
        verifier = secrets.token_urlsafe(32)  # 43 chars base64url
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip('=')
        return PKCEPair(verifier=verifier, challenge=challenge)
    
    async def _run_native_oauth_flow(self):
        """Run RFC 8252 native PKCE OAuth flow via gateway"""
        parsed = urlparse(self.config.url)
        base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        
        # Check if gateway supports native PKCE
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/api/status", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to get gateway status: {resp.status}")
                status_data = await resp.json()
        
        auth_flows = status_data.get("auth_flows", [])
        if "native_pkce" not in auth_flows:
            raise RuntimeError("Gateway does not support native PKCE OAuth flow")
        
        # Get available providers
        providers = []
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/api/auth/providers", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    providers = data.get("providers", [])
        
        # Pick provider (prefer non-password)
        provider = None
        for p in providers:
            if p.get("kind") != "password":
                provider = p.get("id")
                break
        if not provider and providers:
            provider = providers[0].get("id")
        
        # Generate PKCE pair
        pkce = self._generate_pkce_pair()
        state = secrets.token_urlsafe(24)
        
        # Start local callback server
        callback_url = f"http://127.0.0.1:{self._callback_port}/callback"
        auth_code = {}
        callback_event = asyncio.Event()
        
        server = HTTPServer(("127.0.0.1", self._callback_port), 
                           lambda *args, **kwargs: OAuthCallbackHandler(*args, 
                           callback_event=callback_event, auth_code=auth_code, **kwargs))
        server_thread = asyncio.get_event_loop().run_in_executor(None, server.serve_forever)
        
        try:
            # Build authorize URL
            authorize_url = f"{base_url}/auth/native/authorize?" + urlencode({
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "redirect_uri": callback_url,
                "state": state,
            })
            if provider:
                authorize_url += f"&provider={provider}"
            
            print(f"\n🔐 Authorization required (Native PKCE)")
            print(f"   Open: {authorize_url}")
            print(f"   Waiting for authorization...\n")
            
            try:
                webbrowser.open(authorize_url)
            except Exception:
                pass
            
            # Wait for callback
            try:
                await asyncio.wait_for(callback_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                raise RuntimeError("Authorization timed out")
            
            if "error" in auth_code:
                raise RuntimeError(f"Auth error: {auth_code.get('error')} - {auth_code.get('error_description')}")
            
            code = auth_code.get("code")
            if not code:
                raise RuntimeError("No authorization code received")
            
            # Exchange code for tokens with increased header limit
            token_url = f"{base_url}/auth/native/token"
            # Use connector with higher header limit via timeout
            timeout = aiohttp.ClientTimeout(total=10)
            # The header size limit is in the internal parser, not configurable via connector
            # We'll rely on the default which should handle it
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    token_url,
                    json={
                        "code": code,
                        "code_verifier": pkce.verifier,
                        "redirect_uri": callback_url,
                        "grant_type": "authorization_code",
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"Token exchange failed: {resp.status} - {error_text}")
                    token_data = await resp.json()
            
            # Parse token response
            self._oauth_tokens = {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in", 3600),
                "token_type": token_data.get("token_type", "Bearer"),
                "provider": token_data.get("provider", "nous"),
                "user_id": token_data.get("user_id", "unknown"),
            }
            import time
            self._oauth_tokens["expires_at"] = time.time() + self._oauth_tokens["expires_in"]
            await self._save_oauth_state()
            print("✅ Authorization successful!")
            
        finally:
            server.shutdown()
            server.server_close()
    
    async def _refresh_oauth_tokens(self) -> bool:
        """Refresh OAuth access token using refresh token"""
        if not self._oauth_tokens.get("refresh_token"):
            return False
        
        parsed = urlparse(self.config.url)
        base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        refresh_url = f"{base_url}/auth/native/refresh"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    refresh_url,
                    json={"refresh_token": self._oauth_tokens["refresh_token"]},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
            
            self._oauth_tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", self._oauth_tokens["refresh_token"]),
                "expires_in": data.get("expires_in", 3600),
                "token_type": data.get("token_type", "Bearer"),
                "provider": self._oauth_tokens.get("provider", "nous"),
                "user_id": self._oauth_tokens.get("user_id", "unknown"),
            }
            import time
            self._oauth_tokens["expires_at"] = time.time() + self._oauth_tokens["expires_in"]
            await self._save_oauth_state()
            return True
        except Exception:
            return False
    
    async def _save_oauth_state(self):
        """Save OAuth tokens to config directory"""
        config_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        auth_file = config_dir / "remote-gateway-oauth.json"
        
        state = {
            "url": self.config.url,
            "tokens": self._oauth_tokens
        }
        
        try:
            auth_file.write_text(json.dumps(state))
            auth_file.chmod(0o600)
        except Exception:
            pass
    
    async def _load_oauth_state(self) -> bool:
        """Load OAuth tokens from config directory"""
        config_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        auth_file = config_dir / "remote-gateway-oauth.json"
        
        if not auth_file.exists():
            return False
        
        try:
            data = json.loads(auth_file.read_text())
            if data.get("url") != self.config.url:
                return False
            
            tokens = data.get("tokens", {})
            if tokens.get("access_token"):
                self._oauth_tokens = tokens
                return True
        except Exception:
            pass
        
        return False