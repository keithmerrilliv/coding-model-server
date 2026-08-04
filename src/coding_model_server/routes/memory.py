"""Memory, web-search, Apple-docs, ingest, and file-upload routes.

The backing services (memory, web search, Apple Deep Docs MCP) are initialized
in the app lifespan and read at call-time from ``runtime.services`` so these
handlers see whatever lifespan published rather than capturing None at import.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException

from coding_model_server import runtime
from coding_model_server.config import Config
from coding_model_server.runtime import verify_admin_key
from coding_model_server.schemas import (
    DeepDocRequest, FileUploadRequest, IngestRequest, MemoryDeleteRequest,
    MemoryRequest, SearchRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/memory", dependencies=[Depends(verify_admin_key)])
def save_memory_endpoint(request: MemoryRequest):
    """Save a memory/fact to the long-term storage.

    If *source* is provided (e.g. a file path like "main.swift"), the text is
    parsed into language-aware chunks using tree-sitter before storage.
    Without *source*, the text is stored as a single document (backward compatible).
    """
    if not runtime.services.memory:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    try:
        if request.source:
            result = runtime.services.memory.add_memory_chunked(request.text, source=request.source)
        else:
            # `source` routes to the tree-sitter CODE chunker, which is wrong
            # for prose. Doc scrapers therefore send prose via this branch and
            # carry provenance in `metadata` instead.
            result = runtime.services.memory.add_memory(request.text, metadata=request.metadata)

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error saving memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/memory/delete", dependencies=[Depends(verify_admin_key)])
def delete_memory_endpoint(request: MemoryDeleteRequest):
    """Delete memories by id and/or metadata filter.

    POST rather than DELETE: the filter is a nested object, and bodies on DELETE
    are poorly supported across clients and proxies.

    Admin-key gated like every other memory route. Note the asymmetry this
    closes — anyone who could POST /v1/memory could grow the collection without
    limit, but nobody could shrink it.
    """
    if not runtime.services.memory:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    result = runtime.services.memory.delete_memories(
        ids=request.ids, where=request.where,
        allow_delete_all=request.allow_delete_all,
    )
    if "error" in result:
        # A refusal to wipe unfiltered is the caller's mistake, not a server
        # fault, so it comes back as 400 rather than 500.
        code = 400 if "refusing to delete" in result["error"] else 500
        raise HTTPException(status_code=code, detail=result["error"])
    return result


@router.post("/v1/memory/search", dependencies=[Depends(verify_admin_key)])
def search_memory_endpoint(request: SearchRequest):
    """Search for relevant memories in the long-term storage"""
    if not runtime.services.memory:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    try:
        results = runtime.services.memory.search_memory(request.query)
        return {"results": results}
    except Exception as e:
        logger.error("Error searching memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/tools/search", dependencies=[Depends(verify_admin_key)])
def web_search_endpoint(request: SearchRequest):
    """Perform a web search using DuckDuckGo"""
    if not runtime.services.web_search:
        raise HTTPException(status_code=503, detail="Web search service not initialized")

    result = runtime.services.web_search.search(request.query)
    return {"result": result}


@router.post("/v1/tools/apple_deep_docs", dependencies=[Depends(verify_admin_key)])
def apple_deep_docs_endpoint(request: DeepDocRequest):
    """Perform an Apple Documentation search using the server-side MCP"""
    if not runtime.services.apple_deep_docs:
        raise HTTPException(status_code=503, detail="Apple Deep Docs service not initialized")

    result = runtime.services.apple_deep_docs.call_tool(request.tool, request.arguments)
    return {"result": result}


@router.post("/v1/memory/ingest", dependencies=[Depends(verify_admin_key)])
def ingest_memory_endpoint(request: IngestRequest):
    """Ingest a local PDF file into long-term memory"""
    if not runtime.services.memory:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    # Path security validation. Use realpath (not just normpath) so a symlink
    # from an allowed directory to outside-the-jail is caught. normpath alone
    # would let /allowed/symlink → /etc/passwd through.
    if '..' in request.path.split(os.sep):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if not os.path.isabs(request.path):
        raise HTTPException(status_code=400, detail="Only absolute paths are allowed")
    try:
        resolved = os.path.realpath(request.path)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Cannot resolve path: {e}")

    # Allow ingestion from system temp directory (for uploads)
    import tempfile
    temp_dir = os.path.realpath(tempfile.gettempdir())
    is_in_temp = resolved.startswith(temp_dir + os.sep) or resolved == temp_dir

    if Config.INGEST_ALLOWED_DIR:
        allowed = os.path.realpath(Config.INGEST_ALLOWED_DIR)
        is_in_allowed = resolved.startswith(allowed + os.sep) or resolved == allowed

        if not is_in_allowed and not is_in_temp:
            raise HTTPException(status_code=403, detail=f"Path must be under {allowed} or {temp_dir}")
    elif not is_in_temp:
        raise HTTPException(status_code=403, detail=f"Path must be under {temp_dir} (set INGEST_ALLOWED_DIR to allow other paths)")

    # Honor the documented per-file cap (DEV-164) — PDF ingest previously
    # had no size limit at all, so a multi-GB file would be read whole.
    if Config.INGEST_MAX_FILE_SIZE:
        try:
            size = os.path.getsize(resolved)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Cannot stat path: {e}")
        if size > Config.INGEST_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=(f"File too large ({size} bytes); INGEST_MAX_FILE_SIZE "
                        f"is {Config.INGEST_MAX_FILE_SIZE}"),
            )

    result = runtime.services.memory.ingest_pdf(resolved)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/v1/files/upload", dependencies=[Depends(verify_admin_key)])
def upload_file_endpoint(request: FileUploadRequest):
    """Upload a file to the server's temporary directory"""
    try:
        import base64
        import tempfile

        # Reject oversized uploads before decoding (100 MB base64 ≈ 75 MB file)
        MAX_UPLOAD_B64_LEN = 100 * 1024 * 1024
        if len(request.content) > MAX_UPLOAD_B64_LEN:
            raise HTTPException(
                status_code=413,
                detail=f"Upload too large ({len(request.content) // (1024*1024)} MB base64). Max: 100 MB"
            )

        # Decode the base64 content
        try:
            file_content = base64.b64decode(request.content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 content: {str(e)}")

        # Sanitize filename: strip directory components to prevent path traversal
        safe_filename = os.path.basename(request.filename)
        if not safe_filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        temp_dir = tempfile.gettempdir()
        # Use mkstemp for atomic creation (avoids race conditions and overwrites)
        suffix = os.path.splitext(safe_filename)[1]
        fd, temp_path = tempfile.mkstemp(suffix=suffix, dir=temp_dir)
        try:
            os.write(fd, file_content)
        finally:
            os.close(fd)

        logger.info("File uploaded successfully: %s", temp_path)
        return {"status": "success", "path": temp_path, "size": len(file_content)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error uploading file: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
