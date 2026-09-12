"""
Hermes Remote Gateway Plugin - Protocol
Message types and serialization for remote gateway WebSocket protocol
"""
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum


class MessageType(Enum):
    """WebSocket message types"""
    # Client -> Server
    CHAT_MESSAGE = "chat_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    
    # Server -> Client
    CHAT_RESPONSE = "chat_response"
    CHAT_STREAM = "chat_stream"
    TOOL_CALL_REQUEST = "tool_call_request"
    TOOL_RESULT_RESPONSE = "tool_result_response"
    PONG = "pong"
    ERROR = "error"
    SESSION_UPDATE = "session_update"
    STATUS = "status"


@dataclass
class BaseMessage:
    """Base message structure"""
    type: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=lambda: __import__('time').time())
    
    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str)
    
    @classmethod
    def from_json(cls, data: str) -> "BaseMessage":
        return cls(**json.loads(data))


@dataclass
class ChatMessage(BaseMessage):
    """User chat message"""
    type: str = "chat_message"
    content: str = ""
    profile: str = "default"
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse(BaseMessage):
    """Assistant chat response (final)"""
    type: str = "chat_response"
    content: str = ""
    session_id: str = ""
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatStreamChunk(BaseMessage):
    """Streaming chat response chunk"""
    type: str = "chat_stream"
    content: str = ""
    session_id: str = ""
    done: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRequest(BaseMessage):
    """Server requests tool execution"""
    type: str = "tool_call_request"
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class ToolResult(BaseMessage):
    """Tool execution result"""
    type: str = "tool_result"
    call_id: str = ""
    result: Any = None
    error: Optional[str] = None


@dataclass
class ErrorMessage(BaseMessage):
    """Error message"""
    type: str = "error"
    code: str = ""
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PingMessage(BaseMessage):
    """Ping message"""
    type: str = "ping"


@dataclass
class PongMessage(BaseMessage):
    """Pong message"""
    type: str = "pong"


@dataclass
class SessionUpdate(BaseMessage):
    """Session state update"""
    type: str = "session_update"
    session_id: str = ""
    profile: str = "default"
    status: str = ""  # created, active, closed
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusMessage(BaseMessage):
    """Status/health message"""
    type: str = "status"
    status: str = ""  # connected, ready, busy, error
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# Message type registry for deserialization
MESSAGE_TYPES = {
    "chat_message": ChatMessage,
    "chat_response": ChatResponse,
    "chat_stream": ChatStreamChunk,
    "tool_call_request": ToolCallRequest,
    "tool_result": ToolResult,
    "error": ErrorMessage,
    "ping": PingMessage,
    "pong": PongMessage,
    "session_update": SessionUpdate,
    "status": StatusMessage,
}


def parse_message(data: str) -> BaseMessage:
    """Parse JSON message to appropriate message class"""
    try:
        obj = json.loads(data)
        msg_type = obj.get("type", "")
        cls = MESSAGE_TYPES.get(msg_type, BaseMessage)
        return cls(**obj)
    except Exception as e:
        # Return raw error message
        return ErrorMessage(
            type="error",
            code="parse_error",
            message=f"Failed to parse message: {e}",
            details={"raw": data[:200]}
        )


def create_chat_message(content: str, profile: str = "default", 
                        session_id: Optional[str] = None) -> ChatMessage:
    """Create a chat message"""
    return ChatMessage(
        content=content,
        profile=profile,
        session_id=session_id
    )


def create_ping() -> PingMessage:
    """Create a ping message"""
    return PingMessage()


# JSON-RPC 2.0 support (alternative protocol)
@dataclass
class JSONRPCRequest:
    """JSON-RPC 2.0 request"""
    jsonrpc: str = "2.0"
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    id: Union[str, int, None] = field(default_factory=lambda: str(uuid.uuid4()))
    
    def to_json(self) -> str:
        d = self.__dict__.copy()
        if d["id"] is None:
            del d["id"]
        return json.dumps(d)


@dataclass
class JSONRPCResponse:
    """JSON-RPC 2.0 response"""
    jsonrpc: str = "2.0"
    result: Any = None
    error: Optional[Dict[str, Any]] = None
    id: Union[str, int, None] = None
    
    def to_json(self) -> str:
        d = self.__dict__.copy()
        if d["error"] is None:
            del d["error"]
        if d["result"] is None:
            del d["result"]
        return json.dumps(d)
    
    @classmethod
    def from_json(cls, data: str) -> "JSONRPCResponse":
        return cls(**json.loads(data))


def create_jsonrpc_request(method: str, params: Dict[str, Any] = None, 
                           request_id: Union[str, int] = None) -> JSONRPCRequest:
    """Create JSON-RPC request"""
    return JSONRPCRequest(
        method=method,
        params=params or {},
        id=request_id or str(uuid.uuid4())
    )


def create_jsonrpc_response(request_id: Union[str, int], 
                           result: Any = None, error: Dict = None) -> JSONRPCResponse:
    """Create JSON-RPC response"""
    return JSONRPCResponse(
        id=request_id,
        result=result,
        error=error
    )