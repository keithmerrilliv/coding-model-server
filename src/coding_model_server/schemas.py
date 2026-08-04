"""Pydantic request/response models for the FastAPI server.

Extracted from server.py so the route modules and the app wiring can share one
schema source. Pure data definitions — no app, no services, no side effects.
"""
from typing import Any, Dict, List, Literal, Optional, Union

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
    # What to retrieve ON, when the last user message is the wrong query.
    # Retrieval embeds the last user message by default, but all-MiniLM-L6-v2
    # truncates at 256 tokens and autonomous prompts are far longer: an
    # implementer's per-file message leads with the whole spec and design and
    # ends with the actual ask, so the ask is always discarded and every file
    # in a project retrieves on the same spec preamble (DEV-489). Callers that
    # know their real subject pass it here — e.g. the target file's path,
    # purpose and exports. Falls back to the last user message when unset.
    memory_query: Optional[str] = None

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
    # Gates waiting on a human (DEV-430). A quiet daemon and a green health
    # check used to look identical whether the pipeline was idle or blocked
    # on a review nobody knew had opened. None means the count is unavailable.
    open_review_gates: Optional[int] = None


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
    # Free-form provenance stored alongside the chunk. Without this every
    # HTTP ingest was stamped source="manual" by add_memory's defaults, which
    # is why ~89% of the collection cannot be attributed to a framework, URL
    # or SDK version and so cannot be selectively refreshed. Keys collide with
    # and override the defaults (source/date/timestamp), which is intended:
    # a doc scraper knows its real source, the default does not.
    # Chroma only stores scalars, so values are restricted accordingly.
    metadata: Optional[Dict[str, Union[str, int, float, bool]]] = None


class MemoryDeleteRequest(BaseModel):
    """Request to delete memories by id and/or metadata filter.

    `where` is a chromadb metadata filter, so provenance keys written by the
    doc scraper are directly usable — {"framework": "Metal"} drops exactly one
    framework's chunks, which is what makes a per-framework refresh possible
    instead of an archive-and-rebuild.
    """
    ids: Optional[List[str]] = None
    where: Optional[Dict[str, Any]] = None
    # Explicit opt-in for an unfiltered wipe; see MemoryService.delete_memories.
    allow_delete_all: bool = False


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
