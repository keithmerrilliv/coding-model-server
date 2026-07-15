"""Agent theme management — fetching available models from the server."""
import requests

from coding_model_client.config import config, COLORS, THEME_STYLES, print_colored

# Populated at runtime from the server (or fallback defaults)
AGENT_THEMES = {}


def fetch_available_models():
    """Fetch available models from the server and populate AGENT_THEMES."""
    global AGENT_THEMES
    try:
        response = requests.get(config.MODELS_URL, headers=config.auth_headers, timeout=config.REQUEST_TIMEOUT)
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
        "implementer": "Implementer — Qwen3.5-35B (offline)",
        "deep_implementer": "Implementer — Coder-Next Deep (offline)",
        "fast_implementer": "Implementer — Coder-30B Fast (offline)",
        "architect": "Architect — Qwen3.6-27B (offline)",
        "reviewer": "Reviewer — Coder-30B HD (offline)",
        "debugger": "Debugger — Coder-30B Turbo (offline)",
        "moe_implementer": "Implementer — MiniMax M2.5 (offline)",
        "moe_architect": "Architect — MiniMax M2.5 (offline)",
        "dense_architect": "Architect — Qwen3.6-27B (offline)",
        "brainstorm": "Brainstorm — Nemotron-3-Nano (offline)",
        "native_implementer": "Implementer — GLM-4.7-Flash (offline)",
    }
    for mid, desc in defaults.items():
        style = THEME_STYLES.get(mid, THEME_STYLES["default"])
        AGENT_THEMES[mid] = {
            "color": style["color"],
            "icon": style["icon"],
            "prompt": style["prompt"],
            "desc": desc,
        }
