"""
Hermes Remote Gateway Plugin - WebSocket Client
Async WebSocket client with auto-reconnect, ping/pong, and message handling
"""
import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, Callable, Awaitable, List
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
from aiohttp import WSMsgType

from .config import RemoteGatewayConfig
from .auth import RemoteGatewayAuth
from .protocol import (
    BaseMessage, ChatMessage, ChatResponse, ChatStreamChunk,
    ToolCallRequest, ToolResult, ErrorMessage, PingMessage, PongMessage,
    SessionUpdate, StatusMessage, parse_message, create_chat_message, create_ping
)

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class RemoteGatewayClient:
    """WebSocket client for remote Hermes gateway"""
    
    config: RemoteGatewayConfig
    auth: RemoteGatewayAuth
    
    # State
    state: ConnectionState = ConnectionState.DISCONNECTED
    ws: Optional[aiohttp.ClientWebSocketResponse] = None
    session: Optional[aiohttp.ClientSession] = None
    
    # Callbacks
    on_message: Optional[Callable[[BaseMessage], Awaitable[None]]] = None
    on_state_change: Optional[Callable[[ConnectionState], Awaitable[None]]] = None
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None
    
    # Reconnection
    _reconnect_task: Optional[asyncio.Task] = None
    _ping_task: Optional[asyncio.Task] = None
    _receive_task: Optional[asyncio.Task] = None
    _reconnect_attempt: int = 0
    _should_reconnect: bool = True
    _last_pong: float = 0
    
    # Session
    current_session_id: Optional[str] = None
    
    async def connect(self) -> bool:
        """Connect to remote gateway"""
        if self.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED):
            return True
        
        await self._set_state(ConnectionState.CONNECTING)
        
        try:
            # Get auth data
            auth_data = await self.auth.get_websocket_auth()
            
            # Create session if needed
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()
            
            # Connect WebSocket
            self.ws = await self.session.ws_connect(
                auth_data["url"],
                headers=auth_data.get("headers", {}),
                protocols=auth_data.get("subprotocols", ["hermes-gateway-v1"]),
                heartbeat=None,  # We handle ping/pong manually
                timeout=aiohttp.ClientWSTimeout(
                    ws_receive=self.config.request_timeout,
                    ws_close=10.0
                )
            )
            
            await self._set_state(ConnectionState.CONNECTED)
            self._reconnect_attempt = 0
            self._last_pong = time.time()
            
            # Start background tasks
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._ping_loop())
            
            logger.info(f"Connected to remote gateway: {self.config.url}")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            await self._set_state(ConnectionState.ERROR)
            await self._handle_error(e)
            return False
    
    async def disconnect(self):
        """Disconnect from remote gateway"""
        self._should_reconnect = False
        
        # Cancel background tasks
        for task in [self._receive_task, self._ping_task, self._reconnect_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Close WebSocket
        if self.ws and not self.ws.closed:
            await self.ws.close()
        
        # Close session
        if self.session and not self.session.closed:
            await self.session.close()
        
        await self._set_state(ConnectionState.DISCONNECTED)
        logger.info("Disconnected from remote gateway")
    
    async def send_message(self, message: BaseMessage) -> bool:
        """Send a message to the gateway"""
        if self.state != ConnectionState.CONNECTED or not self.ws or self.ws.closed:
            logger.warning("Cannot send message: not connected")
            return False
        
        try:
            await self.ws.send_str(message.to_json())
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            await self._handle_error(e)
            return False
    
    async def send_chat(self, content: str, profile: str = None, 
                        session_id: str = None) -> bool:
        """Send a chat message"""
        msg = create_chat_message(
            content=content,
            profile=profile or self.config.profile,
            session_id=session_id or self.current_session_id
        )
        return await self.send_message(msg)
    
    async def send_tool_result(self, call_id: str, result: Any = None, 
                               error: str = None) -> bool:
        """Send tool execution result"""
        from .protocol import ToolResult
        msg = ToolResult(call_id=call_id, result=result, error=error)
        return await self.send_message(msg)
    
    async def send_ping(self) -> bool:
        """Send ping"""
        return await self.send_message(create_ping())
    
    async def _receive_loop(self):
        """Main receive loop"""
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    logger.warning("Received binary message, ignoring")
                elif msg.type == WSMsgType.PING:
                    await self.ws.pong()
                elif msg.type == WSMsgType.PONG:
                    self._last_pong = time.time()
                elif msg.type == WSMsgType.CLOSE:
                    logger.info("WebSocket closed by server")
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self.ws.exception()}")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            await self._handle_error(e)
        finally:
            if self.state == ConnectionState.CONNECTED:
                await self._set_state(ConnectionState.DISCONNECTED)
                if self._should_reconnect and self.config.auto_reconnect:
                    await self._schedule_reconnect()
    
    async def _ping_loop(self):
        """Send periodic pings"""
        try:
            while self.state == ConnectionState.CONNECTED:
                await asyncio.sleep(self.config.ping_interval)
                
                if self.state != ConnectionState.CONNECTED:
                    break
                
                # Check if we got a pong recently
                if time.time() - self._last_pong > self.config.ping_timeout:
                    logger.warning("Ping timeout, reconnecting")
                    await self._handle_error(TimeoutError("Ping timeout"))
                    break
                
                await self.send_ping()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Ping loop error: {e}")
    
    async def _handle_message(self, data: str):
        """Handle incoming message"""
        try:
            message = parse_message(data)
            
            # Update session ID if present
            if hasattr(message, 'session_id') and message.session_id:
                self.current_session_id = message.session_id
            
            # Call callback
            if self.on_message:
                await self.on_message(message)
                
        except Exception as e:
            logger.error(f"Message handling error: {e}")
    
    async def _schedule_reconnect(self):
        """Schedule reconnection with exponential backoff"""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
    
    async def _reconnect_loop(self):
        """Reconnection loop with exponential backoff"""
        await self._set_state(ConnectionState.RECONNECTING)
        
        while self._should_reconnect and self._reconnect_attempt < self.config.reconnect_attempts:
            self._reconnect_attempt += 1
            delay = self.config.reconnect_delay * (2 ** (self._reconnect_attempt - 1))
            
            logger.info(f"Reconnecting... attempt {self._reconnect_attempt}/{self.config.reconnect_attempts} in {delay}s")
            await asyncio.sleep(delay)
            
            if not self._should_reconnect:
                break
            
            if await self.connect():
                logger.info("Reconnected successfully")
                return
        
        logger.error("Max reconnection attempts reached")
        await self._set_state(ConnectionState.ERROR)
    
    async def _set_state(self, new_state: ConnectionState):
        """Update connection state"""
        if self.state != new_state:
            old_state = self.state
            self.state = new_state
            logger.debug(f"State changed: {old_state.value} -> {new_state.value}")
            
            if self.on_state_change:
                try:
                    await self.on_state_change(new_state)
                except Exception as e:
                    logger.error(f"State change callback error: {e}")
    
    async def _handle_error(self, error: Exception):
        """Handle error"""
        if self.on_error:
            try:
                await self.on_error(error)
            except Exception as e:
                logger.error(f"Error callback error: {e}")
    
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED
    
    async def wait_for_connected(self, timeout: float = 30.0) -> bool:
        """Wait until connected or timeout"""
        start = time.time()
        while time.time() - start < timeout:
            if self.is_connected():
                return True
            if self.state == ConnectionState.ERROR:
                return False
            await asyncio.sleep(0.1)
        return False