"""Main interactive chat loop and entry point."""
import re
import os
import sys
import time
import argparse
import atexit

from coding_model_client.config import config, COLORS, print_colored
from coding_model_client.display import set_terminal_title
from coding_model_client.models import AGENT_THEMES, fetch_available_models
from coding_model_client.history import save_chat_history, load_chat_history, migrate_legacy_sessions, session_path
from coding_model_client.readline_mgr import READLINE_AVAILABLE, setup_readline, add_to_history
from coding_model_client.temp_manager import _add_temp_file
from coding_model_client.services import (
    save_memory, web_search, ingest_pdf, ingest_url_content,
    handle_cupertino_search, handle_apple_deep_docs,
)
from coding_model_client.commands import handle_user_command, set_tool_handlers
from coding_model_client.orchestrator import (
    process_agent_tasks, PENDING_TASKS, _pending_tasks_lock,
    set_tool_functions,
)

# Rich terminal rendering (optional)
try:
    from rich.console import Console
    RICH_AVAILABLE = True
    rich_console = Console()
except ImportError:
    RICH_AVAILABLE = False
    rich_console = None

# Logging (already configured in config.py, but grab the logger)
import logging
logger = logging.getLogger("coding_model_client")


# ---------------------------------------------------------------------------
# tool_handlers configuration bridge
# ---------------------------------------------------------------------------

def _configure_tool_handlers():
    """Wire up tool_handlers.py with all its dependencies."""
    from coding_model_server import tool_handlers

    tool_handlers.configure(
        config=config,
        colors=COLORS,
        print_colored_fn=print_colored,
        logger_inst=logger,
        temp_tracker={
            'add': _add_temp_file,
        },
        external_handlers={
            'save_memory': save_memory,
            'web_search': web_search,
            'handle_cupertino_search': handle_cupertino_search,
            'handle_apple_deep_docs': handle_apple_deep_docs,
            'ingest_pdf': ingest_pdf,
            'ingest_url_content': ingest_url_content,
        },
        rich_console=rich_console if RICH_AVAILABLE else None,
        permission_mode=config.PERMISSION_MODE,
        verbose_mode=config.VERBOSE_MODE,
    )

    # Give commands.py a handle on tool_handlers so it can call
    # set_permission_mode, set_verbose_mode, undo_last_checkpoint, etc.
    set_tool_handlers(tool_handlers)

    # Give orchestrator.py the three tool functions it needs
    set_tool_functions(
        tool_handlers.process_remote_commands,
        tool_handlers.extract_fallback_commands,
        tool_handlers.execute_remote_command,
    )


# ---------------------------------------------------------------------------
# Main chat loop
# ---------------------------------------------------------------------------

def chat(model="implementer", session_name=None):
    """Main interactive chat loop."""
    migrate_legacy_sessions()

    if session_name:
        config.SESSION_NAME = session_name
        config.CHAT_HISTORY_FILE = session_path(session_name)

    setup_readline()
    _configure_tool_handlers()
    fetch_available_models()

    print_colored(f"\nCoding Model Remote CLI (Connected to {config.LINUX_SERVER_IP})", COLORS['HEADER'])
    title = f"Coding Model - {config.SESSION_NAME}" if config.SESSION_NAME else "Coding Model - Idle"
    set_terminal_title(title)
    if config.SESSION_NAME:
        print_colored(f"Session: {config.SESSION_NAME}", COLORS['CYAN'])

    # Resolve initial agent
    if model not in AGENT_THEMES:
        if "implementer" in AGENT_THEMES:
            model = "implementer"
        elif AGENT_THEMES:
            model = list(AGENT_THEMES.keys())[0]
        else:
            AGENT_THEMES["default"] = {
                "color": COLORS['WARNING'],
                "icon": "\u2753",
                "prompt": "Agent",
                "desc": "Unknown Agent",
            }
            model = "default"

    agent_theme = AGENT_THEMES[model]
    print_colored(f"Agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
    print_colored(f"({agent_theme['desc']})", COLORS['BLUE'])

    perm_labels = {
        'default': 'Prompt for all operations',
        'acceptEdits': 'Auto-approve file ops, prompt for shell',
        'yolo': 'Auto-approve ALL (dangerous!)',
    }
    perm_color = COLORS['FAIL'] if config.PERMISSION_MODE == 'yolo' else COLORS['GREEN']
    print_colored(
        f"\nPermissions: {config.PERMISSION_MODE} — {perm_labels.get(config.PERMISSION_MODE, '?')}",
        perm_color
    )
    if config.ALLOW_SHELL_MODE:
        print_colored("  Shell mode: ENABLED", COLORS['WARNING'])
    print_colored(
        f"  Display: {'verbose' if config.VERBOSE_MODE else 'compact'} | Multiline: \\ + Enter",
        COLORS['BLUE']
    )

    print_colored(
        "\nCommands: /help /exit /agent /clear /resume /permissions /verbose /context /compact /undo /rename /review",
        COLORS['BLUE']
    )
    if READLINE_AVAILABLE:
        print_colored("Use arrow keys for history. Ctrl+C to interrupt generation.\n", COLORS['BLUE'])
    else:
        print_colored("(Install readline for command history support)\n", COLORS['WARNING'])

    history, loaded_agent = load_chat_history()

    if history and loaded_agent and loaded_agent in AGENT_THEMES:
        model = loaded_agent
        agent_theme = AGENT_THEMES[model]
        print_colored(f"Resuming with agent: {model}", COLORS['GREEN'])

        if history:
            print_colored("\n--- Previous Context ---", COLORS['HEADER'])
            start_idx = max(0, len(history) - 4)
            for msg in history[start_idx:]:
                role = msg["role"].capitalize()
                content = msg["content"]
                display_content = content[:200] + "..." if len(content) > 200 else content
                color = COLORS['CYAN'] if msg["role"] == "user" else COLORS['GREEN']
                print(f"{color}{role}: {display_content}{COLORS['ENDC']}")
            print_colored("------------------------\n", COLORS['HEADER'])

    # ── Input loop ────────────────────────────────────────────────────────
    while True:
        try:
            raw_color = agent_theme['color']
            prompt_text = f"{agent_theme['prompt']} {agent_theme['icon']} > "
            full_prompt = f"\001{raw_color}\002{prompt_text}\001{COLORS['ENDC']}\002"

            try:
                user_input = input(full_prompt)
                while user_input.endswith('\\'):
                    user_input = user_input[:-1] + '\n'
                    try:
                        user_input += input('... ')
                    except EOFError:
                        break
                if user_input.strip():
                    add_to_history(user_input)
            except EOFError:
                break

            if not user_input.strip():
                continue

            if user_input.lower() in ['/exit', '/quit']:
                break

            should_continue, new_model = handle_user_command(user_input, history, model, agent_theme)
            if new_model != model:
                model = new_model
                agent_theme = AGENT_THEMES[model]

            if should_continue:
                continue

            # Resume interrupted chain
            if user_input.lower() == '/resume':
                with _pending_tasks_lock:
                    print_colored(f"Resuming {len(PENDING_TASKS)} pending tasks...", COLORS['GREEN'])
                    tasks = list(PENDING_TASKS)
                    PENDING_TASKS.clear()
            else:
                # Normal multi-agent orchestration
                try:
                    segments = re.split(r'(@\w+)', user_input)
                    tasks = []
                    current_task_agent = model

                    first_segment = segments[0].strip()
                    if first_segment:
                        tasks.append((current_task_agent, first_segment))

                    for i in range(1, len(segments), 2):
                        agent_name = segments[i][1:].lower()
                        message_content = segments[i + 1].strip() if i + 1 < len(segments) else ""
                        if agent_name in AGENT_THEMES:
                            tasks.append((agent_name, message_content))
                        else:
                            if tasks:
                                tasks[-1] = (tasks[-1][0], tasks[-1][1] + " " + segments[i] + message_content)
                            else:
                                tasks.append((current_task_agent, segments[i] + message_content))
                except Exception as e:
                    print_colored(f"Error parsing tasks: {e}", COLORS['FAIL'])
                    tasks = []

            model = process_agent_tasks(tasks, history, model, agent_theme)
            if model in AGENT_THEMES:
                agent_theme = AGENT_THEMES[model]

        except KeyboardInterrupt:
            print_colored("\n(Press Ctrl+C again to exit, or type /exit)", COLORS['WARNING'])
            try:
                time.sleep(0.3)
            except KeyboardInterrupt:
                break
            continue

    print_colored("\nGoodbye!", COLORS['HEADER'])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Coding Model Remote Client")
    parser.add_argument("--model", type=str, default="implementer", help="Agent model to use")
    parser.add_argument("--name", type=str, default=None, help="Name this session")
    args = parser.parse_args()

    try:
        chat(args.model, session_name=args.name)
    except Exception as e:
        print_colored(f"\nCRITICAL CLIENT ERROR: {e}", COLORS['FAIL'])
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
