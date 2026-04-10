"""Context compaction — model-generated conversation summaries."""
import re
import json
import logging

import requests

from qwen_client.config import config, COLORS, print_colored

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = """\
Summarize the conversation so far into a structured summary with these sections:

1. TASK: What the user is trying to accomplish
2. COMPLETED: What has been done so far (files created/modified, commands run, decisions made)
3. CURRENT_STATE: Where things stand right now (what's working, what's not)
4. KEY_FILES: Important file paths that were read, written, or discussed
5. OPEN_ITEMS: Anything left to do or unresolved issues
6. ERRORS: Any errors encountered and their resolution status
7. DECISIONS: Key architectural or design decisions made
8. CONTEXT: Any other important context the next part of the conversation needs
9. WORKING_DIRECTORY: The current working directory and project structure context

Be concise but complete. This summary replaces the conversation history, so include \
everything needed to continue the work. Use bullet points, not prose."""


def _format_messages_for_summary(messages, max_chars=40000):
    """Format messages into a text block for the summary prompt."""
    parts = []
    total = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        if role == "system":
            continue
        content = msg.get("content", "")
        # Cap individual messages to avoid one giant tool output dominating
        if len(content) > 800:
            content = content[:400] + "\n...[truncated]...\n" + content[-400:]
        line = f"[{role}]: {content}"
        if total + len(line) > max_chars:
            parts.append(f"[...{len(messages) - len(parts)} older messages omitted...]")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)


def _summarize_tool_output(content):
    """Extract a one-line summary from a tool output message."""
    if not content.startswith("Tool output:\n"):
        return None
    body = content[len("Tool output:\n"):]
    # Count lines and extract key info
    lines = body.split('\n')
    line_count = len(lines)
    # Look for file paths, command names
    first_line = lines[0].strip() if lines else ""
    if first_line.startswith("[Command"):
        return f"Tool output: {first_line[:120]}... ({line_count} lines)"
    return f"Tool output: ({line_count} lines, {len(body)} chars)"


def microcompact(messages, keep_recent=8):
    """Replace old tool output bodies with one-line summaries.

    Modifies *messages* in place.  Preserves message count and role sequence
    so conversation structure remains valid.  No model call needed.
    """
    if len(messages) <= keep_recent:
        return
    cutoff = len(messages) - keep_recent
    compacted = 0
    for i in range(cutoff):
        msg = messages[i]
        content = msg.get("content", "")
        summary = _summarize_tool_output(content)
        if summary and len(content) > 500:
            messages[i] = {**msg, "content": summary}
            compacted += 1
        elif msg.get("role") == "assistant" and len(content) > 3000:
            # Truncate very long assistant messages (keep first + last 300 chars)
            messages[i] = {
                **msg,
                "content": content[:300] + f"\n...[compacted, was {len(content)} chars]...\n" + content[-300:]
            }
            compacted += 1
    if compacted:
        logger.info("Microcompacted %d messages", compacted)


def compact_conversation(history, model, agent_theme=None, keep_recent=4, reason="manual"):
    """Generate a model-based summary of older messages and replace them.

    Args:
        history: The mutable message list (modified in place).
        model: Current agent model name.
        agent_theme: Theme dict for display (unused, kept for API consistency).
        keep_recent: Number of recent messages to preserve verbatim.
        reason: "manual" (/compact command) or "auto" (context limit approaching).

    Returns:
        (success: bool, message: str) describing what happened.
    """
    if len(history) <= keep_recent + 4:
        return False, "History too short to compact."

    old_messages = history[:-keep_recent] if keep_recent else history[:]
    recent = history[-keep_recent:] if keep_recent else []

    # Don't bother if old_messages is trivial
    if len(old_messages) < 8:
        return False, "Not enough old messages to warrant compaction."

    formatted = _format_messages_for_summary(old_messages)
    prompt_content = f"{COMPACTION_PROMPT}\n\n--- Conversation to summarize ---\n{formatted}"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_content}],
        "stream": False,
        "max_tokens": 2000,
        "temperature": 0.3,
    }

    try:
        print_colored(
            f"  [{reason}] Generating conversation summary ({len(old_messages)} messages)...",
            COLORS['BLUE']
        )
        resp = requests.post(
            config.API_URL, json=payload,
            headers={"Content-Type": "application/json", **config.auth_headers},
            timeout=120,
        )
        if resp.status_code != 200:
            logger.error("Compaction request failed: %d %s", resp.status_code, resp.text[:200])
            return False, f"Server returned {resp.status_code}."

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return False, "No response from model."

        summary_text = choices[0].get("message", {}).get("content", "").strip()
        if not summary_text or len(summary_text) < 50:
            return False, "Model produced an empty or too-short summary."

        # Validate summary has at least some expected structure
        expected_sections = ["TASK", "COMPLETED", "CURRENT_STATE", "KEY_FILES"]
        if not any(s in summary_text.upper() for s in expected_sections):
            logger.warning("Compaction summary missing expected sections, using anyway")

        # Replace history in place
        summary_msg = {
            "role": "user",
            "content": f"[Conversation Summary — {len(old_messages)} messages compacted]\n\n{summary_text}"
        }
        history[:] = [summary_msg] + recent

        logger.info("Compacted %d messages into summary (%d chars), kept %d recent",
                     len(old_messages), len(summary_text), len(recent))
        return True, f"Summarized {len(old_messages)} messages."

    except requests.exceptions.Timeout:
        return False, "Compaction request timed out (120s)."
    except Exception as e:
        logger.error("Compaction failed: %s", e)
        return False, f"Compaction error: {e}"
