"""Slash-command dispatcher for the interactive CLI."""
import os
import json

from coding_model_client.config import config, COLORS, HISTORY_CHAR_BUDGET, print_colored
from coding_model_client.display import set_terminal_title
from coding_model_client.models import AGENT_THEMES
from coding_model_client.history import save_chat_history
from coding_model_client.readline_mgr import READLINE_AVAILABLE
from coding_model_client.services import (
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
        print_colored("\n--- Coding Model Remote CLI Help ---", COLORS['HEADER'])
        print_colored(f"{COLORS['BOLD']}GENERAL COMMANDS:{COLORS['ENDC']}", COLORS['BLUE'])
        print("  /help                - Show this help menu")
        print("  /exit, /quit         - Exit the CLI and cleanup resources")
        print("  /agent <name>        - Switch the active agent (e.g. /agent architect)")
        print("  /clear               - Clear conversation history and start fresh")
        print("  /resume              - Resume interrupted multi-agent tasks")
        print("  /history             - Show recent command history")
        print("  /history clear       - Clear command history")

        print_colored(f"\n{COLORS['BOLD']}SESSION & DISPLAY:{COLORS['ENDC']}", COLORS['BLUE'])
        print("  /permissions         - Cycle permission mode (default -> acceptEdits -> yolo)")
        print("  /workspace [dir]     - Show or set where the agent may write (default: a temp dir)")
        print("  /verbose             - Toggle verbose/compact tool output display")
        print("  /context             - Show context window usage (tokens, budget)")
        print("  /compact             - Manually compress conversation history")
        print("  /undo                - Revert the last file modification")
        print("  /rename <name>       - Rename the current session (migrates file)")
        print("  /review              - Fan out uncommitted git diff to 4 judges")
        print("                         (Claude + Gemini + Coding Model reviewer + deep_reviewer)")
        print("  /sessions            - List all saved sessions")
        print("  /session <name>      - Switch to a named session")
        print("  /session new <name>  - Create and switch to a new session")
        print("  \\ + Enter            - Multiline input (backslash continuation)")
        print("  Ctrl+C               - Interrupt generation (keeps partial response)")

        print_colored(f"\n{COLORS['BOLD']}DOCUMENTATION TOOLS:{COLORS['ENDC']}", COLORS['BLUE'])
        print("  /cupertino <query>   - Search local Apple documentation on macOS")
        print("                         Example: /cupertino MTLMeshRenderPipelineDescriptor")
        print("  /apple <tool> <args> - Search Apple Deep Docs on the Linux server")
        print("                         Example: /apple search_swift_evolution {\"feature\": \"actors\"}")
        print("  /ingest <path>       - Ingest a PDF into memory (supports server files or local: prefix for client files)")
        print("  /ingest-code <dir>   - Ingest a codebase directory with AST-aware chunking")
        print("                         Examples: /ingest /home/user/Metal4_Specs.pdf")
        print("                                   /ingest local:/Users/me/Reports/annual.pdf")
        print("  /scrape [framework]  - Run the documentation scraper (default: Metal)")
        print("                         Example: /scrape MetalFX")

        print_colored(f"\n{COLORS['BOLD']}AGENT SHORTCUTS:{COLORS['ENDC']}", COLORS['BLUE'])
        print("  @<agent_name> [msg]  - Switch agent and optionally send message in one go")
        print("                         Example: @architect Design a Metal 4 renderer")
        print("                         Example: @debugger Why is this kernel crashing?")
        print("  MULTI-AGENT:         - You can use multiple @ mentions in one prompt!")
        print("                         Example: @architect Design X then @implementer build it.")

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
        from coding_model_client.services import ingest_codebase
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
        # scraping/main.py has never existed — /scrape failed 100% of the
        # time (DEV-165). scrape_all_apple_frameworks.py is the real entry
        # point and takes optional framework names as argv.
        scrape_cmd = f"cd scraping && python3 scrape_all_apple_frameworks.py{framework_arg}"
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

    # ── Agent switch ──────────────────────────────────────────────────────
    if user_input.lower().startswith('/agent '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /agent <name>", COLORS['FAIL'])
            print_colored(f"Available agents: {', '.join(AGENT_THEMES.keys())}", COLORS['BLUE'])
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

    # ── Multi-judge review of uncommitted diff ────────────────────────────
    if user_input.lower() == '/review':
        from coding_model_client.review import run_review_command
        run_review_command()
        return True, model

    # ── Resume ────────────────────────────────────────────────────────────
    if user_input.lower() == '/resume':
        # Import here to avoid circular — orchestrator owns PENDING_TASKS
        from coding_model_client.orchestrator import PENDING_TASKS, _pending_tasks_lock
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
        _tool_handlers.set_permission_mode(config.PERMISSION_MODE)
        color = COLORS['FAIL'] if config.PERMISSION_MODE == 'yolo' else COLORS['GREEN']
        print_colored(f"Permission mode: {config.PERMISSION_MODE} — {labels[config.PERMISSION_MODE]}", color)
        return True, model

    # ── Workspace ─────────────────────────────────────────────────────────
    if user_input.lower() == '/workspace' or user_input.lower().startswith('/workspace '):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            current = _tool_handlers.get_workspace()
            print_colored(f"Workspace: {current}", COLORS['GREEN'])
            print_colored("  Relative paths resolve here; writes outside it are refused.", COLORS['CYAN'])
            print_colored("  Set with: /workspace <dir>", COLORS['CYAN'])
            return True, model

        ok, result = _tool_handlers.set_workspace(parts[1].strip())
        if ok:
            print_colored(f"Workspace: {result}", COLORS['GREEN'])
        else:
            print_colored(f"Workspace unchanged — {result}", COLORS['FAIL'])
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
        print_colored("\nContext Usage:", COLORS['HEADER'])
        print_colored(f"  Messages:  {msg_count}", COLORS['CYAN'])
        print_colored(f"  Chars:     {total_chars:,} / {budget:,}", COLORS['CYAN'])
        print_colored(f"  Est tokens: ~{est_tokens:,}", COLORS['CYAN'])
        print_colored(f"  Budget:    {bar} {pct:.1f}%", color)
        return True, model

    # ── Manual compaction ─────────────────────────────────────────────────
    if user_input.lower() == '/compact':
        from coding_model_client.compaction import compact_conversation
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
        from coding_model_client.history import session_path
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
        set_terminal_title(f"Coding Model - {new_name}")
        save_chat_history(history, model)
        print_colored(f"Session renamed to: {new_name} ({new_file})", COLORS['GREEN'])
        return True, model

    # ── Session listing ───────────────────────────────────────────────────
    if user_input.lower() == '/sessions':
        from coding_model_client.history import list_sessions
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
        from coding_model_client.history import session_path, load_chat_history_from_file
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
            set_terminal_title(f"Coding Model - {name}")
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
        set_terminal_title(f"Coding Model - {name}")
        print_colored(f"Switched to session: {name} ({len(history)} messages, agent: {model})", COLORS['GREEN'])
        return True, model

    # ── Not a command — fall through to normal processing ─────────────────
    return False, model
