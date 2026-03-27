"""Agent theme management — fetching available models from the server."""
import requests

from qwen_client.config import config, COLORS, THEME_STYLES, print_colored

# Populated at runtime from the server (or fallback defaults)
AGENT_THEMES = {}


def fetch_available_models():
    """Fetch available models from the server and populate AGENT_THEMES."""
    global AGENT_THEMES
    try:
        response = requests.get(config.MODELS_URL, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            data = response.json().get("data", [])
            AGENT_THEMES.clear()
            for model in data:
                mid = model["id"]
                style = THEME_STYLES.get(mid, THEME_STYLES["default"])
                AGENT_THEMES[mid] = {
                    "color": style["color"],
                    "icon": style["icon"],
                    "prompt": style["prompt"],
                    "desc": model["description"],
                }
        else:
            print_colored(f"Failed to fetch models: {response.status_code}", COLORS['FAIL'])
            _load_fallback_themes()
    except Exception:
        _load_fallback_themes()


def _load_fallback_themes():
    """Load default themes if server is unreachable."""
    global AGENT_THEMES
    AGENT_THEMES.clear()
    defaults = {
        "implementer": "Code Agent (Offline)",
        "fast_implementer": "Fast Code Agent (Offline)",
        "architect": "System Design (Offline)",
        "reviewer": "Code Review (Offline)",
        "debugger": "Debugging (Offline)",
        "metal_implementer": "Metal & Graphics (Offline)",
        "m25_implementer": "MiniMax M2.5 Implementer (Offline)",
        "m25_architect": "MiniMax M2.5 Architect (Offline)",
    }
    for mid, desc in defaults.items():
        style = THEME_STYLES.get(mid, THEME_STYLES["default"])
        AGENT_THEMES[mid] = {
            "color": style["color"],
            "icon": style["icon"],
            "prompt": style["prompt"],
            "desc": desc,
        }
