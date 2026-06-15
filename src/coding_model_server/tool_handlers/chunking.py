"""Output chunking: split large tool output into context-window-friendly
pieces, and format a chunk (or first/last + error summary) for display.

Self-contained except for chunk-size/overlap defaults read from state.config.
"""
from coding_model_server.tool_state import state


def chunk_large_output(content, chunk_size=None, overlap=None):
    """
    Split large output into chunks with overlap to preserve context.

    Args:
        content (str): The large output content to chunk
        chunk_size (int): Maximum size of each chunk in characters (uses config if None)
        overlap (int): Number of overlapping characters between chunks (uses config if None)

    Returns:
        dict: Contains 'chunks' list, 'total_chunks', 'chunk_size', and 'original_length'
    """
    # Use config values if not provided
    if chunk_size is None:
        chunk_size = state.config.CHUNK_SIZE
    if overlap is None:
        overlap = state.config.CHUNK_OVERLAP

    if len(content) <= chunk_size:
        return {
            'chunks': [content],
            'total_chunks': 1,
            'chunk_size': chunk_size,
            'original_length': len(content),
            'needs_chunking': False
        }

    chunks = []
    start = 0
    content_len = len(content)

    while start < content_len:
        end = start + chunk_size

        # If this is the last chunk, include the remainder
        if end >= content_len:
            chunk = content[start:]
        else:
            # Try to break at a line boundary using rfind (simpler than binary search)
            line_break = content.rfind('\n', start, end)

            if line_break != -1 and line_break > start:
                chunk = content[start:line_break + 1]
            else:
                chunk = content[start:end]

        chunks.append(chunk)
        chunk_len = len(chunk)

        # Calculate next start position with overlap
        if chunk_len < chunk_size:
            # Last chunk, no need to continue
            break

        # Move start forward, accounting for overlap
        next_start = start + chunk_len - overlap
        if next_start >= content_len:
            break
        start = next_start

    return {
        'chunks': chunks,
        'total_chunks': len(chunks),
        'chunk_size': chunk_size,
        'original_length': len(content),
        'needs_chunking': True
    }


def get_chunk_for_display(content, chunk_idx=None, chunk_size=None, overlap=None, log_path=None):
    """
    Get a specific chunk or a summary of chunks for display in the context window.

    Args:
        content (str): The full content to chunk
        chunk_idx (int, optional): Specific chunk index to return (-1 for all chunks)
        chunk_size (int): Size of each chunk (uses config if None)
        overlap (int): Overlap between chunks (uses config if None)
        log_path (str, optional): Path to the log file for reference

    Returns:
        str: Formatted output for display
    """
    # Use config values if not provided
    if chunk_size is None:
        chunk_size = state.config.CHUNK_SIZE
    if overlap is None:
        overlap = state.config.CHUNK_OVERLAP

    chunk_result = chunk_large_output(content, chunk_size, overlap)

    if not chunk_result['needs_chunking']:
        return content

    if chunk_idx is not None and 0 <= chunk_idx < len(chunk_result['chunks']):
        # Return specific chunk
        return f"[CHUNK {chunk_idx + 1}/{chunk_result['total_chunks']}]\n{chunk_result['chunks'][chunk_idx]}"
    elif chunk_idx == -1:
        # Return all chunks (for when space permits)
        result = []
        for i, chunk in enumerate(chunk_result['chunks']):
            result.append(f"[CHUNK {i + 1}/{chunk_result['total_chunks']}]\n{chunk}")
        return "\n--- CHUNK BREAK ---\n".join(result)
    else:
        # Return first chunk + last chunk + error summary (similar to current approach but more structured)
        first_chunk = chunk_result['chunks'][0]
        last_chunk = chunk_result['chunks'][-1]

        # Look for errors in all chunks
        all_error_lines = []
        for i, chunk in enumerate(chunk_result['chunks'][1:-1]):  # Skip first and last which are displayed fully
            for line in chunk.splitlines():
                if 'error' in line.lower() or 'fail' in line.lower() or 'warning' in line.lower():
                    all_error_lines.append(f"(Chunk {i+2}) {line.strip()}")

        # Limit error lines to prevent overflow
        error_summary = ""
        if all_error_lines:
            error_summary = "\n... [ERROR/WARNING SUMMARY] ...\n" + "\n".join(all_error_lines[:20])
            if len(all_error_lines) > 20:
                error_summary += f"\n... ({len(all_error_lines) - 20} more error lines omitted) ..."

        log_ref = f"... [Log: {log_path}] ..." if log_path else ""

        return (
            f"[CHUNK 1/{chunk_result['total_chunks']} - FIRST]\n{first_chunk}\n"
            f"\n... [CHUNKED: {chunk_result['original_length']} chars in {chunk_result['total_chunks']} chunks] ...\n"
            f"{log_ref}\n"
            f"{error_summary}\n"
            f"\n[CHUNK {chunk_result['total_chunks']}/{chunk_result['total_chunks']} - LAST]\n{last_chunk}"
        )
