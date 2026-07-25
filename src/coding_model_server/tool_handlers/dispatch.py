"""Marker parsing and tool dispatch — the top layer.

process_remote_commands scans an agent response for <<<TAG>>> markers and runs
the matching handler from files/shell (plus external handlers via state); the
git-style SEARCH/REPLACE normalizer and the markdown-code-block fallback live
here too. ingest_pdf_content is dispatch-only, so it stays with this layer.
"""
import os
import re
from typing import List, Optional

from coding_model_server.tool_state import state
from coding_model_server.tool_handlers.editing import _compact_summary
from coding_model_server.tool_handlers.files import (
    edit_file_content,
    glob_files,
    grep_search,
    list_directory,
    read_file_content,
    write_file_content,
)
from coding_model_server.tool_handlers.safety import _should_auto_approve
from coding_model_server.tool_handlers.shell import execute_remote_command


# Tools that reach the network. Whatever the agent read this turn can leave the
# machine through one of these, so they get an approval gate at dispatch time.
# CUPERTINO / APPLE_DEEP_DOCS also egress, but only to fixed Apple documentation
# endpoints — they carry no attacker-chosen destination, so they are not gated.
_OUTBOUND_FETCH_TAGS = frozenset({'WEB_SEARCH', 'DEEP_INGEST'})


def _confirm_outbound_fetch(tag, arg):
    """Gate an outbound-fetch tool before it runs.

    Returns None to proceed, or a denial string to short-circuit dispatch.
    yolo/acceptEdits auto-approve, consistent with _should_auto_approve on the
    write path; 'default' mode prompts.
    """
    if _should_auto_approve(tag):
        state.print_colored(
            f"   Auto-approved outbound fetch ({state.permission_mode} mode)", state.colors['GREEN']
        )
        return None
    state.print_colored(f"\nAgent wants an outbound fetch via {tag}: {arg[:200]}",
                        state.colors['WARNING'])
    state.print_colored("   This can send local data off-machine.", state.colors['WARNING'])
    try:
        choice = input(f"{state.colors['BOLD']}Allow outbound {tag}? [y/N] > {state.colors['ENDC']}")
    except (EOFError, KeyboardInterrupt):
        return f"User cancelled outbound {tag}."
    if choice.lower() != 'y':
        state.logger.info("Outbound %s denied by user: %s", tag, arg[:100])
        return f"User denied outbound {tag}."
    return None


_FENCE_RE = re.compile(r'```.*?(?:```|\Z)', re.DOTALL)
_WRITE_EDIT_OPENER_RE = re.compile(r'<{1,3}(?:WRITE_FILE|EDIT_FILE)>{1,3}', re.IGNORECASE)
_END_FILE_RE = re.compile(r'<{1,3}END_FILE>{1,3}', re.IGNORECASE)


def _mask_protected_regions(text: str) -> str:
    """Same-length copy of *text* with marker brackets neutralized inside
    regions the command scanner must treat as opaque content (DEV-131):

      * fenced code blocks — a response that QUOTES a tool marker inside
        ``` fences (docs, examples, tests) must not have it executed;
      * WRITE_FILE/EDIT_FILE payloads explicitly terminated by
        <<<END_FILE>>> — lets the model write content that itself contains
        live markers by declaring where the payload ends.

    Only '<' and '>' are substituted (with NUL/SOH), so every offset in the
    masked copy maps 1:1 onto the original: the scanner matches against the
    masked text and slices actual content out of the original by span.
    Without this, a WRITE_FILE whose content contained any known marker was
    truncated at it and the tail DISPATCHED as a real command — writing a
    corrupt file and executing quoted examples.
    """
    chars = list(text)

    def _mask(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] == '<':
                chars[i] = '\x00'
            elif chars[i] == '>':
                chars[i] = '\x01'

    for m in _FENCE_RE.finditer(text):
        _mask(m.start(), m.end())

    # Snapshot after fence masking: openers inside fences are already dead,
    # and an END_FILE quoted inside a fence can't terminate a real payload.
    fence_masked = ''.join(chars)
    for m in _WRITE_EDIT_OPENER_RE.finditer(fence_masked):
        term = _END_FILE_RE.search(fence_masked, m.end())
        if term:
            _mask(m.end(), term.start())

    return ''.join(chars)


def _normalize_standalone_search_replace(text):
    """Convert standalone git-style SEARCH/REPLACE blocks into <<<EDIT_FILE>>> blocks.

    Catches the case where the model emits a bare search/replace block without
    wrapping it in <<<EDIT_FILE>>>:
        /path/to/file
        <<<<<<< SEARCH
        old text
        =======
        new text
        >>>>>>> REPLACE

    Converts to:
        <<<EDIT_FILE>>>/path/to/file
        <<<OLD>>>
        old text
        <<<NEW>>>
        new text
    """
    # Match: filepath line, then <<<<<<< SEARCH block(s)
    # The filepath is on the line before <<<<<<< SEARCH
    pattern = (
        r'^([^\n<>]+?)\s*\n'                  # filepath (line without angle brackets)
        r'<{1,7}\s*SEARCH\s*>{0,7}\s*\n'      # <<<<<<< SEARCH
        r'(.*?)'                                # old text (multi-line, lazy)
        r'\n={3,}\s*\n'                         # =======
        r'(.*?)'                                # new text (multi-line, lazy)
        r'\n>{1,7}\s*REPLACE\s*>{0,7}'         # >>>>>>> REPLACE
    )
    def _replace(m):
        path = m.group(1).strip()
        old = m.group(2)
        new = m.group(3)
        return f'<<<EDIT_FILE>>>{path}\n<<<OLD>>>\n{old}\n<<<NEW>>>\n{new}\n'

    return re.sub(pattern, _replace, text, flags=re.DOTALL | re.MULTILINE)


def process_remote_commands(response_text: str) -> Optional[str]:
    """Process ALL remote command markers in agent response.

    Finds and executes every tool marker in the response, in order of appearance.
    Returns aggregated output from all commands, or None if no markers found.
    """
    # Pre-process: strip Qwen3.5 native formatting that breaks marker parsing.
    # <tool_call> special token turns <<<TAG>>> into <tool_call>TAG>>> — strip it.
    response_text = re.sub(r'</?tool_call\s*>', '', response_text)
    # <REACT>Thought:...</REACT> reasoning blocks are noise — strip them.
    response_text = re.sub(r'<REACT>.*?</REACT>', '', response_text, flags=re.DOTALL)

    # Strip agentic markers (SCRATCHPAD, PLAN, CONFIDENCE) so they don't leak
    # into file content. These aren't tool commands but the command regex doesn't
    # know about them, so <<<CONFIDENCE>>>95 ends up inside WRITE_FILE payloads.
    response_text = re.sub(
        r'<{1,3}SCRATCHPAD>{1,3}\s*.*?(?=<{1,3}\w+>{1,3}|\Z)', '', response_text, flags=re.DOTALL
    )
    response_text = re.sub(
        r'<{1,3}PLAN>{1,3}\s*.*?(?=<{1,3}\w+>{1,3}|\Z)', '', response_text, flags=re.DOTALL
    )
    response_text = re.sub(r'<{0,3}/?CONFIDEN\w*>{0,3}\s*\d*', '', response_text)

    # Pre-process: convert standalone git-style SEARCH/REPLACE into EDIT_FILE blocks
    response_text = _normalize_standalone_search_replace(response_text)

    # Dispatch table: opening tag name -> handler
    command_handlers = {
        'REMOTE_EXEC':    lambda arg: execute_remote_command(arg.strip()),
        'READ_FILE':      lambda arg: read_file_content(arg.strip()),
        'WRITE_FILE':     lambda arg: write_file_content(arg),
        'EDIT_FILE':      lambda arg: edit_file_content(arg),
        'LIST_DIR':       lambda arg: list_directory(arg),
        'GLOB':           lambda arg: glob_files(arg),
        'GREP':           lambda arg: grep_search(arg),
        'SAVE_MEMORY':    lambda arg: state.external_handlers['save_memory'](arg.strip()),
        'WEB_SEARCH':     lambda arg: state.external_handlers['web_search'](arg.strip()),
        'CUPERTINO':      lambda arg: state.external_handlers['handle_cupertino_search'](arg.strip()),
        'APPLE_DEEP_DOCS': lambda arg: state.external_handlers['handle_apple_deep_docs'](arg.strip()),
        'INGEST_PDF':     lambda arg: ingest_pdf_content(arg),
        'DEEP_INGEST':    lambda arg: state.external_handlers['ingest_url_content'](arg.strip()),
    }

    # Build regex from known tags so internal markers (<<<OLD>>>, <<<NEW>>>) are not
    # mistaken for command boundaries.  Each command block runs from its opening tag
    # to the next known opening tag (or end of string).  No closing tags required.
    #
    # The regex matches liberally (1-3 brackets on each side) so we can detect
    # malformed markers as content-boundary anchors, but a post-match validation
    # step rejects any bracket combination that isn't one of the documented forms:
    #   <<<TAG>>>  -- standard triple-bracket format
    #   <TAG>>>    -- after <tool_call> special token strip (1 open, 3 close)
    #   <TAG>      -- XML-style (1 open, 1 close; seen with EDIT_FILE)
    # Asymmetric or partial markers like <<<TAG> or <<TAG>>> are rejected, which
    # forces the model to emit a properly-formed marker on its next turn instead
    # of silently executing a tool from a malformed call.
    _TAG_NAMES = '|'.join(command_handlers.keys())
    _COMMAND_RE = (
        rf'(<{{1,3}})({_TAG_NAMES})(>{{1,3}})\s*'
        rf'(.*?)'
        rf'(?=<{{1,3}}(?:{_TAG_NAMES})>{{1,3}}|\Z)'
    )
    _VALID_BRACKETS = frozenset({('<<<', '>>>'), ('<', '>>>'), ('<', '>')})

    # Scan against a masked copy so markers inside fenced code or inside an
    # END_FILE-terminated write payload are content, not command boundaries
    # (DEV-131). Offsets are identical, so payloads are sliced from the
    # original text by span.
    masked_text = _mask_protected_regions(response_text)

    all_matches = []
    for match in re.finditer(_COMMAND_RE, masked_text, re.DOTALL | re.IGNORECASE):
        open_brackets = match.group(1)
        tag = match.group(2).upper()
        close_brackets = match.group(3)
        content = response_text[match.start(4):match.end(4)].strip()

        if (open_brackets, close_brackets) not in _VALID_BRACKETS:
            if state.logger:
                state.logger.warning(
                    "Rejecting malformed tool marker: %s%s%s — expected one of "
                    "<<<TAG>>>, <TAG>>>, or <TAG>",
                    open_brackets, tag, close_brackets
                )
            continue

        if content:  # skip empty blocks (e.g. stale closing tags parsed as openers)
            all_matches.append((match.start(), match, tag, command_handlers[tag]))

    if not all_matches:
        return None

    # Execute all commands and aggregate results
    results = []
    total_len = 0
    GLOBAL_MAX_LEN = 40000 # ~10k tokens global cap for tool outputs in one turn

    for i, (_, match, tag, handler) in enumerate(all_matches):
        if total_len > GLOBAL_MAX_LEN:
            results.append(f"\n... [OMITTED {len(all_matches) - i} ADDITIONAL COMMANDS TO PREVENT CONTEXT OVERFLOW] ...")
            break

        # Slice the payload from the ORIGINAL text (the match ran on the
        # masked copy — same offsets, but the mask bytes must not reach the
        # handler).
        arg = response_text[match.start(4):match.end(4)]
        if tag in ('WRITE_FILE', 'EDIT_FILE'):
            # An explicit <<<END_FILE>>> terminator ends the payload; prose
            # between it and the next marker is not file content. Search on
            # the masked slice so a terminator QUOTED inside a fence doesn't
            # truncate the real payload.
            term = _END_FILE_RE.search(masked_text, match.start(4), match.end(4))
            if term:
                arg = response_text[match.start(4):term.start()]
        # Strip closing tags the model generates (e.g. </REMOTE_EXEC>, </list_dir>).
        # These get captured as part of the content and corrupt shell commands
        # (the shell interprets </TAG> as input redirection < /TAG>).
        arg = re.sub(r'</\w+>\s*$', '', arg)

        # Outbound-fetch gate. These tools reach the network, so they can carry
        # off whatever the agent has read this turn — that is the second half of
        # the read-and-exfil path READ_FILE's protected-path prompt closes the
        # first half of. Gate them here at the chokepoint, mirroring how the
        # file and shell handlers gate internally.
        if tag in _OUTBOUND_FETCH_TAGS:
            denial = _confirm_outbound_fetch(tag, arg.strip())
            if denial is not None:
                res_str = f"[Command {i+1}] {denial}"
                results.append(res_str)
                total_len += len(res_str)
                continue
        try:
            result = handler(arg)
            if result is None:
                continue  # Handler silently refused (e.g., write-loop detection)
            if result:
                # In compact mode, show one-liner to user but keep full result for history
                if not state.verbose_mode:
                    summary = _compact_summary(tag, arg, result)
                    if summary:
                        state.print_colored(summary, state.colors['CYAN'])
                res_str = f"[Command {i+1}] {result}"
                results.append(res_str)
                total_len += len(res_str)
        except Exception as e:
            err_str = f"[Command {i+1}] Error: {str(e)}"
            results.append(err_str)
            total_len += len(err_str)

    return "\n\n".join(results) if results else None


# Prose beyond this outside the fences means the response is an explanation
# that HAPPENS to contain a shell example, not a command the model meant to
# run. Short lead-ins ("Running the tests:") stay under it; a paragraph of
# "to build this project you would run..." does not.
_FALLBACK_MAX_PROSE_CHARS = 200


def extract_fallback_commands(response_text: str) -> List[str]:
    """Extract shell commands from markdown code blocks when <<<REMOTE_EXEC>>> markers are missing.

    Catches the common failure mode where the model writes a correct command
    inside a fenced code block instead of using the marker protocol.
    Only extracts blocks explicitly tagged as shell languages (bash/shell/sh/zsh).
    Plain or non-shell code blocks (python, swift, etc.) are ignored to prevent
    catastrophic misexecution of code snippets as shell commands.

    Extraction only applies when the response is essentially JUST the
    fence(s): substantial prose around a ```bash block means the model was
    explaining, not acting, and running its illustrative commands is the
    DEV-136 failure ("to build you'd run: ...make distclean" → executed).
    The caller additionally confirms before executing what this returns.
    """
    # Measure prose outside ALL fenced blocks (any language).
    prose = _FENCE_RE.sub('', response_text)
    if len(prose.strip()) > _FALLBACK_MAX_PROSE_CHARS:
        return []

    commands = []

    # REQUIRE a shell language hint -- the ? was removed to prevent matching
    # python/swift/unlabeled blocks which caused catastrophic misexecution.
    # Allow matching to end-of-string (```|\Z) for unclosed code blocks --
    # the model sometimes emits a stop token before closing backticks.
    for m in re.finditer(r'```(?:bash|shell|sh|zsh)\s*\n(.+?)(?:```|\Z)', response_text, re.DOTALL):
        block = m.group(1).strip()
        if block:
            commands.append(block)

    return commands


def ingest_pdf_content(payload):
    """Ingest a PDF file into memory. Can handle both local client files and server files.

    Payload format:
        /path/to/document.pdf       - Path to PDF file (on client or server)
        local:/path/to/document.pdf - Explicitly indicates client-side file to upload first

    Examples:
        /home/user/docs/manual.pdf
        local:/Users/me/Documents/report.pdf
    """
    try:
        import base64
        import requests

        path = payload.strip()
        if not path:
            return "Error: No PDF path provided"

        # Check if this is a local file that needs to be uploaded
        if path.startswith('local:') or not path.startswith('/'):
            # This is a local file path - we need to upload it first
            if path.startswith('local:'):
                local_path = path[6:]  # Remove 'local:' prefix
            else:
                local_path = path

            # Expand user path if needed
            local_path = os.path.expanduser(local_path)

            # Check if file exists locally
            if not os.path.exists(local_path):
                return f"Error: Local file not found: {local_path}"

            # Read the file content
            with open(local_path, 'rb') as f:
                file_content = f.read()

            # Encode as base64
            encoded_content = base64.b64encode(file_content).decode('utf-8')

            # Upload the file to the server
            upload_payload = {
                "filename": os.path.basename(local_path),
                "content": encoded_content
            }

            upload_url = f"http://{state.config.LINUX_SERVER_IP}:5000/v1/files/upload"
            state.print_colored(f"Uploading {local_path} to server...", state.colors['CYAN'])

            response = requests.post(upload_url, json=upload_payload, timeout=state.config.LONG_REQUEST_TIMEOUT)
            if response.status_code != 200:
                return f"Error uploading file: {response.text}"

            upload_result = response.json()
            server_path = upload_result.get("path", "")

            if not server_path:
                return "Error: Upload succeeded but no path returned"

            state.print_colored(f"File uploaded to server: {server_path}", state.colors['GREEN'])

            # Now ingest the uploaded file
            result = state.external_handlers['ingest_pdf'](server_path)
            return result
        else:
            # This is a server-side file path - ingest directly
            result = state.external_handlers['ingest_pdf'](path)
            return result
    except Exception as e:
        state.logger.error("Error ingesting PDF %s: %s", path, str(e))
        return f"Error ingesting PDF: {str(e)}"
