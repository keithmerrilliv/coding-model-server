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
    """Load default themes if server is unreachable.

    Derived from THEME_STYLES keys (DEV-154): this used to be a THIRD
    hand-maintained agent list, and it had already drifted — deep_reviewer
    was missing entirely and the model names it described were a generation
    stale. Offline, only the visual identity matters; real descriptions
    come from /v1/models the moment the server is reachable.
    """
    global AGENT_THEMES
    AGENT_THEMES.clear()
    for mid, style in THEME_STYLES.items():
        if mid == "default":
            continue
        AGENT_THEMES[mid] = {
            "color": style["color"],
            "icon": style["icon"],
            "prompt": style["prompt"],
            "desc": f"{style['prompt']} (offline — server unreachable)",
        }


def resolve_agent(name):
    """Map a legacy alias to its canonical agent name (Config.AGENT_ALIASES).

    /v1/models lists only canonical names, so AGENT_THEMES never contains
    aliases. The server resolves aliases for request.model, but the client
    validated the raw name — so `@architect` and `--model architect` were
    rejected and fell back to implementer, contradicting the documented aliases.
    Resolve here so they work interactively too. Only resolves to an agent the
    server actually offers; an unknown name is returned unchanged for the
    caller's own existence check.
    """
    if not name or name in AGENT_THEMES:
        return name
    try:
        from coding_model_server.config import Config
        canonical = Config.resolve_agent(name)
    except Exception:
        return name
    return canonical if canonical in AGENT_THEMES else name
