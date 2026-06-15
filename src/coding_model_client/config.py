"""Configuration, constants, and shared utilities."""
import os
import logging

# ---------------------------------------------------------------------------
# Configuration class
# ---------------------------------------------------------------------------

class Config:
    LINUX_SERVER_IP = os.getenv("CODING_MODEL_SERVER_IP", "192.168.50.101")
    API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/chat/completions"
    MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"
    INGEST_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory/ingest"
    SEARCH_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/search"
    DEEP_DOCS_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/apple_deep_docs"
    HEALTH_URL = f"http://{LINUX_SERVER_IP}:5000/health"
    MODELS_URL = f"http://{LINUX_SERVER_IP}:5000/v1/models"
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

    @property
    def auth_headers(self):
        """Headers dict with X-Admin-Key if configured."""
        if self.ADMIN_API_KEY:
            return {"X-Admin-Key": self.ADMIN_API_KEY}
        return {}

    # Command execution security settings
    ALLOW_SHELL_MODE = os.getenv('ALLOW_SHELL_MODE', 'false').lower() == 'true'
    COMMAND_WHITELIST = os.getenv('COMMAND_WHITELIST', '').split(',') if os.getenv('COMMAND_WHITELIST') else None

    # Permission mode: 'default' (prompt all), 'acceptEdits' (auto-approve file ops), 'yolo' (auto-approve all)
    _explicit_perm = os.getenv('PERMISSION_MODE')
    if _explicit_perm:
        PERMISSION_MODE = _explicit_perm
    elif os.getenv('ALLOW_ALL', 'false').lower() == 'true':
        PERMISSION_MODE = 'yolo'
    else:
        PERMISSION_MODE = 'default'

    # Display settings
    VERBOSE_MODE = True  # True=full tool output, False=compact one-liners

    # Session settings
    SESSION_NAME = None
    SESSION_DIR = os.path.expanduser("~/.coding_model_sessions")

    # Temporary file settings
    HISTORY_FILE = os.path.expanduser("~/.coding_model_client_history")
    CHAT_HISTORY_FILE = os.path.join(SESSION_DIR, "default.json")
    HISTORY_MAX_LENGTH = 1000

    # Chunking settings
    CHUNK_SIZE = 6000
    CHUNK_OVERLAP = 500
    CHUNK_THRESHOLD = 8000

    # Timeout settings
    COMMAND_TIMEOUT = 240
    REQUEST_TIMEOUT = 30
    LONG_REQUEST_TIMEOUT = 60


config = Config()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.coding_model_client.log")),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

COLORS = {
    "HEADER": "\033[95m",
    "BLUE": "\033[94m",
    "GREEN": "\033[92m",
    "WARNING": "\033[93m",
    "FAIL": "\033[91m",
    "ENDC": "\033[0m",
    "BOLD": "\033[1m",
    "CYAN": "\033[96m",
}

# Colors with readline non-printing escape codes (ONLY for input prompts)
PROMPT_COLORS = {k: f"\001{v}\002" for k, v in COLORS.items()}

# ---------------------------------------------------------------------------
# Theme definitions (visuals only — agent availability comes from server)
# ---------------------------------------------------------------------------

THEME_STYLES = {
    "implementer":       {"color": COLORS['GREEN'],   "icon": "\U0001f4bb", "prompt": "Implementer"},
    "deep_implementer":  {"color": COLORS['GREEN'],   "icon": "\U0001f9e0", "prompt": "Deep Impl"},
    "fast_implementer":  {"color": COLORS['GREEN'],   "icon": "\u23e9",     "prompt": "Fast Impl"},
    "architect":         {"color": COLORS['HEADER'],  "icon": "\U0001f3d7\ufe0f", "prompt": "Architect"},
    "reviewer":          {"color": COLORS['CYAN'],    "icon": "\U0001f50d", "prompt": "Reviewer"},
    "debugger":          {"color": COLORS['FAIL'],    "icon": "\U0001f41e", "prompt": "Debugger"},
    "moe_implementer":   {"color": COLORS['GREEN'],   "icon": "\U0001f319", "prompt": "MoE Impl"},
    "moe_architect":     {"color": COLORS['HEADER'],  "icon": "\U0001f319", "prompt": "MoE Arch"},
    "dense_architect":     {"color": COLORS['HEADER'],  "icon": "\U0001f3af", "prompt": "Dense Arch"},
    "brainstorm":          {"color": COLORS['WARNING'], "icon": "\u26a1",     "prompt": "Brainstorm"},
    "native_implementer":               {"color": COLORS['GREEN'],   "icon": "\U0001f30a", "prompt": "Native"},
    "default":           {"color": COLORS['WARNING'], "icon": "\U0001f916", "prompt": "Agent"},
}

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

HISTORY_CHAR_BUDGET = 150000  # ~37.5k tokens, leaves room for system prompt + response

# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def print_colored(text, color):
    print(f"{color}{text}{COLORS['ENDC']}")
