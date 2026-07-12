"""Pydantic request/response models for the FastAPI server.

Extracted from server.py so the route modules and the app wiring can share one
schema source. Pure data definitions — no app, no services, no side effects.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    """A single message in the chat conversation.

    Supports OpenAI tool-calling shape: assistant messages may carry
    `tool_calls`, and tool-result turns use role='tool' with `tool_call_id`.
    """
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

    @field_validator('content', mode='before')
    @classmethod
    def flatten_content_parts(cls, v):
        # OpenAI multimodal content: third-party clients (qwen-code, openai-python
        # ≥1.0) may send `content` as a list of parts like
        # [{"type":"text","text":"..."}]. Flatten to a plain string so the rest
        # of the pipeline (template formatting, marker scanning) keeps working.
        # Non-text parts are dropped — there's no vision pipeline here.
        if isinstance(v, list):
            parts = []
            for part in v:
                if isinstance(part, dict) and part.get('type') == 'text':
                    text = part.get('text')
                    if isinstance(text, str):
                        parts.append(text)
            return '\n'.join(parts) if parts else None
        return v

    @field_validator('content')
    @classmethod
    def content_not_empty(cls, v: Optional[str]) -> Optional[str]:
        # Empty content is permitted for assistant turns that carry only tool_calls.
        if v is None:
            return v
        if not v.strip():
            raise ValueError('content cannot be empty when provided')
        return v


class ChatCompletionRequest(BaseModel):
    """Request body for chat completions endpoint"""
    model: str = "implementer"
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: int = Field(default=16384, ge=1, le=524288)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None  # str ("auto"|"none"|"required") or {"type":"function","function":{"name":...}}
    parallel_tool_calls: Optional[bool] = None
    # Opt-out for the server-side ChromaDB memory injection. Autonomous
    # callers (architect/implementer/reviewer/supervisor/planner) set this
    # to True so the user-populated chat memory doesn't pollute their
    # structured prompts (the memory was populated by chat-style use; a
    # semantic search of "## Specification\n\n# Roman numeral converter…"
    # mostly retrieves noise). Default False preserves chat behavior.
    skip_memory: bool = False

    @field_validator('messages')
    @classmethod
    def messages_not_empty(cls, v: List[ChatMessage]) -> List[ChatMessage]:
        if not v:
            raise ValueError('messages array cannot be empty')
        return v


class ChatMessageResponse(BaseModel):
    """Response message format"""
    role: str
    content: str


class ChatChoice(BaseModel):
    """A single choice in chat completion response"""
    index: int
    message: ChatMessageResponse
    finish_reason: str


class UsageStats(BaseModel):
    """Token usage statistics"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """Full chat completion response"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: UsageStats


class ModelInfo(BaseModel):
    """Model information for list endpoint"""
    id: str
    object: str = "model"
    created: int
    owned_by: str
    description: str


class ModelListResponse(BaseModel):
    """Response for list models endpoint"""
    object: str = "list"
    data: List[ModelInfo]


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    model_loaded: bool
    agents: List[str]
    timestamp: str


class ErrorDetail(BaseModel):
    """Error response detail"""
    message: str
    type: str
    code: int


class ErrorResponse(BaseModel):
    """Error response wrapper"""
    error: ErrorDetail


class MemoryRequest(BaseModel):
    """Request to save a memory"""
    text: str = Field(..., max_length=200_000)  # ~100KB, matches client file-size cap
    source: Optional[str] = None  # file path hint for language detection


class SearchRequest(BaseModel):
    """Request to search the web"""
    query: str


class IngestRequest(BaseModel):
    """Request to ingest a local file"""
    path: str


class DeepDocRequest(BaseModel):
    """Request for Apple Deep Docs"""
    tool: str
    arguments: Dict[str, Any]


class FileUploadRequest(BaseModel):
    """Request to upload a file to the server"""
    filename: str
    content: str  # Base64 encoded content
