"""Slash-command dispatcher for the interactive CLI."""
import os
import json

from qwen_client.config import config, COLORS, PROMPT_COLORS, HISTORY_CHAR_BUDGET, print_colored
from qwen_client.display import set_terminal_title
from qwen_client.models import AGENT_THEMES
from qwen_client.history import save_chat_history
from qwen_client.readline_mgr import READLINE_AVAILABLE
from qwen_client.services import (
    handle_cupertino_search,
    apple_deep_docs_search,
)

# These are imported lazily from tool_handlers in main.py's configure() bridge.
# We store references here so commands.py can call them without circular imports.
_tool_handlers = None


def set_tool_handlers(th_module):
    """Called once from main.py after tool_handlers.configure()."""
    global _tool_handlers
    _tool_handlers = th_module


def handle_user_command(user_input, history, model, agent_theme):
    """Handle special slash commands. Returns (should_continue, updated_model)."""

    # ── Help ──────────────────────────────────────────────────────────────
    if user_input.lower() == '/help':
        print_colored("\n--- Qwen Remote CLI Help ---", COLORS['HEADER'])
        print_colored(f"{COLORS['BOLD']}GENERAL COMMANDS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /help                - Show this help menu")
        print(f"  /exit, /quit         - Exit the CLI and cleanup resources")
        print(f"  /model <name>        - Switch the active agent (e.g. /model architect)")
        print(f"  /clear               - Clear conversation history and start fresh")
        print(f"  /resume              - Resume interrupted multi-agent tasks")
        print(f"  /history             - Show recent command history")
        print(f"  /history clear       - Clear command history")

        print_colored(f"\n{COLORS['BOLD']}SESSION & DISPLAY:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /permissions         - Cycle permission mode (default -> acceptEdits -> yolo)")
        print(f"  /verbose             - Toggle verbose/compact tool output display")
        print(f"  /context             - Show context window usage (tokens, budget)")
        print(f"  /compact             - Manually compress conversation history")
        print(f"  /undo                - Revert the last file modification")
        print(f"  /rename <name>       - Rename the current session (migrates file)")
        print(f"  /sessions            - List all saved sessions")
        print(f"  /session <name>      - Switch to a named session")
        print(f"  /session new <name>  - Create and switch to a new session")
        print(f"  \\ + Enter            - Multiline input (backslash continuation)")
        print(f"  Ctrl+C               - Interrupt generation (keeps partial response)")

        print_colored(f"\n{COLORS['BOLD']}DOCUMENTATION TOOLS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /cupertino <query>   - Search local Apple documentation on macOS")
        print(f"                         Example: /cupertino MTLMeshRenderPipelineDescriptor")
        print(f"  /apple <tool> <args> - Search Apple Deep Docs on the Linux server")
        print(f"                         Example: /apple search_swift_evolution {{\"feature\": \"actors\"}}")
        print(f"  /ingest <path>       - Ingest a PDF into memory (supports server files or local: prefix for client files)")
        print(f"  /ingest-code <dir>   - Ingest a codebase directory with AST-aware chunking")
        print(f"                         Examples: /ingest /home/user/Metal4_Specs.pdf")
        print(f"                                   /ingest local:/Users/me/Reports/annual.pdf")
        print(f"  /scrape [framework]  - Run the documentation scraper (default: Metal)")
        print(f"                         Example: /scrape MetalFX")

        print_colored(f"\n{COLORS['BOLD']}AGENT SHORTCUTS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  @<agent_name> [msg]  - Switch agent and optionally send message in one go")
        print(f"                         Example: @architect Design a Metal 4 renderer")
        print(f"                         Example: @debugger Why is this kernel crashing?")
        print(f"  MULTI-AGENT:         - You can use multiple @ mentions in one prompt!")
        print(f"                         Example: @architect Design X then @implementer build it.")

        print_colored(f"\n{COLORS['BOLD']}AVAILABLE AGENTS:{COLORS['ENDC']}", COLORS['BLUE'])
        # Group agents by role parsed from description ("<Role> — <details>").
        # Preserve insertion order both across groups and within each group.
        groups = {}
        for name, theme in AGENT_THEMES.items():
            desc = theme['desc']
            role, _, detail = desc.partition(' — ')
            if not detail:  # description without em-dash → ungrouped
                role, detail = 'Other', desc
            groups.setdefault(role, []).append((name, detail))
        for role, agents in groups.items():
            print_colored(f"  {role}:", COLORS['CYAN'])
            for name, detail in agents:
                print(f"    {name.ljust(18)} - {detail}")
        print_colored("----------------------------\n", COLORS['HEADER'])
        return True, model

    # ── Ingest codebase ────────────────────────────────────────────────────
    if user_input.lower().startswith('/ingest-code '):
        from qwen_client.services import ingest_codebase
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /ingest-code <directory>", COLORS['FAIL'])
            print_colored("  Ingests code files with AST-aware chunking into RAG memory.", COLORS['BLUE'])
            return True, model
        directory = parts[1].strip()
        result = ingest_codebase(directory)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # ── Ingest PDF ────────────────────────────────────────────────────────
    if user_input.lower().startswith('/ingest '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /ingest <path> (path can be on server or local: prefix for client files)", COLORS['FAIL'])
            print_colored("  Examples:", COLORS['BLUE'])
            print_colored("    /ingest /path/on/server.pdf", COLORS['BLUE'])
            print_colored("    /ingest local:/path/on/client.pdf", COLORS['BLUE'])
            return True, model
        path = parts[1].strip()
        result = _tool_handlers.ingest_pdf_content(path)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # ── Scrape documentation ──────────────────────────────────────────────
    if user_input.lower().startswith('/scrape'):
        parts = user_input.split(' ', 1)
        framework_arg = ""
        if len(parts) > 1 and parts[1].strip():
            framework_arg = f" {parts[1].strip()}"
            print_colored(f"Starting documentation scraper for '{parts[1].strip()}' on the server...", COLORS['CYAN'])
        else:
            print_colored("Starting Metal documentation scraper on the server...", COLORS['CYAN'])
        scrape_cmd = f"cd scraping && python3 main.py{framework_arg}"
        result = _tool_handlers.execute_remote_command(scrape_cmd)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # ── Quick agent switch (@mention) ─────────────────────────────────────
    if user_input.startswith('@'):
        parts = user_input.split(' ', 1)
        potential_agent = parts[0][1:].lower()
        if potential_agent in AGENT_THEMES:
            model = potential_agent
            agent_theme = AGENT_THEMES[model]
            print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
            print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])
            if len(parts) > 1:
                return False, model
            else:
                return True, model
        else:
            print_colored(f"Unknown agent '{potential_agent}'. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
            print_colored("Treating as normal text...", COLORS['BLUE'])
            return False, model

    # ── Cupertino Apple docs ──────────────────────────────────────────────
    if user_input.lower().startswith('/cupertino '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /cupertino <query>", COLORS['FAIL'])
            return True, model
        query = parts[1].strip()
        result = handle_cupertino_search(query)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # ── Apple Deep Docs ───────────────────────────────────────────────────
    if user_input.lower().startswith('/apple '):
        parts = user_input.split(' ', 2)
        if len(parts) < 2:
            print_colored("Usage: /apple <tool_name> [args_json]", COLORS['FAIL'])
            print_colored("Example: /apple search_swift_evolution {\"feature\": \"actors\"}", COLORS['BLUE'])
            return True, model
        tool = parts[1]
        args_str = parts[2].strip() if len(parts) > 2 else "{}"
        if not args_str:
            args_str = "{}"
        try:
            args = json.loads(args_str)
            if not isinstance(args, dict):
                print_colored("Error: Arguments must be a JSON object (dictionary).", COLORS['FAIL'])
                print_colored("Example: /apple tool {\"key\": \"value\"}", COLORS['BLUE'])
                return True, model
            result = apple_deep_docs_search(tool, args)
            print_colored(f"\n{result}\n", COLORS['GREEN'])
        except json.JSONDecodeError as e:
            print_colored(f"Error: Invalid JSON arguments: {e}", COLORS['FAIL'])
            print_colored("Hint: Ensure keys and values are in double quotes.", COLORS['BLUE'])
            print_colored("Example: /apple tool {\"query\": \"something\"}", COLORS['CYAN'])
        return True, model

    # ── History ───────────────────────────────────────────────────────────
    if user_input.lower() == '/history':
        if READLINE_AVAILABLE:
            import readline
            history_len = readline.get_current_history_length()
            print_colored(f"\nCommand History ({history_len} entries):", COLORS['HEADER'])
            for i in range(1, min(history_len + 1, 21)):
                idx = max(1, history_len - 20 + i)
                if idx <= history_len:
                    item = readline.get_history_item(idx)
                    print_colored(f"  {idx}: {item}", COLORS['CYAN'])
            if history_len > 20:
                print_colored(f"  ... ({history_len - 20} older entries)", COLORS['BLUE'])
        else:
            if history:
                recent = history[-20:]
                print_colored(f"\nChat History ({len(history)} messages, showing last {len(recent)}):", COLORS['HEADER'])
                for msg in recent:
                    role = msg["role"].capitalize()
                    content = msg["content"][:120]
                    color = COLORS['CYAN'] if msg["role"] == "user" else COLORS['GREEN']
                    print_colored(f"  {role}: {content}", color)
            else:
                print_colored("No chat history available.", COLORS['WARNING'])
        return True, model

    if user_input.lower() == '/history clear':
        if READLINE_AVAILABLE:
            import readline
            readline.clear_history()
            print_colored("Command history cleared.", COLORS['GREEN'])
        else:
            print_colored("Readline not available - cannot clear command history", COLORS['WARNING'])
        return True, model

    # ── Model switch ──────────────────────────────────────────────────────
    if user_input.lower().startswith('/model '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /model <name>", COLORS['FAIL'])
            print_colored(f"Available models: {', '.join(AGENT_THEMES.keys())}", COLORS['BLUE'])
            return True, model
        requested_model = parts[1].strip().lower()
        if requested_model in AGENT_THEMES:
            model = requested_model
            agent_theme = AGENT_THEMES[model]
            print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
            print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])
        else:
            print_colored(f"Unknown agent '{requested_model}'. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
        return True, model

    # ── Clear ─────────────────────────────────────────────────────────────
    if user_input.lower() == '/clear':
        history.clear()
        save_chat_history(history, model)
        print_colored("Conversation history cleared. Starting fresh.", COLORS['GREEN'])
        return True, model

    # ── Resume ────────────────────────────────────────────────────────────
    if user_input.lower() == '/resume':
        # Import here to avoid circular — orchestrator owns PENDING_TASKS
        from qwen_client.orchestrator import PENDING_TASKS, _pending_tasks_lock
        with _pending_tasks_lock:
            if not PENDING_TASKS:
                print_colored("No interrupted tasks to resume.", COLORS['WARNING'])
                return True, model
        return False, model

    # ── Permissions ───────────────────────────────────────────────────────
    if user_input.lower() == '/permissions':
        modes = ['default', 'acceptEdits', 'yolo']
        labels = {
            'default': 'Prompt for all operations',
            'acceptEdits': 'Auto-approve file ops, prompt for shell commands',
            'yolo': 'Auto-approve everything (dangerous!)',
        }
        current_idx = modes.index(config.PERMISSION_MODE) if config.PERMISSION_MODE in modes else 0
        config.PERMISSION_MODE = modes[(current_idx + 1) % len(modes)]
        config.ALLOW_ALL = config.PERMISSION_MODE == 'yolo'
        _tool_handlers.set_permission_mode(config.PERMISSION_MODE)
        color = COLORS['FAIL'] if config.PERMISSION_MODE == 'yolo' else COLORS['GREEN']
        print_colored(f"Permission mode: {config.PERMISSION_MODE} — {labels[config.PERMISSION_MODE]}", color)
        return True, model

    # ── Verbose toggle ────────────────────────────────────────────────────
    if user_input.lower() == '/verbose':
        config.VERBOSE_MODE = not config.VERBOSE_MODE
        _tool_handlers.set_verbose_mode(config.VERBOSE_MODE)
        mode_str = "verbose (full tool output)" if config.VERBOSE_MODE else "compact (one-line summaries)"
        print_colored(f"Display mode: {mode_str}", COLORS['GREEN'])
        return True, model

    # ── Context usage ─────────────────────────────────────────────────────
    if user_input.lower() == '/context':
        total_chars = sum(len(m.get("content", "")) for m in history)
        est_tokens = total_chars // 4
        msg_count = len(history)
        budget = HISTORY_CHAR_BUDGET
        pct = (total_chars / budget) * 100 if budget > 0 else 0
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = f"[{'#' * filled}{'.' * (bar_len - filled)}]"
        color = COLORS['FAIL'] if pct > 80 else COLORS['WARNING'] if pct > 50 else COLORS['GREEN']
        print_colored(f"\nContext Usage:", COLORS['HEADER'])
        print_colored(f"  Messages:  {msg_count}", COLORS['CYAN'])
        print_colored(f"  Chars:     {total_chars:,} / {budget:,}", COLORS['CYAN'])
        print_colored(f"  Est tokens: ~{est_tokens:,}", COLORS['CYAN'])
        print_colored(f"  Budget:    {bar} {pct:.1f}%", color)
        return True, model

    # ── Manual compaction ─────────────────────────────────────────────────
    if user_input.lower() == '/compact':
        from qwen_client.compaction import compact_conversation
        before_chars = sum(len(m.get("content", "")) for m in history)
        success, message = compact_conversation(history, model, agent_theme, reason="manual")
        if success:
            after_chars = sum(len(m.get("content", "")) for m in history)
            freed = before_chars - after_chars
            print_colored(f"Compacted: {message} Freed ~{freed // 4:,} tokens.", COLORS['GREEN'])
            save_chat_history(history, model)
        else:
            print_colored(message, COLORS['WARNING'])
        return True, model

    # ── Undo ──────────────────────────────────────────────────────────────
    if user_input.lower() == '/undo':
        result = _tool_handlers.undo_last_checkpoint()
        color = COLORS['GREEN'] if 'Restored' in result else COLORS['WARNING']
        print_colored(result, color)
        return True, model

    # ── Session rename ────────────────────────────────────────────────────
    if user_input.lower().startswith('/rename '):
        from qwen_client.history import session_path
        new_name = user_input.split(' ', 1)[1].strip()
        if not new_name:
            print_colored("Usage: /rename <session_name>", COLORS['FAIL'])
            return True, model
        old_file = config.CHAT_HISTORY_FILE
        config.SESSION_NAME = new_name
        new_file = session_path(new_name)
        config.CHAT_HISTORY_FILE = new_file
        if os.path.exists(old_file) and old_file != new_file:
            import shutil
            try:
                shutil.move(old_file, new_file)
            except Exception as e:
                print_colored(f"Warning: Could not migrate session file: {e}", COLORS['WARNING'])
        set_terminal_title(f"Qwen - {new_name}")
        save_chat_history(history, model)
        print_colored(f"Session renamed to: {new_name} ({new_file})", COLORS['GREEN'])
        return True, model

    # ── Session listing ───────────────────────────────────────────────────
    if user_input.lower() == '/sessions':
        from qwen_client.history import list_sessions
        sessions = list_sessions()
        if not sessions:
            print_colored("No saved sessions found.", COLORS['WARNING'])
        else:
            print_colored(f"\n--- Saved Sessions ({len(sessions)}) ---", COLORS['HEADER'])
            for s in sessions:
                current = config.SESSION_NAME or "default"
                marker = " <-- current" if s['name'] == current else ""
                ts = s['timestamp'][:19] if len(s['timestamp']) > 19 else s['timestamp']
                print_colored(
                    f"  {s['name']:<20} {s['messages']:>3} msgs  {s['last_agent']:<16} {ts}{marker}",
                    COLORS['CYAN']
                )
            print_colored("Use '/session <name>' to switch, '/session new <name>' to create.", COLORS['BLUE'])
        return True, model

    # ── Session switching ─────────────────────────────────────────────────
    if user_input.lower().startswith('/session '):
        from qwen_client.history import session_path, load_chat_history_from_file
        parts = user_input.split(None, 2)
        action = parts[1].strip() if len(parts) > 1 else ""

        if action == 'new':
            name = parts[2].strip() if len(parts) > 2 else None
            if not name:
                print_colored("Usage: /session new <name>", COLORS['FAIL'])
                return True, model
            save_chat_history(history, model)
            config.SESSION_NAME = name
            config.CHAT_HISTORY_FILE = session_path(name)
            history.clear()
            save_chat_history(history, model)
            set_terminal_title(f"Qwen - {name}")
            print_colored(f"Created and switched to new session: {name}", COLORS['GREEN'])
            return True, model

        # Switch to existing session
        name = action
        target_file = session_path(name)
        if not os.path.exists(target_file):
            print_colored(f"Session '{name}' not found. Use '/session new {name}' to create.", COLORS['FAIL'])
            return True, model
        save_chat_history(history, model)
        config.SESSION_NAME = name
        config.CHAT_HISTORY_FILE = target_file
        loaded_history, loaded_agent = load_chat_history_from_file(target_file)
        history[:] = loaded_history
        model = loaded_agent
        set_terminal_title(f"Qwen - {name}")
        print_colored(f"Switched to session: {name} ({len(history)} messages, agent: {model})", COLORS['GREEN'])
        return True, model

    # ── Not a command — fall through to normal processing ─────────────────
    return False, model
