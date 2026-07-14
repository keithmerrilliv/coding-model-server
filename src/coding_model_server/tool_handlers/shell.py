"""Shell command execution: safe parsing, path expansion, whitelist gating, and
the REMOTE_EXEC handler with its security checks (deny rules, dangerous-command
warnings, yolo auto-approval) and chunked output.
"""
import json
import os
import re
import shlex
import subprocess
import tempfile
from typing import List

from coding_model_server.tool_state import state
from coding_model_server.tool_handlers.chunking import chunk_large_output, get_chunk_for_display
from coding_model_server.tool_handlers.workspace import get_workspace
from coding_model_server.tool_handlers.safety import (
    _check_dangerous_command,
    _check_deny_rules,
    _is_auto_approvable_command,
    _should_auto_approve,
)


def parse_command_safely(command: str) -> List[str]:
    """Parse command string into argument list safely"""
    if not command or not isinstance(command, str):
        raise ValueError("Command must be a non-empty string")

    # Sanitize command string
    command = command.strip()
    if not command:
        raise ValueError("Command cannot be empty after trimming")

    if not state.config.ALLOW_SHELL_MODE:
        dangerous_chars = ['|', '&', ';', '$', '`', '\n', '>', '<', '(', ')', '*', '?', '[', ']']
        if any(char in command for char in dangerous_chars):
            raise ValueError(
                f"Command contains shell metacharacters. "
                f"Set ALLOW_SHELL_MODE=true to enable shell features, "
                f"or rewrite command without: {', '.join(dangerous_chars)}"
            )

    try:
        parsed = shlex.split(command)
        # Additional validation: ensure no empty arguments that could cause issues
        if any(arg == '' for arg in parsed):
            raise ValueError("Command contains empty arguments after parsing")
        return parsed
    except ValueError as e:
        raise ValueError(f"Failed to parse command: {e}")
    except Exception as e:
        raise ValueError(f"Unexpected error parsing command: {e}")


def expand_paths_in_args(command_args: List[str]) -> List[str]:
    """Expand tilde (~) in command arguments for proper path resolution

    When using shell=False, tilde expansion doesn't happen automatically.
    This function expands ~ in arguments that look like paths.
    """
    expanded_args = []
    for arg in command_args:
        # Expand tilde if argument starts with ~ or contains =~
        if arg.startswith('~'):
            expanded_args.append(os.path.expanduser(arg))
        elif '=~' in arg:
            # Handle cases like --file=~/path or VAR=~/path
            key, value = arg.split('=', 1)
            if value.startswith('~'):
                expanded_args.append(f"{key}={os.path.expanduser(value)}")
            else:
                expanded_args.append(arg)
        else:
            expanded_args.append(arg)
    return expanded_args


def is_command_allowed(command_args: List[str]) -> tuple:
    """Check if command is allowed based on whitelist.

    COMMAND_WHITELIST is unset by default, and an unset whitelist permits every
    binary. That is deliberate — the agent has to be able to run this project's
    build and test tooling — but it means this function is NOT the thing keeping
    a destructive binary from running. In safe mode the protection is:

      1. parse_command_safely() rejects shell metacharacters (no chaining,
         redirection or $(...) — the command is one argv, run with shell=False),
      2. the deny rules block the unconditionally-catastrophic forms, and
      3. every command still needs confirmation unless it is on the
         auto-approve allow-list (safety._is_auto_approvable_command).

    Set COMMAND_WHITELIST to a comma-separated list of binaries to also restrict
    *which* commands may run at all.
    """
    if not state.config.COMMAND_WHITELIST:
        return True, "No whitelist configured (metacharacter filtering only; command still requires approval)"

    if not command_args:
        return False, "Empty command"

    base_command = command_args[0]

    # Additional security check: prevent path traversal attempts
    if '..' in base_command:
        return False, f"Command '{base_command}' contains path traversal ('..') which is not allowed"

    if base_command in state.config.COMMAND_WHITELIST:
        return True, f"Command '{base_command}' is whitelisted"

    if '/' in base_command:
        base_name = os.path.basename(base_command)
        if base_name in state.config.COMMAND_WHITELIST:
            return True, f"Command '{base_name}' is whitelisted"

    return False, f"Command '{base_command}' not in whitelist: {', '.join(state.config.COMMAND_WHITELIST)}"


# Output chunking lives in .chunking; re-exported for the public API + the
# file/shell handlers below that call get_chunk_for_display.
from coding_model_server.tool_handlers.chunking import (  # noqa: E402
    chunk_large_output,
    get_chunk_for_display,
)


def execute_remote_command(command, chunk_output=True):
    """Internal function to execute a command with security checks"""

    # Validate input
    if not command or not isinstance(command, str):
        state.logger.warning("Invalid command received: %s", command)
        return "Error: Invalid command - command must be a non-empty string"

    state.logger.info("Executing command: %s", command)

    # Detect and block shell-based file writes that bypass WRITE_FILE loop detection.
    # Agents use 'python3 -c "open(...).write()"' or heredocs to circumvent.
    _shell_write_match = re.search(
        r"open\s*\(.+['\"]w['\"]|>\s*\S+\.(?:swift|py|md|yml|json|txt|h|m|metal)\b|cat\s*<<",
        command
    )
    if _shell_write_match:
        state.shell_write_count += 1
        if state.shell_write_count > state.MAX_WRITES_PER_FILE:
            over = state.shell_write_count - state.MAX_WRITES_PER_FILE
            msg = (f"Shell file write BLOCKED ({state.shell_write_count - 1} attempts). "
                   f"Do NOT use python open(), cat <<, sed -i, or > to write files. "
                   f"You MUST use <<<WRITE_FILE>>>path\\ncontent or <<<EDIT_FILE>>> instead.")
            state.logger.warning(msg)
            state.print_colored(f"\n[Shell write blocked] {msg}", state.colors['FAIL'])
            if over > 2:
                return None  # Hard stop — agent won't learn, break the loop
            return msg  # Return feedback so agent learns to switch approach
        state.print_colored(f"\nAgent is writing files via shell instead of <<<WRITE_FILE>>>.", state.colors['WARNING'])
        state.print_colored(f"   This bypasses diff preview and loop detection. ({state.shell_write_count}/{state.MAX_WRITES_PER_FILE})", state.colors['WARNING'])

    state.print_colored(f"\nAgent wants to run command: {command}", state.colors['WARNING'])

    # Deny rules — unconditionally blocked, no prompt
    denied, deny_reason = _check_deny_rules(command)
    if denied:
        state.print_colored(f"   DENIED: {deny_reason} — this operation is blocked.", state.colors['FAIL'])
        state.logger.warning("Command denied by deny rule: %s — %s", command[:100], deny_reason)
        return f"Command denied: {deny_reason}. This operation is not allowed."

    if state.config.ALLOW_SHELL_MODE:
        state.print_colored(f"   Shell mode enabled (less safe)", state.colors['WARNING'])
    else:
        state.print_colored(f"   Safe mode (shell=False)", state.colors['GREEN'])

    try:
        if not state.config.ALLOW_SHELL_MODE:
            command_args = parse_command_safely(command)
            allowed, msg = is_command_allowed(command_args)
            if not allowed:
                state.print_colored(f"   {msg}", state.colors['FAIL'])
                state.logger.warning("Command rejected: %s - %s", command, msg)
                return f"Command rejected: {msg}"
            state.print_colored(f"   {msg}", state.colors['GREEN'])
    except ValueError as e:
        state.print_colored(f"   {str(e)}", state.colors['FAIL'])
        state.logger.error("Command validation failed: %s - %s", command, str(e))
        return f"Command validation failed: {str(e)}"
    except Exception as e:
        state.print_colored(f"   Unexpected error during command validation: {str(e)}", state.colors['FAIL'])
        state.logger.error("Unexpected error during command validation: %s - %s", command, str(e), exc_info=True)
        return f"Command validation failed with unexpected error: {str(e)}"

    # Dangerous command detection — prompts even in yolo mode
    is_dangerous, danger_reason = _check_dangerous_command(command)
    if is_dangerous:
        state.print_colored(f"   DANGEROUS: {danger_reason}", state.colors['FAIL'])
        try:
            choice = input(f"{state.colors['BOLD']}Allow DANGEROUS command? [y/N] > {state.colors['ENDC']}")
        except (EOFError, KeyboardInterrupt):
            return "User cancelled dangerous command."
        if choice.lower() != 'y':
            state.logger.info("Dangerous command denied by user: %s — %s", command[:100], danger_reason)
            return f"User denied dangerous command ({danger_reason})."
    else:
        # Silent execution requires BOTH a permission mode that allows it AND a
        # command tame enough to be on the allow-list. The mode alone used to be
        # enough, which made the dangerous/deny pattern lists the security
        # boundary — and they leak: `find / -delete` and
        # `python3 -c "shutil.rmtree('/home/u')"` matched neither, so yolo ran
        # them without a word. An unknown command now costs a prompt, not the disk.
        auto_ok = False
        if state.config.ALLOW_SHELL_MODE:
            # shell=True: the whole string goes to /bin/sh, so metacharacters,
            # pipes and $(...) are live and none of the argv/whitelist checks
            # above ran. Never auto-approve that, in any mode.
            approvable_reason = "shell mode is on (shell=True is unrestricted)"
        elif not _should_auto_approve('REMOTE_EXEC'):
            approvable_reason = f"{state.permission_mode} mode does not auto-approve shell"
        else:
            auto_ok, approvable_reason = _is_auto_approvable_command(command)

        if auto_ok:
            state.print_colored(f"   Auto-approved ({state.permission_mode} mode: {approvable_reason})",
                                state.colors['GREEN'])
            state.logger.info("Auto-approving command (%s mode): %s", state.permission_mode, command)
            choice = 'y'
        else:
            if _should_auto_approve('REMOTE_EXEC'):
                # Mode would have auto-approved; the command itself did not qualify.
                state.print_colored(f"   Confirmation required: {approvable_reason}",
                                    state.colors['WARNING'])
                state.logger.info("Auto-approval refused (%s): %s", approvable_reason, command[:100])
            try:
                choice = input(f"{state.colors['BOLD']}Allow? [y/N] > {state.colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                state.logger.info("Command execution cancelled by user: %s", command)
                return "User cancelled command execution."

            if choice.lower() != 'y':
                state.logger.info("Command denied by user: %s", command)
                return "User denied command execution."

    try:
        # Create a temp file to capture output
        # We keep it if output is large so the agent can inspect it later
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, prefix='qwen_cmd_', suffix='.log') as tmp:
            tmp_path = tmp.name
        state.temp_tracker['add'](tmp_path)

        # Run commands from the workspace, not the process CWD (which is the repo
        # checkout — bin/start-client.sh cds there). This keeps `> out.txt` and
        # friends inside the workspace.
        #
        # Note this is a default, not a sandbox: shell=True can still `cd` out and
        # write anywhere the user can. Confining that needs real sandboxing (see
        # "Shell execution" under what-remains-open in docs/SECURITY_MIGRATION.md).
        # The file tools (WRITE_FILE/EDIT_FILE) *are* hard-confined.
        workspace = get_workspace()
        try:
            if state.config.ALLOW_SHELL_MODE:
                state.logger.debug("Running command in shell mode (cwd=%s): %s", workspace, command)
                with open(tmp_path, 'w') as f:
                    result = subprocess.run(command, shell=True, cwd=workspace, stdout=f, stderr=subprocess.STDOUT, text=True, errors='replace', timeout=state.config.COMMAND_TIMEOUT)
            else:
                command_args = parse_command_safely(command)
                command_args = expand_paths_in_args(command_args)
                state.logger.debug("Running command in safe mode (cwd=%s): %s", workspace, command_args)
                with open(tmp_path, 'w') as f:
                    result = subprocess.run(command_args, shell=False, cwd=workspace, stdout=f, stderr=subprocess.STDOUT, text=True, errors='replace', timeout=state.config.COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            state.logger.error("Command timed out: %s", command[:100])
            return f"Error: Command timed out after {state.config.COMMAND_TIMEOUT} seconds. Command: {command[:100]}..."
        except Exception as e:
            state.logger.error("Error running command subprocess: %s - %s", command[:100], str(e))
            return f"Error running command subprocess: {str(e)}. Command: {command[:100]}..."

        # Analyze output file
        try:
            with open(tmp_path, 'r', errors='replace') as f:
                content = f.read()
        except IOError as e:
            state.logger.error("Error reading command output: %s", str(e))
            return f"Error reading command output: {str(e)}"

        output_len = len(content)
        state.logger.info("Command executed successfully, output length: %d", output_len)

        # Use chunked processing if output is large and chunking is enabled
        if chunk_output and output_len > state.config.CHUNK_THRESHOLD:  # Same threshold as original
            state.logger.debug("Using chunked output for command with %d characters", output_len)
            final_output = get_chunk_for_display(content, chunk_size=state.config.CHUNK_SIZE, overlap=state.config.CHUNK_OVERLAP, log_path=tmp_path)

            # Store chunk info in a way that can be accessed later if needed
            chunk_info_path = tmp_path + '.chunks'
            state.temp_tracker['add'](chunk_info_path)
            chunk_result = chunk_large_output(content, chunk_size=state.config.CHUNK_SIZE, overlap=state.config.CHUNK_OVERLAP)

            # Save chunk metadata for potential later retrieval
            try:
                with open(chunk_info_path, 'w') as f:
                    json.dump({
                        'total_chunks': chunk_result['total_chunks'],
                        'original_length': chunk_result['original_length'],
                        'chunk_size': chunk_result['chunk_size'],
                        'file_path': tmp_path
                    }, f)
            except IOError as e:
                state.print_colored(f"Warning: Could not save chunk metadata: {str(e)}", state.colors['WARNING'])
                state.logger.warning("Could not save chunk metadata: %s", str(e))

            return final_output

        # If it fits in one go, just return it
        state.logger.debug("Returning command output directly, length: %d", len(content) if content else 0)
        return content if content else "(empty output)"

    except Exception as e:
        state.logger.error("Error executing command: %s", str(e), exc_info=True)
        return f"Error executing command: {str(e)}"
